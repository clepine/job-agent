"""HTML email render + Gmail SMTP send (PLAN.md §2 stage 9).

Uses SMTP with an app password rather than the Gmail API: sending only needs
one credential and no OAuth dance, no credentials.json, and no token refresh in
CI. (Gmail API is still required for the v2 LinkedIn-alert *reading* feature —
that is a different scope and a different problem.)

--dry-run writes the HTML to a file and sends nothing, so the whole pipeline is
exercisable with no credentials at all.
"""

from __future__ import annotations

import html
import smtplib
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional, Sequence

from . import keywords, resume_pick
from .config import require_env
from .jd import compress_jd
from .models import Job
from .pick import Selection

# --- styling -----------------------------------------------------------------
# Inline styles only. Gmail strips <style> blocks in many clients.

_WRAP = "max-width:680px;margin:0 auto;padding:16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:#1a1a1a;line-height:1.5;"
_CARD = "border:1px solid #e2e2e2;border-radius:8px;padding:14px 16px;margin:0 0 12px 0;background:#ffffff;"
_TITLE = "font-size:16px;font-weight:600;margin:0 0 2px 0;color:#111;"
_META = "font-size:13px;color:#666;margin:0 0 8px 0;"
_RATIONALE = "font-size:14px;margin:0 0 10px 0;color:#222;"
_KWLABEL = "font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#888;margin:0 0 3px 0;"
_CHIP_OK = "display:inline-block;background:#e8f5e9;color:#1b5e20;border-radius:10px;padding:2px 8px;margin:0 4px 4px 0;font-size:12px;"
_CHIP_GAP = "display:inline-block;background:#fff3e0;color:#7a4100;border-radius:10px;padding:2px 8px;margin:0 4px 4px 0;font-size:12px;"
_BTN = "display:inline-block;background:#1a73e8;color:#ffffff !important;text-decoration:none;border-radius:6px;padding:8px 16px;font-size:14px;font-weight:600;"
_RESUME = "font-size:12px;margin:0 0 8px 0;padding:5px 9px;border-radius:4px;background:#eef4ff;color:#1a3d7c;border-left:3px solid #4a7fd4;"
_RESUME_FLIP = "font-size:12px;margin:0 0 8px 0;padding:5px 9px;border-radius:4px;background:#fff3e6;color:#7a3d00;border-left:3px solid #e08a1e;"
_NOTE = "background:#fffbe6;border-left:3px solid #f0c419;padding:10px 12px;margin:0 0 14px 0;font-size:13px;color:#4a3b00;"
_H2 = "font-size:18px;font-weight:700;margin:22px 0 10px 0;padding-bottom:6px;border-bottom:2px solid #1a1a1a;"


def _e(text: object) -> str:
    return html.escape(str(text if text is not None else ""))


@dataclass
class RenderedEmail:
    subject: str
    html: str
    text: str
    job_ids: list[str]


def _tier_label(job: Job) -> str:
    return "Tier 1" if job.tier == 1 else "Tier 2"


def _job_card(
    job: Job, resume: dict, resume_sw: dict, resume_hw: dict, jd_max_chars: int
) -> str:
    jd = compress_jd(job.description, jd_max_chars)
    diff = keywords.diff(jd, resume)
    matched, missing = keywords.summarize(diff)
    rec = resume_pick.recommend(jd, resume_sw, resume_hw)

    meta_bits = [job.location or "location not stated", job.age_label(), _tier_label(job)]
    if job.metro:
        meta_bits.insert(1, job.metro)
    if job.clearance_advantage:
        meta_bits.append("clearance-eligible advantage")

    rationale = job.fit_rationale or "No rationale recorded."
    score = f"{job.fit_score}/100" if job.fit_score is not None else "unscored"

    kw_html = ""
    if matched or missing:
        kw_html += f'<p style="{_KWLABEL}">ATS keywords &mdash; you already show ({len(diff.matched)})</p><p style="margin:0 0 8px 0;">'
        kw_html += "".join(f'<span style="{_CHIP_OK}">{_e(t)}</span>' for t in matched) or '<span style="font-size:12px;color:#888;">none detected</span>'
        kw_html += "</p>"
        if missing:
            kw_html += f'<p style="{_KWLABEL}">Asked for, not on your resume ({len(diff.missing)})</p><p style="margin:0 0 8px 0;">'
            kw_html += "".join(f'<span style="{_CHIP_GAP}">{_e(t)}</span>' for t in missing)
            kw_html += "</p>"
            surfaceable = {t: diff.surfacing[t] for t in missing if t in diff.surfacing}
            if surfaceable:
                items = "".join(
                    f"<li style='margin:0 0 3px 0;'><b>{_e(t)}</b> &mdash; {_e(where)}</li>"
                    for t, where in list(surfaceable.items())[:4]
                )
                kw_html += (
                    f'<p style="{_KWLABEL}">Truthfully surfaceable from your master resume</p>'
                    f'<ul style="margin:0 0 8px 0;padding-left:18px;font-size:12px;color:#555;">{items}</ul>'
                )

    # The track already filed this posting under one resume; say so out loud, and
    # say plainly when the measurement points at the other one instead.
    if resume_pick.disagrees_with_track(rec, job.track):
        resume_html = (
            f'<p style="{_RESUME_FLIP}">{_e(rec.label())} '
            f'&mdash; filed under {_e(job.track)}, but the <b>{_e(rec.best)}</b> '
            f'resume covers this posting better. Worth a look before you send.</p>'
        )
    else:
        resume_html = f'<p style="{_RESUME}">{_e(rec.label())}</p>'

    return f"""
    <div style="{_CARD}">
      <p style="{_TITLE}">{_e(job.company)} &mdash; {_e(job.title)}</p>
      <p style="{_META}">{_e(" &middot; ".join(meta_bits)).replace("&amp;middot;", "&middot;")} &middot; fit {_e(score)}</p>
      {resume_html}
      <p style="{_RATIONALE}">{_e(rationale)}</p>
      {kw_html}
      <p style="margin:10px 0 0 0;"><a href="{_e(job.url)}" style="{_BTN}">Apply</a>
        <span style="font-size:11px;color:#999;margin-left:10px;">tailor: <code>python -m pipeline.tailor --job-id {_e(job.id)}</code></span>
      </p>
    </div>"""


def _track_section(
    title: str, sel: Selection, resume: dict, resume_sw: dict, resume_hw: dict,
    jd_max_chars: int,
) -> str:
    parts = [f'<h2 style="{_H2}">{_e(title)}</h2>']
    for note in sel.notes:
        parts.append(f'<div style="{_NOTE}">{_e(note)}</div>')
    if not sel.jobs:
        parts.append('<p style="color:#666;font-size:14px;">No matches cleared the filters today.</p>')
    for job in sel.jobs:
        parts.append(_job_card(job, resume, resume_sw, resume_hw, jd_max_chars))
    return "".join(parts)


def _plain_text(
    software: Selection,
    hardware: Selection,
    resume_sw: Optional[dict] = None,
    resume_hw: Optional[dict] = None,
    jd_max_chars: int = 1600,
) -> str:
    lines: list[str] = []
    for label, sel in (("SOFTWARE", software), ("HARDWARE", hardware)):
        lines.append(f"== {label} ==")
        for note in sel.notes:
            lines.append(f"  ! {note}")
        for job in sel.jobs:
            lines.append(f"  * {job.company} - {job.title}")
            lines.append(f"    {job.location} | {job.age_label()} | fit {job.fit_score}/100")
            if resume_sw is not None and resume_hw is not None:
                rec = resume_pick.recommend(
                    compress_jd(job.description, jd_max_chars), resume_sw, resume_hw
                )
                lines.append(f"    {rec.label()}")
            lines.append(f"    {job.fit_rationale}")
            lines.append(f"    {job.url}")
        if not sel.jobs:
            lines.append("  (nothing today)")
        lines.append("")
    return "\n".join(lines)


def render_email(
    software: Selection,
    hardware: Selection,
    resume_sw: dict,
    resume_hw: dict,
    cfg: dict,
    *,
    run_notes: Optional[Sequence[str]] = None,
    applied_total: int = 0,
) -> RenderedEmail:
    jd_max_chars = int(cfg["limits"].get("jd_max_chars", 1600))
    today = date.today().isoformat()
    n_sw, n_hw = len(software.jobs), len(hardware.jobs)

    subject = f"{cfg['email'].get('subject_prefix','Daily Job Matches')} — {n_sw} software, {n_hw} hardware ({today})"

    notes_html = "".join(f'<div style="{_NOTE}">{_e(n)}</div>' for n in (run_notes or []))

    body = f"""<div style="{_WRAP}">
      <p style="font-size:22px;font-weight:700;margin:0 0 2px 0;">Daily Job Matches</p>
      <p style="font-size:13px;color:#666;margin:0 0 16px 0;">
        {_e(today)} &middot; {n_sw} software &middot; {n_hw} hardware &middot;
        every posting is new to you and age-stamped with its true posting date
      </p>
      {f'<p style="font-size:12px;color:#777;margin:0 0 14px 0;">You have applied to <b>{applied_total}</b> role(s) so far. Record a new one with <code>python run.py --applied &lt;job-id&gt;</code> &mdash; applied roles are never shown again.</p>' if applied_total else ''}
      {notes_html}
      {_track_section("Software", software, resume_sw, resume_sw, resume_hw, jd_max_chars)}
      {_track_section("Hardware", hardware, resume_hw, resume_sw, resume_hw, jd_max_chars)}
      <p style="font-size:11px;color:#999;margin-top:28px;border-top:1px solid #eee;padding-top:10px;">
        Keyword diffs are computed locally by set arithmetic over the posting text &mdash;
        every &ldquo;asked for&rdquo; term is literally present in the job description, never inferred.
        Generated by job-agent.
      </p>
    </div>"""

    return RenderedEmail(
        subject=subject,
        html=body,
        text=_plain_text(software, hardware, resume_sw, resume_hw, jd_max_chars),
        job_ids=[j.id for j in software.jobs] + [j.id for j in hardware.jobs],
    )


def send(rendered: RenderedEmail, to_address: str) -> None:
    """Send via Gmail SMTP over implicit TLS. Credentials come from the env."""
    gmail_address = require_env("GMAIL_ADDRESS", "sending the daily email")
    app_password = require_env("GMAIL_APP_PASSWORD", "sending the daily email")

    message = EmailMessage()
    message["Subject"] = rendered.subject
    message["From"] = gmail_address
    message["To"] = to_address
    message.set_content(rendered.text)
    message.add_alternative(rendered.html, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as server:
        server.login(gmail_address, app_password)
        server.send_message(message)


def write_dry_run(rendered: RenderedEmail, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "email.html"
    path.write_text(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(rendered.subject)}</title></head>"
        f"<body style='background:#f6f7f9;margin:0;padding:20px;'>"
        f"<p style='font-family:monospace;font-size:12px;color:#666;max-width:680px;margin:0 auto 12px;'>"
        f"DRY RUN &mdash; nothing was sent.<br>Subject: {html.escape(rendered.subject)}</p>"
        f"{rendered.html}</body></html>",
        encoding="utf-8",
    )
    (out_dir / "email.txt").write_text(rendered.text, encoding="utf-8")
    return path
