"""One fresh builder session per model (docs/plans/BUILDER_PER_MODEL_PLAN.md, gap 9).

While `[builder] enabled`, Foundry writes a Kilo subagent `au-builder` into kilo.jsonc, and
the implementer sessions of tasks matching `builder.tasks` may call it (Kilo `task` tool). Each
call is a fresh child session that builds ONE model and stops at the result lines. The sprint
session keeps the look gate, the judge, export, upload, Studio, records and every commit.
Off by default.
"""

from __future__ import annotations

import fnmatch
from typing import Optional

DEFAULTS = {"enabled": False, "tasks": ["AS-*"], "agent": "au-builder", "model": "",
            "steps": 200}

PROMPT = (
    "You are a builder for ONE 3D asset of Animal Uprising. Build exactly the model the "
    "caller names, with the headless Blender tools, and stop when its result lines are in: "
    "mesh_report PASS, side_check (when the pack lists sided parts), ref_iou numbers and the "
    "render_views images. Load the au-blender-asset skill first. Work only in the files and "
    "output folders the caller gives (absolute paths inside the worktree); run Blender the way "
    "the caller says (the foundry-blender / pool command when given). Never export, upload, "
    "open Studio, commit, push or change the plan or ledger: the calling sprint does that. "
    "Your final message: the model ID, every file you wrote, the exact result lines, and what "
    "still looks wrong against the reference views (by axis: +X / -X).")

DENY = ([{"permission": "bash", "pattern": p, "action": "deny"}
         for p in ("git commit*", "git push*", "git tag*", "git reset*", "git checkout*",
                   "git rebase*", "git merge*", "git stash*")]
        + [{"permission": p, "pattern": "*", "action": "deny"}
           for p in ("task", "Roblox_Studio_*", "robloxstudio-mcp_*", "foundry-media_*",
                     "Media_Gen_*")]
        + [{"permission": "read", "pattern": "C:/Roblox/_secrets/*", "action": "deny"},
           {"permission": "edit", "pattern": "C:/Roblox/_secrets/*", "action": "deny"},
           {"permission": "skill", "pattern": "au-blender-asset", "action": "allow"}])


def settings(profile) -> dict:
    raw = (getattr(profile, "raw", None) or {}).get("builder") or {}
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        out.update({k: raw[k] for k in DEFAULTS if k in raw})
    out["enabled"] = bool(out.get("enabled"))
    tasks = out.get("tasks") or []
    if isinstance(tasks, str):
        tasks = [x for x in tasks.replace(",", " ").split() if x]
    out["tasks"] = [str(x).strip() for x in tasks if str(x).strip()]
    out["agent"] = str(out.get("agent") or "au-builder").strip() or "au-builder"
    out["model"] = str(out.get("model") or "").strip()
    try:
        out["steps"] = max(10, min(1000, int(out.get("steps") or 200)))
    except (TypeError, ValueError):
        out["steps"] = 200
    return out


def matches(profile, task_id: str) -> bool:
    cfg = settings(profile)
    if not cfg["enabled"] or not task_id:
        return False
    tid = str(task_id).upper()
    return any(fnmatch.fnmatchcase(tid, g.upper()) for g in cfg["tasks"])


def session_rules(profile, task_id: str, role: str) -> list[dict]:
    """The implementer of a matching task may call the builder (appended after the role
    rules, so it wins over their `task` deny)."""
    if role != "implementer" or not matches(profile, task_id):
        return []
    return [{"permission": "task", "pattern": settings(profile)["agent"], "action": "allow"}]


def agent_block(profile, role_rules: Optional[dict], workspace: str) -> Optional[dict]:
    """The kilo.jsonc `agent.<builder>` object, or None while the builder is off."""
    cfg = settings(profile)
    if not cfg["enabled"]:
        return None
    from foundry_automation import role_rules as RR
    r = RR.normalize("implementer", (role_rules or {}).get("implementer"))
    lst = [{"permission": "*", "pattern": "*", "action": r.get("default") or "deny"}]
    lst += RR.session_rules("implementer", r, workspace)
    lst += DENY
    perm = RR.fold(lst)
    perm["task"] = "deny"                  # no nested subagents (the role's allow-list is dropped)
    block = {"description": "Builds ONE 3D asset headless (Blender) and stops at mesh_report, "
                            "side_check, ref_iou and renders; the sprint exports, uploads and "
                            "commits. Coordinator-owned (gap 9).",
             "mode": "subagent", "steps": cfg["steps"], "prompt": PROMPT,
             "permission": perm}
    if cfg["model"]:
        block["model"] = cfg["model"]
    return block


def notice(profile, task_id: str) -> str:
    """The BUILD prompt paragraph for a matching AS- sprint ('' otherwise)."""
    if not matches(profile, task_id):
        return ""
    a = settings(profile)["agent"]
    return (f"\nBUILDER PER MODEL (trial): build each model in its own fresh `{a}` subagent "
            f"(the Kilo task tool, subagent `{a}`), not in this session. Give each one: the model "
            "ID, the reference pack (`<ID>_ref_vN.json`, `<ID>_views_vN.png`, the concept "
            "sheet), the short note (sizes with their bible source, palette hex, hidden "
            "details, rig and budget), the builder script path and ABSOLUTE output folders, and "
            "this session's Blender command. Several models may build at once: one task call "
            "each in the same message (the Blender pool spreads them). Each builder stops at "
            "mesh_report PASS, side_check, REF_IOU and the renders, and returns its files and "
            "result lines. Then YOU run the look gate and the asset judge, export, upload, do "
            "the Studio checks, write the records and commit. Build a model here yourself only "
            "after its builder failed twice; say so in the report.\n")
