"""Cheap local pre-rank — BM25, zero tokens, zero vendors.

PLAN.md §2 stage 5 said "embed surviving JDs, cosine against the resume". That
is pinned to BM25 instead: Anthropic has no embeddings endpoint, so embeddings
would mean a second vendor and a second bill for a stage that only has to pick
the top ~40 out of ~100 keyword-dense engineering postings. At this volume,
lexical scoring against a resume that is itself a bag of concrete technical
nouns (Verilog, MSP430, LangChain, PowerShell) is a good fit.

The output feeds two things: which jobs get sent for LLM scoring first, and a
tiebreaker among equally-scored jobs.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable, Sequence

from .models import Job

_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#./_-]*")

_STOP = {
    "the", "and", "for", "with", "you", "our", "are", "will", "that", "this",
    "have", "from", "your", "job", "work", "team", "role", "position", "we",
    "a", "an", "of", "to", "in", "on", "as", "at", "or", "be", "is", "it",
    "by", "us", "all", "not", "can", "may", "who", "new", "including",
    "experience", "years", "ability", "strong", "excellent", "please",
}

K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    return [
        t for t in _TOKEN.findall((text or "").lower())
        if len(t) > 1 and t not in _STOP
    ]


class Bm25:
    """Standard Okapi BM25 over the surviving job descriptions."""

    def __init__(self, docs: Sequence[Sequence[str]]):
        self.docs = [Counter(d) for d in docs]
        self.lengths = [sum(c.values()) for c in self.docs]
        self.avgdl = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        self.n = len(self.docs)
        df: Counter[str] = Counter()
        for c in self.docs:
            df.update(c.keys())
        self.idf = {
            term: math.log(1 + (self.n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def score(self, index: int, query_terms: Iterable[str]) -> float:
        if self.avgdl == 0:
            return 0.0
        doc = self.docs[index]
        dl = self.lengths[index]
        total = 0.0
        for term in query_terms:
            tf = doc.get(term, 0)
            if not tf:
                continue
            idf = self.idf.get(term, 0.0)
            total += idf * (tf * (K1 + 1)) / (tf + K1 * (1 - B + B * dl / self.avgdl))
        return total


def resume_query(resume: dict) -> list[str]:
    """Flatten a master resume YAML into a weighted bag of query terms.

    Skills are repeated so they outweigh prose — the resume's concrete nouns are
    what should drive the match, not its verbs.
    """
    parts: list[str] = []
    for group in (resume.get("skills") or {}).values():
        items = group if isinstance(group, list) else [group]
        parts.extend(str(i) for i in items)
        parts.extend(str(i) for i in items)  # weight x2
        parts.extend(str(i) for i in items)  # weight x3
    for section in ("experience", "projects"):
        for entry in resume.get(section) or []:
            parts.append(str(entry.get("title", "")))
            parts.append(str(entry.get("stack", "")))
            for bullet in entry.get("bullets") or []:
                parts.append(str(bullet))
    edu = resume.get("education") or {}
    for key in ("coursework", "in_progress"):
        parts.extend(str(c) for c in (edu.get(key) or []))
    return tokenize(" ".join(parts))


def prerank(jobs: list[Job], resume: dict, keep: int) -> list[Job]:
    """Score every job against the resume; return the best `keep`, in order."""
    if not jobs:
        return []
    docs = [tokenize(f"{j.title} {j.title} {j.company} {j.description}") for j in jobs]
    bm25 = Bm25(docs)
    query = resume_query(resume)
    for i, job in enumerate(jobs):
        base = bm25.score(i, query)
        # Geography and tier are policy, not lexical similarity — applied here so
        # the cap we hand to the model is chosen on the same terms the email is.
        if job.metro_class == "primary":
            base += 4.0
        elif job.metro_class == "secondary":
            base -= 1.5
        if job.tier == 1:
            base += 1.5
        if job.clearance_advantage:
            base += 2.0
        job.prerank = round(base, 3)
    return sorted(jobs, key=lambda j: j.prerank, reverse=True)[:keep]
