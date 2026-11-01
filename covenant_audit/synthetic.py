"""Synthetic borrower financials for demonstrations: a fake statement document plus sealed facts.

Everything produced here is invented. It is built from the *spec's own input list*, so it works for whatever
inputs a (live or recorded) extraction declared, provided the caller supplies a value for each. Amounts are
given in USD millions and printed at scale 1,000,000, which exercises the verifier's scale check."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Mapping

from .base import sha256_hex
from .models import CovenantSpec, FinancialFact
from .policy import BankPolicy
from .verification import DocumentStore, SourceVerifier


class SyntheticDataError(Exception):
    pass


def build_synthetic_facts(
    spec: CovenantSpec,
    values_millions: Mapping[str, str],
    test_date: date,
    store: DocumentStore,
    verifier: SourceVerifier,
    policy: BankPolicy,
    *,
    borrower: str = "SYNTHETIC BORROWER CORP",
) -> list[FinancialFact]:
    missing = [i.name for i in spec.inputs if i.name not in values_millions]
    unknown = [k for k in values_millions if k not in {i.name for i in spec.inputs}]
    if missing or unknown:
        raise SyntheticDataError(
            f"synthetic financials must match the spec's inputs exactly; missing: {missing or '-'}; not in spec: {unknown or '-'}"
        )
    lines = [f"{borrower} - financial statements and compliance schedule for the period ended {test_date.isoformat()} (SYNTHETIC, USD millions)"]
    lines += [f"{i.name} ({i.description}): {Decimal(values_millions[i.name]):,}" for i in spec.inputs]
    doc_id = f"SYN-FS-{test_date.isoformat()}-{sha256_hex(repr(sorted(values_millions.items())))[:6]}"
    store.register(doc_id, ("\n".join(lines),))
    ltm_start = test_date.replace(year=test_date.year - 1) + timedelta(days=1)
    facts: list[FinancialFact] = []
    for req in spec.inputs:
        amount = Decimal(values_millions[req.name])
        quote = f"{req.name} ({req.description}): {amount:,}"
        span = store.locate(doc_id, 1, quote)
        flow = req.basis != "point_in_time"
        start = None
        if req.basis == "ltm_flow":
            start = ltm_start
        elif req.basis == "quarter_flow":
            start = test_date - timedelta(days=89)
        fact = FinancialFact(
            fact_id=f"F-{test_date.isoformat()}-{req.name}", input_name=req.name, value=amount * Decimal(1_000_000),
            currency="USD", reported_scale=1_000_000, period_start=start if flow else None, period_end=test_date,
            origin="unaudited_fs", source=span, extractor_run_id="synthetic-generator",
        )
        facts.append(verifier.verify_fact(fact, policy))
    return facts
