"""Applied-tracking and the re-score escape hatch. No live API calls anywhere.

Two small features that both exist to stop the score-once/show-once design from
becoming a trap:

  * `run.py --applied <job-id>` records an application. Over months the real
    failure is not missing a role, it is re-applying to one already sent.
  * `python -m pipeline.score --rescore <job-id>` discards one cached score so
    it can be recomputed. Without it a bad score is permanent until the resume
    itself changes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import db as dbm, state as state_mod
from pipeline.models import Job
from pipeline.score import clear_score


def _job(name="X", url="https://example.com/1", track="software") -> Job:
    return Job(company=name, title="Software Engineer", location="Durham, NC",
               url=url, track=track)


def _scored(conn, job, score=80, resume_hash="H"):
    job.fit_score = score
    job.fit_rationale = "ok"
    dbm.save_scores(conn, [job], resume_hash)


# --- applied tracking ------------------------------------------------------


def test_mark_applied_stamps_the_job(tmp_path):
    with dbm.connect(tmp_path / "t.db") as conn:
        job = _job()
        dbm.upsert(conn, [job])
        out = dbm.mark_applied(conn, job.id)
        assert out is not None
        assert out.applied_at is not None
        assert dbm.get(conn, job.id).applied_at is not None


def test_mark_applied_on_an_unknown_id_returns_none(tmp_path):
    with dbm.connect(tmp_path / "t.db") as conn:
        assert dbm.mark_applied(conn, "does-not-exist") is None


def test_applied_jobs_are_never_shown_again(tmp_path):
    """The whole point of the feature."""
    with dbm.connect(tmp_path / "t.db") as conn:
        job = _job()
        dbm.upsert(conn, [job])
        _scored(conn, job)
        assert len(dbm.candidates(conn, "software", "H")) == 1

        dbm.mark_applied(conn, job.id)
        assert dbm.candidates(conn, "software", "H") == []


def test_applied_jobs_are_never_re_queued_for_scoring(tmp_path):
    """A resume edit must not resurrect an applied job into the scoring queue —
    that would spend tokens on a role he has already applied to."""
    with dbm.connect(tmp_path / "t.db") as conn:
        job = _job()
        dbm.upsert(conn, [job])
        _scored(conn, job, resume_hash="H_OLD")
        assert len(dbm.unscored(conn, "software", 10, "H_NEW")) == 1

        dbm.mark_applied(conn, job.id)
        assert dbm.unscored(conn, "software", 10, "H_NEW") == []
        assert dbm.count_stale_scores(conn, "H_NEW") == 0


def test_applying_also_marks_it_shown(tmp_path):
    """Applying to something the email has not surfaced yet must not leave it
    queued to arrive tomorrow as a fresh suggestion."""
    with dbm.connect(tmp_path / "t.db") as conn:
        job = _job()
        dbm.upsert(conn, [job])
        assert dbm.get(conn, job.id).shown_at is None
        dbm.mark_applied(conn, job.id)
        assert dbm.get(conn, job.id).shown_at is not None


def test_re_applying_keeps_the_original_date(tmp_path):
    """The first application is the one whose age matters when chasing a reply."""
    with dbm.connect(tmp_path / "t.db") as conn:
        job = _job()
        dbm.upsert(conn, [job])
        first = datetime(2026, 1, 5, tzinfo=timezone.utc)
        dbm.mark_applied(conn, job.id, when=first)
        dbm.mark_applied(conn, job.id, when=datetime(2026, 6, 9, tzinfo=timezone.utc))
        assert dbm.get(conn, job.id).applied_at == first


def test_applied_listing_is_most_recent_first(tmp_path):
    with dbm.connect(tmp_path / "t.db") as conn:
        old, new = _job("Old", "https://example.com/1"), _job("New", "https://example.com/2")
        dbm.upsert(conn, [old, new])
        dbm.mark_applied(conn, old.id, when=datetime(2026, 1, 1, tzinfo=timezone.utc))
        dbm.mark_applied(conn, new.id, when=datetime(2026, 2, 1, tzinfo=timezone.utc))
        assert [j.company for j in dbm.applied(conn)] == ["New", "Old"]


def test_stats_counts_applications_and_excludes_them_from_the_backlog(tmp_path):
    with dbm.connect(tmp_path / "t.db") as conn:
        a, b = _job("A", "https://example.com/1"), _job("B", "https://example.com/2")
        dbm.upsert(conn, [a, b])
        _scored(conn, a)
        _scored(conn, b)
        assert dbm.stats(conn)["unshown_scored"] == 2
        assert dbm.stats(conn)["applied"] == 0

        dbm.mark_applied(conn, a.id)
        stats = dbm.stats(conn)
        assert stats["applied"] == 1
        assert stats["unshown_scored"] == 1


def test_applied_survives_the_committed_ledger_roundtrip(tmp_path):
    """applied_at must persist in state/seen_jobs.json — it is the only store
    that survives a fresh CI checkout."""
    src, dst, ledger = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "seen.json"
    job = _job()
    when = datetime(2026, 3, 4, tzinfo=timezone.utc)
    with dbm.connect(src) as conn:
        dbm.upsert(conn, [job])
        dbm.mark_applied(conn, job.id, when=when)
        state_mod.dump(conn, ledger)

    payload = json.loads(ledger.read_text(encoding="utf-8"))
    assert "applied_at" in payload["jobs"][0]
    assert state_mod.summarize(ledger)["applied"] == 1

    with dbm.connect(dst) as conn:
        state_mod.load(conn, ledger)
        assert dbm.get(conn, job.id).applied_at == when
        # And it stays excluded after the reload.
        assert dbm.candidates(conn, "software", "H") == []


def test_a_ledger_written_before_this_feature_still_loads(tmp_path):
    """Backward compatibility: old files have no applied_at key at all."""
    ledger = tmp_path / "seen.json"
    job = _job()
    with dbm.connect(tmp_path / "a.db") as conn:
        dbm.upsert(conn, [job])
        state_mod.dump(conn, ledger)

    payload = json.loads(ledger.read_text(encoding="utf-8"))
    for record in payload["jobs"]:
        record.pop("applied_at")
    ledger.write_text(json.dumps(payload), encoding="utf-8")

    with dbm.connect(tmp_path / "b.db") as conn:
        assert state_mod.load(conn, ledger) == 1
        assert dbm.get(conn, job.id).applied_at is None


def test_migration_adds_applied_at_to_a_pre_existing_database(tmp_path):
    """The owner's state.db predates this column."""
    import sqlite3

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE jobs (
             id TEXT PRIMARY KEY, company TEXT NOT NULL, title TEXT NOT NULL,
             location TEXT NOT NULL DEFAULT '', url TEXT NOT NULL,
             ats TEXT NOT NULL DEFAULT 'other', description TEXT NOT NULL DEFAULT '',
             posted_at TEXT, first_seen_at TEXT NOT NULL,
             source TEXT NOT NULL DEFAULT '', track TEXT NOT NULL DEFAULT 'software',
             shown_at TEXT, tier INTEGER NOT NULL DEFAULT 2, metro TEXT,
             metro_class TEXT NOT NULL DEFAULT 'none',
             clearance_advantage INTEGER NOT NULL DEFAULT 0,
             fit_score INTEGER, fit_rationale TEXT NOT NULL DEFAULT '',
             scored_at TEXT, dedupe_key TEXT NOT NULL DEFAULT '');"""
    )
    conn.commit()
    conn.close()

    with dbm.connect(path) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        assert "applied_at" in columns and "resume_hash" in columns
        job = _job()
        dbm.upsert(conn, [job])
        assert dbm.mark_applied(conn, job.id) is not None


# --- rescore ---------------------------------------------------------------


def test_clear_score_makes_a_job_eligible_to_be_scored_again(tmp_path):
    with dbm.connect(tmp_path / "t.db") as conn:
        job = _job()
        dbm.upsert(conn, [job])
        _scored(conn, job, score=30)
        assert len(dbm.candidates(conn, "software", "H")) == 1
        assert dbm.unscored(conn, "software", 10, "H") == []

        assert clear_score(conn, job.id) is True

        refreshed = dbm.get(conn, job.id)
        assert refreshed.fit_score is None
        assert refreshed.fit_rationale == ""
        assert refreshed.scored_at is None
        # It has left the candidate pool and re-entered the scoring queue.
        assert dbm.candidates(conn, "software", "H") == []
        assert len(dbm.unscored(conn, "software", 10, "H")) == 1


def test_clear_score_on_an_unknown_id_reports_failure(tmp_path):
    with dbm.connect(tmp_path / "t.db") as conn:
        assert clear_score(conn, "nope") is False


def test_clear_score_does_not_resurrect_an_applied_job(tmp_path):
    """Re-scoring is for fixing a bad score, not for re-showing a done deal."""
    with dbm.connect(tmp_path / "t.db") as conn:
        job = _job()
        dbm.upsert(conn, [job])
        _scored(conn, job)
        dbm.mark_applied(conn, job.id)
        clear_score(conn, job.id)
        assert dbm.unscored(conn, "software", 10, "H") == []
        assert dbm.candidates(conn, "software", "H") == []


def test_rescore_cli_requires_a_job_id():
    from pipeline import score as score_mod

    with pytest.raises(SystemExit):
        score_mod.main([])


def test_rescore_cli_exits_cleanly_on_an_unknown_job(tmp_path, capsys):
    """Must not construct an LLM client — and therefore cannot spend — before
    it has established that the job even exists."""
    from pipeline import score as score_mod

    code = score_mod.main(
        ["--rescore", "no-such-id", "--db", str(tmp_path / "t.db"),
         "--state", str(tmp_path / "s.json")]
    )
    assert code == 2
    assert "no job with id" in capsys.readouterr().err


def test_rescore_dry_run_spends_nothing(tmp_path, capsys):
    from pipeline import score as score_mod

    job = _job()
    job.description = "<p>We need a new grad software engineer in Durham.</p>"
    db_path = tmp_path / "t.db"
    with dbm.connect(db_path) as conn:
        dbm.upsert(conn, [job])
        _scored(conn, job, score=20)

    code = score_mod.main(
        ["--rescore", job.id, "--dry-run", "--db", str(db_path),
         "--state", str(tmp_path / "s.json")]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "no API call made" in out

    # And the existing score is untouched: --dry-run must not clear anything.
    with dbm.connect(db_path) as conn:
        assert dbm.get(conn, job.id).fit_score == 20
