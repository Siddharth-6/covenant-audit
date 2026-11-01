"""Document ingestion: SEC EDGAR HTML -> canonical page text -> hashed `SourceDocument`.

Every quotation in a spec points into this canonical text, so the canonicalisation is part of the audit
trail and is deliberately small and documented:

1. Page = one rendering page of the filing (EDGAR HTML marks page breaks with `<hr>`). Page numbers are
   1-based indexes of those breaks, NOT the printed page numbers (which differ by the front matter).
2. Tags are removed; HTML entities decoded; all whitespace runs become one space.
3. Spaces just inside typographic quotes (`“ Term ”`) are removed, then typographic quotes/dashes become
   ASCII (`“ ”` -> `"`, `’` -> `'`, en/em dash -> `-`) so a model copying text does not fail on look-alike
   characters. Spaces before `, . ; : ) %` and after `(` are removed too, to undo EDGAR's rendering
   artefacts ("20 .0%" -> "20.0%").
Nothing else is altered: no words are added, removed or reordered.

Network access is limited to `fetch_url`, which needs a contact-bearing User-Agent (SEC policy).
"""

from __future__ import annotations

import gzip
import hashlib
import html as html_lib
import json
import re
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Sequence

from .base import FrozenModel, Sha256Hex
from .verification import DocumentStore, SourceDocument, _document_hash

PAGE_BREAK = re.compile(r"<hr[^>]*>", re.IGNORECASE)
_TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)


class IngestError(Exception):
    """A document could not be fetched, parsed or matched to its manifest."""


class KnownAmendment(FrozenModel):
    """An amendment that exists in the public record. If its document was not among those the extractor
    read, approval is blocked: terms may have changed after the base agreement."""

    doc_id: str
    description: str
    dated: date


class SourceManifest(FrozenModel):
    doc_id: str
    company: str
    cik: str
    form: str  # the SEC form the exhibit was filed with, e.g. "8-K"
    exhibit: str
    accession_number: str
    filed: date
    filing_url: str
    description: str
    raw_sha256: Sha256Hex  # of the HTML bytes as served by EDGAR
    canonical_sha256: Sha256Hex  # of the canonical page text (same hash DocumentStore uses)
    page_count: int
    known_amendments: tuple[KnownAmendment, ...] = ()


def canonicalize_text(raw: str) -> str:
    """Normalise one page of extracted text (see module docstring for the exact rules)."""
    text = html_lib.unescape(raw).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    # Fix spacing while open/close quotes are still distinguishable (nested quoted paragraphs make
    # pairing straight quotes unreliable), then flatten typography to ASCII.
    text = re.sub("\u201c\\s+", "\u201c", text)
    text = re.sub("\\s+\u201d", "\u201d", text)
    text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"\s+([,.;:)%])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    return text


def html_to_pages(html: str) -> list[str]:
    """Split filing HTML at its page breaks and canonicalise each page. Trailing empty pages are dropped;
    interior empty pages are kept so that page numbers stay aligned with the rendering."""
    pages = [canonicalize_text(_TAGS.sub(" ", chunk)) for chunk in PAGE_BREAK.split(html)]
    while pages and not pages[-1]:
        pages.pop()
    return pages


def fetch_url(url: str, user_agent: str, *, timeout: float = 60.0, retries: int = 3) -> bytes:
    """Download with retries and backoff. SEC requires a User-Agent that identifies the requester."""
    if "@" not in user_agent:
        raise IngestError("SEC requires a User-Agent with a contact address, e.g. 'Name name@example.com' (set SEC_USER_AGENT)")
    request = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept-Encoding": "identity"})
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URLs from a manifest
                return response.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            time.sleep(2**attempt)
    raise IngestError(f"could not fetch {url}: {last}")


def build_manifest(
    *, doc_id: str, company: str, cik: str, form: str, exhibit: str, accession_number: str, filed: date,
    filing_url: str, description: str, raw: bytes, known_amendments: Sequence[KnownAmendment] = ()
) -> tuple[SourceManifest, list[str]]:
    pages = html_to_pages(raw.decode("utf-8", errors="replace"))
    if not pages:
        raise IngestError(f"{doc_id}: no text found")
    manifest = SourceManifest(
        doc_id=doc_id, company=company, cik=cik, form=form, exhibit=exhibit, accession_number=accession_number,
        filed=filed, filing_url=filing_url, description=description, raw_sha256=hashlib.sha256(raw).hexdigest(),
        canonical_sha256=_document_hash(pages), page_count=len(pages), known_amendments=tuple(known_amendments),
    )
    return manifest, pages


def save_pages(path: Path, pages: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(list(pages), fh, ensure_ascii=False)


def load_pages(path: Path) -> list[str]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return list(json.load(fh))


def register_source(store: DocumentStore, manifest: SourceManifest, pages: Sequence[str]) -> SourceDocument:
    """Register pages only if they hash to the manifest's canonical hash: a corrupted or edited copy is refused."""
    if len(pages) != manifest.page_count or _document_hash(pages) != manifest.canonical_sha256:
        raise IngestError(f"{manifest.doc_id}: pages do not match the manifest hash {manifest.canonical_sha256[:12]}...")
    return store.register(manifest.doc_id, pages)
