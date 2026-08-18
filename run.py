#!/usr/bin/env python3
"""Daily job agent — orchestrator.

    fetch -> normalize -> dedupe -> hard-filter -> hydrate -> re-filter
          -> BM25 pre-rank -> score NEW jobs once (the only LLM stage)
          -> pick 5+5 locally -> render email -> send -> mark shown

Everything up to and including the pre-rank is free. The single LLM stage is
budget-gated and only ever sees postings that have never been scored before.

    python run.py --dry-run        # full pipeline, writes email HTML, sends nothing
    python run.py --no-llm         # free stages only; no API key needed at all
    python run.py                  # live run: scores new jobs and sends the email
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from pipeline import (
    db,
    email as email_mod,
    fetch as fetch_mod,
    filters,
    pick as pick_mod,
    state as state_mod,
)
from pipeline.config import load_config, repo_path
from pipeline.fingerprint import resume_hash
from pipeline.llm import BudgetExceeded, LlmClient, api_key_present
from pipeline.models import Job
from pipeline.rank import prerank
from pipeline.score import score_jobs
from pipeline.track import infer_track
from sources import hydrate
from sources.base import make_client

log = logging.getLogger("run")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )


def _hr(title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="write the email to out/<date>/email.html instead of sending")
    parser.add_argument("--no-llm", action="store_true", help="skip the scoring stage entirely (no API key required)")
    parser.add_argument("--no-mark-shown", action="store_true", help="do not mark the selected jobs as shown")
    parser.add_argument("--skip-repos", action="store_true", help="skip the GitHub aggregator READMEs")
    parser.add_argument("--skip-boards", action="store_true", help="skip the ATS boards")
    parser.add_argument("--max-scores", type=int, help="override limits.max_new_scores_per_run")
    parser.add_argument("--db", help="override the local working database path")
    parser.add_argument("--state", help="override the committed JSON state path")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    started = datetime.now(timezone.utc)
    cfg = load_config()
    limits = cfg["limits"]

    db_path = repo_path(args.db or cfg["paths"]["db"])
    state_path = repo_path(args.state or cfg["paths"]["state"])
    out_dir = repo_path(cfg["paths"]["out_dir"], date.today().isoformat())

    sys.path.insert(0, str(repo_path()))
    from resume.render import load_resume  # noqa: PLC0415

    resume_sw = load_resume(repo_path(cfg["paths"]["resume_sw"]))
    resume_hw = load_resume(repo_path(cfg["paths"]["resume_hw"]))
    resumes = {"software": resume_sw, "hardware": resume_hw}
    hashes = {"software": resume_hash(resume_sw), "hardware": resume_hash(resume_hw)}

    run_notes: list[str] = []
    est_cost = 0.0
    scored_count = 0
    rescored_count = 0

    with db.connect(db_path) as conn:
        # ---------------- hydrate from the committed JSON ledger ----------------
        # state.db is the local working store and is gitignored; the committed
        # state is state/seen_jobs.json. On a fresh CI checkout the DB is empty,
        # so without this every run would re-send everything.
        loaded = state_mod.load(conn, state_path)
        if loaded:
            print(f"loaded {loaded} records from {state_path.name}")
        before = db.stats(conn)

        stale = {t: db.count_stale_scores(conn, hashes[t]) for t in hashes}
        for track, count in stale.items():
            if count:
                print(
                    f"NOTE: {count} {track} score(s) were computed against an older "
                    f"resume and will be re-scored (budget permitting)"
                )

        # ---------------- fetch / normalize / dedupe ----------------
        _hr("FETCH")
        jobs, report = fetch_mod.fetch_all(
            cfg,
            known_ids=db.known_ids(conn),
            known_keys=db.known_dedupe_keys(conn),
            skip_repos=args.skip_repos,
            skip_boards=args.skip_boards,
        )
        print(f"boards attempted      : {report.boards_attempted} (failed: {report.boards_failed})")
        print(f"postings from boards  : {report.from_boards}")
        print(f"postings from repos   : {report.from_repos}")
        print(f"raw total             : {report.raw_total}")
        print(f"after URL dedupe      : {report.after_url_dedupe}  (-{report.url_duplicates})")
        print(f"after fuzzy dedupe    : {report.after_fuzzy_dedupe}  (-{report.fuzzy_duplicates})")
        print(f"new since last run    : {len(jobs)}")
        for label, err in report.failures[:5]:
            print(f"  ! {label}: {err}")

        # ---------------- hard filter (titles + location) ----------------
        _hr("HARD FILTER")
        stage_counts: dict[str, int] = {}
        remaining = jobs
        for name, fn in (
            ("seniority", lambda j: filters.check_title_seniority(j.title)),
            ("discipline", lambda j: filters.check_title_discipline(j.title)),
            ("level", lambda j: filters.check_title_level(j.title)),
            ("location", lambda j: filters.check_location(j.location)),
            ("stale", lambda j: filters.check_age(j.age_days, limits.get("max_posting_age_days"))),
        ):
            kept = []
            rejected = 0
            for job in remaining:
                result = fn(job)
                if result.passed:
                    if name == "location":
                        job.metro_class = result.metro_class
                        job.metro = result.metro
                    kept.append(job)
                else:
                    rejected += 1
            stage_counts[name] = rejected
            remaining = kept
            print(f"after {name:<11}: {len(remaining):6d}   (rejected {rejected})")

        # ---------------- hydrate survivors only ----------------
        _hr("HYDRATE (survivors only)")
        client = make_client(cfg)
        try:
            attempted, ok = hydrate.hydrate_all(client, remaining, max_workers=cfg["fetch"]["max_concurrency"])
        finally:
            client.close()
        print(f"needed hydration      : {attempted}")
        print(f"hydrated successfully : {ok}")

        # ---------------- re-filter with descriptions ----------------
        _hr("RE-FILTER (with descriptions)")
        survivors: list[Job] = []
        re_rejected = 0
        for job in remaining:
            result = filters.evaluate(job.title, job.location, job.description)
            if result.passed:
                job.metro_class = result.metro_class
                job.metro = result.metro
                job.clearance_advantage = result.clearance_advantage
                job.track = infer_track(job.title, job.description, job.track)
                survivors.append(job)
            else:
                re_rejected += 1
                stage_counts[result.stage] = stage_counts.get(result.stage, 0) + 1
        print(f"rejected on description/title: {re_rejected}")

        # Hydration rewrites truncated aggregator titles and locations, which
        # changes a job's fuzzy dedupe key AFTER the pre-fetch dedupe already
        # ran. Without this second pass, "Analog/Mixed-Signal Circuit Design
        # En..." and its hydrated full title become two separate rows, and the
        # owner sees the same job twice on different days.
        known_ids_now = db.known_ids(conn)
        known_keys_now = db.known_dedupe_keys(conn)
        deduped: list[Job] = []
        post_hydration_dupes = 0
        for job in survivors:
            key = "|".join(job.dedupe_key)
            if job.id in known_ids_now or (key.strip("|") and key in known_keys_now):
                post_hydration_dupes += 1
                continue
            known_ids_now.add(job.id)
            if key.strip("|"):
                known_keys_now.add(key)
            deduped.append(job)
        if post_hydration_dupes:
            print(f"post-hydration duplicates dropped: {post_hydration_dupes}")
        survivors = deduped

        print(f"SURVIVORS             : {len(survivors)}")
        by_track = {t: sum(1 for j in survivors if j.track == t) for t in ("software", "hardware")}
        by_class = {}
        for job in survivors:
            by_class[job.metro_class] = by_class.get(job.metro_class, 0) + 1
        print(f"  by track            : {by_track}")
        print(f"  by metro class      : {by_class}")
        primary_hw = [j for j in survivors if j.metro_class == "primary" and j.track == "hardware"]
        print(f"  hardware in PRIMARY metros: {len(primary_hw)}")

        inserted = db.upsert(conn, survivors)
        print(f"inserted into state.db: {inserted}")

        # ---------------- score new jobs (the only LLM stage) ----------------
        _hr("SCORE (new jobs only)")
        if args.no_llm:
            print("--no-llm: scoring skipped")
            run_notes.append("Scoring was skipped for this run (--no-llm).")
        elif not api_key_present():
            print("ANTHROPIC_API_KEY not set — scoring skipped, email will use existing scores")
            run_notes.append("ANTHROPIC_API_KEY was not set, so no new postings were scored this run.")
        else:
            cap = args.max_scores if args.max_scores is not None else int(limits["max_new_scores_per_run"])
            llm = LlmClient(cfg, run_id=started.isoformat())
            print(f"budget ceiling        : ${llm.ledger.ceiling_usd:.4f}/run")
            for track in ("software", "hardware"):
                pool = db.unscored(
                    conn, track, limit=int(limits["max_jobs_to_prerank"]),
                    resume_hash=hashes[track],
                )
                if not pool:
                    print(f"{track:9}: nothing new to score")
                    continue
                # A resume edit can invalidate the whole pool at once. unscored()
                # already orders new-first then best-known-score-first, so a
                # partial re-score under the ceiling does the most valuable work
                # and the rest carries to later runs.
                was_scored = sum(1 for j in pool if j.scored_at is not None)
                ranked = prerank(pool, resumes[track], keep=cap)
                rescoring = sum(1 for j in ranked if j.scored_at is not None)
                detail = f" ({rescoring} re-score after resume edit)" if rescoring else ""
                print(
                    f"{track:9}: {len(pool)} need scoring{f' ({was_scored} stale)' if was_scored else ''}"
                    f" -> sending best {len(ranked)}{detail}"
                )
                try:
                    scored, usage, warnings = score_jobs(llm, ranked, resumes[track], track, cfg)
                except BudgetExceeded as exc:
                    print(f"  BUDGET STOP: {exc}")
                    run_notes.append("Scoring stopped early: per-run budget ceiling reached.")
                    break
                db.save_scores(conn, scored, hashes[track])
                scored_count += len(scored)
                rescored_count += sum(1 for j in scored if j.scored_at is not None)
                print(f"  scored {len(scored)}  |  {usage.input_tokens} in / {usage.output_tokens} out"
                      f"  |  cache r/w {usage.cache_read_input_tokens}/{usage.cache_creation_input_tokens}")
                for warning in warnings:
                    print(f"  ! {warning}")
            est_cost = llm.ledger.spent_usd
            print(f"run cost              : ${est_cost:.4f} over {llm.ledger.calls} call(s)")

        # ---------------- pick ----------------
        _hr("PICK")
        sw_candidates = db.candidates(conn, "software", hashes["software"])
        hw_candidates = db.candidates(conn, "hardware", hashes["hardware"])
        print(f"unshown scored candidates: {len(sw_candidates)} software / {len(hw_candidates)} hardware")
        sw_sel, hw_sel = pick_mod.pick(sw_candidates, hw_candidates, cfg)
        print(f"selected: {len(sw_sel.jobs)} software / {len(hw_sel.jobs)} hardware")
        for sel in (sw_sel, hw_sel):
            for note in sel.notes:
                print(f"  ! {note}")

        # Descriptions are not persisted in the committed ledger, so a job
        # scored days ago arrives with an empty body. Re-hydrate just the ten
        # being emailed — free HTTP, and only the keyword diff depends on it.
        picked = sw_sel.jobs + hw_sel.jobs
        stale_bodies = [j for j in picked if not j.description and hydrate.hydratable(j)]
        if stale_bodies:
            client = make_client(cfg)
            try:
                _att, ok = hydrate.hydrate_all(client, stale_bodies, max_workers=6)
            finally:
                client.close()
            db.fill_descriptions(conn, stale_bodies)
            print(f"re-hydrated {ok}/{len(stale_bodies)} descriptions for the email")

        # ---------------- email ----------------
        _hr("EMAIL")
        rendered = email_mod.render_email(
            sw_sel, hw_sel, resume_sw, resume_hw, cfg, run_notes=run_notes
        )
        print(f"subject: {rendered.subject}")

        if not rendered.job_ids:
            print("nothing to send — no matches selected")
        elif args.dry_run:
            path = email_mod.write_dry_run(rendered, out_dir)
            print(f"DRY RUN — wrote {path}")
        else:
            email_mod.send(rendered, cfg["owner"]["email"])
            print(f"sent to {cfg['owner']['email']}")

        if rendered.job_ids and not args.dry_run and not args.no_mark_shown:
            marked = db.mark_shown(conn, rendered.job_ids)
            print(f"marked {marked} jobs as shown")
        elif args.dry_run:
            print("dry run — jobs NOT marked as shown, so the next run sees them again")

        db.record_run(
            conn,
            started,
            fetched=report.raw_total,
            new_jobs=len(jobs),
            survivors=len(survivors),
            scored=scored_count,
            emailed=len(rendered.job_ids),
            est_cost_usd=est_cost,
            notes="; ".join(run_notes),
        )

        after = db.stats(conn)
        _hr("STATE")
        print(f"jobs in db  : {before['total']} -> {after['total']}")
        print(f"scored      : {before['scored']} -> {after['scored']}")
        print(f"shown       : {before['shown']} -> {after['shown']}")
        print(f"backlog     : {after['unshown_scored']} scored and not yet shown")

        written = state_mod.dump(conn, state_path)
        print(f"wrote {written} records to {state_path.relative_to(repo_path())}")

        # ---------------- machine-readable run summary ----------------
        # The workflow puts this in the GitHub Actions job summary. It must make
        # "zero matches today" and "the job never ran" impossible to confuse.
        n_sw, n_hw = len(sw_sel.jobs), len(hw_sel.jobs)
        outcome = (
            f"Sent {n_sw} software + {n_hw} hardware."
            if rendered.job_ids and not args.dry_run
            else f"Rendered {n_sw} software + {n_hw} hardware (dry run, not sent)."
            if rendered.job_ids
            else "ZERO MATCHES — the pipeline ran to completion and found nothing to send."
        )
        summary = "\n".join(
            [
                f"# Job agent run — {date.today().isoformat()}",
                "",
                f"**{outcome}**",
                "",
                "| metric | value |",
                "|---|---|",
                f"| boards fetched | {report.boards_attempted} ({report.boards_failed} failed) |",
                f"| postings pulled | {report.raw_total} |",
                f"| new after dedupe | {len(jobs)} |",
                f"| survived hard filters | {len(survivors)} |",
                f"| newly scored | {scored_count}{f' ({rescored_count} re-scored after a resume edit)' if rescored_count else ''} |",
                f"| software selected | {n_sw} |",
                f"| hardware selected | {n_hw} |",
                f"| estimated cost | ${est_cost:.4f} |",
                f"| backlog (scored, unshown) | {after['unshown_scored']} |",
                "",
            ]
            + [f"- note: {n}" for n in run_notes]
            + [f"- note: {n}" for sel in (sw_sel, hw_sel) for n in sel.notes]
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "summary.md").write_text(summary + "\n", encoding="utf-8")
        print(f"\n{outcome}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
