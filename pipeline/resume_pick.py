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

from dataclasses import dataclass, field

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
    # The diffs this verdict was computed from, kept so callers do not run the
    # same set arithmetic a second time. The email needs the track's diff for
    # its keyword chips, and recomputing it there meant three diffs per posting
    # per render - four once the plain-text part re-rendered the same card.
    diff_sw: keywords.KeywordDiff = field(default_factory=keywords.KeywordDiff)
    diff_hw: keywords.KeywordDiff = field(default_factory=keywords.KeywordDiff)

    def diff_for(self, track: str) -> keywords.KeywordDiff:
        """The diff against the resume that `track` actually sends."""
        return self.diff_hw if track == "hardware" else self.diff_sw

    @property
    def margin(self) -> float:
        return abs(self.coverage_sw - self.coverage_hw)

    def label(self) -> str:
        """One line for the email card.

        Every branch here exists because the previous phrasing overstated what
        was measured. "no keywords extracted" was printed when keywords HAD been
        extracted and simply matched nothing, and an exact tie was reported as
        one resume leading "by a hair". A line the reader cannot trust on the
        easy cases is not worth reading on the hard ones.
        """
        if not self.terms:
            return (
                "Resume: this posting names no keywords we track - "
                "fall back to the track default"
            )
        sw = f"{self.coverage_sw:.0%}"
        hw = f"{self.coverage_hw:.0%}"
        if not self.matched_sw and not self.matched_hw:
            return (
                f"Resume: neither covers any of the {self.terms} keywords this "
                f"posting names - fall back to the track default"
            )
        if self.close:
            if self.coverage_sw == self.coverage_hw:
                return f"Resume: either works - both cover {sw} of its keywords"
            lead = "software" if self.coverage_sw > self.coverage_hw else "hardware"
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
        return Recommendation(
            "software", cov_sw, cov_hw, True, 0, 0, terms, diff_sw=d_sw, diff_hw=d_hw
        )
    best = "software" if cov_sw >= cov_hw else "hardware"
    return Recommendation(
        best=best,
        coverage_sw=cov_sw,
        coverage_hw=cov_hw,
        close=abs(cov_sw - cov_hw) < CLOSE_MARGIN,
        matched_sw=len(d_sw.matched),
        matched_hw=len(d_hw.matched),
        terms=terms,
        diff_sw=d_sw,
        diff_hw=d_hw,
    )


def disagrees_with_track(rec: Recommendation, track: str) -> bool:
    """True when the measurement points at the other resume, clearly.

    Only a NON-close disagreement counts. The email flags these so a bridge role
    filed under one track can still be applied to with the other resume.
    """
    return not rec.close and rec.best != track
