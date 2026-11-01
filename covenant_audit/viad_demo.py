"""Configuration for the end-to-end demonstration on a real public agreement.

Source: Viad Corp (now Pursuit Attractions and Hospitality, Inc.), CIK 0000884219, Credit Agreement dated as of
July 30, 2021 (SEC accession 0001564590-21-039835, Exhibit 10.1 to Form 8-K filed 2021-08-02) and its First
Amendment dated as of March 23, 2022 (accession 0000950170-22-004478, Exhibit 10.1 to Form 8-K filed 2022-03-24).
Covenant: Section 7.10(c) Total Net Leverage Ratio as restated by the First Amendment; definitions in Section 1.01;
rounding in Section 1.04. Both documents are public SEC filings. The borrower data used for the calculation is synthetic.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .clauses import ExcerptSet, select_excerpts
from .extraction import ExtractionTask
from .ingest import SourceManifest, load_pages, register_source
from .models import CovenantKind
from .verification import DocumentStore

ROOT = Path(__file__).resolve().parent.parent
SOURCES_DIR = ROOT / "data" / "sources"
SOURCE_FILES = ("viad_credit_agreement_2021", "viad_first_amendment_2022")
REFERENCE_RESPONSE = ROOT / "data" / "extraction" / "viad_reference_response.json"
SYNTHETIC_FINANCIALS = ROOT / "data" / "demo" / "viad_synthetic_financials.json"

DEFAULT_TEST_DATE = date(2022, 12, 31)

EXTRA_PASSAGES = (
    (r"Any financial ratios required to be maintained", "contractual rounding (Section 1.04)"),
    (r"FIRST AMENDMENT, dated as of", "amendment date and parties"),
    (r"the date upon which this Amendment becomes effective", "amendment effectiveness (Section 2)"),
)

TASK = ExtractionTask(
    covenant_name="Total Net Leverage Ratio",
    kind=CovenantKind.MAX_TOTAL_NET_LEVERAGE,
    agreement_id="VIAD-CA-2021-AMD1",
    doc_ids=("VIAD-CA-2021", "VIAD-FA1-2022"),
    scope_instructions=(
        "Extract only the maximum Total Net Leverage Ratio of Section 7.10(c) as amended and restated by the First Amendment (VIAD-FA1-2022).",
        "Definitions come from the Credit Agreement (VIAD-CA-2021). The First Amendment restates Section 7.10(a)-(c) and takes precedence for the covenant levels.",
        "Do NOT model the Acquisition Period proviso, the equity cure right, or the Liquidity and Interest Coverage covenants; record each exclusion in scope_notes.",
        "Model the 20.0% limit on clause (d) of Consolidated EBITDA and the Unrestricted Cash cap in Consolidated Net Funded Indebtedness as formulas, with cited constants.",
        "Section 1.04 states a rounding rule for financial ratios; capture it (ratio_decimal_places, rounding, rounding_source).",
    ),
)


def load_sources() -> tuple[DocumentStore, dict[str, list[str]], list[SourceManifest]]:
    """Register the committed canonical pages, refusing any copy whose hash differs from its manifest."""
    store = DocumentStore()
    docs: dict[str, list[str]] = {}
    manifests: list[SourceManifest] = []
    for name in SOURCE_FILES:
        manifest = SourceManifest.model_validate_json((SOURCES_DIR / f"{name}.manifest.json").read_text())
        pages = load_pages(SOURCES_DIR / f"{name}.pages.json.gz")
        register_source(store, manifest, pages)
        docs[manifest.doc_id] = pages
        manifests.append(manifest)
    return store, docs, manifests


def excerpts(docs: dict[str, list[str]]) -> ExcerptSet:
    return select_excerpts(docs, TASK.covenant_name, extra_passages=EXTRA_PASSAGES)
