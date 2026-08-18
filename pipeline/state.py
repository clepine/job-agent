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


def dump(conn: sqlite3.Connection, path: str | Path) -> int:
    """Write the SQLite contents out as sorted JSON. Returns the record count."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    columns = ", ".join(FIELDS)
    rows = conn.execute(f"SELECT {columns} FROM jobs ORDER BY id ASC").fetchall()
    records = [_row_to_record(r) for r in rows]

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


def load(conn: sqlite3.Connection, path: str | Path) -> int:
    """Hydrate SQLite from the committed JSON. Returns the record count.

    Existing rows win: this only fills in what the local database is missing,
    so a local run that has already fetched fresh data is never clobbered.
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
    return len(records)


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
    }
