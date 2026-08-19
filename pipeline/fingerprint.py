"""Stable content hash of a master resume.

Score-once persists a fit score computed against a specific resume. The owner
will edit his resume repeatedly during a job search, so a score computed
against last month's resume is stale — it may be based on skills he has since
reworded, projects he has reordered, or coursework he has completed.

`resume_hash()` fingerprints ONLY the content that actually feeds scoring:
skills, experience, projects, and coursework. Cosmetic edits — fixing a phone
number, adding a LinkedIn URL, changing `meta.source_file` — must NOT invalidate
the score pool, because re-scoring costs real money.

The hash must be stable across runs and across machines, so serialization is
fully sorted and deterministic.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Only these top-level keys affect a fit score.
_SCORED_KEYS = ("skills", "experience", "projects")

# Within `education`, only what a scorer would weigh.
_EDUCATION_KEYS = ("degree", "coursework", "in_progress", "in_progress_completes")


def _canonical(node: Any) -> Any:
    """Recursively normalize for hashing: sorted dict keys, stripped strings."""
    if isinstance(node, dict):
        return {k: _canonical(node[k]) for k in sorted(node)}
    if isinstance(node, list):
        return [_canonical(v) for v in node]
    if isinstance(node, str):
        return " ".join(node.split())
    return node


def scored_content(resume: dict) -> dict:
    """The subset of a resume that a fit score actually depends on."""
    payload: dict[str, Any] = {}
    for key in _SCORED_KEYS:
        if resume.get(key) is not None:
            payload[key] = resume[key]
    education = resume.get("education") or {}
    edu = {k: education[k] for k in _EDUCATION_KEYS if education.get(k) is not None}
    if edu:
        payload["education"] = edu
    # Clearance eligibility is scored (it is a differentiator at defense firms).
    citizenship = (resume.get("contact") or {}).get("citizenship")
    if citizenship:
        payload["citizenship"] = citizenship
    return payload


def resume_hash(resume: dict) -> str:
    """16-hex-char fingerprint of the scoring-relevant resume content."""
    canonical = _canonical(scored_content(resume))
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def regime_hash(cfg: dict) -> str:
    """Fingerprint of everything OTHER than the resume that decides a score.

    A cached score is only comparable to a fresh one if both were produced the
    same way. The resume fingerprint above covers the candidate side of that
    and nothing else, so until 2026-08-19 editing the scoring prompt, switching
    the model, or changing how much of a job description the model gets to read
    left every old score in place and silently mixed two scoring regimes in the
    same ranking. Nothing announced it: pick.py just compared the numbers.

    That is a trap laid specifically for whoever tunes calibration next — the
    most likely reason anyone touches score.py — because the symptom is a
    ranking that is subtly wrong rather than a run that fails.

    Included here, and deliberately nothing else:
      * the scoring system prompt, which IS the calibration
      * the model id, since scores are not comparable across models
      * jd_max_chars, which decides how much of the posting the model sees

    Excluded: batch size, per-run budget, retries. Those change throughput and
    cost, not the judgement.
    """
    from .score import SYSTEM_TEMPLATE  # local import: score.py imports this module

    parts = [
        SYSTEM_TEMPLATE,
        str((cfg.get("model") or {}).get("id", "")),
        str((cfg.get("limits") or {}).get("jd_max_chars", "")),
    ]
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:8]


def score_fingerprint(resume: dict, cfg: dict) -> str:
    """The full identity of a cached score: WHO was scored and HOW.

    Stored in the `resume_hash` column, which keeps its name for compatibility
    with existing ledgers; a mismatch on either half re-queues the posting.
    """
    return f"{resume_hash(resume)}:{regime_hash(cfg)}"
