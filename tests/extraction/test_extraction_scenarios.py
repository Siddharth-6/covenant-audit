"""LLM-extraction workflow tests (offline; no API key, no network).

They run on the committed text of a REAL public SEC credit agreement (Viad Corp, 2021, and its 2022 First
Amendment) and a committed reference response, applying one targeted corruption per test. Every test states its
expected result and why. The deterministic arithmetic tests live elsewhere and are untouched."""

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from covenant_audit.base import evolve
from covenant_audit.ledger import Ledger
from covenant_audit.models import ComplianceStatus as S, SpecStatus
from covenant_audit.pipeline import run_period
from covenant_audit.policy import BankPolicy
from covenant_audit.registry import NoApplicableSpecError, SpecRegistry
from covenant_audit.review import ApprovalRefused, approve, prepare_review, render_review, reject
from covenant_audit.synthetic import build_synthetic_facts
from covenant_audit.verification import SealError
from covenant_audit.viad_demo import SYNTHETIC_FINANCIALS, TASK

from .helpers import RUN_ID, codes, find_first, outcome_from, package, parse, reference, sources, verifier

D = Decimal
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
CA, FA = "VIAD-CA-2021", "VIAD-FA1-2022"


def term(resp, name):
    return next(t for t in resp["covenant"]["terms"] if t["name"] == name)


def first_span(resp, predicate=lambda s: True):
    return find_first(resp, lambda d: "quote" in d and "page" in d and predicate(d))


# ---------------------------------------------------------------- 1. success


def test_01_successful_extraction_of_a_real_covenant():
    """Expected: a DRAFT spec with no issues and no unresolved items; every citation was located by software in the
    real filing text, with its offsets and the stored document hash; the review package is approvable.
    Why: the reference response quotes verbatim text that occurs exactly once on the cited page."""
    spec, unresolved, issues = parse(reference())
    assert issues == () and unresolved == () and spec is not None
    assert spec.status is SpecStatus.DRAFT and spec.seal is None and spec.approvals == () and spec.extractor_run_id == RUN_ID
    store = sources()[0]
    spans = list(spec.all_spans())
    assert len(spans) > 40 and {s.doc_id for s in spans} == {CA, FA}
    for s in spans:
        doc = store.get(s.doc_id)
        assert s.doc_sha256 == doc.sha256 and doc.pages[s.page - 1][s.char_start : s.char_end] == s.quote
    assert [t.level.value for t in sorted(spec.thresholds, key=lambda t: t.effective_from)] == [D("5.25"), D("4.75"), D("4.50"), D("4.00")]
    pkg = package(reference())
    assert pkg.approvable and pkg.spec.status is SpecStatus.VERIFIED and pkg.blocking == []


# ---------------------------------------------------------------- 2-3. malformed / incomplete


@pytest.mark.parametrize("text", ["not json at all", '{"covenant": {"kind": "max_total', "[1, 2, 3]", "", "null"])
def test_02_malformed_json_is_rejected_without_exception(text):
    """Expected: no spec, a single 'malformed_json' issue, no exception. Why: unusable model output must become an
    explicit, recorded failure rather than a crash or a partial spec."""
    spec, _, issues = parse(text)
    assert spec is None and codes(issues) == ["malformed_json"]


def test_02b_a_json_code_fence_is_tolerated():
    """Expected: JSON wrapped in a ```json fence parses. Why: models commonly add fences; the content is still validated in full."""
    spec, _, issues = parse("```json\n" + json.dumps(reference()) + "\n```")
    assert spec is not None and issues == ()


@pytest.mark.parametrize("missing", ["denominator", "numerator", "inputs", "thresholds", "effective_from"])
def test_03_missing_mandatory_definition_yields_no_spec(missing):
    """Expected: no spec and a schema_error naming the missing field. Why: the covenant cannot be computed without a
    numerator, denominator, inputs, thresholds and an effective date, and an incomplete extraction must not proceed."""
    resp = reference()
    del resp["covenant"][missing]
    spec, _, issues = parse(resp)
    assert spec is None and any(i.code == "schema_error" and missing in i.path for i in issues)
    assert not outcome_from(resp).blocking_reasons == []


def test_03b_response_without_a_covenant_object():
    """Expected: issue 'no_covenant'. Why: nothing to build."""
    spec, _, issues = parse({"unresolved": []})
    assert spec is None and codes(issues) == ["no_covenant"]


# ---------------------------------------------------------------- 4-6. citations


def test_04_invented_quotation_is_rejected():
    """Expected: 'quote_not_found', no spec. Why: the model cites a 6.25x threshold that the amendment does not contain."""
    resp = reference()
    span = first_span(resp, lambda s: s["quote"].startswith("Test Period Ending Maximum Total Net Leverage Ratio September 30, 2022"))
    span["quote"] = "Test Period Ending Maximum Total Net Leverage Ratio September 30, 2022 6.25 to 1.00"
    spec, _, issues = parse(resp)
    assert spec is None and "quote_not_found" in codes(issues)


def test_04b_paraphrase_and_ambiguous_quotes_are_rejected():
    """Expected: a paraphrase is 'quote_not_found'; a quote occurring several times on the page is 'ambiguous_quote'.
    Why: quotations must be exact, and an ambiguous one could point at the wrong passage."""
    resp = reference()
    first_span(resp)["quote"] = "the cap on cost savings is twenty percent"
    assert "quote_not_found" in codes(parse(resp)[2])
    resp = reference()
    first_span(resp, lambda s: s["page"] == 18)["quote"] = "Consolidated EBITDA"
    assert "ambiguous_quote" in codes(parse(resp)[2])


def test_05_incorrect_character_offsets_are_rejected_and_correct_ones_accepted():
    """Expected: offsets the model volunteers must be exactly right: wrong offsets are 'incorrect_offsets'; correct
    offsets are accepted; one offset alone is refused. Why: offsets are normally computed by software; claimed ones
    are checked, never trusted or silently 'fixed'."""
    resp = reference()
    span = first_span(resp, lambda s: s["doc_id"] == CA and s["page"] == 61)
    text = sources()[0].get(CA).pages[60]
    start = text.find(span["quote"])
    span.update(char_start=start + 5, char_end=start + 5 + len(span["quote"]))
    spec, _, issues = parse(resp)
    assert spec is None and "incorrect_offsets" in codes(issues)
    span.update(char_start=start, char_end=start + len(span["quote"]))
    assert parse(resp)[0] is not None
    del span["char_end"]
    assert "incorrect_offsets" in codes(parse(resp)[2])


def test_06_wrong_document_hash_unknown_document_and_bad_page():
    """Expected: a wrong claimed hash is 'wrong_document_hash'; an unprovided document is 'unknown_document'; a page
    beyond the end is 'page_out_of_range'; the correct hash is accepted. Why: the citation must point at the exact stored text."""
    store = sources()[0]
    for field, value, code in (("doc_sha256", "f" * 64, "wrong_document_hash"), ("doc_id", "SOME-OTHER-DOC", "unknown_document"), ("page", 9999, "page_out_of_range")):
        resp = reference()
        first_span(resp)[field] = value
        assert code in codes(parse(resp)[2]), field
    resp = reference()
    span = first_span(resp)
    span["doc_sha256"] = store.get(span["doc_id"]).sha256
    assert parse(resp)[0] is not None


def test_06b_model_cannot_choose_the_text_origin_or_ocr_confidence():
    """Expected: 'forbidden_span_field'. Why: native-vs-OCR provenance comes from the stored document, not from the model."""
    resp = reference()
    first_span(resp)["text_origin"] = "ocr"
    assert "forbidden_span_field" in codes(parse(resp)[2])


# ---------------------------------------------------------------- 7-9. formula validity


def test_07_unsupported_ast_operation_is_rejected():
    """Expected: schema_error 'unsupported expression operation' for an operation the AST lacks (pow). Why: the model may
    only use the nine existing node types; anything else cannot be evaluated deterministically."""
    resp = reference()
    node = term(resp, "accepted_cost_savings")["formula"]["args"][1]
    node.update(op="pow")
    spec, _, issues = parse(resp)
    assert spec is None and any("unsupported expression operation" in i.message for i in issues)


def test_07b_executable_content_is_forbidden():
    """Expected: 'forbidden_field' for code, eval, tool_calls and similar keys anywhere in the response, and the whole
    response is discarded. Why: nothing the model returns may carry executable intent."""
    for key, value in (("code", "__import__('os').system('id')"), ("eval", "1+1"), ("tool_calls", [{"name": "send_wire"}]), ("python", "print(1)")):
        resp = reference()
        term(resp, "accepted_dividends")["formula"][key] = value
        spec, _, issues = parse(resp)
        assert spec is None and "forbidden_field" in codes(issues), key


def test_08_cyclic_term_reference_is_rejected():
    """Expected: schema_error mentioning a cycle. Why: making accepted_dividends depend on consolidated_ebitda closes the
    loop dividends -> ebitda_before_clause_d -> accepted_dividends; the existing spec validator refuses it."""
    resp = reference()
    term(resp, "accepted_dividends")["formula"] = {"op": "term", "name": "consolidated_ebitda"}
    spec, _, issues = parse(resp)
    assert spec is None and any("cyclic" in i.message for i in issues)


def test_09_unresolved_contractual_cross_reference():
    """Expected: (a) a reference to a term nobody defined is a schema_error 'unresolved defined term'; (b) a cross-reference
    the model itself could not resolve is returned as an unresolved item and blocks approval, with the item in the refusal.
    Why: an unresolved definition must never become part of an approved spec."""
    resp = reference()
    term(resp, "accepted_dividends")["formula"]["args"][0] = {"op": "term", "name": "section_1_08_adjustment"}
    spec, _, issues = parse(resp)
    assert spec is None and any("unresolved defined term" in i.message for i in issues)
    resp = reference()
    resp["unresolved"] = [{"kind": "cross_reference", "description": "'Pro Forma Basis' refers to Section 1.08, which was not provided", "reference": "Section 1.08"}]
    pkg = package(resp)
    assert pkg.spec is not None and not pkg.approvable
    with pytest.raises(ApprovalRefused, match="Section 1.08"):
        approve(pkg, approver_id="reviewer@example.com", role="credit_officer", confirm=pkg.spec.content_hash()[:12], verifier=verifier())


# ---------------------------------------------------------------- 10-11. amendments and ambiguity


def test_10_missing_amendment_blocks_approval():
    """Expected: (a) if the extractor was never given the First Amendment, a citation of it is 'unknown_document' and the
    review reports the known amendment as unread; (b) a model-reported missing amendment blocks approval.
    Why: the base agreement's covenant was restated by the amendment; treating a missing amendment as absent would apply superseded terms."""
    pkg = package(reference(), source_docs=(CA,))
    assert [a.doc_id for a in pkg.missing_amendments] == [FA]
    assert not pkg.approvable and any("known amendment VIAD-FA1-2022" in b for b in pkg.blocking)
    from dataclasses import replace

    from covenant_audit.extraction import parse_model_response

    base_only = replace(TASK, doc_ids=(CA,))
    spec, _, issues = parse_model_response(json.dumps(reference()), base_only, sources()[0], RUN_ID)
    assert spec is None and "unknown_document" in codes(issues)
    resp = reference()
    resp["unresolved"] = [{"kind": "missing_amendment", "description": "Section 7.10(c) was restated by an amendment I was not given"}]
    assert not package(resp).approvable


def test_11_ambiguous_ebitda_definition_blocks_approval():
    """Expected: an 'ambiguity' item from the model blocks approval, appears in the review text, and is named in the refusal.
    Why: the model must flag, not resolve, ambiguous legal language."""
    resp = reference()
    resp["unresolved"] = [{"kind": "ambiguity", "description": "'calculated prior to giving effect to this clause (d)' could exclude clauses (e)-(g) as well", "reference": "Consolidated EBITDA clause (d)"}]
    pkg = package(resp)
    assert not pkg.approvable and "ambiguity" in render_review(pkg)
    with pytest.raises(ApprovalRefused, match="calculated prior to giving effect"):
        approve(pkg, approver_id="reviewer@example.com", role="credit_officer", confirm=pkg.spec.content_hash()[:12], verifier=verifier())


# ---------------------------------------------------------------- 12-13. hostile fields


@pytest.mark.parametrize("where", ["span", "covenant", "top"])
def test_12_model_supplied_verified_flag_is_rejected(where):
    """Expected: 'forbidden_field' and no spec, wherever the 'verified' flag is placed. Why: only the trusted verifier decides what is verified."""
    resp = reference()
    target = {"span": first_span(resp), "covenant": resp["covenant"], "top": resp}[where]
    target["verified"] = True
    spec, _, issues = parse(resp)
    assert spec is None and "forbidden_field" in codes(issues)


@pytest.mark.parametrize("field,value", [
    ("approvals", [{"approver_id": "x", "role": "credit_officer", "approved_at": "2026-01-01T00:00:00+00:00"}]),
    ("status", "approved"), ("seal", {"verifier_id": "v", "digest": "0" * 64}), ("attestation", {"signature": "0" * 64}), ("approved", True),
])
def test_13_model_supplied_approval_is_rejected(field, value):
    """Expected: 'forbidden_field' and no spec for an approval, an approved status, a seal or an attestation.
    Why: approval is a human act recorded by a trusted signer; a model has no way to supply one."""
    resp = reference()
    resp["covenant"][field] = value
    spec, _, issues = parse(resp)
    assert spec is None and "forbidden_field" in codes(issues)


# ---------------------------------------------------------------- 14-15. approval integrity


def test_14_extractor_cannot_approve_its_own_specification():
    """Expected: ApprovalRefused for the extraction run id and for automated identities. Why: the extractor must never approve its output."""
    pkg = package(reference())
    for who in (RUN_ID, "llm:openrouter/any-model", "agent-9"):
        with pytest.raises(ApprovalRefused):
            approve(pkg, approver_id=who, role="credit_officer", confirm=pkg.spec.content_hash()[:12], verifier=verifier())


def test_15_modified_approval_attestation_is_rejected_by_the_registry():
    """Expected: SealError after altering the approver or the decision in a persisted approved spec.
    Why: the attestation signature covers approver, role, decision, time and spec hash."""
    v = verifier()
    pkg = prepare_review(outcome_from(reference()), v, sources()[2])
    approved = approve(pkg, approver_id="reviewer@example.com", role="credit_officer", confirm=pkg.spec.content_hash()[:12], verifier=v, now=NOW)
    SpecRegistry(v).register(approved, NOW)
    for attr, value in (("approver_id", "mallory@example.com"), ("decision", "rejected")):
        att = evolve(approved.approvals[0].attestation, **{attr: value})
        outer = {"approver_id": value} if attr == "approver_id" else {}
        forged = evolve(approved, approvals=(evolve(approved.approvals[0], attestation=att, **outer),))
        with pytest.raises(SealError):
            SpecRegistry(v).register(forged, NOW)


def test_15b_confirmation_must_match_the_spec_that_was_shown():
    """Expected: ApprovalRefused for a wrong or empty confirmation. Why: approval is bound to the displayed spec's hash."""
    pkg = package(reference())
    for wrong in ("", "000000000000", pkg.spec.content_hash()[:11]):
        with pytest.raises(ApprovalRefused, match="confirmation"):
            approve(pkg, approver_id="reviewer@example.com", role="credit_officer", confirm=wrong, verifier=verifier())


def test_15c_rejection_is_recorded_not_approved():
    """Expected: a signed rejection; the spec stays unapproved. Why: the reviewer's refusal is part of the audit trail."""
    v = verifier()
    pkg = prepare_review(outcome_from(reference()), v, sources()[2])
    rejection = reject(pkg, approver_id="reviewer@example.com", role="credit_officer", reason="cap should be read as 25 percent", verifier=v, now=NOW)
    assert rejection.attestation.decision == "rejected" and pkg.spec.status is SpecStatus.VERIFIED


# ---------------------------------------------------------------- 16. approval -> deterministic calculation


def _approved_registry():
    v = verifier()
    pkg = prepare_review(outcome_from(reference()), v, sources()[2])
    spec = approve(pkg, approver_id="reviewer@example.com", role="credit_officer", confirm=pkg.spec.content_hash()[:12], verifier=v, now=NOW)
    ledger = Ledger()
    registry = SpecRegistry(v, ledger)
    registry.register(spec, NOW)
    return v, spec, ledger, registry


def _facts(spec, v, test_date, **override):
    values = json.loads(SYNTHETIC_FINANCIALS.read_text())["values_millions"] | override
    policy = BankPolicy(policy_id="TEST", version=1, thin_headroom_pct="0.10", clean_pass_sample_pct="0")
    return build_synthetic_facts(spec, values, test_date, sources()[0], v, policy), policy


def test_16_extraction_to_approval_to_deterministic_calculation():
    """Expected: the approved real-covenant spec yields PASS at 3.646x against the amended 4.75x level on 31 Dec 2022, with
    EBITDA 220.8m (the 20.0% add-back cap reduces 40m to 36.8m), netted cash 50m (cap applies because revolver borrowings of 25m
    exceed 10m) and net funded debt 805m. The result equals an independent hand calculation, the trace cites the filing text,
    and the ledger verifies. Why: the model only wrote the spec; every number comes from the deterministic engine."""
    v, spec, ledger, registry = _approved_registry()
    test_date = datetime(2022, 12, 31).date()
    facts, policy = _facts(spec, v, test_date)
    result = run_period(registry, ledger, spec.agreement_id, spec.kind, test_date, facts, policy, v, now=NOW)
    ebitda_pre_d = D(70 + 25 + 20 + 40 + 18 + 3 + 0 + 6 + 4 + 0 - 2)  # clause (a) dividends capped at min(3, 5) = 3
    savings = min(D(40), D("0.20") * ebitda_pre_d)
    ebitda = ebitda_pre_d + savings
    net_debt = D(800 + 80 + 5) - min(D(30), D(50)) - D(0) - min(D(120), D(50))  # revolver 25 > 10, so cash is capped at 50
    expected = (net_debt / ebitda).quantize(D("0.001"), rounding="ROUND_HALF_UP")
    assert (savings, ebitda, net_debt, expected) == (D("36.8"), D("220.8"), D(805), D("3.646"))
    assert result.status is S.PASS and result.ratio_tested == expected and result.threshold == D("4.75")
    by_node = {s.node_id: s for s in result.trace}
    assert by_node["term:accepted_cost_savings"].value == D("36800000.00")
    assert by_node["term:netted_unrestricted_cash"].value == D(50_000_000)
    step = by_node["term:netted_unrestricted_cash/ifgt"]
    assert step.operand_values[:2] == (D(25_000_000), D(10_000_000)) and step.value == D(50_000_000)
    cap = next(s for s in result.trace if s.op == "lit" and s.value == D("0.20"))
    assert cap.source_refs[0].doc_id == CA and "20.0%" in cap.source_refs[0].quote
    assert result.ratio_decimal_places == 3 and result.rounding == "ROUND_HALF_UP"
    ledger.verify_chain()
    assert [a.approver_id for a in ledger.approvals_for(spec.spec_id)] == ["reviewer@example.com"]


def test_16b_the_covenant_steps_down_and_dates_before_the_amendment_are_refused():
    """Expected: the same financials are PASS_THIN_HEADROOM against 4.00x on 30 Jun 2023 (headroom 8.85% < 10%), a clear PASS
    against 4.50x on 31 Mar 2023, and a date before the first scheduled test (30 Jun 2022) has no governing spec.
    Why: the schedule came from the amendment's table; before it, the base agreement's covenant was not in force here."""
    v, spec, ledger, registry = _approved_registry()
    outcomes = {}
    for d in (datetime(2023, 3, 31).date(), datetime(2023, 6, 30).date()):
        facts, policy = _facts(spec, v, d)
        outcomes[d] = run_period(registry, ledger, spec.agreement_id, spec.kind, d, facts, policy, v, now=NOW)
    q1, q2 = outcomes.values()
    assert (q1.threshold, q1.status) == (D("4.50"), S.PASS)
    assert (q2.threshold, q2.status) == (D("4.00"), S.PASS_THIN_HEADROOM) and q2.needs_review
    with pytest.raises(NoApplicableSpecError):
        registry.spec_for(spec.agreement_id, spec.kind, datetime(2022, 6, 30).date())


def test_16c_a_borrower_that_breaches_is_flagged():
    """Expected: BREACH when net funded debt is high enough (debt 1,100m gives 1,105/220.8 = 5.005x > 4.75x). Why: the engine, not the model, classifies."""
    v, spec, ledger, registry = _approved_registry()
    d = datetime(2022, 12, 31).date()
    facts, policy = _facts(spec, v, d, borrowed_money_debt="1100")
    r = run_period(registry, ledger, spec.agreement_id, spec.kind, d, facts, policy, v, now=NOW)
    assert r.status is S.BREACH and r.ratio_tested == D("5.005") and r.needs_review


# ---------------------------------------------------------------- deliberately incorrect extractions are refused


def test_incorrect_cap_cited_to_a_quote_that_says_otherwise_is_refused():
    """Expected: the extraction parses and every quote verifies (provenance is fine), but approval is REFUSED because the
    constant 0.25 does not appear in its own quotation ('20.0%'). Why: a syntactically valid, well-sourced but wrong
    reading is exactly what the semantic checks and the human gate exist to stop."""
    resp = reference()
    for t in resp["covenant"]["terms"]:
        node = find_first(t["formula"], lambda d: d.get("op") == "lit" and d.get("value") == "0.20")
        if node:
            node["value"] = "0.25"
    pkg = package(resp)
    assert pkg.spec is not None and pkg.verification_error is None
    assert not pkg.approvable and any("literal_not_in_quote" in b and "0.25" in b for b in pkg.blocking)
    with pytest.raises(ApprovalRefused, match="0.25"):
        approve(pkg, approver_id="reviewer@example.com", role="credit_officer", confirm=pkg.spec.content_hash()[:12], verifier=verifier())


@pytest.mark.parametrize("mutate,code", [
    (lambda r: r["covenant"]["thresholds"][0]["level"].update(value="5.35"), "threshold_not_in_quote"),
    (lambda r: r["covenant"].update(comparison="ge"), "comparison_direction"),
    (lambda r: r["covenant"]["inputs"].append({"name": "ghost_adjustment", "description": "invented input", "basis": "ltm_flow", "sign": "positive",
                                                "definition_spans": [r["covenant"]["inputs"][0]["definition_spans"][0]]}), "unused_input"),
    (lambda r: r["covenant"].pop("rounding_source"), "rounding_without_source"),
])
def test_other_deliberately_incorrect_extractions_are_refused(mutate, code):
    """Expected: approval refused with the named semantic check. Why: a wrong threshold, a maximum tested as a minimum, an
    invented input, or an unsourced rounding rule are each caught by deterministic checks before a human is asked."""
    resp = reference()
    mutate(resp)
    pkg = package(resp)
    assert not pkg.approvable and any(code in b for b in pkg.blocking), pkg.blocking


def test_structural_literal_cannot_dodge_the_citation_rule():
    """Expected: marking the 20% cap 'structural' (so it needs no source) is refused. Why: structural literals may only be 0 or 1."""
    resp = reference()
    for t in resp["covenant"]["terms"]:
        node = find_first(t["formula"], lambda d: d.get("op") == "lit" and d.get("value") == "0.20")
        if node:
            node.pop("source")
            node["structural"] = True
    pkg = package(resp)
    assert not pkg.approvable and any("structural_abuse" in b for b in pkg.blocking)


def test_tampering_a_resolved_citation_after_parsing_fails_independent_verification():
    """Expected: the review's own verification rejects a span whose offsets were altered after extraction.
    Why: prepare_review re-verifies every quotation against the stored document; it does not trust the extraction step."""
    outcome = outcome_from(reference())
    step = outcome.spec.thresholds[0]
    span = step.level.spans[0]
    bad_span = evolve(span, char_start=span.char_start + 1, char_end=span.char_end + 1)
    bad = evolve(outcome.spec, thresholds=(evolve(step, level=evolve(step.level, spans=(bad_span,))), *outcome.spec.thresholds[1:]))
    pkg = prepare_review(evolve(outcome, spec=bad), verifier(), sources()[2])
    assert pkg.spec is None and not pkg.approvable and "verification failed" in " ".join(pkg.blocking)


def test_known_limit_a_consistent_but_wrong_reading_passes_every_machine_check():
    """Expected: swapping numerator and denominator (EBITDA / debt) passes provenance and semantic checks and is approvable by the
    machine; the readable formula shows the swap. Why: this documents the limit of automation. Source verification proves
    provenance, not meaning; a human reading the generated formula is the only control for a wrong-but-consistent reading."""
    resp = reference()
    cov = resp["covenant"]
    cov["numerator"], cov["denominator"] = cov["denominator"], cov["numerator"]
    pkg = package(resp)
    assert pkg.approvable
    assert "RATIO = <consolidated_ebitda>  /  <consolidated_net_funded_indebtedness>" in render_review(pkg)
