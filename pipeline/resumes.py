"""Master-resume schema helpers.

Skill categories carry an EXPLICIT `label`. They used to be snake_case YAML
keys with the display header derived by title-casing, which silently dropped
every "&" in the owner's real section headers — "Frameworks & Libraries"
printed as "Frameworks Libraries" on a document that goes to employers.

Display text is never derived from a key. It is copied verbatim from the
resume source.

    skills:
      - label: "Hardware & Digital Design"
        items: [Verilog, "PCB layout (KiCad)", ...]
"""

from __future__ import annotations

from typing import Any, Iterator


def skill_groups(resume: dict) -> list[tuple[str, list[str]]]:
    """[(label, items)] in document order, from either schema.

    The legacy mapping form is still read so an older hand-edited file does not
    blow up, but it is not written anywhere and the derived label is explicitly
    marked as a fallback.
    """
    skills = resume.get("skills")
    if not skills:
        return []

    if isinstance(skills, list):
        out: list[tuple[str, list[str]]] = []
        for group in skills:
            if not isinstance(group, dict):
                continue
            label = str(group.get("label", "")).strip()
            items = group.get("items") or []
            if not isinstance(items, list):
                items = [items]
            out.append((label, [str(i) for i in items]))
        return out

    # Legacy: {snake_case_key: [items]}. Derived label — lossy, see docstring.
    return [
        (str(key).replace("_", " ").title(), [str(i) for i in (v if isinstance(v, list) else [v])])
        for key, v in skills.items()
    ]


def skill_items(resume: dict) -> Iterator[str]:
    """Every individual skill string, flattened."""
    for _label, items in skill_groups(resume):
        yield from items


def set_skill_items(resume: dict, label: str, items: list[str]) -> None:
    """Replace one group's items in place, preserving schema form and label."""
    skills = resume.get("skills")
    if isinstance(skills, list):
        for group in skills:
            if isinstance(group, dict) and str(group.get("label", "")).strip() == label:
                group["items"] = items
                return
    elif isinstance(skills, dict):
        for key in skills:
            if str(key).replace("_", " ").title() == label:
                skills[key] = items
                return
