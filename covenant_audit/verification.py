"""Trusted verification: the only code that can turn extracted claims into verified artefacts.

Why seals: Pydantic models can always be constructed directly, so a boolean such as
`verified=True` proves nothing. Instead the verifier checks a claim against the stored
document and attaches a keyed HMAC (`Seal`) over the artefact's content hash. The engine
re-checks the seal at its input boundary, so a hand-built spec or fact, or a verified one
that was edited afterwards, is rejected. In production the key lives in a KMS/HSM and the
verifier is a separate service; here it is an in-process object holding a random key.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, Sequence

from pydantic import ValidationError

from .approval import ApprovalAttestationError, ApprovalSigner, LocalDevApprovalSigner, make_attestation, verify_spec_approvals
from .base import FrozenModel, Sha256Hex, content_hash, evolve, sha256_hex
from .errors import SealError, VerificationError
from .models import (
    Approval,
    CovenantSpec,
    FinancialFact,
    Seal,
    SourceSpan,
    SpecStatus,
    TieOutCheck,
)
from .policy import BankPolicy


class SpanVerificationError(VerificationError):
    """A quotation could not be grounded in the stored source document."""


class SpecNotApprovedError(VerificationError):
    """The engine was handed a spec that has not completed human approval."""


class UntrustedInputError(VerificationError):
    """Extractor output tried to supply a field that only trusted code may set."""


# --------------------------------------------------------------------------
# Source documents
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceDocument:
    doc_id: str
    sha256: str
    pages: tuple[str, ...]
    page_origins: tuple[Literal["native_text", "ocr"], ...]


def _document_hash(pages: Sequence[str]) -> str:
    # Offsets refer to this canonical page text, so the hash covers it. A production store
    # would also keep the original PDF bytes and hash those at ingestion.
    return sha256_hex("\f".join(pages))


class DocumentStore:
    """Write-once registry of canonical document text, keyed by doc_id and SHA-256."""

    def __init__(self) -> None:
        self._docs: dict[str, SourceDocument] = {}

    def register(
        self,
        doc_id: str,
        pages: Sequence[str],
        origin: Literal["native_text", "ocr"] | Sequence[Literal["native_text", "ocr"]] = "native_text",
    ) -> SourceDocument:
        if not pages:
            raise ValueError("a document needs at least one page")
        origins = (origin,) * len(pages) if isinstance(origin, str) else tuple(origin)
        if len(origins) != len(pages):
            raise ValueError("one text origin is required per page")
        doc = SourceDocument(doc_id, _document_hash(pages), tuple(pages), origins)  # type: ignore[arg-type]
        existing = self._docs.get(doc_id)
        if existing is not None and existing != doc:
            raise ValueError(f"document '{doc_id}' is already registered with different content")
        self._docs[doc_id] = doc
        return doc

    def get(self, doc_id: str) -> SourceDocument:
        try:
            return self._docs[doc_id]
        except KeyError:
            raise SpanVerificationError(f"unknown document '{doc_id}'") from None

    def locate(
        self,
        doc_id: str,
        page: int,
        quote: str,
        *,
        occurrence: int = 0,
        clause_ref: str | None = None,
        table_cell: tuple[int, int] | None = None,
        ocr_confidence: float | None = None,
    ) -> SourceSpan:
        """Build a span from a quotation by finding it in the stored page text.

        An extractor therefore only has to name the page and quote the text; code computes the
        offsets, so the LLM is never trusted to count characters."""
        doc = self.get(doc_id)
        if not 1 <= page <= len(doc.pages):
            raise SpanVerificationError(f"page {page} is outside document '{doc_id}'")
        text = doc.pages[page - 1]
        start = -1
        for _ in range(occurrence + 1):
            start = text.find(quote, start + 1)
            if start < 0:
                raise SpanVerificationError(f"quote not found on p.{page} of '{doc_id}': {quote!r}")
        return SourceSpan(
            doc_id=doc_id,
            doc_sha256=doc.sha256,
            page=page,
            char_start=start,
            char_end=start + len(quote),
            quote=quote,
            clause_ref=clause_ref,
            text_origin=doc.page_origins[page - 1],
            ocr_confidence=ocr_confidence if doc.page_origins[page - 1] == "ocr" else None,
            table_cell=table_cell,
        )


class VerifiedSpan(FrozenModel):
    """A quotation that the trusted verifier has checked against the stored document."""

    span: SourceSpan
    verifier_id: str
    attestation: Sha256Hex


# --------------------------------------------------------------------------
# Verifier
# --------------------------------------------------------------------------

_NUMBER = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")


def _amounts_in(quote: str) -> set[Decimal]:
    """Numbers printed in a quotation; parentheses mean negative, as in financial statements."""
    found: set[Decimal] = set()
    for token in _NUMBER.findall(quote):
        negative = token.startswith("(") or "-" in token
        digits = token.strip("()-").replace(",", "")
        try:
            value = Decimal(digits)
        except InvalidOperation:
            continue
        found.add(-value if negative else value)
    return found


class SourceVerifier:
    def __init__(
        self,
        store: DocumentStore,
        *,
        key: bytes | None = None,
        verifier_id: str = "local-verifier-v1",
        approval_signer: ApprovalSigner | None = None,
    ) -> None:
        self._store = store
        self._key = key if key is not None else secrets.token_bytes(32)
        self.verifier_id = verifier_id
        # Default: a development signer derived from the seal key. It is NOT identity authentication
        # (see approval.py); inject a KMS-backed signer in production.
        self.approval_signer: ApprovalSigner = approval_signer or LocalDevApprovalSigner(
            hmac.new(self._key, b"approval-signer-key", hashlib.sha256).digest()
        )

    # -- primitives ---------------------------------------------------------

    def _digest(self, domain: str, digest_of: str) -> str:
        return hmac.new(self._key, f"{domain}|{digest_of}".encode(), hashlib.sha256).hexdigest()

    def _seal(self, domain: str, digest_of: str) -> Seal:
        return Seal(verifier_id=self.verifier_id, digest=self._digest(domain, digest_of))

    def _check_seal(self, domain: str, digest_of: str, seal: Seal | None, what: str) -> None:
        if seal is None:
            raise SealError(f"{what} is not verified: no seal")
        if seal.verifier_id != self.verifier_id or not hmac.compare_digest(
            seal.digest, self._digest(domain, digest_of)
        ):
            raise SealError(f"{what} has an invalid seal (forged, or modified after verification)")

    # -- spans --------------------------------------------------------------

    def verify_span(self, span: SourceSpan) -> VerifiedSpan:
        """Ground one quotation: document exists, hash matches, page and offsets are valid,
        the quote equals the stored text exactly, and the text origin (native vs OCR) matches."""
        doc = self._store.get(span.doc_id)
        if span.doc_sha256 != doc.sha256:
            raise SpanVerificationError(
                f"document hash mismatch for '{span.doc_id}': span cites {span.doc_sha256[:12]}..., "
                f"stored document is {doc.sha256[:12]}..."
            )
        if span.page > len(doc.pages):
            raise SpanVerificationError(f"page {span.page} is outside document '{span.doc_id}'")
        text = doc.pages[span.page - 1]
        if span.char_end > len(text):
            raise SpanVerificationError(f"offsets exceed the length of p.{span.page} of '{span.doc_id}'")
        if text[span.char_start : span.char_end] != span.quote:
            raise SpanVerificationError(
                f"quote does not match the source text at p.{span.page}:{span.char_start}-{span.char_end}"
            )
        if span.text_origin != doc.page_origins[span.page - 1]:
            raise SpanVerificationError(
                f"span claims {span.text_origin} text but p.{span.page} of '{span.doc_id}' is "
                f"{doc.page_origins[span.page - 1]}"
            )
        return VerifiedSpan(
            span=span, verifier_id=self.verifier_id, attestation=self._digest("span", content_hash(span))
        )

    def check_span(self, verified: VerifiedSpan) -> None:
        self._check_seal(
            "span", content_hash(verified.span), Seal(verifier_id=verified.verifier_id, digest=verified.attestation),
            "span",
        )

    # -- specs --------------------------------------------------------------

    def verify_spec(self, spec: CovenantSpec) -> CovenantSpec:
        """Ground every quotation in a DRAFT spec and return it sealed as VERIFIED."""
        if spec.status is not SpecStatus.DRAFT:
            raise VerificationError(f"only DRAFT specs can be verified, got {spec.status.value}")
        failures: list[str] = []
        for span in spec.all_spans():
            try:
                self.verify_span(span)
            except SpanVerificationError as exc:
                failures.append(str(exc))
        if failures:
            raise SpanVerificationError(
                f"{len(failures)} quotation(s) failed verification: " + "; ".join(failures)
            )
        return evolve(spec, status=SpecStatus.VERIFIED, seal=self._seal("spec", spec.content_hash()))

    def check_spec(self, spec: CovenantSpec, *, require_approved: bool = True) -> None:
        """Re-check, at the point of use, that a spec is sealed, unmodified and (by default) approved."""
        self._check_seal("spec", spec.content_hash(), spec.seal, f"spec {spec.spec_id}")
        if spec.status is SpecStatus.DRAFT:
            raise SealError(f"spec {spec.spec_id} is a DRAFT")
        if require_approved and spec.status is not SpecStatus.APPROVED:
            raise SpecNotApprovedError(f"spec {spec.spec_id} is {spec.status.value}, not approved")
        if spec.status is SpecStatus.APPROVED:
            try:
                verify_spec_approvals(spec, self.approval_signer)
            except ApprovalAttestationError as exc:
                raise SealError(f"spec {spec.spec_id}: approval not valid: {exc}") from exc

    # -- facts --------------------------------------------------------------

    def verify_fact(self, fact: FinancialFact, policy: BankPolicy) -> FinancialFact:
        """Seal a fact only if its quote is grounded, the printed amount matches its value, every
        tie-out passes and every tie-out the policy requires for this input is present."""
        if fact.seal is not None:
            raise VerificationError(f"fact {fact.fact_id} already carries a seal; refusing to re-seal")
        self.verify_span(fact.source)
        printed = _amounts_in(fact.source.quote)
        if fact.value not in {amount * fact.reported_scale for amount in printed}:
            raise VerificationError(
                f"fact {fact.fact_id}: value {fact.value} is not an amount printed in the quote "
                f"{fact.source.quote!r} at scale {fact.reported_scale}"
            )
        failing = [t.name for t in fact.tie_outs if not t.passed]
        if failing:
            raise VerificationError(f"fact {fact.fact_id}: tie-out(s) failed: {', '.join(failing)}")
        for tie_out in fact.tie_outs:
            self._ground_tie_out(fact, tie_out, policy)
        present = {t.name for t in fact.tie_outs}
        missing = [n for n in policy.required_tie_outs_for(fact.input_name) if n not in present]
        if missing:
            raise VerificationError(
                f"fact {fact.fact_id}: required tie-out(s) missing for '{fact.input_name}': {', '.join(missing)}"
            )
        return evolve(fact, seal=self._seal("fact", fact.content_hash()))

    def _ground_tie_out(self, fact: FinancialFact, tie_out: TieOutCheck, policy: BankPolicy) -> None:
        """Each component must be quoted from the source, and `expected` must be their sum."""
        if not tie_out.component_spans:
            if policy.allow_ungrounded_tie_outs:
                return
            raise VerificationError(
                f"fact {fact.fact_id}: tie-out '{tie_out.name}' cites no source components, so its "
                f"expected value is only the extractor's claim"
            )
        total = Decimal(0)
        for span in tie_out.component_spans:
            self.verify_span(span)
            printed = _amounts_in(span.quote)
            if len(printed) != 1:
                raise VerificationError(
                    f"fact {fact.fact_id}: component quote {span.quote!r} must print exactly one amount"
                )
            total += next(iter(printed)) * fact.reported_scale
        if total != tie_out.expected:
            raise VerificationError(
                f"fact {fact.fact_id}: tie-out '{tie_out.name}' expected {tie_out.expected} but the quoted "
                f"components sum to {total}"
            )

    def check_fact(self, fact: FinancialFact) -> None:
        self._check_seal("fact", fact.content_hash(), fact.seal, f"fact {fact.fact_id}")
        if not fact.tie_outs_passed:
            raise SealError(f"fact {fact.fact_id} has a failing tie-out")


# --------------------------------------------------------------------------
# Human approval and the untrusted-input boundary
# --------------------------------------------------------------------------


def approve_spec(spec: CovenantSpec, approval: Approval, verifier: SourceVerifier) -> CovenantSpec:
    """Gate 1. Only a verified, unmodified spec can be approved, and the approver must be a
    person other than the extraction run. The trusted signer attests the decision (see approval.py)."""
    verifier.check_spec(spec, require_approved=False)
    if spec.status is not SpecStatus.VERIFIED:
        raise VerificationError(f"only VERIFIED specs can be approved, got {spec.status.value}")
    if approval.approver_id == spec.extractor_run_id:
        raise VerificationError("the extraction run cannot approve its own output")
    if approval.attestation is not None:
        raise VerificationError("a caller cannot supply an attestation; only the trusted signer mints it")
    try:
        attestation = make_attestation(spec, approval, verifier.approval_signer)
    except ApprovalAttestationError as exc:
        raise VerificationError(str(exc)) from exc
    return evolve(spec, status=SpecStatus.APPROVED, approvals=(evolve(approval, attestation=attestation),))


class ExtractionBatch(FrozenModel):
    """The complete schema of what a financial-facts extractor may return: facts, nothing else.
    There is no field for tool calls, so an instruction injected into a borrower document has
    nowhere to go; any extra key is a validation error."""

    facts: tuple[FinancialFact, ...]


def parse_untrusted_facts(json_text: str) -> tuple[FinancialFact, ...]:
    """Parse LLM output. Rejects unknown keys, floats, and any attempt to supply a seal."""
    try:
        batch = ExtractionBatch.model_validate_json(json_text)
    except ValidationError as exc:
        raise UntrustedInputError(f"extractor output rejected: {exc.error_count()} validation error(s): {exc}") from exc
    for fact in batch.facts:
        if fact.seal is not None:
            raise UntrustedInputError(f"extractor output supplied a seal for fact {fact.fact_id}")
    return batch.facts


def parse_untrusted_spec(json_text: str) -> CovenantSpec:
    """Parse LLM-extracted spec JSON. It must be a DRAFT with no seal and no approvals."""
    try:
        spec = CovenantSpec.model_validate_json(json_text)
    except ValidationError as exc:
        raise UntrustedInputError(f"spec draft rejected: {exc}") from exc
    if spec.status is not SpecStatus.DRAFT or spec.seal is not None or spec.approvals:
        raise UntrustedInputError("extractor output must be a DRAFT spec without a seal or approvals")
    return spec
