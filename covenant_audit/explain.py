"""Readable, deterministic explanation of a spec's formulas, generated from the AST by code.

There is no model call here and none may be added: the reviewer must see what the *engine* will compute, not
what an LLM says it will compute. Every function is a pure walk over the typed expression tree."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .models import (
    CovenantSpec,
    Diff,
    Div,
    IfGt,
    Input,
    Lit,
    MaxOf,
    MinOf,
    Mul,
    SourceSpan,
    Sum,
    TermRef,
    walk,
)


def _num(value: Decimal) -> str:
    text = f"{value:,}"
    return text


def explain_expr(expr: Any) -> str:
    """Infix text for an expression. Inputs and defined terms appear by name, e.g. `MIN(cash, 50,000,000)`."""
    if isinstance(expr, Lit):
        return _num(expr.value)
    if isinstance(expr, Input):
        return expr.name
    if isinstance(expr, TermRef):
        return f"<{expr.name}>"
    if isinstance(expr, Sum):
        return "(" + " + ".join(explain_expr(a) for a in expr.args) + ")"
    if isinstance(expr, Diff):
        return f"({explain_expr(expr.left)} - {explain_expr(expr.right)})"
    if isinstance(expr, Mul):
        return f"({explain_expr(expr.left)} x {explain_expr(expr.right)})"
    if isinstance(expr, Div):
        return f"({explain_expr(expr.num)} / {explain_expr(expr.den)})"
    if isinstance(expr, MinOf):
        return "MIN(" + ", ".join(explain_expr(a) for a in expr.args) + ")"
    if isinstance(expr, MaxOf):
        return "MAX(" + ", ".join(explain_expr(a) for a in expr.args) + ")"
    if isinstance(expr, IfGt):
        return (
            f"IF {explain_expr(expr.left)} > {explain_expr(expr.right)} "
            f"THEN {explain_expr(expr.then)} ELSE {explain_expr(expr.otherwise)}"
        )
    return f"<unsupported {type(expr).__name__}>"


def cite(span: SourceSpan) -> str:
    ref = f"{span.clause_ref}, " if span.clause_ref else ""
    return f'{span.doc_id} {ref}p.{span.page}, chars {span.char_start}-{span.char_end}: "{span.quote}"'


def explain_spec(spec: CovenantSpec, *, quote_width: int = 220) -> str:
    """The full readable specification: ratio, inputs, defined terms, schedule, rounding and sources."""
    clip = lambda s: s if len(s) <= quote_width else s[: quote_width - 3] + "..."  # noqa: E731
    cmp_text = "no greater than" if spec.comparison.value == "le" else "no less than"
    lines = [
        f"COVENANT  {spec.kind.value}   (agreement {spec.agreement_id}, spec {spec.spec_id} v{spec.version}, status {spec.status.value})",
        f"  Test: ratio must be {cmp_text} the level for the test date.   Test period: {spec.test_period}",
        f"  RATIO = {explain_expr(spec.numerator)}  /  {explain_expr(spec.denominator)}",
        "",
        "DEFINED TERMS (evaluated by the deterministic engine, in this form)",
    ]
    for t in spec.terms:
        lines.append(f"  <{t.name}> = {explain_expr(t.formula)}")
        for s in t.definition_spans:
            lines.append(f"      source: {clip(cite(s))}")
    lines += ["", "INPUTS (the borrower must supply these, each tied to a source document line)"]
    for i in spec.inputs:
        lines.append(f"  {i.name}  [{i.basis}, sign {i.sign}]  {i.description}")
        for s in i.definition_spans:
            lines.append(f"      source: {clip(cite(s))}")
    lines += ["", "CONSTANTS CITED IN FORMULAS"]
    seen = False
    for t in spec.terms:
        for node in walk(t.formula):
            if isinstance(node, Lit) and node.source is not None:
                seen = True
                lines.append(f"  {_num(node.value)} in <{t.name}>: {clip(cite(node.source))}")
    if not seen:
        lines.append("  (none)")
    lines += ["", "THRESHOLD SCHEDULE"]
    for step in sorted(spec.thresholds, key=lambda s: s.effective_from):
        end = step.effective_to.isoformat() if step.effective_to else "open-ended"
        lines.append(f"  test dates {step.effective_from} to {end}: {step.level.value}x")
        for s in step.level.spans:
            lines.append(f"      source: {clip(cite(s))}")
    rounding = (
        f"{spec.ratio_decimal_places} decimal places, {spec.rounding}"
        if spec.ratio_decimal_places is not None
        else "none specified in the contract"
    )
    lines += [
        "",
        f"ROUNDING  {rounding}",
        *([f"      source: {clip(cite(spec.rounding_source))}"] if spec.rounding_source else []),
        f"EFFECTIVE FROM  {spec.effective_from}" + (f"   executed on {spec.executed_on}" if spec.executed_on else ""),
    ]
    if spec.retroactive_source:
        lines.append(f"  retroactivity clause: {clip(cite(spec.retroactive_source))}")
    lines += ["", "SCOPE NOTES (what this spec deliberately does not model, or how a clause was read)"]
    lines += [f"  - {n}" for n in spec.scope_notes] or ["  (none)"]
    return "\n".join(lines)
