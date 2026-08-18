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
