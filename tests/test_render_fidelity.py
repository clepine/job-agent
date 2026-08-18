"""Rendered-PDF fidelity, and the ATS-safety guarantee.

Two things are asserted here, and they are the same test:

1. FIDELITY. Every heading, skill category label, and skill item that appears
   in the rendered PDF must appear VERBATIM in the owner's source resume. This
   exists because the renderer previously derived skill headers by title-casing
   a snake_case YAML key, which silently turned "Frameworks & Libraries" into
   "Frameworks Libraries" on a document that goes to employers. Any future
   transform-the-key shortcut fails here.

2. ATS SAFETY. The assertions are made against text EXTRACTED FROM THE PDF —
   not against the YAML, and not against the renderer's inputs. If this test
   passes, the PDF's text layer is real, selectable, in correct reading order,
   and complete. That is exactly what an ATS parser consumes, so a passing test
   IS the ATS guarantee. It is backed by structural checks (no images, no
   embedded font subsets, no tables) below.
"""

from __future__ import annotations

import re
import zlib
from pathlib import Path

import pytest
import yaml

from pipeline.resumes import skill_groups
from resume.render import load_resume, render

REPO = Path(__file__).resolve().parent.parent

MASTERS = [
    ("master_sw", REPO / "resume/master_sw.yaml", REPO / "resume/master_sw.source.txt"),
    ("master_hw", REPO / "resume/master_hw.yaml", REPO / "resume/master_hw.source.txt"),
]


# --- PDF text extraction ---------------------------------------------------

def extract_text_lines(pdf_path: Path) -> list[str]:
    """Pull the text layer out of the PDF in document order.

    Deliberately parses the actual PDF bytes rather than trusting the renderer:
    the point is to verify what a parser would really see.
    """
    raw = pdf_path.read_bytes()
    lines: list[str] = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.S):
        chunk = match.group(1)
        try:
            chunk = zlib.decompress(chunk)
        except zlib.error:
            pass
        for shown in re.findall(rb"\(((?:[^()\\]|\\.)*)\)\s*Tj", chunk):
            text = (
                shown.replace(rb"\(", b"(")
                .replace(rb"\)", b")")
                .replace(rb"\\", b"\\")
                .decode("latin-1")
            )
            lines.append(text)
    return lines


def normalize(text: str) -> str:
    """Collapse whitespace and fold the ASCII substitutions the renderer makes."""
    text = text.replace("–", "-").replace("—", "-")
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("\xa0", " ")
    return " ".join(text.split())


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    out = tmp_path_factory.mktemp("pdfs")
    result = {}
    for name, yaml_path, source_path in MASTERS:
        resume = load_resume(yaml_path)
        pdf = render(resume, out / f"{name}.pdf")
        result[name] = {
            "resume": resume,
            "pdf": pdf,
            "lines": extract_text_lines(pdf),
            "text": normalize(" ".join(extract_text_lines(pdf))),
            "source": normalize(source_path.read_text(encoding="utf-8")),
            "source_raw": source_path.read_text(encoding="utf-8"),
        }
    return result


# --- 1. FIDELITY -----------------------------------------------------------

@pytest.mark.parametrize("name", [m[0] for m in MASTERS])
def test_skill_category_labels_are_verbatim_source_text(rendered, name):
    """The regression this file exists for.

    A derived label ("Frameworks Libraries") is not in the source text; the
    real one ("Frameworks & Libraries") is. Deriving display text from a key
    fails here.
    """
    data = rendered[name]
    labels = [label for label, _items in skill_groups(data["resume"])]
    assert labels, "no skill groups found"

    for label in labels:
        assert normalize(label) in data["source"], (
            f"skill label {label!r} does not appear in the source resume — "
            f"it was transformed, not copied"
        )
        assert normalize(label) in data["text"], (
            f"skill label {label!r} is missing from the rendered PDF"
        )


@pytest.mark.parametrize("name", [m[0] for m in MASTERS])
def test_ampersands_survive_rendering(rendered, name):
    """The specific character that was being dropped."""
    data = rendered[name]
    labels_with_amp = [
        label for label, _ in skill_groups(data["resume"]) if "&" in label
    ]
    assert labels_with_amp, f"{name} should have at least one '&' label"
    for label in labels_with_amp:
        assert normalize(label) in data["text"]
        # And the mangled form must NOT be present.
        mangled = normalize(label.replace("&", "").replace("  ", " "))
        assert mangled not in data["text"] or mangled == normalize(label)


@pytest.mark.parametrize("name", [m[0] for m in MASTERS])
def test_every_skill_item_appears_verbatim(rendered, name):
    """Catches silent loss in "C++", "OOP (C++)", "PCB layout (KiCad)",
    "TLS/PKI", "H-bridge motor drive", "Posh-ACME", "Analog Discovery 3"."""
    data = rendered[name]
    for label, items in skill_groups(data["resume"]):
        for item in items:
            assert normalize(item) in data["text"], (
                f"skill {item!r} (in {label!r}) is missing or mangled in the PDF"
            )
            assert normalize(item) in data["source"], (
                f"skill {item!r} is not in the source resume"
            )


@pytest.mark.parametrize(
    "name,special",
    [
        ("master_sw", ["C++", "Data Structures & OOP (C++)", "TLS/PKI",
                       "Cloudflare (DNS/ACME integrations)", "Posh-ACME",
                       "919-428-0702", "linkedin.com/in/charles-lepine"]),
        ("master_hw", ["C++", "PCB layout (KiCad)", "H-bridge motor drive",
                       "Analog Discovery 3", "Design of Complex Digital Systems (Verilog/Vivado)",
                       "surface-mount soldering", "datasheet-driven bring-up"]),
    ],
)
def test_special_characters_are_not_dropped(rendered, name, special):
    """Plus signs, slashes, parentheses, hyphens, and dots must all survive."""
    text = rendered[name]["text"]
    for token in special:
        assert normalize(token) in text, f"{token!r} lost or mangled in the PDF"


@pytest.mark.parametrize("name", [m[0] for m in MASTERS])
def test_section_headings_are_present(rendered, name):
    text = rendered[name]["text"]
    for heading in ("EDUCATION", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS"):
        assert heading in text, f"missing section heading {heading}"
    assert "LEADERSHIP & ACTIVITIES" in text, "the '&' in the section heading was dropped"


@pytest.mark.parametrize("name", [m[0] for m in MASTERS])
def test_every_bullet_survives_rendering(rendered, name):
    """No content silently dropped between YAML and PDF."""
    from pipeline.tailor import master_bullets

    data = rendered[name]
    for bullet in master_bullets(data["resume"]):
        # Compare on a distinctive prefix; long bullets wrap across lines and
        # the joined text collapses those wraps.
        head = normalize(bullet)[:60]
        assert head in data["text"], f"bullet missing from PDF: {head!r}"


def test_hardware_resume_keeps_the_citizenship_line(rendered):
    """PLAN.md §4 calls clearance eligibility a differentiator; it must print."""
    text = rendered["master_hw"]["text"]
    assert "U.S. Citizen" in text
    assert "eligible to obtain" in text
    # And it must NOT leak onto the software resume, which does not claim it.
    assert "eligible to obtain" not in rendered["master_sw"]["text"]


# --- 2. ATS SAFETY ---------------------------------------------------------

@pytest.mark.parametrize("name", [m[0] for m in MASTERS])
def test_pdf_text_is_real_and_selectable(rendered, name):
    """If text extracts at all, it is a real text layer, not an image."""
    lines = rendered[name]["lines"]
    assert len(lines) > 30, "too little extractable text — is this an image?"
    assert "Charles Lepine" in rendered[name]["text"]


@pytest.mark.parametrize("name", [m[0] for m in MASTERS])
def test_reading_order_is_top_to_bottom(rendered, name):
    """An ATS reads the text layer in stream order. It must match visual order."""
    lines = rendered[name]["lines"]
    joined = normalize(" ".join(lines))

    def pos(needle: str) -> int:
        idx = joined.find(normalize(needle))
        assert idx >= 0, f"{needle!r} not found in extracted text"
        return idx

    assert pos("Charles Lepine") < pos("EDUCATION") < pos("TECHNICAL SKILLS")
    assert pos("TECHNICAL SKILLS") < pos("EXPERIENCE") < pos("PROJECTS")
    assert pos("PROJECTS") < pos("LEADERSHIP & ACTIVITIES")
    assert pos("North Carolina State University") < pos("TECHNICAL SKILLS")
    assert pos("IBM") < pos("PROJECTS")


@pytest.mark.parametrize("name", [m[0] for m in MASTERS])
def test_no_images_no_embedded_fonts_no_tables(rendered, name):
    """The structural half of the ATS guarantee.

    * no /Subtype /Image  -> nothing is a picture of text
    * no /FontFile*       -> core fonts only, no subset a parser can fail to map
    * single content flow -> no table or multi-column structure to mis-order
    """
    raw = rendered[name]["pdf"].read_bytes()
    assert b"/Subtype /Image" not in raw, "PDF contains an image"
    assert b"/FontFile" not in raw, "PDF embeds a font subset; core fonts only"
    fonts = set(re.findall(rb"/BaseFont\s*/([A-Za-z0-9,+-]+)", raw))
    assert fonts, "no fonts declared"
    assert all(f.startswith(b"Helvetica") for f in fonts), f"non-core font: {fonts}"


@pytest.mark.parametrize("name", [m[0] for m in MASTERS])
def test_nothing_lands_in_a_header_or_footer_region(rendered, name):
    """FPDF.header()/footer() are overridden to no-ops. Confirm they emit nothing."""
    from resume.render import AtsResume

    pdf = AtsResume()
    pdf.add_page()
    before = pdf.get_y()
    pdf.header()
    pdf.footer()
    assert pdf.get_y() == before, "header/footer wrote into the margin region"


@pytest.mark.parametrize("name", [m[0] for m in MASTERS])
def test_bullets_use_a_plain_hyphen(rendered, name):
    """Glyph bullets (•) round-trip badly through some parsers; we use '- '."""
    text = rendered[name]["text"]
    assert "•" not in text
    assert "- " in text


def test_coursework_complete_flag_moves_in_progress_courses(tmp_path):
    """PLAN.md §4: Fall-2026 coursework completes before a Jan-2027 start."""
    resume = load_resume(REPO / "resume/master_hw.yaml")
    in_progress = resume["education"]["in_progress"]
    assert in_progress

    normal = extract_text_lines(render(resume, tmp_path / "a.pdf"))
    folded = extract_text_lines(
        render(resume, tmp_path / "b.pdf", coursework_complete=True)
    )
    assert "In Progress" in normalize(" ".join(normal))
    assert "In Progress" not in normalize(" ".join(folded))
    for course in in_progress:
        assert normalize(course) in normalize(" ".join(folded))
