"""Hard filters — PLAN.md §2 stage 4.

This is the most important code in the project. Upstream sources have very poor
precision (the aggregator repos surface lawyers, salespeople, and factory-floor
shift work under "engineering"), and this layer is also the budget guard: every
posting it rejects is a posting that never costs a token.

Design rules:
  * Reject-first, then require a positive engineering signal. A title must earn
    its way through, not merely fail to trip a blocklist.
  * Every rejection carries a machine-readable stage + human reason, so the
    run report can show exactly where the funnel narrows.
  * Clearance: reject reqs that require an ACTIVE/current clearance; KEEP
    "must be able to obtain" — the owner is a US citizen and clearance-eligible,
    which is a competitive advantage at Draper/MITRE/Raytheon/BAE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .geo import MetroClass, classify_location

# ---------------------------------------------------------------------------
# Stage 1 — seniority
# ---------------------------------------------------------------------------

_SENIORITY = re.compile(
    r"(?<![a-z])("
    r"senior|sr\.?|staff|principal|lead|leader|head of|director|vp|"
    r"vice president|chief|manager|mgr|supervisor|architect|fellow|"
    r"distinguished|expert|master|executive|president|owner|partner"
    r")(?![a-z])",
    re.I,
)
# "Lead" is legitimate inside e.g. "Lead-Free Solder"; require it to be a role word.
_SENIORITY_FALSE_POSITIVE = re.compile(r"lead[- ]free|lead time|leading edge", re.I)

# Roman/arabic level markers meaning "not entry level".
_LEVEL_MARKER = re.compile(
    r"(?<![a-z])(?:"
    r"iii|iv|v|vi|vii|viii|ix|x"          # III and above
    r"|ii"                                 # II — already one level up
    r"|[3-9]"                              # numeric 3+
    r"|l[3-9]|t[3-9]|e[3-9]"               # ladder codes L4/T5/E3
    r")(?![a-z0-9])",
    re.I,
)
# "II"/"2" can appear innocently (e.g. "Tier 2 Support" — which we reject anyway).
# We only apply _LEVEL_MARKER to the trailing segment of a title.

_ENTRY_LEVEL_OVERRIDE = re.compile(
    r"(?<![a-z])("
    r"new ?grad(?:uate)?|entry[- ]level|early career|university (?:grad|hire|program)|"
    r"campus|college (?:grad|hire)|associate engineer|graduate engineer|"
    r"engineer i(?![ivx])|engineer 1(?![0-9])|"
    r"rotational|leadership development program|ldp|"
    r"class of 20\d\d|20\d\d (?:grad|start|new grad)"
    r")(?![a-z])",
    re.I,
)

# ---------------------------------------------------------------------------
# Stage 2 — non-engineering / wrong discipline
# ---------------------------------------------------------------------------

# Hard reject regardless of any "engineer" token in the title. These are the
# false positives PLAN.md calls out by name.
_NON_ENGINEERING = re.compile(
    r"(?<![a-z])("
    # legal / finance / HR / sales / marketing
    r"legal|counsel|attorney|paralegal|compliance officer|"
    r"account (?:partner|executive|manager|director)|sales|business development|"
    r"pre[- ]?sales|solutions? consultant|customer success|"
    r"recruit(?:er|ing)|talent acquisition|human resources|payroll|"
    r"marketing|brand|content (?:writer|strategist)|copywriter|social media|"
    r"accountant|accounting|auditor|actuar|underwrit|tax |bookkeep|"
    r"procurement|purchasing|buyer|supply chain planner|logistics coordinator|"
    # PM / program / non-technical ops
    r"project manager|program manager|product manager|product owner|"
    r"scrum master|business analyst|operations manager|office manager|"
    r"consultant|consulting|solutions? architect|implementation (?:specialist|consultant)|"
    r"technical account|professional services|delivery manager|"
    # facilities / construction / trades / plant floor
    r"facilities|janitor|custodian|maintenance (?:technician|worker|engineer)|"
    r"hvac|plumb|electrician|welder|machinist|millwright|"
    r"fire (?:protection|sprinkler|alarm)|sprinkler|"
    r"construction|surveyor|estimator|drafter|cad (?:drafter|operator)|"
    r"sand and prep|sander|painter|assembler|packer|forklift|warehouse|"
    r"machine operator|production (?:operator|worker|associate)|"
    r"line (?:operator|worker)|shift (?:lead|supervisor|worker)|"
    r"[0-9](?:st|nd|rd|th) shift|night shift|swing shift|weekend shift|"
    # field / support / clinical / other non-design engineering
    r"field service|field engineer|service technician|"
    r"technical support|help ?desk|desktop support|it support|"
    r"sales engineer|application engineer[,\- ]*(?:sales|pre)|"
    r"clinical|nurse|pharmac|physician|veterinar|"
    r"teacher|instructor|professor|tutor|curriculum|"
    r"driver|courier|security guard|dispatcher|"
    r"chef|cook|barista|server|cashier|retail|"
    # domains outside the owner's field
    r"civil engineer|structural engineer|geotechnical|environmental engineer|"
    r"chemical engineer|petroleum|mining|agricultur|food scien|"
    r"industrial hygien|safety (?:engineer|specialist)|ergonomic|"
    r"process engineer[,\- ]*(?:fire|chemical|plant)|"
    r"nuclear (?:operator|technician)|"
    # experience level mismatches phrased as titles
    r"intern(?:ship)?|co[- ]?op(?![a-z])|apprentice|contractor|temp(?:orary)?|"
    r"phd|post[- ]?doc|research scientist"
    r")(?![a-z])",
    re.I,
)

# Positive gate: the title must look like an engineering role the owner could do.
_ENGINEERING_POSITIVE = re.compile(
    r"(?<![a-z])("
    # software
    r"software|swe|developer|programmer|full[- ]?stack|backend|back[- ]?end|"
    r"frontend|front[- ]?end|platform engineer|systems engineer|"
    r"infrastructure engineer|devops|site reliability|sre|cloud engineer|"
    r"data engineer|machine learning|ml engineer|ai engineer|"
    r"security engineer|network engineer|qa engineer|"
    # NOT bare "quality engineer" — at AbbVie/Abbott that is pharma/manufacturing
    # QA, not engineering. Require a software/hardware qualifier.
    r"quality assurance engineer|software quality|"
    r"test engineer|automation engineer|"
    r"computer (?:engineer|scientist)|applications? developer|"
    # embedded / firmware — the owner's strongest hardware angle
    r"embedded|firmware|bare[- ]?metal|rtos|device driver|bsp|"
    r"microcontroller|mcu|soc engineer|"
    # digital / ASIC / FPGA
    r"asic|fpga|rtl|verilog|vhdl|digital design|logic design|"
    r"verification engineer|design verification|dv engineer|physical design|"
    r"silicon|semiconductor|chip design|hardware engineer|"
    # analog / mixed signal / EE
    r"analog|mixed[- ]signal|electrical engineer|electronics? engineer|"
    r"electronic design|circuit design|power (?:electronics|engineer)|"
    r"rf engineer|signal integrity|pcb|board design|hardware design|"
    r"validation engineer|characterization|bring[- ]?up|"
    # Field Applications Engineer: a legitimate new-grad path into hardware at
    # semiconductor vendors. Sales-flavoured variants are still rejected upstream
    # by the bare "sales" rule and "application engineer, sales".
    r"field applications? engineer|(?<![a-z])fae(?![a-z])|"
    r"systems? design|controls? engineer|mechatronics|robotics engineer|"
    # generic but qualified
    r"engineer(?:ing)? (?:rotational|development program)|"
    r"technical (?:rotational|development)"
    r")(?![a-z])",
    re.I,
)

# Generic "Engineer" with nothing else — allow only when an entry-level marker
# or an in-scope keyword appears elsewhere.
_BARE_ENGINEER = re.compile(r"(?<![a-z])engineer(?:ing)?(?![a-z])", re.I)

# ---------------------------------------------------------------------------
# Stage 5 — description-level gates
# ---------------------------------------------------------------------------

_YEARS_REQUIRED = re.compile(
    r"(?<![.\d])(\d{1,2})\s*\+?\s*(?:-|to|–)?\s*(?:\d{1,2})?\s*"
    r"(?:\+\s*)?years?(?:'|’)?\s*(?:of\s+)?"
    r"(?:relevant\s+|related\s+|professional\s+|industry\s+|work\s+)*"
    r"experience",
    re.I,
)
# Phrases that neutralize a years requirement (internships/coursework count).
_YEARS_SOFTENER = re.compile(
    r"(?:including|such as|internship|co-?op|academic|coursework|projects?|"
    r"or equivalent|degree|education)",
    re.I,
)

_ACTIVE_CLEARANCE = re.compile(
    r"("
    r"(?:active|current|existing|possess(?:es|ing)?|hold(?:s|ing)?|maintain(?:s|ing)?)"
    r"[^.\n]{0,60}?"
    r"(?:security\s+)?clearance"
    r"|clearance[^.\n]{0,40}?(?:is\s+)?(?:required|must be active|currently active)"
    r"|must (?:have|possess|hold)[^.\n]{0,60}?clearance"
    r"|ts\s*/\s*sci|top secret[^.\n]{0,30}(?:required|active)"
    r"|(?:secret|ts|sci|poly(?:graph)?)\s+clearance\s+required"
    r"|requires?\s+an?\s+active"
    r")",
    re.I,
)
# "must be able to obtain" is an ADVANTAGE for a clearance-eligible US citizen.
#
# The second alternative below covers the phrasing the defense tier actually
# uses: "Applicants selected for this position will be REQUIRED TO OBTAIN and
# maintain a government security clearance". That describes a clearance the
# employer sponsors after hire, but it names no ability/eligibility word, so it
# used to fall through to _ACTIVE_CLEARANCE, which matched the "maintain ...
# clearance" half and rejected the posting.
#
# Measured 2026-08-18 while enabling the Workday source: this dropped 23 of 23
# Draper postings that had already cleared the title and location gates,
# including "Mixed Signal Electronic Design Engineer", which tests/test_filters
# lists as a MUST-KEEP acceptance fixture. It is the standard boilerplate at
# Draper, Northrop Grumman, Leidos and RTX — precisely the employers where
# PLAN.md §4 says clearance eligibility is the owner's differentiator, so the
# bug was silently subtracting his strongest advantage.
_OBTAINABLE_CLEARANCE = re.compile(
    r"(?:able|ability|eligib\w*|willing|qualify)\s*(?:to\s+)?(?:obtain|acquire|"
    r"receive|be granted|get)[^.\n]{0,60}?clearance"
    r"|(?:required|must|need(?:s|ed)?|expected)\s+to\s+(?:obtain|acquire|get)"
    r"[^.\n]{0,80}?clearance"
    r"|clearance[^.\n]{0,60}?(?:can|may|will) be (?:obtained|sponsored|granted)"
    r"|(?:sponsor|sponsorship)[^.\n]{0,40}?clearance"
    r"|eligible (?:for|to obtain)[^.\n]{0,40}?clearance",
    re.I,
)

_ADVANCED_DEGREE_REQUIRED = re.compile(
    r"(?:ph\.?\s?d\.?|doctorate)[^.\n]{0,70}?\b(?:required|mandatory)\b"
    r"|requires?\s+(?:an?\s+)?(?:ph\.?\s?d\.?|doctorate|master(?:'|’)?s?\s+degree)"
    r"|must have (?:an? )?(?:ph\.?\s?d\.?|master(?:'|’)?s)",
    re.I,
)
_DEGREE_SOFTENER = re.compile(r"or\s+(?:equivalent|bachelor|b\.?s\.?)", re.I)


# ---------------------------------------------------------------------------

STAGES = (
    "seniority",
    "discipline",
    "level",
    "location",
    "stale",
    "description",
)


def check_age(age_days: Optional[int], max_age_days: Optional[int]) -> FilterResult:
    """Drop postings older than the cutoff.

    Lever reports the requisition's original createdAt, which surfaces reqs
    that have been open for years. A posting with NO date is kept — unknown
    age is not the same as old age, and the email says "posted date unknown"
    rather than guessing.
    """
    if not max_age_days or age_days is None:
        return FilterResult(True)
    if age_days > max_age_days:
        return FilterResult(False, "stale", f"posted {age_days}d ago (cutoff {max_age_days}d)")
    return FilterResult(True)


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    stage: Optional[str] = None
    reason: str = ""
    metro_class: MetroClass = "none"
    metro: Optional[str] = None
    clearance_advantage: bool = False


def _title_tail(title: str) -> str:
    """Last whitespace-separated chunk after stripping punctuation — where level
    markers live ('Software Engineer II', 'Field Service Engineer IV')."""
    cleaned = re.sub(r"[(),/|]", " ", title)
    parts = cleaned.split()
    return " ".join(parts[-2:]) if parts else ""


def check_title_seniority(title: str) -> FilterResult:
    if _ENTRY_LEVEL_OVERRIDE.search(title):
        # An explicit new-grad marker beats an ambiguous level token, but not
        # an explicit senior word.
        m = _SENIORITY.search(title)
        if m and not _SENIORITY_FALSE_POSITIVE.search(title):
            return FilterResult(False, "seniority", f"senior title: {m.group(0)!r}")
        return FilterResult(True)

    m = _SENIORITY.search(title)
    if m and not _SENIORITY_FALSE_POSITIVE.search(title):
        return FilterResult(False, "seniority", f"senior title: {m.group(0)!r}")

    tail = _title_tail(title)
    lm = _LEVEL_MARKER.search(tail)
    if lm:
        return FilterResult(
            False, "seniority", f"level marker in title: {lm.group(0)!r}"
        )
    return FilterResult(True)


def check_title_discipline(title: str) -> FilterResult:
    m = _NON_ENGINEERING.search(title)
    if m:
        return FilterResult(False, "discipline", f"non-target role: {m.group(0)!r}")

    if _ENGINEERING_POSITIVE.search(title):
        return FilterResult(True)

    if _BARE_ENGINEER.search(title) and _ENTRY_LEVEL_OVERRIDE.search(title):
        # "Engineer I", "Graduate Engineer", "Engineering Rotational Program"
        return FilterResult(True)

    return FilterResult(
        False, "discipline", "no engineering signal in title"
    )


# A level marker attached to a role noun anywhere in the title, not just the
# tail: "Software Engineer 3, Atlas Identity and Access Management".
_ROLE_LEVEL = re.compile(
    r"(?<![a-z])(?:engineer|developer|scientist|analyst|designer|programmer)"
    r"\s*[,\-]?\s*"
    r"(?:level\s*)?"
    r"(?P<lvl>i{2,3}|iv|v(?![a-z])|vi{0,3}|[2-9]|l[2-9]|t[2-9]|e[2-9])"
    r"(?![a-z0-9])",
    re.I,
)


def check_title_level(title: str) -> FilterResult:
    """Reject titles that are plausible engineering but clearly not new-grad."""
    if _ENTRY_LEVEL_OVERRIDE.search(title):
        return FilterResult(True)
    m = _ROLE_LEVEL.search(title)
    if m:
        return FilterResult(
            False, "level", f"level marker on role noun: {m.group('lvl')!r}"
        )
    if re.search(r"(?<![a-z])(?:ii|2)(?![a-z0-9])", _title_tail(title), re.I):
        return FilterResult(False, "level", "level II title")
    return FilterResult(True)


def check_location(location: str) -> FilterResult:
    metro_class, metro = classify_location(location)
    if metro_class == "none":
        return FilterResult(
            False, "location", f"outside target metros: {location or '(blank)'}"
        )
    return FilterResult(True, metro_class=metro_class, metro=metro)


def check_description(description: str) -> FilterResult:
    """Gates that need the posting body. Safe to call with an empty string."""
    if not description:
        return FilterResult(True)

    text = description

    # --- clearance ---
    obtainable = bool(_OBTAINABLE_CLEARANCE.search(text))
    active = _ACTIVE_CLEARANCE.search(text)
    if active and not obtainable:
        return FilterResult(
            False, "description", f"requires active clearance: {active.group(0)[:60]!r}"
        )

    # --- years of experience ---
    for m in _YEARS_REQUIRED.finditer(text):
        try:
            years = int(m.group(1))
        except (TypeError, ValueError):
            continue
        if years < 3:
            continue
        window = text[max(0, m.start() - 120) : m.end() + 120]
        if _YEARS_SOFTENER.search(window):
            continue
        return FilterResult(
            False, "description", f"requires {years}+ years experience"
        )

    # --- advanced degree ---
    dm = _ADVANCED_DEGREE_REQUIRED.search(text)
    if dm:
        window = text[max(0, dm.start() - 80) : dm.end() + 120]
        if not _DEGREE_SOFTENER.search(window):
            return FilterResult(
                False, "description", "requires an advanced degree"
            )

    return FilterResult(True, clearance_advantage=obtainable)


def evaluate(title: str, location: str, description: str = "") -> FilterResult:
    """Run every stage in order. Returns the first failure, or a pass carrying
    the metro classification and the clearance-advantage flag."""
    r = check_title_seniority(title)
    if not r.passed:
        return r
    r = check_title_discipline(title)
    if not r.passed:
        return r
    r = check_title_level(title)
    if not r.passed:
        return r
    loc = check_location(location)
    if not loc.passed:
        return loc
    desc = check_description(description)
    if not desc.passed:
        return desc
    return FilterResult(
        True,
        metro_class=loc.metro_class,
        metro=loc.metro,
        clearance_advantage=desc.clearance_advantage,
    )


def filter_jobs(jobs) -> tuple[list, dict[str, int]]:
    """Filter a list of Job objects. Returns (survivors, per-stage reject counts)."""
    counts = {stage: 0 for stage in STAGES}
    survivors = []
    for job in jobs:
        result = evaluate(job.title, job.location, job.description)
        if result.passed:
            job.metro_class = result.metro_class
            job.metro = result.metro
            job.clearance_advantage = result.clearance_advantage
            survivors.append(job)
        else:
            counts[result.stage] = counts.get(result.stage, 0) + 1
    return survivors, counts
