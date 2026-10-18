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

import socket
import time
from typing import Callable, Optional

from . import states as S
from .adapters.foundry import FoundryClient, FoundryError

RETRY_UNRECORDED_S = 120      # wait this long for the engine to record a Kilo question


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

    # ------------------------------------------------------------------ loop

    def poll(self) -> None:
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

    def _lifecycle_on(self) -> bool:
        """At least one project has [lifecycle] enabled."""
        lc = getattr(self.c, "_lc", None)
        if not callable(lc):
            return False
        return any(lc(P) for P in (getattr(self.c, "projects", None) or {}).values())

    def _beat(self) -> dict:
        tasks = self.store.tasks()
        return {"source": {"id": self.source_id, "kind": "coordinator", "host": socket.gethostname(),
                           "version": _version(), "capabilities": {
                               "actions": ["answer_question", "reply_permission", "approve_review",
                                           "reject_review", "pause", "resume", "cancel", "unblock",
                                           "instruction", "withdraw_instruction",
                                           "decision_answer"]}},
                "contract": _contract(), "coordinator_version": _coordinator_version(),
                # advertised only when some project actually runs the lifecycle
                "features": (["decision_answer", "lifecycle"] if self._lifecycle_on() else []),
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
        if not prior:
            self.store.op_begin(key, a["project_id"], a["task_id"], "control")
        pid, tid = a["project_id"], a["task_id"]
        if pid not in self.c.projects:
            return self._report(a, "rejected", f"{pid} is not run by this coordinator")
        t = self.store.task(pid, tid)
        if not t:
            return self._report(a, "rejected", f"{pid}/{tid} is not on this coordinator's queue")
        exp = a.get("expected") or {}
        fn = getattr(self, f"_do_{a['kind']}", None)
        if fn is None:
            return self._report(a, "rejected", f"unsupported action {a['kind']!r}")
        actor = f"{a.get('actor') or 'operator'} (control center)"
        if a["kind"] not in ("answer_question", "reply_permission", "instruction",
                             "withdraw_instruction", "lanes", "decision_answer") \
                and exp.get("task_state") \
                and exp["task_state"] != t["state"]:
            return self._report(a, "rejected", f"{tid} is now {t['state']}, not "
                                               f"{exp['task_state']} as the operator saw it")
        return fn(a, t, actor)

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
        after = self.c.resume(a["project_id"], a["task_id"], actor=actor)
        return self._report(a, "applied", f"resumed to {after['state']}",
                            {"task_state": after["state"], "session_id": after.get("session_id")})

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

    def report_messages(self) -> None:
        for m in self.store.operator_messages():
            if str(m.get("action_id") or "").startswith("internal:"):
                continue              # the coordinator's own (unblock resolution): no action
            done = m.get("reported") or ""
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
                self.log(f"control: report {m['action_id']}: {e}")

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
