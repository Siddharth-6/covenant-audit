"""Typed contracts between the probabilistic extractor and the deterministic engine.

Everything the LLM produces must parse into these models. Everything the engine
consumes or emits is one of these models. Money and ratios are Decimal, never float.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated, Iterator, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

Money = Annotated[Decimal, Field(allow_inf_nan=False)]
Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=False)


# --------------------------------------------------------------------------
# Lineage primitive
# --------------------------------------------------------------------------


class SourceSpan(_Frozen):
    """Pointer into an immutable, hashed source document. `quote` must equal
    page_text[char_start:char_end]; `verified` is set only by verify_span()."""

    doc_id: str
    doc_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    page: int = Field(ge=1)
    char_start: int = Field(ge=0)
    char_end: int
    quote: str = Field(min_length=1)
    clause_ref: str | None = None  # e.g. "Section 7.11(a)", as written in the document
    text_origin: Literal["native_text", "ocr"] = "native_text"
    ocr_confidence: float | None = Field(default=None, ge=0, le=1)
    table_cell: tuple[int, int] | None = None  # (row, col) for financial statement facts
    verified: bool = False

    @model_validator(mode="after")
    def _offsets(self) -> SourceSpan:
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be > char_start")
        if self.text_origin == "ocr" and self.ocr_confidence is None:
            raise ValueError("OCR-derived spans must carry ocr_confidence")
        return self


def verify_span(span: SourceSpan, page_text: str) -> SourceSpan:
    """Deterministic grounding check. Raises if the LLM-supplied quote is not
    literally present at the claimed offsets; returns a verified copy otherwise."""
    if span.char_end > len(page_text):
        raise ValueError(f"span exceeds page length on p.{span.page}")
    if page_text[span.char_start : span.char_end] != span.quote:
        raise ValueError(f"quote does not match page text at p.{span.page}:{span.char_start}")
    return span.model_copy(update={"verified": True})


class Sourced(_Frozen):
    """A scalar extracted from a document together with the evidence for it."""

    value: Money
    spans: list[SourceSpan] = Field(min_length=1)


# --------------------------------------------------------------------------
# Stream A: contract -> CovenantSpec (compiled once per agreement version)
# --------------------------------------------------------------------------


class CovenantKind(str, Enum):
    MAX_TOTAL_NET_LEVERAGE = "max_total_net_leverage"
    MAX_SENIOR_SECURED_LEVERAGE = "max_senior_secured_leverage"
    MIN_INTEREST_COVERAGE = "min_interest_coverage"
    MIN_FIXED_CHARGE_COVERAGE = "min_fixed_charge_coverage"


class Comparison(str, Enum):
    LE = "le"  # ratio must be <= threshold (leverage)
    GE = "ge"  # ratio must be >= threshold (coverage)


class Lit(_Frozen):
    op: Literal["lit"] = "lit"
    value: Money
    structural: bool = False  # True only for 0/1 plumbing; every other literal needs a source
    source: SourceSpan | None = None


class Input(_Frozen):
    op: Literal["input"] = "input"
    name: Key  # must be declared in CovenantSpec.inputs


class TermRef(_Frozen):
    op: Literal["term"] = "term"
    name: Key  # must resolve to a DefinedTerm


class Sum(_Frozen):
    op: Literal["sum"] = "sum"
    args: list[Expr] = Field(min_length=1)


class Diff(_Frozen):
    op: Literal["diff"] = "diff"
    left: Expr
    right: Expr


class Mul(_Frozen):
    op: Literal["mul"] = "mul"
    left: Expr
    right: Expr


class Div(_Frozen):
    op: Literal["div"] = "div"
    num: Expr
    den: Expr


class MinOf(_Frozen):
    op: Literal["min"] = "min"
    args: list[Expr] = Field(min_length=2)  # caps, e.g. add-backs <= 25% of EBITDA


class MaxOf(_Frozen):
    op: Literal["max"] = "max"
    args: list[Expr] = Field(min_length=2)  # floors, e.g. non-negative net debt


Expr = Annotated[
    Union[Lit, Input, TermRef, Sum, Diff, Mul, Div, MinOf, MaxOf],
    Field(discriminator="op"),
]
for _m in (Sum, Diff, Mul, Div, MinOf, MaxOf):
    _m.model_rebuild()


def walk(expr: Expr) -> Iterator[Expr]:
    yield expr
    if isinstance(expr, (Sum, MinOf, MaxOf)):
        for a in expr.args:
            yield from walk(a)
    elif isinstance(expr, (Diff, Mul)):
        yield from walk(expr.left)
        yield from walk(expr.right)
    elif isinstance(expr, Div):
        yield from walk(expr.num)
        yield from walk(expr.den)


class InputRequirement(_Frozen):
    """A canonical financial line item the engine needs, defined by the contract."""

    name: Key
    description: str
    basis: Literal["ltm_flow", "quarter_flow", "point_in_time"]
    sign: Literal["positive", "as_reported"] = "positive"
    definition_spans: list[SourceSpan] = Field(min_length=1)


class DefinedTerm(_Frozen):
    """A contractual defined term (e.g. 'Consolidated EBITDA') compiled to an expression."""

    name: Key
    formula: Expr
    definition_spans: list[SourceSpan] = Field(min_length=1)
    amended_by: list[str] = Field(default_factory=list)  # amendment doc_ids, ordered


class ThresholdStep(_Frozen):
    effective_from: date
    effective_to: date | None  # None = open-ended
    level: Sourced


class Approval(_Frozen):
    approver_id: str
    role: Literal["credit_officer", "legal_reviewer"]
    approved_at: datetime
    edits_from_draft: list[str] = Field(default_factory=list)  # JSON-pointer paths changed


class SpecStatus(str, Enum):
    DRAFT = "draft"  # straight from the extractor
    VERIFIED = "verified"  # passed deterministic grounding + structural checks
    APPROVED = "approved"  # human gate 1 signed
    SUPERSEDED = "superseded"  # an amendment replaced it
    REVOKED = "revoked"  # found wrong; dependent results become stale


class CovenantSpec(_Frozen):
    spec_id: str
    agreement_id: str
    version: int = Field(ge=1)
    supersedes: str | None = None
    kind: CovenantKind
    comparison: Comparison
    numerator: Expr
    denominator: Expr
    terms: list[DefinedTerm] = Field(default_factory=list)
    inputs: list[InputRequirement] = Field(min_length=1)
    thresholds: list[ThresholdStep] = Field(min_length=1)
    test_period: Literal["ltm_quarterly", "ltm_monthly"] = "ltm_quarterly"
    ratio_decimal_places: int = Field(default=2, ge=0, le=6)
    thin_headroom_pct: Decimal = Decimal("0.10")  # policy, not contract; set by bank
    extractor_run_id: str
    status: SpecStatus = SpecStatus.DRAFT
    approvals: list[Approval] = Field(default_factory=list)

    @model_validator(mode="after")
    def _structure(self) -> CovenantSpec:
        term_names = {t.name for t in self.terms}
        if len(term_names) != len(self.terms):
            raise ValueError("duplicate defined term")
        input_names = {i.name for i in self.inputs}
        roots = [self.numerator, self.denominator, *[t.formula for t in self.terms]]
        for root in roots:
            for n in walk(root):
                if isinstance(n, Input) and n.name not in input_names:
                    raise ValueError(f"undeclared input '{n.name}'")
                if isinstance(n, TermRef) and n.name not in term_names:
                    raise ValueError(f"unresolved defined term '{n.name}'")
                if isinstance(n, Lit) and not n.structural and n.source is None:
                    raise ValueError(f"literal {n.value} has no source clause")
        self._assert_acyclic()
        self._assert_thresholds()
        if self.status in (SpecStatus.VERIFIED, SpecStatus.APPROVED):
            if not all(s.verified for s in self.all_spans()):
                raise ValueError("every span must be grounding-verified before VERIFIED")
        if self.status is SpecStatus.APPROVED and not self.approvals:
            raise ValueError("APPROVED requires a human approval")
        return self

    def all_spans(self) -> Iterator[SourceSpan]:
        for i in self.inputs:
            yield from i.definition_spans
        for t in self.terms:
            yield from t.definition_spans
        for th in self.thresholds:
            yield from th.level.spans
        for root in (self.numerator, self.denominator, *[t.formula for t in self.terms]):
            for n in walk(root):
                if isinstance(n, Lit) and n.source:
                    yield n.source

    def _assert_acyclic(self) -> None:
        graph = {
            t.name: {n.name for n in walk(t.formula) if isinstance(n, TermRef)} for t in self.terms
        }
        state: dict[str, int] = {}

        def visit(k: str) -> None:
            if state.get(k) == 1:
                raise ValueError(f"cyclic definition involving '{k}'")
            if state.get(k) == 2:
                return
            state[k] = 1
            for d in graph[k]:
                visit(d)
            state[k] = 2

        for k in graph:
            visit(k)

    def _assert_thresholds(self) -> None:
        steps = sorted(self.thresholds, key=lambda s: s.effective_from)
        for a, b in zip(steps, steps[1:]):
            if a.effective_to is None or a.effective_to + timedelta(days=1) != b.effective_from:
                raise ValueError("threshold schedule has a gap or overlap")

    def threshold_on(self, test_date: date) -> Decimal | None:
        for s in self.thresholds:
            if s.effective_from <= test_date and (s.effective_to is None or test_date <= s.effective_to):
                return s.level.value
        return None

    def content_hash(self) -> str:
        return hashlib.sha256(self.model_dump_json(exclude={"approvals"}).encode()).hexdigest()


# --------------------------------------------------------------------------
# Stream B: borrower financials -> FinancialFact (extracted every quarter)
# --------------------------------------------------------------------------


class TieOutCheck(_Frozen):
    """Deterministic arithmetic cross-check on extracted facts."""

    name: str  # e.g. "debt_components_sum_to_total_debt"
    expected: Money
    actual: Money
    tolerance: Money = Decimal("0")
    passed: bool


class FinancialFact(_Frozen):
    fact_id: str
    input_name: Key  # canonical key from CovenantSpec.inputs
    value: Money  # normalised to whole currency units
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    reported_scale: Literal[1, 1_000, 1_000_000]  # scale printed on the statement
    period_start: date | None = None  # None for point-in-time items
    period_end: date
    origin: Literal["audited_fs", "unaudited_fs", "compliance_certificate", "management_adj"]
    source: SourceSpan
    extractor_run_id: str
    tie_outs: list[TieOutCheck] = Field(default_factory=list)

    @model_validator(mode="after")
    def _period(self) -> FinancialFact:
        if self.period_start and self.period_start > self.period_end:
            raise ValueError("period_start after period_end")
        return self

    @property
    def verified(self) -> bool:
        return self.source.verified and all(t.passed for t in self.tie_outs)


# --------------------------------------------------------------------------
# Output: deterministic result with full computation trace
# --------------------------------------------------------------------------


class ComplianceStatus(str, Enum):
    PASS = "pass"
    PASS_THIN_HEADROOM = "pass_thin_headroom"
    BREACH = "breach"
    INDETERMINATE = "indeterminate"  # missing/unverified input; never defaults to PASS


class TraceStep(_Frozen):
    """One evaluated node of the expression tree. The trace is the audit lineage:
    value <- operands <- fact_ids <- SourceSpans <- (doc_sha256, page, offset)."""

    step_id: int
    label: str  # defined-term name or operator
    op: str
    operand_step_ids: list[int] = Field(default_factory=list)
    fact_id: str | None = None
    literal_source: SourceSpan | None = None
    value: Money


class ReviewDecision(_Frozen):
    reviewer_id: str
    reviewer_role: Literal["monitoring_analyst", "credit_officer"]
    decision: Literal["confirmed", "overridden", "facts_rejected", "escalated"]
    rationale: str = ""
    override_value: Money | None = None
    decided_at: datetime

    @model_validator(mode="after")
    def _rationale(self) -> ReviewDecision:
        if self.decision != "confirmed" and len(self.rationale.strip()) < 20:
            raise ValueError("non-confirm decisions require a written rationale")
        if (self.decision == "overridden") != (self.override_value is not None):
            raise ValueError("override_value is required iff decision == overridden")
        return self


def quantize_ratio(ratio: Decimal, places: int) -> Decimal:
    return ratio.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def classify(
    tested: Decimal | None, threshold: Decimal | None, cmp: Comparison, thin_pct: Decimal
) -> tuple[ComplianceStatus, Decimal | None, Decimal | None]:
    """The one place compliance status is decided. Returns (status, headroom_abs, headroom_pct)."""
    if tested is None or threshold is None or threshold == 0:
        return ComplianceStatus.INDETERMINATE, None, None
    head = threshold - tested if cmp is Comparison.LE else tested - threshold
    pct = head / threshold
    if head < 0:
        return ComplianceStatus.BREACH, head, pct
    if pct < thin_pct:
        return ComplianceStatus.PASS_THIN_HEADROOM, head, pct
    return ComplianceStatus.PASS, head, pct


class CovenantTestResult(_Frozen):
    result_id: str
    agreement_id: str
    spec_id: str
    spec_version: int
    spec_hash: str  # CovenantSpec.content_hash() at compute time
    engine_version: str
    test_date: date
    fact_ids: list[str]
    ratio_raw: Money | None
    ratio_tested: Money | None
    threshold: Money | None
    comparison: Comparison
    headroom_abs: Money | None
    headroom_pct: Money | None
    status: ComplianceStatus
    reasons: list[str] = Field(default_factory=list)  # why INDETERMINATE / why routed to review
    trace: list[TraceStep]
    tie_outs: list[TieOutCheck] = Field(default_factory=list)
    needs_review: bool
    review: ReviewDecision | None = None
    stale: bool = False  # set when the spec or a fact is later revoked/restated
    prev_hash: str | None = None  # append-only hash chain
    computed_at: datetime

    @model_validator(mode="after")
    def _consistent(self) -> CovenantTestResult:
        # Reject any result whose status disagrees with the arithmetic, so a buggy
        # or tampered writer cannot persist "PASS" for a breached ratio.
        if self.ratio_raw is not None and self.ratio_tested is not None:
            places = max(-int(self.ratio_tested.as_tuple().exponent), 0)
            if quantize_ratio(self.ratio_raw, places) != self.ratio_tested:
                raise ValueError("ratio_tested is not the rounded ratio_raw")
        status, _, _ = classify(
            self.ratio_tested, self.threshold, self.comparison, Decimal("0.10")
        )
        if status is ComplianceStatus.BREACH and self.status is not ComplianceStatus.BREACH:
            raise ValueError("status contradicts arithmetic: ratio violates threshold")
        if status is ComplianceStatus.INDETERMINATE and self.status is not ComplianceStatus.INDETERMINATE:
            raise ValueError("missing ratio/threshold must be INDETERMINATE")
        if self.status in (ComplianceStatus.BREACH, ComplianceStatus.INDETERMINATE) and not self.needs_review:
            raise ValueError("BREACH and INDETERMINATE always require human review")
        return self

    def content_hash(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()
