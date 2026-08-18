"""Greenhouse public board API.

    GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

`content=true` returns the full HTML description in the list response, so one
request per board yields everything we need — no per-job hydration.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from pipeline.models import Job
from .base import get_json

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


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
        url = raw.get("absolute_url") or ""
        if not url:
            continue
        location = (raw.get("location") or {}).get("name") or ""
        # Some boards list several offices; join so the metro matcher sees them all.
        offices = [o.get("name", "") for o in (raw.get("offices") or [])]
        if offices and not location:
            location = "; ".join(o for o in offices if o)
        jobs.append(
            Job(
                company=company,
                title=raw.get("title") or "",
                location=location,
                url=url,
                ats="greenhouse",
                description=raw.get("content") or "",
                posted_at=_parse_dt(raw.get("first_published"))
                or _parse_dt(raw.get("updated_at")),
                source=f"greenhouse:{slug}",
                track=track if track != "both" else "software",
            )
        )
    return jobs
