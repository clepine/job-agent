"""The HANDOFF decisions table must agree with config.yaml.

That table is titled "decisions that must not be silently reverted" and is read
as authority by anyone picking this project up. A row that misstates the value
it documents is worse than no row at all — it actively misleads.

This is not hypothetical. On 2026-08-19 the table said `email.min_fit: 50`
while config.yaml said `40`, introduced in the same commit that created both.
The row's own justification ("score.py calibrates 0-39 as poor match") implied
40, so the document contradicted itself and nothing caught it.

Only settings the table actually cites are checked. Adding a setting to
config.yaml does not oblige anyone to document it — but documenting it wrong is
now a test failure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
HANDOFF = ROOT / "HANDOFF.md"

# (name as written in HANDOFF, path into config.yaml)
DOCUMENTED = [
    ("min_fit", ("email", "min_fit")),
    ("tier1_min_fit", ("email", "tier1_min_fit")),
    ("max_backlog_age_days", ("limits", "max_backlog_age_days")),
    ("max_posting_age_days", ("limits", "max_posting_age_days")),
    ("max_posting_age_days_primary", ("limits", "max_posting_age_days_primary")),
]


@pytest.fixture(scope="module")
def raw_config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def handoff() -> str:
    return HANDOFF.read_text(encoding="utf-8")


@pytest.mark.parametrize("name,path", DOCUMENTED)
def test_handoff_states_the_real_value(name, path, raw_config, handoff):
    node = raw_config
    for key in path:
        node = node[key]

    # Two near-miss traps, both real:
    #   * `min_fit:` is a substring of `tier1_min_fit:` — hence the lookbehind,
    #     since "_" is a word character and blocks the match.
    #   * `max_posting_age_days:` cannot match `max_posting_age_days_primary:`,
    #     because the colon falls in a different place.
    found = re.findall(rf"(?<![\w]){re.escape(name)}:\s*(\d+)", handoff)
    assert found, f"HANDOFF.md does not mention {name} — remove it from DOCUMENTED"
    wrong = [v for v in found if int(v) != int(node)]
    assert not wrong, (
        f"HANDOFF.md says {name} is {wrong[0]}, config.yaml says {node}. "
        f"Update the decisions table in the same commit as the setting."
    )


def test_handoff_has_no_dangling_cross_references(handoff):
    """A pointer to a section that was renamed is a small lie that compounds.

    "See 'Open question' below" survived a rename of that very section on
    2026-08-19 and pointed at nothing.
    """
    headings = {
        line.lstrip("#").strip().lower()
        for line in handoff.splitlines()
        if line.startswith("#")
    }
    for match in re.finditer(r'See "([^"]+)" (?:below|above)', handoff):
        target = match.group(1).lower()
        assert any(target in h for h in headings), (
            f"HANDOFF.md points at a section named {match.group(1)!r}, "
            f"which does not exist"
        )


def test_the_no_live_api_call_constraint_is_still_stated(handoff):
    """The single most expensive rule to forget."""
    assert "Never make live Anthropic API calls" in handoff
