"""Merging a finished track back into master (Team Lead piece 2, merge part).

Only at a quiescent point of the main sprint (no live main session, no main
task between a bound head and its close), never mid-session. Stages, each
recorded in the track's data (`merge_stage`) so a restart resumes:

  wait      -> the main sprint is quiescent
  rebase    integrate <master> in the worktree (nothing when master is already in
            the track, a merge when the track has merge commits, else `git
            rebase`); a conflict is aborted and
            becomes a Lead repair (when a lead manager offers one) plus a
            decision with the default "stop this track"
  ff        fast-forward master to the track branch in the main checkout; the
            track's commits are added to every open main task's coordinator
            commits (bookkeeping B1), so the main sprint's scope checks exempt them
  gate      the project's checks on the merge result in the main checkout, for
            the merged files: the studio_gate runner (fast lane gate module)
            under a Studio lease when the track needs Studio, else the adapter's
            verify; a green gate of the last sprint that still carries to the
            merged head (fast_lane._carries) is reused
  studio_batch  (2026-10-28, [automation] merge_studio_batch) after a green verify:
            the scripted Studio gate ([studio_calls], studio_calls.py) in the main
            checkout for each sprint of the track that [studio_calls] covers, in order,
            back to back under ONE Studio lease (it waits like the gate's lease above).
            The assets are built first, Studio-tested once at the end; a failing sprint
            fails the track naming it and its rows (and gets them as its findings); a
            tooling ERROR retries that sprint, never re-running the ones that passed
  done      record `track_merged`, remove the worktree (the branch is kept)

A red gate leaves the merged commits on master and the track `failed` with the
findings: the operator (or the chat) decides; nothing is reverted by itself.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Optional

from .. import states as S
from ..adapters.base import FAIL, PASS, UNKNOWN
from ..adapters.project import failed_findings
from .manager import TrackError, _git

DECISION_DEFAULT = "stop this track"
# D (2026-10-28): a merge gate that ends in a tooling fault (a runner that exited without
# a verdict, an ERROR/UNKNOWN result, checks that could not run) is retried this many times
# with this backoff (doubling) before the track fails
GATE_TOOLING_RETRIES = 2
GATE_RETRY_BACKOFF_S = 60
DECISION_OPTIONS = (DECISION_DEFAULT, "retry the merge", "keep the branch, no merge")


class MergeManager:
    def __init__(self, tm):
        self.tm = tm
        self.c = tm.c
        self.store = tm.store
        self.clock = tm.clock

    # ------------------------------------------------------------------ requests

    def request(self, P, name: str, actor: str) -> dict:
        """The operator asks for the merge (track_merge): allowed when every sprint
        of the track is COMPLETE, or to retry after a conflict / failed gate."""
        P = self.tm.main_of(P)
        row = self.store.track(P.id, name)
        if row is None:
            raise TrackError(f"no track {name}")
        if row["state"] in ("merged", "stopped"):
            raise TrackError(f"track {name} is already {row['state']}")
        open_ = [s for s in row["sprints"]
                 if (self.store.task(P.id, s) or {}).get("state") != S.COMPLETE]
        if open_ and row["state"] != "failed":
            raise TrackError(f"track {name}: not finished ({', '.join(open_[:4])})")
        if open_:
            raise TrackError(f"track {name} failed with unfinished sprints "
                             f"({', '.join(open_[:4])}); stop it or re-queue them")
        data = {k: v for k, v in row["data"].items()
                if k not in ("conflict", "decision", "gate", "failed", "merge_wait",
                             "gate_retries", "gate_retry_at", "merge_bg", "studio_batch")}
        data.update(merge_stage="wait", merge_requested_by=actor)
        row = self.store.update_track(P.id, name, state="merging", data=data)
        self.store.event(P.id, "", "track_merge_requested", detail={"track": name, "actor": actor})
        return row

    def abort(self, P, row: dict, why: str) -> None:
        """Stop whatever merge stage is in flight (a track stop)."""
        run = (row.get("data") or {}).get("gate") or {}
        if run.get("pid") and not run.get("result"):
            try:
                self.c.fast_lane._kill_tree(int(run["pid"]))
            except Exception:                                     # noqa: BLE001
                pass
        if (row.get("data") or {}).get("merge_stage") == "rebase":
            _git(row["worktree"], "rebase", "--abort", check=False)
        cur = ((row.get("data") or {}).get("studio_batch") or {}).get("current") or {}
        if cur.get("sprint"):
            try:
                self.c.studio_calls.abort(P, cur["sprint"], why)
            except Exception:                                     # noqa: BLE001
                pass

    # ------------------------------------------------------------------ the step

    def step(self, P, row: dict) -> None:
        P = self.tm.main_of(P)
        if not row or row.get("state") != "merging":
            return
        data = dict(row.get("data") or {})
        stage = data.get("merge_stage") or "wait"
        if data.get("decision") and data["decision"].get("status") == "open":
            return                                 # waiting on the conflict decision
        if stage == "wait":
            quiet, why = self.tm.main_quiescent(P)
            if not quiet:
                self._wait(P, row, why)
                return
            dirty = self._main_dirty(P)
            if dirty:
                self._wait(P, row, "main checkout has uncommitted changes: "
                           + ", ".join(dirty[:4]))
                return
            self._set(P, row, merge_stage="rebase", merge_wait=None)
            self._rebase(P, self.store.track(P.id, row["name"]))
        elif stage == "rebase":
            self._rebase(P, row)
        elif stage == "ff":
            self._ff(P, row)
        elif stage == "gate":
            at = float(data.get("gate_retry_at") or 0)
            if at and self.clock() < at:
                self._wait(P, row, f"merge gate retry {data.get('gate_retries')}/"
                                   f"{GATE_TOOLING_RETRIES} after a tooling fault")
                return
            self._gate(P, row)
        elif stage == "gate_running":
            self._poll_gate(P, row)
        elif stage == "studio_batch":
            at = float(data.get("gate_retry_at") or 0)
            if at and self.clock() < at:
                self._wait(P, row, f"merge Studio batch retry {data.get('gate_retries')}/"
                                   f"{GATE_TOOLING_RETRIES} after a tooling fault")
                return
            self._batch_step(P, row)
        elif stage == "done":
            self._finish(P, row)

    def _set(self, P, row: dict, **kv) -> dict:
        return self.store.merge_track_data(P.id, row["name"], **kv)

    def _wait(self, P, row: dict, why: str) -> None:
        if (row.get("data") or {}).get("merge_wait") != why:
            self._set(P, row, merge_wait=why[:300])
            self.store.event(P.id, "", "track_merge_waiting", detail={"track": row["name"],
                                                                      "why": why[:300]})

    def _main_dirty(self, P) -> list[str]:
        """Tracked files with uncommitted changes in the main checkout. Untracked files
        don't block a fast-forward (git refuses only one that would be overwritten, and
        _ff then waits): AU keeps permanent untracked files (.kilo/TRIAL.md, plans,
        local rules), and counting them parked GF-NAME1's merge forever (2026-10-27)."""
        try:
            return sorted({p for xy, p in P.ws.status() if xy != "??"})
        except Exception:                                         # noqa: BLE001
            return []

    # ------------------------------------------------------------------ rebase

    def _rebase(self, P, row: dict) -> None:
        wt = row["worktree"]
        base = P.profile.branch
        if not os.path.isdir(wt):
            self.tm.fail(P, row, f"worktree {wt} is gone")
            return
        ref = f"refs/heads/{base}"
        # 0.146.2: a track that already merged master (to pick up the main sprint's
        # work) must not be rebased: a rebase drops its merge commits and replays the
        # track's commits over master, conflicting by construction (GF-NAME1, Lead
        # case rc-bb1526ee). Master already in the track: nothing to integrate. The
        # track has merge commits: integrate master by merge. Else rebase as before.
        how = "rebase"
        if _git(wt, "merge-base", "--is-ancestor", ref, "HEAD", check=False).returncode == 0:
            how = "none"
        elif _git(wt, "rev-list", "--merges", "-n", "1", f"{ref}..HEAD",
                  check=False).stdout.strip():
            how = "merge"
        if how != "none":
            cmd = ["rebase", ref] if how == "rebase" else ["merge", "--no-edit", ref]
            r = _git(wt, *cmd, check=False)
            if r.returncode != 0:
                conflicts = [l.strip() for l in _git(wt, "diff", "--name-only",
                                                     "--diff-filter=U", check=False
                                                     ).stdout.splitlines() if l.strip()]
                _git(wt, how, "--abort", check=False)
                self._conflict(P, row, conflicts, (r.stderr or r.stdout)[-800:])
                return
        head = _git(wt, "rev-parse", "HEAD").stdout.strip()
        self._set(P, row, merge_stage="ff", rebased_head=head)
        self.store.event(P.id, "", "track_rebased", detail={"track": row["name"], "onto": base,
                                                           "head": head[:12], "how": how})
        self._ff(P, self.store.track(P.id, row["name"]))

    def _conflict(self, P, row: dict, files: list[str], log: str) -> None:
        """A rebase conflict: a Lead repair in the worktree when the lead manager
        offers one (`open_track_repair`), then a decision whose default is to stop
        the track (applied after [automation] decision_timeout_minutes)."""
        repair = None
        lead = getattr(self.c, "lead", None)
        fn = getattr(lead, "open_track_repair", None) if lead is not None else None
        if callable(fn):
            try:
                repair = fn(P, row, files)
            except Exception as e:                                # noqa: BLE001
                repair = {"error": f"{type(e).__name__}: {e}"[:300]}
        timeout = float(self.tm.setting(P, "decision_timeout_minutes") or 0)
        now = self.clock()
        decision = {"id": "trd-" + secrets.token_hex(4), "status": "open",
                    "question": f"Track {row['name']} does not rebase onto {P.profile.branch}: "
                                f"conflicts in {', '.join(files[:6]) or 'unknown files'}. "
                                f"What now?",
                    "options": list(DECISION_OPTIONS), "default": DECISION_DEFAULT,
                    "opened_ts": now, "deadline_ts": (now + timeout * 60) if timeout else None}
        self._set(P, row, merge_stage="wait",
                  conflict={"files": files[:40], "log": log, "ts": now,
                            "lead_repair": repair or "no lead repair available"},
                  decision=decision)
        self.store.event(P.id, "", "track_conflict", detail={
            "track": row["name"], "files": files[:20], "decision": decision["id"],
            "default": DECISION_DEFAULT, "lead_repair": bool(repair and not repair.get("error"))})

    def decide(self, P, name: str, option: str, actor: str) -> dict:
        P = self.tm.main_of(P)
        row = self.store.track(P.id, name)
        if row is None:
            raise TrackError(f"no track {name}")
        d = (row["data"] or {}).get("decision") or {}
        if d.get("status") != "open":
            raise TrackError(f"track {name} has no open decision")
        option = str(option or d.get("default") or DECISION_DEFAULT)
        if option not in DECISION_OPTIONS:
            raise TrackError(f"option must be one of {', '.join(DECISION_OPTIONS)}")
        d = {**d, "status": "answered", "answer": option, "by": actor, "answered_ts": self.clock()}
        self._set(P, row, decision=d)
        self.store.event(P.id, "", "track_decision", detail={"track": name, "option": option,
                                                            "actor": actor})
        if option == DECISION_DEFAULT:
            return self.tm.stop(P, name, actor, "merge conflict")
        if option == "retry the merge":
            self._set(P, row, merge_stage="wait", conflict=None, decision=None)
            return self.store.track(P.id, name)
        self._set(P, row, merge_stage=None)
        return self.tm.fail(P, self.store.track(P.id, name), "merge declined: branch kept")

    def sweep_decisions(self, P) -> None:
        now = self.clock()
        for row in self.store.tracks(P.id):
            d = (row.get("data") or {}).get("decision") or {}
            dl = d.get("deadline_ts")
            if d.get("status") == "open" and dl and now >= float(dl):
                try:
                    self.decide(P, row["name"], d.get("default") or DECISION_DEFAULT,
                                "coordinator (decision timeout)")
                except TrackError:
                    pass

    # ------------------------------------------------------------------ fast-forward

    def _ff(self, P, row: dict) -> None:
        main = P.profile.workspace
        base = P.profile.branch
        cur = _git(main, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
        if cur != base:
            self._wait(P, row, f"main checkout is on {cur}, not {base}")
            return
        pre = P.ws.head()
        r = _git(main, "merge", "--ff-only", f"refs/heads/{row['branch']}", check=False)
        if r.returncode != 0:
            # master moved under us (a main sprint committed): rebase again
            self._set(P, row, merge_stage="wait")
            self.store.event(P.id, "", "track_ff_retry", detail={
                "track": row["name"], "error": (r.stderr or r.stdout)[-300:]})
            return
        post = P.ws.head()
        try:
            files = P.ws.changed_files(pre, post)
            shas = [sha for sha, _ in P.ws.commits(pre, post)]
        except Exception:                                         # noqa: BLE001
            files, shas = [], []
        self._exempt_for_main(P, shas)
        self._set(P, row, merge_stage="gate", merge_base=pre, merge_head=post,
                  merged_files=files[:400], merged_commits=shas[:200])
        self.store.event(P.id, "", "track_fast_forwarded", detail={
            "track": row["name"], "from": pre[:12], "to": post[:12], "files": len(files),
            "commits": len(shas)})
        self._gate(P, self.store.track(P.id, row["name"]))

    def _exempt_for_main(self, P, shas: list[str]) -> None:
        """B1: the merge commits count as the coordinator's for every open main
        task, so its scope checks never charge the merged files to the agent."""
        from .. import bookkeeping as BK
        if not shas:
            return
        for t in self.tm.main_tasks(P):
            if t["state"] in S.TERMINAL:
                continue
            kept = list((t.get("data") or {}).get(BK.KEY) or []) + shas
            self.store.merge_data(P.id, t["task_id"], **{BK.KEY: kept[-BK.MAX_KEPT:]})

    # ------------------------------------------------------------------ the gate

    def _spec(self, P, row: dict):
        last = row["sprints"][-1]
        return P.adapter.describe(last)

    def _studio_gate(self, P, row: dict) -> bool:
        """Does the merge result need the studio_gate runner (Studio)?"""
        needs = row.get("needs") or []
        if "studio" not in needs:
            return False
        try:
            from ..fast_lane import settings
            return bool(str(settings(P).get("gate_module") or "").strip()) and \
                any(self.c.fast_lane.on(P, s) for s in row["sprints"])
        except Exception:                                         # noqa: BLE001
            return False

    def _gate(self, P, row: dict) -> None:
        data = row.get("data") or {}
        head = data.get("merge_head") or P.ws.head()
        base = data.get("merge_base") or head
        files = [f for f in (data.get("merged_files") or [])
                 if not f.replace("\\", "/").startswith("docs/")]
        try:
            spec = self._spec(P, row)
        except Exception as e:                                    # noqa: BLE001
            self.tm.fail(P, row, f"merge gate: no spec for {row['sprints'][-1]}: {e}")
            return
        if self._carried(P, row, spec, head):
            self._set(P, row, merge_stage="done", gate={"result": PASS, "carried": True})
            self.store.event(P.id, "", "track_gate_carried", detail={"track": row["name"],
                                                                     "head": head[:12]})
            self._finish(P, self.store.track(P.id, row["name"]))
            return
        if not files:
            self._set(P, row, merge_stage="done", gate={"result": PASS, "note": "no code files"})
            self._finish(P, self.store.track(P.id, row["name"]))
            return
        if self._studio_gate(P, row):
            ok, why = self.tm.resources.admit_studio(P, row["name"], "merge gate")
            if not ok:
                self._wait(P, row, f"Studio: {why}")
                return
            run_id = f"{row['name']}-{head[:12]}-{secrets.token_hex(6)}"
            fl = self.c.fast_lane
            cmd = fl._command(P, spec.task_id, files, run_id)
            from ..adapters.base import evidence_dir
            log = os.path.join(evidence_dir(self.c.state_dir, P.id, f"track-{row['name']}", head),
                               "merge_gate.log")
            try:
                proc = fl._spawn(cmd, log, run_id)
            except OSError as e:
                self.tm.resources.release_studio(P, row["name"], "gate could not start")
                self._tooling_fault(P, row, f"merge gate could not start: {e}")
                return
            self._set(P, row, merge_stage="gate_running", merge_wait=None,
                      gate={"head": head, "started": self.clock(), "wall": time.time(),
                            "pid": proc.pid, "log": log, "run_id": run_id, "files": files[:40],
                            "sprint": spec.task_id, "studio": True})
            self.store.event(P.id, "", "track_gate_started", detail={
                "track": row["name"], "head": head[:12], "pid": proc.pid, "files": files[:40]})
            return
        # no Studio: the adapter's checks on the merged head, in the background
        # (E, 2026-10-28: bg_checks kind "merge"; the tick goes on, main sessions wait)
        st, val = self._bg_verify(P, row, spec, head, base)
        if st != "done":
            return
        if isinstance(val, BaseException):
            self._tooling_fault(P, self.store.track(P.id, row["name"]) or row,
                                f"merge gate error: {type(val).__name__}: {val}")
            return
        results = list(val or [])
        row = self.store.track(P.id, row["name"]) or row
        vid = self.store.save_verification(P.id, f"track:{row['name']}", head, "merge", results)
        bad = [r for r in results if r.required and r.result in (FAIL, UNKNOWN)]
        rec = {"head": head, "result": FAIL if bad else PASS, "verification_id": vid,
               "checks": {r.check: r.result for r in results}, "studio": False,
               "findings": failed_findings([r for r in bad if r.result == FAIL])[:20]}
        if bad and all(r.result == UNKNOWN for r in bad):
            self._set(P, row, gate=rec)
            self._tooling_fault(P, self.store.track(P.id, row["name"]) or row,
                                "merge gate checks could not run: " + "; ".join(
                                    f"{r.check} ({(r.detail or '').splitlines()[0][:120]})"
                                    if r.detail else r.check for r in bad[:3]))
            return
        if bad:
            self._set(P, row, gate=rec, merge_stage=None)
            self.tm.fail(P, self.store.track(P.id, row["name"]),
                         "merge gate red: " + "; ".join(rec["findings"][:3] or [
                             f"{r.check} {r.result}" for r in bad[:3]]))
            return
        plan = self._batch_plan(P, row)
        if plan:
            self._set(P, row, merge_stage="studio_batch", gate=rec, gate_retries=0,
                      gate_retry_at=None,
                      studio_batch={"sprints": plan, "results": [], "current": None,
                                    "leased": False, "head": head})
            self.store.event(P.id, "", "track_studio_batch_planned", detail={
                "track": row["name"], "sprints": [x["sprint"] for x in plan]})
            self._batch_step(P, self.store.track(P.id, row["name"]))
            return
        self._set(P, row, merge_stage="done", gate=rec)
        self._finish(P, self.store.track(P.id, row["name"]))

    def _carried(self, P, row: dict, spec, head: str) -> bool:
        """The last sprint's own green gate still describes the merged head (master
        did not move; only evidence / coordinator commits since): no second run."""
        fl = self.c.fast_lane
        try:
            rec = fl._fl(P, spec.task_id)
        except Exception:                                         # noqa: BLE001
            return False
        run = (rec or {}).get("run") or {}
        gate_head = str(run.get("head") or "")
        if not gate_head or not run.get("run_id"):
            return False
        try:
            from ..fast_lane import _read_json, _verdict_copy, effective_result
            v = fl._check_verdict(spec.task_id, run, _read_json(_verdict_copy(run)))
            if v is None or effective_result(v) != PASS:
                return False
            return bool(fl._carries(P, spec, gate_head, head, v))
        except Exception:                                         # noqa: BLE001
            return False

    def _poll_gate(self, P, row: dict) -> None:
        run = dict((row.get("data") or {}).get("gate") or {})
        fl = self.c.fast_lane
        from ..fast_lane import _read_json, verdict_rel
        path = os.path.join(P.profile.workspace, *verdict_rel(run["sprint"], run["head"]).split("/"))
        v = fl._check_verdict(run["sprint"], run, _read_json(path))
        if v is None:
            alive = run.get("pid") and self._pid_alive(int(run["pid"]))
            over = self.clock() - float(run.get("started") or 0) > \
                self.tm.resources.max_minutes(P) * 60
            if alive:
                if over and not run.get("over_noted"):
                    # never interrupted mid-run; noted, and the lease shows as over
                    self._set(P, row, gate={**run, "over_noted": True})
                    self.store.event(P.id, "", "track_gate_over_lease", detail={
                        "track": row["name"], "minutes": self.tm.resources.max_minutes(P)})
                return
            self.tm.resources.release_studio(P, row["name"], "runner ended without a verdict")
            self._set(P, row, gate={**run, "result": "ERROR"})
            self._tooling_fault(P, self.store.track(P.id, row["name"]) or row,
                                "merge gate: the runner ended without a verdict", run)
            return
        self.tm.resources.release_studio(P, row["name"], f"gate {v.get('result')}")
        from ..fast_lane import effective_result, failed_check_lines
        res = effective_result(v)
        if str(v.get("result") or "").upper() in ("ERROR", "UNKNOWN") or res == "ERROR":
            self._set(P, row, gate={**run, "result": "ERROR", "finished": self.clock(),
                                    "findings": failed_check_lines(v)[:20]})
            self._tooling_fault(P, self.store.track(P.id, row["name"]) or row,
                                f"merge gate: the runner reported {v.get('result')}: "
                                + "; ".join(failed_check_lines(v)[:2]), run)
            return
        rec = {**run, "result": res, "finished": self.clock(),
               "findings": failed_check_lines(v)[:20] if res != PASS else []}
        if res != PASS:
            self._set(P, row, gate=rec, merge_stage=None)
            self.tm.fail(P, self.store.track(P.id, row["name"]),
                         "merge gate red: " + "; ".join(rec["findings"][:3]))
            return
        self._set(P, row, gate=rec, merge_stage="done")
        self.store.event(P.id, "", "track_gate_green", detail={"track": row["name"],
                                                               "head": run["head"][:12]})
        self._finish(P, self.store.track(P.id, row["name"]))

    # ------------------------------------------------------------------ the Studio batch

    def _batch_on(self, P) -> bool:
        try:
            return bool(self.tm.setting(P, "merge_studio_batch"))
        except (KeyError, TypeError):
            return True

    def _batch_plan(self, P, row: dict) -> list:
        """The track's sprints [studio_calls] covers, in track order, each with the
        scripted request it runs with (studio_calls.default_request: its last host-half
        parameters, else its own suites). A covered sprint with neither (a Blender sprint
        built Studio-free) gets the full pinned regression: boot, verifyLive and the
        pinned rows on the merged assets."""
        if not self._batch_on(P):
            return []
        fn = getattr(P.profile, "studio_calls_task", None)
        sc = getattr(self.c, "studio_calls", None)
        if not callable(fn) or sc is None:
            return []
        from ..studio_calls import DEFAULT_FILES, PATH_RE
        files = [f.replace("\\", "/") for f in (row.get("data") or {}).get("merged_files") or []]
        files = [f for f in files if not f.startswith("docs/") and PATH_RE.fullmatch(f)][:200]
        plan = []
        for sid in row["sprints"]:
            try:
                spec = P.adapter.describe(sid)
            except Exception:                                     # noqa: BLE001
                spec = None
            tier = sc._tier(P, sid, spec)
            if not fn(sid, tier):
                continue
            req = None
            if spec is not None:
                try:
                    req = sc.default_request(P, sid, spec)
                except Exception:                                 # noqa: BLE001
                    req = None
            if req is None:
                req = {"suites": [], "regression": "full", "tier": tier, "rerun": "failed",
                       "files": []}
            req = {**req, "files": list(req.get("files") or []) or files or list(DEFAULT_FILES)}
            plan.append({"sprint": sid, "tier": tier, "req": req})
        return plan

    def _batch_minutes(self, P, n: int) -> float:
        try:
            m = float(self.tm.setting(P, "merge_studio_batch_max_minutes") or 0)
        except (KeyError, TypeError, ValueError):
            m = 0.0
        return m if m > 0 else self.tm.resources.max_minutes(P) * max(1, n)

    def _batch_set(self, P, row: dict, batch: dict, **kv) -> dict:
        return self._set(P, row, studio_batch=batch, **kv)

    def _batch_step(self, P, row: dict) -> None:
        """One step of the merge Studio batch: take (or keep) the Studio lease, start the
        next sprint's scripted run, poll the running one, record its outcome; when every
        sprint ran, the merge completes or the track fails naming the red sprints."""
        name = row["name"]
        batch = dict((row.get("data") or {}).get("studio_batch") or {})
        plan = list(batch.get("sprints") or [])
        done = {r["sprint"] for r in batch.get("results") or []}
        todo = [x for x in plan if x["sprint"] not in done]
        if not todo:
            self._batch_end(P, row, batch)
            return
        sc = self.c.studio_calls
        ok, why = self.tm.resources.admit_studio(P, name, "merge Studio batch",
                                                 minutes=self._batch_minutes(P, len(plan)))
        if not ok:
            self._wait(P, row, f"Studio: {why}")
            return
        if not batch.get("leased"):
            batch.update(leased=True, lease_started=self.clock())
            self._batch_set(P, row, batch, merge_wait=None)
            self.store.event(P.id, "", "track_studio_batch_started", detail={
                "track": name, "sprints": [x["sprint"] for x in todo]})
        item = todo[0]
        sid = item["sprint"]
        t = self.store.task(P.id, sid)
        if t is None:
            batch["results"] = list(batch.get("results") or []) + [
                {"sprint": sid, "result": "SKIPPED",
                 "why": "not a coordinator task"}]
            self._batch_set(P, row, batch)
            self._batch_step(P, self.store.track(P.id, name))
            return
        cur = dict(batch.get("current") or {})
        d = sc._d(P, sid)
        outcome = None
        if cur.get("sprint") != sid:
            if d.get("run") or d.get("pending"):
                self._wait(P, row, f"merge Studio batch: {sid} already has a scripted run")
                return
            cur = {"sprint": sid, "since": self.clock(),
                   "before": (d.get("last") or {}).get("run_id") or ""}
            d["pending"] = {"req": dict(item["req"]), "session_id": "", "track": name,
                            "ts": time.time()}
            sc._save(P, sid, d)
            batch["current"] = cur
            self._batch_set(P, row, batch)
            self.store.event(P.id, sid, "track_studio_batch_run", detail={
                "track": name, "sprint": sid, "request": item["req"]})
            sc._start(P, self.store.task(P.id, sid) or t, d["pending"])
            d = sc._d(P, sid)
            if d.get("run"):
                cur["run_id"] = d["run"].get("run_id")
                batch["current"] = cur
                self._batch_set(P, row, batch, merge_wait=None)
                return
            last = d.get("last") or {}
            if d.get("pending") or not last or last.get("run_id") == cur["before"]:
                why = (self.store.task(P.id, sid) or {}).get("next_action") or "Studio"
                self._wait(P, row, f"merge Studio batch: {sid} waits to start ({why})")
                return
            outcome = last                                # it could not start (ERROR)
        elif d.get("run") or d.get("pending"):
            outcome = sc.tick(P, self.store.task(P.id, sid) or t, None)
            if outcome is None:
                run = sc._d(P, sid).get("run") or {}
                if run.get("run_id") and cur.get("run_id") != run["run_id"]:
                    cur["run_id"] = run["run_id"]
                    batch["current"] = cur
                    self._batch_set(P, row, batch)
                self._batch_over_lease(P, row, batch)
                return
        else:
            last = d.get("last") or {}
            if last.get("run_id") and last.get("run_id") != cur.get("before"):
                outcome = last                            # consumed by another tick
            else:
                batch["current"] = None                   # lost: start it again
                self._batch_set(P, row, batch)
                return
        self._batch_record(P, self.store.track(P.id, name) or row, item, outcome or {})

    def _batch_over_lease(self, P, row: dict, batch: dict) -> None:
        """Never interrupted mid-run; an overrun of the lease bound is noted once."""
        if self.tm.resources.lease_over(P) and not batch.get("over_noted"):
            batch["over_noted"] = True
            self._batch_set(P, row, batch)
            self.store.event(P.id, "", "track_gate_over_lease", detail={
                "track": row["name"], "batch": True,
                "minutes": (self.tm.resources.lease(P) or {}).get("minutes")})

    def _batch_record(self, P, row: dict, item: dict, outcome: dict) -> None:
        name, sid = row["name"], item["sprint"]
        batch = dict((row.get("data") or {}).get("studio_batch") or {})
        res = str(outcome.get("result") or "ERROR").upper()
        if res not in ("PASS", "FAIL"):
            # a tooling fault (no verdict): retry this sprint later, the ones that passed
            # stay passed; after GATE_TOOLING_RETRIES the track fails as a tooling fault
            batch.update(current=None, leased=False)
            self._batch_set(P, row, batch)
            self.tm.resources.release_studio(P, name, "Studio batch tooling fault")
            self._tooling_fault(P, self.store.track(P.id, name) or row,
                                f"merge Studio batch: {sid} tooling error: "
                                f"{str(outcome.get('error') or 'no verdict')[:300]}",
                                stage="studio_batch")
            return
        failing = [f"row {f.get('row')} {f.get('verdict')}" for f in outcome.get("failures") or []]
        if res == "FAIL" and not failing:
            failing = ["the host half (tools/gate.ps1) did not pass"
                       if outcome.get("stage") == "host" else
                       (outcome.get("finish_line") or "gate FAIL")]
        rec = {"sprint": sid, "result": res, "run_id": outcome.get("run_id"),
               "head": outcome.get("head"), "counts": outcome.get("counts") or {},
               "failures": failing[:20], "stage": outcome.get("stage"),
               "finish_line": outcome.get("finish_line") or "", "commit": outcome.get("commit")}
        batch["results"] = list(batch.get("results") or []) + [rec]
        batch["current"] = None
        self._batch_set(P, row, batch)
        self.store.event(P.id, sid, "track_studio_batch_result", detail={
            "track": name, **{k: v for k, v in rec.items() if k != "counts"}})
        self._batch_step(P, self.store.track(P.id, name))

    def _batch_end(self, P, row: dict, batch: dict) -> None:
        name = row["name"]
        results = list(batch.get("results") or [])
        red = [r for r in results if r.get("result") not in ("PASS", "SKIPPED")]
        self.tm.resources.release_studio(P, name, "Studio batch " + ("red" if red else "green"))
        batch.update(leased=False, current=None, finished=self.clock())
        gate = dict((row.get("data") or {}).get("gate") or {})
        gate.update(studio=True, studio_batch=[{k: r.get(k) for k in
                                                ("sprint", "result", "run_id", "failures")}
                                               for r in results])
        if red:
            findings = [f"{r['sprint']} {r['result']}: " + "; ".join(r.get("failures") or [])
                        for r in red]
            gate.update(result=FAIL, findings=findings[:20])
            self._set(P, row, studio_batch=batch, gate=gate, merge_stage=None)
            self.store.event(P.id, "", "track_studio_batch_red", detail={
                "track": name, "sprints": [r["sprint"] for r in red], "findings": findings[:10]})
            self.tm.fail(P, self.store.track(P.id, name),
                         "merge Studio batch red: " + " | ".join(findings[:4]))
            return
        gate.update(result=PASS)
        self._set(P, row, studio_batch=batch, gate=gate, merge_stage="done")
        self.store.event(P.id, "", "track_studio_batch_green", detail={
            "track": name, "sprints": [r["sprint"] for r in results]})
        self._finish(P, self.store.track(P.id, name))

    # ------------------------------------------------------------------ background / retries

    def _bg_verify(self, P, row: dict, spec, head: str, base: str) -> tuple:
        """The adapter's verify of the merged head as a bg_checks job (kind "merge"):
        ("done", rows | exception) once finished, else (state, value) while it runs or
        waits for a slot; a note (merge_wait) says so and `merge_bg` holds main
        sessions (checks_busy)."""
        try:
            cap = int(self.tm.setting(P, "max_parallel_checks") or 2)
            wait = float(self.tm.setting(P, "inline_check_wait_s") or 0)
        except (KeyError, TypeError, ValueError):
            cap, wait = 2, 2.0
        name = row["name"]
        key = (P.id, f"track:{name}", "merge", head)
        if not (row.get("data") or {}).get("merge_bg"):
            self._set(P, row, merge_bg={"head": head, "since": self.clock()})
        st, val = self.c.bg_checks.run(
            key, lambda: list(P.adapter.verify(spec, "verify", head, base)), wait, cap)
        if st == "done":
            self._set(P, row, merge_bg=None, merge_wait=None)
            return st, val
        self._wait(P, self.store.track(P.id, name) or row,
                   f"merge gate checks {'queued' if st == 'queued' else 'running'} in the "
                   f"background at {head[:12]}")
        return st, val

    def checks_busy(self, P) -> str:
        """The track whose merge gate checks run in P's main checkout ('' = none)."""
        try:
            P = self.tm.main_of(P)
            for r in self.store.tracks(P.id):
                d = r.get("data") or {}
                if r["state"] == "merging" and d.get("merge_bg") and \
                        d.get("merge_stage") == "gate":
                    return r["name"]
                if r["state"] == "merging" and d.get("merge_stage") == "studio_batch" and \
                        (d.get("studio_batch") or {}).get("leased"):
                    return r["name"]          # its scripted Studio runs need HEAD to stay put
        except Exception:                                         # noqa: BLE001
            return ""
        return ""

    def keeps_check(self, key: tuple) -> bool:
        """bg_checks sweep: a "merge" job lives while its track still merges at the gate."""
        tid = str(key[1])
        if not tid.startswith("track:"):
            return False
        r = self.store.track(key[0], tid[len("track:"):])
        return bool(r and r["state"] == "merging"
                    and (r.get("data") or {}).get("merge_stage") == "gate")

    def _tooling_fault(self, P, row: dict, why: str, run: Optional[dict] = None,
                       stage: str = "gate") -> None:
        """D: a merge gate that failed on tooling, not on the code: clear a stale runner
        lock and retry the gate (GATE_TOOLING_RETRIES times, backoff doubling) before
        the track fails."""
        data = row.get("data") or {}
        n = int(data.get("gate_retries") or 0)
        if n >= GATE_TOOLING_RETRIES:
            self._set(P, row, merge_stage=None, merge_bg=None, gate_retry_at=None)
            self.tm.fail(P, self.store.track(P.id, row["name"]) or row,
                         f"{why} (after {n} retr{'y' if n == 1 else 'ies'})")
            return
        cleared = self._clear_runner_lock(P, run)
        delay = GATE_RETRY_BACKOFF_S * (2 ** n)
        self._set(P, row, merge_stage=stage, merge_bg=None, gate_retries=n + 1,
                  gate_retry_at=self.clock() + delay, gate_last_fault=why[:400])
        self.store.event(P.id, "", "track_gate_retry", detail={
            "track": row["name"], "retry": n + 1, "of": GATE_TOOLING_RETRIES,
            "in_s": delay, "why": why[:400], "runner_lock_cleared": cleared})

    def _clear_runner_lock(self, P, run: Optional[dict]) -> bool:
        """Remove the studio_gate runner lock (<main>/.plan/tmp/gate/runner.lock) when
        it names the ended run's pid or a dead process. True when removed."""
        import json
        from ..fast_lane import GATE_DIR
        p = os.path.join(P.profile.workspace, *GATE_DIR.split("/"), "runner.lock")
        try:
            with open(p, encoding="utf-8") as f:
                cur = json.load(f)
        except (OSError, ValueError):
            return False
        pid = cur.get("pid") if isinstance(cur, dict) else None
        ours = bool(run and run.get("pid") and pid == run.get("pid"))
        if not ours and pid and self._pid_alive(int(pid)):
            return False
        try:
            os.remove(p)
            return True
        except OSError:
            return False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            from ..autoheal import pid_alive
            return bool(pid_alive(pid))
        except Exception:                                         # noqa: BLE001
            return False

    # ------------------------------------------------------------------ done

    def _finish(self, P, row: dict) -> None:
        data = row.get("data") or {}
        head = data.get("merge_head") or P.ws.head()
        self.tm.resources.release_studio(P, row["name"], "merged")
        data = {k: v for k, v in data.items() if k not in ("merge_bg", "gate_retry_at")}
        self.tm.resources.release_for_track(P, row["name"])
        removed = self.tm.remove_worktree(P, row, "merged")
        self.store.update_track(P.id, row["name"], state="merged", merged_sha=head,
                                data={**data, "merge_stage": "merged", "merge_wait": None,
                                      "worktree_removed": removed, "merged_ts": self.clock()})
        self.store.event(P.id, "", "track_merged", detail={
            "track": row["name"], "head": head[:12], "base": str(data.get("merge_base") or "")[:12],
            "files": len(data.get("merged_files") or []), "sprints": row["sprints"],
            "gate": (data.get("gate") or {}).get("result"), "worktree_removed": removed})
