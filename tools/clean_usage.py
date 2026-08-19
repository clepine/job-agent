#!/usr/bin/env python3
"""Strip StubAnthropic rows from out/usage.jsonl.

usage.jsonl exists to answer one question honestly: what has this agent
actually cost? That answer was wrong. Before tests were isolated (conftest's
`_isolate_usage_log`), every test that exercised an LLM stage appended a row to
the REAL ledger, so the file claimed 357 calls and $2.18 against a live spend of
6 calls and $0.0757 — a 29x overstatement on a $5 balance. A cost ledger that
overstates by 29x is worse than no ledger: it would have the owner throttling a
pipeline that is not actually spending, and it hides a real overspend inside the
noise.

StubAnthropic returns a flat, constant usage shape, which is what makes the
stub rows separable at all:

    input_tokens=1000, output_tokens=200, cache_* = 0

A real call cannot plausibly land on those four numbers simultaneously, and
every real row observed carries nonzero cache_creation OR cache_read tokens
because the resume prefix is cached. Rows are matched on the full four-field
signature, never on cost alone.

    python tools/clean_usage.py            # report only
    python tools/clean_usage.py --write    # rewrite, keeping a .bak

Nothing here touches the network or an API key.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config import load_config, repo_path  # noqa: E402

STUB_SIGNATURE = {
    "input_tokens": 1000,
    "output_tokens": 200,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


def is_stub(row: dict) -> bool:
    return all(row.get(k) == v for k, v in STUB_SIGNATURE.items())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="rewrite the file")
    ap.add_argument("--path", help="override the usage log path")
    args = ap.parse_args()

    cfg = load_config()
    path = Path(args.path) if args.path else repo_path(cfg["budget"]["usage_log"])
    if not path.exists():
        print(f"{path} does not exist — nothing to clean")
        return 0

    kept, dropped = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)          # never discard something unparsed
            continue
        (dropped if is_stub(row) else kept).append(line if not is_stub(row) else row)

    real = [json.loads(k) if isinstance(k, str) else k for k in kept]
    total = sum(r.get("call_cost_usd", 0.0) for r in real if isinstance(r, dict))

    print(f"{path}")
    print(f"  stub rows (test pollution) : {len(dropped)}")
    print(f"  real rows                  : {len(kept)}")
    print(f"  real spend to date         : ${total:.4f}")

    if not dropped:
        print("  already clean")
        return 0
    if not args.write:
        print("\n  report only — pass --write to rewrite (a .bak is kept)")
        return 0

    shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text("\n".join(k if isinstance(k, str) else json.dumps(k) for k in kept) + "\n",
                    encoding="utf-8")
    print(f"\n  rewrote {path} ({len(kept)} rows kept); backup at {path.name}.bak")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
