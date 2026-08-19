"""Score-once ingest stage — the ONLY LLM call in a normal daily run.

A posting's fit against a fixed resume does not change between days, so it is
scored exactly once, when it first survives the hard filters, and the result is
persisted (`fit_score`, `fit_rationale`, `scored_at`). The daily pick then reads
scores out of SQLite and calls nothing.

Cost shape:
  * Jobs are batched (config: limits.score_batch_size) so one request covers
    ~8 postings and the cached resume prefix is amortized across them.
  * The stable prefix — instructions + resume summary — carries the cache
    breakpoint; the volatile job list goes after it.
  * Every JD is compressed by pipeline.jd.compress_jd() first.
"""

from __future__ import annotations

import json
from typing import Iterable, Sequence

from .jd import compress_jd
from .llm import BudgetExceeded, LlmClient, Usage
from .models import Job
from .resumes import skill_groups

SYSTEM_TEMPLATE = """You score new-grad engineering job postings against one candidate's resume.

CANDIDATE ({track} resume)
{resume_summary}

CONTEXT
- Graduating December 2026; targeting new-grad / entry-level roles starting 2027.
- US citizen, eligible to obtain a security clearance (an advantage at defense employers).
- Strongest hardware angle is embedded / firmware (C, MSP430, board bring-up).
- Digital/ASIC is a reach: coursework-level Verilog, no tapeout experience.
- Mechanical is the weakest area; deprioritize unless the role is mechatronics or test.

TASK
For each posting, output:
  id        the posting id, copied exactly
  score     0-100 fit. Calibration:
              80-100 strong match, he should apply today
              60-79  good match, clearly worth applying
              40-59  plausible but a stretch on skills or focus
              0-39   poor match
  rationale ONE sentence, under 25 words, naming the concrete overlap or the
            concrete gap. No filler, no restating the job title.

Score honestly. A generous score wastes one of five daily slots on a bad match."""

SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "integer"},
                    "rationale": {"type": "string"},
                },
                "required": ["id", "score", "rationale"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scores"],
    "additionalProperties": False,
}


def resume_summary(resume: dict) -> str:
    """A compact, stable rendering of the resume for the cached prefix.

    Deterministic: same input -> same bytes, so the cache actually hits.
    """
    lines: list[str] = []
    edu = resume.get("education") or {}
    lines.append(
        f"Education: {edu.get('degree','')}, {edu.get('school','')} "
        f"({edu.get('dates','')})"
    )
    if edu.get("coursework"):
        lines.append("Coursework: " + ", ".join(edu["coursework"]))
    if edu.get("in_progress"):
        lines.append(
            f"In progress ({edu.get('in_progress_term','')}, completes "
            f"{edu.get('in_progress_completes','')}): "
            + ", ".join(edu["in_progress"])
        )
    for label, items in skill_groups(resume):
        lines.append(f"{label}: " + ", ".join(str(v) for v in items))
    for entry in resume.get("experience") or []:
        head = f"{entry.get('title','')} @ {entry.get('company','')} ({entry.get('dates','')})"
        lines.append(head)
        for sub in entry.get("subsections") or []:
            lines.append(f"  - {sub.get('name','')}")
            for bullet in (sub.get("bullets") or [])[:3]:
                lines.append(f"    * {bullet}")
        for bullet in (entry.get("bullets") or [])[:2]:
            lines.append(f"  * {bullet}")
    for project in resume.get("projects") or []:
        lines.append(f"Project: {project.get('title','')} — {project.get('stack','')}")
        for bullet in (project.get("bullets") or [])[:3]:
            lines.append(f"  * {bullet}")
    return "\n".join(lines)


def job_payload(job: Job, jd_max_chars: int) -> dict:
    return {
        "id": job.id,
        "company": job.company,
        "title": job.title,
        "location": job.location,
        "metro": job.metro or "",
        "tier": job.tier,
        "clearance_advantage": job.clearance_advantage,
        "jd": compress_jd(job.description, jd_max_chars),
    }


def build_user_content(jobs: Sequence[Job], jd_max_chars: int) -> str:
    payload = [job_payload(j, jd_max_chars) for j in jobs]
    return "Score these postings:\n" + json.dumps(payload, ensure_ascii=False)


def score_jobs(
    llm: LlmClient,
    jobs: Sequence[Job],
    resume: dict,
    track: str,
    cfg: dict,
) -> tuple[list[Job], Usage, list[str]]:
    """Score `jobs` in batches. Returns (scored_jobs, usage, warnings).

    A BudgetExceeded stops scoring and returns whatever was scored so far —
    the run still sends an email from the jobs already in the database.
    """
    warnings: list[str] = []
    if not jobs:
        return [], Usage(), warnings

    limits = cfg["limits"]
    batch_size = int(limits.get("score_batch_size", 8))
    jd_max_chars = int(limits.get("jd_max_chars", 1600))
    max_tokens = int(cfg["model"].get("max_tokens_score", 1200))

    system_cached = SYSTEM_TEMPLATE.format(
        track=track, resume_summary=resume_summary(resume)
    )

    by_id = {j.id: j for j in jobs}
    scored: list[Job] = []
    total = Usage()

    for start in range(0, len(jobs), batch_size):
        batch = list(jobs[start : start + batch_size])
        label = f"score:{track}:{start // batch_size}"
        try:
            data, usage = llm.complete_json(
                system_cached=system_cached,
                user_content=build_user_content(batch, jd_max_chars),
                schema=SCHEMA,
                max_tokens=max_tokens,
                label=label,
            )
        except BudgetExceeded as exc:
            warnings.append(str(exc))
            break
        except Exception as exc:  # noqa: BLE001 — one bad batch must not kill the run
            warnings.append(f"{label} failed: {exc}")
            continue

        total = total + usage
        returned = {row["id"] for row in data.get("scores", [])}
        for row in data.get("scores", []):
            job = by_id.get(row["id"])
            if job is None:
                warnings.append(f"{label}: model returned unknown id {row['id']!r}")
                continue
            job.fit_score = max(0, min(100, int(row["score"])))
            job.fit_rationale = str(row["rationale"]).strip()[:300]
            scored.append(job)
        missing = {j.id for j in batch} - returned
        if missing:
            warnings.append(f"{label}: {len(missing)} postings came back unscored")

    return scored, total, warnings


def clear_score(conn, job_id: str) -> bool:
    """Forget a job's score so it is eligible to be computed again.

    Score-once is the right default — a posting's fit to a fixed resume does not
    change between days — but it makes a bad score permanent until the resume
    itself changes. This is the escape hatch for the case where the model simply
    got one wrong.
    """
    cur = conn.execute(
        "UPDATE jobs SET fit_score = NULL, fit_rationale = '', "
        "scored_at = NULL, resume_hash = '' WHERE id = ?",
        (job_id,),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# CLI — on-demand re-scoring of a single posting.
#
# Lives here rather than on run.py for the same reason pipeline/tailor.py has
# its own entry point: both are per-job, on-demand operations that SPEND money,
# and neither belongs in the unattended daily path. run.py stays the thing cron
# calls; these are the things the owner calls by hand.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from . import db
    from .config import load_config, repo_path
    from .fingerprint import score_fingerprint
    from .jd import compress_jd
    from .llm import BudgetExceeded, LlmClient, api_key_present

    parser = argparse.ArgumentParser(
        prog="python -m pipeline.score",
        description="Recompute the fit score for one posting (the job id is "
        "printed in the email under each match).",
    )
    parser.add_argument("--rescore", metavar="JOB_ID", required=True,
                        help="job id whose cached score should be discarded and recomputed")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be sent; makes no API call and spends nothing")
    parser.add_argument("--db", help="override the local working database path")
    parser.add_argument("--state", help="override the committed JSON state path")
    args = parser.parse_args(argv)

    cfg = load_config()
    db_path = repo_path(args.db or cfg["paths"]["db"])
    state_path = repo_path(args.state or cfg["paths"]["state"])

    sys.path.insert(0, str(repo_path()))
    from resume.render import load_resume  # noqa: PLC0415

    with db.connect(db_path) as conn:
        from . import state as state_mod

        state_mod.load(conn, state_path)
        job = db.get(conn, args.rescore)
        if job is None:
            print(f"error: no job with id {args.rescore!r} in the database", file=sys.stderr)
            return 2

        master_path = (
            cfg["paths"]["resume_hw"] if job.track == "hardware" else cfg["paths"]["resume_sw"]
        )
        resume = load_resume(repo_path(master_path))

        print(f"Job     : {job.company} — {job.title}")
        print(f"Track   : {job.track}  ({job.location})")
        print(f"Link    : {job.url}")
        print(f"Current : {job.fit_score} — {job.fit_rationale or '(none)'}")

        # A score is only meaningful against a body. One left in the database
        # days ago has none — the ledger deliberately does not persist them —
        # so re-fetch it rather than silently re-scoring an empty posting.
        if not job.description:
            from sources import hydrate  # noqa: PLC0415
            from sources.base import make_client  # noqa: PLC0415

            client = make_client(cfg)
            try:
                ok = hydrate.hydrate_one(client, job)
            finally:
                client.close()
            print(f"Body    : re-fetched ({len(job.description)} chars)" if ok
                  else "Body    : could not be re-fetched; scoring on title and location alone")
            if ok:
                db.fill_descriptions(conn, [job])

        if args.dry_run:
            jd = compress_jd(job.description, int(cfg["limits"]["jd_max_chars"]))
            print(f"\n--dry-run: no API call made, nothing spent.")
            print(f"compressed JD: {len(jd)} chars (from {len(job.description)})")
            return 0

        if not api_key_present():
            print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
            return 2

        if not clear_score(conn, job.id):
            print(f"error: could not clear the score for {job.id!r}", file=sys.stderr)
            return 2
        job.fit_score, job.fit_rationale, job.scored_at = None, "", None

        llm = LlmClient(cfg, run_id=f"rescore-{job.id}")
        try:
            scored, usage, warnings = score_jobs(llm, [job], resume, job.track, cfg)
        except BudgetExceeded as exc:
            print(f"BUDGET STOP: {exc}", file=sys.stderr)
            return 3
        for warning in warnings:
            print(f"  ! {warning}")
        if not scored:
            print("the model returned no score; the posting is left unscored", file=sys.stderr)
            return 3

        # Must be the SAME fingerprint the daily run computes, or the run
        # would read this fresh score as stale and immediately pay to
        # redo it — turning the escape hatch into a recurring bill.
        db.save_scores(conn, scored, score_fingerprint(resume, cfg))
        print(f"\nNew     : {scored[0].fit_score} — {scored[0].fit_rationale}")
        print(f"Tokens  : {usage.input_tokens} in / {usage.output_tokens} out")
        print(f"Cost    : ${llm.ledger.spent_usd:.4f} ({llm.ledger.calls} call)")
        written = state_mod.dump(conn, state_path)
        print(f"Ledger  : wrote {written} records to {state_path.name}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
