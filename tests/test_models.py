from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from covenant_audit.models import (
    ComplianceStatus,
    Comparison,
    CovenantKind,
    CovenantSpec,
    CovenantTestResult,
    DefinedTerm,
    Diff,
    Div,
    Input,
    InputRequirement,
    Lit,
    MinOf,
    Mul,
    ReviewDecision,
    Sourced,
    SourceSpan,
    SpecStatus,
    Sum,
    TermRef,
    ThresholdStep,
    classify,
    verify_span,
)

PAGE = "Maximum Total Net Leverage Ratio shall not exceed 4.50 to 1.00 for any Test Period."
SHA = "a" * 64


def span(quote: str, page_text: str = PAGE) -> SourceSpan:
    i = page_text.index(quote)
    return SourceSpan(doc_id="CA-1", doc_sha256=SHA, page=1, char_start=i, char_end=i + len(quote), quote=quote)


def make_spec(**over):
    s = verify_span(span("4.50"), PAGE)
    d = verify_span(span("Test Period"), PAGE)
    lit25 = Lit(value=Decimal("0.25"), source=d)
    ebitda = Sum(
        args=[
            Input(name="net_income"),
            MinOf(args=[Input(name="addbacks"), Mul(left=lit25, right=Input(name="net_income"))]),
        ]
    )
    kw = dict(
        spec_id="S1",
        agreement_id="CA-1",
        version=1,
        kind=CovenantKind.MAX_TOTAL_NET_LEVERAGE,
        comparison=Comparison.LE,
        numerator=TermRef(name="net_debt"),
        denominator=TermRef(name="ebitda"),
        terms=[
            DefinedTerm(name="ebitda", formula=ebitda, definition_spans=[d]),
            DefinedTerm(
                name="net_debt",
                formula=Diff(left=Input(name="total_debt"), right=Input(name="cash")),
                definition_spans=[d],
            ),
        ],
        inputs=[
            InputRequirement(name=n, description=n, basis="point_in_time", definition_spans=[d])
            for n in ("net_income", "addbacks", "total_debt", "cash")
        ],
        thresholds=[ThresholdStep(effective_from=date(2025, 1, 1), effective_to=None, level=Sourced(value=Decimal("4.50"), spans=[s]))],
        extractor_run_id="run-1",
    )
    kw.update(over)
    return CovenantSpec(**kw)


def test_spec_valid_and_threshold_lookup():
    spec = make_spec()
    assert spec.threshold_on(date(2025, 6, 30)) == Decimal("4.50")
    assert spec.threshold_on(date(2024, 6, 30)) is None


def test_verify_span_rejects_hallucinated_quote():
    bad = span("4.50").model_copy(update={"quote": "5.50"})
    with pytest.raises(ValueError):
        verify_span(bad, PAGE)


def test_unsourced_literal_rejected():
    with pytest.raises(ValidationError, match="no source"):
        make_spec(denominator=Div(num=Lit(value=Decimal("2")), den=Lit(value=Decimal("1"), structural=True)))


def test_unresolved_term_and_cycle_rejected():
    with pytest.raises(ValidationError, match="unresolved"):
        make_spec(numerator=TermRef(name="nope"))
    d = verify_span(span("Test Period"), PAGE)
    cyc = [
        DefinedTerm(name="term_a", formula=TermRef(name="term_b"), definition_spans=[d]),
        DefinedTerm(name="term_b", formula=TermRef(name="term_a"), definition_spans=[d]),
    ]
    with pytest.raises(ValidationError, match="cyclic"):
        make_spec(terms=cyc, numerator=TermRef(name="term_a"), denominator=TermRef(name="term_b"))


def test_threshold_gap_rejected():
    s = verify_span(span("4.50"), PAGE)
    steps = [
        ThresholdStep(effective_from=date(2025, 1, 1), effective_to=date(2025, 6, 30), level=Sourced(value=Decimal("4.5"), spans=[s])),
        ThresholdStep(effective_from=date(2025, 8, 1), effective_to=None, level=Sourced(value=Decimal("4.0"), spans=[s])),
    ]
    with pytest.raises(ValidationError, match="gap"):
        make_spec(thresholds=steps)


def test_unverified_spans_cannot_be_verified_or_approved():
    unverified = span("Test Period")
    d = DefinedTerm(name="net_debt", formula=Input(name="total_debt"), definition_spans=[unverified])
    with pytest.raises(ValidationError):
        make_spec(
            status=SpecStatus.VERIFIED,
            terms=[d, make_spec().terms[0]],
        )
    with pytest.raises(ValidationError, match="human approval"):
        make_spec(status=SpecStatus.APPROVED)


@pytest.mark.parametrize(
    "tested,thr,cmp,expected",
    [
        ("4.50", "4.50", Comparison.LE, ComplianceStatus.PASS_THIN_HEADROOM),
        ("4.51", "4.50", Comparison.LE, ComplianceStatus.BREACH),
        ("3.00", "4.50", Comparison.LE, ComplianceStatus.PASS),
        ("2.99", "3.00", Comparison.GE, ComplianceStatus.BREACH),
        ("3.40", "3.00", Comparison.GE, ComplianceStatus.PASS),
        (None, "3.00", Comparison.GE, ComplianceStatus.INDETERMINATE),
    ],
)
def test_classify(tested, thr, cmp, expected):
    t = Decimal(tested) if tested else None
    assert classify(t, Decimal(thr), cmp, Decimal("0.10"))[0] is expected


def result(**over):
    kw = dict(
        result_id="R1", agreement_id="CA-1", spec_id="S1", spec_version=1, spec_hash="h",
        engine_version="0.1", test_date=date(2025, 6, 30), fact_ids=["F1"],
        ratio_raw=Decimal("4.5049"), ratio_tested=Decimal("4.50"), threshold=Decimal("4.50"),
        comparison=Comparison.LE, headroom_abs=Decimal("0"), headroom_pct=Decimal("0"),
        status=ComplianceStatus.PASS_THIN_HEADROOM, trace=[], needs_review=True,
        computed_at=datetime.now(timezone.utc),
    )
    kw.update(over)
    return CovenantTestResult(**kw)


def test_result_cannot_claim_pass_when_breached():
    assert result().status is ComplianceStatus.PASS_THIN_HEADROOM
    with pytest.raises(ValidationError, match="contradicts"):
        result(ratio_raw=Decimal("4.5149"), ratio_tested=Decimal("4.51"), status=ComplianceStatus.PASS)


def test_breach_requires_review_and_rounding_checked():
    with pytest.raises(ValidationError, match="require human review"):
        result(ratio_raw=Decimal("4.6"), ratio_tested=Decimal("4.60"), status=ComplianceStatus.BREACH, needs_review=False)
    with pytest.raises(ValidationError, match="rounded"):
        result(ratio_raw=Decimal("4.9"), ratio_tested=Decimal("4.50"))


def test_review_requires_rationale_and_override_value():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        ReviewDecision(reviewer_id="u", reviewer_role="credit_officer", decision="overridden", decided_at=now)
    ok = ReviewDecision(reviewer_id="u", reviewer_role="credit_officer", decision="confirmed", decided_at=now)
    assert ok.decision == "confirmed"
