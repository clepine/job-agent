# Handoff — Daily Job Agent

Orientation for a fresh session. This file holds only what is **not derivable
from the code or the other docs**: the constraints, the decisions and their
rationale, and what is still open.

Everything else lives elsewhere and is not repeated here, because a fact stated
twice drifts: `PLAN.md` is the design spec and the owner profile, `README.md` is
setup, operation, the pipeline walkthrough, and the full commands.

## Status as of 2026-08-21

Working. **399 tests pass** (`./venv/bin/python -m pytest tests/ -q`).

Ledger on this branch: 770 postings, 40 scored, 10 shown, 0 applied. Live spend
to date **$0.0757** of an original $5 balance, across one real run. Projected
~$2/month at the 30-scores-per-track cap set on 2026-08-21.

The unattended schedule works — `origin/main` carries `job-agent: run 2026-08-18`
and `job-agent: run 2026-08-19` commits, and the owner received a real digest on
2026-08-21. The workflow has a DST guard (fires 10:30 and 11:30 UTC, exits
unless it is really 6:30 in New York) and an `if: failure()` email notifier, so
silence means it never fired rather than that nothing matched.

**This checkout is not on the default branch.** It sits on
`fix/carry-forward-legacy-scores`, which is local-only, 1 commit ahead of and 3
behind `origin/main`, and the remote-tracking ref was last fetched before the
2026-08-21 run. The scheduled workflow commits state back to whatever branch it
runs on, so `origin/main`'s ledger has moved on from this one and
`state/seen_jobs.json` will conflict on merge. That is expected: `state.load()`
restores earned fields into holes and never overwrites, so take the union rather
than either side wholesale.

## Hard constraints

- **Never make live Anthropic API calls.** Every LLM stage is tested against
  `StubAnthropic`; a session-scoped fixture strips `ANTHROPIC_API_KEY` in tests,
  so an accidental real client fails loudly rather than quietly billing. Nothing
  an assistant does in this repo should ever cost the owner money.
- **Do not run the agent.** `run.py` is the owner's to execute, in *every* mode
  — including the free ones. `--no-llm` and `--dry-run` spend nothing, but they
  still hit ~290 live job boards, mutate `state.db`, and rewrite the committed
  ledger, and a run left half-finished is a worse starting point than no run.
  Hand the owner the command instead. The same applies to `tools/*.py` and to
  anything else that opens a socket.
  - **The offline test suite is the exception, and it is the only one.**
    `./venv/bin/python -m pytest tests/ -q` touches no network and no API, and an
    assistant that may commit but may not verify is strictly more dangerous than
    one that can do both. Run the tests before every commit.
- **You may commit; ask before pushing.** Committing is local and reversible, so
  make one whenever a change is complete and the suite is green. Pushing is not
  local: this repo's workflow has `contents: write` and pushes state back to the
  branch it runs on, so a push interacts with a scheduled job the owner is
  relying on. Ask first, every time.
- Secrets (`ANTHROPIC_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`) live in
  GitHub repository secrets. Locally they need a gitignored `.env`.
  **Never ask the owner to paste a secret into chat.**

## Decisions that must not be silently reverted

Each has a regression test citing the date it was made. **If you change one of
these settings, change this table in the same commit** — a stale row here is
worse than no row, because it is read as authority. `tests/test_docs_match_config.py`
enforces the numeric half of that automatically.

### Entry level

The owner graduates December 2026. "Entry level" is the hard requirement of this
agent, not a preference, and the gates below are the whole of how it is enforced
— the title filters catch `Senior`/`Staff`/`III`, and `check_description` catches
everything that carries a mid-level requirement under an innocent title.

| Decision | Why |
|---|---|
| `check_description` normalizes HTML **before** any gate reads the body | Every pattern in it depends on structure that HTML hides: the years softener is scoped to a clause, and the clearance patterns are bounded by `[^.\n]{0,60}`. Neither boundary exists in `<p>Master&#39;s degree preferred.</p><p><b>Experience</b></p><p>3-5 years experience</p>` — that is one unbroken line, so a softener three paragraphs above a requirement still cancelled it. Measured 2026-08-21: of 67 hydrated postings stating a 3+ year requirement, the gate rejected **zero**. |
| A years softener only counts **inside its own clause** | `_clause_around()`, not a character window. Workday puts the degree bullet directly above the years bullet, so a ±120-character window straddled the boundary and "or equivalent" from the *degree* line waived the years requirement on the *next* one. Live NVIDIA JR2022612 demands 5+ years; the 2026-08-21 email shipped it at fit 68 as an entry-level match. |
| Bare `degree` and `education` are **not** softeners | They express the opposite of what the softener list is for: "Bachelor's degree in EE **and** 4+ years of experience" compounds the requirement, it does not offer a way around it. That one word accounted for 22 of the 67. `_YEARS_SOFTENER` now lists only genuine alternation — `in lieu of`, `or equivalent`, and `including`/`such as` when what follows is an internship, co-op, coursework or project. |
| `_YEARS_ROLE_REQUIRED` catches the phrasing with no "experience" in it | "3+ years **in** SRE", "4+ years **building** automation", "Requires 3-5 years **in** Systems Engineering". 64 of 227 hydrated postings stated a requirement this way and `_YEARS_REQUIRED` could not see any of them. The continuation word is mandatory, which is what keeps company boilerplate out — "transforming computer graphics for more than 25 years." never reaches one. |
| Net effect, measured 2026-08-21 | 65 of 67 such postings now rejected, up from 0. The two survivors say "in lieu of degree" and "or equivalent" and are correctly kept. Half the hydrated pool (116 of 227) now fails this gate; supply still clears the email's needs with room. |
| Obtainable-clearance ≠ active clearance | "Required to obtain and maintain a clearance" means he QUALIFIES. Matching "maintain" as an active-clearance requirement dropped 23/23 Draper postings. His clearance eligibility is a differentiator, not a filter. |
| The URL is a second witness to the title | Aggregator READMEs truncate titles, and a truncated title can hide the word that disqualifies it. Draper's "Embedded Quality & Fielded Systems **Intern**" arrived as "...Systems In" and was the top hardware pick of a real email. `filters.check_url_title` re-runs the title gates on the Workday URL slug. Reject-only: the slug is lossy, so it can add a rejection but never rescue, and is never displayed. |

### Selection

| Decision | Why |
|---|---|
| `email.min_fit: 30` | Still the scorer's own bottom band enforced, not a loosened one. On 2026-08-21 `score.py` gained a `30-39 loose match` band and was retargeted to judge *accessibility to a strong new grad* rather than overlap with the resume, on the owner's note that a role "doesn't have to align exactly with my resume". 0-29 remains "poor match, do not apply" and is still never sent. `test_the_floor_is_the_models_own_calibration` reads the prompt rather than restating a number, so the two cannot drift apart. |
| `email.tier1_min_fit: 40` | A Tier-1 slot is a *preference*, not a mandate. On 2026-08-18 a Waymo role scoring 30 displaced a Draper role scoring 50. Distinct from `min_fit`: this one only *benches* a Tier 1, it does not drop it. |
| Short lists are normal, and always explained | The email states how many were held back and why, so a four-role day is never confusable with an outage. `run.py` separately diagnoses *which* scarcity it is — empty pool, unscored backlog, all-shown, or degraded fetch — and those are not the same problem. Read that before changing any number. |

### Freshness and recency

| Decision | Why |
|---|---|
| `limits.max_posting_age_days: 7` — what is worth **paying to score** | Lever in particular reports a req's original `createdAt`, which surfaces reqs open for literally years. Postings with no date are kept (unknown age is not old age) and age-stamped "unknown". |
| `max_posting_age_days_primary: 10` — the same, in the five primary metros | Was 30 from 2026-08-19 to 2026-08-21. **The measurement that justified 30 was taken through the broken description gate above**, so the "drought" it corrected was a pool the owner could not actually apply to. Re-measured with the gate fixed: 56 software / 26 hardware entry-level-clean postings inside **one** day, 83 / 51 inside seven. The wide window bought nothing and cost heavily — fetch depth scales with it (RTX ~223 Workday pages at 30 days against ~50 at 7), and that depth is what draws the 429s. Narrowing it took a full run from over seven minutes to 2m43s. |
| `limits.max_backlog_age_days: 7` — what is still worth **applying to** | Lowered from 30 on 2026-08-21. The distinction from the ingest window still holds, but at 30 the backlog was the door three-week-old postings kept re-entering through, and the owner's ask is "posted that day or a few days before". A posting is age-filtered when it ARRIVES; without this, nothing re-checks it while it sits in the scored backlog. The 2026-08-18 email sent a 74-day-old role and the pool held a sendable 124-day-old one. |
| `freshness.*` — staleness as a **ranking** cost | Distinct from both windows above: those decide what enters and what may leave, this decides order among postings that cleared both. The old rule was `max(-6.0, -age / 5.0)` under the comment "age never outranks fit", and at a 6-point cap it never outranked anything — a fit-68 at 21 days beat a fit-65 at 1 day, so the 2026-08-21 email led with postings 21, 22 and 24 days old. Now flat inside `fresh_days: 3` and linear-uncapped past it. An undated posting is penalized, not exempted; it is not evidence of freshness. |
| `db.unscored()` orders **freshest first** | The deepest cause of the same symptom, and it is a capacity problem rather than a ranking one. A posting cannot be sent until it is scored, scoring is capped at 20 per track per run, and arrivals were measured at **246 in one day**. Ordering by `metro_class = 'primary'` ahead of the date meant a two-week-old Boston req outranked this morning's arrivals every single day: the email was never choosing among recent postings, it was choosing among whatever won the scoring lottery weeks earlier. The query also refuses to spend on anything already past `max_backlog_age_days`. `fit_score` still sorts ahead of the date and is inert for never-scored rows, so the re-score-after-resume-edit path is unchanged. |

### Fetch and sources

| Decision | Why |
|---|---|
| Fetch pages to the **widest** cutoff | Metro class is unknown until the location filter, which runs after the fetch. Stopping at the 7-day cutoff would discard exactly the postings the primary-metro window exists to admit. `filters.fetch_max_age()` is the only place that decides this. |
| Workday pages in concurrent waves of 6 | Paging depth is the whole cost of that source. Sequentially, a full 290-board fetch took **52 minutes**, past the workflow timeout. Pages fetched past the stop are **discarded, never appended** — appending them would leak the postings the staleness cutoff exists to exclude. Page 0 is never waved, so a dead tenant still costs one request and `probe()`'s 401/404/422 diagnosis reads one failure. |
| 429 is retried; other 4xx never are | 429 is the one 4xx meaning "ask again" rather than "stop asking". The 400/401/404/422 distinction stays immediate because `probe()` reads it to tell "not on Workday" from "wrong site segment". |
| 429 has its **own** retry budget | `fetch.transient_retries: 5`, separate from `fetch.retries: 2`. Sharing one budget of 2 was the unfixed half of the 2026-08-19 board-loss bug: a board needing 200 pages collects hundreds of 429s, and two attempts is not a rate-limit strategy. One run on 2026-08-21 took **675** 429s and lost 32 of 290 boards to them — Cardinal Health, Booz Allen, Cox, Curtiss-Wright and CVS Health among them, all live and answering normally, all reported to the owner as a degraded fetch. With the separate budget the same run loses 4. Backoff is jittered: up to 48 requests are in flight at once and un-jittered backoff marches them into the next window together. |

### Text handling

The posting body feeds the keyword diff, the scorer, and every description gate.
Anything wrong here is wrong three times over and shows up as a quality problem
rather than an error.

| Decision | Why |
|---|---|
| `html_to_text` unescapes **escaped** markup first | Greenhouse returns `&lt;div class=&quot;content-intro&quot;&gt;` rather than real tags. Every strip pattern matches on real angle brackets, so against escaped input they stripped nothing and the "text" that came out was still markup. A SpaceX posting yielded ZERO keywords through that path and eleven — C, C++, Python, Go, Rust, gRPC, PostgreSQL — once unescaped. Bounded to two rounds so it cannot loop on adversarial input. |
| `compress_jd` tests for `&lt;` as well as `<` | Its guard was `html_to_text(raw) if "<" in raw`, so escaped postings skipped normalization entirely and reached the section splitter as tag soup, matching no heading. This is why the unescape fix above was invisible until both landed. |
| `compress_jd` drops everything before the first section heading | `dropping` starts False, so the "about us" preamble was kept in full and the `jd_max_chars` truncation then cut the posting off before it ever reached the qualifications. A SpaceX req compressed to 1,551 characters of "SpaceX was founded under the belief that..." — the model was scoring fit from the company blurb. Only applied when a keep-heading exists; a posting with no recognizable sections still uses the substance-scored fallback. |
| ATS keyword diff uses **no LLM** | Set arithmetic over a skills vocabulary. Cheaper *and* more reliable — it cannot hallucinate a keyword the posting never contained. Postings yielding zero keywords fell from 14% of the pool to 10% once the three fixes above landed. |
| Each posting is compressed and diffed **once** per render | The HTML part and the plain-text part render the same postings, and both used to compress the body and re-run the keyword diff themselves — a ten-posting email compressed twenty bodies and ran fifty diffs, all duplicates. `_build_cards()` computes each once and hands the result to both, and `Recommendation` carries the two diffs it was derived from so the keyword chips need no third. Beyond the waste, it removed the way the two halves of one email could disagree about what a posting asked for. |
| The resume line never overstates the measurement | Three separate phrasings were wrong in a rendered email: an exact 75%/75% tie reported as software ahead "by a hair"; "no keywords extracted" printed for a posting whose keywords extracted fine and matched neither resume; and a 0-vs-0 dressed up as a verdict. A line the reader cannot trust on the easy cases is not worth reading on the hard ones. |

### Resume routing

| Decision | Why |
|---|---|
| Embedded routes to **hardware** | `infer_track` documented this and did not do it. `if sw and not hw` fired ahead of the `hw and sw` branch whose own comment names the case, so "Embedded Software Engineer" — PLAN.md §4's strongest hardware angle — returned software. Every embedded posting was therefore scored against the wrong resume and filed in the wrong section while the hardware track ran dry: the 2026-08-21 email filed an "Embedded Linux Software Engineer" under Software and reported 0 hardware matches on the same page. |
| The email **names** the resume to send | `pipeline/resume_pick.py` measures both masters against the posting's own vocabulary and prints which covers more of it, by how much. The track is a routing decision made from the title, and on a bridge role — "Systems Engineer", "Test Engineer" — it is a close call the owner should see rather than have made for him silently. A gap under `CLOSE_MARGIN` is reported as a tie instead of a winner, and a 0-vs-0 is reported as no signal rather than dressed up as one. |
| No LLM in that call either | It reuses `keywords.diff`, so the recommendation cannot contradict the keyword chips printed directly beneath it in the same card. |

### State and cost

| Decision | Why |
|---|---|
| The ledger outranks the local DB | `state.load()` was `INSERT OR IGNORE`, so a `state.db` that had drifted behind `state/seen_jobs.json` kept its own NULLs and `dump()` wrote them back over the committed file. On 2026-08-19 that erased 40 scores and every `shown_at`; `run.py --applied` had the same failure mode. `load()` now restores earned fields into holes and never overwrites; `dump()` refuses to write a ledger with fewer earned records than the file on disk. |
| Score once, cache forever | Cost. A posting's fit does not change. Invalidated by `score_fingerprint()`, which covers both the resume *and* the scoring regime (prompt, model id, `jd_max_chars`) — a cached score is only comparable to a fresh one if both were produced the same way. The 2026-08-21 prompt change therefore invalidates the 40 existing scores, and that is correct; the cost is small and self-limiting because `db.unscored()` will not re-score anything past the backlog window, and most of those 40 are already outside it. |
| Legacy scores are carried forward, not re-scored | `db.upgrade_score_fingerprints()` upgrades a row only when its stored hash equals the *current* resume-only fingerprint. The cost that matters is not the ~$0.08 — a run's scoring budget is capped, so a morning re-scoring old work is a morning not spent draining the backlog. |
| State committed as sorted JSON, not SQLite | Binary SQLite delta-compresses poorly; committing it every run would bloat the repo. `state.db` is gitignored and local-only, and self-heals from the ledger on load. |
| `budget.max_usd_per_run: 0.15` and `limits.max_new_scores_per_run: 30` move **together** | Raised from $0.10 / 20 on 2026-08-21 to buy capacity. They are coupled and nothing enforced it before `test_the_budget_ceiling_covers_a_full_scoring_run`: the ceiling aborts *before* the call that would breach it, so a cap that outgrows its budget does not fail loudly — it scores half the pool and the email quietly draws on a thinner backlog than it should. Costs are measured, not guessed: $0.0126 per call at `score_batch_size: 8`, so $0.00158 per posting, so ~$0.095 for a full 60-posting run — about 37% headroom. `max_jobs_to_prerank` went to 60 in the same edit because preranking 40 candidates down to 30 is barely a selection, and that stage is free. |
| Scoring capacity is the binding constraint, not the fit floor | A posting cannot be sent until it is scored. Measured 2026-08-21, one fetch brought in 60 software and 50 hardware postings; at 20 per track the majority were never scored, and because `db.unscored()` orders freshest-first they could never catch up — the next morning's arrivals outrank them permanently. 30 covers a normal day's hardware inflow outright and most of software's. If the email is thin, check this before touching `min_fit`. |
| Tailoring is **on demand**, not 10/day | He applies to ~3 jobs/week, not 50. |
| BM25, not embeddings | Anthropic has no embeddings endpoint; embeddings would mean a second vendor and bill. |
| Resume = structured YAML + fixed renderer | The model edits *data*, never layout. Formatting cannot drift, so ATS-safety is verified once on the masters rather than per document. |

## Open work, roughly prioritized

1. **The 2026-08-21 changes have never run live.** Everything in the entry
   level, freshness, text handling and resume routing sections above was
   verified against the test suite and against offline replays of a real fetch,
   but no scored run has happened since. The first live run will re-score from
   scratch under the new prompt; expect the email to fill from that day's
   arrivals rather than the old backlog. The owner should run
   `./venv/bin/python run.py --dry-run` and read the list before letting the
   schedule send one.
2. **This branch needs reconciling with `origin/main`.** See the status section
   for the topology. The ledger is the conflict that matters.
3. **Hardware coverage gap.** Qorvo, Wolfspeed, Teradyne, Infineon and BAE are
   **not on Workday** (all 422). Qorvo/Infineon use SuccessFactors; Teradyne and
   BAE run their own portals; Wolfspeed's `cree` tenant is decommissioned.
   Reaching them needs a SuccessFactors fetcher plus per-company scrapers.
   Apple, Lenovo and Siemens Energy answer but block anonymous access (401).
   Less urgent than it was: routing embedded to hardware roughly balanced the
   two tracks (60 software / 50 hardware on the last full fetch, against
   153 / 93 before), so the hardware drought was mostly a routing bug.
4. **v2: LinkedIn.** Never built. Plan is the Gmail API reading LinkedIn
   job-alert emails (within ToS; no scraping). The owner was asked to create
   alerts so data accumulates.
5. **No outcome feedback.** `--applied` records that he applied, but nothing
   learns from responses. Scores never improve from evidence. He has 0
   applications recorded so far — the funnel currently ends at "email sent".
6. **Seasonality.** Inflow was measured in August, at peak new-grad hiring, and
   the entry-level gate now removes about half the pool that survives the title
   filters. Both cuts are correct and they compound. If February comes up short,
   `limits.max_posting_age_days` is the first number to raise — not the fit
   floor, and not the entry-level gates.
   Some postings are still never scored on a heavy day: 30 per track against
   60 software arrivals. That is a money dial rather than a bug, and the two
   settings that move it are in the State and cost table.
7. **`companies.yaml` `metros` is decorative.** No code reads it, and Northrop
   Grumman's list is empty. It was nearly used to gate the wider age window
   per board, which would have kept exactly the wrong board narrow. Either fix
   the data or label the field as documentation before someone trusts it.
8. **A judgment call left open.** State Street's "AI Engineer – GenAI & Agentic
   Systems (**Officer**)" scored 65 and is a strong software match, but
   "Officer" is a State Street rank above entry level. Adding `officer` to the
   seniority regex would drop it. Deliberately not decided for him.
