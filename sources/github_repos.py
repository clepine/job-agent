"""zapplyjobs new-grad aggregator repos (PLAN.md §2 stage 1, "supplementary").

Both repos publish their listings as markdown pipe tables in README.md:

    | **Company** | Role | Location | Posted | Visa | [<img ...>](APPLY_URL) |

Two properties of this source drive the design:

  1. Titles and locations are TRUNCATED with an ellipsis when long
     ("Associate Software Engineer, Core Inf..."). A truncated title cannot be
     filtered reliably and must never be shown to the owner, so those records
     are flagged `needs_hydration` and the real title/description is fetched
     from the apply URL.
  2. The tables contain literal duplicate rows. Canonical-URL hashing catches
     most; the fuzzy (company, normalized_title, location) key catches the rest.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx

from pipeline.models import Job

SOFTWARE_README = (
    "https://raw.githubusercontent.com/zapplyjobs/"
    "New-Grad-Software-Engineering-Jobs-2027/main/README.md"
)
HARDWARE_README = (
    "https://raw.githubusercontent.com/zapplyjobs/"
    "New-Grad-Hardware-Engineering-Jobs-2027/main/README.md"
)

REPOS = (
    (SOFTWARE_README, "software", "github:zapplyjobs-sw"),
    (HARDWARE_README, "hardware", "github:zapplyjobs-hw"),
)

# | **Company** | Role | Location | Posted | Visa | [![Apply](img)](url) |
ROW = re.compile(
    r"^\|\s*\*\*(?P<company>.+?)\*\*\s*"
    r"\|\s*(?P<title>.+?)\s*"
    r"\|\s*(?P<location>.+?)\s*"
    r"\|\s*(?P<posted>.+?)\s*"
    r"\|\s*(?P<visa>.*?)\s*"
    r"\|\s*\[.*?\]\((?P<url>\S+?)\)\s*\|\s*$"
)

TRUNCATED = re.compile(r"\.\.\.\s*$|…\s*$")

# After the ellipsis is stripped, a cut mid-phrase leaves dangling punctuation
# ("Automation and Design Test Engineer,"). Strip it so the title never *looks*
# broken even when we cannot recover the tail.
_DANGLING = re.compile(r"[\s,;:|/&\-\u2013\u2014]+$")


def _recover_tail(title: str, url: str) -> str:
    """Recover the words an aggregator table cut off, using the apply URL slug.

    Repo tables truncate at a fixed width, but most boards put the full title in
    the URL slug. Hydration normally repairs this, but it only works for the
    four ATSes we can fetch — jobs.apple.com and friends land as `ats: other`
    and would otherwise ship a visibly chopped title.

    Best-effort and append-only: it never replaces the parsed title, and bails
    out unless the slug demonstrably starts with the title we already have.
    """
    from urllib.parse import urlparse

    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    if not slug or "-" not in slug:
        return ""
    words = [w for w in re.split(r"[-_]+", slug.lower()) if w and not w.isdigit()]
    head = [w for w in re.split(r"[^a-z0-9]+", title.lower()) if w]
    if not head or len(words) <= len(head) or words[: len(head)] != head:
        return ""
    return " ".join(w.capitalize() for w in words[len(head):])

_AGE = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.I)
_AGE_UNITS = {
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
}


def _parse_posted(value: str) -> datetime | None:
    """'20m' / '3h' / '5d' / '2w' -> an absolute timestamp.

    The repos publish a relative age, so this is only as accurate as the moment
    we fetched. It is still a true age at read time, which is what the email
    promises.
    """
    m = _AGE.match(value or "")
    if not m:
        return None
    unit = _AGE_UNITS.get(m.group(2).lower())
    if not unit:
        return None
    return datetime.now(timezone.utc) - (int(m.group(1)) * unit)


def _ats_for(url: str) -> str:
    u = url.lower()
    if "greenhouse.io" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "ashbyhq.com" in u:
        return "ashby"
    if "smartrecruiters.com" in u:
        return "smartrecruiters"
    if "myworkdayjobs.com" in u:
        return "workday"
    return "other"


def parse_readme(markdown: str, track: str, source: str) -> list[Job]:
    jobs: list[Job] = []
    for line in markdown.splitlines():
        if not line.startswith("|"):
            continue
        m = ROW.match(line)
        if not m:
            continue
        title = m.group("title")
        location = m.group("location")
        url = m.group("url")
        truncated = bool(TRUNCATED.search(title) or TRUNCATED.search(location))
        clean_title = _DANGLING.sub("", TRUNCATED.sub("", title).strip())
        if truncated:
            tail = _recover_tail(clean_title, url)
            if tail:
                # Rejoin with the separator the truncation ate, when we saw one.
                sep = ", " if TRUNCATED.sub("", title).rstrip().endswith(",") else " "
                clean_title = f"{clean_title}{sep}{tail}"
                truncated = False
        jobs.append(
            Job(
                company=m.group("company").strip(),
                title=clean_title,
                location=TRUNCATED.sub("", location).strip(),
                url=url,
                ats=_ats_for(url),
                description="",
                posted_at=_parse_posted(m.group("posted")),
                source=source,
                track=track,
                # Every row needs a body; truncated rows also need a real title.
                needs_hydration=True,
                title_truncated=truncated,
            )
        )
    return jobs


def fetch(client: httpx.Client, track_filter: str | None = None) -> list[Job]:
    jobs: list[Job] = []
    for url, track, source in REPOS:
        if track_filter and track != track_filter:
            continue
        resp = client.get(url)
        resp.raise_for_status()
        jobs.extend(parse_readme(resp.text, track, source))
    return jobs
