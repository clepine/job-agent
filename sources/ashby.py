"""Ashby public job-board API.

    GET https://api.ashbyhq.com/posting-api/job-board/{slug}

Descriptions arrive as `descriptionPlain` in the list response.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from pipeline.models import Job
from .base import get_json

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fetch(client: httpx.Client, company: str, slug: str, track: str) -> list[Job]:
    data = get_json(client, API.format(slug=slug))
    jobs: list[Job] = []
    for raw in data.get("jobs", []) or []:
        if raw.get("isListed") is False:
            continue
        url = raw.get("jobUrl") or raw.get("applyUrl") or ""
        if not url:
            continue
        locs = [raw.get("location") or ""]
        locs += [s.get("location", "") for s in (raw.get("secondaryLocations") or [])]
        if raw.get("isRemote"):
            locs.append("Remote")
        jobs.append(
            Job(
                company=company,
                title=raw.get("title") or "",
                location="; ".join(x for x in locs if x),
                url=url,
                ats="ashby",
                description=raw.get("descriptionPlain")
                or raw.get("descriptionHtml")
                or "",
                posted_at=_parse_dt(raw.get("publishedAt")),
                source=f"ashby:{slug}",
                track=track if track != "both" else "software",
            )
        )
    return jobs
