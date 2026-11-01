"""Scorecard: how well does an extraction capture the key facts of the real Viad covenant?

The checklist below is the *contract's* facts, read from the filing text by a person (here: the coding agent that
built this repo, so treat it as a careful reading, not an oracle). Each check is mechanical and deterministic. The
scorecard is meant for honest reporting of a live-model run: run `extract --provider openrouter`, then `score`, and
publish the failures. A high score does not mean the extraction is approvable, and approval does not depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .extraction import ExtractionOutcome
from .models import CovenantSpec, IfGt, Lit, walk

FA = "VIAD-FA1-2022"
EXPECTED_LEVELS = {date(2022, 9, 30): "5.25", date(2022, 12, 31): "4.75", date(2023, 3, 31): "4.50", date(2023, 6, 30): "4.00", date(2024, 12, 31): "4.00"}


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def _lits(spec: CovenantSpec) -> list[Lit]:
    roots = [spec.numerator, spec.denominator, *[t.formula for t in spec.terms]]
    return [n for r in roots for n in walk(r) if isinstance(n, Lit)]


def _lit_with(spec: CovenantSpec, values: set[Decimal], quote_part: str) -> bool:
    return any(l.value in values and l.source is not None and quote_part in l.source.quote for l in _lits(spec))


def score_spec(spec: CovenantSpec) -> list[Check]:
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append(Check(name, bool(ok), detail))

    add("covenant is a maximum total net leverage test", spec.kind.value == "max_total_net_leverage" and spec.comparison.value == "le",
        f"kind={spec.kind.value}, comparison={spec.comparison.value}")
    wrong = {d.isoformat(): (spec.threshold_on(d), exp) for d, exp in EXPECTED_LEVELS.items() if spec.threshold_on(d) != Decimal(exp)}
    add("threshold schedule matches the amended Section 7.10(c)", not wrong, f"mismatches (got, expected): {wrong}" if wrong else "5.25 / 4.75 / 4.50 / 4.00 by test date")
    add("thresholds are cited to the First Amendment", all(s.doc_id == FA for t in spec.thresholds for s in t.level.spans),
        "every threshold quotation is in " + FA)
    add("rounding: three places, half up (Section 1.04)", spec.ratio_decimal_places == 3 and spec.rounding == "ROUND_HALF_UP",
        f"places={spec.ratio_decimal_places}, mode={spec.rounding}")
    add("rounding clause is cited", spec.rounding_source is not None and "one place more" in spec.rounding_source.quote,
        spec.rounding_source.quote[:70] if spec.rounding_source else "no rounding_source")
    add("20.0% cap on the run-rate savings add-back", _lit_with(spec, {Decimal("0.2"), Decimal("0.20"), Decimal("20")}, "20.0%"),
        "a constant 0.20 citing '20.0%'")
    add("cash-netting cap of $50,000,000 present", _lit_with(spec, {Decimal(50_000_000)}, "$50,000,000"), "a constant 50,000,000 citing '$50,000,000'")
    add("cash cap is conditional on $10,000,000 of revolver borrowings", any(isinstance(n, IfGt) for t in spec.terms for n in walk(t.formula))
        and _lit_with(spec, {Decimal(10_000_000)}, "$10,000,000"), "an IfGt node and a constant 10,000,000 citing '$10,000,000'")
    add("new-material-project exclusion cap of $50,000,000", sum(1 for l in _lits(spec) if l.value == Decimal(50_000_000)) >= 2,
        "a second 50,000,000 constant (the first is the cash cap)")
    add("ratio is net funded indebtedness over EBITDA", _ratio_terms(spec), "numerator mentions net funded indebtedness; denominator mentions EBITDA")
    return checks


def _ratio_terms(spec: CovenantSpec) -> bool:
    from .models import TermRef

    num = [n.name for n in walk(spec.numerator) if isinstance(n, TermRef)]
    den = [n.name for n in walk(spec.denominator) if isinstance(n, TermRef)]
    return any("funded" in n or "debt" in n or "indebted" in n for n in num) and any("ebitda" in n for n in den)


def score_outcome(outcome: ExtractionOutcome) -> list[Check]:
    if outcome.spec is None:
        return [Check("extraction produced a spec", False, "; ".join(outcome.blocking_reasons) or "no spec")]
    return [Check("extraction produced a spec", True, ""), *score_spec(outcome.spec)]


def render_scorecard(checks: list[Check]) -> str:
    lines = [f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}" + (f"  ({c.detail})" if c.detail else "") for c in checks]
    passed = sum(c.passed for c in checks)
    lines.append(f"\nScore: {passed}/{len(checks)} key contract facts captured. This is not an approval decision.")
    return "\n".join(lines)
