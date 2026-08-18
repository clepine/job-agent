"""Budget-guard, scoring, and picking tests. No live API calls anywhere."""

from __future__ import annotations

import json

import pytest

from pipeline.jd import compress_jd, html_to_text
from pipeline.keywords import diff, extract_terms
from pipeline.llm import BudgetExceeded, Ledger, LlmClient, Usage, estimate_tokens
from pipeline.models import Job, canonical_url, normalize_company, url_hash
from pipeline.pick import pick_track
from pipeline.score import build_user_content, resume_summary, score_jobs

from .conftest import StubAnthropic, StubResponse, StubUsage


# --- budget ceiling --------------------------------------------------------

PRICES = {
    "price_input_per_mtok": 3.0,
    "price_output_per_mtok": 15.0,
    "price_cache_write_per_mtok": 3.75,
    "price_cache_read_per_mtok": 0.30,
}


def test_ledger_blocks_a_call_that_would_cross_the_ceiling():
    ledger = Ledger(prices=PRICES, ceiling_usd=0.01)
    # 1M input tokens would cost $3 — far past a $0.01 ceiling.
    with pytest.raises(BudgetExceeded) as exc:
        ledger.check(1_000_000, 1000, "huge")
    assert "ceiling" in str(exc.value)
    assert ledger.aborted


def test_ledger_allows_a_call_within_the_ceiling():
    ledger = Ledger(prices=PRICES, ceiling_usd=0.50)
    ledger.check(2000, 500, "small")   # ~$0.0135
    assert not ledger.aborted


def test_ledger_accumulates_real_usage():
    ledger = Ledger(prices=PRICES, ceiling_usd=1.0)
    ledger.record(Usage(input_tokens=1000, output_tokens=100))
    ledger.record(Usage(input_tokens=1000, output_tokens=100, cache_read_input_tokens=5000))
    assert ledger.calls == 2
    expected = (2000 / 1e6 * 3.0) + (200 / 1e6 * 15.0) + (5000 / 1e6 * 0.30)
    assert ledger.spent_usd == pytest.approx(expected, rel=1e-6)


def test_budget_guard_fires_before_any_request_is_sent(cfg):
    """The whole point: a filter bug must not be able to spend the balance."""
    tight = json.loads(json.dumps(cfg))
    tight["budget"]["max_usd_per_run"] = 0.0000001
    stub = StubAnthropic([StubResponse({"scores": []})])
    llm = LlmClient(tight, client=stub)
    with pytest.raises(BudgetExceeded):
        llm.complete_json(
            system_cached="x" * 50_000,
            user_content="y" * 50_000,
            schema={"type": "object"},
            max_tokens=1000,
            label="test",
        )
    assert stub.messages.calls == [], "a request was sent despite the ceiling"


def test_estimate_is_pessimistic():
    text = "a" * 3400
    assert estimate_tokens(text) >= 1000


# --- scoring ---------------------------------------------------------------

def _jobs(n: int, track="software") -> list[Job]:
    return [
        Job(
            company=f"Co{i}",
            title="Software Engineer",
            location="Durham, NC",
            url=f"https://example.com/{i}",
            description="Requirements\nPython, C++, Linux, Git.",
            track=track,
        )
        for i in range(n)
    ]


def test_score_jobs_batches_and_records_scores(cfg, resume_sw):
    jobs = _jobs(10)
    batch1 = {"scores": [{"id": j.id, "score": 70, "rationale": "good"} for j in jobs[:8]]}
    batch2 = {"scores": [{"id": j.id, "score": 55, "rationale": "ok"} for j in jobs[8:]]}
    stub = StubAnthropic([StubResponse(batch1), StubResponse(batch2)])
    llm = LlmClient(cfg, client=stub)

    scored, usage, warnings = score_jobs(llm, jobs, resume_sw, "software", cfg)
    assert len(scored) == 10
    assert len(stub.messages.calls) == 2, "should batch into 2 calls, not 10"
    assert warnings == []
    assert all(j.fit_score is not None for j in scored)


def test_score_jobs_survives_a_bad_batch(cfg, resume_sw):
    jobs = _jobs(16)
    good = {"scores": [{"id": j.id, "score": 60, "rationale": "ok"} for j in jobs[:8]]}
    stub = StubAnthropic([RuntimeError("transient 529"), StubResponse(good)])
    llm = LlmClient(cfg, client=stub)
    scored, _usage, warnings = score_jobs(llm, jobs, resume_sw, "software", cfg)
    assert len(scored) == 8
    assert any("failed" in w for w in warnings)


def test_score_jobs_stops_at_the_budget_ceiling(cfg, resume_sw):
    tight = json.loads(json.dumps(cfg))
    tight["budget"]["max_usd_per_run"] = 0.0000001
    jobs = _jobs(16)
    stub = StubAnthropic([StubResponse({"scores": []})])
    llm = LlmClient(tight, client=stub)
    scored, _usage, warnings = score_jobs(llm, jobs, resume_sw, "software", tight)
    assert scored == []
    assert stub.messages.calls == []
    assert any("ceiling" in w for w in warnings)


def test_score_prompt_puts_resume_in_the_cached_prefix(cfg, resume_sw):
    jobs = _jobs(2)
    stub = StubAnthropic([StubResponse({"scores": [{"id": jobs[0].id, "score": 50, "rationale": "x"}]})])
    llm = LlmClient(cfg, client=stub)
    score_jobs(llm, jobs, resume_sw, "software", cfg)

    call = stub.messages.calls[0]
    system_text = call["system"][0]["text"]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "MSP430" in system_text or "LangChain" in system_text
    assert "Co0" not in system_text          # per-job content must not be in the prefix
    assert "Co0" in call["messages"][0]["content"]
    assert call["thinking"] == {"type": "disabled"}
    assert call["output_config"]["format"]["type"] == "json_schema"


def test_resume_summary_is_deterministic(resume_sw):
    """A byte-unstable prefix silently destroys the cache."""
    assert resume_summary(resume_sw) == resume_summary(resume_sw)


def test_unknown_id_from_model_is_warned_not_crashed(cfg, resume_sw):
    jobs = _jobs(2)
    stub = StubAnthropic([StubResponse({"scores": [{"id": "nope", "score": 90, "rationale": "x"}]})])
    llm = LlmClient(cfg, client=stub)
    scored, _u, warnings = score_jobs(llm, jobs, resume_sw, "software", cfg)
    assert scored == []
    assert any("unknown id" in w for w in warnings)


# --- pick ------------------------------------------------------------------

def _scored(company, tier, score, track="software", metro_class="primary") -> Job:
    job = Job(
        company=company, title="Software Engineer", location="Durham, NC",
        url=f"https://example.com/{company}", track=track,
    )
    job.tier = tier
    job.fit_score = score
    job.fit_rationale = "r"
    job.metro_class = metro_class
    job.metro = "RTP/Raleigh-Durham"
    return job


def test_pick_applies_the_2_plus_3_tier_quota(cfg):
    candidates = [_scored(f"T1-{i}", 1, 90 - i) for i in range(4)]
    candidates += [_scored(f"T2-{i}", 2, 80 - i) for i in range(6)]
    sel = pick_track(candidates, cfg, "software")
    assert len(sel.jobs) == 5
    assert sum(1 for j in sel.jobs if j.tier == 1) == 2
    assert sum(1 for j in sel.jobs if j.tier == 2) == 3


def test_pick_backfills_and_says_so_when_tier1_is_empty(cfg):
    candidates = [_scored(f"T2-{i}", 2, 80 - i) for i in range(8)]
    sel = pick_track(candidates, cfg, "software")
    assert len(sel.jobs) == 5
    assert sel.backfilled == 2
    assert any("Tier 1" in n for n in sel.notes)


def test_pick_reports_a_short_day_rather_than_padding(cfg):
    sel = pick_track([_scored("Only", 2, 70)], cfg, "hardware")
    assert len(sel.jobs) == 1
    assert sel.short_by == 4
    assert any("cleared the filters" in n for n in sel.notes)


def test_primary_metro_beats_secondary_at_equal_fit(cfg):
    primary = _scored("Near", 2, 70, metro_class="primary")
    secondary = _scored("Far", 2, 70, metro_class="secondary")
    sel = pick_track([secondary, primary], cfg, "software")
    assert sel.jobs[0].company == "Near"


def test_clearance_advantage_is_weighted_up(cfg):
    plain = _scored("Plain", 2, 72)
    cleared = _scored("Defense", 2, 70)
    cleared.clearance_advantage = True
    sel = pick_track([plain, cleared], cfg, "hardware")
    assert sel.jobs[0].company == "Defense"


# --- dedupe / canonicalization --------------------------------------------

def test_canonical_url_strips_tracking_but_keeps_job_id():
    a = "https://boards.greenhouse.io/spacex/jobs/123?gh_jid=123&utm_source=x"
    b = "https://job-boards.greenhouse.io/spacex/jobs/123?gh_jid=123"
    assert canonical_url(a) == canonical_url(b)
    assert url_hash(a) == url_hash(b)


def test_company_normalization_collapses_hpe_variants():
    assert normalize_company("HPE") == normalize_company("HPE (University)")
    assert normalize_company("Wellmark, Inc.") == normalize_company("Wellmark")


def test_dedupe_key_collapses_duplicate_repo_rows():
    a = Job(company="HPE", title="ASIC Verification Engineer",
            location="Durham, North Carolina", url="https://a.example/1")
    b = Job(company="HPE (University)", title="ASIC Verification Engineer",
            location="Durham, North Carolina", url="https://b.example/2")
    assert a.dedupe_key == b.dedupe_key
    assert a.id != b.id   # different URLs, so only the fuzzy key catches this


# --- JD compression --------------------------------------------------------

def test_compress_drops_boilerplate_keeps_requirements():
    jd = """
    About Us
    We are a fast-growing company with a great culture and free snacks.
    Responsibilities
    - Design and verify RTL blocks in Verilog
    - Debug on the bench with an oscilloscope
    Minimum Qualifications
    - BS in Electrical or Computer Engineering
    - Familiarity with FPGA tools
    Benefits
    - 401(k) matching, dental, vision insurance, unlimited PTO
    Equal Employment Opportunity
    We are an equal opportunity employer and consider applicants without regard to race.
    """
    out = compress_jd(jd)
    assert "Verilog" in out and "FPGA" in out and "oscilloscope" in out
    assert "401(k)" not in out and "snacks" not in out
    assert "equal opportunity" not in out.lower()


def test_compress_is_a_real_reduction():
    jd = ("About Us\n" + "We are wonderful. " * 200 + "\nRequirements\n- Python\n- C++\n"
          + "Benefits\n" + "Great perks. " * 200)
    out = compress_jd(jd)
    assert len(out) < len(jd) / 4
    assert "Python" in out


def test_compress_respects_the_hard_cap():
    jd = "Requirements\n" + "\n".join(f"- Experience with tool number {i}" for i in range(500))
    out = compress_jd(jd, max_chars=800)
    assert len(out) <= 820


def test_compress_falls_back_when_there_are_no_headings():
    jd = "You will write Python and C++ code and debug embedded firmware on MSP430 hardware."
    out = compress_jd(jd)
    assert "Python" in out and "MSP430" in out


def test_html_is_flattened():
    assert "Verilog" in html_to_text("<div><ul><li>Verilog</li></ul></div>")


# --- keyword diff ----------------------------------------------------------

def test_keyword_diff_only_reports_terms_present_in_the_jd(resume_hw):
    jd = "Requirements: Verilog, FPGA, SystemVerilog, UVM, oscilloscope debugging."
    d = diff(jd, resume_hw)
    assert "Verilog" in d.matched
    assert "Oscilloscope" in d.matched
    assert "SystemVerilog" in d.missing
    assert "UVM" in d.missing
    for term in d.jd_terms:
        assert term in extract_terms(jd)


def test_keyword_diff_never_invents(resume_sw):
    jd = "Requirements: Python and Linux."
    d = diff(jd, resume_sw)
    assert set(d.matched + d.missing) <= set(d.jd_terms)
    assert "Kubernetes" not in d.jd_terms


def test_surfacing_hints_point_at_real_resume_content(resume_hw):
    jd = "We need RTL design and SystemVerilog experience."
    d = diff(jd, resume_hw)
    for term, where in d.surfacing.items():
        assert where, f"empty surfacing hint for {term}"


# --- staleness + diversity -------------------------------------------------

def test_stale_postings_are_dropped():
    from pipeline.filters import check_age

    assert check_age(30, 180).passed
    assert check_age(180, 180).passed
    r = check_age(1442, 180)
    assert not r.passed and r.stage == "stale"
    assert "1442" in r.reason


def test_unknown_age_is_kept_not_guessed():
    from pipeline.filters import check_age

    assert check_age(None, 180).passed


def test_pick_caps_slots_per_company(cfg):
    """One company with a big board must not take the whole email."""
    candidates = [_scored("BigCo", 2, 99 - i) for i in range(6)]
    candidates += [_scored(f"Other{i}", 2, 50 - i) for i in range(6)]
    sel = pick_track(candidates, cfg, "software")
    assert len(sel.jobs) == 5
    assert sum(1 for j in sel.jobs if j.company == "BigCo") <= 2


# --- resume fingerprint + score staleness ---------------------------------

def test_cosmetic_resume_edits_do_not_invalidate_scores(resume_sw):
    """Re-scoring costs money. A phone number change must not trigger it."""
    import copy
    from pipeline.fingerprint import resume_hash

    before = resume_hash(resume_sw)
    edited = copy.deepcopy(resume_sw)
    edited["contact"]["phone"] = "555-000-0000"
    edited["contact"]["linkedin"] = "linkedin.com/in/someone-else"
    edited["meta"]["source_file"] = "renamed.txt"
    edited["leadership"][0]["bullets"] = ["Reworded entirely."]
    assert resume_hash(edited) == before


def test_substantive_resume_edits_do_invalidate_scores(resume_sw):
    import copy
    from pipeline.fingerprint import resume_hash

    before = resume_hash(resume_sw)
    for mutate in (
        lambda r: r["skills"]["languages"].append("Rust"),
        lambda r: r["education"]["coursework"].append("Operating Systems"),
        lambda r: r["projects"].pop(),
        lambda r: r["experience"][0]["subsections"][0]["bullets"].append("New bullet."),
    ):
        edited = copy.deepcopy(resume_sw)
        mutate(edited)
        assert resume_hash(edited) != before, mutate


def test_resume_hash_is_stable_across_calls(resume_hw):
    from pipeline.fingerprint import resume_hash

    assert resume_hash(resume_hw) == resume_hash(resume_hw)


def test_stale_scores_are_re_queued_and_excluded_from_candidates(tmp_path):
    from pipeline import db as dbm

    path = tmp_path / "t.db"
    job = Job(company="X", title="Software Engineer", location="Durham, NC",
              url="https://example.com/1", track="software")
    with dbm.connect(path) as conn:
        dbm.upsert(conn, [job])
        job.fit_score = 80
        job.fit_rationale = "ok"
        dbm.save_scores(conn, [job], "HASH_OLD")

        # Same resume: scored, eligible, nothing to re-score.
        assert len(dbm.candidates(conn, "software", "HASH_OLD")) == 1
        assert dbm.unscored(conn, "software", 10, "HASH_OLD") == []
        assert dbm.count_stale_scores(conn, "HASH_OLD") == 0

        # Resume edited: the score is stale, so it leaves the candidate pool
        # and re-enters the scoring queue.
        assert dbm.candidates(conn, "software", "HASH_NEW") == []
        assert len(dbm.unscored(conn, "software", 10, "HASH_NEW")) == 1
        assert dbm.count_stale_scores(conn, "HASH_NEW") == 1


def test_unscored_puts_new_jobs_before_stale_rescores(tmp_path):
    from pipeline import db as dbm

    path = tmp_path / "t.db"
    fresh = Job(company="New", title="Software Engineer", location="Durham, NC",
                url="https://example.com/new", track="software")
    stale = Job(company="Old", title="Software Engineer", location="Durham, NC",
                url="https://example.com/old", track="software")
    with dbm.connect(path) as conn:
        dbm.upsert(conn, [fresh, stale])
        stale.fit_score = 95
        stale.fit_rationale = "was great"
        dbm.save_scores(conn, [stale], "HASH_OLD")
        queue = dbm.unscored(conn, "software", 10, "HASH_NEW")
        assert [j.company for j in queue] == ["New", "Old"]


# --- committed state ledger ------------------------------------------------

def test_state_roundtrip_is_lossless_and_sorted(tmp_path):
    import json

    from pipeline import db as dbm, state as state_mod

    jobs = [
        Job(company=f"Co{i}", title="Software Engineer", location="Durham, NC",
            url=f"https://example.com/{i}", track="software",
            description="a long description that must NOT be persisted")
        for i in range(5)
    ]
    src, dst = tmp_path / "a.db", tmp_path / "b.db"
    ledger = tmp_path / "seen.json"

    with dbm.connect(src) as conn:
        dbm.upsert(conn, jobs)
        jobs[0].fit_score = 70
        jobs[0].fit_rationale = "good"
        dbm.save_scores(conn, [jobs[0]], "HASH1")
        assert state_mod.dump(conn, ledger) == 5

    payload = json.loads(ledger.read_text())
    ids = [j["id"] for j in payload["jobs"]]
    assert ids == sorted(ids), "records must be sorted for readable diffs"
    assert ledger.read_text().endswith("\n")
    assert "a long description" not in ledger.read_text(), "descriptions must not be committed"

    with dbm.connect(dst) as conn:
        assert state_mod.load(conn, ledger) == 5
        restored = dbm.get(conn, jobs[0].id)
        assert restored is not None
        assert restored.fit_score == 70
        assert restored.resume_hash == "HASH1"
        assert dbm.known_ids(conn) == {j.id for j in jobs}


def test_state_dump_is_byte_stable(tmp_path):
    from pipeline import db as dbm, state as state_mod

    jobs = [Job(company=f"Co{i}", title="T", url=f"https://e.com/{i}") for i in range(4)]
    path = tmp_path / "a.db"
    with dbm.connect(path) as conn:
        dbm.upsert(conn, jobs)
        a, b = tmp_path / "1.json", tmp_path / "2.json"
        state_mod.dump(conn, a)
        state_mod.dump(conn, b)
        # generated_at is date-granular, so two dumps in one run are identical.
        assert a.read_text() == b.read_text()


def test_missing_state_file_is_not_an_error(tmp_path):
    from pipeline import db as dbm, state as state_mod

    with dbm.connect(tmp_path / "a.db") as conn:
        assert state_mod.load(conn, tmp_path / "nope.json") == 0


def test_state_load_does_not_clobber_local_rows(tmp_path):
    from pipeline import db as dbm, state as state_mod

    job = Job(company="Co", title="Software Engineer", url="https://e.com/1")
    ledger = tmp_path / "seen.json"
    with dbm.connect(tmp_path / "a.db") as conn:
        dbm.upsert(conn, [job])
        state_mod.dump(conn, ledger)

    fresh = Job(company="Co", title="Software Engineer", url="https://e.com/1",
                description="freshly fetched body")
    with dbm.connect(tmp_path / "b.db") as conn:
        dbm.upsert(conn, [fresh])
        state_mod.load(conn, ledger)
        assert dbm.get(conn, fresh.id).description == "freshly fetched body"
