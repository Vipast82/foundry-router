"""Merging a finished track back into master (Team Lead piece 2, merge part).

Only at a quiescent point of the main sprint (no live main session, no main
task between a bound head and its close), never mid-session. Stages, each
recorded in the track's data (`merge_stage`) so a restart resumes:

  wait      -> the main sprint is quiescent
  rebase    `git rebase <master>` in the worktree; a conflict is aborted and
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
                if k not in ("conflict", "decision", "gate", "failed", "merge_wait")}
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
            self._gate(P, row)
        elif stage == "gate_running":
            self._poll_gate(P, row)
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
        try:
            return sorted({p for _, p in P.ws.status()})
        except Exception:                                         # noqa: BLE001
            return []

    # ------------------------------------------------------------------ rebase

    def _rebase(self, P, row: dict) -> None:
        wt = row["worktree"]
        base = P.profile.branch
        if not os.path.isdir(wt):
            self.tm.fail(P, row, f"worktree {wt} is gone")
            return
        r = _git(wt, "rebase", f"refs/heads/{base}", check=False)
        if r.returncode != 0:
            conflicts = [l.strip() for l in _git(wt, "diff", "--name-only", "--diff-filter=U",
                                                 check=False).stdout.splitlines() if l.strip()]
            _git(wt, "rebase", "--abort", check=False)
            self._conflict(P, row, conflicts, (r.stderr or r.stdout)[-800:])
            return
        head = _git(wt, "rev-parse", "HEAD").stdout.strip()
        self._set(P, row, merge_stage="ff", rebased_head=head)
        self.store.event(P.id, "", "track_rebased", detail={"track": row["name"], "onto": base,
                                                           "head": head[:12]})
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
                self.tm.fail(P, row, f"merge gate could not start: {e}")
                return
            self._set(P, row, merge_stage="gate_running", merge_wait=None,
                      gate={"head": head, "started": self.clock(), "wall": time.time(),
                            "pid": proc.pid, "log": log, "run_id": run_id, "files": files[:40],
                            "sprint": spec.task_id, "studio": True})
            self.store.event(P.id, "", "track_gate_started", detail={
                "track": row["name"], "head": head[:12], "pid": proc.pid, "files": files[:40]})
            return
        # no Studio: the adapter's checks on the merged head, now
        try:
            results = P.adapter.verify(spec, "verify", head, base)
        except Exception as e:                                    # noqa: BLE001
            self.tm.fail(P, row, f"merge gate error: {type(e).__name__}: {e}")
            return
        vid = self.store.save_verification(P.id, f"track:{row['name']}", head, "merge", results)
        bad = [r for r in results if r.required and r.result in (FAIL, UNKNOWN)]
        rec = {"head": head, "result": FAIL if bad else PASS, "verification_id": vid,
               "checks": {r.check: r.result for r in results}, "studio": False,
               "findings": failed_findings([r for r in bad if r.result == FAIL])[:20]}
        if bad:
            self._set(P, row, gate=rec, merge_stage=None)
            self.tm.fail(P, self.store.track(P.id, row["name"]),
                         "merge gate red: " + "; ".join(rec["findings"][:3] or [
                             f"{r.check} {r.result}" for r in bad[:3]]))
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
            self._set(P, row, gate={**run, "result": "ERROR"}, merge_stage=None)
            self.tm.fail(P, self.store.track(P.id, row["name"]),
                         "merge gate: the runner ended without a verdict")
            return
        self.tm.resources.release_studio(P, row["name"], f"gate {v.get('result')}")
        from ..fast_lane import effective_result, failed_check_lines
        res = effective_result(v)
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
        self.tm.resources.release_for_track(P, row["name"])
        removed = self.tm.remove_worktree(P, row, "merged")
        self.store.update_track(P.id, row["name"], state="merged", merged_sha=head,
                                data={**data, "merge_stage": "merged", "merge_wait": None,
                                      "worktree_removed": removed, "merged_ts": self.clock()})
        self.store.event(P.id, "", "track_merged", detail={
            "track": row["name"], "head": head[:12], "base": str(data.get("merge_base") or "")[:12],
            "files": len(data.get("merged_files") or []), "sprints": row["sprints"],
            "gate": (data.get("gate") or {}).get("result"), "worktree_removed": removed})
