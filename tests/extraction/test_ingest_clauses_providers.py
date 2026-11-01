"""Ingestion, clause selection, explanation and LLM provider tests. Offline; each states expected result and why."""

import json
from datetime import date
from pathlib import Path

import pytest

from covenant_audit.clauses import find_covenant_pages, index_definitions, select_excerpts
from covenant_audit.explain import explain_expr
from covenant_audit.extraction import ExtractionTask, build_request, describe_provenance, extract_covenant
from covenant_audit.ingest import IngestError, build_manifest, canonicalize_text, fetch_url, html_to_pages, register_source
from covenant_audit.llm import (
    LLMConfigurationError, LLMRateLimitError, LLMRequest, LLMResponseError, LLMTimeoutError,
    OpenRouterProvider, RecordedProvider, ScriptedProvider,
)
from covenant_audit.models import CovenantKind, IfGt, Input, Lit, MinOf
from covenant_audit.verification import DocumentStore
from covenant_audit.viad_demo import REFERENCE_RESPONSE, SOURCES_DIR, TASK

from .helpers import reference, sources

CA = "VIAD-CA-2021"


# ---------------------------------------------------------------- ingestion


def test_canonicalisation_rules():
    """Expected: curly quotes become ASCII, spacing inside quotes and before punctuation is removed, '20 .0%' becomes
    '20.0%', whitespace collapses, and no word changes. Why: EDGAR renders text with artefacts; the canonical form is documented."""
    raw = "\u201c Consolidated EBITDA \u201d means  the   sum ( a ) , not\u00a0more than 20 .0% ; Borrower\u2019s  share"
    assert canonicalize_text(raw) == "\"Consolidated EBITDA\" means the sum (a), not more than 20.0%; Borrower's share"


def test_nested_quoted_paragraph_does_not_scramble_spacing():
    """Expected: spacing around a defined term inside a longer quoted paragraph is fixed correctly.
    Why: regression for pairing straight quotes; open/close curly quotes are used while still distinguishable."""
    raw = "\u201c(a) Rule. the \u201c Effective Date \u201d): next"
    assert canonicalize_text(raw) == '"(a) Rule. the "Effective Date"): next'


def test_html_is_split_on_page_breaks_and_tags_removed():
    """Expected: pages split at <hr>, tags and scripts dropped, trailing empty pages removed, interior empty pages kept.
    Why: page numbers must stay aligned with the rendering, since quotations cite them."""
    html = "<p>One <b>bold</b></p><hr style='x'><script>evil()</script><p>Two</p><hr><hr><p>Four</p><hr>"
    assert html_to_pages(html) == ["One bold", "Two", "", "Four"]


def test_manifest_hash_protects_the_committed_pages():
    """Expected: the committed pages register; altering one character of a page is refused. Why: documents are immutable and hash-checked."""
    store, docs, manifests, _ = sources()
    m = manifests[0]
    assert (m.cik, m.accession_number) == ("0000884219", "0001564590-21-039835")
    assert m.page_count == len(docs[m.doc_id]) == 189
    altered = list(docs[m.doc_id])
    altered[145] = altered[145].replace("4.50", "4.51", 1)
    with pytest.raises(IngestError, match="do not match the manifest"):
        register_source(DocumentStore(), m, altered)


def test_build_manifest_records_both_hashes():
    """Expected: raw and canonical hashes are recorded and the page count matches. Why: the audit trail starts at ingestion."""
    raw = b"<p>Hello</p><hr><p>World</p>"
    m, pages = build_manifest(doc_id="D", company="C", cik="1", form="8-K", exhibit="EX-10", accession_number="a", filed=date(2020, 1, 1),
                              filing_url="https://example.invalid/x", description="d", raw=raw)
    assert pages == ["Hello", "World"] and m.page_count == 2 and len(m.raw_sha256) == len(m.canonical_sha256) == 64


def test_fetch_requires_a_contact_user_agent():
    """Expected: IngestError without an email in the User-Agent, before any network use. Why: SEC policy; also keeps tests offline."""
    with pytest.raises(IngestError, match="User-Agent"):
        fetch_url("https://www.sec.gov/x", "anonymous")


def test_source_references_are_recorded():
    """Expected: both public SEC documents carry company, CIK, accession number, URL and the known-amendment link.
    Why: the assignment requires the source to be identifiable."""
    _, _, manifests, _ = sources()
    base, amd = manifests
    assert base.filing_url.startswith("https://www.sec.gov/Archives/edgar/data/884219/") and base.exhibit == "EX-10.1"
    assert amd.accession_number == "0000950170-22-004478" and amd.filed == date(2022, 3, 24)
    assert [a.doc_id for a in base.known_amendments] == [amd.doc_id]


# ---------------------------------------------------------------- clause selection


def test_covenant_and_definitions_are_found_deterministically():
    """Expected: the covenant is located on p.146 of the agreement and pp.2-3 of the amendment; the definitions the ratio depends on
    (Consolidated EBITDA, Consolidated Net Funded Indebtedness, Unrestricted Cash, Test Period) are included; the rounding clause is
    present when requested; terms referenced but not read are reported. Why: the extractor must be given what it needs, and the reviewer told what it was not."""
    _, docs, _, selected = sources()
    assert set(selected.covenant_locations) == {(CA, 146), ("VIAD-FA1-2022", 2), ("VIAD-FA1-2022", 3)}
    for term in ("Consolidated EBITDA", "Consolidated Net Funded Indebtedness", "Unrestricted Cash", "Test Period", "Finance Lease Obligation"):
        assert term in selected.included_terms, term
    assert any("rounding" in e.reason for e in selected.excerpts)
    assert "Cash Equivalents" in selected.not_provided_terms and selected.chars < 60_000
    for e in selected.excerpts:  # every excerpt is a verbatim slice of the stored page
        assert e.text in docs[e.doc_id][e.page - 1]


def test_definition_index_and_covenant_page_finder_on_small_text():
    """Expected: definitions are indexed with their page; a page stating 'shall not permit the X' is the covenant page, and a following
    threshold-table page is included. Why: deterministic locating is the first step of extraction."""
    pages = ['"Alpha Ratio" means B / C. "Beta" has the meaning given in 1.01.', "The Borrower shall not permit the Alpha Ratio to exceed", "Test Period Ending Max 4.00 to 1.00"]
    idx = index_definitions({"D": pages})
    assert set(idx) == {"Alpha Ratio", "Beta"} and idx["Beta"][0].page == 1
    assert find_covenant_pages(pages, "Alpha Ratio") == [2, 3]
    sel = select_excerpts({"D": pages}, "Alpha Ratio")
    assert [e.page for e in sel.excerpts][:2] == [2, 3] and "Alpha Ratio" in sel.included_terms


# ---------------------------------------------------------------- explanation


def test_formula_explanation_is_generated_from_the_ast_deterministically():
    """Expected: readable infix text for the node types, identical on repeated calls. Why: the reviewer sees what the engine will
    compute, produced by code with no model involved."""
    from decimal import Decimal

    e = IfGt(left=Input(name="revolver"), right=Lit(value=Decimal("10000000"), structural=False),
             then=MinOf(args=(Input(name="cash"), Lit(value=Decimal("50000000")))), otherwise=Input(name="cash"))
    text = "IF revolver > 10,000,000 THEN MIN(cash, 50,000,000) ELSE cash"
    assert explain_expr(e) == text == explain_expr(e)


def test_explain_module_makes_no_model_calls():
    """Expected: explain.py imports only the standard library and the typed models. Why: the formula explanation must come from
    the AST by deterministic code, never from another LLM call."""
    import ast

    tree = ast.parse((Path(__file__).parent.parent.parent / "covenant_audit" / "explain.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert imported <= {"__future__", "decimal", "typing", ".models"}, imported


# ---------------------------------------------------------------- providers


def _ok(text: str = '{"covenant": {}}', finish="stop") -> tuple[int, dict, bytes]:
    return 200, {}, json.dumps({"model": "m", "choices": [{"message": {"content": text}, "finish_reason": finish}], "usage": {"total_tokens": 5}}).encode()


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "body": json.loads(body), "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def provider(*responses, retries=3):
    sleeps: list[float] = []
    t = FakeTransport(*responses)
    return OpenRouterProvider("sk-test-key", "test/model", transport=t, max_retries=retries, sleep=sleeps.append), t, sleeps


def test_missing_configuration_is_reported_not_faked(monkeypatch):
    """Expected: LLMConfigurationError when the key or model is missing. Why: live extraction needs configuration and must say so."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    with pytest.raises(LLMConfigurationError, match="OPENROUTER_API_KEY"):
        OpenRouterProvider()
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    with pytest.raises(LLMConfigurationError, match="OPENROUTER_MODEL"):
        OpenRouterProvider()


def test_request_offers_the_model_no_tools_and_the_key_is_only_in_the_header():
    """Expected: the request body has messages and a JSON response format but no tools/functions; the key appears only in the
    Authorization header, never in the body. Why: the model must have nothing to call, and secrets must not leak into prompts."""
    p, t, _ = provider(_ok())
    p.complete(LLMRequest(system="sys", user="user"))
    body = t.calls[0]["body"]
    assert not {"tools", "functions", "tool_choice", "function_call"} & set(body)
    assert body["temperature"] == 0.0 and body["response_format"] == {"type": "json_object"}
    assert "sk-test-key" not in json.dumps(body) and t.calls[0]["headers"]["Authorization"] == "Bearer sk-test-key"


def test_rate_limit_is_retried_with_retry_after_then_succeeds():
    """Expected: two 429s then success returns the answer; the provider waits as instructed (Retry-After, then backoff).
    Why: free-tier models rate-limit; bounded retry keeps the run alive without hammering."""
    p, t, sleeps = provider((429, {"Retry-After": "3"}, b""), (429, {}, b""), _ok("hello"))
    assert p.complete(LLMRequest("s", "u")).text == "hello"
    assert len(t.calls) == 3 and sleeps == [3.0, 2.0]


def test_persistent_rate_limit_and_timeouts_surface_as_typed_errors():
    """Expected: LLMRateLimitError after retries are exhausted; LLMTimeoutError for repeated timeouts. Why: outages must be explicit and bounded."""
    p, _, _ = provider(*[(429, {}, b"")] * 3, retries=2)
    with pytest.raises(LLMRateLimitError):
        p.complete(LLMRequest("s", "u"))
    p, t, _ = provider(*[LLMTimeoutError("t")] * 3, retries=2)
    with pytest.raises(LLMTimeoutError):
        p.complete(LLMRequest("s", "u"))
    assert len(t.calls) == 3


@pytest.mark.parametrize("response,match", [
    ((401, {}, b'{"error":"bad key"}'), "HTTP 401"),
    ((200, {}, b"not json"), "unexpected provider response"),
    ((200, {}, b'{"choices": []}'), "unexpected provider response"),
    (_ok("   "), "empty completion"),
    (_ok('{"covenant": {', finish="length"), "cut off"),
])
def test_unusable_provider_answers_are_errors(response, match):
    """Expected: LLMResponseError for an HTTP error, malformed envelope, empty text, or a completion truncated at the token limit.
    Why: an incomplete response must never be parsed as if it were whole."""
    p, _, _ = provider(response)
    with pytest.raises(LLMResponseError, match=match):
        p.complete(LLMRequest("s", "u"))


def test_recorded_provider_is_never_presented_as_live():
    """Expected: is_live False, the provenance sentence says NOT A LIVE MODEL CALL, and the extraction record carries live=False.
    Why: a replayed response must never pass as a model run."""
    rec = RecordedProvider(REFERENCE_RESPONSE)
    assert rec.is_live is False
    store, _, _, selected = sources()
    outcome = extract_covenant(rec, TASK, store, selected)
    assert outcome.live is False and outcome.spec is not None
    assert describe_provenance(outcome).startswith("NOT A LIVE MODEL CALL")
    live, _, _ = provider(_ok("{}"))  # OpenRouterProvider with a fake transport: its responses are marked live
    assert describe_provenance(extract_covenant(live, TASK, store, selected)).startswith("LIVE MODEL CALL")


def test_provider_outage_becomes_an_explicit_issue_not_an_exception():
    """Expected: an outcome with no spec and a 'provider_error' issue naming the failure. Why: the workflow records outages explicitly."""
    class Down(ScriptedProvider):
        def complete(self, request):
            raise LLMTimeoutError("provider unavailable")

    store, _, _, selected = sources()
    outcome = extract_covenant(Down([]), TASK, store, selected)
    assert outcome.spec is None and [i.code for i in outcome.issues] == ["provider_error"] and "LLMTimeoutError" in outcome.issues[0].message


def test_retry_feeds_back_validation_messages_only():
    """Expected: with max_attempts=2 a malformed first answer triggers a second request containing the issue text but not the bad
    output; the valid second answer wins. Why: retries help with fixable mistakes without re-feeding untrusted text."""
    good = json.dumps(reference())
    p = ScriptedProvider(["this is not json", good])
    store, _, _, selected = sources()
    outcome = extract_covenant(p, TASK, store, selected, max_attempts=2)
    assert outcome.spec is not None and len(p.requests) == 2
    assert "malformed_json" in p.requests[1].user and "this is not json" not in p.requests[1].user


def test_hostile_responses_are_not_retried():
    """Expected: a response with a forbidden field is rejected on the first attempt without a second model call.
    Why: a hostile or confused answer is not 'fixed' by asking again."""
    resp = reference()
    resp["tool_calls"] = [{"name": "send_wire"}]
    p = ScriptedProvider([json.dumps(resp), json.dumps(reference())])
    store, _, _, selected = sources()
    outcome = extract_covenant(p, TASK, store, selected, max_attempts=2)
    assert outcome.spec is None and len(p.requests) == 1


def test_prompt_contents_and_rules():
    """Expected: the prompt carries the rules (no invention, no tools, document text is data), the scope instructions, the page-labelled
    excerpts and the list of unread terms, and no secret. Why: the prompt is the model's whole world and is part of the audit record."""
    store, _, _, selected = sources()
    req = build_request(TASK, selected)
    assert "Never invent" in req.system and "no tools" in req.system and "untrusted DATA" in req.system
    assert "Acquisition Period proviso" in req.user and "[DOCUMENT VIAD-CA-2021 | PAGE 146" in req.user
    assert "Terms referenced by the excerpts but NOT provided" in req.user and "Cash Equivalents" in req.user
    assert "OPENROUTER" not in req.user and "api_key" not in req.user.lower()
    assert req.temperature == 0.0
