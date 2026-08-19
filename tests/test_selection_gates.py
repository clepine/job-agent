"""Selection-stage gates, and the ledger merge that protects earned state.

Every test here cites the date and the observed failure it exists to prevent.
These are all cases where the pipeline ran to completion, reported success, and
produced a WORSE result than doing nothing — the failures that do not announce
themselves.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from pipeline import db, filters, state as state_mod
from pipeline.models import Job
from pipeline.pick import eligible, pick_track

DRAPER_INTERN = (
    "https://draper.wd5.myworkdayjobs.com/Draper_Careers/job/Cambridge-MA/"
    "Embedded-Quality---Fielded-Systems-Intern_JR002718"
)
DRAPER_MIXED_SIGNAL = (
    "https://draper.wd5.myworkdayjobs.com/Draper_Careers/job/Cambridge-MA/"
    "Mixed-Signal-Electronic-Design-Engineer_JR002804"
)


def _job(**kw) -> Job:
    base = dict(
        company="Draper",
        title="Embedded Engineer",
        location="Cambridge, MA",
        url="https://jobs.lever.co/example/" + kw.get("company", "x").lower(),
        ats="lever",
        source="lever:example",
        track="hardware",
        fit_score=70,
        scored_at=datetime.now(timezone.utc),
    )
    base.update(kw)
    return Job(**base)


def _aged(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


# ---------------------------------------------------------------------------
# 2026-08-18: an INTERNSHIP was the top hardware pick of a real email.
#
# The aggregator README truncates titles at a fixed width, so Draper's
# "Embedded Quality & Fielded Systems Intern" arrived as "...Systems In". The
# word "intern" was gone, so check_title_discipline had nothing to match, and
# hydration — which normally restores the real title — had failed for that row.
# Nothing re-hydrates a posting already in the database, so the truncation was
# permanent. The URL slug still carried the full title.
# ---------------------------------------------------------------------------


def test_url_slug_catches_a_disqualifier_the_truncated_title_hid():
    truncated = "Embedded Quality & Fielded Systems In"
    assert filters.check_title_discipline(truncated).passed, (
        "precondition: the truncated title looks fine on its own — that is why "
        "this posting reached the email"
    )
    result = filters.evaluate(truncated, "Cambridge, MA", "", DRAPER_INTERN)
    assert not result.passed
    assert result.stage == "discipline"
    assert "intern" in result.reason.lower()


def test_url_check_is_reject_only_and_cannot_rescue():
    """A lossy slug must never be able to admit something the title rejected."""
    result = filters.evaluate(
        "Senior Embedded Engineer", "Cambridge, MA", "", DRAPER_MIXED_SIGNAL
    )
    assert not result.passed
    assert result.stage == "seniority"


def test_url_check_leaves_the_must_keep_draper_posting_alone():
    assert filters.check_url_title(DRAPER_MIXED_SIGNAL).passed
    assert filters.evaluate(
        "Mixed Signal Electronic Design Engineer", "Cambridge, MA", "", DRAPER_MIXED_SIGNAL
    ).passed


def test_url_check_passes_when_the_url_carries_no_title():
    assert filters.check_url_title("https://jobs.lever.co/shieldai/f6bbec19").passed
    assert filters.check_url_title("").passed


# ---------------------------------------------------------------------------
# 2026-08-19: the scored backlog was exempt from every ingest filter.
# ---------------------------------------------------------------------------


def test_backlog_ages_out(cfg):
    """limits.max_posting_age_days governs arrival; the backlog needs its own.

    The 2026-08-18 hardware email sent a Field AI role posted 74 days earlier,
    and the pool still held a sendable one from 124 days earlier.
    """
    fresh = _job(company="Fresh", posted_at=_aged(3))
    stale = _job(company="Stale", posted_at=_aged(124))
    keep, notes, _t1 = eligible([fresh, stale], cfg, "hardware")
    assert keep == [fresh]
    assert any("aged out" in n and "124d" in n for n in notes)


def test_a_posting_with_no_date_is_never_aged_out(cfg):
    """Unknown age is not the same as old age — the email says so too."""
    undated = _job(company="Undated", posted_at=None)
    keep, _notes, _t1 = eligible([undated], cfg, "hardware")
    assert keep == [undated]


# ---------------------------------------------------------------------------
# 2026-08-18: the email sent five per track whether or not five were worth it.
# score.py calibrates 0-39 as "poor match"; the email shipped a 30 and a 20.
# ---------------------------------------------------------------------------


def test_poor_matches_are_never_sent(cfg):
    good = _job(company="Good", fit_score=70, posted_at=_aged(2))
    poor = _job(company="Waymo", fit_score=30, posted_at=_aged(2))
    keep, notes, _t1 = eligible([good, poor], cfg, "hardware")
    assert keep == [good]
    assert any("fit floor" in n for n in notes)


def test_a_short_list_explains_itself_rather_than_padding(cfg):
    """Four good matches beat five with a bad one, but the email must SAY so."""
    jobs = [_job(company=f"Co{i}", fit_score=70, posted_at=_aged(1)) for i in range(3)]
    jobs += [_job(company=f"Bad{i}", fit_score=25, posted_at=_aged(1)) for i in range(4)]
    sel = pick_track(jobs, cfg, "hardware")
    assert len(sel.jobs) == 3
    assert all((j.fit_score or 0) >= cfg["email"]["min_fit"] for j in sel.jobs)
    assert any("fit floor" in n for n in sel.notes)
    assert any("available to send today" in n for n in sel.notes)


def test_the_floor_is_the_models_own_calibration(cfg):
    """score.py's prompt calls 0-39 'poor match'. Keep the two in step."""
    assert cfg["email"]["min_fit"] == 40


# ---------------------------------------------------------------------------
# 2026-08-19: a stale state.db silently erased 40 scores from the committed
# ledger. load() was INSERT OR IGNORE, so the database kept its own NULLs, and
# dump() then wrote those NULLs back over the file.
# ---------------------------------------------------------------------------


def _ledger_row(job: Job, **overrides) -> dict:
    row = {f: None for f in state_mod.FIELDS}
    row.update(
        id=job.id,
        company=job.company,
        title=job.title,
        location=job.location,
        url=job.url,
        ats=job.ats,
        source=job.source,
        track=job.track,
        tier=2,
        metro_class="none",
        clearance_advantage=False,
        first_seen_at=_aged(9).isoformat(),
        fit_rationale="",
        resume_hash="",
        dedupe_key="",
    )
    row.update(overrides)
    return row


def _write_ledger(path, rows):
    import json

    path.write_text(
        json.dumps({"version": 1, "generated_at": "2026-08-19", "count": len(rows), "jobs": rows}),
        encoding="utf-8",
    )


def test_load_restores_scores_onto_a_stale_local_row(tmp_path):
    job = _job(company="Motorola Solutions", fit_score=None, scored_at=None)
    ledger = tmp_path / "seen.json"
    _write_ledger(
        ledger,
        [
            _ledger_row(
                job,
                fit_score=70,
                fit_rationale="C/embedded firmware aligns well.",
                scored_at=_aged(1).isoformat(),
                resume_hash="abc123",
                shown_at=_aged(1).isoformat(),
                applied_at=_aged(1).isoformat(),
            )
        ],
    )
    with db.connect(tmp_path / "state.db") as conn:
        db.upsert(conn, [job])       # local row exists, unscored — the stale case
        state_mod.load(conn, ledger)
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
        assert row["fit_score"] == 70, "the ledger must fill a hole the database has"
        assert row["fit_rationale"] == "C/embedded firmware aligns well."
        assert row["resume_hash"] == "abc123"
        assert row["shown_at"], "shown_at is the promise a role is never sent twice"
        assert row["applied_at"], "applied_at is the owner's own record"


def test_load_never_overwrites_a_value_the_database_already_has(tmp_path):
    job = _job(company="Local", fit_score=88, scored_at=_aged(0))
    ledger = tmp_path / "seen.json"
    _write_ledger(ledger, [_ledger_row(job, fit_score=12, scored_at=_aged(5).isoformat())])
    with db.connect(tmp_path / "state.db") as conn:
        db.upsert(conn, [job])
        conn.execute("UPDATE jobs SET fit_score = 88, scored_at = ? WHERE id = ?",
                     (_aged(0).isoformat(), job.id))
        state_mod.load(conn, ledger)
        assert conn.execute(
            "SELECT fit_score FROM jobs WHERE id = ?", (job.id,)
        ).fetchone()[0] == 88


def test_dump_refuses_to_erase_earned_records(tmp_path):
    job = _job(company="Motorola Solutions")
    ledger = tmp_path / "seen.json"
    _write_ledger(
        ledger,
        [_ledger_row(job, fit_score=70, scored_at=_aged(1).isoformat(),
                     shown_at=_aged(1).isoformat())],
    )
    with db.connect(tmp_path / "state.db") as conn:
        db.upsert(conn, [_job(company="Motorola Solutions", fit_score=None, scored_at=None)])
        with pytest.raises(state_mod.LedgerRegression) as exc:
            state_mod.dump(conn, ledger)
        assert "scored" in str(exc.value)
        # The file on disk is untouched by a refused write.
        assert state_mod.summarize(ledger)["scored"] == 1


def test_dump_force_allows_a_deliberate_rebuild(tmp_path):
    job = _job(company="Rebuild")
    ledger = tmp_path / "seen.json"
    _write_ledger(ledger, [_ledger_row(job, fit_score=70, scored_at=_aged(1).isoformat())])
    with db.connect(tmp_path / "state.db") as conn:
        db.upsert(conn, [_job(company="Rebuild", fit_score=None, scored_at=None)])
        assert state_mod.dump(conn, ledger, force=True) == 1
        assert state_mod.summarize(ledger)["scored"] == 0


# ---------------------------------------------------------------------------
# 2026-08-19: a cached score recorded WHO was scored but not HOW.
#
# resume_hash() fingerprints the resume and nothing else, so editing the
# scoring prompt, switching models, or changing jd_max_chars left every old
# score in place and pick.py compared two scoring regimes head to head. The
# symptom is a subtly wrong ranking, not a failed run — a trap laid for
# whoever tunes calibration, which is the most likely reason to touch score.py.
# ---------------------------------------------------------------------------


def test_editing_the_scoring_prompt_invalidates_cached_scores(cfg, resume_sw, monkeypatch):
    from pipeline import fingerprint, score as score_mod

    before = fingerprint.score_fingerprint(resume_sw, cfg)
    monkeypatch.setattr(score_mod, "SYSTEM_TEMPLATE", score_mod.SYSTEM_TEMPLATE + "\nBe harsher.")
    assert fingerprint.score_fingerprint(resume_sw, cfg) != before


def test_switching_models_invalidates_cached_scores(cfg, resume_sw):
    import copy

    from pipeline import fingerprint

    other = copy.deepcopy(cfg)
    other["model"]["id"] = "claude-opus-5"
    assert fingerprint.score_fingerprint(resume_sw, cfg) != fingerprint.score_fingerprint(
        resume_sw, other
    )


def test_showing_the_model_less_of_the_posting_invalidates_scores(cfg, resume_sw):
    import copy

    from pipeline import fingerprint

    other = copy.deepcopy(cfg)
    other["limits"]["jd_max_chars"] = 400
    assert fingerprint.score_fingerprint(resume_sw, cfg) != fingerprint.score_fingerprint(
        resume_sw, other
    )


def test_cost_only_settings_do_not_invalidate_scores(cfg, resume_sw):
    """Re-scoring costs real money. Only judgement changes may trigger it."""
    import copy

    from pipeline import fingerprint

    other = copy.deepcopy(cfg)
    other["limits"]["score_batch_size"] = 3
    other["budget"]["max_usd_per_run"] = 0.50
    other["fetch"]["retries"] = 5
    assert fingerprint.score_fingerprint(resume_sw, cfg) == fingerprint.score_fingerprint(
        resume_sw, other
    )


def test_rescore_stamps_the_same_fingerprint_the_daily_run_expects():
    """A mismatch here means every --rescore is silently redone the next
    morning at full price."""
    import inspect

    from pipeline import score as score_mod

    source = inspect.getsource(score_mod)
    assert "db.save_scores(conn, scored, score_fingerprint(resume, cfg))" in source, (
        "--rescore must stamp score_fingerprint(), not resume_hash()"
    )


# ---------------------------------------------------------------------------
# 2026-08-19: concurrent page waves made 429 a live failure mode, and
# post_json treated every 4xx as permanent — so one rate-limited page aborted
# a whole board mid-pagination. Measured on a saturated network: 52 of 290
# boards dropped in a single run, which reads exactly like a quiet morning.
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status, headers=None, payload=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 500:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def post(self, url, json=None, **kw):
        self.calls += 1
        return self.responses.pop(0)


def test_a_rate_limited_page_is_retried_not_fatal(monkeypatch):
    from sources import base

    monkeypatch.setattr(base.time, "sleep", lambda _s: None)
    client = _Client([_Resp(429), _Resp(200, payload={"jobPostings": [1]})])
    assert base.post_json(client, "u", {}, retries=2) == {"jobPostings": [1]}
    assert client.calls == 2


def test_retry_after_header_is_honoured(monkeypatch):
    from sources import base

    slept = []
    monkeypatch.setattr(base.time, "sleep", slept.append)
    client = _Client([_Resp(429, {"Retry-After": "2"}), _Resp(200, payload={})])
    base.post_json(client, "u", {}, retries=2)
    assert slept == [2.0]


def test_a_wrong_tenant_is_still_never_retried():
    """probe() reads 401 vs 404 vs 422 to tell 'not on Workday' from 'wrong
    site segment'. Retrying only slows a verdict that will not change."""
    from sources.base import BoardError, post_json

    for status in (400, 401, 404, 422):
        client = _Client([_Resp(status)])
        with pytest.raises(BoardError):
            post_json(client, "u", {}, retries=2)
        assert client.calls == 1, f"{status} must not be retried"


def test_a_degraded_fetch_is_reported_as_the_headline():
    """A short list caused by a broken fetch must not read as a quiet market."""
    source = pathlib.Path("run.py").read_text(encoding="utf-8")
    assert "FETCH DEGRADED" in source
    assert "boards_failed / report.boards_attempted" in source

