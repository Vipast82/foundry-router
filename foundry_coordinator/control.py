"""Operator actions from the control center, applied by the engine's owner.

Foundry stores what an operator asked for (answer a question, reply to a
permission request, approve or reject a visual/review checkpoint, pause,
resume, cancel, unblock, or a written instruction). It can't change the
coordinator's state: this channel runs inside the one process that owns the
engine (`run --control`), after each tick:

  1. heartbeat to Foundry (the watchdog there notices when this stops)
  2. claim pending actions for this coordinator's projects
  3. for each: key it in the coordinator's own store (never applied twice),
     re-check it against the current task (state, session, request id,
     revision, artifact hashes), apply it through the engine's own methods,
     and report queued / delivered / applied / rejected / failed
  4. report what happened to queued instructions (delivered with the session
     that received them, then applied once that session answered)

An HTTP acceptance is not delivery: "delivered" means Kilo accepted the reply
or prompt; "applied" means the coordinator recorded the resulting state.
Natural-language instructions go to the controlled agent inside its existing
scope and permissions. Nothing here executes shell text.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Callable, Optional

from . import states as S
from .adapters.foundry import FoundryClient, FoundryError

RETRY_UNRECORDED_S = 120      # wait this long for the engine to record a Kilo question
# project-scoped actions (task_id may be empty): pause / resume a whole project
# for an operator upgrade (payload {mode: "safe" | "now", note})
PROJECT_KINDS = ("project_pause", "project_resume")
# launch assistant (2026-10-21): plain-language request -> a checked plan -> run on confirm
ASSISTANT_KINDS = ("assistant_plan", "assistant_run")
# lead recovery case controls (v0.123; payload {case_id, note}): applied through
# the engine's LeadManager (engine.lead) when this coordinator has one
LEAD_KINDS = ("lead_case_pause", "lead_case_resume", "lead_case_takeover", "lead_case_instruct")
# Team Lead piece 4: project memory edits from the chat / Memory tab, applied as
# coordinator commits in docs/memory. Project-scoped (no task); payload
# {"file", "heading", "text"}.
MEMORY_KINDS = ("memory_add", "memory_edit")
# A1 playbook (2026-10-23): cross-project playbook edits from the chat / Memory
# tab's playbook scope, applied as coordinator commits in the playbook repo.
# Project-scoped only for routing/journaling; payload {"file", "heading", "text"}.
PLAYBOOK_KINDS = ("playbook_add", "playbook_edit")
LEAD_UNAVAILABLE = "lead manager not available in this coordinator"
MAX_LEAD_NOTE = 2000
# Team Lead tracks (2026-10-22): project-scoped, applied through the engine's
# TrackManager (tracks/manager.py). Payloads:
#   tracks_analyze {}                          -> the deterministic analysis (read)
#   tracks_propose {max_agents?}               -> a plan {plan_id, tracks, agents, notes}
#   track_start    {plan_id} | {name, sprints, needs?}   create + queue (the confirm)
#   track_stop     {name, reason}
#   track_merge    {name, option?}             merge now (quiet point), or answer a
#                                              conflict decision with `option`
#   track_status   {}                          -> the Tracks tab's data
#   set_max_agents {max_agents}                live cap on running sprints
TRACK_KINDS = ("tracks_analyze", "tracks_propose", "track_start", "track_stop", "track_merge",
               "track_status", "set_max_agents")
# Queue API (2026-10-23, review A2): edits of the approved queue through the
# engine's own methods. Order is priority (tick() walks queue positions);
# nothing here dispatches work or invents a transition. Payloads:
#   queue_approve {deps?, note?, track?}   approve a task onto the queue (or
#                                          update deps/note while still QUEUED)
#   queue_reorder {position}               1-based position within the project
#   queue_deps    {deps}                   replace deps (QUEUED tasks only)
#   queue_requeue {reason}                 reopen a CANCELLED task (COMPLETE
#                                          stays closed: passed work never reruns)
QUEUE_KINDS = ("queue_approve", "queue_reorder", "queue_deps", "queue_requeue")


class ControlChannel:
    def __init__(self, coordinator, foundry: Optional[FoundryClient] = None,
                 source_id: str = "", log: Callable[[str], None] = lambda m: None,
                 heartbeat_every_s: float = 15, clock: Callable[[], float] = time.time):
        self.c = coordinator
        self.store = coordinator.store
        self.foundry = foundry or next((P.foundry for P in coordinator.projects.values()
                                        if P.foundry is not None), None)
        self.source_id = source_id or f"coordinator:{socket.gethostname()}"
        self.log = log
        self.clock = clock
        self.heartbeat_every_s = heartbeat_every_s
        self._last_beat = 0.0
        self._waiting: dict[str, float] = {}     # action id -> first seen (question not recorded yet)
        self.started = clock()
        self.ticks = 0
        # a project paused with resume_after_upgrade is resumed once, at the
        # first poll, when this process runs a different build than the pause
        # recorded (see _resume_after_upgrade)
        self._startup_checked = False

    # ------------------------------------------------------------------ loop

    def poll(self) -> None:
        if not self._startup_checked:
            self._startup_checked = True
            try:
                self._resume_after_upgrade()
            except Exception as e:                               # noqa: BLE001
                self.log(f"control: resume after upgrade: {type(e).__name__}: {e}")
        if self.foundry is None:
            return
        self.ticks += 1
        now = self.clock()
        if now - self._last_beat >= self.heartbeat_every_s:
            try:
                self.foundry.heartbeat(self._beat())
                self._last_beat = now
            except FoundryError as e:
                self.log(f"control: heartbeat failed: {e}")
        try:
            actions = self.foundry.claim_actions(self.source_id, sorted(self.c.projects))
        except FoundryError as e:
            self.log(f"control: claim failed: {e}")
            actions = []
        for a in actions:
            try:
                self.handle(a)
            except Exception as e:                               # noqa: BLE001
                self.log(f"control: {a.get('action_id')}: {type(e).__name__}: {e}")
                self._report(a, "failed", f"coordinator error: {type(e).__name__}: {e}"[:500])
        self.report_messages()
        self._publish_memory()

    def _memory_on(self) -> bool:
        mem = getattr(self.c, "memory", None)
        if mem is None:
            return False
        for P in (getattr(self.c, "projects", None) or {}).values():
            try:
                if mem.enabled(P):
                    return True
            except Exception:                                    # noqa: BLE001
                continue
        return False

    def _publish_memory(self) -> None:
        """Changed memory snapshots (files, entries, slices, notes) go to the
        router as `memory` observations, so the chat and the Memory tab read them
        without a round trip to the coordinator."""
        mem = getattr(self.c, "memory", None)
        if mem is None or self.foundry is None or not hasattr(self.foundry, "telemetry"):
            return
        try:
            mem.publish(self.foundry, self.source_id)
        except Exception as e:                                   # noqa: BLE001
            self.log(f"control: memory snapshot: {type(e).__name__}: {e}")
        pb = getattr(self.c, "playbook", None)
        if pb is not None:
            try:
                pb.publish(self.foundry, self.source_id)
            except Exception as e:                               # noqa: BLE001
                self.log(f"control: playbook snapshot: {type(e).__name__}: {e}")

    def _memory_action(self, a, actor):
        """memory_add / memory_edit: a validated edit of one docs/memory file,
        committed by the coordinator with the MEMORY prefix (or the task's when
        the payload names a task)."""
        mem = getattr(self.c, "memory", None)
        if mem is None:
            return self._report(a, "rejected", "this coordinator has no project memory")
        P = self.c.projects[a["project_id"]]
        p = a.get("payload") or {}
        fn = mem.add if a["kind"] == "memory_add" else mem.edit
        out = fn(P, str(p.get("file") or ""), str(p.get("heading") or ""), str(p.get("text") or ""),
                 actor, task_id=str(p.get("task_id") or a.get("task_id") or ""))
        if not out.get("ok"):
            return self._report(a, "rejected", str(out.get("error") or "refused")[:500], out)
        detail = (f"{out['path']} > {str(p.get('heading') or '')[:60]}: "
                  + ("committed " + out["sha"][:12] if out.get("sha") else "no change"))
        self._publish_memory()
        return self._report(a, "applied", detail[:500], out)

    def _playbook_on(self) -> bool:
        pb = getattr(self.c, "playbook", None)
        try:
            return pb is not None and pb.configured()
        except Exception:                                        # noqa: BLE001
            return False

    def _playbook_action(self, a, actor):
        """playbook_add / playbook_edit: a validated edit of one playbook file,
        committed by the coordinator to the playbook repo (A1). The project id
        only routes and journals the action; the playbook is cross-project."""
        pb = getattr(self.c, "playbook", None)
        if pb is None or not pb.configured():
            return self._report(a, "rejected", "this coordinator has no playbook configured "
                                               "(--playbook or $FOUNDRY_PLAYBOOK)")
        P = self.c.projects[a["project_id"]]
        p = a.get("payload") or {}
        fn = pb.add if a["kind"] == "playbook_add" else pb.edit
        out = fn(P, str(p.get("file") or ""), str(p.get("heading") or ""),
                 str(p.get("text") or ""), actor, task_id=str(p.get("task_id") or
                                                              a.get("task_id") or ""))
        if not out.get("ok"):
            return self._report(a, "rejected", str(out.get("error") or "refused")[:500], out)
        detail = (f"{out['path']} > {str(p.get('heading') or '')[:60]}: "
                  + ("committed " + out["sha"][:12] if out.get("sha") else "no change"))
        self._publish_memory()
        return self._report(a, "applied", detail[:500], out)

    def _lifecycle_on(self) -> bool:
        """At least one project has [lifecycle] enabled."""
        lc = getattr(self.c, "_lc", None)
        if not callable(lc):
            return False
        return any(lc(P) for P in (getattr(self.c, "projects", None) or {}).values())

    def _lead(self):
        """The engine's LeadManager, or None (wired by the integrator)."""
        return getattr(self.c, "lead", None)

    def _lead_on(self) -> bool:
        """The engine exposes a lead manager and at least one project enables it."""
        lead = self._lead()
        enabled = getattr(lead, "enabled", None) if lead is not None else None
        if not callable(enabled):
            return False
        for P in (getattr(self.c, "projects", None) or {}).values():
            try:
                if enabled(P):
                    return True
            except Exception:                                    # noqa: BLE001
                continue
        return False

    def _beat(self) -> dict:
        tasks = self.store.tasks()
        lead_on = self._lead_on()
        mem_on = self._memory_on()
        pb_on = self._playbook_on()
        return {"source": {"id": self.source_id, "kind": "coordinator", "host": socket.gethostname(),
                           "version": _version(), "capabilities": {
                               "actions": ["answer_question", "reply_permission", "approve_review",
                                           "reject_review", "pause", "resume", "cancel", "unblock",
                                           "instruction", "withdraw_instruction",
                                           "decision_answer", *PROJECT_KINDS, *ASSISTANT_KINDS,
                                           *TRACK_KINDS, *QUEUE_KINDS,
                                           *(LEAD_KINDS if lead_on else ()),
                                           *(MEMORY_KINDS if mem_on else ()),
                                           *(PLAYBOOK_KINDS if pb_on else ())]}},
                "contract": _contract(), "coordinator_version": _coordinator_version(),
                # lifecycle features advertised only when some project actually
                # runs the lifecycle; project pause/resume always; lead_manager
                # only when the engine has a lead manager some project enables
                # (decision_answer also with the lead manager on: Victor answers its
                # lead_escalation decisions even when no project runs the lifecycle)
                "features": (["decision_answer", "lifecycle"] if self._lifecycle_on() else
                             ["decision_answer"] if lead_on else [])
                + ["project_pause", "tracks", "queue"] + (["lead_manager"] if lead_on else [])
                + (["project_memory"] if mem_on else [])
                + (["playbook"] if pb_on else []),
                # Team Lead tracks per project (the Tracks tab reads it from the
                # coordinator source's heartbeat): max_agents, tracks, Studio timeline
                "tracks": self._tracks_status(),
                # per project: {paused, mode, note, actor, since, busy_sessions,
                # running_helpers, running_reviews, safe_to_upgrade}
                "project_pause": {pid: self._pause_status(pid) for pid in sorted(self.c.projects)},
                "sent_ts": self.clock(), "tick": self.ticks,
                "uptime_s": round(self.clock() - self.started, 1),
                "projects": sorted(self.c.projects),
                "coordinator_owner": {"present": True, "id": (_owner(self.store) or {}).get("id"),
                                      "beat_ts": (_owner(self.store) or {}).get("beat")},
                "sessions_tracked": sum(1 for t in tasks if t["state"] in S.WRITING)}

    # ------------------------------------------------------------------ reporting

    def _report(self, a: dict, status: str, detail: str, result: Optional[dict] = None) -> None:
        key = f"control:{a['action_id']}"
        if status in ("applied", "rejected", "failed"):
            self.store.op_update(key, "done", {"status": status, "detail": detail,
                                               "result": result or {}})
        elif status == "queued":
            self.store.op_update(key, "queued", {"status": status, "detail": detail,
                                                 "result": result or {}})
        try:
            self.foundry.action_status(a["action_id"], self.source_id, status, detail, result)
        except FoundryError as e:
            # Foundry offers the claimed action again after a minute; the op key
            # makes the second pass report the stored outcome, not redo it.
            self.log(f"control: could not report {a['action_id']} {status}: {e}")

    # ------------------------------------------------------------------ actions

    def handle(self, a: dict) -> None:
        key = f"control:{a['action_id']}"
        prior = self.store.op(key)
        retrying = a["kind"] in ("answer_question", "reply_permission") and             bool(prior) and prior["status"] == "queued"
        if prior and (prior["status"] == "done" or (prior["status"] == "queued" and not retrying)):
            r = prior["result"] or {}
            return self._report(a, r.get("status") or "failed", r.get("detail") or "",
                                r.get("result"))
        if prior and prior["status"] == "started" and a["action_id"] not in self._waiting:
            # we crashed while applying it: don't guess, and don't redo it
            return self._report(a, "failed", "outcome uncertain: the coordinator stopped while "
                                             "applying this action; check the task's history "
                                             "before repeating it")
        pid, tid = a.get("project_id") or "", a.get("task_id") or ""
        if not prior:
            self.store.op_begin(key, pid, tid, "control")
        if pid not in self.c.projects:
            return self._report(a, "rejected", f"{pid or '(no project)'} is not run by this "
                                               f"coordinator")
        if a["kind"] in ASSISTANT_KINDS:
            return self._assistant_action(a, f"{a.get('actor') or 'operator'} (control center)")
        if a["kind"] in PROJECT_KINDS:
            # project-scoped: no task to look up or re-check
            return getattr(self, f"_do_{a['kind']}")(
                a, f"{a.get('actor') or 'operator'} (control center)")
        if a["kind"] in TRACK_KINDS:
            return self._track_action(a, f"{a.get('actor') or 'operator'} (control center)")
        if a["kind"] in MEMORY_KINDS:
            return self._memory_action(a, f"{a.get('actor') or 'operator'} (control center)")
        if a["kind"] in PLAYBOOK_KINDS:
            return self._playbook_action(a, f"{a.get('actor') or 'operator'} (control center)")
        if a["kind"] in QUEUE_KINDS:
            return self._queue_action(a, f"{a.get('actor') or 'operator'} (control center)")
        t = self.store.task(pid, tid)
        if not t:
            return self._report(a, "rejected", f"{pid}/{tid} is not on this coordinator's queue")
        exp = a.get("expected") or {}
        fn = getattr(self, f"_do_{a['kind']}", None)
        if fn is None:
            return self._report(a, "rejected", f"unsupported action {a['kind']!r}")
        actor = f"{a.get('actor') or 'operator'} (control center)"
        if a["kind"] not in ("answer_question", "reply_permission", "instruction",
                             "withdraw_instruction", "lanes", "decision_answer", *LEAD_KINDS) \
                and exp.get("task_state") \
                and exp["task_state"] != t["state"]:
            return self._report(a, "rejected", f"{tid} is now {t['state']}, not "
                                               f"{exp['task_state']} as the operator saw it")
        return fn(a, t, actor)

    def _assistant_action(self, a, actor):
        from .assistant import Assistant
        if not hasattr(self, "_assistant"):
            self._assistant = Assistant(self.c)
        P = self.c.projects[a["project_id"]]
        p = a.get("payload") or {}
        try:
            if a["kind"] == "assistant_plan":
                plan = self._assistant.plan(P, str(p.get("text") or ""), actor)
                return self._report(a, "applied", f"plan {plan['plan_id']}: "
                                    f"{len(plan['steps'])} step(s) proposed",
                                    {k: plan[k] for k in ("plan_id", "summary", "steps",
                                                          "warnings", "questions",
                                                          "served_model")})
            plan = self._assistant.run(P, str(p.get("plan_id") or ""), p.get("steps"), actor)
            failed = [r for r in plan["results"] if r["result"] != "done"]
            return self._report(a, "failed" if failed else "applied",
                                f"ran {len(plan['results']) - len(failed)} of "
                                f"{len(plan['results'])} step(s)",
                                {"plan_id": plan["plan_id"], "results": plan["results"],
                                 "warnings": plan.get("run_warnings") or []})
        except Exception as e:                                   # noqa: BLE001
            return self._report(a, "rejected", f"launch assistant: {e}"[:500])

    # ------------------------------------------------------------------ tracks

    def _tracks_status(self) -> dict:
        tm = getattr(self.c, "tracks", None)
        if tm is None:
            return {}
        out = {}
        for pid, P in sorted(self.c.projects.items()):
            try:
                st = tm.status(P)
                st["studio"]["journal"] = (st["studio"].get("journal") or [])[-40:]
                out[pid] = st
            except Exception as e:                               # noqa: BLE001
                out[pid] = {"error": f"{type(e).__name__}: {e}"[:200]}
        return out

    def _track_action(self, a, actor):
        from .tracks.manager import TrackError
        tm = getattr(self.c, "tracks", None)
        if tm is None:
            return self._report(a, "rejected", "tracks are not available in this coordinator")
        P = self.c.projects[a["project_id"]]
        p = a.get("payload") or {}
        kind = a["kind"]
        try:
            if kind == "tracks_analyze":
                an = tm.analyze(P)
                return self._report(a, "applied", f"{len(an['tracks'])} track(s), "
                                    f"{len(an['serial'])} serial pair(s)",
                                    {"tracks": an["tracks"], "serial": an["serial"][:40],
                                     "warnings": an["warnings"][:20], "done": an["done"],
                                     "open": an["open"], "max_agents": tm.max_agents(),
                                     "sprints": {k: {"needs": v["needs"],
                                                     "needs_source": v["needs_source"],
                                                     "deps": v["deps"], "files": len(v["files"])}
                                                 for k, v in an["sprints"].items()}})
            if kind == "tracks_propose":
                plan = tm.propose(P, p.get("max_agents"), actor=actor)
                return self._report(a, "applied", f"proposal {plan['plan_id']}: "
                                    f"{plan['agents']} agent(s), {len(plan['tracks'])} parallel "
                                    f"track(s)", plan)
            if kind == "track_start":
                if p.get("plan_id"):
                    rows = tm.start_plan(P, str(p["plan_id"]), actor, p.get("names") or None)
                else:
                    tm.create(P, str(p.get("name") or ""), list(p.get("sprints") or []),
                              p.get("needs"), actor)
                    rows = [tm.start(P, str(p.get("name") or "").strip().lower(), actor)]
                return self._report(a, "applied", "started " + ", ".join(
                    f"{r['name']} ({' -> '.join(r['sprints'])})" for r in rows),
                    {"tracks": [{k: r[k] for k in ("name", "state", "branch", "worktree",
                                                   "sprints", "needs")} for r in rows]})
            if kind == "track_stop":
                r = tm.stop(P, str(p.get("name") or ""), actor, str(p.get("reason") or ""))
                return self._report(a, "applied", f"track {r['name']} stopped",
                                    {"name": r["name"], "state": r["state"]})
            if kind == "track_merge":
                name = str(p.get("name") or "")
                if p.get("option"):
                    r = tm.merger.decide(P, name, str(p["option"]), actor)
                    return self._report(a, "applied", f"track {name}: {p['option']}",
                                        {"name": name, "state": r["state"]})
                r = tm.merger.request(P, name, actor)
                return self._report(a, "applied", f"track {name} merges at the main sprint's "
                                    f"next quiet point", {"name": name, "state": r["state"]})
            if kind == "track_status":
                return self._report(a, "applied", "ok", tm.status(P))
            if kind == "set_max_agents":
                n = tm.set_max_agents(P, p.get("max_agents"), actor)
                return self._report(a, "applied", f"max_agents is {n}", {"max_agents": n})
        except TrackError as e:
            return self._report(a, "rejected", str(e)[:500])
        return self._report(a, "rejected", f"unsupported track action {kind!r}")

    # ------------------------------------------------------------------ queue

    def _queue_action(self, a, actor):
        """queue_approve / queue_reorder / queue_deps / queue_requeue: edits of
        the approved queue through the engine's own methods. queue_approve may
        name a task the bridge hasn't reported yet (a planned sprint), so the
        task lookup is per kind, not the shared one in handle()."""
        pid, tid = a["project_id"], a.get("task_id") or ""
        p = a.get("payload") or {}
        kind = a["kind"]
        if not tid:
            return self._report(a, "rejected", "a queue action names a task")
        t = self.store.task(pid, tid)
        exp = (a.get("expected") or {}).get("task_state")
        if t is None and kind != "queue_approve":
            return self._report(a, "rejected", f"{pid}/{tid} is not on this coordinator's queue")
        if t is not None and exp and exp != t["state"]:
            return self._report(a, "rejected", f"{tid} is now {t['state']}, not {exp} as the "
                                               f"operator saw it")
        try:
            if kind == "queue_approve":
                deps = [str(d) for d in (p.get("deps") or [])]
                if self.c._deps_cycle(pid, tid, deps):
                    raise ValueError("those dependencies make a loop")
                entry = self.c.approve(pid, tid, deps, actor, note=str(p.get("note") or ""),
                                       track=str(p.get("track") or ""))
                return self._report(a, "applied", f"{tid} approved at position "
                                                  f"{entry['position']}",
                                    {"position": entry["position"], "deps": entry["deps"]})
            if kind == "queue_reorder":
                pos = int(p.get("position") or 0)
                if pos < 1:
                    return self._report(a, "rejected", "position must be 1 or higher")
                self.c.move(pid, tid, pos, actor)
                after = self.store.queue_entry(pid, tid) or {}
                return self._report(a, "applied", f"{tid} moved to position "
                                                  f"{after.get('position', pos)}",
                                    {"position": after.get("position", pos)})
            if kind == "queue_deps":
                entry = self.c.set_deps(pid, tid, [str(d) for d in (p.get("deps") or [])], actor)
                return self._report(a, "applied", f"{tid} now depends on "
                                                  f"{', '.join(entry['deps']) or 'nothing'}",
                                    {"deps": entry["deps"]})
            # queue_requeue: a CANCELLED task back on the queue (reopen keeps its
            # history, resets counters); COMPLETE is refused by reopen itself —
            # passed work is never rerun
            deps = list((self.store.queue_entry(pid, tid) or {}).get("deps") or [])
            entry = self.c.reopen(pid, tid, deps, actor, reason=str(p.get("reason") or ""))
            return self._report(a, "applied", f"{tid} reopened at position {entry['position']}",
                                {"position": entry["position"]})
        except ValueError as e:
            return self._report(a, "rejected", str(e)[:500])

    def _do_answer_question(self, a, t, actor):
        return self._answer(a, t, actor, "question", (a.get("payload") or {}).get("answers") or [])

    def _do_reply_permission(self, a, t, actor):
        reply = (a.get("payload") or {}).get("reply")
        if reply not in ("once", "reject"):
            return self._report(a, "rejected", "only 'once' or 'reject' replies are accepted")
        return self._answer(a, t, actor, "permission", [reply])

    def _answer(self, a, t, actor, kind, answers):
        pid, tid = a["project_id"], a["task_id"]
        rid, sid = a.get("request_id") or "", a.get("session_id") or ""
        q = (t.get("data") or {}).get("question") or {}
        P = self.c.projects[pid]
        if t["state"] == S.AWAITING_HUMAN and q.get("request_id") == rid:
            if q.get("kind", "question") != kind:
                return self._report(a, "rejected", f"request {rid} is a {q.get('kind')}, not a {kind}")
            if sid and t["session_id"] != sid:
                return self._report(a, "rejected", "the task's session changed since the request "
                                                   "was shown; the reply was not sent")
            self._waiting.pop(a["action_id"], None)
            self.c.answer(pid, tid, answers, actor=actor)
            after = self.store.task(pid, tid)
            return self._report(a, "applied", f"reply delivered to Kilo for session {sid or t['session_id']}; "
                                              f"the coordinator moved {tid} to {after['state']} and "
                                              f"polls that session again",
                                {"session_id": t["session_id"], "task_state": after["state"]})
        # a helper (child) session's request: the engine doesn't hold it as the
        # task's question; reply through the same supported Kilo path
        helper = next((s for s in self.store.sessions(pid, tid) if s["session_id"] == sid
                       and s["role"] == "helper"), None)
        if helper and t["state"] in S.WRITING:
            if (helper.get("data") or {}).get("parent") != t["session_id"]:
                return self._report(a, "rejected", "that helper belongs to an earlier session")
            try:
                P.client.answer(sid, rid, answers, directory=P.profile.workspace, kind=kind)
            except Exception as e:                               # noqa: BLE001
                return self._report(a, "failed", f"Kilo refused the reply: {e}"[:500])
            self.store.event(pid, tid, "operator_reply", session_id=sid,
                             detail={"request_id": rid, "kind": kind, "actor": actor})
            return self._report(a, "applied", f"reply delivered to helper session {sid}",
                                {"session_id": sid, "task_state": t["state"]})
        # the bridge saw the request before the engine recorded it: wait briefly
        seen = a["action_id"] in self._waiting
        first = self._waiting.setdefault(a["action_id"], self.clock())
        if t["state"] in S.WRITING and self.clock() - first < RETRY_UNRECORDED_S:
            if not seen:
                self._report(a, "queued", "the coordinator hasn't recorded this request yet; "
                                          "it will be sent once it does")
            return None
        self._waiting.pop(a["action_id"], None)
        cur = q.get("request_id")
        return self._report(a, "rejected", "that request is no longer the task's open question"
                            + (f" (current: {cur})" if cur else "") + "; nothing was sent",
                            {"current_request": cur})

    def _approval_check(self, a, t):
        pid, tid = a["project_id"], a["task_id"]
        p = a.get("payload") or {}
        kind = p.get("approval_kind")
        data = t.get("data") or {}
        if t["state"] != S.AWAITING_HUMAN or kind not in (data.get("awaiting_kinds") or []):
            return f"{tid} is not waiting for a {kind!r} approval"
        P = self.c.projects[pid]
        spec = P.adapter.describe(tid)
        rule = next((r for r in P.profile.approvals if r.kind == kind), None)
        if rule is None:
            return f"no approval rule {kind!r} in the profile"
        revision = t["reviewed_sha"] if data.get("stage") == "before_close" else t["head_sha"]
        arts = P.adapter.approval_artifacts(spec, rule.artifacts) if rule.artifacts else {}
        if p.get("revision") != revision:
            return (f"revision changed: the card showed {str(p.get('revision'))[:12]}, the task is "
                    f"at {str(revision)[:12]}")
        if (p.get("artifacts") or {}) != arts:
            changed = sorted(set(arts) ^ set(p.get("artifacts") or {}) | {
                k for k in arts if (p.get("artifacts") or {}).get(k) != arts[k]})
            return "evidence changed since the card was opened: " + ", ".join(changed[:8])
        return None

    def _do_approve_review(self, a, t, actor):
        why = self._approval_check(a, t)
        if why:
            return self._report(a, "rejected", why)
        p = a["payload"]
        aid = self.c.approve_human(a["project_id"], a["task_id"], p["approval_kind"], actor,
                                   p.get("note") or "")
        return self._report(a, "applied", f"approval {aid} recorded for revision "
                                          f"{str(p['revision'])[:12]}; the engine advances on its "
                                          f"next tick", {"approval_id": aid})

    def _do_reject_review(self, a, t, actor):
        why = self._approval_check(a, t)
        if why:
            return self._report(a, "rejected", why)
        p = a["payload"]
        label = "requested changes" if p.get("mode") == "request_changes" else "rejected"
        findings = [f"[{label} {p['approval_kind']} @ {str(p['revision'])[:12]}] {f}"
                    for f in p.get("findings") or []]
        after = self.c.reject_human(a["project_id"], a["task_id"], findings, actor)
        return self._report(a, "applied", f"{label}; {a['task_id']} is now {after['state']} with "
                                          f"{len(findings)} finding(s) for the repair session",
                            {"task_state": after["state"]})

    def _do_pause(self, a, t, actor):
        if t["state"] in S.TERMINAL or t["state"] == S.PAUSED:
            return self._report(a, "rejected", f"{a['task_id']} is {t['state']}")
        after = self.c.pause(a["project_id"], a["task_id"],
                             (a.get("payload") or {}).get("reason") or "operator", actor=actor)
        return self._report(a, "applied", f"paused (was {t['state']}); its session was stopped",
                            {"task_state": after["state"]})

    def _do_resume(self, a, t, actor):
        if t["state"] != S.PAUSED:
            return self._report(a, "rejected", f"{a['task_id']} is {t['state']}, not PAUSED")
        if self._project_paused(a["project_id"]):
            return self._report(a, "rejected", f"project {a['project_id']} is paused; nothing new "
                                               f"starts until it is resumed (project_resume)")
        after = self.c.resume(a["project_id"], a["task_id"], actor=actor)
        return self._report(a, "applied", f"resumed to {after['state']}",
                            {"task_state": after["state"], "session_id": after.get("session_id")})

    # ------------------------------------------------------------------ project pause

    def _project_paused(self, pid: str) -> bool:
        proj = self.store.project(pid)
        return bool(proj and proj["paused"])

    def _pause_status(self, pid: str) -> dict:
        from .telemetry import project_pause_status
        return project_pause_status(self.store.q, pid, self.clock())

    def _set_pause_meta(self, pid: str, meta: Optional[dict]) -> None:
        from .telemetry import PROJECT_PAUSE_META
        if meta is None:
            self.store.x("DELETE FROM meta WHERE key=?", (PROJECT_PAUSE_META + pid,))
        else:
            self.store.set_meta(PROJECT_PAUSE_META + pid, json.dumps(meta, sort_keys=True))

    def _do_project_pause(self, a, actor):
        """Pause the whole project for an operator upgrade. "safe": nothing
        new starts (task, session, prompt to a primary, review call, helper or
        lane dispatch); a running model turn finishes. "now": the same, and
        every task in a writing state is paused through the engine (its session
        aborted, its state kept) and the project's running helpers are stopped."""
        from .telemetry import PROJECT_UPGRADE_RESUME, project_pause_meta
        pid = a["project_id"]
        p = a.get("payload") or {}
        mode = p.get("mode") or "safe"
        if mode not in ("safe", "now"):
            return self._report(a, "rejected", f"mode must be 'safe' or 'now', not {mode!r}")
        note = str(p.get("note") or "")[:500]
        if self.store.project(pid) is None:
            return self._report(a, "rejected", f"{pid} is not registered in this coordinator's "
                                               f"store")
        was = self._project_paused(pid)
        prev = project_pause_meta(self.store.q, pid) if was else {}
        meta = {"mode": "now" if "now" in (mode, prev.get("mode")) else "safe",
                "note": note or prev.get("note") or "",
                "actor": prev.get("actor") or actor,
                "since": prev.get("since") or self.clock(),
                "tasks": list(prev.get("tasks") or []),
                # opt-in: resume by itself when the coordinator next starts on a
                # different build than this one (the operator's upgrade)
                "resume_after_upgrade": bool(p.get("resume_after_upgrade") is True
                                             or prev.get("resume_after_upgrade")),
                "version": prev.get("version") or build_identity()}
        # the record first, then the flag the engine checks: a crash in between
        # leaves a record without a pause, which a resume (or a new pause) clears
        self._set_pause_meta(pid, meta)
        if not was:
            # a new pause: the previous "resumed after upgrade" report is history
            self.store.x("DELETE FROM meta WHERE key=?", (PROJECT_UPGRADE_RESUME + pid,))
        self.store.set_project_paused(pid, True, meta["note"])
        paused_now = []
        if mode == "now":
            P = self.c.projects[pid]
            why = f"project paused now by {actor}" + (f": {meta['note']}" if meta["note"] else "")
            for t in self.store.tasks(pid):
                if t["state"] in S.WRITING:
                    self.c.pause(pid, t["task_id"], why[:300], actor=actor)
                    # only these are resumed by project_resume
                    self.store.merge_data(pid, t["task_id"], paused_by_project=True)
                    paused_now.append(t["task_id"])
                    meta["tasks"] = sorted(set(meta["tasks"]) | {t["task_id"]})
                    self._set_pause_meta(pid, meta)
                elif t["state"] not in S.TERMINAL:
                    self.c.helpers.abort_running(P, t["task_id"], "cancelled",
                                                 f"project paused now: {why}"[:300])
        status = self._pause_status(pid)
        detail = (f"project {pid} paused ({meta['mode']}"
                  + (", already paused" if was else "") + "): nothing new starts"
                  + (f"; paused {len(paused_now)} running task(s): {', '.join(paused_now)}"
                     if paused_now else "; running model turns finish")
                  + ("; safe to upgrade" if status["safe_to_upgrade"] else
                     f"; not yet safe to upgrade ({status['busy_sessions']} session(s), "
                     f"{status['running_helpers']} helper(s), {status['running_reviews']} "
                     f"review(s) still running)"))
        return self._report(a, "applied", detail, {"project_pause": status,
                                                   "paused_tasks": paused_now})

    def _do_project_resume(self, a, actor):
        """Resume the project: the engine starts new work again, and only the
        tasks this project pause paused (paused_by_project) are resumed, each
        continuing its own session. A task an operator paused by itself stays
        paused."""
        from .telemetry import project_pause_meta
        pid = a["project_id"]
        proj = self.store.project(pid)
        meta = project_pause_meta(self.store.q, pid)
        if proj is None or (not proj["paused"] and not meta
                            and not any((t["data"] or {}).get("paused_by_project")
                                        for t in self.store.tasks(pid))):
            return self._report(a, "rejected", f"project {pid} is not paused")
        resumed, left, failed = self._project_resume(pid, actor, f"project resumed by {actor}")
        detail = (f"project {pid} resumed; new work starts again"
                  + (f"; resumed {', '.join(resumed)}" if resumed else "")
                  + (f"; left paused (paused individually): {', '.join(left)}" if left else "")
                  + (f"; could not resume: {'; '.join(failed)}" if failed else ""))
        return self._report(a, "applied", detail[:1500], {
            "resumed": resumed, "left_paused": left, "failed": failed,
            "project_pause": self._pause_status(pid)})

    def _project_resume(self, pid: str, actor: str, note: str) -> tuple[list, list, list]:
        """The one resume both the operator's action and the after-upgrade
        resume use: clear the flag, resume only the tasks the project pause
        paused, drop the pause record. (resumed, left_paused, failed)."""
        self.store.set_project_paused(pid, False)
        resumed, failed, left = [], [], []
        for t in self.store.tasks(pid):
            tid = t["task_id"]
            if not (t["data"] or {}).get("paused_by_project"):
                if t["state"] == S.PAUSED:
                    left.append(tid)
                continue
            if t["state"] != S.PAUSED:          # resumed or moved meanwhile: only the mark goes
                self.store.merge_data(pid, tid, paused_by_project=None)
                continue
            try:
                after = self.c.resume(pid, tid, actor=actor, note=note)
            except Exception as e:                               # noqa: BLE001
                failed.append(f"{tid}: {type(e).__name__}: {e}"[:300])
                continue
            self.store.merge_data(pid, tid, paused_by_project=None)
            resumed.append(f"{tid} -> {after['state']}")
        self._set_pause_meta(pid, None)
        return resumed, left, failed

    def _resume_after_upgrade(self) -> None:
        """At startup (the first poll): a project paused with
        resume_after_upgrade whose recorded build differs from the one now
        running is resumed exactly as project_resume would, on the original
        operator's authority. The same build (a plain restart), no recorded
        build, or a pause without the option: it stays paused. The outcome is
        kept in the store (meta project_upgrade_resume:<pid>) and reported in
        the pause status, so the heartbeat and the bridge both carry it, and it
        is recorded as an event."""
        from .telemetry import PROJECT_UPGRADE_RESUME, project_pause_meta
        cur = build_identity()
        for pid in sorted(self.c.projects):
            if not self._project_paused(pid):
                continue
            meta = project_pause_meta(self.store.q, pid)
            if not meta.get("resume_after_upgrade"):
                continue
            rec = meta.get("version") if isinstance(meta.get("version"), dict) else {}
            if not upgraded(rec, cur):
                self.log(f"control: {pid} stays paused: same build as at the pause "
                         f"(v{cur.get('version') or '?'})")
                continue
            by = meta.get("actor") or "operator"
            actor = f"coordinator (after upgrade, authorized by {by})"
            frm, to = rec.get("version") or "", cur.get("version") or ""
            resumed, left, failed = self._project_resume(
                pid, actor, f"project resumed after upgrade to v{to or '?'} (authorized by {by})")
            rep = {"ts": self.clock(), "actor": actor, "authorized_by": by,
                   "from_version": frm, "to_version": to,
                   "from_fingerprint": rec.get("fingerprint") or "",
                   "to_fingerprint": cur.get("fingerprint") or "",
                   "note": meta.get("note") or "", "resumed": resumed[:50],
                   "left_paused": left[:50], "failed": failed[:20]}
            self.store.set_meta(PROJECT_UPGRADE_RESUME + pid, json.dumps(rep, sort_keys=True))
            self.store.event(pid, "", "project_resumed_after_upgrade", detail=rep)
            self.log(f"control: {pid} resumed after upgrade to v{to or '?'} "
                     f"(authorized by {by}); resumed {len(resumed)} task(s)")

    def _do_cancel(self, a, t, actor):
        if t["state"] in S.TERMINAL:
            return self._report(a, "rejected", f"{a['task_id']} is {t['state']}")
        after = self.c.cancel(a["project_id"], a["task_id"],
                              (a.get("payload") or {}).get("reason") or "operator", actor=actor)
        return self._report(a, "applied", "cancelled; locks released", {"task_state": after["state"]})

    def _do_unblock(self, a, t, actor):
        if t["state"] not in (S.BLOCKED, S.FAILED):
            return self._report(a, "rejected", f"{a['task_id']} is {t['state']}")
        p = a.get("payload") or {}
        after = self.c.unblock(a["project_id"], a["task_id"], p.get("resolution") or "",
                               reset_attempts=bool(p.get("reset_attempts")), actor=actor)
        return self._report(a, "applied", "back to READY", {"task_state": after["state"]})

    def _do_decision_answer(self, a, t, actor):
        """An operator's answer to an open coordinator decision (obligations.
        decisions). Authority is always the operator; the engine refuses a
        model/session actor, a non-open decision, a contradicting answer, or a
        question that changed since the operator read it."""
        from .obligations import DecisionConflict, DecisionRefused
        p = a.get("payload") or {}
        if p.get("authority") not in (None, "", "operator"):
            return self._report(a, "rejected", "decisions are answered from the control center "
                                               "with operator authority only")
        did = str(p.get("decision_id") or "")
        if not did:
            return self._report(a, "rejected", "no decision_id given")
        try:
            res = self.c.decision_answer(a["project_id"], a["task_id"], did,
                                         p.get("answer"), option=p.get("option") or "",
                                         actor=actor, note=p.get("note") or "",
                                         question_hash=p.get("question_hash") or "",
                                         channel="control")
        except (DecisionRefused, DecisionConflict) as e:
            return self._report(a, "rejected", f"{type(e).__name__}: {e}"[:500],
                                {"decision_id": did})
        except ValueError as e:
            return self._report(a, "rejected", str(e)[:500], {"decision_id": did})
        return self._report(a, "applied", f"decision {did} answered by {actor}; the engine applies "
                                          f"it on its next tick",
                            {"decision_id": did, "status": res.get("status"),
                             "task_state": (self.store.task(a["project_id"], a["task_id"]) or {}
                                            ).get("state")})

    # ------------------------------------------------------------------ lead recovery cases

    def _lead_case_row(self, case_id: str) -> Optional[dict]:
        """The case row when the table exists (None: no table or no such case)."""
        try:
            r = self.store.q("SELECT case_id, project_id, task_id, state FROM recovery_cases "
                             "WHERE case_id=?", (case_id,))
        except Exception:                                        # noqa: BLE001
            return None
        return r[0] if r else None

    def _lead_case(self, a, t, actor, method: str, verb: str, extra: Optional[dict] = None):
        """pause / resume / take over / instruct one recovery case through engine.lead.
        The case must belong to the action's task (project isolation); the
        LeadManager itself refuses a case it doesn't hold."""
        lead = self._lead()
        fn = getattr(lead, method, None) if lead is not None else None
        if not callable(fn) or any(not _accepts(fn, k) for k in (extra or {})):
            return self._report(a, "rejected", LEAD_UNAVAILABLE)
        p = a.get("payload") or {}
        cid = str(p.get("case_id") or "")
        note = str(p.get("note") or "")
        if not cid:
            return self._report(a, "rejected", "no case_id given")
        if len(note) > MAX_LEAD_NOTE:
            return self._report(a, "rejected", f"note is over {MAX_LEAD_NOTE} characters")
        if method == "instruct" and not note.strip():
            return self._report(a, "rejected", "an instruction needs text (note)")
        row = self._lead_case_row(cid)
        if row and (row.get("project_id") != a["project_id"] or row.get("task_id") != a["task_id"]):
            return self._report(a, "rejected", f"case {cid} belongs to another task",
                                {"case_id": cid})
        try:
            if method == "instruct":
                res = fn(cid, note, actor)
            else:
                kw = dict(extra or {})
                if note and _accepts(fn, "note"):
                    kw["note"] = note
                if _accepts(fn, "P"):
                    # takeover stops the case's lead / repair sessions in the project
                    kw["P"] = self.c.projects[a["project_id"]]
                res = fn(cid, actor, **kw)
        except (KeyError, LookupError, ValueError, PermissionError) as e:
            return self._report(a, "rejected", f"{type(e).__name__}: {e}"[:500], {"case_id": cid})
        res = res if isinstance(res, dict) else {}
        after = self._lead_case_row(cid) or {}
        state = res.get("state") or after.get("state")
        return self._report(a, "applied", f"case {cid} {verb} by {actor}"
                            + (f"; case is {state}" if state else ""),
                            {"case_id": cid, "case_state": state,
                             "task_state": (self.store.task(a["project_id"], a["task_id"]) or {}
                                            ).get("state")})

    def _do_lead_case_pause(self, a, t, actor):
        return self._lead_case(a, t, actor, "pause_case", "paused")

    def _do_lead_case_resume(self, a, t, actor):
        """Unpause (also ends a takeover): the case continues where it was; the
        paused time does not count toward max_case_minutes."""
        return self._lead_case(a, t, actor, "pause_case", "resumed", extra={"paused": False})

    def _do_lead_case_takeover(self, a, t, actor):
        return self._lead_case(a, t, actor, "takeover", "taken over (the task stays BLOCKED for "
                                                        "the operator)")

    def _do_lead_case_instruct(self, a, t, actor):
        return self._lead_case(a, t, actor, "instruct", "given an operator instruction")

    def _do_lanes(self, a, t, actor):
        """Operational kill switch for proactive helper lanes of this project.
        It can only stop new lane work (running units finish; their results
        are still shown). Turning lanes ON stays a profile change."""
        p = a.get("payload") or {}
        paused = bool(p.get("paused", True))
        self.c.helpers.lanes.set_paused(a["project_id"], paused, actor, p.get("note") or "")
        return self._report(a, "applied", ("proactive lanes paused for " if paused else
                                           "proactive lanes un-paused (profile settings apply) "
                                           "for ") + a["project_id"], {"lanes_paused": paused})

    def _do_instruction(self, a, t, actor):
        p = a.get("payload") or {}
        pid, tid = a["project_id"], a["task_id"]
        delivery, scope = p.get("delivery") or "next_session", p.get("scope") or "task"
        sid = (a.get("expected") or {}).get("session_id") or ""
        if t["state"] in S.TERMINAL:
            return self._report(a, "rejected", f"{tid} is {t['state']}")
        if delivery == "pause_and_steer":
            if self._project_paused(pid):
                return self._report(a, "rejected", f"project {pid} is paused: the steer would "
                                                   f"send a new prompt; queue it for the next "
                                                   f"session instead, or resume the project")
            if t["state"] not in S.WRITING or not t["session_id"]:
                return self._report(a, "rejected", f"no running worker to steer ({tid} is "
                                                   f"{t['state']}); nothing was sent")
            if sid and sid != t["session_id"]:
                return self._report(a, "rejected", "the worker session changed since you wrote "
                                                   "this; it was not sent to the new one")
        self.store.add_operator_message(a["action_id"], pid, tid,
                                        t["session_id"] if scope == "session" or
                                        delivery == "pause_and_steer" else "",
                                        scope, delivery, a.get("actor") or "operator", p.get("text") or "")
        pb = getattr(self.c, "playbook", None)
        if pb is not None:
            # A1: the third repeat of the same normalized operator instruction
            # proposes a howto/ playbook entry (deterministic; inert unless
            # [automation] playbook_promote is on)
            try:
                pb.promote.observe_instruction(self.c.projects[pid], tid, p.get("text") or "",
                                               a.get("actor") or "operator")
            except Exception as e:                               # noqa: BLE001
                self.log(f"control: playbook observe_instruction: {type(e).__name__}: {e}")
        if delivery == "next_session":
            return self._report(a, "queued", f"queued: added to the next session prompt the "
                                             f"coordinator sends for {tid} (implementation, repair, "
                                             f"closeout or continue); the running worker is not "
                                             f"interrupted")
        # explicit pause-and-steer: stop the current turn, then continue the same
        # session with the instruction (the engine's own pause/resume)
        self.c.pause(pid, tid, f"operator pause-and-steer ({actor})", actor=actor)
        after = self.c.resume(pid, tid, actor=actor, note="continue with operator instruction")
        m = self.store.operator_message(a["action_id"]) or {}
        if m.get("status") == "delivered":
            self.store.set_operator_reported(m["msg_id"], "delivered")
            return self._report(a, "delivered", f"sent to session {m.get('delivered_session')} "
                                                f"in its continue prompt",
                                {"session_id": m.get("delivered_session"),
                                 "task_state": after["state"]})
        return self._report(a, "queued", f"the session could not be continued ({after['state']}); "
                                         f"{'a task-scoped instruction goes with the next session' if scope == 'task' else 'session-specific: not moved to another session'}",
                            {"task_state": after["state"]})

    def _do_withdraw_instruction(self, a, t, actor):
        target = (a.get("payload") or {}).get("target_action_id") or ""
        m = self.store.operator_message(target)
        if not m or m["project_id"] != a["project_id"]:
            return self._report(a, "rejected", "no such queued instruction here")
        if m["status"] != "queued":
            return self._report(a, "rejected", f"instruction is {m['status']}; too late to withdraw")
        self.store.mark_operator_messages([m["msg_id"]], "withdrawn", "", f"withdrawn by {actor}")
        try:
            self.foundry.action_status(target, self.source_id, "rejected",
                                       f"withdrawn by {actor} before delivery")
        except FoundryError:
            pass
        self.store.set_operator_reported(m["msg_id"], "rejected")
        return self._report(a, "applied", f"instruction {target} withdrawn before delivery")

    # ------------------------------------------------------------------ instruction outcomes

    # revamp B13: a terminal "unknown action" answer from the control plane retires
    # the action id; before, its report was retried every poll (28k log lines)
    RETIRED = "retired"
    ANSWER_CHECK_SECONDS = 60

    @staticmethod
    def _unknown_action(e: Exception) -> bool:
        text = str(e)
        return "HTTP 404" in text or "HTTP 410" in text

    def report_messages(self) -> None:
        checked = self.__dict__.setdefault("_answer_checked", {})
        for m in self.store.operator_messages():
            if str(m.get("action_id") or "").startswith("internal:"):
                continue              # the coordinator's own (unblock resolution): no action
            done = m.get("reported") or ""
            if done == self.RETIRED:
                continue
            if m["status"] == "delivered" and done == "delivered":
                # the transcript read behind _answered_since is not repeated every poll
                # (every poll for the first two minutes, then once a minute)
                last = checked.get(m["msg_id"], 0.0)
                young = self.clock() - float(m.get("delivered_ts") or 0) < 120
                if not young and self.clock() - last < self.ANSWER_CHECK_SECONDS:
                    continue
                checked[m["msg_id"]] = self.clock()
            try:
                if m["status"] == "delivered" and done in ("", "queued"):
                    self.foundry.action_status(m["action_id"], self.source_id, "delivered",
                                               f"sent to session {m['delivered_session']} in the "
                                               f"prompt Kilo accepted",
                                               {"session_id": m["delivered_session"]})
                    self.store.set_operator_reported(m["msg_id"], "delivered")
                elif m["status"] == "delivered" and done == "delivered":
                    if self._answered_since(m):
                        self.foundry.action_status(m["action_id"], self.source_id, "applied",
                                                   f"session {m['delivered_session']} replied after "
                                                   f"receiving it", {"session_id": m["delivered_session"]})
                        self.store.set_operator_reported(m["msg_id"], "applied")
                elif m["status"] == "rejected" and done not in ("rejected",):
                    self.foundry.action_status(m["action_id"], self.source_id, "rejected",
                                               m.get("detail") or "not deliverable")
                    self.store.set_operator_reported(m["msg_id"], "rejected")
                elif m["status"] == "delivering" and self.clock() - (m["created_ts"] or 0) > 600 \
                        and done != "uncertain":
                    self.foundry.action_status(m["action_id"], self.source_id, "failed",
                                               "outcome uncertain: the coordinator stopped while "
                                               "sending the prompt that carried it")
                    self.store.set_operator_reported(m["msg_id"], "uncertain")
            except FoundryError as e:
                if self._unknown_action(e):
                    # the control plane does not know this action: retire it once
                    self.store.set_operator_reported(m["msg_id"], self.RETIRED)
                    self.store.event(m.get("project_id") or "", m.get("task_id") or "",
                                     "control_action_retired",
                                     detail={"action_id": m["action_id"],
                                             "error": str(e)[:300]})
                self.log(f"control: report {m['action_id']}: {e}"
                         + (" (retired: not retried)" if self._unknown_action(e) else ""))

    def _answered_since(self, m: dict) -> bool:
        P = self.c.projects.get(m["project_id"])
        if P is None or not m.get("delivered_session"):
            return False
        try:
            msgs = P.client.transcript(m["delivered_session"], directory=P.profile.workspace) or []
        except Exception:                                        # noqa: BLE001
            return False
        for x in msgs:
            info = x.get("info") or {}
            if info.get("role") == "assistant" and \
                    ((info.get("time") or {}).get("created") or 0) / 1000 >= (m["delivered_ts"] or 0):
                return True
        return False


def _accepts(fn, name: str) -> bool:
    import inspect
    try:
        ps = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in ps or any(p.kind == p.VAR_KEYWORD for p in ps.values())


def _git_head(root: str) -> str:
    """The commit a checkout is at, read from its .git files (no git process):
    "" when it is not a git checkout or the ref can't be read."""
    import os
    try:
        git = os.path.join(root, ".git")
        if os.path.isfile(git):                       # a worktree: "gitdir: <path>"
            with open(git, encoding="utf-8") as f:
                line = f.read().strip()
            if not line.startswith("gitdir:"):
                return ""
            git = os.path.normpath(os.path.join(root, line[7:].strip()))
        with open(os.path.join(git, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
        if not head.startswith("ref:"):
            return head[:40]
        ref = head[4:].strip()
        common = git
        cd = os.path.join(git, "commondir")
        if os.path.isfile(cd):
            with open(cd, encoding="utf-8") as f:
                common = os.path.normpath(os.path.join(git, f.read().strip()))
        for base in (git, common):
            p = os.path.join(base, *ref.split("/"))
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    return f.read().strip()[:40]
        packed = os.path.join(common, "packed-refs")
        if os.path.isfile(packed):
            with open(packed, encoding="utf-8") as f:
                for line in f:
                    parts = line.strip().split(" ")
                    if len(parts) == 2 and parts[1] == ref:
                        return parts[0][:40]
    except OSError:
        pass
    return ""


def build_identity() -> dict:
    """The build this coordinator process runs: the release version
    (foundry_router, bumped every release), the engine package version and,
    when the code is a git checkout, its HEAD commit. Recorded with a project
    pause; compared at startup for resume_after_upgrade."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return {"version": _version(), "coordinator_version": _coordinator_version(),
            "fingerprint": _git_head(root)}


def upgraded(recorded: dict, current: dict) -> bool:
    """True when the running build differs from the recorded one in a field
    both sides know. Nothing recorded (an older pause) is never an upgrade."""
    keys = ("version", "coordinator_version", "fingerprint")
    known = [k for k in keys if (recorded or {}).get(k) and (current or {}).get(k)]
    return bool(known) and any(str(recorded[k]) != str(current[k]) for k in known)


def _owner(store) -> Optional[dict]:
    import json
    try:
        return json.loads(store.meta("owner") or "null")
    except ValueError:
        return None


def _contract() -> str:
    """The control-center contract this coordinator speaks (the same string the
    telemetry bridge sends), so the server's version gate can say ok."""
    try:
        from .telemetry import CONTRACT
        return CONTRACT
    except Exception:                                            # noqa: BLE001
        return "foundry.cc.v1"


def _coordinator_version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:                                            # noqa: BLE001
        return ""


def _version() -> str:
    try:
        from foundry_router import __version__
        return __version__
    except Exception:                                            # noqa: BLE001
        return ""
