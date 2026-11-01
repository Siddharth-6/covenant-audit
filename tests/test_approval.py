"""Approval attestation: signed, bound to the spec content, and re-verified by the registry and engine.

Each test states its expected result and why."""

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from covenant_audit.approval import (
    ApprovalAttestationError,
    KmsApprovalSigner,
    LocalDevApprovalSigner,
    attestation_payload,
    reject_spec,
)
from covenant_audit.base import evolve
from covenant_audit.ledger import Ledger
from covenant_audit.models import Approval, CovenantKind, CovenantSpec
from covenant_audit.registry import SpecRegistry
from covenant_audit.verification import SealError, SourceVerifier, SpecNotApprovedError, VerificationError, approve_spec

from .factory import NOW, World

KIND = CovenantKind.MAX_TOTAL_NET_LEVERAGE


@pytest.fixture()
def world() -> World:
    return World()


def request(approver="credit.officer@bank", **kw) -> Approval:
    return Approval(approver_id=approver, role="credit_officer", approved_at=NOW, **kw)


def tampered(spec: CovenantSpec, **changes) -> CovenantSpec:
    """Replace the first approval with an altered copy (keeping the spec's seal intact)."""
    return evolve(spec, approvals=(evolve(spec.approvals[0], **changes), *spec.approvals[1:]))


def tampered_attestation(spec: CovenantSpec, **changes) -> CovenantSpec:
    att = evolve(spec.approvals[0].attestation, **changes)
    return tampered(spec, attestation=att)


def test_valid_approval_is_signed_bound_and_accepted(world):
    """Expected: the approved spec carries an attestation binding spec hash, approver, role, decision and time; the
    verifier, the registry and the ledger all accept it, and the ledger records the approval as its own entry.
    Why: the signer signs the canonical payload of exactly those fields."""
    spec = world.build_spec()
    att = spec.approvals[0].attestation
    assert (att.spec_hash, att.approver_id, att.role, att.decision, att.approved_at) == (
        spec.content_hash(), "credit.officer@bank", "credit_officer", "approved", NOW)
    assert att.approval_id.startswith("APR-") and att.signer_id == world.verifier.approval_signer.signer_id
    world.verifier.check_spec(spec)
    ledger = Ledger()
    SpecRegistry(world.verifier, ledger).register(spec, NOW)
    assert ledger.approvals_for(spec.spec_id) == [att]
    ledger.verify_chain()


def test_attestation_survives_a_json_round_trip(world):
    """Expected: an approved spec saved to JSON and reloaded still verifies. Why: approvals are persisted, and the
    signature covers canonical content, not Python object identity."""
    spec = world.build_spec()
    reloaded = CovenantSpec.model_validate_json(spec.model_dump_json())
    world.verifier.check_spec(reloaded)


@pytest.mark.parametrize("field,value", [("approver_id", "mallory@evil"), ("role", "legal_reviewer")])
def test_modified_approver_identity_or_role_invalidates_the_attestation(world, field, value):
    """Expected: SealError when the approver id or role is changed in the approval alone, in the attestation alone, or in both.
    Why: both are inside the signed payload, and the outer record must match what was signed."""
    spec = world.build_spec()
    for broken in (tampered(spec, **{field: value}), tampered_attestation(spec, **{field: value})):
        with pytest.raises(SealError):
            world.verifier.check_spec(broken)
        with pytest.raises(SealError):
            SpecRegistry(world.verifier).register(broken)
    both = tampered(tampered_attestation(spec, **{field: value}), **{field: value})
    with pytest.raises(SealError, match="signature is invalid"):
        world.verifier.check_spec(both)


def test_modified_spec_hash_in_the_attestation_is_detected(world):
    """Expected: SealError for an altered spec_hash; and for a perfectly valid attestation copied from a different spec.
    Why: the hash is signed; and an attestation only counts for the exact content it was signed for."""
    spec = world.build_spec()
    with pytest.raises(SealError, match="signature is invalid"):
        world.verifier.check_spec(tampered_attestation(spec, spec_hash="0" * 64))
    other = world.build_spec(spec_id="SPEC-OTHER", thresholds=((date(2025, 1, 1), None, "4.00"),))
    assert other.content_hash() != spec.content_hash()
    transplanted = evolve(spec, approvals=other.approvals)
    with pytest.raises(SealError, match="different spec content"):
        world.verifier.check_spec(transplanted)


def test_modified_approval_decision_is_detected(world):
    """Expected: SealError when a 'rejected' attestation is flipped to 'approved' (and the reverse); a correctly signed
    rejection cannot approve a spec either.
    Why: the decision is signed, and only decision 'approved' is accepted."""
    spec = world.build_spec()
    with pytest.raises(SealError, match="signature is invalid"):
        world.verifier.check_spec(tampered_attestation(spec, decision="rejected"))
    draft = world.verifier.verify_spec(world.build_spec(approve=False))
    rejection = reject_spec(draft, request(), "the cap is 15 percent, not 25 percent", world.verifier.approval_signer)
    flipped = evolve(rejection.attestation, decision="approved")
    forged = evolve(draft, status="approved", approvals=(evolve(request(), attestation=flipped),))
    with pytest.raises(SealError, match="signature is invalid"):
        world.verifier.check_spec(forged)
    honest = evolve(draft, status="approved", approvals=(evolve(request(), attestation=rejection.attestation),))
    with pytest.raises(SealError, match="decision 'rejected'"):
        world.verifier.check_spec(honest)


def test_missing_signature_is_refused(world):
    """Expected: an approved spec whose approval has no attestation is refused by check_spec and the registry.
    Why: an approval record without a signature is only a claim."""
    spec = world.build_spec()
    unsigned = tampered(spec, attestation=None)
    with pytest.raises(SealError, match="no signed attestation"):
        world.verifier.check_spec(unsigned)
    with pytest.raises(SealError):
        SpecRegistry(world.verifier).register(unsigned)
    with pytest.raises(ValidationError, match="requires a human approval"):
        evolve(spec, approvals=())  # an APPROVED spec with no approval record cannot even be constructed


def test_extractor_cannot_approve_its_own_spec(world):
    """Expected: VerificationError for the extraction run's own id, for automated identities, and for a caller-supplied
    attestation. Why: the extractor must never approve its output, and only the trusted signer mints attestations."""
    verified = world.verifier.verify_spec(world.build_spec(approve=False))
    with pytest.raises(VerificationError, match="own output"):
        approve_spec(verified, request("agent-run-1"), world.verifier)
    for automated in ("llm:openrouter/some-model", "agent-7", "extractor-bot", "system:scheduler"):
        with pytest.raises(VerificationError, match="cannot approve"):
            approve_spec(verified, request(automated), world.verifier)
    spec = world.build_spec()
    with pytest.raises(VerificationError, match="cannot supply an attestation"):
        approve_spec(verified, request(attestation=spec.approvals[0].attestation), world.verifier)


@pytest.mark.parametrize("changes", [
    {"approved_at": datetime(2030, 1, 1, tzinfo=timezone.utc)},
    {"edits_from_draft": ("/thresholds/0/level/value",)},
])
def test_tampered_approval_record_is_detected(world, changes):
    """Expected: SealError when the timestamp or the list of reviewer edits is altered in the approval.
    Why: both are signed; the outer record must equal the signed values."""
    with pytest.raises(SealError, match="differ from what was signed"):
        world.verifier.check_spec(tampered(world.build_spec(), **changes))


def test_attestation_from_another_signer_or_key_is_refused(world):
    """Expected: SealError for an attestation signed under a different key or signer id.
    Why: only the configured signer's signatures verify."""
    spec = world.build_spec()
    other = SourceVerifier(world.store, key=world.verifier._key, approval_signer=LocalDevApprovalSigner(b"x" * 32, "someone-else"))
    with pytest.raises(SealError, match="unknown signer"):
        other.check_spec(spec)
    same_id_other_key = SourceVerifier(world.store, key=world.verifier._key, approval_signer=LocalDevApprovalSigner(b"y" * 32, world.verifier.approval_signer.signer_id))
    with pytest.raises(SealError, match="signature is invalid"):
        same_id_other_key.check_spec(spec)


def test_editing_the_spec_after_approval_breaks_both_seal_and_approval(world):
    """Expected: SealError. Why: the spec seal and the approval both cover the content hash."""
    spec = world.build_spec()
    edited = evolve(spec, scope_notes=("quietly added note",))
    with pytest.raises(SealError):
        world.verifier.check_spec(edited)


def test_previous_versions_and_their_approval_records_are_preserved(world):
    """Expected: after an amendment is registered both versions are retained, each still verifies, and the ledger holds
    both approval attestations. Why: versions are immutable; approvals are append-only audit records."""
    ledger = Ledger()
    registry = SpecRegistry(world.verifier, ledger)
    v1 = world.build_spec()
    v2 = world.build_spec(spec_id="SPEC-2", version=2, supersedes="SPEC-1", addback_pct="0.15",
                          effective_from=date(2025, 7, 1), executed_on=date(2025, 6, 20))
    registry.register(v1, NOW)
    registry.register(v2, NOW)
    kept = registry.versions("CA-1", KIND)
    assert [s.spec_id for s in kept] == ["SPEC-1", "SPEC-2"]
    for s in kept:
        world.verifier.check_spec(s)
        assert ledger.approvals_for(s.spec_id) == [s.approvals[0].attestation]
    ledger.verify_chain()


def test_rejection_is_signed_recorded_and_never_approves(world):
    """Expected: a rejection needs a reason, is signed, is appended to the ledger, and leaves the spec unapproved.
    Why: refusals are part of the audit trail, and a rejected spec must not be usable."""
    draft = world.verifier.verify_spec(world.build_spec(approve=False))
    with pytest.raises(ApprovalAttestationError, match="written reason"):
        reject_spec(draft, request(), "no", world.verifier.approval_signer)
    rejection = reject_spec(draft, request(), "threshold does not match the amendment", world.verifier.approval_signer)
    assert rejection.attestation.decision == "rejected"
    ledger = Ledger()
    ledger.append_rejection(rejection)
    assert ledger.rejections() == [rejection]
    ledger.verify_chain()
    with pytest.raises(SpecNotApprovedError):
        world.verifier.check_spec(draft, require_approved=True)


def test_dev_signer_is_clearly_not_production_grade(world):
    """Expected: the dev signer reports production_grade=False; the KMS signer refuses to run.
    Why: an in-process HMAC key must not be presented as identity authentication or production signing."""
    assert LocalDevApprovalSigner(b"k" * 16).production_grade is False
    assert world.verifier.approval_signer.production_grade is False
    kms = KmsApprovalSigner()
    assert kms.production_grade is True
    with pytest.raises(NotImplementedError):
        kms.sign(b"payload")
    with pytest.raises(ValueError, match="at least 16"):
        LocalDevApprovalSigner(b"short")


def test_attestation_payload_covers_every_decision_field(world):
    """Expected: changing any of id, spec hash, approver, role, decision, time or edits changes the signed payload.
    Why: nothing about the decision may be alterable without invalidating the signature."""
    att = world.build_spec().approvals[0].attestation
    base = attestation_payload(att)
    for change in (
        {"approval_id": "APR-x"}, {"spec_hash": "1" * 64}, {"approver_id": "z"}, {"role": "legal_reviewer"},
        {"decision": "rejected"}, {"approved_at": datetime(2031, 1, 1, tzinfo=timezone.utc)}, {"edits_from_draft": ("/a",)},
    ):
        assert attestation_payload(evolve(att, **change)) != base, change
