"""The scorecard must fail on wrong extractions, otherwise a perfect score on the reference means nothing.
Each test states its expected result and why."""

from covenant_audit.scoring import render_scorecard, score_outcome

from .helpers import find_first, outcome_from, reference


def failed(resp) -> list[str]:
    return [c.name for c in score_outcome(outcome_from(resp)) if not c.passed]


def test_reference_captures_every_key_fact():
    """Expected: all checks pass. Why: the reference was written against this checklist. This is circular by construction, so the
    tests below matter more."""
    assert failed(reference()) == []


def test_wrong_threshold_and_wrong_rounding_are_caught():
    """Expected: a changed 4.75 level and a two-place rounding rule fail their checks. Why: they differ from the amended text."""
    resp = reference()
    resp["covenant"]["thresholds"][1]["level"]["value"] = "4.70"
    resp["covenant"]["ratio_decimal_places"] = 2
    assert {"threshold schedule matches the amended Section 7.10(c)", "rounding: three places, half up (Section 1.04)"} <= set(failed(resp))


def test_missing_cash_condition_and_missing_cap_are_caught():
    """Expected: replacing the conditional cash cap with an unconditional MIN, and dropping the 20% constant's quote, fail.
    Why: a model that simplifies the clause would silently overstate or understate netted cash and EBITDA."""
    resp = reference()
    term = next(t for t in resp["covenant"]["terms"] if t["name"] == "netted_unrestricted_cash")
    term["formula"] = term["formula"]["then"]
    cap = next(t for t in resp["covenant"]["terms"] if t["name"] == "accepted_cost_savings")
    node = find_first(cap["formula"], lambda d: d.get("op") == "lit" and d.get("value") == "0.20")
    node["value"] = "0.30"
    names = failed(resp)
    assert "cash cap is conditional on $10,000,000 of revolver borrowings" in names and "20.0% cap on the run-rate savings add-back" in names


def test_base_agreement_thresholds_instead_of_the_amendment_are_caught():
    """Expected: citing only the base agreement's covenant (the missing-amendment error) fails the amendment checks.
    Why: the base schedule (4.50x then 4.00x) was restated by the First Amendment."""
    resp = reference()
    spans = [s for t in resp["covenant"]["thresholds"] for s in t["level"]["spans"]]
    for s in spans:
        s.update(doc_id="VIAD-CA-2021", page=146, quote="December 31, 2022 and thereafter 4.00 to 1.00")
    names = failed(resp)
    assert "thresholds are cited to the First Amendment" in names


def test_no_spec_scores_as_a_failure_and_report_is_readable():
    """Expected: a response with no spec yields a single failed check; the report states the score. Why: failures must be visible, not hidden."""
    outcome = outcome_from("not json")
    checks = score_outcome(outcome)
    assert [c.passed for c in checks] == [False]
    assert "0/1" in render_scorecard(checks) and "not an approval decision" in render_scorecard(checks)
