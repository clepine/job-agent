"""Fetch + normalize + dedupe (PLAN.md §2 stages 1-3).

All of this is free HTTP against public JSON endpoints. No API key is touched
anywhere in this module — nothing here can cost the owner money.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import yaml

from sources import ashby, github_repos, greenhouse, lever, smartrecruiters, workday
from sources.base import fetch_many, make_client

from .config import repo_path
from .models import Job

log = logging.getLogger("fetch")

FETCHERS = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
    "smartrecruiters": smartrecruiters.fetch,
    "workday": workday.fetch,
}


@dataclass
class FetchReport:
    boards_attempted: int = 0
    boards_failed: int = 0
    from_boards: int = 0
    from_repos: int = 0
    raw_total: int = 0
    after_url_dedupe: int = 0
    after_fuzzy_dedupe: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def url_duplicates(self) -> int:
        return self.raw_total - self.after_url_dedupe

    @property
    def fuzzy_duplicates(self) -> int:
        return self.after_url_dedupe - self.after_fuzzy_dedupe


def load_companies(path: Optional[str] = None) -> list[dict]:
    p = repo_path(path) if path else repo_path("companies.yaml")
    with open(p, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    boards = list(data.get("boards") or [])
    # Workday entries live in their own key and are skipped while the fetcher
    # is a stub, but are loaded so `--include-workday` can exercise them once
    # sources/workday.py is implemented.
    for entry in data.get("workday_boards") or []:
        entry.setdefault("valid", False)
        boards.append(entry)
    return boards


def fetch_boards(cfg: dict, companies: list[dict]) -> tuple[list[Job], FetchReport]:
    report = FetchReport()
    client = make_client(cfg)
    tasks = []
    tier_by_slug: dict[tuple[str, str], int] = {}

    for entry in companies:
        if not entry.get("valid"):
            continue
        ats = entry.get("ats")
        fn = FETCHERS.get(ats)
        if fn is None or (ats == "workday" and not workday.ENABLED):
            continue
        company = entry["company"]
        slug = entry["slug"]
        track = entry.get("track", "software")
        tier_by_slug[(ats, slug)] = int(entry.get("tier", 2))
        label = f"{ats}:{slug}"
        tasks.append(
            (label, lambda f=fn, c=company, s=slug, t=track: f(client, c, s, t))
        )

    report.boards_attempted = len(tasks)
    jobs, failures = fetch_many(tasks, max_workers=cfg["fetch"]["max_concurrency"])
    report.failures = failures
    report.boards_failed = len(failures)

    for job in jobs:
        ats_slug = (job.ats, job.source.split(":", 1)[-1])
        job.tier = tier_by_slug.get(ats_slug, 2)

    report.from_boards = len(jobs)
    client.close()
    return jobs, report


def fetch_repos(cfg: dict, companies: list[dict]) -> list[Job]:
    """The two zapplyjobs README tables."""
    client = make_client(cfg)
    try:
        jobs = github_repos.fetch(client)
    finally:
        client.close()

    tier_by_company = {
        e["company"].lower(): int(e.get("tier", 2)) for e in companies
    }
    for job in jobs:
        job.tier = tier_by_company.get(job.company.lower(), 2)
    return jobs


def dedupe(jobs: list[Job], known_ids: set[str], known_keys: set[str]) -> tuple[list[Job], int, int]:
    """URL-hash dedupe first (exact), then fuzzy (company, title, location).

    Returns (unique_new_jobs, url_dupes_dropped, fuzzy_dupes_dropped).
    """
    seen_ids: set[str] = set(known_ids)
    seen_keys: set[str] = set(known_keys)
    unique: list[Job] = []
    url_dupes = 0
    fuzzy_dupes = 0

    # Prefer richer records: a job that already has a description wins over the
    # same job scraped from a README with no body.
    ordered = sorted(jobs, key=lambda j: (len(j.description) > 0, len(j.title)), reverse=True)

    for job in ordered:
        if job.id in seen_ids:
            url_dupes += 1
            continue
        key = "|".join(job.dedupe_key)
        # A blank title/company key is meaningless — don't collapse on it.
        if key.strip("|") and key in seen_keys:
            fuzzy_dupes += 1
            continue
        seen_ids.add(job.id)
        if key.strip("|"):
            seen_keys.add(key)
        unique.append(job)

    return unique, url_dupes, fuzzy_dupes


def fetch_all(
    cfg: dict,
    known_ids: Optional[set[str]] = None,
    known_keys: Optional[set[str]] = None,
    skip_repos: bool = False,
    skip_boards: bool = False,
) -> tuple[list[Job], FetchReport]:
    companies = load_companies(cfg["paths"]["companies"])

    jobs: list[Job] = []
    report = FetchReport()

    if not skip_boards:
        board_jobs, report = fetch_boards(cfg, companies)
        jobs.extend(board_jobs)

    if not skip_repos:
        repo_jobs = fetch_repos(cfg, companies)
        report.from_repos = len(repo_jobs)
        jobs.extend(repo_jobs)

    report.raw_total = len(jobs)
    unique, url_dupes, fuzzy_dupes = dedupe(
        jobs, known_ids or set(), known_keys or set()
    )
    report.after_url_dedupe = report.raw_total - url_dupes
    report.after_fuzzy_dedupe = len(unique)
    return unique, report
