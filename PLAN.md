# Daily Job Agent — Build Plan

Sends one email each weekday morning: 5 software + 5 hardware new-grad roles matched
to my resumes, each with an ATS keyword diff and a link to a tailored resume PDF.

**Owner:** Charles Lepine · clepine050@gmail.com
**Graduation:** December 2026 · **Citizenship:** US, clearance-eligible
**Primary metros:** RTP/Raleigh-Durham · Charlotte · Boston · NYC · Chicago
**Secondary metros (hardware reach):** Austin · San Jose/Bay · Seattle · Phoenix · Dallas · Huntsville

---

## 1. Core design decisions

| Decision | Choice | Why |
|---|---|---|
| Runtime | GitHub Actions cron | Free, runs without my laptop, secrets built in |
| Delivery | 6:30 AM ET, Mon–Fri | Overnight postings land before I wake; early applications matter |
| LinkedIn | Parse LinkedIn job-alert emails via Gmail API | Within ToS, no scraping, no extra cost |
| Resume out | Keyword diff inline + tailored PDF committed to repo, linked | Skimmable email, permanent versioned archive |
| "New" means | New *to me*, age-stamped | 5/day fresh hardware roles do not exist; see §3 |
| Geography | 5 primary metros + 6 hardware hubs | ~5x the hardware pool; primary metros get a rank bonus |
| Model | Claude Sonnet 5 for rank + tailor | ~$2–5/mo at this volume |

### Tiering (per user request: 2 of 5 top-tier, 3 of 5 strong-but-reachable)
- **Tier 1** — FAANG+ and equivalent: Google, Meta, Apple, Amazon, Microsoft, Nvidia,
  Netflix, OpenAI, Anthropic, Jane Street, Citadel, Two Sigma, Waymo, SpaceX, Anduril.
- **Tier 2** — strong, meaningfully less competitive: HPE, Cisco, IBM, Draper, MITRE,
  Analog Devices, Qorvo, Wolfspeed, Infineon, Teradyne, Raytheon, BAE, Honeywell,
  Bloomberg, Datadog, MongoDB, Red Hat, Lenovo, Motorola Solutions, Zebra, Abbott.
- Email composes **2 from Tier 1 + 3 from Tier 2** per track. If Tier 1 is empty that
  day, backfill from Tier 2 and say so explicitly rather than padding.

---

## 2. Pipeline

```
fetch → normalize → dedupe → hard-filter → cheap-rank → LLM-pick → tailor → render → send
```

Cost control: a run pulls ~2,000 postings. Regex filters cut to ~150, embeddings to ~40,
and only then does Claude read full descriptions to choose the final 5+5.

### Stage detail

**1. Fetch**
- *Primary:* Greenhouse / Lever / Ashby public JSON board APIs over a curated company
  list (~200 boards). Unauthenticated, full descriptions, no ToS issue.
- *Secondary:* Workday `CxS` endpoints for the defense/industrial primes (different
  shape, needs POST — second pass).
- *Supplementary:* `zapplyjobs/New-Grad-Software-Engineering-Jobs-2027` and
  `New-Grad-Hardware-Engineering-Jobs-2027` READMEs (markdown pipe tables).
- *LinkedIn:* Gmail API reads job-alert emails, parses postings out.

**2. Normalize** — common schema: company, title, location, url, ats, description,
posted_at, first_seen_at, source, track.

**3. Dedupe** — SQLite, keyed on canonical URL hash. Checked before any spend.
The repos contain literal duplicate rows (HPE and Draper both appear twice), so
also fuzzy-dedupe on (company, normalized_title, location).

**4. Hard filter** — free, regex, kills ~90%:
- Drop senior/staff/principal/lead/manager/director titles; drop "5+ years".
- Keep new grad / entry level / university / campus / associate / 0–2 years /
  "2026 grad" / "2027 start".
- Drop non-engineering false positives. Required — the repos surface
  "Embedded **Legal** Engineer", "Account Partner", "Sand and Prep 3rd shift".
- Location must match a primary or secondary metro (or explicit remote-US).
  Primary metros carry a +15 rank bonus so they win ties; secondary hubs are
  eligible but must clear a higher fit bar to justify a relocation.
- Drop reqs requiring an *active* clearance (I'm eligible, not cleared);
  keep "must be able to obtain" — those are an advantage.

**5. Cheap rank** — embed surviving JDs, cosine against the matching resume, keep top ~20.

**6. LLM pick** — Claude reads full JDs + structured resume, scores fit 0–100,
applies the tier quota, writes one line on why each matched.

**7. Tailor** — extract JD keywords, diff against resume.
**Hard rule: only surface skills I actually have.** Permitted moves are reordering
bullets, renaming a true skill to the JD's phrasing (Verilog→"RTL design",
"continuous integration"→"CI/CD"), and promoting buried coursework.
Inventing a skill is a bug, not a feature.

**8. Render** — master resume lives as structured YAML; a template renders ATS-safe
PDF (no tables, no columns, no text boxes, no header/footer). Tailoring swaps strings
in the data, never in docx XML.

**9. Send + record** — Gmail API, then mark all 10 as shown so they never repeat.

---

## 3. Why "new" is redefined

Measured against the live repos on 2026-08-18:

| | Software | Hardware |
|---|---|---|
| Total rows | 600 | 456 |
| In my 5 metros | 67 | 19 |
| Survive new-grad title filter | 20 | 4 |

Four hardware roles — as a **standing total**, not a daily rate. There is no source
that yields 5 genuinely-new-grad hardware roles per day in these 5 metros. Peak season
(Aug–Oct) produces 2–5/day; spring is near zero.

So the DB retains every job ever seen and the email sends the 5 best **not yet shown**.
Week one drains a backlog of good matches; after that it tracks real inflow. Every
entry is stamped with its true age ("posted 3d ago") so freshness is never misrepresented.

---

## 4. Profile notes that drive matching

- **Strongest hardware angle: embedded / firmware.** C + MSP430 + bring-up experience
  bridges the IBM software internship. Weight these highest.
- **Digital/ASIC is a reach** at Tier 1 — coursework-level Verilog, no tapeout. Still
  worth surfacing at Tier 2 (HPE Durham ASIC Verification is a real fit).
- **Mechanical is weakest** — deprioritize unless the req is mechatronics/test.
- **Clearance eligibility is a differentiator.** Draper, MITRE, Raytheon, BAE, and the
  defense tier screen out most applicants on this alone. Weight it up.
- **Geography reality:** Boston is the hardware market (ADI, Draper, Teradyne, Raytheon,
  MITRE, BAE, Amazon Robotics, Nvidia Westford). RTP is solid (Wolfspeed, Qorvo,
  Infineon, Cisco, HPE, Lenovo). **NYC is nearly dead for hardware.** Charlotte skews
  finance-software plus Honeywell/Siemens Energy.
- Fall 2026 in-progress coursework (Design of Complex Digital Systems, Microprocessor
  Architecture) completes before start date — should read as complete on tailored
  resumes for Jan 2027 starts.

---

## 5. Stack

Python 3.11+ · `httpx` · SQLite · `anthropic` (Sonnet 5) · `pydantic` ·
`google-api-python-client` (Gmail) · WeasyPrint or python-docx (resume render) ·
GitHub Actions (schedule + secrets + PDF storage)

## 6. Repo layout

```
.github/workflows/daily.yml   cron 6:30 ET Mon–Fri
sources/                      greenhouse, lever, ashby, workday, github_repos, gmail_linkedin
companies.yaml                curated boards, tagged by track + tier + metro
resume/master_sw.yaml         structured software resume
resume/master_hw.yaml         structured hardware resume
resume/render.py              YAML -> ATS-safe PDF
pipeline/                     normalize, dedupe, filter, rank, pick, tailor
out/YYYY-MM-DD/               tailored PDFs, committed each run
state.db                      seen + shown jobs, committed each run
```

## 7. Open items

- [ ] GitHub repo URL to push to
- [ ] Anthropic API key
- [ ] Gmail API OAuth credentials
