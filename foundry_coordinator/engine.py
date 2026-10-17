"""The coordinator engine: a deterministic step function over durable state.

Each tick looks at every approved task (projects in round-robin order) and
takes at most the next safe step for it. A step either changes nothing
(waiting on a session, a lock, a person), or performs one operation and
records a transition with its evidence. Operations that must not repeat
(start a session, send its prompt, ask for a review, push) are keyed in the
store, so a restart resumes them instead of doing them twice.

Nothing here knows about a particular project: task text, scope, checks,
prompts, approvals and publishing come from the project's profile and
adapter.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Callable, Optional

from . import review as R
from . import states as S
from . import training_hook
from . import handoff
from .adapters import Project
from .adapters.base import FAIL, PASS, UNKNOWN, SessionState, VerificationResult
from .adapters.foundry import FoundryDenied, FoundryError
from .adapters.git import GitError
from .adapters.project import AdapterError, failed_findings
from .profile import matches_any
from .store import Store

REVIEW_ERRORS_BEFORE_BLOCK = 3
INVALID_REVIEWS_BEFORE_BLOCK = 2
ROUTE_RETRY_S = 300


class Coordinator:
    def __init__(self, store: Store, projects: list[Project], state_dir: str,
                 max_writers: int = 1, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda m: None):
        self.store = store
        self.projects = {p.id: p for p in projects}
        self.state_dir = state_dir
        self.max_writers = max(1, int(max_writers))
        self.clock = clock
        self.log = log
        # Called after every tick by the process that owns the engine (`run`):
        # the control-center channel (control.py) applies operator actions here,
        # so nothing but this process ever changes the engine's state.
        self.after_tick: list[Callable[[], None]] = []
        from .helpers import HelperManager
        # optional coding helpers (profile [helpers]); inert unless enabled
        self.helpers = HelperManager(self, clock=clock)
        from .frontier import FrontierPhase
        # optional frontier-primary phase (profile [frontier_primary]); off by default
        self.frontier = FrontierPhase(self, clock=clock)
        for p in projects:
            store.register_project(p.id, p.profile.name, p.profile.path, p.profile.hash,
                                   p.profile.version, os.path.realpath(p.profile.workspace))

    # ======================================================================= queue

    def approve(self, project_id: str, task_id: str, deps: list[str], approver: str,
                note: str = "", adopt_existing: Optional[list[str]] = None) -> dict:
        """Put a task on the approved queue. Dependencies must be tasks of the
        same project that are already approved (a dependency on another
        project's task is refused: projects are isolated)."""
        P = self._project(project_id)
        for d in deps:
            if ":" in d or "/" in d:
                raise ValueError(f"dependency {d!r} names another project; cross-project "
                                 f"dependencies aren't allowed")
            if d == task_id:
                raise ValueError("a task can't depend on itself")
            if not self.store.task(project_id, d):
                raise ValueError(f"dependency {d} is not on {project_id}'s approved queue")
        try:
            spec = P.adapter.describe(task_id)
        except AdapterError as e:
            raise ValueError(f"{project_id}/{task_id}: {e}") from None
        existing = self.store.task(project_id, task_id)
        if existing and existing['state'] != S.QUEUED:
            raise ValueError(f"{project_id}/{task_id} is already {existing['state']}")
        adopted = (handoff.capture(P.ws, spec, self.state_dir, adopt_existing, approver)
                   if adopt_existing else None)
        with self.store.tx():
            entry = self.store.approve(project_id, task_id, deps, approver, note)
            if adopted:
                self.store.merge_data(project_id, task_id, inherited_work=adopted)
                self.store.event(project_id, task_id, 'work_adopted', detail=adopted)
        return entry

    # ======================================================================= loop

    def tick(self) -> list[str]:
        acted: list[str] = []
        ids = sorted(self.projects)
        if not ids:
            return acted
        rr = int(self.store.meta("rr") or 0) % len(ids)
        order = ids[rr:] + ids[:rr]
        self.store.set_meta("rr", str(rr + 1))
        for pid in order:
            P = self.projects[pid]
            for t in self.store.tasks(pid):
                before = (t["state"], t["updated_ts"])
                try:
                    self._step(P, t)
                except Exception as e:                            # noqa: BLE001
                    self.store.update_task(pid, t["task_id"],
                                           next_action=f"error: {type(e).__name__}: {e}"[:500])
                    self.store.event(pid, t["task_id"], "error", detail={"error": str(e)[:500]})
                    self.log(f"{pid}/{t['task_id']}: {type(e).__name__}: {e}")
                after = self.store.task(pid, t["task_id"])
                if after and after["state"] != before[0]:
                    acted.append(f"{pid}/{t['task_id']}: {before[0]} -> {after['state']}")
                    # closeout finalizes capture itself; these exits are best effort
                    if after["state"] == S.FAILED or (after["state"] == S.COMPLETE
                                                      and before[0] == S.AWAITING_HUMAN):
                        self._training(P, after, after["state"])
        self.flush()
        return acted

    def run(self, poll_seconds: float = 5, max_ticks: Optional[int] = None,
            until_settled: bool = False, owner: str = "") -> None:
        n = 0
        while True:
            acted = self.tick()
            for a in acted:
                self.log(a)
            for hook in list(self.after_tick):
                try:
                    hook()
                except Exception as e:                            # noqa: BLE001
                    self.log(f"after-tick hook failed: {type(e).__name__}: {e}")
            if owner:
                self.store.heartbeat(owner)
            n += 1
            if max_ticks and n >= max_ticks:
                return
            if until_settled and self.settled():
                return
            time.sleep(poll_seconds)

    def settled(self) -> bool:
        """Nothing the engine can do right now: every task is complete, or
        waiting on a person, an operator, or a budget/route retry time (a
        long-running `run` without --until-settled picks those up itself)."""
        return not any(t["state"] in S.ACTIVE for t in self.store.tasks())

    # ======================================================================= steps

    def _step(self, P: Project, t: dict) -> None:
        st = t["state"]
        if st in S.TERMINAL or st in (S.BLOCKED, S.FAILED):
            return
        proj = self.store.project(P.id)
        if proj and proj["paused"] and st in (S.QUEUED, S.READY, S.CHANGES_REQUIRED):
            self._note(P, t, f"project paused: {proj.get('pause_note') or ''}")
            return
        handler = {
            S.QUEUED: self._queued, S.READY: self._start_implementation,
            S.CHANGES_REQUIRED: self._start_implementation, S.IMPLEMENTING: self._implementing,
            S.VERIFYING: self._verifying, S.REVIEWING: self._reviewing,
            S.APPROVED: self._approved, S.CLOSING: self._closing,
            S.AWAITING_HUMAN: self._awaiting, S.PAUSED: self._paused,
        }.get(st)
        if st == S.IMPLEMENTING:
            try:
                self.helpers.tick(P, t)
            except Exception as e:                                # noqa: BLE001
                self.store.event(P.id, t["task_id"], "warning",
                                 detail={"helpers": f"{type(e).__name__}: {e}"[:300]})
        if handler:
            handler(P, t)

    # -- QUEUED ----------------------------------------------------------------

    def _queued(self, P: Project, t: dict) -> None:
        q = self.store.queue_entry(P.id, t["task_id"]) or {"deps": []}
        waiting = []
        for d in q["deps"]:
            dt = self.store.task(P.id, d)
            if dt is None:
                return self._block(P, t, f"dependency {d} is not on the approved queue")
            if dt["state"] in (S.FAILED, S.CANCELLED):
                return self._block(P, t, f"dependency {d} is {dt['state']}")
            if not self._dep_satisfied(P, dt):
                waiting.append(f"{d} ({dt['state']})")
        if waiting:
            return self._note(P, t, "waiting for " + ", ".join(waiting))
        try:
            spec = P.adapter.describe(t["task_id"])
        except AdapterError as e:
            return self._block(P, t, f"task description unavailable: {e}")
        problem = P.adapter.preflight_task(spec)
        if problem:
            return self._block(P, t, problem)
        undeclared = [d for d in spec.deps if d not in q["deps"]
                      and not self._dep_satisfied(P, self.store.task(P.id, d) or {})
                      and not P.adapter.dependency_done(d)]
        if undeclared:
            return self._block(P, t, f"the task source says {t['task_id']} needs "
                                     f"{', '.join(undeclared)}, which is not approved/complete")
        self.store.transition(P.id, t["task_id"], S.READY,
                              {"deps_satisfied": q["deps"] or ["(none)"]})

    def _dep_satisfied(self, P: Project, dt: dict) -> bool:
        if not dt:
            return False
        if dt.get("state") == S.COMPLETE:
            return True
        if dt.get("state") == S.AWAITING_HUMAN and (dt.get("data") or {}).get("stage") == "after_close":
            kinds = (dt.get("data") or {}).get("awaiting_kinds") or []
            return bool(kinds) and all(k in P.profile.deps_accept_pending for k in kinds)
        return False

    # -- READY / CHANGES_REQUIRED -> IMPLEMENTING ---------------------------------

    def _start_implementation(self, P: Project, t: dict) -> None:
        pid, tid = P.id, t["task_id"]
        lim = P.profile
        free = bool(t["data"].get("free_retry"))
        attempt = t["attempt"] + (0 if free else 1)
        if attempt > lim.limit("max_attempts", 3):
            return self.store.transition(pid, tid, S.FAILED, {
                "reason": f"{t['attempt']} implementation attempts used "
                          f"(max_attempts {lim.limit('max_attempts', 3)})"})
        if t["sessions"] >= lim.limit("max_sessions_per_task", 8):
            return self.store.transition(pid, tid, S.FAILED, {
                "reason": f"{t['sessions']} sessions used (max_sessions_per_task)"})
        minutes = lim.limit("max_task_minutes", 0)
        if minutes and t["started_ts"] and self.clock() - t["started_ts"] > minutes * 60:
            return self.store.transition(pid, tid, S.FAILED, {
                "reason": f"over max_task_minutes ({minutes})"})
        try:
            spec = P.adapter.describe(tid)
        except AdapterError as e:
            return self._block(P, t, f"task description unavailable: {e}")
        held = self._acquire(P, t, self._workspace_locks(P) + self._session_locks(P),
                             writer=True)
        if held:
            return
        try:
            dirty = P.ws.status()
        except GitError as e:
            self.store.release(pid, tid)
            return self._block(P, t, f"git: {e}")
        # Uncommitted changes in the task's files block the start, unless this
        # task's own earlier session left them (then the repair session is told
        # to commit them). Work that was there before the task began is never
        # touched or attributed to the task.
        before_task = t["data"].get("task_baseline_dirty")
        inherited = t['data'].get('inherited_work') or {}
        adopted = set(inherited.get('files') or {})
        if before_task is None and adopted:
            try:
                handoff.validate(P.ws, spec, self.state_dir, inherited)
            except (ValueError, OSError) as e:
                self.store.release(pid, tid)
                return self._block(P, t, f"handoff: {e}")
        in_scope = [p for _, p in dirty if matches_any(p, spec.scope)]
        foreign = [p for p in in_scope if (before_task is None or p in before_task)
                   and p not in adopted]
        if foreign:
            self.store.release(pid, tid)
            return self._block(P, t, "uncommitted changes in this task's files "
                                     f"({', '.join(foreign[:8])}); commit or move them first — "
                                     "the coordinator never discards work")
        own = [p for p in in_scope if p not in foreign]
        baseline = {p: P.ws.file_hash(p) for _, p in dirty if p not in own}
        base = P.ws.head()
        run_id = f"{tid}-a{attempt}-{uuid.uuid4().hex[:6]}"
        findings = list(t["data"].get("findings") or [])
        if own:
            findings.append("the previous session left these task files uncommitted: "
                            + ", ".join(own[:8]) + " — check them and commit them")
        ctx = {"attempt": attempt, "run_id": run_id, "base": base}
        prompt = (P.adapter.repair_prompt(spec, findings, ctx) if findings
                  else P.adapter.implement_prompt(spec, ctx))
        if adopted:
            prompt += ("\n## Explicit existing-work handoff\nThe operator adopted these task-scope "
                       "files; original bytes are hash-verified and backed up outside the workspace:\n"
                       + "\n".join('- ' + p for p in sorted(adopted))
                       + "\nInspect and finish this work within the approved plan. Prior screenshots "
                       "are not accepted evidence until the required validity and vision checks pass. "
                       "Do not alter backups or unrelated work. Commit only verified task work.\n")
        data = dict(t["data"])
        if before_task is None:
            data["task_baseline_dirty"] = sorted(p for _, p in dirty if p not in adopted)
        data.update(baseline_dirty=baseline, free_retry=None, findings=findings or None,
                    task_base=data.get("task_base") or base, closeout_session=None,
                    closeout_attempts=0, review_errors=0)
        data = {k: v for k, v in data.items() if v is not None}
        try:
            sid = self._start_session(P, tid, spec, "implement", "implementer", run_id, attempt,
                                      prompt)
        except Exception as e:                                    # noqa: BLE001
            self._release_session(P, t)
            return self._note(P, t, f"could not start a session: {e}")
        self.store.transition(pid, tid, S.IMPLEMENTING, {
            "session_id": sid, "base_sha": base, "run_id": run_id, "attempt": attempt,
            "repair": bool(findings)},
            attempt=attempt, sessions=t["sessions"] + 1, run_id=run_id, session_id=sid,
            base_sha=base, phase="implement", started_ts=t["started_ts"] or self.clock(),
            next_action="session working", data=data)

    def _start_session(self, P: Project, tid: str, spec, phase: str, role: str, run_id: str,
                       attempt: int, prompt: str) -> str:
        key = f"session:{P.id}:{tid}:{run_id}:{phase}"
        existing = self.store.op_begin(key, P.id, tid, "session")
        persona = P.profile.persona(role)
        agent = P.profile.client.get("agent") or "code"
        directory = P.profile.workspace
        res = (existing or {}).get("result") or {}
        sid = res.get("session_id")
        if not sid:
            sid = P.client.start_session(
                directory=directory, title=f"{P.id} {tid} {phase} #{attempt}", persona=persona,
                agent=agent, metadata={"project": P.id, "task": tid, "run": run_id,
                                       "phase": phase, "attempt": attempt, "role": role},
                permissions=P.adapter.session_permissions(spec, phase))
            self.store.op_update(key, "started", {"session_id": sid})
            self.store.add_session(sid, P.id, tid, run_id, role, phase, attempt, P.client.name,
                                   persona)
            self._bind(P, sid, tid, run_id, role, persona)
        if not res.get("prompt_ts"):
            ts = self.clock()
            if role == "implementer":
                self.frontier.on_new_session(P, tid)
            prompt, notes = self._with_operator_notes(P.id, tid, prompt, "")
            units = (self.helpers.proposals_for(P.id, tid)
                     if role == "implementer" and self.helpers.enabled(P) else [])
            if units:
                prompt += "\n\n" + self.helpers.integration_prompt(units)
            if role == "implementer" and self.helpers.enabled(P):
                prompt += "\n" + self.helpers.request_guidance(P)
                note, hu = self.helpers.lanes.handoff_note(P, tid)
                if note:
                    prompt += "\n" + note
            else:
                hu = ""
            P.client.prompt(sid, prompt, directory=directory, persona=persona, agent=agent)
            self._notes_delivered(notes, sid)
            if units:
                self.helpers.mark_delivered(units, sid)
            if hu:
                self.helpers.mark_delivered(self.helpers.store.q(
                    "SELECT * FROM helper_units WHERE unit_id=?", (hu,)), sid)
            self.store.op_update(key, "done", {"session_id": sid, "prompt_ts": ts})
            self.store.update_session(sid, status="prompted", prompt_ts=ts)
            self.store.event(P.id, tid, "session_started", session_id=sid,
                             detail={"phase": phase, "role": role, "persona": persona,
                                     "attempt": attempt, "run_id": run_id})
        return sid

    def _bind(self, P: Project, sid: str, tid: str, run_id: str, role: str, persona: str,
              parent: str = "", purpose: str = "") -> None:
        if P.foundry is None:
            return
        # required purposes for the engine's own sessions (helper policy):
        # recorded on every call; the optional-work gate never applies to them
        purpose = purpose or ("review" if role in ("reviewer", "deep_reviewer", "visual_reviewer")
                              else "closeout" if "-close" in (run_id or "")
                              else "implementer" if role == "implementer" else "")
        try:
            P.foundry.bind(sid, P.id, tid, run_id, role, persona, parent=parent, purpose=purpose,
                           worker_client=self.helpers.settings(P)["worker_client"]
                           if role == "implementer" else "")
        except FoundryError as e:
            if P.profile.foundry.get("require_binding", True):
                raise RuntimeError(f"Foundry binding failed ({e}); sessions are not started "
                                   f"without project budgets") from None
            self.store.event(P.id, tid, "warning", session_id=sid,
                             detail={"binding_failed": str(e)[:300]})

    # -- IMPLEMENTING -------------------------------------------------------------

    def _implementing(self, P: Project, t: dict) -> None:
        s = self._poll(P, t, t["session_id"], phase="implement")
        if s is None:
            return
        pid, tid = P.id, t["task_id"]
        head = P.ws.head()
        rep = s.report or {}
        data = self.store.merge_data(pid, tid, last_report=rep or None)
        status = str(rep.get("status") or "").upper()
        blocked = status in ("BLOCKED", "NEEDS_HUMAN") and head == t["base_sha"]
        if self.helpers.enabled(P) and rep.get("helper_feedback"):
            self.helpers.record_feedback(P, self.store.task(pid, tid), rep)
        if self.frontier.on_idle(P, self.store.task(pid, tid), rep):
            return                       # a frontier phase started, continued or handed back
        if not blocked and self.helpers.enabled(P) and \
                self._helper_handoff(P, self.store.task(pid, tid), rep):
            return                       # the primary continues, or waits for its helpers
        if self.helpers.enabled(P):
            self.helpers.abort_running(P, tid, "cancelled", "the primary finished first")
            self.helpers.skip_waiting(P, tid, "the primary finished before this unit was "
                                              "admitted")
            self.helpers.expire_lane_results(P, tid, "the primary finished; lane results are "
                                                     "never delivered after verification starts")
        data = self.store.merge_data(pid, tid, helper_wait_since=None)
        if blocked:
            self.store.release(pid, tid)
            return self.store.transition(pid, tid, S.BLOCKED, {
                "blocker": f"session reported {status}: {rep.get('blocker') or rep.get('summary')}",
                "session_id": t["session_id"]}, blocker=str(rep.get("blocker") or status)[:500])
        self._release_session(P, t)
        self.store.transition(pid, tid, S.VERIFYING, {
            "head_sha": head, "session_idle": True, "session_id": t["session_id"],
            "model_report": rep or "none (advisory only)"},
            head_sha=head, next_action="verifying", data=data)

    def _poll(self, P: Project, t: dict, sid: str, phase: str):
        """Poll a session. Returns the idle SessionState, or None after
        handling anything else (still busy, question, error, denial)."""
        s = self._poll_inner(P, t, sid, phase)
        # the handlers below return transition rows (dicts); only an idle
        # session's state may reach the caller
        return s if isinstance(s, SessionState) else None

    def _poll_inner(self, P: Project, t: dict, sid: str, phase: str):
        pid, tid = P.id, t["task_id"]
        sess = self.store.session(sid) or {}
        prompt_ts = sess.get("prompt_ts") or 0
        directory = P.profile.workspace
        if not prompt_ts:
            # Restarted between creating the session and prompting it.
            op = self.store.op(f"session:{pid}:{tid}:{t['run_id']}:{phase}")
            if op:
                spec = P.adapter.describe(tid)
                ctx = {"attempt": t["attempt"], "run_id": t["run_id"], "base": t["base_sha"]}
                prompt = (P.adapter.closeout_prompt(spec, ctx) if phase == "closeout" else
                          P.adapter.repair_prompt(spec, t["data"].get("findings") or [], ctx)
                          if t["data"].get("findings") else P.adapter.implement_prompt(spec, ctx))
                self._start_session(P, tid, spec, phase, sess.get("role") or "implementer",
                                    t["run_id"], t["attempt"], prompt)
            return None
        timeout = P.profile.limit("session_timeout_minutes",
                                  int(P.profile.client.get("session_timeout_minutes") or 180))
        s = P.client.state(sid, directory=directory, since_ts=prompt_ts)
        if s.status == "busy":
            self._sync_children(P, t, sid)
            if s.denial:
                P.client.abort(sid, directory=directory)
                return self._on_denial(P, t, s.denial, s.error, resume=t["state"], session=sid)
            if timeout and self.clock() - prompt_ts > timeout * 60:
                P.client.abort(sid, directory=directory)
                self._end_session(P, sid, "timeout")
                return self._session_failed(P, t, f"session ran over {timeout} minutes and was "
                                                  f"stopped", phase)
            self._note(P, t, f"{phase} session working")
            return None
        if s.status == "attention":
            q = s.question or {}
            self.store.update_session(sid, status="waiting")
            return self.store.transition(pid, tid, S.AWAITING_HUMAN, {
                "awaiting": f"{q.get('kind', 'question')}: {q.get('text', '')}"[:1000],
                "session_id": sid},
                awaiting=f"{q.get('kind', 'question')}: {q.get('text', '')}"[:1000],
                data={**t["data"], "question": q, "question_phase": phase,
                      "question_from": t["state"]})
        if s.status == "missing":
            self._end_session(P, sid, "lost")
            self.store.merge_data(pid, tid, free_retry=True)
            return self._session_failed(P, t, "the coding session disappeared (Kilo restarted?); "
                                              "a fresh session resumes from the recorded state",
                                        phase, free=True)
        if s.status == "error":
            self._end_session(P, sid, "error")
            if s.denial:
                return self._on_denial(P, t, s.denial, s.error, resume=t["state"], session=sid)
            return self._session_failed(P, t, f"session ended with an error: {s.error[:400]}",
                                        phase)
        # idle
        self._sync_children(P, t, sid)
        self._end_session(P, sid, "idle", tokens=s.tokens, report=s.report)
        return s

    def _session_failed(self, P: Project, t: dict, why: str, phase: str, free: bool = False):
        pid, tid = P.id, t["task_id"]
        self._release_session(P, t)
        if phase == "closeout":
            return self._closeout_retry(P, t, why)
        self.store.transition(pid, tid, S.CHANGES_REQUIRED, {"findings": [why], "free": free},
                              data={**self.store.task(pid, tid)["data"], "findings": [why]})

    def _end_session(self, P: Project, sid: str, outcome: str, tokens=None, report=None) -> None:
        sess = self.store.session(sid) or {}
        if sess.get("ended_ts"):
            return
        data = dict(sess.get("data") or {})
        if tokens:
            data["tokens"] = tokens
        if report:
            data["report"] = report
        self.store.update_session(sid, status="ended", outcome=outcome, ended_ts=self.clock(),
                                  data=data)
        if P.foundry is not None:
            try:
                P.foundry.end(sid, outcome)
            except FoundryError:
                pass

    def _sync_children(self, P: Project, t: dict, sid: str) -> None:
        """Bind Kilo subagent sessions (the `task` tool) to the same project,
        task and run, with role helper and the parent session."""
        try:
            kids = P.client.children(sid, directory=P.profile.workspace)
        except Exception:                                         # noqa: BLE001
            return
        for k in kids or []:
            kid = k.get("id")
            if not kid or self.store.session(kid):
                continue
            self.store.add_session(kid, P.id, t["task_id"], t["run_id"], "helper", t["phase"] or "",
                                   t["attempt"], P.client.name,
                                   ((k.get("model") or {}).get("id")) or "", {"parent": sid})
            purpose = self.helpers.child_purpose(P, k.get("title") or "")
            try:
                if P.foundry is not None:
                    P.foundry.bind(kid, P.id, t["task_id"], t["run_id"], "helper",
                                   ((k.get("model") or {}).get("id")) or "", parent=sid,
                                   purpose=purpose,
                                   worker_client=self.helpers.settings(P)["worker_client"])
            except FoundryError:
                pass
            self.store.event(P.id, t["task_id"], "helper_started", session_id=kid,
                             detail={"parent": sid, "title": k.get("title")})

    def _on_denial(self, P: Project, t: dict, denial: dict, text: str, resume: str,
                   session: str = "") -> None:
        """Foundry refused a model call: budget -> pause until the reset;
        pause switch -> pause until an operator resumes; route -> retry later;
        config/auth -> blocked (a person must fix the setup)."""
        pid, tid = P.id, t["task_id"]
        cat = denial.get("category")
        retry = denial.get("retry_after")
        self.frontier.on_refusal(P, tid, str(cat))
        self._release_session(P, t)
        if cat in ("config", "auth"):
            return self._block(P, t, f"Foundry refused the call ({cat}): {text[:300]}")
        until = None
        if cat == "budget":
            until = self.clock() + (retry or 900) if P.profile.limits.get(
                "auto_resume_after_budget_reset", True) else None
        elif cat == "route":
            until = self.clock() + ROUTE_RETRY_S
        self.store.transition(pid, tid, S.PAUSED, {
            "reason": f"Foundry {cat} refusal: {text[:300]}", "resume_state": resume,
            "category": cat, "session_id": session},
            pause_until=until,
            data={**self.store.task(pid, tid)["data"], "resume_action": "continue",
                  "resume_session": session})

    # -- VERIFYING -----------------------------------------------------------------

    def _verifying(self, P: Project, t: dict) -> None:
        pid, tid = P.id, t["task_id"]
        spec = P.adapter.describe(tid)
        head = P.ws.head()
        results = self._generic_checks(P, t, spec, head, phase="verify")
        results += P.adapter.verify(spec, "verify", head, t["data"].get("task_base") or t["base_sha"])
        for sess in self.store.sessions(pid, tid):
            if sess["run_id"] == t["run_id"] and sess["role"] == "implementer":
                try:
                    tr = P.client.transcript(sess["session_id"], directory=P.profile.workspace)
                    results += P.adapter.session_audit(spec, sess["session_id"], tr, head)
                except Exception as e:                            # noqa: BLE001
                    results.append(VerificationResult("coordinator.session_audit", UNKNOWN, False,
                                                      "", f"transcript unavailable: {e}", head))
        vid = self.store.save_verification(pid, tid, head, "verify", results)
        fails = [r for r in results if r.required and r.result == FAIL]
        unknown = [r for r in results if r.required and r.result == UNKNOWN]
        self.store.event(pid, tid, "verification", detail={
            "set": vid, "revision": head, "results": {r.check: r.result for r in results}})
        if fails:
            findings = failed_findings(fails)
            return self.store.transition(pid, tid, S.CHANGES_REQUIRED, {
                "verification_id": vid, "findings": findings},
                verification_id=vid, head_sha=head, data={**t["data"], "findings": findings})
        if unknown:
            self.store.release(pid, tid)
            return self.store.transition(pid, tid, S.BLOCKED, {
                "verification_id": vid,
                "blocker": "required checks could not be run: " + ", ".join(
                    f"{r.check} ({(r.detail or '').splitlines()[0] if r.detail else 'unknown'})"
                    for r in unknown)}, verification_id=vid, head_sha=head)
        self.store.transition(pid, tid, S.REVIEWING, {
            "verification_id": vid, "head_sha": head, "all_required_pass": True},
            verification_id=vid, head_sha=head, next_action="review")

    def _generic_checks(self, P: Project, t: dict, spec, head: str, phase: str) -> list:
        base = t["data"].get("task_base") or t["base_sha"]
        exempt = P.adapter.exempt_paths(spec)
        out = []

        def add(name, ok, detail, required=True):
            out.append(VerificationResult(check=f"coordinator.{name}", result=ok,
                                          required=required, detail=detail, revision=head,
                                          evidence=f"git {base[:12]}..{head[:12]}"))
        if not P.ws.is_ancestor(base, head):
            add("history", FAIL, f"{base[:12]} is no longer an ancestor of {head[:12]} "
                                 f"(history rewritten)")
            return out
        add("history", PASS, f"{base[:12]} is an ancestor of {head[:12]}")
        commits = P.ws.commits(base, head)
        if phase == "verify" and t["data"].get("findings") and t["base_sha"] == head \
                and not spec.allow_no_change:
            add("repair_changed", FAIL, "the repair session committed nothing; the earlier "
                                        "findings still apply")
        if phase == "verify":
            if commits or spec.allow_no_change:
                add("commits", PASS, f"{len(commits)} commit(s) since {base[:12]}")
            else:
                add("commits", FAIL, "no commit since the task started (work must be committed; "
                                     "evidence from an uncommitted working copy doesn't count)")
        prefix = P.profile.commit.get("prefix", "{task}").replace("{task}", spec.task_id)
        if P.profile.commit.get("require_prefix", True):
            bad = [c for c in commits if not c[1].startswith(prefix)]
            add("commit_messages", FAIL if bad else PASS,
                ("commits without the '" + prefix + "' prefix: "
                 + "; ".join(f"{c[0][:10]} {c[1][:60]}" for c in bad[:5])) if bad
                else f"all {len(commits)} commit message(s) start with {prefix!r}")
        changed = P.ws.changed_files(base, head)
        outside = [f for f in changed if not matches_any(f, spec.scope + exempt)]
        add("scope", FAIL if outside else PASS,
            ("changed outside the task's files: " + ", ".join(outside[:12])
             + " — the coordinator never widens scope") if outside
            else f"{len(changed)} changed file(s), all within scope")
        dirty = P.ws.status()
        baseline = t["data"].get("baseline_dirty") or {}
        in_scope_dirty = [p for _, p in dirty if matches_any(p, spec.scope)
                         or (matches_any(p, exempt) and (p not in baseline or p in changed))]
        add("clean", FAIL if in_scope_dirty else PASS,
            ("uncommitted changes in task files: " + ", ".join(in_scope_dirty[:10]))
            if in_scope_dirty else "task files are committed and clean")
        touched = [p for p, h in baseline.items() if P.ws.file_hash(p) != h]
        new_dirty = [p for _, p in dirty if p not in baseline and p not in in_scope_dirty]
        add("unrelated_preserved", FAIL if touched or new_dirty else PASS,
            "; ".join(x for x in (
                ("pre-existing uncommitted changes were modified: " + ", ".join(touched[:8]))
                if touched else "",
                ("new uncommitted files outside the task: " + ", ".join(new_dirty[:8]))
                if new_dirty else "") if x)
            or f"{len(baseline)} pre-existing uncommitted change(s) untouched")
        return out

    # -- REVIEWING -------------------------------------------------------------------

    def _reviewing(self, P: Project, t: dict) -> None:
        pid, tid = P.id, t["task_id"]
        head = t["head_sha"]
        if P.ws.head() != head:
            return self.store.transition(pid, tid, S.VERIFYING, {
                "invalidated": f"workspace moved from {head[:12]} to {P.ws.head()[:12]} "
                               f"before the review"})
        spec = P.adapter.describe(tid)
        if not P.profile.review.get("required", True):
            return self.store.transition(pid, tid, S.APPROVED, {
                "review_id": "policy:review-not-required", "reviewed_commit_sha": head},
                reviewed_sha=head, review_id="policy:review-not-required")
        prior = self.store.reviews(pid, tid, head)
        valid = [r for r in prior if r["valid"]]
        if valid:
            return self._apply_review(P, t, valid[-1])
        invalid = len(prior)
        if invalid >= INVALID_REVIEWS_BEFORE_BLOCK:
            self.store.release(pid, tid)
            return self.store.transition(pid, tid, S.BLOCKED, {
                "blocker": f"the reviewer returned {invalid} invalid results for {head[:12]}: "
                           f"{prior[-1]['error']}"})
        all_reviews = self.store.reviews(pid, tid)
        cap = P.profile.limit("max_review_calls", 4)
        if len(all_reviews) >= cap:
            self.store.release(pid, tid)
            return self.store.transition(pid, tid, S.BLOCKED, {
                "blocker": f"review budget used ({len(all_reviews)}/{cap} review calls)"})
        deep = [p for p in P.profile.review.get("deep_when") or []
                if any(matches_any(tag, [p]) for tag in spec.tags)]
        role = "deep_reviewer" if deep and "deep_reviewer" in P.profile.roles else "reviewer"
        persona = P.profile.persona(role)
        authors = self.helpers.authors(pid, tid) | self.frontier.authors(pid, tid)
        if persona in authors:
            # a persona that proposed code integrated here can't be its sole reviewer
            alt = next((r for r in ("deep_reviewer", "reviewer") if r in P.profile.roles
                        and P.profile.persona(r) not in authors), None)
            if alt is None:
                return self._block(P, t, f"no independent reviewer: {persona} also authored helper "
                                         f"proposals integrated into this revision; configure a "
                                         f"different reviewer persona")
            role, persona = alt, P.profile.persona(alt)
        if P.foundry is None:
            return self._block(P, t, "review needs Foundry ([foundry] url) and none is configured")
        key = f"review:{pid}:{tid}:{head}:{role}:{invalid}"
        if (self.store.op(key) or {}).get("status") == "done":
            return
        self.store.op_begin(key, pid, tid, "review")
        base = t["data"].get("task_base") or t["base_sha"]
        diff, cut = P.ws.diff(base, head, int(P.profile.review.get("max_diff_bytes") or 60000))
        ver = self.store.verification(t["verification_id"], pid, tid)
        extra = P.adapter.review_extra(spec, head)
        if authors:
            extra = (extra or "") + ("\n\n## Code authorship\nThe primary session integrated "
                                     "proposals from read-only helpers using: "
                                     + ", ".join(sorted(authors)) + ". Review the result "
                                     "independently; their proposals are not evidence.")
        packet = R.build_packet(project_id=pid, task_id=tid, title=spec.title, spec=spec.spec,
                                scope=spec.scope, base=base, head=head,
                                commits=P.ws.commits(base, head), diff=diff, diff_truncated=cut,
                                verification=ver, extra=extra,
                                question=P.profile.review.get("question") or "")
        rsid = f"review-{t['run_id']}-{invalid}"
        try:
            self._bind(P, rsid, tid, t["run_id"], role, persona)
            text, meta = P.foundry.chat(persona, [{"role": "system", "content": R.SYSTEM},
                                                  {"role": "user", "content": packet}],
                                        session_id=rsid, schema=R.SCHEMA,
                                        max_tokens=int(P.profile.review.get("max_tokens") or 4000))
        except FoundryDenied as e:
            self._end_review_binding(P, rsid, "denied")
            self.store.op_update(key, "denied", {"category": e.category, "code": e.code})
            self.store.x("DELETE FROM ops WHERE key=?", (key,))
            return self._on_denial(P, t, {"category": e.category, "retry_after": e.retry_after},
                                   e.message, resume=S.REVIEWING, session=rsid)
        except (FoundryError, RuntimeError) as e:
            self.store.x("DELETE FROM ops WHERE key=?", (key,))
            n = int(t["data"].get("review_errors") or 0) + 1
            self.store.merge_data(pid, tid, review_errors=n)
            if n >= REVIEW_ERRORS_BEFORE_BLOCK:
                self.store.release(pid, tid)
                return self.store.transition(pid, tid, S.BLOCKED, {
                    "blocker": f"review call failed {n} times: {e}"})
            return self._note(P, t, f"review call failed ({n}/{REVIEW_ERRORS_BEFORE_BLOCK}): {e}")
        self._end_review_binding(P, rsid, "done")
        try:
            res = R.validate(text, project_id=pid, task_id=tid, head_sha=head, scope=spec.scope)
            rid = self.store.save_review(pid, tid, head, role, persona, res["verdict"], True, "",
                                         res, text)
        except R.ReviewInvalid as e:
            rid = self.store.save_review(pid, tid, head, role, persona, "", False, str(e), {},
                                         text)
            self.store.op_update(key, "done", {"review_id": rid, "valid": False})
            self.store.event(pid, tid, "review_invalid", detail={"error": str(e), "role": role})
            return self._note(P, t, f"review result rejected: {e}")
        self.store.op_update(key, "done", {"review_id": rid, "valid": True})
        self.store.event(pid, tid, "review", detail={"verdict": res["verdict"], "role": role,
                                                     "persona": persona,
                                                     "served_model": meta.get("served_model"),
                                                     "reviewed_commit_sha": head})
        self._apply_review(P, t, self.store.review(rid, pid, tid))

    def _end_review_binding(self, P: Project, rsid: str, outcome: str) -> None:
        if P.foundry is not None:
            try:
                P.foundry.end(rsid, outcome)
            except FoundryError:
                pass

    def _apply_review(self, P: Project, t: dict, rev: dict) -> None:
        pid, tid = P.id, t["task_id"]
        res = rev["result"]
        if rev["verdict"] == "APPROVED":
            return self.store.transition(pid, tid, S.APPROVED, {
                "review_id": rev["review_id"], "reviewed_commit_sha": rev["reviewed_sha"],
                "verdict": "APPROVED"}, reviewed_sha=rev["reviewed_sha"],
                review_id=rev["review_id"])
        if rev["verdict"] == "CHANGES_REQUIRED":
            findings = R.findings_for_repair(res) or ["reviewer asked for changes without details"]
            return self.store.transition(pid, tid, S.CHANGES_REQUIRED, {
                "review_id": rev["review_id"], "findings": findings, "verdict": "CHANGES_REQUIRED"},
                review_id=rev["review_id"], data={**t["data"], "findings": findings})
        self.store.release(pid, tid)
        self.store.transition(pid, tid, S.BLOCKED, {
            "blocker": "reviewer: " + ("; ".join(R.findings_for_repair(res)) or "BLOCKED"),
            "review_id": rev["review_id"], "verdict": "BLOCKED"}, review_id=rev["review_id"])

    # -- APPROVED / CLOSING ----------------------------------------------------------

    def _review_still_valid(self, P: Project, t: dict, spec, head: str) -> Optional[str]:
        """None if the reviewed commit still describes the code at head (only
        closeout-exempt files changed since), else why not."""
        rs = t["reviewed_sha"]
        if not rs:
            return "no review recorded"
        if rs == head:
            return None
        if not P.ws.is_ancestor(rs, head):
            return f"reviewed commit {rs[:12]} is not an ancestor of {head[:12]}"
        changed = P.ws.changed_files(rs, head)
        code = [f for f in changed if not matches_any(f, P.adapter.exempt_paths(spec))]
        if code:
            return "files changed after the review: " + ", ".join(code[:10])
        return None

    def _approved(self, P: Project, t: dict) -> None:
        pid, tid = P.id, t["task_id"]
        spec = P.adapter.describe(tid)
        why = self._review_still_valid(P, t, spec, P.ws.head())
        if why:
            self.store.revoke_approvals(pid, tid, why)
            return self.store.transition(pid, tid, S.VERIFYING, {"invalidated": why})
        need = self._approvals_needed(P, t, spec, "before_close", t["reviewed_sha"])
        if need:
            return self.store.transition(pid, tid, S.AWAITING_HUMAN, {
                "awaiting": "approval: " + ", ".join(need)},
                awaiting="approval: " + ", ".join(need),
                data={**t["data"], "stage": "before_close", "awaiting_kinds": need})
        self.store.transition(pid, tid, S.CLOSING, {"approvals_ok": True}, phase="closeout",
                              next_action="closeout")

    def _closing(self, P: Project, t: dict) -> None:
        pid, tid = P.id, t["task_id"]
        held = t["data"].get("training_hold")
        if held:
            # capture policy `hold`: closeout already passed at this head; only
            # the capture manifest is outstanding
            if held.get("head") == P.ws.head():
                return self._finish_close(P, t, held["cid"], held["head"], held["need"],
                                          held["published"])
            self.store.merge_data(pid, tid, training_hold=None)
            t = self.store.task(pid, tid)
        spec = P.adapter.describe(tid)
        mode = P.adapter.closeout_mode()
        if mode == "session":
            sid = t["data"].get("closeout_session")
            if not sid:
                if self._acquire(P, t, self._session_locks(P), writer=True):
                    return
                run_id = f"{t['run_id']}-close{int(t['data'].get('closeout_attempts') or 0)}"
                ctx = {"attempt": t["attempt"], "run_id": run_id, "base": t["reviewed_sha"],
                       "retry_reason": t["data"].get("closeout_retry_reason")}
                try:
                    sid = self._start_session(P, tid, spec, "closeout", "implementer", run_id,
                                              t["attempt"], P.adapter.closeout_prompt(spec, ctx))
                except Exception as e:                            # noqa: BLE001
                    self._release_session(P, t)
                    return self._note(P, t, f"could not start the closeout session: {e}")
                self.store.update_task(pid, tid, run_id=run_id, sessions=t["sessions"] + 1,
                                       session_id=sid)
                self.store.merge_data(pid, tid, closeout_session=sid)
                return
            if t["data"].get("closeout_done") != sid:
                s = self._poll(P, t, sid, phase="closeout")
                if s is None:
                    return
                self._release_session(P, t)
                self.store.merge_data(pid, tid, closeout_done=sid, closeout_report=s.report)
        head = P.ws.head()
        t = self.store.task(pid, tid)
        why = self._review_still_valid(P, t, spec, head)
        if why:
            self.store.revoke_approvals(pid, tid, why)
            return self.store.transition(pid, tid, S.VERIFYING, {"invalidated": why},
                                         data={**t["data"], "closeout_session": None,
                                               "closeout_done": None})
        results = self._generic_checks(P, t, spec, head, phase="closeout")
        results += P.adapter.verify(spec, "closeout", head, t["reviewed_sha"])
        published = self._publish(P, t, head, results)
        cid = self.store.save_verification(pid, tid, head, "closeout", results)
        bad = [r for r in results if r.required and r.result != PASS]
        if bad:
            return self._closeout_retry(P, t, "; ".join(failed_findings(bad))[:1500], cid=cid)
        need = self._approvals_needed(P, t, spec, "after_close", head)
        self._finish_close(P, t, cid, head, need, published)

    def _finish_close(self, P: Project, t: dict, cid: str, head: str, need: list,
                      published) -> None:
        pid, tid = P.id, t["task_id"]
        outcome = "CLOSED_AWAITING_APPROVAL" if need else S.COMPLETE
        if not self._training(P, {**t, "head_sha": head}, outcome):
            self.store.merge_data(pid, tid, training_hold={
                "cid": cid, "head": head, "need": need, "published": published})
            return self._note(P, t, "closeout passed; holding until the training capture "
                                    "manifest is written ([training.capture] on_failure=hold)")
        t = self.store.task(pid, tid)
        data = {k: v for k, v in t["data"].items() if k != "training_hold"}
        if need:
            self.store.release(pid, tid)
            return self.store.transition(pid, tid, S.AWAITING_HUMAN, {
                "closeout_id": cid, "awaiting": "approval: " + ", ".join(need),
                "published": published},
                closeout_id=cid, head_sha=head, awaiting="approval: " + ", ".join(need),
                data={**data, "stage": "after_close", "awaiting_kinds": need})
        self.store.release(pid, tid)
        self.store.transition(pid, tid, S.COMPLETE, {
            "closeout_id": cid, "review_valid": True, "published": published},
            closeout_id=cid, head_sha=head, next_action="", data=data)

    def _training(self, P: Project, t: dict, outcome: str) -> bool:
        """Queue the task's capture manifest. False only when it failed and
        the profile's policy is `hold`; capture never fails a task."""
        if not training_hook.enabled(P.profile):
            return True
        try:
            training_hook.finalize(self.store, P, t, outcome, self.state_dir)
            training_hook.launch_auto(self.store, P, t, self.state_dir)
            return True
        except Exception as e:                                    # noqa: BLE001
            pol = training_hook.policy(P.profile)
            if pol != "ignore":
                self.store.event(P.id, t["task_id"], "training_capture_failed",
                                 detail={"error": f"{type(e).__name__}: {e}"[:500],
                                         "policy": pol, "outcome": outcome})
                self.log(f"{P.id}/{t['task_id']}: training capture failed: {e}")
            return pol != "hold"

    def _closeout_retry(self, P: Project, t: dict, why: str, cid: str = "") -> None:
        pid, tid = P.id, t["task_id"]
        t = self.store.task(pid, tid)
        n = int(t["data"].get("closeout_attempts") or 0) + 1
        if P.adapter.closeout_mode() == "session" and n < P.profile.limit("max_closeout_attempts", 2):
            self.store.merge_data(pid, tid, closeout_attempts=n, closeout_session=None,
                                  closeout_done=None, closeout_retry_reason=why)
            return self._note(P, t, f"closeout incomplete, retrying with a fresh session: {why[:200]}")
        self.store.release(pid, tid)
        self.store.transition(pid, tid, S.CHANGES_REQUIRED, {
            "findings": [f"closeout: {why}"], "closeout_id": cid},
            data={**t["data"], "findings": [f"closeout: {why}"], "closeout_session": None,
                  "closeout_done": None, "closeout_attempts": 0})

    def _publish(self, P: Project, t: dict, head: str, results: list) -> str:
        """Push (or check the agent's push) exactly once per commit. A push is
        a backup, not a verdict: it is the last step after every check."""
        pub = P.profile.publish
        mode = pub.get("mode", "none")
        if mode == "none":
            return "not published (policy: none)"
        if any(r.required and r.result != PASS for r in results):
            return "not published (checks failed)"
        remote, branch = pub.get("remote") or P.profile.remote, pub.get("branch") or P.profile.branch
        pid, tid = P.id, t["task_id"]

        def remote_head():
            try:
                return P.ws.remote_head(remote, branch)
            except GitError as e:
                return f"error: {e}"
        cur = remote_head()
        if cur == head:
            results.append(VerificationResult("coordinator.published", PASS, True,
                                              f"{remote}/{branch}", f"{remote}/{branch} = {head[:12]}",
                                              head))
            return f"{remote}/{branch}@{head[:12]}"
        if mode == "verify_only":
            results.append(VerificationResult(
                "coordinator.published", FAIL, True, f"{remote}/{branch}",
                f"{remote}/{branch} is {str(cur)[:40]}, expected {head[:12]} (the closeout "
                f"session must push)", head))
            return ""
        key = f"push:{pid}:{tid}:{head}"
        if self.store.op_begin(key, pid, tid, "push") is None:
            try:
                out = P.ws.push(remote, branch)
                self.store.op_update(key, "done", {"output": out[-500:]})
            except GitError as e:
                self.store.op_update(key, "failed", {"error": str(e)[:500]})
        cur = remote_head()
        ok = cur == head
        results.append(VerificationResult(
            "coordinator.published", PASS if ok else FAIL, True, f"{remote}/{branch}",
            f"{remote}/{branch} = {str(cur)[:40]}" + ("" if ok else f", expected {head[:12]}; "
                                                       f"push not retried automatically"), head))
        return f"{remote}/{branch}@{head[:12]}" if ok else ""

    # -- human approval ----------------------------------------------------------------

    def _approvals_needed(self, P: Project, t: dict, spec, stage: str, revision: str) -> list[str]:
        need = []
        for rule in P.profile.approvals:
            if rule.stage != stage or not any(matches_any(tag, [rule.match]) for tag in spec.tags):
                continue
            if not self._valid_approval(P, t, spec, rule, revision):
                need.append(rule.kind)
        return need

    def _valid_approval(self, P: Project, t: dict, spec, rule, revision: str) -> Optional[dict]:
        arts = P.adapter.approval_artifacts(spec, rule.artifacts) if rule.artifacts else {}
        for a in reversed(self.store.approvals(P.id, t["task_id"], rule.kind)):
            if a["revision"] != revision:
                continue
            if rule.artifacts and a["artifacts"] != arts:
                continue
            return a
        return None

    def _awaiting(self, P: Project, t: dict) -> None:
        pid, tid = P.id, t["task_id"]
        if t["data"].get("question"):
            return                       # answered through answer()
        spec = P.adapter.describe(tid)
        stage = t["data"].get("stage")
        if stage == "before_close":
            why = self._review_still_valid(P, t, spec, P.ws.head())
            if why:
                self.store.revoke_approvals(pid, tid, why)
                return self.store.transition(pid, tid, S.VERIFYING, {"invalidated": why})
            if not self._approvals_needed(P, t, spec, "before_close", t["reviewed_sha"]):
                a = self.store.approvals(pid, tid)[-1]
                return self.store.transition(pid, tid, S.CLOSING, {"approval_id": a["approval_id"]},
                                             phase="closeout")
        elif stage == "after_close":
            if not self._approvals_needed(P, t, spec, "after_close", t["head_sha"]):
                a = self.store.approvals(pid, tid)[-1]
                return self.store.transition(pid, tid, S.COMPLETE, {
                    "approval_id": a["approval_id"], "closeout_id": t["closeout_id"]})

    # -- PAUSED --------------------------------------------------------------------------

    def _paused(self, P: Project, t: dict) -> None:
        if t["pause_until"] and self.clock() >= t["pause_until"]:
            self.resume(P.id, t["task_id"], actor="engine", note="budget/route retry time reached")

    # ======================================================================= operator

    def resume(self, project_id: str, task_id: str, actor: str = "operator", note: str = "") -> dict:
        P = self._project(project_id)
        t = self.store.task(project_id, task_id)
        if t["state"] != S.PAUSED:
            raise ValueError(f"{project_id}/{task_id} is {t['state']}, not PAUSED")
        target = t["resume_state"] or S.READY
        data = dict(t["data"])
        sid = data.pop("resume_session", None)
        action = data.pop("resume_action", None)
        if target in (S.IMPLEMENTING, S.CLOSING) and action == "continue" and sid:
            # Continue the same session (its context holds the work so far),
            # unless it is gone: then a fresh session starts from the recorded state.
            if P.client.state(sid, directory=P.profile.workspace,
                              since_ts=0).status == "missing" or \
                    self._acquire(P, t, self._session_locks(P), writer=True):
                target = S.CHANGES_REQUIRED if target == S.IMPLEMENTING else S.CLOSING
                self._notes_undeliverable(project_id, task_id, sid)
                data["findings"] = data.get("findings") or ["continue the interrupted work"]
                data["free_retry"] = True
                if target == S.CLOSING:
                    data["closeout_session"] = None
            else:
                spec = P.adapter.describe(task_id)
                ts = self.clock()
                prompt, notes = self._with_operator_notes(
                    project_id, task_id, P.adapter.continue_prompt(spec, t["pause_reason"] or "paused"),
                    sid)
                P.client.prompt(sid, prompt, directory=P.profile.workspace,
                                persona=P.profile.persona("implementer"),
                                agent=P.profile.client.get("agent") or "code")
                self._notes_delivered(notes, sid)
                self.store.update_session(sid, status="prompted", prompt_ts=ts, ended_ts=None)
                if P.foundry is not None:
                    self._bind(P, sid, task_id, t["run_id"], "implementer",
                               P.profile.persona("implementer"))
        return self.store.transition(project_id, task_id, target,
                                     {"resumed": note or f"resumed by {actor}"}, actor=actor,
                                     data=data)

    def pause(self, project_id: str, task_id: str, reason: str, actor: str = "operator") -> dict:
        P = self._project(project_id)
        t = self.store.task(project_id, task_id)
        if t["state"] in S.WRITING and t["session_id"]:
            P.client.abort(t["session_id"], directory=P.profile.workspace)
            self._release_session(P, t)
        self.helpers.abort_running(P, task_id, "cancelled", f"task paused: {reason}"[:300])
        return self.store.transition(project_id, task_id, S.PAUSED, {
            "reason": reason, "resume_state": t["state"]}, actor=actor,
            data={**t["data"], "resume_action": "continue" if t["state"] in S.WRITING else None,
                  "resume_session": t["session_id"] if t["state"] in S.WRITING else None})

    def cancel(self, project_id: str, task_id: str, reason: str, actor: str = "operator") -> dict:
        P = self._project(project_id)
        t = self.store.task(project_id, task_id)
        if t["state"] in S.WRITING and t["session_id"]:
            P.client.abort(t["session_id"], directory=P.profile.workspace)
            self._end_session(P, t["session_id"], "cancelled")
        self.helpers.abort_running(P, task_id, "cancelled", f"task cancelled: {reason}"[:300])
        self.store.release(project_id, task_id)
        return self.store.transition(project_id, task_id, S.CANCELLED, {"reason": reason},
                                     actor=actor)

    def unblock(self, project_id: str, task_id: str, resolution: str, reset_attempts: bool = False,
                actor: str = "operator") -> dict:
        t = self.store.task(project_id, task_id)
        fields = {"attempt": 0, "sessions": 0, "started_ts": None} if reset_attempts else {}
        data = {k: v for k, v in t["data"].items() if k not in ("question",)}
        return self.store.transition(project_id, task_id, S.READY, {"resolution": resolution},
                                     actor=actor, data=data, **fields)

    def approve_human(self, project_id: str, task_id: str, kind: str, approver: str,
                      note: str = "") -> str:
        """Record a person's approval, bound to the task's current revision and
        the current content of the artifacts the approval rule names."""
        P = self._project(project_id)
        t = self.store.task(project_id, task_id)
        if t["state"] != S.AWAITING_HUMAN or kind not in (t["data"].get("awaiting_kinds") or []):
            raise ValueError(f"{project_id}/{task_id} is not waiting for a {kind!r} approval")
        spec = P.adapter.describe(task_id)
        rule = next(r for r in P.profile.approvals if r.kind == kind)
        revision = t["reviewed_sha"] if t["data"].get("stage") == "before_close" else t["head_sha"]
        arts = P.adapter.approval_artifacts(spec, rule.artifacts) if rule.artifacts else {}
        return self.store.add_approval(project_id, task_id, kind, revision, arts, approver, note)

    def reject_human(self, project_id: str, task_id: str, findings: list[str],
                     approver: str) -> dict:
        t = self.store.task(project_id, task_id)
        f = [f"{approver}: {x}" for x in findings]
        return self.store.transition(project_id, task_id, S.CHANGES_REQUIRED, {"findings": f},
                                     actor=approver, data={**t["data"], "findings": f,
                                                           "stage": None, "awaiting_kinds": None})

    def answer(self, project_id: str, task_id: str, answer: list, actor: str = "operator") -> dict:
        """Answer the coding session's question (or permission request) and let
        it continue."""
        P = self._project(project_id)
        t = self.store.task(project_id, task_id)
        q = t["data"].get("question")
        if t["state"] != S.AWAITING_HUMAN or not q:
            raise ValueError(f"{project_id}/{task_id} has no open question")
        sid = t["session_id"]
        P.client.answer(sid, q["request_id"], answer, directory=P.profile.workspace,
                        kind=q.get("kind", "question"))
        data = {k: v for k, v in t["data"].items() if k not in ("question", "question_phase",
                                                                "question_from")}
        back = t["data"].get("question_from") or S.IMPLEMENTING
        self.store.update_session(sid, status="prompted", prompt_ts=self.clock())
        # a closeout session is polled from CLOSING, an implementation one from IMPLEMENTING
        return self.store.transition(project_id, task_id,
                                     S.CLOSING if back == S.CLOSING else S.IMPLEMENTING,
                                     {"answered": answer}, actor=actor, data=data)

    # ======================================================================= helper proposals

    def _helper_handoff(self, P: Project, t: dict, rep: dict) -> bool:
        """The primary just went idle (a safe point: no prompt in flight). In order:
        1. consume this report's helper_requests (once per report) and dispatch
           them now, through strict admission and binding
        2. deliver returned proposals once to this primary session -> it continues
        3. while ADMITTED helpers still run, wait up to handoff_wait_minutes, then
           stop them (timed_out): a proposal is never delivered after this point,
           so none reaches a revision that is verified or reviewed
        4. units never admitted (deferred / unknown quota) don't hold the primary:
           they're skipped. If the primary stopped to ask for help (report not
           DONE) and none arrived, it's told the outcome once and continues alone.
        True = stay IMPLEMENTING this tick."""
        pid, tid = P.id, t["task_id"]
        h = self.helpers.settings(P)
        try:
            spec = P.adapter.describe(tid)
            new = self.helpers.collect_report(P, t, spec, rep)
            if new:
                self.store.event(pid, tid, "helper_requests", session_id=t["session_id"],
                                 detail={"units": new, "report": rep.get("status")})
                if str(rep.get("status") or "").upper() != "DONE":
                    self.store.merge_data(pid, tid, helper_asked=t["session_id"])
            self.helpers.tick(P, self.store.task(pid, tid))
        except Exception as e:                                    # noqa: BLE001
            self.store.event(pid, tid, "warning", detail={"helpers": f"{type(e).__name__}: "
                                                                    f"{e}"[:300]})
            return False
        t = self.store.task(pid, tid)
        if self._deliver_proposals(P, t):
            self.store.merge_data(pid, tid, helper_wait_since=None, helper_asked=None)
            return True
        running = self.helpers.holding(pid, tid)
        if running:
            now = self.clock()
            since = (t.get("data") or {}).get("helper_wait_since")
            if not since:
                since = now
                self.store.merge_data(pid, tid, helper_wait_since=now)
                self.store.event(pid, tid, "helper_handoff_wait",
                                 detail={"units": [u["unit_id"] for u in running],
                                         "max_minutes": h["handoff_wait_minutes"]})
            limit = float(h["handoff_wait_minutes"]) * 60
            if now - since < limit:
                self._note(P, t, f"primary idle; waiting for {len(running)} running helper(s), "
                                 f"at most {int((limit - (now - since)) // 60) + 1} more min")
                return True
            stopped = self.helpers.abort_running(P, tid, "timed_out",
                                                 f"the primary was idle and waited "
                                                 f"{h['handoff_wait_minutes']} min")
            self.store.event(pid, tid, "helper_handoff_timeout", detail={"units": stopped})
        self.helpers.skip_waiting(P, tid, "not admitted before the primary was ready")
        self.store.merge_data(pid, tid, helper_wait_since=None)
        t = self.store.task(pid, tid)
        # a lane observation held for a proposer that was just skipped becomes
        # deliverable now (ordinary skipped units have no result: no change)
        if self._deliver_proposals(P, t):
            return True
        if (t.get("data") or {}).get("helper_asked") == t["session_id"]:
            self.store.merge_data(pid, tid, helper_asked=None)
            asked = [u for u in self.helpers.units(pid, tid)
                     if u["status"] in ("skipped", "timed_out", "failed", "cancelled")
                     and u["source"] == "report"]
            lines = "\n".join(f"- {u['unit_id']} ({u['responsibility']}): {u['status']} - "
                               f"{u['reason'] or ''}"[:400] for u in asked)
            text = (P.adapter.continue_prompt(spec, "the helpers you asked for are unavailable")
                    + "\n\n## Helper requests: no proposal\nNo helper proposal is coming for "
                    "your last request:\n" + (lines or "- (none admitted)") + "\nContinue the "
                    "task yourself. You may ask again later only if it really helps.\n")
            key = "none:" + ",".join(sorted(f"{u['unit_id']}.{u['requests']}" for u in asked))
            if self._prompt_primary(P, t, text, key):
                self.store.event(pid, tid, "helper_outcome_delivered", session_id=t["session_id"],
                                 detail={"units": [u["unit_id"] for u in asked]})
                return True
        return False

    def _prompt_primary(self, P: Project, t: dict, text: str, key: str) -> bool:
        """One coordinator prompt to the idle primary session, at most once per
        key (journaled before sending). After a restart between journal and
        prompt it isn't repeated: False (treated as sent, logged uncertain)."""
        sid = t["session_id"]
        okey = f"deliver:{P.id}:{t['task_id']}:{sid}:{key}"
        prior = self.store.op_begin(okey, P.id, t["task_id"], "helper_delivery")
        if prior is not None:
            if prior["status"] != "done":
                self.store.event(P.id, t["task_id"], "helper_delivery_uncertain", session_id=sid,
                                 detail={"key": key, "reason": "the coordinator stopped while "
                                         "sending; not repeated"})
                self.store.op_update(okey, "done", {"uncertain": True})
            return False
        ts = self.clock()
        persona = self.frontier.persona(P, t)
        # bind BEFORE the prompt: a frontier turn is gated as optional work
        self._bind(P, sid, t["task_id"], t["run_id"], "implementer", persona,
                   purpose=self.frontier.purpose(P, t))
        P.client.prompt(sid, text, directory=P.profile.workspace, persona=persona,
                        agent=P.profile.client.get("agent") or "code")
        self.store.op_update(okey, "done", {"prompt_ts": ts, "persona": persona})
        self.store.update_session(sid, status="prompted", prompt_ts=ts, ended_ts=None)
        return True

    def _deliver_proposals(self, P: Project, t: dict) -> bool:
        """Helpers returned proposals the primary hasn't seen: send them to the
        same idle session and keep implementing. Each proposal is delivered
        once; a unit counts as delivered (a possible author) even when a
        restart left the delivery uncertain."""
        units = (self.helpers.proposals_for(P.id, t["task_id"], t["session_id"])
                 if self.helpers.enabled(P) else [])
        if not units:
            return False
        sid = t["session_id"]
        spec = P.adapter.describe(t["task_id"])
        text = (P.adapter.continue_prompt(spec, "helper proposals arrived") + "\n\n"
                + self.helpers.integration_prompt(units))
        sent = self._prompt_primary(P, t, text, ",".join(sorted(u["unit_id"] for u in units)))
        self.helpers.mark_delivered(units, sid)
        if sent:
            self.store.event(P.id, t["task_id"], "helper_proposals_delivered", session_id=sid,
                             detail={"units": [u["unit_id"] for u in units]})
        return sent

    # ======================================================================= operator notes

    def _with_operator_notes(self, project_id: str, task_id: str, prompt: str,
                             session_id: str) -> tuple[str, list[str]]:
        """Append queued control-center instructions to a prompt the engine is
        about to send anyway (a safe point: never a second, concurrent prompt).
        They are the operator's words for this task, inside its existing scope
        and permissions: they don't widen scope or waive a check."""
        try:
            notes = self.store.pending_operator_messages(project_id, task_id, session_id)
        except Exception:                                         # noqa: BLE001
            return prompt, []
        if not notes:
            return prompt, []
        ids = [n["msg_id"] for n in notes]
        self.store.mark_operator_messages(ids, "delivering", session_id)
        body = "\n\n".join(
            f"[{n['actor']}, {time.strftime('%Y-%m-%d %H:%M', time.localtime(n['created_ts']))}]"
            f"\n{n['text']}" for n in notes)
        return (prompt + "\n\n## Operator instructions (from the control center)\n"
                "Follow these within the task's approved scope and rules; they do not authorize "
                "new scope, skipped checks or changed routing.\n\n" + body + "\n"), ids

    def _notes_delivered(self, ids: list[str], session_id: str) -> None:
        if ids:
            self.store.mark_operator_messages(ids, "delivered", session_id,
                                              "included in the prompt Kilo accepted")

    def _notes_undeliverable(self, project_id: str, task_id: str, session_id: str) -> None:
        for n in self.store.pending_operator_messages(project_id, task_id, session_id):
            if n["scope"] == "session":
                self.store.mark_operator_messages([n["msg_id"]], "rejected", "",
                                                  f"session {session_id} is gone; a "
                                                  f"session-specific instruction is not moved "
                                                  f"to another session")

    # ======================================================================= helpers

    def _project(self, project_id: str) -> Project:
        if project_id not in self.projects:
            raise ValueError(f"unknown project {project_id!r}")
        return self.projects[project_id]

    def _workspace_locks(self, P: Project) -> list[str]:
        return [f"workspace:{os.path.normcase(os.path.realpath(P.profile.workspace))}"]

    def _session_locks(self, P: Project) -> list[str]:
        return list(P.profile.resources)

    def _acquire(self, P: Project, t: dict, resources: list[str],
                 writer: bool = False) -> Optional[dict]:
        """Take every resource (all or nothing) and, for a session that may
        write, one of the global writer slots. Returns None on success, else
        what it is waiting for (and keeps nothing it newly took)."""
        pid, tid = P.id, t["task_id"]
        before = set(self.store.holds(pid, tid))
        held = self.store.acquire(resources, pid, tid)
        if held:
            self._note(P, t, f"waiting for {held['resource']} (held by "
                             f"{held['project_id']}/{held['task_id']})")
            return held
        if writer and not any(r.startswith("writer:") for r in before):
            for i in range(self.max_writers):
                if self.store.acquire([f"writer:{i}"], pid, tid) is None:
                    return None
            self.store.release(pid, tid, [r for r in resources if r not in before])
            busy = [l for l in self.store.locks() if l["resource"].startswith("writer:")]
            self._note(P, t, "waiting for a writer slot (held by "
                             + ", ".join(f"{l['project_id']}/{l['task_id']}" for l in busy) + ")")
            return {"resource": "writer"}
        return None

    def _release_session(self, P: Project, t: dict) -> None:
        pid, tid = P.id, t["task_id"]
        self.store.release(pid, tid, self._session_locks(P)
                           + [r for r in self.store.holds(pid, tid) if r.startswith("writer:")])

    def _block(self, P: Project, t: dict, why: str) -> None:
        self.store.release(P.id, t["task_id"])
        self.store.transition(P.id, t["task_id"], S.BLOCKED, {"blocker": why}, blocker=why[:1000])

    def _note(self, P: Project, t: dict, text: str) -> None:
        if t.get("next_action") != text:
            self.store.update_task(P.id, t["task_id"], next_action=text[:500])

    # ======================================================================= reporting

    def flush(self) -> None:
        """Send queued events and changed task snapshots to Foundry (display
        only; failures never affect the lifecycle)."""
        evs = self.store.unpushed_events()
        by_project: dict[str, list] = {}
        for e in evs:
            by_project.setdefault(e["project_id"], []).append(e)
        for pid, items in by_project.items():
            P = self.projects.get(pid)
            if P is None or P.foundry is None:
                self.store.mark_pushed([e["id"] for e in items])
                continue
            try:
                P.foundry.push_events([{"event_id": e["event_id"], "ts": e["ts"],
                                        "project_id": pid, "task_id": e["task_id"],
                                        "session_id": e["session_id"], "kind": e["kind"],
                                        "state": e["state"], "detail": e["detail"]} for e in items])
                tids = {e["task_id"] for e in items if e["task_id"]}
                for tid in tids:
                    t = self.store.task(pid, tid)
                    if t:
                        P.foundry.put_task(pid, tid, self.snapshot(t))
                self.store.mark_pushed([e["id"] for e in items])
            except FoundryError:
                pass

    @staticmethod
    def snapshot(t: dict) -> dict:
        d = t.get("data") or {}
        return {k: t.get(k) for k in ("state", "attempt", "sessions", "run_id", "session_id",
                                      "phase", "base_sha", "head_sha", "reviewed_sha",
                                      "review_id", "blocker", "awaiting", "next_action",
                                      "pause_reason", "pause_until")} | {
            # findings only while they are what the task is waiting on
            "findings": (d.get("findings") or [])[:10] if t.get("state") in (
                S.CHANGES_REQUIRED, S.IMPLEMENTING, S.BLOCKED, S.FAILED) else [],
            "last_report": d.get("last_report"), "stage": d.get("stage")}

    def status(self) -> dict:
        out = {"projects": [], "locks": self.store.locks()}
        for p in self.store.projects():
            tasks = self.store.tasks(p["project_id"])
            out["projects"].append({**p, "tasks": tasks})
        return out
