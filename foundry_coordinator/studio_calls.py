"""Scripted Studio half of a sprint gate (2026-10-27, reports/STUDIO_GATE_SCRIPTED_PLAN.md).

Victor: "move the Studio half of the gate (the CALLs the LLM runs one by one) to the
scripted studio_gate runner, so the LLM only reads the results."

A task under [studio_calls] (off by default; `tasks`, else every task of `tiers`) no
longer runs tools/gate.ps1's Studio CALLs itself. Its session ends a turn with

    ```STUDIO_GATE
    {"suites": "GF_GATE1,S51", "regression": "full", "tier": "A", "rerun": "failed"}
    ```

and the coordinator runs ONE detached pipeline at the current HEAD (`pipeline` below):
gate.ps1's host half -> `studio_gate calls` (every CALL, the live lines file
docs/progress/<ID>_studio_lines.txt, PRE/POST-PLAY verifyLive, GateRunner.tally) ->
`gate.ps1 -Finish -StudioFile ...`. While it runs the idle session is never prompted and
the coordinator's own memory-note commits wait (HEAD must not move). Then the coordinator:
  - records verified ledger records (boot:server|client, suite:<S>, runtime:verify_live,
    gate:<ID>) through the adapter's classify_tool_result, each with the runtime identity
    of the PRE-PLAY verify and provenance (run id, the lines file's sha256, executor
    studio_gate): the session never sees a result before it is recorded;
  - commits the runner's outputs (docs/progress/<ID>_* : the lines, the evidence log,
    the verdicts and expect maps; closeout-exempt, so the gate binding stays PASS);
  - sends ONE prompt: `GATE <ID> PASS|FAIL at <sha>`, the counts, and for each failing
    row its CALL numbers, up to [studio_calls] failure_lines raw failure lines and where
    they are in the lines file. Passing rows are never pasted. Rows needing two players
    (S17) are MANUAL (Victor 2026-10-27): never run, never red; the prompt asks the
    session to run just those with multiplayer_playtest.
A tooling ERROR (MCP gone, Studio not at HEAD, HEAD moved, runner died, budget) stops
the task BLOCKED with the reason (auto-heal / the lead), never a red gate.

Also: [studio_calls] auto_verify runs it in VERIFYING for a tier A/B sprint whose gate is
missing or stale at HEAD (once per HEAD), and `foundry studio-calls run <project>
<task>` runs it with no session at all (the GF-GATE1 pilot); its failing rows become the
task's findings, so the next BUILD session gets only those.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from typing import Callable, Optional

from . import states as S
from .adapters.base import evidence_dir
from .autoheal import pid_alive

KEY = "studio_calls"
GATE_DIR = ".plan/tmp/gate"
PIPELINE_FILE = "calls_pipeline.json"
ROUTER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOCK_RE = re.compile(r"```STUDIO_GATE[ \t]*\r?\n?(.*?)```", re.S)   # (one-line form too)
RERUN = ("failed", "affected", "all")
TIERS = ("A", "B", "C")
ID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
SEL_RE = re.compile(r"(?:none|full|subset:[A-Za-z0-9_.\-]+|[A-Za-z0-9_.\-]+(?:[, ]+[A-Za-z0-9_.\-]+)*)")
PATH_RE = re.compile(r"[A-Za-z0-9_./\-]+")
DEFAULT_FILES = ["tests/RegressionIndex.lua", "tests/GateRunner.lua"]
EXEMPT = ("docs/progress/{task}_*", "docs/memory/**")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def defaults() -> dict:
    from .profile import STUDIO_CALLS_DEFAULTS
    return {k: (list(v) if isinstance(v, list) else v) for k, v in STUDIO_CALLS_DEFAULTS.items()}


def settings(P) -> dict:
    sc = getattr(P.profile, "studio_calls", None)
    out = defaults()
    if isinstance(sc, dict):
        out.update(sc)
    return out


def split_ids(v) -> list:
    if isinstance(v, (list, tuple)):
        v = ",".join(str(x) for x in v)
    return [x for x in re.split(r"[,\s]+", str(v or "")) if x]


def parse_request(text: str) -> Optional[dict]:
    """The LAST ```STUDIO_GATE``` block of a session's final message -> a normalized
    request, {"error": why} for a malformed one, None when there is none."""
    blocks = BLOCK_RE.findall(text or "")
    if not blocks:
        return None
    raw = blocks[-1].strip()
    try:
        d = json.loads(raw)
    except ValueError as e:
        return {"error": f"the STUDIO_GATE block is not JSON ({e})"}
    if not isinstance(d, dict):
        return {"error": "the STUDIO_GATE block must be a JSON object"}
    unknown = sorted(set(d) - {"suites", "regression", "tier", "rerun", "files"})
    if unknown:
        return {"error": f"unknown STUDIO_GATE keys {unknown} (suites, regression, tier, rerun, "
                         f"files)"}
    suites = split_ids(d.get("suites"))
    bad = [s for s in suites if not ID_RE.fullmatch(s)]
    regression = " ".join(str(d.get("regression") or "none").split())
    tier = str(d.get("tier") or "").strip().upper()
    rerun = str(d.get("rerun") or "failed").strip().lower()
    files = split_ids(d.get("files"))
    badf = [f for f in files if not PATH_RE.fullmatch(f) or ".." in f.split("/")]
    if bad:
        return {"error": f"bad suite ids {bad}"}
    if not SEL_RE.fullmatch(regression):
        return {"error": f"bad regression {regression!r} (none | full | subset:<name> | ids)"}
    if tier and tier not in TIERS:
        return {"error": f"bad tier {tier!r} (A, B or C)"}
    if rerun not in RERUN:
        return {"error": f"bad rerun {rerun!r} ({', '.join(RERUN)})"}
    if badf:
        return {"error": f"unsafe file paths {badf}"}
    if not suites and regression == "none":
        return {"error": "nothing to run: give suites and/or a regression selection"}
    return {"suites": suites, "regression": regression, "tier": tier, "rerun": rerun,
            "files": files}


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=1)
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


def _module_cmd(mod: str) -> list:
    mod = str(mod or "").strip()
    if mod.endswith(".py"):
        return [sys.executable, mod if os.path.isabs(mod) else os.path.join(ROUTER_ROOT, mod)]
    return [sys.executable, "-m", mod]


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))], capture_output=True,
                       timeout=20, creationflags=_NO_WINDOW)
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            os.kill(pid, signal.SIGTERM)


def _sg():
    from .projects.animaluprising import studio_gate
    return studio_gate


# ============================================================================ the pipeline
# Runs detached (its own process group): host half -> calls -> -Finish. It writes
# .plan/tmp/gate/<ID>/calls_pipeline.json (phase per step; `done` with the result).

def run_step(cmd: list, cwd: str, timeout: float, env: Optional[dict] = None) -> tuple:
    """(rc, output) of one step; on timeout the whole process tree is killed (rc 124)."""
    kw = {"creationflags": _NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} \
        if os.name == "nt" else {"start_new_session": True}
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                             errors="replace", env=env, **kw)
    except OSError as e:
        return 127, f"cannot run {cmd[0]}: {e}"
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, (out or "").rstrip()
    except subprocess.TimeoutExpired:
        try:
            _kill_tree(p.pid)
        except Exception:                                         # noqa: BLE001
            p.kill()
        out, _ = p.communicate()
        return 124, ((out or "") + f"\ntimed out after {timeout:.0f}s").strip()


def gate_ps1_cmd(a, finish: bool = False) -> list:
    ps = shutil.which("pwsh") or shutil.which("powershell") or "pwsh"
    # no -ExecutionPolicy Bypass (AS-BDG2: AU's own rule for the gate command)
    cmd = [ps, "-NoProfile", "-File", "tools/gate.ps1", "-Sprint", a.sprint,
           "-Files", ",".join(split_ids(a.files) or DEFAULT_FILES), "-Tier", a.tier,
           "-Regression", a.regression or "none"]
    if split_ids(a.suites):
        cmd += ["-Suites", ",".join(split_ids(a.suites))]
    if finish:
        cmd += ["-Finish", "-StudioFile", f"docs/progress/{a.sprint}_studio_lines.txt"]
    return cmd


def run_pipeline(a, run: Callable = run_step, log: Callable = print) -> dict:
    """host half -> calls -> -Finish at one HEAD. Returns the final pipeline record
    (also written to calls_pipeline.json): result PASS | FAIL | ERROR and the stage."""
    repo, sprint = os.path.abspath(a.repo), a.sprint
    gd = os.path.join(repo, *GATE_DIR.split("/"), sprint)
    pj = os.path.join(gd, PIPELINE_FILE)
    st = {"run_id": a.run_id, "sprint": sprint, "pid": os.getpid(), "phase": "host",
          "started": time.time(), "updated": time.time()}

    def save(**kw):
        st.update(kw, updated=time.time())
        _write_json(pj, st)

    save()
    rc, head = run(["git", "-C", repo, "rev-parse", "HEAD"], repo, 60)
    head = (head or "").strip()
    if rc != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
        save(phase="done", result="ERROR", stage="host", error=f"git rev-parse HEAD: {head[:200]}")
        return st
    save(head=head)
    host_cmd = gate_ps1_cmd(a)
    log("$ " + " ".join(host_cmd))
    rc, out = run(host_cmd, repo, float(a.host_timeout_min) * 60)
    log(out[-20000:])
    problem, _ = _sg().calls_preconditions(repo, sprint, head)
    if problem:
        fails = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("FAIL:")][:20]
        if rc == 124 or (rc not in (0, 1) and not fails):
            save(phase="done", result="ERROR", stage="host", host_rc=rc,
                 error=f"the host half did not finish (rc {rc}): {problem}"[:800])
        else:
            save(phase="done", result="FAIL", stage="host", host_rc=rc, host_problem=problem,
                 host_fail_lines=fails)
        return st
    save(phase="calls", host_rc=rc)
    calls_cmd = (_module_cmd(a.runner) + ["--repo", repo, "--run-id", a.run_id, "--timeout",
                                          str(a.call_timeout), "calls", "--sprint", sprint,
                                          "--regression", a.regression or "none", "--rerun",
                                          a.rerun, "--tier", a.tier, "--budget-min",
                                          str(a.budget_min), "--group-min", str(a.group_min)])
    if split_ids(a.suites):
        calls_cmd += ["--suites", ",".join(split_ids(a.suites))]
    env = dict(os.environ)
    env["PYTHONPATH"] = ROUTER_ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["FOUNDRY_GATE_RUN_ID"] = a.run_id
    env["PYTHONIOENCODING"] = "utf-8"
    log("$ " + " ".join(calls_cmd))
    rc2, out2 = run(calls_cmd, ROUTER_ROOT, float(a.budget_min) * 60 + 600, env)
    log(out2[-20000:])
    cv = _read_json(os.path.join(gd, f"{head[:12]}.calls.json"))
    if cv is not None and (cv.get("run_id") != a.run_id or cv.get("head") != head):
        cv = None
    save(calls_rc=rc2, calls_json=f"{GATE_DIR}/{sprint}/{head[:12]}.calls.json" if cv else "")
    if rc2 not in (0, 1) or cv is None or cv.get("result") == "ERROR":
        why = (cv or {}).get("error") or next(
            (ln for ln in reversed(out2.splitlines()) if ln.strip()), f"calls exited {rc2}")
        save(phase="done", result="ERROR", stage="calls", error=str(why)[:800])
        return st
    save(phase="finish")
    fin_cmd = gate_ps1_cmd(a, finish=True)
    log("$ " + " ".join(fin_cmd))
    rc3, out3 = run(fin_cmd, repo, 600)
    log(out3[-20000:])
    m = None
    for ln in out3.splitlines():
        mm = re.match(r"^GATE %s (PASS|FAIL)\b" % re.escape(sprint), ln.strip())
        if mm:
            m = mm
    if m is None:
        save(phase="done", result="ERROR", stage="finish", finish_rc=rc3,
             error=f"gate.ps1 -Finish printed no 'GATE {sprint} PASS|FAIL' line (rc {rc3})")
        return st
    save(phase="done", result=m.group(1), stage="done", finish_rc=rc3, finish_line=m.group(0),
         calls_result=cv.get("result"))
    return st


def _pipeline_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m foundry_coordinator.studio_calls")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pipeline", help="host half -> calls -> -Finish (the coordinator runs it)")
    p.add_argument("--repo", required=True)
    p.add_argument("--sprint", required=True)
    p.add_argument("--run-id", dest="run_id", required=True)
    p.add_argument("--files", default="")
    p.add_argument("--suites", default="")
    p.add_argument("--regression", default="none")
    p.add_argument("--tier", default="A", choices=list(TIERS))
    p.add_argument("--rerun", default="failed", choices=list(RERUN))
    p.add_argument("--runner", default="foundry_coordinator.projects.animaluprising.studio_gate")
    p.add_argument("--call-timeout", dest="call_timeout", type=float, default=120.0)
    p.add_argument("--group-min", dest="group_min", type=float, default=10.0)
    p.add_argument("--budget-min", dest="budget_min", type=float, default=30.0)
    p.add_argument("--host-timeout-min", dest="host_timeout_min", type=float, default=30.0)
    return ap


def main(argv=None) -> int:
    a = _pipeline_parser().parse_args(argv)
    if not ID_RE.fullmatch(a.sprint):
        print(f"bad sprint id {a.sprint!r}")
        return 2
    st = run_pipeline(a, log=lambda s: print(s, flush=True))
    print(f"PIPELINE {a.sprint} {st.get('result')} stage={st.get('stage')} "
          f"{st.get('finish_line') or st.get('error') or ''}".rstrip(), flush=True)
    return {"PASS": 0, "FAIL": 1}.get(str(st.get("result")), 2)


# ============================================================================ the coordinator side

class StudioCalls:
    def __init__(self, coordinator, clock=time.time):
        self.c = coordinator
        self.store = coordinator.store
        self.clock = clock
        self._procs: dict = {}                       # (project, task) -> Popen

    # ---------------------------------------------------------------- queries
    def _d(self, P, tid: str) -> dict:
        t = self.store.task(P.id, tid) or {}
        return dict((t.get("data") or {}).get(KEY) or {})

    def _save(self, P, tid: str, d: dict) -> None:
        self.store.merge_data(P.id, tid, **{KEY: d})

    def _gate_dir(self, P, tid: str) -> str:
        return os.path.join(P.profile.workspace, *GATE_DIR.split("/"), tid)

    def _tier(self, P, tid: str, spec=None) -> str:
        try:
            spec = spec or P.adapter.describe(tid)
            return str((getattr(spec, "data", None) or {}).get("tier") or "A").upper()
        except Exception:                                         # noqa: BLE001
            return "A"

    def on(self, P, tid: str, spec=None) -> bool:
        """[studio_calls] covers this task (never a fast-lane task, never a track)."""
        fn = getattr(P.profile, "studio_calls_task", None)
        try:
            if not callable(fn) or not fn(tid, self._tier(P, tid, spec)):
                return False
            if self.c.fast_lane.on(P, tid):
                return False
            return not self.c.tracks.is_track(P)
        except Exception:                                         # noqa: BLE001
            return False

    def wants(self, P, tid: str, s) -> Optional[dict]:
        """The STUDIO_GATE request in an idle session's final message (enabled tasks only)."""
        text = getattr(s, "final_text", "") or ""
        if "STUDIO_GATE" not in text:
            return None
        rep = getattr(s, "report", None) or {}
        if str(rep.get("status") or "").upper() in ("BLOCKED", "NEEDS_HUMAN"):
            return None
        if not self.on(P, tid):
            return None
        key = self._req_key(P, tid, text)
        if self._d(P, tid).get("req_key") == key:
            # this exact turn was already served (its result prompt could not be sent):
            # never a second run for the same request; the idle path takes over
            return None
        req = parse_request(text)
        if req is not None:
            req["_key"] = key
        return req

    def _req_key(self, P, tid: str, text: str) -> str:
        """One key per session TURN: the session id, the prompt it answered (prompt_ts)
        and its final text."""
        t = self.store.task(P.id, tid) or {}
        sid = str(t.get("session_id") or "")
        ts = str((self.store.session(sid) or {}).get("prompt_ts") or "") if sid else ""
        return hashlib.sha1(f"{sid}\n{ts}\n{text or ''}".encode("utf-8", "replace")).hexdigest()[:16]

    def active(self, P, tid: str) -> bool:
        d = self._d(P, tid)
        return bool(d.get("run") or d.get("pending"))

    # ---------------------------------------------------------------- session requests
    def on_request(self, P, t: dict, s, req: dict) -> bool:
        """An idle session asked for the scripted Studio half. True = handled (the run
        started or waits for Studio, or the session was told its block was malformed)."""
        tid = t["task_id"]
        sid = t.get("session_id") or ""
        req = dict(req)
        key = req.pop("_key", "")
        if key:
            d = self._d(P, tid)
            d["req_key"] = key
            self._save(P, tid, d)
        if req.get("error"):
            text = (f"## Scripted Studio gate: your STUDIO_GATE block was refused\n"
                    f"{req['error']}\nEnd your turn with one corrected block, e.g.\n"
                    f"```STUDIO_GATE\n{{\"suites\": \"S51\", \"regression\": \"none\", "
                    f"\"tier\": \"A\", \"rerun\": \"failed\"}}\n```\n")
            key = "studio-calls:bad:" + hashlib.sha1(req["error"].encode()).hexdigest()[:10]
            if sid and self.store.op(f"deliver:{P.id}:{tid}:{sid}:{key}"):
                return False                          # same malformed block again: normal path
            if s is not None:
                self.c._memory_notes(P, t, s)         # no run follows: nothing to wait for
            return self._prompt(P, t, text, key)
        try:
            head = P.ws.head()
        except Exception:                                         # noqa: BLE001
            head = ""
        d = self._d(P, tid)
        d["pending"] = {"req": req, "session_id": sid, "head": head, "ts": time.time()}
        self._save(P, tid, d)
        self.store.event(P.id, tid, "studio_calls_requested", session_id=sid,
                         detail={"request": req, "head": head})
        self.tick(P, self.store.task(P.id, tid), s)
        return True

    def tick(self, P, t: dict, s=None) -> Optional[dict]:
        """One step of this task's scripted run: start a pending one, else poll the
        running one (consumed when done). Returns the outcome once consumed."""
        tid = t["task_id"]
        d = self._d(P, tid)
        if d.get("run"):
            return self._poll(P, t, s, d["run"])
        if d.get("pending"):
            self._start(P, t, d["pending"])
        return None

    # ---------------------------------------------------------------- start
    def _files(self, P, t: dict, head: str) -> list:
        base = (t.get("data") or {}).get("task_base") or t.get("base_sha") or ""
        try:
            files = [f for f in P.ws.changed_files(base, head)
                     if not f.replace("\\", "/").startswith("docs/")] if base else []
        except Exception:                                         # noqa: BLE001
            files = []
        files = [f.replace("\\", "/") for f in files if PATH_RE.fullmatch(f.replace("\\", "/"))]
        return files[:200] or list(DEFAULT_FILES)

    def _studio_lock_free(self, P, t: dict) -> bool:
        """AU's studio.lock: free, or this task's own idle session left it behind (the
        coordinator releases that); held by anyone else -> wait."""
        tid = t["task_id"]
        try:
            SG = _sg()
            line = SG.studio_lock_read()
        except Exception:                                         # noqa: BLE001
            return True
        if not line:
            return True
        parts = line.split()
        if len(parts) > 2 and parts[2] == tid:
            if parts[0] == SG.STUDIO_LOCK_OWNER:
                # an earlier scripted run's lock: stale unless its runner still lives
                if SG.lock_holder(P.profile.workspace) is None and SG.studio_lock_release(tid):
                    return True
                self.c._note(P, t, f"scripted Studio gate waits for a running studio_gate "
                                   f"({line[:120]})")
                return False
            try:
                os.remove(SG.studio_lock_path())
            except OSError:
                return False
            self.store.event(P.id, tid, "studio_calls_lock_released",
                             detail={"line": line[:300], "why": "left behind by this task's "
                                                                "idle session"})
            return True
        self.c._note(P, t, f"scripted Studio gate waits for studio.lock ({line[:120]})")
        return False

    def _limit_s(self, P, req: dict) -> int:
        s = settings(P)
        budget = int(s["full_run_budget_minutes"] if req.get("regression") == "full"
                      else s["run_budget_minutes"])
        return (int(s["host_timeout_minutes"]) + budget + 15) * 60

    def _command(self, P, tid: str, req: dict, files: list, run_id: str, tier: str) -> list:
        s = settings(P)
        budget = (s["full_run_budget_minutes"] if req.get("regression") == "full"
                  else s["run_budget_minutes"])
        return (_module_cmd(s["pipeline_module"])
                + ["pipeline", "--repo", P.profile.workspace, "--sprint", tid, "--run-id", run_id,
                   "--files", ",".join(files), "--suites", ",".join(req.get("suites") or []),
                   "--regression", req.get("regression") or "none", "--tier", tier,
                   "--rerun", req.get("rerun") or "failed", "--runner", s["runner_module"],
                   "--call-timeout", str(s["call_timeout_seconds"]),
                   "--group-min", str(s["group_budget_minutes"]),
                   "--budget-min", str(budget),
                   "--host-timeout-min", str(s["host_timeout_minutes"])])

    def _spawn(self, cmd: list, log: str, run_id: str):
        env = dict(os.environ)
        env["PYTHONPATH"] = ROUTER_ROOT + (os.pathsep + env["PYTHONPATH"]
                                           if env.get("PYTHONPATH") else "")
        env["FOUNDRY_GATE_RUN_ID"] = run_id
        env["PYTHONIOENCODING"] = "utf-8"
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | _NO_WINDOW
        with open(log, "w", encoding="utf-8") as lf:
            lf.write("$ " + " ".join(cmd) + "\n")
            lf.flush()
            return subprocess.Popen(cmd, cwd=ROUTER_ROOT, stdout=lf, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env, creationflags=flags)

    def _start(self, P, t: dict, pend: dict) -> bool:
        tid = t["task_id"]
        if self.c._held(P, t, "scripted Studio gate start"):
            return False
        if self.c._acquire(P, t, self.c._session_locks(P)):
            return False                          # another task holds Studio: wait
        if not self._studio_lock_free(P, t):
            return False
        head = P.ws.head()
        req = dict(pend.get("req") or {})
        tier = req.get("tier") or self._tier(P, tid)
        files = list(req.get("files") or []) or self._files(P, t, head)
        run_id = f"{tid}-calls-{head[:12]}-{secrets.token_hex(6)}"
        log = os.path.join(evidence_dir(self.c.state_dir, P.id, tid, head), "studio_calls.log")
        cmd = self._command(P, tid, req, files, run_id, tier)
        with_pipeline = os.path.join(self._gate_dir(P, tid), PIPELINE_FILE)
        try:
            os.remove(with_pipeline)                 # never read an older run's record
        except OSError:
            pass
        try:
            proc = self._spawn(cmd, log, run_id)
        except OSError as e:
            d = self._d(P, tid)
            d.pop("pending", None)
            why = f"the scripted Studio gate could not start: {e}"
            if pend.get("cli"):
                d["last"] = {"result": "ERROR", "error": why, "stage": "start", "run_id": run_id,
                             "head": head}
                self._save(P, tid, d)
                return False
            self._save(P, tid, d)
            self._tooling(P, t, None, {"head": head, "run_id": run_id}, why)
            return False
        self._procs[(P.id, tid)] = proc
        d = self._d(P, tid)
        d.pop("pending", None)
        d["run"] = {"run_id": run_id, "head": head, "pid": proc.pid, "log": log,
                    "started": self.clock(), "wall": time.time(), "req": req, "tier": tier,
                    "files": files[:60], "session_id": pend.get("session_id") or "",
                    "auto": bool(pend.get("auto")), "cli": bool(pend.get("cli")),
                    "limit_s": self._limit_s(P, req), "cmd": [str(x)[:200] for x in cmd[:24]]}
        d["runs"] = int(d.get("runs") or 0) + 1
        self._save(P, tid, d)
        self.store.event(P.id, tid, "studio_calls_started", session_id=pend.get("session_id") or "",
                         detail={"head": head, "pid": proc.pid, "run_id": run_id, "log": log,
                                 "request": req, "files": files[:40]})
        self.c._note(P, t, f"scripted Studio gate started at {head[:12]} "
                           f"({req.get('regression')}; suites {','.join(req.get('suites') or []) or '-'})")
        return True

    # ---------------------------------------------------------------- poll / kill
    def _pipeline(self, P, tid: str, run: dict) -> Optional[dict]:
        pj = _read_json(os.path.join(self._gate_dir(P, tid), PIPELINE_FILE))
        return pj if pj and pj.get("run_id") == run.get("run_id") else None

    def _log_tail(self, run: dict, n: int = 600) -> str:
        try:
            with open(run.get("log") or "", encoding="utf-8", errors="replace") as f:
                return f.read()[-n:].strip()
        except OSError:
            return ""

    def _poll(self, P, t: dict, s, run: dict) -> Optional[dict]:
        tid = t["task_id"]
        pj = self._pipeline(P, tid, run)
        if pj and pj.get("phase") == "done":
            return self._consume(P, t, s, run, pj)
        proc = self._procs.get((P.id, tid))
        if proc is not None and proc.poll() is not None:
            for _ in range(10):              # a read racing the final os.replace (Windows)
                pj = self._pipeline(P, tid, run)
                if pj and pj.get("phase") == "done":
                    return self._consume(P, t, s, run, pj)
                time.sleep(0.1)
            return self._consume(P, t, s, run, {
                "result": "ERROR", "stage": (pj or {}).get("phase") or "start",
                "error": f"the pipeline exited with code {proc.returncode} without a result; log "
                         f"{run.get('log')}: {self._log_tail(run)}"})
        if proc is None and run.get("pid") and not pid_alive(run["pid"]):
            pj = self._pipeline(P, tid, run)
            if pj and pj.get("phase") == "done":
                return self._consume(P, t, s, run, pj)
            st = _read_json(os.path.join(self._gate_dir(P, tid), "status.json")) or {}
            if st.get("playing"):
                self._stop_playtest(P, tid)
            self._release_runner_lock(tid)
            return self._consume(P, t, s, run, {
                "result": "ERROR", "stage": (pj or {}).get("phase") or "unknown",
                "error": f"the pipeline (pid {run.get('pid')}) is gone without a result; log "
                         f"{run.get('log')}: {self._log_tail(run, 400)}"})
        elapsed = self.clock() - float(run.get("started") or self.clock())
        limit = float(run.get("limit_s") or self._limit_s(P, run.get("req") or {}))
        st = _read_json(os.path.join(self._gate_dir(P, tid), "status.json")) or {}
        if elapsed > limit:
            self._kill(P, tid, run, "timeout")
            return self._consume(P, t, s, run, {
                "result": "ERROR", "stage": (pj or {}).get("phase") or "unknown",
                "error": f"the scripted Studio gate did not finish within {int(limit // 60)} "
                         f"minutes (phase {(pj or {}).get('phase')}, {st.get('phase')} "
                         f"{st.get('progress') or ''}); it was stopped"})
        stale = float(settings(P)["stale_status_minutes"]) * 60
        if (pj or {}).get("phase") == "calls" and st.get("mode") == "calls" \
                and st.get("phase") != "done" and stale > 0:
            upd = float(st.get("updated") or 0)
            if upd and time.time() - upd > stale and pid_alive(st.get("pid")):
                self._kill(P, tid, run, "stale")
                return self._consume(P, t, s, run, {
                    "result": "ERROR", "stage": "calls",
                    "error": f"no CALL progress for {int(stale // 60)} min (status.json phase "
                             f"{st.get('phase')} {st.get('progress') or ''}: a modal dialog, a "
                             f"hung plugin?); the runner was stopped"})
        self.c._note(P, t, f"scripted Studio gate at {run['head'][:12]}: "
                           f"{(pj or {}).get('phase') or 'starting'}"
                           + (f" / {st.get('phase')} {st.get('progress') or ''}"
                              if (pj or {}).get("phase") == "calls" else "")
                           + f", {int(elapsed // 60)}:{int(elapsed % 60):02d} of "
                             f"{int(limit // 60)} min")
        return None

    def _release_runner_lock(self, tid: str) -> None:
        try:
            _sg().studio_lock_release(tid)        # only a studio.lock naming studio_gate + tid
        except Exception:                                         # noqa: BLE001
            pass

    def _stop_playtest(self, P, tid: str) -> None:
        mod = str(settings(P)["runner_module"]).strip()
        if mod.endswith(".py"):
            return
        try:
            subprocess.Popen(_module_cmd(mod) + ["--repo", P.profile.workspace, "stop-playtest"],
                             cwd=ROUTER_ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        except OSError as e:
            self.store.event(P.id, tid, "warning", detail={"studio_calls": f"stop-playtest: {e}"[:300]})

    def _kill(self, P, tid: str, run: dict, why: str) -> None:
        """Stop the pipeline AND its tree (runner, MCP children), then a playtest it left
        running (best effort) and the studio.lock it held."""
        proc = self._procs.pop((P.id, tid), None)
        st = _read_json(os.path.join(self._gate_dir(P, tid), "status.json")) or {}
        try:
            if proc is not None and proc.poll() is None:
                _kill_tree(proc.pid)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            elif proc is None and run.get("pid") and pid_alive(run["pid"]):
                _kill_tree(int(run["pid"]))
        except Exception as e:                                    # noqa: BLE001
            self.store.event(P.id, tid, "warning", detail={"studio_calls": f"kill: {e}"[:300]})
        if st.get("playing"):
            self._stop_playtest(P, tid)
        self._release_runner_lock(tid)
        self.store.event(P.id, tid, "studio_calls_stopped", detail={
            "head": run.get("head"), "pid": run.get("pid"), "reason": why[:300],
            "playtest_stop": bool(st.get("playing"))})

    def abort(self, P, tid: str, why: str) -> None:
        """The task was cancelled: stop its run (best effort) and forget a pending one."""
        d = self._d(P, tid)
        if d.get("run"):
            self._kill(P, tid, d["run"], why)
        if d.get("run") or d.get("pending"):
            d.pop("run", None)
            d.pop("pending", None)
            self._save(P, tid, d)

    # ---------------------------------------------------------------- consume
    def _calls_verdict(self, P, tid: str, run: dict) -> Optional[dict]:
        v = _read_json(os.path.join(self._gate_dir(P, tid), f"{run['head'][:12]}.calls.json"))
        if v and v.get("run_id") == run.get("run_id") and v.get("head") == run.get("head") \
                and v.get("mode") == "calls":
            return v
        return None

    def _lines_sha(self, P, tid: str) -> str:
        p = os.path.join(P.profile.workspace, "docs", "progress", f"{tid}_studio_lines.txt")
        try:
            with open(p, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except OSError:
            return ""

    def _finish_block(self, P, tid: str) -> str:
        """The evidence log from the last host-half `GATE RUN` line of this sprint to its
        end: `PASS: GATE HEAD`, the pasted lines and -Finish's `GATE <ID> PASS|FAIL`."""
        p = os.path.join(P.profile.workspace, "docs", "progress", f"{tid}_EVIDENCE_RAW.log")
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            return ""
        start = max((i for i, ln in enumerate(lines)
                     if ln.startswith(f"GATE RUN sprint={tid} ")), default=-1)
        return "\n".join(lines[start:]) if start >= 0 else ""

    def _record(self, P, t: dict, run: dict, pj: dict, v: Optional[dict], lines_sha: str) -> int:
        """Verified ledger records from the run's own output (adapter parsing), bound to
        the PRE-PLAY runtime identity and the gate HEAD ([lifecycle] evidence on)."""
        if not v or not self.c._lc(P, "evidence"):
            return 0
        classify = getattr(P.adapter, "classify_tool_result", None)
        ident_fn = getattr(P.adapter, "runtime_identity", None)
        if not callable(classify):
            return 0
        tid, head = t["task_id"], run["head"]
        ev = v.get("evidence") or {}
        boot_text = (ev.get("verify_pre") or "") + "\n" + (ev.get("boot") or "")
        n = [0]

        def rec():
            ident = ident_fn(boot_text) if callable(ident_fn) else None
            rid = ""
            if ident:
                ident = dict(ident, gate_head=head, kind=ident.get("kind") or "studio")
                rid = self.c.ledger._runtime(P, "", ident)
            prov = {"executor": "studio_gate", "run_id": run["run_id"], "lines_sha256": lines_sha,
                    "lines_file": f"docs/progress/{tid}_studio_lines.txt",
                    "calls_json": f"{GATE_DIR}/{tid}/{head[:12]}.calls.json",
                    "gate_head": head, "pipeline_result": pj.get("result"),
                    "finish_line": pj.get("finish_line") or "", "counts": v.get("counts")}
            sections = (("boot+verify", boot_text, ("boot", "runtime")),
                        ("tally", ev.get("tally") or "", ("suite",)),
                        ("finish", self._finish_block(P, tid), ("gate",)))
            started = float(v.get("started") or 0) or None
            ended = float(v.get("finished") or 0) or None
            for name, text, kinds in sections:
                if not text.strip():
                    continue
                items = classify({"tool": "studio_gate", "status": "completed", "output": text},
                                 {"task_id": tid, "runtime_hint": rid, "project_id": P.id}) or []
                for it in items:
                    cid = str(it.get("check_id") or "")
                    if it.get("check_kind") not in kinds or it.get("exclusion") or not cid:
                        continue
                    if it.get("check_kind") == "gate" and cid != f"gate:{tid}":
                        continue
                    result = str(it.get("result") or "UNKNOWN").upper()
                    if result not in ("PASS", "FAIL", "UNKNOWN", "NA"):
                        result = "UNKNOWN"
                    _, created = self.c.ledger.record(
                        project_id=P.id, task_id=tid, check_id=cid,
                        idem_key=f"{P.id}|{tid}|studio_calls|{run['run_id']}|{cid}",
                        run_id=t.get("run_id") or "", tool="studio_gate", source="coordinator",
                        check_kind=str(it.get("check_kind")), chain_id=str(it.get("chain_id") or ""),
                        seq=it.get("seq"), prereqs=list(it.get("prereqs") or []),
                        started_ts=started, ended_ts=ended, exec_status="completed",
                        result=result, counts=it.get("counts") or {},
                        tier="verified" if result in ("PASS", "FAIL") else "observed",
                        source_revision=head, runtime_id=rid,
                        provenance={**prov, "section": name, "lines": list(it.get("lines") or [])[:10]},
                        output=text[:60000])
                    n[0] += int(bool(created))
        self.c._lc_safe(P, tid, "studio_calls_record", rec)
        return n[0]

    def _commit(self, P, t: dict, run: dict, result: str) -> tuple:
        """The runner's outputs (docs/progress/<ID>_* by default), committed by the
        coordinator: (sha, "") | ("", "") nothing to commit | ("", error)."""
        from . import bookkeeping as BK
        tid = t["task_id"]
        pats = [x.replace("{task}", tid) for x in settings(P).get("outputs") or []]
        try:
            dirty = P.ws.status()
        except Exception as e:                                    # noqa: BLE001
            return "", str(e)[:300]
        paths = [p for p in BK.runner_paths(dirty, pats, set()) if P.profile.in_workspace(p)]
        if not paths:
            return "", ""
        return BK.commit(self.c, P, t, paths,
                         f"scripted Studio gate {result} at {run['head'][:12]}: lines, evidence "
                         f"and gate files (coordinator)", "studio_calls_outputs")

    def _consume(self, P, t: dict, s, run: dict, pj: dict) -> dict:
        tid = t["task_id"]
        self._procs.pop((P.id, tid), None)
        v = self._calls_verdict(P, tid, run)
        result = str(pj.get("result") or "ERROR").upper()
        outcome = {"run_id": run["run_id"], "head": run["head"], "result": result,
                   "stage": pj.get("stage"), "finish_line": pj.get("finish_line") or "",
                   "error": pj.get("error") or "", "counts": (v or {}).get("counts") or {},
                   "failures": [{k: f.get(k) for k in ("row", "verdict", "calls", "lines")}
                                for f in (v or {}).get("failures") or []][:40],
                   "manual": [m.get("row") for m in (v or {}).get("manual") or []],
                   "duration_s": round(max(0.0, time.time() - float(run.get("wall") or time.time())), 1),
                   "auto": bool(run.get("auto")), "cli": bool(run.get("cli"))}
        if s is not None:
            self.c._memory_notes(P, t, s)            # deferred while the run held HEAD
        d = self._d(P, tid)
        d.pop("run", None)
        d["last"] = outcome
        self._save(P, tid, d)
        if result == "ERROR":
            # whatever the runner and the host half rewrote is committed (a clean tree for
            # the next run / the lead); no gate verdict was written (-Finish never ran)
            sha, cerr = self._commit(P, t, run, "ERROR")
            outcome["commit"], outcome["commit_error"] = sha, cerr
            self.store.event(P.id, tid, "studio_calls_error", detail=outcome)
            if run.get("cli"):
                self.c.store.release(P.id, tid, self.c._session_locks(P))
                return outcome
            return self._tooling(P, t, outcome, run, f"scripted Studio gate: {outcome['error']}"
                                 or "scripted Studio gate ERROR") or outcome
        lines_sha = self._lines_sha(P, tid)
        outcome["records"] = self._record(P, t, run, pj, v, lines_sha)
        sha, cerr = self._commit(P, t, run, result)
        outcome["commit"], outcome["commit_error"] = sha, cerr
        text = prompt_text(tid, pj, v, run, sha, settings(P))
        outcome["prompt"] = text
        d = self._d(P, tid)
        d["last"] = {k: x for k, x in outcome.items() if k != "prompt"}
        self._save(P, tid, d)
        self.store.event(P.id, tid, "studio_calls_done", detail={
            k: x for k, x in outcome.items() if k != "prompt"})
        if run.get("cli"):
            return self._after_cli(P, t, outcome)
        if run.get("auto"):
            return self._after_verify(P, t, outcome)
        cur = self.store.task(P.id, tid) or t
        if cur.get("session_id"):
            spec = None
            try:
                spec = P.adapter.describe(tid)
            except Exception:                                     # noqa: BLE001
                pass
            head_txt = ""
            if spec is not None and callable(getattr(P.adapter, "continue_prompt", None)):
                head_txt = P.adapter.continue_prompt(
                    spec, f"the scripted Studio gate {result} at {run['head'][:12]}") + "\n\n"
            outcome["sent"] = self._prompt(P, cur, head_txt + text, f"studio-calls:{run['run_id']}")
        return outcome

    def _prompt(self, P, t: dict, text: str, key: str) -> bool:
        tid = t["task_id"]
        if not t.get("session_id") or self.c._held(P, t, "scripted Studio gate result"):
            return False
        try:
            return bool(self.c._prompt_primary(P, t, text, key))
        except Exception as e:                                    # noqa: BLE001
            self.store.event(P.id, tid, "warning", session_id=t.get("session_id") or "", detail={
                "studio_calls": f"prompt failed ({key}): {type(e).__name__}: {e}"[:400]})
            return False

    def _tooling(self, P, t: dict, outcome: Optional[dict], run: dict, why: str):
        """A tooling ERROR: the task stops BLOCKED with the reason (auto-heal / lead)."""
        tid = t["task_id"]
        self.c.store.release(P.id, tid)
        cur = self.store.task(P.id, tid) or t
        self.c._stop(P, cur, S.BLOCKED, {"blocker": why[:1000], "blocker_kind": "tooling",
                                         "studio_calls": run.get("run_id")},
                     cls="studio_calls_error", blocker=why[:500])
        return outcome

    # ---------------------------------------------------------------- no-session paths
    def _after_cli(self, P, t: dict, outcome: dict) -> dict:
        """`studio-calls run`: the failing rows become the task's findings (the next
        BUILD session gets only those); Studio is released."""
        tid = t["task_id"]
        self.c.store.release(P.id, tid, self.c._session_locks(P))
        if outcome["result"] != "PASS":
            findings = findings_lines(outcome, self._calls_verdict(P, tid, outcome) or {})
            self.store.merge_data(P.id, tid, findings=findings)
            self.store.event(P.id, tid, "studio_calls_findings", detail={"findings": findings[:20]})
        return outcome

    def _after_verify(self, P, t: dict, outcome: dict) -> dict:
        """auto_verify: PASS -> the outputs commit moved HEAD; VERIFYING re-runs at the new
        head and finds the gate fresh. FAIL -> CHANGES_REQUIRED with the failing rows."""
        tid = t["task_id"]
        self.c._release_session(P, t)
        if outcome["result"] != "PASS":
            findings = findings_lines(outcome, self._calls_verdict(P, tid, outcome) or {})
            cur = self.store.task(P.id, tid) or t
            self.store.transition(P.id, tid, S.CHANGES_REQUIRED, {
                "findings": findings, "studio_calls": outcome["run_id"]},
                data={**(cur.get("data") or {}), "findings": findings})
        return outcome

    def run_now(self, P, t: dict, req: dict, poll: float = 10.0, out=print,
                sleep=time.sleep, max_wait_s: Optional[float] = None) -> dict:
        """`foundry studio-calls run`: start now (no session), wait, consume."""
        tid = t["task_id"]
        d = self._d(P, tid)
        before = d.get("last")
        d["pending"] = {"req": req, "session_id": "", "cli": True, "ts": time.time()}
        self._save(P, tid, d)
        if not self._start(P, self.store.task(P.id, tid) or t, d["pending"]):
            d = self._d(P, tid)
            if d.get("pending"):
                d.pop("pending", None)
                self._save(P, tid, d)
            why = (self.store.task(P.id, tid) or {}).get("next_action") or "Studio is busy"
            if d.get("last") != before and (d.get("last") or {}).get("result") == "ERROR":
                self.c.store.release(P.id, tid, self.c._session_locks(P))
                return d["last"]
            self.c.store.release(P.id, tid, self.c._session_locks(P))
            return {"result": "ERROR", "error": f"not started: {why}", "stage": "start"}
        run = self._d(P, tid).get("run") or {}
        out(f"started {run.get('run_id')} at {str(run.get('head'))[:12]}; log {run.get('log')}")
        t0 = time.time()
        while True:
            res = self.tick(P, self.store.task(P.id, tid) or t, None)
            if res is not None:
                return res
            if not self._d(P, tid).get("run"):
                return self._d(P, tid).get("last") or {"result": "ERROR", "error": "no outcome"}
            if max_wait_s is not None and time.time() - t0 > max_wait_s:
                return {"result": "RUNNING", "run_id": run.get("run_id")}
            out(f"  {(self.store.task(P.id, tid) or {}).get('next_action') or 'running'}")
            sleep(poll)

    def verify_hold(self, P, t: dict, spec, head: str) -> bool:
        """[studio_calls] auto_verify: a tier A/B sprint whose gate is missing or stale at
        HEAD gets the scripted run before the project's checks (once per HEAD). True while
        it runs or after it moved the task."""
        tid = t["task_id"]
        s = settings(P)
        if not s.get("auto_verify") or not self.on(P, tid, spec):
            return False
        if self._tier(P, tid, spec) not in ("A", "B"):
            return False
        d = self._d(P, tid)
        if d.get("run") or d.get("pending"):
            self.tick(P, t, None)
            return True
        if d.get("auto_head") == head or self.gate_fresh(P, tid, head):
            return False
        req = self.default_request(P, tid, spec)
        if req is None:
            return False
        d["auto_head"] = head
        d["pending"] = {"req": req, "session_id": "", "auto": True, "ts": time.time()}
        self._save(P, tid, d)
        self.store.event(P.id, tid, "studio_calls_auto", detail={"head": head, "request": req})
        self._start(P, self.store.task(P.id, tid) or t, d["pending"])
        return True

    def gate_fresh(self, P, tid: str, head: str) -> bool:
        """The evidence log's last `GATE <ID>` verdict is PASS and the gate's verdicts
        were stamped at HEAD, or at an ancestor whose diff to HEAD is closeout-exempt."""
        ws = P.profile.workspace
        try:
            with open(os.path.join(ws, "docs", "progress", f"{tid}_EVIDENCE_RAW.log"),
                      encoding="utf-8", errors="replace") as f:
                log = f.read()
        except OSError:
            return False
        last = re.findall(r"^GATE %s (PASS|FAIL)\b" % re.escape(tid), log, re.M)
        if not last or last[-1] != "PASS":
            return False
        v = _read_json(os.path.join(ws, "docs", "progress", f"{tid}_gate_verdicts.json")) or {}
        stamp = str(v.get("headSha") or "")
        if not stamp:
            return False
        if stamp == head:
            return True
        from .profile import matches_any
        try:
            if not P.ws.is_ancestor(stamp, head):
                return False
            changed = P.ws.changed_files(stamp, head)
        except Exception:                                         # noqa: BLE001
            return False
        pats = [x.replace("{task}", tid) for x in EXEMPT]
        return all(matches_any(f, pats) for f in changed)

    def default_request(self, P, tid: str, spec) -> Optional[dict]:
        """The sprint's last host-half parameters (the evidence log's `GATE RUN` line),
        else its own suites (required checks) with no regression."""
        ws = P.profile.workspace
        try:
            with open(os.path.join(ws, "docs", "progress", f"{tid}_EVIDENCE_RAW.log"),
                      encoding="utf-8", errors="replace") as f:
                runs = re.findall(r"^GATE RUN sprint=%s tier=(\w) regression=(\S+) suites=\[([^\]]*)\]"
                                  % re.escape(tid), f.read(), re.M)
        except OSError:
            runs = []
        if runs:
            tier, reg, suites = runs[-1]
            req = {"suites": split_ids(suites), "regression": reg, "tier": tier.upper(),
                   "rerun": "failed", "files": []}
            if req["suites"] or req["regression"] != "none":
                return req
        try:
            req_checks = P.adapter.required_checks(spec, "BUILD") or []
        except Exception:                                         # noqa: BLE001
            req_checks = []
        suites = [str(r["check_id"]).split(":", 1)[1] for r in req_checks
                  if str(r.get("check_id") or "").startswith("suite:")]
        if not suites:
            return None
        return {"suites": suites, "regression": "none", "tier": self._tier(P, tid, spec),
                "rerun": "failed", "files": []}


# ============================================================================ texts

def findings_lines(outcome: dict, v: dict) -> list:
    """One finding per failing row (and the host-half / manual lines): what the next
    session must fix, failures only."""
    tid = (v or {}).get("sprint") or ""
    out = [f"scripted Studio gate {outcome.get('result')} at {str(outcome.get('head'))[:12]} "
           f"(run {outcome.get('run_id')}): " + ", ".join(
               f"{k} {x}" for k, x in (outcome.get("counts") or {}).items()
               if k in ("fresh", "carried", "failed", "unknown", "manual"))]
    for f in (v or {}).get("failures") or []:
        where = (f"; docs/progress/{tid}_studio_lines.txt lines {f['lines'][0]}-{f['lines'][1]}"
                 if f.get("lines") else "")
        calls = f.get("calls") or []
        rng = f"{calls[0]}-{calls[-1]}" if len(calls) > 1 else (str(calls[0]) if calls else "-")
        out.append(f"row {f['row']} {f['verdict']} (CALL {rng}{where}): "
                   + " | ".join([str(f.get("detail") or "")[:200]] + [str(x)[:200] for x in
                                                                       f.get("fail_lines") or []][:5]))
    for m in (v or {}).get("manual") or []:
        out.append(f"row {m.get('row')} MANUAL: {m.get('why')}")
    return out


def prompt_text(tid: str, pj: dict, v: Optional[dict], run: dict, sha: str, s: dict) -> str:
    """The ONE prompt after a run: verdict, counts, failing rows only."""
    res = str(pj.get("result") or "").upper()
    head = str(run.get("head") or "")[:12]
    lines_rel = f"docs/progress/{tid}_studio_lines.txt"
    nfail = max(1, int(s.get("failure_lines") or 5))
    out = [f"## Scripted Studio gate: GATE {tid} {res} at {head} (coordinator run {run.get('run_id')})"]
    if pj.get("stage") == "host":
        out.append("The host half (tools/gate.ps1 without -Finish) did not pass, so no Studio "
                   "CALL ran: " + str(pj.get("host_problem") or ""))
        out += [f"    {x}" for x in (pj.get("host_fail_lines") or [])[:12]]
    elif v:
        c = v.get("counts") or {}
        out.append(f"{pj.get('finish_line') or ''} - rows: fresh {c.get('fresh', 0)}, carried "
                   f"{c.get('carried', 0)}, failed {c.get('failed', 0)}, UNKNOWN "
                   f"{c.get('unknown', 0)}, manual {c.get('manual', 0)} (of {c.get('rows', 0)})")
        for f in v.get("failures") or []:
            calls = f.get("calls") or []
            rng = f"{calls[0]}-{calls[-1]}" if len(calls) > 1 else (str(calls[0]) if calls else "-")
            where = f", {lines_rel} lines {f['lines'][0]}-{f['lines'][1]}" if f.get("lines") else ""
            out.append(f"- {f['row']} {f['verdict']} (CALL {rng}{where}): "
                       f"{str(f.get('detail') or '')[:300]}")
            out += [f"    {str(x)[:300]}" for x in (f.get("fail_lines") or [])[:nfail]]
        other = [x for x in v.get("summary") or [] if x.startswith("CHECK ")]
        out += [f"- {x[6:]}" for x in other[:10]]
        if v.get("manual"):
            out.append("MANUAL rows (never run by the script, never a red gate): "
                       + ", ".join(f"{m['row']} (CALL {m['calls'][0] if m.get('calls') else '-'})"
                                   for m in v["manual"])
                       + f". Each {(v['manual'][0].get('why') or 'needs a 2-player playtest')}: "
                         f"run just these rows yourself with multiplayer_playtest (robloxstudio-mcp, take "
                         f"studio.lock first), append their raw lines to "
                         f"docs/progress/{tid}_EVIDENCE_RAW.log under '=== MANUAL ROWS ===' and "
                         f"commit by explicit path.")
    if sha:
        out.append(f"The coordinator committed the lines, the evidence log and the gate files at "
                   f"{sha[:12]}.")
    if res == "PASS":
        out.append("The gate is green: continue the plan from its NEXT line (the manual rows "
                   "above, if any), then report.")
    else:
        out.append("Fix the cause within the plan's file list, commit by explicit path, then end "
                   "your turn with the same STUDIO_GATE block again: only failed and affected rows "
                   "rerun (passed rows are carried). Never run the CALLs, the host half or -Finish "
                   "yourself, and never weaken a check.")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    sys.exit(main())
