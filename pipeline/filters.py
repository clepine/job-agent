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

from . import jd
from dataclasses import dataclass
from typing import Optional

from sources import workday

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
# The same requirement phrased WITHOUT the word "experience" - which is how
# roughly a quarter of postings phrase it:
#
#     - 3+ years in SRE, DevOps, field/systems engineering
#     - 4+ years building automation, developer infrastructure
#     - Requires 3-5 years in Systems Engineering or relevant role.
#
# Measured 2026-08-21: 64 of 227 hydrated postings cleared the gate this way,
# including Draper, Anduril, OpenAI, Citi and three NVIDIA reqs.
#
# The continuation is mandatory, and that is what keeps company boilerplate out:
# "NVIDIA has been transforming computer graphics for more than 25 years." ends
# the sentence at "years" and never reaches one of these words.
_YEARS_ROLE_REQUIRED = re.compile(
    r"(?<![.\d])(\d{1,2})\s*\+?\s*(?:-|to|\u2013)?\s*(?:\d{1,2})?\s*\+?\s*"
    r"years?\s+(?:of\s+)?"
    r"(?:in|with|building|designing|developing|writing|working|leading|managing"
    r"|relevant|related|professional|industry|hands[- ]on|prior|practical"
    # "N years of <noun>" - "7+ years of post-silicon validation". Requires the
    # "of", which is what company boilerplate ("...for more than 25 years.")
    # does not have.
    r"|of\s+[a-z])",
    re.I,
)

# Phrases that neutralize a years requirement.
#
# Every entry must express an ALTERNATIVE to the years, not merely sit near
# them. Bare "degree" and "education" used to be on this list and they are the
# opposite of a softener in the shape postings actually use:
#
#     Bachelor's degree in Electrical Engineering and 4+ years of experience
#
# The degree there is an ADDITIONAL requirement joined by "and", so matching on
# the word cancelled the very requirement it was compounding. Measured
# 2026-08-21 across the live pool, that one word accounted for 22 of the 67
# postings stating a 3+ year requirement - every one of them passed the gate.
# "or equivalent" and "in lieu of" stay, because those genuinely offer a way in
# without the years.
_YEARS_SOFTENER = re.compile(
    r"(?:in lieu of"
    r"|or equivalent"
    r"|equivalent (?:combination|experience)"
    # "including" and "such as" only soften when what follows is the kind of
    # experience the owner HAS. Bare, they are descriptive, not permissive:
    # "7+ years of post-silicon validation, preferably including ASIC bring-up"
    # narrows the requirement, it does not waive it.
    r"|(?:including|such as)[^.]{0,40}?"
    r"(?:internships?|co-?ops?|academic|coursework|school|projects?)"
    r"|internships?|co-?ops?|academic|coursework)",
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


def max_age_for(metro_class: str, limits: dict) -> Optional[int]:
    """The posting-age cutoff that applies to one metro class.

    Owner's call, 2026-08-19, replacing a single global 7 days.

    A 7-day window was measured in August against the whole board list and ran
    a healthy surplus — 119 software and 67 hardware postings already past the
    filters. But that surplus is not distributed the way his priorities are.
    Broken down by metro on 2026-08-19 the same pool held:

        Bay Area 392   Seattle 106   NYC 95
        Boston 37      Chicago 21    Charlotte 3    RTP/Raleigh-Durham 1

    So the window was abundant exactly where he is least likely to move and
    starving in the two markets he actually lives near. The obvious hypothesis
    — missing employers — was tested and rejected: eighteen additional boards,
    all probed live and answering (Truist, Duke Energy, TIAA, Epic Games,
    Pendo, Bandwidth, SAS, Toast, Klaviyo, Cognex, Marvell, Cadence and
    others), contribute FOUR postings inside 7 days and FIFTY-THREE with no age
    limit. Epic Games, Bandwidth and Pendo each have RTP new-grad engineering
    roles 13-29 days old. They are open reqs; the cutoff was throwing them away.

    The intent of the original decision — only genuinely fresh reqs are worth
    his time — is preserved where supply justifies it. In the five primary
    metros, where a month can pass without a single qualifying posting, a
    three-week-old req is still a real lead.

    Note for whoever tunes this next: the FETCH-side cutoff must be the most
    generous of these values, not this one. Metro class is not known until
    after the location filter runs, so fetching to the 7-day cutoff would throw
    away the primary-metro postings before anything could classify them.
    """
    primary = limits.get("max_posting_age_days_primary")
    default = limits.get("max_posting_age_days")
    if metro_class == "primary" and primary:
        return int(primary)
    return int(default) if default else None


def fetch_max_age(limits: dict) -> Optional[int]:
    """The most generous cutoff, for the fetch layer. See max_age_for()."""
    values = [
        limits.get("max_posting_age_days"),
        limits.get("max_posting_age_days_primary"),
    ]
    values = [int(v) for v in values if v]
    return max(values) if values else None


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


def check_url_title(url: str) -> FilterResult:
    """Re-run the title gates against the title encoded in the posting URL.

    A second, independent witness to what a posting is called. The aggregator
    READMEs truncate titles at a fixed width, and a truncated title can hide the
    exact word that disqualifies it: Draper's "Embedded Quality & Fielded
    Systems Intern" arrives as "...Systems In", which contains no "intern" for
    check_title_discipline to match. On 2026-08-18 that internship was the top
    hardware pick of a real email.

    Hydration usually restores the full title, but it is best-effort over the
    network and nothing ever re-hydrates a posting already in the database, so
    one failed request meant a permanently disqualifier-blind title. This check
    needs no request.

    REJECT-ONLY, by construction. The URL slug is lossy (Workday collapses "&"
    and "," into dashes), so it is never good enough to display or to rescue a
    posting the real title already failed — it can only add a rejection the
    truncated title was hiding. A non-Workday URL yields no title and passes.
    """
    probe = workday.title_from_url(url)
    if not probe:
        return FilterResult(True)
    for check in (check_title_seniority, check_title_discipline, check_title_level):
        result = check(probe)
        if not result.passed:
            return FilterResult(
                False, result.stage, f"{result.reason} (from URL slug: {probe!r})"
            )
    return FilterResult(True)


def check_location(location: str) -> FilterResult:
    metro_class, metro = classify_location(location)
    if metro_class == "none":
        return FilterResult(
            False, "location", f"outside target metros: {location or '(blank)'}"
        )
    return FilterResult(True, metro_class=metro_class, metro=metro)


# A clause boundary: a line break, a bullet, or the end of a sentence.
_TAG_LEFTOVER = re.compile(r"<[a-z/][^>]{0,80}>", re.I)

_CLAUSE_SPLIT = re.compile(r"[\n\r]+|(?<=[.;:])\s+|</?(?:li|p|br|ul|ol|div|tr|h[1-6])\b[^>]*>", re.I)


def _clause_around(text: str, start: int, end: int) -> str:
    """The bullet or sentence containing text[start:end].

    Deliberately NOT a fixed character window. Workday renders a requirements
    block with the degree bullet directly above the years bullet:

        - Bachelor's in EE/CE/CS, or a related field, or equivalent experience.
        - 5+ years of experience in embedded software, firmware, ...

    A +/-120 character window straddles that boundary, so "or equivalent" from
    the DEGREE line silently neutralized the years requirement on the NEXT one,
    and the gate passed every posting laid out this way - which is most of them.

    Measured 2026-08-21 against live NVIDIA JR2022612: the posting demands 5+
    years, the gate passed it, and the 2026-08-21 email shipped it at fit 68 as
    an entry-level match. Owner's call the same day: entry-level is the hard
    requirement of this agent, so a softener only counts inside its own clause.
    """
    lo, hi = 0, len(text)
    for m in _CLAUSE_SPLIT.finditer(text):
        if m.end() <= start:
            lo = m.end()
        elif m.start() >= end:
            hi = m.start()
            break
    return text[lo:hi]


def _normalize_body(description: str) -> str:
    """Posting body as plain text, whatever the source handed us.

    The hydrated body is raw HTML for most sources, and every gate in this
    function reads structure that HTML hides: the years softener is scoped to a
    clause, and the clearance patterns are bounded by `[^.\n]{0,60}`. Neither
    boundary exists in `<p>Master&#39;s degree preferred.</p><p><b>Experience</b>
    </p><p>3-5 years experience in electrical engineering</p>` - it is one
    unbroken line, so the degree softener three paragraphs up still cancelled
    the years requirement below it and the clearance window ran straight through
    tag soup.

    Measured 2026-08-21 against the live pool: of 25 hydrated postings stating a
    3+ year requirement, the gate rejected ZERO. Clause-scoping alone recovered
    4; normalizing the body first is what recovers the rest.

    Two passes, because some sources double-escape (`&amp;lt;/li&amp;gt;`):
    unescaping once leaves literal tags behind in the text.
    """
    text = jd.html_to_text(description)
    # Belt and braces: html_to_text already unescapes escaped markup, but a
    # source that escapes three times would still leave tags behind here.
    if _TAG_LEFTOVER.search(text):
        text = jd.html_to_text(text)
    return text


def check_description(description: str) -> FilterResult:
    """Gates that need the posting body. Safe to call with an empty string."""
    if not description:
        return FilterResult(True)

    text = _normalize_body(description)

    # --- clearance ---
    obtainable = bool(_OBTAINABLE_CLEARANCE.search(text))
    active = _ACTIVE_CLEARANCE.search(text)
    if active and not obtainable:
        return FilterResult(
            False, "description", f"requires active clearance: {active.group(0)[:60]!r}"
        )

    # --- years of experience ---
    # Both phrasings: "N years of experience" and "N years in/building/...".
    matches = sorted(
        list(_YEARS_REQUIRED.finditer(text)) + list(_YEARS_ROLE_REQUIRED.finditer(text)),
        key=lambda m: m.start(),
    )
    for m in matches:
        try:
            years = int(m.group(1))
        except (TypeError, ValueError):
            continue
        if years < 3:
            continue
        # Scoped to the CLAUSE, not a character window. See _clause_around.
        if _YEARS_SOFTENER.search(_clause_around(text, m.start(), m.end())):
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


def evaluate(
    title: str, location: str, description: str = "", url: str = ""
) -> FilterResult:
    """Run every stage in order. Returns the first failure, or a pass carrying
    the metro classification and the clearance-advantage flag.

    `url` is optional and reject-only: it lets check_url_title catch a
    disqualifier that a truncated title was hiding.
    """
    r = check_title_seniority(title)
    if not r.passed:
        return r
    r = check_title_discipline(title)
    if not r.passed:
        return r
    r = check_title_level(title)
    if not r.passed:
        return r
    r = check_url_title(url)
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
        result = evaluate(job.title, job.location, job.description, job.url)
        if result.passed:
            job.metro_class = result.metro_class
            job.metro = result.metro
            job.clearance_advantage = result.clearance_advantage
            survivors.append(job)
        else:
            counts[result.stage] = counts.get(result.stage, 0) + 1
    return survivors, counts
