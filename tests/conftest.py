"""Shared fixtures.

CRITICAL: no test in this suite may make a live Anthropic API call. Every test
that exercises an LLM stage injects StubAnthropic. `no_api_key` is applied to
the whole session so that even a mistake — a code path that constructs a real
client — fails loudly with a missing-key error instead of silently spending the
owner's balance.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config import load_config, repo_path  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _no_api_key():
    """Remove any real key from the environment for the whole test session."""
    import os

    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    yield
    if saved is not None:
        os.environ["ANTHROPIC_API_KEY"] = saved


@pytest.fixture(autouse=True)
def _isolate_usage_log(tmp_path, monkeypatch):
    """Never let a test append to the REAL out/usage.jsonl.

    Every LLM stage is exercised against StubAnthropic, whose canned usage is a
    flat 1000-in / 200-out. Those rows were being written into the same ledger
    the README tells the owner to total up to check real spend, so a few test
    runs made it read ~$2 of spend that never happened. With a ~$4.90 balance,
    a money file that lies upward is worse than no money file.

    Patched at the writer rather than in config so it holds no matter how a
    test builds its LlmClient.
    """
    from pipeline import llm as llm_mod

    real_append = llm_mod._append_usage_log
    sink = tmp_path / "usage.jsonl"
    monkeypatch.setattr(
        llm_mod, "_append_usage_log", lambda _path, entry: real_append(sink, entry)
    )


class StubUsage:
    def __init__(self, i=1000, o=200, cr=0, cw=0):
        self.input_tokens = i
        self.output_tokens = o
        self.cache_read_input_tokens = cr
        self.cache_creation_input_tokens = cw


class StubBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class StubResponse:
    def __init__(self, payload: dict, usage: StubUsage | None = None):
        self.content = [StubBlock(json.dumps(payload))]
        self.usage = usage or StubUsage()
        self.stop_reason = "end_turn"


class StubMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("StubAnthropic ran out of scripted responses")
        payload = self._responses.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return payload


class StubAnthropic:
    """Drop-in for anthropic.Anthropic, scripted with canned responses."""

    def __init__(self, responses):
        self.messages = StubMessages(responses)


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def resume_sw():
    with open(repo_path("resume/master_sw.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def resume_hw():
    with open(repo_path("resume/master_hw.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)
