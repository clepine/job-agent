"""SmartRecruiters public postings API.

    GET https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset=N

The list response carries title + location but NOT the description; the body
needs a second call per posting. We deliberately do not hydrate here — the
hard-filter layer rejects ~95% of these on title and location alone, so
hydration is done later, only for survivors (see hydrate_descriptions()).
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from pipeline.models import Job
from .base import BoardError, get_json

LIST_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}"
DETAIL_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}"

MAX_PAGES = 4  # 400 postings per board is already far past what we can use


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _location(raw: dict) -> str:
    loc = raw.get("location") or {}
    full = loc.get("fullLocation")
    if full:
        return full
    bits = [loc.get("city"), loc.get("region"), loc.get("country")]
    text = ", ".join(b for b in bits if b)
    if loc.get("remote"):
        text = f"{text}; Remote".strip("; ")
    return text


def fetch(client: httpx.Client, company: str, slug: str, track: str) -> list[Job]:
    jobs: list[Job] = []
    offset = 0
    for _ in range(MAX_PAGES):
        data = get_json(client, LIST_API.format(slug=slug, offset=offset))
        content = data.get("content") or []
        if not content:
            break
        for raw in content:
            job_id = raw.get("id")
            if not job_id:
                continue
            jobs.append(
                Job(
                    company=company,
                    title=raw.get("name") or "",
                    location=_location(raw),
                    url=f"https://jobs.smartrecruiters.com/{slug}/{job_id}",
                    ats="smartrecruiters",
                    description="",
                    posted_at=_parse_dt(raw.get("releasedDate")),
                    source=f"smartrecruiters:{slug}",
                    track=track if track != "both" else "software",
                    needs_hydration=True,
                )
            )
        total = int(data.get("totalFound") or 0)
        offset += len(content)
        if offset >= total:
            break
    return jobs


def hydrate(client: httpx.Client, job: Job) -> bool:
    """Fetch the posting body for one job. Returns True if it filled in."""
    slug = job.source.split(":", 1)[-1]
    job_id = job.url.rstrip("/").rsplit("/", 1)[-1]
    try:
        data = get_json(client, DETAIL_API.format(slug=slug, job_id=job_id), retries=1)
    except BoardError:
        return False
    ad = (data.get("jobAd") or {}).get("sections") or {}
    parts = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        section = ad.get(key) or {}
        text = section.get("text")
        if text:
            parts.append(f"{section.get('title') or key}\n{text}")
    job.description = "\n\n".join(parts)
    job.needs_hydration = False
    return bool(job.description)
