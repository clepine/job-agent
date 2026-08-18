"""SQLite state. Committed to the repo each run (PLAN.md §6).

Two jobs:
  1. Dedupe before any spend — a posting seen yesterday costs nothing today.
  2. Hold the score-once result. `fit_score` / `fit_rationale` / `scored_at` are
     written the first time a job survives the hard filters and are never
     recomputed: a posting's fit to a fixed resume does not change between days,
     so re-scoring it is pure waste.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,      -- canonical-URL hash
    company           TEXT NOT NULL,
    title             TEXT NOT NULL,
    location          TEXT NOT NULL DEFAULT '',
    url               TEXT NOT NULL,
    ats               TEXT NOT NULL DEFAULT 'other',
    description       TEXT NOT NULL DEFAULT '',
    posted_at         TEXT,
    first_seen_at     TEXT NOT NULL,
    source            TEXT NOT NULL DEFAULT '',
    track             TEXT NOT NULL DEFAULT 'software',
    shown_at          TEXT,
    tier              INTEGER NOT NULL DEFAULT 2,
    metro             TEXT,
    metro_class       TEXT NOT NULL DEFAULT 'none',
    clearance_advantage INTEGER NOT NULL DEFAULT 0,
    fit_score         INTEGER,
    fit_rationale     TEXT NOT NULL DEFAULT '',
    scored_at         TEXT,
    -- Fingerprint of the resume the score was computed against. A score whose
    -- resume_hash no longer matches the current resume is treated as unscored.
    resume_hash       TEXT NOT NULL DEFAULT '',
    dedupe_key        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe   ON jobs(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_jobs_unshown  ON jobs(shown_at, track, fit_score);
CREATE INDEX IF NOT EXISTS idx_jobs_unscored ON jobs(scored_at, resume_hash);

CREATE TABLE IF NOT EXISTS runs (
    started_at   TEXT PRIMARY KEY,
    finished_at  TEXT,
    fetched      INTEGER NOT NULL DEFAULT 0,
    new_jobs     INTEGER NOT NULL DEFAULT 0,
    survivors    INTEGER NOT NULL DEFAULT 0,
    scored       INTEGER NOT NULL DEFAULT 0,
    emailed      INTEGER NOT NULL DEFAULT 0,
    est_cost_usd REAL NOT NULL DEFAULT 0.0,
    notes        TEXT NOT NULL DEFAULT ''
);
"""


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for databases created by an earlier version."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    for column, ddl in (("resume_hash", "TEXT NOT NULL DEFAULT ''"),):
        if column not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {ddl}")


@contextmanager
def connect(path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        company=row["company"],
        title=row["title"],
        location=row["location"],
        url=row["url"],
        ats=row["ats"],
        description=row["description"],
        posted_at=_dt(row["posted_at"]),
        first_seen_at=_dt(row["first_seen_at"]),
        source=row["source"],
        track=row["track"],
        shown_at=_dt(row["shown_at"]),
        tier=row["tier"],
        metro=row["metro"],
        metro_class=row["metro_class"],
        clearance_advantage=bool(row["clearance_advantage"]),
        fit_score=row["fit_score"],
        fit_rationale=row["fit_rationale"],
        scored_at=_dt(row["scored_at"]),
        resume_hash=row["resume_hash"],
    )


def known_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT id FROM jobs")}


def known_dedupe_keys(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT dedupe_key FROM jobs WHERE dedupe_key != ''")}


def upsert(conn: sqlite3.Connection, jobs: Iterable[Job]) -> int:
    """Insert new jobs; leave existing rows (and their scores) untouched."""
    rows = []
    for j in jobs:
        rows.append(
            (
                j.id, j.company, j.title, j.location, j.url, j.ats, j.description,
                _iso(j.posted_at), _iso(j.first_seen_at) or _iso(datetime.now(timezone.utc)),
                j.source, j.track, _iso(j.shown_at), j.tier, j.metro, j.metro_class,
                int(j.clearance_advantage), j.fit_score, j.fit_rationale,
                _iso(j.scored_at), j.resume_hash, "|".join(j.dedupe_key),
            )
        )
    cur = conn.executemany(
        """INSERT OR IGNORE INTO jobs
           (id, company, title, location, url, ats, description, posted_at,
            first_seen_at, source, track, shown_at, tier, metro, metro_class,
            clearance_advantage, fit_score, fit_rationale, scored_at,
            resume_hash, dedupe_key)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return cur.rowcount


def fill_descriptions(conn: sqlite3.Connection, jobs: Iterable[Job]) -> int:
    """Persist freshly-hydrated descriptions back into the local working store."""
    rows = [(j.description, j.id) for j in jobs if j.description]
    conn.executemany("UPDATE jobs SET description=? WHERE id=?", rows)
    return len(rows)


def save_scores(conn: sqlite3.Connection, jobs: Iterable[Job], resume_hash: str) -> int:
    now = _iso(datetime.now(timezone.utc))
    rows = [
        (j.fit_score, j.fit_rationale, now, resume_hash, j.id)
        for j in jobs
        if j.fit_score is not None
    ]
    conn.executemany(
        "UPDATE jobs SET fit_score=?, fit_rationale=?, scored_at=?, resume_hash=? "
        "WHERE id=?",
        rows,
    )
    return len(rows)


def unscored(
    conn: sqlite3.Connection, track: str, limit: int, resume_hash: str
) -> list[Job]:
    """Jobs needing a score: never scored, OR scored against an older resume.

    Ordering puts genuinely-new postings first, then stale-score rows in
    best-known-score order — so if a resume edit invalidates more than one run's
    budget allows, the most promising jobs are re-scored first and the rest
    carry to later runs.
    """
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE track = ? AND shown_at IS NULL
             AND (scored_at IS NULL OR resume_hash != ?)
           ORDER BY (scored_at IS NULL) DESC,
                    (metro_class = 'primary') DESC,
                    COALESCE(fit_score, -1) DESC,
                    COALESCE(posted_at, first_seen_at) DESC
           LIMIT ?""",
        (track, resume_hash, limit),
    ).fetchall()
    return [_row_to_job(r) for r in rows]


def count_stale_scores(conn: sqlite3.Connection, resume_hash: str) -> int:
    """How many existing scores were computed against a different resume."""
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE scored_at IS NOT NULL "
            "AND shown_at IS NULL AND resume_hash != ?",
            (resume_hash,),
        ).fetchone()[0]
    )


def candidates(
    conn: sqlite3.Connection, track: str, resume_hash: str, limit: int = 200
) -> list[Job]:
    """Scored, never-shown jobs for the daily pick — no model call needed.

    Only scores computed against the CURRENT resume are eligible. A stale score
    is not shown; it waits its turn to be re-scored.
    """
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE shown_at IS NULL AND scored_at IS NOT NULL
             AND track = ? AND resume_hash = ?
           ORDER BY fit_score DESC, COALESCE(posted_at, first_seen_at) DESC
           LIMIT ?""",
        (track, resume_hash, limit),
    ).fetchall()
    return [_row_to_job(r) for r in rows]


def get(conn: sqlite3.Connection, job_id: str) -> Optional[Job]:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def mark_shown(conn: sqlite3.Connection, job_ids: Iterable[str]) -> int:
    now = _iso(datetime.now(timezone.utc))
    ids = list(job_ids)
    conn.executemany("UPDATE jobs SET shown_at=? WHERE id=?", [(now, i) for i in ids])
    return len(ids)


def record_run(conn: sqlite3.Connection, started: datetime, **fields) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO runs
           (started_at, finished_at, fetched, new_jobs, survivors, scored,
            emailed, est_cost_usd, notes)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            _iso(started),
            _iso(datetime.now(timezone.utc)),
            fields.get("fetched", 0),
            fields.get("new_jobs", 0),
            fields.get("survivors", 0),
            fields.get("scored", 0),
            fields.get("emailed", 0),
            fields.get("est_cost_usd", 0.0),
            fields.get("notes", ""),
        ),
    )


def stats(conn: sqlite3.Connection) -> dict:
    def one(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])

    return {
        "total": one("SELECT COUNT(*) FROM jobs"),
        "scored": one("SELECT COUNT(*) FROM jobs WHERE scored_at IS NOT NULL"),
        "shown": one("SELECT COUNT(*) FROM jobs WHERE shown_at IS NOT NULL"),
        "unshown_scored": one(
            "SELECT COUNT(*) FROM jobs WHERE shown_at IS NULL AND scored_at IS NOT NULL"
        ),
    }
