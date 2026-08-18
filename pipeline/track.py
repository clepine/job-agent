"""Per-posting track inference.

companies.yaml carries a track per *company*, which is only a hint: DoorDash is
tagged `hardware` because it appears in the hardware aggregator repo, but
"Software Engineer, Backend" at DoorDash is obviously a software role. Matching
that posting against the hardware resume would be wrong on both sides — bad
BM25 signal in, bad keyword diff out.

So the track is decided per posting from the title (and, as a tiebreaker, the
description), with the company tag used only when the title is genuinely
ambiguous.
"""

from __future__ import annotations

import re

_HARDWARE_STRONG = re.compile(
    r"(?<![a-z])("
    r"asic|fpga|rtl|verilog|vhdl|systemverilog|"
    r"analog|mixed[- ]signal|rf engineer|radio frequency|"
    r"electrical engineer|electronics? engineer|electronic design|"
    r"circuit design|power electronics|signal integrity|"
    r"pcb|board design|hardware (?:design|engineer)|schematic|"
    r"silicon|semiconductor|chip design|physical design|dft|"
    r"soc design|vlsi|process engineer|device engineer|"
    r"mechanical engineer|mechatronics|thermal engineer|"
    r"optical engineer|photonic|electro-?optic|"
    r"test engineer[,\- ]*(?:hardware|electrical|rf)|"
    r"hardware validation|characterization engineer|"
    r"manufacturing engineer|systems engineer[,\- ]*hardware"
    r")(?![a-z])",
    re.I,
)

_EMBEDDED = re.compile(
    r"(?<![a-z])(embedded|firmware|bare[- ]?metal|rtos|device driver|"
    r"microcontroller|bsp|board support)(?![a-z])",
    re.I,
)

_SOFTWARE_STRONG = re.compile(
    r"(?<![a-z])("
    r"software engineer|software developer|swe|"
    r"backend|back[- ]end|frontend|front[- ]end|full[- ]?stack|"
    r"web developer|mobile (?:engineer|developer)|ios|android|"
    r"devops|site reliability|sre|platform engineer|infrastructure engineer|"
    r"cloud engineer|data engineer|database engineer|"
    r"machine learning engineer|ml engineer|ai engineer|"
    r"research engineer|applied scientist|"
    r"security engineer|network engineer|"
    r"api|microservice|distributed systems"
    r")(?![a-z])",
    re.I,
)


def infer_track(title: str, description: str = "", company_hint: str = "software") -> str:
    """Return 'software' or 'hardware' for one posting.

    Embedded/firmware is deliberately routed to HARDWARE: PLAN.md §4 names it
    the owner's strongest hardware angle, and the hardware resume is the one
    that carries MSP430, bring-up, and the clearance-eligibility line.
    """
    t = title or ""

    hw = bool(_HARDWARE_STRONG.search(t))
    sw = bool(_SOFTWARE_STRONG.search(t))
    emb = bool(_EMBEDDED.search(t))

    if emb and not sw:
        return "hardware"
    if hw and not sw:
        return "hardware"
    if sw and not hw:
        return "software"
    if hw and sw:
        # e.g. "Embedded Software Engineer" — embedded wins, it is the bridge role.
        return "hardware" if emb or hw else "software"

    # Title was ambiguous ("Engineer I", "Systems Engineer"). Try the body.
    body = (description or "")[:4000]
    if body:
        hw_hits = len(_HARDWARE_STRONG.findall(body)) + len(_EMBEDDED.findall(body))
        sw_hits = len(_SOFTWARE_STRONG.findall(body))
        if hw_hits > sw_hits * 1.5:
            return "hardware"
        if sw_hits > hw_hits * 1.5:
            return "software"

    if company_hint in ("software", "hardware"):
        return company_hint
    return "software"
