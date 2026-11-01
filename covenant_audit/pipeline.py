"""The deterministic covenant computation pipeline.

    approved spec -> verified facts -> input checks -> AST evaluation -> contractual rounding
    -> classification -> headroom -> trace -> CovenantTestResult -> (human review if required)

Nothing here calls a model. The pipeline refuses to start without an approved, sealed spec, and
it never lets an unverified, stale, missing or ill-fitting fact produce a PASS: such inputs
yield an INDETERMINATE result that names the reason and is routed to a human.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Collection, Iterable

from .base import sha256_hex
from .engine import EvaluationError, Evaluator, required_inputs
from .models import (
    ComplianceStatus,
    CovenantSpec,
    CovenantTestResult,
    FinancialFact,
    InputRequirement,
    TieOutCheck,
    classify,
)
from .policy import BankPolicy
from .verification import SealError, SourceVerifier

ENGINE_VERSION = "0.2.0"


class ReplayMismatchError(Exception):
    """Re-running the stored inputs did not reproduce the stored result."""


def _fit_error(fact: FinancialFact, req: InputRequirement, test_date: date) -> str | None:
    """Does a (sealed) fact describe the right period, basis and sign for its input?"""
    if fact.period_end != test_date:
        return f"fact {fact.fact_id} is for period ending {fact.period_end}, not test date {test_date}"
    if req.basis == "point_in_time":
        if fact.period_start is not None:
            return f"fact {fact.fact_id} is a flow but '{req.name}' is a point-in-time input"
    else:
        if fact.period_start is None:
            return f"fact {fact.fact_id} has no period_start but '{req.name}' is a flow input"
        days = (fact.period_end - fact.period_start).days + 1
        lo, hi = (365, 366) if req.basis == "ltm_flow" else (89, 92)
        if not lo <= days <= hi:
            return f"fact {fact.fact_id} covers {days} days; '{req.name}' needs {req.basis} ({lo}-{hi} days)"
    if req.sign == "positive" and fact.value < 0:
        return f"fact {fact.fact_id} is negative but '{req.name}' must be non-negative"
    return None


def _select_facts(
    spec: CovenantSpec,
    facts: Iterable[FinancialFact],
    test_date: date,
    verifier: SourceVerifier,
    stale_fact_ids: Collection[str],
) -> tuple[dict[str, FinancialFact], list[str], list[FinancialFact]]:
    """Pick exactly one admissible fact per mandatory input. Returns (usable, blocking reasons,
    every candidate considered)."""
    by_name: dict[str, list[FinancialFact]] = defaultdict(list)
    for f in facts:
        by_name[f.input_name].append(f)
    reqs = {r.name: r for r in spec.inputs}
    usable: dict[str, FinancialFact] = {}
    reasons: list[str] = []
    considered: list[FinancialFact] = []
    for name in required_inputs(spec):
        candidates = by_name.get(name, [])
        considered.extend(candidates)
        if not candidates:
            reasons.append(f"missing mandatory input '{name}'")
            continue
        if len(candidates) > 1:
            ids = ", ".join(sorted(c.fact_id for c in candidates))
            reasons.append(f"conflicting facts for '{name}': {ids}")
            continue
        fact = candidates[0]
        try:
            verifier.check_fact(fact)
        except SealError as exc:
            reasons.append(f"input '{name}': {exc}")
            continue
        if fact.fact_id in stale_fact_ids:
            reasons.append(f"input '{name}': fact {fact.fact_id} is stale (restated)")
            continue
        problem = _fit_error(fact, reqs[name], test_date)
        if problem:
            reasons.append(problem)
            continue
        usable[name] = fact
    currencies = {f.currency for f in usable.values()}
    if len(currencies) > 1:
        reasons.append(f"facts are in different currencies: {', '.join(sorted(currencies))}")
        usable = {}
    return usable, reasons, considered


def run_covenant_test(
    spec: CovenantSpec,
    facts: Iterable[FinancialFact],
    test_date: date,
    policy: BankPolicy,
    verifier: SourceVerifier,
    *,
    first_test_after_spec_change: bool = False,
    stale_fact_ids: Collection[str] = (),
    supersedes_result_id: str | None = None,
    now: datetime | None = None,
) -> CovenantTestResult:
    """Run one covenant test for one test date.

    Raises (rather than returning INDETERMINATE) when the *spec* is not approved and sealed:
    that is a programming or process error, not a data-quality outcome."""
    verifier.check_spec(spec, require_approved=True)
    spec_hash = spec.content_hash()
    policy_hash = policy.content_hash()
    threshold = spec.threshold_on(test_date)

    usable, blocking, considered = _select_facts(spec, facts, test_date, verifier, stale_fact_ids)
    if threshold is None:
        blocking.append(f"spec {spec.spec_id} defines no covenant level for test date {test_date}")

    ratio_raw: Decimal | None = None
    ratio_tested: Decimal | None = None
    trace: tuple = ()
    if not blocking:
        evaluator = Evaluator(spec, usable)
        try:
            evaluation = evaluator.evaluate_ratio()
        except EvaluationError as exc:
            blocking.append(f"{type(exc).__name__}: {exc}")
            trace = tuple(evaluator.trace)
        else:
            ratio_raw, ratio_tested, trace = evaluation.ratio_raw, evaluation.ratio_tested, evaluation.trace

    status, headroom_abs, headroom_pct = classify(ratio_tested, threshold, spec.comparison, policy.thin_headroom_pct)

    review: list[str] = []
    if status is not ComplianceStatus.PASS:
        review.append(f"status is {status.value}")
    floor = policy.min_ocr_confidence
    for f in usable.values():
        s = f.source
        if s.text_origin == "ocr" and s.ocr_confidence is not None and Decimal(str(s.ocr_confidence)) < floor:
            review.append(f"fact {f.fact_id} rests on OCR text below the {floor} confidence floor")
    if first_test_after_spec_change:
        review.append("first test after a spec change")
    sample_key = f"{spec.agreement_id}|{spec.kind.value}|{test_date}|{spec_hash}"
    if status is ComplianceStatus.PASS and policy.is_sampled(sample_key):
        review.append("selected by the clean-pass sample")

    tie_outs: tuple[TieOutCheck, ...] = tuple(t for f in usable.values() for t in f.tie_outs)
    used_hashes = sorted(f.content_hash() for f in considered)
    result_id = "R-" + sha256_hex(
        "|".join([spec_hash, policy_hash, ENGINE_VERSION, spec.kind.value, str(test_date), *used_hashes])
    )[:24]
    return CovenantTestResult(
        result_id=result_id,
        agreement_id=spec.agreement_id,
        kind=spec.kind,
        spec_id=spec.spec_id,
        spec_version=spec.version,
        spec_hash=spec_hash,
        policy_id=policy.policy_id,
        policy_version=policy.version,
        policy_hash=policy_hash,
        thin_headroom_pct=policy.thin_headroom_pct,
        engine_version=ENGINE_VERSION,
        test_date=test_date,
        fact_ids=tuple(sorted(f.fact_id for f in considered)),
        ratio_raw=ratio_raw,
        ratio_tested=ratio_tested,
        threshold=threshold,
        comparison=spec.comparison,
        ratio_decimal_places=spec.ratio_decimal_places,
        rounding=spec.rounding,
        headroom_abs=headroom_abs,
        headroom_pct=headroom_pct,
        status=status,
        reasons=(*blocking, *review),
        trace=trace,
        tie_outs=tie_outs,
        needs_review=bool(review),
        supersedes_result_id=supersedes_result_id,
        computed_at=now or datetime.now(timezone.utc),
    )


_REPLAY_FIELDS = (
    "spec_hash", "policy_hash", "thin_headroom_pct", "fact_ids", "ratio_raw", "ratio_tested", "threshold",
    "headroom_abs", "headroom_pct", "status", "trace", "ratio_decimal_places", "rounding",
)


def replay_result(
    stored: CovenantTestResult,
    spec: CovenantSpec,
    facts: Iterable[FinancialFact],
    policy: BankPolicy,
    verifier: SourceVerifier,
) -> CovenantTestResult:
    """Independent audit: recompute from the stored inputs and require identical numbers, trace and
    status. Raises ReplayMismatchError listing every field that differs."""
    again = run_covenant_test(spec, facts, stored.test_date, policy, verifier, now=stored.computed_at)
    diffs = [f for f in _REPLAY_FIELDS if getattr(again, f) != getattr(stored, f)]
    if diffs:
        raise ReplayMismatchError(f"replay of {stored.result_id} differs in: {', '.join(diffs)}")
    if stored.engine_version != again.engine_version:
        raise ReplayMismatchError(
            f"stored result used engine {stored.engine_version}, replay used {again.engine_version}"
        )
    return again
