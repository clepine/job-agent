"""Daily selection — pure local SQL + policy. Zero tokens.

Scores already live in the database (see pipeline/score.py), so choosing the
day's 5 software + 5 hardware is arithmetic, not inference:

  final = fit_score + geography bonus + tier bonus + clearance bonus + freshness

Tier quota per PLAN.md §1: 2 from Tier 1 + 3 from Tier 2 per track. If Tier 1
is empty that day we backfill from Tier 2 and SAY SO explicitly rather than
padding the list with a bad match.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .geo import location_bonus
from .models import Job


@dataclass
class Selection:
    jobs: list[Job] = field(default_factory=list)
    backfilled: int = 0
    short_by: int = 0
    notes: list[str] = field(default_factory=list)


def final_score(job: Job, cfg: dict) -> float:
    score = float(job.fit_score or 0)
    score += location_bonus(job.metro_class, cfg)
    if job.tier == 1:
        score += 3.0
    if job.clearance_advantage:
        score += 5.0
    # Mild freshness preference — a 30-day-old posting is a colder lead than a
    # 1-day-old one, but age never outranks fit.
    age = job.age_days
    if age is not None:
        score += max(-6.0, -age / 5.0)
    return round(score, 3)


def pick_track(candidates: Sequence[Job], cfg: dict, track: str) -> Selection:
    email_cfg = cfg["email"]
    want = int(email_cfg.get("per_track", 5))
    t1_quota = int(email_cfg.get("tier1_quota", 2))
    t2_quota = int(email_cfg.get("tier2_quota", 3))

    max_per_company = int(cfg.get("limits", {}).get("max_per_company", 2))

    ranked = sorted(candidates, key=lambda j: final_score(j, cfg), reverse=True)

    def take(pool: Sequence[Job], n: int, chosen: list[Job]) -> list[Job]:
        """Take the best `n`, respecting the per-company diversity cap."""
        counts: dict[str, int] = {}
        for job in chosen:
            key = job.company.lower()
            counts[key] = counts.get(key, 0) + 1
        picked: list[Job] = []
        for job in pool:
            if len(picked) >= n:
                break
            if job in chosen or job in picked:
                continue
            key = job.company.lower()
            if counts.get(key, 0) >= max_per_company:
                continue
            counts[key] = counts.get(key, 0) + 1
            picked.append(job)
        return picked

    tier1 = [j for j in ranked if j.tier == 1]
    tier2 = [j for j in ranked if j.tier != 1]

    sel = Selection()
    chosen: list[Job] = []
    chosen.extend(take(tier1, t1_quota, chosen))
    chosen.extend(take(tier2, t2_quota, chosen))

    # Backfill whichever side came up short.
    if len(chosen) < want:
        shortfall = want - len(chosen)
        backfill = take(ranked, shortfall, chosen)
        if backfill:
            sel.backfilled = len(backfill)
            got_t1 = len([j for j in chosen if j.tier == 1])
            if got_t1 < t1_quota:
                sel.notes.append(
                    f"Only {got_t1} Tier 1 {track} match{'' if got_t1 == 1 else 'es'} "
                    f"available today — backfilled {len(backfill)} from Tier 2 rather "
                    f"than padding the list."
                )
            chosen.extend(backfill)

    if len(chosen) < want:
        sel.short_by = want - len(chosen)
        sel.notes.append(
            f"Only {len(chosen)} {track} match{'' if len(chosen) == 1 else 'es'} "
            f"available to send today (wanted {want}). run.py diagnoses why — a "
            f"short list can mean unscored backlog, an exhausted pool, or a "
            f"genuine drought, and these are not the same problem."
        )

    sel.jobs = sorted(chosen, key=lambda j: final_score(j, cfg), reverse=True)[:want]
    return sel


def pick(
    software: Sequence[Job], hardware: Sequence[Job], cfg: dict
) -> tuple[Selection, Selection]:
    return pick_track(software, cfg, "software"), pick_track(hardware, cfg, "hardware")
