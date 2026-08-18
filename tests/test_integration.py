"""End-to-end integration: score -> persist -> pick -> render email.

Exercises the whole post-filter half of the pipeline against a stubbed model,
so the wiring is proven without spending anything.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline import db, email as email_mod, pick as pick_mod, state as state_mod
from pipeline.fingerprint import resume_hash
from pipeline.llm import LlmClient
from pipeline.models import Job
from pipeline.score import score_jobs

from .conftest import StubAnthropic, StubResponse


def _make_jobs(n: int, track: str, tier_pattern=(1, 2, 2, 2, 2)) -> list[Job]:
    jobs = []
    for i in range(n):
        job = Job(
            company=f"{'Big' if tier_pattern[i % len(tier_pattern)] == 1 else 'Mid'}Co{i}",
            title="Embedded Software Engineer" if track == "hardware" else "Software Engineer",
            location="Durham, NC" if i % 2 else "Boston, MA",
            url=f"https://example.com/{track}/{i}",
            description=(
                "Requirements\n"
                "- BS in Computer Engineering\n"
                "- Experience with C, Python, and Git\n"
                "- Familiarity with Verilog and FPGA tools\n"
                "Benefits\n- 401(k) matching and dental insurance\n"
            ),
            track=track,
        )
        job.tier = tier_pattern[i % len(tier_pattern)]
        job.metro_class = "primary"
        job.metro = "RTP/Raleigh-Durham" if i % 2 else "Boston"
        jobs.append(job)
    return jobs


def test_full_post_filter_pipeline(tmp_path, cfg, resume_sw, resume_hw):
    db_path = tmp_path / "state.db"
    ledger = tmp_path / "seen.json"
    out_dir = tmp_path / "out"

    sw_jobs = _make_jobs(8, "software")
    hw_jobs = _make_jobs(8, "hardware")
    hashes = {"software": resume_hash(resume_sw), "hardware": resume_hash(resume_hw)}

    with db.connect(db_path) as conn:
        db.upsert(conn, sw_jobs + hw_jobs)

        for track, jobs, resume in (
            ("software", sw_jobs, resume_sw),
            ("hardware", hw_jobs, resume_hw),
        ):
            pool = db.unscored(conn, track, 40, hashes[track])
            assert len(pool) == 8
            payload = {
                "scores": [
                    {"id": j.id, "score": 90 - idx * 3, "rationale": f"match {idx}"}
                    for idx, j in enumerate(pool)
                ]
            }
            stub = StubAnthropic([StubResponse(payload), StubResponse({"scores": []})])
            llm = LlmClient(cfg, client=stub)
            scored, _usage, warnings = score_jobs(llm, pool, resume, track, cfg)
            assert warnings == []
            db.save_scores(conn, scored, hashes[track])
            assert llm.ledger.spent_usd < cfg["budget"]["max_usd_per_run"]

        sw_c = db.candidates(conn, "software", hashes["software"])
        hw_c = db.candidates(conn, "hardware", hashes["hardware"])
        assert len(sw_c) == 8 and len(hw_c) == 8

        sw_sel, hw_sel = pick_mod.pick(sw_c, hw_c, cfg)
        assert len(sw_sel.jobs) == 5 and len(hw_sel.jobs) == 5

        rendered = email_mod.render_email(sw_sel, hw_sel, resume_sw, resume_hw, cfg)
        assert len(rendered.job_ids) == 10

        # Every required element from the brief must be present.
        for job in sw_sel.jobs + hw_sel.jobs:
            assert job.company in rendered.html
            assert job.title in rendered.html
            assert job.url in rendered.html
            assert job.fit_rationale in rendered.html
        assert "posted" in rendered.html or "date unknown" in rendered.html
        assert "ATS keywords" in rendered.html
        assert "Apply" in rendered.html

        path = email_mod.write_dry_run(rendered, out_dir)
        assert path.exists() and path.stat().st_size > 2000

        db.mark_shown(conn, rendered.job_ids)
        # 8 scored, 5 shown -> 3 stay in the backlog for tomorrow, and none of
        # today's ten can come back.
        remaining = db.candidates(conn, "software", hashes["software"])
        assert len(remaining) == 3
        assert not (set(j.id for j in remaining) & set(rendered.job_ids))

        state_mod.dump(conn, ledger)

    # A second process starting from the committed ledger must not re-send.
    with db.connect(tmp_path / "fresh.db") as conn:
        state_mod.load(conn, ledger)
        fresh_ids = {j.id for j in db.candidates(conn, "software", hashes["software"])}
        assert not (fresh_ids & set(rendered.job_ids)), "shown jobs must not reappear"
        assert db.stats(conn)["shown"] == 10


def test_resume_edit_forces_rescore_then_recovers(tmp_path, cfg, resume_sw):
    """A resume edit invalidates scores; the next run re-scores and recovers."""
    import copy

    db_path = tmp_path / "state.db"
    jobs = _make_jobs(4, "software")
    old_hash = resume_hash(resume_sw)

    with db.connect(db_path) as conn:
        db.upsert(conn, jobs)
        pool = db.unscored(conn, "software", 40, old_hash)
        payload = {"scores": [{"id": j.id, "score": 80, "rationale": "ok"} for j in pool]}
        llm = LlmClient(cfg, client=StubAnthropic([StubResponse(payload)]))
        scored, _u, _w = score_jobs(llm, pool, resume_sw, "software", cfg)
        db.save_scores(conn, scored, old_hash)
        assert len(db.candidates(conn, "software", old_hash)) == 4

        edited = copy.deepcopy(resume_sw)
        edited["skills"][0]["items"].append("Rust")
        new_hash = resume_hash(edited)
        assert new_hash != old_hash

        # Stale: out of the candidate pool, back in the scoring queue.
        assert db.candidates(conn, "software", new_hash) == []
        assert db.count_stale_scores(conn, new_hash) == 4

        requeued = db.unscored(conn, "software", 40, new_hash)
        payload2 = {"scores": [{"id": j.id, "score": 75, "rationale": "re"} for j in requeued]}
        llm2 = LlmClient(cfg, client=StubAnthropic([StubResponse(payload2)]))
        rescored, _u, _w = score_jobs(llm2, requeued, edited, "software", cfg)
        db.save_scores(conn, rescored, new_hash)

        assert len(db.candidates(conn, "software", new_hash)) == 4
        assert db.count_stale_scores(conn, new_hash) == 0


def test_partial_rescore_carries_the_remainder_forward(tmp_path, cfg, resume_sw):
    """If a resume edit invalidates more than the budget allows, the best-known
    jobs are re-scored first and the rest wait — never an abort, never a blowout."""
    db_path = tmp_path / "state.db"
    jobs = _make_jobs(8, "software")
    old_hash, new_hash = "OLD", "NEW"

    with db.connect(db_path) as conn:
        db.upsert(conn, jobs)
        for idx, job in enumerate(jobs):
            job.fit_score = 10 * idx        # job 7 is the best-known
            job.fit_rationale = "prior"
        db.save_scores(conn, jobs, old_hash)

        queue = db.unscored(conn, "software", limit=3, resume_hash=new_hash)
        assert len(queue) == 3
        assert [j.fit_score for j in queue] == [70, 60, 50], "best-known first"

        payload = {"scores": [{"id": j.id, "score": 88, "rationale": "x"} for j in queue]}
        llm = LlmClient(cfg, client=StubAnthropic([StubResponse(payload)]))
        scored, _u, _w = score_jobs(llm, queue, resume_sw, "software", cfg)
        db.save_scores(conn, scored, new_hash)

        assert db.count_stale_scores(conn, new_hash) == 5   # carried forward
        assert len(db.candidates(conn, "software", new_hash)) == 3
