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
from .autoheal import pid_alive
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


def runner_patterns(P, task_id: str) -> tuple[list, list]:
    """([runner_outputs], [runner_captures]) for the task ({task} filled in): files the
    studio_gate runner writes. The coordinator commits them; they are never the agent's."""
    s = settings(P)
    return ([x.replace("{task}", task_id) for x in s.get("runner_outputs") or []],
            [x.replace("{task}", task_id) for x in s.get("runner_captures") or []])


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


STALE_CHECKS = ("host.live_hash",)


def rojo_stale_only(v: dict) -> bool:
    """True when the only required checks that did not pass are the Studio-vs-HEAD
    source hash checks (W1.8: a stale Rojo sync, tooling, never the code)."""
    bad = [c for c in (v or {}).get("checks") or []
           if isinstance(c, dict) and c.get("required", True)
           and str(c.get("result") or "").upper() not in ("PASS", "SKIP")]
    return bool(bad) and all(str(c.get("id") or "") in STALE_CHECKS for c in bad)


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

    def _runner(self, P, run_id: str) -> list[str]:
        gm = settings(P)["gate_module"].strip()
        if gm.endswith(".py"):
            path = gm if os.path.isabs(gm) else os.path.join(ROUTER_ROOT, gm)
            cmd = [sys.executable, path]
        else:
            cmd = [sys.executable, "-m", gm]
        return cmd + ["--repo", P.profile.workspace, "--run-id", run_id]

    def _command(self, P, tid: str, files: list[str], run_id: str) -> list[str]:
        return self._runner(P, run_id) + ["sprint", "--sprint", tid, "--files", ",".join(files)]

    def _spawn(self, cmd: list, log: str, run_id: str):
        """Start the runner detached (its own process group, no console window): it
        outlives a tick and a coordinator restart. Returns the Popen."""
        env = dict(os.environ)
        env["PYTHONPATH"] = ROUTER_ROOT + (os.pathsep + env["PYTHONPATH"]
                                           if env.get("PYTHONPATH") else "")
        env["FOUNDRY_GATE_RUN_ID"] = run_id
        env["PYTHONIOENCODING"] = "utf-8"             # stdout is a log file: never cp1252
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x08000000
        with open(log, "w", encoding="utf-8") as lf:
            lf.write("$ " + " ".join(cmd) + "\n")
            lf.flush()
            return subprocess.Popen(cmd, cwd=ROUTER_ROOT, stdout=lf, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env, creationflags=flags)

    # -- the automatic baseline (B2) -----------------------------------------------------

    def baseline_hold(self, P, t: dict) -> bool:
        """B2 (2026-10-21): before a fast-lane sprint's FIRST session, while the workspace
        is still at the sprint's base commit, the coordinator runs the runner's `baseline`
        mode once (regression only, no model tokens) and it becomes baseline.json: suites
        already red at the base are PRE-EXISTING in every sprint gate (UI-02a lost ~4 h to
        reds that a manual baseline later proved pre-existing), and suites that passed
        there carry. True = the first session waits (the runner holds Studio). Fails open:
        an errored baseline only means a strict gate, as before."""
        tid = t["task_id"]
        if not settings(P).get("auto_baseline", True) or (t.get("data") or {}).get("task_base"):
            return False
        fl = self._fl(P, tid)
        b = dict(fl.get("baseline") or {})
        if b.get("status") in ("done", "skipped", "error"):
            return False
        gd = self._gate_dir(P, tid)
        if not b:
            if os.path.exists(os.path.join(gd, "baseline.json")):
                fl["baseline"] = {"status": "skipped", "why": "baseline.json already present"}
                self._save(P, tid, fl)
                return False
            if self.c._held(P, t, "baseline start"):
                return True
            try:
                head = P.ws.head()
            except Exception as e:                                # noqa: BLE001
                return self._baseline_done(P, t, {"status": "error"}, f"git: {e}")
            run_id = f"{tid}-base-{head[:12]}-{secrets.token_hex(6)}"
            log = os.path.join(evidence_dir(self.c.state_dir, P.id, tid, head),
                               "fast_lane_baseline.log")
            cmd = self._runner(P, run_id) + ["baseline", "--sprint", tid]
            try:
                proc = self._spawn(cmd, log, run_id)
            except OSError as e:
                return self._baseline_done(P, t, {"status": "error"}, f"could not start: {e}")
            self._procs[(P.id, tid, f"baseline:{head}")] = proc
            fl["baseline"] = {"status": "running", "head": head, "run_id": run_id, "pid": proc.pid,
                              "log": log, "started": self.clock(), "wall": time.time()}
            self._save(P, tid, fl)
            self.store.event(P.id, tid, "fast_lane_baseline_started", detail={
                "head": head, "pid": proc.pid, "run_id": run_id, "log": log})
            self.c._note(P, t, f"automatic baseline running at {head[:12]} before the first "
                               f"session (no model tokens)")
            return True
        head = str(b.get("head") or "")
        v = _read_json(os.path.join(gd, f"{head[:12]}.baseline.json"))
        if v and v.get("run_id") == b.get("run_id") and v.get("mode") == "baseline" \
                and v.get("head") == head:
            self._procs.pop((P.id, tid, f"baseline:{head}"), None)
            if str(v.get("result") or "").upper() == "ERROR":
                return self._baseline_done(P, t, b, f"runner error: {str(v.get('error'))[:300]}")
            red = [str(c.get("id")) for c in v.get("checks") or [] if isinstance(c, dict)
                   and str(c.get("id", "")).startswith("regression.")
                   and str(c.get("result") or "").upper() in ("FAIL", "UNKNOWN")]
            b.update(status="done", result=v.get("result"), pre_existing=red[:60])
            fl = self._fl(P, tid)
            fl["baseline"] = b
            self._save(P, tid, fl)
            self.store.event(P.id, tid, "fast_lane_baseline", detail={
                "head": head, "result": v.get("result"), "red": red[:30],
                "summary": v.get("summary_line")})
            return False
        proc = self._procs.get((P.id, tid, f"baseline:{head}"))
        if proc is not None and proc.poll() is not None:
            self._procs.pop((P.id, tid, f"baseline:{head}"), None)
            return self._baseline_done(P, t, b, f"the runner exited with code {proc.returncode} "
                                                f"without a baseline verdict")
        st = _read_json(os.path.join(gd, "status.json")) or {}
        if proc is None and st.get("pid") == b.get("pid") and st.get("phase") == "done":
            return self._baseline_done(P, t, b, "the runner finished without a baseline verdict")
        limit = float(settings(P)["gate_timeout_minutes"]) * 60
        elapsed = self.clock() - float(b.get("started") or self.clock())
        if elapsed > limit:
            self._kill(P, tid, {"head": f"baseline:{head}", "pid": b.get("pid")}, "baseline timeout")
            return self._baseline_done(P, t, b, f"did not finish within {int(limit // 60)} minutes")
        self.c._note(P, t, f"automatic baseline running at {head[:12]}: phase "
                           f"{st.get('phase') or 'starting'} {st.get('progress') or ''}, "
                           f"{int(elapsed // 60)}:{int(elapsed % 60):02d}")
        return True

    def _baseline_done(self, P, t: dict, b: dict, why: str) -> bool:
        tid = t["task_id"]
        fl = self._fl(P, tid)
        fl["baseline"] = {**b, "status": "error", "error": why[:500]}
        self._save(P, tid, fl)
        self.store.event(P.id, tid, "fast_lane_baseline_error", detail={"reason": why[:500]})
        return False

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
        try:
            proc = self._spawn(cmd, log, run_id)
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
        # a red (or errored) gate never carries to a newer commit: the session may have fixed
        # it, or the runner may judge differently now (UI-02a 2026-10-21: its own gate PASSed
        # with the baseline at 746db62, but the coordinator re-consumed the red verdict from
        # eb14163, saw "same findings" and opened a no-progress decision)
        if v is not None and effective_result(v) != PASS:
            return False
        try:
            if not P.ws.is_ancestor(gate_head, head):
                return False
            changed = P.ws.changed_files(gate_head, head)
        except Exception:                                         # noqa: BLE001
            return False
        caps = [str(c.get("path")) for c in (v or {}).get("captures") or []
                if isinstance(c, dict) and c.get("path")]
        outs, rcaps = runner_patterns(P, spec.task_id) if spec is not None else ([], [])
        allowed = list(P.adapter.exempt_paths(spec)) + caps + outs + rcaps
        rest = [f for f in changed if not matches_any(f, allowed)]
        if rest and spec is not None:
            # B1: a coordinator commit (runner outputs, checklist ticks) never invalidates
            # the gate it followed
            from . import bookkeeping as BK
            t = self.store.task(P.id, spec.task_id) or {}
            mine = set(BK.only_coordinator(P, t, gate_head, head, rest))
            rest = [f for f in rest if f not in mine]
        return not rest

    def _commit_runner_files(self, P, t: dict, v: dict, spec=None,
                             run: Optional[dict] = None) -> tuple[str, str]:
        """B1 (2026-10-21): the coordinator commits the runner's outputs itself (the
        verdict's captures, the task's runner_captures folder, its runner_outputs such as
        the evidence log) instead of asking the session to. UI-D1 spent attempts 3-7 on
        "commit its captures" turns. B4: the same commit ticks the plan checklist rows the
        green gate verifies. (sha, "") | ("", "") nothing to commit | ("", error)."""
        from . import bookkeeping as BK
        tid = t["task_id"]
        outs, caps = runner_patterns(P, tid)
        exact = {str(c.get("path")).replace("\\", "/") for c in v.get("captures") or []
                 if isinstance(c, dict) and c.get("path")}
        try:
            dirty = P.ws.status()
        except Exception as e:                                    # noqa: BLE001
            return "", str(e)[:300]
        paths = [p for p in BK.runner_paths(dirty, outs + caps, exact)
                 if P.profile.in_workspace(p)]
        ticked = self._tick_plan(P, t, spec, v, run or {}, dirty, exact)
        if ticked:
            paths.append(ticked)
        if not paths:
            return "", ""
        what = "gate captures and runner outputs" + (
            ", verified checklist ticks" if ticked else "")
        return BK.commit(self.c, P, t, paths, f"{what} (coordinator)",
                         "runner_outputs" + ("+checklist" if ticked else ""))

    def _tick_plan(self, P, t: dict, spec, v: dict, run: dict, dirty: list,
                   caps: set) -> str:
        """B4 (2026-10-21): tick the plan checklist rows a green gate verifies (rows about
        the gate, and about captures when the gate made some), each with its evidence;
        never the final CLOSE row, never a plan with uncommitted edits. UI-D1's three
        Deep-Reviews blocked on unticked BUILD rows. Returns the plan path when changed."""
        from . import bookkeeping as BK
        if spec is None or not settings(P).get("tick_checklist", True):
            return ""
        plan = str((getattr(spec, "data", None) or {}).get("plan") or "").replace("\\", "/")
        if not plan or not P.profile.in_workspace(plan) or \
                plan in {p.replace("\\", "/") for _, p in dirty}:
            return ""
        path = os.path.join(P.profile.workspace, *plan.split("/"))
        try:
            with open(path, encoding="utf-8", newline="") as f:
                text = f.read()
        except OSError:
            return ""
        tid = t["task_id"]
        full = str(run.get("head") or v.get("head") or "")
        gh = full[:12]
        verified = {r"\bgate\b(?!\.json)": f"GATE {tid} PASS at {gh}, {verdict_rel(tid, full)}"}
        if caps:
            verified[r"\b(?:captures?|screenshots?)\b"] = (
                f"{len(caps)} capture(s) from the gate at {gh}, committed by the coordinator")
        new, rows = BK.tick_lines(text, verified)
        if not rows:
            return ""
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(new)
        except OSError:
            return ""
        fl = self._fl(P, tid)
        fl["ticked"] = (list(fl.get("ticked") or []) + rows)[-40:]
        self._save(P, tid, fl)
        self.store.event(P.id, tid, "checklist_ticked", detail={"plan": plan, "rows": rows[:20],
                                                                 "gate_head": gh})
        return plan

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
        fl = self._fl(P, tid)
        run = fl.get("run")
        if run:
            self._kill(P, tid, run, why)
        b = fl.get("baseline") or {}
        if b.get("status") == "running":
            self._kill(P, tid, {"head": f"baseline:{b.get('head')}", "pid": b.get("pid")}, why)

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
        if proc is None and run.get("pid") and st.get("pid") == run.get("pid") and \
                st.get("phase") != "done" and not pid_alive(run.get("pid")):
            # W1.8: after a coordinator restart the runner is known only by its pid; a
            # dead pid without a verdict used to wait out gate_timeout_minutes (45) and
            # then count as a red gate. It is tooling (auto-heal re-runs the gate).
            v = self._verdict(P, tid, run)
            if v is not None:
                return self._consume(P, t, spec, head, generic, run, v)
            if st.get("playing"):
                self._stop_playtest(P, tid)
            self.store.event(P.id, tid, "fast_lane_runner_dead", detail={
                "head": run["head"], "pid": run.get("pid"), "phase": st.get("phase"),
                "playing": bool(st.get("playing"))})
            return self._error(P, t, head, generic,
                               f"the studio gate runner (pid {run.get('pid')}) is gone without a "
                               f"verdict for {run['head'][:12]} (last phase "
                               f"{st.get('phase') or 'unknown'}); log {run.get('log')}: "
                               + self._log_tail(run, 400).strip()[-400:])
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
        if res != PASS and rojo_stale_only(v):
            # W1.8: Studio's loaded sources differ from HEAD while nothing else failed:
            # Rojo / the plugin is stale, not the code. Tooling: auto-heal restarts Rojo
            # through the client and re-runs the gate; no red gate, no session prompt
            return self._error(P, t, head, generic + rows,
                               "Rojo stale: Studio's loaded sources differ from HEAD "
                               "(host.live_hash) while every other check passed: "
                               + "; ".join(failed_check_lines(v)[:3]))
        if res != PASS:
            return self._red(P, t, spec, head, generic + rows, v, vkey, first)
        # PASS: the runner's own outputs are committed by the coordinator (B1), then this
        # verdict is re-read at the new head next tick (a coordinator commit carries it)
        sha, cerr = self._commit_runner_files(P, t, v, spec, run)
        if sha:
            self.c._note(P, t, f"studio gate PASS at {run['head'][:12]}; the coordinator "
                               f"committed its captures and outputs at {sha[:12]}")
            return None
        bad = [r for r in generic if r.required and r.result != PASS]
        if bad and not cerr:
            # what is left is the agent's own work (B1: runner outputs never land here)
            vid = self.store.save_verification(P.id, tid, head, "verify", generic + rows)
            lines = failed_findings(bad)
            key = f"fast-lane:checks-after:{head[:12]}:" + hashlib.sha1(
                "\n".join(lines).encode()).hexdigest()[:10]
            return self._continue(P, t, spec, head, vid, key,
                                  f"Studio gate PASS at {run['head'][:12]}, but the "
                                  f"coordinator's checks failed", lines,
                                  "the studio gate passed; the coordinator's checks did not")
        if cerr:
            # the coordinator could not commit the runner outputs itself (a hook refused,
            # git failed): the old path, the session commits them
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
                else []) + [f"(the coordinator's own commit of these files failed: "
                            f"{cerr[:200]})"]
            return self._continue(P, t, spec, head, vid, f"fast-lane:commit:{head}",
                                  f"Studio gate PASS at {run['head'][:12]}: commit its captures",
                                  lines, "the studio gate passed; its captures must be committed")
        if settings(P).get("early_review", True):
            # B4: the code review needs only the green gate, not the look: it starts now and
            # runs while the vision batches do; both feed ONE fix round
            if (P.id, tid, head) not in self.c._early_reviews:
                vid0 = self.store.save_verification(P.id, tid, head, "verify", generic + rows)
                self.c.early_review_start(P, self.store.task(P.id, tid) or t, head, vid0)
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
                  lines: list, reason: str, extra: str = "",
                  review_id: str = "") -> Optional[list]:
        tid = t["task_id"]
        sid = t.get("session_id") or ""
        if sid and self.store.op(f"deliver:{P.id}:{tid}:{sid}:{key}"):
            # this exact prompt already went to this session and it came back
            # unchanged: no loop, the ordinary repair path takes over
            return self._fallback(P, t, vid, lines, review_id)
        fl = self._fl(P, tid)
        fl["pending"] = {"key": key, "head": head, "vid": vid, "title": title,
                         "lines": list(lines)[:30], "reason": reason, "extra": extra,
                         "review_id": review_id}
        self._save(P, tid, fl)
        return self._send(P, self.store.task(P.id, tid), spec, head, fl["pending"])

    def _send(self, P, t: dict, spec, head: str, pend: dict, resumed: bool = False):
        tid = t["task_id"]
        sid = t.get("session_id") or ""
        if not sid or not self.store.session(sid):
            return self._fallback(P, t, pend["vid"], pend["lines"], pend.get("review_id") or "")
        try:
            gone = P.client.state(sid, directory=P.profile.workspace,
                                  since_ts=0).status == "missing"
        except Exception:                                         # noqa: BLE001
            gone = False                 # unknown: the prompt decides
        if gone:
            return self._fallback(P, t, pend["vid"], pend["lines"], pend.get("review_id") or "")
        if self.c._held(P, t, "fast-lane continuation"):
            return None                  # kept pending for the resume
        if self.c._acquire(P, t, self.c._session_locks(P), writer=True):
            return None                  # waiting for the writer slot / Studio
        text = (P.adapter.continue_prompt(spec, pend["reason"])
                + f"\n\n## {pend['title']} (from the coordinator; fix it in this session)\n"
                + "\n".join(f"{i}. {x}" for i, x in enumerate(pend["lines"], 1)) + "\n"
                + (pend.get("extra") or "")
                + "\nFix the cause within the plan's file list. Check your fix with `stories "
                  "--sprint <ID> --story <name>` for each failing story and the Lune specs of "
                  "the models you touched, nothing else: never run `sprint` or `regression` "
                  "yourself and do not re-run checks that passed (passed work is recorded and "
                  "carried). Commit by explicit path, then report DONE: the coordinator runs "
                  "the one gate, re-running only what failed or what your change touched; it "
                  "commits the runner's captures and logs itself. "
                  "Never weaken a check, skip a story "
                  "or edit the runner to get PASS. If a finding needs a decision the plan "
                  "doesn't settle, report BLOCKED with 2-4 options and the one you recommend "
                  "(`options`, `recommended` in the report); the coordinator decides and "
                  "continues the sprint, nobody is asked.\n")
        try:
            sent = self.c._prompt_primary(P, t, text, pend["key"])
        except Exception as e:                                    # noqa: BLE001
            self.c._release_session(P, t)
            self.store.event(P.id, tid, "warning", session_id=sid, detail={
                "fast_lane": f"continuation failed ({pend['key']}): {type(e).__name__}: {e}"[:400]})
            return self._fallback(P, t, pend["vid"], pend["lines"], pend.get("review_id") or "")
        fl = self._fl(P, tid)
        fl.pop("pending", None)
        self._save(P, tid, fl)
        self.store.event(P.id, tid, "fast_lane_continuation", session_id=sid, detail={
            "key": pend["key"], "sent": sent, "resumed": resumed, "title": pend["title"],
            "findings": pend["lines"][:10]})
        ev = {"verification_id": pend["vid"], "session_id": sid, "continuation": pend["key"],
              "fast_lane": pend["title"]}
        if pend.get("review_id"):
            ev["review_id"] = pend["review_id"]          # (REVIEWING -> IMPLEMENTING, B4)
        self.store.transition(P.id, tid, S.IMPLEMENTING, ev, phase="implement", head_sha=head,
            next_action=f"session working (fast lane: {pend['title']})"[:500])
        return None

    def _fallback(self, P, t: dict, vid: str, lines: list, review_id: str = "") -> None:
        tid = t["task_id"]
        fl = self._fl(P, tid)
        fl.pop("pending", None)
        self._save(P, tid, fl)
        cur = self.store.task(P.id, tid)
        self.c._release_session(P, cur)
        findings = list(lines) or ["fast lane: the studio gate did not pass"]
        self.store.event(P.id, tid, "fast_lane_fallback", detail={"findings": findings[:10]})
        ev = {"verification_id": vid, "findings": findings}
        kw = {"verification_id": vid} if vid else {}
        if review_id:
            ev["review_id"] = review_id
            kw["review_id"] = review_id
        self.store.transition(P.id, tid, S.CHANGES_REQUIRED, ev,
                              data={**cur["data"], "findings": findings}, **kw)
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
        all_frames = list(frames)
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
        # once the look was accepted (approved, or accepted with notes after the CHANGES
        # rounds), a later revision is reviewed only where its frames' bytes changed: UI-D1
        # re-sent all ~500 frames (26 image calls) for every small commit
        prior = self.store.approvals(P.id, tid, "visual")
        if prior:
            seen = prior[-1].get("artifacts") or {}
            changed = [f for f in frames if seen.get(f[0]) != arts[f[0]]]
            if not changed:
                self.store.add_approval(P.id, tid, "visual", head, arts, prior[-1]["approver"],
                                        f"carried: every frame is byte-identical to the "
                                        f"visual approval at {str(prior[-1]['revision'])[:12]}")
                return VerificationResult("fast_lane.vision", PASS, True, "",
                                          f"carried: {len(frames)} frame(s) unchanged since the "
                                          f"approval at {str(prior[-1]['revision'])[:12]}", head)
            frames = changed
        # vision memory (2026-10-21): a frame the reviewer already judged clean (seen, valid, no
        # finding) at these exact bytes is never sent again, in any round. Only frames that
        # changed or had findings are reviewed.
        clean = VA.clean_memory(self.store.task(P.id, tid) or t)
        if clean:
            todo = [f for f in frames if arts[f[0]] not in clean]
            if not todo:
                self.store.add_approval(P.id, tid, "visual", head, arts, "coordinator:vision-memory",
                                        f"every frame was judged clean at these bytes in an earlier "
                                        f"round ({len(frames)} frame(s))")
                return VerificationResult("fast_lane.vision", PASS, True, "",
                                          f"remembered: {len(frames)} frame(s) already judged clean "
                                          f"at these bytes", head)
            frames = todo
        fp = hashlib.sha256(json.dumps([run["head"], arts], sort_keys=True).encode()).hexdigest()
        tries = int((fl.get("vision_uncertain") or {}).get(fp, 0))
        # Batches (UI-D1, 2026-10-20: ~560 frames in ONE call came back "uncertain"). B3
        # (2026-10-21): a round's batches are cut once and kept, so the clean-frame memory
        # saved after each batch never re-cuts the batches still to come; up to
        # [fast_lane] vision_parallel batch calls run at once (a pass took 30-39 min one
        # call per tick); every batch must approve; findings merge.
        size = max(1, int(settings(P).get("vision_batch_frames") or 20))
        rnd = fl.get("vision_round") or {}
        if rnd.get("fp") != fp:
            rnd = {"fp": fp, "batches": [[rel for rel, _m, _b in frames[i:i + size]]
                                         for i in range(0, len(frames), size)]}
            fl["vision_round"] = rnd
            self._save(P, tid, fl)
        by_rel = {f[0]: f for f in all_frames}
        batches = [[by_rel[r] for r in b if r in by_rel] for b in rnd["batches"]]
        batches = [b for b in batches if b]
        total = sum(len(b) for b in batches)
        for _pass in range(2):
            results_b, pending, waiting = self._vision_state(P, tid, fp, tries, batches, arts,
                                                             size)
            if pending and _pass == 0:
                if self.c._held(P, t, "vision review"):
                    return None
                self._vision_calls(P, t, spec, head, run, v, persona, routes, pending, tries,
                                   results_b, len(batches), total, arts)
                continue
            break
        if pending or waiting:
            return None
        served_all = {r.get("served_model") or "unknown" for r in results_b}
        verdicts = [r.get("verdict") for r in results_b]
        findings, unsure = [], []
        for bi, r in enumerate(results_b):
            pre = f"[batch {bi + 1}/{len(results_b)}] " if len(results_b) > 1 else ""
            if r.get("verdict") == "changes_required":
                findings += [pre + str(f) for f in (r.get("findings") or [])]
            elif r.get("verdict") != "approve":
                unsure.append(pre + "; ".join(str(f) for f in (r.get("findings") or [])[:2]))
        served = ",".join(sorted(served_all)) or "unknown"
        actor = f"model:{persona}/{served}"
        if unsure and tries < 1:
            # one structured retry (B3): only the inconclusive batches are asked again, in
            # small chunks with a JSON schema; decisive batches are kept
            fl = self._fl(P, tid)
            unc = dict(fl.get("vision_uncertain") or {})
            unc[fp] = tries + 1
            fl["vision_uncertain"] = unc
            self._save(P, tid, fl)
            self.c._note(P, t, f"fast-lane vision review: {len(unsure)} batch(es) inconclusive; "
                               f"one structured retry")
            return None
        if all(x == "approve" for x in verdicts):
            self.store.add_approval(P.id, tid, "visual", head, arts, actor,
                                    f"fast-lane vision approval at gate {run['head'][:12]}; "
                                    f"policy visual_model v1")
            return VerificationResult("fast_lane.vision", PASS, True, "",
                                      f"APPROVED by {actor} ({total} frame(s))", head)
        unsure_notes = [f"inconclusive after one structured retry (accepted with notes): {u}"[:400]
                        for u in unsure]
        if findings:
            fl = self._fl(P, tid)
            rounds = int(fl.get("vision_rounds") or 0)
            mx = int(settings(P)["max_vision_rounds"])
            if rounds < mx:
                # B4: a code review running alongside joins this fix round (one round, one
                # warm session); wait for it rather than send two rounds
                merged = self._review_for_merge(P, t, head)
                if merged == "wait":
                    self.c._note(P, t, "vision asks for changes; waiting for the code review in "
                                       "flight to send one merged fix round")
                    return None
                fl = self._fl(P, tid)
                fl["vision_rounds"] = rounds + 1
                self._save(P, tid, fl)
                vid = self.store.save_verification(P.id, tid, head, "verify", results + [
                    VerificationResult("fast_lane.vision", FAIL, True, "",
                                       "CHANGES: " + " | ".join(findings[:6]), head)])
                return self._vision_changes(P, t, spec, head, vid, fp, rounds + 1, mx,
                                            findings[:60] + unsure_notes, merged)
        # accept with notes: CHANGES after the fix rounds, or batches still inconclusive after
        # the one structured retry (never a stop: the operator decided 2026-10-21)
        fl = self._fl(P, tid)
        rounds = int(fl.get("vision_rounds") or 0)
        notes = (findings[:30] + unsure_notes)[:40]
        fl["vision_notes"] = (list(fl.get("vision_notes") or []) + notes)[-60:]
        self._save(P, tid, fl)
        why = (f"after {rounds} CHANGES round(s)" if findings else
               f"{len(unsure)} batch(es) inconclusive after one structured retry")
        self.store.add_approval(P.id, tid, "visual", head, arts,
                                f"coordinator:fast_lane ({actor})",
                                f"accepted with notes {why}: " + " | ".join(notes)[:1500])
        self.store.event(P.id, tid, "fast_lane_vision_accepted_with_notes", detail={
            "rounds": rounds, "notes": notes[:20], "head": head,
            "inconclusive": len(unsure)})
        return VerificationResult("fast_lane.vision", PASS, True, "",
                                  f"accepted with notes {why} (follow-ups): "
                                  + " | ".join(notes[:8]), head)

    def _vision_changes(self, P, t: dict, spec, head: str, vid: str, fp: str, rnd: int, mx: int,
                        findings: list, review: Optional[dict] = None) -> Optional[VerificationResult]:
        """Vision CHANGES go back into the same session (one fix round); with a code
        review's CHANGES at the same head, both in ONE prompt (B4)."""
        lines = list(findings or ["changes required"])
        title = f"Vision review: CHANGES (round {rnd}/{mx})"
        key = f"fast-lane:vision:{fp}"
        if review:
            lines = [f"vision: {x}" for x in lines] + review["lines"]
            title += f" + code review {review['review_id']}: CHANGES_REQUIRED"
            key += f":review:{review['review_id']}"
        return self._continue(P, t, spec, head, vid, key, title, lines[:60],
                              "the vision reviewer" + (" and the code reviewer" if review else "")
                              + " asked for changes")

    def _review_for_merge(self, P, t: dict, head: str):
        """B4: "wait" while the early review of `head` runs; its CHANGES_REQUIRED (lines,
        id) once done; None when there is nothing to merge."""
        from . import review as R
        tid = t["task_id"]
        got = self.c.early_review_collect(P, t, head)
        if got == "running":
            return "wait"
        revs = [r for r in self.store.reviews(P.id, tid, head) if r["valid"]
                and r["verdict"] == "CHANGES_REQUIRED" and R.packet_version(r) == R.PACKET_VERSION]
        if not revs:
            return None
        rev = revs[-1]
        lines = ["code review: " + x for x in (R.findings_for_repair(rev["result"] or {})
                                              or ["the reviewer asked for changes"])]
        self.store.event(P.id, tid, "fast_lane_merged_fix_round", detail={
            "head": head, "review_id": rev["review_id"], "review_findings": lines[:10]})
        return {"review_id": rev["review_id"], "lines": lines[:30]}

    def review_changes(self, P, t: dict, rev: dict, findings: list) -> bool:
        """B4: a code review's CHANGES_REQUIRED goes back into the same warm session
        (fast lane), like a red gate or vision CHANGES, instead of a cold repair
        session. False = not handled here (no session): the caller's repair path."""
        tid = t["task_id"]
        if not t.get("session_id") or not settings(P).get("early_review", True):
            return False
        spec = P.adapter.describe(tid)
        self._continue(P, t, spec, rev["reviewed_sha"], t.get("verification_id") or "",
                       f"fast-lane:review:{rev['review_id']}",
                       f"Code review {rev['review_id']}: CHANGES_REQUIRED",
                       findings or ["the reviewer asked for changes"],
                       "the code reviewer asked for changes", review_id=rev["review_id"])
        return True

    def review_evidence(self, P, t: dict, spec, head: str) -> list:
        """B4: the coordinator's own evidence for the review packet, built from what it
        verified itself: the gate verdict (with carried and pre-existing checks), the
        captures and vision state, and its bookkeeping commits (runner outputs,
        checklist ticks), which are never review findings."""
        from . import bookkeeping as BK
        tid = t["task_id"]
        fl = self._fl(P, tid)
        run = fl.get("run") or {}
        v = self._verdict(P, tid, run) if run.get("head") else None
        items = []
        if v:
            checks = [c for c in v.get("checks") or [] if isinstance(c, dict)]
            res = effective_result(v)
            carried = [c.get("id") for c in checks if "CARRIED from" in str(c.get("detail") or "")]
            pre = [c.get("id") for c in checks if "PRE-EXISTING" in str(c.get("detail") or "")]
            lines = [f"{v.get('summary_line') or ''} at {run['head']} (run {run.get('run_id')}, "
                     f"mode {v.get('mode')})",
                     f"effective result {res}: {len(checks)} check(s), "
                     f"{sum(1 for c in checks if c.get('result') == 'PASS')} PASS, "
                     f"{len(carried)} carried from earlier verdicts (inputs unchanged), "
                     f"{len(pre)} pre-existing at the sprint's baseline"]
            for c in checks[:80]:
                ln = f"- {c.get('id')}: {c.get('result')}"
                if not c.get("required", True):
                    ln += " (advisory)"
                if c.get("result") != "PASS" or c.get("id") in pre:
                    ln += f" - {str(c.get('detail') or '')[:160]}"
                lines.append(ln)
            same = run["head"] == head
            items.append({"title": f"{tid} studio gate verdict (read by the coordinator)",
                          "path": verdict_rel(tid, run["head"]), "revision": run["head"],
                          "text": "\n".join(lines), "status": "ok", "priority": 1,
                          "note": "the coordinator ran this gate itself; " + (
                              "at the reviewed commit" if same else
                              f"at {run['head'][:12]}; only coordinator-committed or evidence "
                              f"files changed since")})
            caps = [c for c in v.get("captures") or [] if isinstance(c, dict) and c.get("path")]
            if caps:
                apr = self.store.approvals(P.id, tid, "visual")
                vis = (f"visual approval at {str(apr[-1]['revision'])[:12]} by "
                       f"{apr[-1]['approver']}: {str(apr[-1].get('note') or '')[:300]}"
                       if apr else "vision review not finished when this packet was built "
                                   "(it runs alongside this review)")
                items.append({"title": f"{tid} gate captures and vision (coordinator)",
                              "path": "", "revision": head, "status": "ok", "priority": 3,
                              "text": f"{len(caps)} capture(s) from the gate, committed by the "
                                      f"coordinator: " + ", ".join(str(c['path']) for c in caps[:30])
                                      + ("" if len(caps) <= 30 else f" and {len(caps) - 30} more")
                                      + f"\n{vis}"})
        mine = BK.commits(self.store.task(P.id, tid) or t)
        ticked = list(fl.get("ticked") or [])
        if mine or ticked:
            text = ["Bookkeeping the coordinator does itself (verified by it; not the "
                    "implementer's work and never a review finding):"]
            if mine:
                text.append(f"- {len(mine)} coordinator commit(s) of runner outputs "
                            f"(captures, evidence log) and checklist ticks")
            text += [f"- ticked by the coordinator: {r}" for r in ticked[:20]]
            text.append("- closeout documents (summary, state row) are written by the "
                        "coordinator after approval")
            items.append({"title": "coordinator bookkeeping (not for review)", "path": "",
                          "revision": head, "status": "ok", "priority": 4,
                          "text": "\n".join(text)})
        return items

    def _vision_state(self, P, tid: str, fp: str, tries: int, batches: list, arts: dict,
                      size: int):
        """(results of finished batches in order, [pending call units], waiting?). A
        unit is one call: a whole batch on the first try, a chunk of an inconclusive
        batch on the retry. Saves the clean-frame memory of every finished batch."""
        from . import visual_approval as VA
        results_b, pending, waiting = [], [], False
        newly = []
        for bi, batch in enumerate(batches):
            suffix = f":b{bi + 1}of{len(batches)}" if len(batches) > 1 else ""
            key = f"fast-lane-vision:{P.id}:{tid}:{fp}:{tries}{suffix}"
            reused = None
            for earlier in range(tries + 1):
                op = self.store.op(f"fast-lane-vision:{P.id}:{tid}:{fp}:{earlier}{suffix}")
                r0 = (op or {}).get("result") or {}
                if op and op.get("status") == "done" and (
                        earlier == tries or r0.get("verdict") in ("approve", "changes_required")):
                    reused = r0
            if reused is not None:
                results_b.append(reused)
                newly += [arts[r] for r in VA.clean_frames(
                    reused.get("verdict"), reused.get("findings") or [],
                    [rel for rel, _m, _b in batch])]
                continue
            small = max(1, size // 4)
            chunks = [batch] if tries == 0 else [batch[i:i + small]
                                                 for i in range(0, len(batch), small)]
            parts_r = []
            for ci, chunk in enumerate(chunks):
                ckey = key if len(chunks) == 1 else f"{key}.c{ci + 1}of{len(chunks)}"
                op = self.store.op(ckey)
                r0 = (op or {}).get("result") or {}
                st = (op or {}).get("status")
                if st == "done":
                    parts_r.append(r0)
                    continue
                if st == "calling":
                    # a restart during the call: its outcome is unknown and never paid twice
                    res = {"verdict": "uncertain", "findings": ["call outcome unknown after a "
                                                                "restart"], "served_model": ""}
                    self.store.op_update(ckey, "done", res)
                    parts_r.append(res)
                    continue
                if st == "deferred" and self.clock() < float(r0.get("retry_at") or 0):
                    waiting = True
                    continue
                pending.append({"bi": bi, "ci": ci, "chunks": len(chunks), "key": ckey,
                                "chunk": chunk, "batch": batch,
                                "arts": {rel: arts[rel] for rel, _m, _b in chunk}})
            if len(parts_r) != len(chunks):
                continue
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
                if self.store.op_begin(key, P.id, tid, "visual_approval") is None or \
                        (self.store.op(key) or {}).get("status") != "done":
                    self.store.op_update(key, "done", res)
            results_b.append(res)
            newly += [arts[r] for r in VA.clean_frames(res.get("verdict"), res.get("findings")
                                                       or [], [rel for rel, _m, _b in batch])]
        if newly:
            fl = self._fl(P, tid)
            old = list(fl.get("vision_clean") or [])
            mem = list(dict.fromkeys(old + newly))[-4000:]
            if mem != old:
                fl["vision_clean"] = mem
                self._save(P, tid, fl)
        return results_b, pending, waiting

    def _vision_calls(self, P, t, spec, head, run, v, persona, routes, pending: list,
                      tries: int, results_b: list, n_batches: int, total: int, arts: dict) -> None:
        """Up to [fast_lane] vision_parallel calls at once (B3). Bookkeeping (journal,
        sessions, events, memory) stays on this thread; only the HTTP calls run in the
        pool. Each finished batch's clean frames are remembered right away."""
        from concurrent.futures import ThreadPoolExecutor
        tid = t["task_id"]
        par = max(1, int(settings(P).get("vision_parallel") or 1))
        units = pending[:par]
        prev = self._fl(P, tid).get("live") or {}
        so_far = [str(f) for r in results_b if r.get("verdict") != "approve"
                  for f in (r.get("findings") or [])]
        self._live(P, tid, {
            "kind": "vision", "head": head, "batch": units[0]["bi"] + 1, "batches": n_batches,
            "frames": sum(len(u["chunk"]) for u in units), "total_frames": total,
            "done": len(results_b), "in_flight": len(units),
            "changes": sum(1 for r in results_b if r.get("verdict") == "changes_required"),
            "findings": [f[:300] for f in so_far[:8]], "more_findings": max(0, len(so_far) - 8),
            "persona": str(persona)[:120],
            "started_ts": prev.get("started_ts") if prev.get("kind") == "vision"
            and prev.get("head") == head else time.time()})
        self.c._note(P, t, f"fast-lane vision review: {len(units)} call(s) in flight, "
                           f"{len(results_b)}/{n_batches} batch(es) done")
        jobs = []
        for u in units:
            if self.store.op_begin(u["key"], P.id, tid, "visual_approval") is not None and \
                    (self.store.op(u["key"]) or {}).get("status") not in ("deferred", "started"):
                continue
            sid, messages = self._vision_request(P, t, spec, head, run, v, u, persona,
                                                 (u["bi"] + 1, n_batches), tries)
            jobs.append((u, sid, messages))
        if not jobs:
            return
        schema = None
        if tries > 0:
            from . import visual_approval as VA
            schema = VA.VISION_SCHEMA

        def call(job):
            _u, sid, messages = job
            try:
                return P.foundry.chat(persona, messages, session_id=sid, max_tokens=6000,
                                      **({"schema": schema} if schema else {})), None
            except (FoundryDenied, FoundryError) as e:
                return None, e
        if len(jobs) == 1:
            outs = [call(jobs[0])]
        else:
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                outs = list(pool.map(call, jobs))
        for (u, sid, _m), (ans, err) in zip(jobs, outs):
            self._vision_result(P, t, head, u, sid, persona, routes, ans, err)

    def _vision_request(self, P, t, spec, head, run, v, u: dict, persona: str,
                        batch_label: tuple, tries: int) -> tuple[str, list]:
        """Journal the call as "calling", bind its session and build its messages."""
        from . import visual_approval as VA
        tid = t["task_id"]
        key = u["key"]
        frames, arts = u["chunk"], u["arts"]
        sid = f"fast-lane-vision-{key.rsplit(':', 2)[-2][:16]}-{key.rsplit(':', 1)[-1]}"
        if not self.store.session(sid):
            self.store.add_session(sid, P.id, tid, t["run_id"], "visual_reviewer",
                                   "visual_approval", t["attempt"], "foundry-direct", persona,
                                   {"parent": t.get("session_id"), "revision": head,
                                    "fast_lane": True})
        by_path = {str(c.get("path")).replace("\\", "/"): c for c in v.get("captures") or []
                   if isinstance(c, dict)}
        retry = ("This is the one retry of a batch whose first answer could not be verified: "
                 "answer with exactly the JSON object the response schema describes, one frame "
                 "object per attached image, each sha256 copied exactly. " if tries > 0 else "")
        parts = [{"type": "text", "text": (
            f"Fast-lane vision approval for project {P.id}, sprint {tid}, revision {head} (the "
            f"studio gate passed at {run['head'][:12]}). This is batch {batch_label[0]} of "
            f"{batch_label[1]}; every batch must approve. {retry}Review ALL {len(frames)} "
            "captures in this batch against the sprint and the rubric below. Do not infer "
            "pixels from filenames or claim inspection without seeing each image. Return valid "
            'JSON with keys "verdict" (approve, changes_required or uncertain), "frames" (one '
            "object per image with path, sha256, image_seen, subject_present, validity, "
            'observation and findings), and "limitations". ' + VA.FRAME_FIELDS + " Approve only "
            f"if every frame is valid, actually seen and meets the rubric.\n\n{RUBRIC}\n\n"
            f"Sprint:\n{spec.spec[:8000]}")}]
        for rel, mime, blob in frames:
            c = by_path.get(rel) or {}
            layout = [str(x) for x in (c.get("layout_lines") or [])][:8]
            parts.append({"type": "text", "text": (
                f"Capture {rel} (story {c.get('story')}, size {c.get('size')}), SHA256 "
                f"{arts[rel]}; checker geometry defects: {c.get('geometry_defects', 0)}"
                + ("; checker lines: " + " | ".join(layout) if layout else ""))})
            parts.append({"type": "image_url", "image_url": {
                "url": f"data:{mime};base64,{base64.b64encode(blob).decode()}"}})
        P.foundry.bind(sid, P.id, tid, t["run_id"], "visual_reviewer", persona,
                       parent=t.get("session_id") or "", purpose="review")
        self.store.op_update(key, "calling", {"session_id": sid})
        return sid, [{"role": "system", "content": "You are a read-only, independent vision "
                      "reviewer. Judge only the actual attached frames. Never waive test gates "
                      "or invent evidence."}, {"role": "user", "content": parts}]

    def _vision_result(self, P, t, head, u: dict, sid: str, persona: str, routes: list,
                       ans, err) -> None:
        from . import visual_approval as VA
        tid = t["task_id"]
        key = u["key"]
        if err is not None:
            if isinstance(err, FoundryDenied):
                self.store.op_update(key, "deferred", {"retry_at": self.clock() + max(
                    60, int(err.retry_after or 300)), "reason": str(err)[:300]})
                self.c._note(P, t, f"fast-lane vision approval deferred: {err}"[:500])
            else:
                self.store.op_update(key, "deferred", {"retry_at": self.clock() + 300,
                                                       "reason": str(err)[:300]})
                self.c._note(P, t, f"fast-lane vision approval unavailable: {err}"[:500])
            return
        raw, meta = ans
        served = str((meta or {}).get("served_model") or "")
        verdict, findings = VA._valid_verdict(VA._answer(raw), u["chunk"], u["arts"])
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
            "fast_lane": True, "artifacts": u["arts"], "findings": findings[:10]})
