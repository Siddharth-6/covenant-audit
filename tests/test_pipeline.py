"""Pipeline guards, review routing, replay and policy history.

Each test states its expected result and why."""

from datetime import date
from decimal import Decimal

import pytest

from covenant_audit.base import evolve
from covenant_audit.ledger import Ledger, LedgerError
from covenant_audit.models import ComplianceStatus as S, CovenantKind
from covenant_audit.pipeline import ENGINE_VERSION, ReplayMismatchError, replay_result, run_covenant_test, run_period
from covenant_audit.registry import SpecRegistry
from covenant_audit.verification import SealError, SpecNotApprovedError

from .factory import NOW, SAMPLE_VALUES, World, make_policy

D = Decimal
Q2 = date(2025, 6, 30)
KIND = CovenantKind.MAX_TOTAL_NET_LEVERAGE


@pytest.fixture()
def world() -> World:
    return World()


def run(world, spec, facts, policy=None, test_date=Q2, **kw):
    f = facts.values() if isinstance(facts, dict) else facts
    return run_covenant_test(spec, f, test_date, policy or world.policy, world.verifier, now=NOW, **kw)


# ---------------------------------------------------------------- spec gate


def test_unapproved_or_forged_spec_cannot_be_run(world):
    """Expected: SpecNotApprovedError for a VERIFIED-only spec; SealError for a DRAFT or hand-sealed spec.
    Why: the engine executes only human-approved specs whose seal still matches their content."""
    facts = world.package(SAMPLE_VALUES)
    verified = world.verifier.verify_spec(world.build_spec(approve=False))
    with pytest.raises(SpecNotApprovedError):
        run(world, verified, facts)
    with pytest.raises(SealError):
        run(world, world.build_spec(approve=False), facts)


# ---------------------------------------------------------------- input admission


def test_fact_for_the_wrong_period_is_not_used(world):
    """Expected: INDETERMINATE. Why: a 31 Mar balance cannot stand in for 30 Jun."""
    spec = world.build_spec()
    r = run(world, spec, world.package(SAMPLE_VALUES, date(2025, 3, 31)))
    assert r.status is S.INDETERMINATE and any("not test date" in x for x in r.reasons)


def test_conflicting_duplicate_facts_are_rejected(world):
    """Expected: INDETERMINATE naming the conflict. Why: two cash figures for one period are ambiguous; picking one is a guess."""
    spec = world.build_spec()
    facts = world.package(SAMPLE_VALUES)
    other = world.package({**SAMPLE_VALUES, "cash": "41"}, fact_suffix="-b")
    r = run(world, spec, [*facts.values(), other["cash"]])
    assert r.status is S.INDETERMINATE and any("conflicting facts for 'cash'" in x for x in r.reasons)


def test_flow_input_must_cover_twelve_months(world):
    """Expected: INDETERMINATE for a 3-month figure supplied as LTM EBITDA.
    Why: annualising by accident is a classic error; the spec says this input is a last-twelve-months flow."""
    spec = world.build_spec()
    facts = world.package(SAMPLE_VALUES, seal=False)
    short = evolve(facts["base_ebitda"], period_start=date(2025, 4, 1))
    facts["base_ebitda"] = world.verifier.verify_fact(short, world.policy)
    sealed = {k: (v if v.seal else world.verifier.verify_fact(v, world.policy)) for k, v in facts.items()}
    r = run(world, spec, sealed)
    assert r.status is S.INDETERMINATE and any("ltm_flow" in x for x in r.reasons)


def test_negative_value_for_a_non_negative_input_is_rejected(world):
    """Expected: INDETERMINATE for negative total debt. Why: the input is declared sign='positive'; a negative would
    mean a sign-convention error in extraction."""
    spec = world.build_spec()
    facts = world.package({**SAMPLE_VALUES, "total_debt": "-540"}, seal=False)
    facts["total_debt"] = evolve(facts["total_debt"], tie_outs=())
    policy = evolve(world.policy, required_tie_outs=())
    sealed = {k: world.verifier.verify_fact(v, policy) for k, v in facts.items()}
    r = run(world, spec, sealed, policy=policy)
    assert r.status is S.INDETERMINATE and any("negative" in x for x in r.reasons)


def test_mixed_currencies_are_rejected(world):
    """Expected: INDETERMINATE. Why: adding USD debt to EUR cash is meaningless; no FX conversion is modelled."""
    spec = world.build_spec()
    facts = world.package(SAMPLE_VALUES, seal=False)
    facts["cash"] = world.verifier.verify_fact(evolve(facts["cash"], currency="EUR"), world.policy)
    sealed = {k: (v if v.seal else world.verifier.verify_fact(v, world.policy)) for k, v in facts.items()}
    r = run(world, spec, sealed)
    assert r.status is S.INDETERMINATE and any("different currencies" in x for x in r.reasons)


def test_date_outside_the_threshold_schedule_is_indeterminate(world):
    """Expected: INDETERMINATE. Why: the spec defines no covenant level before 1 Jan 2025."""
    spec = world.build_spec()
    r = run(world, spec, world.package(SAMPLE_VALUES, date(2024, 12, 31)), test_date=date(2024, 12, 31))
    assert r.status is S.INDETERMINATE and any("no covenant level" in x for x in r.reasons)


def test_unrelated_facts_are_ignored(world):
    """Expected: an extra sealed fact the ratio does not use changes nothing. Why: only reachable inputs are mandatory."""
    spec = world.build_spec()
    facts = world.package(SAMPLE_VALUES)
    base = run(world, spec, facts)
    extra = evolve(facts["cash"], fact_id="F-other", input_name="capex")
    sealed_extra = world.verifier.verify_fact(evolve(extra, seal=None), world.policy)
    again = run(world, spec, [*facts.values(), sealed_extra])
    assert again.ratio_tested == base.ratio_tested and again.fact_ids == base.fact_ids


# ---------------------------------------------------------------- review routing


def test_ocr_evidence_below_the_policy_floor_routes_to_review(world):
    """Expected: a PASS whose add-backs rest on 0.80-confidence OCR is flagged for review; 0.97 confidence is not.
    Why: the policy floor is 0.90; low-confidence OCR is exactly where a digit can be misread."""
    spec = world.build_spec()
    outcomes = {}
    for conf in (0.80, 0.97):
        facts = world.package(SAMPLE_VALUES)
        world.store.register(f"SCAN-{conf}", ("LTM add-backs: 40,000,000",), origin="ocr")
        span = world.store.locate(f"SCAN-{conf}", 1, "LTM add-backs: 40,000,000", ocr_confidence=conf)
        ocr_fact = world.verifier.verify_fact(evolve(facts["addbacks"], source=span, seal=None, fact_id=f"F-ocr-{conf}"), world.policy)
        r = run(world, spec, [facts["base_ebitda"], ocr_fact, facts["total_debt"], facts["cash"]])
        outcomes[conf] = r
    assert outcomes[0.80].status is S.PASS and outcomes[0.80].needs_review
    assert any("OCR" in x for x in outcomes[0.80].reasons)
    assert not outcomes[0.97].needs_review


def test_clean_pass_sampling_is_policy_driven_and_deterministic(world):
    """Expected: 100% sampling flags every clean pass, 0% flags none, and the same key always gives the same answer;
    a 50% policy samples roughly half of 400 keys. Why: sampling rate is bank policy, and reproducible hashing lets an
    auditor re-derive why a pass was or was not sampled."""
    spec, facts = world.build_spec(), world.package(SAMPLE_VALUES)
    assert run(world, spec, facts, make_policy(sample="1")).needs_review
    assert not run(world, spec, facts, make_policy(sample="0")).needs_review
    half = make_policy(sample="0.5")
    keys = [f"k{i}" for i in range(400)]
    picked = [half.is_sampled(k) for k in keys]
    assert picked == [half.is_sampled(k) for k in keys]
    assert 140 < sum(picked) < 260


def test_result_pins_every_version_used(world):
    """Expected: the result records spec id/version/hash, policy id/version/hash/value, engine and schema versions and exact fact ids.
    Why: an auditor must be able to say exactly which rules and inputs produced a number."""
    spec, facts = world.build_spec(), world.package(SAMPLE_VALUES)
    r = run(world, spec, facts)
    assert (r.spec_id, r.spec_version, r.spec_hash) == (spec.spec_id, spec.version, spec.content_hash())
    assert (r.policy_id, r.policy_version, r.policy_hash) == ("BANK-POLICY", 1, world.policy.content_hash())
    assert r.thin_headroom_pct == D("0.10") and r.engine_version == ENGINE_VERSION and r.schema_version == "2"
    assert r.fact_ids == tuple(sorted(f.fact_id for f in facts.values()))
    assert len(r.tie_outs) == 2 and all(t.passed for t in r.tie_outs)


# ---------------------------------------------------------------- replay and policy history


def test_replay_reproduces_the_stored_result_and_detects_changed_inputs(world):
    """Expected: replay with the same spec, facts and policy matches exactly; a different fact or policy is a mismatch.
    Why: an auditor re-runs sealed inputs through the pinned engine and must get the same numbers, trace and status."""
    spec, facts = world.build_spec(), world.package(SAMPLE_VALUES)
    stored = run(world, spec, facts)
    assert replay_result(stored, spec, facts.values(), world.policy, world.verifier).result_id == stored.result_id
    other = world.package({**SAMPLE_VALUES, "cash": "41"})
    with pytest.raises(ReplayMismatchError, match="fact_ids|trace|ratio"):
        replay_result(stored, spec, other.values(), world.policy, world.verifier)
    with pytest.raises(ReplayMismatchError, match="policy_hash|thin_headroom"):
        replay_result(stored, spec, facts.values(), make_policy(thin="0.05", version=2), world.verifier)


def test_policy_change_does_not_alter_historical_results(world):
    """Expected: a result computed under policy v1 stays THIN under v1 even after policy v2 exists; the ledger keeps both
    policies by hash; a result cannot be filed under a policy it was not computed with.
    Why: each result records the policy hash and value it used, so tightening or loosening policy later re-interprets nothing."""
    ledger = Ledger()
    registry = SpecRegistry(world.verifier, ledger)
    registry.register(world.build_spec(), NOW)
    facts = world.package({**SAMPLE_VALUES, "total_debt": "560"})
    v1 = run_period(registry, ledger, "CA-1", KIND, Q2, facts.values(), world.policy, world.verifier, now=NOW)
    assert v1.status is S.PASS_THIN_HEADROOM
    v2_policy = make_policy(thin="0.05", version=2)
    spec = registry.spec_for("CA-1", KIND, Q2)
    v2 = run(world, spec, facts, policy=v2_policy)
    ledger.append_result(v2, v2_policy, NOW)
    assert v2.status is S.PASS
    reloaded = ledger.get_result(v1.result_id)
    assert reloaded.status is S.PASS_THIN_HEADROOM and ledger.policy_for(reloaded) == world.policy
    assert ledger.policy_for(ledger.get_result(v2.result_id)) == v2_policy
    other_policy_result = run(world, spec, world.package({**SAMPLE_VALUES, "total_debt": "561"}), policy=v2_policy)
    with pytest.raises(LedgerError, match="not computed under the supplied policy"):
        ledger.append_result(other_policy_result, world.policy, NOW)
    ledger.verify_chain()
