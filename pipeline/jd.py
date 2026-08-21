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

    # Some boards hand back ESCAPED markup - Greenhouse returns
    # `&lt;div class=&quot;content-intro&quot;&gt;` rather than `<div ...>`.
    # Every pattern below matches on real angle brackets, so against escaped
    # input they strip nothing at all and the "text" that comes out the far end
    # is still markup. That is not cosmetic: this function feeds compress_jd,
    # which feeds both the keyword diff and the JD sent to the scorer. Measured
    # 2026-08-21, a SpaceX posting yielded ZERO keywords through the escaped
    # path and eleven (C, C++, Python, Go, Rust, gRPC, PostgreSQL...) once
    # unescaped - and the 2026-08-21 email printed "none detected" under two of
    # its five roles for exactly this reason, while the model scored their fit
    # from tag soup.
    #
    # Bounded to two rounds: enough for the one real double-escape case, and it
    # cannot loop on adversarial input.
    text = raw
    for _ in range(2):
        if _TAG.search(text) or "&lt;" not in text:
            break
        text = html.unescape(text)

    text = _SCRIPT_STYLE.sub(" ", text)
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
    # `&lt;` matters as much as `<` here: Greenhouse escapes its markup, so an
    # escaped posting has no literal angle bracket, skipped this branch
    # entirely, and arrived at the section splitter still as tag soup - which
    # matches no heading, so the whole keep-list fell through to the substance
    # fallback and the compressed JD went to the model as markup. This guard is
    # why the html_to_text unescape fix above was invisible until now.
    raw = raw or ""
    text = html_to_text(raw) if ("<" in raw or "&lt;" in raw) else raw
    if not text.strip():
        return ""

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    # Anything ahead of the FIRST recognized section heading is preamble, and
    # preamble is the "about us" pitch. `dropping` starts False, so that block
    # used to be kept in full and then the max_chars truncation cut the posting
    # off before it ever reached the qualifications. Measured 2026-08-21, a
    # SpaceX req compressed to 1,551 characters of "SpaceX was founded under the
    # belief that..." and yielded ZERO keywords, while its own requirements
    # section listed Rust, C++, Python, Go, gRPC and PostgreSQL. The model was
    # scoring fit from the company blurb.
    #
    # Only applied when a keep-heading actually exists: a posting with no
    # recognizable sections still falls through to the substance-scored
    # fallback below, unchanged.
    first_keep = next(
        (
            i
            for i, ln in enumerate(lines)
            if len(ln) <= 90 and _KEEP_HEADING.match(ln.rstrip(":"))
        ),
        None,
    )
    if first_keep is not None:
        lines = lines[first_keep:]

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
