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

Working and verified. **339 tests pass** (`./venv/bin/python -m pytest tests/ -q`).
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
| `email.min_fit: 50` | score.py calibrates 0-39 as "poor match, do not apply". Before the floor, the email always sent 5+5 whether or not 10 were worth sending — on 2026-08-18 it shipped a Waymo ML ASIC role at 30/100 whose own rationale said he lacks tapeout experience. Short lists are now normal and are explained. |
| `limits.max_backlog_age_days: 30` | The ingest age filter only ever ran on the morning's arrivals. A posting could sit in the scored backlog for months and still be sent: the 2026-08-18 email carried a 74-day-old Field AI role and the pool held a sendable 124-day-old one. 30, not 7 — the ingest window governs what is worth PAYING to score, this one governs what is still worth applying to. |
| The URL is a second witness to the title | Aggregator READMEs truncate titles, and a truncated title can hide the word that disqualifies it. Draper's "Embedded Quality & Fielded Systems **Intern**" arrived as "...Systems In" and was the top hardware pick of a real email. `filters.check_url_title` re-runs the title gates on the Workday URL slug. Reject-only: the slug is lossy, so it can add a rejection but never rescue or be displayed. |
| The ledger outranks the local DB | `state.load()` was `INSERT OR IGNORE`, so a `state.db` that had drifted behind `state/seen_jobs.json` kept its own NULLs and the `dump()` at the end of the run wrote them back over the committed file. On 2026-08-19 that erased 40 scores and every `shown_at`. `load()` now restores earned fields (score, shown_at, applied_at) into holes, and `dump()` refuses to write a ledger with fewer earned records than the one on disk. |

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
6. ~~**`out/usage.jsonl` was polluted**~~ — cleaned 2026-08-19 with
   `python tools/clean_usage.py --write`. 351 stub rows removed, 6 real rows
   kept, real spend $0.0757. A `.bak` sits beside it. Stub rows are separable
   because `StubAnthropic` returns a constant 1000-in/200-out with no cache
   tokens, which a real call cannot coincide on.
7. **Primary metros are starved and it is the age window, not coverage.**
   Measured 2026-08-19: the 770-posting pool holds 392 Bay Area and 106 Seattle
   but 3 Charlotte and **1** RTP. The obvious hypothesis — missing employers —
   was tested and is wrong: 18 candidate boards (Truist, Duke Energy, TIAA,
   Epic Games, Pendo, Bandwidth, SAS, Toast, Klaviyo, Cognex, Marvell, Cadence
   and others, all probed live and answering) contribute **4** postings inside
   the 7-day window and **53** with no age limit. Epic Games, Bandwidth and
   Pendo have RTP new-grad engineering roles 13-29 days old that the 7-day
   window discards permanently. See "Open question" below.

## The age window is now metro-aware (owner's call, 2026-08-19)

`limits.max_posting_age_days: 7` still governs secondary metros and remote.
`limits.max_posting_age_days_primary: 30` governs the five primary metros.

The original 7-day surplus (119 SW / 67 HW) was measured across the whole board
list, which is heavily Bay Area weighted, so it was a surplus in the markets he
is *least* likely to move to. Measured across all 290 boards on 2026-08-19,
postings surviving the title and location filters:

| cutoff | in primary metros | RTP sw/hw | Boston hw | NYC hw | Chicago hw |
|---|---|---|---|---|---|
| 7d | 84 | 7 / 0 | 8 | 0 | 1 |
| 14d | 124 | 10 / 1 | 13 | 2 | 2 |
| **30d** | **207** | **16 / 3** | **23** | **7** | **3** |
| 60d | 261 | 17 / 3 | 25 | 9 | 3 |

Hardware in primary metros — the scarcest resource in the whole system — goes
from 11 to 36. Returns flatten after 30 days, which is why 30 is the number.

Verified end to end on 2026-08-19 (`--dry-run --no-llm`, full fetch, 0 boards
failed): **183 new postings added, 100 of them in primary metros, 24 of those
hardware**. For comparison, the entire 770-posting pool before this change held
37 Boston, 21 Chicago, 3 Charlotte and 1 RTP. Fetch wall time 7m18s, against
3m36s at the old 7-day cutoff and 52m before page waves existed.

**The fetch layer must page to the widest cutoff**, not the default one: metro
class is unknown until after the location filter, so stopping at 7 days would
discard the very postings the setting exists to admit. `filters.fetch_max_age()`
is the single place that decides this.

That has a real cost, and it needed a second change to be affordable. At 30
days RTX needs ~223 Workday pages and Northrop ~185, against ~50 and ~35 at 7
days. A full 290-board fetch with the old strictly-sequential paging took **52
minutes** — past the workflow's 25-minute timeout. Workday paging is pure
latency, so `sources/workday.py` now requests pages in concurrent waves of 6
after page 0 (`fetch.workday_page_wave`), discarding anything fetched past the
stop. Page 0 is never waved, so a dead tenant still costs exactly one request.

## Owner profile notes that drive matching

- Strongest hardware angle is **embedded/firmware** — C, MSP430, bring-up —
  because it bridges his IBM software internship.
- Digital/ASIC is a reach at Tier 1 (coursework-level Verilog, no tapeout).
  Mechanical is weakest.
- **Clearance eligibility is a real differentiator** at Draper, MITRE, RTX, BAE.
- His IBM internship was LangChain/LiteLLM pipeline work, which positions him
  unusually well for the current agentic-AI hiring wave. Worth leading with.
- Boston is his strongest hardware market; NYC is nearly dead for hardware.
