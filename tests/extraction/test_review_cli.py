"""Approval workflow persistence and the command line. Offline; each test states expected result and why."""

import json
from pathlib import Path

import pytest

from covenant_audit.cli import main
from covenant_audit.review import StateDir

from .helpers import sources


def run(capsys, tmp_path: Path, *argv: str) -> tuple[int, str, str]:
    code = main(["--state-dir", str(tmp_path / "state"), *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def spec_hash(out: str) -> str:
    return next(line.split()[-1] for line in out.splitlines() if line.startswith("SPEC CONTENT HASH"))


def test_live_extraction_without_configuration_reports_it_and_runs_nothing(capsys, tmp_path, monkeypatch):
    """Expected: exit code 2, a message naming OPENROUTER_API_KEY and the NOT-a-model-call alternative, and no state written.
    Why: the demonstration must say it needs configuration instead of pretending to call a model."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    code, out, err = run(capsys, tmp_path, "extract", "--provider", "openrouter")
    assert code == 2 and "OPENROUTER_API_KEY" in err and "NOT run" in err and "--provider recorded" in err
    assert not (tmp_path / "state" / "pending_extraction.json").exists()


def test_sources_command_prints_the_public_filing_references(capsys, tmp_path):
    """Expected: company, accession numbers, URLs and hash checks for both documents. Why: provenance of the contract text is part of the output."""
    code, out, _ = run(capsys, tmp_path, "sources")
    assert code == 0 and "0001564590-21-039835" in out and "0000950170-22-004478" in out and "Viad Corp" in out
    assert "https://www.sec.gov/Archives/edgar/data/884219/" in out and out.count("hash check against committed pages: OK") == 2


def test_full_cli_flow_extract_review_approve_compute(capsys, tmp_path):
    """Expected: replay-extract is labelled NOT a live call; review shows the readable formula and no blockers; a wrong confirmation is
    refused (exit 3); the right one approves and persists a signed spec; compute registers it (re-verifying the attestation) and prints
    PASS 3.646x vs 4.75x with the trace and a verified ledger. Why: this is the whole workflow, each step enforced."""
    code, out, _ = run(capsys, tmp_path, "extract", "--provider", "recorded")
    assert code == 0 and "NOT A LIVE MODEL CALL" in out and "UNTRUSTED until verified and approved" in out
    code, review, _ = run(capsys, tmp_path, "review")
    assert code == 0 and "No blocking issues" in review and "IF revolving_and_swing_line_outstanding > 10,000,000 THEN MIN(unrestricted_cash, 50,000,000)" in review
    h = spec_hash(review)
    code, _, err = run(capsys, tmp_path, "approve", "--approver", "reviewer@example.com", "--confirm", "badbadbadbad")
    assert code == 3 and "confirmation does not match" in err
    code, _, err = run(capsys, tmp_path, "approve", "--approver", "agent-7", "--confirm", h[:12])
    assert code == 3 and "cannot approve" in err
    code, out, _ = run(capsys, tmp_path, "approve", "--approver", "reviewer@example.com", "--confirm", h[:12])
    assert code == 0 and "APPROVED and signed" in out and "authenticates nothing about who the approver is" in out
    code, out, _ = run(capsys, tmp_path, "compute")
    assert code == 0 and "approval attestation verified" in out and "RESULT PASS" in out and "tested 3.646x vs maximum 4.75x" in out
    assert "Ledger hash chain verified" in out and "min(40,000,000 , 36,800,000.00)" in out


def test_approving_without_an_extraction_or_with_a_blocked_one_does_nothing(capsys, tmp_path):
    """Expected: approve with no pending extraction raises a clear error; approve on an extraction with unresolved items exits 3 and writes no approved spec.
    Why: an unresolved or missing extraction can never become an approved spec."""
    with pytest.raises(FileNotFoundError, match="extract"):
        run(capsys, tmp_path, "approve", "--approver", "reviewer@example.com", "--confirm", "x")
    run(capsys, tmp_path, "extract", "--provider", "recorded")
    state = StateDir(tmp_path / "state")
    outcome = state.load_outcome()
    from covenant_audit.extraction import UnresolvedItem
    from covenant_audit.base import evolve

    blocked = evolve(outcome, unresolved=(UnresolvedItem(kind="ambiguity", description="EBITDA clause (d) reading is ambiguous"),))
    state.save_outcome(blocked)
    code, out, err = run(capsys, tmp_path, "approve", "--approver", "reviewer@example.com", "--confirm", "x")
    assert code == 3 and "APPROVAL BLOCKED" in out and "ambiguous" in out and "nothing was approved" in err
    assert not list((tmp_path / "state").glob("*.approved.json"))


def test_reject_command_records_a_signed_rejection(capsys, tmp_path):
    """Expected: a rejection with a reason is saved and the spec is not approved. Why: refusals are recorded."""
    run(capsys, tmp_path, "extract", "--provider", "recorded")
    code, out, _ = run(capsys, tmp_path, "reject", "--approver", "reviewer@example.com", "--reason", "cap reading needs legal input")
    assert code == 0 and "signed record saved" in out
    saved = next((tmp_path / "state").glob("*.rejected.json"))
    assert json.loads(saved.read_text())["attestation"]["decision"] == "rejected"
    assert not list((tmp_path / "state").glob("*.approved.json"))


def test_a_persisted_approval_edited_on_disk_is_rejected_at_registration(capsys, tmp_path):
    """Expected: editing the approver in the saved approved-spec JSON makes `compute` fail verification. Why: the attestation
    signature detects any change to approval metadata after it was saved."""
    run(capsys, tmp_path, "extract", "--provider", "recorded")
    _, review, _ = run(capsys, tmp_path, "review")
    run(capsys, tmp_path, "approve", "--approver", "reviewer@example.com", "--confirm", spec_hash(review)[:12])
    path = next((tmp_path / "state").glob("*.approved.json"))
    path.write_text(path.read_text().replace("reviewer@example.com", "someone.else@example.com"))
    from covenant_audit.verification import SealError

    with pytest.raises(SealError):
        run(capsys, tmp_path, "compute")


def test_extraction_state_round_trips_through_json(tmp_path):
    """Expected: a saved extraction reloads equal to the original. Why: review happens in a later process than extraction."""
    from .helpers import outcome_from, reference

    state = StateDir(tmp_path / "s")
    outcome = outcome_from(reference())
    state.save_outcome(outcome)
    assert state.load_outcome() == outcome
    assert sources()
