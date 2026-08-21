"""Description hydration for records that arrive without a body.

Only called AFTER the title/location hard filters, so we pay one HTTP request
per *survivor* rather than per posting — a ~20x reduction in requests.

Records whose title was truncated by the aggregator ("Associate Software
Engineer, Core Inf...") get their real title back here, then are re-filtered:
a truncated title can hide a disqualifier ("...Core Infrastructure, Senior").
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Iterable
from urllib.parse import urlsplit

import httpx

from pipeline.models import Job
from . import smartrecruiters, workday
from .base import BoardError, get_json

GH_JOB = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
LEVER_JOB = "https://api.lever.co/v0/postings/{slug}/{job_id}"
ASHBY_BOARD = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
WD_JOB = "https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"

_GH = re.compile(r"greenhouse\.io/(?P<slug>[^/]+)/jobs/(?P<id>\d+)")
_LEVER = re.compile(r"lever\.co/(?P<slug>[^/]+)/(?P<id>[0-9a-f-]{8,})")
_ASHBY = re.compile(r"ashbyhq\.com/(?P<slug>[^/]+)/(?P<id>[0-9a-f-]{8,})")
# Both our own fetched URLs and the aggregators' apply links. Some tenants
# insert a locale segment ("/en-US/PfizerCareers/job/..."), which is not part of
# the CxS path and must be dropped.
_WORKDAY = re.compile(
    r"https?://(?P<tenant>[^./]+)\.(?P<shard>wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[^/]+)(?P<path>/job/.+)$"
)


def _hydrate_greenhouse(client: httpx.Client, job: Job) -> bool:
    m = _GH.search(job.url)
    if not m:
        return False
    data = get_json(client, GH_JOB.format(slug=m["slug"], job_id=m["id"]), retries=1)
    job.description = data.get("content") or ""
    if data.get("title"):
        job.title = data["title"]
        job.title_truncated = False
    loc = (data.get("location") or {}).get("name")
    if loc:
        job.location = loc
    return bool(job.description)


def _hydrate_lever(client: httpx.Client, job: Job) -> bool:
    m = _LEVER.search(job.url)
    if not m:
        return False
    data = get_json(client, LEVER_JOB.format(slug=m["slug"], job_id=m["id"]), retries=1)
    parts = [data.get("descriptionPlain") or ""]
    for block in data.get("lists") or []:
        parts.append(f"{block.get('text','')}\n{block.get('content','')}")
    job.description = "\n".join(p for p in parts if p)
    if data.get("text"):
        job.title = data["text"]
        job.title_truncated = False
    cats = data.get("categories") or {}
    if cats.get("location"):
        job.location = cats["location"]
    return bool(job.description)


def _hydrate_ashby(client: httpx.Client, job: Job) -> bool:
    m = _ASHBY.search(job.url)
    if not m:
        return False
    data = get_json(client, ASHBY_BOARD.format(slug=m["slug"]), retries=1)
    for raw in data.get("jobs", []) or []:
        if raw.get("id") == m["id"]:
            job.description = raw.get("descriptionPlain") or ""
            if raw.get("title"):
                job.title = raw["title"]
                job.title_truncated = False
            if raw.get("location"):
                job.location = raw["location"]
            return bool(job.description)
    return False


def _hydrate_workday(client: httpx.Client, job: Job) -> bool:
    """One GET against the CxS detail endpoint.

    This also covers the ~470 Workday rows the aggregator READMEs carry, which
    were previously unhydratable — so a truncated Workday title now gets its
    real text back and is re-filtered like every other source.

    Two fields here beat what the list response can offer: `startDate` is an
    absolute ISO date rather than "Posted 3 Days Ago", and `additionalLocations`
    reveals the other sites behind a "6 Locations" placeholder.
    """
    m = _WORKDAY.match(job.url.split("?", 1)[0])
    if not m:
        return False
    data = get_json(
        client,
        WD_JOB.format(
            tenant=m["tenant"], shard=m["shard"], site=m["site"], path=m["path"]
        ),
        retries=1,
    )
    info = (data or {}).get("jobPostingInfo") or {}
    job.description = info.get("jobDescription") or ""
    if info.get("title"):
        job.title = info["title"]
        job.title_truncated = False

    places = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
    normalized = [workday.normalize_location(p) for p in places if p]
    if normalized:
        # Semicolons because pipeline/geo.py splits multi-site strings on them
        # and takes the best metro class across the sites.
        job.location = "; ".join(dict.fromkeys(normalized))

    start = info.get("startDate")
    if start:
        try:
            job.posted_at = datetime.fromisoformat(str(start)).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    return bool(job.description)


_HANDLERS = {
    "greenhouse": _hydrate_greenhouse,
    "lever": _hydrate_lever,
    "ashby": _hydrate_ashby,
    "smartrecruiters": lambda c, j: smartrecruiters.hydrate(c, j),
    "workday": _hydrate_workday,
}


def hydrate_one(client: httpx.Client, job: Job) -> bool:
    handler = _HANDLERS.get(job.ats)
    if handler is None:
        # Workday and unknown ATSs have no free JSON body endpoint.
        return False
    try:
        ok = handler(client, job)
    except (BoardError, Exception):  # noqa: BLE001 — hydration is best-effort
        return False
    if ok:
        job.needs_hydration = False
    return ok


def hydrate_all(
    client: httpx.Client, jobs: Iterable[Job], max_workers: int = 8
) -> tuple[int, int]:
    """Hydrate in parallel. Returns (attempted, succeeded)."""
    targets = [j for j in jobs if j.needs_hydration]
    if not targets:
        return (0, 0)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(lambda j: hydrate_one(client, j), targets))
    return (len(targets), sum(1 for r in results if r))


def hydratable(job: Job) -> bool:
    return job.ats in _HANDLERS


def mark_for_rehydration(jobs: Iterable[Job]) -> list[Job]:
    """Jobs whose body must be re-fetched, flagged so hydrate_all acts on them.

    `needs_hydration` is set by the FETCHERS at ingest and is a non-persisted
    working field, so anything read back out of the database arrives False.
    `hydrate_all` targets `[j for j in jobs if j.needs_hydration]`, which meant a
    caller handing it a list of database-loaded jobs got a silent no-op: it
    reported "re-hydrated 0/5" and carried on.

    That is exactly what run.py's pre-email top-up did. Descriptions are not
    persisted in the committed ledger, so every posting picked from the scored
    backlog reached the email with an empty body - which is why the 2026-08-21
    digest printed "ATS keywords: none detected" under two of five roles and a
    rationale complaining that an "empty JD limits detail". The bodies were
    never fetched, and nothing said so.

    Selecting and flagging live together here so a caller cannot do one without
    the other.
    """
    stale = [j for j in jobs if not j.description and hydratable(j)]
    for job in stale:
        job.needs_hydration = True
    return stale
