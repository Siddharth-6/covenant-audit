"""Mutation check: break one guarantee at a time and confirm the test suite notices.

    python3 tools/mutation_check.py

Each mutation is applied to the source, pytest is run, and the file is restored. Every mutation must be KILLED."""
import subprocess, shutil, sys
muts = [
 ("hardcoded thin policy", "covenant_audit/models.py", "if pct < thin_pct:", 'if pct < Decimal("0.10"):'),
 ("MIN cap becomes MAX", "covenant_audit/arith.py", "return min(operands)", "return max(operands)"),
 ("ledger skips payload hash", "covenant_audit/ledger.py", "if sha256_hex(payload) != entry.payload_hash:", "if False:"),
 ("registry ignores effective_from", "covenant_audit/registry.py", "applicable = [s for s in candidates if s.effective_from <= test_date]", "applicable = list(candidates)"),
 ("pipeline trusts unsealed facts", "covenant_audit/pipeline.py", "verifier.check_fact(fact)", "pass"),
 ("no seal check on spec", "covenant_audit/verification.py", 'self._check_seal("spec", spec.content_hash(), spec.seal, f"spec {spec.spec_id}")', "pass"),
 ("rounding ignored", "covenant_audit/arith.py", "if places is None:\n        return ratio", "if True:\n        return ratio"),
 ("negative denominator allowed", "covenant_audit/engine.py", "if denominator < 0:", "if False:"),
 ("stale facts accepted", "covenant_audit/pipeline.py", "if fact.fact_id in stale_fact_ids:", "if False:"),
 ("review not required for breach", "covenant_audit/models.py", "if self.status in must_review and not self.needs_review:", "if False:"),
 ("fact value not matched to quote", "covenant_audit/verification.py", "if fact.value not in {amount * fact.reported_scale for amount in printed}:", "if False:"),
 ("tie-out components ungrounded OK", "covenant_audit/verification.py", "if total != tie_out.expected:", "if False:"),
]
survived=[]
import os
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
for name,path,old,new in muts:
    src=open(path).read()
    assert old in src,(name,"pattern missing")
    shutil.copy(path,"/tmp/orig.bak")
    open(path,"w").write(src.replace(old,new,1))
    r=subprocess.run([sys.executable,"-m","pytest","-q","-x","-p","no:cacheprovider"],capture_output=True,text=True)
    shutil.copy("/tmp/orig.bak",path)
    killed = r.returncode!=0
    print(f"{'KILLED  ' if killed else 'SURVIVED'} {name}")
    if not killed: survived.append(name)
print("survivors:",survived)
sys.exit(1 if survived else 0)
