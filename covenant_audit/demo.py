"""Run one covenant calculation end to end on synthetic data and print the audit trace.

    python -m covenant_audit.demo
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from .ledger import Ledger
from .models import CovenantKind, CovenantTestResult
from .pipeline import replay_result, run_period
from .registry import SpecRegistry
from .sample import NOW, SAMPLE_VALUES, World


def _fmt(value: Decimal) -> str:
    """Display only: long quotients are shortened for reading; stored values are never rounded."""
    text = f"{value:,}"
    if "." in text and len(text.split(".")[1]) > 10:
        return f"{value.quantize(Decimal('1e-10')):,}"
    return text


def render_trace(result: CovenantTestResult) -> str:
    """Human-readable view of the trace: one line per evaluated node, with its operands, value and sources."""
    lines = []
    for s in result.trace:
        operands = " , ".join(_fmt(v) for v in s.operand_values)
        source = ""
        if s.fact_id and s.evidence:
            source = f'   <- {s.fact_id}  "{s.evidence.quote}" (p.{s.evidence.page}, chars {s.evidence.char_start}-{s.evidence.char_end})'
        elif s.op == "lit" and s.source_refs:
            ref = s.source_refs[0]
            source = f'   <- contract "{ref.quote}" (p.{ref.page}, {ref.clause_ref or ref.doc_id})'
        elif s.op == "term" and s.source_refs:
            source = f'   <- defined by "{s.source_refs[0].quote[:60]}..."'
        shown = f"{s.op}({operands})" if s.operand_values else s.op
        lines.append(f"  {s.step_id:>2}  {s.node_id:<42} {shown:<46} = {_fmt(s.value)}{source}")
    return "\n".join(lines)


def main() -> None:
    world = World()
    ledger = Ledger()
    registry = SpecRegistry(world.verifier, ledger)
    spec = world.build_spec()
    registry.register(spec, NOW)
    test_date = date(2025, 6, 30)
    facts = world.package(SAMPLE_VALUES, test_date)

    print("SYNTHETIC DATA ONLY. Borrower inputs (millions): " + ", ".join(f"{k}={v}" for k, v in SAMPLE_VALUES.items()))
    print(f"Spec {spec.spec_id} v{spec.version} [{spec.status.value}] hash {spec.content_hash()[:16]}  covenant: ratio <= {spec.threshold_on(test_date)} on {test_date}")
    result = run_period(
        registry, ledger, "CA-1", CovenantKind.MAX_TOTAL_NET_LEVERAGE, test_date, facts.values(), world.policy, world.verifier, now=NOW
    )
    print("\nCalculation trace (every line is one AST node; values are exact Decimals):")
    print(render_trace(result))
    assert result.headroom_pct is not None
    print(
        f"\nResult {result.result_id}: {result.status.value.upper()}  ratio {result.ratio_raw} -> tested {result.ratio_tested}x "
        f"vs {result.threshold}x  headroom {result.headroom_abs} ({_fmt(result.headroom_pct * 100)}%)"
    )
    print(f"  spec {result.spec_id} v{result.spec_version} {result.spec_hash[:16]}, policy {result.policy_id} v{result.policy_version} "
          f"{result.policy_hash[:16]} (thin-headroom {result.thin_headroom_pct}), engine {result.engine_version}, needs_review={result.needs_review}")
    replay_result(result, spec, facts.values(), world.policy, world.verifier)
    ledger.verify_chain()
    print(f"Replay from sealed inputs reproduced the result exactly; ledger hash chain verified ({len(ledger.results())} result, head {ledger.head_hash[:16]}).")


if __name__ == "__main__":
    main()
