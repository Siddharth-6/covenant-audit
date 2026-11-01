"""Deterministic semantic checks on a spec, run before a human is asked to approve it.

Source verification proves a quotation is real. It does not prove the extraction means what the contract
means. These checks catch a class of *cheap, mechanical* mistakes that a syntactically valid extraction can
still contain: a threshold that is not the number in its own quote, a constant marked 'structural' to dodge
the citation rule, an input declared but never used, a maximum covenant tested with >=. They do not replace
the reviewer: a wrong-but-consistent reading of a clause passes all of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .models import CovenantSpec, Input, Lit, TermRef, walk
from .verification import amounts_in


@dataclass(frozen=True)
class Finding:
    severity: Literal["error", "warning"]
    code: str
    message: str


def _matches_quote(value: Decimal, quote: str) -> bool | None:
    """True/False if the quote prints numbers that do/do not include `value` (as is, or as a percentage);
    None if the quote prints no number at all, so the value cannot be machine-checked."""
    printed = amounts_in(quote)
    if not printed:
        return None
    return any(a == value or a / Decimal(100) == value for a in printed)


def check_spec(spec: CovenantSpec) -> list[Finding]:
    findings: list[Finding] = []

    expected = "le" if spec.kind.value.startswith("max_") else "ge"
    if spec.comparison.value != expected:
        findings.append(Finding("error", "comparison_direction",
                                f"{spec.kind.value} must be tested with '{expected}', not '{spec.comparison.value}'"))

    terms = {t.name: t for t in spec.terms}
    used_inputs, used_terms = _reachable(spec.numerator, terms)
    den_inputs, den_terms = _reachable(spec.denominator, terms)
    used_inputs |= den_inputs
    used_terms |= den_terms
    for i in spec.inputs:
        if i.name not in used_inputs:
            findings.append(Finding("error", "unused_input", f"input '{i.name}' is declared but never used by the ratio"))
    for t in spec.terms:
        if t.name not in used_terms:
            findings.append(Finding("error", "unused_term", f"defined term '{t.name}' is never used by the ratio"))
    if not den_inputs:
        findings.append(Finding("error", "constant_denominator", "the denominator uses no financial input"))

    for step in spec.thresholds:
        for span in step.level.spans:
            ok = _matches_quote(step.level.value, span.quote)
            if ok is False:
                findings.append(Finding("error", "threshold_not_in_quote",
                                        f"threshold {step.level.value} does not appear in its quotation: \"{span.quote[:80]}\""))
            elif ok is None:
                findings.append(Finding("warning", "threshold_unchecked", f"threshold {step.level.value}: its quotation prints no number"))

    roots = [spec.numerator, spec.denominator, *[t.formula for t in spec.terms]]
    for root in roots:
        for n in walk(root):
            if not isinstance(n, Lit):
                continue
            if n.structural and n.value not in (Decimal(0), Decimal(1)):
                findings.append(Finding("error", "structural_abuse",
                                        f"literal {n.value} is marked structural but is not 0 or 1; it needs a cited source"))
            if n.source is not None:
                ok = _matches_quote(n.value, n.source.quote)
                if ok is False:
                    findings.append(Finding("error", "literal_not_in_quote",
                                            f"constant {n.value} does not appear in its quotation: \"{n.source.quote[:80]}\""))
                elif ok is None:
                    findings.append(Finding("warning", "literal_unchecked", f"constant {n.value}: its quotation prints no number"))

    if spec.ratio_decimal_places is not None and spec.rounding_source is None:
        findings.append(Finding("error", "rounding_without_source", "a rounding rule is recorded but no clause is cited for it"))
    if spec.ratio_decimal_places is None:
        findings.append(Finding("warning", "no_rounding_rule", "no contractual rounding rule recorded"))
    return findings


def _reachable(root, terms) -> tuple[set[str], set[str]]:
    """Inputs and defined terms reachable from `root`, following term references transitively."""
    inputs: set[str] = set()
    seen_terms: set[str] = set()
    stack = [root]
    while stack:
        for n in walk(stack.pop()):
            if isinstance(n, Input):
                inputs.add(n.name)
            elif isinstance(n, TermRef) and n.name in terms and n.name not in seen_terms:
                seen_terms.add(n.name)
                stack.append(terms[n.name].formula)
    return inputs, seen_terms


def errors(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == "error"]
