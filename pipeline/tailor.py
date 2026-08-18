"""On-demand resume tailoring (PLAN.md §2 stage 7).

Invoked per job, by hand, for a job the owner has decided to apply to:

    python -m pipeline.tailor --job-id <id>

NOT run for all ten daily matches. Ten tailoring calls every weekday would be
the single largest line item in the run, and most days the owner applies to one
or two. The daily email ships the free local keyword diff (pipeline/keywords.py)
instead; this stage runs only when it will actually be used.

THE HONESTY RULE IS ENFORCED IN CODE, NOT IN THE PROMPT.

PLAN.md says the tailored resume may only reorder, rephrase, or promote content
already in the master. A prompt asking for that is a request; validate_tailored()
is a guarantee. The model is structurally prevented from adding content:

  * It cannot emit new bullets. It emits REWRITES keyed to exact master bullets,
    and anything that does not match a master bullet is discarded.
  * It cannot emit new skills. It emits an ORDER over existing skills, plus
    renames whose source must exist in the master AND whose target must appear
    verbatim in the job description (that is the PLAN.md carve-out: renaming a
    true skill to the JD's phrasing, Verilog -> "RTL design").
  * Every rewritten string is scanned for technology terms. Any term not
    traceable to the master resume fails the run.

A resume claiming a skill he does not have is a bug that surfaces in a technical
screen, so the failure mode here is "refuse to write the PDF", never "write it
and warn".
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Optional

from . import db, keywords
from .config import load_config, repo_path
from .jd import compress_jd
from .llm import LlmClient
from .models import Job
from .resumes import set_skill_items, skill_groups, skill_items

# ---------------------------------------------------------------------------
# Honesty validator
# ---------------------------------------------------------------------------

# Tokens that look like a technology identifier rather than ordinary English.
_TECHY = re.compile(
    r"\b("
    r"[A-Za-z]+[0-9]+[A-Za-z0-9_+#.-]*"        # MSP430, 802.11, C99, S3
    r"|[A-Z]{2,}(?:[0-9]+)?"                    # ASIC, FPGA, RTOS, I2C
    r"|[A-Za-z]+\+\+"                           # C++
    r"|[A-Za-z]+#"                              # C#
    r"|[A-Za-z]+\.(?:js|py|net|io|ai)"          # Node.js
    r")\b"
)

# Techy-looking tokens that are ordinary English or resume furniture.
_TECHY_ALLOWLIST = {
    "I", "II", "III", "IV", "V", "VI", "AI", "ML", "US", "USA", "U", "S",
    "PII", "FAQ", "OOP", "BS", "MS", "PHD", "CCS", "UPSL", "FIFA", "NC",
    "AND", "OR", "THE", "A", "AN", "IT", "TO", "IN", "ON", "OF", "BY",
    "R", "D", "RD", "QA", "PM", "HR", "CEO", "CTO", "VP", "LLC", "INC",
    "NCSU", "GPA", "TBD", "N", "IE", "EG", "ETC",
}


def _blob(node: Any, parts: Optional[list[str]] = None) -> str:
    """Flatten any nested structure to one lowercase text blob."""
    if parts is None:
        parts = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            for v in n.values():
                walk(v)
        elif isinstance(n, (list, tuple)):
            for v in n:
                walk(v)
        elif n is not None:
            parts.append(str(n))

    walk(node)
    return " ".join(parts).lower()


def master_skill_items(resume: dict) -> set[str]:
    return {str(item).strip().lower() for item in skill_items(resume)}


def master_bullets(resume: dict) -> list[str]:
    """Every bullet string in the master, in document order."""
    bullets: list[str] = []
    for entry in resume.get("experience") or []:
        for sub in entry.get("subsections") or []:
            bullets.extend(str(b) for b in (sub.get("bullets") or []))
        bullets.extend(str(b) for b in (entry.get("bullets") or []))
    for project in resume.get("projects") or []:
        bullets.extend(str(b) for b in (project.get("bullets") or []))
    for entry in resume.get("leadership") or []:
        bullets.extend(str(b) for b in (entry.get("bullets") or []))
    return bullets


def _normalize(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+#.]+", " ", (text or "").lower()).split())


@dataclass
class Violation:
    kind: str          # unknown_skill | unknown_term | unmatched_bullet | bad_rename
    detail: str
    context: str = ""

    def __str__(self) -> str:
        ctx = f" — in: {self.context[:110]}" if self.context else ""
        return f"[{self.kind}] {self.detail}{ctx}"


def find_new_terms(text: str, master_blob: str) -> list[str]:
    """Technology terms in `text` that are not traceable to the master resume."""
    new: list[str] = []

    # 1. Curated vocabulary — alias-aware, catches "Kubernetes", "PyTorch", "SPI".
    for term in keywords.extract_terms(text):
        if not keywords._present(master_blob, term):  # noqa: SLF001 - same package
            new.append(term)

    # 2. Anything that merely looks like a technology identifier.
    for match in _TECHY.finditer(text):
        token = match.group(0)
        if token.upper() in _TECHY_ALLOWLIST:
            continue
        if token.lower() in master_blob:
            continue
        new.append(token)

    seen: set[str] = set()
    out: list[str] = []
    for term in new:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            out.append(term)
    return out


def validate_tailored(
    tailored: dict, master: dict, jd_text: str = ""
) -> list[Violation]:
    """Return every honesty violation. An empty list means the output is safe.

    Checked:
      * every rewritten bullet maps to a real master bullet
      * no rewritten bullet introduces an untraceable technology term
      * every skill listed exists in the master (or is a validated rename)
      * every rename's source is a real master skill and its target appears in
        the job description
    """
    violations: list[Violation] = []
    master_blob = _blob(master)
    master_bullet_norms = {_normalize(b): b for b in master_bullets(master)}
    master_skills = master_skill_items(master)   # not `skill_items` — that name is the imported helper
    jd_lower = (jd_text or "").lower()

    # --- renames --------------------------------------------------------
    renames: dict[str, str] = {}
    for rename in tailored.get("skill_renames") or []:
        src = str(rename.get("from", "")).strip()
        dst = str(rename.get("to", "")).strip()
        if not src or not dst:
            continue
        if src.lower() not in master_skills:
            violations.append(
                Violation("bad_rename", f"renames {src!r}, which is not a master skill")
            )
            continue
        if jd_lower and dst.lower() not in jd_lower:
            violations.append(
                Violation(
                    "bad_rename",
                    f"renames {src!r} to {dst!r}, but {dst!r} does not appear in "
                    f"the job description — that is invention, not rephrasing",
                )
            )
            continue
        renames[src.lower()] = dst

    allowed_skill_strings = master_skills | {v.lower() for v in renames.values()}

    # --- skills ---------------------------------------------------------
    for group, items in (tailored.get("skills") or {}).items():
        for item in items if isinstance(items, list) else [items]:
            text = str(item).strip()
            if text.lower() in allowed_skill_strings:
                continue
            # Allow a pure reordering/regrouping of master items inside a
            # comma-joined string, but nothing new.
            pieces = [p.strip() for p in re.split(r"[,;/]", text) if p.strip()]
            unknown = [p for p in pieces if p.lower() not in allowed_skill_strings]
            if unknown:
                violations.append(
                    Violation(
                        "unknown_skill",
                        f"skill {unknown[0]!r} is not in the master resume",
                        context=f"{group}: {text}",
                    )
                )

    # --- bullet rewrites ------------------------------------------------
    for rewrite in tailored.get("bullet_rewrites") or []:
        original = str(rewrite.get("original", ""))
        rewritten = str(rewrite.get("rewritten", ""))
        if _normalize(original) not in master_bullet_norms:
            violations.append(
                Violation(
                    "unmatched_bullet",
                    "rewrite does not correspond to any master bullet",
                    context=original,
                )
            )
            continue
        for term in find_new_terms(rewritten, master_blob):
            if term.lower() in {v.lower() for v in renames.values()}:
                continue
            violations.append(
                Violation(
                    "unknown_term",
                    f"introduces {term!r}, which is not in the master resume",
                    context=rewritten,
                )
            )

    return violations


# ---------------------------------------------------------------------------
# Applying an accepted tailoring to the master
# ---------------------------------------------------------------------------

def apply_tailoring(master: dict, tailored: dict) -> dict:
    """Produce the tailored resume dict.

    Structurally additive-proof: this only ever REPLACES existing strings and
    REORDERS existing lists. There is no code path here that appends a bullet,
    a skill, an experience entry, or a project.
    """
    result = copy.deepcopy(master)

    rewrites = {
        _normalize(str(r.get("original", ""))): str(r.get("rewritten", ""))
        for r in (tailored.get("bullet_rewrites") or [])
        if str(r.get("rewritten", "")).strip()
    }
    renames = {
        str(r.get("from", "")).strip().lower(): str(r.get("to", "")).strip()
        for r in (tailored.get("skill_renames") or [])
        if str(r.get("from", "")).strip() and str(r.get("to", "")).strip()
    }

    def swap(bullets: Iterable[str]) -> list[str]:
        out = []
        for bullet in bullets or []:
            out.append(rewrites.get(_normalize(str(bullet)), str(bullet)))
        return out

    for entry in result.get("experience") or []:
        for sub in entry.get("subsections") or []:
            sub["bullets"] = swap(sub.get("bullets"))
        if entry.get("bullets"):
            entry["bullets"] = swap(entry["bullets"])
    for project in result.get("projects") or []:
        project["bullets"] = swap(project.get("bullets"))
    for entry in result.get("leadership") or []:
        entry["bullets"] = swap(entry.get("bullets"))

    # Skills: apply renames, then reorder within each group per skill_order.
    # Category LABELS are never touched — they are verbatim source text.
    order = [str(s).strip().lower() for s in (tailored.get("skill_order") or [])]
    for label, items in skill_groups(result):
        values = [renames.get(str(v).lower(), str(v)) for v in items]
        if order:
            def rank(value: str) -> int:
                low = value.lower()
                for idx, wanted in enumerate(order):
                    if wanted == low or wanted in low or low in wanted:
                        return idx
                return len(order) + 1
            values.sort(key=rank)
        set_skill_items(result, label, values)

    # Project/experience ordering: promote entries the model flagged, never add.
    promote = [str(p).strip().lower() for p in (tailored.get("promote_projects") or [])]
    if promote and result.get("projects"):
        def project_rank(project: dict) -> int:
            title = str(project.get("title", "")).lower()
            for idx, wanted in enumerate(promote):
                if wanted in title or title in wanted:
                    return idx
            return len(promote) + 1
        result["projects"].sort(key=project_rank)

    return result


# ---------------------------------------------------------------------------
# LLM stage
# ---------------------------------------------------------------------------

SYSTEM_TEMPLATE = """You tailor one candidate's resume to one job posting.

ABSOLUTE RULE: you may only reorder, rephrase, or promote content that is
already in the master resume below. You may NOT introduce a skill, tool,
technology, framework, or accomplishment that does not appear there. Output
that adds anything is rejected automatically and the run fails — so if the job
wants something he does not have, leave it out. That is the correct answer.

MASTER RESUME ({track})
{resume_yaml}

WHAT YOU MAY DO
1. bullet_rewrites — rephrase existing bullets toward the posting's language.
   `original` must be the master bullet text COPIED EXACTLY (it is matched
   against the master; a paraphrase there is discarded). `rewritten` must keep
   every fact, number, and claim of the original. Rewrite at most 6 bullets;
   leave the rest alone.
2. skill_renames — rename a skill he genuinely has to the posting's phrasing,
   e.g. Verilog -> "RTL design", continuous integration -> "CI/CD". `from` must
   be an exact master skill. `to` MUST be a phrase that literally appears in the
   job description.
3. skill_order — the skills most relevant to this posting, most relevant first.
   Every entry must be an existing master skill.
4. promote_projects — project titles to move up, most relevant first. Titles
   must match master projects.
5. summary — one sentence, under 30 words, on what you emphasized and why.

Do not invent. Do not inflate. Do not add a bullet."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "bullet_rewrites": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "original": {"type": "string"},
                    "rewritten": {"type": "string"},
                },
                "required": ["original", "rewritten"],
                "additionalProperties": False,
            },
        },
        "skill_renames": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                },
                "required": ["from", "to"],
                "additionalProperties": False,
            },
        },
        "skill_order": {"type": "array", "items": {"type": "string"}},
        "promote_projects": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "bullet_rewrites", "skill_renames", "skill_order"],
    "additionalProperties": False,
}


class TailoringRejected(RuntimeError):
    """The model produced output that would have added content to the resume."""


@dataclass
class TailorResult:
    resume: dict
    summary: str = ""
    violations: list[Violation] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    pdf_path: Optional[Path] = None


def tailor_for_job(
    llm: LlmClient,
    job: Job,
    master: dict,
    cfg: dict,
    *,
    strict: bool = True,
) -> TailorResult:
    """Tailor `master` toward `job`. Raises TailoringRejected on any violation."""
    jd = compress_jd(job.description, int(cfg["limits"].get("jd_max_chars", 1600)))

    import yaml  # local import: keeps module import cheap for tests

    system_cached = SYSTEM_TEMPLATE.format(
        track=job.track,
        resume_yaml=yaml.safe_dump(master, sort_keys=False, width=100),
    )
    user_content = json.dumps(
        {
            "company": job.company,
            "title": job.title,
            "location": job.location,
            "job_description": jd,
        },
        ensure_ascii=False,
    )

    data, _usage = llm.complete_json(
        system_cached=system_cached,
        user_content=user_content,
        schema=SCHEMA,
        max_tokens=int(cfg["model"].get("max_tokens_tailor", 2500)),
        label=f"tailor:{job.id}",
    )

    violations = validate_tailored(data, master, jd)
    if violations and strict:
        raise TailoringRejected(
            "Tailored output was rejected because it would have added content "
            "not present in the master resume:\n  "
            + "\n  ".join(str(v) for v in violations)
        )

    return TailorResult(
        resume=apply_tailoring(master, data),
        summary=str(data.get("summary", "")),
        violations=violations,
        raw=data,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:40] or "job"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tailor a master resume to one saved job and render an ATS-safe PDF."
    )
    parser.add_argument("--job-id", required=True, help="job id from the daily email")
    parser.add_argument("--out", help="output PDF path (default: out/YYYY-MM-DD/...)")
    parser.add_argument(
        "--coursework-complete",
        action="store_true",
        help="fold Fall-2026 in-progress courses into completed coursework",
    )
    parser.add_argument(
        "--allow-violations",
        action="store_true",
        help="DANGEROUS: render even if the honesty validator flags additions",
    )
    parser.add_argument("--dry-run", action="store_true", help="do not call the API; print what would be sent")
    args = parser.parse_args(argv)

    cfg = load_config()
    sys.path.insert(0, str(repo_path()))
    from resume.render import load_resume, render  # noqa: PLC0415

    with db.connect(repo_path(cfg["paths"]["db"])) as conn:
        job = db.get(conn, args.job_id)

    if job is None:
        print(f"error: no job with id {args.job_id!r} in the database", file=sys.stderr)
        return 2

    master_path = (
        cfg["paths"]["resume_hw"] if job.track == "hardware" else cfg["paths"]["resume_sw"]
    )
    master = load_resume(repo_path(master_path))

    print(f"Job   : {job.company} — {job.title}")
    print(f"Track : {job.track}  (master: {master_path})")
    print(f"Link  : {job.url}")

    if args.dry_run:
        jd = compress_jd(job.description, int(cfg["limits"]["jd_max_chars"]))
        print(f"\n--dry-run: no API call made.")
        print(f"compressed JD: {len(jd)} chars (from {len(job.description)})")
        print(f"master bullets available for rewrite: {len(master_bullets(master))}")
        diff = keywords.diff(jd, master)
        print(f"local keyword diff: {len(diff.matched)} matched / {len(diff.missing)} missing")
        return 0

    llm = LlmClient(cfg, run_id=f"tailor-{job.id}")
    try:
        result = tailor_for_job(llm, job, master, cfg, strict=not args.allow_violations)
    except TailoringRejected as exc:
        print(f"\nREJECTED — no PDF written.\n{exc}", file=sys.stderr)
        return 3

    out_dir = repo_path(cfg["paths"]["out_dir"], date.today().isoformat())
    out_path = (
        Path(args.out)
        if args.out
        else out_dir / f"{_safe_name(job.company)}-{_safe_name(job.title)}.pdf"
    )
    render(result.resume, out_path, coursework_complete=args.coursework_complete)

    print(f"\nSummary : {result.summary}")
    if result.violations:
        print(f"WARNING : {len(result.violations)} violation(s) allowed via --allow-violations")
        for v in result.violations:
            print(f"          {v}")
    print(f"PDF     : {out_path}")
    print(f"Cost    : ${llm.ledger.spent_usd:.4f} ({llm.ledger.calls} call)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
