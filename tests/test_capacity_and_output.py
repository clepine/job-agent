"""The scoring budget must cover the scoring cap, and the email must render
each posting from one computation.

Both are 2026-08-21 changes made when the owner raised max_usd_per_run to $0.15
to buy capacity. The two settings are coupled and nothing enforced it: the
ceiling aborts BEFORE the call that would breach it, so a cap that outgrows its
budget does not fail loudly - it scores half the pool and the email quietly
draws on a thinner backlog.
"""

from __future__ import annotations

import yaml
from pathlib import Path

from pipeline import email as email_mod
from pipeline import resume_pick
from pipeline.models import Job

ROOT = Path(__file__).resolve().parent.parent

# Measured across three real runs (see the config.yaml comment for the table):
# $0.00330-$0.00345 per posting scored. Take the top of the range.
#
# An earlier value of $0.00158 was derived rather than measured -- it assumed
# every batch call carried a full score_batch_size of 8 -- and it was 2.1x
# optimistic. That is precisely how a budget guard gets written and still lets a
# run abort mid-scoring, so this number comes from usage.jsonl and nowhere else.
USD_PER_POSTING = 0.0035


def _cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def test_the_budget_ceiling_covers_a_full_scoring_run():
    cfg = _cfg()
    per_track = int(cfg["limits"]["max_new_scores_per_run"])
    ceiling = float(cfg["budget"]["max_usd_per_run"])
    projected = USD_PER_POSTING * per_track * 2  # the cap is PER TRACK
    assert projected < ceiling, (
        f"a full run scores {per_track * 2} postings for ~${projected:.4f}, "
        f"over the ${ceiling:.2f} ceiling — scoring would abort mid-run. "
        f"Raise budget.max_usd_per_run in the same edit."
    )


def test_the_ceiling_keeps_real_headroom_for_variance():
    """Per-call cost varies with cache hits; a ceiling met exactly is a ceiling
    breached on a bad day."""
    cfg = _cfg()
    projected = USD_PER_POSTING * int(cfg["limits"]["max_new_scores_per_run"]) * 2
    assert projected <= float(cfg["budget"]["max_usd_per_run"]) * 0.8


def test_preranking_actually_selects():
    """BM25 exists to choose which postings the paid stage sees. A pool no
    larger than the cap makes it a no-op that still costs a full run's budget."""
    cfg = _cfg()
    assert int(cfg["limits"]["max_jobs_to_prerank"]) > int(
        cfg["limits"]["max_new_scores_per_run"]
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

RESUME_SW = {"skills": [{"label": "Software", "items": ["Python", "Docker"]}]}
RESUME_HW = {"skills": [{"label": "Hardware", "items": ["Verilog", "MSP430"]}]}

CROWE_LOCATIONS = "; ".join(f"City{i} ST USA" for i in range(37))


def _job(**kw) -> Job:
    base = dict(
        company="Acme",
        title="Software Engineer",
        url="https://boards.greenhouse.io/acme/jobs/1",
        description="<p>Requirements: Python, Docker</p>",
        fit_score=60,
        fit_rationale="Sample.",
    )
    base.update(kw)
    return Job(**base)


def test_a_thirty_seven_location_posting_does_not_swamp_the_meta_line():
    """A Crowe posting in the 2026-08-21 email listed 37 offices, pushing the
    age stamp and fit score off the readable part of the line."""
    label = email_mod._location_label(CROWE_LOCATIONS)
    assert label.count(";") == 2
    assert label.endswith("+34 more")


def test_a_short_location_list_is_left_intact():
    assert email_mod._location_label("Boston, MA; Austin, TX") == "Boston, MA; Austin, TX"
    assert email_mod._location_label("") == "location not stated"


def test_each_posting_is_compressed_and_diffed_once(monkeypatch):
    """The HTML and plain-text parts render the same postings and both used to
    recompute everything themselves."""
    calls = {"compress": 0, "diff": 0}
    real_compress = email_mod.compress_jd
    real_diff = resume_pick.keywords.diff

    def counting_compress(raw, n):
        calls["compress"] += 1
        return real_compress(raw, n)

    def counting_diff(jd, resume):
        calls["diff"] += 1
        return real_diff(jd, resume)

    monkeypatch.setattr(email_mod, "compress_jd", counting_compress)
    monkeypatch.setattr(resume_pick.keywords, "diff", counting_diff)

    from pipeline.pick import Selection

    sel = Selection(jobs=[_job(), _job(company="Beta")])
    rendered = email_mod.render_email(
        sel, Selection(jobs=[]), RESUME_SW, RESUME_HW, _cfg()
    )
    assert calls["compress"] == 2          # one per posting, not one per renderer
    assert calls["diff"] == 4              # two resumes x two postings, no more
    assert "Acme" in rendered.html and "Acme" in rendered.text


def test_an_unfetchable_body_is_declared_rather_than_rendered_as_no_skills():
    """check_description passes an empty string unconditionally, so a posting
    with no body is the one case where entry level was never verified."""
    from pipeline.pick import Selection

    sel = Selection(jobs=[_job(description="")])
    rendered = email_mod.render_email(
        sel, Selection(jobs=[]), RESUME_SW, RESUME_HW, _cfg()
    )
    assert "could not be fetched" in rendered.html
    assert "not verified" in rendered.text


def test_a_fetched_body_carries_no_such_warning():
    from pipeline.pick import Selection

    sel = Selection(jobs=[_job()])
    rendered = email_mod.render_email(
        sel, Selection(jobs=[]), RESUME_SW, RESUME_HW, _cfg()
    )
    assert "could not be fetched" not in rendered.html
