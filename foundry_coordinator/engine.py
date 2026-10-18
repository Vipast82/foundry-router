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
        from .retry_guidance import RetryGuidance
        self.retry_guidance = RetryGuidance(self)
        # [lifecycle] (v0.122): evidence ledger, obligations/decisions/budget
        # ledger, recovery. Built lazily, and only used for projects whose
        # profile enables them; a disabled project never touches their tables.
        self._ledger = None
        self._obligations = None
        self._recovery = None
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
        self._close_session_notes(P, t)
        if st == S.BLOCKED and self._lc(P):
            return self._lc_blocked(P, t)
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
                      and not self._dep_done_outside(P, d)]
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

    def _dep_done_outside(self, P: Project, dep: str) -> bool:
        """A dependency the task source names but the queue doesn't declare,
        finished outside the coordinator (adapter.dependency_done, e.g. a
        STATE.md ledger row). With [lifecycle] on, a dependency the coordinator
        owns (it has a tasks row) is satisfied only by the engine's own COMPLETE
        or the deps_accept_pending policy (_dep_satisfied), never by a document
        a CLOSE session wrote."""
        if self._lc(P) and self.store.task(P.id, dep):
            return False
        return bool(P.adapter.dependency_done(dep))

    # -- READY / CHANGES_REQUIRED -> IMPLEMENTING ---------------------------------

    def _start_implementation(self, P: Project, t: dict) -> None:
        pid, tid = P.id, t["task_id"]
        lim = P.profile
        free = bool(t["data"].get("free_retry"))
        attempt = t["attempt"] + (0 if free else 1)
        lc_budget = self._lc(P, "obligations")
        charge = None
        paid = False
        if lc_budget:
            # [lifecycle] limits come from the append-only budget ledger
            # (ordinary + guided allowance + authorized extensions). A start whose
            # run was already charged (rows under ANY attempt counter, e.g. written
            # before a failed or interrupted start) resumes with the same run id
            # and is never charged, or refused for that charge, again
            pend0 = t["data"].get("pending_start") or {}
            paid = bool(pend0.get("run_id") and pend0.get("attempt") == attempt
                        and self._lc_run_paid(P, tid, pend0["run_id"]))
            if not paid:
                charge, stop = self._lc_budget(P, t, free)
                if stop:
                    return
        elif attempt > lim.limit("max_attempts", 3) + self.retry_guidance.allowance(t):
            if self.retry_guidance.consult(P, t):
                return
            if ((self.store.task(pid, tid).get("data") or {}).get("retry_guidance") or {}).get(
                    "status") == "deferred":
                return
            return self.store.transition(pid, tid, S.FAILED, {
                "reason": f"{t['attempt']} implementation attempts used "
                          f"(max_attempts {lim.limit('max_attempts', 3)}, "
                          f"guided extra {self.retry_guidance.allowance(t)})"})
        if not lc_budget and t["sessions"] >= lim.limit("max_sessions_per_task", 8):
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
        if self._lc(P, "readiness"):
            # [lifecycle] the environment/tooling must be ready before a session
            # is started; a blocking failure consumes no attempt
            r = self._lc_readiness(P, t, spec, {"attempt": attempt, "base": t["base_sha"]})
            if r:
                return self._lc_block_readiness(P, t, r)
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
        # the task's own untracked intermediate gate outputs (exact adapter paths)
        # are neither "left uncommitted -- commit them" repair findings nor
        # pre-existing work to preserve byte-for-byte: the gate regenerates them,
        # and final verification requires them committed
        inter = [p for p in self._intermediate_dirty(P, t, spec, dirty) if p not in foreign] \
            if self._lc(P) else []
        own = [p for p in in_scope if p not in foreign and p not in inter]
        baseline = {p: P.ws.file_hash(p) for _, p in dirty if p not in own and p not in inter}
        base = P.ws.head()
        # a start that died between Kilo accepting it and the IMPLEMENTING
        # transition is RESUMED (same run id -> same journaled session), never
        # repeated with a second writer session
        pend = t["data"].get("pending_start") or {}
        run_id = (pend["run_id"] if pend.get("attempt") == attempt and pend.get("run_id")
                  else f"{tid}-a{attempt}-{uuid.uuid4().hex[:6]}")
        findings = list(t["data"].get("findings") or [])
        # An in-flight worker from the first BUILD-checkpoint rollout may have
        # persisted this progress marker as a repair finding. It is a normal
        # continuation, not a repeated failure to escalate or assign for repair.
        if free and findings == ["Continue the committed plan's open BUILD steps; "
                                "final gate and review remain required."]:
            findings = []
        if own:
            findings.append("the previous session left these task files uncommitted: "
                            + ", ".join(own[:8]) + " — check them and commit them")
        ctx = {"attempt": attempt, "run_id": run_id, "base": base}
        lc_extra = ""
        if self._lc(P):
            # [lifecycle] recover evidence from every earlier session, seed and
            # reconcile obligations, and (for a repair) build one bounded
            # assignment; an escalation stops here without a session
            try:
                out = self._lc_before_session(P, t, spec, base, attempt, run_id, findings,
                                              paid=paid)
            except Exception:
                # never hold the workspace / session / writer locks across ticks
                # on an exception (the tick records the error; the next one retries)
                self.store.release(pid, tid)
                raise
            if out.get("stopped"):
                return
            if out.get("assignment"):
                ctx["assignment"] = out["assignment"]
            lc_extra = out.get("prompt_extra") or ""
            t = self.store.task(pid, tid)
        prompt = (P.adapter.repair_prompt(spec, findings, ctx) if findings
                  else P.adapter.implement_prompt(spec, ctx))
        prompt += lc_extra
        if inter:
            prompt += ("\n## Intermediate gate outputs\nThese task-owned files are untracked "
                       "intermediate gate output: " + ", ".join(inter[:8]) + ". They do not "
                       "block BUILD progress; commit them with the final gate run (or let the "
                       "gate regenerate them). Final verification requires a clean tree.\n")
        if adopted:
            prompt += ("\n## Explicit existing-work handoff\nThe operator adopted these task-scope "
                       "files; original bytes are hash-verified and backed up outside the workspace:\n"
                       + "\n".join('- ' + p for p in sorted(adopted))
                       + "\nInspect and finish this work within the approved plan. Prior screenshots "
                       "are not accepted evidence until the required validity and vision checks pass. "
                       "Do not alter backups or unrelated work. Commit only verified task work.\n")
        data = dict(t["data"])
        data.pop("pending_start", None)
        data.pop(self.LC_PENDING, None)          # a continuation belongs to its own session
        if before_task is None:
            data["task_baseline_dirty"] = sorted(p for _, p in dirty if p not in adopted)
        data.update(baseline_dirty=baseline, free_retry=None, findings=findings or None,
                    task_base=data.get("task_base") or base, closeout_session=None,
                    closeout_attempts=0, review_errors=0)
        data = {k: v for k, v in data.items() if v is not None}
        self.store.merge_data(pid, tid, pending_start={"run_id": run_id, "attempt": attempt})
        try:
            sid = self._start_session(P, tid, spec, "implement", "implementer", run_id, attempt,
                                      prompt, meta=self._lc_session_meta(P, base))
        except Exception as e:                                    # noqa: BLE001
            self._release_session(P, t)
            return self._note(P, t, f"could not start a session: {e}")
        with self.store.tx():
            if lc_budget:
                # [lifecycle] charged with the IMPLEMENTING transition, keyed by run
                # id: a session that never started consumes nothing, and a resumed
                # start (same run id) is never charged twice
                if charge:
                    self._lc_charge(P, t, charge, f"attempt:{run_id}")
                self._lc_charge(P, t, "sessions", f"session:{run_id}")
            self.store.transition(pid, tid, S.IMPLEMENTING, {
                "session_id": sid, "base_sha": base, "run_id": run_id, "attempt": attempt,
                "repair": bool(findings)},
                attempt=attempt, sessions=t["sessions"] + 1, run_id=run_id, session_id=sid,
                base_sha=base, phase="implement", started_ts=t["started_ts"] or self.clock(),
                next_action="session working", data=data)

    def _start_session(self, P: Project, tid: str, spec, phase: str, role: str, run_id: str,
                       attempt: int, prompt: str, meta: Optional[dict] = None) -> str:
        key = f"session:{P.id}:{tid}:{run_id}:{phase}"
        existing = self.store.op_begin(key, P.id, tid, "session")
        persona = P.profile.persona(role, tid)
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
            if meta:
                self.store.add_session(sid, P.id, tid, run_id, role, phase, attempt,
                                       P.client.name, persona, meta)
            else:
                self.store.add_session(sid, P.id, tid, run_id, role, phase, attempt,
                                       P.client.name, persona)
            self._bind(P, sid, tid, run_id, role, persona)
        if not res.get("prompt_ts") and res.get("prompting_ts"):
            # died between sending and recording: Kilo may hold the prompt, so it
            # is never sent twice (an unanswered session ends by session_timeout)
            ts = self.clock()
            self.store.op_update(key, "done", {"session_id": sid, "prompt_ts": ts,
                                               "uncertain": True})
            self.store.update_session(sid, status="prompted", prompt_ts=ts)
            self.store.event(P.id, tid, "session_prompt_uncertain", session_id=sid,
                             detail={"phase": phase, "run_id": run_id,
                                     "note": "restart after the prompt was sent; not repeated"})
        elif not res.get("prompt_ts"):
            ts = self.clock()
            if role == "implementer":
                self.frontier.on_new_session(P, tid)
            prompt, notes = self._with_operator_notes(P.id, tid, prompt, "")
            units = (self.helpers.proposals_for(P.id, tid, sid, run_id=run_id)
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
            self.store.op_update(key, "prompting", {"session_id": sid, "prompting_ts": ts})
            try:
                P.client.prompt(sid, prompt, directory=directory, persona=persona, agent=agent)
            except Exception:
                # refused, so nothing was sent: a retry may prompt this session again
                self.store.op_update(key, "started", {"session_id": sid})
                self._notes_requeue(notes)
                raise
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
        if self._held(P, t, "the idle session's next step"):
            # the turn finished; its handoff (helper delivery, frontier turn,
            # continuation, verification -> review) runs when the project resumes
            return
        pid, tid = P.id, t["task_id"]
        head = P.ws.head()
        rep = s.report or {}
        data = self.store.merge_data(pid, tid, last_report=rep or None)
        if self._lc(P):
            self._lc_session_ended(P, t, t["session_id"], head, rep)
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
        # A committed, partially ticked BUILD plan is progress, not a failed
        # final submission. Keep final verification and every required check for
        # the point when BUILD is actually complete. Only grant a free next
        # session if HEAD advanced and the cheap scope/clean/history checks pass;
        # otherwise the normal verifier handles the failure.
        pending_fn = getattr(P.adapter, "pending_build_steps", None)
        if self._lc(P) and callable(pending_fn) and head != t["base_sha"]:
            spec = P.adapter.describe(tid)
            pending = pending_fn(spec, head, P.ws)
            if pending:
                # The project's own gate leaves this task's intermediate outputs
                # (exact, task-scoped paths) untracked between gate runs: they
                # don't stop a partial-BUILD continuation, and final verification
                # still requires them committed. Anything else dirty still blocks.
                basics = self._generic_checks(P, t, spec, head, phase="verify",
                                              intermediate=True)
                if all(r.result == PASS for r in basics if r.required):
                    inter = next((r.detail for r in basics
                                  if r.check == "coordinator.intermediate_outputs"), "")
                    findings = ["Continue the committed plan's open BUILD steps; "
                                "final gate and review remain required."]
                    total = self._build_total(P, spec, head)
                    self._release_session(P, t)
                    return self.store.transition(pid, tid, S.CHANGES_REQUIRED, {
                        "head_sha": head, "build_progress": True,
                        "open_steps": pending[:12], "session_id": t["session_id"],
                        **({"total_steps": total} if total is not None else {}),
                        "findings": findings, **({"intermediate_outputs": inter} if inter
                                                 else {})},
                        head_sha=head, next_action="continue BUILD from committed plan",
                        # This is planned progress, not a repeated failed check.
                        # The next session reads the committed NEXT/checklist;
                        # stale gate findings must not open a recovery decision.
                        data={**data, "findings": None, "build_progress": pending[:12],
                              "free_retry": True})
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
            if op and self._held(P, t, "the first prompt of a restarted session"):
                return None
            if op:
                spec = P.adapter.describe(tid)
                ctx = {"attempt": t["attempt"], "run_id": t["run_id"], "base": t["base_sha"]}
                if phase != "closeout" and self._lc(P):
                    a = self._lc_stored_assignment(P, t)
                    if a:
                        ctx["assignment"] = a
                prompt = (P.adapter.closeout_prompt(spec, ctx) if phase == "closeout" else
                          P.adapter.repair_prompt(spec, t["data"].get("findings") or [], ctx)
                          if t["data"].get("findings") else P.adapter.implement_prompt(spec, ctx))
                if phase != "closeout" and self._lc(P):
                    prompt += self._lc_prompt_extra(P, t, spec, t["base_sha"],
                                                    ctx.get("assignment"))
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
            if s.compaction:
                return self._compacted(P, t, sid, phase, s.compaction)
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

    def _compacted(self, P: Project, t: dict, sid: str, phase: str, comp: dict):
        """The turn ended on Kilo's compaction summary. It is never verified and
        never costs an attempt: wait compaction_grace_seconds for Kilo's own
        continuation, then send ONE journaled continue prompt to the same
        session (Kilo still does all summarizing)."""
        mid = comp.get("message_id") or ""
        seen = t["data"].get("compaction_seen") or {}
        now = self.clock()
        if seen.get("message_id") != mid or seen.get("session_id") != sid:
            self.store.merge_data(P.id, t["task_id"], compaction_seen={
                "message_id": mid, "session_id": sid, "ts": now})
            self.store.event(P.id, t["task_id"], "session_compacted", session_id=sid,
                             detail={"phase": phase, "message": mid})
            self._note(P, t, f"{phase} session compacted its context; waiting for Kilo to "
                             "continue")
            return None
        grace = float(P.profile.client.get("compaction_grace_seconds") or 30)
        if now - float(seen.get("ts") or now) < grace or sid != t["session_id"]:
            return None
        if self._held(P, t, f"{phase} compaction nudge"):
            return None
        nudges = dict(t["data"].get("compaction_nudges") or {})
        cap = int(P.profile.client.get("max_compaction_nudges") or 2)
        if int(nudges.get(sid) or 0) >= cap:
            # Kilo keeps compacting without progress: stop nudging (each nudge
            # restarts the session timeout); end the session like a stall
            P.client.abort(sid, directory=P.profile.workspace)
            self._end_session(P, sid, "compaction_loop")
            return self._session_failed(P, t, f"the session compacted its context {cap + 1} "
                                              "times without finishing; a fresh session "
                                              "continues from the recorded state", phase)
        nudges[sid] = int(nudges.get(sid) or 0) + 1
        self.store.merge_data(P.id, t["task_id"], compaction_nudges=nudges)
        spec = P.adapter.describe(t["task_id"])
        if self._prompt_primary(P, t, P.adapter.continue_prompt(
                spec, "your context was compacted; continue from the summary"),
                f"compaction:{mid}"):
            self._note(P, t, f"{phase} session resumed after compaction")
        return None

    def _session_failed(self, P: Project, t: dict, why: str, phase: str, free: bool = False):
        pid, tid = P.id, t["task_id"]
        self._release_session(P, t)
        if self.helpers.enabled(P):
            # this run is over: its lane work never reaches the next session
            self.helpers.abort_lane_running(P, tid, f"the session ended: {why}"[:300])
            self.helpers.expire_lane_results(P, tid, "the run ended before delivery")
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
        if self._lc(P) and data.get("head_start"):
            # [lifecycle] the session's records are bound to the revision their
            # tool calls ran at: a session that committed loses its single
            # "head", so the ledger resolves each record by its own time
            try:
                end = P.ws.head()
            except GitError:
                end = ""
            data["head_end"] = end
            if end and end != data["head_start"]:
                data.pop("head", None)
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
        if self._lc(P) and self._lc_resume_continuation(P, t):
            return                       # a pending continuation at this head: never re-verified
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
            if self._lc(P, "recovery"):
                self._lc_safe(P, tid, "record_findings", lambda: self.recovery.record_findings(
                    P, t, self._lc_attempt_no(P, t), [{"source": "check", "item": {
                        "check": r.check, "result": r.result, "detail": r.detail}}
                        for r in fails]))
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
        if self._lc(P) and self._lc_submission_gate(P, t, spec, head, vid, results):
            return                       # premature review refused: never submitted
        self.store.transition(pid, tid, S.REVIEWING, {
            "verification_id": vid, "head_sha": head, "all_required_pass": True},
            verification_id=vid, head_sha=head, next_action="review")

    def _build_total(self, P: Project, spec, head: str) -> Optional[int]:
        """BUILD checklist steps in the committed plan at `head` (the final,
        CLOSE row excluded); None = no structured checklist."""
        fn = getattr(P.adapter, "plan_progress", None)
        try:
            rows = fn(spec, head, P.ws) if callable(fn) else None
        except Exception:                                         # noqa: BLE001
            rows = None
        return max(len(rows) - 1, 0) if rows else None

    def _intermediate_dirty(self, P: Project, t: dict, spec, dirty: list) -> list[str]:
        """Untracked files at EXACTLY the adapter's intermediate-output paths for
        this task (intermediate_outputs(spec)), that were not already dirty
        before the task started: the task's own regenerated gate outputs."""
        fn = getattr(P.adapter, "intermediate_outputs", None)
        try:
            exact = set(fn(spec) or []) if callable(fn) else set()
        except Exception:                                         # noqa: BLE001
            exact = set()
        if not exact:
            return []
        before = set(t["data"].get("task_baseline_dirty") or []) \
            | set(t["data"].get("baseline_dirty") or {})
        return [p for xy, p in dirty
                if xy == "??" and p.replace("\\", "/") in exact and p not in before]

    def _generic_checks(self, P: Project, t: dict, spec, head: str, phase: str,
                        intermediate: bool = False) -> list:
        """Scope / history / commits / clean / unrelated-dirty checks. With
        intermediate=True (ONLY the committed partial-BUILD continuation),
        task-owned unfinished files are carried into the next BUILD session.
        Final verification and closeout never pass this flag."""
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
        inter = [p for p in self._intermediate_dirty(P, t, spec, dirty)
                 if p in in_scope_dirty]
        note = ("intermediate gate output (task-owned, untracked): " + ", ".join(inter[:10])
                + " — commit it (or regenerate it at the final gate) before final "
                  "verification") if inter else ""
        if intermediate and inter:
            add("intermediate_outputs", PASS, note, required=False)
            in_scope_dirty = [p for p in in_scope_dirty if p not in inter]
        # A worker can commit valid BUILD progress and leave a task-scoped
        # screenshot or baseline edit for the next session. It must be finished
        # and committed before final verification, but must not turn an open
        # checklist into a final-gate failure or a repeated-repair decision.
        # Never carry pre-existing dirty files or anything outside task scope.
        pending_task = ([p for p in in_scope_dirty if p not in baseline and
                         p not in set(t["data"].get("task_baseline_dirty") or [])]
                        if intermediate else [])
        if pending_task:
            add("pending_task_work", PASS,
                "task-owned uncommitted BUILD work: " + ", ".join(pending_task[:10])
                + " — inspect and commit it before final verification",
                required=False)
            in_scope_dirty = [p for p in in_scope_dirty if p not in pending_task]
        add("clean", FAIL if in_scope_dirty else PASS,
            ("uncommitted changes in task files: " + ", ".join(in_scope_dirty[:10])
             + (f"; {note}" if note and not intermediate else ""))
            if in_scope_dirty else "task files are committed and clean")
        touched = [p for p, h in baseline.items() if P.ws.file_hash(p) != h]
        new_dirty = [p for _, p in dirty if p not in baseline and p not in in_scope_dirty
                     and not (intermediate and (p in inter or p in pending_task))]
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
        if self._lc(P) and self._lc_resume_continuation(P, t):
            return                       # a pending continuation at this head: never re-reviewed
        spec, spec_from, spec_notes = self._reviewed_spec(P, tid, head)
        if not P.profile.review.get("required", True):
            return self.store.transition(pid, tid, S.APPROVED, {
                "review_id": "policy:review-not-required", "reviewed_commit_sha": head},
                reviewed_sha=head, review_id="policy:review-not-required")
        prior = self.store.reviews(pid, tid, head)
        # only a review made from the current packet format is this commit's review;
        # one from an older format (possibly truncated, no phase contract) is kept in
        # the audit trail and its findings carried forward, never reused as a verdict
        valid = [r for r in prior if r["valid"] and R.packet_version(r) == R.PACKET_VERSION]
        if self._lc(P):
            # [lifecycle] a review routed evidence_incomplete/access_blocked is kept
            # for the audit but not reused: the commit is reviewed again
            again = set(t["data"].get("lc_rereview") or [])
            valid = [r for r in valid if r["review_id"] not in again]
        if valid:
            return self._apply_review(P, t, valid[-1])
        older = [r for r in prior if r["valid"]]
        invalid = len([r for r in prior if not r["valid"]])
        n = len(prior)                                  # call index at this head (op key)
        if invalid >= INVALID_REVIEWS_BEFORE_BLOCK:
            self.store.release(pid, tid)
            return self.store.transition(pid, tid, S.BLOCKED, {
                "blocker": f"the reviewer returned {invalid} invalid results for {head[:12]}: "
                           f"{prior[-1]['error']}"})
        if self._held(P, t, "review"):
            return                       # a paused project starts no new paid review
        all_reviews = self.store.reviews(pid, tid)
        cap = P.profile.limit("max_review_calls", 4)
        lc_calls = self._lc(P, "obligations")
        if not lc_calls and len(all_reviews) >= cap:
            self.store.release(pid, tid)
            return self.store.transition(pid, tid, S.BLOCKED, {
                "blocker": f"review budget used ({len(all_reviews)}/{cap} review calls)"})
        deep = [p for p in P.profile.review.get("deep_when") or []
                if any(matches_any(tag, [p]) for tag in spec.tags)]
        role = "deep_reviewer" if deep and "deep_reviewer" in P.profile.roles else "reviewer"
        persona = P.profile.persona(role, tid)
        authors = (self.helpers.authors(pid, tid) | self.frontier.authors(pid, tid)
                   | self.retry_guidance.authors(t))
        for session in self.store.sessions(pid, tid):
            if session["role"] == "implementer":
                authors.add(session["persona"])
                authors.update(session["data"].get("persona_history", []))
        if persona in authors:
            # a persona that proposed code integrated here can't be its sole reviewer
            alt = next((r for r in ("deep_reviewer", "reviewer") if r in P.profile.roles
                        and P.profile.persona(r, tid) not in authors), None)
            if alt is None:
                return self._block(P, t, f"no independent reviewer: {persona} also authored helper "
                                         f"proposals integrated into this revision; configure a "
                                         f"different reviewer persona")
            role, persona = alt, P.profile.persona(alt, tid)
        if P.foundry is None:
            return self._block(P, t, "review needs Foundry ([foundry] url) and none is configured")
        key = f"review:{pid}:{tid}:{head}:{role}:{n}:p{R.PACKET_VERSION}"
        if (self.store.op(key) or {}).get("status") == "done":
            return
        if lc_calls and not self._lc_charged(P, tid, "review_calls", key):
            # [lifecycle] the budget ledger's limit (base + authorized extensions);
            # a call already charged under this key (a retried error) is not refused
            self._lc_backfill(P, t)
            lim = self.obligations.effective_limits(P, t)["review_calls"]
            if lim["remaining"] <= 0:
                self.store.release(pid, tid)
                return self.store.transition(pid, tid, S.BLOCKED, {
                    "blocker": f"review budget used ({lim['used']}/{lim['max']} review calls, "
                               f"budget ledger)"})
        base = t["data"].get("task_base") or t["base_sha"]
        ver = self.store.verification(t["verification_id"], pid, tid)
        limits, limits_from = self._reviewer_limits(P, persona)
        budget = R.packet_budget(P.profile.review, limits, persona, limits_from)
        contract = R.normalize_contract(self._adapter_call(
            P, "review_contract", lambda *_: {"phase": "full", "reviewed": [], "deferred": []},
            spec))
        evidence = self._review_evidence(P, spec, head)
        if self._lc(P, "evidence"):
            evidence = self._lc_review_items(P, spec, head, evidence)
        prio_fn = getattr(P.adapter, "diff_priority", None)

        def priority(f):
            if callable(prio_fn):
                try:
                    return int(prio_fn(f["path"], spec))
                except Exception:                                 # noqa: BLE001
                    pass
            return R.default_diff_priority(f["path"], spec, f.get("binary", False))
        earlier = [r for r in self.store.reviews(pid, tid) if r["valid"]
                   and r["verdict"] != "APPROVED"
                   and (r["reviewed_sha"] != head or R.packet_version(r) != R.PACKET_VERSION)]
        packet, info = R.build_packet(
            project_id=pid, task_id=tid, title=spec.title, spec=spec.spec, scope=spec.scope,
            base=base, head=head, commits=P.ws.commits(base, head), verification=ver,
            files=P.ws.diff_files(base, head), patch=lambda rel: P.ws.file_diff(base, head, rel),
            priority=priority, evidence=evidence, contract=contract,
            provenance={"spec_from": spec_from, "notes": spec_notes}, budget=budget,
            earlier=earlier, authors=authors, question=P.profile.review.get("question") or "")
        if info["insufficient"]:
            # never a partial-code review: the reviewer isn't called at all
            self.store.event(pid, tid, "review_insufficient", detail={
                "reviewed_commit_sha": head, "packet_version": R.PACKET_VERSION,
                **info["coverage"], "budget_source": budget["source"]})
            return self._block(P, t, info["insufficient"])
        if older:
            self.store.event(pid, tid, "review_not_reused", detail={
                "reviewed_commit_sha": head, "packet_version": R.PACKET_VERSION,
                "older": [{"review_id": r["review_id"], "verdict": r["verdict"],
                           "packet_version": R.packet_version(r)} for r in older]})
        self._settle_stale_review_ops(P, t, key)
        self.store.op_begin(key, pid, tid, "review")
        if self._lc(P, "obligations"):
            self._lc_charge(P, t, "review_calls", key)
        cov = info["coverage"]
        meta_packet = {"version": R.PACKET_VERSION, "phase": info["phase"], "spec_from": spec_from,
                       "chars": info["chars"], "est_tokens": info["est_tokens"],
                       "budget_source": budget["source"], "max_tokens": budget["max_tokens"],
                       "code_bytes": cov["code_bytes"], "partial": cov["partial"][:50],
                       "omitted": cov["omitted"][:50], "evidence": cov["evidence"][:30]}
        rsid = f"review-{t['run_id']}-{n}"
        try:
            self._bind(P, rsid, tid, t["run_id"], role, persona)
            text, meta = P.foundry.chat(persona, [{"role": "system", "content": R.SYSTEM},
                                                  {"role": "user", "content": packet}],
                                        session_id=rsid, schema=R.SCHEMA,
                                        max_tokens=budget["max_tokens"])
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
            max_err = REVIEW_ERRORS_BEFORE_BLOCK
            if self._lc(P, "obligations"):
                # [lifecycle] recorded in the ledger; the per-attempt limit is the
                # ledger's (profile max_review_errors + authorized extensions)
                self._lc_charge(P, t, "review_errors", f"review_error:{t['run_id']}:{key}:{n}")
                max_err = self.obligations.effective_limits(P, t)["review_errors"]["max"]
            if n >= max_err:
                self.store.release(pid, tid)
                return self.store.transition(pid, tid, S.BLOCKED, {
                    "blocker": f"review call failed {n} times: {e}"})
            return self._note(P, t, f"review call failed ({n}/{max_err}): {e}")
        self._end_review_binding(P, rsid, "done")
        try:
            res = R.validate(text, project_id=pid, task_id=tid, head_sha=head, scope=spec.scope)
            res["packet"] = meta_packet
            rid = self.store.save_review(pid, tid, head, role, persona, res["verdict"], True, "",
                                         res, text)
        except R.ReviewInvalid as e:
            rid = self.store.save_review(pid, tid, head, role, persona, "", False, str(e),
                                         {"packet": meta_packet}, text)
            self.store.op_update(key, "done", {"review_id": rid, "valid": False})
            self.store.event(pid, tid, "review_invalid", detail={"error": str(e), "role": role})
            return self._note(P, t, f"review result rejected: {e}")
        self.store.op_update(key, "done", {"review_id": rid, "valid": True})
        self.store.event(pid, tid, "review", detail={"verdict": res["verdict"], "role": role,
                                                     "persona": persona,
                                                     "served_model": meta.get("served_model"),
                                                     "reviewed_commit_sha": head})
        self._apply_review(P, t, self.store.review(rid, pid, tid))

    def _adapter_call(self, P: Project, name: str, default: Callable, *args):
        fn = getattr(P.adapter, name, None)
        return fn(*args) if callable(fn) else default(*args)

    def _reviewed_spec(self, P: Project, tid: str, head: str):
        """(spec, where it was read, notes): from the reviewed commit when the
        adapter can (describe_at), else the working tree, said as much."""
        fn = getattr(P.adapter, "describe_at", None)
        if callable(fn):
            try:
                spec = fn(tid, head, P.ws)
                data = spec.data if isinstance(spec.data, dict) else {}
                if data.get("spec_source") == "working tree":
                    return spec, ("working tree (" + str(data.get("spec_note") or "not bound "
                                  "to the reviewed revision") + ")"), []
                blob = data.get("task_file_blob")
                return spec, f"revision {head}" + (
                    f" ({data.get('task_file')} blob {str(blob)[:12]})" if blob else ""), []
            except Exception as e:                                # noqa: BLE001
                return (P.adapter.describe(tid), "working tree (describe_at failed)",
                        [f"describe_at({head[:12]}) failed: {str(e)[:200]}; the task spec and "
                         f"scope were read from the working tree"])
        return (P.adapter.describe(tid),
                "working tree (the adapter has no describe_at; not bound to the reviewed "
                "revision)", [])

    def _review_evidence(self, P: Project, spec, head: str) -> list:
        """Cited evidence items for the packet (adapter.review_evidence), or the
        legacy review_extra text marked unverified."""
        fn = getattr(P.adapter, "review_evidence", None)
        try:
            if callable(fn):
                return list(fn(spec, head, P.ws) or [])
            legacy = getattr(P.adapter, "review_extra", None)
            return R.legacy_evidence(legacy(spec, head) if callable(legacy) else "")
        except Exception as e:                                    # noqa: BLE001
            return [{"title": "project evidence", "path": "", "revision": head, "blob": None,
                     "lines": "", "text": "", "status": "missing", "truncated": False,
                     "priority": 0, "note": f"the adapter could not read its evidence: "
                                            f"{str(e)[:300]}"}]

    def _reviewer_limits(self, P: Project, persona: str) -> tuple[Optional[dict], str]:
        """The reviewer persona's advertised {context, output} token limits, when
        the coordinator can read them: a Foundry persona catalog (persona_limits,
        if the client offers one) or the coding client's effective config (Kilo
        provider.<p>.models[persona].limit, the limits doctor reports).
        (None, "") when neither is available."""
        fn = getattr(P.foundry, "persona_limits", None)
        if callable(fn):
            try:
                lim = fn(persona) or {}
                ctx = lim.get("context") or lim.get("context_window")
                if ctx:
                    return ({"context": ctx, "output": lim.get("output")
                             or lim.get("max_output_tokens")}, "Foundry persona catalog")
            except Exception:                                     # noqa: BLE001
                pass
        cfg_fn = getattr(P.client, "effective_config", None)
        if callable(cfg_fn):
            try:
                cfg = cfg_fn(P.profile.workspace) or {}
                prov = getattr(P.client, "provider", "") or "foundry"
                models = ((cfg.get("provider") or {}).get(prov) or {}).get("models") or {}
                lim = (models.get(persona) or {}).get("limit") or {}
                if lim.get("context"):
                    return ({"context": lim.get("context"), "output": lim.get("output")},
                            f"Kilo effective config provider.{prov}.models.{persona}.limit")
            except Exception:                                     # noqa: BLE001
                pass
        return None, ""

    def _settle_stale_review_ops(self, P: Project, t: dict, key: str) -> None:
        """A review call left "started" by an older packet format (a crash or
        restart mid-call): its result was never recorded, so whether it was sent
        is uncertain. It is closed as uncertain and never resumed or waited on;
        the current-format review has its own key."""
        suffix = f":p{R.PACKET_VERSION}"
        for op in self.store.q("SELECT * FROM ops WHERE kind='review' AND project_id=? AND "
                               "task_id=? AND status='started'", (P.id, t["task_id"])):
            if op["key"] == key or op["key"].endswith(suffix):
                continue
            self.store.op_update(op["key"], "done", {
                "uncertain": True, "superseded_by": f"packet v{R.PACKET_VERSION}"})
            self.store.event(P.id, t["task_id"], "review_call_uncertain", detail={
                "op": op["key"], "packet_version": R.PACKET_VERSION,
                "note": "an older-format review call may have been sent; no result was "
                        "recorded, so it is not reused and the commit is reviewed again"})

    def _end_review_binding(self, P: Project, rsid: str, outcome: str) -> None:
        if P.foundry is not None:
            try:
                P.foundry.end(rsid, outcome)
            except FoundryError:
                pass

    def _apply_review(self, P: Project, t: dict, rev: dict) -> None:
        pid, tid = P.id, t["task_id"]
        res = rev["result"]
        if self._lc(P) and self._lc_apply_review(P, t, rev):
            return
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
                if self._held(P, t, "closeout session start"):
                    return
                if self._acquire(P, t, self._session_locks(P), writer=True):
                    return
                run_id = f"{t['run_id']}-close{int(t['data'].get('closeout_attempts') or 0)}"
                ctx = {"attempt": t["attempt"], "run_id": run_id, "base": t["reviewed_sha"],
                       "retry_reason": t["data"].get("closeout_retry_reason")}
                if self._lc(P, "obligations"):
                    self._lc_charge(P, t, "sessions", f"session:{run_id}")
                    self._lc_charge(P, t, "closeout", f"closeout:{run_id}")
                try:
                    sid = self._start_session(P, tid, spec, "closeout", "implementer", run_id,
                                              t["attempt"], P.adapter.closeout_prompt(spec, ctx),
                                              meta=self._lc_session_meta(P, P.ws.head()))
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
        max_close = P.profile.limit("max_closeout_attempts", 2)
        if self._lc(P, "obligations"):
            # [lifecycle] base + authorized extensions from the budget ledger
            max_close = self.obligations.effective_limits(P, t)["closeout"]["max"]
        if P.adapter.closeout_mode() == "session" and n < max_close:
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
            if self._held(P, t, "budget/route retry"):
                return                   # the resume sends a continue prompt: not while paused
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
                # one continue prompt per pause, journaled: a crash before the
                # transition below must not send it again on the next resume
                row = self.store.q("SELECT MAX(rowid) AS n FROM transitions WHERE project_id=? "
                                   "AND task_id=? AND to_state=?", (project_id, task_id, S.PAUSED))
                okey = f"resume:{project_id}:{task_id}:{sid}:{(row[0]['n'] if row else 0) or 0}"
                if self.store.op_begin(okey, project_id, task_id, "resume_prompt"):
                    self.store.event(project_id, task_id, "resume_prompt_uncertain",
                                     session_id=sid, detail={"note": "continue prompt already "
                                                             "sent (or uncertain); not repeated"})
                else:
                    prompt, notes = self._with_operator_notes(
                        project_id, task_id,
                        P.adapter.continue_prompt(spec, t["pause_reason"] or "paused"), sid)
                    try:
                        persona = P.profile.persona("implementer", task_id)
                        # The resumed turn may use a newly authorized task override.
                        # Bind before inference, and retain every previous author.
                        if P.foundry is not None:
                            self._bind(P, sid, task_id, t["run_id"], "implementer", persona)
                        session = self.store.session(sid)
                        if session and session["persona"] != persona:
                            history = set(session["data"].get("persona_history", []))
                            history.add(session["persona"])
                            self.store.update_session(sid, persona=persona,
                                data={**session["data"], "persona_history": sorted(history)})
                            self.store.event(project_id, task_id, "primary_persona_changed",
                                session_id=sid, detail={"from": session["persona"], "to": persona})
                        P.client.prompt(sid, prompt, directory=P.profile.workspace,
                                        persona=persona,
                                        agent=P.profile.client.get("agent") or "code")
                    except Exception:
                        self.store.x("DELETE FROM ops WHERE key=?", (okey,))
                        self._notes_requeue(notes)
                        raise
                    self._notes_delivered(notes, sid)
                    self.store.op_update(okey, "done", {"prompt_ts": ts})
                self.store.update_session(sid, status="prompted", prompt_ts=ts, ended_ts=None)
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
        lc_on = project_id in self.projects and self._lc(self.projects[project_id])
        # (the lifecycle's block markers go with the block an operator resolved, so a
        # later, unrelated BLOCKED never auto-resumes on a stale decision/readiness;
        # a project without [lifecycle] keeps every data key but the question)
        drop = ("question", "decision_wait", "readiness_block", "readiness", "blocker_class",
                self.LC_PENDING, "lc_stop") if lc_on else ("question",)
        data = {k: v for k, v in t["data"].items() if k not in drop}
        stop = (t["data"].get("lc_stop") or {}) if lc_on else {}
        if reset_attempts:
            # a fresh budget: see RetryGuidance.on_reset (guided extra not carried
            # over; a never-called, transiently skipped adviser re-armed, bounded)
            data = self.retry_guidance.on_reset(data)
        evidence = {"resolution": resolution, "reset_attempts": bool(reset_attempts)}
        S.check(t["state"], S.READY, evidence)        # a refused unblock leaves no note
        with self.store.tx():
            if str(resolution or "").strip():
                # one note per unblock (keyed on the transition it belongs to)
                n = self.store.q("SELECT COUNT(*) AS n FROM transitions WHERE project_id=? "
                                 "AND task_id=?", (project_id, task_id))[0]["n"]
                self.store.add_operator_message(
                    f"internal:unblock:{project_id}:{task_id}:{n}", project_id, task_id, "",
                    "task", "next_session", actor,
                    f"Resolution of the block, from {actor}: {str(resolution).strip()[:2000]}")
            lc = reset_attempts and project_id in self.projects                 and self._lc(self.projects[project_id], "obligations")
            if lc:
                # [lifecycle] a reset is an authorized ledger row bound to this very
                # unblock transition (its 0-based index), never an overwrite
                P = self.projects[project_id]
                n = self.store.q("SELECT COUNT(*) AS n FROM transitions WHERE project_id=? "
                                 "AND task_id=?", (project_id, task_id))[0]["n"]
                self._lc_backfill(P, t)
            out = self.store.transition(project_id, task_id, S.READY, evidence,
                                        actor=actor, data=data, **fields)
            if stop.get("decision_id"):
                # [lifecycle] the operator resolved a "stop" answer: it is withdrawn
                # (kept for the audit), so the next escalation asks again instead of
                # stopping on the same answer forever
                self.store.x("UPDATE decisions SET status='withdrawn' WHERE decision_id=? AND "
                             "status='answered'", (stop["decision_id"],))
                self.store.event(project_id, task_id, "decision_withdrawn", detail={
                    "decision_id": stop["decision_id"], "by": actor,
                    "reason": "the operator unblocked a task stopped by this answer"})
            if lc:
                for counter in ("attempt_ordinary", "sessions"):
                    self.obligations.reset(P, t, counter, f"unblock: {resolution}"[:300],
                                           actor, f"unblock:{n}")
            return out

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
        if self._held(P, None, "prompt"):
            # every caller checks the pause first; this only keeps a new
            # caller from ever prompting a paused project's primary
            return False
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
        # queued operator notes ride along (task-scoped, and session-scoped ones
        # for exactly this session): this is a prompt the engine sends anyway
        text, notes = self._with_operator_notes(P.id, t["task_id"], text, sid)
        # bind BEFORE the prompt: a frontier turn is gated as optional work
        try:
            self._bind(P, sid, t["task_id"], t["run_id"], "implementer", persona,
                       purpose=self.frontier.purpose(P, t))
            P.client.prompt(sid, text, directory=P.profile.workspace, persona=persona,
                            agent=P.profile.client.get("agent") or "code")
        except Exception:
            self._notes_requeue(notes)
            # nothing was sent: forget the journal entry so a later safe point
            # can send it, instead of "uncertain, not repeated" (and units being
            # marked delivered without ever reaching the primary)
            self.store.x("DELETE FROM ops WHERE key=?", (okey,))
            raise
        self._notes_delivered(notes, sid)
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
        if not units or self._held(P, t, "helper delivery"):
            return False                 # (paused: kept undelivered for the resume)
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
        P = self.projects.get(project_id)
        if P is not None and self._lc(P):
            prompt += self._lc_memory_blocks(P, task_id)
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

    def _notes_requeue(self, ids: list[str]) -> None:
        """The prompt carrying these notes was refused (nothing sent): they wait
        for the next prompt instead of staying 'delivering' forever."""
        if ids:
            self.store.mark_operator_messages(ids, "queued", "", "the prompt carrying it was "
                                                                 "refused; waiting for the next")

    def _notes_delivered(self, ids: list[str], session_id: str) -> None:
        if ids:
            self.store.mark_operator_messages(ids, "delivered", session_id,
                                              "included in the prompt Kilo accepted")

    def _close_session_notes(self, P: Project, t: dict) -> None:
        """A session-scoped instruction whose session can no longer receive a
        prompt (a new session took over, or the task stopped) gets an explicit
        outcome instead of waiting 'queued' forever."""
        try:
            rows = self.store.q("SELECT msg_id, session_id FROM operator_messages WHERE "
                                "project_id=? AND task_id=? AND status='queued' AND "
                                "scope='session'", (P.id, t["task_id"]))
        except Exception:                                         # noqa: BLE001
            return
        if not rows:
            return
        st = t["state"]
        live = set()
        if st in (S.IMPLEMENTING, S.CLOSING, S.AWAITING_HUMAN) and t.get("session_id"):
            live.add(t["session_id"])
        if st == S.PAUSED and (t.get("data") or {}).get("resume_session"):
            live.add(t["data"]["resume_session"])
        for r in rows:
            if r["session_id"] not in live:
                self.store.mark_operator_messages(
                    [r["msg_id"]], "rejected", "",
                    f"session {r['session_id']} can no longer receive a prompt (task is "
                    f"{st}); a session-specific instruction is not moved to another session")

    def _notes_undeliverable(self, project_id: str, task_id: str, session_id: str) -> None:
        for n in self.store.pending_operator_messages(project_id, task_id, session_id):
            if n["scope"] == "session":
                self.store.mark_operator_messages([n["msg_id"]], "rejected", "",
                                                  f"session {session_id} is gone; a "
                                                  f"session-specific instruction is not moved "
                                                  f"to another session")

    # ======================================================================= lifecycle (v0.122)
    #
    # Everything in this section runs only for a project whose profile has
    # [lifecycle] enabled = true (sub-switches evidence / obligations / recovery /
    # readiness). A disabled project never reaches it and never creates its tables.
    #
    #   readiness   adapter.readiness() before a session; a blocking failure is
    #               BLOCKED with its class, consumes no attempt, and environment /
    #               runtime_sync blocks re-check themselves on a timer
    #   evidence    the ledger recovers exact tool results from every session of
    #               the task before a retry and before review; its blocks ride in
    #               every implementation prompt and its items in the review packet
    #   obligations seeded from the adapter, reconciled with the plan at HEAD; a
    #               model's text that moves BUILD work to CLOSE opens a decision
    #   recovery    repair assignments (adviser = RetryGuidance, at most once),
    #               the submission gate (no paid review while BUILD evidence is
    #               missing), same-session continuations, review routing, progress
    #   decisions   typed operator answers (control.py decision_answer); a task
    #               waiting on one is BLOCKED and resumes when it is answered
    #   budget      the append-only ledger (ordinary, guided, sessions, review
    #               calls, closeout, continuations); a reset is an authorized row

    LC_AUTO_CLASSES = ("environment", "runtime_sync")
    # task data key: a same-session continuation decided but not sent yet (waiting
    # for the writer slot, a paused project, ...). Durable, so a later tick sends
    # it without paying for another review or re-running the verification.
    LC_PENDING = "lc_pending_continuation"
    # decision kinds whose answer may be "stop the task" (Recovery._escalation_answer)
    LC_STOP_KINDS = ("recovery_escalation", "no_progress", "review_access")
    # the control center shows (and hashes) at most this much of a question
    LC_UI_QUESTION_CHARS = 1500

    @property
    def ledger(self):
        if self._ledger is None:
            from .evidence import EvidenceLedger
            self._ledger = EvidenceLedger(self.store, self.state_dir, clock=self.clock)
        return self._ledger

    @property
    def obligations(self):
        if self._obligations is None:
            from .obligations import Obligations
            self._obligations = Obligations(self.store, clock=self.clock)
        return self._obligations

    @property
    def recovery(self):
        if self._recovery is None:
            from .recovery import Recovery
            self._recovery = Recovery(self.store, clock=self.clock)
        return self._recovery

    @staticmethod
    def _lc(P: Project, part: str = "") -> bool:
        fn = getattr(P.profile, "lifecycle_on", None)
        try:
            return bool(fn(part)) if callable(fn) else False
        except Exception:                                         # noqa: BLE001
            return False

    @staticmethod
    def _lcv(P: Project, key: str, default):
        v = (getattr(P.profile, "lifecycle", None) or {}).get(key, default)
        return default if v is None else v

    def _lc_safe(self, P: Project, tid: str, what: str, fn, default=None):
        """Best-effort lifecycle work (recovery scans, promotions, prompt blocks):
        a failure is an event, never a stuck task. Gates are NOT run through this."""
        try:
            return fn()
        except Exception as e:                                    # noqa: BLE001
            self.store.event(P.id, tid, "warning", detail={
                "lifecycle": f"{what}: {type(e).__name__}: {e}"[:400]})
            self.log(f"{P.id}/{tid}: lifecycle {what} failed: {type(e).__name__}: {e}")
            return default

    def _lc_session_meta(self, P: Project, head: str) -> Optional[dict]:
        """Session data at start: the HEAD it began at (the ledger's source
        revision for its tool results until the session commits)."""
        if not self._lc(P) or not head:
            return None
        return {"head": head, "head_start": head}

    # -- budget ledger -------------------------------------------------------------

    def _lc_backfill(self, P: Project, t: dict) -> None:
        """A task that ran before the ledger existed (DB migration) gets its
        counters recorded once from the task row, so nothing is forgotten."""
        tid = t["task_id"]
        obs = self.obligations                  # creates the ledger tables on first use
        if obs is not None and self.store.q(
                "SELECT 1 FROM budget_ledger WHERE project_id=? AND task_id=? LIMIT 1",
                        (P.id, tid)):
            return
        t = self.store.task(P.id, tid) or t
        with self.store.tx():
            if int(t.get("attempt") or 0):
                self.obligations.charge(P.id, tid, "attempt_ordinary", int(t["attempt"]),
                                        "migrated:attempt", "engine")
            if int(t.get("sessions") or 0):
                self.obligations.charge(P.id, tid, "sessions", int(t["sessions"]),
                                        "migrated:sessions", "engine")
            n = len(self.store.reviews(P.id, tid))
            if n:
                self.obligations.charge(P.id, tid, "review_calls", n, "migrated:reviews",
                                        "engine")

    def _lc_attempt_no(self, P: Project, t: dict) -> int:
        """The task's implementation attempts so far, counted by the ledger
        (ordinary + guided; never reset). t['attempt'] without the ledger."""
        if not self._lc(P, "obligations"):
            return int(t.get("attempt") or 0)
        self._lc_backfill(P, t)
        c = self.obligations.counters(P.id, t["task_id"])
        return int(c.get("attempt_ordinary", 0)) + int(c.get("attempt_guided", 0))

    def _lc_charged(self, P: Project, tid: str, counter: str, key: str) -> bool:
        if not self.store.q("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                            "name='budget_ledger'"):
            return False
        return bool(self.store.q("SELECT 1 FROM budget_ledger WHERE project_id=? AND task_id=? "
                                 "AND counter=? AND reason=?", (P.id, tid, counter, key)))

    def _lc_run_paid(self, P: Project, tid: str, run_id: str) -> bool:
        """The run's start was already charged (attempt and/or session row, under
        any counter): resuming it must not charge, or refuse, it again."""
        self.obligations                        # creates the ledger tables on first use
        return bool(self.store.q(
            "SELECT 1 FROM budget_ledger WHERE project_id=? AND task_id=? AND reason IN (?,?)",
            (P.id, tid, f"attempt:{run_id}", f"session:{run_id}")))

    def _lc_charge(self, P: Project, t: dict, counter: str, key: str, n: int = 1) -> bool:
        """Charge a ledger counter once per key (restarts never charge twice)."""
        self._lc_backfill(P, t)
        tid = t["task_id"]
        with self.store.tx():
            if self.store.q("SELECT 1 FROM budget_ledger WHERE project_id=? AND task_id=? AND "
                            "counter=? AND reason=?", (P.id, tid, counter, key)):
                return False
            self.obligations.charge(P.id, tid, counter, n, key, "engine")
        return True

    def _lc_budget(self, P: Project, t: dict, free: bool) -> tuple[Optional[str], bool]:
        """(counter to charge for this attempt, stop). Nested limits: ordinary
        attempts, then the guided allowance (RetryGuidance, at most one consult),
        each extendable only by an authorized ledger row; sessions separately."""
        pid, tid = P.id, t["task_id"]
        self._lc_backfill(P, t)
        L = self.obligations.effective_limits(P, t)
        if L["sessions"]["remaining"] <= 0:
            self.store.transition(pid, tid, S.FAILED, {
                "reason": f"{L['sessions']['used']} sessions used (max_sessions_per_task "
                          f"{L['sessions']['max']}, budget ledger)"})
            return None, True
        if free:
            return None, False
        if L["ordinary"]["remaining"] > 0:
            return "attempt_ordinary", False
        if L["guided_extra"]["remaining"] > 0:
            return "attempt_guided", False
        if self.retry_guidance.consult(P, t):
            return None, True             # one guided extra granted: next tick charges it
        if ((self.store.task(pid, tid).get("data") or {}).get("retry_guidance") or {}).get(
                "status") == "deferred":
            return None, True             # quota reading may recover; spend no attempt
        self.store.transition(pid, tid, S.FAILED, {
            "reason": f"{L['ordinary']['used']} implementation attempts used (max_attempts "
                      f"{L['ordinary']['max']}, guided extra {L['guided_extra']['used']}/"
                      f"{L['guided_extra']['max']}, budget ledger)"})
        return None, True

    # -- readiness -----------------------------------------------------------------

    def _lc_readiness(self, P: Project, t: dict, spec, ctx: dict) -> Optional[dict]:
        """None when nothing blocks; else {cls, classes, items, auto}."""
        fn = getattr(P.adapter, "readiness", None)
        if not callable(fn):
            return None
        try:
            try:
                items = fn(spec, "implement", ctx)
            except TypeError:
                items = fn(spec)
        except Exception as e:                                    # noqa: BLE001
            self.store.event(P.id, t["task_id"], "warning", detail={
                "readiness": f"the adapter's readiness check failed: {type(e).__name__}: "
                             f"{e}"[:400]})
            return None
        bad = [i for i in items or [] if isinstance(i, dict) and i.get("ok") is False
               and bool(i.get("blocking", i.get("required", True)))]
        if not bad:
            return None
        classes = sorted({str(i.get("cls") or "environment") for i in bad})
        rows = [{"name": str(i.get("name") or i.get("check") or "?"),
                 "cls": str(i.get("cls") or "environment"),
                 "detail": str(i.get("detail") or "")[:300],
                 "remedy": str(i.get("remedy") or "")[:200]} for i in bad]
        return {"cls": rows[0]["cls"], "classes": classes, "items": rows,
                "auto": all(c in self.LC_AUTO_CLASSES for c in classes)}

    def _lc_readiness_text(self, r: dict) -> str:
        return (f"readiness ({r['cls']}): " + "; ".join(
            f"{i['name']}: {i['detail']}" + (f" (fix: {i['remedy']})" if i["remedy"] else "")
            for i in r["items"]))[:1000]

    def _lc_block_readiness(self, P: Project, t: dict, r: dict) -> None:
        pid, tid = P.id, t["task_id"]
        now = self.clock()
        retry = int(self._lcv(P, "readiness_retry_seconds", 120))
        text = self._lc_readiness_text(r)
        cur = self.store.task(pid, tid)
        data = {**cur["data"], "readiness": r["items"], "blocker_class": r["cls"],
                "readiness_block": {**r, "since": now, "checks": 1, "retry_s": retry,
                                    "retry_at": (now + retry) if r["auto"] else None}}
        self.store.release(pid, tid)
        self.store.transition(pid, tid, S.BLOCKED, {
            "blocker": text, "blocker_class": r["cls"], "readiness": r["items"],
            "attempt_consumed": "no"}, blocker=text, data=data)

    # -- BLOCKED: readiness re-check, decision waits --------------------------------

    def _lc_blocked(self, P: Project, t: dict) -> None:
        pid, tid = P.id, t["task_id"]
        data = t.get("data") or {}
        rb = data.get("readiness_block")
        if rb:
            if not rb.get("retry_at") or self.clock() < float(rb["retry_at"]):
                return                    # a person must fix it, or not time yet (no busy loop)
            try:
                spec = P.adapter.describe(tid)
            except AdapterError:
                return
            r = self._lc_readiness(P, t, spec, {"attempt": t["attempt"] + 1,
                                                "base": t["base_sha"], "recheck": True})
            if r is None:
                rest = {k: v for k, v in data.items()
                        if k not in ("readiness_block", "readiness", "blocker_class")}
                self.store.transition(pid, tid, S.READY, {
                    "resolution": f"readiness re-check passed ({rb.get('cls')} cleared after "
                                  f"{rb.get('checks', 1)} check(s))", "actor": "engine"},
                    data=rest)
                return
            n = int(rb.get("checks") or 1) + 1
            back = min(int(rb.get("retry_s") or 120) * (2 ** (n - 1)), 3600)
            text = self._lc_readiness_text(r)
            self.store.update_task(pid, tid, blocker=text, data={
                **data, "readiness": r["items"], "blocker_class": r["cls"], "readiness_block": {
                **r, "since": rb.get("since"), "checks": n, "retry_s": rb.get("retry_s"),
                "retry_at": (self.clock() + back) if r["auto"] else None}})
            return
        dw = data.get("decision_wait")
        if not dw:
            return
        ids = list(dw.get("ids") or [])
        # Repair the older behavior that treated a transient usage-source 429
        # as the only adviser consultation and opened an operator decision.
        # This is safe only when admission failed before any adviser call.
        rg = data.get("retry_guidance") or {}
        if (len(ids) == 1 and rg.get("status") == "skipped" and
                "429" in str(rg.get("reason") or "") and
                (d := self.obligations.decision(ids[0])) and
                d.get("kind") == "recovery_escalation" and d.get("status") == "open" and
                (d.get("evidence") or {}).get("adviser") == "adviser:unavailable"):
            with self.store.tx():
                self.store.x("UPDATE decisions SET status='withdrawn' WHERE decision_id=? "
                             "AND status='open'", (ids[0],))
                self.store.update_task(pid, tid, data={**data, "retry_guidance": {
                    **rg, "status": "deferred", "retry_at": self.clock(),
                    "reason": str(rg.get("reason") or "")}})
                self.store.event(pid, tid, "retry_guidance", detail={
                    "status": "deferred", "reason": "transient usage 429 recovered by "
                    "coordinator; no adviser inference was attempted",
                    "decision_withdrawn": ids[0]})
            data = (self.store.task(pid, tid).get("data") or {})
        # a superseded decision is followed to the decision that superseded it: the
        # task waits while THAT one is open (a withdrawn one without a successor is
        # resolved: the operator withdrew the question)
        decs = [self._lc_final_decision(d) for d in ids]
        final_ids = [d["decision_id"] if d else i for i, d in zip(ids, decs)]
        pending = [d for d in decs if d and d["status"] == "open"]
        if pending:
            if final_ids != ids:
                self._lc_rewait(P, t, final_ids)
            return
        answered = [d for d in decs if d]
        stop = self._lc_stop_answer(answered)
        if stop:
            d, val = stop
            return self._lc_stop_block(P, t, d, val)
        reopened = self._lc_apply_answers(P, t, answered)
        if reopened:
            # a malformed / refused typed answer: the task waits on the re-opened
            # decision (which carries the error), never wedges or resumes on it
            self._lc_rewait(P, self.store.task(pid, tid),
                            [reopened.get(i, i) for i in final_ids])
            return
        resume = dw.get("resume") or S.READY
        if resume == S.VERIFYING:
            proj = self.store.project(pid)
            if proj and proj["paused"]:
                # VERIFYING leads to a paid review / continuation prompt: not while paused
                return self._note(P, t, f"project paused (decision answered; resumes when "
                                        f"unpaused): {proj.get('pause_note') or ''}")
        rest = {k: v for k, v in (self.store.task(pid, tid)["data"] or {}).items()
                if k not in ("decision_wait", "blocker_class")}
        why = "decision(s) " + ", ".join(f"{d['decision_id']} {d['status']}" for d in answered)
        if resume == S.VERIFYING:
            head = P.ws.head()
            if t.get("head_sha") and head != t["head_sha"]:
                # the gate / review decision was about another commit: the normal
                # path (READY -> a session) instead of verifying a moved head
                self.store.transition(pid, tid, S.READY, {
                    "resolution": f"{why}; the workspace moved from {t['head_sha'][:12]} to "
                                  f"{head[:12]} while blocked", "actor": "engine"}, data=rest)
                return
            # the decision came from the gate or the review: verify the same head again
            if self._acquire(P, t, self._workspace_locks(P)):
                return
            self.store.transition(pid, tid, S.VERIFYING, {
                "resolution": why, "decision_id": ",".join(dw.get("ids") or [])},
                data=rest, next_action="verifying")
            return
        # a re-evaluated no-progress decision (decision_reevaluate) resumes as the
        # BUILD continuation it should have been: the same evidence keys as the
        # IMPLEMENTING -> CHANGES_REQUIRED continuation, for the control center
        br = rest.pop("build_resume", None)
        cont = {"build_progress": True, **{k: br[k] for k in ("head_sha", "open_steps",
                                                             "total_steps") if k in br}} \
            if isinstance(br, dict) and br.get("decision_id") in ids else {}
        self.store.transition(pid, tid, S.READY, {"resolution": why, "actor": "engine",
                                                  **cont},
                              data=rest, **({"next_action": "continue BUILD from committed "
                                                            "plan"} if cont else {}))

    def _lc_final_decision(self, did: str) -> Optional[dict]:
        d = self.obligations.decision(did)
        seen = {did}
        while d and d["status"] == "superseded" and d.get("superseded_by") \
                and d["superseded_by"] not in seen:
            seen.add(d["superseded_by"])
            nxt = self.obligations.decision(d["superseded_by"])
            if not nxt:
                break
            d = nxt
        return d

    def _lc_stop_answer(self, decisions: list) -> Optional[tuple]:
        """(decision, reason) for the first answered decision whose answer is
        "stop" (Recovery._escalation_answer), for every kind that offers it."""
        from .recovery import Recovery
        for d in decisions:
            if d and d.get("status") == "answered" and d.get("kind") in self.LC_STOP_KINDS:
                act, val = Recovery._escalation_answer(d)
                if act == "stop":
                    return d, val
        return None

    def _lc_stop_block(self, P: Project, t: dict, d: dict, reason: str) -> None:
        """An operator's "stop": the task stays BLOCKED with the reason (an
        operator unblock withdraws the answer and lets it continue)."""
        pid, tid = P.id, t["task_id"]
        did = d.get("decision_id") or ""
        text = f"recovery stopped by operator decision {did}: {reason or 'stop'}"[:900]
        cur = self.store.task(pid, tid)
        data = {k: v for k, v in (cur["data"] or {}).items()
                if k not in ("decision_wait", self.LC_PENDING)}
        data.update(blocker_class="decision", lc_stop={"decision_id": did,
                                                       "kind": d.get("kind") or ""})
        self.store.event(pid, tid, "decision_stop", detail={"decision_id": did,
                                                           "kind": d.get("kind"),
                                                           "reason": str(reason)[:300]})
        self.store.release(pid, tid)
        if cur["state"] == S.BLOCKED:
            self.store.update_task(pid, tid, blocker=text, next_action=text[:500], data=data)
            return
        self.store.transition(pid, tid, S.BLOCKED, {"blocker": text, "decision_id": did,
                                                    "blocker_class": "decision"},
                              blocker=text, data=data)

    def _lc_rewait(self, P: Project, t: dict, ids: list) -> None:
        """The task keeps waiting, now on these (current) decision ids."""
        pid, tid = P.id, t["task_id"]
        data = dict(t["data"] or {})
        dw = dict(data.get("decision_wait") or {})
        dw["ids"] = list(dict.fromkeys(i for i in ids if i))
        dw["question_hashes"] = self._lc_question_hashes(dw["ids"])
        data["decision_wait"] = dw
        text = f"waiting for operator decision {', '.join(dw['ids'])}: {dw.get('why') or ''}"
        self.store.update_task(pid, tid, data=data, blocker=text[:1000])

    @staticmethod
    def _lc_qhash(question) -> str:
        import hashlib
        import json as _json
        return hashlib.sha256(_json.dumps(question, default=str, sort_keys=True)
                              .encode()).hexdigest()[:24]

    def _lc_question_hashes(self, ids: list) -> dict:
        """decision id -> the engine's own question hash (what decision_answer
        checks), so a bridge can send it back."""
        out = {}
        for i in ids:
            d = self.obligations.decision(i)
            if d:
                out[i] = self._lc_qhash(d["question"])
        return out

    def _lc_reopen(self, P: Project, t: dict, d: dict, error: str) -> str:
        """Supersede a decision whose typed answer could not be applied with a
        re-opened one carrying the error text; the task waits on it."""
        pid, tid = P.id, t["task_id"]
        base = str(d.get("question") or "")[:1500]
        ev = d.get("evidence") if isinstance(d.get("evidence"), dict) \
            else {"evidence": d.get("evidence")}
        new = ""
        for n in range(8):
            q = f"{base}\n[the answer to {d['decision_id']} was refused: {error}]" + \
                (f" [re-ask {n}]" if n else "")
            new = self.obligations.open_decision(
                pid, tid, d["kind"], q,
                evidence={**ev, "refused_answer": d.get("answer"), "error": error[:500],
                          "refused_decision": d["decision_id"]},
                options=d.get("options") or [], recommendation=d.get("recommendation") or "",
                blocks=d.get("blocks") or [tid], scope=d.get("scope") or {},
                supersedes=d["decision_id"])
            if (self.obligations.decision(new) or {}).get("status") == "open":
                break
        with self.store.tx():
            self.obligations._mark_superseded(d["decision_id"], new, pid, tid)
        self.store.event(pid, tid, "warning", detail={
            "decision": d["decision_id"], "answer_refused": error[:400], "reopened": new})
        return new

    def _lc_apply_answers(self, P: Project, t: dict, decisions: list) -> dict:
        """Typed answers the engine acts on (once per decision). Returns
        {refused decision id: re-opened decision id} for answers that could not
        be applied (a malformed n, no counter, a refused authorization)."""
        from .obligations import LIMITS, LedgerRefused
        tid = t["task_id"]
        reopened = {}
        for d in decisions:
            if d.get("status") != "answered":
                continue
            ans = d.get("answer") if isinstance(d.get("answer"), dict) else {}
            if d["kind"] == "budget_extension" and isinstance(d.get("answer"), dict):
                # (a plain-text answer such as "no" declines the extension)
                if self.store.q("SELECT 1 FROM budget_ledger WHERE project_id=? AND task_id=? "
                                "AND decision_id=?", (P.id, tid, d["decision_id"])):
                    continue                      # applied already
                try:
                    raw = str(ans.get("counter") or "").strip()
                    if not raw:
                        raise ValueError("the answer names no counter")
                    # effective_limits keys (ordinary, sessions, review_calls, ...)
                    # name ledger counters (attempt_ordinary, ...)
                    counter = LIMITS[raw][0] if raw in LIMITS else raw
                    n = int(ans.get("n"))
                    if n <= 0:
                        raise ValueError(f"n must be a positive integer, not {ans.get('n')!r}")
                    self.obligations.authorize(P.id, tid, counter, n,
                                               f"decision:{d['decision_id']}",
                                               d.get("actor") or "operator", d["decision_id"])
                except (TypeError, ValueError, LedgerRefused) as e:
                    reopened[d["decision_id"]] = self._lc_reopen(
                        P, t, d, f"{type(e).__name__}: {e}"[:400])
            elif d["kind"] == "obligation_phase" and ans.get("obligation") and ans.get("phase"):
                try:
                    self.obligations.set_phase(P, t, ans["obligation"], ans["phase"],
                                               d["decision_id"])
                except ValueError as e:
                    self.store.event(P.id, tid, "warning", detail={
                        "decision": d["decision_id"], "set_phase": str(e)[:300]})
        return reopened

    def _lc_wait(self, P: Project, t: dict, ids: list, why: str, resume: str,
                 evidence: Optional[dict] = None) -> None:
        """Stop for operator decisions: BLOCKED with the decision ids (no session,
        no attempt); _lc_blocked resumes when every one is answered."""
        pid, tid = P.id, t["task_id"]
        ids = list(dict.fromkeys(i for i in ids if i))
        text = f"waiting for operator decision {', '.join(ids)}: {why}"[:1000]
        cur = self.store.task(pid, tid)
        data = {**cur["data"], "blocker_class": "decision",
                "decision_wait": {"ids": ids, "resume": resume, "since": self.clock(),
                                  "why": why[:500],
                                  "question_hashes": self._lc_question_hashes(ids)}}
        data.pop(self.LC_PENDING, None)
        self.store.release(pid, tid)
        self.store.event(pid, tid, "decision_wait", detail={"decisions": ids, "why": why[:500],
                                                            "resume": resume})
        self.store.transition(pid, tid, S.BLOCKED, {
            **(evidence or {}), "blocker": text, "blocker_class": "decision",
            "decision_id": ",".join(ids)}, blocker=text, data=data)

    def _lc_decided(self, did: str) -> bool:
        d = self.obligations.decision(did)
        return bool(d) and d["status"] != "open"

    # -- before a session ------------------------------------------------------------

    def _lc_plan_text(self, P: Project, tid: str, head: str, spec) -> str:
        fn = getattr(P.adapter, "plan_text", None)
        if callable(fn):
            try:
                return str(fn(spec, head, P.ws) or "")
            except Exception:                                     # noqa: BLE001
                pass
        try:
            return str(self._reviewed_spec(P, tid, head)[0].spec or "")
        except Exception:                                         # noqa: BLE001
            return str(getattr(spec, "spec", "") or "")

    def _lc_recover(self, P: Project, t: dict) -> Optional[dict]:
        if not self._lc(P, "evidence"):
            return None
        rep = self._lc_safe(P, t["task_id"], "recover", lambda: self.ledger.recover(
            P, t, max_parts=int(self._lcv(P, "recover_max_parts", 400)),
            max_seconds=float(self._lcv(P, "recover_max_seconds", 20))))
        if rep and (rep.get("added") or rep.get("errors") or rep.get("stopped")):
            self.store.event(P.id, t["task_id"], "evidence_recovered", detail={
                k: rep.get(k) for k in ("scanned", "added", "duplicates", "excluded",
                                        "truncated", "stopped")} | {
                "errors": (rep.get("errors") or [])[:5],
                "checks": {k: v.get("result") for k, v in (rep.get("checks") or {}).items()}})
        return rep

    def _lc_evaluate(self, P: Project, t: dict, spec, head: str) -> None:
        if not (self._lc(P, "obligations") and self._lc(P, "evidence")):
            return
        cur = self.ledger.current(P, spec, head)
        miss = self.ledger.missing(P, spec, head)
        self.obligations.evaluate(P, self.store.task(P.id, t["task_id"]), head, cur, miss)

    def _lc_before_session(self, P: Project, t: dict, spec, base: str, attempt: int,
                           run_id: str, findings: list, paid: bool = False) -> dict:
        """recover -> seed/reconcile -> (repair) progress escalation and the
        assignment. {'stopped': True} when the task now waits on a decision."""
        pid, tid = P.id, t["task_id"]
        self._lc_recover(P, t)
        if self._lc(P, "obligations"):
            self.obligations.seed(P, spec)
            self.obligations.reconcile(P, spec, self._lc_plan_text(P, tid, base, spec))
            self._lc_safe(P, tid, "evaluate", lambda: self._lc_evaluate(P, t, spec, base))
        assignment = None
        if findings and self._lc(P, "recovery") and self._lc(P, "obligations"):
            strategy = ""
            pr = self._lc_progress(P, t, spec)
            if pr["no_progress"]:
                q = (f"{pr['stalls']} sessions in a row made no progress (no newly verified "
                     f"obligation, same findings, {pr['summary_only_sessions']} summary-only); "
                     f"another broad retry would repeat them")
                did = self.obligations.open_decision(
                    pid, tid, "no_progress", q, evidence=pr,
                    options=["answer with a different strategy {strategy: ...}",
                             "authorize more budget", "stop the task"],
                    recommendation="give a materially different strategy or stop",
                    blocks=[tid])
                consulted = (t["data"].get("lc_stall_consult") or {}).get("stalls")
                if not self._lc_decided(did) and pr["recommendation"] == "consult" \
                        and consulted != pr["stalls"]:
                    self.store.merge_data(pid, tid, lc_stall_consult={"stalls": pr["stalls"],
                                                                      "decision": did})
                    g = self._lc_adviser(P, tid)({"project": pid, "task": tid,
                                                  "progress": pr, "findings": findings[-8:]})
                    if g.get("action") == "repair" and g.get("steps"):
                        strategy = (str(g.get("summary") or "") + "\n" +
                                    "\n".join(str(s) for s in g["steps"])).strip()
                if not strategy and not self._lc_decided(did):
                    self._lc_wait(P, t, [did], "no progress across sessions", S.READY)
                    return {"stopped": True}
                d = self._lc_final_decision(did)
                if d and d["status"] == "answered":
                    # "stop the task" (option or typed text) stops; only a real
                    # strategy becomes one (Recovery._escalation_answer)
                    from .recovery import Recovery
                    act, val = Recovery._escalation_answer(d)
                    if act == "stop":
                        self._lc_stop_block(P, t, d, val)
                        return {"stopped": True}
                    if act == "strategy" and val:
                        strategy = str(val)
            if self._lc(P, "evidence"):
                items = self._lc_safe(P, tid, "review_items", lambda: self.ledger.review_items(
                    P, spec, base, cap_chars=4000), []) or []
            else:
                items = []
            items += self._review_evidence(P, spec, base)
            rep = t["data"].get("last_report") or {}
            last = " ".join(str(rep.get(k) or "") for k in ("summary", "next", "blocker"))
            # recovery numbers attempts monotonically (ledger), so an operator reset
            # never makes a new attempt reuse an old assignment or hide a repeat
            free = bool(t["data"].get("free_retry"))
            trow = {**self.store.task(pid, tid), "run_id": run_id,
                    "attempt": self._lc_attempt_no(P, t) + (0 if free or paid else 1)
                    if self._lc(P, "obligations") else attempt}
            from .recovery import AssignmentInvariant
            try:
                a = self.recovery.assignment(
                    P, trow, ledger=self.ledger if self._lc(P, "evidence") else None,
                    obligations=self.obligations, ws=P.ws, strategy=strategy,
                    adviser=self._lc_adviser(P, tid), evidence_items=items, last_report=last,
                    spec=spec, cap=int(self._lcv(P, "prompt_cap", 6000)))
            except AssignmentInvariant as e:
                self.store.event(pid, tid, "warning", detail={"assignment": str(e)[:400]})
                a = None
            if a and a.get("defer"):
                # No adviser inference happened. Leave the task READY; the next
                # tick retries admission after its bounded backoff.
                return {"stopped": True}
            if a and a.get("decision"):
                self._lc_wait(P, t, [a["decision"]], "findings repeat with no materially "
                              "different strategy (adviser consulted at most once)", S.READY)
                return {"stopped": True}
            if a and a.get("stop"):
                # the operator answered the escalation with "stop": no session starts
                self._lc_stop_block(P, t, {"decision_id": a.get("decision_id") or "",
                                           "kind": "recovery_escalation"},
                                    a.get("reason") or "stop")
                return {"stopped": True}
            assignment = a
            if a:
                self.store.event(pid, tid, "repair_assignment", detail={
                    "attempt": attempt, "repair_id": a.get("repair_id"),
                    "unresolved": [u["obligation_id"] for u in a.get("unresolved") or []],
                    "repeats": len(a.get("repeats") or []),
                    "rerun": a.get("affected_checks_to_rerun")})
        return {"assignment": assignment,
                "prompt_extra": self._lc_prompt_extra(P, t, spec, base, assignment)}

    def _lc_stored_assignment(self, P: Project, t: dict) -> Optional[dict]:
        if not self._lc(P, "recovery"):
            return None
        for r in reversed(self.recovery.repairs(P.id, t["task_id"])):
            if int(r.get("attempt") or 0) == self._lc_attempt_no(P, t) and r.get("assignment"):
                return r["assignment"]
        return None

    def _lc_prompt_extra(self, P: Project, t: dict, spec, head: str,
                         assignment: Optional[dict]) -> str:
        """The assignment and the ledger's evidence block (memory and decisions
        ride in every prompt through _with_operator_notes). Bounded."""
        cap = int(self._lcv(P, "prompt_cap", 6000))
        parts = []
        if assignment and assignment.get("prompt_text"):
            parts.append(str(assignment["prompt_text"])[:cap])
        if self._lc(P, "evidence"):
            block = self._lc_safe(P, t["task_id"], "prompt_block",
                                  lambda: self.ledger.prompt_block(P, spec, head, cap=cap), "")
            if block:
                parts.append(block)
        return ("\n\n" + "\n\n".join(parts) + "\n") if parts else ""

    def _lc_memory_blocks(self, P: Project, tid: str) -> str:
        parts = []
        if self._lc(P, "evidence"):
            parts.append(self._lc_safe(P, tid, "memory_block",
                                       lambda: self.ledger.memory_block(P.id, tid, cap=4000), ""))
        if self._lc(P, "obligations"):
            parts.append(self._lc_safe(P, tid, "decisions_block",
                                       lambda: self.obligations.decisions_block(P.id, tid,
                                                                                cap=3000), ""))
        parts = [p for p in parts if p]
        return ("\n\n" + "\n\n".join(parts) + "\n") if parts else ""

    def _lc_adviser(self, P: Project, tid: str):
        """RetryGuidance as the assignment's adviser: consulted at most once per
        task (its own record), with its re-arm and extra-attempt caps intact."""
        from .retry_guidance import RetryGuidance

        def adviser(packet: dict) -> dict:
            if not RetryGuidance.settings(P)["enabled"]:
                return {"action": "unavailable", "summary": "retry guidance is not enabled",
                        "steps": []}
            t = self.store.task(P.id, tid)
            prior = (t.get("data") or {}).get("retry_guidance") or {}
            if prior and prior.get("status") != "deferred":
                return {"action": "unavailable", "summary": "the adviser was already "
                        "consulted for this task", "steps": []}
            self.retry_guidance.consult(P, t)
            rec = (self.store.task(P.id, tid).get("data") or {}).get("retry_guidance") or {}
            act = rec.get("status")
            return {"action": act if act in ("repair", "needs_human", "stop", "deferred")
                    else "unavailable",
                    "summary": rec.get("summary") or rec.get("reason") or "",
                    "steps": list(rec.get("steps") or [])}
        return adviser

    # -- a session ended -------------------------------------------------------------

    def _lc_build_progress(self, P: Project, tid: str, prev: str, head: str,
                           spec=None) -> dict:
        """Committed BUILD progress of one session, prev..head, read from git
        only (never from the model's report):
          work_files  in-scope files changed (code, tests, evidence, committed
                      gate outputs), except the plan itself: a plan edit counts
                      only through its checklist (a NEXT-line rewrite is not
                      progress); None = unknown
          checklist   {"before": plan rows at prev, "after": plan rows at head}
                      (adapter.plan_progress; both None without a checklist)"""
        out: dict = {}
        try:
            spec = spec or P.adapter.describe(tid)
        except Exception:                                         # noqa: BLE001
            return out
        if prev and head:
            try:
                changed = P.ws.changed_files(prev, head) if prev != head else []
                plan = str((getattr(spec, "data", None) or {}).get("plan") or "")
                out["work_files"] = len([f for f in changed if matches_any(f, spec.scope)
                                         and f != plan])
            except Exception:                                     # noqa: BLE001
                pass
        fn = getattr(P.adapter, "plan_progress", None)
        try:
            after = fn(spec, head, P.ws) if callable(fn) and head else None
            before = fn(spec, prev, P.ws) if callable(fn) and prev and after else None
            # recorded even when empty, so a later progress() needn't resolve it again
            out["checklist"] = {"before": before, "after": after}
        except Exception:                                         # noqa: BLE001
            pass
        return out

    def _lc_progress(self, P: Project, t: dict, spec=None) -> dict:
        """recovery.progress with marks recorded before v0.122.5 (no committed
        BUILD progress columns) resolved from git and the committed plan."""
        tid = t["task_id"]
        return self.recovery.progress(P, t, resolve=lambda m: self._lc_build_progress(
            P, tid, m.get("prev_head") or "", m.get("head") or "", spec))

    def _lc_session_ended(self, P: Project, t: dict, sid: str, head: str, rep: dict) -> None:
        tid = t["task_id"]
        if self._lc(P, "obligations"):
            # a model's report is data: a move of BUILD work to CLOSE opens a
            # decision and never moves an obligation
            text = "\n".join(str(rep.get(k) or "") for k in ("summary", "next", "blocker",
                                                             "notes"))
            if text.strip():
                self._lc_safe(P, tid, "screen_report", lambda: self.obligations.screen_text(
                    P, tid, text, "model_report"))
        if self._lc(P, "recovery"):
            sess = self.store.session(sid) or {}
            prev = (sess.get("data") or {}).get("head_start") or t.get("base_sha") or ""

            def mark():
                cur = self.store.task(P.id, tid)
                build = self._lc_build_progress(P, tid, prev, head)
                self.recovery.mark_session(P, {**cur, "attempt": self._lc_attempt_no(P, cur)},
                                           sid, prev_head=prev,
                                           head=head, reported=bool(rep),
                                           actions=str(rep.get("summary") or ""),
                                           work_files=build.get("work_files"),
                                           checklist=build.get("checklist"))
                self.recovery.close_repair(P, t, self._lc_attempt_no(P, t), end_rev=head,
                                           diff_files=P.ws.changed_files(prev, head)
                                           if prev and prev != head else [])
            self._lc_safe(P, tid, "mark_session", mark)

    # -- VERIFYING: promotion and the submission gate --------------------------------

    def _lc_promote(self, P: Project, t: dict, spec, head: str, vid: str, results: list) -> None:
        """Record this verification set's verdicts for the required checks as
        verified ledger records, and promote transcript records this set
        confirmed (observed PASS of a check that passed here)."""
        tid = t["task_id"]
        req = {str(r.get("check_id")) for r in (self._adapter_call(
            P, "required_checks", lambda *_: [], spec, "BUILD") or []) if isinstance(r, dict)}
        for r in results:
            if r.check not in req or r.result not in (PASS, FAIL):
                continue
            self.ledger.record(
                project_id=P.id, task_id=tid, check_id=r.check,
                idem_key=f"{P.id}|{tid}|verification|{vid}|{r.check}",
                run_id=t.get("run_id") or "", source="coordinator", check_kind="verification",
                exec_status="completed", result=r.result, tier="verified",
                source_revision=head, started_ts=r.executed_ts, ended_ts=r.executed_ts,
                provenance={"verification_id": vid, "evidence": r.evidence},
                output=r.detail or r.result)
        passed = {r.check for r in results if r.result == PASS}
        for cid, rec in self.ledger.current(P, spec, head).items():
            if cid in passed and rec["tier"] == "observed" and rec["result"] == "PASS" \
                    and not rec["truncated"]:
                try:
                    self.ledger.promote(rec["record_id"], "verified", actor="engine",
                                        ref_kind="verification", ref_id=vid)
                except ValueError:
                    pass

    @staticmethod
    def _lc_gate_lines(items: list) -> list[str]:
        out = []
        for g in items:
            it = g.get("item") or {}
            if it.get("obligation_id"):
                line = f"obligation {it['obligation_id']} ({it.get('title') or ''}): " \
                       f"{it.get('reason') or 'not verified at head'}"
            elif it.get("check_id"):
                line = f"check {it['check_id']}: {it.get('reason')} ({it.get('detail') or ''})"
            else:
                line = f"evidence {it.get('path') or it.get('title')}: {it.get('status')} " \
                       f"{it.get('note') or ''}"
            if line not in out:
                out.append(line[:400])
        return out

    def _lc_submission_gate(self, P: Project, t: dict, spec, head: str, vid: str,
                            results: list) -> bool:
        """True when the review was refused (the task moved on): every BUILD
        obligation verified at head, no missing/stale required evidence and no
        open obligation conflict, or no paid review."""
        pid, tid = P.id, t["task_id"]
        if self._lc(P, "evidence"):
            self._lc_recover(P, t)
            self._lc_safe(P, tid, "promote",
                          lambda: self._lc_promote(P, t, spec, head, vid, results))
        if not (self._lc(P, "recovery") and self._lc(P, "obligations")
                and self._lc(P, "evidence")):
            return False
        self.obligations.seed(P, spec)          # lazily, for a task that predates the ledger
        gate = self.recovery.gate_submission(P, self.store.task(pid, tid), head, self.ledger,
                                             self.obligations, spec=spec,
                                             evidence_items=self._review_evidence(P, spec, head))
        if not gate:
            self.store.event(pid, tid, "submission_gate", detail={"head": head, "ok": True})
            return False
        self.store.event(pid, tid, "submission_refused", detail={
            "head": head, "verification_id": vid, "items": gate[:20]})
        decisions = [g["item"].get("decision_id") for g in gate if g["action"] == "decision"]
        if decisions:
            self._lc_wait(P, t, decisions, "an obligation conflict must be decided before "
                          "review", S.VERIFYING, {"verification_id": vid})
            return True
        missing = [g for g in gate if g["class"] == "missing_evidence"]
        lines = self._lc_gate_lines(missing or gate)
        findings = ["missing evidence before review: " + x for x in lines] or \
            ["the submission gate refused the review"]
        fb = {"kind": "gate", "verification_id": vid, "head": head, "findings": findings,
              "records": [{"class": "missing_evidence", "text": x,
                           "check_id": (g["item"] or {}).get("check_id") or ""}
                          for x, g in zip(lines, missing or gate)]}
        if missing and all(g["action"] == "continue_same_session" for g in missing):
            used = self.obligations.effective_limits(P, t)[
                "missing_evidence_continuations"]["used"]
            if self._lc_continue(P, t, spec, head, lines, f"submit:{head}:{used}",
                                 {"verification_id": vid}, fb):
                return True
        self._lc_fallback(P, t, fb)
        return True

    def _lc_fallback(self, P: Project, t: dict, fb: dict) -> None:
        """No continuation is possible (no session, it is gone, the prompt
        failed): the gate's repair (CHANGES_REQUIRED) or the review's
        evidence-only free retry. The pending marker goes with it."""
        pid, tid = P.id, t["task_id"]
        cur = self.store.task(pid, tid)
        data = {k: v for k, v in (cur["data"] or {}).items() if k != self.LC_PENDING}
        findings = list(fb.get("findings") or ["the coordinator found evidence missing"])
        if fb.get("kind") == "review":
            self.store.release(pid, tid)
            self.store.transition(pid, tid, S.CHANGES_REQUIRED, {
                "review_id": fb["review_id"], "findings": findings,
                "verdict": "CHANGES_REQUIRED", "route": "evidence_incomplete"},
                review_id=fb["review_id"],
                data={**data, "findings": findings, "free_retry": True, "evidence_only": True})
            return
        records = list(fb.get("records") or [])
        if records:
            self._lc_safe(P, tid, "record_findings", lambda: self.recovery.record_findings(
                P, cur, self._lc_attempt_no(P, cur), records))
        self.store.transition(pid, tid, S.CHANGES_REQUIRED, {
            "verification_id": fb["verification_id"], "findings": findings,
            "submission_gate": "refused"},
            verification_id=fb["verification_id"], head_sha=fb.get("head") or cur["head_sha"],
            data={**data, "findings": findings})

    def _lc_resume_continuation(self, P: Project, t: dict) -> bool:
        """A continuation decided earlier (gate or review) and not sent yet: send
        it now, at the same head and session, without verifying or reviewing
        again. True when handled (sent, still waiting, or fell back)."""
        m = (t.get("data") or {}).get(self.LC_PENDING)
        if not m:
            return False
        pid, tid = P.id, t["task_id"]
        head = P.ws.head()
        if m.get("from_state") != t["state"] or m.get("head") != head \
                or m.get("session_id") != t.get("session_id") or not m.get("fallback"):
            self.store.merge_data(pid, tid, **{self.LC_PENDING: None})
            self.store.event(pid, tid, "continuation_dropped", detail={
                "key": m.get("key"), "from_state": m.get("from_state"),
                "state": t["state"], "head": head, "marker_head": m.get("head")})
            return False
        spec = P.adapter.describe(tid)
        if self._lc_continue(P, t, spec, head, list(m.get("lines") or []), m["key"],
                             dict(m.get("evidence") or {}), m["fallback"]):
            return True
        self._lc_fallback(P, self.store.task(pid, tid), m["fallback"])
        return True

    def _lc_continue(self, P: Project, t: dict, spec, head: str, lines: list, key: str,
                     evidence: dict, fallback: Optional[dict] = None) -> bool:
        """ONE journaled continuation prompt to the same (idle) session listing the
        exact missing evidence, then back to IMPLEMENTING. Charges
        same_session_continuations, never an implementation attempt.

        True = handled: sent, or waiting (writer slot, paused project) with a
        durable pending marker that later ticks resume without paying for another
        review or verification. False = no continuation is possible (no session,
        Kilo lost it, the prompt failed): the marker is cleared, the locks this
        took are released, and the caller falls back."""
        pid, tid = P.id, t["task_id"]
        sid = t.get("session_id")

        def drop():
            self.store.merge_data(pid, tid, **{self.LC_PENDING: None})
        if not sid or not self.store.session(sid):
            drop()
            return False
        try:
            gone = P.client.state(sid, directory=P.profile.workspace,
                                  since_ts=0).status == "missing"
        except Exception:                                         # noqa: BLE001
            gone = False                  # unknown: the prompt below decides
        if gone:
            self.store.event(pid, tid, "warning", session_id=sid, detail={
                "continuation": f"session {sid} is gone; no continuation ({key})"})
            drop()
            return False
        marker = {"key": key, "from_state": t["state"], "head": head, "session_id": sid,
                  "review_id": evidence.get("review_id"),
                  "verification_id": evidence.get("verification_id"),
                  "lines": list(lines[:20]), "evidence": evidence, "fallback": fallback}
        if (self.store.task(pid, tid)["data"] or {}).get(self.LC_PENDING) != marker:
            self.store.merge_data(pid, tid, **{self.LC_PENDING: marker})
        proj = self.store.project(pid)
        if proj and proj["paused"]:
            self._note(P, t, f"project paused (evidence continuation waits): "
                             f"{proj.get('pause_note') or ''}")
            return True
        if self._acquire(P, t, self._session_locks(P), writer=True):
            return True                   # waiting for the writer slot / resources
        text = (P.adapter.continue_prompt(spec, "the coordinator found evidence missing "
                                                "before review")
                + "\n\n## Missing before review (checked by the coordinator; do it in this "
                  "session)\n" + "\n".join("- " + x for x in lines[:20])
                + "\nProduce exactly this evidence at the current code: rerun the listed checks "
                  "(do not rerun what the ledger shows as verified), keep to the task scope, "
                  "commit only task files, then report. Missing evidence is BUILD work; it is "
                  "never deferred to closeout.\n")
        if self._lc(P, "evidence"):
            block = self._lc_safe(P, tid, "prompt_block", lambda: self.ledger.prompt_block(
                P, spec, head, cap=int(self._lcv(P, "prompt_cap", 6000))), "")
            if block:
                text += "\n" + block + "\n"
        try:
            sent = self._prompt_primary(P, t, text, key)
        except Exception as e:                                    # noqa: BLE001
            # nothing was sent (_prompt_primary forgot its journal entry): never
            # hold the session / writer locks across ticks on it
            self._release_session(P, t)
            self.store.event(pid, tid, "warning", session_id=sid, detail={
                "continuation": f"prompt failed ({key}): {type(e).__name__}: {e}"[:400]})
            drop()
            return False
        # the charge and the transition commit together: a restart before them
        # finds the same key (journaled, not resent) and the pending marker;
        # after them, IMPLEMENTING
        try:
            with self.store.tx():
                self._lc_charge(P, t, "same_session_continuations", f"continuation:{sid}:{key}")
                self.store.event(pid, tid, "same_session_continuation", session_id=sid, detail={
                    "key": key, "sent": sent, "missing": lines[:20]})
                drop()
                self.store.transition(pid, tid, S.IMPLEMENTING, {
                    **evidence, "session_id": sid, "continuation": key, "missing": lines[:20]},
                    phase="implement", head_sha=head,
                    next_action="session working (evidence continuation)")
        except Exception:
            # the prompt is journaled and the marker kept: the next tick takes the
            # locks again and finishes the transition without resending
            self._release_session(P, t)
            raise
        return True

    # -- REVIEWING -------------------------------------------------------------------

    def _lc_review_items(self, P: Project, spec, head: str, evidence: list) -> list:
        """The ledger's exact stored outputs first (obligation-linked checks get
        priority), then the adapter's items for paths the ledger doesn't cover."""
        items = self._lc_safe(P, spec.task_id, "review_items", lambda: self.ledger.review_items(
            P, spec, head, cap_chars=int(self._lcv(P, "prompt_cap", 6000)) * 2), []) or []
        if not items:
            return evidence
        linked = set()
        if self._lc(P, "obligations"):
            linked = {str(e["ref"]) for o in self.obligations.all(P.id, spec.task_id)
                      if o["phase"] == "BUILD" for e in o["required_evidence"]
                      if e["kind"] == "check"}
        for it in items:
            if it["title"].split(":", 1)[0] in linked:
                it["priority"] = min(int(it.get("priority", 2)), 1)
        paths = {it["path"] for it in items if it.get("path")}
        return items + [e for e in evidence if not e.get("path") or e["path"] not in paths]

    def _lc_apply_review(self, P: Project, t: dict, rev: dict) -> bool:
        """Route a review (True = handled here). APPROVED: promote and link,
        then the normal path. CHANGES_REQUIRED: product_changes -> the normal
        repair path (plus repeat fingerprints); evidence_incomplete -> recover
        and re-review once, else a same-session continuation or an
        evidence-only repair (no ordinary attempt); access_blocked -> a decision,
        no session."""
        pid, tid = P.id, t["task_id"]
        res = rev["result"] or {}
        rid, head = rev["review_id"], rev["reviewed_sha"]
        if rev["verdict"] == "APPROVED":
            def promote():
                spec = P.adapter.describe(tid)
                for rec in self.ledger.current(P, spec, head).values():
                    if rec["tier"] in ("verified",) and not rec["truncated"]:
                        self.ledger.promote(rec["record_id"], "reviewed", actor="engine",
                                            ref_kind="review", ref_id=rid)
                if self._lc(P, "obligations"):
                    self.obligations.review_link(P, t, rid, "APPROVED", head)
            if self._lc(P, "evidence"):
                self._lc_safe(P, tid, "promote_reviewed", promote)
            return False
        if rev["verdict"] != "CHANGES_REQUIRED" or not self._lc(P, "recovery"):
            return False
        from .recovery import review_route
        spec = P.adapter.describe(tid)
        pk = res.get("packet") or {}
        state = {}
        if self._lc(P, "evidence"):
            state["missing"] = self.ledger.missing(P, spec, head)
        route = review_route(res, {"omitted": pk.get("omitted") or []}, state)
        self.store.event(pid, tid, "review_routed", detail={"review_id": rid, "route": route,
                                                            "reviewed_commit_sha": head})
        if route == "product_changes":
            items = [{"source": "review", "item": f if isinstance(f, dict) else {"text": str(f)}}
                     for f in res.get("findings") or []]
            items += [{"source": "review", "item": {"kind": "missing_evidence", "text": str(m)}}
                      for m in res.get("missing_evidence") or []]
            self._lc_safe(P, tid, "record_findings", lambda: self.recovery.record_findings(
                P, t, self._lc_attempt_no(P, t), items))
            if self._lc(P, "obligations"):
                self._lc_safe(P, tid, "review_link", lambda: self.obligations.review_link(
                    P, t, rid, "CHANGES_REQUIRED", head))
            return False
        again = list(dict.fromkeys([*(t["data"].get("lc_rereview") or []), rid]))
        if route == "access_blocked":
            did = self.obligations.open_decision(
                pid, tid, "review_access",
                f"The reviewer could not access the review packet or its evidence for "
                f"{head[:12]} (review {rid})",
                evidence={"review_id": rid, "findings": (res.get("findings") or [])[:5],
                          "missing_evidence": (res.get("missing_evidence") or [])[:5]},
                options=["access fixed: review again", "stop the task"],
                recommendation="fix the reviewer's access, then review the same commit again",
                blocks=[tid])
            self.store.merge_data(pid, tid, lc_rereview=again)
            self._lc_wait(P, self.store.task(pid, tid), [did], "the reviewer could not access "
                          "the evidence; no session is started", S.VERIFYING, {"review_id": rid})
            return True
        # evidence_incomplete
        self._lc_recover(P, t)
        missing = self.ledger.missing(P, spec, head) if self._lc(P, "evidence") else []
        heads = list(t["data"].get("lc_rereviewed") or [])
        if not missing and head not in heads:
            # the ledger has the evidence now: one re-review of the same commit
            # (charges review_calls, never an attempt)
            self.store.merge_data(pid, tid, lc_rereview=again, lc_rereviewed=heads + [head])
            self.store.event(pid, tid, "review_again", detail={
                "review_id": rid, "reason": "evidence_incomplete; the ledger now holds the "
                                            "evidence", "reviewed_commit_sha": head})
            self._note(P, t, "review asked for evidence the ledger holds; reviewing again")
            return True
        lines = [f"check {m['check_id']}: {m['reason']} ({m.get('detail') or ''})"[:400]
                 for m in missing]
        lines += [f"reviewer: {m}"[:400] for m in res.get("missing_evidence") or []]
        lines = lines or ["the reviewer found the evidence incomplete"]
        self.store.merge_data(pid, tid, lc_rereview=again)
        t = self.store.task(pid, tid)
        findings = ["evidence-only repair (the review asked for evidence, not product "
                    "changes): " + x for x in lines]
        fb = {"kind": "review", "review_id": rid, "head": head, "findings": findings}
        lim = self.obligations.effective_limits(P, t)["missing_evidence_continuations"]
        if lim["remaining"] > 0 and self._lc_continue(
                P, t, spec, head, lines, f"review:{rid}:{lim['used']}", {"review_id": rid}, fb):
            return True
        self._lc_fallback(P, t, fb)
        return True

    # -- operator decisions ------------------------------------------------------------

    def decision_answer(self, project_id: str, task_id: str, decision_id: str, answer,
                        option: str = "", actor: str = "operator", note: str = "",
                        question_hash: str = "", channel: str = "") -> dict:
        """An operator's answer to an open decision (authority is always the
        operator here). Raises DecisionRefused / DecisionConflict / ValueError;
        the engine resumes a task waiting on it on its next tick."""
        import json as _json
        from .obligations import DecisionRefused
        P = self._project(project_id)
        if not self.store.q("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                            "name='decisions'"):
            raise DecisionRefused(f"unknown decision {decision_id} (no lifecycle decisions in "
                                  f"this coordinator)")
        d = self.obligations.decision(decision_id)
        if not d or d["project_id"] != P.id or d["task_id"] != task_id:
            raise DecisionRefused(f"unknown decision {decision_id} for {project_id}/{task_id}")
        if question_hash:
            # the engine's hash of the stored question (decision_wait.question_hashes),
            # or the control center's hash of the text it shows (the first
            # LC_UI_QUESTION_CHARS characters), so a long question stays answerable
            q = d["question"]
            accepted = {self._lc_qhash(q)}
            if isinstance(q, str):
                accepted.add(self._lc_qhash(q[:self.LC_UI_QUESTION_CHARS]))
            if str(question_hash) not in accepted:
                raise ValueError("the question changed since the operator read it; nothing "
                                 "was recorded")
        text = str(answer if answer is not None else "").strip()
        typed = None
        if text.startswith("{"):
            try:
                typed = _json.loads(text)
            except ValueError:
                typed = None
        if not isinstance(typed, dict):
            typed = None
        if typed is None and d["kind"] in ("recovery_escalation", "no_progress"):
            from .recovery import Recovery
            if Recovery._STOP.match(text) or Recovery._STOP.match(option or ""):
                # "stop the task" (typed or chosen) is a stop, never a strategy
                typed = {"stop": True, "option": option, "reason": text or option}
            else:
                typed = {"strategy": text, "option": option} if text else {"option": option}
        value = typed if typed is not None else (text or option)
        if value in ("", None, {}):
            raise ValueError("an answer or an option is required")
        import inspect
        kw = {"channel": channel} if "channel" in inspect.signature(
            self.obligations.answer).parameters else {}
        out = self.obligations.answer(decision_id, value, actor, "operator", **kw)
        self.store.event(project_id, task_id, "decision_answered", detail={
            "decision_id": decision_id, "kind": d["kind"], "actor": actor,
            "note": str(note or "")[:300]})
        return out

    def decision_reevaluate(self, decision_id: str, actor: str, reason: str = "",
                            dry_run: bool = False) -> dict:
        """Re-run the no-progress or premature-repair decision against the
        current committed partial-BUILD state.

        A genuine stall or repair without a safe BUILD continuation stays open.
        Progress recognized: the decision is WITHDRAWN -- kept, never deleted --
        with a 'decision_reevaluated' event naming the actor, the reason, the
        decision's original evidence and the new result; the engine resumes the
        waiting task on its next tick (_lc_blocked: a withdrawn decision without
        a successor is resolved). When the task's committed HEAD is exactly the
        state the partial-BUILD continuation accepts (open plan steps; scope,
        clean-but-for-intermediate-outputs and history checks pass), its stale
        final-verification findings are replaced by that continuation, as the
        fixed engine would have done at that session's end. Attempts, sessions
        and the budget ledger are never touched. dry_run reports only.

        Operator procedure (after deploying the fix):
          python -m foundry_coordinator decision reevaluate <decision-id> \\
              --by <operator> --reason "false no-progress ([~] + intermediate gate
              outputs), fixed in vX.Y.Z" [--dry-run]"""
        from .obligations import DecisionRefused, is_model_actor
        if is_model_actor(actor) or not str(actor or "").strip():
            raise DecisionRefused(f"actor {actor!r} may not re-evaluate decisions")
        if not self.store.q("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                            "name='decisions'"):
            raise DecisionRefused(f"unknown decision {decision_id}")
        d = self.obligations.decision(decision_id)
        if not d:
            raise DecisionRefused(f"unknown decision {decision_id}")
        pid, tid = d["project_id"], d["task_id"]
        P = self._project(pid)
        if not self._lc(P, "recovery"):
            raise DecisionRefused(f"{pid} has no lifecycle recovery to re-evaluate")
        if d["kind"] not in ("no_progress", "recovery_escalation"):
            raise DecisionRefused(f"{decision_id} is a {d['kind']} decision; only no_progress "
                                  "and premature BUILD recovery_escalation decisions can be "
                                  "re-evaluated (answer it instead)")
        if d["status"] != "open":
            raise DecisionRefused(f"{decision_id} is {d['status']}; nothing to re-evaluate")
        t = self.store.task(pid, tid)
        try:
            spec = P.adapter.describe(tid)
        except AdapterError:
            spec = None
        pr = self._lc_progress(P, t, spec)
        out = {"decision_id": decision_id, "project": pid, "task": tid, "progress": pr,
               "previous_evidence": d.get("evidence"), "withdrawn": False,
               "continuation": None, "dry_run": bool(dry_run)}
        if d["kind"] == "no_progress" and pr["no_progress"]:
            out["result"] = "still no progress: the decision stays open"
            if not dry_run:
                self.store.event(pid, tid, "decision_reevaluated", detail={
                    "decision_id": decision_id, "by": actor, "reason": str(reason)[:500],
                    "result": "unchanged", "progress": pr})
            return out
        cont = None
        head = P.ws.head()
        pending_fn = getattr(P.adapter, "pending_build_steps", None)
        if spec is not None and callable(pending_fn) and t["state"] == S.BLOCKED \
                and t.get("head_sha") == head and head != t["base_sha"]:
            pending = pending_fn(spec, head, P.ws)
            if pending:
                basics = self._generic_checks(P, t, spec, head, phase="verify",
                                              intermediate=True)
                if all(r.result == PASS for r in basics if r.required):
                    cont = {"head_sha": head, "open_steps": pending[:12],
                            "total_steps": self._build_total(P, spec, head),
                            "intermediate_outputs": next(
                                (r.detail for r in basics
                                 if r.check == "coordinator.intermediate_outputs"), ""),
                            "replaced_findings": list(t["data"].get("findings") or [])[:12]}
        if d["kind"] == "recovery_escalation" and cont is None:
            out["result"] = "no committed partial-BUILD continuation: decision stays open"
            if not dry_run:
                self.store.event(pid, tid, "decision_reevaluated", detail={
                    "decision_id": decision_id, "by": actor, "reason": str(reason)[:500],
                    "result": "unchanged", "progress": pr})
            return out
        out.update(result="progress recognized: decision withdrawn", withdrawn=True,
                   continuation=cont)
        if dry_run:
            out["result"] = "progress recognized (dry run: nothing changed)"
            return out
        with self.store.tx():
            if (self.obligations.decision(decision_id) or {}).get("status") != "open":
                raise DecisionRefused(f"{decision_id} changed while it was re-evaluated")
            self.store.x("UPDATE decisions SET status='withdrawn' WHERE decision_id=? AND "
                         "status='open'", (decision_id,))
            if cont:
                self.store.merge_data(pid, tid, findings=None, build_progress=cont["open_steps"],
                                      free_retry=True, build_resume={
                                          k: cont[k] for k in ("head_sha", "open_steps",
                                                               "total_steps")
                                          if cont.get(k) is not None} | {
                                          "decision_id": decision_id})
            self.store.event(pid, tid, "decision_reevaluated", detail={
                "decision_id": decision_id, "by": actor, "reason": str(reason)[:500],
                "result": "withdrawn", "progress": pr,
                "previous_evidence": d.get("evidence"), "continuation": cont})
        return out

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

    def _held(self, P: Project, t: Optional[dict], what: str) -> bool:
        """True while an operator has the project paused (CLI `project pause`
        or the control center's project_pause): the one check every point that
        would START something new uses (a session, a prompt to a primary, a
        review call, a helper/lane dispatch). A running turn is never touched
        here; it finishes and is polled as usual. The flag is in the store, so
        it holds across a coordinator restart."""
        proj = self.store.project(P.id)
        if not (proj and proj["paused"]):
            return False
        if t is not None:
            self._note(P, t, f"project paused ({what} waits): {proj.get('pause_note') or ''}")
        return True

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
