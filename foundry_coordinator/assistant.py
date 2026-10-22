"""Launch assistant (2026-10-21): plain-language control of a project from the GUI.

The operator types a request on the Automation page ("start Animal Uprising from the
next sprint that isn't done"). Two typed control actions carry it:

  assistant_plan {text}         -> a compact project snapshot (every sprint plan's
                                   checklist status + coordinator state, queue, pause)
                                   goes to the planner persona once; its proposed steps
                                   are checked deterministically and stored as a plan.
  assistant_run  {plan_id, steps} -> runs the confirmed steps in order, nothing else.

Nothing runs without the operator's confirmation. The model only proposes; every
step is one of a fixed set of coordinator operations and is validated against the
live queue before it is shown and again before it runs. One model call per request.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Optional

from . import states as S

STEP_KINDS = {
    "queue_approve": "put a sprint on the queue (task_id, deps)",
    "reopen": "put a CANCELLED sprint back on the queue (task_id, deps)",
    "resume_task": "resume a PAUSED sprint (task_id)",
    "pause_task": "pause a running sprint (task_id)",
    "unblock_task": "send a BLOCKED/FAILED sprint back to READY (task_id, note)",
    "cancel_task": "cancel a sprint (task_id, note)",
    "project_resume": "resume the whole project",
    "project_pause": "pause the whole project at a safe point (note)",
}
MAX_TEXT = 2000
MAX_STEPS = 20
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "steps", "questions"],
    "properties": {
        "summary": {"type": "string"},
        "steps": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "task_id", "deps", "note", "why"],
            "properties": {
                "kind": {"type": "string", "enum": sorted(STEP_KINDS)},
                "task_id": {"type": "string"},
                "deps": {"type": "array", "items": {"type": "string"}},
                "note": {"type": "string"},
                "why": {"type": "string"}}}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
}
SYSTEM = (
    "You are the launch assistant of a coding-automation coordinator. The operator asks, in "
    "plain language, to start, continue, pause or re-order work on a project. You get a JSON "
    "snapshot of the project: every sprint plan with its checklist progress and the "
    "coordinator's state for it, the queue, and whether the project is paused. Propose the "
    "smallest ordered list of steps that does what the operator asked, using only these step "
    "kinds: " + "; ".join(f"{k} = {v}" for k, v in STEP_KINDS.items()) + ". Rules: a sprint is "
    "done when the coordinator says COMPLETE or its plan says DONE / all checklist rows are "
    "ticked; sprints run in plan order unless the operator says otherwise, each depending on "
    "the previous one you queue; a CANCELLED sprint is queued again with reopen, never "
    "queue_approve; a sprint already QUEUED or running needs no step; if the project is paused "
    "and the operator wants work to run, end with project_resume. Use task ids exactly as in "
    "the snapshot. Never invent sprints. If the request is ambiguous, propose nothing and ask "
    "in questions. Reply with JSON only.")


def _natural(tid: str) -> list:
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", tid)]


def plan_status(text: str) -> dict:
    """Cheap, deterministic progress signals from a plan file."""
    ticked = len(re.findall(r"^\s*[-*]\s+\[[xX]\]", text, re.M))
    open_ = len(re.findall(r"^\s*[-*]\s+\[ \]", text, re.M))
    title = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("#")), "")
    m = re.search(r"^\**\s*status\**\s*[:=]\s*\**\s*([A-Za-z _-]+)", text, re.I | re.M)
    deps = re.findall(r"(?:depends on|after)\s*[:=]?\s*`?([A-Z][A-Za-z0-9_-]*-\w+)`?", text, re.I)
    return {"title": title[:120], "checklist": f"{ticked}/{ticked + open_}",
            "plan_status": (m.group(1).strip().upper()[:24] if m else ""),
            "plan_deps": sorted(set(deps))[:6]}


def snapshot(coordinator, P) -> dict:
    """What the model sees: small, deterministic, no file bodies."""
    store = coordinator.store
    ids = []
    try:
        ids = list(P.adapter.list_tasks())
    except Exception:                                            # noqa: BLE001
        ids = []
    known = {t["task_id"]: t for t in store.tasks(P.id)}
    for tid in known:
        if tid not in ids:
            ids.append(tid)
    sprints = []
    for tid in sorted(set(ids), key=_natural):
        row: dict[str, Any] = {"task_id": tid}
        path = _plan_path(P, tid)
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    row.update(plan_status(f.read(60000)))
            except OSError:
                pass
        t = known.get(tid)
        row["coordinator_state"] = t["state"] if t else "NOT_QUEUED"
        q = store.queue_entry(P.id, tid) if t else None
        if q:
            row["queued_deps"] = q.get("deps") or []
        sprints.append(row)
    proj = store.project(P.id)
    paused = bool(proj and proj["paused"])
    return {"project": P.id, "paused": paused, "sprints": sprints}


def _plan_path(P, tid: str) -> str:
    ws = getattr(getattr(P, "profile", None), "workspace", "") or ""
    p = os.path.join(ws, "docs", "progress", f"{tid}_PLAN.md")
    return p if ws else ""


def validate(snap: dict, steps: list) -> tuple[list, list]:
    """Check each proposed step against the snapshot, simulating the queue as it
    goes. Returns (clean steps, warnings); an invalid step is dropped with a reason."""
    state = {s["task_id"]: s["coordinator_state"] for s in snap["sprints"]}
    clean, warn = [], []
    for i, st in enumerate(steps[:MAX_STEPS], 1):
        kind = str(st.get("kind") or "")
        tid = str(st.get("task_id") or "").strip()
        deps = [str(d).strip() for d in (st.get("deps") or []) if str(d).strip()]
        note = str(st.get("note") or "")[:500]
        why = str(st.get("why") or "")[:300]
        if kind not in STEP_KINDS:
            warn.append(f"step {i}: unknown kind {kind!r} dropped")
            continue
        if kind.startswith("project_"):
            clean.append({"kind": kind, "task_id": "", "deps": [], "note": note, "why": why})
            continue
        if tid not in state:
            warn.append(f"step {i}: {tid or '(no task)'} is not a sprint of {snap['project']}; dropped")
            continue
        cur = state[tid]
        bad = [d for d in deps if d not in state or state[d] == "NOT_QUEUED"]
        ok = {
            "queue_approve": cur in ("NOT_QUEUED", S.QUEUED),
            "reopen": cur == S.CANCELLED,
            "resume_task": cur == S.PAUSED,
            "pause_task": cur not in S.TERMINAL and cur not in (S.PAUSED, "NOT_QUEUED"),
            "unblock_task": cur in (S.BLOCKED, S.FAILED),
            "cancel_task": cur not in S.TERMINAL and cur != "NOT_QUEUED",
        }[kind]
        if not ok:
            warn.append(f"step {i}: {kind} {tid} does not fit its state {cur}; dropped")
            continue
        if kind in ("queue_approve", "reopen") and bad:
            warn.append(f"step {i}: {tid} depends on {', '.join(bad)}, which is not queued "
                        f"(earlier steps included); dependency dropped")
            deps = [d for d in deps if d not in bad]
        clean.append({"kind": kind, "task_id": tid, "deps": deps, "note": note, "why": why})
        state[tid] = {"queue_approve": S.QUEUED, "reopen": S.QUEUED, "resume_task": "RESUMED",
                      "pause_task": S.PAUSED, "unblock_task": S.READY,
                      "cancel_task": S.CANCELLED}[kind]
    return clean, warn


class Assistant:
    def __init__(self, coordinator, persona_fn=None):
        self.c = coordinator
        self.persona_fn = persona_fn

    # ------------------------------------------------------------------ storage

    def _dir(self) -> str:
        d = os.path.join(self.c.state_dir, "assistant")
        os.makedirs(d, exist_ok=True)
        return d

    def _save(self, plan: dict) -> None:
        with open(os.path.join(self._dir(), plan["plan_id"] + ".json"), "w",
                  encoding="utf-8") as f:
            json.dump(plan, f, indent=1)

    def load(self, plan_id: str) -> Optional[dict]:
        if not re.fullmatch(r"asp-[0-9a-f]{12}", plan_id or ""):
            return None
        p = os.path.join(self._dir(), plan_id + ".json")
        if not os.path.isfile(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    # ------------------------------------------------------------------ plan

    def persona(self, P) -> str:
        if self.persona_fn:
            return self.persona_fn(P)
        cfg = (getattr(P.profile, "raw", None) or {}).get("assistant") or {}
        return str(cfg.get("persona") or P.profile.roles.get("planner") or
                   P.profile.persona("reviewer", ""))

    def plan(self, P, text: str, actor: str) -> dict:
        text = (text or "").strip()[:MAX_TEXT]
        if not text:
            raise ValueError("say what you want done")
        if P.foundry is None:
            raise ValueError("the assistant needs Foundry ([foundry] url)")
        snap = snapshot(self.c, P)
        plan_id = "asp-" + uuid.uuid4().hex[:12]
        persona = self.persona(P)
        sid = "assistant-" + plan_id
        try:
            P.foundry.bind(sid, P.id, "_assistant", plan_id, "planner", persona,
                           purpose="", meta={"purpose": "launch_assistant"})
        except Exception:                                        # noqa: BLE001
            pass                                     # unbound is fine where allowed
        raw, meta = P.foundry.chat(persona, [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Operator request: " + text + "\n\nProject snapshot:\n"
             + json.dumps(snap, separators=(",", ":"))}],
            session_id=sid, max_tokens=1500, schema=SCHEMA, timeout=120)
        try:
            out = json.loads(_json_block(raw))
        except ValueError:
            raise ValueError("the assistant's answer was not valid JSON; try rephrasing") from None
        steps, warnings = validate(snap, out.get("steps") or [])
        plan = {"plan_id": plan_id, "project": P.id, "text": text, "actor": actor,
                "created_ts": time.time(), "summary": str(out.get("summary") or "")[:1000],
                "questions": [str(q)[:300] for q in (out.get("questions") or [])][:5],
                "steps": steps, "warnings": warnings, "persona": persona,
                "served_model": (meta or {}).get("served_model") or "", "status": "proposed",
                "snapshot": snap}
        self._save(plan)
        self.c.store.event(P.id, "", "assistant_plan", detail={
            "plan_id": plan_id, "actor": actor, "steps": len(steps), "warnings": warnings[:5],
            "served_model": plan["served_model"]})
        return plan

    # ------------------------------------------------------------------ run

    def run(self, P, plan_id: str, picked: Optional[list], actor: str) -> dict:
        plan = self.load(plan_id)
        if plan is None or plan.get("project") != P.id:
            raise ValueError(f"no plan {plan_id!r} for {P.id}")
        if plan.get("status") != "proposed":
            raise ValueError(f"plan {plan_id} was already {plan.get('status')}")
        steps = plan["steps"]
        if picked is not None:
            idx = sorted({int(i) for i in picked if 0 <= int(i) < len(steps)})
            steps = [steps[i] for i in idx]
        # re-check against the live queue: the world may have moved since the preview
        steps, warn = validate(snapshot(self.c, P), steps)
        by = f"{actor} via launch assistant"
        results = []
        for st in steps:
            k, tid, deps, note = st["kind"], st["task_id"], st["deps"], st["note"]
            try:
                if k == "queue_approve":
                    self.c.approve(P.id, tid, deps, by, note or f"launch assistant {plan_id}")
                elif k == "reopen":
                    self.c.reopen(P.id, tid, deps, by, note or f"launch assistant {plan_id}")
                elif k == "resume_task":
                    self.c.resume(P.id, tid, actor=by)
                elif k == "pause_task":
                    self.c.pause(P.id, tid, note or "launch assistant", actor=by)
                elif k == "unblock_task":
                    self.c.unblock(P.id, tid, note or "launch assistant", reset_attempts=False,
                                   actor=by)
                elif k == "cancel_task":
                    self.c.cancel(P.id, tid, note or "launch assistant", actor=by)
                elif k == "project_resume":
                    self.c.store.set_project_paused(P.id, False)
                elif k == "project_pause":
                    self.c.store.set_project_paused(P.id, True, note or f"{by}: {plan_id}")
                results.append({**st, "result": "done"})
            except Exception as e:                               # noqa: BLE001
                results.append({**st, "result": f"failed: {e}"[:300]})
                break                                # later steps may depend on this one
        plan.update(status="ran", ran_by=actor, ran_ts=time.time(), results=results,
                    run_warnings=warn)
        self._save(plan)
        self.c.store.event(P.id, "", "assistant_run", detail={
            "plan_id": plan_id, "actor": actor,
            "results": [f"{r['kind']} {r['task_id']}: {r['result']}" for r in results]})
        return plan


def _json_block(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    i, j = t.find("{"), t.rfind("}")
    return t[i:j + 1] if i >= 0 and j > i else t
