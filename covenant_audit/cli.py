"""Command line interface.

    python3 -m covenant_audit.cli sources                       verify the committed SEC documents
    python3 -m covenant_audit.cli extract --provider openrouter   live model extraction (needs OPENROUTER_API_KEY)
    python3 -m covenant_audit.cli extract --provider recorded     replay the reference response (NOT a model call)
    python3 -m covenant_audit.cli review                          show the reviewer's view of the pending extraction
    python3 -m covenant_audit.cli score                           check the pending extraction against the contract's key facts
    python3 -m covenant_audit.cli approve --approver ID           explicit human approval (type the spec hash to confirm)
    python3 -m covenant_audit.cli reject --approver ID --reason ...
    python3 -m covenant_audit.cli compute                         register the approved spec, run the deterministic engine
    python3 -m covenant_audit.cli demo --provider ...             all of the above in sequence

Exit codes: 0 ok, 1 extraction/verification failed, 2 configuration missing, 3 approval refused.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from .approval import ApprovalAttestationError
from .demo import render_trace
from .extraction import describe_provenance, extract_covenant
from .ledger import Ledger
from .llm import LLMConfigurationError, LLMProvider, OpenRouterProvider, RecordedProvider
from .pipeline import replay_result, run_period
from .policy import BankPolicy
from .registry import SpecRegistry
from .scoring import render_scorecard, score_outcome
from .review import ApprovalRefused, StateDir, approve, prepare_review, reject, render_review
from .synthetic import SyntheticDataError, build_synthetic_facts
from .viad_demo import DEFAULT_TEST_DATE, REFERENCE_RESPONSE, SYNTHETIC_FINANCIALS, TASK, excerpts, load_sources

DEFAULT_STATE = Path(os.environ.get("COVENANT_STATE_DIR", ".covenant_state"))
CONFIG_MESSAGE = (
    "Live extraction requires configuration and was NOT run.\n"
    "  export OPENROUTER_API_KEY=<your key>\n"
    "  export OPENROUTER_MODEL=<a model id from https://openrouter.ai/models, e.g. a free-tier model>\n"
    "The deterministic engine and the test suite do not need these. To replay the committed reference response\n"
    "instead (clearly labelled as NOT a model call), run with:  --provider recorded"
)


def _provider(name: str) -> LLMProvider:
    if name == "recorded":
        return RecordedProvider(REFERENCE_RESPONSE)
    return OpenRouterProvider()


def cmd_sources(_: argparse.Namespace) -> int:
    _store, _docs, manifests = load_sources()
    for m in manifests:
        print(f"{m.doc_id}: {m.company}\n  {m.description}\n  SEC accession {m.accession_number} (Form {m.form}, {m.exhibit}, filed {m.filed})"
              f"\n  {m.filing_url}\n  {m.page_count} pages, canonical sha256 {m.canonical_sha256}\n  hash check against committed pages: OK")
        for a in m.known_amendments:
            print(f"  known amendment: {a.doc_id} dated {a.dated}: {a.description}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    try:
        provider = _provider(args.provider)
    except LLMConfigurationError as exc:
        print(f"CONFIGURATION MISSING: {exc}\n\n{CONFIG_MESSAGE}", file=sys.stderr)
        return 2
    store, loaded_docs, _ = load_sources()
    selected = excerpts(loaded_docs)
    state = StateDir(args.state_dir)
    print(f"Excerpts selected deterministically: {len(selected.excerpts)} passages, {selected.chars:,} characters; "
          f"{len(selected.included_terms)} defined terms followed; {len(selected.not_provided_terms)} referenced terms not provided.")
    outcome = extract_covenant(provider, TASK, store, selected, max_attempts=args.attempts)
    path = state.save_outcome(outcome)
    print(describe_provenance(outcome))
    print(f"Saved untrusted extraction to {path}")
    for issue in outcome.issues:
        print(f"  ISSUE {issue.code} {issue.path}: {issue.message}")
    for item in outcome.unresolved:
        print(f"  UNRESOLVED ({item.kind}): {item.description}")
    print("Extraction produced a DRAFT spec; it is UNTRUSTED until verified and approved." if outcome.spec else "No spec was produced.")
    return 0 if outcome.spec else 1


def _package(state: StateDir):
    store, _, manifests = load_sources()
    verifier = state.verifier(store)
    return store, verifier, prepare_review(state.load_outcome(), verifier, manifests)


def cmd_review(args: argparse.Namespace) -> int:
    _, _, pkg = _package(StateDir(args.state_dir))
    print(render_review(pkg))
    return 0 if pkg.approvable else 3


def cmd_score(args: argparse.Namespace) -> int:
    outcome = StateDir(args.state_dir).load_outcome()
    checks = score_outcome(outcome)
    print(describe_provenance(outcome))
    print(render_scorecard(checks))
    return 0 if all(c.passed for c in checks) else 1


def cmd_approve(args: argparse.Namespace) -> int:
    state = StateDir(args.state_dir)
    store, verifier, pkg = _package(state)
    print(render_review(pkg))
    if not pkg.approvable:
        print("\nApproval is blocked; nothing was approved.", file=sys.stderr)
        return 3
    confirm = args.confirm
    if confirm is None:
        print(f"\nTo approve, type the first 12 characters of the spec content hash shown above ({pkg.spec.content_hash()[:12]})."
              "\nAnything else cancels.")
        confirm = input("confirm> ")
    try:
        spec = approve(pkg, approver_id=args.approver, role=args.role, confirm=confirm, verifier=verifier)
    except ApprovalRefused as exc:
        print(f"\nAPPROVAL REFUSED: {exc}", file=sys.stderr)
        return 3
    path = state.save_approved(spec)
    att = spec.approvals[0].attestation
    assert att is not None  # approve_spec always attaches one
    print(f"\nAPPROVED and signed: approval {att.approval_id} by {att.approver_id} ({att.role}) at {att.approved_at.isoformat()}"
          f"\n  spec hash {att.spec_hash}\n  signer {att.signer_id} (development signer: authenticates nothing about who the approver is)\n  saved to {path}")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    state = StateDir(args.state_dir)
    _, verifier, pkg = _package(state)
    try:
        rejection = reject(pkg, approver_id=args.approver, role=args.role, reason=args.reason, verifier=verifier)
    except (ApprovalRefused, ApprovalAttestationError) as exc:
        print(f"REJECTION NOT RECORDED: {exc}", file=sys.stderr)
        return 3
    print(f"Rejected; signed record saved to {state.save_rejection(rejection)}")
    return 0


def cmd_compute(args: argparse.Namespace) -> int:
    state = StateDir(args.state_dir)
    store, _, _ = load_sources()
    verifier = state.verifier(store)
    spec = state.load_approved()
    payload = json.loads(Path(args.financials).read_text())
    test_date = date.fromisoformat(args.test_date or payload.get("test_date", DEFAULT_TEST_DATE.isoformat()))
    now = datetime.now(timezone.utc)
    ledger = Ledger()
    registry = SpecRegistry(verifier, ledger)
    registry.register(spec, now)  # re-verifies the seal and the approval attestation
    print(f"Registered spec {spec.spec_id} (approval attestation verified). Governs test dates from {spec.effective_from}.")
    policy = BankPolicy(policy_id="DEMO-POLICY", version=1, thin_headroom_pct=Decimal("0.10"), clean_pass_sample_pct=Decimal("0"))
    try:
        facts = build_synthetic_facts(spec, payload["values_millions"], test_date, store, verifier, policy)
    except SyntheticDataError as exc:
        print(f"SYNTHETIC DATA MISMATCH: {exc}", file=sys.stderr)
        return 1
    print(f"\n{payload.get('NOTE', 'SYNTHETIC borrower data')}\nTest date {test_date}; {len(facts)} verified synthetic facts.")
    result = run_period(registry, ledger, spec.agreement_id, spec.kind, test_date, facts, policy, verifier, now=now)
    print("\nCalculation trace (each line is one AST node; values are exact Decimals; the model played no part):")
    print(render_trace(result))
    assert result.headroom_pct is not None or result.status.value == "indeterminate"
    print(f"\nRESULT {result.status.value.upper()}: ratio {result.ratio_raw} -> tested {result.ratio_tested}x vs maximum {result.threshold}x"
          + (f", headroom {result.headroom_abs} ({result.headroom_pct * 100:.2f}%)" if result.headroom_pct is not None else ""))
    for reason in result.reasons:
        print(f"  reason: {reason}")
    print(f"  spec {result.spec_id} {result.spec_hash[:16]} | policy {result.policy_id} v{result.policy_version} {result.policy_hash[:16]} | engine {result.engine_version} | needs_review={result.needs_review}")
    replay_result(result, spec, facts, policy, verifier)
    ledger.verify_chain()
    print(f"Replay reproduced the result exactly. Ledger hash chain verified: {len(ledger._entries)} entries, head {ledger.head_hash[:16]}.")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    print("=" * 100 + "\nEND-TO-END DEMONSTRATION on a real public SEC credit agreement (borrower financials are SYNTHETIC)\n" + "=" * 100)
    code = cmd_sources(args)
    print()
    code = cmd_extract(args)
    if code:
        return code
    print()
    if args.approver is None:
        print("A human approver is required: re-run with --approver <id> (you will be asked to type the spec hash), "
              "or review first with the 'review' command.")
        _, _, pkg = _package(StateDir(args.state_dir))
        print("\n" + render_review(pkg))
        return 3
    code = cmd_approve(args)
    if code:
        return code
    if args.provider == "recorded":
        print("\nDEMONSTRATION NOTE: this run replays a hand-authored reference response (not a model), and the approval above was entered on the"
              f"\ncommand line as '{args.approver}'. It shows the mechanism only; it is not a real credit officer's review of a real extraction.")
    print()
    args.financials = str(SYNTHETIC_FINANCIALS)
    args.test_date = None
    return cmd_compute(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="covenant_audit.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sources").set_defaults(func=cmd_sources)
    for name, func in (("extract", cmd_extract), ("demo", cmd_demo)):
        p = sub.add_parser(name)
        p.add_argument("--provider", choices=["openrouter", "recorded"], default="openrouter")
        p.add_argument("--attempts", type=int, default=2, help="max model attempts, feeding validation errors back (default 2)")
        if name == "demo":
            p.add_argument("--approver", default=None)
            p.add_argument("--role", choices=["credit_officer", "legal_reviewer"], default="credit_officer")
            p.add_argument("--confirm", default=None, help="first 12 chars of the spec hash; omit to be prompted")
        p.set_defaults(func=func)
    sub.add_parser("review").set_defaults(func=cmd_review)
    sub.add_parser("score").set_defaults(func=cmd_score)
    p = sub.add_parser("approve")
    p.add_argument("--approver", required=True)
    p.add_argument("--role", choices=["credit_officer", "legal_reviewer"], default="credit_officer")
    p.add_argument("--confirm", default=None)
    p.set_defaults(func=cmd_approve)
    p = sub.add_parser("reject")
    p.add_argument("--approver", required=True)
    p.add_argument("--role", choices=["credit_officer", "legal_reviewer"], default="credit_officer")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_reject)
    p = sub.add_parser("compute")
    p.add_argument("--financials", default=str(SYNTHETIC_FINANCIALS))
    p.add_argument("--test-date", default=None)
    p.set_defaults(func=cmd_compute)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
