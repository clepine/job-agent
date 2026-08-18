"""Metro matching for the geography filter (PLAN.md §2 stage 4 / §4).

Locations arrive in wildly inconsistent shapes:
    "Durham, NC" | "Research Triangle Park" | "Mountain View, CALIFORNIA"
    "United States - Illinois - Abbott Park" | "Gaffney, SC US" | "Remote"
so we match on city tokens with a state guard rather than exact strings.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

MetroClass = Literal["primary", "secondary", "remote_us", "none"]

# Each metro: canonical name -> (city phrases, acceptable state codes/names)
PRIMARY_METROS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "RTP/Raleigh-Durham": (
        (
            "raleigh", "durham", "cary", "chapel hill", "morrisville",
            "research triangle", "rtp", "apex", "wake forest", "holly springs",
        ),
        ("nc", "north carolina"),
    ),
    "Charlotte": (
        ("charlotte", "concord", "huntersville", "matthews", "fort mill", "rock hill"),
        ("nc", "north carolina", "sc", "south carolina"),
    ),
    "Boston": (
        (
            "boston", "cambridge", "somerville", "waltham", "burlington",
            "bedford", "lexington", "andover", "billerica", "chelmsford",
            "lowell", "marlborough", "westford", "woburn", "needham",
            "quincy", "framingham", "natick", "wilmington", "north reading",
            "devens", "hopkinton", "canton", "norwood", "littleton",
            "tewksbury", "beverly", "wakefield", "watertown", "newton",
        ),
        ("ma", "massachusetts", "nh", "new hampshire"),
    ),
    "NYC": (
        (
            "new york", "nyc", "manhattan", "brooklyn", "queens", "jersey city",
            "hoboken", "newark", "white plains", "stamford", "long island city",
        ),
        ("ny", "new york", "nj", "new jersey", "ct", "connecticut"),
    ),
    "Chicago": (
        (
            "chicago", "evanston", "schaumburg", "naperville", "oak brook",
            "deerfield", "northbrook", "rosemont", "elk grove", "aurora",
            "abbott park", "libertyville", "lincolnshire", "warrenville",
        ),
        ("il", "illinois"),
    ),
}

SECONDARY_METROS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "Austin": (("austin", "round rock", "cedar park", "georgetown"), ("tx", "texas")),
    "Bay Area": (
        (
            "san jose", "santa clara", "sunnyvale", "mountain view", "palo alto",
            "san francisco", "fremont", "milpitas", "cupertino", "menlo park",
            "redwood city", "san mateo", "hayward", "pleasanton", "livermore",
            "berkeley", "oakland", "campbell", "los gatos", "newark ca",
            "south san francisco", "foster city", "san carlos", "emeryville",
        ),
        ("ca", "california"),
    ),
    "Seattle": (
        ("seattle", "bellevue", "redmond", "kirkland", "renton", "everett", "bothell"),
        ("wa", "washington"),
    ),
    "Phoenix": (
        ("phoenix", "chandler", "tempe", "mesa", "scottsdale", "gilbert", "glendale"),
        ("az", "arizona"),
    ),
    "Dallas": (
        (
            "dallas", "plano", "richardson", "irving", "fort worth", "frisco",
            "addison", "allen", "mckinney", "arlington tx", "garland",
        ),
        ("tx", "texas"),
    ),
    "Huntsville": (("huntsville", "madison al", "redstone"), ("al", "alabama")),
}

# "Remote" that is explicitly US-scoped (or unqualified remote on a US board).
_REMOTE_RE = re.compile(r"\bremote\b|\bwork from home\b|\bwfh\b|\bvirtual\b", re.I)
_REMOTE_NON_US = re.compile(
    r"\b(canada|emea|apac|europe|india|uk|united kingdom|ireland|germany|poland|"
    r"israel|singapore|japan|china|australia|latam|mexico|brazil|argentina)\b",
    re.I,
)

# Non-US signal used to reject outright.
_NON_US = re.compile(
    r"\b(canada|ontario|toronto|vancouver|montreal|london|dublin|berlin|munich|"
    r"paris|madrid|barcelona|amsterdam|zurich|tel aviv|bangalore|bengaluru|"
    r"hyderabad|pune|chennai|noida|gurgaon|singapore|tokyo|seoul|shanghai|"
    r"beijing|shenzhen|taipei|sydney|melbourne|warsaw|krakow|prague|lisbon|"
    r"stockholm|copenhagen|oslo|helsinki|mexico city|sao paulo|bogota|"
    r"buenos aires|united kingdom|netherlands|switzerland|germany|france|"
    r"spain|italy|sweden|norway|denmark|finland|poland|romania|portugal|"
    r"israel|india|japan|korea|australia|new zealand|philippines|vietnam|"
    r"malaysia|indonesia|thailand|costa rica|colombia|chile|peru)\b",
    re.I,
)

_STATE_ABBR = re.compile(r"\b([A-Z]{2})\b")


def _state_ok(fragment: str, states: tuple[str, ...]) -> bool:
    """A city phrase alone is not enough — guard against e.g. Cambridge, UK."""
    low = fragment.lower()
    for st in states:
        if len(st) == 2:
            if re.search(rf"(?<![a-z]){st}(?![a-z])", low):
                return True
        elif st in low:
            return True
    # If the fragment carries no state at all (e.g. bare "Research Triangle
    # Park"), accept — the city names in our lists are distinctive in the US.
    if not _STATE_ABBR.search(fragment) and "," not in fragment:
        return True
    return False


def _city_part(fragment: str) -> str:
    """The leading segment of 'City, State, Country'.

    Matching the whole string is what turned 'Clifton Park, New York' into a
    NYC hit — the STATE name 'New York' satisfied the CITY phrase 'new york'.
    Only the part before the first comma may satisfy a city phrase.
    """
    return " " + " ".join(fragment.split(",")[0].lower().split()) + " "


def _match(fragment: str, table: dict) -> Optional[str]:
    city_text = _city_part(fragment)
    for name, (cities, states) in table.items():
        for city in cities:
            if f" {city} " in city_text and _state_ok(fragment, states):
                return name
    return None


def classify_location(location: str) -> tuple[MetroClass, Optional[str]]:
    """Return (class, metro_name). Only primary/secondary/remote_us are eligible.

    Multi-site postings ("San Francisco, CA; New York, NY; Seattle, WA") are
    split and each site is classified; the best class wins, so a job that lists
    one primary metro among several counts as primary.
    """
    if not location:
        return ("none", None)

    fragments = [f.strip() for f in re.split(r"[;|/]|\bor\b", location) if f.strip()]
    if not fragments:
        fragments = [location]

    best: tuple[MetroClass, Optional[str]] = ("none", None)
    for fragment in fragments:
        if _NON_US.search(fragment):
            continue
        name = _match(fragment, PRIMARY_METROS)
        if name:
            return ("primary", name)
        if best[0] == "none":
            name = _match(fragment, SECONDARY_METROS)
            if name:
                best = ("secondary", name)

    if best[0] != "none":
        return best

    if _REMOTE_RE.search(location) and not _REMOTE_NON_US.search(location):
        return ("remote_us", "Remote (US)")

    return ("none", None)


def location_bonus(metro_class: MetroClass, cfg: dict) -> int:
    geo = cfg.get("geography", {})
    if metro_class == "primary":
        return int(geo.get("primary_bonus", 15))
    if metro_class == "secondary":
        return int(geo.get("secondary_penalty", -5))
    return 0
