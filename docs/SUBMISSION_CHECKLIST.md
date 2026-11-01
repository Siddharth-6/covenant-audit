# Submission checklist

The assignment (Section 3) asks for three things. This maps each to what exists and what only you can do. I can't see the company's marking scheme beyond the PDF, so this follows the PDF's own questions.

## 1. Written brief (4 pages max)

| Item | Status |
|---|---|
| Brief, 4 pages including the 1-page appendix | **Done.** Submit `docs/brief_latex.pdf` (pdfLaTeX, Letter). Source: `docs/brief.tex`. A Markdown rendering is in `docs/brief.md` and `docs/brief.pdf` (A4); they carry the same content. |
| Area explored, what surprised you | Done (real-agreement findings: 20.0% cap, conditional cash cap, rounding clause, restating amendment) |
| Problem statement: who, task, how often, cost | Done. Cost figures are labelled as estimates; validate them with a practitioner if you can. |
| Architecture diagram with human and AI roles; alternatives rejected | Done |
| Risks: wrong answers, outdated rules, confidential data, liability | Done |
| Things to consider before spending more time | Done |
| Appendix: where AI helped, what you rejected, what came from you, one thing you can defend | Done, but **you must verify every first-person claim is true of you** (see below) |

Rebuild: `cd docs && pdflatex brief.tex` (twice if you edit). Check it is still 4 pages.

## 2. Prototype (optional but strong)

| Item | Status |
|---|---|
| Repo with README, how to run, examples tested | Done (`README.md`, 246 tests, `python3 -m covenant_audit.cli demo ...`) |
| **Link must be publicly accessible** | **You must do this.** Check the repository is public, then open it in a private browser window and run the README's install and test commands from a fresh clone. |
| Say which examples are made up | Done: borrower data is synthetic; the agreement is real and public. |
| Note which model was used | **Open.** No live model was run. See step A below. |

## 3. LLM logs

**Only you can do this.** Export your chat conversations with the assistant (all of them: the covenant design, the hardening phase, the extraction phase) and zip them for the form. I cannot produce your logs. Do not edit them.

## What only you can do, in priority order

**A. Run a live extraction and report it honestly (largest gap).**

```bash
export OPENROUTER_API_KEY=<your key>
export OPENROUTER_MODEL=<a free-tier model id>
python3 -m covenant_audit.cli extract --provider openrouter
python3 -m covenant_audit.cli score        # which key contract facts did it capture?
python3 -m covenant_audit.cli review       # what blocks approval, and why?
```

Whatever happens is a result. A model that fails on the conditional cash cap, quotes badly or flags unresolved items is a better story than a perfect run, as long as you report it truthfully. Free models are rate-limited and may not follow the schema. Then:
- add a few lines to brief section 3 ("Not done" becomes "Done, with these failures") and the README limitations;
- save the `score` output in `docs/`;
- try 2-3 more runs or models if you can, and report the spread.

**B. Verify the appendix.** It says "I chose...", "I required...", "I expected...". Keep only what is true of you, and rephrase the rest. The brief says the AI wrote nearly all the code; keep that. The evaluators are testing whether you can separate your thinking from the AI's.

**C. Read the contract passages** listed at the bottom of `docs/WALKTHROUGH.md`, then rehearse the walkthrough with no tools.

**D. Re-check the numbers you state** (4.5 h per test, 400 borrowers, $90/h). They are estimates. If you can't source them, keep the "my estimates" label.

**E. Independence.** Don't compare designs or drafts with your friend. Both of you may use the same assistant; that is fine, but the submission should be your own understanding.

## What is honest to claim, and what is not

| You can say | You must not say |
|---|---|
| "I built a deterministic covenant engine with signed approvals and a hash-chained ledger, with 246 offline tests" | "An LLM extracted this covenant" (no live run exists) |
| "The extraction pipeline runs on a real SEC agreement" | "The demo approval was a credit officer's review" (it was simulated) |
| "I used a reference extraction, clearly labelled, because no API key was available" | "The scorecard validates the extraction" (it is circular for the reference) |
| "The development signer shows tamper evidence, not identity" | "Approvals are authenticated" |
