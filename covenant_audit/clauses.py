"""Deterministic clause identification: which passages of the agreement(s) does an extractor need to read?

No model is involved. We (1) find the pages that state the covenant, (2) index every defined term in each
document, and (3) follow the defined terms the covenant uses, to a bounded depth, collecting their
definitions. Terms we refer to but cannot include (stop-list, depth or size budget) are reported so the
extractor and the human reviewer can see exactly what was *not* read.

Excerpts are verbatim substrings of canonical page text, so a quotation copied from an excerpt can be
located and verified in the stored document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping, Sequence

# `"Term" means ...` / `"Term" has the meaning ...` / `"Term" shall mean ...`
_DEF_START = re.compile(r'"([A-Z][A-Za-z0-9&\-\.,\' /]{2,80}?)" (?:means|has the meaning|shall mean|shall have the meaning)')

# Terms that are everywhere and defined at length; following them would swamp the excerpt budget without
# changing a covenant calculation. They are still listed as "referenced but not provided".
GENERIC_TERMS = frozenset(
    {
        "Agreement", "Borrower", "Restricted Subsidiary", "Restricted Subsidiaries", "Subsidiary", "Subsidiaries", "Person",
        "GAAP", "Loan Documents", "Loan Document", "Administrative Agent", "Lender", "Lenders", "Closing Date", "Law",
        "Laws", "Governmental Authority", "Obligations", "Business Day", "Code", "Loan Party", "Loan Parties", "Guarantor",
        "Guarantors", "Material Adverse Effect", "Required Lenders", "Facility", "Responsible Officer",
    }
)


@dataclass(frozen=True)
class Excerpt:
    doc_id: str
    page: int
    text: str  # verbatim substring of the canonical page text
    reason: str  # why it was selected, e.g. 'covenant text' or 'definition of "Consolidated EBITDA"'


@dataclass
class ExcerptSet:
    excerpts: list[Excerpt] = field(default_factory=list)
    included_terms: list[str] = field(default_factory=list)
    not_provided_terms: list[str] = field(default_factory=list)  # referenced by what we included, but not read
    covenant_locations: list[tuple[str, int]] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(e.text) for e in self.excerpts)

    def render(self) -> str:
        """The text block given to the extractor: each excerpt labelled with document and page."""
        parts = []
        for e in sorted(self.excerpts, key=lambda x: (x.doc_id, x.page)):
            parts.append(f"[DOCUMENT {e.doc_id} | PAGE {e.page} | {e.reason}]\n{e.text}")
        return "\n\n".join(parts)


@dataclass(frozen=True)
class DefinitionSite:
    doc_id: str
    page: int
    start: int
    end: int  # end of this definition on this page
    spills_to_next_page: bool


def index_definitions(docs: Mapping[str, Sequence[str]]) -> dict[str, list[DefinitionSite]]:
    """Every defined term -> where its definition text sits. A definition ends at the next definition on the
    same page, or at the end of the page (and then may continue on the next page)."""
    index: dict[str, list[DefinitionSite]] = {}
    for doc_id, pages in docs.items():
        for page_no, text in enumerate(pages, 1):
            starts = [(m.start(), m.group(1).strip()) for m in _DEF_START.finditer(text)]
            for i, (start, term) in enumerate(starts):
                end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
                index.setdefault(term, []).append(
                    DefinitionSite(doc_id, page_no, start, end, spills_to_next_page=i + 1 == len(starts))
                )
    return index


def find_covenant_pages(pages: Sequence[str], covenant_term: str) -> list[int]:
    """Pages that state a limit on `covenant_term`: it is the subject of 'shall not permit', or it sits in a
    threshold table (the page right after such a statement that opens with 'Test Period Ending ... to 1.00')."""
    found: list[int] = []
    pattern = re.compile(rf"shall not permit the {re.escape(covenant_term)}", re.IGNORECASE)
    for n, text in enumerate(pages, 1):
        if pattern.search(text):
            found.append(n)
            if n < len(pages) and re.search(r"to 1\.00", pages[n][:600]):
                found.append(n + 1)
    return sorted(set(found))


def _mentions(text: str, term: str) -> bool:
    return re.search(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", text) is not None


def select_excerpts(
    docs: Mapping[str, Sequence[str]],
    covenant_term: str,
    *,
    max_depth: int = 2,
    max_chars: int = 60_000,
    stop_terms: frozenset[str] = GENERIC_TERMS,
    extra_passages: Sequence[tuple[str, str]] = (),
) -> ExcerptSet:
    """`extra_passages` are (regex, reason) pairs for clauses that are not definitions, such as a rounding
    section or an amendment's date and effectiveness language. Each match contributes a short verbatim window."""
    result = ExcerptSet()
    index = index_definitions(docs)
    for pattern, reason in extra_passages:
        rx = re.compile(pattern)
        for doc_id, pages in docs.items():
            for n, text in enumerate(pages, 1):
                m = rx.search(text)
                if m:
                    lo = max(0, m.start() - 20)
                    result.excerpts.append(Excerpt(doc_id, n, text[lo : lo + 700], reason))

    for doc_id, pages in docs.items():
        for n in find_covenant_pages(pages, covenant_term):
            result.covenant_locations.append((doc_id, n))
            result.excerpts.append(Excerpt(doc_id, n, pages[n - 1], f"covenant text: {covenant_term}"))

    # Only definitions are scanned for further terms (not the surrounding covenant text, which mentions
    # equity-cure and default machinery that is irrelevant to the ratio). Depth 0 = the covenant term itself.
    seen_text: list[str] = []
    frontier = {covenant_term}
    included: list[str] = []
    skipped: set[str] = set()
    for depth in range(max_depth + 1):
        next_frontier: set[str] = set()
        for term in sorted(frontier):
            if term in included:
                continue
            sites = index.get(term, [])
            if not sites:
                continue
            included.append(term)
            for site in sites:
                pages = docs[site.doc_id]
                chunk = pages[site.page - 1][site.start : site.end]
                if result.chars + len(chunk) > max_chars:
                    skipped.add(term)
                    continue
                result.excerpts.append(Excerpt(site.doc_id, site.page, chunk, f'definition of "{term}"'))
                seen_text.append(chunk)
                if site.spills_to_next_page and site.page < len(pages):
                    nxt = pages[site.page]
                    cut = _DEF_START.search(nxt)
                    tail = nxt[: cut.start()] if cut else nxt
                    if tail.strip() and result.chars + len(tail) <= max_chars:
                        result.excerpts.append(Excerpt(site.doc_id, site.page + 1, tail, f'definition of "{term}" (continued)'))
                        seen_text.append(tail)
        if depth == max_depth:
            break
        corpus = "\n".join(seen_text)
        for term in index:
            if term in included or term in stop_terms:
                continue
            if _mentions(corpus, term):
                next_frontier.add(term)
        frontier = next_frontier

    corpus = "\n".join(seen_text)
    mentioned = {t for t in index if t not in included and _mentions(corpus, t)}
    result.included_terms = included
    result.not_provided_terms = sorted(mentioned | skipped)
    return result
