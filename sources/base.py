"""Shared HTTP plumbing for the board fetchers.

All of these are free, unauthenticated, public JSON endpoints. No API keys, no
rate-limit budget, no ToS issue — the only cost is wall-clock time, so we fetch
concurrently and fail soft: one dead board must never abort a run.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable, Optional

import httpx

log = logging.getLogger("sources")


class BoardError(Exception):
    """A single board failed. Always caught at the fetch-all level."""


def make_client(cfg: dict) -> httpx.Client:
    f = cfg.get("fetch", {})
    return httpx.Client(
        timeout=httpx.Timeout(float(f.get("timeout_seconds", 25))),
        follow_redirects=True,
        headers={
            "User-Agent": f.get("user_agent", "job-agent/1.0"),
            "Accept": "application/json",
        },
    )


def get_json(client: httpx.Client, url: str, retries: int = 2) -> Any:
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            resp = client.get(url)
            if resp.status_code == 404:
                raise BoardError(f"404 {url}")
            resp.raise_for_status()
            return resp.json()
        except BoardError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail soft by design
            last = exc
            if attempt == retries:
                break
    raise BoardError(f"{url}: {last}")


# 429 is the one 4xx that means "ask again", not "stop asking".
_TRANSIENT_STATUS = {429}


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait: the server's Retry-After if it gave one, else backoff."""
    raw = resp.headers.get("Retry-After")
    if raw:
        try:
            return min(10.0, max(0.0, float(raw)))
        except ValueError:
            pass
    return min(4.0, 0.5 * (2**attempt))


def post_json(
    client: httpx.Client, url: str, payload: dict, retries: int = 2
) -> Any:
    """POST a JSON body and return the decoded response.

    Workday's CxS endpoint is the only source that needs POST. Most of its
    failure modes are deterministic rather than transient — a wrong tenant/site
    returns 422, an over-large `limit` returns 400 — so those are raised
    immediately and never retried. That distinction is load-bearing: probe()
    reads 401 vs 404 vs 422 to tell "not on Workday" from "wrong site segment",
    and retrying would only slow down a verdict that will not change.

    **429 is the exception, and it has to be**, because it is the one 4xx that
    means "ask again later" rather than "stop asking". Treating it like the
    others was harmless while paging was strictly sequential and gentle. It is
    not harmless now that pages are requested in concurrent waves: a single
    rate-limited page aborts the WHOLE board mid-pagination, and because the
    fetcher fails soft, the run continues and simply reports fewer postings.
    Measured 2026-08-19, a saturated network turned that into 52 of 290 boards
    dropped in one run — an 18% loss that looked exactly like a quiet morning.
    """
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            resp = client.post(url, json=payload)
            if resp.status_code in _TRANSIENT_STATUS:
                last = BoardError(f"{resp.status_code} {url}")
                if attempt == retries:
                    break
                time.sleep(_retry_after(resp, attempt))
                continue
            if 400 <= resp.status_code < 500:
                raise BoardError(f"{resp.status_code} {url}")
            resp.raise_for_status()
            return resp.json()
        except BoardError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail soft by design
            last = exc
            if attempt == retries:
                break
    raise BoardError(f"{url}: {last}")


def fetch_many(
    tasks: Iterable[tuple[str, Callable[[], list]]],
    max_workers: int = 8,
) -> tuple[list, list[tuple[str, str]]]:
    """Run board fetches concurrently.

    Returns (all_jobs, failures) where failures is [(board_label, error_text)].
    """
    results: list = []
    failures: list[tuple[str, str]] = []
    tasks = list(tasks)
    if not tasks:
        return results, failures

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fn): label for label, fn in tasks}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                results.extend(fut.result())
            except Exception as exc:  # noqa: BLE001
                failures.append((label, str(exc)[:180]))
                log.debug("board failed: %s: %s", label, exc)
    return results, failures
