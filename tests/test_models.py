"""Model-level invariants: spec structure, tie-outs, money type, classification, immutability.

Each test states its expected result and why."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from covenant_audit.arith import divide as div, round_ratio
from covenant_audit.models import (
    Approval,
    ComplianceStatus,
    Comparison,
    CovenantSpec,
    CovenantTestResult,
    DefinedTerm,
    Diff,
    Div,
    FinancialFact,
    Input,
    Lit,
    ReviewDecision,
    Seal,
    Sourced,
    SpecStatus,
    TermRef,
    ThresholdStep,
    TraceStep,
    TieOutCheck,
    classify,
)

from .factory import NOW, World

D = Decimal


@pytest.fixture()
def world() -> World:
    return World()


# ---------------------------------------------------------------- spec structure


def test_valid_spec_and_threshold_lookup(world):
    """Expected: the spec builds, and thresholds resolve per date (4.50 then 4.00, None outside).
    Why: the schedule is contiguous and the lookup is date-effective."""
    spec = world.build_spec()
    assert spec.threshold_on(date(2025, 6, 30)) == D("4.50")
    assert spec.threshold_on(date(2025, 7, 1)) == D("4.00")
    assert spec.threshold_on(date(2024, 12, 31)) is None


def test_unsourced_literal_rejected(world):
    """Expected: ValidationError 'has no source clause'.
    Why: a constant such as a 25% cap that cites no clause could be an LLM invention."""
    with pytest.raises(ValidationError, match="no source clause"):
        world.build_spec(approve=False, denominator=Div(num=Lit(value=D("2")), den=Lit(value=D("1"), structural=True)))


def test_structural_literal_needs_no_source(world):
    """Expected: accepted. Why: 0/1 plumbing constants are explicitly marked structural."""
    spec = world.build_spec(approve=False, denominator=Div(num=TermRef(name="consolidated_ebitda"), den=Lit(value=D("1"), structural=True)))
    assert spec.status is SpecStatus.DRAFT


def test_unresolved_term_rejected(world):
    """Expected: ValidationError 'unresolved defined term'. Why: the formula cites a term nobody defined."""
    with pytest.raises(ValidationError, match="unresolved defined term"):
        world.build_spec(approve=False, denominator=TermRef(name="nope"))


def test_undeclared_input_rejected(world):
    """Expected: ValidationError 'undeclared input'. Why: the engine could never obtain that fact."""
    with pytest.raises(ValidationError, match="undeclared input"):
        world.build_spec(approve=False, denominator=Input(name="ghost"))


def test_cyclic_terms_rejected(world):
    """Expected: ValidationError 'cyclic definition'. Why: a -> b -> a has no finite value."""
    base = world.build_spec(approve=False)
    span = base.terms[0].definition_spans
    cyc = (
        DefinedTerm(name="term_a", formula=TermRef(name="term_b"), definition_spans=span),
        DefinedTerm(name="term_b", formula=TermRef(name="term_a"), definition_spans=span),
    )
    with pytest.raises(ValidationError, match="cyclic"):
        CovenantSpec(**{**dict(base), "terms": cyc, "numerator": TermRef(name="term_a"), "denominator": TermRef(name="term_b")})


@pytest.mark.parametrize(
    "thresholds,msg",
    [
        (((date(2025, 1, 1), date(2025, 6, 30), "4.50"), (date(2025, 8, 1), None, "4.00")), "gap or overlap"),
        (((date(2025, 1, 1), date(2025, 7, 15), "4.50"), (date(2025, 7, 1), None, "4.00")), "gap or overlap"),
    ],
)
def test_invalid_threshold_schedules_rejected(world, thresholds, msg):
    """Expected: ValidationError. Why: a gap leaves dates with no covenant level; an overlap makes
    the level ambiguous. Both must be fixed by a human before the spec is usable."""
    with pytest.raises(ValidationError, match=msg):
        world.build_spec(approve=False, thresholds=thresholds)


def test_non_positive_threshold_rejected(world):
    """Expected: ValidationError. Why: a zero or negative maximum ratio is meaningless."""
    span = world.span(("CA-1", 2, "4.50 to 1.00"))
    with pytest.raises(ValidationError, match="positive"):
        ThresholdStep(effective_from=date(2025, 1, 1), effective_to=None, level=Sourced(value=D("0"), spans=(span,)))


def test_status_requires_seal_and_approval(world):
    """Expected: VERIFIED/APPROVED without a seal, APPROVED without approval, DRAFT with seal all fail.
    Why: lifecycle states must be backed by evidence, never just asserted."""
    draft = world.build_spec(approve=False)
    with pytest.raises(ValidationError, match="requires a verifier seal"):
        CovenantSpec(**{**dict(draft), "status": SpecStatus.VERIFIED})
    seal = Seal(verifier_id="x", digest="0" * 64)
    with pytest.raises(ValidationError, match="requires a human approval"):
        CovenantSpec(**{**dict(draft), "status": SpecStatus.APPROVED, "seal": seal})
    with pytest.raises(ValidationError, match="DRAFT spec cannot carry"):
        CovenantSpec(**{**dict(draft), "seal": seal})


def test_retroactive_amendment_must_cite_clause(world):
    """Expected: ValidationError unless a retroactive_source is cited.
    Why: an amendment signed on 2025-08-01 but effective from 2025-07-01 changes past periods, which
    is only allowed when the contract says so."""
    kwargs = dict(approve=False, version=2, supersedes="SPEC-1", effective_from=date(2025, 7, 1), executed_on=date(2025, 8, 1))
    with pytest.raises(ValidationError, match="retroactive"):
        world_spec = world.build_spec(**kwargs, addback_pct="0.15")
    clause = world.span(("AMD-1", 1, "the Lenders agree this Amendment applies to the Test Period ending June 30, 2025"))
    spec = world.build_spec(**kwargs, addback_pct="0.15", retroactive_source=clause)
    assert spec.is_retroactive


# ---------------------------------------------------------------- tie-outs


def test_tie_out_derives_passed_from_numbers():
    """Expected: passed is True only when |expected - actual| <= tolerance.
    Why: the verdict is computed, not supplied."""
    assert TieOutCheck(name="t", expected=D("100"), actual=D("100")).passed
    assert not TieOutCheck(name="t", expected=D("100"), actual=D("99")).passed


def test_tie_out_boundary_is_inclusive():
    """Expected: a difference exactly equal to the tolerance passes; one smallest unit more fails.
    Why: the rule is '<= tolerance'."""
    assert TieOutCheck(name="t", expected=D("100.00"), actual=D("99.50"), tolerance=D("0.50")).passed
    assert not TieOutCheck(name="t", expected=D("100.00"), actual=D("99.49"), tolerance=D("0.50")).passed


def test_tie_out_uses_exact_decimal_arithmetic():
    """Expected: 0.1 + 0.2 ties out to 0.3 exactly with zero tolerance. Why: Decimal, not binary floats."""
    assert TieOutCheck(name="t", expected=D("0.1") + D("0.2"), actual=D("0.3")).passed


def test_tie_out_cannot_be_told_it_passed():
    """Expected: supplying passed=True is rejected with an explanatory message.
    Why: neither a caller nor an LLM output may assert the verdict."""
    with pytest.raises(ValidationError, match="derived"):
        TieOutCheck(name="t", expected=D("100"), actual=D("1"), passed=True)
    with pytest.raises(ValidationError, match="derived"):
        TieOutCheck.model_validate({"name": "t", "expected": "100", "actual": "1", "passed": True})


def test_negative_tolerance_rejected():
    """Expected: ValidationError. Why: a negative tolerance would make every tie-out fail or pass nonsensically."""
    with pytest.raises(ValidationError):
        TieOutCheck(name="t", expected=D("1"), actual=D("1"), tolerance=D("-1"))


# ---------------------------------------------------------------- money type


def test_floats_are_rejected_for_money():
    """Expected: ValidationError for float input, success for str/int/Decimal.
    Why: float(0.1)+float(0.2) is 0.30000000000000004; binary error must not enter a covenant ratio."""
    with pytest.raises(ValidationError, match="binary floats"):
        TieOutCheck(name="t", expected=0.1 + 0.2, actual=D("0.3"))
    assert TieOutCheck(name="t", expected="0.3", actual=3, tolerance=D("3")).passed


def test_json_numbers_with_fraction_rejected_but_strings_accepted():
    """Expected: a JSON number like 40.5 is rejected, the string '40.5' accepted.
    Why: JSON numbers parse as floats; extractors must emit money as strings."""
    with pytest.raises(ValidationError):
        TieOutCheck.model_validate_json('{"name":"t","expected":40.5,"actual":"40.5"}')
    assert TieOutCheck.model_validate_json('{"name":"t","expected":"40.5","actual":"40.5"}').passed


# ---------------------------------------------------------------- classification


@pytest.mark.parametrize(
    "tested,thr,cmp,expected",
    [
        ("4.50", "4.50", Comparison.LE, ComplianceStatus.PASS_THIN_HEADROOM),  # exactly at limit: 0 headroom
        ("4.51", "4.50", Comparison.LE, ComplianceStatus.BREACH),
        ("3.00", "4.50", Comparison.LE, ComplianceStatus.PASS),
        ("2.99", "3.00", Comparison.GE, ComplianceStatus.BREACH),
        ("3.40", "3.00", Comparison.GE, ComplianceStatus.PASS),
        ("4.05", "4.50", Comparison.LE, ComplianceStatus.PASS),  # headroom exactly 10%: not below the 10% bound
        (None, "3.00", Comparison.GE, ComplianceStatus.INDETERMINATE),
    ],
)
def test_classify(tested, thr, cmp, expected):
    """Expected: the status in the table. Why: breach iff headroom < 0; thin iff 0 <= headroom% < policy."""
    t = D(tested) if tested else None
    assert classify(t, D(thr), cmp, D("0.10"))[0] is expected


def test_classify_uses_the_supplied_policy_value():
    """Expected: the same ratio is PASS at a 5% policy and PASS_THIN_HEADROOM at 10%.
    Why: thin-headroom is bank policy and must come from configuration, never a constant."""
    args = (D("4.20"), D("4.50"), Comparison.LE)
    assert classify(*args, D("0.05"))[0] is ComplianceStatus.PASS
    assert classify(*args, D("0.10"))[0] is ComplianceStatus.PASS_THIN_HEADROOM


def test_classify_headroom_values():
    """Expected: leverage 4.00x vs 4.50x gives headroom 0.50 and 1/9 of the threshold.
    Why: head = threshold - tested for a maximum covenant; pct = head / threshold."""
    status, head, pct = classify(D("4.00"), D("4.50"), Comparison.LE, D("0.10"))
    assert (status, head) == (ComplianceStatus.PASS, D("0.50"))
    assert pct == div(D("0.5"), D("4.5"))
    _, head_ge, _ = classify(D("3.50"), D("3.00"), Comparison.GE, D("0.10"))
    assert head_ge == D("0.50")  # minimum covenant: tested - threshold


# ---------------------------------------------------------------- result validator


def ratio_trace(raw, places, mode="ROUND_HALF_UP"):
    """A minimal valid trace ending in a 'ratio' division and a rounding step."""
    steps = [
        TraceStep(step_id=1, node_id="n", label="numerator", op="lit", value=raw),
        TraceStep(step_id=2, node_id="d", label="denominator", op="lit", value=D("1")),
        TraceStep(step_id=3, node_id="r", label="ratio", op="div", operand_step_ids=(1, 2), operand_values=(raw, D("1")), value=raw),
    ]
    if places is not None:
        steps.append(TraceStep(step_id=4, node_id="q", label="rounded", op="round", operand_step_ids=(3,), operand_values=(raw,), value=round_ratio(raw, places, mode)))
    return tuple(steps)


def make_result(**over):
    kw = dict(
        result_id="R1", agreement_id="CA-1", kind="max_total_net_leverage", spec_id="S1", spec_version=1,
        spec_hash="a" * 64, policy_id="P", policy_version=1, policy_hash="b" * 64, thin_headroom_pct=D("0.10"),
        engine_version="t", test_date=date(2025, 6, 30), fact_ids=(), ratio_raw=D("4.00"), ratio_tested=D("4.00"),
        threshold=D("4.50"), comparison=Comparison.LE, ratio_decimal_places=2, rounding="ROUND_HALF_UP",
        headroom_abs=D("0.50"), headroom_pct=div(D("0.5"), D("4.5")), status=ComplianceStatus.PASS, needs_review=False,
        computed_at=NOW,
    )
    kw.update(over)
    if "trace" not in kw and kw["ratio_raw"] is not None:
        kw["trace"] = ratio_trace(kw["ratio_raw"], kw["ratio_decimal_places"], kw["rounding"])
    return CovenantTestResult(**kw)


def test_result_accepts_a_correct_pass():
    """Expected: constructs. Why: 4.00 vs 4.50 has 11.1% headroom, above the 10% policy."""
    assert make_result().status is ComplianceStatus.PASS


@pytest.mark.parametrize(
    "over,msg",
    [
        # headroom 6.7% < 10%: PASS is wrong in the *other* direction too
        (dict(ratio_raw=D("4.20"), ratio_tested=D("4.20"), headroom_abs=D("0.30"), headroom_pct=div(D("0.3"), D("4.5"))), "contradicts"),
        (dict(ratio_raw=D("4.60"), ratio_tested=D("4.60"), headroom_abs=D("-0.10"), headroom_pct=div(D("-0.1"), D("4.5"))), "contradicts"),
        (dict(status=ComplianceStatus.BREACH), "contradicts"),  # 4.00 is a clear pass
        (dict(status=ComplianceStatus.PASS_THIN_HEADROOM, needs_review=True), "contradicts"),
        (dict(ratio_raw=None, ratio_tested=None, headroom_abs=None, headroom_pct=None), "contradicts"),  # PASS with no ratio
        (dict(headroom_abs=D("999")), "headroom"),
        (dict(ratio_raw=D("4.004"), ratio_tested=D("4.0")), "not ratio_raw"),  # wrong precision
        (dict(ratio_raw=D("4.005"), ratio_tested=D("4.00")), "not ratio_raw"),  # half-up gives 4.01
    ],
)
def test_result_rejects_every_inconsistency(over, msg):
    """Expected: ValidationError for each tampered or buggy combination.
    Why: status, headroom and rounding are all re-derived, so inconsistency is rejected in BOTH
    directions (PASS for a thin/breach ratio, BREACH for a pass, PASS with no ratio)."""
    with pytest.raises(ValidationError, match=msg):
        make_result(**over)


def test_result_validator_uses_recorded_policy_not_a_constant():
    """Expected: PASS is valid under a 5% policy and invalid for the same numbers under 10%.
    Why: validation uses the policy value stored in (and hashed into) the result."""
    thin = dict(ratio_raw=D("4.20"), ratio_tested=D("4.20"), headroom_abs=D("0.30"), headroom_pct=div(D("0.3"), D("4.5")))
    assert make_result(**thin, thin_headroom_pct=D("0.05")).status is ComplianceStatus.PASS
    with pytest.raises(ValidationError):
        make_result(**thin, thin_headroom_pct=D("0.10"))


@pytest.mark.parametrize(
    "over",
    [
        dict(ratio_raw=D("4.60"), ratio_tested=D("4.60"), headroom_abs=D("-0.10"), headroom_pct=div(D("-0.1"), D("4.5")), status=ComplianceStatus.BREACH),
        dict(ratio_raw=None, ratio_tested=None, headroom_abs=None, headroom_pct=None, status=ComplianceStatus.INDETERMINATE),
        dict(ratio_raw=D("4.20"), ratio_tested=D("4.20"), headroom_abs=D("0.30"), headroom_pct=div(D("0.3"), D("4.5")), status=ComplianceStatus.PASS_THIN_HEADROOM),
    ],
)
def test_breach_indeterminate_and_thin_require_review(over):
    """Expected: needs_review=False is rejected for BREACH, INDETERMINATE and PASS_THIN_HEADROOM; True is accepted.
    Why: a human must see every non-clean outcome."""
    with pytest.raises(ValidationError, match="requires human review"):
        make_result(**over, needs_review=False)
    assert make_result(**over, needs_review=True).needs_review


def test_result_without_rounding_rule_keeps_exact_ratio():
    """Expected: with ratio_decimal_places=None tested == raw. Why: the contract specifies no rounding."""
    raw = D("3.123456789")
    r = make_result(ratio_raw=raw, ratio_tested=raw, ratio_decimal_places=None, headroom_abs=D("4.5") - raw, headroom_pct=div(D("4.5") - raw, D("4.5")))
    assert r.ratio_tested == raw


# ---------------------------------------------------------------- immutability, review


def test_approved_artefacts_are_deeply_immutable(world):
    """Expected: attribute assignment and in-place list mutation both fail.
    Why: models are frozen and sequences are tuples; an approved spec cannot be edited in place."""
    spec = world.build_spec()
    assert isinstance(spec.terms, tuple) and isinstance(spec.thresholds[0].level.spans, tuple)
    with pytest.raises(ValidationError):
        spec.version = 9  # type: ignore[misc]
    with pytest.raises(AttributeError):
        spec.terms.append(spec.terms[0])  # type: ignore[attr-defined]


def test_spec_hash_ignores_lifecycle_but_not_content(world):
    """Expected: approving does not change content_hash; changing a threshold does.
    Why: the seal covers contractual content, which must be stable across status changes."""
    draft = world.build_spec(approve=False)
    approved = world.build_spec()
    assert draft.content_hash() == approved.content_hash()
    assert world.build_spec(approve=False, thresholds=((date(2025, 1, 1), None, "4.00"),)).content_hash() != draft.content_hash()


def test_review_decision_rules():
    """Expected: overrides need a value and a >=20 char rationale; confirmation needs neither.
    Why: a human override of a deterministic result must be explained."""
    ok = ReviewDecision(reviewer_id="u", reviewer_role="credit_officer", decision="confirmed", decided_at=NOW)
    assert ok.decision == "confirmed"
    with pytest.raises(ValidationError):
        ReviewDecision(reviewer_id="u", reviewer_role="credit_officer", decision="overridden", decided_at=NOW)
    with pytest.raises(ValidationError):
        ReviewDecision(reviewer_id="u", reviewer_role="credit_officer", decision="escalated", rationale="short", decided_at=NOW)


def test_approval_requires_timezone():
    """Expected: ValidationError for a naive timestamp. Why: audit times must be unambiguous."""
    with pytest.raises(ValidationError, match="timezone"):
        Approval(approver_id="a", role="credit_officer", approved_at=datetime(2025, 1, 1))
