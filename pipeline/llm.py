"""Anthropic client wrapper: budget ceiling, usage ledger, prompt caching.

The owner has a small, fixed API balance. Three properties matter more here
than anywhere else in the codebase:

  1. A HARD per-run ceiling. Estimated spend is checked BEFORE each request; if
     the request would cross the ceiling the run raises BudgetExceeded rather
     than sending it. A filter-layer bug cannot drain the balance overnight.
  2. Real usage is logged to out/usage.jsonl after every call, including
     cache_read / cache_creation, so estimated vs actual can be compared.
  3. The stable prefix (system prompt + resume YAML) carries
     cache_control: {"type": "ephemeral"} and volatile per-job content goes
     AFTER it. Caching is a prefix match — a byte change ahead of the
     breakpoint invalidates everything after it.

Nothing in this module runs at import time, so the whole pipeline can be
imported, unit-tested, and dry-run with no API key present.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import repo_path, require_env

log = logging.getLogger("llm")

# Local pre-flight estimate. Deliberately pessimistic (real English is closer to
# ~4 chars/token) so the ceiling trips early rather than late.
CHARS_PER_TOKEN = 3.4


class BudgetExceeded(RuntimeError):
    """Raised instead of sending a request that would cross the run ceiling."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_creation_input_tokens + other.cache_creation_input_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
        )

    def cost(self, prices: dict) -> float:
        return (
            self.input_tokens / 1e6 * prices["price_input_per_mtok"]
            + self.output_tokens / 1e6 * prices["price_output_per_mtok"]
            + self.cache_creation_input_tokens / 1e6 * prices["price_cache_write_per_mtok"]
            + self.cache_read_input_tokens / 1e6 * prices["price_cache_read_per_mtok"]
        )


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text or "") / CHARS_PER_TOKEN))


@dataclass
class Ledger:
    """Running spend for one pipeline run."""

    prices: dict
    ceiling_usd: float
    total: Usage = field(default_factory=Usage)
    calls: int = 0
    aborted: bool = False

    @property
    def spent_usd(self) -> float:
        return round(self.total.cost(self.prices), 6)

    @property
    def remaining_usd(self) -> float:
        return round(self.ceiling_usd - self.spent_usd, 6)

    def check(self, est_input_tokens: int, max_output_tokens: int, label: str) -> None:
        """Gate one prospective call. Raises before any network I/O."""
        projected = Usage(
            input_tokens=est_input_tokens, output_tokens=max_output_tokens
        )
        projected_cost = projected.cost(self.prices)
        if self.spent_usd + projected_cost > self.ceiling_usd:
            self.aborted = True
            raise BudgetExceeded(
                f"aborting before '{label}': spent ${self.spent_usd:.4f}, this call "
                f"could add up to ${projected_cost:.4f}, ceiling is "
                f"${self.ceiling_usd:.4f}. Raise budget.max_usd_per_run in "
                f"config.yaml if this is expected."
            )

    def record(self, usage: Usage) -> None:
        self.total = self.total + usage
        self.calls += 1


def _usage_from_response(response: Any) -> Usage:
    u = getattr(response, "usage", None)
    if u is None:
        return Usage()
    return Usage(
        input_tokens=int(getattr(u, "input_tokens", 0) or 0),
        output_tokens=int(getattr(u, "output_tokens", 0) or 0),
        cache_creation_input_tokens=int(
            getattr(u, "cache_creation_input_tokens", 0) or 0
        ),
        cache_read_input_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
    )


def _append_usage_log(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


class LlmClient:
    """Thin wrapper over anthropic.Anthropic with the budget guard attached.

    `client` may be injected for tests — every unit test in this repo passes a
    stub, so the suite never makes a live call.
    """

    def __init__(self, cfg: dict, client: Any | None = None, run_id: str = ""):
        self.cfg = cfg
        self.model_cfg = cfg["model"]
        self.budget_cfg = cfg["budget"]
        self.ledger = Ledger(
            prices=self.budget_cfg,
            ceiling_usd=float(self.budget_cfg["max_usd_per_run"]),
        )
        self.run_id = run_id or datetime.now(timezone.utc).isoformat()
        self.usage_log = repo_path(self.budget_cfg.get("usage_log", "out/usage.jsonl"))
        self._client = client
        self._injected = client is not None

    # -- client construction -------------------------------------------------

    @property
    def client(self) -> Any:
        if self._client is None:
            api_key = require_env("ANTHROPIC_API_KEY", "Claude API calls")
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "The 'anthropic' package is not installed. "
                    "Run: pip install -r requirements.txt"
                ) from exc
            # The key is passed straight through and never logged or echoed.
            self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    # -- request -------------------------------------------------------------

    def complete_json(
        self,
        *,
        system_cached: str,
        user_content: str,
        schema: dict,
        max_tokens: int,
        label: str,
    ) -> tuple[dict, Usage]:
        """One structured-output request.

        `system_cached` is the STABLE prefix (instructions + resume). It carries
        the cache breakpoint. `user_content` is the volatile per-job payload and
        must come after it.
        """
        est_in = estimate_tokens(system_cached) + estimate_tokens(user_content)
        self.ledger.check(est_in, max_tokens, label)

        system = [
            {
                "type": "text",
                "text": system_cached,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        output_config: dict[str, Any] = {
            "effort": self.model_cfg.get("effort", "low"),
            "format": {"type": "json_schema", "schema": schema},
        }

        kwargs: dict[str, Any] = {
            "model": self.model_cfg["id"],
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
            "output_config": output_config,
        }
        # Thinking is ON BY DEFAULT on Sonnet 5; turn it off explicitly unless
        # config asks for adaptive. Note: temperature/top_p/top_k are rejected
        # on this model, so they are never sent.
        thinking = self.model_cfg.get("thinking", "disabled")
        if thinking == "disabled":
            kwargs["thinking"] = {"type": "disabled"}
        elif thinking == "adaptive":
            kwargs["thinking"] = {"type": "adaptive"}

        response = self.client.messages.create(**kwargs)

        usage = _usage_from_response(response)
        self.ledger.record(usage)
        self._log(label, usage, response)

        text = ""
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text += getattr(block, "text", "")
        if not text.strip():
            raise RuntimeError(f"{label}: model returned no text content")

        try:
            return json.loads(text), usage
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{label}: structured output was not valid JSON: {text[:200]!r}"
            ) from exc

    def _log(self, label: str, usage: Usage, response: Any) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "label": label,
            "model": self.model_cfg["id"],
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
            "call_cost_usd": round(usage.cost(self.budget_cfg), 6),
            "run_cost_usd": self.ledger.spent_usd,
            "stop_reason": getattr(response, "stop_reason", None),
        }
        try:
            _append_usage_log(self.usage_log, entry)
        except OSError as exc:  # pragma: no cover - logging must never break a run
            log.warning("could not write usage log: %s", exc)


def api_key_present() -> bool:
    """True if a key is available. Never returns or logs the value itself."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    env_file = repo_path(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                return bool(line.split("=", 1)[1].strip())
    return False
