"""Configuration loading. Single source of truth is config.yaml."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Load .env if present. Never logs or echoes values."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Real environment always wins over .env.
        os.environ.setdefault(key, value)


@lru_cache(maxsize=1)
def load_config(path: str | None = None) -> dict[str, Any]:
    _load_dotenv()
    cfg_path = Path(path) if path else REPO_ROOT / "config.yaml"
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg["_repo_root"] = str(REPO_ROOT)
    return cfg


def repo_path(*parts: str) -> Path:
    return REPO_ROOT.joinpath(*parts)


def require_env(name: str, purpose: str) -> str:
    """Fetch a required secret. Raises with a clear message; never prints the value."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Environment variable {name} is not set (needed for {purpose}). "
            f"Set it in your shell or add it to a .env file at the repo root. "
            f"See README.md > 'Required environment variables'."
        )
    return value
