# Auditable Covenant Compliance

> **The LLM interprets the contract. A trusted verifier checks its evidence. A human approves the specification. A deterministic engine is the only thing that computes the ratio and classifies the covenant.**

A take-home prototype for commercial-loan financial-covenant monitoring. It now covers the whole path on a **real public SEC credit agreement**: ingestion, deterministic clause selection, LLM extraction into a typed `CovenantSpec`, source verification, a signed human approval, effective-dated registration, and a deterministic, traced calculation recorded in a hash-chained ledger.

Borrower financial data is **synthetic**. The credit agreement is real and public.

## Read this first: what ran, and what did not

| | Status |
|---|---|
| Deterministic core (models, AST evaluator, pipeline, registry, ledger) | **Implemented and tested** |
| SEC document ingestion, canonicalisation, hash-pinned sources | **Implemented**, reproducible from EDGAR (`tools/build_sources.py --check`) |
| Deterministic clause selection, extraction parsing, quote resolution, semantic checks, reviewer view, approval attestation | **Implemented and tested** (offline) |
| OpenRouter provider (timeouts, 429 retry, truncation and empty-answer handling, no tools offered) | **Implemented; tested only with a fake transport.** It has **not** been run against the live OpenRouter API in this repository: no API key was available |
| A genuine LLM extraction of the Viad covenant | **Not produced.** There is no live-model output in this repo |
| `extract --provider recorded` | **Simulated stand-in.** It replays `data/extraction/viad_reference_response.json`, a reference **hand-authored by the coding agent** (script: `tools/author_reference_extraction.py`) from the real text. It is labelled `NOT A LIVE MODEL CALL` everywhere it appears |
| Human approval in the transcript below | **Simulated**: entered on the command line as `demo.reviewer@example.com` to show the mechanism. It is not a real credit officer's review |
| Approval signing and key custody | **Development only** (in-process HMAC, local key file). Not identity authentication, not production signing |

To get a real extraction: set `OPENROUTER_API_KEY` and `OPENROUTER_MODEL` and run `extract --provider openrouter` (section 6). Compare the result with the reference; expect a real model to differ, and let the verifier and the reviewer judge it.

## 1. Architecture

```
public SEC HTML --ingest--> canonical page text + SHA-256   (ingest.py, data/sources/)
      |
      +--> deterministic clause selection: covenant pages, defined-term closure, rounding clause   (clauses.py)
      |
      +--> PROMPT (rules + page-labelled excerpts, no tools) --> LLM provider --> UNTRUSTED TEXT     (llm.py, extraction.py)
                                                                      |
   forbidden-field scan -> quote resolution by software (page+quote -> offsets+hash) -> CovenantSpec validators
                                                                      |   issues / unresolved items block approval
                                                                 DRAFT spec
      SourceVerifier.verify_span / verify_spec (independent re-check, seal)  -> VERIFIED spec     (verification.py)
      semantic checks (threshold in its quote, constant in its quote, direction, unused inputs)   (spec_checks.py)
      reviewer view generated from the AST by code (no model)                                     (explain.py, review.py)
                                                                      |
                              HUMAN: type the spec hash to approve  -->  signed ApprovalAttestation   (approval.py)
                                                                      |
                           SpecRegistry.register (re-verifies seal AND attestation)  -> approved, effective-dated spec
                                                                      |
   synthetic sealed facts -> Evaluator (Decimal AST, trace) -> rounding -> classify() -> result -> hash-chained Ledger
```

The model's only influence on a number is the *text of the spec* a human approved. It never sees borrower data, computes nothing, and has no tool to call.

Core guarantees and where they are enforced:

| Guarantee | Enforced in | Tested in |
|---|---|---|
| No float enters a financial value; no `eval` or generated code | `base.Money`, `arith`, closed AST, source scan test | `test_models`, `test_engine` |
| An extracted quote is not a verified quote; the model cannot supply `verified`, a seal, an approval, offsets we trust or a status | `SourceSpan` has no verified flag; `FORBIDDEN_KEYS`; offsets computed by software | `test_verification`, `extraction/test_extraction_scenarios` |
| The engine runs only sealed, approved specs whose **approval attestation verifies**, and sealed facts | `check_spec`, `check_fact`, `verify_approval` | `test_approval`, `test_pipeline` |
| Status, headroom, rounding and every trace step are re-derived | `CovenantTestResult` validators, one `classify()` | `test_models`, `test_engine` |
| Policy is separate from contract terms and pinned per result | `BankPolicy`, `policy_hash` | `test_pipeline` |
| Amendments, restatements, revocations are different events | `SpecRegistry` vs `Ledger` events | `test_versioning_ledger` |
| Unresolved items, failed verification, unread amendments and semantic errors block approval | `ReviewPackage.blocking`, `approve()` | `extraction/test_extraction_scenarios` |
| History cannot be silently edited | frozen models, tuples, hash-chained ledger | `test_versioning_ledger` |

## 2. Implementation status (detail)

**Implemented features**: everything in the "Implemented" rows above, plus the `IfGt` conditional AST node (needed for the real clause "if revolver borrowings exceed $10,000,000, netted cash is capped at $50,000,000"), `scope_notes` on specs (what is deliberately not modelled), `rounding_source`, signed approval records in the ledger (rejections are signed and saved by the CLI, and the ledger can store them), and the CLI.

**Simulated prototype components**: the recorded reference extraction; the command-line approval used in the demo transcript; the development signer and verifier keys (random bytes in `.covenant_state/dev_keys.json`); synthetic borrower statements; the in-memory ledger.

**Production requirements (not implemented)**: KMS/HSM-backed asymmetric signing with approver identity from SSO/OIDC claims, N-of-M approval above an exposure limit, persistent write-once ledger storage with signed checkpoints, secrets management, a self-hosted or zero-retention model inside the lender's boundary, OCR ingestion, and a reviewer UI.

**Unimplemented features**: a live-API run, extraction accuracy measurement across agreements, the compliance-certificate back-test, a second independent extraction and diff, hidden-text (text layer vs rendered page) detection, Acquisition Period and equity-cure modelling, pro forma adjustments, multi-covenant specs.

## 3. Public SEC agreement used

| | |
|---|---|
| Company | Viad Corp (now Pursuit Attractions and Hospitality, Inc.), CIK 0000884219 |
| Agreement | Credit Agreement dated as of July 30, 2021 among Viad Corp, the lenders party thereto and Bank of America, N.A., as Administrative Agent |
| Filing | Exhibit 10.1 to Form 8-K filed 2021-08-02, accession **0001564590-21-039835** |
| URL | https://www.sec.gov/Archives/edgar/data/884219/000156459021039835/vvi-ex101_28.htm |
| Amendment | First Amendment dated as of March 23, 2022 (amends and restates Sections 7.10(a)-(c)), Exhibit 10.1 to Form 8-K filed 2022-03-24, accession **0000950170-22-004478** |
| Amendment URL | https://www.sec.gov/Archives/edgar/data/884219/000095017022004478/vvi-ex10_1.htm |

Why this one: a real Total Net Leverage covenant, a Consolidated EBITDA definition with a genuine sub-cap, a conditional cap on netted cash, an explicit rounding rule, a step-down schedule, and a real amendment that *changed the covenant levels*. That last point is a live example of the "missing amendment" risk: the base agreement alone gives the wrong answer for any date after March 2022.

Clauses used (page numbers are the filing's rendering page breaks, not printed page numbers):

| Element | Where |
|---|---|
| Covenant levels (5.25x, 4.75x, 4.50x, then 4.00x, by test date) | First Amendment, Section 1, restated Section 7.10(c), pp.2-3 |
| `Total Net Leverage Ratio` = Consolidated Net Funded Indebtedness / Consolidated EBITDA for the Test Period | Credit Agreement Section 1.01, p.61 |
| `Consolidated EBITDA`, clauses (a)-(g); clause (d) run-rate savings capped at **20.0%** of EBITDA calculated before clause (d) | Section 1.01, pp.17-18 |
| `Consolidated Net Funded Indebtedness`, clauses (a)-(c), New Material Project exclusion capped at **$50,000,000**, minus Unrestricted Cash | Section 1.01, pp.18-19 |
| `Unrestricted Cash`: capped at **$50,000,000** only if revolver and swing line borrowings exceed **$10,000,000** | Section 1.01, p.62 |
| Rounding: carry the ratio one place beyond how it is expressed, round to nearest, ties up | Section 1.04, p.64 |
| Original Section 7.10 (superseded for covenant levels by the amendment) | p.146 |

The sources are committed under `data/sources/` as canonical page text with SHA-256 manifests, and can be re-derived from EDGAR to prove they match:

```bash
SEC_USER_AGENT="Your Name you@example.com" python3 tools/build_sources.py --check
```

## 4. Installation

Python 3.10+ (developed on 3.12). One runtime dependency (`pydantic>=2.7`); `pytest` for tests; network access only for the optional SEC re-download and for live extraction. No database and no framework.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 5. API configuration (live extraction only)

```bash
export OPENROUTER_API_KEY=<your key>      # never commit this
export OPENROUTER_MODEL=<model id>        # any model id from https://openrouter.ai/models; free-tier models are rate-limited
```

No key is stored anywhere in the repository, and the test suite never reads one or calls a model. Without configuration the extraction command stops and says so (exit code 2); it never silently substitutes anything.

Do not send confidential data. The documents here are public, which is why a hosted model is acceptable for the prototype; real borrower material would need a model inside the lender's boundary.

## 6. Extraction command

```bash
python3 -m covenant_audit.cli sources                              # show the SEC references and hash checks
python3 -m covenant_audit.cli extract --provider openrouter        # LIVE model call (needs the variables above)
python3 -m covenant_audit.cli extract --provider recorded          # replay the reference response: NOT a model call
```

What `extract` does: selects excerpts deterministically (about 37,000 characters: the covenant pages, the definitions the ratio depends on, the rounding clause), sends them with the rules, parses the response into a DRAFT spec, and saves it to `.covenant_state/pending_extraction.json`. A malformed, truncated, hostile or invented answer becomes a recorded issue, not an exception and not a spec. Retries (default 2 attempts) feed back only validation messages, never the previous raw output, and a response containing forbidden fields is never retried.

## 7. Approval workflow

```bash
python3 -m covenant_audit.cli score                                # check the extraction against the contract's key facts (exit 1 if any fail)
python3 -m covenant_audit.cli review                               # what the human sees (exit 3 if approval is blocked)
python3 -m covenant_audit.cli approve --approver you@bank          # prompts you to type the spec hash; or pass --confirm <first 12 hash chars>
python3 -m covenant_audit.cli reject  --approver you@bank --reason "..."
python3 -m covenant_audit.cli compute                              # register + run the engine on synthetic financials
python3 -m covenant_audit.cli demo --provider recorded --approver demo.reviewer@example.com --confirm 921471d92c72
```

The reviewer sees: provenance (live or not), documents read, every blocking reason, the formula for the ratio and every defined term **generated from the AST by deterministic code (no model)**, every input, every constant with its quotation, document, page and character offsets, the threshold schedule with its sources, the rounding rule and its source, the scope notes, the model's unresolved items, and the list of terms the extractor was not shown.

Approval is **refused** (not warned about) if: the response produced no spec or an invalid one; any quotation fails independent re-verification; the model reported any unresolved item (ambiguity, cross-reference, missing definition, missing amendment); a known amendment document was not among those read; or a semantic check fails (a threshold or constant not present in its own quotation, a maximum tested as a minimum, an unused or invented input, a constant marked "structural" to avoid citing it, a rounding rule without a cited clause).

Approval is an explicit act: type the first 12 characters of the content hash of the spec you were shown. The trusted signer then issues an **`ApprovalAttestation`** binding: approval id, spec content hash, approver id, approver role, decision, timestamp and the reviewer's edit list. Changing any of those, or the spec, invalidates the signature; the registry and the engine re-verify it, only `decision == "approved"` counts, an approval signed for different spec content is refused, and the extraction run and automated identities cannot approve. Approvals are appended to the ledger when a spec is registered, so previous versions keep their approval records. The CLI saves a rejection as a signed record on disk; the ledger API (`append_rejection`) can store it in the chain, but the CLI does not do so yet.

**What the dev signer is not.** `LocalDevApprovalSigner` is an HMAC with a local key. It shows the record was not altered by someone without the key. It does **not** authenticate the approver: the approver id is whatever string the caller passes. `KmsApprovalSigner` documents the production shape and refuses to run.

## 8. Sample deterministic calculation

Real covenant from the approved spec, synthetic borrower (test date 31 Dec 2022, USD millions): borrowed money debt 800, finance leases and construction debt 80, guarantees 5, new-material-project obligations 30, unrestricted cash 120 with revolver and swing line borrowings of 25, net income 70, interest 25, taxes 20, D&A 40, other enumerated add-backs 18, dividends 3 (cap 5), non-cash items 6, transaction charges 4, NCI distributions 2, run-rate cost savings 40.

```
EBITDA before clause (d)   = 70+25+20+40+18+MIN(3,5)+0+6+4+0 - 2          = 184.0
Clause (d) cap             = 20.0% x 184.0                                = 36.8   -> MIN(40, 36.8) = 36.8
Consolidated EBITDA        = 184.0 + 36.8                                 = 220.8
Netted cash                = revolver 25 > 10, so MIN(120, 50)            = 50
Net funded indebtedness    = (800+80+5) - MIN(30,50) - 0 - 50             = 805
Ratio                      = 805 / 220.8 = 3.64583...  -> 3.646 (Section 1.04: three places, ties up)
Covenant level on 31 Dec 2022 = 4.75x  -> headroom 1.104 (23.24%) -> PASS
```

Both real sub-caps bite (40 becomes 36.8; 120 becomes 50). The same financials on 30 Jun 2023 face the 4.00x step-down and come out `PASS_THIN_HEADROOM` (8.85% headroom, below the 10% policy), which needs review (`tests/extraction/test_extraction_scenarios.py::test_16b`). The full 47-step trace, with each input tied to its statement line and each constant tied to a quotation in the filing, is in [`docs/demo_transcript.txt`](docs/demo_transcript.txt). Its final part:

```text
RESULT PASS: ratio 3.6458333333333333333333333333333333333333333333333 -> tested 3.646x vs maximum 4.75x, headroom 1.104 (23.24%)
  spec VIAD-CA-2021-AMD1-max_total_net_leverage-725a8385 921471d92c728287 | policy DEMO-POLICY v1 d55f07e2190d9b7c | engine 0.2.0 | needs_review=False
Replay reproduced the result exactly. Ledger hash chain verified: 4 entries, head a7ee32439a522f3c.
```

The spec produced by the extraction step, as the reviewer sees it (excerpt):

```text
RATIO = <consolidated_net_funded_indebtedness>  /  <consolidated_ebitda>
<accepted_cost_savings> = MIN(run_rate_cost_savings, (0.20 x <ebitda_before_clause_d>))
      source: VIAD-CA-2021 Section 1.01, p.18, chars 917-1042: "shall not (i) exceed 20.0% of Consolidated EBITDA for such Test Period (calculated prior to giving effect to this clause (d))"
<netted_unrestricted_cash> = IF revolving_and_swing_line_outstanding > 10,000,000 THEN MIN(unrestricted_cash, 50,000,000) ELSE unrestricted_cash
      source: VIAD-CA-2021 Section 1.01, p.62, chars 840-952: "the aggregate Outstanding Amount of all Revolving Loans and Swing Line Loans as of such date exceeds $10,000,000"
THRESHOLDS  test dates 2022-09-30..2022-12-30: 5.25x | 2022-12-31..2023-03-30: 4.75x | 2023-03-31..2023-06-29: 4.50x | 2023-06-30..: 4.00x
ROUNDING    3 decimal places, ROUND_HALF_UP   (Section 1.04, p.64)
```

## 9. Running the tests

```bash
python3 -m pytest
```

Actual output from the last run (no network and no API key used):

```text
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.2.0, pluggy-1.7.0
rootdir: /workspace
configfile: pyproject.toml
testpaths: tests
collected 246 items

tests/extraction/test_extraction_scenarios.py .......................... [ 10%]
.....................                                                    [ 19%]
tests/extraction/test_ingest_clauses_providers.py ...................... [ 28%]
...                                                                      [ 29%]
tests/extraction/test_review_cli.py .......                              [ 32%]
tests/extraction/test_scoring.py .....                                   [ 34%]
tests/test_approval.py ................                                  [ 40%]
tests/test_engine.py ...........................                         [ 51%]
tests/test_models.py .............................................       [ 69%]
tests/test_pipeline.py .............                                     [ 75%]
tests/test_scenarios.py ...............                                  [ 81%]
tests/test_verification.py ................................              [ 94%]
tests/test_versioning_ledger.py ..............                           [100%]

============================= 246 passed in 1.65s ==============================
```

| Group | Tests |
|---|---|
| Original deterministic core (`test_models`, `test_verification`, `test_engine`, `test_pipeline`, `test_scenarios`, `test_versioning_ledger`) | 146 (the original 143, plus 3 for the new `IfGt` node) |
| Approval attestation (`test_approval`) | 16 |
| Extraction workflow on the real agreement (`tests/extraction/`, kept separate; includes the scorecard) | 84 |

Original-suite note: one original assertion was deliberately *narrowed*, not removed. `test_scenarios.py::test_12` used to forbid every process/network import anywhere in the package; the new document-download and model-call modules legitimately need `urllib`. It now enforces that the deterministic core imports no network or process module, that only `ingest.py` and `llm.py` may import `urllib` (and `os` for configuration), and that nothing imports a process-execution module.

`python3 tools/mutation_check.py` breaks 28 individual guarantees one at a time (approval signature unchecked, attestation hash unchecked, forbidden model fields accepted, claimed offsets unchecked, invented quote accepted, unresolved items not blocking, known amendments ignored, quotes not re-verified at review, confirmation not checked, and the earlier arithmetic/ledger/registry ones) and confirms the suite fails for each. In the last run every mutation was KILLED. Type check: `python3 -m mypy covenant_audit --ignore-missing-imports` reports no issues in 25 files.

## 10. Security and auditability

Documents are treated as untrusted input. The model is given text and returns text; it is offered no tools, functions or code execution, and the response schema has no field in which an injected instruction could request one.

| Risk | Implemented protection | Future production requirement |
|---|---|---|
| Prompt injection in a filing | Rules tell the model the excerpts are data; response is schema-checked; keys such as `tool_calls`, `code`, `eval`, `approval`, `verified`, `status`, `seal` anywhere in the response reject the whole answer; nothing the model returns is ever executed; the engine consumes only the sealed, approved, structured spec, never model text | Run extraction in a sandbox with no network egress other than the provider; red-team the prompt |
| Hidden PDF text, look-alike characters | Canonicalisation of typography; quotes must match the stored text exactly | Compare text layer with the rendered page; reject documents with invisible text |
| Malicious or misleading amendment | Known amendments listed per document; approval blocked if a known amendment was not read; the First Amendment is a real example | A maintained amendment register confirmed by the agent bank; signature/provenance checks on amendment documents |
| Fabricated quotation | Quote must occur exactly once on the cited page; offsets/hash computed by software, model-claimed ones must be exact; `verify_spec` re-checks independently | none beyond this |
| Incorrect but syntactically valid extraction | Semantic checks (threshold/constant in its own quote, direction, unused inputs, structural abuse, rounding source); the reviewer sees the formula in plain form; deliberately wrong extractions are refused in tests | Compliance-certificate back-test; two independent extractions diffed; reviewer training; sampling of approved specs |
| Unauthorized approval | Attestation bound to spec hash, approver, role, decision and time; extractor and automated identities refused; registry and engine re-verify; hash typed by the reviewer | KMS/HSM signing, SSO-authenticated approver identity, N-of-M approval above an exposure limit |
| Model hallucination | Nothing is accepted without a verbatim, unique, hashed quotation; unresolved items block; the model never computes | Accuracy measurement across a corpus of agreements |
| Missing documents | The extractor is told which referenced terms it was not shown; the engine refuses dates no spec governs | Document-completeness checks against the filing index |
| Provider outage, rate limits, truncation | Typed errors, bounded backoff honouring `Retry-After`, truncated or empty completions rejected, outage recorded as an issue | Fallback providers, alerting |

**Source verification establishes provenance, not semantic correctness.** A quotation can be real and the reading wrong. A consistent but wrong reading, for example numerator and denominator swapped, passes every machine check by design; `test_known_limit_a_consistent_but_wrong_reading_passes_every_machine_check` documents this. Human approval remains necessary and is the control for it.

## 11. Known limitations

- **No live-model run exists in this repository.** The OpenRouter provider is tested against a fake transport only; real models may fail to follow the schema, quote exactly, or may differ from the reference. Extraction accuracy is unmeasured.
- The reference extraction was written by the coding agent that built this repo, which reads the same text a model would; it is a plausible target, not independent evidence of model performance.
- The spec models the *standard* maximum level only. The **Acquisition Period** alternative levels, the **equity cure**, **pro forma** adjustments, Unrestricted Subsidiary exclusion, and the conditions attached to the run-rate savings add-back are recorded in `scope_notes` as not modelled. Several EBITDA add-backs are grouped into one input (documented in the scope notes).
- The Amendment's "Effective Date" is defined by conditions precedent, not a calendar date; the spec applies from the first scheduled test date (30 Sep 2022).
- Only native-text pages are supported for extraction; there is no OCR ingestion. Page numbers are rendering page breaks.
- Semantic checks catch mechanical mistakes only (see section 10). Approval is a single signature; the dev signer proves integrity, not identity.
- The ledger is in memory with unsigned checkpoints; the CLI state directory holds local dev keys.
- Synthetic borrower data has no tie-out components in the demo policy (tie-out machinery is tested separately).
- Everything from the earlier phase still applies: single ratio per spec, no FX, negative EBITDA yields INDETERMINATE, and borrower fraud is out of scope.

## Submission documents

`docs/brief_final.pdf` is the four-page brief to submit (source `docs/brief.tex`; rebuild with `cd docs && pdflatex -jobname=brief_final brief.tex`, twice). `docs/SUBMISSION_CHECKLIST.md` lists what remains for you to do (live run, public repo, LLM logs, verifying the appendix). `docs/WALKTHROUGH.md` is a rehearsal guide for defending the project live. `python3 -m covenant_audit.cli score` checks any extraction against the contract's key facts so a live-model run can be reported honestly; its checklist was written by the same coding assistant as the reference extraction, so a perfect score on the reference proves nothing by itself (the tests show it fails on wrong extractions).

## Repository structure

```
covenant_audit/
  base.py arith.py models.py policy.py          deterministic core: types, Decimal arithmetic, AST, policy
  verification.py approval.py errors.py         source verification, seals, approval attestation, signers
  engine.py pipeline.py registry.py ledger.py   evaluator, computation pipeline, versioning, hash-chained ledger
  ingest.py clauses.py                          SEC ingestion/canonicalisation; deterministic clause selection
  llm.py extraction.py                          provider interface (OpenRouter, recorded, scripted); extraction workflow
  spec_checks.py explain.py review.py scoring.py   semantic checks; AST-to-text; reviewer view and approval; contract-facts scorecard
  synthetic.py sample.py viad_demo.py cli.py demo.py
data/sources/       committed canonical SEC pages + manifests (hash-pinned)
data/extraction/    the reference response (hand-authored, labelled)
data/demo/          synthetic borrower financials
tools/              build_sources.py (SEC re-derivation), author_reference_extraction.py, mutation_check.py
tests/              original core tests + test_approval.py;  tests/extraction/ for the extraction workflow
docs/               four-page brief (brief.tex -> brief_final.pdf), demo_transcript.txt, WALKTHROUGH.md, SUBMISSION_CHECKLIST.md
```
