"""Download the two public SEC exhibits, canonicalise them, and write data/sources/.

    SEC_USER_AGENT="Your Name you@example.com" python3 tools/build_sources.py [--check]

--check re-downloads and verifies that the result matches the committed manifests (the EDGAR HTML hash and
the canonical text hash), without rewriting anything. Both documents are public SEC filings."""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from covenant_audit.ingest import (  # noqa: E402
    KnownAmendment,
    SourceManifest,
    build_manifest,
    fetch_url,
    load_pages,
    save_pages,
)

COMPANY = "Viad Corp (now Pursuit Attractions and Hospitality, Inc.)"
AMENDMENT = KnownAmendment(
    doc_id="VIAD-FA1-2022",
    description="First Amendment dated as of March 23, 2022, amending and restating Sections 7.10(a), (b) and (c)",
    dated=date(2022, 3, 23),
)
SOURCES = [
    dict(
        name="viad_credit_agreement_2021", doc_id="VIAD-CA-2021", company=COMPANY, cik="0000884219", form="8-K",
        exhibit="EX-10.1", accession_number="0001564590-21-039835", filed=date(2021, 8, 2),
        filing_url="https://www.sec.gov/Archives/edgar/data/884219/000156459021039835/vvi-ex101_28.htm",
        description="Credit Agreement dated as of July 30, 2021 among Viad Corp, the lenders party thereto and Bank of America, N.A., as Administrative Agent",
        known_amendments=(AMENDMENT,),
    ),
    dict(
        name="viad_first_amendment_2022", doc_id="VIAD-FA1-2022", company=COMPANY, cik="0000884219", form="8-K",
        exhibit="EX-10.1", accession_number="0000950170-22-004478", filed=date(2022, 3, 24),
        filing_url="https://www.sec.gov/Archives/edgar/data/884219/000095017022004478/vvi-ex10_1.htm",
        description="First Amendment dated as of March 23, 2022 to the Credit Agreement dated as of July 30, 2021",
        known_amendments=(),
    ),
]


def main() -> int:
    check = "--check" in sys.argv
    agent = os.environ.get("SEC_USER_AGENT", "")
    out = ROOT / "data" / "sources"
    failures = 0
    for spec in SOURCES:
        meta = {k: v for k, v in spec.items() if k != "name"}
        raw = fetch_url(meta["filing_url"], agent)
        manifest, pages = build_manifest(raw=raw, **meta)
        path = out / f"{spec['name']}.manifest.json"
        if check:
            committed = SourceManifest.model_validate_json(path.read_text())
            same = (committed.raw_sha256, committed.canonical_sha256, committed.page_count) == (
                manifest.raw_sha256, manifest.canonical_sha256, manifest.page_count)
            same_pages = load_pages(out / f"{spec['name']}.pages.json.gz") == pages
            print(f"{spec['doc_id']}: raw/canonical hash match = {same}; committed pages identical = {same_pages}")
            failures += not (same and same_pages)
        else:
            save_pages(out / f"{spec['name']}.pages.json.gz", pages)
            path.write_text(manifest.model_dump_json(indent=2) + "\n")
            print(f"{spec['doc_id']}: {manifest.page_count} pages, canonical sha256 {manifest.canonical_sha256}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
