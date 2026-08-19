"""Normalized job schema, canonical URL hashing, and fuzzy dedupe keys."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Literal, Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator

Track = Literal["software", "hardware", "both"]
Ats = Literal[
    "greenhouse", "lever", "ashby", "workday", "smartrecruiters", "github_repo", "other"
]

# Query params that identify the posting vs. params that are pure tracking noise.
_MEANINGFUL_QUERY_KEYS = {"gh_jid", "jobId", "job_id", "id", "jid", "posting"}

_TRACKING_PREFIXES = ("utm_", "gh_src", "src", "ref", "source", "trk", "lever-")


def canonical_url(url: str) -> str:
    """Strip tracking params, trailing slashes, and case-normalize the host.

    Two URLs that reach the same posting must produce the same string, because
    the primary key of the whole system is a hash of this.
    """
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    scheme = "https"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    # Greenhouse serves the same board under two hostnames.
    if netloc == "job-boards.greenhouse.io":
        netloc = "boards.greenhouse.io"
    path = re.sub(r"/+$", "", parts.path) or "/"

    kept = []
    for pair in parts.query.split("&"):
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        if key in _MEANINGFUL_QUERY_KEYS:
            kept.append(f"{key}={value}")
        elif any(key.startswith(p) for p in _TRACKING_PREFIXES):
            continue
    query = "&".join(sorted(kept))
    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:32]


_TITLE_NOISE = re.compile(
    r"\b(?:job\s*id|req(?:uisition)?\s*(?:id|no|number)?)\b[\s:#-]*[\w-]+", re.I
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Level/qualifier tails that don't change the identity of the role.
_TITLE_TAIL = re.compile(
    r"\b(?:i{1,3}|iv|v|vi|jr|sr|1|2|3|20\d\d|20\d\d\s*(?:grad|start)?)\b", re.I
)


# Corporate suffixes and parentheticals that make the SAME employer look like
# two: "HPE" vs "HPE (University)", "Wellmark, Inc." vs "Wellmark".
_COMPANY_PAREN = re.compile(r"\([^)]*\)")
_COMPANY_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|company|co|plc|gmbh|sa|nv|ag|"
    r"holdings?|group|technologies|technology|systems|labs?|"
    r"university|campus|college|careers?|jobs?|usa|us|na|"
    r"north america|global|international|the)\b",
    re.I,
)


def normalize_company(company: str) -> str:
    """Company key for fuzzy dedupe only (never displayed)."""
    c = (company or "").lower()
    c = _COMPANY_PAREN.sub(" ", c)
    c = _NON_ALNUM.sub(" ", c)
    c = _COMPANY_SUFFIX.sub(" ", c)
    return " ".join(c.split())


def normalize_title(title: str) -> str:
    """Aggressively normalized title used only for fuzzy dedupe (never displayed)."""
    t = (title or "").lower()
    t = _TITLE_NOISE.sub(" ", t)
    t = t.replace("&", " and ")
    t = _TITLE_TAIL.sub(" ", t)
    t = _NON_ALNUM.sub(" ", t)
    # Collapse common synonyms so "Software Engineer New Grad" == "Software Engineer".
    for phrase in (
        "new grad", "new graduate", "university graduate", "university grad",
        "entry level", "early career", "campus", "college", "full time",
        "united states", "us", "remote",
    ):
        t = t.replace(phrase, " ")
    return " ".join(t.split())


_LOC_NOISE = re.compile(r"[^a-z0-9]+")


def normalize_location(location: str) -> str:
    loc = (location or "").lower()
    loc = loc.replace("united states", " ").replace("usa", " ").replace("u s a", " ")
    loc = _LOC_NOISE.sub(" ", loc)
    return " ".join(sorted(set(loc.split())))


class Job(BaseModel):
    """One posting, normalized. This is the only job shape the pipeline knows."""

    id: str = Field(default="", description="Primary key: canonical-URL hash")
    company: str
    title: str
    location: str = ""
    url: str
    ats: Ats = "other"
    description: str = ""
    posted_at: Optional[datetime] = None
    first_seen_at: Optional[datetime] = None
    source: str = ""
    track: Track = "software"
    shown_at: Optional[datetime] = None
    # Set by `python run.py --applied <job-id>`. An applied job is never shown
    # again: over months the real failure mode is not missing a role, it is
    # re-applying to one already sent.
    applied_at: Optional[datetime] = None

    # Scored once at ingest, then persisted and never recomputed.
    fit_score: Optional[int] = None
    fit_rationale: str = ""
    scored_at: Optional[datetime] = None
    # Fingerprint of the resume this score was computed against; a mismatch
    # against the current resume makes the score stale (see pipeline/fingerprint.py).
    resume_hash: str = ""

    # Non-persisted working fields.
    needs_hydration: bool = False
    title_truncated: bool = False
    tier: int = 2
    metro_class: str = "none"
    metro: Optional[str] = None
    clearance_advantage: bool = False
    prerank: float = 0.0

    @field_validator("company", "title", "location", mode="before")
    @classmethod
    def _clean_text(cls, v: object) -> str:
        if v is None:
            return ""
        return " ".join(str(v).replace(" ", " ").split())

    def model_post_init(self, __context: object) -> None:
        if not self.id:
            self.id = url_hash(self.url)
        if self.first_seen_at is None:
            self.first_seen_at = datetime.now(timezone.utc)

    @property
    def dedupe_key(self) -> tuple[str, str, str]:
        """Secondary fuzzy key. The source repos contain literal duplicate rows."""
        return (
            normalize_company(self.company),
            normalize_title(self.title),
            normalize_location(self.location),
        )

    @property
    def age_days(self) -> Optional[int]:
        if not self.posted_at:
            return None
        posted = self.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - posted).days)

    def age_label(self) -> str:
        """True posting age. Never guessed — 'unknown' if the source didn't say."""
        days = self.age_days
        if days is None:
            return "posted date unknown"
        if days == 0:
            return "posted today"
        if days == 1:
            return "posted 1d ago"
        return f"posted {days}d ago"
