"""Shared fixtures for the extraction tests. They run fully offline on the committed SEC pages and the
committed reference response; no API key and no network are needed or used."""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path

from covenant_audit.clauses import ExcerptSet
from covenant_audit.extraction import ExtractionOutcome, parse_model_response
from covenant_audit.ingest import SourceManifest
from covenant_audit.review import ReviewPackage, prepare_review
from covenant_audit.verification import DocumentStore, SourceVerifier
from covenant_audit.viad_demo import REFERENCE_RESPONSE, TASK, excerpts, load_sources

RUN_ID = "llm:test/mock-model:abc123"


@lru_cache(maxsize=1)
def sources() -> tuple[DocumentStore, dict[str, list[str]], list[SourceManifest], ExcerptSet]:
    store, docs, manifests = load_sources()
    return store, docs, manifests, excerpts(docs)


def reference() -> dict:
    """A deep copy of the reference response, as a dict the test can corrupt."""
    record = json.loads(Path(REFERENCE_RESPONSE).read_text())
    return copy.deepcopy(json.loads(record["response_text"]))


def parse(resp: dict | str, run_id: str = RUN_ID):
    store = sources()[0]
    text = resp if isinstance(resp, str) else json.dumps(resp)
    return parse_model_response(text, TASK, store, run_id)


def codes(issues) -> list[str]:
    return [i.code for i in issues]


def outcome_from(resp: dict | str, *, source_docs: tuple[str, ...] | None = None) -> ExtractionOutcome:
    spec, unresolved, issues = parse(resp)
    return ExtractionOutcome(
        spec=spec, unresolved=unresolved, issues=issues, provider="test", model="mock-model", live=False, run_id=RUN_ID,
        prompt_sha256="0" * 64, raw_response=resp if isinstance(resp, str) else json.dumps(resp),
        source_doc_ids=source_docs or TASK.doc_ids,
    )


def verifier() -> SourceVerifier:
    return SourceVerifier(sources()[0], key=b"extraction-test-key-not-a-secret")


def package(resp: dict | str, *, source_docs: tuple[str, ...] | None = None) -> ReviewPackage:
    return prepare_review(outcome_from(resp, source_docs=source_docs), verifier(), sources()[2])


def find_first(node, predicate):
    """Depth-first search for a dict satisfying `predicate` inside nested dict/list JSON."""
    if isinstance(node, dict):
        if predicate(node):
            return node
        for v in node.values():
            hit = find_first(v, predicate)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for v in node:
            hit = find_first(v, predicate)
            if hit is not None:
                return hit
    return None
