"""Workday CxS fetcher — DELIBERATE STUB (PLAN.md §2 stage 1, "second pass").

Workday is by far the largest single ATS in the source repos (~470 of 1056
rows), so this is the highest-value remaining source. It is stubbed rather than
half-built because it differs from the other three in ways that need their own
pass:

  * POST, not GET:
        POST https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
        body: {"appliedFacets":{},"limit":20,"offset":0,"searchText":""}
  * Every tenant has its own `{wdN}` shard AND its own `{site}` path segment,
    both of which must be mined per company (companies.yaml stores them as
    "tenant|wdN|site").
  * Descriptions need a second GET per posting against the job's `externalPath`.
  * Several tenants gate the endpoint behind a session cookie obtained from the
    HTML board first.

Enabling it is a matter of implementing fetch() below; companies.yaml already
carries validated tenant/shard/site triples for every Workday company mined
from the source repos, so no re-mining is needed.
"""

from __future__ import annotations

import httpx

from pipeline.models import Job

ENABLED = False

CXS_ENDPOINT = "https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"


def parse_slug(slug: str) -> tuple[str, str, str]:
    """companies.yaml stores Workday boards as 'tenant|shard|site'."""
    parts = (slug or "").split("|")
    if len(parts) != 3:
        raise ValueError(f"malformed workday slug {slug!r}; expected 'tenant|wdN|site'")
    return parts[0], parts[1], parts[2]


def fetch(client: httpx.Client, company: str, slug: str, track: str) -> list[Job]:
    """Not implemented — see module docstring. Returns [] so a run never breaks."""
    return []
