"""Committed state: a sorted, diff-friendly JSON ledger.

`state.db` is the LOCAL working store and is gitignored. Committing a binary
SQLite file would be a slow-motion repo bloat problem: SQLite delta-compresses
poorly and the whole file is rewritten every run, so ~250 runs a year would
store ~250 full copies in history to track what is really just a list of job
IDs and scores.

The committed artifact is `state/seen_jobs.json` instead:

  * one JSON object per job, keys in a fixed order
  * records sorted by id
  * two-space indent, trailing newline

so a normal day's diff is a handful of added objects and a few `shown_at`
flips, readable in a PR.

Job DESCRIPTIONS are deliberately not persisted here — they are large, they
change upstream, and only the ten jobs in today's email need one. Those are
re-hydrated over free HTTP at send time.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Fixed key order. Never reorder these — it would rewrite every line of the
# file and produce a diff the size of the whole ledger.
FIELDS = (
    "id",
    "company",
    "title",
    "location",
    "url",
    "ats",
    "source",
    "track",
    "tier",
    "metro",
    "metro_class",
    "clearance_advantage",
    "posted_at",
    "first_seen_at",
    "shown_at",
    # Grouped with shown_at because that is where a human reading the ledger
    # expects it. Adding a key costs one added line per record either way; what
    # must never change is the order of the keys ALREADY here.
    # Ledgers written before this field simply lack it, and load() reads a
    # missing key as None, so old files stay readable.
    "applied_at",
    "fit_score",
    "fit_rationale",
    "scored_at",
    "resume_hash",
    "dedupe_key",
)

VERSION = 1


def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for field in FIELDS:
        value = row[field]
        if field == "clearance_advantage":
            value = bool(value)
        record[field] = value
    return record


def dump(conn: sqlite3.Connection, path: str | Path, force: bool = False) -> int:
    """Write the SQLite contents out as sorted JSON. Returns the record count.

    Refuses to write a ledger that has FEWER scored, shown, or applied records
    than the one already on disk, because that can only mean the database it is
    being written from has lost something the committed file still had.

    The counts are monotonic by design: a score is cached forever, `shown_at`
    is never cleared, and `applied_at` is the owner's own record. So a decrease
    is never a legitimate outcome of a run — it is a symptom, and by the time
    it reaches the file the previous contents are gone. `force=True` is the
    escape hatch for a deliberate rebuild.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    columns = ", ".join(FIELDS)
    rows = conn.execute(f"SELECT {columns} FROM jobs ORDER BY id ASC").fetchall()
    records = [_row_to_record(r) for r in rows]

    if not force:
        previous = summarize(path)
        outgoing = {
            "scored": sum(1 for r in records if r.get("scored_at")),
            "shown": sum(1 for r in records if r.get("shown_at")),
            "applied": sum(1 for r in records if r.get("applied_at")),
        }
        lost = {
            k: (previous.get(k, 0), outgoing[k])
            for k in outgoing
            if outgoing[k] < previous.get(k, 0)
        }
        if lost:
            detail = ", ".join(
                f"{k} {was} -> {now}" for k, (was, now) in sorted(lost.items())
            )
            raise LedgerRegression(
                f"refusing to write {path}: it would lose earned records ({detail}). "
                f"The database being written from has less than the committed ledger. "
                f"Restore the ledger (git checkout {path}) and re-run, or pass "
                f"force=True for a deliberate rebuild."
            )

    payload = {
        "version": VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "count": len(records),
        "jobs": records,
    }
    # sort_keys=False because FIELDS order is already fixed and meaningful;
    # the determinism that matters is the row ordering, done in SQL above.
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")
    return len(records)


# Fields the LEDGER is authoritative for when the local database has nothing.
#
# These are EARNED, not fetched: a score cost money, `shown_at` is the promise
# that a role is never sent twice, and `applied_at` is the owner's own record
# of where he applied. Re-fetching a posting reproduces its title and location
# for free; nothing reproduces these.
_EARNED = ("shown_at", "applied_at", "fit_score", "scored_at")
_EARNED_TEXT = ("fit_rationale", "resume_hash")


def load(conn: sqlite3.Connection, path: str | Path) -> int:
    """Hydrate SQLite from the committed JSON. Returns the record count.

    New rows are inserted. For rows the local database ALREADY has, the earned
    fields above are restored wherever the database is empty and the ledger is
    not — the database never wins by having less.

    That asymmetry is the whole point, and getting it wrong destroys data.
    This function used to be a bare INSERT OR IGNORE, on the reasoning that
    "existing rows win, so a local run that already fetched fresh data is never
    clobbered". But a freshly fetched row is empty exactly where it matters:
    no score, no shown_at, no applied_at. So a state.db that had drifted behind
    the ledger — a crashed run, a restored copy, a run with --state pointing
    elsewhere — would keep its own NULLs, and the dump() at the end of the run
    would then write those NULLs back over the committed ledger.

    Observed 2026-08-19: a local state.db holding 770 rows and 0 scores loaded
    a ledger holding 770 rows and 40 scores, kept its own NULLs, and the dump
    at the end of that run erased all 40 scores and every shown_at flag from
    the committed file. The ledger IS the agent's memory; losing it means
    re-paying for every score and re-sending every role already sent.
    """
    path = Path(path)
    if not path.exists():
        return 0

    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)

    records = payload.get("jobs") or []
    if not records:
        return 0

    columns = ", ".join(FIELDS)
    placeholders = ", ".join("?" for _ in FIELDS)
    values = [
        tuple(
            int(record.get(f, 0)) if f == "clearance_advantage" else record.get(f)
            for f in FIELDS
        )
        for record in records
    ]
    conn.executemany(
        f"INSERT OR IGNORE INTO jobs ({columns}, description) "
        f"VALUES ({placeholders}, '')",
        values,
    )

    # Restore earned fields onto rows that already existed locally. COALESCE
    # and the empty-string CASE both mean the same thing: the ledger fills a
    # hole, never overwrites a value the database already has.
    sets = [f"{f} = COALESCE({f}, ?)" for f in _EARNED]
    sets += [f"{f} = CASE WHEN {f} = '' OR {f} IS NULL THEN ? ELSE {f} END" for f in _EARNED_TEXT]
    # first_seen_at drives the age stamp in the email, so the EARLIER of the
    # two wins rather than whichever was written last.
    sets.append("first_seen_at = COALESCE(MIN(first_seen_at, ?), first_seen_at)")
    restore_sql = f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?"
    conn.executemany(
        restore_sql,
        [
            tuple(record.get(f) for f in _EARNED + _EARNED_TEXT)
            + (record.get("first_seen_at"), record.get("id"))
            for record in records
        ],
    )
    return len(records)


class LedgerRegression(RuntimeError):
    """dump() would erase earned records that the ledger on disk still has."""


def summarize(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {"count": 0, "shown": 0, "scored": 0}
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    jobs = payload.get("jobs") or []
    return {
        "count": len(jobs),
        "shown": sum(1 for j in jobs if j.get("shown_at")),
        "scored": sum(1 for j in jobs if j.get("scored_at")),
        "applied": sum(1 for j in jobs if j.get("applied_at")),
    }
