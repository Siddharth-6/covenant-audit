"""Effective-dated versioning, invalidation, and the hash-chained ledger.

Each test states its expected result and why."""

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from covenant_audit.base import evolve
from covenant_audit.ledger import GENESIS, Ledger, LedgerError, LedgerTamperedError
from covenant_audit.models import ComplianceStatus as S, CovenantKind, ReviewDecision
from covenant_audit.pipeline import recompute_stale, run_period
from covenant_audit.registry import NoApplicableSpecError, RegistryError, SpecRegistry, SpecRevokedError

from .factory import NOW, SAMPLE_VALUES, World

D = Decimal
KIND = CovenantKind.MAX_TOTAL_NET_LEVERAGE
Q2, Q3 = date(2025, 6, 30), date(2025, 9, 30)


@pytest.fixture()
def world() -> World:
    return World()


@pytest.fixture()
def env(world):
    ledger = Ledger()
    registry = SpecRegistry(world.verifier, ledger)
    registry.register(world.build_spec(), NOW)
    return world, ledger, registry


def amendment(world, spec_id="SPEC-2", version=2, supersedes="SPEC-1", **kw):
    kw = {"addback_pct": "0.15", "effective_from": date(2025, 7, 1), "executed_on": date(2025, 6, 20), **kw}
    return world.build_spec(spec_id=spec_id, version=version, supersedes=supersedes, **kw)


def run_q(world, registry, ledger, period=Q2, values=SAMPLE_VALUES):
    facts = world.package(values, period)
    return run_period(registry, ledger, "CA-1", KIND, period, facts.values(), world.policy, world.verifier, now=NOW)


# ---------------------------------------------------------------- registry rules


def test_registry_rejects_unapproved_specs(world):
    """Expected: the registry refuses DRAFT and VERIFIED specs. Why: only approved, sealed specs may govern a period."""
    registry = SpecRegistry(world.verifier)
    with pytest.raises(Exception):
        registry.register(world.build_spec(approve=False))
    with pytest.raises(Exception):
        registry.register(world.verifier.verify_spec(world.build_spec(approve=False)))


def test_version_chain_rules(env):
    """Expected: duplicates, skipped versions, wrong supersedes, a non-later effective date and a missing executed_on
    are each refused. Why: versions form an unbroken, dated chain and are never overwritten."""
    world, ledger, registry = env
    with pytest.raises(RegistryError, match="already registered"):
        registry.register(world.build_spec())
    with pytest.raises(RegistryError, match="version 2 and supersede SPEC-1"):
        registry.register(amendment(world, spec_id="SPEC-3", version=3))
    with pytest.raises(RegistryError, match="version 2 and supersede SPEC-1"):
        registry.register(amendment(world, supersedes="SPEC-0"))
    with pytest.raises(RegistryError, match="must be after"):
        registry.register(amendment(world, effective_from=date(2025, 1, 1), executed_on=date(2024, 12, 1)))
    with pytest.raises(RegistryError, match="executed_on"):
        registry.register(amendment(world, executed_on=None))
    first = SpecRegistry(world.verifier)
    with pytest.raises(RegistryError, match="version 1"):
        first.register(amendment(world))


def test_spec_for_selects_the_version_in_force_on_each_date(env):
    """Expected: SPEC-1 up to 30 Jun 2025, SPEC-2 from 1 Jul 2025; nothing before SPEC-1's start.
    Why: each version governs test dates from its effective_from until the next version's."""
    world, ledger, registry = env
    registry.register(amendment(world), NOW)
    assert registry.spec_for("CA-1", KIND, date(2025, 1, 1)).spec_id == "SPEC-1"
    assert registry.spec_for("CA-1", KIND, Q2).spec_id == "SPEC-1"
    assert registry.spec_for("CA-1", KIND, date(2025, 7, 1)).spec_id == "SPEC-2"
    with pytest.raises(NoApplicableSpecError):
        registry.spec_for("CA-1", KIND, date(2024, 12, 31))
    assert registry.versions("CA-1", KIND)[0].spec_id == "SPEC-1"  # preserved


# ---------------------------------------------------------------- retroactivity


def test_retroactive_amendment_invalidates_and_recomputes_earlier_periods(env):
    """Expected: an amendment that cites a retroactivity clause makes the Q2 result stale; recomputation applies the
    amended 15% cap (EBITDA 115, 4.35x, thin headroom) and supersedes the old result, which is kept.
    Why: only an explicit contractual clause lets an amendment reach back, and even then history is superseded, not edited."""
    world, ledger, registry = env
    q2 = run_q(world, registry, ledger, Q2)
    assert q2.ratio_tested == D("4.00")
    clause = world.span(("AMD-1", 1, "the Lenders agree this Amendment applies to the Test Period ending June 30, 2025"))
    registry.register(
        amendment(world, effective_from=date(2025, 4, 1), executed_on=date(2025, 8, 1), retroactive_source=clause), NOW
    )
    assert ledger.is_stale(q2.result_id) and ledger.pending_recompute() == [q2.result_id]
    facts = world.package(SAMPLE_VALUES, Q2)
    redone = recompute_stale(registry, ledger, q2.result_id, facts.values(), world.policy, world.verifier, now=NOW)
    assert (redone.spec_id, redone.ratio_tested, redone.status) == ("SPEC-2", D("4.35"), S.PASS_THIN_HEADROOM)
    assert redone.supersedes_result_id == q2.result_id and ledger.current_results() == [redone]
    assert ledger.get_result(q2.result_id).spec_id == "SPEC-1"
    ledger.verify_chain()


def test_non_retroactive_amendment_leaves_history_alone(env):
    """Expected: no result is marked stale. Why: executed 20 Jun, effective 1 Jul: it does not reach periods before 1 Jul."""
    world, ledger, registry = env
    q2 = run_q(world, registry, ledger, Q2)
    registry.register(amendment(world), NOW)
    assert not ledger.is_stale(q2.result_id) and ledger.invalidations() == []


def test_recompute_refuses_results_that_are_not_stale(env):
    """Expected: LedgerError. Why: recomputing a current result would silently create a competing record."""
    world, ledger, registry = env
    q2 = run_q(world, registry, ledger, Q2)
    with pytest.raises(LedgerError, match="not stale"):
        recompute_stale(registry, ledger, q2.result_id, world.package(SAMPLE_VALUES).values(), world.policy, world.verifier)


# ---------------------------------------------------------------- revocation


def test_revoked_spec_invalidates_results_and_blocks_until_corrected(env):
    """Expected: revoking SPEC-2 marks its results stale and blocks Q3 (SpecRevokedError), without falling back to SPEC-1;
    registering a corrected SPEC-3 unblocks Q3, and the stale result is recomputed under SPEC-3.
    Why: reverting to the old definition would silently apply the wrong contract terms; a corrected approved spec is required."""
    world, ledger, registry = env
    registry.register(amendment(world), NOW)
    q3 = run_q(world, registry, ledger, Q3)
    assert q3.spec_id == "SPEC-2" and q3.ratio_tested == D("4.35")
    registry.revoke("SPEC-2", "extractor misread the cap as 15%; the amendment says 20%", NOW)
    assert ledger.is_stale(q3.result_id)
    with pytest.raises(SpecRevokedError, match="corrected approved spec"):
        registry.spec_for("CA-1", KIND, Q3)
    with pytest.raises(SpecRevokedError):
        run_q(world, registry, ledger, Q3)
    assert registry.spec_for("CA-1", KIND, Q2).spec_id == "SPEC-1"  # earlier periods unaffected
    corrected = amendment(world, spec_id="SPEC-3", version=3, supersedes="SPEC-2", addback_pct="0.25")
    registry.register(corrected, NOW)
    assert registry.spec_for("CA-1", KIND, Q3).spec_id == "SPEC-3"
    redone = recompute_stale(registry, ledger, q3.result_id, world.package(SAMPLE_VALUES, Q3).values(), world.policy, world.verifier, now=NOW)
    assert redone.spec_id == "SPEC-3" and redone.ratio_tested == D("4.00")


def test_revoking_the_only_spec_blocks_all_periods(env):
    """Expected: SpecRevokedError for every date. Why: with no valid version left there is nothing to apply."""
    world, ledger, registry = env
    registry.revoke("SPEC-1", "wrong", NOW)
    with pytest.raises(SpecRevokedError):
        registry.spec_for("CA-1", KIND, Q2)
    with pytest.raises(RegistryError, match="unknown spec"):
        registry.revoke("NOPE", "x", NOW)


# ---------------------------------------------------------------- ledger integrity


def filled_ledger(env):
    world, ledger, registry = env
    run_q(world, registry, ledger, Q2)
    run_q(world, registry, ledger, Q3, {**SAMPLE_VALUES, "total_debt": "640"})
    return ledger


def test_hash_chain_verifies_and_round_trips_results(env):
    """Expected: verify_chain passes, and every stored result reloads identical to what was written.
    Why: payloads are canonical JSON; reloading re-runs the result validators."""
    ledger = filled_ledger(env)
    ledger.verify_chain()
    assert [e.seq for e in ledger._entries] == list(range(len(ledger._entries)))
    assert ledger._entries[0].prev_hash == GENESIS
    assert len(ledger.results()) == 2 and ledger.results()[1].status is S.BREACH


def test_tampering_with_a_stored_result_is_detected(env):
    """Expected: LedgerTamperedError naming the entry. Why: flipping a breach to a pass changes the payload hash."""
    ledger = filled_ledger(env)
    idx = next(i for i, e in enumerate(ledger._entries) if e.kind == "result" and "breach" in ledger._payloads[i])
    ledger._payloads[idx] = ledger._payloads[idx].replace('"status":"breach"', '"status":"pass"')
    with pytest.raises(LedgerTamperedError, match="payload was modified"):
        ledger.verify_chain()


def test_deleting_reordering_or_forging_entries_is_detected(env):
    """Expected: LedgerTamperedError for a deleted entry, a re-ordered pair, and a recomputed-but-forged entry hash.
    Why: each entry hash covers its sequence number and the previous hash."""
    ledger = filled_ledger(env)
    import copy

    deleted = copy.deepcopy(ledger)
    del deleted._entries[1], deleted._payloads[1]
    with pytest.raises(LedgerTamperedError):
        deleted.verify_chain()
    swapped = copy.deepcopy(ledger)
    swapped._entries[1], swapped._entries[2] = swapped._entries[2], swapped._entries[1]
    swapped._payloads[1], swapped._payloads[2] = swapped._payloads[2], swapped._payloads[1]
    with pytest.raises(LedgerTamperedError):
        swapped.verify_chain()
    forged = copy.deepcopy(ledger)
    forged._entries[2] = evolve(forged._entries[2], entry_hash="e" * 64)
    with pytest.raises(LedgerTamperedError, match="entry hash"):
        forged.verify_chain()


def test_checkpoint_detects_truncation_and_history_rewrites(env):
    """Expected: with a saved checkpoint, dropping the newest entries or rebuilding the whole chain is detected.
    Why: a self-consistent rewritten chain still cannot match the head hash saved earlier (the production anchor)."""
    world, ledger, registry = env
    run_q(world, registry, ledger, Q2)
    cp = ledger.checkpoint()
    run_q(world, registry, ledger, Q3)
    ledger.verify_chain(cp)
    import copy

    truncated = copy.deepcopy(ledger)
    del truncated._entries[cp.length - 1 :], truncated._payloads[cp.length - 1 :]
    with pytest.raises(LedgerTamperedError, match="checkpoint"):
        truncated.verify_chain(cp)
    rewritten = Ledger()
    registry2 = SpecRegistry(world.verifier, rewritten)
    registry2.register(world.build_spec(), NOW)
    run_q(world, registry2, rewritten, Q2, {**SAMPLE_VALUES, "total_debt": "441"})
    run_q(world, registry2, rewritten, Q3)
    rewritten.verify_chain()  # internally consistent...
    with pytest.raises(LedgerTamperedError, match="rewritten"):
        rewritten.verify_chain(cp)  # ...but not the history that was anchored


def test_duplicate_and_dangling_appends_are_refused(env):
    """Expected: LedgerError for a duplicate result id, a supersedes pointing nowhere, and a review of an unknown result.
    Why: the ledger keeps one record per result and references must resolve."""
    world, ledger, registry = env
    r = run_q(world, registry, ledger, Q2)
    with pytest.raises(LedgerError, match="already recorded"):
        ledger.append_result(r, world.policy, NOW)
    with pytest.raises(LedgerError, match="unknown result"):
        ledger.append_result(evolve(r, result_id="R-new", supersedes_result_id="R-missing"), world.policy, NOW)
    with pytest.raises(LedgerError, match="unknown result"):
        ledger.record_review("R-missing", ReviewDecision(reviewer_id="u", reviewer_role="credit_officer", decision="confirmed", decided_at=NOW))


def test_human_review_is_recorded_without_touching_the_result(env):
    """Expected: a breach waits in awaiting_review until a decision is appended; the result itself is unchanged.
    Why: reviews are separate, signed-off ledger entries so the machine result stays as computed."""
    world, ledger, registry = env
    breach = run_q(world, registry, ledger, Q2, {**SAMPLE_VALUES, "total_debt": "640"})
    assert ledger.awaiting_review() == [breach]
    decision = ReviewDecision(
        reviewer_id="analyst@bank", reviewer_role="monitoring_analyst", decision="confirmed", decided_at=NOW
    )
    ledger.record_review(breach.result_id, decision)
    assert ledger.awaiting_review() == [] and ledger.reviews_for(breach.result_id)[0].decision == decision
    assert ledger.get_result(breach.result_id) == breach
    ledger.verify_chain()
