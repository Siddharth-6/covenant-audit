"""AST evaluator: every operation, Decimal exactness, guards, and the audit trace.

Each test states its expected result and why."""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from covenant_audit.base import evolve
from covenant_audit.engine import (
    CyclicTermError,
    Evaluator,
    MissingInputError,
    NonPositiveDenominatorError,
    UnresolvedTermError,
    UnsupportedExpressionError,
    ZeroDenominatorError,
    required_inputs,
)
from covenant_audit.models import (
    CovenantSpec,
    DefinedTerm,
    Div,
    Diff,
    IfGt,
    Input,
    Lit,
    MaxOf,
    MinOf,
    Mul,
    Sum,
    TermRef,
)
from covenant_audit.pipeline import run_covenant_test

from .factory import NOW, SAMPLE_VALUES, World

D = Decimal
TEST_DATE = date(2025, 6, 30)


@pytest.fixture()
def world() -> World:
    return World()


def lit(v: str, structural: bool = True) -> Lit:
    return Lit(value=D(v), structural=structural)


def mini_spec(world: World, numerator, denominator, terms=()) -> CovenantSpec:
    """A DRAFT spec with a custom ratio expression over the standard inputs."""
    return evolve(world.build_spec(approve=False), numerator=numerator, denominator=denominator, terms=tuple(terms))


def evaluate(world: World, numerator, denominator, values=SAMPLE_VALUES, terms=()):
    facts = {k: v for k, v in world.package(values, seal=False).items()}
    return Evaluator(mini_spec(world, numerator, denominator, terms), facts).evaluate_ratio()


# ---------------------------------------------------------------- worked example


def test_worked_example_is_produced_by_the_ast(world):
    """Expected: net debt 500, add-back cap 25, accepted add-backs MIN(40,25)=25, capped EBITDA 125, ratio 4.00x.
    Why: these values come from evaluating the spec's expression tree over the verified facts, not from
    hardcoded formulas; the trace exposes each intermediate."""
    spec = world.build_spec()
    facts = world.package(SAMPLE_VALUES)
    evaluation = Evaluator(spec, facts).evaluate_ratio()
    by_node = {s.node_id: s for s in evaluation.trace}
    M = lambda x: D(x) * 1_000_000  # noqa: E731
    assert by_node["term:net_debt"].value == M(500)
    assert by_node["term:accepted_addbacks/min/1/mul"].value == M(25)  # the cap: 25% x 100
    accepted = by_node["term:accepted_addbacks"]
    assert accepted.operand_values == (M(25),) and accepted.value == M(25)
    assert accepted.term == "accepted_addbacks"
    assert by_node["term:accepted_addbacks/min"].operand_values == (M(40), M(25))  # MIN(40, 25)
    assert by_node["term:consolidated_ebitda"].value == M(125)
    assert evaluation.ratio_raw == D(4)
    assert evaluation.ratio_tested == D("4.00") and str(evaluation.ratio_tested) == "4.00"


def test_trace_links_inputs_to_facts_evidence_and_clauses(world):
    """Expected: input steps carry fact_id and the borrower-document evidence span; literal steps carry the clause
    that defines them; term steps carry their definition clauses.
    Why: an auditor must go from any number to the page, offset and quote that justify it."""
    spec = world.build_spec()
    facts = world.package(SAMPLE_VALUES)
    trace = Evaluator(spec, facts).evaluate_ratio().trace
    debt = next(s for s in trace if s.op == "input" and s.label == "total_debt")
    assert debt.fact_id == "F-2025-06-30-total_debt"
    assert debt.evidence is not None and debt.evidence.quote.startswith("Total debt:")
    assert debt.source_refs and debt.source_refs[0].clause_ref == "Section 1.01"
    cap = next(s for s in trace if s.op == "lit" and s.value == D("0.25"))
    assert cap.source_refs[0].quote == "twenty-five percent (25%)"
    net_debt = next(s for s in trace if s.op == "term" and s.label == "net_debt")
    assert net_debt.source_refs[0].quote.startswith('"Consolidated Net Debt"')


def test_shared_term_is_evaluated_once(world):
    """Expected: a term used twice appears as one trace step referenced by both users.
    Why: the trace stays a DAG, so an auditor sees one value for one defined term."""
    shared = TermRef(name="net_debt")
    spec_terms = world.build_spec(approve=False).terms
    ev = evaluate(world, Sum(args=(shared, shared)), TermRef(name="consolidated_ebitda"), terms=spec_terms)
    assert sum(1 for s in ev.trace if s.node_id == "term:net_debt") == 1
    top = next(s for s in ev.trace if s.node_id == "ratio.numerator/sum")
    assert top.operand_step_ids[0] == top.operand_step_ids[1]
    assert top.value == 1_000_000_000


# ---------------------------------------------------------------- operations


@pytest.mark.parametrize(
    "numerator,expected",
    [
        (Sum(args=(Input(name="total_debt"), Input(name="cash"), lit("5"))), "580000005"),
        (Diff(left=Input(name="total_debt"), right=Input(name="cash")), "500000000"),
        (Mul(left=Input(name="cash"), right=lit("2")), "80000000"),
        (Div(num=Input(name="total_debt"), den=lit("4")), "135000000"),
        (MinOf(args=(Input(name="total_debt"), Input(name="cash"), lit("7"))), "7"),
        (MaxOf(args=(Input(name="total_debt"), Input(name="cash"), lit("7"))), "540000000"),
        (Diff(left=Input(name="cash"), right=Input(name="total_debt")), "-500000000"),
    ],
)
def test_every_operation_evaluates(world, numerator, expected):
    """Expected: the arithmetic result in the table (sum, diff, mul, div, min, max, negative result).
    Why: each AST node type maps to exactly one Decimal operation."""
    ev = evaluate(world, numerator, lit("1"))
    assert ev.ratio_raw == D(expected)
    assert all(isinstance(s.value, Decimal) for s in ev.trace)


def test_decimal_arithmetic_is_exact_not_binary_float(world):
    """Expected: 0.1 + 0.2 evaluates to exactly 0.3, and 1/3 carries 50 significant digits.
    Why: Decimal under an explicit 50-digit context; float would give 0.30000000000000004."""
    spec = mini_spec(world, Sum(args=(Input(name="cash"), Input(name="addbacks"))), lit("1"))
    base = world.package(SAMPLE_VALUES, seal=False)
    facts = {"cash": evolve(base["cash"], value=D("0.1")), "addbacks": evolve(base["addbacks"], value=D("0.2"))}
    assert Evaluator(spec, facts).evaluate_ratio().ratio_raw == D("0.3")
    third = Evaluator(mini_spec(world, lit("1"), lit("3")), {}).evaluate_ratio()
    assert len(third.ratio_raw.as_tuple().digits) == 50


def test_contractual_rounding_is_applied_with_the_declared_mode(world):
    """Expected: 10/3 gives raw 3.333..., tested 3.33 (two places); with places=None the exact ratio is tested.
    Why: the contract's rounding rule is part of the spec and appears as a trace step."""
    spec = evolve(mini_spec(world, lit("10"), lit("3")), ratio_decimal_places=2)
    ev = Evaluator(spec, {}).evaluate_ratio()
    assert ev.ratio_tested == D("3.33") and ev.trace[-1].op == "round"
    exact = Evaluator(evolve(spec, ratio_decimal_places=None), {}).evaluate_ratio()
    assert exact.ratio_tested == exact.ratio_raw and exact.trace[-1].op == "div"


# ---------------------------------------------------------------- guards


def test_missing_input_is_reported_by_name(world):
    """Expected: MissingInputError('cash'). Why: the evaluator never invents or defaults a value."""
    facts = world.package(SAMPLE_VALUES, omit=("cash",), seal=False)
    with pytest.raises(MissingInputError) as exc:
        Evaluator(world.build_spec(approve=False), facts).evaluate_ratio()
    assert exc.value.name == "cash"


def test_required_inputs_follow_terms(world):
    """Expected: the four inputs reachable from the ratio. Why: they are used through defined terms."""
    assert required_inputs(world.build_spec(approve=False)) == ("addbacks", "base_ebitda", "cash", "total_debt")
    only_cash = mini_spec(world, Input(name="cash"), lit("1"))
    assert required_inputs(only_cash) == ("cash",)


def test_division_by_zero_inside_the_tree(world):
    """Expected: ZeroDenominatorError. Why: x / 0 has no value; it must not become Infinity or NaN."""
    with pytest.raises(ZeroDenominatorError, match="division by zero"):
        evaluate(world, Div(num=Input(name="cash"), den=lit("0")), lit("1"))


def test_zero_ratio_denominator(world):
    """Expected: ZeroDenominatorError when EBITDA is 0. Why: the ratio is undefined, not infinite."""
    with pytest.raises(ZeroDenominatorError):
        evaluate(world, TermRef(name="net_debt"), TermRef(name="consolidated_ebitda"),
                 values={**SAMPLE_VALUES, "base_ebitda": "0", "addbacks": "0"}, terms=world.build_spec(approve=False).terms)


def test_negative_ratio_denominator_is_never_computed(world):
    """Expected: NonPositiveDenominatorError for negative EBITDA.
    Why: net debt / negative EBITDA is negative, which would 'pass' a maximum-leverage covenant. A human must decide."""
    with pytest.raises(NonPositiveDenominatorError):
        evaluate(world, TermRef(name="net_debt"), TermRef(name="consolidated_ebitda"),
                 values={**SAMPLE_VALUES, "base_ebitda": "-50", "addbacks": "0"}, terms=world.build_spec(approve=False).terms)


def test_pipeline_turns_evaluation_failures_into_indeterminate_never_pass(world):
    """Expected: INDETERMINATE with the reason and needs_review for zero and negative EBITDA.
    Why: failures to compute are surfaced to a human; they never default to PASS."""
    spec = world.build_spec()
    for ebitda, name in (("0", "ZeroDenominatorError"), ("-50", "NonPositiveDenominatorError")):
        facts = world.package({**SAMPLE_VALUES, "base_ebitda": ebitda, "addbacks": "0"})
        r = run_covenant_test(spec, facts.values(), TEST_DATE, world.policy, world.verifier, now=NOW)
        assert r.status.value == "indeterminate" and r.needs_review
        assert r.ratio_raw is None and any(name in reason for reason in r.reasons)


def _unvalidated_spec(world: World, **fields) -> CovenantSpec:
    """Bypass model validators (as a buggy writer or hostile deserialiser might)."""
    base = world.build_spec(approve=False)
    return CovenantSpec.model_construct(**{**dict(base), **fields})


def test_cycle_is_detected_by_the_evaluator_itself(world):
    """Expected: CyclicTermError naming the cycle, even if model validation was bypassed.
    Why: the evaluator does not rely on the spec validator; it tracks the active term stack."""
    span = world.build_spec(approve=False).terms[0].definition_spans
    terms = (
        DefinedTerm.model_construct(name="term_a", formula=TermRef(name="term_b"), definition_spans=span, amended_by=()),
        DefinedTerm.model_construct(name="term_b", formula=TermRef(name="term_a"), definition_spans=span, amended_by=()),
    )
    spec = _unvalidated_spec(world, terms=terms, numerator=TermRef(name="term_a"), denominator=lit("1"))
    with pytest.raises(CyclicTermError, match="term_a -> term_b -> term_a"):
        Evaluator(spec, {}).evaluate_ratio()


def test_unresolved_term_is_detected_by_the_evaluator_itself(world):
    """Expected: UnresolvedTermError. Why: a reference to an undefined term has no value."""
    spec = _unvalidated_spec(world, numerator=TermRef(name="ghost_term"), denominator=lit("1"))
    with pytest.raises(UnresolvedTermError, match="ghost_term"):
        Evaluator(spec, {}).evaluate_ratio()


def test_unsupported_expression_type_is_rejected(world):
    """Expected: UnsupportedExpressionError. Why: unknown node types are refused, never interpreted or exec'd."""

    class Power(BaseModel):
        op: str = "pow"

    spec = _unvalidated_spec(world, numerator=Power(), denominator=lit("1"))
    with pytest.raises(UnsupportedExpressionError, match="Power"):
        Evaluator(spec, {}).evaluate_ratio()


def test_engine_source_contains_no_eval_exec_or_float():
    """Expected: no eval/exec/compile/float() calls in the calculation modules.
    Why: the engine's guarantee is that no LLM-supplied code or binary float reaches a calculation."""
    import ast
    import pathlib

    pkg = pathlib.Path(__file__).parent.parent / "covenant_audit"
    for name in ("engine.py", "arith.py", "pipeline.py"):
        tree = ast.parse((pkg / name).read_text())
        called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert not called & {"eval", "exec", "compile", "float", "__import__"}, (name, called)


# ---------------------------------------------------------------- result validator re-checks the trace


def _good_result(world):
    spec = world.build_spec()
    facts = world.package(SAMPLE_VALUES)
    return run_covenant_test(spec, facts.values(), TEST_DATE, world.policy, world.verifier, now=NOW)


def test_result_trace_is_independently_rechecked(world):
    """Expected: ValidationError when a trace value, operand value, or reference is altered.
    Why: the result validator recomputes every operation from its operands with the same arith module."""
    good = _good_result(world)
    idx = next(i for i, s in enumerate(good.trace) if s.op == "min" and s.term == "accepted_addbacks")
    step = good.trace[idx]

    def with_step(new):
        return evolve(good, trace=(*good.trace[:idx], new, *good.trace[idx + 1 :]))

    with pytest.raises(ValidationError, match="recomputed"):
        with_step(evolve(step, value=D("40000000")))  # claims MIN(40, 25) = 40 (cap ignored)
    with pytest.raises(ValidationError, match="operand_values disagree"):
        with_step(evolve(step, operand_values=(D("40000000"), D("99000000"))))
    with pytest.raises(ValidationError, match="later or unknown"):
        with_step(evolve(step, operand_step_ids=(99, 98)))
    ratio_idx = next(i for i, s in enumerate(good.trace) if s.label == "ratio")
    with pytest.raises(ValidationError):
        evolve(good, trace=(*good.trace[:ratio_idx], evolve(good.trace[ratio_idx], value=D("3")), *good.trace[ratio_idx + 1 :]))


def test_result_ratio_must_equal_the_trace_ratio(world):
    """Expected: ValidationError when ratio_raw differs from the trace's final ratio step.
    Why: the headline number must be the number the trace computed."""
    good = _good_result(world)
    with pytest.raises(ValidationError, match="final ratio step"):
        evolve(good, ratio_raw=D("3.99"), ratio_tested=D("3.99"), headroom_abs=D("0.51"),
               headroom_pct=D("0.51") / D("4.5"))


# ---------------------------------------------------------------- IfGt (conditional)


def _conditional(world, revolver: str):
    """Netted cash is capped at 50M only if revolver borrowings exceed 10M (the real Viad clause shape)."""
    expr = IfGt(left=Input(name="addbacks"), right=lit("10000000"),
                then=MinOf(args=(Input(name="cash"), lit("50000000"))), otherwise=Input(name="cash"))
    return evaluate(world, expr, lit("1"), values={**SAMPLE_VALUES, "cash": "80", "addbacks": revolver})


def test_ifgt_selects_the_branch_and_traces_all_four_operands(world):
    """Expected: cash 80M is capped to 50M when the condition input (12M) exceeds 10M, and left at 80M when it is 5M.
    Why: IfGt returns `then` iff left > right; every operand is evaluated and appears in the trace."""
    capped = _conditional(world, "12")
    assert capped.ratio_raw == D("50000000")
    step = next(s for s in capped.trace if s.op == "ifgt")
    assert step.operand_values == (D("12000000"), D("10000000"), D("50000000"), D("80000000"))
    assert _conditional(world, "5").ratio_raw == D("80000000")


def test_ifgt_boundary_is_strictly_greater(world):
    """Expected: a condition value exactly equal to the 10M threshold takes the `otherwise` branch (80M).
    Why: the contract says 'exceeds', which is strict."""
    assert _conditional(world, "10").ratio_raw == D("80000000")


def test_ifgt_trace_is_rechecked_by_the_result_validator(world):
    """Expected: a result whose ifgt step records the wrong branch is rejected; the right branch is accepted.
    Why: arith.apply_op re-derives the selected branch from the recorded operands, as it does for every other op."""
    from covenant_audit.models import Comparison, TraceStep, classify

    from .test_models import make_result

    def trace(selected):
        leaf = lambda i, v: TraceStep(step_id=i, node_id=f"n{i}", label="x", op="lit", value=D(v))  # noqa: E731
        ops = (D("3"), D("2"), D("1.5"), D("2.5"))
        return (
            leaf(1, "3"), leaf(2, "2"), leaf(3, "1.5"), leaf(4, "2.5"),
            TraceStep(step_id=5, node_id="c", label="ifgt", op="ifgt", operand_step_ids=(1, 2, 3, 4), operand_values=ops, value=selected),
            leaf(6, "1"),
            TraceStep(step_id=7, node_id="r", label="ratio", op="div", operand_step_ids=(5, 6), operand_values=(selected, D("1")), value=selected),
        )

    ratio = D("1.5")
    status, head, pct = classify(ratio, D("4.50"), Comparison.LE, D("0.10"))
    base = dict(ratio_raw=ratio, ratio_tested=ratio, ratio_decimal_places=None, headroom_abs=head, headroom_pct=pct, status=status)
    assert make_result(**base, trace=trace(D("1.5"))).ratio_raw == ratio  # 3 > 2 selects `then`
    with pytest.raises(ValidationError, match="recomputed"):
        make_result(**base, trace=trace(D("2.5")))  # claims the `otherwise` branch
