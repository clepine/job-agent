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

from . import filters
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


def eligible(
    candidates: Sequence[Job], cfg: dict, track: str
) -> tuple[list[Job], list[str], list[str]]:
    """Drop candidates that are no longer worth one of five slots.

    The hard filters run at INGEST, against the postings arriving that morning.
    Nothing re-examines a posting once it is in the database, and a posting can
    sit in the scored backlog for weeks because better ones kept outranking it.
    Three things go wrong there, all of them visible in the 2026-08-18 email:

      * Age. limits.max_posting_age_days is 7, but the backlog is exempt from
        it, so the hardware list carried a Field AI role posted 74 days earlier
        and the pool still held one from 124 days earlier. A four-month-old req
        is not a lead; the ingest cutoff has to have a backlog counterpart.

      * Fit. The email sent five per track whether or not five were worth
        sending. It shipped a Waymo ML ASIC role at 30/100 whose own rationale
        said the owner lacks the tapeout experience. score.py calibrates 0-39
        as "poor match", so email.min_fit defaults to 40: never spend one of
        his five slots on a posting the model itself called a poor match.
        Four good matches beat five with a bad one on the end.

      * Truncated titles. Postings scored before filters.check_url_title
        existed are still in the database with disqualifiers hidden by the
        truncation. Re-checking here catches them without a migration.

    Returns (eligible, backlog_notes, tier1_notes). Every drop is counted and
    reported — a short list must always say why, never just look like a quiet
    market — but the two note lists have different audiences, see below.
    """
    limits = cfg.get("limits", {})
    email_cfg = cfg.get("email", {})
    max_age = limits.get("max_backlog_age_days")
    min_fit = email_cfg.get("min_fit")

    keep: list[Job] = []
    too_old: list[Job] = []
    too_weak: list[Job] = []
    disqualified: list[Job] = []

    for job in candidates:
        if not filters.check_url_title(job.url).passed:
            disqualified.append(job)
            continue
        age = job.age_days
        if max_age and age is not None and age > int(max_age):
            too_old.append(job)
            continue
        if min_fit is not None and (job.fit_score or 0) < int(min_fit):
            too_weak.append(job)
            continue
        keep.append(job)

    # A held-back TIER 1 is reported even on a full day. It answers a question
    # the owner will otherwise ask himself: "why is there no Google today?"
    # The 2026-08-18 decision behind email.tier1_min_fit is that prestige never
    # displaces a better-matched Tier 2 role — but that decision has to be
    # VISIBLE, or a silent absence reads as a broken source.
    tier1_notes: list[str] = []
    benched_t1 = [j for j in too_weak if j.tier == 1]
    if benched_t1:
        best = max((j.fit_score or 0) for j in benched_t1)
        tier1_notes.append(
            f"{len(benched_t1)} Tier 1 {track} role(s) scored below the fit "
            f"floor of {min_fit} (best was {best}) and were not sent. The "
            f"absence of a big name today is a fit result, not a missing source."
        )

    notes: list[str] = []
    if too_old:
        oldest = max((j.age_days or 0) for j in too_old)
        notes.append(
            f"{len(too_old)} scored {track} posting(s) aged out of the backlog "
            f"(older than {max_age}d; oldest was {oldest}d). They are stale reqs, "
            f"not new competition."
        )
    if too_weak:
        best = max((j.fit_score or 0) for j in too_weak)
        notes.append(
            f"{len(too_weak)} scored {track} posting(s) were below the fit floor "
            f"of {min_fit} (best was {best}) and were not sent. A short list of "
            f"good matches beats a full one padded with poor ones."
        )
    if disqualified:
        notes.append(
            f"{len(disqualified)} {track} posting(s) were dropped on a "
            f"disqualifier recovered from the posting URL — their stored title "
            f"was truncated by the source and hid it."
        )
    return keep, notes, tier1_notes


def pick_track(candidates: Sequence[Job], cfg: dict, track: str) -> Selection:
    email_cfg = cfg["email"]
    want = int(email_cfg.get("per_track", 5))
    t1_quota = int(email_cfg.get("tier1_quota", 2))
    t2_quota = int(email_cfg.get("tier2_quota", 3))

    max_per_company = int(cfg.get("limits", {}).get("max_per_company", 2))

    candidates, gate_notes, tier1_notes = eligible(candidates, cfg, track)
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

    # A Tier-1 slot must be earned. Prestige never displaces a materially
    # better-matched Tier-2 role; below the floor the slot is backfilled.
    t1_floor = int(email_cfg.get("tier1_min_fit", 50))
    tier1 = [j for j in ranked if j.tier == 1 and (j.fit_score or 0) >= t1_floor]
    benched_t1 = [j for j in ranked if j.tier == 1 and (j.fit_score or 0) < t1_floor]
    tier2 = [j for j in ranked if j.tier != 1]

    sel = Selection()
    sel.notes.extend(tier1_notes)
    chosen: list[Job] = []
    chosen.extend(take(tier1, t1_quota, chosen))
    chosen.extend(take(tier2, t2_quota, chosen))

    # Backfill whichever side came up short.
    if len(chosen) < want:
        shortfall = want - len(chosen)
        # Backfill from Tier 2 first; a sub-floor Tier 1 is a last resort and
        # can only take a slot nothing better wanted.
        backfill = take(tier2 + benched_t1, shortfall, chosen)
        if backfill:
            sel.backfilled = len(backfill)
            got_t1 = len([j for j in chosen if j.tier == 1])
            if benched_t1 and got_t1 < t1_quota:
                worst = max((j.fit_score or 0) for j in benched_t1)
                sel.notes.append(
                    f"{len(benched_t1)} Tier 1 {track} role(s) were held back for "
                    f"scoring below the fit floor of {t1_floor} (best was {worst}) — "
                    f"better-matched Tier 2 roles took those slots instead."
                )
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

    # Why the gate notes come LAST and only when the list is short:
    # the gates drop the same aged-out and sub-floor postings every single day,
    # because nothing ever clears them out of the backlog. Reporting that on a
    # full 5-role day would put a permanent, growing line of noise in the email
    # and train the owner to stop reading the notes — which is exactly where
    # the genuinely important ones live. When five good roles were found, the
    # ones that lost is not news. When the list is short, it is the answer.
    if len(chosen) < want:
        sel.notes.extend(gate_notes)

    sel.jobs = sorted(chosen, key=lambda j: final_score(j, cfg), reverse=True)[:want]
    return sel


def pick(
    software: Sequence[Job], hardware: Sequence[Job], cfg: dict
) -> tuple[Selection, Selection]:
    return pick_track(software, cfg, "software"), pick_track(hardware, cfg, "hardware")
