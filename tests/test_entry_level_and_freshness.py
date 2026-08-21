"""Regressions for the 2026-08-21 email defects.

That morning's email shipped, under an "entry-level" mandate, an NVIDIA role
demanding 5+ years at fit 68, led with postings 21/22/24 days old while day-old
ones ranked beneath them, filed an "Embedded Linux Software Engineer" under
Software, and reported 0 hardware matches on the same page. Each test below
pins one of those causes.
"""

from __future__ import annotations

import pytest

from pipeline import filters, resume_pick
from pipeline.pick import freshness_penalty
from pipeline.track import infer_track


# --------------------------------------------------------------------------
# 1. The years gate: a softener only counts inside its own clause.
# --------------------------------------------------------------------------

# The exact shape live NVIDIA JR2022612 uses, and the shape most Workday
# requirement blocks use: degree bullet, then years bullet.
NVIDIA_SHAPED = (
    "- BS degree in Electrical Engineering, Computer Engineering, Computer "
    "Science, or a related field, or equivalent experience.\n"
    "- 5+ years of experience in embedded software, firmware, Linux device "
    "drivers, systems software, hardware validation, diagnostics."
)


def test_degree_softener_on_the_previous_bullet_does_not_excuse_the_years_bullet():
    """The +/-120 char window straddled the bullet boundary, so "or equivalent"
    from the DEGREE line neutralized the years line below it."""
    result = filters.check_description(NVIDIA_SHAPED)
    assert not result.passed
    assert "5+ years" in result.reason


def test_softener_still_applies_inside_its_own_clause():
    """Narrowing the window must not turn every posting into a rejection."""
    assert filters.check_description(
        "- 3+ years of experience or equivalent combination of education and experience."
    ).passed
    assert filters.check_description(
        "- 4 years of experience including internships and academic projects."
    ).passed


def test_under_three_years_is_still_entry_level():
    assert filters.check_description("- 2+ years of experience with Python.").passed


@pytest.mark.parametrize("body", [
    "Requirements:\n- 7 years of experience in ASIC design.",
    "The ideal candidate has 5-8 years of experience building distributed systems.",
])
def test_plain_years_requirements_are_rejected(body):
    assert not filters.check_description(body).passed


def test_clause_around_stops_at_the_bullet_boundary():
    idx = NVIDIA_SHAPED.index("5+")
    clause = filters._clause_around(NVIDIA_SHAPED, idx, idx + 2)
    assert clause.startswith("- 5+ years")
    assert "equivalent" not in clause


# --------------------------------------------------------------------------
# 2. Track routing: embedded is a HARDWARE angle (PLAN.md section 4).
# --------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Embedded Linux Software Engineer",      # the one the 2026-08-21 email misfiled
    "Embedded Software Engineer",
    "Firmware Engineer",
    "Embedded Systems Software Developer",
])
def test_embedded_titles_route_to_hardware_even_when_they_say_software(title):
    """`sw and not hw` used to fire first, making the `hw and sw` branch - whose
    own comment names this exact case - unreachable."""
    assert infer_track(title) == "hardware"


@pytest.mark.parametrize("title", [
    "Machine Learning Engineer",
    "Backend Software Engineer",
    "Site Reliability Engineer",
])
def test_plain_software_titles_are_unaffected(title):
    assert infer_track(title) == "software"


# --------------------------------------------------------------------------
# 3. Freshness outranks a marginal fit edge.
# --------------------------------------------------------------------------

CFG = {"freshness": {"fresh_days": 3, "per_day_penalty": 4.0, "unknown_age_penalty": 12.0}}


def test_everything_inside_the_fresh_window_is_ranked_on_fit_alone():
    assert freshness_penalty(0, CFG) == 0.0
    assert freshness_penalty(3, CFG) == 0.0


def test_a_three_week_old_posting_cannot_win_on_a_three_point_fit_edge():
    """The 2026-08-21 ordering: fit 68 at 21 days ranked above fit 65 at 1 day."""
    old = 68 + freshness_penalty(21, CFG)
    fresh = 65 + freshness_penalty(1, CFG)
    assert fresh > old


def test_the_penalty_is_uncapped():
    assert freshness_penalty(30, CFG) < freshness_penalty(10, CFG) < 0


def test_undated_postings_are_penalized_not_exempted():
    assert freshness_penalty(None, CFG) == -12.0
    assert freshness_penalty(None, CFG) < freshness_penalty(2, CFG)


# --------------------------------------------------------------------------
# 4. The email states which resume to send.
# --------------------------------------------------------------------------

RESUME_SW = {"skills": [{"label": "Software", "items": ["Python", "LangChain", "Docker", "React"]}]}
RESUME_HW = {"skills": [{"label": "Hardware", "items": ["Verilog", "MSP430", "KiCad", "Oscilloscope"]}]}


def test_a_posting_with_no_extractable_keywords_defers_to_the_track():
    rec = resume_pick.recommend("Requirements: teamwork and communication", RESUME_SW, RESUME_HW)
    assert rec.terms == 0
    assert "track default" in rec.label()


def test_recommendation_names_the_resume_that_covers_the_posting():
    rec = resume_pick.recommend("Requirements: Verilog, MSP430, KiCad", RESUME_SW, RESUME_HW)
    assert rec.best == "hardware"
    assert rec.coverage_hw > rec.coverage_sw
    assert "HARDWARE" in rec.label()


def test_a_tie_is_reported_as_a_tie_rather_than_a_coin_flip():
    """A true bridge role - one term each resume covers - must not pick a winner."""
    rec = resume_pick.recommend("Requirements: Python, Verilog", RESUME_SW, RESUME_HW)
    assert rec.terms == 2
    assert rec.close
    assert "either works" in rec.label()


def test_a_clear_disagreement_with_the_track_is_flagged():
    rec = resume_pick.recommend("Requirements: Verilog, MSP430, KiCad", RESUME_SW, RESUME_HW)
    assert resume_pick.disagrees_with_track(rec, "software")
    assert not resume_pick.disagrees_with_track(rec, "hardware")


def test_a_close_call_is_never_flagged_as_a_disagreement():
    rec = resume_pick.recommend("Requirements: Python, Verilog", RESUME_SW, RESUME_HW)
    assert not resume_pick.disagrees_with_track(rec, "software")
    assert not resume_pick.disagrees_with_track(rec, "hardware")


# --------------------------------------------------------------------------
# 5. The description gate must see through HTML.
# --------------------------------------------------------------------------

# The shape Draper's board actually returns: no newlines, no sentence break
# between the degree line and the years line - the boundary is a tag.
HTML_BODY = (
    "<p>Master&#39;s degree preferred.</p><p><br /><b>Experience</b></p>"
    "<p>3-5 years experience in electrical engineering or related field.</p>"
)


def test_gate_sees_clause_boundaries_through_html_markup():
    """check_description used to run on raw markup, where the only clause
    boundary is a tag - so a softener paragraphs away still cancelled the
    requirement below it. Measured 2026-08-21: 0 of 67 such postings rejected."""
    assert not filters.check_description(HTML_BODY).passed


def test_double_escaped_markup_is_still_normalized():
    """Some sources escape twice; unescaping once leaves literal tags behind."""
    body = "&lt;li&gt;Bachelor's degree in EE.&lt;/li&gt;&lt;li&gt;7+ years of experience.&lt;/li&gt;"
    assert not filters.check_description(body).passed


def test_normalize_body_strips_markup():
    text = filters._normalize_body(HTML_BODY)
    assert "<" not in text
    assert "3-5 years experience" in text


# --------------------------------------------------------------------------
# 6. "degree" is not a softener; alternation is.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    # The degree is an ADDITIONAL requirement joined by "and", not a way around
    # the years. Bare `degree` on the softener list cancelled 22 of 67 postings.
    "- Bachelor's degree in Electrical Engineering and 4+ years of experience.",
    "- Degree in EE or relevant field with a minimum of 5 years of related experience.",
    # "including" used descriptively narrows the ask, it does not waive it.
    "- 7+ years of post-silicon validation, preferably including ASIC bring-up.",
])
def test_a_requirement_next_to_the_word_degree_is_still_a_requirement(body):
    assert not filters.check_description(body).passed


@pytest.mark.parametrize("body", [
    "- Or 3+ years of professional experience in software development in lieu of a degree.",
    "- 5 years of experience or equivalent combination of education and experience.",
    "- 3 years of experience, including internships and academic projects.",
])
def test_genuine_alternation_still_softens(body):
    assert filters.check_description(body).passed


# --------------------------------------------------------------------------
# 7. The resume line must not overstate what was measured.
# --------------------------------------------------------------------------
# Each case below was a real phrasing bug seen in a rendered email on
# 2026-08-21: an exact 75%/75% tie reported as software leading "by a hair",
# and "no keywords extracted" printed for a posting whose keywords had been
# extracted perfectly well and simply matched neither resume.


def test_an_exact_tie_is_not_reported_as_a_lead():
    rec = resume_pick.recommend("Requirements: Python, Verilog", RESUME_SW, RESUME_HW)
    assert rec.coverage_sw == rec.coverage_hw
    assert "by a hair" not in rec.label()
    assert "both cover" in rec.label()


def test_a_near_tie_still_names_the_nose_ahead():
    rec = resume_pick.recommend(
        "Requirements: Python, Docker, Verilog, MSP430, KiCad", RESUME_SW, RESUME_HW
    )
    if rec.close and rec.coverage_sw != rec.coverage_hw:
        assert "by a hair" in rec.label()


def test_keywords_that_match_neither_resume_are_reported_as_such():
    """Distinct from extracting none at all — the count is real and is shown."""
    rec = resume_pick.recommend(
        "Requirements: Kubernetes, Terraform", RESUME_SW, RESUME_HW
    )
    assert rec.terms == 2
    assert "neither covers any of the 2 keywords" in rec.label()
    assert "no keywords" not in rec.label()


def test_a_posting_naming_nothing_trackable_says_that_instead():
    rec = resume_pick.recommend(
        "Requirements: teamwork and communication", RESUME_SW, RESUME_HW
    )
    assert rec.terms == 0
    assert "names no keywords we track" in rec.label()


def test_neither_no_signal_case_is_flagged_as_a_track_disagreement():
    """No measurement is not evidence for the other resume."""
    for jd in ("Requirements: Kubernetes, Terraform", "Requirements: teamwork"):
        rec = resume_pick.recommend(jd, RESUME_SW, RESUME_HW)
        assert not resume_pick.disagrees_with_track(rec, "software")
        assert not resume_pick.disagrees_with_track(rec, "hardware")


def test_the_recommendation_carries_the_diffs_it_was_computed_from():
    """So the email can print keyword chips without a third diff."""
    rec = resume_pick.recommend("Requirements: Verilog, Python", RESUME_SW, RESUME_HW)
    assert rec.diff_for("software").matched == ["Python"]
    assert rec.diff_for("hardware").matched == ["Verilog"]
