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
import re
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

# every implementer prompt of a track sprint (0.145.0): its session tried to run
# the Studio gate itself, hit the main sprint's Studio lock and went BLOCKED
TRACK_BRIEF = """
## Track sprint: no Studio here
This sprint runs on the parallel track `{track}` (git worktree `{workspace}`), beside the
main sprint, which owns Studio and Rojo. Do not run the Studio gate, a playtest or a boot
check, and do not take or wait for studio.lock: the coordinator runs the Studio gate on the
merge result in the main checkout when this track merges. Finish the code, the
Studio-free checks (Lune, the sprint's unit checks, done_check without GATE / BOOT /
EVIDENCE) and the plan, commit, and report DONE. Never report BLOCKED on Studio or
studio.lock.
"""
# a track session's BLOCKED report about Studio, Rojo, the gate or studio.lock
TRACK_REVIEW_NOTE = ("track sprint: this work ran in a git worktree without Studio. The "
                     "Studio gate, boot checks, Studio suites and the gate evidence log "
                     "are run by the merge gate in the main checkout on the merge result, "
                     "so their absence here is expected: do not ask for them. Review the "
                     "code, the Lune results and done_check.")
TRACK_STUDIO_BLOCKER = re.compile(r"studio\.lock|\bstudio\b|\brojo\b|playtest|studio[_ ]gate|"
                                  r"\bgate\b[^.\n]{0,40}\b(?:run|lock|studio)", re.I)


def _nothing_left_to_repair(results: list) -> None:
    """`repair_changed` fails a repair session that committed nothing. When every
    other required check passes at this head, the earlier findings no longer apply
    (a coordinator fix cleared them, or another sprint's merge did): nothing was
    left to repair, so it passes. Before 0.145.2 an unparked GF-NAME1 failed on
    this alone and went straight to "findings repeat" (2026-10-27)."""
    rc = [r for r in results if r.check == "coordinator.repair_changed" and r.result == FAIL]
    if not rc:
        return
    if any(r.required and r.result in (FAIL, UNKNOWN) and r.check != "coordinator.repair_changed"
           for r in results):
        return
    for r in rc:
        r.result = PASS
        r.detail = ("nothing left to repair: every other check passes at this head (the "
                    "earlier findings no longer apply)")


def _prio(item) -> int:
    try:
        return int((item or {}).get("priority") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0
ROUTE_RETRY_S = 300


class Coordinator:
    # ceiling of the heartbeat cover over one verify / closeout check run
    # (keepalive.busy); past it a stuck check lets the heartbeat go stale
    VERIFY_COVER_S = 5400            # two 30-min Lune runs + done_check (AU)

    def __init__(self, store: Store, projects: list[Project], state_dir: str,
                 max_writers: Optional[int] = None, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = lambda m: None, playbook_path: str = ""):
        self.store = store
        self.projects = {p.id: p for p in projects}
        self.state_dir = state_dir
        # Team Lead (2026-10-22): the writer cap is the live [automation] max_agents
        # (tracks/manager.py); an explicit --max-writers stays as an override
        self.max_writers_override = max(1, int(max_writers)) if max_writers else None
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
        from .fast_lane import FastLane
        # [fast_lane] (UI-T0): studio_gate-verified one-session sprints; inert
        # for every task the profile's [fast_lane] table doesn't name
        self.fast_lane = FastLane(self, clock=clock)
        from .bg_checks import BackgroundChecks
        self.bg_checks = BackgroundChecks()      # 0.147.0: verify/closeout off the tick
        from .memory import MemoryManager
        # Team Lead piece 4: project memory (docs/memory in the repo), indexed here and
        # sliced per session; inert for a project without the layout
        self.memory = MemoryManager(self)
        from .playbook import PlaybookManager
        # A1: the cross-project playbook (its own repo, --playbook / $FOUNDRY_PLAYBOOK);
        # inert without a configured path
        self.playbook = PlaybookManager(self, playbook_path)
        # B4: code reviews started early (while vision runs), by (project, task, head)
        self._early_reviews: dict = {}
        self._early_pool = None
        self._live_audit_ts: dict[str, float] = {}     # session -> last live checkpoint write
        self._q_missing_since: dict[str, float] = {}   # request id -> first seen withdrawn
        # [lifecycle] (v0.122): evidence ledger, obligations/decisions/budget
        # ledger, recovery. Built lazily, and only used for projects whose
        # profile enables them; a disabled project never touches their tables.
        self._ledger = None
        self._obligations = None
        self._recovery = None
        # [lead_manager] (v0.123): the last automated recovery tier (lead.py).
        # Built lazily and only when some project enables it; `lead` is None
        # otherwise, and a disabled project never touches its tables.
        self._lead = None
        self._lead_assigned = False
        for p in projects:
            store.register_project(p.id, p.profile.name, p.profile.path, p.profile.hash,
                                   p.profile.version, os.path.realpath(p.profile.workspace))
        from .tracks import TrackManager
        # Team Lead tracks: parallel sprints on git worktrees, the Studio scheduler
        # and merges back at a quiescent point; inert until a track exists
        self.tracks = TrackManager(self, clock=clock)

    @property
    def max_writers(self) -> int:
        """Writer slots in force: --max-writers when given, else [automation] max_agents."""
        return self.tracks.max_agents() if hasattr(self, "tracks") else \
            (self.max_writers_override or 1)

    def _task_project(self, project_id: str, task_id: str) -> Project:
        """The Project a task runs with: its track's worktree, or the main checkout."""
        P = self._project(project_id)
        return self.tracks.project_for(P, {"task_id": task_id})

    # ======================================================================= queue

    def approve(self, project_id: str, task_id: str, deps: list[str], approver: str,
                note: str = "", adopt_existing: Optional[list[str]] = None,
                track: str = "") -> dict:
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
            entry = self.store.approve(project_id, task_id, deps, approver, note, track=track)
            if adopted:
                self.store.merge_data(project_id, task_id, inherited_work=adopted)
                self.store.event(project_id, task_id, 'work_adopted', detail=adopted)
        return entry

    def reopen(self, project_id: str, task_id: str, deps: list[str], approver: str,
               reason: str = "") -> dict:
        """Put a CANCELLED task back on the queue (fresh attempt counters, no run or
        session, cleared findings); the transition history keeps the earlier runs."""
        t = self.store.task(project_id, task_id)
        if t is None:
            return self.approve(project_id, task_id, deps, approver, reason)
        if t["state"] != S.CANCELLED:
            raise ValueError(f"{project_id}/{task_id} is {t['state']}, not CANCELLED")
        for d in deps:
            if d == task_id or not self.store.task(project_id, d):
                raise ValueError(f"dependency {d} is not on {project_id}'s queue")
        keep = {k: v for k, v in (t.get("data") or {}).items()
                if k in ("baseline_dirty", "task_base")}
        with self.store.tx():
            self.store.transition(project_id, task_id, S.QUEUED,
                                  {"reopened_by": approver, "reason": reason or "reopened"},
                                  actor=approver)
            self.store.update_task(project_id, task_id, attempt=0, sessions=0, run_id=None,
                                   session_id=None, phase=None, base_sha=None, head_sha=None,
                                   reviewed_sha=None, review_id=None, verification_id=None,
                                   closeout_id=None, next_action=None, started_ts=None,
                                   data={**keep, "reopened": {"by": approver, "reason": reason}})
            entry = self.store.approve(project_id, task_id, deps, approver, reason)
        return entry

    def move(self, project_id: str, task_id: str, position: int, actor: str) -> list[dict]:
        """Move a queued row to a 1-based position (queue order = priority).
        Presentation of the queue only; no task changes state."""
        self._project(project_id)
        entry = self.store.queue_entry(project_id, task_id)
        if entry is None:
            raise ValueError(f"{project_id}/{task_id} is not on the queue")
        with self.store.tx():
            out = self.store.move(project_id, task_id, int(position))
            self.store.event(project_id, task_id, "queue_moved",
                             detail={"from": entry["position"], "to": int(position),
                                     "actor": actor})
        return out

    def set_deps(self, project_id: str, task_id: str, deps: list[str], actor: str) -> dict:
        """Replace a QUEUED task's dependencies. Deps are consumed at
        QUEUED→READY, so edits after that are refused."""
        self._project(project_id)
        t = self.store.task(project_id, task_id)
        if t is None:
            raise ValueError(f"unknown task {project_id}/{task_id}")
        if t["state"] != S.QUEUED:
            raise ValueError(f"{project_id}/{task_id} is {t['state']}; dependencies can "
                             f"only change while QUEUED")
        deps = sorted(set(deps))
        for d in deps:
            if ":" in d or "/" in d:
                raise ValueError(f"dependency {d!r} names another project; cross-project "
                                 f"dependencies aren't allowed")
            if d == task_id:
                raise ValueError("a task can't depend on itself")
            if not self.store.queue_entry(project_id, d):
                raise ValueError(f"dependency {d} is not on {project_id}'s approved queue")
        if self._deps_cycle(project_id, task_id, deps):
            raise ValueError("those dependencies make a loop")
        old = self.store.queue_entry(project_id, task_id)
        with self.store.tx():
            entry = self.store.set_deps(project_id, task_id, deps)
            self.store.event(project_id, task_id, "deps_changed",
                             detail={"from": (old or {}).get("deps") or [], "to": deps,
                                     "actor": actor})
        return entry

    def _deps_cycle(self, project_id: str, task_id: str, deps: list[str]) -> bool:
        graph = {e["task_id"]: list(e["deps"] or []) for e in self.store.queue(project_id)}
        graph[task_id] = list(deps)
        seen, stack = set(), list(deps)
        while stack:
            n = stack.pop()
            if n == task_id:
                return True
            if n in seen:
                continue
            seen.add(n)
            stack.extend(graph.get(n, []))
        return False

    # ======================================================================= loop

    def tick(self) -> list[str]:
        acted: list[str] = []
        ids = sorted(self.projects)
        if not ids:
            return acted
        try:
            from . import decision_policy       # revamp B11: decisions past their timeout
            decision_policy.sweep(self)
        except Exception as e:                                    # noqa: BLE001
            self.log(f"decision timeout sweep failed: {type(e).__name__}: {e}")
        try:
            # A1: re-index the cross-project playbook and apply answered
            # promotion decisions (both inert without a configured playbook)
            self.playbook.tick()
            self.playbook.promote.sweep()
        except Exception as e:                                    # noqa: BLE001
            self.log(f"playbook tick failed: {type(e).__name__}: {e}")
        try:
            # 0.147.0: forget background check runs of tasks that left the state
            # they were started in (cancelled, paused, unblocked, head moved on)
            want = {"verify": S.VERIFYING, "closeout": S.CLOSING}
            self.bg_checks.drop(lambda k: (self.store.task(k[0], k[1]) or {}).get("state")
                                == want.get(k[2]))
        except Exception as e:                                    # noqa: BLE001
            self.log(f"background checks sweep failed: {type(e).__name__}: {e}")
        rr = int(self.store.meta("rr") or 0) % len(ids)
        order = ids[rr:] + ids[:rr]
        self.store.set_meta("rr", str(rr + 1))
        for pid in order:
            P = self.projects[pid]
            self._queue_approved_followups(P)
            try:
                self.tracks.tick(P)                          # track lifecycle + merges
            except Exception as e:                            # noqa: BLE001
                self.log(f"{pid}: tracks tick failed: {type(e).__name__}: {e}")
            try:
                self.memory.tick(P)
            except Exception as e:                                # noqa: BLE001
                self.log(f"{pid}: memory tick failed: {type(e).__name__}: {e}")
            for t in self.store.tasks(pid):
                # 0.147.0: re-read the row right before its step. An earlier task's
                # long step (a 13-min verify) left this snapshot stale, and an
                # operator unblock made meanwhile was overwritten by it (GF-NAME1,
                # 2026-10-27: its resolution note was lost, the old findings re-asked)
                t = self.store.task(pid, t["task_id"]) or t
                before = (t["state"], t["updated_ts"])
                try:
                    # a sprint queued on a track runs with the track's worktree
                    self._step(self.tracks.project_for(P, t), t)
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

    def _queue_approved_followups(self, P: Project) -> None:
        """Queue explicitly pre-approved successors once their parent is queued.

        This preserves the normal dependency gate: a followup cannot run before
        its parent completes, and no model can invent or approve a new task.
        """
        followups = P.profile.raw.get("auto_queue_followups") or {}
        if not isinstance(followups, dict):
            return
        for parent, children in followups.items():
            if not self.store.task(P.id, parent) or not isinstance(children, list):
                continue
            for task_id in children:
                if not isinstance(task_id, str) or self.store.task(P.id, task_id):
                    continue
                try:
                    self.approve(P.id, task_id, [parent], "Victor",
                                 f"pre-approved followup after {parent}")
                except Exception as exc:  # noqa: BLE001
                    self.store.event(P.id, parent, "followup_queue_error",
                                     detail={"followup": task_id, "error": str(exc)[:300]})

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
        if st == S.BLOCKED and (t.get("data") or {}).get("auto_heal"):
            from . import autoheal                        # revamp B9: a scheduled retry
            if autoheal.step(self, P, t):
                return
            t = self.store.task(P.id, t["task_id"])
        if st == S.BLOCKED and (t.get("data") or {}).get("recovery_case") and self._lead_on(P):
            return self._lead_blocked(P, t)
        if (t.get("data") or {}).get("recovery_case") and not self._lead_on(P):
            # [lead_manager] turned off while a case is live: the case is cancelled
            # (sessions aborted, an unstarted upgrade cancelled) and the task
            # keeps its original blocker for the operator
            self._lead_disabled(P, t)
            t = self.store.task(P.id, t["task_id"])
            st = t["state"]
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
        elif attempt > lim.limit("max_attempts", 3) + self.retry_guidance.allowance(t) \
                + self._lead_allowance(t):
            if self.retry_guidance.consult(P, t):
                return
            if ((self.store.task(pid, tid).get("data") or {}).get("retry_guidance") or {}).get(
                    "status") == "deferred":
                return
            lead_extra = self._lead_allowance(t)
            return self._stop(P, t, S.FAILED, {
                "reason": f"{t['attempt']} implementation attempts used "
                          f"(max_attempts {lim.limit('max_attempts', 3)}, "
                          f"guided extra {self.retry_guidance.allowance(t)}"
                          + (f", lead extra {lead_extra}" if lead_extra else "") + ")"},
                cls="attempts_exhausted")
        if not lc_budget and t["sessions"] >= lim.limit("max_sessions_per_task", 8) \
                + self._lead_allowance(t):
            return self._stop(P, t, S.FAILED, {
                "reason": f"{t['sessions']} sessions used (max_sessions_per_task)"},
                cls="sessions_exhausted")
        minutes = lim.limit("max_task_minutes", 0)
        if minutes and t["started_ts"] and self.clock() - t["started_ts"] > minutes * 60:
            return self._stop(P, t, S.FAILED, {
                "reason": f"over max_task_minutes ({minutes})"}, cls="task_minutes")
        try:
            spec = P.adapter.describe(tid)
        except AdapterError as e:
            return self._block(P, t, f"task description unavailable: {e}")
        from . import infra                        # revamp B12: infra before a session
        note = infra.waiting(self, P, t)
        if note:
            return self._note(P, t, note)
        from . import services                     # Team Lead 5: the sprint's needs are up
        if services.blocks(self, P, t, spec):
            return                                 # "waiting for <service>": no lock, no attempt
        held = self._acquire(P, t, self._workspace_locks(P) + self._session_locks(P),
                             writer=True)
        if held:
            return
        if infra.preflight_blocks(self, P, t, P.profile.persona("implementer", tid)):
            self.store.release(pid, tid)
            return
        if self._lc(P, "readiness"):
            # [lifecycle] the environment/tooling must be ready before a session
            # is started; a blocking failure consumes no attempt
            r = self._lc_readiness(P, t, spec, {"attempt": attempt, "base": t["base_sha"]})
            if r:
                return self._lc_block_readiness(P, t, r)
        if self.fast_lane.on(P, tid):
            if self.fast_lane.baseline_hold(P, t):
                return   # B2: the sprint's automatic baseline runs before its first session
            t = self.store.task(pid, tid)        # keep the baseline record it just saved
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
        # A free partial-BUILD continuation may carry this task's own
        # unfinished files. Name them in the next prompt, but do not classify
        # them as a failed repair and resurrect historical repeat findings.
        carry_own = own if free and bool(t["data"].get("build_progress")) else []
        if own and not carry_own:
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
        if self.tracks.is_track(P):
            prompt += TRACK_BRIEF.format(track=self.tracks.track_name(P),
                                         workspace=P.profile.workspace)
        if inter:
            prompt += ("\n## Intermediate gate outputs\nThese task-owned files are untracked "
                       "intermediate gate output: " + ", ".join(inter[:8]) + ". They do not "
                       "block BUILD progress; commit them with the final gate run (or let the "
                       "gate regenerate them). Final verification requires a clean tree.\n")
        if carry_own:
            prompt += ("\n## Unfinished task-owned BUILD work\nInspect and finish these "
                       "files left by the prior UI-02 session: " + ", ".join(carry_own[:8])
                       + ". Commit each only after its required evidence check. They remain "
                       "unaccepted until final clean-tree verification.\n")
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
        if data.get("lead_assignment"):
            # [lead_manager] a lead assignment rides in exactly one session start
            data["lead_assignment_used"] = {**data.pop("lead_assignment"), "run_id": run_id}
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
                permissions=self._session_rules(P, spec, phase, role))
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
            prompt, notes = self._with_operator_notes(P.id, tid, prompt, "",
                                                      memory_for=sid)
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

    # W1.1 (Victor 2026-10-22): an implementer session never asks a person. Kilo's
    # `question` tool is denied (a session-level deny removes the tool), appended last
    # so no profile or role rule re-allows it; the prompt says to report BLOCKED with
    # options instead (adapters.base.report_instructions).
    IMPLEMENTER_DENIES = ({"permission": "question", "pattern": "*", "action": "deny"},)

    def _session_rules(self, P: Project, spec, phase: str, role: str) -> list[dict]:
        rules = [dict(r) for r in (P.adapter.session_permissions(spec, phase) or [])]
        if role == "implementer":
            rules += [dict(r) for r in self.IMPLEMENTER_DENIES]
        return rules

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
        # W1.4: commit messages without the task prefix are the coordinator's to fix,
        # on its own unpushed commits, before anything (gate, review, approvals) is
        # bound to this head; the session is never asked to rebase
        self._reword_commits(P, t)
        head = P.ws.head()
        rep = s.report or {}
        data = self.store.merge_data(pid, tid, last_report=rep or None)
        self._memory_notes(P, t, s)          # MEMORY_NOTE lines: committed by the coordinator
        if self._lc(P):
            self._lc_session_ended(P, t, t["session_id"], head, rep)
        from . import infra
        infra.session_ok(self, P, tid)
        handoff = infra.over_ceiling(P, s, rep)
        if handoff:
            # revamp B12: a fresh session continues instead of compacting until Kilo
            # gives up (UI-02b lost 85 min that way); no attempt is charged
            self.store.event(pid, tid, "context_handoff", session_id=t["session_id"],
                             detail={"context": getattr(s, "context", None),
                                     "why": handoff[:300]})
            return self._session_failed(P, t, handoff, "implement", free=True)
        status = str(rep.get("status") or "").upper()
        # A session can commit legitimate BUILD progress and still hit a hard
        # scope or operator blocker.  Do not reinterpret its explicit stop as
        # permission to start another partial-BUILD session merely because
        # HEAD advanced: that repeats the same blocker until the session
        # budget is exhausted.  The blocked path records the completed commit
        # and waits for the blocker to be resolved.
        blocked = status in ("BLOCKED", "NEEDS_HUMAN")
        if self.helpers.enabled(P):
            # revamp B5: delivered helper results are judged by the consumer's commits
            self.helpers.measure_git(P, tid)
        if self.helpers.enabled(P) and rep.get("helper_feedback"):
            self.helpers.record_feedback(P, self.store.task(pid, tid), rep)
        if self._scope_requests(P, tid, s, rep):
            return                       # revamp B10: granted and continued, or a decision waits
        if self.frontier.on_idle(P, self.store.task(pid, tid), rep):
            return                       # a frontier phase started, continued or handed back
        if not blocked and self.helpers.enabled(P) and \
                self._helper_handoff(P, self.store.task(pid, tid), rep):
            return                       # the primary continues, or waits for its helpers
        if self.helpers.enabled(P):
            self.helpers.measure_git(P, tid, final=True)
            self.helpers.abort_running(P, tid, "cancelled", "the primary finished first")
            self.helpers.skip_waiting(P, tid, "the primary finished before this unit was "
                                              "admitted")
            self.helpers.expire_lane_results(P, tid, "the primary finished; lane results are "
                                                     "never delivered after verification starts")
        data = self.store.merge_data(pid, tid, helper_wait_since=None)
        if blocked and self.tracks.is_track(P) and \
                TRACK_STUDIO_BLOCKER.search(str(rep.get("blocker") or rep.get("summary") or "")):
            # a track never needs Studio (TRACK_BRIEF): its committed work goes to the
            # Studio-free verify, and the merge gate runs Studio; no decision, no lead
            self.store.event(pid, tid, "track_studio_blocker_ignored", session_id=t["session_id"],
                             detail={"blocker": str(rep.get("blocker") or "")[:300]})
            blocked = False
        if blocked:
            self.store.release(pid, tid)
            if self._blocked_decision(P, self.store.task(pid, tid), rep, status):
                return                   # W1.1: a BLOCKED-with-options report is a decision
            return self._stop(P, t, S.BLOCKED, {
                "blocker": f"session reported {status}: {rep.get('blocker') or rep.get('summary')}",
                "session_id": t["session_id"]}, cls="session_" + status.lower(),
                blocker=str(rep.get("blocker") or status)[:500])
        # A committed, partially ticked BUILD plan is progress, not a failed
        # final submission. Keep final verification and every required check for
        # the point when BUILD is actually complete. Only grant a free next
        # session if HEAD advanced and the cheap scope/clean/history checks pass;
        # otherwise the normal verifier handles the failure.
        pending_fn = getattr(P.adapter, "pending_build_steps", None)
        # ([fast_lane] tasks have no per-step plan ticking (rules card, §34 dropped):
        # one session to the gate, never a partial-BUILD session hand-over)
        if self._lc(P) and callable(pending_fn) and head != t["base_sha"]                 and not self.fast_lane.on(P, tid):
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

    def _reword_commits(self, P: Project, t: dict) -> None:
        """W1.4: reword the coordinator's own unpushed commits that lack the task
        prefix (commits.reword); journaled, never a session's job."""
        try:
            spec = P.adapter.describe(t["task_id"])
        except AdapterError:
            return
        from . import commits as CM
        try:
            CM.reword(self, P, self.store.task(P.id, t["task_id"]) or t, spec)
        except Exception as e:                                    # noqa: BLE001
            self.store.event(P.id, t["task_id"], "warning", detail={
                "commit_reword": f"{type(e).__name__}: {e}"[:400]})

    # options an implementer's BLOCKED report must carry to become a decision (W1.1)
    BLOCKED_OPTIONS = (2, 4)

    def _blocked_decision(self, P: Project, t: dict, rep: dict, status: str) -> bool:
        """W1.1 (Victor 2026-10-22): the implementer never asks a person. A BLOCKED
        report that carries 2-4 `options` (and optionally `recommended`) becomes an
        `implementer_blocked` decision: recommended = the first option unless the
        report names one, the standard timeout applies, and the task waits BLOCKED
        on it (decision_wait). A tooling question (`blocker_kind: tooling`, or any
        blocker text without options) still goes the auto-heal / lead way; an
        infrastructure signature always does. True when a decision now holds the
        task."""
        if not self._lc(P):
            return False
        pid, tid = P.id, t["task_id"]
        options = rep.get("options")
        if not isinstance(options, list):
            return False
        options = [str(o).strip()[:300] for o in options if str(o or "").strip()]
        lo, hi = self.BLOCKED_OPTIONS
        if not lo <= len(options) <= hi:
            return False
        blocker = str(rep.get("blocker") or rep.get("summary") or "").strip()
        from .autoheal import infra_signature
        if infra_signature(blocker):
            return False                 # deterministic retry / repair first
        from . import decision_policy as DP
        rec = DP.match_option(options, rep.get("recommended")) or options[0]
        kind_hint = str(rep.get("blocker_kind") or "").strip().lower()
        tooling = kind_hint in ("tooling", "harness", "environment", "infra", "infrastructure",
                                "coordinator")
        q = f"the {status.lower()} report of {tid}: {blocker[:700] or '(no blocker text)'}"
        did = self.obligations.open_decision(
            pid, tid, "implementer_blocked", q,
            evidence={"blocker": blocker[:1000], "session_id": t.get("session_id") or "",
                      "recommended": rec, "options": options,
                      "blocker_kind": "tooling" if tooling else "product",
                      "summary": str(rep.get("summary") or "")[:500]},
            options=options, recommendation=rec, blocks=[tid])
        self.store.event(pid, tid, "implementer_blocked_decision", session_id=t.get("session_id"),
                         detail={"decision_id": did, "options": options, "recommended": rec,
                                 "blocker_kind": "tooling" if tooling else "product",
                                 "blocker": blocker[:300]})
        d = self.obligations.decision(did) or {}
        if d.get("status") == "answered":
            # the same question was decided before: apply it without waiting
            self._lc_apply_answers(P, self.store.task(pid, tid), [d])
            if self._lc_stop_answer([d]):
                self._lc_stop_block(P, self.store.task(pid, tid), d,
                                    self._lc_stop_answer([d])[1])
                return True
            self._release_session(P, t)
            self.store.transition(pid, tid, S.CHANGES_REQUIRED, {
                "findings": list((self.store.task(pid, tid)["data"] or {}).get("findings")
                                 or [f"decision {did} answered earlier"])},
                next_action=f"continue per decision {did}")
            return True
        self._lc_wait(P, self.store.task(pid, tid), [did],
                      f"the session's BLOCKED report offered options; recommended "
                      f"{rec!r} applies after the timeout", S.READY,
                      evidence={"session_id": t.get("session_id") or ""})
        return True

    def _scope_requests(self, P: Project, tid: str, s, rep: dict) -> bool:
        """revamp B10: typed scope requests of an idle primary (scope.py). True
        when the session was continued after a grant (or a decision waits)."""
        from . import scope as SC
        reqs = SC.parse_requests(getattr(s, "final_text", "") or "", rep)
        if not reqs:
            return False
        try:
            spec = P.adapter.describe(tid)
        except AdapterError:
            return False
        return SC.handle(self, P, self.store.task(P.id, tid), spec, reqs) in (
            "continued", "waiting")

    def _poll(self, P: Project, t: dict, sid: str, phase: str):
        """Poll a session. Returns the idle SessionState, or None after
        handling anything else (still busy, question, error, denial)."""
        s = self._poll_inner(P, t, sid, phase)
        # the handlers below return transition rows (dicts); only an idle
        # session's state may reach the caller
        return s if isinstance(s, SessionState) else None

    def _live_checkpoint(self, P: Project, t: dict, sid: str) -> None:
        """Refresh the adapter's live checkpoint file for a working session, at
        most every `live_checkpoint_seconds` (0 = off). The session reads the
        file instead of calling the authenticated Kilo server itself."""
        fn = getattr(P.adapter, "live_session_audit", None)
        every = P.profile.limit("live_checkpoint_seconds", 120)
        if not callable(fn) or not every:
            return
        last = self._live_audit_ts.get(sid, 0.0)
        if self.clock() - last < float(every):
            return
        self._live_audit_ts[sid] = self.clock()
        try:
            tr = P.client.transcript(sid, directory=P.profile.workspace)
            if tr:
                fn(sid, tr, P.ws.head())
        except Exception as e:                                    # noqa: BLE001
            # advisory only: a failed refresh never changes the task
            self.store.event(P.id, t["task_id"], "live_checkpoint_failed", session_id=sid,
                             detail={"error": str(e)[:300]})

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
            try:
                # the main session's last Studio tool call (the Studio scheduler's
                # "in use" fact); rate limited, only while a track exists
                self.tracks.observe_session(P, t, sid)
            except Exception:                                     # noqa: BLE001
                pass
            if s.denial:
                P.client.abort(sid, directory=directory)
                return self._on_denial(P, t, s.denial, s.error, resume=t["state"], session=sid)
            if s.unanswered and self.clock() - prompt_ts > float(
                    P.profile.client.get("prompt_admission_timeout_seconds") or 180):
                # Kilo reports this session idle, with no assistant turn for the
                # prompt. A normal (possibly slow) model call reports busy and
                # does not enter this branch. Do not wait for the much longer
                # session execution timeout when the prompt never started.
                P.client.abort(sid, directory=directory)
                self._end_session(P, sid, "prompt_not_admitted")
                return self._session_failed(P, t, "Kilo did not admit the prompt within "
                                            "the admission window; a fresh session resumes "
                                            "from recorded state", phase, free=True)
            if timeout and self.clock() - prompt_ts > timeout * 60:
                P.client.abort(sid, directory=directory)
                self._end_session(P, sid, "timeout")
                return self._session_failed(P, t, f"session ran over {timeout} minutes and was "
                                                  f"stopped", phase)
            if s.compaction:
                return self._compacted(P, t, sid, phase, s.compaction)
            if phase != "closeout":
                self._live_checkpoint(P, t, sid)
            self._note(P, t, f"{phase} session working")
            return None
        if s.status == "attention":
            q = s.question or {}
            if q.get("kind", "question") == "question" and self._auto_answer_question(
                    P, t, sid, phase, q):
                return None              # W1.1: never a person; the session decides
            if q.get("kind") == "permission" and self._auto_reject_permission(
                    P, t, sid, phase, q):
                return None              # W2/W1: a permission ask is a deny, never a person
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
        recovery = t["data"].get("permission_reply_recovery") or {}
        if phase == "implement" and recovery.get("session_id") == sid and not s.report:
            # Rejecting a tool permission can finish the assistant turn without
            # a final report or a new Kilo turn. Continue once in the same
            # session instead of verifying an unfinished BUILD or waiting for
            # the long execution timeout.
            key = f"permission-reply:{recovery.get('request_id') or 'unknown'}"
            lead_in = ("The permission was rejected: your session's rules are the policy "
                       "and nobody widens them mid-session. " if recovery.get("auto_rejected")
                       else "The permission reply was applied. ")
            if self._prompt_primary(P, t, lead_in + "Continue the "
                    "approved sprint using only in-scope paths. Preserve all required "
                    "evidence and checks. If you cannot proceed, return a precise "
                    "BLOCKED report with 2-4 options and the one you recommend; do not "
                    "request broader access for scratch files.", key):
                self.store.merge_data(pid, tid, permission_reply_recovery=None)
                self.store.event(pid, tid, "permission_reply_continued", session_id=sid,
                                 detail={"request_id": recovery.get("request_id")})
                return None
        if recovery.get("session_id") == sid:
            self.store.merge_data(pid, tid, permission_reply_recovery=None)
        if phase == "implement" and not s.report and not self._held(
                P, t, "an incomplete BUILD handoff"):
            pending_fn = getattr(P.adapter, "pending_build_steps", None)
            nudges = dict(t["data"].get("build_handoff_nudges") or {})
            if callable(pending_fn) and not nudges.get(sid):
                spec = P.adapter.describe(tid)
                pending = pending_fn(spec, P.ws.head(), P.ws)
                if pending:
                    message = P.adapter.continue_prompt(
                        spec, "your turn ended without a structured coordinator-report "
                        "while the committed BUILD checklist is still open. Continue the "
                        "remaining in-scope work, commit its evidence and run the final "
                        "checks. Keep temporary evidence inside approved workspace paths. "
                        "If a required step truly cannot be done, report the exact blocker")
                    if self._prompt_primary(P, t, message, "open-build-no-report-v1"):
                        nudges[sid] = 1
                        self.store.merge_data(pid, tid, build_handoff_nudges=nudges)
                        self.store.event(pid, tid, "open_build_continued", session_id=sid,
                                         detail={"open_steps": pending[:8]})
                        return None
        self._end_session(P, sid, "idle", tokens=s.tokens, report=s.report)
        return s

    QUESTION_AUTO_ANSWER = (
        "The coordinator does not answer questions during a session and no person is "
        "waiting. Decide within the plan and the sprint's rules. If a decision the plan does "
        "not settle truly blocks you, commit safe partial work and end your turn with a "
        "BLOCKED report that lists 2-4 concrete options and the one you recommend "
        "(`options`, `recommended`); the coordinator decides and continues the sprint.")

    def _auto_answer_question(self, P: Project, t: dict, sid: str, phase: str, q: dict) -> bool:
        """W1.1: a Kilo `question` from an implementer / closeout session (the tool is
        denied; this is the safety net when a role rule re-allowed it) is answered
        by the coordinator with the standing instruction, once per request, and the
        session continues. False when the reply could not be sent (the old
        AWAITING_HUMAN path then applies)."""
        if phase not in ("implement", "closeout"):
            return False
        rid = str(q.get("request_id") or "")
        key = f"question-auto:{P.id}:{t['task_id']}:{sid}:{rid or 'none'}"
        if self.store.op_begin(key, P.id, t["task_id"], "question_auto") is not None:
            return False                 # answered once already and still pending: a person
        n = max(1, len(q.get("options") or []) or 1)
        try:
            P.client.answer(sid, rid, [[self.QUESTION_AUTO_ANSWER]] * n,
                            directory=P.profile.workspace, kind="question")
        except Exception as e:                                    # noqa: BLE001
            self.store.op_update(key, "failed", {"error": str(e)[:300]})
            return False
        self.store.op_update(key, "done", {"request_id": rid})
        self.store.event(P.id, t["task_id"], "question_auto_answered", session_id=sid,
                         detail={"request_id": rid, "text": str(q.get("text") or "")[:400],
                                 "phase": phase})
        self._note(P, t, f"{phase} session asked a question; answered by the coordinator "
                         f"(decide within the plan or report BLOCKED with options)")
        return True

    PERMISSION_AUTO_REJECT = ("not allowed here; report BLOCKED with options if you need it")

    def _auto_reject_permission(self, P: Project, t: dict, sid: str, phase: str,
                                q: dict) -> bool:
        """W2 / W1 follow-up: a Kilo permission ask from an implementer / closeout
        session is answered `reject` by the coordinator (the session's rules are
        the policy; a person never widens them mid-session), once per request,
        journaled (permission_auto_rejected). The session then continues once
        in place (permission_reply_recovery) if the rejected turn ended without
        a report. False when the reply could not be sent (AWAITING_HUMAN then)."""
        if phase not in ("implement", "closeout"):
            return False
        rid = str(q.get("request_id") or "")
        key = f"permission-auto:{P.id}:{t['task_id']}:{sid}:{rid or 'none'}"
        if self.store.op_begin(key, P.id, t["task_id"], "permission_auto") is not None:
            return False
        try:
            P.client.answer(sid, rid, ["reject", self.PERMISSION_AUTO_REJECT],
                            directory=P.profile.workspace, kind="permission")
        except Exception as e:                                    # noqa: BLE001
            self.store.op_update(key, "failed", {"error": str(e)[:300]})
            return False
        self.store.op_update(key, "done", {"request_id": rid})
        self.store.merge_data(P.id, t["task_id"], permission_reply_recovery={
            "session_id": sid, "request_id": rid, "auto_rejected": True})
        self.store.event(P.id, t["task_id"], "permission_auto_rejected", session_id=sid,
                         detail={"request_id": rid, "text": str(q.get("text") or "")[:400],
                                 "phase": phase})
        self._note(P, t, f"{phase} session asked for a permission; rejected by the coordinator "
                         f"({str(q.get('text') or '')[:120]})")
        return True

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
        if phase != "closeout":
            from . import infra                    # revamp B12: infra failures are free
            free = infra.session_failure(self, P, t, why, free)
        self._release_session(P, t)
        if self.helpers.enabled(P):
            # this run is over: its lane work never reaches the next session
            self.helpers.measure_git(P, tid, final=True)
            self.helpers.abort_lane_running(P, tid, f"the session ended: {why}"[:300])
            self.helpers.expire_lane_results(P, tid, "the run ended before delivery")
        if phase == "closeout":
            return self._closeout_retry(P, t, why)
        self.store.transition(pid, tid, S.CHANGES_REQUIRED, {"findings": [why], "free": free},
                              data={**self.store.task(pid, tid)["data"], "findings": [why],
                                    "free_retry": bool(free)})

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
        if not self.fast_lane.on(P, tid):
            # 0.147.0: the project's checks run in the background; the tick goes on
            heavy = self._bg_checks(P, t, "verify", head,
                                    lambda: self._verify_heavy(P, t, spec, head))
            if heavy is None:
                return
        results = self._generic_checks(P, t, spec, head, phase="verify")
        if self.fast_lane.on(P, tid):
            # [fast_lane] (UI-T0): the studio_gate runner is this task's verify step
            # (a background run polled per tick), then the vision approval. Red
            # gates and vision CHANGES go back into the same session (fast_lane.py);
            # only a green, approved revision continues below to review.
            extra = self.fast_lane.verifying(P, t, spec, head, results)
            if extra is None:
                return
            results += extra
        else:
            results += heavy
        # only findings the checks themselves raised can be seen to have cleared; a
        # code review's findings are not in `results`, so they still need a change
        last_cr = next((h for h in reversed(self.store.history(pid, tid))
                        if h["to_state"] == S.CHANGES_REQUIRED), None)
        if not last_cr or last_cr["from_state"] != S.REVIEWING:
            _nothing_left_to_repair(results)
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
            return self._stop(P, t, S.BLOCKED, {
                "verification_id": vid,
                "blocker": "required checks could not be run: " + ", ".join(
                    f"{r.check} ({(r.detail or '').splitlines()[0] if r.detail else 'unknown'})"
                    for r in unknown)}, cls="checks_unknown", verification_id=vid,
                head_sha=head)
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

    GENERIC_CACHE_SECONDS = 600

    def _verify_heavy(self, P: Project, t: dict, spec, head: str) -> list:
        """The project's own verify checks (worker thread, 0.147.0): a track
        sprint's Studio-free checks in its worktree (the Studio gate runs at merge
        time in the main checkout, tracks/merge.py), else the adapter's verify and
        the implementer session audit. Reads only; results go back to the tick."""
        pid, tid = P.id, t["task_id"]
        base = t["data"].get("task_base") or t["base_sha"]
        if self.tracks.is_track(P):
            return list(self.tracks.verify_track(P, spec, head, base))
        out = list(P.adapter.verify(spec, "verify", head, base))
        for sess in self.store.sessions(pid, tid):
            if sess["run_id"] == t["run_id"] and sess["role"] == "implementer":
                try:
                    tr = P.client.transcript(sess["session_id"], directory=P.profile.workspace)
                    out += P.adapter.session_audit(spec, sess["session_id"], tr, head)
                except Exception as e:                            # noqa: BLE001
                    out.append(VerificationResult("coordinator.session_audit", UNKNOWN, False,
                                                  "", f"transcript unavailable: {e}", head))
        return out

    def _bg_checks(self, P: Project, t: dict, kind: str, head: str, fn) -> Optional[list]:
        """The results of `fn` (a check run) for this task, kind and head, or None
        while it runs in the background (a note says so). A run that raised is one
        required UNKNOWN row, like a crashed check."""
        from .automation import get as auto_get
        try:
            cap = int(auto_get(P, "max_parallel_checks") or 2)
            wait = float(auto_get(P, "inline_check_wait_s") or 0)
        except (KeyError, TypeError, ValueError):
            cap, wait = 2, 2.0
        st, val = self.bg_checks.run((P.id, t["task_id"], kind, head), fn, wait, cap)
        if st == "done":
            if isinstance(val, BaseException):
                return [VerificationResult("coordinator.checks", UNKNOWN, True, "",
                                           f"{kind} checks raised {type(val).__name__}: "
                                           f"{val}"[:500], head)]
            return list(val or [])
        if st == "queued":
            self._note(P, t, f"{kind} checks queued: {val} check run(s) already in the "
                             f"background ([automation] max_parallel_checks = {cap})")
        else:
            self._note(P, t, f"{kind} checks running in the background ({val} s) at "
                             f"{head[:12]}")
        return None

    def _generic_checks(self, P: Project, t: dict, spec, head: str, phase: str,
                        intermediate: bool = False) -> list:
        """revamp B13: _verifying runs these every tick while a gate runs (up to 45
        min; ~6 git calls plus a SHA of every pre-existing dirty file each time).
        The result is reused while HEAD, the porcelain status, the pre-existing
        dirty files' size/mtime and the task's inputs are unchanged (at most
        GENERIC_CACHE_SECONDS); any change recomputes."""
        from .automation import get as auto_get
        if not auto_get(P, "generic_checks_cache"):
            return self._generic_checks_uncached(P, t, spec, head, phase, intermediate)
        key = self._generic_key(P, t, spec, head, phase, intermediate)
        cache = self.__dict__.setdefault("_generic_cache", {})
        hit = cache.get((P.id, t["task_id"]))
        now = self.clock()
        if key and hit and hit[0] == key and now - hit[1] < self.GENERIC_CACHE_SECONDS:
            return list(hit[2])
        out = self._generic_checks_uncached(P, t, spec, head, phase, intermediate)
        if key:
            cache[(P.id, t["task_id"])] = (key, now, list(out))
        return out

    def _generic_key(self, P: Project, t: dict, spec, head: str, phase: str,
                     intermediate: bool) -> str:
        import hashlib
        import json as _json
        data = t.get("data") or {}
        try:
            status = P.ws.status()
        except Exception:                                         # noqa: BLE001
            return ""
        stats = []
        for p in sorted(data.get("baseline_dirty") or {}):
            try:
                st = os.stat(os.path.join(P.profile.workspace, p))
                stats.append((p, st.st_size, st.st_mtime_ns))
            except OSError:
                stats.append((p, None, None))
        body = _json.dumps([head, phase, bool(intermediate), t.get("base_sha"),
                            data.get("task_base"), sorted(map(list, status)), stats,
                            sorted(spec.scope), bool(data.get("findings")),
                            data.get("task_baseline_dirty"), data.get("scope_grants"),
                            self.fast_lane.on(P, spec.task_id)], default=str)
        return hashlib.sha256(body.encode()).hexdigest()

    def _generic_checks_uncached(self, P: Project, t: dict, spec, head: str, phase: str,
                                 intermediate: bool = False) -> list:
        """Scope / history / commits / clean / unrelated-dirty checks. With
        intermediate=True (ONLY the committed partial-BUILD continuation),
        task-owned unfinished files are carried into the next BUILD session.
        Final verification and closeout never pass this flag."""
        base = t["data"].get("task_base") or t["base_sha"]
        # 0.145.1: a track that merged the main branch is measured from that merge
        base = self.tracks.effective_base(P, base, head)
        # 0.145.1: docs/memory is the coordinator's (notes reach it through the
        # report); a session edit there is reported below, never a scope FAIL
        from .memory.layout import MEMORY_DIR
        exempt = list(P.adapter.exempt_paths(spec)) + [MEMORY_DIR]
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
        # [fast_lane]: the studio gate re-verifies every revision itself, and a repair whose
        # findings were coordinator-side (UI-D1 attempt 5, build.gate) has nothing to commit
        if phase == "verify" and t["data"].get("findings") and t["base_sha"] == head \
                and not spec.allow_no_change and not self.fast_lane.on(P, spec.task_id):
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
            # W1.4: `revert:` and merge commits are exempt; a commit the coordinator could
            # not reword itself (already pushed, a non-linear range) is advisory, never a
            # FAIL only a history rewrite could clear (UI-02b lost a 6.7 h lead session to
            # exactly that)
            from . import commits as CM
            bad, exempt_commits = CM.unprefixed(P, commits, prefix)
            fixable = CM.rewordable(P, t, [c[0] for c in bad]) if bad else ""
            add("commit_messages", FAIL if (bad and not fixable) else PASS,
                ("commits without the '" + prefix + "' prefix: "
                 + "; ".join(f"{c[0][:10]} {c[1][:60]}" for c in bad[:5])
                 + (f" (not reworded by the coordinator: {fixable}; advisory)" if fixable
                    else " (the coordinator rewords its own unpushed commits before "
                         "verification; this set predates that)")) if bad
                else f"all {len(commits)} commit message(s) start with {prefix!r}"
                + (f"; {len(exempt_commits)} revert/merge commit(s) exempt"
                   if exempt_commits else ""),
                required=not (bad and fixable))
        changed = P.ws.changed_files(base, head)
        runner_out, runner_caps = [], []
        if self.fast_lane.on(P, spec.task_id):
            from .fast_lane import runner_patterns
            runner_out, runner_caps = runner_patterns(P, spec.task_id)
        # (revamp B10: the task-source file a coordinator scope grant edited is not
        # the session's change)
        granted_src = [g.get("file") for g in (t.get("data") or {}).get("scope_grants") or []
                       if g.get("file")]
        # B1 (2026-10-21): scope blames the agent only for files it changed. Runner outputs
        # and files only the coordinator's own commits touched are never "outside scope".
        outside = [f for f in changed if not matches_any(f, spec.scope + exempt + runner_out
                                                         + runner_caps + granted_src)]
        mem = [f for f in changed if matches_any(f, [MEMORY_DIR])]
        if mem:
            from . import bookkeeping as BK
            own = set(BK.only_coordinator(P, t, base, head, mem))
            by_session = [f for f in mem if f not in own]
            if by_session:
                add("memory_direct_edit", FAIL,
                    "the session edited project memory directly (advisory; notes belong in "
                    "the report's memory_notes): " + ", ".join(by_session[:8]), required=False)
        if outside:
            from . import bookkeeping as BK
            mine = set(BK.only_coordinator(P, t, base, head, outside))
            outside = [f for f in outside if f not in mine]
        from . import scope as _scope           # revamp B10: granted files keep to their rule
        auto = []
        if outside and phase == "verify":
            # W2.5 (wired by W1): a changed file the [automation] scope_auto_approve
            # allow-list covers is granted here like a SCOPE_REQUEST would have been
            # (the session may edit it before asking since W2); its rule is still
            # checked below, never a scope FAIL that asks a person
            auto = [f for f in outside if _scope.rule_for(P, spec.task_id, f) is not None]
            for f in auto:
                _scope.auto_grant_changed(self, P, t, spec, f)
            if auto:
                outside = [f for f in outside if f not in auto]
                t = self.store.task(P.id, spec.task_id) or t
        add("scope", FAIL if outside else PASS,
            ("changed outside the task's files: " + ", ".join(outside[:12])
             + " — ask for a harness line with a SCOPE_REQUEST block; the coordinator "
               "grants only allow-listed paths itself") if outside
            else f"{len(changed)} changed file(s), all within scope"
            + (f"; granted by the allow-list: {', '.join(auto[:8])}" if auto else ""))
        out += _scope.grant_checks(P, t, spec, base, head)
        dirty = P.ws.status()
        baseline = t["data"].get("baseline_dirty") or {}
        if runner_out or runner_caps:
            # [fast_lane] B1: the studio_gate runner's own outputs (evidence log, captures)
            # are the coordinator's to commit after a green gate, never session work: they
            # can't fail clean or unrelated_preserved
            own = runner_out + runner_caps
            dirty = [(st, p) for st, p in dirty if not matches_any(p, own)]
            baseline = {p: h for p, h in baseline.items() if not matches_any(p, own)}
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
        touched = [p for p, h in baseline.items() if P.ws.file_hash(p) != h
                   and not matches_any(p, runner_caps)]
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
        # B4: a review started early (while vision ran) for this head is collected, never
        # paid for twice
        early = self.early_review_collect(P, t, head)
        if early == "running":
            return self._note(P, t, f"code review in flight at {head[:12]} (started with "
                                    f"the vision review)")
        prep = self._review_prepare(P, t, head)
        if prep is None:
            return
        if prep.get("apply"):
            return self._apply_review(P, t, prep["apply"])
        text, meta, err = self._review_call(P, prep)
        rev = self._review_finish(P, t, prep, text, meta, err)
        if rev is not None:
            self._apply_review(P, t, rev)

    def _review_prepare(self, P: Project, t: dict, head: str,
                        early: bool = False) -> Optional[dict]:
        """Everything before the paid review call: reuse, budgets, the reviewer's
        independence, the packet, the journal entry and the binding. None = handled
        here (or, early, not startable now); {"apply": review} = reuse that review;
        else the call context for _review_call / _review_finish. With early=True
        (B4, the fast lane starts the review while vision runs) nothing stops,
        blocks or moves the task: any such condition only means "not early"."""
        pid, tid = P.id, t["task_id"]
        spec, spec_from, spec_notes = self._reviewed_spec(P, tid, head)
        if not P.profile.review.get("required", True):
            if early:
                return None
            self.store.transition(pid, tid, S.APPROVED, {
                "review_id": "policy:review-not-required", "reviewed_commit_sha": head},
                reviewed_sha=head, review_id="policy:review-not-required")
            return None
        prior = self.store.reviews(pid, tid, head)
        # only a review made from the current packet format is this commit's review;
        # one from an older format (possibly truncated, no phase contract) is kept in
        # the audit trail and its findings carried forward, never reused as a verdict
        valid = [r for r in prior if r["valid"] and R.packet_version(r) == R.PACKET_VERSION]
        again = set(t["data"].get("lc_rereview") or []) if self._lc(P) else set()
        # [lifecycle] a review routed evidence_incomplete/access_blocked is kept
        # for the audit but not reused: the commit is reviewed again
        valid = [r for r in valid if r["review_id"] not in again]
        if valid:
            return None if early else {"apply": valid[-1]}
        older = [r for r in prior if r["valid"]]
        invalid = len([r for r in prior if not r["valid"]])
        n = len(prior)                                  # call index at this head (op key)
        if invalid >= INVALID_REVIEWS_BEFORE_BLOCK:
            if early:
                return None
            self.store.release(pid, tid)
            self._stop(P, t, S.BLOCKED, {
                "blocker": f"the reviewer returned {invalid} invalid results for {head[:12]}: "
                           f"{prior[-1]['error']}"}, cls="review_invalid")
            return None
        if self._held(P, t, "review"):
            return None                  # a paused project starts no new paid review
        all_reviews = self.store.reviews(pid, tid)
        cap = P.profile.limit("max_review_calls", 4)
        lc_calls = self._lc(P, "obligations")
        if not lc_calls and len(all_reviews) >= cap:
            if early:
                return None
            self.store.release(pid, tid)
            self._stop(P, t, S.BLOCKED, {
                "blocker": f"review budget used ({len(all_reviews)}/{cap} review calls)"},
                cls="review_budget")
            return None
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
                if not early:
                    self._block(P, t, f"no independent reviewer: {persona} also authored helper "
                                      f"proposals integrated into this revision; configure a "
                                      f"different reviewer persona")
                return None
            role, persona = alt, P.profile.persona(alt, tid)
        if P.foundry is None:
            if not early:
                self._block(P, t, "review needs Foundry ([foundry] url) and none is configured")
            return None
        key = f"review:{pid}:{tid}:{head}:{role}:{n}:p{R.PACKET_VERSION}"
        if (self.store.op(key) or {}).get("status") == "done":
            return None
        if lc_calls and not self._lc_charged(P, tid, "review_calls", key):
            # [lifecycle] the budget ledger's limit (base + authorized extensions);
            # a call already charged under this key (a retried error) is not refused
            self._lc_backfill(P, t)
            lim = self.obligations.effective_limits(P, t)["review_calls"]
            if lim["remaining"] <= 0:
                if not early:
                    self.store.release(pid, tid)
                    self._stop(P, t, S.BLOCKED, {
                        "blocker": f"review budget used ({lim['used']}/{lim['max']} review "
                                   f"calls, budget ledger)"}, cls="review_budget")
                return None
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
        if self.fast_lane.on(P, tid):
            # B4: the coordinator's own evidence (gate verdict, captures, vision, its
            # bookkeeping commits) so a reviewer never flags bookkeeping
            evidence = self.fast_lane.review_evidence(P, t, spec, head) + evidence
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
                   and (r["reviewed_sha"] != head or R.packet_version(r) != R.PACKET_VERSION
                        or r["review_id"] in again)]
        # Incremental re-review (2026-10-21, token cost): after a CHANGES_REQUIRED review at an
        # ancestor commit, send only the files changed since that review, plus its findings.
        # Files reviewed there and unchanged since are not sent again. B4: the same holds for
        # a re-review of the SAME commit ([lifecycle] evidence_incomplete): no diff at all,
        # only the evidence and the findings.
        dbase, dnotes = base, []
        if invalid:
            # W1.6: an invalid review is re-sent ONCE, with the validation error
            # appended, never identical (the same packet got the same bad answer)
            bad = [r for r in prior if not r["valid"]]
            dnotes.append(f"your previous review output for {head[:12]} was REJECTED "
                          f"({str(bad[-1].get('error') or 'invalid')[:300]}); this is the one "
                          f"re-send: answer with exactly the JSON object the schema describes "
                          f"(sprint_id, project_id, reviewed_commit_sha = {head}, verdict, "
                          f"findings, evidence_inspected, missing_evidence, required_repairs, "
                          f"residual_risks), nothing else")
        last = next((r for r in reversed(earlier) if r["verdict"] == "CHANGES_REQUIRED"
                     and R.packet_version(r) == R.PACKET_VERSION), None)
        if last and P.profile.review.get("incremental", True):
            try:
                ok = last["reviewed_sha"] == head or P.ws.is_ancestor(last["reviewed_sha"], head)
            except Exception:                                     # noqa: BLE001
                ok = False
            if ok:
                dbase = last["reviewed_sha"]
                kept = sorted({f["path"] for f in P.ws.diff_files(base, head)}
                              - {f["path"] for f in P.ws.diff_files(dbase, head)})
                if dbase == head:
                    dnotes = [f"incremental re-review of the SAME commit {head[:12]}: review "
                              f"{last['review_id']} inspected its code and asked for evidence. "
                              f"No code changed since, so no diff is sent: check the evidence "
                              f"below and whether its findings (listed below) are now resolved."]
                else:
                    dnotes = [f"incremental re-review: the diff below is {dbase[:12]}..{head[:12]} "
                              f"(since review {last['review_id']}); the task diff from {base[:12]} "
                              f"was reviewed there. Check that its findings (listed below) are "
                              f"resolved and that the changes since are correct."]
                dnotes.append(f"{len(kept)} file(s) reviewed at {dbase[:12]} and unchanged since "
                              f"(not re-sent): " + ", ".join(kept[:40])
                              + (" ..." if len(kept) > 40 else ""))
                evidence, unchanged = self._review_evidence_delta(P, evidence, dbase, head, last)
                if unchanged:
                    dnotes.append(f"{unchanged} evidence item(s) unchanged since review "
                                  f"{last['review_id']} are listed without their text")
        evidence, dropped = self._review_evidence_cap(P, evidence)
        if dropped:
            dnotes.append(f"{dropped} lower-priority evidence item(s) left out ([review] "
                          f"max_evidence_items)")
        mem_note = self._memory_review_note(P, spec)
        if mem_note:
            dnotes.append(mem_note)
        if self.tracks.is_track(P):
            # 0.145.3: a track has no Studio; its gate log and Studio checks belong to
            # the merge gate, so the reviewer is told and never asks for them
            owns = self.tracks.merge_gate_owns
            evidence = [e for e in evidence if not (
                isinstance(e, dict) and str(e.get("status") or "") in ("missing", "stale")
                and owns("file", e.get("path")))]
            dnotes.append(TRACK_REVIEW_NOTE)
        packet, info = R.build_packet(
            project_id=pid, task_id=tid, title=spec.title, spec=spec.spec, scope=spec.scope,
            base=dbase, head=head, commits=P.ws.commits(dbase, head), verification=ver,
            files=P.ws.diff_files(dbase, head), patch=lambda rel: P.ws.file_diff(dbase, head, rel),
            priority=priority, evidence=evidence, contract=contract,
            provenance={"spec_from": spec_from, "notes": list(spec_notes or []) + dnotes},
            budget=budget,
            earlier=earlier, authors=authors, question=P.profile.review.get("question") or "")
        if info["insufficient"]:
            if early:
                return None
            # never a partial-code review: the reviewer isn't called at all
            self.store.event(pid, tid, "review_insufficient", detail={
                "reviewed_commit_sha": head, "packet_version": R.PACKET_VERSION,
                **info["coverage"], "budget_source": budget["source"]})
            self._block(P, t, info["insufficient"])
            return None
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
                       "omitted": cov["omitted"][:50], "evidence": cov["evidence"][:30],
                       "incremental_from": dbase if dbase != base else None,
                       "early": bool(early)}
        rsid = f"review-{t['run_id']}-{n}"
        prep = {"key": key, "rsid": rsid, "role": role, "persona": persona, "packet": packet,
                "max_tokens": budget["max_tokens"], "meta_packet": meta_packet, "head": head,
                "scope": spec.scope}
        try:
            self._bind(P, rsid, tid, t["run_id"], role, persona)
        except (FoundryDenied, FoundryError, RuntimeError) as e:
            prep["bind_error"] = e        # handled like a failed call by _review_finish
        return prep

    @staticmethod
    def _review_call(P: Project, prep: dict) -> tuple:
        """The paid call itself (safe off the main thread: no store access).
        (text, meta, None) or (None, None, exception)."""
        if prep.get("bind_error") is not None:
            return None, None, prep["bind_error"]
        try:
            text, meta = P.foundry.chat(prep["persona"], [
                {"role": "system", "content": R.SYSTEM},
                {"role": "user", "content": prep["packet"]}],
                session_id=prep["rsid"], schema=R.SCHEMA, max_tokens=prep["max_tokens"])
            return text, meta, None
        except (FoundryDenied, FoundryError, RuntimeError) as e:
            return None, None, e

    def _review_finish(self, P: Project, t: dict, prep: dict, text, meta, err,
                       early: bool = False) -> Optional[dict]:
        """Journal and store the review; the stored review row, or None (an error,
        an invalid result, a denial: handled here). Early (B4): an error only
        drops the early review; the normal REVIEWING step tries again."""
        pid, tid = P.id, t["task_id"]
        key, rsid, head = prep["key"], prep["rsid"], prep["head"]
        if isinstance(err, FoundryDenied):
            self._end_review_binding(P, rsid, "denied")
            self.store.op_update(key, "denied", {"category": err.category, "code": err.code})
            self.store.x("DELETE FROM ops WHERE key=?", (key,))
            if early:
                return None
            self._on_denial(P, t, {"category": err.category, "retry_after": err.retry_after},
                            err.message, resume=S.REVIEWING, session=rsid)
            return None
        if err is not None:
            self.store.x("DELETE FROM ops WHERE key=?", (key,))
            if early:
                self._end_review_binding(P, rsid, "error")
                self.store.event(pid, tid, "early_review_failed", detail={
                    "head": head, "error": str(err)[:300]})
                return None
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
                self._stop(P, t, S.BLOCKED, {
                    "blocker": f"review call failed {n} times: {err}"}, cls="review_errors")
                return None
            self._note(P, t, f"review call failed ({n}/{max_err}): {err}")
            return None
        self._end_review_binding(P, rsid, "done")
        try:
            res = R.validate(text, project_id=pid, task_id=tid, head_sha=head, scope=prep["scope"])
            res["packet"] = prep["meta_packet"]
            rid = self.store.save_review(pid, tid, head, prep["role"], prep["persona"],
                                         res["verdict"], True, "", res, text)
        except R.ReviewInvalid as e:
            rid = self.store.save_review(pid, tid, head, prep["role"], prep["persona"], "", False,
                                         str(e), {"packet": prep["meta_packet"]}, text)
            self.store.op_update(key, "done", {"review_id": rid, "valid": False})
            self.store.event(pid, tid, "review_invalid", detail={"error": str(e),
                                                                 "role": prep["role"]})
            if not early:
                self._note(P, t, f"review result rejected: {e}")
            return None
        self.store.op_update(key, "done", {"review_id": rid, "valid": True})
        self.store.event(pid, tid, "review", detail={"verdict": res["verdict"],
                                                     "role": prep["role"],
                                                     "persona": prep["persona"],
                                                     "served_model": (meta or {}).get(
                                                         "served_model"),
                                                     "reviewed_commit_sha": head,
                                                     "early": bool(early)})
        return self.store.review(rid, pid, tid)

    # -- B4: the review starts while vision runs ----------------------------------------

    def early_review_start(self, P: Project, t: dict, head: str, vid: str) -> bool:
        """Start the code review of `head` in the background (the fast lane calls this
        once the gate is green, so the review runs alongside the vision batches). True
        when a review for this head is running or was started now."""
        pid, tid = P.id, t["task_id"]
        k = (pid, tid, head)
        pool = getattr(self, "_early_reviews", None)
        if pool is None:
            pool = self._early_reviews = {}
        if k in pool:
            return True
        tried = getattr(self, "_early_tried", None)
        if tried is None:
            tried = self._early_tried = set()
        if k in tried:
            return False             # tried once for this head: the normal path reviews it
        tried.add(k)
        if len(tried) > 500:
            tried.clear()
        cur = self.store.task(pid, tid) or t
        tt = {**cur, "verification_id": vid, "head_sha": head}
        if self._lc(P, "recovery") and self._lc(P, "obligations") and self._lc(P, "evidence"):
            # never ahead of the [lifecycle] submission gate: a review it would refuse is
            # not started early (the gate runs on REVIEWING as before)
            spec = P.adapter.describe(tid)
            try:
                refused = self.recovery.gate_submission(
                    P, tt, head, self.ledger, self.obligations, spec=spec,
                    evidence_items=self._review_evidence(P, spec, head))
            except Exception:                                     # noqa: BLE001
                refused = True
            if refused:
                return False
        prep = self._review_prepare(P, tt, head, early=True)
        if not prep or prep.get("apply"):
            return False
        from concurrent.futures import ThreadPoolExecutor
        ex = getattr(self, "_early_pool", None)
        if ex is None:
            ex = self._early_pool = ThreadPoolExecutor(max_workers=2,
                                                       thread_name_prefix="early-review")
        pool[k] = (ex.submit(self._review_call, P, prep), prep, tt)
        self.store.event(pid, tid, "early_review_started", detail={
            "head": head, "persona": prep["persona"], "key": prep["key"],
            "incremental_from": prep["meta_packet"].get("incremental_from")})
        return True

    def early_review_collect(self, P: Project, t: dict, head: str):
        """"running" while the early review for `head` is in flight; the stored review
        once it finished (saved here, on the engine thread); None when there is none."""
        pool = getattr(self, "_early_reviews", None) or {}
        k = (P.id, t["task_id"], head)
        if k not in pool:
            return None
        fut, prep, tt = pool[k]
        if not fut.done():
            return "running"
        pool.pop(k, None)
        text, meta, err = fut.result()
        return self._review_finish(P, {**(self.store.task(P.id, t["task_id"]) or tt),
                                       "verification_id": tt["verification_id"]},
                                   prep, text, meta, err, early=True)

    def _review_evidence_delta(self, P: Project, evidence: list, dbase: str, head: str,
                               last: dict) -> tuple[list, int]:
        """B4 (incremental re-review): an evidence item read from a file that did not
        change since the last review is listed without its text; it was sent there."""
        if dbase == head:
            return evidence, 0
        try:
            changed = {f["path"] for f in P.ws.diff_files(dbase, head)}
        except Exception:                                         # noqa: BLE001
            return evidence, 0
        out, n = [], 0
        for it in evidence:
            path = str((it or {}).get("path") or "")
            if path and path not in changed and it.get("text") and \
                    str(it.get("status") or "") == "ok" and int(it.get("priority") or 0) > 0:
                n += 1
                out.append({**it, "text": "", "note": (str(it.get("note") or "") + "; " if
                                                        it.get("note") else "")
                            + f"unchanged since review {last['review_id']} at {dbase[:12]} "
                              f"(sent there)"})
            else:
                out.append(it)
        return out, n

    def _review_evidence_cap(self, P: Project, evidence: list) -> tuple[list, int]:
        """B4: at most [review] max_evidence_items (default 24) evidence items, the
        essential (priority 0) ones always kept, then by priority."""
        cap = int(P.profile.review.get("max_evidence_items") or 24)
        if len(evidence) <= cap:
            return evidence, 0
        order = sorted(range(len(evidence)), key=lambda i: (_prio(evidence[i]), i))
        keep = set(order[:cap]) | {i for i, it in enumerate(evidence) if _prio(it) == 0}
        return [it for i, it in enumerate(evidence) if i in keep], len(evidence) - len(keep)

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
            if self.fast_lane.on(P, tid) and self.fast_lane.review_changes(P, t, rev, findings):
                return           # B4: the same warm session fixes it (one round, no cold start)
            return self.store.transition(pid, tid, S.CHANGES_REQUIRED, {
                "review_id": rev["review_id"], "findings": findings, "verdict": "CHANGES_REQUIRED"},
                review_id=rev["review_id"], data={**t["data"], "findings": findings})
        self.store.release(pid, tid)
        self._stop(P, t, S.BLOCKED, {
            "blocker": "reviewer: " + ("; ".join(R.findings_for_repair(res)) or "BLOCKED"),
            "review_id": rev["review_id"], "verdict": "BLOCKED"}, cls="review_blocked",
            review_id=rev["review_id"])

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
            # B7: the coordinator's own commits (templated closeout documents, runner
            # outputs) never invalidate the review they followed
            from . import bookkeeping as BK
            mine = set(BK.only_coordinator(P, t, rs, head, code))
            code = [f for f in code if f not in mine]
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
        templated = False
        if mode == "session" and not t["data"].get("closeout_session"):
            # B7 (2026-10-21): the coordinator writes the closeout documents from the task
            # record itself (no model tokens); a CLOSE session only when it can't, or when
            # the templated closeout fails its checks (closeout_attempts > 0)
            got = self._closeout_template(P, t, spec)
            if got == "wait":
                return
            templated = got == "done"
            t = self.store.task(pid, tid)
        if mode == "session" and not templated:
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
                self._memory_notes(P, t, s)
                self._reword_commits(P, t)          # W1.4: a CLOSE commit without the prefix
        head = P.ws.head()
        t = self.store.task(pid, tid)
        why = self._review_still_valid(P, t, spec, head)
        if why:
            self.store.revoke_approvals(pid, tid, why)
            return self.store.transition(pid, tid, S.VERIFYING, {"invalidated": why},
                                         data={**t["data"], "closeout_session": None,
                                               "closeout_done": None})
        rs = t["reviewed_sha"]
        if self.tracks.is_track(P):
            # a track sprint closes without Studio; the merge gate covers it
            heavy_fn = lambda: list(self.tracks.verify_track(P, spec, head, rs,  # noqa: E731
                                                             phase="closeout"))
        else:
            heavy_fn = lambda: list(P.adapter.verify(spec, "closeout", head, rs))  # noqa: E731
        heavy = self._bg_checks(P, t, "closeout", head, heavy_fn)   # 0.147.0: off the tick
        if heavy is None:
            return
        results = self._generic_checks(P, t, spec, head, phase="closeout") + heavy
        # a templated closeout has no session to push: the coordinator pushes (B7)
        published = self._publish(P, t, head, results, push=templated)
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

    def _closeout_template(self, P: Project, t: dict, spec) -> str:
        """B7: one deterministic closeout by the coordinator. "done" (documents
        committed at this head; the normal closeout checks follow), "wait" (locks /
        paused), "none" (no template, or it was tried: the CLOSE session runs). The
        adapter's closeout_template(spec, ctx) returns {"files": {path: text}} (or
        None when it can't); closeout_template_finish(spec, ctx) may return more
        files to commit after the first commit (e.g. done_check output for the
        evidence log), or {"error": why} to hand over to the session."""
        from . import bookkeeping as BK
        pid, tid = P.id, t["task_id"]
        fn = getattr(P.adapter, "closeout_template", None)
        if not callable(fn) or (P.profile.raw.get("closeout") or {}).get("template") is False:
            return "none"
        st = dict(t["data"].get("closeout_template") or {})
        if int(t["data"].get("closeout_attempts") or 0) > 0 or st.get("status") == "failed":
            return "none"
        head = P.ws.head()
        if st.get("status") == "done":
            return "done" if st.get("head") == head else "none"
        if self._held(P, t, "closeout"):
            return "wait"
        if self._acquire(P, t, self._session_locks(P), writer=True):
            return "wait"

        def failed(why: str) -> str:
            self.store.merge_data(pid, tid, closeout_template={"status": "failed",
                                                               "error": why[:500], "head": head})
            self.store.event(pid, tid, "closeout_template_failed", detail={"reason": why[:500]})
            self._release_session(P, t)
            return "none"
        ctx = self._closeout_facts(P, t, spec, head)
        try:
            out = fn(spec, ctx) or {}
        except Exception as e:                                    # noqa: BLE001
            return failed(f"template: {type(e).__name__}: {e}")
        files = dict(out.get("files") or {})
        if out.get("error") or not files:
            return failed(str(out.get("error") or "the template produced no documents"))
        sha, err = self._write_and_commit(P, t, files, "closeout (coordinator template)",
                                          "closeout")
        if err:
            return failed(err)
        fin = getattr(P.adapter, "closeout_template_finish", None)
        if callable(fin):
            try:
                more = fin(spec, {**ctx, "head": P.ws.head(), "commit": sha}) or {}
            except Exception as e:                                # noqa: BLE001
                return failed(f"finish: {type(e).__name__}: {e}")
            if more.get("error"):
                return failed(str(more["error"]))
            if more.get("files"):
                sha2, err = self._write_and_commit(P, t, dict(more["files"]),
                                                   "closeout evidence (coordinator template)",
                                                   "closeout")
                if err:
                    return failed(err)
                sha = sha2 or sha
        head = P.ws.head()
        self.store.merge_data(pid, tid, closeout_template={"status": "done", "head": head,
                                                           "commit": sha,
                                                           "files": sorted(files)[:20]})
        self.store.event(pid, tid, "closeout_templated", detail={
            "head": head, "files": sorted(files)[:20], "notes": list(out.get("notes") or [])[:10]})
        self._release_session(P, t)
        return "done"

    def _write_and_commit(self, P: Project, t: dict, files: dict, text: str,
                          kind: str) -> tuple[str, str]:
        """Write the template's files (workspace paths only) and commit exactly them as
        the coordinator. (sha or "", error or "")."""
        from . import bookkeeping as BK
        for rel, content in files.items():
            rel = str(rel).replace("\\", "/")
            if not P.profile.in_workspace(rel):
                return "", f"template path {rel!r} leaves the workspace"
            path = os.path.join(P.profile.workspace, *rel.split("/"))
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(str(content))
            except OSError as e:
                return "", f"cannot write {rel}: {e}"
        sha, err = BK.commit(self, P, t, list(files), text, kind)
        return sha, err

    def _closeout_facts(self, P: Project, t: dict, spec, head: str) -> dict:
        """What the coordinator knows about the finished task, for templated documents."""
        pid, tid = P.id, t["task_id"]
        base = t["data"].get("task_base") or t["base_sha"]
        try:
            changed = P.ws.changed_files(base, head)
            commits = P.ws.commits(base, head)
        except Exception:                                         # noqa: BLE001
            changed, commits = [], []
        rev = self.store.review(t.get("review_id") or "", pid, tid) if t.get("review_id") else None
        apr = self.store.approvals(pid, tid, "visual")
        fl = (t["data"].get("fast_lane") or {}) if isinstance(t["data"], dict) else {}
        gate = None
        if self.fast_lane.on(P, tid) and fl.get("run"):
            from .fast_lane import verdict_rel
            run = fl["run"]
            v = self.fast_lane._verdict(P, tid, run)
            if v:
                gate = {"head": run["head"], "verdict": verdict_rel(tid, run["head"]),
                        "summary": v.get("summary_line"), "result": v.get("result"),
                        "checks": len(v.get("checks") or []),
                        "captures": len(v.get("captures") or [])}
        return {"task_id": tid, "title": spec.title, "head": head, "base": base,
                "reviewed_sha": t.get("reviewed_sha") or "", "attempt": t.get("attempt"),
                "sessions": t.get("sessions"), "changed": changed[:200],
                "commits": [list(c) for c in commits[:60]],
                "review": {"review_id": rev["review_id"], "verdict": rev["verdict"],
                           "persona": rev.get("persona")} if rev else None,
                "visual": {"approver": apr[-1]["approver"], "note": apr[-1].get("note"),
                           "revision": apr[-1]["revision"]} if apr else None,
                "vision_notes": list(fl.get("vision_notes") or [])[:30], "gate": gate,
                "date": time.strftime("%Y-%m-%d", time.localtime(self.clock()))}

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

    def _publish(self, P: Project, t: dict, head: str, results: list,
                 push: bool = False) -> str:
        """Push (or check the agent's push) exactly once per commit. A push is
        a backup, not a verdict: it is the last step after every check. push=True
        (a coordinator-templated closeout, B7): verify_only pushes too, since no
        session is there to push."""
        pub = P.profile.publish
        mode = pub.get("mode", "none")
        if push and mode == "verify_only":
            mode = "push"
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
            self._question_withdrawn(P, t)
            return                       # answered through answer(), or resumed above
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
            if "visual" in (t["data"].get("awaiting_kinds") or []):
                from . import visual_approval
                if visual_approval.enabled(P):
                    rule = next((r for r in P.profile.approvals
                                 if r.kind == "visual" and r.stage == "after_close"), None)
                    if rule and not self._valid_approval(P, t, spec, rule, t["head_sha"]):
                        visual_approval.step(self, P, t, spec, rule, t["head_sha"])
                        t = self.store.task(pid, tid)
                        if t["state"] != S.AWAITING_HUMAN:
                            return
            if not self._approvals_needed(P, t, spec, "after_close", t["head_sha"]):
                if self._lc(P, "obligations"):
                    self.obligations.evaluate(P, t, t["head_sha"], phases=("APPROVAL",))
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
        P = self._task_project(project_id, task_id)
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
        P = self._task_project(project_id, task_id)
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
        P = self._task_project(project_id, task_id)
        t = self.store.task(project_id, task_id)
        if t["state"] in S.WRITING and t["session_id"]:
            P.client.abort(t["session_id"], directory=P.profile.workspace)
            self._end_session(P, t["session_id"], "cancelled")
        self.helpers.abort_running(P, task_id, "cancelled", f"task cancelled: {reason}"[:300])
        if self.fast_lane.on(P, task_id):
            self.fast_lane.abort(P, task_id, f"task cancelled: {reason}"[:300])
        case_id = (t.get("data") or {}).get("recovery_case")
        if case_id:
            # [lead_manager] the case goes with the task: its lead / repair sessions
            # are aborted, an upgrade no supervisor started is cancelled, its open
            # escalation is withdrawn
            L = self._lead_any()
            if L is not None:
                L.cancel(case_id, f"task cancelled by {actor}: {reason}"[:300], actor=actor, P=P)
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
        case_id = (t["data"] or {}).get("recovery_case")
        if case_id:
            # [lead_manager] the operator resolved the block: the case goes with it
            drop = drop + ("recovery_case", "lead_trigger")
        data = {k: v for k, v in t["data"].items() if k not in drop}
        stop = (t["data"].get("lc_stop") or {}) if lc_on else {}
        if reset_attempts:
            # a fresh budget: see RetryGuidance.on_reset (guided extra not carried
            # over; a never-called, transiently skipped adviser re-armed, bounded)
            data = self.retry_guidance.on_reset(data)
            # An operator-authorized fresh session must not feed the previous
            # session's own unfinished files back into repeat-failure detection.
            # The operator note and plan still carry the unfinished work, while
            # its prior findings and decisions remain in the audit trail.
            if data.get("build_progress"):
                data["free_retry"] = True
        if lc_on and str(resolution or "").strip() and \
                project_id in self.projects and self._lead_on(self.projects[project_id]):
            # the operator's resolution IS the materially different strategy for the
            # next start: it rides like a lead assignment (one session, crosses the
            # repeat guard once), so the guard does not re-ask the question the
            # operator just answered (UI-02, v0.126.6)
            data["lead_assignment"] = {
                "case_id": f"operator-unblock:{len(self.store.history(project_id, task_id))}",
                "assignment": str(resolution).strip()[:4000], "files": [], "tests": [],
                "extra_attempts": 0, "authorized": [], "via": "operator_unblock"}
        evidence = {"resolution": resolution, "reset_attempts": bool(reset_attempts)}
        S.check(t["state"], S.READY, evidence)        # a refused unblock leaves no note
        with self.store.tx():
            if reset_attempts and lc_on:
                # The old wait was explicitly resolved by this reset. Leaving
                # its open recovery question behind makes the next READY tick
                # revive that same question before a new worker can start.
                for d in self.obligations.active_decisions(project_id, task_id):
                    if d["status"] != "open" or d["kind"] not in (
                            "no_progress", "recovery_escalation"):
                        continue
                    self.store.x("UPDATE decisions SET status='withdrawn' WHERE decision_id=? "
                                 "AND status='open'", (d["decision_id"],))
                    self.store.event(project_id, task_id, "decision_withdrawn", detail={
                        "decision_id": d["decision_id"], "by": actor,
                        "reason": "operator-authorized fresh-session budget reset"})
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
            lead = self._lead_any() if case_id else None
            if lead is not None and hasattr(lead, "cancel"):
                lead.cancel(case_id, f"operator unblock by {actor}: {resolution}"[:300],
                                  actor=actor, P=self.projects.get(project_id))
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
        P = self._task_project(project_id, task_id)
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
        P = self._task_project(project_id, task_id)
        t = self.store.task(project_id, task_id)
        q = t["data"].get("question")
        if t["state"] != S.AWAITING_HUMAN or not q:
            raise ValueError(f"{project_id}/{task_id} has no open question")
        sid = t["session_id"]
        try:
            P.client.answer(sid, q["request_id"], answer, directory=P.profile.workspace,
                            kind=q.get("kind", "question"))
        except Exception:                                         # noqa: BLE001
            # Kilo may have withdrawn the request (it times some out, e.g. its
            # snapshot prompt): then there is nothing left to answer and the
            # session goes on. A request still pending re-raises the failure.
            if self._question_pending(P, t) is not False:
                raise
            return self._resume_from_question(P, t, {"answered": answer, "withdrawn": True},
                                              actor, recovery=False)
        return self._resume_from_question(P, t, {"answered": answer}, actor)

    def _resume_from_question(self, P: Project, t: dict, evidence: dict, actor: str,
                              recovery: bool = True) -> dict:
        q = t["data"].get("question") or {}
        sid = t["session_id"]
        data = {k: v for k, v in t["data"].items() if k not in ("question", "question_phase",
                                                                "question_from")}
        back = t["data"].get("question_from") or S.IMPLEMENTING
        # A reply resumes the existing prompt; it is not a new prompt. Resetting
        # prompt_ts makes Kilo's completed assistant turn look older than the
        # prompt, so an idle session is mistaken for one awaiting admission.
        if recovery and q.get("kind") == "permission":
            data["permission_reply_recovery"] = {"session_id": sid,
                                                 "request_id": q.get("request_id")}
        self.store.update_session(sid, status="prompted")
        self._q_missing_since.pop(q.get("request_id") or "", None)
        # a closeout session is polled from CLOSING, an implementation one from IMPLEMENTING
        return self.store.transition(P.id, t["task_id"],
                                     S.CLOSING if back == S.CLOSING else S.IMPLEMENTING,
                                     evidence, actor=actor, data=data)

    def _question_pending(self, P: Project, t: dict):
        """True while Kilo still holds the task's request, False once it has
        withdrawn it, None when Kilo can't be asked (unknown: keep waiting)."""
        q = t["data"].get("question") or {}
        sess = self.store.session(t["session_id"]) or {}
        try:
            s = P.client.state(t["session_id"], directory=P.profile.workspace,
                               since_ts=sess.get("prompt_ts") or 0)
        except Exception:                                         # noqa: BLE001
            return None
        return s.status == "attention" and (s.question or {}).get("request_id") == \
            q.get("request_id")

    def _question_withdrawn(self, P: Project, t: dict) -> None:
        """A session question Kilo no longer holds (timed out or answered in
        Kilo itself) would otherwise leave the task AWAITING_HUMAN for good
        while the session keeps working. After `question_withdrawn_grace_seconds`
        of absence (default 60; 0 = never) the task returns to the state it
        came from and the coordinator polls the session again."""
        grace = P.profile.limit("question_withdrawn_grace_seconds", 60)
        q = t["data"].get("question") or {}
        rid = q.get("request_id") or ""
        if not grace or not rid or not t.get("session_id"):
            return
        if self._question_pending(P, t) is not False:
            self._q_missing_since.pop(rid, None)
            return
        first = self._q_missing_since.setdefault(rid, self.clock())
        if self.clock() - first < float(grace):
            return
        self.store.event(P.id, t["task_id"], "question_withdrawn", session_id=t["session_id"],
                         detail={"request_id": rid, "kind": q.get("kind"),
                                 "text": (q.get("text") or "")[:300]})
        self._resume_from_question(P, t, {"answered": "(withdrawn by Kilo)", "question_withdrawn": rid,
                                          "note": "Kilo no longer holds the session's request; "
                                                  "the session continues"}, "engine",
                                   recovery=False)

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
                             session_id: str, memory_for: str = "") -> tuple[str, list[str]]:
        """Append queued control-center instructions to a prompt the engine is
        about to send anyway (a safe point: never a second, concurrent prompt).
        They are the operator's words for this task, inside its existing scope
        and permissions: they don't widen scope or waive a check.
        revamp B13: the [lifecycle] memory blocks go to a session once; later
        prompts into the same session carry only the paragraphs that changed
        ([automation] memory_delta)."""
        P = self.projects.get(project_id)
        if P is not None and self._lc(P):
            prompt += self._memory_delta(P, task_id, memory_for or session_id,
                                         self._lc_memory_blocks(P, task_id))
        if P is not None:
            # Team Lead piece 4: the project memory slice (docs/memory), hashed per
            # entry like the lifecycle blocks: a later prompt carries only changes
            block = self._memory_block(P, task_id, memory_for or session_id)
            if block:
                delta = self._memory_delta(P, task_id, memory_for or session_id, block)
                if delta.strip():
                    prompt += "\n\n" + delta.strip("\n") + "\n"
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

    # ======================================================================= project memory

    def _memory_block(self, P: Project, tid: str, sid: str) -> str:
        """The docs/memory slice for this task's prompt (memory/manager.py), plus
        the playbook slice filling what's left of the shared budget (A1); never
        raises: a memory failure costs the prompt its slice, nothing else."""
        try:
            # a sprint queued on a track gets the track's slice (tracks/manager.py)
            track = self.store.task_track(P.id, tid)
            block = self.memory.prompt_block(P, tid, sid, track=track)
        except Exception as e:                                    # noqa: BLE001
            self.log(f"{P.id}/{tid}: memory slice failed: {type(e).__name__}: {e}")
            block, track = "", None
        try:
            pb = self.playbook.prompt_block(P, tid, sid, track=track, used_text=block)
        except Exception as e:                                    # noqa: BLE001
            self.log(f"{P.id}/{tid}: playbook slice failed: {type(e).__name__}: {e}")
            pb = ""
        if block and pb:
            return block.rstrip("\n") + "\n\n" + pb
        return block or pb

    def _memory_notes(self, P: Project, t: dict, s) -> None:
        """MEMORY_NOTE / PLAYBOOK_NOTE lines of an ended session's final message
        -> validated, appended, committed and indexed by the coordinator."""
        try:
            self.memory.apply_notes(P, t, s)
        except Exception as e:                                    # noqa: BLE001
            self.store.event(P.id, t["task_id"], "warning", session_id=t.get("session_id") or "",
                             detail={"memory_notes": f"{type(e).__name__}: {e}"[:400]})
        try:
            self.playbook.apply_notes(P, t, s)
        except Exception as e:                                    # noqa: BLE001
            self.store.event(P.id, t["task_id"], "warning", session_id=t.get("session_id") or "",
                             detail={"playbook_notes": f"{type(e).__name__}: {e}"[:400]})

    def _memory_review_note(self, P: Project, spec) -> str:
        """One provenance note for the review packet: the conventions, known
        issues and decisions that match the sprint's files (compact)."""
        try:
            if not self.memory.enabled(P):
                return ""
            from .memory import slices as MS
            sl = self.memory.slice(P, spec, role="reviewer")
            text = MS.compact(sl["entries"], 1500)
            return ("project memory (docs/memory entries matching this sprint; facts the code "
                    "must respect): " + text) if text else ""
        except Exception as e:                                    # noqa: BLE001
            self.log(f"{P.id}: memory review note failed: {type(e).__name__}: {e}")
            return ""

    def _notes_requeue(self, ids: list[str]) -> None:
        """The prompt carrying these notes was refused (nothing sent): they wait
        for the next prompt instead of staying 'delivering' forever."""
        self.__dict__.pop("_memory_pending", None)    # nothing reached the session
        if ids:
            self.store.mark_operator_messages(ids, "queued", "", "the prompt carrying it was "
                                                                 "refused; waiting for the next")

    MEMORY_SENT_CAP = 400

    def _memory_delta(self, P: Project, tid: str, sid: str, text: str) -> str:
        """`text` minus the paragraphs this session already received. The sent
        set is committed only when Kilo accepted the prompt (_notes_delivered)."""
        from .automation import get as auto_get
        if not text.strip() or not sid or not auto_get(P, "memory_delta"):
            return text
        import hashlib
        import json as _json
        key = f"memsent:{P.id}:{tid}:{sid}"
        try:
            sent = set(_json.loads(self.store.meta(key) or "[]"))
        except ValueError:
            sent = set()
        # W1.6: hashed per ENTRY (a "- " line, or any non-header line), not per block:
        # one new memory entry no longer re-sends the whole memory and decisions blocks
        blocks = []
        for para in text.strip("\n").split("\n\n"):
            if not para.strip():
                continue
            header, entries = [], []
            for line in para.splitlines():
                if not line.strip():
                    continue
                if not entries and (line.lstrip().startswith("#") or line.rstrip().endswith(":")):
                    header.append(line)
                else:
                    entries.append(line)
            if not entries:
                entries, header = header, []
            blocks.append((header, entries))
        entries_all = [(hashlib.sha1(e.strip().encode()).hexdigest()[:16], e)
                       for _, es in blocks for e in es]
        hashes = [h for h, _ in entries_all]
        new_hashes = {h for h in hashes if h not in sent}
        pend = self.__dict__.setdefault("_memory_pending", {})
        prior = set(pend[sid][1]) if sid in pend else set()     # two blocks, one prompt
        pend[sid] = (key, sorted(sent | prior | set(hashes))[-self.MEMORY_SENT_CAP:])
        if not new_hashes:
            return ""
        if len(new_hashes) == len(set(hashes)):
            return text                                   # first time: unchanged text
        out = []
        for header, es in blocks:
            keep = [e for e in es if hashlib.sha1(e.strip().encode()).hexdigest()[:16]
                    in new_hashes]
            if keep:
                out.append("\n".join(header + keep))
        self.store.event(P.id, tid, "memory_delta", session_id=sid, detail={
            "sent": len(new_hashes), "unchanged": len(set(hashes)) - len(new_hashes)})
        return ("\n\n## Coordinator memory (entries changed since your last prompt; the rest "
                "is unchanged)\n" + "\n\n".join(out) + "\n")

    def _notes_delivered(self, ids: list[str], session_id: str) -> None:
        got = self.__dict__.get("_memory_pending", {}).pop(session_id, None)
        if got:
            import json as _json
            self.store.set_meta(got[0], _json.dumps(got[1]))
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
    LC_STOP_KINDS = ("recovery_escalation", "no_progress", "review_access",
                     "implementer_blocked")
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
            self._obligations = Obligations(self.store, clock=self.clock,
                                            unblock_validator=self._unblock_ref_ok,
                                            lead_validator=self._lead_authority_ok,
                                            decision_policy=self._decision_meta)
        return self._obligations

    def _decision_meta(self, project: str, task: str, kind: str, evidence: dict,
                       options: list, recommendation: str = "") -> dict:
        """revamp B11: class, recommended option and timeout of a new decision."""
        from . import decision_policy
        P = self.projects.get(project)
        return decision_policy.meta(P, kind, evidence, options, recommendation) \
            if P is not None else {}

    def _unblock_ref_ok(self, project: str, task: str, ref: str, actor: str) -> bool:
        """Obligations' check of an `unblock:<ref>` budget authorization. The
        ordinary `unblock:<n>` (an applied operator reset unblock) is checked
        exactly as without this hook; `unblock:lead:<case>` is the
        [lead_manager] delegated policy (the operator's profile setting
        extra_attempts), valid only for an open case of this very task whose
        action is a lead game assignment (or Victor's answer to its question),
        and only for the engine's policy actor."""
        if ref.startswith(self.LEAD_REF):
            P = self.projects.get(project)
            if actor != self.LEAD_POLICY_ACTOR or P is None or not self._lead_on(P):
                return False
            case = self.lead.case(ref[len(self.LEAD_REF):])
            from .lead import OPEN_STATES
            return bool(case and case["project_id"] == project and case["task_id"] == task
                        and case["state"] in OPEN_STATES and (case.get("proposed_action") or {})
                        .get("kind") in ("assign_game_repair", "victor_answer", "operate"))
        return self._obligations._unblock_applied(project, task, ref, actor)

    def _lead_authority_ok(self, project: str, task: str, actor: str) -> bool:
        """`lead_manager:<case>` answers decisions only for an open case of this
        very task whose project has [lead_manager] full_authority on."""
        from .obligations import LEAD_ACTOR_PREFIX
        P = self.projects.get(project)
        if P is None or not self._lead_on(P) or                 not self.lead.settings(P).get("full_authority"):
            return False
        case = self.lead.case(str(actor or "")[len(LEAD_ACTOR_PREFIX):])
        from .lead import OPEN_STATES
        return bool(case and case["project_id"] == project and case["task_id"] == task
                    and case["state"] in OPEN_STATES)

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
            self._stop(P, t, S.FAILED, {
                "reason": f"{L['sessions']['used']} sessions used (max_sessions_per_task "
                          f"{L['sessions']['max']}, budget ledger)"}, cls="sessions_exhausted")
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
        self._stop(P, t, S.FAILED, {
            "reason": f"{L['ordinary']['used']} implementation attempts used (max_attempts "
                      f"{L['ordinary']['max']}, guided extra {L['guided_extra']['used']}/"
                      f"{L['guided_extra']['max']}, budget ledger)"}, cls="attempts_exhausted")
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
        ev = {"blocker": text, "blocker_class": r["cls"], "readiness": r["items"],
              "attempt_consumed": "no"}
        if r["auto"]:
            # environment / runtime_sync re-check themselves on a timer: not a final stop
            self.store.transition(pid, tid, S.BLOCKED, ev, blocker=text, data=data)
        else:
            self._stop(P, cur, S.BLOCKED, ev, cls="readiness_" + str(r["cls"]), blocker=text,
                       data=data)

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
        # A previous engine version may have opened a no-progress/repeated-repair
        # question before recognizing committed partial BUILD work. Re-run the
        # deterministic continuation checks before handing that question to a
        # person. This grants no new authority: the same gate, scope, history,
        # and cleanliness checks used by decision_reevaluate still have to pass.
        # Bound repeat scans while still noticing cleanup or other uncommitted
        # work at the same HEAD. The 60-second retry catches content changes
        # whose porcelain status line stays the same.
        candidates = [d for d in pending
                      if d["kind"] in ("no_progress", "recovery_escalation")]
        if candidates:
            checked = dict(data.get("auto_reevaluate_checked") or {})
            head = P.ws.head()
            marker = [head, [list(x) for x in P.ws.status()]]
            changed = False
            for d in candidates:
                did = d["decision_id"]
                prev = checked.get(did) or {}
                if (isinstance(prev, dict) and prev.get("marker") == marker and
                        self.clock() - float(prev.get("ts") or 0) < 60):
                    continue
                checked[did] = {"marker": marker, "ts": self.clock()}
                changed = True
                try:
                    result = self._decision_reevaluate(
                        did, "coordinator:auto", "committed partial BUILD re-check")
                except Exception as e:  # keep the question open on uncertainty
                    self.store.event(pid, tid, "warning", detail={
                        "auto_reevaluate": did,
                        "error": f"{type(e).__name__}: {e}"[:400]})
                else:
                    if result.get("withdrawn"):
                        decs = [self._lc_final_decision(i) for i in ids]
                        pending = [x for x in decs if x and x["status"] == "open"]
            if changed:
                current = self.store.task(pid, tid)
                self.store.update_task(pid, tid, data={**current["data"],
                    "auto_reevaluate_checked": checked})
        if pending:
            if final_ids != ids:
                self._lc_rewait(P, t, final_ids)
            # W1.1: the lead takes an implementer's BLOCKED question only when it is a
            # tooling question; a product choice waits for its default or the operator
            stalls = [d for d in pending if d["kind"] in self.LEAD_WAIT_KINDS and not (
                d["kind"] == "implementer_blocked"
                and (d.get("evidence") or {}).get("blocker_kind") != "tooling")]
            if stalls and self._lead_on(P):
                # [lead_manager] the deterministic re-check above did not clear the
                # no-progress / repeated-repair question: before it reaches Victor,
                # the lead tier diagnoses it (one case per fingerprint)
                self._lead_from_wait(P, self.store.task(pid, tid), stalls)
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
        full = self.obligations.decision(did) if did else None
        by = "timeout default" if (full or {}).get("authority") == "timeout_default" \
            else "operator decision"
        text = f"parked: recovery stopped by {by} {did}: {reason or 'stop'}"[:900]
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
        else:
            self.store.transition(pid, tid, S.BLOCKED, {"blocker": text, "decision_id": did,
                                                        "blocker_class": "decision"},
                                  blocker=text, data=data)
        self._park_summary(P, self.store.task(pid, tid), text, decision_id=did,
                           kind=str(d.get("kind") or ""))

    def _park_summary(self, P: Project, t: dict, why: str, decision_id: str = "",
                      kind: str = "", case_id: str = "") -> None:
        """W1.2 (Victor 2026-10-22): a sprint parked on a tooling limit leaves one
        summary event (sprint_parked) and one operator notice; the queue moves on
        by itself (a dependent sprint waits for it, an independent one starts)."""
        pid, tid = P.id, t["task_id"]
        data = t.get("data") or {}
        try:
            lead_calls = self.lead.lifetime_calls(pid, tid) if self._lead_on(P) else 0
        except Exception:                                         # noqa: BLE001
            lead_calls = 0
        try:
            counters = self.obligations.counters(pid, tid) if self._lc(P, "obligations") else {}
        except Exception:                                         # noqa: BLE001
            counters = {}
        waiting = [q["task_id"] for q in self.store.tasks(pid)
                   if q["state"] == S.QUEUED and tid in (
                       (self.store.queue_entry(pid, q["task_id"]) or {}).get("deps") or [])]
        summary = {
            "task_id": tid, "why": str(why)[:800], "decision_id": decision_id, "kind": kind,
            "case_id": case_id or data.get("recovery_case") or "",
            "head_sha": t.get("head_sha") or "", "attempt": t.get("attempt"),
            "sessions": t.get("sessions"), "lead_calls": lead_calls, "counters": counters,
            "findings": [str(f)[:300] for f in (data.get("findings") or [])[:8]],
            "last_report": {k: str(v)[:300] for k, v in (data.get("last_report") or {}).items()
                            if k in ("status", "summary", "blocker", "next")},
            "dependents_waiting": waiting[:20],
            "resume": "operator: unblock / answer a superseding decision; the queue continues "
                      "with independent sprints meanwhile"}
        self.store.event(pid, tid, "sprint_parked", detail=summary)
        self.store.event(pid, tid, "operator_notice", detail={
            "text": (f"{tid}: parked BLOCKED ({str(why)[:200]}); the queue continues"
                     + (f"; waiting on it: {', '.join(waiting[:6])}" if waiting else "")),
            "kind": "sprint_parked", "decision_id": decision_id, "task_id": tid})

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
            elif d["kind"] == "implementer_blocked":
                # W1.1: the answer to the session's BLOCKED options becomes the first
                # finding of the next session (the repair prompt carries it)
                cur = self.store.task(P.id, tid)
                applied = set((cur["data"] or {}).get("blocked_decisions_applied") or [])
                if d["decision_id"] in applied:
                    continue
                answer = d.get("answer")
                if isinstance(answer, dict):
                    import json as _json
                    answer = answer.get("option") or answer.get("strategy") or \
                        answer.get("answer") or _json.dumps(answer)
                who = "the timeout default" if d.get("authority") == "timeout_default" \
                    else str(d.get("actor") or "the operator")
                line = (f"Decision {d['decision_id']} on your BLOCKED report "
                        f"({str((d.get('evidence') or {}).get('blocker') or '')[:300]}): "
                        f"{str(answer)[:500]} (decided by {who}). Continue the sprint "
                        f"accordingly, within the plan's file list.")
                prior = [f for f in ((cur["data"] or {}).get("findings") or [])
                         if not str(f).startswith("Decision dec-")]
                self.store.merge_data(P.id, tid, findings=[line] + prior[:20],
                                      blocked_decisions_applied=sorted(
                                          applied | {d["decision_id"]}))
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
            lead_a = self._lead_assignment_text(P, t)
            if lead_a:
                # [lead_manager] the lead's diagnosed assignment IS the materially
                # different strategy for this one start (bounded by the case); the
                # no-progress / repeat question is not re-asked for it
                strategy = lead_a
                self.store.event(pid, tid, "lead_strategy", detail={
                    "case_id": (t["data"].get("lead_assignment") or {}).get("case_id"),
                    "stalls": pr.get("stalls")})
            elif pr["no_progress"]:
                from . import decision_policy as DP
                continued = (t["data"].get("no_progress_continued") or {})
                q = (f"{pr['stalls']} sessions in a row made no progress (no newly verified "
                     f"obligation, same findings, {pr['summary_only_sessions']} summary-only); "
                     f"another broad retry would repeat them")
                if continued:
                    q += " (the one continuation was used)"
                # W1.2 (Victor 2026-10-22): the default is to continue once, then park the
                # sprint BLOCKED with a summary so the queue moves on; the timeout applies
                did = self.obligations.open_decision(
                    pid, tid, "no_progress", q, evidence={
                        **pr, "recommended": DP.PARK if continued else DP.CONTINUE_ONCE},
                    options=[DP.CONTINUE_ONCE, DP.STRATEGY, DP.MORE_BUDGET, DP.PARK],
                    recommendation=DP.PARK if continued else DP.CONTINUE_ONCE,
                    blocks=[tid],
                    # the second question replaces the answered "continue once"
                    supersedes=continued.get("decision_id") or None)
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
                    if DP.is_continue_once(d.get("answer")):
                        # the one more session (W1.2); the next no-progress stop parks
                        if not continued:
                            self.store.merge_data(pid, tid, no_progress_continued={
                                "decision_id": did, "stalls": pr["stalls"],
                                "ts": self.clock(), "by": d.get("authority") or ""})
                            self.store.event(pid, tid, "no_progress_continue_once", detail={
                                "decision_id": did, "stalls": pr["stalls"],
                                "by": d.get("authority") or ""})
                        act, val = "continue", ""
                    else:
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
                    trusted_strategy=bool(lead_a),
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
                                             evidence_items=self._review_evidence(P, spec, head),
                                             deferred=(self.tracks.merge_gate_owns
                                                       if self.tracks.is_track(P) else None))
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
            # revamp B13: only what this session has not seen yet
            block = self._memory_delta(P, tid, sid, block) if block else block
            if block.strip():
                text += "\n" + block.strip("\n") + "\n"
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
        if d["kind"] == "recovery_escalation":
            # an escalation opened (before v0.123.1) without naming the older
            # repeat set's answered escalation it replaces gets that link now,
            # so the answer supersedes it instead of tripping the guard
            from .recovery import link_prior_escalation
            link_prior_escalation(self.obligations, P.id, task_id, decision_id)
        out = self.obligations.answer(decision_id, value, actor, "operator", **kw)
        self.store.event(project_id, task_id, "decision_answered", detail={
            "decision_id": decision_id, "kind": d["kind"], "actor": actor,
            "note": str(note or "")[:300]})
        return out

    def decision_reevaluate(self, decision_id: str, actor: str, reason: str = "",
                            dry_run: bool = False) -> dict:
        """Operator-facing reevaluation; a model cannot claim this authority."""
        from .obligations import DecisionRefused, is_model_actor
        if is_model_actor(actor) or not str(actor or "").strip():
            raise DecisionRefused(f"actor {actor!r} may not re-evaluate decisions")
        return self._decision_reevaluate(decision_id, actor, reason, dry_run)

    def _decision_reevaluate(self, decision_id: str, actor: str, reason: str = "",
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
        from .obligations import DecisionRefused
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

    # ======================================================================= lead manager (v0.123)
    #
    # Only for a project whose profile has [lead_manager] enabled = true; for
    # every other project each hook below is a no-op and the original
    # transition is applied unchanged.
    #
    #   final stop   every final BLOCKED / FAILED transition goes through _stop:
    #                deterministic recovery first (evidence recover, the lead's
    #                route/quota readiness refresh; decision waits re-run the
    #                partial-BUILD reevaluation in _lc_blocked before a case is
    #                opened), then lead.open_case (idempotent per fingerprint) and
    #                BLOCKED with data.recovery_case (no new task states)
    #   BLOCKED      a task with an active case: one bounded lead.step per tick;
    #                operator pause / takeover, a project pause and an answer to
    #                the original decision are honoured
    #   resume       lead.resume_action: a game assignment (the primary repairs
    #                with the lead's text as untrusted advice, plus extra attempts
    #                under the delegated [lead_manager] extra_attempts policy, one
    #                ledger row per counter referencing the case), reverify, wait,
    #                a deployed coordinator upgrade, Victor's answer; each resume
    #                transition carries {recovery_case, action} and resolves the case

    LEAD_REF = "unblock:lead:"
    LEAD_POLICY_ACTOR = "delegated_policy:lead_manager"
    LEAD_WAIT_KINDS = ("no_progress", "recovery_escalation", "implementer_blocked")
    LEAD_ASSIGNMENT_HEADER = ("Lead manager assignment (recovery case {case}; advisory and "
                              "untrusted: it stays within this task's approved scope, and every "
                              "gate, check and review still applies): ")
    LEAD_ANSWER_HEADER = ("Operator answer to lead recovery case {case} (within this task's "
                          "approved scope; every gate, check and review still applies): ")

    @property
    def lead(self):
        """The LeadManager, or None while no project enables [lead_manager]."""
        if self._lead is None and not self._lead_assigned and \
                any(self._lead_on(P) for P in self.projects.values()):
            from .lead import LeadManager
            self._lead = LeadManager(self, clock=self.clock)
        return self._lead

    @lead.setter
    def lead(self, value) -> None:
        self._lead = value
        self._lead_assigned = True

    def _lead_any(self):
        """The LeadManager even when no project enables it any more (to cancel
        a case left from when one did); None when no case table exists."""
        L = self.lead
        if L is not None:
            return L
        if self._lead_assigned:
            return None
        try:
            if not self.store.q("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                                "name='recovery_cases'"):
                return None
        except Exception:                                         # noqa: BLE001
            return None
        from .lead import LeadManager
        return LeadManager(self, clock=self.clock)

    def _lead_disabled(self, P: Project, t: dict) -> None:
        """[lead_manager] is off but the task still carries a live case: cancel
        it (abort its sessions, cancel an unstarted upgrade, withdraw its
        escalation) and drop the marker; a BLOCKED task keeps its original
        blocker for the operator."""
        from .lead import OPEN_STATES
        cid = (t.get("data") or {}).get("recovery_case")
        L = self._lead_any()
        if L is not None:
            case = L.case(cid)
            if case and case["state"] in OPEN_STATES:
                L.cancel(cid, "[lead_manager] was disabled while the case was live",
                         actor="engine", P=P)
        if t["state"] == S.BLOCKED:
            self._lead_detach(P, t, f"lead recovery case {cid} cancelled: [lead_manager] is "
                                    f"disabled")
        else:
            data = {k: v for k, v in (t.get("data") or {}).items()
                    if k not in ("recovery_case", "lead_trigger")}
            self.store.update_task(P.id, t["task_id"], data=data)

    @staticmethod
    def _lead_on(P: Project) -> bool:
        fn = getattr(P.profile, "lead_manager_on", None)
        try:
            return bool(fn()) if callable(fn) else False
        except Exception:                                         # noqa: BLE001
            return False

    @staticmethod
    def _lead_allowance(t: dict) -> int:
        """Extra attempts/sessions the lead's delegated policy granted this task
        (projects without the budget ledger; the ledger counts its own rows)."""
        grants = ((t or {}).get("data") or {}).get("lead_grants") or {}
        try:
            return sum(max(0, int(v)) for v in grants.values())
        except (TypeError, ValueError, AttributeError):
            return 0

    def _lead_assignment_text(self, P: Project, t: dict) -> str:
        if not self._lead_on(P):
            return ""
        a = (t.get("data") or {}).get("lead_assignment") or {}
        assignment = str(a.get("assignment") or "")
        policy_fn = getattr(getattr(P, "adapter", None), "recovery_policy", None)
        if assignment and callable(policy_fn):
            assignment += "\nCurrent project recovery policy overrides conflicting historical " \
                          "handoff text: " + policy_fn(t["task_id"])
        return assignment

    def _stop(self, P: Project, t: dict, to: str, evidence: dict, cls: str = "",
              **fields) -> Optional[dict]:
        """A final BLOCKED / FAILED transition. With [lead_manager] on, a
        recovery case is opened first and the task waits BLOCKED on it; when
        the lead is off (or the case cannot be opened) the original transition
        is applied unchanged."""
        if self._lead_on(P):
            # revamp B9: tooling / infrastructure stops are retried deterministically
            # (bounded, with backoff) before any paid lead case
            from . import autoheal
            healed = autoheal.try_heal(self, P, t, to, evidence, cls, dict(fields))
            if healed is not None:
                return healed
            try:
                out = self._lead_open(P, t, to, evidence, cls, dict(fields))
            except Exception as e:                                # noqa: BLE001
                self.store.event(P.id, t["task_id"], "warning", detail={
                    "lead_manager": f"open case failed: {type(e).__name__}: {e}"[:400]})
                self.log(f"{P.id}/{t['task_id']}: lead open_case failed: {e}")
                out = None
            if out is not None:
                return out
        return self.store.transition(P.id, t["task_id"], to, evidence, **fields)

    def _lead_recheck(self, P: Project, t: dict) -> dict:
        """Deterministic recovery before a case: recover evidence (lifecycle) and
        refresh the lead's route/quota readiness. Never a paid model call."""
        out: dict = {}
        if self._lc(P, "evidence"):
            rep = self._lc_recover(P, t) or {}
            out["evidence_recovered"] = int(rep.get("added") or 0)
        try:
            bad = [r["name"] for r in self.lead.readiness(P, fresh=True) if r.get("blocking")]
            out["lead_readiness"] = "ok" if not bad else "not ready: " + ", ".join(bad)
        except Exception as e:                                    # noqa: BLE001
            out["lead_readiness"] = f"unavailable: {type(e).__name__}"
        return out

    def _lead_failed_checks(self, P: Project, tid: str, vid: str) -> list:
        if not vid:
            return []
        try:
            return [{"check_id": r["check_name"], "result": r["result"],
                     "detail": str(r.get("detail") or "")[:300]}
                    for r in self.store.verification(vid, P.id, tid) if r.get("result") != PASS]
        except Exception:                                         # noqa: BLE001
            return []

    def _lead_open(self, P: Project, t: dict, to: str, evidence: dict, cls: str,
                   fields: dict) -> Optional[dict]:
        pid, tid = P.id, t["task_id"]
        cur = self.store.task(pid, tid)
        if cur is None or cur["state"] in S.TERMINAL or cur["state"] == S.BLOCKED:
            return None
        reason = str(evidence.get("blocker") or evidence.get("reason") or to)
        trow = {**cur, **{k: fields[k] for k in ("verification_id", "head_sha", "review_id")
                          if fields.get(k)}}
        ev = {k: evidence[k] for k in ("verification_id", "review_id", "session_id",
                                       "blocker_class", "decision_id") if evidence.get(k)}
        ev.update(self._lead_recheck(P, trow))
        try:
            ev["revision"] = P.ws.head()
        except GitError:
            pass
        # the deterministic recheck DECIDES: recovered evidence re-runs the gate,
        # a cleared readiness block resumes, before any paid lead call
        done = self._lead_recheck_resolves(P, cur, to, evidence, cls, ev, fields)
        if done is not None:
            return done
        ev["failed_checks"] = self._lead_failed_checks(P, tid, trow.get("verification_id") or "")
        if evidence.get("readiness"):
            ev["readiness"] = evidence["readiness"]
        trigger = {"state": to, "reason": reason[:1500], "class": cls}
        case = self.lead.open_case(P, trow, trigger, ev)
        if not case:
            return None
        cid = case["case_id"]
        blocker = self._lead_blocker_text(case, to, reason)
        ev2 = {**evidence, "blocker": blocker, "recovery_case": cid, "lead_trigger": to}
        try:
            S.check(cur["state"], S.BLOCKED, ev2)
        except S.TransitionError:
            # (no BLOCKED transition from this state): the original stop applies;
            # the case stays for the operator's view
            return None
        data = dict(fields.pop("data", None) or cur["data"] or {})
        data.update(recovery_case=cid, lead_trigger={"state": to, "reason": reason[:500],
                                                     "class": cls})
        fields.pop("blocker", None)
        fields.setdefault("next_action", blocker[:500])
        self.store.event(pid, tid, "lead_case", detail={
            "case_id": cid, "state": case["state"], "trigger": to, "class": cls,
            "fingerprint": case.get("fingerprint"), "reason": reason[:300]})
        self.store.release(pid, tid)
        return self.store.transition(pid, tid, S.BLOCKED, ev2, blocker=blocker, data=data,
                                     **fields)

    def _lead_recheck_resolves(self, P: Project, cur: dict, to: str, evidence: dict, cls: str,
                               ev: dict, fields: dict) -> Optional[dict]:
        """The deterministic recheck before a case decides (no paid call):
          * a readiness stop whose readiness now passes: no stop, the start
            retries on the next tick;
          * evidence recovered for a committed head with no uncommitted work:
            the task re-runs verification (BLOCKED -> VERIFYING) instead of
            opening a case.
        Once per (head, class): a repeat at the same head opens the case.
        None when it does not resolve the stop."""
        pid, tid = P.id, cur["task_id"]
        data = dict(cur.get("data") or {})
        head = str(ev.get("revision") or "")
        mark = data.get("lead_recheck") or {}
        if not head or (mark.get("head") == head and mark.get("cls") == cls):
            return None
        now = self.clock()
        marked = {**data, "lead_recheck": {"head": head, "cls": cls, "ts": now}}
        if str(cls or "").startswith("readiness_") and self._lc(P, "readiness"):
            try:
                spec = P.adapter.describe(tid)
            except AdapterError:
                spec = None
            r = self._lc_readiness(P, cur, spec, {"attempt": cur["attempt"] + 1,
                                                  "base": cur["base_sha"], "recheck": True}) \
                if spec is not None else {"cls": "unknown"}
            if r is None:
                self.store.update_task(pid, tid, data=marked,
                                       next_action="readiness re-check passed; retrying")
                self.store.event(pid, tid, "lead_recheck", detail={
                    "result": "readiness cleared", "class": cls, "head": head})
                return self.store.task(pid, tid)
            return None
        rec = int(ev.get("evidence_recovered") or 0)
        base = cur.get("base_sha") or ""
        if rec <= 0 or not base or head == base or cur["state"] == S.BLOCKED:
            return None
        try:
            if P.ws.status():
                return None              # uncommitted work: not a pure re-verification
        except GitError:
            return None
        vid = evidence.get("verification_id") or fields.get("verification_id") or \
            cur.get("verification_id") or ""
        blocker = (f"deterministic recheck: {rec} evidence record(s) recovered at "
                   f"{head[:12]}; re-verifying before any paid lead call")
        bev = {"blocker": blocker, "lead_recheck": True}
        if vid:
            bev["verification_id"] = vid
        try:
            S.check(cur["state"], S.BLOCKED, bev)
        except S.TransitionError:
            return None
        self.store.release(pid, tid)
        if self._acquire(P, cur, self._workspace_locks(P)):
            return None                  # the workspace is busy: the case path applies
        res = f"deterministic recheck recovered {rec} evidence record(s): verifying {head[:12]} " \
              f"again (no paid call)"
        with self.store.tx():
            self.store.transition(pid, tid, S.BLOCKED, bev, blocker=blocker, data=marked)
            out = self.store.transition(pid, tid, S.VERIFYING, {
                "resolution": res, "decision_id": f"lead_recheck:{head[:12]}",
                "evidence_recovered": rec}, next_action=res[:500])
        self.store.event(pid, tid, "lead_recheck", detail={
            "result": "re-verify", "evidence_recovered": rec, "head": head, "trigger": to,
            "class": cls})
        return out

    @staticmethod
    def _lead_blocker_text(case: dict, trigger: str = "", reason: str = "") -> str:
        trig = case.get("trigger") or {}
        trigger = trigger or trig.get("state") or ""
        reason = reason or trig.get("reason") or ""
        return (f"lead recovery: {case['state']} (case {case['case_id']}; would have been "
                f"{trigger}: {reason})")[:1000]

    def _lead_from_wait(self, P: Project, t: dict, decisions: list) -> None:
        """A no-progress / repeated-repair decision still open after the
        deterministic re-check: a lead case for it (the decision stays open
        and an operator answer to it still wins)."""
        pid, tid = P.id, t["task_id"]
        data = t.get("data") or {}
        if data.get("recovery_case"):
            return
        dw = data.get("decision_wait") or {}
        d0 = decisions[0]
        reason = f"decision {d0['decision_id']} ({d0['kind']}): {dw.get('why') or ''}: " \
                 f"{str(d0.get('question') or '')[:600]}"
        ev = {"decision_id": ",".join(d["decision_id"] for d in decisions),
              "decision_evidence": d0.get("evidence")}
        ev.update(self._lead_recheck(P, t))
        try:
            ev["revision"] = P.ws.head()
        except GitError:
            pass
        ev["failed_checks"] = self._lead_failed_checks(P, tid, t.get("verification_id") or "")
        case = self.lead.open_case(P, t, {"state": S.BLOCKED, "reason": reason[:1500],
                                          "class": "decision_" + d0["kind"]}, ev)
        if not case:
            return
        text = self._lead_blocker_text(case, S.BLOCKED, reason)
        self.store.update_task(pid, tid, blocker=text, next_action=text[:500], data={
            **data, "recovery_case": case["case_id"],
            "lead_trigger": {"state": S.BLOCKED, "reason": reason[:500],
                             "class": "decision_" + d0["kind"]}})
        self.store.event(pid, tid, "lead_case", detail={
            "case_id": case["case_id"], "state": case["state"], "trigger": "decision_wait",
            "decisions": [d["decision_id"] for d in decisions]})

    def _lead_detach(self, P: Project, t: dict, why: str) -> None:
        """The case is over without a resume (cancelled / resolved elsewhere):
        the task stays BLOCKED for the operator, without the case marker."""
        data = {k: v for k, v in (t.get("data") or {}).items()
                if k not in ("recovery_case", "lead_trigger")}
        trig = (t.get("data") or {}).get("lead_trigger") or {}
        text = f"{why}; the original blocker: {trig.get('reason') or t.get('blocker') or ''}"
        self.store.update_task(P.id, t["task_id"], data=data, blocker=text[:1000],
                               next_action=text[:500])
        self.store.event(P.id, t["task_id"], "lead_case_detached", detail={"why": why[:300]})

    def _lead_blocked(self, P: Project, t: dict) -> None:
        """BLOCKED on a recovery case: one bounded lead step, then the resume."""
        from .lead import OPEN_STATES
        pid, tid = P.id, t["task_id"]
        L = self.lead
        data = t.get("data") or {}
        cid = data["recovery_case"]
        case = L.case(cid)
        if case is None or case["state"] not in OPEN_STATES:
            return self._lead_detach(P, t, f"lead recovery case {cid} is "
                                           f"{(case or {}).get('state') or 'missing'}")
        dw = data.get("decision_wait") or {}
        # a question the case's own lead_escalation replaced (withdrawn by the
        # lead, superseded once Victor answers the escalation) is not an
        # operator decision: the case's NEEDS_VICTOR step handles that answer
        replaced = set(((case.get("validation") or {}).get("superseded_decisions")) or [])
        waits = [i for i in dw.get("ids") or [] if i not in replaced]
        if waits and self._lc(P) and self._lead_pending_strategy_supersedes(P, t, case, waits):
            return                       # _lc_blocked resumes the task on the next tick
        if waits and self._lc(P):
            decs = [self._lc_final_decision(i) for i in waits]
            if not any(d and d["status"] == "open" for d in decs):
                # the operator answered (or withdrew) the original question: his
                # decision wins; the case is cancelled and _lc_blocked resumes
                L.cancel(cid, "the operator decided the original question", P=P)
                data = {k: v for k, v in data.items() if k not in ("recovery_case",
                                                                   "lead_trigger")}
                self.store.update_task(pid, tid, data=data)
                self.store.event(pid, tid, "lead_case_cancelled", detail={
                    "case_id": cid, "reason": "operator answered the original decision"})
                return
        if case["paused"]:
            takeover = (case.get("validation") or {}).get("takeover")
            return self._note(P, t, f"lead recovery case {cid} "
                                    + ("taken over by " + str(takeover.get("actor"))
                                       if takeover else "paused by the operator")
                                    + ": the task stays BLOCKED")
        if case["state"] != "UPGRADING" and self._held(P, t, "lead recovery"):
            return                       # a paused project starts no lead / repair call
        case = L.step(P, t, case) or case
        t = self.store.task(pid, tid)
        text = self._lead_blocker_text(case)
        if t["blocker"] != text:
            self.store.update_task(pid, tid, blocker=text, next_action=text[:500])
            t = self.store.task(pid, tid)
        ra = L.resume_action(pid, tid)
        if ra and ra.get("case_id") == cid:
            if self._held(P, t, "lead recovery resume"):
                return
            self._lead_resume(P, t, L.case(cid), dict(ra.get("action") or {}))

    def _lead_pending_strategy_supersedes(self, P: Project, t: dict, case: dict,
                                          waits: list) -> bool:
        """A case opened on a repeat question while an EARLIER case's lead
        strategy was still waiting for its one implementer session (before
        v0.123.1 the repeat guard re-asked before the primary ever tried it):
        that unused strategy is the answer. Only while the new case has done
        no more than diagnose (no proposal, candidate or escalation), only for
        open recovery_escalation waits, and only for a strategy that is new to
        the task and waives nothing. The case is cancelled (its sessions are
        ended) and the question withdrawn -- both kept and journaled -- so the
        task resumes READY and the strategy reaches the primary once."""
        from .lead import OPEN_STATES
        pid, tid, cid = P.id, t["task_id"], case["case_id"]
        data = t.get("data") or {}
        pend = data.get("lead_assignment") or {}
        text = str(pend.get("assignment") or "").strip()
        if not text or not pend.get("case_id") or pend.get("case_id") == cid:
            return False
        prior = self.lead.case(pend["case_id"])
        if not prior or prior["state"] in OPEN_STATES:
            return False
        if case["state"] != "DIAGNOSING" or case.get("decision_id") or                 (case.get("proposed_action") or {}) or                 int((case.get("counters") or {}).get("candidates") or 0):
            return False
        decs = [self.obligations.decision(i) for i in waits]
        if not decs or any(not d or d["kind"] != "recovery_escalation" or d["status"] != "open"
                           for d in decs):
            return False
        from .recovery import directive_violations, strategy_fp
        if directive_violations(text):
            return False
        if strategy_fp(text) in {r.get("strategy_fp")
                                 for r in self.recovery.repairs(pid, tid)}:
            return False
        reason = (f"lead case {pend['case_id']}'s strategy has not reached the implementer "
                  f"yet; it answers {', '.join(waits)}")
        self.lead.cancel(cid, reason, P=P)
        with self.store.tx():
            for did in waits:
                self.store.x("UPDATE decisions SET status='withdrawn' WHERE decision_id=? AND "
                             "status='open'", (did,))
                self.store.event(pid, tid, "decision_withdrawn", detail={
                    "decision_id": did, "by": "lead_manager", "case_id": pend["case_id"],
                    "reason": reason[:300]})
            self.store.update_task(pid, tid, data={
                k: v for k, v in data.items() if k not in ("recovery_case", "lead_trigger")})
            self.store.event(pid, tid, "lead_case_cancelled", detail={
                "case_id": cid, "reason": reason[:300], "strategy_case": pend["case_id"]})
        return True

    def _lead_operate(self, P: Project, t: dict, case: dict, act: dict) -> None:
        """[lead_manager] full_authority: execute the lead's operations for it.
        The lead session never writes; the coordinator is the single writer here:
        the task is BLOCKED (no primary session runs) and the workspace lock is
        held for the whole step. Every operation is one journaled action (actor,
        case, source revision, paths, result, validation), idempotent by op key so
        a crash resumes where it stopped. Any refused or failed operation sends
        the case back to DIAGNOSING (a materially different action is then
        required); only when all of them verified does the follow-up run."""
        pid, tid, cid = P.id, t["task_id"], case["case_id"]
        L = self.lead
        actor = "lead_manager:" + cid
        if self._acquire(P, t, self._workspace_locks(P)):
            return                                     # another writer holds the workspace
        rev = P.ws.head()
        spec = L._spec(P, tid)
        s = L.settings(P)
        failures, results = [], []
        for i, op in enumerate(act.get("operations") or []):
            key = f"{cid}:op:{i}"
            prior = L._action(key)
            if prior and prior["status"] == "done":
                results.append(prior.get("result") or "")
                continue
            if prior is None:
                L._act(case, "lead_op", status="started", actor=actor, op_key=key,
                       revision=rev, repo="game", scope_files=list(op.get("paths") or [])
                       + ([op["path"]] if op.get("path") else []),
                       result=f"{op['op']}: started",
                       detail={"op": op, "case_id": cid, "source_revision": rev})
            try:
                ok, result, validation = self._lead_op(P, t, case, op, spec, s, actor)
            except Exception as e:                                # noqa: BLE001
                ok, result, validation = False, f"{type(e).__name__}: {e}"[:500], {}
            L._upd(key, status="done" if ok else "failed", result=f"{op['op']}: {result}"[:2000],
                   validation=validation)
            results.append(result)
            if not ok:
                failures.append(f"{op['op']}: {result}")
        t = self.store.task(pid, tid)
        if failures:
            self.store.release(pid, tid, self._workspace_locks(P))
            L._act(case, "operate_failed", actor=actor, status="failed",
                   result="; ".join(failures)[:1500],
                   detail={"failures": failures, "results": results},
                   op_key=f"{cid}:operate_failed:{len(L.actions(cid))}")
            L._save(cid, state="DIAGNOSING", proposed_action={})
            self.store.event(pid, tid, "lead_operate_failed", detail={
                "case_id": cid, "failures": failures[:6]})
            return
        then = str(act.get("then") or "resume")
        if then == "reverify" and P.ws.head() == t.get("base_sha"):
            # nothing was committed since the last verification: re-running the
            # same checks proves nothing new, the primary continues instead
            then = "resume"
        L._act(case, "operate_done", actor=actor, result=f"all operations verified; then {then}",
               detail={"results": results}, op_key=f"{cid}:operate_done")
        self.store.event(pid, tid, "lead_operate", detail={
            "case_id": cid, "operations": [o.get("op") for o in act.get("operations") or []],
            "then": then, "revision": rev})
        if then != "reverify":
            # the follow-up starts the primary (or waits): the lead's write step is over
            self.store.release(pid, tid, self._workspace_locks(P))
        if then in ("resume", "reverify"):
            # the applied operations ARE the lead's materially different strategy:
            # it rides with the task for exactly one session start (like a lead
            # assignment), so the repeat guard does not re-ask the question
            # this case just answered when the task lands READY
            ops = "; ".join(f"{o.get('op')}: {o.get('reason') or ''}"[:200]
                            for o in act.get("operations") or [])
            text = (f"Lead recovery case {cid} resolved the blocker by delegated operations "
                    f"({ops}). Diagnosis: {str(act.get('hypothesis') or '')[:1500]} Continue "
                    f"the approved plan from its NEXT line; every check still applies.")[:4000]
            # the primary sees what the lead did (first finding, as for an assignment)
            prior = [f for f in ((t["data"] or {}).get("findings") or [])
                     if not isinstance(f, str) or not f.startswith((
                         self.LEAD_ASSIGNMENT_HEADER.split("{")[0],
                         self.LEAD_ANSWER_HEADER.split("{")[0]))]
            self.store.merge_data(pid, tid, findings=[
                (self.LEAD_ASSIGNMENT_HEADER.format(case=cid) + text)[:4000]] + prior[:20],
                lead_assignment={"case_id": cid, "files": [], "tests": [], "extra_attempts": 0,
                                 "authorized": [], "via": "operate", "assignment": text})
            t = self.store.task(pid, tid)
        if then == "assign":
            nxt = {"kind": "assign_game_repair", "assignment": act.get("assignment") or "",
                   "files": list(act.get("files") or []), "tests": [], "extra_attempts": 0,
                   "via": "operate"}
        elif then == "reverify":
            nxt = {"kind": "reverify", "via": "operate"}
        else:
            nxt = {"kind": "resume", "via": "operate"}
        return self._lead_resume(P, t, L.case(cid), nxt)

    def _lead_op(self, P: Project, t: dict, case: dict, op: dict, spec, s: dict,
                 actor: str) -> tuple:
        """(ok, result, validation) of one delegated operation, checked after."""
        import hashlib
        import shutil
        from .lead import _match, OPEN_STATES
        from .obligations import DecisionConflict, DecisionRefused, OPERATOR_ONLY_KINDS
        pid, tid, cid = P.id, t["task_id"], case["case_id"]
        kind = op["op"]
        data = t.get("data") or {}
        if kind == "clean_untracked":
            st = {str(p).replace("\\", "/"): str(x).strip() for x, p in P.ws.status()}
            # only files dirty when the TASK began are the operator's; a session
            # baseline already includes scratch an earlier session left behind
            base = set(data.get("task_baseline_dirty") or [])
            scope = list(getattr(spec, "scope", None) or [])
            acc = self.lead._acceptance_globs(P, spec, s)
            done, refused = [], []
            for rel in op.get("paths") or []:
                full = os.path.join(P.profile.workspace, rel)
                bk = os.path.join(self.state_dir, "lead_backups", cid, rel)
                if not os.path.lexists(full):
                    if os.path.exists(bk):
                        done.append({"path": rel, "backup": bk, "already_removed": True})
                    else:
                        refused.append(f"{rel}: not present")
                    continue
                why = ("leaves the workspace" if not P.profile.in_workspace(rel) else
                       "not an untracked file" if st.get(rel) != "??" else
                       "a directory or link" if os.path.isdir(full) or os.path.islink(full) else
                       "was already there when the task began" if rel in base else
                       "inside the task scope (task work, not scratch)"
                       if scope and _match(rel, scope) else
                       "an acceptance test" if acc and _match(rel, acc) else "")
                if why:
                    refused.append(f"{rel}: {why}")
                    continue
                with open(full, "rb") as fh:
                    blob = fh.read()
                sha = hashlib.sha256(blob).hexdigest()
                os.makedirs(os.path.dirname(bk), exist_ok=True)
                shutil.copy2(full, bk)
                with open(bk, "rb") as fh:
                    if hashlib.sha256(fh.read()).hexdigest() != sha:
                        refused.append(f"{rel}: backup verification failed")
                        continue
                os.remove(full)
                done.append({"path": rel, "sha256": sha, "backup": bk, "bytes": len(blob)})
            after = {str(p).replace("\\", "/") for _, p in P.ws.status()}
            still = [d["path"] for d in done if d["path"] in after]
            # the session baseline recorded this task's own scratch; its journaled,
            # backed-up removal is not "unrelated work modified" (files dirty when
            # the task began were refused above and stay protected)
            gone = {d["path"] for d in done if d["path"] not in still}
            bd = (self.store.task(pid, tid)["data"] or {}).get("baseline_dirty")
            if gone and isinstance(bd, dict):
                self.store.merge_data(pid, tid, baseline_dirty={
                    k: v for k, v in bd.items() if k not in gone})
            elif gone and isinstance(bd, list):
                self.store.merge_data(pid, tid, baseline_dirty=[x for x in bd if x not in gone])
            ok = not refused and not still
            return ok, (f"removed {len(done)} file(s) with backups"
                        + (f"; refused {'; '.join(refused)}" if refused else "")
                        + (f"; still present {still}" if still else "")), \
                {"removed": done, "refused": refused, "still_present": still}
        if kind == "extend_budget":
            n = int(op.get("attempts") or 0)
            ref = self.LEAD_REF + cid
            counters, problems = [], []
            if self._lc(P, "obligations"):
                from .obligations import LedgerRefused
                self._lc_backfill(P, t)
                for counter in ("attempt_ordinary", "sessions"):
                    try:
                        self.obligations.authorize(pid, tid, counter, n,
                                                   f"lead case {cid} (delegated authority): "
                                                   f"{op.get('reason')}"[:500],
                                                   self.LEAD_POLICY_ACTOR, ref)
                        counters.append(counter)
                    except LedgerRefused as e:
                        if "already authorized" in str(e):
                            counters.append(counter)          # idempotent after a crash
                        else:
                            problems.append(f"{counter}: {e}"[:200])
            else:
                grants = dict(data.get("lead_grants") or {})
                grants[cid] = int(grants.get(cid, 0)) + n
                self.store.merge_data(pid, tid, lead_grants=grants)
                counters.append("lead_grants")
            return not problems, f"{n} extra attempt(s) authorized ({', '.join(counters)})" + \
                (f"; refused {problems}" if problems else ""), \
                {"counters": counters, "ref": ref, "n": n, "problems": problems}
        if kind == "resolve_decision":
            did = op.get("decision_id") or ""
            d = self.obligations.decision(did)
            if not d or d["project_id"] != pid or d["task_id"] != tid:
                return False, f"{did} is not a decision of {pid}/{tid}", {}
            if d["status"] == "answered" and d.get("actor") == actor:
                return True, f"{did} already resolved by this case", {"decision_id": did}
            if d["status"] != "open":
                return False, f"{did} is {d['status']}", {}
            if d["kind"] in OPERATOR_ONLY_KINDS or d["kind"] == "lead_escalation":
                return False, f"{did} ({d['kind']}) is reserved for Victor", {}
            res = str(op.get("resolution") or "")
            value = ({"strategy": res, "reason": op.get("reason"), "authority": "lead_policy"}
                     if d["kind"] in ("recovery_escalation", "no_progress") else
                     {"resolution": res, "reason": op.get("reason"), "authority": "lead_policy"})
            if d["kind"] == "recovery_escalation":
                from .recovery import link_prior_escalation
                link_prior_escalation(self.obligations, pid, tid, did)
            try:
                self.obligations.answer(did, value, actor, "lead_policy")
            except (DecisionRefused, DecisionConflict) as e:
                return False, f"{type(e).__name__}: {e}"[:400], {}
            after = self.obligations.decision(did) or {}
            self.store.event(pid, tid, "decision_answered", detail={
                "decision_id": did, "kind": d["kind"], "actor": actor, "authority": "lead_policy",
                "note": str(op.get("reason") or "")[:300]})
            return after.get("status") == "answered", f"{did} ({d['kind']}) resolved", \
                {"decision_id": did, "status": after.get("status")}
        if kind == "raise_bound":
            v = (self.lead.case(cid) or {}).get("validation") or {}
            raised = dict(v.get("raised") or {})
            raised[op["bound"]] = int(raised.get(op["bound"], 0)) + int(op.get("by") or 1)
            self.lead._merge_validation(cid, raised=raised)
            return True, f"{op['bound']} raised by {op.get('by')}", {"raised": raised}
        if kind == "approve_test_change":
            path = op.get("path") or ""
            # W2.3 (wired by W1): a test the lead's own write session changed is never
            # self-approved; it is an operator expectation_change decision (the
            # recommended answer is shown, but an operator-only kind never times out)
            from .lead import lead_self_changed_tests
            before = str(((self.lead.case(cid) or {}).get("source_revision"))
                         or (t.get("data") or {}).get("task_base") or t.get("base_sha") or "")
            own = lead_self_changed_tests(P, before) if s.get("write_access") else {}
            if path in own:
                q = (f"[lead case {cid}] the lead changed acceptance test {path} itself "
                     f"({'; '.join(own[path])[:300]}): accept the new expectation?")
                did = self.obligations.open_decision(
                    pid, tid, "expectation_change", q,
                    evidence={"case_id": cid, "path": path, "reason": op.get("reason"),
                              "self_changed": own[path][:5],
                              "recommended": "reject: restore the test's expectation"},
                    options=["reject: restore the test's expectation",
                             "accept: the new expectation is the product's"],
                    recommendation="reject: restore the test's expectation",
                    blocks=[f"lead_case:{cid}", path])
                self.store.event(pid, tid, "lead_self_approval_refused", detail={
                    "case_id": cid, "path": path, "decision_id": did})
                return False, (f"the lead changed {path} itself in this session; its "
                               f"expectation change waits for operator decision {did} "
                               f"(expectation_change), never a lead approval"), \
                    {"decision_id": did, "path": path, "self_changed": own[path][:5]}
            q = f"[lead case {cid}] approve a change to acceptance test {path}"
            did = self.obligations.open_decision(
                pid, tid, "lead_test_change", q,
                evidence={"case_id": cid, "path": path, "reason": op.get("reason")},
                blocks=[f"lead_case:{cid}"])
            d = self.obligations.decision(did) or {}
            if d.get("status") == "open":
                try:
                    self.obligations.answer(did, {"approved": path, "reason": op.get("reason"),
                                                  "authority": "lead_policy"},
                                            actor, "lead_policy")
                except (DecisionRefused, DecisionConflict) as e:
                    return False, f"{type(e).__name__}: {e}"[:400], {}
            v = (self.lead.case(cid) or {}).get("validation") or {}
            prev = list(v.get("approved_test_changes") or [])
            if path not in prev:
                self.lead._merge_validation(cid, approved_test_changes=prev + [path])
            return True, f"test change to {path} recorded as decision {did}; it must still be " \
                         f"made and re-run", {"decision_id": did, "path": path}
        return False, f"unknown operation {kind}", {}

    def _lead_upgraded(self, case: dict, act: dict) -> bool:
        """The case's coordinator upgrade is installed: the lead's UPGRADING
        step saw the supervisor's request `done` (and set `upgraded`), and the
        request still says so (supervisor.get_request). The supervisor never
        moves the case itself."""
        uid = act.get("upgrade_id") or case.get("upgrade_id")
        if not act.get("upgraded") or not uid:
            return False
        try:
            from .supervisor import get_request
            req = get_request(self.store, uid)
        except Exception:                                         # noqa: BLE001
            return False
        return bool(req and req["status"] == "done")

    def _lead_grant(self, P: Project, t: dict, case: dict, n: int) -> dict:
        """Extra ordinary attempts (and the sessions they need) under the
        delegated [lead_manager] extra_attempts policy, at most once per case:
        authorized ledger rows referencing the case (lifecycle budget ledger),
        else the task's lead_grants allowance. Journaled on the case."""
        pid, tid, cid = P.id, t["task_id"], case["case_id"]
        cap = int(self.lead.settings(P).get("extra_attempts") or 0)
        n = max(0, min(int(n or 0), cap))
        out = {"n": n, "ref": self.LEAD_REF + cid, "policy": "[lead_manager] extra_attempts",
               "counters": []}
        if n <= 0:
            return out
        if self._lc(P, "obligations"):
            from .obligations import LedgerRefused
            self._lc_backfill(P, t)
            for counter in ("attempt_ordinary", "sessions"):
                try:
                    self.obligations.authorize(pid, tid, counter, n,
                                               f"lead case {cid}: delegated policy "
                                               f"[lead_manager] extra_attempts={cap}",
                                               self.LEAD_POLICY_ACTOR, out["ref"])
                    out["counters"].append(counter)
                except LedgerRefused as e:
                    out.setdefault("refused", []).append(f"{counter}: {e}"[:200])
        else:
            out["counters"] = ["lead_grants"]
        try:
            self.lead._act(case, "authorize_attempts", result=f"{n} extra attempt(s)",
                           detail=out, repo="game", actor=self.LEAD_POLICY_ACTOR,
                           op_key=f"{cid}:grant")
        except Exception as e:                                    # noqa: BLE001
            self.store.event(pid, tid, "warning", detail={"lead_grant_journal": str(e)[:300]})
        return out

    def _lead_resume(self, P: Project, t: dict, case: dict, act: dict) -> None:
        """Apply a RESUMING case's action. Each transition carries
        {recovery_case, action}; the case is resolved in the same transaction."""
        pid, tid, cid = P.id, t["task_id"], case["case_id"]
        kind = str(act.get("kind") or "")
        if kind == "operate":
            return self._lead_operate(P, t, case, act)
        if kind in ("reverify", "coordinator_repair"):
            self._reword_commits(P, t)           # W1.4: a lead commit without the prefix
        head = P.ws.head()
        data = {k: v for k, v in (t["data"] or {}).items()
                if k not in ("recovery_case", "lead_trigger", "decision_wait", "blocker_class",
                             "readiness_block", "readiness", self.LC_PENDING)}
        dw_ids = list(((t["data"] or {}).get("decision_wait") or {}).get("ids") or [])
        ev = {"recovery_case": cid, "action": kind}
        target, extra = S.READY, {}
        text = ""
        if kind == "victor_answer":
            if act.get("how"):
                # the lead classified the answer (stop / strategy); budget and
                # unclear answers never reach the engine
                how, val = str(act["how"]), str(act.get("text") or "")
            else:
                from .recovery import Recovery
                d = self.obligations.decision(act.get("decision_id") or "") or \
                    {"answer": act.get("answer")}
                how, val = Recovery._escalation_answer(d)
            if how == "stop":
                did = act.get("decision_id") or ""
                full = self.obligations.decision(did) if did else None
                by = ("timeout default" if (full or {}).get("authority") == "timeout_default"
                      else "operator decision")
                with self.store.tx():
                    self.lead.resolve(cid, f"stopped by {by}: {val}"[:300])
                    stop = f"parked: lead recovery stopped by {by} {did}: {val or 'stop'}"
                    self.store.update_task(pid, tid, blocker=stop[:1000],
                                           next_action=stop[:500], data=data)
                    self.store.event(pid, tid, "lead_case_stopped", detail={
                        "case_id": cid, "decision_id": did, "by": by})
                # W1.2: a parked sprint leaves a summary; the queue moves on
                self._park_summary(P, self.store.task(pid, tid), stop, decision_id=did,
                                   kind="lead_escalation", case_id=cid)
                return
            text = val if how == "strategy" else ""
            ev["decision_id"] = act.get("decision_id") or ""
        if kind == "assign_game_repair" or text:
            assignment = str(act.get("assignment") or "") if kind == "assign_game_repair" \
                else text
            n = int(act.get("extra_attempts") or 0) if kind == "assign_game_repair" else \
                int(self.lead.settings(P).get("extra_attempts") or 0)
            grant = self._lead_grant(P, t, case, n)
            if not self._lc(P, "obligations") and grant["n"]:
                grants = dict(data.get("lead_grants") or {})
                grants[cid] = grant["n"]
                data["lead_grants"] = grants
            finding = (self.LEAD_ASSIGNMENT_HEADER if kind == "assign_game_repair"
                       else self.LEAD_ANSWER_HEADER).format(case=cid) + assignment
            if act.get("files"):
                finding += " (files: " + ", ".join(act["files"][:12]) + ")"
            # the original failures stay: the assignment comes first, then what
            # the checks / review actually reported
            prior = [f for f in ((t["data"] or {}).get("findings") or [])
                     if not isinstance(f, str) or not f.startswith((
                         self.LEAD_ASSIGNMENT_HEADER.split("{")[0],
                         self.LEAD_ANSWER_HEADER.split("{")[0]))]
            data.update(findings=[finding[:4000]] + prior[:20], free_retry=None,
                        lead_assignment={
                "case_id": cid, "assignment": assignment[:4000],
                "files": list(act.get("files") or [])[:20],
                "tests": list(act.get("tests") or [])[:20], "extra_attempts": grant["n"],
                "authorized": grant["counters"]})
            data = {k: v for k, v in data.items() if v is not None}
            ev["extra_attempts"] = grant["n"]
            # the assignment resumes as a proper repair round (BLOCKED ->
            # CHANGES_REQUIRED carries the findings as transition evidence),
            # not as a fresh READY start
            target = S.CHANGES_REQUIRED
            ev["findings"] = list(data.get("findings") or [])
            res = f"lead case {cid}: the primary repairs per the lead's assignment"
        elif kind == "reverify" or (kind == "coordinator_repair" and
                                    self._lead_upgraded(case, act)):
            # committed work and nothing uncommitted on top: verify the current head
            # again (no paid primary session), also when HEAD moved since the stop
            try:
                pending = [p for x, p in P.ws.status() if str(x).strip() != "??"]
            except GitError:
                pending = ["?"]
            if pending and pending != ["?"] and self.lead.settings(P).get("write_access") \
                    and self.lead._lead_fixed(cid):
                # W1.5: edits a write-access lead left uncommitted are committed as the
                # lead's (in scope only) and re-verified here, never handed to a cold
                # implementer session
                sha = self._lead_commit_pending(P, t, case, act)
                if sha:
                    head = P.ws.head()
                    ev["lead_commit"] = sha
                    fresh = self.store.task(pid, tid)["data"] or {}
                    data = {k: v for k, v in fresh.items()
                            if k not in ("recovery_case", "lead_trigger", "decision_wait",
                                         "blocker_class", "readiness_block", "readiness",
                                         self.LC_PENDING)}
                    try:
                        pending = [p for x, p in P.ws.status() if str(x).strip() != "??"]
                    except GitError:
                        pending = ["?"]
            if pending and pending != ["?"]:
                self.store.event(pid, tid, "lead_resume_uncommitted", detail={
                    "case_id": cid, "paths": pending[:20],
                    "note": "uncommitted changes outside the task's files stay; a fresh "
                            "session is told to check them"})
            if head and t.get("base_sha") and head != t["base_sha"] and not pending:
                if self._acquire(P, t, self._workspace_locks(P)):
                    return
                target = S.VERIFYING
                extra["decision_id"] = f"lead_case:{cid}"
                if head != t.get("head_sha"):
                    ev["head_moved"] = f"{str(t.get('head_sha') or '')[:12]}..{head[:12]}"
            if kind == "coordinator_repair":
                ev["upgrade_id"] = case.get("upgrade_id") or act.get("upgrade_id") or ""
                res = f"lead case {cid}: coordinator upgrade {ev['upgrade_id']} installed; " \
                      f"{'verifying the same head again' if target == S.VERIFYING else 'retry'}"
            else:
                res = f"lead case {cid}: evidence re-verification (no paid call)"
        elif kind == "wait":
            res = f"lead case {cid}: the deferred {act.get('classification') or 'environment'}" \
                  f" condition re-checked clear"
        elif kind == "resume":
            res = f"lead case {cid}: delegated operations applied and verified; the primary " \
                  f"resumes the sprint"
        else:
            return self._note(P, t, f"lead recovery case {cid} resuming with an action the "
                                    f"engine does not apply ({kind or 'none'}); waiting")
        with self.store.tx():
            if dw_ids and self._lc(P):
                for did in dw_ids:
                    # the lead's resume answers the stall the question was about; the
                    # decision is kept (withdrawn), never deleted
                    if self.store.q("SELECT 1 FROM decisions WHERE decision_id=? AND "
                                    "status='open'", (did,)):
                        self.store.x("UPDATE decisions SET status='withdrawn' WHERE "
                                     "decision_id=? AND status='open'", (did,))
                        self.store.event(pid, tid, "decision_withdrawn", detail={
                            "decision_id": did, "by": "lead_manager",
                            "reason": f"resolved by lead recovery case {cid} ({kind})"})
            self.store.transition(pid, tid, target, {"resolution": res, **ev, **extra},
                                  data=data, next_action=res[:500])
            self.lead.resolve(cid, f"resumed: {kind} -> {target}")
        self.store.event(pid, tid, "lead_resume", detail={**ev, "to": target})

    def _lead_commit_pending(self, P: Project, t: dict, case: dict, act: dict) -> str:
        """W1.5: commit the in-scope files a write-access lead session left
        uncommitted (the task's files, its closeout-exempt paths and the files
        the lead named), as the lead's own commit under the task prefix. Files
        outside that set are never touched. Returns the commit sha or ''."""
        pid, tid, cid = P.id, t["task_id"], case["case_id"]
        try:
            spec = P.adapter.describe(tid)
        except AdapterError:
            return ""
        allowed = list(spec.scope) + list(P.adapter.exempt_paths(spec)) + \
            [str(f) for f in (act.get("files") or [])]
        baseline = set(t.get("data", {}).get("task_baseline_dirty") or [])
        try:
            dirty = P.ws.status()
        except GitError:
            return ""
        paths = sorted({p for _, p in dirty if matches_any(p, allowed) and p not in baseline})
        if not paths:
            return ""
        from . import bookkeeping as BK
        hyp = str(act.get("hypothesis") or "lead fix")[:72]
        msg = BK.message(P, tid, f"lead case {cid}: {hyp}")
        try:
            sha = P.ws.commit_paths(paths, msg)
        except (GitError, OSError) as e:
            self.store.event(pid, tid, "lead_commit_failed", detail={
                "case_id": cid, "paths": paths[:20], "error": str(e)[:300]})
            return ""
        if not sha:
            return ""
        kept = list((self.store.task(pid, tid).get("data") or {}).get("lead_commits") or [])
        self.store.merge_data(pid, tid, lead_commits=(kept + [sha])[-50:])
        self.store.event(pid, tid, "lead_commit", detail={
            "case_id": cid, "sha": sha, "paths": paths[:40], "message": msg,
            "note": "uncommitted write-access lead edits committed as the lead's; "
                    "re-verified in place (scope, review and gate still apply)"})
        try:
            self.lead._act(case, "lead_commit", result=f"{sha[:12]}: {len(paths)} file(s)",
                           repo="game", scope_files=paths[:40],
                           detail={"sha": sha, "paths": paths[:40]},
                           op_key=f"{cid}:lead_commit:{sha[:12]}")
        except Exception:                                         # noqa: BLE001
            pass
        return sha

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
            # the main checkout may take any slot, a track slot 1.. only: the main
            # sprint is counted first against max_agents (tracks/manager.py)
            for i in self.tracks.writer_slots(P, t):
                if self.store.acquire([f"writer:{i}"], pid, tid) is None:
                    self._journal_locks(P, t, [r for r in resources if r not in before], "acquire")
                    return None
            self.store.release(pid, tid, [r for r in resources if r not in before])
            busy = [l for l in self.store.locks() if l["resource"].startswith("writer:")]
            cap = self.tracks.max_agents()      # the live cap, not the static --max-writers
            if not busy and self.tracks.is_track(P) and cap < 2:
                self._note(P, t, f"waiting for a writer slot: a track needs max_agents 2 or "
                                 f"more (now {cap}; the main checkout counts first)")
            else:
                self._note(P, t, "waiting for a writer slot (held by "
                                 + (", ".join(f"{l['project_id']}/{l['task_id']}" for l in busy)
                                    or "no one")
                                 + f"; max_agents {cap})")
            return {"resource": "writer"}
        self._journal_locks(P, t, [r for r in resources if r not in before], "acquire")
        return None

    def _journal_locks(self, P: Project, t: dict, resources: list[str], event: str) -> None:
        try:
            self.tracks.resources.on_locks(P, t, resources, event)
        except Exception:                                         # noqa: BLE001
            pass

    def _release_session(self, P: Project, t: dict) -> None:
        pid, tid = P.id, t["task_id"]
        held = self.store.holds(pid, tid)
        res = self._session_locks(P) + [r for r in held if r.startswith("writer:")]
        self.store.release(pid, tid, res)
        self._journal_locks(P, t, [r for r in res if r in held and not r.startswith("writer:")],
                            "release")

    def _block(self, P: Project, t: dict, why: str) -> None:
        self.store.release(P.id, t["task_id"])
        self._stop(P, t, S.BLOCKED, {"blocker": why}, blocker=why[:1000])

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
        out = {"projects": [], "locks": self.store.locks(), "max_agents": self.max_writers}
        for p in self.store.projects():
            tasks = self.store.tasks(p["project_id"])
            tracks = self.store.tracks(p["project_id"])
            out["projects"].append({**p, "tasks": tasks, **({"tracks": tracks} if tracks else {})})
        return out
