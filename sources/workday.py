"""Workday CxS fetcher (PLAN.md §2 stage 1, "secondary").

Workday is where the Tier-2 hardware list actually lives — Analog Devices,
Qorvo, Wolfspeed, Teradyne, Infineon, RTX/Raytheon, BAE, HPE, Lenovo, and most
large defense and semiconductor employers. Greenhouse/Lever/Ashby skew hard
toward startups, so without this source the pool is structurally biased away
from the roles the hardware resume targets.

THE CONTRACT, verified empirically on 2026-08-18 against nvidia, analogdevices,
hpe and globalhr (RTX). Several details differ from what the old stub's
docstring predicted; each difference below was measured, not assumed.

    POST https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    Content-Type: application/json
    {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

    -> {"total": 1025,
        "jobPostings": [{"title": ...,
                         "externalPath": "/job/US-OR-Beaverton/Engineer_R264661",
                         "locationsText": "US, OR, Beaverton",
                         "postedOn": "Posted Today",
                         "bulletFields": ["R264661"]}, ...],
        "facets": [...]}

  * NO session cookie is required. The old stub warned that "several tenants
    gate the endpoint behind a session cookie obtained from the HTML board
    first". That was not observed on any tenant that answers at all — a bare
    POST works, and the tenants that fail return 422 from every shard, which is
    a bad-slug signal rather than a missing-cookie one.

  * `limit` IS CAPPED AT 20. `limit: 21` returns HTTP 400. There is no way to
    pull a board in fewer requests, which is what makes the early-stop below
    load-bearing rather than a nicety.

  * `total` IS ONLY MEANINGFUL ON THE FIRST PAGE. At offset 1000 the same board
    reports `total: 0` while still returning 20 postings. Pagination therefore
    terminates on a short page, never on a running comparison against `total`.

  * RESULTS ARE SORTED STRICTLY NEWEST-FIRST. Measured on analogdevices: offset
    0 is "Posted Today", offset 100 is "Posted 3 Days Ago", offset 300 is
    "Posted 12 Days Ago", offset 600 is "Posted 30+ Days Ago". This is the
    entire reason a 4,441-posting board is affordable: with
    limits.max_posting_age_days set, we stop as soon as one whole page is older
    than the cutoff, so a normal day costs a handful of requests per board
    rather than one per 20 postings.

  * WORKDAY WRITES LOCATIONS COUNTRY-FIRST ("US, NC, Durham"). pipeline/geo.py
    matches the city phrase only in the segment BEFORE the first comma, so
    passing these through verbatim classifies every US posting as "none" and
    the whole source silently returns nothing. They are reversed to
    "Durham, NC" here, at the source, rather than by loosening the shared geo
    matcher — that matcher is deliberately strict and is covered by the filter
    fixtures.

  * `locationsText` IS A PLACEHOLDER FOR MULTI-SITE POSTINGS: the literal
    string "6 Locations" rather than any location. The real list is only on the
    detail endpoint, but `externalPath` embeds the primary site, so the
    geography filter never needs a detail call. See sources/hydrate.py, which
    merges `additionalLocations` in for survivors.

Descriptions are NOT in the list response — they need one GET per posting
against the detail endpoint. That request is deliberately not made here.
sources/hydrate.py runs after the title/location filters, so we pay it only for
postings that already survived, exactly as the other fetchers do.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import unquote

import httpx

from pipeline.models import Job
from .base import post_json

ENABLED = True

CXS_JOBS = "https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
BOARD_URL = "https://{tenant}.{shard}.myworkdayjobs.com/{site}{path}"

# Workday rejects limit > 20 with HTTP 400. Not a tunable.
PAGE_LIMIT = 20

# Backstop for when limits.max_posting_age_days is unset, or a tenant ignores
# the newest-first sort. See fetch.workday_max_pages in config.yaml for why the
# configured value is 60: at 25 the cap fired before the staleness cutoff on
# RTX, Northrop Grumman and Thermo Fisher, silently truncating them.
DEFAULT_MAX_PAGES = 60


def parse_slug(slug: str) -> tuple[str, str, str]:
    """companies.yaml stores Workday boards as 'tenant|shard|site'."""
    parts = (slug or "").split("|")
    if len(parts) != 3 or not all(p.strip() for p in parts):
        raise ValueError(f"malformed workday slug {slug!r}; expected 'tenant|wdN|site'")
    return parts[0].strip(), parts[1].strip(), parts[2].strip()


# --------------------------------------------------------------------------
# posted date
# --------------------------------------------------------------------------

# "Posted Today" | "Posted Yesterday" | "Posted 3 Days Ago" | "Posted 30+ Days Ago"
_POSTED_DAYS = re.compile(r"(?:over\s+)?(\d+)\s*\+?\s*day", re.I)


def parse_posted_on(text: str | None) -> Optional[int]:
    """Workday's relative posted string -> age in days. None if unparseable.

    "30+ Days Ago" becomes 30, a floor rather than a guess: the posting is at
    least that old, which is all the staleness filter needs to reject it.
    """
    if not text:
        return None
    low = text.lower()
    if "today" in low or "just posted" in low:
        return 0
    if "yesterday" in low:
        return 1
    m = _POSTED_DAYS.search(low)
    if m:
        return int(m.group(1))
    return None


def _posted_at(text: str | None, now: Optional[datetime] = None) -> Optional[datetime]:
    days = parse_posted_on(text)
    if days is None:
        return None
    return (now or datetime.now(timezone.utc)) - timedelta(days=days)


# --------------------------------------------------------------------------
# location
# --------------------------------------------------------------------------

# "6 Locations" / "2 Locations" — a count, not a place.
_MULTI_PLACEHOLDER = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)
_PATH_LOCATION = re.compile(r"^/job/([^/]+)/")

# Every Workday tenant writes locations in its own order and with its own
# separator. Measured across 2,600 postings from the 130 live boards:
#
#   "US, NC, Durham"                                    nvidia      country first
#   "Alpha, Chelmsford, MA"                             ADI         BUILDING first
#   "US-MA-ANDOVER-AN1 ~ 350 Lowell St ~ AN1 ESSEX BLDG"  RTX       code + street
#   "United States-Alabama-Huntsville"                  Northrop    hyphens
#   "Farmingdale-New York-United States of America"     JCI         hyphens
#   "Westford, Massachusetts, United States of America" HPE         city first
#   "US - DC, Washington" / "Tampa - FL - US"           mixed
#
# pipeline/geo.py matches a city phrase only in the segment BEFORE the first
# comma, with a state guard. That is a deliberate rule — it is what stopped
# "Clifton Park, New York" from being read as NYC — so it is not loosened here.
# Instead this module hands geo.py extra "City, ST" fragments to consider,
# joined with ";" because classify_location() already splits multi-site strings
# on ";" and takes the best metro class across them.
#
# The expansion is strictly ADDITIVE: the original string is always kept as one
# of the fragments, so this can only ever turn a "none" into a match, never the
# reverse. Each candidate pairs a token with the US state found in the SAME
# string, so geo.py's state guard still does its job and a building name that
# happens to collide with a metro city ("Aurora") cannot match the wrong state.
_TOKEN_SPLIT = re.compile(r"[,\-~/|]+")
_MAX_CANDIDATES = 6

_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
_STATE_CODES = set(_US_STATES.values())
# Country/among-the-noise tokens that are never a city worth pairing.
_NOT_A_CITY = {
    "us", "usa", "u.s.", "u.s.a.", "united states", "united states of america",
    "remote", "home office", "virtual", "onsite", "hybrid", "field",
}


def _state_of(token: str) -> Optional[str]:
    """The US state a token names, as a 2-letter code. None if it names none."""
    t = " ".join(token.split())
    if t.upper() in _STATE_CODES and len(t) == 2:
        return t.upper()
    return _US_STATES.get(t.lower())


def expand_location(text: str) -> str:
    """Add 'City, ST' fragments that pipeline/geo.py can actually match.

    Returns the original string plus any candidates, ';'-joined. Additive by
    construction — see the block comment above.
    """
    raw = " ".join((text or "").split())
    if not raw:
        return ""

    tokens = [" ".join(t.split()) for t in _TOKEN_SPLIT.split(raw)]
    tokens = [t for t in tokens if t]

    state = next((s for t in tokens if (s := _state_of(t))), None)
    if state is None:
        return raw

    candidates: list[str] = []
    for token in tokens:
        if _state_of(token) or token.lower() in _NOT_A_CITY:
            continue
        if not any(ch.isalpha() for ch in token):
            continue
        candidates.append(f"{token}, {state}")
        if len(candidates) >= _MAX_CANDIDATES:
            break

    return "; ".join(dict.fromkeys([raw, *candidates]))


# Retained under its documented name because sources/hydrate.py normalizes the
# detail endpoint's `location` and `additionalLocations` through the same rules.
normalize_location = expand_location


# In a URL path the hyphen is BOTH the field separator and the space inside a
# city name ("US-CA-Santa-Clara"), so the generic tokenizer would read
# "Santa, Clara" as two places. These two shapes cover every US path observed;
# anything else falls back to the generic expander.
_PATH_US_CODE = re.compile(r"^(?:US|USA)-([A-Z]{2})-(.+)$")
_PATH_US_NAMED = re.compile(r"^United-States-([A-Za-z]+(?:-[A-Za-z]+)?)-(.+)$")


def location_from_path(path: str) -> str:
    """Recover the primary site from '/job/US-NC-Durham/Some-Title_JR123'.

    Used when `locationsText` is the "N Locations" placeholder. Multi-site
    postings would otherwise carry no parseable location at all, and they skew
    heavily toward the big employers this source exists for.
    """
    m = _PATH_LOCATION.match(path or "")
    if not m:
        return ""
    raw = unquote(m.group(1))

    cm = _PATH_US_CODE.match(raw)
    if cm:
        return expand_location(f"{cm.group(2).replace('-', ' ')}, {cm.group(1)}")

    nm = _PATH_US_NAMED.match(raw)
    if nm and _state_of(nm.group(1).replace("-", " ")):
        return expand_location(
            f"{nm.group(2).replace('-', ' ')}, {nm.group(1).replace('-', ' ')}"
        )

    return expand_location(raw.replace("-", ", "))


def posting_location(raw: dict) -> str:
    text = (raw.get("locationsText") or "").strip()
    if not text or _MULTI_PLACEHOLDER.match(text):
        return location_from_path(raw.get("externalPath") or "")
    return expand_location(text)


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------


def _to_job(
    raw: dict, company: str, slug: str, tenant: str, shard: str, site: str, track: str
) -> Optional[Job]:
    path = raw.get("externalPath") or ""
    title = (raw.get("title") or "").strip()
    if not path or not title:
        return None
    return Job(
        company=company,
        title=title,
        location=posting_location(raw),
        url=BOARD_URL.format(tenant=tenant, shard=shard, site=site, path=path),
        ats="workday",
        description="",
        posted_at=_posted_at(raw.get("postedOn")),
        source=f"workday:{slug}",
        track=track if track != "both" else "software",
        # The list response carries no body. hydrate.py fills it in for the
        # postings that survive the title and location filters.
        needs_hydration=True,
    )


def fetch(
    client: httpx.Client,
    company: str,
    slug: str,
    track: str,
    *,
    max_age_days: Optional[int] = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    retries: int = 2,
) -> list[Job]:
    """Pull one Workday board, newest-first, stopping at the staleness cutoff.

    Raises BoardError on a dead or misconfigured board; fetch_many() catches it
    so one bad tenant can never abort a run.
    """
    tenant, shard, site = parse_slug(slug)
    url = CXS_JOBS.format(tenant=tenant, shard=shard, site=site)

    jobs: list[Job] = []
    seen_paths: set[str] = set()

    for page in range(max(1, int(max_pages))):
        data = post_json(
            client,
            url,
            {
                "appliedFacets": {},
                "limit": PAGE_LIMIT,
                "offset": page * PAGE_LIMIT,
                "searchText": "",
            },
            retries=retries,
        )
        postings = (data or {}).get("jobPostings") or []
        if not postings:
            break

        ages: list[Optional[int]] = []
        for raw in postings:
            path = raw.get("externalPath") or ""
            # Deep offsets on some tenants re-serve the tail of the previous
            # page; without this a board with a short tail can loop.
            if path and path in seen_paths:
                continue
            seen_paths.add(path)
            ages.append(parse_posted_on(raw.get("postedOn")))
            job = _to_job(raw, company, slug, tenant, shard, site, track)
            if job is not None:
                jobs.append(job)

        if len(postings) < PAGE_LIMIT:
            break

        # Results are strictly newest-first, so once an ENTIRE page is past the
        # staleness cutoff nothing later can qualify. Requires every age on the
        # page to have parsed — an unparsed date is treated as "unknown", and
        # unknown age is never grounds for stopping.
        if (
            max_age_days
            and ages
            and all(a is not None and a > int(max_age_days) for a in ages)
        ):
            break

    return jobs


# Workday's status codes are diagnostic, and the distinctions matter when
# deciding whether a dead board is worth re-mining or is simply not on Workday:
#
#   422  the tenant itself is unknown. Confirmed by probing every shard for a
#        422 tenant — all eleven answer 422, so it is never a wrong-shard clue.
#   404  errorCode S21, "not found: Job_Posting_Site_ID=X" — the tenant and
#        shard ARE right and only the site segment is wrong. Worth re-mining.
#   401  the tenant exists but blocks anonymous CxS access ("Unable to verify
#        credentials for system account"). Apple, Lenovo and Siemens Energy do
#        this; there is no public way in and no amount of slug-fixing helps.
_PROBE_REASONS = {
    401: "tenant blocks anonymous CxS access (HTTP 401) — no public endpoint",
    404: "tenant/shard are right but the site segment is wrong (HTTP 404 S21)",
    422: "unknown tenant (HTTP 422 from every shard) — likely not on Workday",
}


def probe(
    client: httpx.Client, slug: str, retries: int = 1
) -> tuple[bool, int, str]:
    """One page-0 request. Returns (ok, total_postings, reason).

    Used by tools/probe_workday.py to decide the `valid` flag in companies.yaml.
    Kept here so the probe and the fetcher can never disagree about the shape of
    a request.
    """
    try:
        tenant, shard, site = parse_slug(slug)
    except ValueError as exc:
        return (False, 0, str(exc))

    url = CXS_JOBS.format(tenant=tenant, shard=shard, site=site)
    payload = {"appliedFacets": {}, "limit": PAGE_LIMIT, "offset": 0, "searchText": ""}
    try:
        resp = client.post(url, json=payload)
    except Exception as exc:  # noqa: BLE001 — a probe reports, it never raises
        return (False, 0, f"transport error: {str(exc)[:100]}")

    if resp.status_code != 200:
        reason = _PROBE_REASONS.get(
            resp.status_code, f"HTTP {resp.status_code}"
        )
        return (False, 0, reason)
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return (False, 0, "endpoint answered but the body was not JSON")

    postings = (data or {}).get("jobPostings") or []
    if not postings:
        return (False, 0, "endpoint is live but the board has zero postings")
    return (True, int((data or {}).get("total") or len(postings)), "")
