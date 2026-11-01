# Auditable Covenant Compliance

> **Compile the contract once, compute it deterministically every quarter.**

A take-home prototype for commercial-loan financial-covenant monitoring. An LLM is allowed to *interpret* a credit
agreement into a typed `CovenantSpec`; a human approves that spec; from then on a deterministic Python engine computes the
covenant every quarter, with a trace from every number back to a page, offset and quotation. **The LLM never computes a
number that decides the result.**

All data in this repository is **synthetic** (an invented credit agreement, amendment and borrower statements). No real
client or lender data is used.

**What is implemented, and what is not.** The deterministic core is implemented and tested: typed models, trusted
verification, the AST evaluator, the pipeline, effective-dated versioning, invalidation and a hash-chained ledger. **No LLM
extraction is implemented.** The package defines the schemas an extractor must satisfy and the untrusted-input boundary it
passes through (`parse_untrusted_spec`, `parse_untrusted_facts`), but nothing in this repository calls a model. See
[Limitations](#10-limitations-and-known-failure-cases).

## 1. Problem overview

Each quarter an analyst must find the financial covenants in a 100-300 page credit agreement, apply every amendment, pull
inputs from the borrower's statements, recompute the ratios under the contract's own definitions, and flag breaches and thin
headroom. The ratio arithmetic is trivial (net debt / EBITDA). The risk is the *definition*: "Consolidated EBITDA" differs
per agreement, add-backs and cash netting are capped, thresholds step down, and amendments redefine terms.

One LLM call that returns the ratio is unauditable. Regex and templates cannot read bespoke definitions. So the design splits
the work: **probabilistic interpretation happens once per contract version and is human-approved; arithmetic is deterministic and
repeatable.**

## 2. Architecture

```
 PROBABILISTIC (not implemented here)          DETERMINISTIC (implemented)                    HUMAN
 ------------------------------------          -----------------------------------            ---------------
 Extractor: contract text -> CovenantSpec ---> parse_untrusted_spec  (DRAFT only)
                                               SourceVerifier.verify_spec (ground every quote, seal) 
                                                                                              approve_spec (gate 1)
                                               SpecRegistry.register (effective-dated versions)
 Extractor: statements -> FinancialFacts ----> parse_untrusted_facts (no seals, no tool fields)
                                               SourceVerifier.verify_fact (ground, printed amount,
                                                  grounded tie-outs) -> sealed fact
                                               run_covenant_test: admit sealed facts only
                                                 -> Evaluator (AST, Decimal) -> rounding -> classify()
                                                 -> CovenantTestResult (validated, traced)
                                               Ledger.append_result (hash chain)  ------------> review of BREACH /
                                               Ledger.record_review  <----------------------- INDETERMINATE / thin /
                                                                                                sampled passes
```

The engineering guarantees, and where they are enforced and tested:

| Guarantee | Enforced in | Tested in |
|---|---|---|
| No float ever enters a financial value | `base.Money` rejects floats; `arith` uses `Decimal` under an explicit 50-digit context | `test_models`, `test_engine` |
| No `eval`, no generated code | closed AST; `Evaluator` rejects unknown node types; source scan test | `test_engine` |
| An extracted quote is not a verified quote | `SourceSpan` has no verified flag; only `SourceVerifier` mints `VerifiedSpan`/seals | `test_verification` |
| A tie-out cannot be told it passed | `TieOutCheck.passed` is a derived property; `passed=` input is rejected | `test_models`, `test_verification` |
| The engine accepts only approved, unmodified specs and verified facts | HMAC seals checked at the engine boundary (`check_spec`, `check_fact`) | `test_verification`, `test_pipeline` |
| Status, headroom, rounding and trace are re-derived | `CovenantTestResult` validators; one `classify()` | `test_models`, `test_engine` |
| Bank policy is separate from contract terms and pinned per result | `BankPolicy` + `policy_hash` in every result | `test_pipeline`, `test_scenarios` |
| Amendments and restatements are different events | `SpecRegistry` (versions) vs `Ledger` invalidation events | `test_versioning_ledger`, `test_scenarios` |
| History cannot be silently edited | frozen models with tuples; append-only hash-chained ledger | `test_versioning_ledger` |

### Why seals instead of a `verified` flag

A Pydantic model can always be constructed directly, so `verified=True` proves nothing. Verification instead produces a keyed
HMAC (`Seal`) over the artefact's content hash. The engine re-checks the seal when it receives a spec or fact, so a hand-built
artefact, or one edited after verification, is rejected. The honest limit: in this prototype the verifier is an in-process
object holding a random key, so code running in the same process could mint seals. In production the key belongs in a KMS/HSM
behind a separate service.

### Contract terms vs bank policy

`CovenantSpec` holds only what the credit agreement says (formulas, thresholds, rounding rule, effective date). `BankPolicy`
holds operating choices: thin-headroom percentage, clean-pass sampling percentage, OCR-confidence floor, required tie-outs.
Every result stores `policy_id`, `policy_version`, `policy_hash` and the thin-headroom value applied, and the result
validator classifies using that stored value. Changing policy creates a new version and hash; historical results are never
re-interpreted.

### Amendments, restatements, revocations

| Event | What happens | What does not happen |
|---|---|---|
| **Contract amendment** | A new `CovenantSpec` version with `effective_from` and `executed_on`. The registry picks, for each test date, the version in force. Earlier versions are preserved. | Earlier periods are not re-tested. |
| **Retroactive amendment** | Allowed only if the spec cites the clause (`retroactive_source`). Results for the affected periods become stale (a ledger event) and are recomputed as new results that supersede the old. | The old results are not edited. |
| **Financial restatement** | The restated facts, and every result that used them, are marked stale. Recompute with the corrected facts. The spec is unchanged. | The contract version does not change. |
| **Revoked spec** | Results computed under it become stale. Periods it governed are **blocked** (`SpecRevokedError`) until a corrected approved spec is registered. | The engine does not fall back to the previous version, which would silently apply the wrong terms. |

## 3. Repository structure

```
covenant_audit/
  base.py          Money (Decimal only), FrozenModel, canonical JSON hashing
  arith.py         the one definition of every arithmetic op and the rounding rule (engine and validator share it)
  models.py        SourceSpan, Expr AST, CovenantSpec, TieOutCheck, FinancialFact, TraceStep, CovenantTestResult, classify()
  policy.py        BankPolicy (thin headroom, sampling, OCR floor, required tie-outs)
  verification.py  DocumentStore, SourceVerifier (spans, specs, facts, seals), approve_spec, untrusted-input parsers
  engine.py        Evaluator: recursive AST evaluation with trace; cycle/unresolved/zero-division/missing-input guards
  pipeline.py      run_covenant_test, run_period, recompute_stale, replay_result
  registry.py      SpecRegistry: effective-dated versions, revocation
  ledger.py        Ledger: append-only hash chain, invalidation events, reviews, checkpoints
  sample.py        synthetic credit agreement, amendment, borrower statements, and a `World` test harness
  demo.py          `python -m covenant_audit.demo`
tests/             143 tests (see section 5)
tools/mutation_check.py   breaks 12 guarantees one at a time and checks the tests notice
docs/              the four-page brief (Markdown, LaTeX, PDF) and build scripts
```

## 4. Installation

Requires Python 3.10+ (developed on 3.12). The only runtime dependency is `pydantic>=2.7`; `pytest` is for tests.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 5. Running the tests

```bash
python3 -m pytest
```

Actual output from the last run:

```text
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.2.0, pluggy-1.7.0
rootdir: /workspace
configfile: pyproject.toml
testpaths: tests
collected 143 items

tests/test_engine.py ........................                            [ 16%]
tests/test_models.py .............................................       [ 48%]
tests/test_pipeline.py .............                                     [ 57%]
tests/test_scenarios.py ...............                                  [ 67%]
tests/test_verification.py ................................              [ 90%]
tests/test_versioning_ledger.py ..............                           [100%]

============================= 143 passed in 0.33s ==============================
```

| File | Tests | Covers |
|---|---|---|
| `test_models.py` | 45 | spec validators, tie-outs, money type, `classify()`, result validator, immutability |
| `test_verification.py` | 32 | span grounding, seals, forged/edited specs and facts, untrusted input |
| `test_engine.py` | 24 | every AST op, Decimal exactness, guards, trace re-checking |
| `test_pipeline.py` | 13 | input admission, review routing, replay, policy history |
| `test_scenarios.py` | 15 | the 12 required end-to-end scenarios (scenario 11 has 4 rounding cases) |
| `test_versioning_ledger.py` | 14 | version chain, retroactivity, revocation, hash chain, checkpoints |

Each test's docstring states the expected result and why. The code is also type-checked (`pip install mypy && python3 -m mypy covenant_audit --ignore-missing-imports`: no issues in 12 source files).

To check that the tests have teeth, `python3 tools/mutation_check.py` temporarily breaks 12
individual guarantees (hardcoded thin threshold, MIN cap turned into MAX, ledger payload check skipped, effective dates
ignored, unsealed facts trusted, spec seal unchecked, rounding ignored, negative denominators allowed, stale facts accepted,
review not required for breaches, fact amount not matched to its quote, tie-out sums not checked) and restores each file. In the last run every mutation was KILLED (made at least one test fail).

## 6. Running a sample covenant calculation

```bash
python3 -m covenant_audit.demo
```

```text
SYNTHETIC DATA ONLY. Borrower inputs (millions): base_ebitda=100, addbacks=40, total_debt=540, cash=40
Spec SPEC-1 v1 [approved] hash 290d4be7e0cd61a7  covenant: ratio <= 4.50 on 2025-06-30

Calculation trace (every line is one AST node; values are exact Decimals):
   1  term:net_debt/diff/left                    input                                          = 540,000,000   <- F-2025-06-30-total_debt  "Total debt: 540,000,000" (p.1, chars 154-177)
   2  term:netted_cash/min/0                     input                                          = 40,000,000   <- F-2025-06-30-cash  "Unrestricted cash: 40,000,000" (p.1, chars 178-207)
   3  term:netted_cash/min/1                     lit                                            = 50,000,000   <- contract "$50,000,000" (p.1, CA-1)
   4  term:netted_cash/min                       min(40,000,000 , 50,000,000)                   = 40,000,000
   5  term:netted_cash                           term(40,000,000)                               = 40,000,000   <- defined by ""Consolidated Net Debt" means Consolidated Total Debt minus ..."
   6  term:net_debt/diff                         diff(540,000,000 , 40,000,000)                 = 500,000,000
   7  term:net_debt                              term(500,000,000)                              = 500,000,000   <- defined by ""Consolidated Net Debt" means Consolidated Total Debt minus ..."
   8  term:consolidated_ebitda/sum/0             input                                          = 100,000,000   <- F-2025-06-30-base_ebitda  "LTM Consolidated EBITDA before add-backs: 100,000,000" (p.1, chars 74-127)
   9  term:accepted_addbacks/min/0               input                                          = 40,000,000   <- F-2025-06-30-addbacks  "LTM add-backs: 40,000,000" (p.1, chars 128-153)
  10  term:accepted_addbacks/min/1/mul/left      lit                                            = 0.25   <- contract "twenty-five percent (25%)" (p.1, CA-1)
  11  term:accepted_addbacks/min/1/mul/right     input                                          = 100,000,000   <- F-2025-06-30-base_ebitda  "LTM Consolidated EBITDA before add-backs: 100,000,000" (p.1, chars 74-127)
  12  term:accepted_addbacks/min/1/mul           mul(0.25 , 100,000,000)                        = 25,000,000.00
  13  term:accepted_addbacks/min                 min(40,000,000 , 25,000,000.00)                = 25,000,000.00
  14  term:accepted_addbacks                     term(25,000,000.00)                            = 25,000,000.00   <- defined by ""Consolidated EBITDA" means Consolidated Net Income plus add..."
  15  term:consolidated_ebitda/sum               sum(100,000,000 , 25,000,000.00)               = 125,000,000.00
  16  term:consolidated_ebitda                   term(125,000,000.00)                           = 125,000,000.00   <- defined by ""Consolidated EBITDA" means Consolidated Net Income plus add..."
  17  ratio                                      div(500,000,000 , 125,000,000.00)              = 4
  18  ratio.rounded                              round(4)                                       = 4.00

Result R-dc714082ac0a1f7c22f0185d: PASS  ratio 4 -> tested 4.00x vs 4.50x  headroom 0.50 (11.1111111111%)
  spec SPEC-1 v1 290d4be7e0cd61a7, policy BANK-POLICY v1 3bb7e51d4a6d9aea (thin-headroom 0.10), engine 0.2.0, needs_review=False
Replay from sealed inputs reproduced the result exactly; ledger hash chain verified (1 result, head 8358dfda6ec4e777).
```

## 7. A complete numerical example

Synthetic borrower, test date 30 June 2025, covenant "Total Net Leverage Ratio must not exceed 4.50x":

| Input | Value |
|---|---|
| Total debt | 540 |
| Unrestricted cash | 40 (net-able up to the 50 cap) |
| EBITDA before add-backs | 100 |
| Add-backs | 40 (capped at 25% of EBITDA before add-backs) |

```
Net debt          = 540 - MIN(40, 50)            = 500
Add-back cap      = 25% x 100                    = 25
Accepted add-backs = MIN(40, 25)                  = 25
Capped EBITDA     = 100 + 25                     = 125
Ratio             = 500 / 125                    = 4 -> 4.00x   (rounded to 2 places, half up, per the contract)
Headroom          = (4.50 - 4.00) / 4.50         = 11.11%  -> PASS (policy: thin if < 10%)
```

Without the cap the ratio would be 500 / 140 = 3.57x, an overstated pass. Every line above is a node in the trace printed by the
demo; none is hardcoded.

Rounding matters: with EBITDA 200 and net debt 901 the exact ratio is 4.505. The contract says "two decimal places, rounding
half up", so the tested ratio is 4.51 and the covenant is breached; under `ROUND_HALF_EVEN` it would be 4.50 and pass at the
limit. Classification uses the rounded ratio (`test_scenarios.py::test_11`).

## 8. The AST

A spec's formulas are data: a tree of nine node types (`Lit`, `Input`, `TermRef`, `Sum`, `Diff`, `Mul`, `Div`, `MinOf`,
`MaxOf`). Net debt is `Diff(Input total_debt, TermRef netted_cash)` where `netted_cash` is `MinOf(Input cash, Lit 50,000,000)`.
There is no `eval` and no model-written Python; an LLM can only fill this schema.

- `Evaluator` walks the tree recursively. Each node appends a `TraceStep` (node id, operation, operand values, value, fact id,
  contract clause spans, borrower-document evidence span). A term used twice is evaluated once.
- It defends itself even if model validation was bypassed: cycles (`CyclicTermError`), unresolved terms, unsupported node types,
  missing inputs, division by zero, and a non-positive denominator (negative EBITDA would make a leverage ratio negative and
  wrongly satisfy a maximum covenant, so the pipeline returns INDETERMINATE for a human).
- `arith.apply_op` is the *only* implementation of the operations. `CovenantTestResult` re-runs it on every stored trace step, so a
  tampered trace fails validation.

## 9. Source verification

- `SourceSpan` is an *extracted* quotation: document id, document SHA-256, page, character offsets and the quoted text. It has
  no verification state.
- `SourceVerifier.verify_span` checks that the document exists, its hash matches, the page and offsets are valid, the quote equals
  the stored text exactly, and the text origin (native vs OCR) matches the stored page. OCR spans must carry a confidence;
  confidence below the bank's floor routes the result to human review.
- `DocumentStore.locate` builds a span from a page number and a quotation, so code, not the LLM, counts characters.
- `verify_spec` grounds *every* quotation in a spec and seals it. `verify_fact` additionally checks that the fact's amount is
  printed in its quote at the stated scale (parentheses are negative), that every tie-out passes, and that every tie-out the
  policy requires is present **and grounded**: each tie-out component must be quoted from the document and `expected` must equal
  their sum, so an extractor cannot invent components that add up to a convenient total.
- Extractor output is untrusted: `parse_untrusted_facts` rejects unknown keys (there is no field for tool calls), floats,
  supplied tie-out verdicts and supplied seals.

## 10. Limitations and known failure cases

Not implemented:

- **No LLM extraction.** There is no prompt, no model call and no extraction accuracy measurement. Specs and facts in the tests
  are built by code.
- **No compliance-certificate back-test**, no two-extraction diff, no AST-to-plain-English rendering for the reviewer, no
  hidden-text (text layer vs rendered page) comparison, no amendment register. The written brief mentions some of these as design
  mitigations; they are designs, not code.
- **No document ingestion.** `DocumentStore` takes page text. It hashes canonical text, not the original PDF bytes, and there is
  no OCR.
- **Approval is a single signature.** The second signer above an exposure limit, reviewer authorisation and role checks are not
  implemented.
- **The ledger is in memory.** The hash chain is real and tamper-evident, but there is no persistence, and checkpoints are not
  signed or anchored externally.
- **Seals use an in-process key** (see section 2).
- **Scope of the calculation.** One ratio per spec; no FX, pro-forma acquisitions, equity cure, or contractual treatment of
  negative EBITDA (these yield INDETERMINATE or are out of scope).

Known failure cases the design cannot fully remove:

- **Grounding is not understanding.** A quote can exist yet be misread (a wrong cap, the pre-amendment definition). Verification
  proves the quote is real and the spec is internally consistent, not that it *means* what the contract means. Human approval is
  the control; spec literals and thresholds are not cross-checked against their quotes because wording such as "twenty-five percent" is
  not machine-parseable here (fact amounts are).
- **Coordinated fabrication.** A borrower who falsifies a statement and its components consistently passes every tie-out.
  Borrower fraud is out of scope.
- **Missing amendments.** The system cannot know about an amendment it was never given.
- **Duplicate/unsupported period shapes.** LTM inputs are accepted if they cover 365-366 days; other fiscal-year shapes are
  rejected as INDETERMINATE rather than adjusted.
