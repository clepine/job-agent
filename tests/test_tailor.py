"""Honesty-validator tests.

This is the correctness requirement from PLAN.md §2 stage 7: a tailored resume
may reorder, rephrase, and promote, but may never claim a skill the owner does
not have. These tests exist because a prompt instruction is not a guarantee —
the validator is.
"""

from __future__ import annotations

import pytest

from pipeline.tailor import (
    TailoringRejected,
    apply_tailoring,
    find_new_terms,
    master_bullets,
    tailor_for_job,
    validate_tailored,
)
from pipeline.llm import LlmClient
from pipeline.models import Job

from .conftest import StubAnthropic, StubResponse


# --- the honest case -------------------------------------------------------

def test_faithful_rewrite_passes(resume_hw):
    original = master_bullets(resume_hw)[0]
    tailored = {
        "summary": "Led with the embedded firmware work.",
        "bullet_rewrites": [
            {"original": original, "rewritten": original.replace("Built", "Engineered")}
        ],
        "skill_renames": [],
        "skill_order": ["Verilog", "C"],
    }
    assert validate_tailored(tailored, resume_hw) == []


def test_reordering_only_passes(resume_sw):
    tailored = {
        "summary": "Reordered skills toward the posting.",
        "bullet_rewrites": [],
        "skill_renames": [],
        "skill_order": ["Python", "C++", "C"],
    }
    assert validate_tailored(tailored, resume_sw) == []


# --- inventing a technology in a bullet ------------------------------------

@pytest.mark.parametrize(
    "invented",
    [
        "Kubernetes", "Rust", "PyTorch", "FreeRTOS", "Altium",
        "SystemVerilog", "Cadence Virtuoso", "STM32", "Terraform", "Kafka",
    ],
)
def test_invented_technology_in_bullet_is_caught(resume_hw, invented):
    original = master_bullets(resume_hw)[0]
    tailored = {
        "summary": "x",
        "bullet_rewrites": [
            {"original": original, "rewritten": f"{original} Deployed with {invented}."}
        ],
        "skill_renames": [],
        "skill_order": [],
    }
    violations = validate_tailored(tailored, resume_hw)
    assert violations, f"{invented!r} should have been rejected"
    assert any(v.kind == "unknown_term" for v in violations)
    # The reported term may be the curated canonical rather than the literal
    # string (FreeRTOS is flagged as "RTOS"), so match either direction.
    invented_low = invented.lower()
    assert any(
        term.lower() in invented_low or invented_low in term.lower()
        for v in violations
        if (term := v.detail.split("'")[1] if "'" in v.detail else "")
    ), f"no violation names {invented!r}: {[str(v) for v in violations]}"


def test_invented_skill_is_caught(resume_sw):
    tailored = {
        "summary": "x",
        "bullet_rewrites": [],
        "skill_renames": [],
        "skills": {"languages": ["Python", "Go"]},   # Go is not on the resume
        "skill_order": [],
    }
    violations = validate_tailored(tailored, resume_sw)
    assert any(v.kind == "unknown_skill" for v in violations)


def test_fabricated_bullet_is_caught(resume_sw):
    tailored = {
        "summary": "x",
        "bullet_rewrites": [
            {
                "original": "Shipped a production Kubernetes platform serving 10M users.",
                "rewritten": "Shipped a production Kubernetes platform serving 10M users.",
            }
        ],
        "skill_renames": [],
        "skill_order": [],
    }
    violations = validate_tailored(tailored, resume_sw)
    assert any(v.kind == "unmatched_bullet" for v in violations)


# --- the permitted rename carve-out ---------------------------------------

def test_permitted_rename_verilog_to_rtl_design(resume_hw):
    jd = "We need strong RTL design skills and simulation experience."
    tailored = {
        "summary": "x",
        "bullet_rewrites": [],
        "skill_renames": [{"from": "Verilog", "to": "RTL design"}],
        "skill_order": [],
    }
    assert validate_tailored(tailored, resume_hw, jd) == []


def test_rename_source_must_be_a_real_skill(resume_hw):
    jd = "We need RTL design."
    tailored = {
        "summary": "x",
        "bullet_rewrites": [],
        "skill_renames": [{"from": "SystemVerilog", "to": "RTL design"}],
        "skill_order": [],
    }
    violations = validate_tailored(tailored, resume_hw, jd)
    assert any(v.kind == "bad_rename" for v in violations)


def test_rename_target_must_appear_in_the_job_description(resume_hw):
    jd = "We need embedded C experience."
    tailored = {
        "summary": "x",
        "bullet_rewrites": [],
        "skill_renames": [{"from": "Verilog", "to": "UVM verification"}],
        "skill_order": [],
    }
    violations = validate_tailored(tailored, resume_hw, jd)
    assert any(v.kind == "bad_rename" for v in violations)


# --- term extraction sanity ------------------------------------------------

def test_ordinary_english_is_not_flagged(resume_sw):
    from pipeline.resumes import skill_items

    blob = " ".join(skill_items(resume_sw))
    assert find_new_terms("Collaborated closely with the team to deliver on time.", blob.lower()) == []


def test_roman_numerals_and_common_acronyms_not_flagged(resume_sw):
    from pipeline.tailor import _blob

    assert find_new_terms("Delivered PII and FAQ guardrails for the AI pipeline.", _blob(resume_sw)) == []


# --- apply_tailoring is structurally additive-proof ------------------------

def test_apply_never_adds_bullets_or_skills(resume_hw):
    from pipeline.resumes import skill_groups

    def count(resume):
        return (
            len(master_bullets(resume)),
            sum(len(items) for _label, items in skill_groups(resume)),
            len(resume.get("projects") or []),
            [label for label, _ in skill_groups(resume)],   # labels must be untouched
        )

    tailored = {
        "summary": "x",
        "bullet_rewrites": [
            {"original": master_bullets(resume_hw)[0], "rewritten": "Short rewrite."},
            {"original": "a bullet that does not exist", "rewritten": "Injected!"},
        ],
        "skill_renames": [{"from": "Verilog", "to": "RTL design"}],
        "skill_order": ["Verilog"],
        "promote_projects": ["Digital Logic Design Project"],
    }
    before = count(resume_hw)
    result = apply_tailoring(resume_hw, tailored)
    assert count(result) == before
    assert "Injected!" not in str(result)
    assert result["projects"][0]["title"] == "Digital Logic Design Project"


def test_apply_preserves_contact_and_education(resume_hw):
    result = apply_tailoring(resume_hw, {"summary": "x", "bullet_rewrites": [], "skill_renames": [], "skill_order": []})
    assert result["contact"] == resume_hw["contact"]
    assert result["education"] == resume_hw["education"]


# --- end-to-end against a stubbed client ----------------------------------

def _job(**kw) -> Job:
    base = dict(
        company="Draper",
        title="Mixed Signal Electronic Design Engineer",
        location="Cambridge, MA",
        url="https://example.com/jobs/1",
        description="Requirements\nStrong RTL design skills. Experience with oscilloscopes and PCB layout.",
        track="hardware",
    )
    base.update(kw)
    return Job(**base)


def test_tailor_for_job_rejects_a_hallucinating_model(cfg, resume_hw):
    bad = {
        "summary": "Emphasized FPGA work.",
        "bullet_rewrites": [
            {
                "original": master_bullets(resume_hw)[0],
                "rewritten": "Built a SystemVerilog UVM testbench in Cadence Xcelium.",
            }
        ],
        "skill_renames": [],
        "skill_order": [],
    }
    llm = LlmClient(cfg, client=StubAnthropic([StubResponse(bad)]))
    with pytest.raises(TailoringRejected) as exc:
        tailor_for_job(llm, _job(), resume_hw, cfg)
    assert "SystemVerilog" in str(exc.value) or "Cadence" in str(exc.value)


def test_tailor_for_job_accepts_an_honest_model(cfg, resume_hw):
    original = master_bullets(resume_hw)[0]
    good = {
        "summary": "Promoted the digital logic project and led with RTL design.",
        "bullet_rewrites": [{"original": original, "rewritten": original}],
        "skill_renames": [{"from": "Verilog", "to": "RTL design"}],
        "skill_order": ["Verilog", "C"],
        "promote_projects": ["Digital Logic Design Project"],
    }
    llm = LlmClient(cfg, client=StubAnthropic([StubResponse(good)]))
    result = tailor_for_job(llm, _job(), resume_hw, cfg)
    assert result.violations == []
    assert result.summary.startswith("Promoted")
    assert llm.ledger.calls == 1


def test_tailor_sends_the_cache_breakpoint_on_the_stable_prefix(cfg, resume_hw):
    original = master_bullets(resume_hw)[0]
    good = {
        "summary": "x",
        "bullet_rewrites": [{"original": original, "rewritten": original}],
        "skill_renames": [],
        "skill_order": [],
    }
    stub = StubAnthropic([StubResponse(good)])
    llm = LlmClient(cfg, client=stub)
    tailor_for_job(llm, _job(), resume_hw, cfg)

    call = stub.messages.calls[0]
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    # The volatile per-job payload must be AFTER the cached prefix.
    assert "Draper" not in call["system"][0]["text"]
    assert "Draper" in call["messages"][0]["content"]
    # Sonnet 5 rejects sampling params; make sure we never send them.
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in call
