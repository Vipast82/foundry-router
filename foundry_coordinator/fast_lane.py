"""Fast-lane sprints (UI-T0, plan AU_UI_REWORK_PLAN.md sections 6.5, 7.2-7.4).

A task named in the profile's [fast_lane] table runs as ONE long primary
session. Its verify step is the project's studio_gate runner, not the
adapter's checks:

  VERIFYING   the coordinator's generic git checks (scope, commits, clean,
              history) first; then the runner's `sprint` mode as a background
              subprocess. Every tick only reads `status.json` / the verdict
              file: a tick never waits for Studio.
  PASS        the gate's captures must be committed (the same session is asked
              to commit exactly them; a verdict carries over a commit that
              changed only evidence/closeout files), then the revision-bound
              vision approval (visual_approval.py's verdict rules, [approval]
              visual_model), then the normal review -> close path.
  FAIL        the failed checks go into the SAME primary session (one journaled
              prompt), back to IMPLEMENTING: no new session, no recovery case,
              no attempt charged. Red gates are counted in the task data; at
              max_red_gates_before_handoff on a LOCAL-primary task the frontier
              primary is requested automatically (frontier.auto_request, which
              keeps max_phases_per_task / cooldown / independence). At twice that
              many the task stops BLOCKED (lead manager, else the operator).
  CHANGES     vision findings go into the same session; after max_vision_rounds
              fix rounds the next CHANGES verdict is accepted with its notes
              recorded as follow-ups.
  ERROR       (runner tooling error, exit without a verdict, unreadable frames,
              repeated inconclusive vision) BLOCKED with the reason: never a pass.

Fail closed throughout: a verdict counts only when it is for this sprint and
revision, was written after the coordinator started that run (the primary is
idle while VERIFYING, so nothing else can write it), and says PASS with every
required check PASS. The gate verdict is recorded in the evidence ledger as a
`fast_lane.gate` record bound to the revision the gate ran at (with
[lifecycle] evidence on), and as the `fast_lane.gate` verification row the
adapter's fast-lane obligations require.

When a same-session prompt is impossible (the session is gone, the prompt
failed, or the session came back with no change after the same finding) the
task falls back to the ordinary CHANGES_REQUIRED path.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from typing import Optional

from . import states as S
from .adapters.base import FAIL, PASS, UNKNOWN, VerificationResult, evidence_dir
from .adapters.foundry import FoundryDenied, FoundryError
from .adapters.project import failed_findings
from .profile import FAST_LANE_DEFAULTS, matches_any

KEY = "fast_lane"                    # task data key
GATE_DIR = ".plan/tmp/gate"          # <workspace>/.plan/tmp/gate/<SPRINT>/ (git-ignored)
SLOP_S = 5.0                         # clock slop when matching a verdict to its run
ROUTER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RUBRIC = """Rubric (all must hold; plan section 6.5):
1. Layout matches the reference zones per device, with no missing or extra persistent elements.
2. Text is readable and fits: nothing clipped, overlapped or off-screen (cross-check the checker lines).
3. Tokens are applied consistently (colours, type scale, spacing, corners); rarity and state are
   legible without relying on colour alone.
4. Phone usability: targets look large enough and sit away from the thumbstick and native
   controls; nothing tappable in the portrait top 25 % except map, currency and Menu.
5. Controller: the focus ring is visible on the focused element, the prompt footer is present,
   and tabs show LB/RB.
6. Every required state renders (loading, empty, normal, pending, error/rejected, disabled with
   reason, downed where relevant).
7. Overall it reads as close to production quality: no placeholder look, no debug text, nothing
   broken or embarrassing.
Findings must be numbered and located (story, size, element) so the primary can fix them."""


def _verdict_copy(run: dict) -> str:
    """The coordinator's copy of a consumed verdict, next to the run's log."""
    log, rid = str(run.get("log") or ""), str(run.get("run_id") or "")
    if not log or not rid or not re.fullmatch(r"[\w.-]+", rid):
        return ""
    return os.path.join(os.path.dirname(log), f"verdict-{rid}.json")


def settings(P) -> dict:
    fl = getattr(P.profile, "fast_lane", None)
    return dict(fl) if isinstance(fl, dict) else dict(FAST_LANE_DEFAULTS)


def is_task(P, task_id: str) -> bool:
    fn = getattr(P.profile, "fast_lane_task", None)
    try:
        return bool(fn(task_id)) if callable(fn) else False
    except Exception:                                             # noqa: BLE001
        return False


def is_local_primary(P, task_id: str) -> bool:
    """The task's implementer is a local model: [training] local_personas when
    the profile lists them, else any persona other than the frontier one."""
    try:
        persona = P.profile.persona("implementer", task_id)
    except Exception:                                             # noqa: BLE001
        return False
    raw = getattr(P.profile, "raw", None) or {}
    local = list((raw.get("training") or {}).get("local_personas") or [])
    if local:
        return persona in local
    fp = str((raw.get("frontier_primary") or {}).get("persona") or "")
    return bool(fp) and persona != fp


def verdict_rel(task_id: str, head: str) -> str:
    """The SPRINT-mode verdict: the runner names verdict files by mode, so an
    implementer's own `stories` / `regression` run can't overwrite or satisfy it."""
    return f"{GATE_DIR}/{task_id}/{head[:12]}.sprint.json"


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def effective_result(v: dict) -> str:
    """PASS only when the runner says PASS AND every required check is PASS
    (fail closed); ERROR stays ERROR; anything else is FAIL."""
    res = str(v.get("result") or "").upper()
    if res == "ERROR":
        return "ERROR"
    checks = [c for c in v.get("checks") or [] if isinstance(c, dict)]
    if res == "PASS" and all(str(c.get("result") or "").upper() == "PASS"
                             for c in checks if c.get("required", True)):
        return PASS
    return FAIL


def failed_check_lines(v: dict, cap: int = 30) -> list[str]:
    out = []
    for c in v.get("checks") or []:
        if not isinstance(c, dict) or str(c.get("result") or "").upper() == "PASS":
            continue
        if not c.get("required", True) and str(c.get("result") or "").upper() == "SKIP":
            continue
        arts = ", ".join(str(a) for a in (c.get("artifacts") or [])[:3])
        out.append(f"{c.get('id')}: {c.get('result')}"
                   + (" (required)" if c.get("required", True) else " (advisory)")
                   + (f" - {str(c.get('detail') or '')[:300]}" if c.get("detail") else "")
                   + (f" [artifacts: {arts}]" if arts else ""))
    for cap_ in v.get("captures") or []:
        if isinstance(cap_, dict) and cap_.get("geometry_defects"):
            lines = [str(x) for x in (cap_.get("layout_lines") or []) if str(x).startswith("FAIL")]
            out.append(f"capture {cap_.get('story')}@{cap_.get('size')}: "
                       f"{cap_.get('geometry_defects')} geometry defect(s)"
                       + (" - " + " | ".join(lines[:3]) if lines else ""))
    if v.get("error"):
        out.append(f"runner error: {str(v['error'])[:400]}")
    return out[:cap] or [f"the studio gate reported {v.get('result')} with no failed check "
                         f"listed ({v.get('summary_line') or ''})"]


class FastLane:
    def __init__(self, coordinator, clock=time.time):
        self.c = coordinator
        self.store = coordinator.store
        self.clock = clock
        self._procs: dict[tuple, subprocess.Popen] = {}   # (project, task, head) -> runner

    # ------------------------------------------------------------------ queries

    def on(self, P, task_id: str) -> bool:
        return is_task(P, task_id)

    def _fl(self, P, tid: str) -> dict:
        t = self.store.task(P.id, tid) or {}
        return dict((t.get("data") or {}).get(KEY) or {})

    def _save(self, P, tid: str, fl: dict) -> None:
        self.store.merge_data(P.id, tid, **{KEY: fl})

    def _live(self, P, tid: str, rec: dict) -> None:
        """The structured progress of the background work running for this task
        (the studio gate or the vision batches), for the control center: neither
        is a Kilo session, so without it the page looks idle. Saved only when
        something but the timestamp changed; the page counts elapsed time itself
        from `started_ts` (wall clock). Display only: nothing reads it back."""
        fl = self._fl(P, tid)
        old = dict(fl.get("live") or {})
        old.pop("updated_ts", None)
        if old == rec:
            return
        fl["live"] = {**rec, "updated_ts": time.time()}
        self._save(P, tid, fl)

    def _gate_dir(self, P, tid: str) -> str:
        return os.path.join(P.profile.workspace, *GATE_DIR.split("/"), tid)

    # ------------------------------------------------------------------ VERIFYING

    def verifying(self, P, t: dict, spec, head: str, generic: list) -> Optional[list]:
        """One VERIFYING step of a fast-lane task. None = handled (the gate is
        running, a prompt went to the session, or the task moved); a list = the
        verification rows to add to `generic` for the normal path (save, the
        [lifecycle] submission gate, REVIEWING)."""
        tid = t["task_id"]
        fl = self._fl(P, tid)
        pend = fl.get("pending")
        if pend:
            if pend.get("head") == head:
                return self._send(P, t, spec, head, pend, resumed=True)
            fl.pop("pending", None)
            self._save(P, tid, fl)
        run = fl.get("run")
        v = None
        if run:
            v = self._verdict(P, tid, run)
            if run["head"] != head and not self._carries(P, spec, run["head"], head, v):
                # the code moved since that run: it says nothing about this revision
                self._kill(P, tid, run, "the revision moved on")
                fl.pop("run", None)
                self._save(P, tid, fl)
                run, v = None, None
        if run and v is None:
            return self._poll_run(P, t, spec, head, generic, run)
        if run and v is not None:
            return self._consume(P, t, spec, head, generic, run, v)
        bad = [r for r in generic if r.required and r.result != PASS]
        if bad:
            vid = self.store.save_verification(P.id, tid, head, "verify", generic)
            lines = failed_findings(bad)
            key = f"fast-lane:checks:{head[:12]}:" + hashlib.sha1(
                "\n".join(lines).encode()).hexdigest()[:10]
            return self._continue(P, t, spec, head, vid, key,
                                  "Coordinator checks failed before the studio gate", lines,
                                  "the coordinator's checks failed before the studio gate")
        return self._start(P, t, spec, head)

    # -- the runner --------------------------------------------------------------

    def _command(self, P, tid: str, files: list[str], run_id: str) -> list[str]:
        gm = settings(P)["gate_module"].strip()
        if gm.endswith(".py"):
            path = gm if os.path.isabs(gm) else os.path.join(ROUTER_ROOT, gm)
            cmd = [sys.executable, path]
        else:
            cmd = [sys.executable, "-m", gm]
        return cmd + ["--repo", P.profile.workspace, "--run-id", run_id, "sprint", "--sprint", tid,
                      "--files", ",".join(files)]

    def _start(self, P, t: dict, spec, head: str) -> Optional[list]:
        tid = t["task_id"]
        if self.c._held(P, t, "studio gate start"):
            return None
        # the runner drives Studio: it holds the project's exclusive resources
        # (Studio, Rojo) like a session does, never concurrently with another task
        if self.c._acquire(P, t, self.c._session_locks(P)):
            return None
        base = t["data"].get("task_base") or t["base_sha"]
        try:
            files = [f for f in P.ws.changed_files(base, head)
                     if not f.replace("\\", "/").startswith("docs/")]
        except Exception:                                         # noqa: BLE001
            files = []
        # a nonce only this run knows: the verdict must carry it (H4), so no other
        # runner's file - same head, same sprint - can ever be taken for ours
        run_id = f"{tid}-{head[:12]}-{secrets.token_hex(8)}"
        cmd = self._command(P, tid, files, run_id)
        log = os.path.join(evidence_dir(self.c.state_dir, P.id, tid, head), "fast_lane_gate.log")
        env = dict(os.environ)
        env["PYTHONPATH"] = ROUTER_ROOT + (os.pathsep + env["PYTHONPATH"]
                                           if env.get("PYTHONPATH") else "")
        env["FOUNDRY_GATE_RUN_ID"] = run_id
        env["PYTHONIOENCODING"] = "utf-8"             # stdout is a log file: never cp1252
        flags = 0
        if os.name == "nt":
            # its own process group, no console window: it outlives a tick (and a
            # coordinator restart) and is never killed by a Ctrl+C meant for us
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x08000000
        try:
            with open(log, "w", encoding="utf-8") as lf:
                lf.write("$ " + " ".join(cmd) + "\n")
                lf.flush()
                proc = subprocess.Popen(cmd, cwd=ROUTER_ROOT, stdout=lf, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, env=env, creationflags=flags)
        except OSError as e:
            return self._error(P, t, head, [], f"the studio gate could not be started: {e}")
        self._procs[(P.id, tid, head)] = proc
        fl = self._fl(P, tid)
        fl["run"] = {"head": head, "started": self.clock(), "wall": time.time(), "pid": proc.pid,
                     "log": log, "cmd": [str(x)[:200] for x in cmd[:14]], "run_id": run_id}
        fl["runs"] = int(fl.get("runs") or 0) + 1
        self._save(P, tid, fl)
        self.store.event(P.id, tid, "fast_lane_gate_started", detail={
            "head": head, "pid": proc.pid, "files": files[:40], "log": log, "run_id": run_id})
        self.c._note(P, t, f"studio gate started at {head[:12]} (fast lane)")
        return None

    def _verdict(self, P, tid: str, run: dict) -> Optional[dict]:
        """The verdict of THIS run, or None (not written yet / not ours). Fail closed:
        a sprint-mode verdict for this sprint and head that carries this run's nonce;
        a stories/regression verdict, or any run's file without our nonce, never counts."""
        for path in (os.path.join(P.profile.workspace, *verdict_rel(tid, run["head"]).split("/")),
                     _verdict_copy(run)):
            v = self._check_verdict(tid, run, _read_json(path) if path else None)
            if v is not None:
                return v
        return None

    def _check_verdict(self, tid: str, run: dict, v) -> Optional[dict]:
        if not v:
            return None
        if v.get("schema") != 1 or v.get("sprint") != tid or v.get("head") != run["head"]:
            return None
        if v.get("mode") != "sprint" or not run.get("run_id") or v.get("run_id") != run["run_id"]:
            return None
        if str(v.get("result") or "").upper() not in ("PASS", "FAIL", "ERROR"):
            return None
        try:
            stamp = float(v.get("finished") or v.get("started") or 0)
        except (TypeError, ValueError):
            return None
        if stamp + SLOP_S < float(run.get("wall") or 0):
            return None                  # an older verdict (e.g. the session's own run)
        return v

    def _carries(self, P, spec, gate_head: str, head: str, v: Optional[dict]) -> bool:
        """A gate run at `gate_head` still describes `head` when only evidence
        and closeout files changed in between (its own captures, the plan's
        closeout-exempt paths), like the old gate-binding rule (R-C)."""
        if gate_head == head:
            return True
        try:
            if not P.ws.is_ancestor(gate_head, head):
                return False
            changed = P.ws.changed_files(gate_head, head)
        except Exception:                                         # noqa: BLE001
            return False
        caps = [str(c.get("path")) for c in (v or {}).get("captures") or []
                if isinstance(c, dict) and c.get("path")]
        allowed = list(P.adapter.exempt_paths(spec)) + caps
        return all(matches_any(f, allowed) for f in changed)

    def _proc(self, P, tid: str, run: dict):
        return self._procs.get((P.id, tid, run.get("head")))

    def _kill(self, P, tid: str, run: dict, why: str) -> None:
        """Stop the runner AND its process tree (cmd.exe -> node / mcp.bat MCP children
        would otherwise be orphaned and keep the bridge port; plain TerminateProcess
        never runs the runner's cleanup), then stop a playtest it left running (best
        effort, non-blocking)."""
        proc = self._procs.pop((P.id, tid, run.get("head")), None)
        st = _read_json(os.path.join(self._gate_dir(P, tid), "status.json")) or {}
        ours = st.get("pid") == run.get("pid") and st.get("phase") != "done"
        try:
            if proc is not None:
                if proc.poll() is None:
                    self._kill_tree(proc.pid)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=10)
            elif run.get("pid") and ours:
                # after a restart only status.json can confirm the pid is our runner
                self._kill_tree(int(run["pid"]))
        except Exception as e:                                    # noqa: BLE001
            self.store.event(P.id, tid, "warning", detail={"fast_lane": f"kill: {e}"[:300]})
        if ours and st.get("playing"):
            self._stop_playtest(P, tid)
        self.store.event(P.id, tid, "fast_lane_gate_stopped", detail={
            "head": run.get("head"), "pid": run.get("pid"), "reason": why[:300],
            "playtest_stop": bool(ours and st.get("playing"))})

    @staticmethod
    def _kill_tree(pid: int) -> None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))], capture_output=True,
                           timeout=20, creationflags=0x08000000)
        else:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except OSError:
                os.kill(pid, signal.SIGTERM)

    def _stop_playtest(self, P, tid: str) -> None:
        """The killed runner was mid-playtest: `studio_gate stop-playtest` (one
        community call) in the background so Studio is back in Edit mode."""
        gm = settings(P)["gate_module"].strip()
        if gm.endswith(".py"):
            return                          # a custom script: no known cleanup command
        try:
            subprocess.Popen([sys.executable, "-m", gm, "--repo", P.profile.workspace, "stop-playtest"],
                             cwd=ROUTER_ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             creationflags=0x08000000 if os.name == "nt" else 0)
        except OSError as e:
            self.store.event(P.id, tid, "warning", detail={"fast_lane": f"stop-playtest: {e}"[:300]})

    def abort(self, P, tid: str, why: str) -> None:
        """The task was cancelled: stop its runner (best effort)."""
        run = self._fl(P, tid).get("run")
        if run:
            self._kill(P, tid, run, why)

    def _log_tail(self, run: dict, n: int = 1500) -> str:
        try:
            with open(run.get("log") or "", encoding="utf-8", errors="replace") as f:
                return f.read()[-n:]
        except OSError:
            return ""

    def _poll_run(self, P, t: dict, spec, head: str, generic: list, run: dict) -> Optional[list]:
        tid = t["task_id"]
        st = _read_json(os.path.join(self._gate_dir(P, tid), "status.json")) or {}
        proc = self._proc(P, tid, run)
        if proc is not None and proc.poll() is not None:
            v = self._verdict(P, tid, run)           # written just before it exited
            if v is not None:
                return self._consume(P, t, spec, head, generic, run, v)
            self._procs.pop((P.id, tid, run["head"]), None)
            return self._error(P, t, head, generic,
                               f"the studio gate exited with code {proc.returncode} without a "
                               f"verdict for {run['head'][:12]}"
                               + (" (2 = not ready / tooling error)" if proc.returncode == 2
                                  else "") + f"; log {run.get('log')}: "
                               + self._log_tail(run, 600).strip()[-600:])
        if proc is None and st.get("pid") == run.get("pid") and st.get("phase") == "done":
            # (after a restart) the runner says it finished, but no verdict of ours
            return self._error(P, t, head, generic,
                               f"the studio gate finished (status.json) without a verdict for "
                               f"{run['head'][:12]}; log {run.get('log')}")
        limit = float(settings(P)["gate_timeout_minutes"]) * 60
        elapsed = self.clock() - float(run.get("started") or self.clock())
        if elapsed > limit:
            self._kill(P, tid, run, "timeout")
            v = {"schema": 1, "sprint": tid, "head": run["head"], "result": "FAIL",
                 "finished": time.time(), "timeout": True,
                 "summary_line": f"GATE {tid} FAIL (timeout)",
                 "checks": [{"id": "fast_lane.timeout", "result": "FAIL", "required": True,
                             "detail": f"the studio gate did not finish within "
                                       f"{int(limit // 60)} minutes (last status: phase "
                                       f"{st.get('phase') or 'unknown'}, progress "
                                       f"{st.get('progress') or 'unknown'}); it was stopped. "
                                       f"Find what hangs (a playtest that never stops, a modal "
                                       f"dialog in Studio, a wait without a timeout) before "
                                       f"the next gate", "artifacts": [run.get("log") or ""]}]}
            return self._consume(P, t, spec, head, generic, run, v)
        self._live(P, tid, {"kind": "gate", "head": run["head"], "run_id": run.get("run_id") or "",
                            "started_ts": float(run.get("wall") or run.get("started") or 0) or None,
                            "limit_s": int(limit), "phase": str(st.get("phase") or "starting")[:60],
                            "progress": str(st.get("progress") or "")[:40],
                            "playing": bool(st.get("playing")), "log": str(run.get("log") or "")[:400]})
        # m:ss, not floored minutes: "5/45 min" read the same for a whole minute
        # and looked frozen while the phase counter moved
        self.c._note(P, t, f"studio gate running at {run['head'][:12]}: phase "
                           f"{st.get('phase') or 'starting'} {st.get('progress') or ''}, "
                           f"{int(elapsed // 60)}:{int(elapsed % 60):02d} of {int(limit // 60)} min")
        return None

    # -- verdicts ----------------------------------------------------------------

    def _gate_rows(self, P, tid: str, run: dict, v: dict, head: str) -> list:
        res = effective_result(v)
        rel = verdict_rel(tid, run["head"])
        summary = str(v.get("summary_line") or f"GATE {tid} {v.get('result')}")
        failed = failed_check_lines(v) if res != PASS else []
        rows = [VerificationResult(
            "fast_lane.gate", PASS if res == PASS else (UNKNOWN if res == "ERROR" else FAIL),
            True, rel, "\n".join([f"{summary} at {run['head'][:12]} (mode "
                                  f"{v.get('mode') or 'sprint'})"] + failed[:12]), head)]
        for c in (v.get("checks") or [])[:80]:
            if not isinstance(c, dict) or not c.get("id"):
                continue
            r = str(c.get("result") or "").upper()
            rows.append(VerificationResult(
                f"fast_lane.check.{c['id']}", r if r in (PASS, FAIL) else UNKNOWN, False,
                ", ".join(str(a) for a in (c.get("artifacts") or [])[:3]),
                (("required; " if c.get("required", True) else "advisory; ")
                 + str(c.get("detail") or r))[:600], head))
        return rows

    def _record(self, P, t: dict, run: dict, v: dict, vkey: str) -> None:
        """The verdict as a `fast_lane.gate` evidence-ledger record bound to the
        revision the gate ran at ([lifecycle] evidence on; best effort)."""
        if not self.c._lc(P, "evidence"):
            return
        tid = t["task_id"]
        res = effective_result(v)

        def rec():
            self.c.ledger.record(
                project_id=P.id, task_id=tid, check_id="fast_lane.gate",
                idem_key=f"{P.id}|{tid}|fast_lane.gate|{vkey}", run_id=t.get("run_id") or "",
                source="coordinator", check_kind="verification", exec_status="completed",
                result=PASS if res == PASS else FAIL, tier="verified",
                source_revision=run["head"], started_ts=float(v.get("started") or 0) or None,
                ended_ts=float(v.get("finished") or 0) or None,
                provenance={"verdict": verdict_rel(tid, run["head"]), "mode": v.get("mode"),
                            "summary_line": v.get("summary_line"), "runner_result": v.get("result")},
                output=json.dumps({k: v.get(k) for k in ("result", "summary_line", "checks",
                                                          "regression", "error")})[:60000])
        self.c._lc_safe(P, tid, "fast_lane_record", rec)

    def _consume(self, P, t: dict, spec, head: str, generic: list, run: dict,
                 v: dict) -> Optional[list]:
        tid = t["task_id"]
        self._procs.pop((P.id, tid, run["head"]), None)
        vkey = f"{run['head']}:{v.get('finished')}"
        res = effective_result(v)
        fl = self._fl(P, tid)
        consumed = dict(fl.get("consumed") or {})
        first = vkey not in consumed
        if first:
            # keep our own copy: a later runner call (the session's) rewrites the workspace
            # verdict for this head with another run id, and a carried run whose verdict
            # vanished was polled as still running and timed out (UI-D1, 11:00)
            cp = _verdict_copy(run)
            if cp and not v.get("timeout"):
                try:
                    with open(cp + ".tmp", "w", encoding="utf-8") as f:
                        json.dump(v, f)
                    os.replace(cp + ".tmp", cp)
                except OSError:
                    pass
            self._record(P, t, run, v, vkey)
            consumed[vkey] = res
            fl["consumed"] = dict(list(consumed.items())[-20:])
            self._save(P, tid, fl)
            self.store.event(P.id, tid, "fast_lane_gate", detail={
                "head": run["head"], "result": res, "runner_result": v.get("result"),
                "summary": v.get("summary_line"), "verdict": verdict_rel(tid, run["head"]),
                "failed": failed_check_lines(v)[:10] if res != PASS else []})
        rows = self._gate_rows(P, tid, run, v, head)
        if res == "ERROR":
            return self._error(P, t, head, generic + rows,
                               "the studio gate reported ERROR (tooling / not ready): "
                               + "; ".join(failed_check_lines(v)[:4]))
        if res != PASS:
            return self._red(P, t, spec, head, generic + rows, v, vkey, first)
        # PASS: the gate's captures (and nothing else) may still be uncommitted
        bad = [r for r in generic if r.required and r.result != PASS]
        if bad:
            vid = self.store.save_verification(P.id, tid, head, "verify", generic + rows)
            caps = [str(c.get("path")) for c in v.get("captures") or []
                    if isinstance(c, dict) and c.get("path")]
            # the session's own stories runs re-render the task's other frames too: they are
            # runner output in the task's capture folder, so they go in the same commit
            # (UI-D1: 40 gate captures committed, 463 other renders left dirty, clean loop)
            pats = [x.replace("{task}", tid) for x in settings(P).get("runner_captures") or []]
            try:
                other = sorted(p for _, p in P.ws.status()
                               if matches_any(p, pats) and p not in set(caps))
            except Exception:                                     # noqa: BLE001
                other = []
            dirs = sorted({x.split("*")[0].rstrip("/") for x in pats if x.split("*")[0]})
            lines = failed_findings(bad) + ([
                "commit exactly the gate's capture files by explicit path (message "
                f"\"{tid}: gate captures\"), nothing else: " + ", ".join(caps[:40])]
                if caps and not other else [
                f"commit the runner's capture files in one commit (message \"{tid}: gate "
                f"captures\"), nothing else: `git add -A -- {' '.join(dirs)}` then commit. "
                f"That is the gate's {len(caps)} capture(s) plus {len(other)} other re-rendered "
                f"frame(s) in the same folder (e.g. {', '.join(other[:5])})"] if caps or other
                else [])
            return self._continue(P, t, spec, head, vid, f"fast-lane:commit:{head}",
                                  f"Studio gate PASS at {run['head'][:12]}: commit its captures",
                                  lines, "the studio gate passed; its captures must be committed")
        vision = self._vision(P, t, spec, head, run, v, generic + rows)
        if vision is None:
            return None
        self.c._release_session(P, t)
        return rows + [vision]

    def _red(self, P, t: dict, spec, head: str, results: list, v: dict, vkey: str,
             first: bool) -> Optional[list]:
        tid = t["task_id"]
        s = settings(P)
        fl = self._fl(P, tid)
        vid = self.store.save_verification(P.id, tid, head, "verify", results)
        lines = failed_check_lines(v)
        if not first:
            # the session came back without a change the gate depends on: the same
            # findings again would loop -> the ordinary repair path
            return self._fallback(P, t, vid, ["fast lane: the studio gate is still red at "
                                              f"{v.get('head', '')[:12]} after the same "
                                              "findings were sent"] + lines)
        n = int(fl.get("red_gates") or 0) + 1
        fl["red_gates"] = n
        if v.get("timeout"):
            fl.pop("run", None)          # a timed-out run is never re-read: the next gate is fresh
        fl.setdefault("red_heads", []).append(str(v.get("head") or "")[:40])
        fl["red_heads"] = fl["red_heads"][-20:]
        self._save(P, tid, fl)
        mx = int(s["max_red_gates_before_handoff"])
        self.store.event(P.id, tid, "fast_lane_red_gate", detail={
            "n": n, "head": v.get("head"), "failed": lines[:10], "timeout": bool(v.get("timeout"))})
        if n >= 2 * mx:
            self.c.store.release(P.id, tid)
            self.c._stop(P, t, S.BLOCKED, {
                "verification_id": vid,
                "blocker": f"fast lane: {n} red studio gates (limit {2 * mx}); last: "
                           + "; ".join(lines[:4])}, cls="fast_lane_red_gates",
                verification_id=vid, head_sha=head)
            return None
        reason = f"the studio gate is red ({n} red gate(s) on this sprint)"
        handoff = ""
        if n >= mx and is_local_primary(P, tid):
            ph, why = self.c.frontier.auto_request(
                P, self.store.task(P.id, tid),
                f"fast lane: {n} red studio gates on a local-primary sprint", head)
            if ph:
                handoff = (f"\nThe coordinator handed this sprint to the frontier primary "
                           f"({ph['persona']}, phase {ph['phase_id']}) after {n} red gates: "
                           f"same session, scope, rules and gate.\n")
                fl = self._fl(P, tid)
                fl["handoff"] = ph["phase_id"]
                self._save(P, tid, fl)
            else:
                self.store.event(P.id, tid, "fast_lane_handoff_unavailable",
                                 detail={"reason": why[:300], "red_gates": n})
        return self._continue(P, t, spec, head, vid, f"fast-lane:red:{vkey}",
                              f"Studio gate FAIL at {str(v.get('head') or '')[:12]} "
                              f"(red gate {n})", lines, reason, extra=handoff)

    def _error(self, P, t: dict, head: str, results: list, why: str) -> None:
        tid = t["task_id"]
        rows = list(results) + [VerificationResult("fast_lane.gate", UNKNOWN, True, "", why[:800],
                                                   head)]
        vid = self.store.save_verification(P.id, tid, head, "verify", rows)
        fl = self._fl(P, tid)
        fl.pop("run", None)              # an unblock / re-verify starts a fresh run
        self._save(P, tid, fl)
        self.store.event(P.id, tid, "fast_lane_gate_error", detail={"reason": why[:500]})
        self.c.store.release(P.id, tid)
        self.c._stop(P, self.store.task(P.id, tid), S.BLOCKED, {
            "verification_id": vid, "blocker": f"fast lane: {why}"[:1000]},
            cls="fast_lane_gate_error", verification_id=vid, head_sha=head)
        return None

    # -- same-session continuation -----------------------------------------------------

    def _continue(self, P, t: dict, spec, head: str, vid: str, key: str, title: str,
                  lines: list, reason: str, extra: str = "") -> Optional[list]:
        tid = t["task_id"]
        sid = t.get("session_id") or ""
        if sid and self.store.op(f"deliver:{P.id}:{tid}:{sid}:{key}"):
            # this exact prompt already went to this session and it came back
            # unchanged: no loop, the ordinary repair path takes over
            return self._fallback(P, t, vid, lines)
        fl = self._fl(P, tid)
        fl["pending"] = {"key": key, "head": head, "vid": vid, "title": title,
                         "lines": list(lines)[:30], "reason": reason, "extra": extra}
        self._save(P, tid, fl)
        return self._send(P, self.store.task(P.id, tid), spec, head, fl["pending"])

    def _send(self, P, t: dict, spec, head: str, pend: dict, resumed: bool = False):
        tid = t["task_id"]
        sid = t.get("session_id") or ""
        if not sid or not self.store.session(sid):
            return self._fallback(P, t, pend["vid"], pend["lines"])
        try:
            gone = P.client.state(sid, directory=P.profile.workspace,
                                  since_ts=0).status == "missing"
        except Exception:                                         # noqa: BLE001
            gone = False                 # unknown: the prompt decides
        if gone:
            return self._fallback(P, t, pend["vid"], pend["lines"])
        if self.c._held(P, t, "fast-lane continuation"):
            return None                  # kept pending for the resume
        if self.c._acquire(P, t, self.c._session_locks(P), writer=True):
            return None                  # waiting for the writer slot / Studio
        text = (P.adapter.continue_prompt(spec, pend["reason"])
                + f"\n\n## {pend['title']} (from the coordinator; fix it in this session)\n"
                + "\n".join(f"{i}. {x}" for i, x in enumerate(pend["lines"], 1)) + "\n"
                + (pend.get("extra") or "")
                + "\nFix the cause within the plan's file list, re-run the affected stories, "
                  "commit by explicit path, then report DONE: the coordinator runs the full "
                  "studio gate again on the committed code. Never weaken a check, skip a story "
                  "or edit the runner to get PASS. If a finding needs a decision the plan "
                  "doesn't settle, report BLOCKED with one question.\n")
        try:
            sent = self.c._prompt_primary(P, t, text, pend["key"])
        except Exception as e:                                    # noqa: BLE001
            self.c._release_session(P, t)
            self.store.event(P.id, tid, "warning", session_id=sid, detail={
                "fast_lane": f"continuation failed ({pend['key']}): {type(e).__name__}: {e}"[:400]})
            return self._fallback(P, t, pend["vid"], pend["lines"])
        fl = self._fl(P, tid)
        fl.pop("pending", None)
        self._save(P, tid, fl)
        self.store.event(P.id, tid, "fast_lane_continuation", session_id=sid, detail={
            "key": pend["key"], "sent": sent, "resumed": resumed, "title": pend["title"],
            "findings": pend["lines"][:10]})
        self.store.transition(P.id, tid, S.IMPLEMENTING, {
            "verification_id": pend["vid"], "session_id": sid, "continuation": pend["key"],
            "fast_lane": pend["title"]}, phase="implement", head_sha=head,
            next_action=f"session working (fast lane: {pend['title']})"[:500])
        return None

    def _fallback(self, P, t: dict, vid: str, lines: list) -> None:
        tid = t["task_id"]
        fl = self._fl(P, tid)
        fl.pop("pending", None)
        self._save(P, tid, fl)
        cur = self.store.task(P.id, tid)
        self.c._release_session(P, cur)
        findings = list(lines) or ["fast lane: the studio gate did not pass"]
        self.store.event(P.id, tid, "fast_lane_fallback", detail={"findings": findings[:10]})
        self.store.transition(P.id, tid, S.CHANGES_REQUIRED, {
            "verification_id": vid, "findings": findings}, verification_id=vid,
            data={**cur["data"], "findings": findings})
        return None

    # -- vision approval ---------------------------------------------------------------

    def _frames(self, P, caps: list) -> tuple[list, dict, str]:
        frames, arts = [], {}
        for c in caps:
            rel = str(c.get("path") or "").replace("\\", "/")
            if not rel or not P.profile.in_workspace(rel):
                return [], {}, f"capture path {rel!r} leaves the workspace"
            try:
                with open(os.path.join(P.profile.workspace, rel), "rb") as fh:
                    blob = fh.read(8_000_001)
            except OSError as e:
                return [], {}, f"cannot read capture {rel}: {e}"
            if not blob or len(blob) > 8_000_000:
                return [], {}, f"capture {rel} is empty or over the 8 MB review limit"
            mime = "image/png" if blob.startswith(b"\x89PNG\r\n\x1a\n") else \
                "image/jpeg" if blob.startswith(b"\xff\xd8\xff") else ""
            if not mime:
                return [], {}, f"capture {rel} is not a readable PNG/JPEG frame"
            frames.append((rel, mime, blob))
            arts[rel] = hashlib.sha256(blob).hexdigest()
        return frames, arts, ""

    def _vision(self, P, t: dict, spec, head: str, run: dict, v: dict,
                results: list) -> Optional[VerificationResult]:
        """The vision approval after a green gate. A VerificationResult when the
        task may go on to review; None when it waits, prompted the session, or
        stopped. Fail closed: no route, frame or valid verdict = not approved."""
        from . import visual_approval as VA
        tid = t["task_id"]
        caps = [c for c in v.get("captures") or [] if isinstance(c, dict) and c.get("path")]
        if not VA.enabled(P) or not caps:
            why = ("[approval] visual_model is off; the profile's approval rules decide the look"
                   if not VA.enabled(P) else "the gate verdict lists no captures")
            return VerificationResult("fast_lane.vision", PASS, False, "", "skipped: " + why, head)
        if P.foundry is None:
            self.c._note(P, t, "fast-lane vision approval needs Foundry ([foundry] url)")
            return None
        frames, arts, problem = self._frames(P, caps)
        if problem:
            return self._error(P, t, head, results, f"vision approval: {problem}")
        dirty = {p.replace("\\", "/") for _, p in P.ws.status()}
        if any(rel in dirty for rel in arts):
            self.c._note(P, t, "fast-lane vision approval waits for committed capture bytes")
            return None
        persona = P.profile.persona("visual_reviewer", tid)
        try:
            info = P.foundry.persona_info(persona)
        except (FoundryError, AttributeError) as e:
            self.c._note(P, t, f"fast-lane vision route unavailable: {e}"[:500])
            return None
        routes = (info or {}).get("routes") or []
        if not (info and info.get("exists", True) and info.get("enabled", True)
                and info.get("strict") is True and info.get("client_allowed") is not False
                and routes and all(r.get("vision") is True and r.get("model") for r in routes)):
            self.c._note(P, t, "fast-lane vision route is not verified vision-capable for every "
                               "possible model")
            return None
        fl = self._fl(P, tid)
        fp = hashlib.sha256(json.dumps([run["head"], arts], sort_keys=True).encode()).hexdigest()
        tries = int((fl.get("vision_uncertain") or {}).get(fp, 0))
        # Batches (UI-D1, 2026-10-20: ~560 frames in ONE call came back "uncertain" because
        # no model reliably returns a row per frame at that size). One idempotent call per
        # batch, at most one call per tick; every batch must approve; findings merge.
        size = max(1, int(settings(P).get("vision_batch_frames") or 20))
        batches = [frames[i:i + size] for i in range(0, len(frames), size)]
        results_b, served_all = [], set()
        for bi, batch in enumerate(batches):
            barts = {rel: arts[rel] for rel, _m, _b in batch}
            suffix = f":b{bi + 1}of{len(batches)}" if len(batches) > 1 else ""
            key = f"fast-lane-vision:{P.id}:{tid}:{fp}:{tries}{suffix}"
            reused = None
            for earlier in range(tries):     # a retry re-asks only the inconclusive batches
                op = self.store.op(f"fast-lane-vision:{P.id}:{tid}:{fp}:{earlier}{suffix}")
                r0 = (op or {}).get("result") or {}
                if op and op.get("status") == "done" and r0.get("verdict") in ("approve",
                                                                                "changes_required"):
                    reused = r0
            if reused is not None:
                results_b.append(reused)
                served_all.add(reused.get("served_model") or "unknown")
                continue
            # a retry re-asks an inconclusive batch in small chunks: at 20 frames a model
            # sometimes drops a frame row or mis-copies a sha256 (UI-D1: 2-4 of 26 batches per
            # round), and two inconclusive rounds stopped the task
            small = max(1, size // 4)
            chunks = [batch] if tries == 0 else [batch[i:i + small]
                                                 for i in range(0, len(batch), small)]
            parts_r = []
            for ci, chunk in enumerate(chunks):
                ckey = key if len(chunks) == 1 else f"{key}.c{ci + 1}of{len(chunks)}"
                carts = {rel: arts[rel] for rel, _m, _b in chunk}
                kept = None              # only a failing chunk is asked again on a later try
                for earlier in range(1, tries):
                    op = self.store.op(f"fast-lane-vision:{P.id}:{tid}:{fp}:{earlier}{suffix}"
                                       f".c{ci + 1}of{len(chunks)}")
                    r0 = (op or {}).get("result") or {}
                    if op and op.get("status") == "done" and \
                            r0.get("verdict") in ("approve", "changes_required"):
                        kept = r0
                if kept is not None:
                    parts_r.append(kept)
                    continue
                state, cres = self._vision_one(P, t, spec, head, run, v, chunk, carts, persona,
                                               routes, ckey, (bi + 1, len(batches)), batches,
                                               batch, frames, results_b)
                if state != "done":
                    return None          # waiting, or one call made: one call per tick
                parts_r.append(cres)
            if len(parts_r) == 1:
                res = parts_r[0]
            else:
                vs = [r.get("verdict") for r in parts_r]
                res = {"verdict": "approve" if all(x == "approve" for x in vs) else
                       "uncertain" if any(x not in ("approve", "changes_required") for x in vs)
                       else "changes_required",
                       "findings": [f for r in parts_r if r.get("verdict") != "approve"
                                    for f in (r.get("findings") or [])],
                       "served_model": ",".join(sorted({r.get("served_model") or "unknown"
                                                        for r in parts_r}))}
                if self.store.op_begin(key, P.id, tid, "visual_approval") is None or                         (self.store.op(key) or {}).get("status") != "done":
                    self.store.op_update(key, "done", res)   # later tries reuse the whole batch
            results_b.append(res)
            served_all.add(res.get("served_model") or "unknown")
        verdicts = [r.get("verdict") for r in results_b]
        findings = []
        for bi, r in enumerate(results_b):
            pre = f"[batch {bi + 1}/{len(results_b)}] " if len(results_b) > 1 else ""
            findings += [pre + str(f) for f in (r.get("findings") or [])
                         if r.get("verdict") != "approve"]
        if all(x == "approve" for x in verdicts):
            verdict = "approve"
        elif any(x == "changes_required" for x in verdicts) and \
                not any(x not in ("approve", "changes_required") for x in verdicts):
            verdict = "changes_required"
        else:
            verdict = "uncertain"
        res = {"verdict": verdict, "findings": findings[:60],
               "served_model": ",".join(sorted(served_all))}
        served = res.get("served_model") or "unknown"
        actor = f"model:{persona}/{served}"
        if verdict == "approve":
            self.store.add_approval(P.id, tid, "visual", head, arts, actor,
                                    f"fast-lane vision approval at gate {run['head'][:12]}; "
                                    f"policy visual_model v1")
            return VerificationResult("fast_lane.vision", PASS, True, "",
                                      f"APPROVED by {actor} ({len(frames)} frame(s))", head)
        if verdict == "changes_required":
            fl = self._fl(P, tid)
            rounds = int(fl.get("vision_rounds") or 0)
            mx = int(settings(P)["max_vision_rounds"])
            if rounds < mx:
                fl["vision_rounds"] = rounds + 1
                self._save(P, tid, fl)
                vid = self.store.save_verification(P.id, tid, head, "verify", results + [
                    VerificationResult("fast_lane.vision", FAIL, True, "",
                                       "CHANGES: " + " | ".join(findings[:6]), head)])
                return self._continue(P, t, spec, head, vid, f"fast-lane:vision:{fp}",
                                      f"Vision review: CHANGES (round {rounds + 1}/{mx})",
                                      findings or ["changes required"],
                                      "the vision reviewer asked for changes")
            notes = findings[:30]
            fl["vision_notes"] = (list(fl.get("vision_notes") or []) + notes)[-60:]
            self._save(P, tid, fl)
            self.store.add_approval(P.id, tid, "visual", head, arts,
                                    f"coordinator:fast_lane ({actor})",
                                    f"accepted with notes after {rounds} CHANGES round(s): "
                                    + " | ".join(notes)[:1500])
            self.store.event(P.id, tid, "fast_lane_vision_accepted_with_notes", detail={
                "rounds": rounds, "notes": notes[:20], "head": head})
            return VerificationResult("fast_lane.vision", PASS, True, "",
                                      f"accepted with notes after {rounds} CHANGES round(s) "
                                      f"(follow-ups): " + " | ".join(notes[:8]), head)
        # uncertain: ask once more, then stop for a person (never a pass)
        fl = self._fl(P, tid)
        unc = dict(fl.get("vision_uncertain") or {})
        unc[fp] = tries + 1
        fl["vision_uncertain"] = unc
        self._save(P, tid, fl)
        if tries + 1 >= 3:
            return self._error(P, t, head, results, "vision approval inconclusive 3 times: "
                               + "; ".join(findings[:3]))
        self.c._note(P, t, "fast-lane vision review inconclusive; asking again")
        return None

    def _vision_one(self, P, t, spec, head, run, v, chunk, carts, persona, routes, key,
                    label, batches, batch, frames, results_b) -> tuple:
        """One journaled vision call: ("done", result) | ("wait", None) | ("called", None)."""
        tid = t["task_id"]
        prior = self.store.op_begin(key, P.id, tid, "visual_approval")
        if prior:
            r = prior.get("result") or {}
            if prior["status"] == "done":
                return "done", r
            if prior["status"] == "deferred" and self.clock() >= float(r.get("retry_at") or 0):
                pass                     # retry the deferred call now
            elif prior["status"] == "calling":
                # a restart during the call: its outcome is unknown and never paid twice
                res = {"verdict": "uncertain", "findings": ["call outcome unknown after a "
                                                            "restart"], "served_model": ""}
                self.store.op_update(key, "done", res)
                return "done", res
            else:
                self.c._note(P, t, f"fast-lane vision approval deferred: "
                                   f"{r.get('reason') or ''}"[:500])
                return "wait", None
        prev = self._fl(P, tid).get("live") or {}
        so_far = [str(f) for r in results_b if r.get("verdict") != "approve"
                  for f in (r.get("findings") or [])]
        self._live(P, tid, {
            "kind": "vision", "head": head, "batch": label[0], "batches": label[1],
            "frames": len(batch), "total_frames": len(frames), "done": len(results_b),
            "changes": sum(1 for r in results_b if r.get("verdict") == "changes_required"),
            "findings": [f[:300] for f in so_far[:8]], "more_findings": max(0, len(so_far) - 8),
            "persona": str(persona)[:120],
            "started_ts": prev.get("started_ts") if prev.get("kind") == "vision"
            and prev.get("head") == head else time.time()})
        self.c._note(P, t, f"fast-lane vision review: batch {label[0]}/{label[1]} "
                           f"({len(chunk)} frames)")
        res = self._vision_call(P, t, spec, head, run, v, chunk, carts, persona, routes,
                                key, batch_label=label)
        if res is None:
            return "wait", None
        if label[0] < label[1] or len(chunk) < len(batch):
            return "called", None        # one call per tick; the next tick goes on
        return "done", res

    def _vision_call(self, P, t, spec, head, run, v, frames, arts, persona, routes,
                     key, batch_label: tuple = (1, 1)) -> Optional[dict]:
        from . import visual_approval as VA
        tid = t["task_id"]
        sid = f"fast-lane-vision-{key.rsplit(':', 2)[-2][:16]}-{key.rsplit(':', 1)[-1]}"
        if not self.store.session(sid):
            self.store.add_session(sid, P.id, tid, t["run_id"], "visual_reviewer",
                                   "visual_approval", t["attempt"], "foundry-direct", persona,
                                   {"parent": t.get("session_id"), "revision": head,
                                    "fast_lane": True})
        by_path = {str(c.get("path")).replace("\\", "/"): c for c in v.get("captures") or []
                   if isinstance(c, dict)}
        parts = [{"type": "text", "text": (
            f"Fast-lane vision approval for project {P.id}, sprint {tid}, revision {head} (the "
            f"studio gate passed at {run['head'][:12]}). This is batch {batch_label[0]} of "
            f"{batch_label[1]}; every batch must approve. Review ALL {len(frames)} captures in "
            "this batch against the sprint and the rubric below. Do not infer pixels from "
            "filenames or claim inspection without seeing each image. Return valid JSON with "
            'keys "verdict" (approve, changes_required or uncertain), "frames" (one object per '
            "image with path, sha256, image_seen, subject_present, validity, observation and "
            'findings), and "limitations". ' + VA.FRAME_FIELDS + " Approve only if every frame "
            f"is valid, actually seen and meets the rubric.\n\n{RUBRIC}\n\nSprint:\n{spec.spec[:8000]}")}]
        for rel, mime, blob in frames:
            c = by_path.get(rel) or {}
            layout = [str(x) for x in (c.get("layout_lines") or [])][:8]
            parts.append({"type": "text", "text": (
                f"Capture {rel} (story {c.get('story')}, size {c.get('size')}), SHA256 "
                f"{arts[rel]}; checker geometry defects: {c.get('geometry_defects', 0)}"
                + ("; checker lines: " + " | ".join(layout) if layout else ""))})
            parts.append({"type": "image_url", "image_url": {
                "url": f"data:{mime};base64,{base64.b64encode(blob).decode()}"}})
        try:
            P.foundry.bind(sid, P.id, tid, t["run_id"], "visual_reviewer", persona,
                           parent=t.get("session_id") or "", purpose="review")
            self.store.op_update(key, "calling", {"session_id": sid})
            raw, meta = P.foundry.chat(persona, [
                {"role": "system", "content": "You are a read-only, independent vision reviewer. "
                 "Judge only the actual attached frames. Never waive test gates or invent "
                 "evidence."}, {"role": "user", "content": parts}], session_id=sid,
                max_tokens=6000)
        except FoundryDenied as e:
            self.store.op_update(key, "deferred", {"retry_at": self.clock() + max(
                60, int(e.retry_after or 300)), "reason": str(e)[:300]})
            self.c._note(P, t, f"fast-lane vision approval deferred: {e}"[:500])
            return None
        except FoundryError as e:
            self.store.op_update(key, "deferred", {"retry_at": self.clock() + 300,
                                                   "reason": str(e)[:300]})
            self.c._note(P, t, f"fast-lane vision approval unavailable: {e}"[:500])
            return None
        served = str((meta or {}).get("served_model") or "")
        verdict, findings = VA._valid_verdict(VA._answer(raw), frames, arts)
        if served not in {r["model"] for r in routes}:
            verdict, findings = "uncertain", ["actual serving model was not verified as "
                                              "vision-capable"]
        res = {"session_id": sid, "verdict": verdict, "served_model": served,
               "findings": findings[:30],
               "response_sha256": hashlib.sha256(str(raw).encode()).hexdigest()}
        self.store.op_update(key, "done", res)
        self.store.update_session(sid, status="ended", outcome=verdict, ended_ts=self.clock())
        self.store.event(P.id, tid, "model_visual_review", session_id=sid, detail={
            "verdict": verdict, "persona": persona, "served_model": served, "revision": head,
            "fast_lane": True, "artifacts": arts, "findings": findings[:10]})
        return res
