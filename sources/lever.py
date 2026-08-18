"""Lever public postings API.

    GET https://api.lever.co/v0/postings/{slug}?mode=json

Returns a flat list with plain-text descriptions already included.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from pipeline.models import Job
from .base import get_json

API = "https://api.lever.co/v0/postings/{slug}?mode=json"


def _parse_ms(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _description(raw: dict) -> str:
    parts = [raw.get("descriptionPlain") or raw.get("description") or ""]
    for block in raw.get("lists") or []:
        text = block.get("text") or ""
        content = block.get("content") or ""
        parts.append(f"{text}\n{content}")
    parts.append(raw.get("additionalPlain") or "")
    return "\n".join(p for p in parts if p)


def fetch(client: httpx.Client, company: str, slug: str, track: str) -> list[Job]:
    data = get_json(client, API.format(slug=slug))
    jobs: list[Job] = []
    for raw in data if isinstance(data, list) else []:
        url = raw.get("hostedUrl") or raw.get("applyUrl") or ""
        if not url:
            continue
        cats = raw.get("categories") or {}
        locations = cats.get("allLocations") or []
        location = "; ".join(locations) if locations else (cats.get("location") or "")
        jobs.append(
            Job(
                company=company,
                title=raw.get("text") or "",
                location=location,
                url=url,
                ats="lever",
                description=_description(raw),
                posted_at=_parse_ms(raw.get("createdAt")),
                source=f"lever:{slug}",
                track=track if track != "both" else "software",
            )
        )
    return jobs
