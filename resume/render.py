"""YAML -> ATS-safe PDF (PLAN.md §2 stage 8 / §6).

Hard constraints, all enforced structurally rather than by convention:

  * NO tables          — every line is a full-width text flow
  * NO multi-column    — single text column, one x-origin
  * NO text boxes      — nothing but cell()/multi_cell() in document order
  * NO header/footer   — FPDF.header()/footer() are explicitly overridden to
                         no-ops, so nothing lands in the margin regions where
                         ATS parsers commonly drop content
  * Standard fonts     — Helvetica (a PDF core font), never an embedded
                         subset that a parser may fail to map
  * Real selectable text — no images, no vector text, no outlines

The output is deliberately plain. An ATS parser reads it top-to-bottom in the
same order a human does, which is the whole point.
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path
from typing import Any

import yaml
from fpdf import FPDF

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.resumes import skill_groups  # noqa: E402

# Core PDF fonts are Latin-1. Rather than embed a Unicode TTF (which some ATS
# parsers handle badly), fold the handful of typographic characters that appear
# in the source resumes down to their ASCII equivalents.
_TRANSLATE = {
    0x2013: "-", 0x2014: "-", 0x2012: "-", 0x2212: "-",   # dashes
    0x2018: "'", 0x2019: "'", 0x201B: "'",                 # single quotes
    0x201C: '"', 0x201D: '"',                              # double quotes
    0x2026: "...", 0x2022: "-", 0x00B7: "-",               # ellipsis, bullets
    0x00A0: " ", 0x2009: " ", 0x202F: " ", 0x200B: "",     # spaces
    0x2122: "(TM)", 0x00AE: "(R)", 0x00A9: "(C)",
}

PAGE_WIDTH_MM = 215.9   # US Letter
MARGIN_MM = 14.0
CONTENT_WIDTH = PAGE_WIDTH_MM - 2 * MARGIN_MM


def ascii_safe(text: Any) -> str:
    """Latin-1-safe plain text. Never silently drops a character."""
    s = str(text if text is not None else "")
    s = s.translate(_TRANSLATE)
    s = unicodedata.normalize("NFKD", s)
    return s.encode("latin-1", "replace").decode("latin-1")


class AtsResume(FPDF):
    """FPDF with the header/footer regions deliberately disabled."""

    def header(self) -> None:  # noqa: D102 - intentionally empty
        return

    def footer(self) -> None:  # noqa: D102 - intentionally empty
        return


def _line(pdf: AtsResume, text: str, size: float = 9.0, style: str = "", gap: float = 0.35) -> None:
    pdf.set_font("Helvetica", style, size)
    pdf.multi_cell(CONTENT_WIDTH, size * 0.415, ascii_safe(text), align="L")
    pdf.ln(gap)


def _section(pdf: AtsResume, title: str) -> None:
    pdf.ln(1.1)
    pdf.set_font("Helvetica", "B", 10.5)
    pdf.multi_cell(CONTENT_WIDTH, 4.2, ascii_safe(title.upper()), align="L")
    y = pdf.get_y()
    pdf.set_line_width(0.3)
    pdf.line(MARGIN_MM, y, PAGE_WIDTH_MM - MARGIN_MM, y)
    pdf.ln(0.7)


def _bullet(pdf: AtsResume, text: str) -> None:
    # A literal hyphen, not a glyph bullet — parsers handle "- " reliably.
    _line(pdf, f"- {text}", size=8.9, gap=0.2)


def _joined(items: Any) -> str:
    if isinstance(items, list):
        return ", ".join(str(i) for i in items)
    return str(items or "")


def render(resume: dict, out_path: str | Path, *, coursework_complete: bool = False) -> Path:
    """Render one resume dict to an ATS-safe PDF.

    coursework_complete=True folds the Fall-2026 in-progress courses into the
    completed list — correct for Jan-2027 start dates (PLAN.md §4), and never
    applied silently: run.py / tailor.py pass it explicitly.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    pdf = AtsResume(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(auto=True, margin=MARGIN_MM)
    pdf.set_margins(MARGIN_MM, MARGIN_MM, MARGIN_MM)
    pdf.set_title(f"{(resume.get('contact') or {}).get('name','Resume')} - Resume")
    pdf.set_creator("job-agent")
    pdf.add_page()

    contact = resume.get("contact") or {}

    # --- header block (in the body flow, NOT the page header region) ---
    pdf.set_font("Helvetica", "B", 15)
    pdf.multi_cell(CONTENT_WIDTH, 6.2, ascii_safe(contact.get("name", "")), align="L")
    pdf.ln(0.4)
    bits = [
        contact.get("phone"),
        contact.get("email"),
        contact.get("location"),
        contact.get("linkedin"),
    ]
    _line(pdf, "  |  ".join(b for b in bits if b), size=9, gap=0.3)
    if contact.get("citizenship"):
        _line(pdf, contact["citizenship"], size=9, style="B", gap=0.3)

    # --- education ---
    edu = resume.get("education") or {}
    if edu:
        _section(pdf, "Education")
        _line(pdf, f"{edu.get('school','')} | {edu.get('school_location','')}", size=9.6, style="B", gap=0.2)
        _line(pdf, f"{edu.get('degree','')}  |  {edu.get('standing','')}  |  {edu.get('dates','')}", size=8.9, gap=0.4)

        coursework = list(edu.get("coursework") or [])
        in_progress = list(edu.get("in_progress") or [])
        if coursework_complete and in_progress:
            coursework = coursework + in_progress
            in_progress = []
        if coursework:
            _line(pdf, f"Relevant Coursework: {_joined(coursework)}", size=8.9)
        if in_progress:
            term = edu.get("in_progress_term", "In Progress")
            _line(pdf, f"In Progress ({term}): {_joined(in_progress)}", size=8.9)

    # --- skills ---
    # Labels are printed VERBATIM from the YAML, never derived from a key.
    groups = skill_groups(resume)
    if groups:
        _section(pdf, "Technical Skills")
        for label, items in groups:
            _line(pdf, f"{label}: {_joined(items)}", size=8.9, gap=0.35)

    # --- experience ---
    experience = resume.get("experience") or []
    if experience:
        _section(pdf, "Experience")
        for entry in experience:
            head = f"{entry.get('company','')} | {entry.get('title','')}"
            if entry.get("location"):
                head += f" - {entry['location']}"
            _line(pdf, head, size=9.6, style="B", gap=0.15)
            if entry.get("dates"):
                _line(pdf, entry["dates"], size=9, style="I", gap=0.35)
            for sub in entry.get("subsections") or []:
                if sub.get("name"):
                    _line(pdf, sub["name"], size=9.5, style="B", gap=0.25)
                for bullet in sub.get("bullets") or []:
                    _bullet(pdf, bullet)
            for bullet in entry.get("bullets") or []:
                _bullet(pdf, bullet)
            pdf.ln(0.7)

    # --- projects ---
    projects = resume.get("projects") or []
    if projects:
        _section(pdf, "Projects")
        for project in projects:
            head = project.get("title", "")
            if project.get("stack"):
                head += f" - {project['stack']}"
            _line(pdf, head, size=9.6, style="B", gap=0.15)
            meta = "  |  ".join(
                str(x) for x in (project.get("context"), project.get("dates")) if x
            )
            if meta:
                _line(pdf, meta, size=9, style="I", gap=0.35)
            for bullet in project.get("bullets") or []:
                _bullet(pdf, bullet)
            pdf.ln(0.7)

    # --- leadership ---
    leadership = resume.get("leadership") or []
    if leadership:
        _section(pdf, "Leadership & Activities")
        for entry in leadership:
            head = entry.get("title", "")
            if entry.get("dates"):
                head += f"  |  {entry['dates']}"
            _line(pdf, head, size=9.5, style="B", gap=0.15)
            for bullet in entry.get("bullets") or []:
                _bullet(pdf, bullet)

    pdf.output(str(out_path))
    return out_path


def load_resume(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a master resume YAML to an ATS-safe PDF.")
    parser.add_argument("yaml_path", nargs="?", help="path to a master resume YAML")
    parser.add_argument("-o", "--out", help="output PDF path")
    parser.add_argument(
        "--coursework-complete",
        action="store_true",
        help="fold Fall-2026 in-progress courses into completed coursework "
             "(correct for Jan-2027 start dates)",
    )
    parser.add_argument("--all", action="store_true", help="render both masters into out/")
    args = parser.parse_args(argv)

    here = Path(__file__).resolve().parent
    if args.all or not args.yaml_path:
        outputs = []
        for name in ("master_sw", "master_hw"):
            src = here / f"{name}.yaml"
            dst = here.parent / "out" / f"{name}.pdf"
            outputs.append(render(load_resume(src), dst, coursework_complete=args.coursework_complete))
        for path in outputs:
            print(f"wrote {path} ({path.stat().st_size} bytes)")
        return 0

    src = Path(args.yaml_path)
    dst = Path(args.out) if args.out else src.with_suffix(".pdf")
    path = render(load_resume(src), dst, coursework_complete=args.coursework_complete)
    print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
