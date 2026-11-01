"""Typed contracts between the probabilistic extractor and the deterministic engine.

Everything the LLM produces must parse into these models. Everything the engine consumes
or emits is one of these models. Money and ratios are Decimal, never float. Models are
frozen and use tuples, so a sealed artefact cannot be mutated in place.

Trust boundary: nothing in this module trusts a flag supplied by a caller. Verification
verdicts (span grounding, tie-outs, classification, rounding) are either derived inside
the model or attached as a `Seal` by `verification.SourceVerifier`, which only the
trusted verification code can mint.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Iterator, Literal, Union

from pydantic import Field, field_validator, model_validator

from .arith import CALC_CONTEXT, RoundingMode, apply_op, round_ratio
from .base import (
    SCHEMA_VERSION,
    FrozenModel,
    Key,
    Money,
    Sha256Hex,
    content_hash,
)

# --------------------------------------------------------------------------
# Lineage primitives
# --------------------------------------------------------------------------


class SourceSpan(FrozenModel):
    """An *extracted* quotation: a claim, made by an LLM or a person, that `quote` appears at
    page `page`, offsets [char_start, char_end) of document `doc_id` whose hash is `doc_sha256`.

    It carries no verification state on purpose. A span only counts as verified once
    `SourceVerifier.verify_span` has checked it against the stored document and returned a
    `VerifiedSpan`, or once it sits inside a spec or fact sealed by the verifier."""

    doc_id: str
    doc_sha256: Sha256Hex
    page: int = Field(ge=1)
    char_start: int = Field(ge=0)
    char_end: int
    quote: str = Field(min_length=1)
    clause_ref: str | None = None  # e.g. "Section 7.11(a)", as written in the document
    text_origin: Literal["native_text", "ocr"] = "native_text"
    ocr_confidence: float | None = Field(default=None, ge=0, le=1)
    table_cell: tuple[int, int] | None = None  # (row, col) for financial statement facts

    @model_validator(mode="after")
    def _offsets(self) -> SourceSpan:
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be > char_start")
        if self.char_end - self.char_start != len(self.quote):
            raise ValueError("quote length does not match the char_start:char_end window")
        if self.text_origin == "ocr" and self.ocr_confidence is None:
            raise ValueError("OCR-derived spans must carry ocr_confidence")
        if self.text_origin == "native_text" and self.ocr_confidence is not None:
            raise ValueError("native-text spans must not carry ocr_confidence")
        return self


class Seal(FrozenModel):
    """Keyed attestation minted by the trusted verifier over an artefact's content hash."""

    verifier_id: str
    digest: Sha256Hex


class Sourced(FrozenModel):
    """A scalar extracted from a document together with the evidence for it."""

    value: Money
    spans: tuple[SourceSpan, ...] = Field(min_length=1)


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


class Lit(FrozenModel):
    op: Literal["lit"] = "lit"
    value: Money
    structural: bool = False  # True only for 0/1 plumbing; every other literal needs a source
    source: SourceSpan | None = None


class Input(FrozenModel):
    op: Literal["input"] = "input"
    name: Key  # must be declared in CovenantSpec.inputs


class TermRef(FrozenModel):
    op: Literal["term"] = "term"
    name: Key  # must resolve to a DefinedTerm


class Sum(FrozenModel):
    op: Literal["sum"] = "sum"
    args: tuple[Expr, ...] = Field(min_length=1)


class Diff(FrozenModel):
    op: Literal["diff"] = "diff"
    left: Expr
    right: Expr


class Mul(FrozenModel):
    op: Literal["mul"] = "mul"
    left: Expr
    right: Expr


class Div(FrozenModel):
    op: Literal["div"] = "div"
    num: Expr
    den: Expr


class MinOf(FrozenModel):
    op: Literal["min"] = "min"
    args: tuple[Expr, ...] = Field(min_length=2)  # caps, e.g. add-backs <= 25% of EBITDA


class MaxOf(FrozenModel):
    op: Literal["max"] = "max"
    args: tuple[Expr, ...] = Field(min_length=2)  # floors, e.g. non-negative net debt


Expr = Annotated[
    Union[Lit, Input, TermRef, Sum, Diff, Mul, Div, MinOf, MaxOf],
    Field(discriminator="op"),
]
for _m in (Sum, Diff, Mul, Div, MinOf, MaxOf):
    _m.model_rebuild()


def children(expr: Any) -> tuple[Any, ...]:
    """Direct sub-expressions, in evaluation order."""
    if isinstance(expr, (Sum, MinOf, MaxOf)):
        return tuple(expr.args)
    if isinstance(expr, (Diff, Mul)):
        return (expr.left, expr.right)
    if isinstance(expr, Div):
        return (expr.num, expr.den)
    return ()


def walk(expr: Any) -> Iterator[Any]:
    yield expr
    for child in children(expr):
        yield from walk(child)


class InputRequirement(FrozenModel):
    """A canonical financial line item the engine needs, defined by the contract."""

    name: Key
    description: str
    basis: Literal["ltm_flow", "quarter_flow", "point_in_time"]
    sign: Literal["positive", "as_reported"] = "positive"  # "positive" rejects negative facts
    definition_spans: tuple[SourceSpan, ...] = Field(min_length=1)


class DefinedTerm(FrozenModel):
    """A contractual defined term (e.g. 'Consolidated EBITDA') compiled to an expression."""

    name: Key
    formula: Expr
    definition_spans: tuple[SourceSpan, ...] = Field(min_length=1)
    amended_by: tuple[str, ...] = ()  # amendment doc_ids, ordered


class ThresholdStep(FrozenModel):
    effective_from: date
    effective_to: date | None  # None = open-ended
    level: Sourced

    @model_validator(mode="after")
    def _level(self) -> ThresholdStep:
        if self.level.value <= 0:
            raise ValueError("a covenant threshold must be positive")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to precedes effective_from")
        return self


class Approval(FrozenModel):
    approver_id: str
    role: Literal["credit_officer", "legal_reviewer"]
    approved_at: datetime
    edits_from_draft: tuple[str, ...] = ()  # JSON-pointer paths changed

    @field_validator("approved_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("approved_at must be timezone-aware")
        return v


class SpecStatus(str, Enum):
    """Lifecycle of a spec *document*. What happens to an approved spec afterwards
    (superseded by an amendment, revoked) is recorded in `SpecRegistry` and the ledger,
    because an approved spec is immutable and must not be edited to say so."""

    DRAFT = "draft"  # straight from the extractor
    VERIFIED = "verified"  # every span grounded in the stored documents; sealed
    APPROVED = "approved"  # human gate 1 signed


_SPEC_HASH_EXCLUDES = ("status", "approvals", "seal")


class CovenantSpec(FrozenModel):
    """Contractual terms only. Bank policy (thin-headroom %, sampling) is in `BankPolicy`."""

    spec_id: str
    agreement_id: str
    version: int = Field(ge=1)
    supersedes: str | None = None
    schema_version: str = SCHEMA_VERSION
    kind: CovenantKind
    comparison: Comparison
    numerator: Expr
    denominator: Expr
    terms: tuple[DefinedTerm, ...] = ()
    inputs: tuple[InputRequirement, ...] = Field(min_length=1)
    thresholds: tuple[ThresholdStep, ...] = Field(min_length=1)
    test_period: Literal["ltm_quarterly", "ltm_monthly"] = "ltm_quarterly"
    ratio_decimal_places: int | None = Field(default=2, ge=0, le=6)  # None = no contractual rounding
    rounding: RoundingMode = "ROUND_HALF_UP"
    effective_from: date  # first test date this version governs
    executed_on: date | None = None  # signing date of the amendment, if any
    retroactive_source: SourceSpan | None = None  # clause that makes an amendment retroactive
    extractor_run_id: str
    status: SpecStatus = SpecStatus.DRAFT
    approvals: tuple[Approval, ...] = ()
    seal: Seal | None = None

    @property
    def is_retroactive(self) -> bool:
        return self.executed_on is not None and self.effective_from < self.executed_on

    @model_validator(mode="after")
    def _structure(self) -> CovenantSpec:
        term_names = {t.name for t in self.terms}
        if len(term_names) != len(self.terms):
            raise ValueError("duplicate defined term")
        input_names = {i.name for i in self.inputs}
        if len(input_names) != len(self.inputs):
            raise ValueError("duplicate input requirement")
        for root in self._roots():
            for n in walk(root):
                if isinstance(n, Input) and n.name not in input_names:
                    raise ValueError(f"undeclared input '{n.name}'")
                if isinstance(n, TermRef) and n.name not in term_names:
                    raise ValueError(f"unresolved defined term '{n.name}'")
                if isinstance(n, Lit) and not n.structural and n.source is None:
                    raise ValueError(f"literal {n.value} has no source clause")
        self._assert_acyclic()
        self._assert_thresholds()
        if self.is_retroactive and self.retroactive_source is None:
            raise ValueError(
                "effective_from precedes executed_on: a retroactive amendment must cite the "
                "clause that makes it retroactive (retroactive_source)"
            )
        if self.supersedes is not None and self.version == 1:
            raise ValueError("version 1 cannot supersede another spec")
        if self.status is SpecStatus.DRAFT and (self.seal or self.approvals):
            raise ValueError("a DRAFT spec cannot carry a seal or approvals")
        if self.status in (SpecStatus.VERIFIED, SpecStatus.APPROVED) and self.seal is None:
            raise ValueError(f"{self.status.value.upper()} requires a verifier seal")
        if self.status is SpecStatus.APPROVED and not self.approvals:
            raise ValueError("APPROVED requires a human approval")
        return self

    def _roots(self) -> tuple[Any, ...]:
        return (self.numerator, self.denominator, *[t.formula for t in self.terms])

    def all_spans(self) -> Iterator[SourceSpan]:
        for i in self.inputs:
            yield from i.definition_spans
        for t in self.terms:
            yield from t.definition_spans
        for th in self.thresholds:
            yield from th.level.spans
        for root in self._roots():
            for n in walk(root):
                if isinstance(n, Lit) and n.source:
                    yield n.source
        if self.retroactive_source:
            yield self.retroactive_source

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
        """Identifies the contractual content. Lifecycle fields (status, approvals, seal) are
        excluded so that approving a spec does not change the hash that the seal covers."""
        return content_hash(self, exclude=_SPEC_HASH_EXCLUDES)


# --------------------------------------------------------------------------
# Stream B: borrower financials -> FinancialFact (extracted every quarter)
# --------------------------------------------------------------------------


class TieOutCheck(FrozenModel):
    """Deterministic arithmetic cross-check on extracted facts.

    The verdict is derived: passed = |expected - actual| <= tolerance. It is a property, not a
    field, so no caller (and no LLM output) can supply or override it."""

    name: str  # e.g. "debt_components_sum_to_total_debt"
    expected: Money
    actual: Money
    tolerance: Money = Decimal("0")

    @model_validator(mode="before")
    @classmethod
    def _no_supplied_verdict(cls, data: Any) -> Any:
        if isinstance(data, dict) and "passed" in data:
            raise ValueError(
                "'passed' is derived from expected, actual and tolerance and cannot be supplied"
            )
        return data

    @field_validator("tolerance")
    @classmethod
    def _tolerance(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("tolerance must be >= 0")
        return v

    @property
    def passed(self) -> bool:
        return CALC_CONTEXT.subtract(self.expected, self.actual).copy_abs() <= self.tolerance


class FinancialFact(FrozenModel):
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
    tie_outs: tuple[TieOutCheck, ...] = ()
    seal: Seal | None = None  # present only once SourceVerifier.verify_fact has approved it

    @model_validator(mode="after")
    def _period(self) -> FinancialFact:
        if self.period_start and self.period_start > self.period_end:
            raise ValueError("period_start after period_end")
        return self

    @property
    def tie_outs_passed(self) -> bool:
        return all(t.passed for t in self.tie_outs)

    def content_hash(self) -> str:
        return content_hash(self, exclude=("seal",))


# --------------------------------------------------------------------------
# Output: deterministic result with full computation trace
# --------------------------------------------------------------------------


class ComplianceStatus(str, Enum):
    PASS = "pass"
    PASS_THIN_HEADROOM = "pass_thin_headroom"
    BREACH = "breach"
    INDETERMINATE = "indeterminate"  # missing/unverified input; never defaults to PASS


class TraceStep(FrozenModel):
    """One evaluated node of the expression tree. The trace is the audit lineage:
    value <- operands <- fact_id <- evidence span <- (doc_sha256, page, offset), and
    source_refs point to the contract clauses that define the node."""

    step_id: int
    node_id: str  # path in the spec, e.g. "term:net_debt/diff/1/min/1"
    label: str  # input name, defined-term name, or operator
    op: str
    term: str | None = None  # defined term this node belongs to
    operand_step_ids: tuple[int, ...] = ()
    operand_values: tuple[Money, ...] = ()
    fact_id: str | None = None
    source_refs: tuple[SourceSpan, ...] = ()  # contract clauses
    evidence: SourceSpan | None = None  # borrower-document span, for inputs
    value: Money


class ReviewDecision(FrozenModel):
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


def classify(
    tested: Decimal | None, threshold: Decimal | None, cmp: Comparison, thin_pct: Decimal
) -> tuple[ComplianceStatus, Decimal | None, Decimal | None]:
    """The one place compliance status is decided. Returns (status, headroom_abs, headroom_pct).

    Headroom is measured on the *tested* (contractually rounded) ratio, as a fraction of the
    threshold. Exactly meeting the threshold is a pass (head == 0) with thin headroom."""
    if tested is None or threshold is None or threshold <= 0:
        return ComplianceStatus.INDETERMINATE, None, None
    head = (
        CALC_CONTEXT.subtract(threshold, tested)
        if cmp is Comparison.LE
        else CALC_CONTEXT.subtract(tested, threshold)
    )
    pct = CALC_CONTEXT.divide(head, threshold)
    if head < 0:
        return ComplianceStatus.BREACH, head, pct
    if pct < thin_pct:
        return ComplianceStatus.PASS_THIN_HEADROOM, head, pct
    return ComplianceStatus.PASS, head, pct


_LEAF_OPS = {"lit", "input"}


class CovenantTestResult(FrozenModel):
    result_id: str
    agreement_id: str
    kind: CovenantKind
    spec_id: str
    spec_version: int
    spec_hash: Sha256Hex  # CovenantSpec.content_hash() at compute time
    policy_id: str
    policy_version: int
    policy_hash: Sha256Hex  # BankPolicy.content_hash() at compute time
    thin_headroom_pct: Money  # the policy value actually applied; bound to policy_hash
    engine_version: str
    schema_version: str = SCHEMA_VERSION
    test_date: date
    fact_ids: tuple[str, ...]
    ratio_raw: Money | None
    ratio_tested: Money | None
    threshold: Money | None
    comparison: Comparison
    ratio_decimal_places: int | None
    rounding: RoundingMode
    headroom_abs: Money | None
    headroom_pct: Money | None
    status: ComplianceStatus
    reasons: tuple[str, ...] = ()  # why INDETERMINATE / why routed to review
    trace: tuple[TraceStep, ...] = ()
    tie_outs: tuple[TieOutCheck, ...] = ()
    needs_review: bool
    supersedes_result_id: str | None = None  # set when this recomputes an earlier, stale result
    computed_at: datetime

    @model_validator(mode="after")
    def _consistent(self) -> CovenantTestResult:
        """Independently re-derive everything that can be re-derived. A result that fails any
        of these checks cannot be constructed, so a buggy or tampered writer cannot persist it."""
        self._check_trace()
        self._check_rounding()
        self._check_classification()
        self._check_review()
        return self

    def _check_trace(self) -> None:
        values: dict[int, Decimal] = {}
        for step in self.trace:
            if step.step_id in values:
                raise ValueError(f"duplicate trace step id {step.step_id}")
            if any(i not in values for i in step.operand_step_ids):
                raise ValueError(f"trace step {step.step_id} references a later or unknown step")
            if tuple(values[i] for i in step.operand_step_ids) != step.operand_values:
                raise ValueError(f"trace step {step.step_id}: operand_values disagree with operands")
            if step.op in _LEAF_OPS:
                if step.operand_step_ids:
                    raise ValueError(f"trace step {step.step_id}: leaf '{step.op}' has operands")
            elif step.op == "round":
                expected = round_ratio(step.operand_values[0], self.ratio_decimal_places, self.rounding)
                if expected != step.value:
                    raise ValueError(f"trace step {step.step_id}: rounding result is wrong")
            else:
                try:
                    expected = apply_op(step.op, step.operand_values)
                except (ArithmeticError, ValueError) as exc:
                    raise ValueError(f"trace step {step.step_id} is not computable: {exc}") from exc
                if expected != step.value:
                    raise ValueError(
                        f"trace step {step.step_id} ({step.op}) value {step.value} "
                        f"!= recomputed {expected}"
                    )
            values[step.step_id] = step.value
        if self.ratio_raw is not None:
            ratio_steps = [s for s in self.trace if s.op == "div" and s.label == "ratio"]
            if not ratio_steps or ratio_steps[-1].value != self.ratio_raw:
                raise ValueError("ratio_raw is not the value of the trace's final ratio step")

    def _check_rounding(self) -> None:
        if (self.ratio_raw is None) != (self.ratio_tested is None):
            raise ValueError("ratio_raw and ratio_tested must both be present or both absent")
        if self.ratio_raw is not None and self.ratio_tested is not None:
            expected = round_ratio(self.ratio_raw, self.ratio_decimal_places, self.rounding)
            if expected != self.ratio_tested or expected.as_tuple() != self.ratio_tested.as_tuple():
                raise ValueError(
                    f"ratio_tested {self.ratio_tested} is not ratio_raw {self.ratio_raw} rounded to "
                    f"{self.ratio_decimal_places} places with {self.rounding}"
                )

    def _check_classification(self) -> None:
        status, head_abs, head_pct = classify(
            self.ratio_tested, self.threshold, self.comparison, self.thin_headroom_pct
        )
        if status is not self.status:
            raise ValueError(
                f"status {self.status.value} contradicts the arithmetic, which gives {status.value}"
            )
        if (head_abs, head_pct) != (self.headroom_abs, self.headroom_pct):
            raise ValueError("headroom does not match the recomputed headroom")

    def _check_review(self) -> None:
        must_review = {
            ComplianceStatus.BREACH,
            ComplianceStatus.INDETERMINATE,
            ComplianceStatus.PASS_THIN_HEADROOM,
        }
        if self.status in must_review and not self.needs_review:
            raise ValueError(f"{self.status.value} always requires human review")

    def content_hash(self) -> str:
        return content_hash(self)
