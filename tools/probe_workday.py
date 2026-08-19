#!/usr/bin/env python3
"""Probe every Workday board in companies.yaml and rewrite its `valid` flag.

One page-0 POST per board, concurrently, over free public HTTP. No API key is
touched and nothing here can cost money.

    python tools/probe_workday.py            # report only
    python tools/probe_workday.py --write    # also update companies.yaml

A board is marked valid only if the endpoint answers AND returns at least one
posting, matching the standard the 160 non-Workday entries were held to. Dead
boards keep valid: false and get a `note` recording what actually happened, so
the next person does not have to re-probe to learn that a slug is wrong rather
than merely untried.

The YAML is edited line-by-line rather than round-tripped through yaml.dump():
companies.yaml is a hand-maintained, comment-heavy file and dumping it would
discard every comment in it.
"""

from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config import load_config, repo_path  # noqa: E402
from sources.base import make_client  # noqa: E402
from sources.workday import probe  # noqa: E402

VALIDATED_NOTE = "probed live {date}; CxS endpoint answered"
DEAD_NOTE = "probed live {date}; {reason}"


def probe_all(entries: list[dict], cfg: dict, workers: int) -> list[dict]:
    client = make_client(cfg)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(
                pool.map(lambda e: probe(client, e["slug"]), entries)
            )
    finally:
        client.close()

    out = []
    for entry, (ok, total, reason) in zip(entries, results):
        out.append({**entry, "_ok": ok, "_total": total, "_reason": reason})
    return out


def rewrite_yaml(path: Path, results: list[dict], today: str) -> int:
    """Update valid / postings_at_validation / note for each workday entry."""
    by_slug = {r["slug"]: r for r in results}
    lines = path.read_text(encoding="utf-8").splitlines()

    out: list[str] = []
    current: dict | None = None
    in_workday = False
    pending_fields: set[str] = set()

    def flush(indent: str) -> None:
        """Emit fields the original entry did not carry."""
        if current is None:
            return
        if "postings_at_validation" in pending_fields and current["_ok"]:
            out.append(f"{indent}postings_at_validation: {current['_total']}")

    for line in lines:
        if re.match(r"^workday_boards:\s*$", line):
            in_workday = True
            out.append(line)
            continue
        if in_workday and re.match(r"^[a-z_]+:\s*$", line):
            in_workday = False

        m = re.match(r"^(\s*)- company:\s*(.*)$", line)
        if m and in_workday:
            flush("    ")
            current = None
            pending_fields = {"postings_at_validation"}
            out.append(line)
            continue

        if in_workday and current is None:
            ms = re.match(r'^(\s*)slug:\s*"([^"]+)"\s*$', line)
            if ms:
                current = by_slug.get(ms.group(2))
                out.append(line)
                continue

        if in_workday and current is not None:
            indent = re.match(r"^(\s*)", line).group(1)
            if re.match(r"^\s*valid:\s", line):
                out.append(f"{indent}valid: {'true' if current['_ok'] else 'false'}")
                continue
            if re.match(r"^\s*postings_at_validation:\s", line):
                pending_fields.discard("postings_at_validation")
                if current["_ok"]:
                    out.append(f"{indent}postings_at_validation: {current['_total']}")
                continue
            if re.match(r"^\s*note:\s", line):
                pending_fields.discard("postings_at_validation")
                if current["_ok"]:
                    out.append(f"{indent}postings_at_validation: {current['_total']}")
                    out.append(
                        f'{indent}note: "{VALIDATED_NOTE.format(date=today)}"'
                    )
                else:
                    reason = (current["_reason"] or "no postings returned").replace(
                        '"', "'"
                    )[:120]
                    out.append(
                        f'{indent}note: "{DEAD_NOTE.format(date=today, reason=reason)}"'
                    )
                continue

        out.append(line)

    flush("    ")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return sum(1 for r in results if r["_ok"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="update companies.yaml")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--date", default=None, help="date stamp for the notes")
    args = parser.parse_args(argv)

    from datetime import date as _date

    today = args.date or _date.today().isoformat()

    cfg = load_config()
    path = repo_path(cfg["paths"]["companies"])
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data.get("workday_boards") or []
    print(f"probing {len(entries)} Workday boards ...", file=sys.stderr)

    results = probe_all(entries, cfg, args.workers)

    ok = [r for r in results if r["_ok"]]
    dead = [r for r in results if not r["_ok"]]
    print(f"\nvalid   : {len(ok)}")
    print(f"dead    : {len(dead)}")
    print(f"postings: {sum(r['_total'] for r in ok)}")
    print("\n--- DEAD ---")
    for r in sorted(dead, key=lambda r: r["company"]):
        print(f"  {r['company']:<34} {r['slug']:<50} {r['_reason'][:60]}")
    print("\n--- VALID (top 40 by size) ---")
    for r in sorted(ok, key=lambda r: -r["_total"])[:40]:
        print(f"  {r['company']:<34} {r['slug']:<50} {r['_total']:>6}")

    if args.write:
        n = rewrite_yaml(path, results, today)
        print(f"\nwrote {path} — {n} entries marked valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
