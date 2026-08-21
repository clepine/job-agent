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
from datetime import datetime, timedelta, timezone
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
    -- Recorded by `run.py --applied <job-id>`. Applied jobs are excluded from
    -- every candidate query so a role can never be surfaced twice.
    applied_at        TEXT,
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


# Indexes are applied AFTER _migrate(), not as part of SCHEMA. They reference
# columns (resume_hash, applied_at) that an older database does not have yet,
# and CREATE INDEX on a missing column is a hard error — so creating them in the
# same script as the table made a pre-existing database impossible to open.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe   ON jobs(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_jobs_unshown  ON jobs(shown_at, track, fit_score);
CREATE INDEX IF NOT EXISTS idx_jobs_unscored ON jobs(scored_at, resume_hash);
CREATE INDEX IF NOT EXISTS idx_jobs_applied  ON jobs(applied_at);
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
    for column, ddl in (
        ("resume_hash", "TEXT NOT NULL DEFAULT ''"),
        ("applied_at", "TEXT"),
    ):
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
        conn.executescript(INDEXES)
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
        applied_at=_dt(row["applied_at"]),
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
                j.source, j.track, _iso(j.shown_at), _iso(j.applied_at),
                j.tier, j.metro, j.metro_class,
                int(j.clearance_advantage), j.fit_score, j.fit_rationale,
                _iso(j.scored_at), j.resume_hash, "|".join(j.dedupe_key),
            )
        )
    cur = conn.executemany(
        """INSERT OR IGNORE INTO jobs
           (id, company, title, location, url, ats, description, posted_at,
            first_seen_at, source, track, shown_at, applied_at, tier, metro,
            metro_class, clearance_advantage, fit_score, fit_rationale,
            scored_at, resume_hash, dedupe_key)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
    conn: sqlite3.Connection,
    track: str,
    limit: int,
    resume_hash: str,
    max_age_days: Optional[int] = None,
) -> list[Job]:
    """Jobs needing a score: never scored, OR scored against an older resume.

    Ordering puts genuinely-new postings first, then stale-score rows in
    best-known-score order — so if a resume edit invalidates more than one run's
    budget allows, the most promising jobs are re-scored first and the rest
    carry to later runs.

    Within the never-scored group the order is FRESHEST FIRST. This is the whole
    ballgame for a daily email and it used to be the last tiebreaker, behind
    `metro_class = 'primary'`.

    A posting cannot be sent until it has been scored, and scoring is capped at
    limits.max_new_scores_per_run — 20 — against an arrival rate measured at 246
    on 2026-08-21. Capacity is therefore an order of magnitude below inflow, so
    what the email can draw on is not "recent postings", it is "whatever won the
    scoring lottery", and primary-metro-first meant a two-week-old Boston req
    outranked this morning's arrivals every single day. That is why the
    2026-08-21 email led with postings 21, 22 and 24 days old: they were not
    ranked above the fresh ones, they were the only fresh-ish ones ever scored.

    `fit_score` sorts ahead of the date and is inert for the never-scored group
    (it is NULL for all of them), so it only orders the re-score case, exactly
    as before.

    `max_age_days` stops the run paying to score postings that
    limits.max_backlog_age_days would refuse to send anyway. Undated postings
    are kept, matching filters.check_age: unknown age is not old age.
    """
    where = [
        "track = ?",
        "shown_at IS NULL",
        "applied_at IS NULL",
        "(scored_at IS NULL OR resume_hash != ?)",
    ]
    params: list[object] = [track, resume_hash]
    if max_age_days is not None:
        where.append(
            "(COALESCE(posted_at, first_seen_at) IS NULL"
            " OR COALESCE(posted_at, first_seen_at) >= ?)"
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(max_age_days))
        params.append(cutoff.isoformat())
    params.append(limit)

    rows = conn.execute(
        f"""SELECT * FROM jobs
           WHERE {' AND '.join(where)}
           ORDER BY (scored_at IS NULL) DESC,
                    COALESCE(fit_score, -1) DESC,
                    COALESCE(posted_at, first_seen_at) DESC,
                    (metro_class = 'primary') DESC
           LIMIT ?""",
        params,
    ).fetchall()
    return [_row_to_job(r) for r in rows]


def upgrade_score_fingerprints(conn: sqlite3.Connection, hashes: dict[str, str]) -> int:
    """One-time migration from the resume-only score hash to the composite.

    Scores used to be stamped with resume_hash(resume) alone. They are now
    stamped with score_fingerprint(), which appends a hash of the scoring
    REGIME — the prompt, the model id, jd_max_chars — because a cached score is
    only comparable to a fresh one if both were produced the same way.

    Introducing that would otherwise invalidate every existing score as a false
    positive. The regime hash is new, but the regime is not: the prompt, model
    and truncation were all unchanged by the commit that added it, so those
    scores are still exactly right and re-deriving them is pure waste. The real
    cost is not the money — it is that a run's scoring budget is capped, so a
    morning spent re-scoring old postings is a morning NOT spent scoring the
    new ones, and the backlog it was meant to drain just sits there.

    Deliberately exact: only a row whose stored hash equals the CURRENT
    resume-only fingerprint is upgraded. A score computed against an older
    resume keeps its own hash and stays stale, which is correct — the resume it
    was judged against really has changed. Idempotent, because an upgraded row
    no longer matches the legacy value.
    """
    upgraded = 0
    for track, composite in hashes.items():
        legacy, sep, _regime = composite.partition(":")
        if not sep:
            continue
        cur = conn.execute(
            "UPDATE jobs SET resume_hash = ? "
            "WHERE track = ? AND resume_hash = ? AND scored_at IS NOT NULL",
            (composite, track, legacy),
        )
        upgraded += cur.rowcount
    return upgraded


def count_stale_scores(
    conn: sqlite3.Connection, resume_hash: str, track: Optional[str] = None
) -> int:
    """How many existing scores were computed against a different resume.

    `track` is not optional in practice — omitting it counts the OTHER track's
    rows too. The two tracks are scored against different resumes and so always
    carry different fingerprints, which means every hardware row looks stale to
    a software query and vice versa. Before this took a track, the startup note
    reported "30 software" and "30 hardware" for the same 30 rows.

    Nothing was ever mis-scored over it — unscored() has always filtered by
    track, and that is what actually drives re-scoring. But this number is the
    one the owner reads to decide whether a resume edit is about to cost him
    money, so it being roughly double is not a harmless cosmetic issue.
    """
    sql = (
        "SELECT COUNT(*) FROM jobs WHERE scored_at IS NOT NULL "
        "AND shown_at IS NULL AND applied_at IS NULL AND resume_hash != ?"
    )
    params: list = [resume_hash]
    if track is not None:
        sql += " AND track = ?"
        params.append(track)
    return int(conn.execute(sql, params).fetchone()[0])


def candidates(
    conn: sqlite3.Connection, track: str, resume_hash: str, limit: int = 200
) -> list[Job]:
    """Scored, never-shown jobs for the daily pick — no model call needed.

    Only scores computed against the CURRENT resume are eligible. A stale score
    is not shown; it waits its turn to be re-scored.
    """
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE shown_at IS NULL AND applied_at IS NULL AND scored_at IS NOT NULL
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


def mark_applied(
    conn: sqlite3.Connection, job_id: str, when: Optional[datetime] = None
) -> Optional[Job]:
    """Record that the owner applied. Returns the job, or None if unknown.

    Also stamps `shown_at` when it is still null: applying to a job the email
    has not surfaced yet (found some other way, or dug out of the ledger) must
    not leave it queued to arrive tomorrow as a fresh suggestion.

    Idempotent — re-applying keeps the ORIGINAL date, because the first
    application is the one whose age matters when chasing a response.
    """
    row = conn.execute(
        "SELECT applied_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        return None
    if not row["applied_at"]:
        stamp = _iso(when or datetime.now(timezone.utc))
        conn.execute(
            "UPDATE jobs SET applied_at = ?, "
            "shown_at = COALESCE(shown_at, ?) WHERE id = ?",
            (stamp, stamp, job_id),
        )
    return get(conn, job_id)


def applied(conn: sqlite3.Connection, limit: int = 500) -> list[Job]:
    """Everything applied to, most recent first."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE applied_at IS NOT NULL "
        "ORDER BY applied_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_job(r) for r in rows]


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
        "applied": one("SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"),
        "unshown_scored": one(
            "SELECT COUNT(*) FROM jobs WHERE shown_at IS NULL "
            "AND applied_at IS NULL AND scored_at IS NOT NULL"
        ),
    }
