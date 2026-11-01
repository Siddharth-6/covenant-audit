# Live-defence walkthrough

Purpose: let you trace **one number from a quotation in a real filing to a covenant result**, with no AI, and answer the questions a reviewer is likely to ask. Rehearse it until you can do it from memory. Every command here runs offline.

## A. Five-minute demo script

```bash
python3 -m covenant_audit.cli sources                         # 1. the real documents: company, accession, URLs, hash check
python3 -m covenant_audit.cli --state-dir /tmp/w extract --provider recorded   # 2. extraction (says NOT A LIVE MODEL CALL)
python3 -m covenant_audit.cli --state-dir /tmp/w score        # 3. checks against the contract's key facts
python3 -m covenant_audit.cli --state-dir /tmp/w review       # 4. what the approver sees
```

Copy the `SPEC CONTENT HASH` from step 4 (first 12 characters), then:

```bash
python3 -m covenant_audit.cli --state-dir /tmp/w approve --approver you@example.com --confirm <12 chars>
python3 -m covenant_audit.cli --state-dir /tmp/w compute      # 5. registers (re-verifies the signature), runs the engine, prints the trace
```

Say, while it runs: the model is only involved in step 2, and in a live run its output would be untrusted text until steps 3-4 check it.

## B. Trace one number by hand

Pick **the 20.0% cap**. You should be able to say each of these without notes.

| # | Question | Answer you give | Where to show it |
|---|---|---|---|
| 1 | Where does 0.20 come from? | The quotation "shall not (i) exceed 20.0% of Consolidated EBITDA for such Test Period (calculated prior to giving effect to this clause (d))", page 18 of the 2021 Credit Agreement, Section 1.01 | `review` output, "CONSTANTS" and the term `accepted_cost_savings` |
| 2 | How do you know the quote is real? | Software located it in the stored page text (exactly once), computed its offsets, and the verifier re-checked it against the document's SHA-256. The model's own offsets are never trusted | `tests/extraction/test_extraction_scenarios.py::test_05` |
| 3 | Where does it sit in the formula? | `MIN(run_rate_cost_savings, 0.20 x <ebitda_before_clause_d>)`, one AST node of nine types | `review` output |
| 4 | Who approved it, and could that be faked? | A person typed the spec hash; a signer attested spec hash, approver, role, decision, time. Changing any of them breaks the signature and the registry refuses it. The signer here is a development HMAC: it proves the record was not altered, not who the approver is | `tests/test_approval.py` |
| 5 | What does the engine do with it? | 184.0 x 0.20 = 36.8, so MIN(40, 36.8) = 36.8; EBITDA = 184.0 + 36.8 = 220.8 | trace steps 40-45 in `docs/demo_transcript.txt` |
| 6 | What is the final result? | 805 / 220.8 = 3.6458..., rounded to 3.646 per Section 1.04, against 4.75x on 31 Dec 2022: PASS, 23.2% headroom | last lines of the transcript |
| 7 | Could anyone re-check it? | `replay_result` re-runs the sealed inputs through the pinned engine; the ledger hash chain detects edits | `tests/test_versioning_ledger.py` |

The arithmetic you can do on paper:

```
EBITDA before clause (d) = 70+25+20+40+18+MIN(3,5)+0+6+4+0 - 2 = 184.0
cap = 20.0% x 184.0 = 36.8      accepted = MIN(40, 36.8) = 36.8
EBITDA = 220.8
netted cash = revolver 25 > 10, so MIN(120, 50) = 50
net funded debt = (800 + 80 + 5) - MIN(30, 50) - 0 - 50 = 805
ratio = 805 / 220.8 = 3.6458... -> 3.646
```

## C. Questions you will probably get (honest answers)

1. **Why not let the LLM compute the ratio?** Non-reproducible arithmetic, no tie-out for silent errors, and lineage would be a story after the fact. The engine uses `Decimal` and a closed AST, so the same inputs always give the same trace.
2. **What stops the model inventing a clause?** A citation must be an exact, unique quotation on the cited page, located by software. A paraphrase or an invented quote is rejected, and tests show each case.
3. **A quote can be real and the reading wrong. What then?** Correct, and it is the weakest link. Machine checks catch mechanical mistakes (a constant not in its own quote, a maximum tested as a minimum). A consistent wrong reading, such as numerator and denominator swapped, passes them; a test documents this. The controls are the human reading the plain formula, the signed approval, and (designed, not built) a back-test against historical compliance certificates.
4. **Did you run a real LLM?** No. No API key was available. The OpenRouter provider is tested against a fake transport only. The demo's extraction is a reference written by the coding assistant and labelled "NOT A LIVE MODEL CALL". Say this first, before they find it.
5. **Is the reference extraction independent evidence?** No. The same assistant wrote the reference and the scorecard, so a perfect score is circular. The scorecard tests show it fails on wrong extractions, which is the only thing it proves.
6. **Is the approval secure?** The dev signer is an in-process HMAC with a local key. It is not identity authentication and not production signing. Production needs KMS/HSM signing, an approver identity from SSO, and several approvers above an exposure limit.
7. **What about the First Amendment?** The base agreement's covenant levels were restated in March 2022. The spec cites the amendment for the schedule and the base agreement for definitions. If the extractor had not been given the amendment, approval is blocked (`known amendments` check). The system cannot know about an amendment it was never given; a maintained amendment register is designed, not built.
8. **Amendment vs restatement?** An amendment is a new spec version with an effective date (retroactive only if it cites a clause saying so). A restatement changes the borrower's facts and marks dependent results stale. Both are ledger events; nothing is edited.
9. **Why an `IfGt` node?** The real "Unrestricted Cash" definition caps netted cash only if revolver borrowings exceed $10,000,000. A flat formula cannot express that. The node evaluates all branches, traces them, and the result validator re-checks the choice.
10. **Why three decimal places?** Section 1.04 says to carry the ratio one place beyond how it is expressed (the schedule uses two places) and round to nearest, ties up. This is an interpretation, recorded in the spec's scope notes.
11. **What is not modelled?** Acquisition Period levels, equity cure, pro forma adjustments, Unrestricted Subsidiary exclusion, the conditions attached to the savings add-back. Each is a scope note the reviewer sees.
12. **What would break first in production?** A wrong-but-consistent spec approved by a tired reviewer; a missing amendment; scanned (OCR) documents, which are refused for now.
13. **What did the AI do and what did you do?** The AI wrote nearly all code, tests and documents. You chose the domain, set the rules (the model never touches the ratio; no pretending a replay is a model; dev signer kept apart from production), and decided to keep this project. Do not claim more than that. If you did not personally do something, do not say you did.
14. **Why `Decimal`?** Binary floats cannot represent most decimal amounts; 0.1 + 0.2 is 0.30000000000000004. Floats are rejected at the model boundary.

## D. Read these passages yourself before the interview

The reviewer may ask you about the contract, not only the code. Read these in the filing (page numbers are rendering pages of the EDGAR HTML, not printed pages): Credit Agreement pp.17-19 (Consolidated EBITDA and Consolidated Net Funded Indebtedness), p.61 (Total Net Leverage Ratio), p.62 (Unrestricted Cash), p.64 (Section 1.04 rounding); First Amendment pp.2-3 (the restated schedule). Check that the quotations in the review output are what you expect.
