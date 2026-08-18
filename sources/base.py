"""Shared HTTP plumbing for the board fetchers.

All of these are free, unauthenticated, public JSON endpoints. No API keys, no
rate-limit budget, no ToS issue — the only cost is wall-clock time, so we fetch
concurrently and fail soft: one dead board must never abort a run.
"""

from __future__ import annotations

import logging
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
