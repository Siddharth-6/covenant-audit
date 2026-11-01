"""Human approval workflow: what the reviewer sees, what blocks approval, and how a decision is recorded.

The reviewer is shown the spec as the *engine* will run it (generated from the AST by `explain.py`, no model
involved), every source quotation with document, page and offsets, the model's unresolved items, the
validation failures, and anything the system could not read. Approval is refused, not merely warned about,
when any of these holds:

* the response produced no spec, or the spec failed schema validation, quotation verification or the
  semantic checks;
* the model reported an unresolved item (ambiguity, missing definition, unresolved cross-reference, missing
  amendment);
* a known amendment document was not among those the extractor read.

Approval is an explicit act: the reviewer must type the first 12 characters of the content hash of the spec
they were shown, so an approval cannot be given to a spec other than the one displayed.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Sequence

from .approval import ApprovalAttestationError, LocalDevApprovalSigner, SignedRejection, reject_spec
from .explain import explain_spec
from .extraction import ExtractionOutcome, describe_provenance
from .ingest import KnownAmendment, SourceManifest
from .models import Approval, CovenantSpec
from .spec_checks import Finding, check_spec, errors
from .verification import DocumentStore, SourceVerifier, VerificationError, approve_spec


class ApprovalRefused(Exception):
    """Approval was blocked. The message lists every reason."""


@dataclass(frozen=True)
class ReviewPackage:
    outcome: ExtractionOutcome
    spec: CovenantSpec | None  # VERIFIED and sealed; None if there is nothing approvable
    findings: tuple[Finding, ...]
    verification_error: str | None
    missing_amendments: tuple[KnownAmendment, ...]

    @property
    def blocking(self) -> list[str]:
        reasons = list(self.outcome.blocking_reasons)
        if self.verification_error:
            reasons.append(f"verification failed: {self.verification_error}")
        reasons += [f"semantic check {f.code}: {f.message}" for f in errors(list(self.findings))]
        reasons += [
            f"known amendment {a.doc_id} ({a.description}, {a.dated}) was not among the documents read; "
            f"terms may have changed" for a in self.missing_amendments
        ]
        if self.spec is None and not reasons:
            reasons.append("no verified spec is available")
        return reasons

    @property
    def approvable(self) -> bool:
        return self.spec is not None and not self.blocking


def prepare_review(outcome: ExtractionOutcome, verifier: SourceVerifier, manifests: Sequence[SourceManifest] = ()) -> ReviewPackage:
    """Independently re-verify every quotation (the extractor's own resolution is not trusted), run the
    semantic checks, and list known amendments that were not read."""
    spec: CovenantSpec | None = None
    findings: tuple[Finding, ...] = ()
    verification_error: str | None = None
    if outcome.spec is not None:
        findings = tuple(check_spec(outcome.spec))
        try:
            spec = verifier.verify_spec(outcome.spec)
        except VerificationError as exc:
            verification_error = str(exc)
    read = set(outcome.source_doc_ids)
    missing = tuple(a for m in manifests if m.doc_id in read for a in m.known_amendments if a.doc_id not in read)
    return ReviewPackage(outcome, spec, findings, verification_error, missing)


def render_review(pkg: ReviewPackage) -> str:
    o = pkg.outcome
    lines = [
        "=" * 100,
        "SPECIFICATION REVIEW",
        f"Provenance : {describe_provenance(o)}",
        f"Run id     : {o.run_id}      prompt sha256: {o.prompt_sha256[:16]}",
        f"Documents  : {', '.join(o.source_doc_ids)}",
        "=" * 100,
    ]
    blocking = pkg.blocking
    if blocking:
        lines.append(f"APPROVAL BLOCKED ({len(blocking)} reason(s)):")
        lines += [f"  [X] {b}" for b in blocking]
    else:
        lines.append("No blocking issues. Approval is possible, but verification proves provenance, not that the reading is right.")
    lines.append("")
    spec = pkg.spec or o.spec
    if spec is not None:
        lines.append(explain_spec(spec))
        lines.append("")
        lines.append(f"SPEC CONTENT HASH  {spec.content_hash()}")
    warnings = [f for f in pkg.findings if f.severity == "warning"]
    if warnings:
        lines += ["", "WARNINGS (not blocking)"] + [f"  [!] {f.code}: {f.message}" for f in warnings]
    if o.unresolved:
        lines += ["", "UNRESOLVED BY THE MODEL"] + [f"  [?] ({u.kind}) {u.description}" + (f" [{u.reference}]" if u.reference else "") for u in o.unresolved]
    if o.terms_not_provided:
        lines += ["", "TERMS THE EXCERPTS REFERENCE BUT THE EXTRACTOR WAS NOT SHOWN", "  " + ", ".join(o.terms_not_provided)]
    return "\n".join(lines)


def approve(
    pkg: ReviewPackage, *, approver_id: str, role: Literal["credit_officer", "legal_reviewer"], confirm: str,
    verifier: SourceVerifier, now: datetime | None = None, edits: Sequence[str] = (),
) -> CovenantSpec:
    """Record an explicit approval. Refuses (ApprovalRefused) if anything blocks, or the confirmation does not
    match the displayed spec's hash."""
    if not pkg.approvable or pkg.spec is None:
        raise ApprovalRefused("approval blocked:\n  - " + "\n  - ".join(pkg.blocking))
    expected = pkg.spec.content_hash()[:12]
    if confirm.strip().lower() != expected:
        raise ApprovalRefused(f"confirmation does not match the reviewed spec (expected the first 12 hash characters: {expected})")
    approval = Approval(approver_id=approver_id, role=role, approved_at=now or datetime.now(timezone.utc), edits_from_draft=tuple(edits))
    try:
        return approve_spec(pkg.spec, approval, verifier)
    except (VerificationError, ApprovalAttestationError) as exc:
        raise ApprovalRefused(str(exc)) from exc


def reject(
    pkg: ReviewPackage, *, approver_id: str, role: Literal["credit_officer", "legal_reviewer"], reason: str,
    verifier: SourceVerifier, now: datetime | None = None,
) -> SignedRejection:
    if pkg.spec is None:
        raise ApprovalRefused("there is no verified spec to reject; the extraction itself already failed")
    approval = Approval(approver_id=approver_id, role=role, approved_at=now or datetime.now(timezone.utc))
    return reject_spec(pkg.spec, approval, reason, verifier.approval_signer)


# --------------------------------------------------------------------------
# Persistence (a directory of JSON files; no database)
# --------------------------------------------------------------------------


class StateDir:
    """CLI state: the pending extraction, approved specs with their attestations, rejections, and a
    DEVELOPMENT keyring. The keys are local random bytes on disk: fine for a prototype, not identity
    authentication or production key custody."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def save_outcome(self, outcome: ExtractionOutcome) -> Path:
        return self._write("pending_extraction.json", outcome.model_dump_json(indent=2))

    def load_outcome(self) -> ExtractionOutcome:
        path = self.root / "pending_extraction.json"
        if not path.exists():
            raise FileNotFoundError("no pending extraction; run the 'extract' command first")
        return ExtractionOutcome.model_validate_json(path.read_text(encoding="utf-8"))

    def save_approved(self, spec: CovenantSpec) -> Path:
        return self._write(f"{spec.spec_id}.approved.json", spec.model_dump_json(indent=2))

    def load_approved(self, spec_id: str | None = None) -> CovenantSpec:
        files = sorted(self.root.glob("*.approved.json"))
        if spec_id:
            files = [f for f in files if f.name == f"{spec_id}.approved.json"]
        if not files:
            raise FileNotFoundError("no approved spec on disk; run 'approve' first")
        return CovenantSpec.model_validate_json(files[-1].read_text(encoding="utf-8"))

    def save_rejection(self, rejection: SignedRejection) -> Path:
        return self._write(f"{rejection.attestation.approval_id}.rejected.json", rejection.model_dump_json(indent=2))

    def verifier(self, store: DocumentStore) -> SourceVerifier:
        keyfile = self.root / "dev_keys.json"
        if keyfile.exists():
            keys = json.loads(keyfile.read_text())
        else:
            keys = {"seal_key": secrets.token_hex(32), "approval_key": secrets.token_hex(32)}
            keyfile.write_text(json.dumps(keys))
            keyfile.chmod(0o600)
        return SourceVerifier(
            store, key=bytes.fromhex(keys["seal_key"]),
            approval_signer=LocalDevApprovalSigner(bytes.fromhex(keys["approval_key"]), signer_id="local-dev-signer"),
        )
