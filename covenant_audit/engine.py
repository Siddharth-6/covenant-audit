"""Deterministic AST evaluator with a full calculation trace.

The evaluator walks the typed expression tree of an approved `CovenantSpec`. There is no
`eval`, no code generation and no float: every value is a `Decimal` and every operation is
`arith.apply_op`. Each evaluated node appends a `TraceStep`, so the final ratio can be
reconstructed (and re-checked by `CovenantTestResult`) from the trace alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from .arith import DivisionByZeroError, RoundingMode, apply_op, round_ratio
from .models import (
    CovenantSpec,
    Div,
    Diff,
    FinancialFact,
    Input,
    Lit,
    MaxOf,
    MinOf,
    Mul,
    SourceSpan,
    Sum,
    TermRef,
    TraceStep,
    children,
)


class EvaluationError(Exception):
    """Base class: the expression could not be evaluated to a number."""


class MissingInputError(EvaluationError):
    def __init__(self, name: str) -> None:
        super().__init__(f"missing mandatory input '{name}'")
        self.name = name


class UnresolvedTermError(EvaluationError):
    """A TermRef names a defined term that does not exist."""


class CyclicTermError(EvaluationError):
    """Defined terms refer to each other in a cycle."""


class ZeroDenominatorError(EvaluationError):
    """A division had a zero denominator."""


class NonPositiveDenominatorError(EvaluationError):
    """The ratio's denominator is negative. A negative EBITDA would make a leverage ratio
    negative, which would wrongly satisfy a maximum covenant, so it is never computed."""


class UnsupportedExpressionError(EvaluationError):
    """The tree contains a node type the engine does not implement."""


@dataclass(frozen=True)
class RatioEvaluation:
    ratio_raw: Decimal
    ratio_tested: Decimal
    trace: tuple[TraceStep, ...]


def required_inputs(spec: CovenantSpec) -> tuple[str, ...]:
    """Inputs actually reachable from the ratio (declared-but-unused inputs are not mandatory)."""
    terms = {t.name: t for t in spec.terms}
    needed: list[str] = []
    seen_terms: set[str] = set()
    stack: list[Any] = [spec.numerator, spec.denominator]
    while stack:
        node = stack.pop()
        if isinstance(node, Input) and node.name not in needed:
            needed.append(node.name)
        elif isinstance(node, TermRef) and node.name in terms and node.name not in seen_terms:
            seen_terms.add(node.name)
            stack.append(terms[node.name].formula)
        stack.extend(children(node))
    return tuple(sorted(needed))


class Evaluator:
    """Evaluates one spec against one set of verified facts, keyed by input name.

    The evaluator does not decide whether the facts are trustworthy; the pipeline admits only
    sealed facts. It does defend itself against a malformed spec (cycles, unresolved terms,
    unknown node types), because a spec built with `model_construct` could skip model validators."""

    def __init__(self, spec: CovenantSpec, facts: Mapping[str, FinancialFact]) -> None:
        self._terms = {t.name: t for t in spec.terms}
        self._inputs = {i.name: i for i in spec.inputs}
        self._spec = spec
        self._facts = facts
        self._values: dict[int, Decimal] = {}
        self._term_steps: dict[str, int] = {}
        self._stack: list[str] = []
        self.trace: list[TraceStep] = []

    # -- public -----------------------------------------------------------

    def evaluate_ratio(self) -> RatioEvaluation:
        spec = self._spec
        num = self._eval(spec.numerator, "ratio.numerator", None)
        den = self._eval(spec.denominator, "ratio.denominator", None)
        denominator = self._values[den]
        if denominator == 0:
            raise ZeroDenominatorError("the ratio's denominator is zero")
        if denominator < 0:
            raise NonPositiveDenominatorError(
                f"the ratio's denominator is negative ({denominator}); contractual treatment needs human review"
            )
        ratio = self._emit("ratio", "ratio", "div", None, (num, den))
        raw = self._values[ratio]
        if spec.ratio_decimal_places is None:
            tested = raw
        else:
            rounded = round_ratio(raw, spec.ratio_decimal_places, spec.rounding)
            self._emit("ratio.rounded", "rounded", "round", None, (ratio,), forced_value=rounded)
            tested = rounded
        return RatioEvaluation(raw, tested, tuple(self.trace))

    # -- internals --------------------------------------------------------

    def _emit(
        self,
        node_id: str,
        label: str,
        op: str,
        term: str | None,
        operand_ids: tuple[int, ...],
        *,
        fact_id: str | None = None,
        source_refs: tuple[SourceSpan, ...] = (),
        evidence: SourceSpan | None = None,
        forced_value: Decimal | None = None,
        leaf_value: Decimal | None = None,
    ) -> int:
        operands = tuple(self._values[i] for i in operand_ids)
        if leaf_value is not None:
            value = leaf_value
        elif forced_value is not None:
            value = forced_value
        else:
            try:
                value = apply_op(op, operands)
            except DivisionByZeroError as exc:
                raise ZeroDenominatorError(f"division by zero at {node_id}") from exc
        step_id = len(self.trace) + 1
        self._values[step_id] = value
        self.trace.append(
            TraceStep(
                step_id=step_id,
                node_id=node_id,
                label=label,
                op=op,
                term=term,
                operand_step_ids=operand_ids,
                operand_values=operands,
                fact_id=fact_id,
                source_refs=source_refs,
                evidence=evidence,
                value=value,
            )
        )
        return step_id

    def _eval(self, expr: Any, node_id: str, term: str | None) -> int:
        if isinstance(expr, Lit):
            refs = (expr.source,) if expr.source is not None else ()
            return self._emit(node_id, "literal", "lit", term, (), source_refs=refs, leaf_value=expr.value)
        if isinstance(expr, Input):
            fact = self._facts.get(expr.name)
            if fact is None:
                raise MissingInputError(expr.name)
            req = self._inputs.get(expr.name)
            refs = req.definition_spans if req is not None else ()
            return self._emit(
                node_id, expr.name, "input", term, (), fact_id=fact.fact_id,
                source_refs=refs, evidence=fact.source, leaf_value=fact.value,
            )
        if isinstance(expr, TermRef):
            return self._eval_term(expr.name)
        if isinstance(expr, (Sum, MinOf, MaxOf, Diff, Mul, Div)):
            kids = children(expr)
            names = _child_names(expr)
            ids = tuple(self._eval(k, f"{node_id}/{expr.op}/{n}", term) for k, n in zip(kids, names))
            return self._emit(f"{node_id}/{expr.op}", expr.op, expr.op, term, ids)
        raise UnsupportedExpressionError(f"unsupported expression node {type(expr).__name__!r} at {node_id}")

    def _eval_term(self, name: str) -> int:
        cached = self._term_steps.get(name)
        if cached is not None:
            return cached
        if name in self._stack:
            raise CyclicTermError("cyclic term reference: " + " -> ".join([*self._stack, name]))
        term = self._terms.get(name)
        if term is None:
            raise UnresolvedTermError(f"unresolved defined term '{name}'")
        self._stack.append(name)
        try:
            body = self._eval(term.formula, f"term:{name}", name)
        finally:
            self._stack.pop()
        step = self._emit(
            f"term:{name}", name, "term", name, (body,), source_refs=term.definition_spans
        )
        self._term_steps[name] = step
        return step


def _child_names(expr: Any) -> tuple[str, ...]:
    """Stable names for each child position, used to build node ids."""
    if isinstance(expr, (Sum, MinOf, MaxOf)):
        return tuple(str(i) for i in range(len(expr.args)))
    if isinstance(expr, (Diff, Mul)):
        return ("left", "right")
    return ("num", "den")
