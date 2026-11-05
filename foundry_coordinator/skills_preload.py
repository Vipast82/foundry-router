"""Skill pages go into the session prompt; agents do not have to choose to load them.

2026-11-02 (owner: "make sure the skills are being used"): the Kilo log showed about 1 in 7
AU implement sessions calling the skill tool at all. Only a sprint's first session did; repair,
hand-off and local-model sessions loaded none, and no session ever loaded one of the 27
`roblox-*` skills installed that day. Cause: a plan names its skills in a `## Skills` section,
but AU's condensed session brief does not carry that section, so an agent saw the list only
if it opened the plan and read that far.

Now every implement and repair prompt carries the SKILL.md bodies of:
  * `[skills] preload_always` (the project's base skills), then
  * the skills the sprint's plan names in backticks in its `## Skills` section,
up to `[skills] preload_max_kb`. A skill that does not fit is listed by name to load with the
skill tool. A skill page is a short entry page; its `references/` stay on disk and the block
says where. `[skills] preload = false` turns it off.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from . import skills_install

DEFAULTS = {"preload": True, "preload_always": [], "preload_max_kb": 24,
            "preload_by_task": {}}

_SECTION = re.compile(r"^#{2,3}\s+Skills\b[^\n]*\n(.*?)(?=^#{1,3}\s|\Z)", re.M | re.S | re.I)
_NAME = re.compile(r"`([A-Za-z0-9_-]{2,64})`")
_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def settings(profile) -> dict:
    raw = (getattr(profile, "raw", None) or {}).get("skills") or {}
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        out.update({k: raw[k] for k in DEFAULTS if k in raw})
    names = out.get("preload_always") or []
    if isinstance(names, str):
        names = names.replace(",", " ").split()
    out["preload_always"] = [str(n).strip() for n in names
                             if skills_install._ok_name(str(n).strip())]
    try:
        out["preload_max_kb"] = max(0, min(128, int(out.get("preload_max_kb") or 0)))
    except (TypeError, ValueError):
        out["preload_max_kb"] = DEFAULTS["preload_max_kb"]
    out["preload"] = out.get("preload") is not False
    by = out.get("preload_by_task") or {}
    clean = {}
    if isinstance(by, dict):
        for pat, names in by.items():
            if isinstance(names, str):
                names = names.replace(",", " ").split()
            if isinstance(names, list):
                clean[str(pat)] = [str(n).strip() for n in names
                                   if skills_install._ok_name(str(n).strip())]
    out["preload_by_task"] = clean
    return out


def task_skills(profile, task_id: str) -> list[str]:
    """`[skills] preload_by_task`: {sprint id glob: [skills]}, every matching pattern in
    order. Victor 2026-11-04: "make sure whatever sprints are reviewing / making assets or
    fixing assets are using the best corresponding skills" - a plan that forgets a skill no
    longer means the session never sees it (no AS- sprint had au-blender-asset)."""
    import fnmatch
    out: list[str] = []
    for pat, names in settings(profile)["preload_by_task"].items():
        if fnmatch.fnmatchcase(task_id, pat):
            out += [n for n in names if n not in out]
    return out


def plan_skills(text: str) -> list[str]:
    """Skill names a plan's `## Skills` section names in backticks, in order."""
    out: list[str] = []
    for m in _SECTION.finditer(str(text or "")):
        for name in _NAME.findall(m.group(1)):
            if name not in out:
                out.append(name)
    return out


def _dirs(c, P) -> list[str]:
    target = skills_install.settings(P.profile).get("target") or ".kilo/skills"
    roots = [P.profile.workspace]
    try:
        main = c.tracks.main_of(P)
        if main is not P:
            roots.append(main.profile.workspace)
    except Exception:                                            # noqa: BLE001
        pass
    return [os.path.join(r, *target.replace("\\", "/").split("/")) for r in roots if r]


def find(c, P, name: str) -> Optional[str]:
    """The folder of an installed skill (the game's skill folder first, then Foundry's copy)."""
    for base in _dirs(c, P):
        d = os.path.join(base, name)
        if os.path.isfile(os.path.join(d, "SKILL.md")):
            return d
    return skills_install.find_source(P.id, name)


def _page(d: str) -> str:
    with open(os.path.join(d, "SKILL.md"), encoding="utf-8", errors="replace") as f:
        text = f.read().replace("\r\n", "\n")
    m = _FRONT.match(text)
    if m:
        desc = re.search(r"^description:\s*(.+)$", m.group(1), re.M)
        text = ((f"_{desc.group(1).strip()}_\n\n" if desc else "") + text[m.end():]).strip()
    return text.strip()


def _shown(P, d: str) -> str:
    ws = os.path.abspath(P.profile.workspace or "")
    full = os.path.abspath(d)
    if ws and (full + os.sep).startswith(ws + os.sep):
        return os.path.relpath(full, ws).replace(os.sep, "/")
    return full.replace(os.sep, "/")


def wanted(P, spec) -> list[str]:
    s = settings(P.profile)
    names = list(s["preload_always"])
    for n in task_skills(P.profile, str(getattr(spec, "task_id", "") or "")):
        if n not in names:
            names.append(n)
    plan = (spec.data or {}).get("skills")
    if not isinstance(plan, list):
        plan = plan_skills(spec.spec)
    for n in plan:
        if skills_install._ok_name(str(n)) and n not in names:
            names.append(str(n))
    return names


def block(c, P, spec) -> tuple[str, dict]:
    """(prompt block, detail). ('' , detail) when off or nothing to load."""
    s = settings(P.profile)
    detail = {"loaded": [], "over_budget": [], "missing": [], "bytes": 0}
    if not s["preload"]:
        return "", detail
    budget = s["preload_max_kb"] * 1024
    parts = []
    for name in wanted(P, spec):
        d = find(c, P, name)
        if not d:
            detail["missing"].append(name)
            continue
        try:
            page = _page(d)
        except OSError:
            detail["missing"].append(name)
            continue
        size = len(page.encode("utf-8"))
        if detail["bytes"] + size > budget:
            detail["over_budget"].append(name)
            continue
        refs = os.path.isdir(os.path.join(d, "references"))
        where = _shown(P, d)
        parts.append(f"### Skill `{name}` ({where}"
                     + (f"; references in {where}/references/" if refs else "") + ")\n" + page)
        detail["loaded"].append(name)
        detail["bytes"] += size
    if not parts and not detail["over_budget"]:
        return "", detail
    head = ("\n\n## Skills (preloaded by the coordinator)\n"
            "These skill pages are already in this prompt: follow them for this sprint and do "
            "not spend a turn loading them with the skill tool. Open a skill's references/ file "
            "only when a step needs that detail.\n\n")
    tail = ""
    if detail["over_budget"]:
        tail = ("\n\nNot preloaded (over the size budget); load each with the skill tool before "
                "the work it covers: " + ", ".join(f"`{n}`" for n in detail["over_budget"]) + ".")
    return head + "\n\n".join(parts) + tail + "\n", detail
