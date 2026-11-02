# Submission checklist

The assignment (Section 3) asks for three things. This maps each to what exists and what only you can do. I can't see the company's marking scheme beyond the PDF, so this follows the PDF's own questions.

## 1. Written brief (4 pages max)

| Item | Status |
|---|---|
| Brief, 4 pages including the 1-page appendix | **Done.** Submit `docs/brief_final.pdf` (pdfLaTeX, Letter, no header or footer). Source: `docs/brief.tex`. |
| Area explored, what surprised you | Done (real-agreement findings: 20.0% cap, conditional cash cap, rounding clause, restating amendment) |
| Problem statement: who, task, how often, cost | Done. Cost figures are labelled as estimates; validate them with a practitioner if you can. |
| Architecture diagram with human and AI roles; alternatives rejected | Done |
| Risks: wrong answers, outdated rules, confidential data, liability | Done |
| Things to consider before spending more time | Done |
| Appendix: where AI helped, what you rejected, what came from you, one thing you can defend | Done, but **you must verify every first-person claim is true of you** (see below) |

Rebuild with `cd docs && pdflatex -jobname=brief_final brief.tex` (run it twice) and check it is still 4 pages. A name can be added under the title by setting `\authorname` near the top of `brief.tex`; the assignment does not ask for one, and the form identifies you.

## 2. Prototype (optional but strong)

| Item | Status |
|---|---|
| Repo with README, how to run, examples tested | Done (`README.md`, 246 tests, `python3 -m covenant_audit.cli demo ...`) |
| **Link must be publicly accessible** | **Done.** The public repository is [Siddharth-6/covenant-audit](https://github.com/Siddharth-6/covenant-audit). GitHub Actions has completed the test, mutation and type-check workflow successfully. As a final personal check, open the repo in a private browser window and try the README install/test commands from a fresh clone. |
| Say which examples are made up | Done: borrower data is synthetic; the agreement is real and public. |
| Note which model was used | **Open.** No live model was run. See step A below. |

## 3. LLM logs

**Only you can do this.** Export your chat conversations with the assistant (all of them: the covenant design, the hardening phase, the extraction phase) and zip them for the form. I cannot produce your logs. Do not edit them.

## What only you can do, in priority order

**A. Optional: run a live extraction if you have API access and time.** This is not required to make the recorded prototype reproducible. No live model call has been made so far; do not claim extraction accuracy unless you actually run and evaluate a model.

```bash
export OPENROUTER_API_KEY=<your key>
export OPENROUTER_MODEL=<a free-tier model id>
python3 -m covenant_audit.cli extract --provider openrouter
python3 -m covenant_audit.cli score        # which key contract facts did it capture?
python3 -m covenant_audit.cli review       # what blocks approval, and why?
```

Whatever happens is a result. A model that fails on the conditional cash cap, quotes badly or flags unresolved items is useful evidence if reported truthfully. Free models are rate-limited and may not follow the schema. If you run it:
- add a few lines to brief section 3 ("Not done" becomes "Done, with these failures") and the README limitations;
- save the `score` output in `docs/`;
- try 2-3 more runs or models if you can, and report the spread.

**B. Verify the appendix.** It has been rewritten to claims the chat logs support: the prompts quoted are verbatim from your messages; the compile-once design, the choice of the Viad agreement and the conditional AST node are credited to the AI. Read it once more and change anything that is not true of you. The evaluators are testing whether you can separate your thinking from the AI's.

**C. Read the contract passages** listed at the bottom of `docs/WALKTHROUGH.md`, then rehearse the walkthrough with no tools.

**D. Re-check the numbers you state** (4.5 h per test, 400 borrowers, $90/h). They are estimates. If you can't source them, keep the "my estimates" label.

**E. Independence.** Don't compare designs or drafts with your friend. Both of you may use the same assistant; that is fine, but the submission should be your own understanding.

## What is honest to claim, and what is not

| You can say | You must not say |
|---|---|
| "I built a deterministic covenant engine with development-signed approval attestations and a hash-chained ledger, with 246 offline tests" | "An LLM extracted this covenant" (no live run exists) |
| "The extraction pipeline runs on a real SEC agreement" | "The demo approval was a credit officer's review" (it was simulated) |
| "I used a reference extraction, clearly labelled, because no API key was available" | "The scorecard validates the extraction" (it is circular for the reference) |
| "The development signer shows tamper evidence, not identity" | "Approvals are authenticated" |
