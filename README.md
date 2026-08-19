# Daily Job Agent

One email each weekday morning: 5 software + 5 hardware new-grad roles matched
against two master resumes, each with a true posting age, a one-line match
rationale, an ATS keyword diff, and an apply link.

Design spec: [`PLAN.md`](PLAN.md). This README covers setup and operation.

---

## Quick start

```bash
git clone https://github.com/clepine/job-agent.git
cd job-agent

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Free stages only. No API key needed, nothing is sent, nothing is spent.
python run.py --dry-run --no-llm
open out/$(date +%F)/email.html
```

That runs fetch → normalize → dedupe → hard-filter → hydrate → re-filter and
writes everything to `state.db`. Add a key and drop `--no-llm` for a real run.

---

## Required environment variables

Put these in a `.env` file at the repo root (already in `.gitignore`) or export
them in your shell. Nothing in this codebase logs, prints, or commits a secret.

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | scoring, tailoring | Without it, scoring is skipped and the email is built from scores already on record. The pipeline never fails just because the key is absent. |
| `GMAIL_ADDRESS` | sending | The Gmail account that sends the mail. |
| `GMAIL_APP_PASSWORD` | sending | A Google **app password**, not your account password. Requires 2FA: Google Account → Security → 2-Step Verification → App passwords. |

```dotenv
# .env
ANTHROPIC_API_KEY=sk-ant-...
GMAIL_ADDRESS=you@gmail.com
GMAIL_APP_PASSWORD=abcdefghijklmnop
```

In GitHub Actions these are repository secrets of the same three names.

---

## Running it

```bash
# Everything except sending. Writes out/<date>/email.html and email.txt.
# Jobs are NOT marked as shown, so you can run it repeatedly.
python run.py --dry-run

# Free stages only — no API key required at all.
python run.py --dry-run --no-llm

# Live: scores new postings, sends the email, marks the 10 as shown.
python run.py

# Useful flags
python run.py --max-scores 5      # cap how many new postings get scored
python run.py --skip-boards       # aggregator repos only
python run.py --skip-repos        # curated ATS boards only
python run.py --no-mark-shown     # send, but let the same jobs reappear tomorrow
```

### Bookkeeping

```bash
# Record that you applied. Free, instant — it does NOT run the pipeline.
# An applied job is never shown again, and the daily summary counts them.
python run.py --applied 6f2a9c...
python run.py --list-applied
```

### Re-scoring one posting

Scores are computed once and cached, so a score the model got wrong stays wrong
until the resume changes. This is the escape hatch:

```bash
python -m pipeline.score --rescore 6f2a9c...            # budget-gated, usage-logged
python -m pipeline.score --rescore 6f2a9c... --dry-run  # no API call, spends nothing
```

It re-fetches the posting body first — the committed ledger deliberately does
not persist descriptions, so an old job arrives with an empty one and would
otherwise be re-scored on its title alone.


### State

Two files, deliberately split:

| File | Role | Committed? |
|---|---|---|
| `state.db` | local SQLite working store | **no** — gitignored |
| `state/seen_jobs.json` | the real ledger: sorted, one object per job, including `applied_at` | **yes** |

SQLite is rewritten in full every run and delta-compresses poorly, so
committing it would add a fresh binary copy to history ~250 times a year to
track what is really a list of job IDs. The committed artifact is a sorted JSON
file instead, so a normal day's diff is a few added objects and a few
`shown_at` flips. The run hydrates SQLite from it on start and writes it back at
the end. Descriptions are not persisted there — the ten jobs in today's email
have theirs re-fetched over free HTTP at send time.

### Resumes

```bash
# Render both masters to out/master_sw.pdf and out/master_hw.pdf
python resume/render.py --all

# Fold Fall-2026 in-progress coursework into completed coursework.
# Correct for Jan-2027 start dates (PLAN.md §4).
python resume/render.py --all --coursework-complete
```

### Tailoring (on demand)

Tailoring is **not** run for all ten daily matches — see
[Cost model](#cost-model). Run it for a job you have decided to apply to; the
job id is printed in the email under each match.

```bash
python -m pipeline.tailor --job-id 6f2a9c...            # writes out/<date>/<company>-<title>.pdf
python -m pipeline.tailor --job-id 6f2a9c... --dry-run  # no API call; shows what would be sent
```

If the model tries to add a skill that is not on the master resume, **the run
fails and no PDF is written**. That is the intended behavior — see
[The honesty rule](#the-honesty-rule).

---

## How it works

```
fetch ─→ normalize ─→ dedupe ─→ hard-filter ─→ hydrate ─→ re-filter
      ─→ BM25 pre-rank ─→ score NEW jobs once ─→ pick 5+5 ─→ email
         └──────────── free ────────────┘   └─ the only LLM stage ─┘  └─ free ─┘
```

| Stage | Module | Cost |
|---|---|---|
| Fetch 290 public ATS boards (160 GH/Lever/Ashby/SR + 130 Workday) + 2 aggregator READMEs | `sources/`, `pipeline/fetch.py` | free HTTP |
| Canonical-URL hash + fuzzy `(company, title, location)` dedupe | `pipeline/models.py` | free |
| Hard filters: seniority, discipline, level, location, staleness, description | `pipeline/filters.py` | free |
| Fetch descriptions for survivors only | `sources/hydrate.py` | free HTTP |
| Per-posting track inference (software vs hardware) | `pipeline/track.py` | free |
| BM25 relevance pre-rank against the resume | `pipeline/rank.py` | free |
| **Score each new posting once, 0-100 + one-line rationale** | `pipeline/score.py` | **Claude Sonnet 5** |
| Pick 5+5 with the 2 Tier-1 / 3 Tier-2 quota | `pipeline/pick.py` | free |
| ATS keyword diff | `pipeline/keywords.py` | free |
| Render + send | `pipeline/email.py` | free |

**Score-once is the central cost decision.** A posting's fit against a fixed
resume does not change between days, so it is scored the first time it survives
the filters and the result is persisted. The daily pick reads scores out of
SQLite and calls nothing. A steady-state run scores only the ~10-15 postings
that are genuinely new.

### Workday

Workday is half the board list and behaves unlike the other four ATSes. The
contract below was verified empirically, not from documentation — the full
write-up with the measurements is the module docstring in `sources/workday.py`.

| | |
|---|---|
| Method | **POST** to `/wday/cxs/{tenant}/{site}/jobs`, body `{appliedFacets, limit, offset, searchText}` |
| Page size | **Hard-capped at 20.** `limit: 21` returns HTTP 400 |
| `total` | Only meaningful on the **first** page — a deep offset reports `total: 0` while still returning postings, so pagination ends on a short page |
| Sort | **Strictly newest-first**, which is what makes this affordable |
| Locations | Written country-first (`US, NC, Durham`) and in a different shape per tenant; multi-site postings say `"6 Locations"` instead of a place |
| Descriptions | Not in the list response — one GET per posting, paid only for filter survivors |
| Auth | None. No session cookie is needed on any tenant that answers at all |

The newest-first sort is load-bearing. With `limits.max_posting_age_days: 7`
the fetcher stops as soon as one whole page is older than the cutoff, so RTX's
4,441-posting board costs ~10 requests a morning instead of 223. Adding 130
Workday boards moved a full run from ~2m40s to ~2m35s — inside the noise.

The location handling is the part most likely to rot. `pipeline/geo.py` matches
a city only in the segment *before* the first comma, so Workday's native format
classifies every US posting as "none" and the source silently returns nothing.
`workday.expand_location()` appends `City, ST` fragments for `geo.py` to
consider; it is **additive by construction** — the original string is always
kept, so it can turn a "none" into a match but never the reverse.

**Scores expire when the resume changes.** Each score is stamped with a
fingerprint of the resume it was computed against (`pipeline/fingerprint.py`),
covering only what actually feeds a score — skills, experience, projects,
coursework. Edit a skill and every affected score is re-queued; fix a typo in
your phone number and nothing is. If an edit invalidates more than one run's
budget allows, the highest-scoring jobs are re-scored first and the rest carry
to later runs — never an abort, never a budget blowout.

---

## Cost model

Measured against real prompts on live data (no API calls were made to produce
these numbers — prompts were built and measured locally):

| Day | Calls | Input tok | Output tok | Cost |
|---|---|---|---|---|
| Quiet (6 new postings) | 2 | ~6,000 | ~360 | **$0.023** |
| Typical (12 new) | 3 | ~10,500 | ~720 | **$0.042** |
| Peak (20 new — the configured cap) | 3 | ~14,600 | ~1,200 | **$0.062** |

At ~22 weekdays that is roughly **$0.90/month**. On-demand tailoring adds about
**$0.019 per job**.

Five things keep it there, all of them structural rather than incidental:

1. **A hard per-run ceiling** (`budget.max_usd_per_run`, default `$0.10`).
   Estimated spend is checked *before* every request; a call that would cross
   the ceiling raises `BudgetExceeded` and is never sent. A filter-layer
   regression cannot drain the balance overnight.
2. **Score-once.** Yesterday's postings cost nothing today.
3. **JD compression** (`pipeline/jd.py`). Benefits, EEO text, "about us", and
   application instructions are stripped before anything reaches the model;
   what is left is truncated to 1,600 characters. Typical reduction is 4-10x.
4. **Prompt caching.** The stable prefix (system prompt + resume summary,
   ~1,350-1,530 tokens — above Sonnet 5's 1,024-token minimum) carries
   `cache_control: {"type": "ephemeral"}`, and all volatile per-job content
   goes *after* it. `resume_summary()` is deterministic, and there is a test
   asserting that, because a byte-unstable prefix silently destroys the cache.
5. **Batching.** Eight postings per request rather than one, which amortizes
   the cached prefix and cuts per-call overhead.

Every call's real `usage` — input, output, `cache_read_input_tokens`,
`cache_creation_input_tokens` — is appended to `out/usage.jsonl` so estimates
can be checked against reality:

```bash
python -c "
import json;rows=[json.loads(l) for l in open('out/usage.jsonl')]
print(f'{len(rows)} calls, \${sum(r[\"call_cost_usd\"] for r in rows):.4f} total')"
```

Model choice is `claude-sonnet-5` with `thinking: disabled` and `effort: low`,
set in `config.yaml`. Thinking is **on by default** on Sonnet 5 and is disabled
explicitly here: scoring is bounded extraction, not deep reasoning, and
thinking tokens bill as output at 5x the input rate.

---

## The honesty rule

PLAN.md §2 stage 7: the tailored resume may only reorder, rephrase, or promote
content already in the master. **This is enforced in code, not in the prompt.**
A prompt asking for it is a request; `validate_tailored()` is a guarantee.

The model is structurally prevented from adding content:

* It cannot emit new bullets. It emits *rewrites* keyed to exact master bullet
  text; anything that does not match a master bullet is discarded.
* It cannot emit new skills. It emits an *order* over existing skills.
* Renames are the one PLAN-sanctioned exception (Verilog → "RTL design"). The
  source must be a real master skill **and** the target must appear verbatim in
  the job description — otherwise it is invention, not rephrasing.
* Every rewritten string is scanned for technology terms, both against a
  curated vocabulary and a "looks like an identifier" heuristic. Any term not
  traceable to the master fails the run.

`apply_tailoring()` has no code path that appends a bullet, skill, experience
entry, or project — it only replaces strings and reorders lists.

There are 30 tests covering this, including a parametrized set that tries to
smuggle in Kubernetes, Rust, PyTorch, FreeRTOS, Altium, SystemVerilog, Cadence
Virtuoso, STM32, Terraform, and Kafka.

The ATS keyword diff is deliberately **not** an LLM job for the same reason: a
model asked "what does this posting want?" will occasionally invent a term the
posting never contained. Set arithmetic over a curated vocabulary cannot. Every
term the email reports as "asked for" is a literal substring of the posting.

---

## Layout

```
run.py                      orchestrator
config.yaml                 model, budget, limits, geography, paths
companies.yaml              308 boards: 160 + 148 Workday (130 live-validated)
sources/
  greenhouse.py lever.py ashby.py smartrecruiters.py    public JSON board APIs
  workday.py                                            CxS POST API + pagination
  github_repos.py                                       aggregator README tables
  hydrate.py                                            descriptions for survivors only
pipeline/
  models.py       Job schema, canonical-URL hash, fuzzy dedupe keys
  geo.py          metro matching (5 primary + 6 secondary)
  filters.py      the hard-filter layer  ← most important file
  track.py        per-posting software/hardware inference
  jd.py           boilerplate stripping + truncation
  rank.py         BM25 pre-rank
  score.py        the one LLM stage
  pick.py         tier quota, diversity cap, final ordering
  keywords.py     local ATS keyword diff
  tailor.py       on-demand tailoring + honesty validator
  email.py        HTML render + Gmail SMTP
  llm.py          budget ceiling, usage ledger, prompt caching
  db.py           SQLite state
resume/
  master_sw.yaml master_hw.yaml     structured masters
  render.py                         YAML → ATS-safe PDF
.github/workflows/daily.yml         6:30 AM ET weekdays
state.db                            committed each run
out/YYYY-MM-DD/                     committed each run
```

### `companies.yaml`

Two keys. `boards:` holds 160 entries that were **probed live** and returned at
least one posting (`postings_at_validation` records how many). `workday_boards:`
holds 148 entries keyed by `tenant|shard|site`; **130 were probed live and
answered**, and the 18 that did not carry a `note` explaining why (unknown
tenant / wrong site segment / anonymous access blocked).

Re-probe every Workday board and rewrite the flags in place:

```bash
python tools/probe_workday.py           # report only
python tools/probe_workday.py --write   # update companies.yaml
```

To add a company, add an entry under `boards:` and confirm it answers:

```bash
curl -s "https://boards-api.greenhouse.io/v1/boards/<slug>/jobs" | head -c 300
curl -s "https://api.lever.co/v0/postings/<slug>?mode=json"       | head -c 300
curl -s "https://api.ashbyhq.com/posting-api/job-board/<slug>"    | head -c 300
```

---

## Scheduling

`.github/workflows/daily.yml` runs at 6:30 AM ET, Monday–Friday.

GitHub Actions cron is always UTC and has no DST support, so 6:30 AM ET is
10:30 UTC in summer and 11:30 UTC in winter. The workflow fires **both** times
and a guard step asks Python for the current `America/New_York` hour, exiting
immediately unless it is 6. Exactly one trigger survives per day; the other
costs a couple of seconds. Using the tz database rather than hardcoded dates
means a future change to the DST rules is handled for free.

The workflow runs the test suite before the pipeline and will not send an email
if the filters regress. It commits `state/seen_jobs.json` and `out/<date>/` back
to the repo on every run — that is the agent's memory; without it, every run
would re-send yesterday's matches.

**Failure is loud, not silent.** The realistic way this project dies is that a
board changes its API, runs start failing, and the owner just stops getting
email and assumes there were no matches. So a failed run emails him directly
(`if: failure()`, `continue-on-error` so the notifier can never mask the real
error), and every successful run writes a summary to the Actions job summary
that says explicitly when zero jobs matched. "Nothing matched" and "the job
never ran" are never confusable. GitHub also disables scheduled workflows after
60 days of repository inactivity — the failure email says so.

---

## Testing

```bash
python -m pytest tests/ -q
```

295 tests. **No test makes a live API call** — the session-scoped `_no_api_key`
fixture removes `ANTHROPIC_API_KEY` from the environment for the whole run, so
a code path that accidentally constructs a real client fails loudly instead of
silently spending. Every LLM stage is exercised against `StubAnthropic`.

The filter fixtures in `tests/test_filters.py` are real postings and are the
acceptance criteria for the whole system:

*Must reject* — "Embedded Legal Engineer" (Palantir; a lawyer), "Account
Partner", "Sand and Prep - 3rd shift", "Field Service Engineer IV", "Associate
Project Manager, Fire Sprinklers".

*Must keep* — "ASIC Verification Engineer" (HPE Durham), "FPGA Engineer"
(Belvedere Chicago), "Mixed Signal Electronic Design Engineer" (Draper
Cambridge).

`tests/test_render_fidelity.py` is both the resume-fidelity check and the
**ATS guarantee**. It renders each master, extracts the text layer from the
actual PDF bytes, and asserts that every section heading, skill category label,
skill item, and bullet appears verbatim, in correct reading order, with no
images and no embedded font subsets. Text that extracts cleanly in reading
order is exactly what an ATS parser consumes.

Skill categories carry an explicit `label` in the YAML that is printed
verbatim. They are never derived from a key — deriving them by title-casing a
snake_case key silently turned "Frameworks & Libraries" into "Frameworks
Libraries" on a document that goes to employers.

---

## Known limitations

* **Workday is implemented** (`sources/workday.py`), and 130 of the 148
  mined boards answer. But the Tier-2 hardware names it was meant to reach are
  only partly there: **Qorvo, Wolfspeed, Teradyne, Infineon and BAE are not on
  Workday at all** — their `tenant|shard|site` triples were hand-guessed, and
  every shard returns HTTP 422. Reaching them needs a SuccessFactors /
  Phenom fetcher, not a Workday one. Raytheon *is* covered, under `RTX`.
  Apple, Lenovo and Siemens Energy return HTTP 401: their tenants exist but
  block anonymous access. See the header of `companies.yaml` for the
  per-company verdicts.
* **Analog Devices answers but contributes nothing today.** Its board is live
  (1,025 postings) and its locations parse correctly; it simply has no
  new-grad-titled engineering role in a target metro inside the 7-day window.
  That is a real result, not a parsing failure — its US postings in the window
  are senior, and its entry-level ones are in Cavite and Chon Buri.
* **Gmail API / LinkedIn alert parsing is v2** and not built. Sending uses SMTP.
* **Aggregator rows arrive truncated.** Titles and locations are cut with an
  ellipsis. Records are flagged `needs_hydration`, the real title is fetched
  from the apply URL where a free JSON endpoint exists (Greenhouse, Lever,
  Ashby, SmartRecruiters, and now Workday), and they are then re-filtered.
* **Posting ages from the aggregators are relative** ("20m", "3d") and are
  converted to absolute timestamps at fetch time. They are accurate as of the
  fetch, which is what the email claims.
