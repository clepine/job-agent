# Handoff — Daily Job Agent

Context for a fresh session. Read this, then `PLAN.md` (design spec) and
`README.md` (setup and operation).

## What this is

A daily agent for Charles Lepine (NC State Computer Engineering, **graduating
December 2026**, US citizen, **clearance-eligible**). Every weekday at 6:30 AM ET,
GitHub Actions emails him 5 software + 5 hardware new-grad roles matched against
two master resumes, each with a true posting age, a one-line match rationale, an
ATS keyword diff, and an apply link.

Target metros: **RTP/Raleigh-Durham, Charlotte, Boston, NYC, Chicago** (primary),
plus Austin, Bay Area, Seattle, Phoenix, Dallas, Huntsville (secondary, higher
fit bar).

## Status as of 2026-08-18

Working and verified. **295 tests pass** (`./venv/bin/python -m pytest tests/ -q`).
One live run has completed successfully; cost $0.0757. The first *unattended*
scheduled run is 2026-08-19 06:30 ET and has never happened yet.

Ledger: ~770 postings, 40 scored, 10 shown. Live spend to date: **$0.0757** of a
$5 balance. Projected ~$1/month.

## Pipeline

```
fetch -> normalize -> dedupe -> hard-filter -> hydrate survivors -> re-filter
      -> BM25 pre-rank -> score NEW jobs once -> pick 5+5 -> email -> mark shown
```

Everything up to the pre-rank is free. Scoring is the only LLM stage in the daily
run; tailoring is a separate on-demand command.

**Sources:** ~160 curated company boards (Greenhouse, Lever, Ashby,
SmartRecruiters) + 130 live Workday boards + two `zapplyjobs` GitHub README
tables. Boards are ~88% of volume; the repos are ~12%.

## Decisions that must not be silently reverted

Each has a regression test citing the date it was made.

| Decision | Why |
|---|---|
| `email.tier1_min_fit: 50` | A Tier-1 slot is a *preference*, not a mandate. On 2026-08-18 a Waymo role scoring 30 — whose own rationale said he lacks tapeout experience — displaced a Draper role scoring 50. |
| `limits.max_posting_age_days: 7` | **Owner's explicit choice** against measured data (119 SW / 67 HW within 7 days). Do not widen without asking. Seasonal risk: measured in August at peak hiring. |
| Score once, cache forever | Cost. A posting's fit does not change. Invalidated by `resume_hash` when the master resume changes. |
| ATS keyword diff uses **no LLM** | Set arithmetic over a skills vocabulary. Cheaper *and* more reliable — it cannot hallucinate a keyword the posting never contained. |
| Tailoring is **on demand**, not 10/day | He applies to ~3 jobs/week, not 50. |
| BM25, not embeddings | Anthropic has no embeddings endpoint; embeddings would mean a second vendor and bill. |
| State committed as sorted JSON, not SQLite | Binary SQLite delta-compresses poorly; committing it every run would bloat the repo. `state.db` is gitignored and local-only. |
| Resume = structured YAML + fixed renderer | The model edits *data*, never layout. Formatting cannot drift, so ATS-safety is verified once on the masters rather than per document. |
| Obtainable-clearance ≠ active clearance | "Required to obtain and maintain a clearance" means he QUALIFIES. Matching "maintain" as an active-clearance requirement dropped 23/23 Draper postings. His clearance eligibility is a differentiator, not a filter. |

## Hard constraints

- **Never make live Anthropic API calls during development.** Balance is ~$4.90.
  Every LLM stage is tested against `StubAnthropic`; a session-scoped fixture
  strips `ANTHROPIC_API_KEY` in tests so an accidental real client fails loudly.
  Fetching job boards over HTTP is free and expected — that is not an API call.
- **Do not run git commands.** Git writes are blocked in this environment; the
  owner commits and pushes manually.
- Secrets (`ANTHROPIC_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`) live in
  GitHub repository secrets. Locally they need a gitignored `.env`.
  **Never ask the owner to paste a secret into chat.**

## Commands

```bash
source venv/bin/activate            # macOS has no bare `python`
python run.py --dry-run --no-llm    # free; no API key needed
python run.py --dry-run             # scores, writes HTML, sends nothing
python run.py                       # live
python run.py --applied <job-id>    # record an application; stops re-showing
python -m pipeline.tailor --job-id <id> [--dry-run]
python -m pipeline.score --rescore <id>
```

## Open work, roughly prioritized

1. **Hardware coverage gap.** Qorvo, Wolfspeed, Teradyne, Infineon, BAE are
   **not on Workday** (all 422). Qorvo/Infineon use SuccessFactors; Teradyne and
   BAE run their own portals; Wolfspeed's `cree` tenant is decommissioned.
   Reaching them needs a SuccessFactors fetcher plus per-company scrapers.
   Apple, Lenovo, Siemens Energy answer but block anonymous access (401).
2. **v2: LinkedIn.** Never built. Plan is Gmail API reading LinkedIn job-alert
   emails (within ToS; no scraping). Owner was asked to create alerts so data
   accumulates.
3. **First unattended run is unverified.** Watch 2026-08-19 06:30 ET. The
   workflow has a DST guard (fires 10:30 and 11:30 UTC, exits unless it is
   really 6:30 in New York) and an `if: failure()` email notifier.
4. **No outcome feedback.** `--applied` records that he applied but nothing
   learns from responses. Scores never improve from evidence.
5. **Seasonality.** The 7-day window runs a surplus in August. It will not in
   February. `run.py` diagnoses *why* a list is short — empty pool vs unscored
   backlog vs all-shown — so do not guess.
6. **`out/usage.jsonl` was polluted** by tests writing stub rows (351 fake vs 6
   real). Tests are isolated now; the historical rows may still need cleaning.

## Owner profile notes that drive matching

- Strongest hardware angle is **embedded/firmware** — C, MSP430, bring-up —
  because it bridges his IBM software internship.
- Digital/ASIC is a reach at Tier 1 (coursework-level Verilog, no tapeout).
  Mechanical is weakest.
- **Clearance eligibility is a real differentiator** at Draper, MITRE, RTX, BAE.
- His IBM internship was LangChain/LiteLLM pipeline work, which positions him
  unusually well for the current agentic-AI hiring wave. Worth leading with.
- Boston is his strongest hardware market; NYC is nearly dead for hardware.
