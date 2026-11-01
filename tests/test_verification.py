"""Trusted verification: spans, seals, facts, approval, and the untrusted-input boundary.

Each test states its expected result and why."""

import json
from datetime import date
from decimal import Decimal

import pytest

from covenant_audit.base import evolve
from covenant_audit.models import Approval, FinancialFact, Seal, SourceSpan, SpecStatus, TieOutCheck
from covenant_audit.verification import (
    SealError,
    SourceVerifier,
    SpanVerificationError,
    SpecNotApprovedError,
    UntrustedInputError,
    VerificationError,
    approve_spec,
    parse_untrusted_facts,
    parse_untrusted_spec,
)

from .factory import NOW, SAMPLE_VALUES, World

D = Decimal


@pytest.fixture()
def world() -> World:
    return World()


# ---------------------------------------------------------------- spans


def test_extracted_span_has_no_verification_state(world):
    """Expected: SourceSpan has no `verified` field and rejects one.
    Why: an extracted quotation must start unverified and cannot self-declare otherwise."""
    span = world.span(("CA-1", 2, "4.50 to 1.00"))
    assert not hasattr(span, "verified")
    with pytest.raises(Exception, match="verified"):
        SourceSpan(**{**dict(span), "verified": True})


def test_verify_span_grounds_a_real_quote(world):
    """Expected: a VerifiedSpan wrapping the same span, with a valid attestation.
    Why: the quote, hash, page and offsets all match the stored document."""
    span = world.span(("CA-1", 2, "4.50 to 1.00"))
    verified = world.verifier.verify_span(span)
    assert verified.span == span
    world.verifier.check_span(verified)


def test_verified_span_attestation_cannot_be_forged(world):
    """Expected: SealError when the attestation is faked or the span is swapped after verification.
    Why: the attestation is an HMAC over the span content; constructing the model by hand has no key."""
    span = world.span(("CA-1", 2, "4.50 to 1.00"))
    verified = world.verifier.verify_span(span)
    forged = evolve(verified, attestation="0" * 64)
    with pytest.raises(SealError):
        world.verifier.check_span(forged)
    other = world.span(("CA-1", 2, "4.00 to 1.00"))
    swapped = evolve(verified, span=other)
    with pytest.raises(SealError):
        world.verifier.check_span(swapped)


def test_hallucinated_quote_fails(world):
    """Expected: SpanVerificationError 'does not match'. Why: the cited text is not at those offsets."""
    span = world.span(("CA-1", 2, "4.50 to 1.00"))
    bad = SourceSpan(**{**dict(span), "quote": "5.50 to 1.00"})
    with pytest.raises(SpanVerificationError, match="does not match"):
        world.verifier.verify_span(bad)


def test_wrong_document_hash_fails(world):
    """Expected: SpanVerificationError 'hash mismatch'.
    Why: the span claims a different version of the document than the one in the store."""
    span = world.span(("CA-1", 2, "4.50 to 1.00"))
    bad = SourceSpan(**{**dict(span), "doc_sha256": "f" * 64})
    with pytest.raises(SpanVerificationError, match="hash mismatch"):
        world.verifier.verify_span(bad)


def test_unknown_document_page_and_offsets_fail(world):
    """Expected: each invalid location raises SpanVerificationError.
    Why: unknown document, a page beyond the end, and offsets beyond the page are all ungrounded."""
    span = world.span(("CA-1", 2, "4.50 to 1.00"))
    with pytest.raises(SpanVerificationError, match="unknown document"):
        world.verifier.verify_span(SourceSpan(**{**dict(span), "doc_id": "NOPE"}))
    with pytest.raises(SpanVerificationError, match="outside document"):
        world.verifier.verify_span(SourceSpan(**{**dict(span), "page": 9}))
    far = SourceSpan(**{**dict(span), "char_start": 99_990, "char_end": 99_990 + len(span.quote)})
    with pytest.raises(SpanVerificationError, match="exceed"):
        world.verifier.verify_span(far)


def test_ocr_claim_must_match_stored_origin(world):
    """Expected: SpanVerificationError when a span claims OCR text on a native-text page.
    Why: the origin decides how much to trust the text; an extractor must not choose it."""
    span = world.span(("CA-1", 2, "4.50 to 1.00"))
    with pytest.raises(SpanVerificationError, match="claims ocr"):
        world.verifier.verify_span(SourceSpan(**{**dict(span), "text_origin": "ocr", "ocr_confidence": 0.99}))


def test_ocr_spans_require_confidence_and_native_spans_forbid_it(world):
    """Expected: ValidationError for OCR without confidence, and for native text with confidence.
    Why: OCR confidence requirements are preserved and cannot be attached to clean text."""
    span = world.span(("CA-1", 2, "4.50 to 1.00"))
    with pytest.raises(Exception, match="ocr_confidence"):
        SourceSpan(**{**dict(span), "text_origin": "ocr"})
    with pytest.raises(Exception, match="native-text"):
        SourceSpan(**{**dict(span), "ocr_confidence": 0.5})


def test_ocr_document_spans_verify_and_keep_confidence(world):
    """Expected: an OCR page verifies when the span says ocr and carries a confidence.
    Why: origin matches the store and the confidence requirement is met."""
    world.store.register("SCAN-1", ("Total debt: 540,000,000",), origin="ocr")
    span = world.store.locate("SCAN-1", 1, "540,000,000", ocr_confidence=0.82)
    assert span.text_origin == "ocr" and span.ocr_confidence == 0.82
    world.verifier.verify_span(span)


def test_document_store_is_write_once(world):
    """Expected: re-registering a doc_id with different text raises. Why: sources are immutable."""
    with pytest.raises(ValueError, match="different content"):
        world.store.register("CA-1", ("tampered",))
    world.store.register("CA-1", tuple(world.store.get("CA-1").pages))  # identical re-registration is fine


def test_locate_finds_offsets_and_reports_missing_quotes(world):
    """Expected: locate returns correct offsets; a missing quote raises. Why: code, not the LLM, counts characters."""
    span = world.store.locate("CA-1", 2, "4.00 to 1.00")
    assert world.store.get("CA-1").pages[1][span.char_start : span.char_end] == "4.00 to 1.00"
    with pytest.raises(SpanVerificationError, match="not found"):
        world.store.locate("CA-1", 2, "9.99 to 1.00")


# ---------------------------------------------------------------- specs and seals


def test_spec_lifecycle_draft_verified_approved(world):
    """Expected: DRAFT -> VERIFIED (sealed) -> APPROVED, and check_spec passes only at APPROVED.
    Why: the engine accepts only human-approved specs."""
    draft = world.build_spec(approve=False)
    verified = world.verifier.verify_spec(draft)
    assert verified.status is SpecStatus.VERIFIED
    with pytest.raises(SpecNotApprovedError):
        world.verifier.check_spec(verified)
    approved = approve_spec(verified, Approval(approver_id="officer", role="credit_officer", approved_at=NOW), world.verifier)
    world.verifier.check_spec(approved)


def test_spec_with_ungrounded_quote_cannot_be_verified(world):
    """Expected: SpanVerificationError naming the failure. Why: one bad quotation blocks the whole spec."""
    draft = world.build_spec(approve=False)
    bad_span = SourceSpan(**{**dict(draft.thresholds[0].level.spans[0]), "quote": "9.50 to 1.00"})
    bad_level = evolve(draft.thresholds[0].level, spans=(bad_span,))
    bad = evolve(draft, thresholds=(evolve(draft.thresholds[0], level=bad_level), *draft.thresholds[1:]))
    with pytest.raises(SpanVerificationError, match="failed verification"):
        world.verifier.verify_spec(bad)


def test_hand_built_spec_cannot_claim_verified(world):
    """Expected: SealError. Why: constructing a VERIFIED/APPROVED spec directly with a made-up seal
    passes model validation but fails the keyed check at the trust boundary."""
    draft = world.build_spec(approve=False)
    forged = evolve(
        draft, status=SpecStatus.APPROVED, seal=Seal(verifier_id=world.verifier.verifier_id, digest="0" * 64),
        approvals=(Approval(approver_id="mallory", role="credit_officer", approved_at=NOW),),
    )
    with pytest.raises(SealError, match="invalid seal"):
        world.verifier.check_spec(forged)


def test_editing_an_approved_spec_breaks_its_seal(world):
    """Expected: SealError after changing the covenant level on an approved spec, keeping its seal.
    Why: the seal covers the content hash; any edit (a new 'version' is the only legal change) is detected."""
    spec = world.build_spec()
    step = spec.thresholds[0]
    edited_step = evolve(step, level=evolve(step.level, value=D("9.00")))
    edited = evolve(spec, thresholds=(edited_step, *spec.thresholds[1:]))
    with pytest.raises(SealError, match="invalid seal"):
        world.verifier.check_spec(edited)


def test_seal_from_another_verifier_key_is_rejected(world):
    """Expected: SealError. Why: only the holder of the key can mint seals."""
    other = SourceVerifier(world.store, key=b"a-different-key")
    with pytest.raises(SealError):
        other.check_spec(world.build_spec())


def test_approval_rules(world):
    """Expected: a DRAFT or already-approved spec cannot be approved, nor can the extraction run approve itself.
    Why: gate 1 needs a verified spec and a person other than the extractor."""
    draft = world.build_spec(approve=False)
    approval = Approval(approver_id="officer", role="credit_officer", approved_at=NOW)
    with pytest.raises(SealError):
        approve_spec(draft, approval, world.verifier)
    verified = world.verifier.verify_spec(draft)
    with pytest.raises(VerificationError, match="own output"):
        approve_spec(verified, Approval(approver_id="agent-run-1", role="credit_officer", approved_at=NOW), world.verifier)
    approved = approve_spec(verified, approval, world.verifier)
    with pytest.raises(VerificationError, match="only VERIFIED"):
        approve_spec(approved, approval, world.verifier)


# ---------------------------------------------------------------- facts


def test_fact_with_passing_tie_outs_is_sealed(world):
    """Expected: sealed facts that pass check_fact. Why: grounded quote, printed amount, tie-outs all hold."""
    facts = world.package(SAMPLE_VALUES)
    for f in facts.values():
        world.verifier.check_fact(f)


def test_failed_tie_out_blocks_verification(world):
    """Expected: VerificationError naming the tie-out. Why: debt components sum to 600M but total is 540M."""
    with pytest.raises(VerificationError, match="tie-out.*failed"):
        world.package(SAMPLE_VALUES, tie_out_parts={"total_debt": (D("300000000"), D("300000000"))})


def test_required_tie_out_must_be_present(world):
    """Expected: VerificationError 'required tie-out(s) missing'.
    Why: policy requires a debt tie-out; omitting it must not be a way around the check."""
    facts = world.package(SAMPLE_VALUES, seal=False)
    bare = evolve(facts["total_debt"], tie_outs=())
    with pytest.raises(VerificationError, match="required tie-out"):
        world.verifier.verify_fact(bare, world.policy)


def test_fact_value_must_be_printed_in_its_quote(world):
    """Expected: VerificationError. Why: the fact says 541M but the grounded quote prints 540,000,000."""
    facts = world.package(SAMPLE_VALUES, seal=False)
    wrong = evolve(facts["total_debt"], value=D("541000000"), tie_outs=())
    with pytest.raises(VerificationError, match="not an amount printed"):
        world.verifier.verify_fact(wrong, evolve(world.policy, required_tie_outs=()))


def test_fact_scale_is_applied_when_matching_the_quote(world):
    """Expected: '540.0' printed at scale 1,000,000 verifies as 540,000,000; at scale 1 it does not.
    Why: statements print thousands/millions; reading the scale wrongly is a classic 1000x error."""
    world.store.register("FS-SCALE", ("Total debt (in millions): 540.0",))
    span = world.store.locate("FS-SCALE", 1, "Total debt (in millions): 540.0")
    base = dict(
        fact_id="F", input_name="total_debt", currency="USD", period_end=date(2025, 6, 30),
        origin="unaudited_fs", source=span, extractor_run_id="r",
    )
    policy = evolve(world.policy, required_tie_outs=())
    ok = FinancialFact(**base, value=D("540000000"), reported_scale=1_000_000)
    world.verifier.check_fact(world.verifier.verify_fact(ok, policy))
    wrong_scale = FinancialFact(**base, value=D("540000000"), reported_scale=1)
    with pytest.raises(VerificationError, match="not an amount printed"):
        world.verifier.verify_fact(wrong_scale, policy)


def test_parenthesised_amounts_are_negative(world):
    """Expected: '(12.5)' verifies as -12.5. Why: parentheses denote negatives in financial statements."""
    world.store.register("FS-NEG", ("Net income: (12.5)",))
    span = world.store.locate("FS-NEG", 1, "Net income: (12.5)")
    fact = FinancialFact(
        fact_id="F", input_name="base_ebitda", value=D("-12.5"), currency="USD", reported_scale=1,
        period_start=date(2024, 7, 1), period_end=date(2025, 6, 30), origin="unaudited_fs", source=span, extractor_run_id="r",
    )
    world.verifier.verify_fact(fact, evolve(world.policy, required_tie_outs=()))


def test_sealed_fact_cannot_be_edited_or_resealed(world):
    """Expected: editing a sealed fact breaks check_fact; sealing twice is refused.
    Why: the seal covers value, source and tie-outs."""
    fact = world.package(SAMPLE_VALUES)["cash"]
    with pytest.raises(SealError):
        world.verifier.check_fact(evolve(fact, value=D("900000000")))
    with pytest.raises(VerificationError, match="already carries a seal"):
        world.verifier.verify_fact(fact, world.policy)


def test_unsealed_fact_is_not_verified(world):
    """Expected: SealError 'no seal'. Why: a fact straight from the extractor is not trusted."""
    fact = world.package(SAMPLE_VALUES, seal=False)["cash"]
    with pytest.raises(SealError, match="no seal"):
        world.verifier.check_fact(fact)


# ---------------------------------------------------------------- untrusted-input boundary


def _fact_json(world, **tweak) -> str:
    fact = world.package(SAMPLE_VALUES, seal=False)["cash"]
    data = json.loads(fact.model_dump_json())
    data.update(tweak)
    return json.dumps({"facts": [data]})


def test_extractor_output_parses_when_clean(world):
    """Expected: one unsealed fact. Why: a well-formed extraction is accepted as a *claim*."""
    (fact,) = parse_untrusted_facts(_fact_json(world))
    assert fact.seal is None


@pytest.mark.parametrize(
    "tweak",
    [
        {"seal": {"verifier_id": "local-verifier-v1", "digest": "0" * 64}},
        {"verified": True},
        {"tool_calls": [{"name": "send_wire", "args": {"amount": "900"}}]},
        {"value": 900.5},
    ],
)
def test_extractor_cannot_supply_seal_flags_tool_calls_or_floats(world, tweak):
    """Expected: UntrustedInputError for a supplied seal, a 'verified' flag, a tool_calls key or a float amount.
    Why: the extraction schema contains facts only; trust fields and tools have no place to go."""
    with pytest.raises(UntrustedInputError):
        parse_untrusted_facts(_fact_json(world, **tweak))


def test_extractor_cannot_supply_tie_out_verdict(world):
    """Expected: UntrustedInputError when the JSON carries passed=true on a tie-out. Why: the verdict is derived."""
    data = json.loads(_fact_json(world))
    data["facts"][0]["tie_outs"][0]["passed"] = True
    with pytest.raises(UntrustedInputError):
        parse_untrusted_facts(json.dumps(data))


def test_extracted_spec_must_be_a_plain_draft(world):
    """Expected: a DRAFT parses; an APPROVED spec supplied by the extractor is rejected.
    Why: only trusted code and a human can advance a spec's status."""
    draft = world.build_spec(approve=False)
    assert parse_untrusted_spec(draft.model_dump_json()).status is SpecStatus.DRAFT
    approved = world.build_spec()
    with pytest.raises(UntrustedInputError):
        parse_untrusted_spec(approved.model_dump_json())
