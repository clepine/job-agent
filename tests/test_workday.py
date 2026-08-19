"""Workday CxS fetcher — tested against RECORDED fixtures, never live calls.

The fixtures in tests/fixtures/ are real responses captured on 2026-08-18 from
analogdevices (list) and nvidia (detail). They exist so the contract this
fetcher depends on is pinned: if Workday changes the shape of `postedOn`,
`locationsText` or `externalPath`, these fail rather than the source silently
returning nothing three months from now.

No test here opens a socket. A FakeClient stands in for httpx.Client.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import filters
from pipeline.geo import classify_location
from pipeline.models import Job
from sources import hydrate, workday
from sources.base import BoardError

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    with open(FIXTURES / name, encoding="utf-8") as fh:
        return json.load(fh)


PAGE0 = _fixture("workday_list_page0.json")
STALE = _fixture("workday_list_stale.json")
DETAIL = _fixture("workday_detail.json")

SLUG = "analogdevices|wd1|External"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 500:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Serves scripted pages by offset. Records every request made."""

    def __init__(self, pages=None, status=200, get_payload=None):
        self.pages = pages or {}
        self.status = status
        self.get_payload = get_payload
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []

    def post(self, url, json=None, **kwargs):
        self.posts.append((url, json))
        if self.status != 200:
            return FakeResponse({"errorCode": "x"}, self.status)
        return FakeResponse(self.pages.get(json["offset"], {"jobPostings": []}))

    def get(self, url, **kwargs):
        self.gets.append(url)
        return FakeResponse(self.get_payload or {})


# ---------------------------------------------------------------------------
# slug parsing
# ---------------------------------------------------------------------------


def test_parse_slug_splits_tenant_shard_site():
    assert workday.parse_slug(SLUG) == ("analogdevices", "wd1", "External")


@pytest.mark.parametrize("bad", ["", "tenant|shard", "a|b|c|d", "a||c", "tenant"])
def test_malformed_slug_raises(bad):
    with pytest.raises(ValueError):
        workday.parse_slug(bad)


# ---------------------------------------------------------------------------
# posted date
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Posted Today", 0),
        ("Posted Yesterday", 1),
        ("Posted 4 Days Ago", 4),
        ("Posted 30+ Days Ago", 30),
        ("Posted Over 30 Days Ago", 30),
        ("Just posted", 0),
        ("", None),
        (None, None),
        ("Posted Recently", None),
    ],
)
def test_parse_posted_on(text, expected):
    assert workday.parse_posted_on(text) == expected


def test_thirty_plus_days_is_a_floor_and_fails_the_staleness_gate():
    """'30+ Days Ago' must reject under a 7-day cutoff, never squeak through."""
    days = workday.parse_posted_on("Posted 30+ Days Ago")
    assert filters.check_age(days, 7).passed is False


# ---------------------------------------------------------------------------
# location — the bug that silently zeroed the whole source
# ---------------------------------------------------------------------------


def test_country_first_location_is_unmatchable_until_expanded():
    """The regression that makes this source worth having.

    pipeline/geo.py reads the segment BEFORE the first comma as the city, so
    Workday's native "US, NC, Durham" classifies as "none" and every US posting
    is dropped on location. Expansion is what fixes it.
    """
    assert classify_location("US, NC, Durham") == ("none", None)
    assert classify_location(workday.expand_location("US, NC, Durham")) == (
        "primary",
        "RTP/Raleigh-Durham",
    )


@pytest.mark.parametrize(
    "raw,metro_class,metro",
    [
        ("US, NC, Durham", "primary", "RTP/Raleigh-Durham"),
        ("US, MA, Boston", "primary", "Boston"),
        # ADI puts the BUILDING name first.
        ("Alpha, Chelmsford, MA", "primary", "Boston"),
        ("Rio Robles, San Jose, CA", "secondary", "Bay Area"),
        # RTX bolts a site code and street address on.
        ("US-MA-ANDOVER-AN1 ~ 350 Lowell St ~ AN1 ESSEX BLDG", "primary", "Boston"),
        # Northrop uses hyphens and full state names.
        ("United States-Alabama-Huntsville", "secondary", "Huntsville"),
        ("Westford, Massachusetts, United States of America", "primary", "Boston"),
        ("US, CA, Remote", "remote_us", "Remote (US)"),
    ],
)
def test_expand_location_reaches_the_right_metro(raw, metro_class, metro):
    assert classify_location(workday.expand_location(raw)) == (metro_class, metro)


@pytest.mark.parametrize(
    "raw",
    [
        "Markham, Ontario, Canada",
        "Taiwan, Hsinchu",
        "India, Bangalore, RMZ",
        "Heredia, Heredia, Costa Rica",
        "Ireland, Limerick",
        "Philippines, Cavite, GTC",
    ],
)
def test_expansion_never_smuggles_a_non_us_posting_in(raw):
    assert classify_location(workday.expand_location(raw))[0] == "none"


def test_expansion_is_additive_and_preserves_the_original():
    """Safety property: expansion may only ADD fragments.

    Because the raw string is always the first fragment and geo.py takes the
    best class across fragments, expansion can turn a "none" into a match but
    can never turn a match into a "none".
    """
    for raw in ["Greater Chicago Area", "Fort Mill/Charlotte", "Cambridge, MA"]:
        expanded = workday.expand_location(raw)
        assert expanded.split(";")[0].strip() == raw
        assert classify_location(expanded)[0] == classify_location(raw)[0]


def test_state_guard_still_blocks_the_clifton_park_false_positive():
    """geo.py's own regression: 'Clifton Park, New York' is not NYC."""
    assert classify_location(workday.expand_location("Clifton Park, New York")) == (
        "none",
        None,
    )


@pytest.mark.parametrize(
    "path,metro",
    [
        ("/job/US-NC-Durham/X_1", "RTP/Raleigh-Durham"),
        # The hyphen is both separator and space; "Santa Clara" must survive.
        ("/job/US-CA-Santa-Clara/X_1", "Bay Area"),
        ("/job/United-States-Massachusetts-Andover/X_1", "Boston"),
        ("/job/Cambridge-MA/X_1", "Boston"),
    ],
)
def test_location_recovered_from_the_url_path(path, metro):
    assert classify_location(workday.location_from_path(path))[1] == metro


def test_multi_location_placeholder_falls_back_to_the_path():
    """'6 Locations' is a count, not a place — the path carries the real site."""
    raw = {"locationsText": "6 Locations", "externalPath": "/job/US-NC-Durham/X_1"}
    assert classify_location(workday.posting_location(raw))[1] == "RTP/Raleigh-Durham"


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def test_fetch_normalizes_a_real_page_into_jobs():
    client = FakeClient({0: PAGE0})
    jobs = workday.fetch(client, "Analog Devices", SLUG, "hardware", max_pages=1)

    assert len(jobs) == len(PAGE0["jobPostings"])
    job = jobs[0]
    assert isinstance(job, Job)
    assert job.company == "Analog Devices"
    assert job.ats == "workday"
    assert job.source == f"workday:{SLUG}"
    assert job.track == "hardware"
    assert job.title
    # Descriptions are NOT in the list response; survivors are hydrated later.
    assert job.description == ""
    assert job.needs_hydration is True


def test_fetch_builds_the_canonical_apply_url():
    """Must match the detail endpoint's own `externalUrl`, or dedupe breaks."""
    client = FakeClient({0: PAGE0})
    job = workday.fetch(client, "ADI", SLUG, "hardware", max_pages=1)[0]
    path = PAGE0["jobPostings"][0]["externalPath"]
    assert job.url == f"https://analogdevices.wd1.myworkdayjobs.com/External{path}"


def test_fetch_posts_the_documented_body_and_never_exceeds_the_limit_cap():
    """Workday rejects limit > 20 with HTTP 400, so 20 is a hard ceiling."""
    client = FakeClient({0: PAGE0, 20: PAGE0, 40: {"jobPostings": []}})
    workday.fetch(client, "ADI", SLUG, "hardware", max_pages=3)

    url, body = client.posts[0]
    assert url == (
        "https://analogdevices.wd1.myworkdayjobs.com"
        "/wday/cxs/analogdevices/External/jobs"
    )
    assert set(body) == {"appliedFacets", "limit", "offset", "searchText"}
    assert all(b["limit"] == workday.PAGE_LIMIT <= 20 for _, b in client.posts)
    assert [b["offset"] for _, b in client.posts] == [0, 20, 40]


def test_pagination_stops_on_a_short_page():
    short = {"jobPostings": PAGE0["jobPostings"][:5]}
    client = FakeClient({0: PAGE0, 20: short})
    workday.fetch(client, "ADI", SLUG, "hardware", max_pages=10)
    assert len(client.posts) == 2


def test_pagination_stops_once_a_whole_page_is_past_the_staleness_cutoff():
    """The optimization the whole source depends on.

    Workday sorts strictly newest-first and caps a page at 20, so a
    4,441-posting board would otherwise cost 223 requests every morning.
    """
    client = FakeClient({0: PAGE0, 20: STALE, 40: PAGE0})
    jobs = workday.fetch(client, "ADI", SLUG, "hardware", max_age_days=7, max_pages=10)

    assert len(client.posts) == 2, "should not have asked for a third page"
    assert all(p["postedOn"] == "Posted 30+ Days Ago" for p in STALE["jobPostings"])
    assert len(jobs) == len(PAGE0["jobPostings"]) + len(STALE["jobPostings"])


def test_no_early_stop_without_a_cutoff():
    client = FakeClient({0: PAGE0, 20: STALE, 40: {"jobPostings": []}})
    workday.fetch(client, "ADI", SLUG, "hardware", max_age_days=None, max_pages=10)
    assert len(client.posts) == 3


def test_unparseable_dates_never_trigger_the_early_stop():
    """Unknown age is not old age — stopping on it would silently truncate."""
    murky = {
        "jobPostings": [
            {**p, "postedOn": "Posted Recently"} for p in STALE["jobPostings"]
        ]
    }
    client = FakeClient({0: murky, 20: {"jobPostings": []}})
    workday.fetch(client, "ADI", SLUG, "hardware", max_age_days=7, max_pages=10)
    assert len(client.posts) == 2


def test_max_pages_is_a_hard_backstop():
    client = FakeClient({i * 20: PAGE0 for i in range(50)})
    workday.fetch(client, "ADI", SLUG, "hardware", max_age_days=None, max_pages=3)
    assert len(client.posts) == 3


def test_repeated_postings_across_pages_are_not_duplicated():
    client = FakeClient({0: PAGE0, 20: PAGE0, 40: {"jobPostings": []}})
    jobs = workday.fetch(client, "ADI", SLUG, "hardware", max_pages=5)
    assert len(jobs) == len(PAGE0["jobPostings"])


def test_postings_without_a_title_or_path_are_skipped():
    page = {"jobPostings": [
        {"title": "", "externalPath": "/job/US-NC-Durham/A_1", "postedOn": "Posted Today"},
        {"title": "Engineer I", "externalPath": "", "postedOn": "Posted Today"},
        {"title": "Engineer I", "externalPath": "/job/US-NC-Durham/B_1",
         "locationsText": "US, NC, Durham", "postedOn": "Posted Today"},
    ]}
    client = FakeClient({0: page})
    assert len(workday.fetch(client, "ADI", SLUG, "hardware", max_pages=1)) == 1


def test_posted_at_is_derived_from_the_relative_string():
    page = {"jobPostings": [{"title": "Engineer I", "externalPath": "/job/US-NC-Durham/B_1",
                             "locationsText": "US, NC, Durham", "postedOn": "Posted 4 Days Ago"}]}
    job = workday.fetch(FakeClient({0: page}), "ADI", SLUG, "hardware", max_pages=1)[0]
    assert job.age_days == 4


def test_track_both_collapses_to_software_like_the_other_fetchers():
    client = FakeClient({0: PAGE0})
    assert workday.fetch(client, "ADI", SLUG, "both", max_pages=1)[0].track == "software"


# ---------------------------------------------------------------------------
# failure behaviour — one dead board must never abort a run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 404, 422])
def test_client_errors_raise_boarderror_without_retrying(status):
    """4xx from Workday is deterministic (bad slug, bad limit). Retrying it
    just multiplies the wait on a board that will never answer."""
    client = FakeClient(status=status)
    with pytest.raises(BoardError):
        workday.fetch(client, "ADI", SLUG, "hardware", max_pages=3, retries=2)
    assert len(client.posts) == 1


def test_probe_reports_a_reason_instead_of_raising():
    ok, total, reason = workday.probe(FakeClient(status=422), SLUG)
    assert ok is False and total == 0 and "422" in reason


def test_probe_counts_postings_on_a_live_board():
    ok, total, reason = workday.probe(FakeClient({0: PAGE0}), SLUG)
    assert ok is True and total == PAGE0["total"] and reason == ""


def test_probe_rejects_a_live_endpoint_with_an_empty_board():
    """MITRE answers 200 with zero postings — that is not a usable board."""
    ok, total, _ = workday.probe(FakeClient({0: {"total": 0, "jobPostings": []}}), SLUG)
    assert ok is False and total == 0


# ---------------------------------------------------------------------------
# hydration
# ---------------------------------------------------------------------------


WD_URL = (
    "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
    "/job/US-NC-Durham/Senior-AI-Security-Researcher_JR2017578"
)


def _wd_job(url=WD_URL, title="Senior AI Security Researcher", **kw):
    return Job(company="Nvidia", title=title, url=url, ats="workday",
               needs_hydration=True, **kw)


def test_hydrate_fetches_the_cxs_detail_endpoint():
    client = FakeClient(get_payload=DETAIL)
    job = _wd_job()
    assert hydrate.hydrate_one(client, job) is True
    assert client.gets == [
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite"
        "/job/US-NC-Durham/Senior-AI-Security-Researcher_JR2017578"
    ]
    assert job.description
    assert job.needs_hydration is False


def test_hydrate_recovers_the_full_title_and_clears_the_truncation_flag():
    job = _wd_job(title="Senior AI Security Resea", title_truncated=True)
    hydrate.hydrate_one(FakeClient(get_payload=DETAIL), job)
    assert job.title == DETAIL["jobPostingInfo"]["title"]
    assert job.title_truncated is False


def test_hydrate_merges_additional_locations():
    """A '6 Locations' posting only reveals its other sites here."""
    job = _wd_job()
    hydrate.hydrate_one(FakeClient(get_payload=DETAIL), job)
    assert ";" in job.location
    assert classify_location(job.location)[0] != "none"


def test_hydrate_upgrades_the_relative_date_to_the_absolute_start_date():
    job = _wd_job(posted_at=datetime.now(timezone.utc) - timedelta(days=30))
    hydrate.hydrate_one(FakeClient(get_payload=DETAIL), job)
    assert job.posted_at.date().isoformat() == DETAIL["jobPostingInfo"]["startDate"]


def test_aggregator_workday_urls_are_hydratable():
    """README rows land as ats='workday' too, and used to be unhydratable."""
    assert hydrate.hydratable(_wd_job(url="https://leidos.wd5.myworkdayjobs.com"
                                          "/External/job/Shiloh-IL/Dev_R-1"))


def test_locale_segment_in_an_apply_url_is_stripped():
    """Some tenants serve '/en-US/PfizerCareers/job/...'; the locale is not
    part of the CxS path and a naive split would send it upstream."""
    client = FakeClient(get_payload=DETAIL)
    hydrate.hydrate_one(client, _wd_job(
        url="https://pfizer.wd1.myworkdayjobs.com/en-US/PfizerCareers"
            "/job/Cambridge-MA/ML-Engineer_4961809-1"))
    assert client.gets == [
        "https://pfizer.wd1.myworkdayjobs.com/wday/cxs/pfizer/PfizerCareers"
        "/job/Cambridge-MA/ML-Engineer_4961809-1"
    ]


def test_hydration_failure_is_soft():
    class Boom(FakeClient):
        def get(self, url, **kwargs):
            raise RuntimeError("network down")

    job = _wd_job()
    assert hydrate.hydrate_one(Boom(), job) is False
    assert job.needs_hydration is True


# ---------------------------------------------------------------------------
# orchestrator wiring
# ---------------------------------------------------------------------------


def test_workday_is_registered_and_enabled():
    from pipeline import fetch as fetch_mod

    assert fetch_mod.FETCHERS["workday"] is workday.fetch
    assert workday.ENABLED is True, "the stub flag must stay flipped on"


def test_companies_yaml_exposes_validated_workday_boards():
    """companies.yaml keeps Workday under its own key; load_companies() must
    fold it into the same list the other four ATSes come from, or the fetcher
    is wired up and still never called."""
    from pipeline import fetch as fetch_mod

    boards = fetch_mod.load_companies()
    workday_boards = [b for b in boards if b.get("ats") == "workday"]
    live = [b for b in workday_boards if b.get("valid")]

    assert len(workday_boards) > 100, "workday_boards key was not merged in"
    assert len(live) > 100, "the probe results were not written back"
    assert all(b.get("postings_at_validation", 0) > 0 for b in live)
    # Dead boards must say WHY, so nobody has to re-probe to learn a slug is bad.
    assert all(b.get("note") for b in workday_boards if not b.get("valid"))


def test_fetch_boards_passes_the_staleness_cutoff_to_workday_only():
    """The early-stop is the difference between ~10 requests a board and ~220,
    and it only happens if run.py hands the cutoff down."""
    import inspect

    from pipeline import fetch as fetch_mod

    source = inspect.getsource(fetch_mod.fetch_boards)
    assert "max_posting_age_days" in source
    assert "workday_max_pages" in source

    params = inspect.signature(workday.fetch).parameters
    assert params["max_age_days"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["max_pages"].kind is inspect.Parameter.KEYWORD_ONLY


def test_other_fetchers_are_not_handed_workday_kwargs():
    """greenhouse.fetch(client, company, slug, track) takes no extras; passing
    them would raise and kill four working sources."""
    import inspect

    from sources import ashby, greenhouse, lever, smartrecruiters

    for module in (greenhouse, lever, ashby, smartrecruiters):
        params = list(inspect.signature(module.fetch).parameters)
        assert params == ["client", "company", "slug", "track"], module.__name__
