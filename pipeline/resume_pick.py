"""Which master resume to send with a given posting.

The track (pipeline/track.py) decides which resume a posting is SCORED against
and which section of the email it lands in. That is a routing decision made from
the title, and on a bridge role - "Systems Engineer", "Test Engineer",
"Embedded Linux Software Engineer" - it is a close call the owner should get to
see rather than have silently made for him.

This module makes the call explicit and shows its work: it measures both master
resumes against the posting's own vocabulary and reports which one covers more
of it, by how much, and whether the gap is wide enough to matter.

No LLM. It is set arithmetic over keywords.diff, the same vocabulary the ATS
keyword block in the email already uses, so the recommendation cannot disagree
with the keyword chips printed directly beneath it.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import keywords

# Coverage gap below which the two resumes are called equivalent. Under ~8
# points the difference is typically one or two incidental terms, and naming a
# winner there would read as more confident than the measurement supports.
CLOSE_MARGIN = 0.08


@dataclass
class Recommendation:
    best: str                  # "software" | "hardware"
    coverage_sw: float
    coverage_hw: float
    close: bool                # the two resumes are effectively tied
    matched_sw: int
    matched_hw: int
    terms: int

    @property
    def margin(self) -> float:
        return abs(self.coverage_sw - self.coverage_hw)

    def label(self) -> str:
        """One line for the email card."""
        if not self.terms:
            return "Resume: no keywords extracted - fall back to the track default"
        sw = f"{self.coverage_sw:.0%}"
        hw = f"{self.coverage_hw:.0%}"
        if self.close:
            lead = "software" if self.coverage_sw >= self.coverage_hw else "hardware"
            return (
                f"Resume: either works - software {sw} vs hardware {hw} "
                f"keyword coverage ({lead} by a hair)"
            )
        return (
            f"Resume: use the {self.best.upper()} one - "
            f"software {sw} vs hardware {hw} keyword coverage"
        )


def recommend(compressed_jd: str, resume_sw: dict, resume_hw: dict) -> Recommendation:
    """Compare both masters against one posting."""
    d_sw = keywords.diff(compressed_jd, resume_sw)
    d_hw = keywords.diff(compressed_jd, resume_hw)
    terms = len(d_sw.jd_terms)
    cov_sw, cov_hw = d_sw.coverage, d_hw.coverage
    # No terms at all, or neither resume covers a single one: there is no
    # measurement here, and "either works (software by a hair)" would dress up
    # a 0-vs-0 as a finding. Report no signal and let the track stand.
    if not terms or (not d_sw.matched and not d_hw.matched):
        return Recommendation("software", cov_sw, cov_hw, True, 0, 0, 0)
    best = "software" if cov_sw >= cov_hw else "hardware"
    return Recommendation(
        best=best,
        coverage_sw=cov_sw,
        coverage_hw=cov_hw,
        close=abs(cov_sw - cov_hw) < CLOSE_MARGIN,
        matched_sw=len(d_sw.matched),
        matched_hw=len(d_hw.matched),
        terms=terms,
    )


def disagrees_with_track(rec: Recommendation, track: str) -> bool:
    """True when the measurement points at the other resume, clearly.

    Only a NON-close disagreement counts. The email flags these so a bridge role
    filed under one track can still be applied to with the other resume.
    """
    return not rec.close and rec.best != track
