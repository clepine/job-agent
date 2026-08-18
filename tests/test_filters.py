"""Filter tests.

The MUST_REJECT / MUST_KEEP fixtures are real postings observed in the source
repos. They are the acceptance criteria for the filter layer — if one of these
regresses, the daily email starts recommending a lawyer or a factory shift.
"""

from __future__ import annotations

import pytest

from pipeline.filters import (
    check_description,
    check_location,
    check_title_discipline,
    check_title_seniority,
    evaluate,
)

# (title, location) that must NOT reach the model.
MUST_REJECT = [
    ("Embedded Legal Engineer", "New York, NY"),            # Palantir — a lawyer
    ("Account Partner", "Boston, MA"),                       # sales
    ("Sand and Prep - 3rd shift", "Durham, NC"),             # factory floor
    ("Field Service Engineer IV", "Charlotte, NC"),          # field service + level IV
    ("Associate Project Manager, Fire Sprinklers", "Chicago, IL"),
]

# (title, location) that must survive.
MUST_KEEP = [
    ("ASIC Verification Engineer", "Durham, NC"),            # HPE Durham
    ("FPGA Engineer", "Chicago, IL"),                        # Belvedere Chicago
    ("Mixed Signal Electronic Design Engineer", "Cambridge, MA"),  # Draper
]


@pytest.mark.parametrize("title,location", MUST_REJECT)
def test_must_reject(title, location):
    result = evaluate(title, location)
    assert not result.passed, f"{title!r} should have been rejected"
    assert result.stage in {"seniority", "discipline", "level", "location"}
    assert result.reason


@pytest.mark.parametrize("title,location", MUST_KEEP)
def test_must_keep(title, location):
    result = evaluate(title, location)
    assert result.passed, f"{title!r} rejected at {result.stage}: {result.reason}"


def test_reject_reasons_are_specific():
    assert "legal" in evaluate(*MUST_REJECT[0]).reason.lower()
    assert "sand and prep" in evaluate(*MUST_REJECT[2]).reason.lower()
    # "Field Service Engineer IV" trips seniority first (level IV) — but it must
    # also be independently rejected on discipline, so a retitled "Field Service
    # Engineer" with no level marker still can't get through.
    assert "iv" in evaluate(*MUST_REJECT[3]).reason.lower()
    assert "field service" in check_title_discipline(MUST_REJECT[3][0]).reason.lower()
    # Likewise "Associate Project Manager, Fire Sprinklers" trips on "Manager"
    # first; the discipline stage independently rejects it as a PM / trades role.
    assert "manager" in evaluate(*MUST_REJECT[4]).reason.lower()
    assert not check_title_discipline(MUST_REJECT[4][0]).passed


def test_field_service_engineer_without_level_still_rejected():
    assert not evaluate("Field Service Engineer", "Charlotte, NC").passed


# --- seniority -------------------------------------------------------------

@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "Sr. Firmware Engineer",
        "Staff ASIC Engineer",
        "Principal Electrical Engineer",
        "Engineering Manager",
        "Lead Embedded Engineer",
        "Director of Hardware",
        "Software Engineer III",
        "Hardware Engineer IV",
        "Software Engineer, L5",
    ],
)
def test_seniority_rejected(title):
    assert not check_title_seniority(title).passed, title


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer",
        "Embedded Software Engineer",
        "Software Engineer I",
        "Software Engineer, New Grad",
        "Associate Engineer",
        "University Graduate, Software Engineer",
        "Hardware Engineer - Class of 2027",
        "Engineering Rotational Program",
    ],
)
def test_entry_level_survives_seniority(title):
    assert check_title_seniority(title).passed, title


def test_lead_free_is_not_a_lead_role():
    assert check_title_seniority("Lead-Free Solder Process Engineer").passed


# --- discipline ------------------------------------------------------------

@pytest.mark.parametrize(
    "title",
    [
        "Account Executive",
        "Technical Recruiter",
        "Civil Engineer",
        "Chemical Engineer",
        "Fire Protection Engineer",
        "Maintenance Engineer",
        "Facilities Engineer",
        "Technical Support Engineer",
        "Product Manager",
        "Scrum Master",
        "Software Engineering Intern",
        "Software Engineering Co-Op - Winter 2027",
        "Process Technician, Engineering - Night Shift",
        "Machine Operator",
        "Research Scientist, PhD",
    ],
)
def test_non_engineering_rejected(title):
    assert not check_title_discipline(title).passed, title


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer",
        "Backend Developer",
        "Embedded Firmware Engineer",
        "ASIC Verification Engineer",
        "FPGA Engineer",
        "RTL Design Engineer",
        "Digital Design Engineer",
        "Mixed Signal Electronic Design Engineer",
        "Analog Design Engineer",
        "Hardware Design Engineer",
        "Electrical Engineer",
        "PCB Design Engineer",
        "Validation Engineer",
        "Site Reliability Engineer",
        "Mechatronics Engineer",
        "Field Application Engineer",
        "Field Applications Engineer",
    ],
)
def test_engineering_kept(title):
    assert check_title_discipline(title).passed, title


def test_fae_is_kept_but_sales_flavours_are_not():
    """Field Applications Engineer is a real new-grad hardware path at TI/ADI/Qorvo.
    Sales engineering is not, and must stay rejected even when similarly worded."""
    assert check_title_discipline("Field Applications Engineer").passed
    assert not check_title_discipline("Sales Engineer").passed
    assert not check_title_discipline("Field Application Engineer, Sales").passed
    assert not check_title_discipline("Application Engineer - Pre-Sales").passed


def test_bare_engineer_needs_an_entry_marker():
    assert not check_title_discipline("Engineer").passed
    assert check_title_discipline("Engineer I").passed


# --- location --------------------------------------------------------------

@pytest.mark.parametrize(
    "location,expected",
    [
        ("Durham, NC", "primary"),
        ("Research Triangle Park", "primary"),
        ("Raleigh, North Carolina", "primary"),
        ("Charlotte, NC", "primary"),
        ("Cambridge, MA", "primary"),
        ("Boston, Massachusetts", "primary"),
        ("New York, NY", "primary"),
        ("Chicago, IL", "primary"),
        ("United States - Illinois - Abbott Park", "primary"),
        ("Austin, TX", "secondary"),
        ("San Jose, CA", "secondary"),
        ("Mountain View, CALIFORNIA", "secondary"),
        ("Seattle, WA", "secondary"),
        ("Huntsville, AL", "secondary"),
        ("Remote - US", "remote_us"),
    ],
)
def test_location_classified(location, expected):
    r = check_location(location)
    assert r.passed
    assert r.metro_class == expected, f"{location} -> {r.metro_class}"


@pytest.mark.parametrize(
    "location",
    [
        "London, United Kingdom",
        "Bengaluru, India",
        "Toronto, Canada",
        "Des Moines, IA",
        "Gresham, OR",
        "Minneapolis, MN",
        "Gaffney, SC US",
        "",
    ],
)
def test_location_rejected(location):
    assert not check_location(location).passed, location


def test_cambridge_uk_is_not_cambridge_ma():
    assert not check_location("Cambridge, United Kingdom").passed


# --- clearance -------------------------------------------------------------

ACTIVE_CLEARANCE_JDS = [
    "Applicants must have an active Secret clearance to be considered.",
    "Requires an active TS/SCI with polygraph.",
    "Candidate must possess a current DoD security clearance.",
    "An active security clearance is required for this position.",
]

OBTAINABLE_CLEARANCE_JDS = [
    "Must be a US citizen and able to obtain a security clearance.",
    "Applicants must be eligible to obtain a DoD Secret clearance.",
    "US citizenship required; clearance will be sponsored.",
    "Candidates must be willing to obtain and maintain a security clearance.",
]


@pytest.mark.parametrize("jd", ACTIVE_CLEARANCE_JDS)
def test_active_clearance_rejected(jd):
    r = check_description(jd)
    assert not r.passed, jd
    assert "clearance" in r.reason.lower()


@pytest.mark.parametrize("jd", OBTAINABLE_CLEARANCE_JDS)
def test_obtainable_clearance_kept_and_flagged(jd):
    r = check_description(jd)
    assert r.passed, jd
    assert r.clearance_advantage, f"should be flagged as an advantage: {jd}"


def test_mixed_clearance_language_keeps_the_job():
    """A req that mentions both should not be dropped — 'able to obtain' wins."""
    jd = (
        "An active clearance is preferred. Candidates who are able to obtain a "
        "security clearance will also be considered."
    )
    assert check_description(jd).passed


# --- years / degree --------------------------------------------------------

@pytest.mark.parametrize(
    "jd",
    [
        "5+ years of experience in embedded systems.",
        "Minimum 7 years experience with RTL design.",
        "3 to 5 years of professional experience required.",
    ],
)
def test_years_requirement_rejected(jd):
    assert not check_description(jd).passed, jd


@pytest.mark.parametrize(
    "jd",
    [
        "0-2 years of experience.",
        "1+ years experience, including internships and coursework.",
        "2 years of experience or equivalent academic projects.",
        "New graduates welcome.",
    ],
)
def test_entry_experience_kept(jd):
    assert check_description(jd).passed, jd


def test_phd_required_rejected():
    assert not check_description("A PhD in Electrical Engineering is required.").passed


def test_ms_or_bs_kept():
    assert check_description(
        "Requires a Master's degree or equivalent Bachelor's plus experience."
    ).passed


def test_empty_description_is_not_a_rejection():
    assert check_description("").passed
