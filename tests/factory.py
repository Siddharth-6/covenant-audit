"""Test and demo fixtures. Everything here is SYNTHETIC: the credit agreement text is invented
for illustration, and no real borrower or lender data is used."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping

from covenant_audit.base import sha256_hex
from covenant_audit.models import (
    Approval,
    Comparison,
    CovenantKind,
    CovenantSpec,
    DefinedTerm,
    Diff,
    FinancialFact,
    Input,
    InputRequirement,
    Lit,
    MinOf,
    Mul,
    Sourced,
    Sum,
    TermRef,
    ThresholdStep,
    TieOutCheck,
)
from covenant_audit.policy import BankPolicy, TieOutRequirement
from covenant_audit.verification import DocumentStore, SourceVerifier, approve_spec

NOW = datetime(2025, 8, 15, 9, 0, tzinfo=timezone.utc)

CREDIT_AGREEMENT = (
    "SYNTHETIC CREDIT AGREEMENT (invented for illustration)\n"
    "Section 1.01 Definitions.\n"
    '"Consolidated EBITDA" means Consolidated Net Income plus add-backs, provided that add-backs '
    "shall not exceed twenty-five percent (25%) of Consolidated EBITDA before add-backs.\n"
    '"Consolidated Total Debt" means all Indebtedness of the Borrower for borrowed money.\n'
    '"Consolidated Net Debt" means Consolidated Total Debt minus Unrestricted Cash, provided that '
    "Unrestricted Cash netted shall not exceed $50,000,000.\n",
    "Section 7.11 Financial Covenants.\n"
    "(a) Maximum Total Net Leverage Ratio. The Borrower shall not permit the ratio of Consolidated "
    "Net Debt to Consolidated EBITDA to exceed 4.50 to 1.00 for any Test Period ending on or before "
    "June 30, 2025, and 4.00 to 1.00 for any Test Period ending thereafter.\n"
    "The ratio shall be calculated to two decimal places, rounding half up.\n",
)

AMENDMENT_1 = (
    "SYNTHETIC AMENDMENT NO. 1 (invented for illustration)\n"
    "Effective July 1, 2025, add-backs shall not exceed fifteen percent (15%) of Consolidated EBITDA "
    "before add-backs.\n"
    "This Amendment applies to Test Periods ending on or after July 1, 2025.\n"
    "Notwithstanding the foregoing, the Lenders agree this Amendment applies to the Test Period ending "
    "June 30, 2025.\n",
)

PCT_QUOTES = {
    "0.25": ("CA-1", 1, "twenty-five percent (25%)"),
    "0.15": ("AMD-1", 1, "fifteen percent (15%)"),
}

DEFINITION_QUOTES = {
    "ebitda": ("CA-1", 1, '"Consolidated EBITDA" means Consolidated Net Income plus add-backs'),
    "debt": ("CA-1", 1, '"Consolidated Total Debt" means all Indebtedness of the Borrower for borrowed money.'),
    "net_debt": ("CA-1", 1, '"Consolidated Net Debt" means Consolidated Total Debt minus Unrestricted Cash'),
}


def M(millions: str | int | Decimal) -> Decimal:
    """Millions to whole currency units, exactly."""
    return Decimal(str(millions)) * Decimal(1_000_000)


def make_policy(*, thin: str = "0.10", sample: str = "0", version: int = 1) -> BankPolicy:
    return BankPolicy(
        policy_id="BANK-POLICY",
        version=version,
        thin_headroom_pct=Decimal(thin),
        clean_pass_sample_pct=Decimal(sample),
        required_tie_outs=(
            TieOutRequirement(input_name="total_debt", tie_out_names=("debt_components_sum_to_total",)),
            TieOutRequirement(input_name="cash", tie_out_names=("cash_components_sum_to_total",)),
        ),
    )


class World:
    """A document store, a verifier and the synthetic contract documents, wired together."""

    def __init__(self) -> None:
        self.store = DocumentStore()
        self.store.register("CA-1", CREDIT_AGREEMENT)
        self.store.register("AMD-1", AMENDMENT_1)
        self.verifier = SourceVerifier(self.store, key=b"test-key-not-a-secret")
        self.policy = make_policy()

    def span(self, ref: tuple[str, int, str], clause: str | None = None):
        doc_id, page, quote = ref
        return self.store.locate(doc_id, page, quote, clause_ref=clause)

    def build_spec(
        self,
        *,
        spec_id: str = "SPEC-1",
        version: int = 1,
        supersedes: str | None = None,
        addback_pct: str = "0.25",
        cash_cap: str = "50000000",
        effective_from: date = date(2025, 1, 1),
        executed_on: date | None = None,
        retroactive_source=None,
        thresholds: tuple[tuple[date, date | None, str], ...] = (
            (date(2025, 1, 1), date(2025, 6, 30), "4.50"),
            (date(2025, 7, 1), None, "4.00"),
        ),
        places: int | None = 2,
        rounding: str = "ROUND_HALF_UP",
        comparison: Comparison = Comparison.LE,
        denominator=None,
        approve: bool = True,
    ) -> CovenantSpec:
        e = self.span(DEFINITION_QUOTES["ebitda"], "Section 1.01")
        d = self.span(DEFINITION_QUOTES["debt"], "Section 1.01")
        n = self.span(DEFINITION_QUOTES["net_debt"], "Section 1.01")
        pct_span = self.span(PCT_QUOTES[addback_pct])
        cash_span = self.span(("CA-1", 1, "$50,000,000"))
        th_quote = {"4.50": "4.50 to 1.00", "4.00": "4.00 to 1.00"}
        steps = tuple(
            ThresholdStep(
                effective_from=f,
                effective_to=t,
                level=Sourced(value=Decimal(lv), spans=(self.span(("CA-1", 2, th_quote[lv]), "Section 7.11(a)"),)),
            )
            for f, t, lv in thresholds
        )
        terms = (
            DefinedTerm(
                name="accepted_addbacks",
                formula=MinOf(args=(Input(name="addbacks"), Mul(left=Lit(value=Decimal(addback_pct), source=pct_span), right=Input(name="base_ebitda")))),
                definition_spans=(e,),
            ),
            DefinedTerm(
                name="consolidated_ebitda",
                formula=Sum(args=(Input(name="base_ebitda"), TermRef(name="accepted_addbacks"))),
                definition_spans=(e,),
            ),
            DefinedTerm(
                name="netted_cash",
                formula=MinOf(args=(Input(name="cash"), Lit(value=Decimal(cash_cap), source=cash_span))),
                definition_spans=(n,),
            ),
            DefinedTerm(
                name="net_debt",
                formula=Diff(left=Input(name="total_debt"), right=TermRef(name="netted_cash")),
                definition_spans=(n,),
            ),
        )
        inputs = (
            InputRequirement(name="base_ebitda", description="LTM EBITDA before add-backs", basis="ltm_flow", sign="as_reported", definition_spans=(e,)),
            InputRequirement(name="addbacks", description="LTM add-backs", basis="ltm_flow", definition_spans=(e,)),
            InputRequirement(name="total_debt", description="Consolidated Total Debt", basis="point_in_time", definition_spans=(d,)),
            InputRequirement(name="cash", description="Unrestricted Cash", basis="point_in_time", definition_spans=(n,)),
        )
        draft = CovenantSpec(
            spec_id=spec_id,
            agreement_id="CA-1",
            version=version,
            supersedes=supersedes,
            kind=CovenantKind.MAX_TOTAL_NET_LEVERAGE,
            comparison=comparison,
            numerator=TermRef(name="net_debt"),
            denominator=denominator if denominator is not None else TermRef(name="consolidated_ebitda"),
            terms=terms,
            inputs=inputs,
            thresholds=steps,
            ratio_decimal_places=places,
            rounding=rounding,
            effective_from=effective_from,
            executed_on=executed_on,
            retroactive_source=retroactive_source,
            extractor_run_id="agent-run-1",
        )
        if not approve:
            return draft
        verified = self.verifier.verify_spec(draft)
        return approve_spec(
            verified,
            Approval(approver_id="credit.officer@bank", role="credit_officer", approved_at=NOW),
            self.verifier,
        )

    def package(
        self,
        values: Mapping[str, str | int | Decimal],
        period_end: date = date(2025, 6, 30),
        *,
        omit: tuple[str, ...] = (),
        seal: bool = True,
        tie_out_parts: Mapping[str, tuple[Decimal, Decimal]] | None = None,
        extra_lines: tuple[str, ...] = (),
    ) -> dict[str, FinancialFact]:
        """Register a synthetic borrower statement and return (sealed) facts keyed by input name.

        `values` are in millions. `tie_out_parts` can override the components used for the
        debt/cash tie-outs, to simulate inconsistent extraction."""
        labels = {
            "base_ebitda": "LTM Consolidated EBITDA before add-backs",
            "addbacks": "LTM add-backs",
            "total_debt": "Total debt",
            "cash": "Unrestricted cash",
        }
        amounts = {k: M(v) for k, v in values.items()}
        lines = [f"Borrower financial statements for the period ended {period_end.isoformat()} (SYNTHETIC)"]
        lines += [f"{labels[k]}: {amounts[k]:,}" for k in labels if k in amounts]
        lines += list(extra_lines)
        doc_id = f"FS-{period_end.isoformat()}-{sha256_hex(repr(sorted(amounts.items())) + repr(extra_lines))[:6]}"
        self.store.register(doc_id, ("\n".join(lines),))
        facts: dict[str, FinancialFact] = {}
        for name in labels:
            if name not in amounts or name in omit:
                continue
            quote = f"{labels[name]}: {amounts[name]:,}"
            span = self.store.locate(doc_id, 1, quote)
            tie_outs: tuple[TieOutCheck, ...] = ()
            if name in ("total_debt", "cash"):
                parts = (tie_out_parts or {}).get(name) or (amounts[name] * Decimal("0.6"), amounts[name] * Decimal("0.4"))
                tie_outs = (
                    TieOutCheck(
                        name="debt_components_sum_to_total" if name == "total_debt" else "cash_components_sum_to_total",
                        expected=sum(parts, Decimal(0)),
                        actual=amounts[name],
                    ),
                )
            is_flow = name in ("base_ebitda", "addbacks")
            fact = FinancialFact(
                fact_id=f"F-{period_end.isoformat()}-{name}",
                input_name=name,
                value=amounts[name],
                currency="USD",
                reported_scale=1,
                period_start=(period_end.replace(year=period_end.year - 1) + timedelta(days=1)) if is_flow else None,
                period_end=period_end,
                origin="unaudited_fs",
                source=span,
                extractor_run_id="agent-run-2",
                tie_outs=tie_outs,
            )
            facts[name] = self.verifier.verify_fact(fact, self.policy) if seal else fact
        return facts


SAMPLE_VALUES = {"base_ebitda": "100", "addbacks": "40", "total_debt": "540", "cash": "40"}
