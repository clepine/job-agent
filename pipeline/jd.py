"""Job-description compression.

Nothing reaches the model raw. A typical posting is ~1200-2500 tokens, of which
the requirements/responsibilities are maybe 20%. The rest is benefits, EEO
boilerplate, "about us", and application instructions — pure cost.

compress_jd() is a pure local function: HTML -> text -> section split ->
keep-list -> truncate. Target ~300 tokens out of a ~1500-token posting.
"""

from __future__ import annotations

import html
import re

# --- HTML -> text -------------------------------------------------------

_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_BR = re.compile(r"<br\s*/?>", re.I)
_BLOCK_END = re.compile(r"</(p|div|li|ul|ol|h[1-6]|tr|table|section)>", re.I)
_LI = re.compile(r"<li[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    text = _SCRIPT_STYLE.sub(" ", raw)
    text = _BR.sub("\n", text)
    text = _LI.sub("\n- ", text)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    # Collapse runs of spaces but preserve line structure.
    lines = [" ".join(ln.split()) for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


# --- Section classification --------------------------------------------

# A heading (or a line acting as one) that starts a section we WANT.
_KEEP_HEADING = re.compile(
    r"^\s*[\W_]*\b("
    r"requirements?|required|minimum (?:qualifications?|requirements?)|"
    r"basic qualifications?|preferred (?:qualifications?|skills?|experience)?|"
    r"qualifications?|skills?(?: (?:and|&) (?:experience|abilities))?|"
    r"what you(?:'| a|\u2019)?ll (?:do|bring|need)|what you will do|"
    r"responsibilities|key responsibilities|duties|the role|role overview|"
    r"job (?:description|duties|summary)|position summary|about the role|"
    r"about (?:this|the) (?:job|position|opportunity)|"
    r"you (?:will|should) (?:have|be)|we(?:'|\u2019)?re looking for|"
    r"who you are|nice to have|technical skills?|desired skills?|"
    r"education(?: (?:and|&) experience)?|experience"
    r")\b[\s:\u2013\u2014-]*$",
    re.I,
)

# A heading that starts a section we want to DROP (everything until the next
# keep-heading).
_DROP_HEADING = re.compile(
    r"^\s*[\W_]*\b("
    r"benefits?|perks?|compensation(?: (?:and|&) benefits?)?|pay (?:range|transparency)|"
    r"salary|total rewards?|what we offer|our offer|why (?:join|work)|"
    r"about (?:us|the company|our company|[A-Z][\w.& ]{0,40})|company overview|"
    r"who we are|our (?:mission|values|culture|story|team culture)|life at|"
    r"equal (?:employment )?opportunity|eeo|e\.?e\.?o\.?|diversity|"
    r"accommodations?|reasonable accommodation|"
    r"how to apply|application (?:process|instructions?)|to apply|next steps|"
    r"legal|privacy|disclaimer|notice to|agencies|recruiters?|"
    r"export control|e-?verify|background check|drug (?:test|screen)|"
    r"physical (?:demands|requirements)|work environment|travel requirements?"
    r")\b",
    re.I,
)

# Sentence/line-level boilerplate, matched anywhere.
_LINE_NOISE = re.compile(
    r"(equal opportunity employer"
    r"|without regard to race"
    r"|regardless of race, (?:color|religion)"
    r"|protected veteran"
    r"|disability status"
    r"|affirmative action"
    r"|e-?verify"
    r"|reasonable accommodation"
    r"|\bEEO\b"
    r"|applicants? will (?:not )?be considered"
    r"|we are committed to (?:building )?(?:a )?diverse"
    r"|criminal histor"
    r"|fair chance"
    r"|san francisco fair chance"
    r"|pay (?:range|transparency)|salary range|base (?:pay|salary) range"
    r"|\$\d[\d,]*\s*(?:-|to|\u2013)\s*\$\d"
    r"|401\(?k\)?|health(?:care)? (?:insurance|benefits)|dental|vision (?:insurance|plan)"
    r"|paid time off|\bPTO\b|parental leave|tuition reimbursement"
    r"|stock options|equity (?:grant|package)|employee stock"
    r"|please (?:apply|submit|note|visit)"
    r"|click (?:here|the link)"
    r"|no agencies|third[- ]party (?:recruiters?|agencies)"
    r"|cookie|privacy policy|terms of (?:use|service)"
    r")",
    re.I,
)

# Signals that a line is substantive even without a heading.
_SUBSTANCE = re.compile(
    r"\b(experience|degree|bachelor|b\.?s\.?|m\.?s\.?|coursework|proficien|"
    r"familiar|knowledge of|understanding of|ability to|skills?|"
    r"python|c\+\+|verilog|vhdl|rtl|fpga|asic|embedded|firmware|pcb|"
    r"schematic|matlab|spice|altium|kicad|cadence|synopsys|vivado|quartus|"
    r"oscilloscope|logic analyzer|soldering|bring-?up|debug|"
    r"java|javascript|typescript|golang|rust|kotlin|swift|sql|linux|"
    r"docker|kubernetes|aws|azure|gcp|ci/?cd|git|rest|api|microservice|"
    r"design|develop|implement|test|validat|verif|analy|build|support|"
    r"collaborat|document|maintain|troubleshoot|responsib|qualif|require)\b",
    re.I,
)


def compress_jd(raw: str, max_chars: int = 1600) -> str:
    """Strip boilerplate; keep requirements / qualifications / responsibilities.

    Deliberately conservative: when a posting has no recognizable section
    headings at all, fall back to substance-scored lines rather than returning
    nothing. An empty JD would silently degrade scoring quality.
    """
    text = html_to_text(raw) if "<" in (raw or "") else (raw or "")
    if not text.strip():
        return ""

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    kept: list[str] = []
    dropping = False
    saw_keep_heading = False

    for line in lines:
        # A short line ending in ':' or a bare title-ish line can be a heading.
        is_headingish = len(line) <= 90

        if is_headingish and _KEEP_HEADING.match(line.rstrip(":")):
            dropping = False
            saw_keep_heading = True
            kept.append(line.rstrip(":") + ":")
            continue

        if is_headingish and _DROP_HEADING.match(line.rstrip(":")):
            dropping = True
            continue

        if dropping:
            continue
        if _LINE_NOISE.search(line):
            continue
        # Drop very long unbroken prose blocks that carry no substance signal.
        if len(line) > 400 and not _SUBSTANCE.search(line):
            continue
        kept.append(line)

    if not saw_keep_heading:
        # No headings found — score lines individually.
        kept = [
            ln
            for ln in lines
            if not _LINE_NOISE.search(ln)
            and (_SUBSTANCE.search(ln) or ln.startswith("-"))
        ]

    out: list[str] = []
    seen: set[str] = set()
    for line in kept:
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)

    result = "\n".join(out).strip()
    if len(result) > max_chars:
        # Cut at a line boundary so we never hand the model a half-sentence.
        cut = result[:max_chars]
        nl = cut.rfind("\n")
        result = (cut[:nl] if nl > max_chars // 2 else cut).rstrip() + "\n[truncated]"
    return result
