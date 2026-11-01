"""Author the REFERENCE extraction used when no LLM API key is available.

IMPORTANT - what this file is: the response below was written by hand (by the coding agent that built this
repository) after reading the public Viad credit agreement text. It is a stand-in for what an extraction model
should produce so the rest of the pipeline can be demonstrated offline. It is NOT the output of an OpenRouter or
any other model call, and the demo labels it as such ("NOT A LIVE MODEL CALL"). Run `extract --provider openrouter`
with OPENROUTER_API_KEY set to produce a genuine model extraction and compare it with this reference.

Every quotation is looked up in the stored canonical page text and asserted to be verbatim and unique on its
page, so the reference cannot contain a mistyped or invented citation.

    python3 tools/author_reference_extraction.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from covenant_audit.ingest import load_pages  # noqa: E402

CA, FA = "VIAD-CA-2021", "VIAD-FA1-2022"
PAGES = {
    CA: load_pages(ROOT / "data/sources/viad_credit_agreement_2021.pages.json.gz"),
    FA: load_pages(ROOT / "data/sources/viad_first_amendment_2022.pages.json.gz"),
}


def Q(doc: str, page: int, start: str, end: str | None = None, clause: str | None = None) -> dict:
    """A citation whose quote is the verbatim text from `start` through `end` (inclusive) on that page."""
    text = PAGES[doc][page - 1]
    i = text.find(start)
    assert i >= 0, f"start marker not on {doc} p.{page}: {start!r}"
    j = i + len(start)
    if end:
        k = text.find(end, i)
        assert k >= 0, f"end marker not on {doc} p.{page}: {end!r}"
        j = k + len(end)
    quote = text[i:j]
    assert text.count(quote) == 1, f"quote not unique on {doc} p.{page}: {quote[:80]!r}"
    out = {"doc_id": doc, "page": page, "quote": quote}
    if clause:
        out["clause_ref"] = clause
    return out


def lit(value: str, source: dict) -> dict:
    return {"op": "lit", "value": value, "source": source}


def inp(name: str) -> dict:
    return {"op": "input", "name": name}


def term(name: str) -> dict:
    return {"op": "term", "name": name}


# ---- citations -------------------------------------------------------------------------------------------
EBITDA_HEAD = Q(CA, 17, '"Consolidated EBITDA" means, for any period, the Borrower and its Restricted Subsidiaries\' consolidated net income', clause="Section 1.01")
NI = Q(CA, 17, "consolidated net income (including net income attributable to noncontrolling interests held by third parties in Restricted Subsidiaries)", clause="Section 1.01")
INT_TAX_DA = Q(CA, 17, "before interest expense, taxes, depreciation, amortization", clause="Section 1.01")
OTHER_ADDBACKS = Q(CA, 17, "non-cash rent expense, preopening and start-up expenses", "(whether or not such acquisitions and mergers were completed)", clause="Section 1.01")
DIV_A = Q(CA, 17, "(a) cash dividends and distributions paid to the Borrower and its Restricted Subsidiaries from any Person that is not a Restricted Subsidiary", clause="Section 1.01")
DIV_CAP = Q(CA, 17, "shall not exceed the cumulative amount of the Borrower's and its Restricted Subsidiaries' share of the Consolidated EBITDA of such Person", clause="Section 1.01")
GAAP_B = Q(CA, 17, "(b) any loss (or gain) arising from a change in GAAP", clause="Section 1.01")
NONCASH_C = Q(CA, 17, "(c) any non-cash losses, costs or expenses (or non-cash gains or income) and any loss or income resulting from an earn out obligation or other contingent consideration being paid or no longer being contingent", clause="Section 1.01")
SAVINGS_D = Q(CA, 17, '(d) at the election of the Borrower, the amount of "run rate" cost savings, operating expense reductions, restructuring or implementation improvements, other operating improvements and synergies projected by the Borrower in good faith to be realized', clause="Section 1.01")
CAP_20 = Q(CA, 18, "shall not (i) exceed 20.0% of Consolidated EBITDA for such Test Period (calculated prior to giving effect to this clause (d))", clause="Section 1.01")
CHARGES_E = Q(CA, 18, "(e) charges and expenses relating to any Specified Transaction, restructuring or implementation of an initiative", clause="Section 1.01")
BI_F = Q(CA, 18, "(f) proceeds received from business interruption insurance (to the extent not reflected as revenue or earnings)", clause="Section 1.01")
NCI_G = Q(CA, 18, "(g) cash dividends and distributions paid to holders of noncontrolling interests held by third parties in Restricted Subsidiaries", clause="Section 1.01")
NFD_HEAD = Q(CA, 18, '"Consolidated Net Funded Indebtedness" means, as of any date of determination', clause="Section 1.01")
NFD_A = Q(CA, 18, "(a) the outstanding principal amount of all Indebtedness for borrowed money (including reimbursement obligations with respect to any drawn letters of credit)", clause="Section 1.01")
NFD_B = Q(CA, 18, "(b) the aggregate amount of all Finance Lease Obligations and Construction Indebtedness", clause="Section 1.01")
NMP_EXCL = Q(CA, 18, "such Finance Lease Obligations and Construction Indebtedness shall be excluded for a period beginning on the first day of the fiscal quarter when construction of such New Material Project has commenced", clause="Section 1.01")
NMP_CAP = Q(CA, 19, "does not exceed $50,000,000 in the aggregate", clause="Section 1.01")
NFD_C = Q(CA, 19, "(c) to the extent not duplicative with the Indebtedness and obligations specified in subsections (a) and (b) above, all Guarantees with respect to outstanding Indebtedness", clause="Section 1.01")
NFD_MINUS = Q(CA, 19, "minus (II) the amount of Unrestricted Cash of the Borrower and its Restricted Subsidiaries", clause="Section 1.01")
DEFEASED = Q(CA, 19, "Consolidated Net Funded Indebtedness shall not include any Defeased Indebtedness or Escrowed Indebtedness", clause="Section 1.01")
UC_DEF = Q(CA, 62, '"Unrestricted Cash" means, as of any date of determination, an amount equal to the unrestricted cash and Cash Equivalents', clause="Section 1.01")
UC_COND = Q(CA, 62, "the aggregate Outstanding Amount of all Revolving Loans and Swing Line Loans as of such date exceeds $10,000,000", clause="Section 1.01")
UC_TEN = Q(CA, 62, "exceeds $10,000,000", clause="Section 1.01")
UC_FIFTY = Q(CA, 62, "the amount of the Unrestricted Cash shall not exceed $50,000,000", clause="Section 1.01")
TNLR_DEF = Q(CA, 61, '"Total Net Leverage Ratio" means, as of any date of determination, the ratio of (a) Consolidated Net Funded Indebtedness as of such date to (b) Consolidated EBITDA for the Test Period then most recently ended', clause="Section 1.01")
ROUNDING = Q(CA, 64, "Any financial ratios required to be maintained by the Borrower pursuant to this Agreement shall be calculated by dividing the appropriate component by the other component, carrying the result to one place more than the number of places by which such ratio is expressed herein and rounding the result up or down to the nearest number (with a rounding-up if there is no nearest number)", clause="Section 1.04")
SCHED1 = Q(FA, 3, "Test Period Ending Maximum Total Net Leverage Ratio September 30, 2022 5.25 to 1.00", clause="Section 7.10(c), as restated by the First Amendment")
SCHED2 = Q(FA, 3, "December 31, 2022 4.75 to 1.00 March 31, 2023 4.50 to 1.00", clause="Section 7.10(c), as restated by the First Amendment")
SCHED3 = Q(FA, 3, "March 31, 2023 4.50 to 1.00 June 30, 2023 and thereafter 4.00 to 1.00", clause="Section 7.10(c), as restated by the First Amendment")
SCHED4 = Q(FA, 3, "June 30, 2023 and thereafter 4.00 to 1.00", clause="Section 7.10(c), as restated by the First Amendment")


def req(name: str, description: str, basis: str, sign: str, *spans: dict) -> dict:
    return {"name": name, "description": description, "basis": basis, "sign": sign, "definition_spans": list(spans)}


inputs = [
    req("consolidated_net_income", "consolidated net income (incl. noncontrolling interests), last twelve months", "ltm_flow", "as_reported", NI),
    req("interest_expense", "interest expense, last twelve months", "ltm_flow", "positive", INT_TAX_DA),
    req("income_taxes", "income taxes, last twelve months", "ltm_flow", "positive", INT_TAX_DA),
    req("depreciation_and_amortization", "depreciation and amortization, last twelve months", "ltm_flow", "positive", INT_TAX_DA),
    req("other_enumerated_addbacks", "net total of the other add-backs listed in the first sentence of the EBITDA definition, last twelve months", "ltm_flow", "as_reported", OTHER_ADDBACKS),
    req("dividends_from_non_restricted_persons", "cash dividends and distributions received from Persons that are not Restricted Subsidiaries", "ltm_flow", "positive", DIV_A),
    req("share_of_ebitda_of_those_persons", "cumulative share of Consolidated EBITDA of the Persons paying those dividends", "ltm_flow", "positive", DIV_CAP),
    req("gaap_change_loss_gain", "loss (gain) arising from a change in GAAP", "ltm_flow", "as_reported", GAAP_B),
    req("other_noncash_items_and_earnout", "other non-cash losses (gains) and earn-out effects", "ltm_flow", "as_reported", NONCASH_C),
    req("run_rate_cost_savings", "projected run-rate cost savings and synergies elected as an add-back", "ltm_flow", "positive", SAVINGS_D),
    req("transaction_and_restructuring_charges", "charges relating to Specified Transactions and restructuring initiatives", "ltm_flow", "positive", CHARGES_E),
    req("business_interruption_proceeds", "business interruption insurance proceeds not reflected in earnings", "ltm_flow", "positive", BI_F),
    req("distributions_to_noncontrolling_holders", "cash distributions to holders of noncontrolling interests in Restricted Subsidiaries", "ltm_flow", "positive", NCI_G),
    req("borrowed_money_debt", "outstanding principal of Indebtedness for borrowed money incl. drawn letters of credit", "point_in_time", "positive", NFD_A),
    req("finance_lease_and_construction_debt", "Finance Lease Obligations and Construction Indebtedness", "point_in_time", "positive", NFD_B),
    req("new_material_project_excluded_obligations", "Finance Lease Obligations and Construction Indebtedness of New Material Projects within their exclusion period", "point_in_time", "positive", NMP_EXCL),
    req("guarantees_of_third_party_debt", "Guarantees of third-party Indebtedness not duplicative of the above (drawn or demanded only)", "point_in_time", "positive", NFD_C),
    req("defeased_or_escrowed_indebtedness", "Defeased Indebtedness and Escrowed Indebtedness", "point_in_time", "positive", DEFEASED),
    req("unrestricted_cash", "unrestricted cash and Cash Equivalents of the Borrower and Restricted Subsidiaries", "point_in_time", "positive", UC_DEF),
    req("revolving_and_swing_line_outstanding", "aggregate Outstanding Amount of Revolving Loans and Swing Line Loans", "point_in_time", "positive", UC_COND),
]

terms = [
    {  # clause (a) dividends, capped at the cumulative share of the payer's EBITDA
        "name": "accepted_dividends",
        "formula": {"op": "min", "args": [inp("dividends_from_non_restricted_persons"), inp("share_of_ebitda_of_those_persons")]},
        "definition_spans": [DIV_A, DIV_CAP],
    },
    {  # Consolidated EBITDA calculated prior to giving effect to clause (d)
        "name": "ebitda_before_clause_d",
        "formula": {"op": "diff", "left": {"op": "sum", "args": [
            inp("consolidated_net_income"), inp("interest_expense"), inp("income_taxes"), inp("depreciation_and_amortization"),
            inp("other_enumerated_addbacks"), term("accepted_dividends"), inp("gaap_change_loss_gain"),
            inp("other_noncash_items_and_earnout"), inp("transaction_and_restructuring_charges"), inp("business_interruption_proceeds")]},
            "right": inp("distributions_to_noncontrolling_holders")},
        "definition_spans": [EBITDA_HEAD, CAP_20],
    },
    {  # clause (d) add-back, capped at 20.0% of the EBITDA before clause (d)
        "name": "accepted_cost_savings",
        "formula": {"op": "min", "args": [inp("run_rate_cost_savings"), {"op": "mul", "left": lit("0.20", CAP_20), "right": term("ebitda_before_clause_d")}]},
        "definition_spans": [SAVINGS_D, CAP_20],
    },
    {
        "name": "consolidated_ebitda",
        "formula": {"op": "sum", "args": [term("ebitda_before_clause_d"), term("accepted_cost_savings")]},
        "definition_spans": [EBITDA_HEAD, TNLR_DEF],
    },
    {  # netted cash: capped at 50,000,000 only when revolver and swing line borrowings exceed 10,000,000
        "name": "netted_unrestricted_cash",
        "formula": {"op": "ifgt", "left": inp("revolving_and_swing_line_outstanding"), "right": lit("10000000", UC_TEN),
                    "then": {"op": "min", "args": [inp("unrestricted_cash"), lit("50000000", UC_FIFTY)]}, "otherwise": inp("unrestricted_cash")},
        "definition_spans": [UC_DEF, UC_COND],
    },
    {
        "name": "excluded_new_material_project_obligations",
        "formula": {"op": "min", "args": [inp("new_material_project_excluded_obligations"), lit("50000000", NMP_CAP)]},
        "definition_spans": [NMP_EXCL, NMP_CAP],
    },
    {
        "name": "consolidated_net_funded_indebtedness",
        "formula": {"op": "diff", "left": {"op": "diff", "left": {"op": "diff",
            "left": {"op": "sum", "args": [inp("borrowed_money_debt"), inp("finance_lease_and_construction_debt"), inp("guarantees_of_third_party_debt")]},
            "right": term("excluded_new_material_project_obligations")}, "right": inp("defeased_or_escrowed_indebtedness")},
            "right": term("netted_unrestricted_cash")},
        "definition_spans": [NFD_HEAD, NFD_MINUS, TNLR_DEF],
    },
]

thresholds = [
    {"effective_from": "2022-09-30", "effective_to": "2022-12-30", "level": {"value": "5.25", "spans": [SCHED1]}},
    {"effective_from": "2022-12-31", "effective_to": "2023-03-30", "level": {"value": "4.75", "spans": [SCHED2]}},
    {"effective_from": "2023-03-31", "effective_to": "2023-06-29", "level": {"value": "4.50", "spans": [SCHED3]}},
    {"effective_from": "2023-06-30", "effective_to": None, "level": {"value": "4.00", "spans": [SCHED4]}},
]

covenant = {
    "kind": "max_total_net_leverage",
    "comparison": "le",
    "numerator": term("consolidated_net_funded_indebtedness"),
    "denominator": term("consolidated_ebitda"),
    "terms": terms,
    "inputs": inputs,
    "thresholds": thresholds,
    "test_period": "ltm_quarterly",
    "ratio_decimal_places": 3,
    "rounding": "ROUND_HALF_UP",
    "rounding_source": ROUNDING,
    "effective_from": "2022-09-30",
    "scope_notes": [
        "Applies Section 7.10(c) as amended and restated by the First Amendment (dated as of March 23, 2022) to the Total Net Leverage Ratio defined in the Credit Agreement dated as of July 30, 2021. The Amendment's Effective Date is defined by conditions precedent and is not a calendar date in the text; the first test date under the amended schedule is September 30, 2022, so the spec governs test dates from then.",
        "NOT MODELLED: the Acquisition Period proviso of Section 7.10(c), which sets higher maximum levels while an Acquisition Period is continuing. Test dates during an Acquisition Period must be assessed outside this spec.",
        "NOT MODELLED: the Equity Cure Right in Section 7.10(d) (Specified Equity Contributions), and the separate Minimum Liquidity, Minimum Consolidated EBITDA and Interest Coverage Ratio covenants.",
        "NOT MODELLED: Pro Forma Basis adjustments, Unrestricted Subsidiaries (inputs are assumed supplied for the Borrower and its Restricted Subsidiaries on the contractual basis), and the conditions attached to the run-rate savings add-back in clause (d)(i)-(iii); these are conditions on the borrower's inputs, not tested by the engine.",
        "INTERPRETATION: the 20.0% cap on clause (d) is applied to Consolidated EBITDA calculated with every other clause of the definition, because the text says 'calculated prior to giving effect to this clause (d)'.",
        "INTERPRETATION: the add-backs listed in the first sentence of the definition (non-cash rent expense through acquisition and merger related charges) are grouped into one input, other_enumerated_addbacks, net of gains; interest, taxes and depreciation and amortization are separate inputs.",
        "INTERPRETATION: the 'cumulative amount' limit on clause (a) dividends is applied to cumulative figures supplied by the borrower; the engine does not accumulate across periods. Letters of credit and Guarantees count as zero until drawn or demanded, which the borrower's inputs must reflect.",
        "INTERPRETATION: Section 1.04 (rounding) is read as: divide, carry the result to one place more than the ratio is expressed (the schedule uses two places, so three), round to the nearest number with ties rounded up, then compare to the maximum level.",
    ],
}

response = {"covenant": covenant, "unresolved": []}

record = {
    "authored_by": "claude-coding-agent-reference",
    "provenance": (
        "Reference extraction written by hand by the coding agent after reading the public SEC text. It is a stand-in so the "
        "pipeline can be demonstrated without an API key. It is NOT the output of OpenRouter or any LLM call."
    ),
    "response_text": json.dumps(response, indent=2),
}

if __name__ == "__main__":
    out = ROOT / "data" / "extraction" / "viad_reference_response.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n")
    print(f"wrote {out} ({len(record['response_text'])} chars of response, {sum(len(v) for v in [inputs, terms])} inputs+terms)")
