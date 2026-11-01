"""Signed approval attestations (gate 1).

An `Approval` record sitting inside a Pydantic object proves nothing: anyone can construct one. An
`ApprovalAttestation` binds, under a signature, exactly what was decided:

    approval id, spec content hash, approver id, approver role, decision, timestamp, edits

The signature is produced by an `ApprovalSigner`. Anything that changes the spec, the approver, the
role, the decision, the timestamp or the edit list invalidates the signature, and the registry and
engine re-verify it before they accept an approved spec.

Two signers are defined, deliberately kept apart:

* `LocalDevApprovalSigner` - an in-process HMAC for the prototype and tests. It proves the record was
  not altered after signing *by someone without the key*. It does NOT authenticate who the approver is:
  the approver identity is whatever string the caller passes. Do not describe it as identity assurance.
* `KmsApprovalSigner` - the production shape (asymmetric signature in a KMS/HSM, approver identity taken
  from a verified SSO/OIDC token, not from a caller-supplied string). It is a documented stub and
  refuses to run.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Literal, Protocol, runtime_checkable

from .base import FrozenModel, canonical_json, sha256_hex
from .errors import SealError
from .models import Approval, ApprovalAttestation, CovenantSpec


class ApprovalAttestationError(SealError):
    """An approval attestation is missing, altered, mismatched or not authorised."""


@runtime_checkable
class ApprovalSigner(Protocol):
    """Signing interface. A production implementation must authenticate the approver before signing."""

    signer_id: str
    production_grade: bool

    def sign(self, payload: bytes) -> str:
        """Return a hex signature over `payload`."""

    def verify(self, payload: bytes, signature: str) -> bool:
        """True iff `signature` is a valid signature over `payload` by this signer."""


class LocalDevApprovalSigner:
    """Development-only signer: HMAC-SHA256 with a local key. NOT identity authentication, NOT production."""

    production_grade = False

    def __init__(self, key: bytes, signer_id: str = "local-dev-signer") -> None:
        if len(key) < 16:
            raise ValueError("signing key must be at least 16 bytes")
        self._key = key
        self.signer_id = signer_id

    def sign(self, payload: bytes) -> str:
        return hmac.new(self._key, b"approval|" + payload, hashlib.sha256).hexdigest()

    def verify(self, payload: bytes, signature: str) -> bool:
        return hmac.compare_digest(self.sign(payload), signature)


class KmsApprovalSigner:
    """Production shape, intentionally not implemented in this prototype.

    A real implementation would: (1) require a verified identity token for the approver (SSO/OIDC) and
    derive `approver_id` and `role` from its claims instead of trusting a caller-supplied string;
    (2) sign the canonical payload with an asymmetric key held in a KMS/HSM so the verifier needs only the
    public key; (3) enforce key policy (who may sign, rotation, revocation) and log every signing call;
    (4) require N-of-M approvers above an exposure limit."""

    production_grade = True
    signer_id = "kms-not-configured"

    def sign(self, payload: bytes) -> str:  # pragma: no cover - documented stub
        raise NotImplementedError("KMS/HSM-backed approval signing is a production requirement, not implemented")

    def verify(self, payload: bytes, signature: str) -> bool:  # pragma: no cover - documented stub
        raise NotImplementedError("KMS/HSM-backed approval verification is a production requirement, not implemented")


# --------------------------------------------------------------------------
# Building and verifying attestations
# --------------------------------------------------------------------------


def attestation_payload(a: ApprovalAttestation) -> bytes:
    """Canonical bytes covered by the signature. Every field of the decision is in it."""
    return canonical_json(a, exclude=("signature",)).encode("utf-8")


def is_human_identity(approver_id: str) -> bool:
    """Prototype rule: automated identities are not approvers. Production relies on the IdP instead."""
    lowered = approver_id.strip().lower()
    return bool(lowered) and not lowered.startswith(("llm:", "agent", "extractor", "system:"))


def make_attestation(
    spec: CovenantSpec,
    approval: Approval,
    signer: ApprovalSigner,
    decision: Literal["approved", "rejected"] = "approved",
) -> ApprovalAttestation:
    """Sign a decision about `spec` by `approval.approver_id`. Refuses automated or self-approval."""
    if approval.approver_id == spec.extractor_run_id or not is_human_identity(approval.approver_id):
        raise ApprovalAttestationError(
            f"'{approval.approver_id}' cannot approve: the extraction run, and automated identities, may not approve its own output"
        )
    spec_hash = spec.content_hash()
    approval_id = "APR-" + sha256_hex(f"{spec_hash}|{approval.approver_id}|{approval.approved_at.isoformat()}|{decision}")[:20]
    unsigned = ApprovalAttestation(
        approval_id=approval_id,
        spec_hash=spec_hash,
        approver_id=approval.approver_id,
        role=approval.role,
        decision=decision,
        approved_at=approval.approved_at,
        edits_from_draft=approval.edits_from_draft,
        signer_id=signer.signer_id,
        signature="0" * 64,
    )
    signature = signer.sign(attestation_payload(unsigned))
    return ApprovalAttestation.model_validate({**dict(unsigned), "signature": signature})


def verify_approval(spec: CovenantSpec, approval: Approval, signer: ApprovalSigner) -> None:
    """Raise ApprovalAttestationError unless `approval` is a valid, unaltered, signed *approval of this exact spec*."""
    att = approval.attestation
    if att is None:
        raise ApprovalAttestationError(f"approval by '{approval.approver_id}' has no signed attestation")
    if att.signer_id != signer.signer_id:
        raise ApprovalAttestationError(f"attestation was signed by unknown signer '{att.signer_id}'")
    if not signer.verify(attestation_payload(att), att.signature):
        raise ApprovalAttestationError("approval attestation signature is invalid (the record was altered or forged)")
    if att.decision != "approved":
        raise ApprovalAttestationError(f"attestation records decision '{att.decision}', not 'approved'")
    if att.spec_hash != spec.content_hash():
        raise ApprovalAttestationError("attestation was signed for different spec content")
    outer = (approval.approver_id, approval.role, approval.approved_at, approval.edits_from_draft)
    signed = (att.approver_id, att.role, att.approved_at, att.edits_from_draft)
    if outer != signed:
        raise ApprovalAttestationError("approval fields differ from what was signed")
    if att.approver_id == spec.extractor_run_id or not is_human_identity(att.approver_id):
        raise ApprovalAttestationError("the extraction run cannot approve its own output")


def verify_spec_approvals(spec: CovenantSpec, signer: ApprovalSigner) -> None:
    if not spec.approvals:
        raise ApprovalAttestationError(f"spec {spec.spec_id} has no approvals")
    for approval in spec.approvals:
        verify_approval(spec, approval, signer)


class SignedRejection(FrozenModel):
    """A reviewer's signed refusal. Kept in the audit trail; it never makes a spec APPROVED."""

    attestation: ApprovalAttestation
    reason: str


def reject_spec(spec: CovenantSpec, approval: Approval, reason: str, signer: ApprovalSigner) -> SignedRejection:
    if len(reason.strip()) < 10:
        raise ApprovalAttestationError("a rejection needs a written reason")
    return SignedRejection(attestation=make_attestation(spec, approval, signer, decision="rejected"), reason=reason)

