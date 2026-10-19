"""Upgrade supervisor: installs an approved coordinator repair from outside
the coordinator process.

    python -m foundry_coordinator.supervisor --state <dir> --repo <router checkout> \\
        --coordinator-cmd "<cmd>" [--stop-cmd "<cmd>"] [--profiles ...] --once|--loop

It consumes `upgrade_requests` rows in the coordinator DB
(<state>/coordinator.sqlite) and carries each through a journaled step
machine. The row's `status` is the step to run next; it advances only after
the step's effect is durable, and every step is idempotent, so a supervisor
crash resumes from the last completed step and never half-applies:

  waiting_safe_point  pause EVERY project in the store (meta project_pause:<pid>,
                      mode safe, actor "supervisor"; an operator's pause is kept
                      and never cleared) and wait until each is
                      telemetry.project_pause_status(...).safe_to_upgrade
  verifying           the row is re-checked, never trusted: candidate sha present
                      and a fast-forward of the router HEAD (prior_sha, its branch
                      recorded), no dirty tracked files, the manifest's review
                      verdict APPROVED, its files equal to the actual
                      prior..candidate diff, the release identity, and the
                      manifest's focused + regression + broader tests (validated
                      ids, sandboxed env, junit-checked) pass in a temporary
                      worktree at the candidate sha
  stopping            the configured stop command, or the pid file
                      <state>/coordinator.pid: only a VERIFIED process is ever
                      signalled (its recorded creation time / exe match, or a
                      fresh heartbeat with the same pid); a stop-request file
                      (<state>/coordinator.stop {upgrade_id, supervisor_pid};
                      SIGTERM on POSIX) first, then that one process is
                      terminated after --stop-timeout (never a process tree)
  backing_up          the now quiescent DB (sqlite backup API) plus evidence_blobs
                      into <state>/upgrade_backups/<upgrade_id>/
  installing          `git merge --ff-only <candidate_sha>` in the router
                      checkout on the recorded branch (refused when tracked files
                      are dirty); the intent is journaled before the merge
  starting            the coordinator command, detached (Windows: new process
                      group, no console)
  checking            a fresh heartbeat (<state>/coordinator.heartbeat JSON
                      {pid, ts, ok, version|release, lead_ready}) from the pid
                      the started coordinator wrote, that process alive; the
                      manifest's release; lead_ready True when the manifest or a
                      profile enables the lead manager (None fails); the store
                      owner beat counts only as <this host>:<that pid> and only
                      when no release/lead check is due; schema meta versions
                      not lower; the DB opens
  resuming            clear the project pauses the supervisor set
  done

The supervisor reports ONLY through its own `upgrade_requests` row (status,
journal, and on a rollback `log.state.rollback_result`, exposed by
get_request as `rollback_result`). It never writes `recovery_cases`: the lead
manager's UPGRADING step polls get_request and moves the case itself.

A failure before the stop ends the request `failed` (nothing was stopped; the
supervisor's pauses are cleared). A failure at or after the stop runs the
rollback (status `rolling_back`, itself journaled per part):
  * stop whatever verified coordinator is running;
  * the router checkout: untouched when HEAD is still the prior; `git reset
    --keep <prior>` (never --hard: it refuses to discard local changes) only
    when HEAD is the installed candidate on the branch recorded at verify;
    untouched when HEAD moved without an install; anything else (or a
    refused --keep) ends `failed` with rollback_failed for a human;
  * the DB: restored from the backup ONLY when the new build migrated it (the
    schema meta changed), otherwise the current DB is kept (no other
    project's progress is rolled back);
  * restart the old build (a running one it started earlier is adopted);
  * clear the supervisor's pauses (also when the rollback fails), then
    `rolled_back`.

The journal is mirrored to <state>/upgrade_backups/<id>/journal.json, so the
request's own record survives the DB restore (the higher journal seq wins).

Process files: the coordinator should write its pid file with
`write_pid_file` ({pid, created, exe}); a legacy bare-pid file is accepted
but then only a fresh heartbeat with the same pid identifies the process.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from typing import Callable, Optional

from .obligations import migrate_schema
from .store import Store
from .telemetry import PROJECT_PAUSE_META, project_pause_status

SCHEMA_NAME = "schema:supervisor"
_V1 = """
CREATE TABLE IF NOT EXISTS upgrade_requests (
  upgrade_id TEXT PRIMARY KEY, case_id TEXT, project_id TEXT, task_id TEXT, candidate_ref TEXT,
  candidate_sha TEXT, manifest TEXT, status TEXT, prior_sha TEXT, backup_path TEXT, log TEXT,
  created_ts REAL, updated_ts REAL);
CREATE INDEX IF NOT EXISTS idx_upgrade_status ON upgrade_requests(status, created_ts)
"""
_STEPS: list = []

# verify -> stop -> back up the quiescent DB -> install
STEPS = ("waiting_safe_point", "verifying", "stopping", "backing_up", "installing", "starting",
         "checking", "resuming")
TERMINAL = ("done", "rolled_back", "failed", "cancelled")
STATUSES = ("requested",) + STEPS + ("rolling_back",) + TERMINAL
# a failure in one of these steps restores the prior build and state
AFTER_STOP = ("stopping", "backing_up", "installing", "starting", "checking", "resuming")

DB_NAME = "coordinator.sqlite"
PID_FILE = "coordinator.pid"
HEARTBEAT_FILE = "coordinator.heartbeat"
STOP_FILE = "coordinator.stop"
ACTOR = "supervisor"
OWNER_STALE_S = 120           # store.claim_owner's stale window
HB_FRESH_S = 120              # a heartbeat this recent identifies a running coordinator

CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
# a hidden console instead of none: with DETACHED_PROCESS a venv launcher's real
# interpreter (and every console tool it runs) would pop a visible window
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_NO_WINDOW = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}


class StepFailed(RuntimeError):
    pass


class StepPending(Exception):
    """The step can't finish yet (no safe point); try again later."""


def migrate(store) -> int:
    return migrate_schema(store, SCHEMA_NAME, _V1, _STEPS)


def _j(v) -> str:
    return json.dumps(v, default=str, sort_keys=True)


def _uj(v, default):
    try:
        out = json.loads(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return out if out is not None else default


def request_upgrade(store, *, case_id: str, project_id: str, task_id: str,
                    candidate_sha: str, manifest: dict, candidate_ref: str = "") -> str:
    """Queue an upgrade (idempotent per case and candidate sha). The only way
    a request is created: the row carries the supervisor's journal object."""
    migrate(store)
    uid = f"up-{case_id}-{candidate_sha[:12]}"
    now = time.time()
    store.x("INSERT OR IGNORE INTO upgrade_requests (upgrade_id, case_id, project_id, task_id, "
            "candidate_ref, candidate_sha, manifest, status, prior_sha, backup_path, log, "
            "created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, case_id, project_id, task_id, candidate_ref or f"lead/{case_id}", candidate_sha,
             _j(manifest or {}), "requested", "", "", _j({"seq": 0, "entries": [], "state": {}}),
             now, now))
    return uid


def cancel_request(store, upgrade_id: str, reason: str = "") -> bool:
    """Cancel an upgrade the supervisor has not started (status 'requested'
    only), in one guarded statement; a request already in progress is left to
    the supervisor. True when it was cancelled."""
    migrate(store)
    before = store.q("SELECT status FROM upgrade_requests WHERE upgrade_id=?", (upgrade_id,))
    if not before or before[0]["status"] != "requested":
        return False
    store.x("UPDATE upgrade_requests SET status='cancelled', updated_ts=? "
            "WHERE upgrade_id=? AND status='requested'", (time.time(), upgrade_id))
    after = store.q("SELECT status FROM upgrade_requests WHERE upgrade_id=?", (upgrade_id,))
    ok = bool(after) and after[0]["status"] == "cancelled"
    if ok and reason:
        try:
            store.event("", "", "upgrade_cancelled", detail={"upgrade_id": upgrade_id,
                                                            "reason": reason[:300]})
        except Exception:                                        # noqa: BLE001
            pass
    return ok


def get_request(store, upgrade_id: str) -> Optional[dict]:
    migrate(store)
    r = store.q("SELECT * FROM upgrade_requests WHERE upgrade_id=?", (upgrade_id,))
    if not r:
        return None
    r[0]["manifest"] = _uj(r[0]["manifest"], {})
    lg = _uj(r[0]["log"], {"seq": 0, "entries": [], "state": {}})
    if not isinstance(lg, dict):                 # never a list: the journal is an object
        lg = {"seq": 0, "entries": [], "state": {}}
    r[0]["log"] = lg
    st = lg.get("state") if isinstance(lg.get("state"), dict) else {}
    r[0]["rollback_result"] = st.get("rollback_result")
    r[0]["failure"] = st.get("failure")
    return r[0]


# --------------------------------------------------------------------------- processes

_K32 = None


def _kernel32():
    """kernel32 with explicit signatures (64-bit handles), cached."""
    global _K32
    if _K32 is None:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k.GetProcessTimes.restype = wintypes.BOOL
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                 wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        _K32 = k
    return _K32


_SYNCHRONIZE = 0x00100000
_QUERY_LIMITED = 0x1000
_WAIT_TIMEOUT = 0x102


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = _kernel32()
        h = k.OpenProcess(_SYNCHRONIZE | _QUERY_LIMITED, False, int(pid))
        if not h:
            return ctypes.get_last_error() == 5          # access denied: it exists
        try:
            # signalled = exited (an exit code of 259 is not "still active")
            return k.WaitForSingleObject(h, 0) == _WAIT_TIMEOUT
        finally:
            k.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                                 # a zombie child is dead
        wpid, _ = os.waitpid(int(pid), os.WNOHANG)
        return wpid == 0
    except ChildProcessError:
        return True


def process_identity(pid: int) -> Optional[dict]:
    """{created (epoch seconds), exe} of a running process, or None when it
    can't be read. Two readings of the same process are equal; a reused pid
    has another creation time."""
    if not pid or pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k = _kernel32()
        h = k.OpenProcess(_QUERY_LIMITED, False, int(pid))
        if not h:
            return None
        try:
            ft = [wintypes.FILETIME() for _ in range(4)]
            if not k.GetProcessTimes(h, *[ctypes.byref(f) for f in ft]):
                return None
            c = ft[0]
            created = ((c.dwHighDateTime << 32) | c.dwLowDateTime) / 1e7 - 11644473600.0
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(32768)
            exe = buf.value if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
            return {"created": round(created, 6), "exe": exe}
        finally:
            k.CloseHandle(h)
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
        start = int(data[data.rindex(")") + 2:].split()[19])
        btime = 0
        with open("/proc/stat", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("btime "):
                    btime = int(line.split()[1])
        created = btime + start / os.sysconf("SC_CLK_TCK")
        try:
            exe = os.readlink(f"/proc/{int(pid)}/exe")
        except OSError:
            exe = ""
        return {"created": round(created, 6), "exe": exe}
    except (OSError, ValueError, IndexError):
        return {"created": None, "exe": ""} if pid_alive(pid) else None


def write_pid_file(path: str, pid: Optional[int] = None, *, exclusive: bool = False) -> dict:
    """Write the coordinator pid file: JSON {pid, created, exe, host,
    written_ts} for `pid` (default: this process), atomically. exclusive:
    only when no pid file exists (FileExistsError otherwise). The coordinator
    (cli.py _ProcessFiles) should call this at start."""
    pid = int(pid or os.getpid())
    ident = process_identity(pid) or {}
    rec = {"pid": pid, "created": ident.get("created"),
           "exe": ident.get("exe") or (sys.executable if pid == os.getpid() else ""),
           "host": socket.gethostname(), "written_ts": time.time()}
    text = json.dumps(rec)
    if exclusive:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        return rec
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return rec


def read_pid_file(path: str) -> Optional[dict]:
    """{pid, created, exe, legacy, mtime} from a pid file (JSON, or a legacy
    bare pid: created None, legacy True), or None."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    try:
        d = json.loads(text) if text else None
    except ValueError:
        d = None
    try:
        if isinstance(d, dict):
            c = d.get("created")
            return {"pid": int(d.get("pid") or 0), "created": float(c) if c is not None else None,
                    "exe": str(d.get("exe") or ""), "legacy": False, "mtime": mtime}
        pid = int(text.split()[0])
    except (TypeError, ValueError, IndexError):
        return None
    return {"pid": pid, "created": None, "exe": "", "legacy": True, "mtime": mtime}


def pid_matches(rec: dict) -> Optional[bool]:
    """True: the running process is the one the record names (creation time
    and exe match); False: that pid is dead or now another process; None:
    can't tell (a legacy record, or the identity is unreadable)."""
    pid = int((rec or {}).get("pid") or 0)
    if not pid_alive(pid):
        return False
    if rec.get("created") is None:
        return None
    ident = process_identity(pid)
    if not ident or ident.get("created") is None:
        return None
    if abs(float(ident["created"]) - float(rec["created"])) > 0.05:
        return False
    if rec.get("exe") and ident.get("exe") and \
            os.path.normcase(rec["exe"]) != os.path.normcase(ident["exe"]):
        return False
    return True


def _git(cwd: str, *args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       stdin=subprocess.DEVNULL, **_NO_WINDOW)
    if check and r.returncode != 0:
        raise StepFailed(f"git {' '.join(args[:3])} failed: {(r.stderr or r.stdout).strip()[:600]}")
    return r


def _split(cmd):
    if isinstance(cmd, (list, tuple)):
        return list(cmd)
    return cmd if os.name == "nt" else shlex.split(cmd)


def schema_versions(db_path: str) -> dict:
    """{meta key: int} for every schema version the DB records."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    try:
        rows = con.execute("SELECT key, value FROM meta").fetchall()
        con.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    finally:
        con.close()
    out = {}
    for k, v in rows:
        if k == "schema_version" or str(k).startswith("schema:") or str(k).startswith("schema_"):
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                continue
    return out


def _profile_enables_lead(path: str) -> bool:
    try:
        import tomllib
        with open(os.path.expanduser(path), "rb") as fh:
            d = tomllib.load(fh)
    except (OSError, ValueError):
        return False
    return bool((d.get("lead_manager") or {}).get("enabled"))


# --------------------------------------------------------------------------- supervisor

class Supervisor:
    def __init__(self, state_dir: str, repo: str, coordinator_cmd, *, stop_cmd=None,
                 python: Optional[str] = None, profiles: Optional[list] = None,
                 stop_timeout: float = 30.0, health_timeout: float = 120.0,
                 safe_timeout: float = 3600.0, safe_wait: float = 0.0,
                 test_timeout: float = 900.0, poll: float = 0.2,
                 heartbeat_file: Optional[str] = None,
                 hook: Optional[Callable[[str, str, dict], None]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], float] = time.time):
        self.state_dir = os.path.abspath(os.path.expanduser(state_dir))
        self.repo = os.path.abspath(_git(os.path.abspath(repo), "rev-parse",
                                         "--show-toplevel").stdout.strip())
        self.coordinator_cmd = coordinator_cmd
        self.stop_cmd = stop_cmd
        self.python = python or sys.executable
        self.profiles = list(profiles or [])
        self.stop_timeout, self.health_timeout = float(stop_timeout), float(health_timeout)
        self.safe_timeout, self.safe_wait = float(safe_timeout), float(safe_wait)
        self.test_timeout, self.poll = float(test_timeout), float(poll)
        self.db_path = os.path.join(self.state_dir, DB_NAME)
        self.pid_path = os.path.join(self.state_dir, PID_FILE)
        self.hb_path = heartbeat_file or os.path.join(self.state_dir, HEARTBEAT_FILE)
        self.stop_path = os.path.join(self.state_dir, STOP_FILE)
        self.backup_root = os.path.join(self.state_dir, "upgrade_backups")
        self.hook = hook
        self.log = log or (lambda m: print(time.strftime("%H:%M:%S"), "[supervisor]", m,
                                           flush=True))
        self.clock = clock
        self._procs: dict[int, subprocess.Popen] = {}
        self._proc_ts: dict[int, float] = {}
        self._stale_pid: Optional[dict] = None
        os.makedirs(self.state_dir, exist_ok=True)
        self.store = Store(self.db_path)
        migrate(self.store)

    def close(self) -> None:
        try:
            self.store.conn.close()
        except Exception:                                        # noqa: BLE001
            pass

    # -- journal ------------------------------------------------------------------

    def _journal_path(self, uid: str) -> str:
        return os.path.join(self.backup_root, uid, "journal.json")

    def load(self, uid: str) -> Optional[dict]:
        """The request, reconciled with its journal mirror (the higher seq
        wins: after a DB restore the mirror is ahead)."""
        req = get_request(self.store, uid)
        side = None
        try:
            with open(self._journal_path(uid), encoding="utf-8") as fh:
                side = json.load(fh)
        except (OSError, ValueError):
            pass
        if side and (req is None or int(side["log"].get("seq") or 0)
                     > int(req["log"].get("seq") or 0)):
            self._write_row(side)
            req = get_request(self.store, uid)
        return req

    def _write_row(self, req: dict) -> None:
        # (rollback_result / failure are views of log.state, not columns)
        self.store.x("INSERT OR REPLACE INTO upgrade_requests (upgrade_id, case_id, project_id, "
                     "task_id, candidate_ref, candidate_sha, manifest, status, prior_sha, "
                     "backup_path, log, created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (req["upgrade_id"], req["case_id"], req["project_id"], req["task_id"],
                      req["candidate_ref"], req["candidate_sha"], _j(req["manifest"]),
                      req["status"], req.get("prior_sha") or "", req.get("backup_path") or "",
                      _j(req["log"]), req["created_ts"], req["updated_ts"]))

    def _save(self, req: dict, step: str, event: str, detail: Optional[dict] = None,
              status: Optional[str] = None) -> None:
        """One journal entry (and optionally the new status), mirror first."""
        lg = req["log"]
        lg["seq"] = int(lg.get("seq") or 0) + 1
        lg.setdefault("entries", []).append({"seq": lg["seq"], "ts": self.clock(), "step": step,
                                             "event": event, "detail": detail or {}})
        lg["entries"] = lg["entries"][-200:]
        if status:
            req["status"] = status
        req["updated_ts"] = self.clock()
        path = self._journal_path(req["upgrade_id"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(req, fh, default=str, sort_keys=True)
        os.replace(tmp, path)
        self._write_row(req)
        self.log(f"{req['upgrade_id']}: {step} {event}"
                 + (f" -> {status}" if status else "")
                 + (f" ({detail.get('reason')})" if detail and detail.get("reason") else ""))

    def _hook(self, step: str, when: str, req: dict) -> None:
        if self.hook:
            self.hook(step, when, req)

    # -- driver --------------------------------------------------------------------

    def pending(self) -> list[str]:
        return [r["upgrade_id"] for r in self.store.q(
            "SELECT upgrade_id FROM upgrade_requests WHERE status NOT IN ("
            + ",".join("?" * len(TERMINAL)) + ") ORDER BY created_ts", TERMINAL)]

    def run_once(self) -> list[dict]:
        """Advance every open request as far as it goes now."""
        out = []
        with _Lock(os.path.join(self.state_dir, "supervisor.lock")):
            for uid in self.pending():
                out.append(self.run(uid))
                if out[-1]["status"] not in TERMINAL:
                    break                     # one upgrade at a time
        return out

    def loop(self, interval: float = 5.0) -> None:
        while True:
            self.run_once()
            time.sleep(interval)

    def run(self, uid: str) -> dict:
        req = self.load(uid)
        if req is None:
            raise KeyError(uid)
        while req["status"] not in TERMINAL:
            if req["status"] == "requested":
                self._save(req, "requested", "accepted", status=STEPS[0])
            step = req["status"]
            if step == "rolling_back":
                self._rollback(req)
                continue
            if step not in STEPS:
                self._save(req, step, "unknown status", status="failed")
                break
            try:
                detail = getattr(self, "_step_" + step)(req) or {}
            except StepPending as e:
                self._note(req, step, "pending", {"reason": str(e)})
                return self._summary(req)
            except Exception as e:                               # noqa: BLE001
                reason = f"{type(e).__name__}: {e}" if not isinstance(e, StepFailed) else str(e)
                req["log"].setdefault("state", {})["failure"] = {"step": step,
                                                                 "reason": reason[:1000]}
                if step in AFTER_STOP:
                    self._save(req, step, "failed", {"reason": reason[:1000]},
                               status="rolling_back")
                else:
                    released = self._release_pause(req)
                    self._save(req, step, "failed", {"reason": reason[:1000], "stopped": False,
                                                     "pauses_released": released},
                               status="failed")
                continue
            self._hook(step, "done", req)
            nxt = STEPS[STEPS.index(step) + 1] if step != STEPS[-1] else "done"
            self._save(req, step, "done", detail, status=nxt)
            self._hook(step, "advanced", req)
        return self._summary(req)

    def _note(self, req, step, event, detail):
        # repeated "pending" notes don't flood the journal
        last = (req["log"].get("entries") or [{}])[-1]
        if last.get("step") == step and last.get("event") == event \
                and last.get("detail") == detail:
            return
        self._save(req, step, event, detail)

    def _summary(self, req) -> dict:
        return {"upgrade_id": req["upgrade_id"], "status": req["status"],
                "prior_sha": req.get("prior_sha"), "candidate_sha": req["candidate_sha"],
                "failure": req["log"].get("state", {}).get("failure")}

    # -- steps ---------------------------------------------------------------------

    def _pause_all(self, req, st) -> bool:
        """Pause every registered project not yet seen (an operator's pause
        is recorded as not ours). True when a new project was paused/seen."""
        pauses = st.setdefault("pauses", {})
        new = False
        for proj in self.store.projects():
            p = proj["project_id"]
            if p in pauses:
                continue
            new = True
            if proj["paused"]:
                pauses[p] = False                 # someone else's pause: never cleared by us
                continue
            meta = {"mode": "safe", "note": f"supervisor upgrade {req['upgrade_id']}",
                    "actor": ACTOR, "since": self.clock(), "tasks": [],
                    "resume_after_upgrade": False, "version": {},
                    "upgrade_id": req["upgrade_id"]}
            # record first, then the flag the engine checks (control.py order)
            self.store.set_meta(PROJECT_PAUSE_META + p, _j(meta))
            self.store.set_project_paused(p, True, meta["note"])
            pauses[p] = True
        st["pause_owned"] = bool(pauses.get(req["project_id"]))
        return new

    def _step_waiting_safe_point(self, req) -> dict:
        st = req["log"].setdefault("state", {})
        pid = req["project_id"]
        if self.store.project(pid) is None:
            raise StepFailed(f"project {pid} is not registered in the coordinator store")
        if "safe_since" not in st:
            st["safe_since"] = self.clock()
        deadline = time.time() + self.safe_wait
        while True:
            if self._pause_all(req, st):
                self._save(req, "waiting_safe_point", "paused",
                           {"owned": st["pause_owned"], "pauses": dict(st["pauses"])})
            stats = {p: project_pause_status(self.store.q, p, self.clock()) for p in st["pauses"]}
            if all(s["safe_to_upgrade"] for s in stats.values()):
                s = stats.get(pid) or {}
                return {"pause_status": {k: s.get(k) for k in ("busy_sessions", "running_helpers",
                                                               "running_reviews")},
                        "projects": sorted(stats)}
            cleared = sorted(p for p, s in stats.items() if not s["paused"])
            if cleared:
                raise StepFailed(f"the project pause of {', '.join(cleared)} was cleared while "
                                 f"waiting for a safe point")
            busy = {p: s for p, s in stats.items() if not s["safe_to_upgrade"]}
            why = "; ".join(f"{p}: {s['busy_sessions']} session(s), {s['running_helpers']} "
                            f"helper(s), {s['running_reviews']} review(s) running"
                            for p, s in sorted(busy.items()))
            if self.clock() - st["safe_since"] > self.safe_timeout:
                raise StepFailed(f"no safe point within {self.safe_timeout:.0f}s ({why})")
            if time.time() >= deadline:
                raise StepPending(f"not yet safe: {why}")
            time.sleep(self.poll)

    def _tracked_dirty(self) -> str:
        return _git(self.repo, "status", "--porcelain", "--untracked-files=no").stdout.strip()

    def _branch(self) -> str:
        return _git(self.repo, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()

    def _step_verifying(self, req) -> dict:
        from . import repair as RP
        st = req["log"]["state"]
        if not req.get("prior_sha"):
            req["prior_sha"] = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
            st["prior_branch"] = self._branch()
        st.setdefault("prior_branch", self._branch())
        sha = req["candidate_sha"]
        man = req["manifest"] or {}
        r = _git(self.repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False)
        if r.returncode != 0:
            raise StepFailed(f"candidate {sha[:12]} is not in the router repository")
        full = r.stdout.strip()
        if not full.startswith(sha.lower()) and full != sha:
            raise StepFailed(f"candidate {sha} resolves to {full}")
        if man.get("candidate_sha") and man["candidate_sha"] != full:
            raise StepFailed(f"manifest names {man['candidate_sha'][:12]}, not {full[:12]}")
        st["candidate_full"] = full
        prior = req["prior_sha"]
        if _git(self.repo, "merge-base", "--is-ancestor", prior, full, check=False).returncode:
            raise StepFailed(f"candidate {full[:12]} is not a fast-forward of {prior[:12]}")
        dirty = self._tracked_dirty()
        if dirty:
            raise StepFailed(f"router checkout has dirty tracked files: {dirty[:300]}")
        release = self._release_at(full)
        if man.get("release") and release and man["release"] != release:
            raise StepFailed(f"release identity {release!r} != manifest {man['release']!r}")
        tests = man.get("tests") if isinstance(man.get("tests"), dict) else {}
        focused = list(tests.get("focused") or [])
        broader = list(tests.get("broader") or [])
        regression = tests.get("regression") or ""
        if not focused or not broader or not regression:
            raise StepFailed("the manifest names no tests to re-run (focused, broader and the "
                             "regression test are all required)")
        try:
            RP.check_test_ids([regression], require_node=True)
            ids = RP.check_test_ids(focused + [regression] + broader)
        except RP.RepairError as e:
            raise StepFailed(f"the manifest's test ids are refused: {e}") from None
        # the row is data: re-check what the lead's gate claims
        if man.get("review_verdict") != "APPROVED":
            raise StepFailed(f"the manifest's review verdict is "
                             f"{man.get('review_verdict') or 'missing'!r}, not APPROVED")
        actual = sorted(f for f in _git(self.repo, "diff", "--name-only", "-z", "--no-renames",
                                        prior, full).stdout.split("\0") if f)
        if sorted(man.get("files") or []) != actual:
            raise StepFailed(f"the manifest's files {sorted(man.get('files') or [])[:20]} are not "
                             f"the actual {prior[:12]}..{full[:12]} diff {actual[:20]}")
        self.stop_target()                     # resolvable before anything is stopped
        res = self._run_tests_at(req, full, ids)
        if not res["ok"]:
            raise StepFailed(f"manifest tests failed at the candidate (rc {res['rc']}): "
                             f"{'; '.join(res.get('problems') or [])[:400]} "
                             f"{res['log'][-600:]}")
        return {"candidate": full, "release": release, "tests": ids,
                "seconds": res["seconds"], "prior_sha": prior,
                "prior_branch": st.get("prior_branch")}

    def _release_at(self, sha: str) -> str:
        r = _git(self.repo, "show", f"{sha}:foundry_router/__init__.py", check=False)
        if r.returncode:
            return ""
        for line in r.stdout.splitlines():
            if line.startswith("__version__"):
                return line.split("=", 1)[1].strip().strip("\"'")
        return ""

    def _run_tests_at(self, req, sha: str, ids: list[str]) -> dict:
        from . import repair as RP
        wt = os.path.join(self.backup_root, req["upgrade_id"], f"verify-{uuid.uuid4().hex[:8]}")
        _git(self.repo, "worktree", "add", "--detach", wt, sha)
        try:
            return RP.run_pytest(self.python, wt, ids, self.test_timeout)
        finally:
            _git(self.repo, "worktree", "remove", "--force", wt, check=False)
            _git(self.repo, "worktree", "prune", check=False)
            shutil.rmtree(wt, ignore_errors=True)

    # -- process identification -----------------------------------------------------

    def _read_pid(self) -> int:
        rec = read_pid_file(self.pid_path)
        return int(rec["pid"]) if rec else 0

    def _alive(self, pid: int) -> bool:
        p = self._procs.get(pid)
        if p is not None:
            return p.poll() is None
        return pid_alive(pid)

    def _hb(self) -> Optional[dict]:
        try:
            with open(self.hb_path, encoding="utf-8") as fh:
                hb = json.load(fh)
        except (OSError, ValueError):
            return None
        return hb if isinstance(hb, dict) else None

    def _hb_fresh_for(self, pid: int) -> bool:
        hb = self._hb()
        try:
            return bool(hb and int(hb.get("pid") or 0) == int(pid)
                        and float(hb.get("ts") or 0) >= time.time() - HB_FRESH_S)
        except (TypeError, ValueError):
            return False

    def verify_pid(self, rec: Optional[dict]) -> str:
        """Whether the process a pid-file record names is the coordinator:
        verified | dead | reused (that pid is another process now) |
        unverified (alive, but nothing proves it is ours)."""
        pid = int((rec or {}).get("pid") or 0)
        if not pid or not self._alive(pid):
            return "dead"
        p = self._procs.get(pid)
        if p is not None and p.poll() is None:
            return "verified"                    # a child this supervisor started
        m = pid_matches(rec)
        if m is True:
            return "verified"
        if m is False:
            return "reused"
        if self._hb_fresh_for(pid):
            return "verified"
        # a legacy record written after a coordinator this supervisor started
        for cpid, q in self._procs.items():
            if q.poll() is None and rec.get("mtime", 0) >= self._proc_ts.get(cpid, 1e18) - 1:
                return "verified"
        return "unverified"

    def _wait_dead(self, pid: int, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if not self._alive(pid):
                return True
            time.sleep(self.poll)
        return not self._alive(pid)

    def stop_target(self) -> tuple[int, str]:
        """The coordinator process to stop: the pid file's process when it is
        verified, else the store's owner record (host:pid, heart-beating) when
        it is on this host and the heartbeat file names the same pid.
        StepFailed when a running process can't be verified as the
        coordinator (configure --stop-cmd, or stop it by hand). A dead or
        reused pid in the pid file is ignored (never signalled)."""
        self._stale_pid = None
        rec = read_pid_file(self.pid_path)
        if rec and rec["pid"]:
            v = self.verify_pid(rec)
            if v == "verified":
                return rec["pid"], "pid_file"
            if v == "unverified":
                if self.stop_cmd:
                    return 0, "stop_cmd"
                raise StepFailed(f"the pid file names pid {rec['pid']}, which is running but "
                                 f"can't be verified as the coordinator (no matching process "
                                 f"identity, no fresh heartbeat with that pid): refusing to "
                                 f"signal it; stop it by hand or configure --stop-cmd")
            self._stale_pid = {"pid": rec["pid"], "why": v}
        try:
            owner = _uj(self.store.meta("owner"), {})
        except Exception:                                        # noqa: BLE001
            owner = {}
        fresh = time.time() - float(owner.get("beat") or 0) < OWNER_STALE_S
        if not owner or not fresh:
            return 0, "none"
        host, _, opid = str(owner.get("id") or "").rpartition(":")
        if host == socket.gethostname() and opid.isdigit() and self._alive(int(opid)) \
                and self._hb_fresh_for(int(opid)):
            return int(opid), "store_owner"
        if self.stop_cmd:
            return 0, "stop_cmd"
        raise StepFailed(f"a coordinator ({owner.get('id')}) is heart-beating on this store but "
                         f"no pid file identifies it here: configure --stop-cmd")

    def _terminate(self, pid: int) -> None:
        """Terminate exactly one process (never its tree)."""
        try:
            if os.name == "nt":
                os.kill(pid, signal.SIGTERM)             # TerminateProcess on this pid only
            else:
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    def stop_coordinator(self, upgrade_id: str = "") -> dict:
        """Stop whatever verified coordinator runs on this state dir. Graceful
        first (the stop command, or a stop-request file / SIGTERM), then that
        one process is terminated after stop_timeout, re-verified first."""
        pid, source = self.stop_target()
        ident0 = process_identity(pid) if pid else None
        out = {"pid": pid, "source": source, "was_running": bool(pid and self._alive(pid)),
               "how": "none"}
        if self._stale_pid:
            out["ignored_pid"] = dict(self._stale_pid)
        if self.stop_cmd:
            r = subprocess.run(_split(self.stop_cmd), cwd=self.repo, capture_output=True,
                               text=True, timeout=self.stop_timeout + 30,
                               stdin=subprocess.DEVNULL, **_NO_WINDOW)
            out.update(how="stop_cmd", stop_rc=r.returncode)
            if pid and not self._wait_dead(pid, self.stop_timeout):
                raise StepFailed(f"coordinator pid {pid} still running after the stop command")
        elif out["was_running"]:
            tmp = self.stop_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"upgrade_id": upgrade_id, "supervisor_pid": os.getpid(), "pid": pid,
                           "ts": time.time()}, fh)
            os.replace(tmp, self.stop_path)
            if os.name != "nt":
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            if self._wait_dead(pid, self.stop_timeout):
                out["how"] = "graceful"
            else:
                ident1 = process_identity(pid)
                if ident0 and ident1 and ident0.get("created") is not None \
                        and ident0.get("created") != ident1.get("created"):
                    raise StepFailed(f"pid {pid} changed identity while stopping: not signalled")
                out["how"] = "killed"
                self._terminate(pid)
                if not self._wait_dead(pid, 15):
                    raise StepFailed(f"could not stop coordinator pid {pid}")
        # children this supervisor started (a venv launcher in front of the
        # coordinator) end with it; reap them, terminating only our own
        stopped = bool(pid) and not self._alive(pid)
        for cpid, p in list(self._procs.items()):
            if p.poll() is None:
                if not stopped:
                    continue
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()                     # our own child, nothing else
                    try:
                        p.wait(5)
                    except subprocess.TimeoutExpired:
                        continue
            self._procs.pop(cpid, None)
            self._proc_ts.pop(cpid, None)
        try:
            os.remove(self.stop_path)
        except OSError:
            pass
        rec = read_pid_file(self.pid_path)
        if rec is None or rec["pid"] in (pid, (self._stale_pid or {}).get("pid")) \
                or not self._alive(rec["pid"]):
            try:
                os.remove(self.pid_path)
            except OSError:
                pass
        # a killed coordinator never released the store: drop its owner record
        # (only when that process is on this host and gone) so the next one
        # can claim the store without --force
        try:
            owner = _uj(self.store.meta("owner"), {})
            host, _, opid = str(owner.get("id") or "").rpartition(":")
            if owner and host == socket.gethostname() and opid.isdigit() \
                    and not self._alive(int(opid)):
                self.store.x("DELETE FROM meta WHERE key='owner'")
                out["owner_released"] = owner.get("id")
        except Exception:                                        # noqa: BLE001
            pass
        return out

    def start_coordinator(self, tag: str) -> dict:
        """Start the coordinator command detached, cwd = router checkout."""
        rec = read_pid_file(self.pid_path)
        if rec and rec["pid"]:
            v = self.verify_pid(rec)
            if v == "verified":
                raise StepFailed(f"a coordinator (pid {rec['pid']}) is already running")
            if v == "unverified":
                raise StepFailed(f"pid {rec['pid']} from the pid file is running and can't be "
                                 f"verified: refusing to start a second coordinator")
        for f in (self.hb_path, self.pid_path, self.stop_path):
            try:
                os.remove(f)
            except OSError:
                pass
        logf = open(os.path.join(self.state_dir, "coordinator.out.log"), "ab")
        kw = {"cwd": self.repo, "stdout": logf, "stderr": subprocess.STDOUT,
              "stdin": subprocess.DEVNULL,
              "env": dict(os.environ, FOUNDRY_COORD_STATE=self.state_dir,
                          FOUNDRY_SUPERVISOR_TAG=tag)}
        if os.name == "nt":
            kw["creationflags"] = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        started = self.clock()
        try:
            p = subprocess.Popen(_split(self.coordinator_cmd), **kw)
        finally:
            logf.close()
        self._procs[p.pid] = p
        self._proc_ts[p.pid] = time.time()
        # the pid file, unless the coordinator already wrote its own (a venv
        # launcher may stand in front of the real interpreter on Windows)
        try:
            write_pid_file(self.pid_path, p.pid, exclusive=True)
        except FileExistsError:
            pass
        return {"pid": p.pid, "started_ts": started}

    def _step_stopping(self, req) -> dict:
        return self.stop_coordinator(req["upgrade_id"])

    def _step_backing_up(self, req) -> dict:
        # after the stop: the DB is quiescent
        st = req["log"]["state"]
        bdir = os.path.join(self.backup_root, req["upgrade_id"])
        os.makedirs(bdir, exist_ok=True)
        dst_db = os.path.join(bdir, DB_NAME)
        tmp = dst_db + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        dst = sqlite3.connect(tmp)
        try:
            self.store.conn.backup(dst)
        finally:
            dst.close()
        os.replace(tmp, dst_db)
        blobs = os.path.join(self.state_dir, "evidence_blobs")
        bblobs = os.path.join(bdir, "evidence_blobs")
        n = 0
        if os.path.isdir(blobs):
            shutil.copytree(blobs, bblobs, dirs_exist_ok=True)
            n = sum(len(f) for _, _, f in os.walk(bblobs))
        req["backup_path"] = bdir
        st["schema_before"] = schema_versions(dst_db)
        return {"backup": bdir, "blobs": n, "prior_sha": req["prior_sha"],
                "schema": st["schema_before"]}

    def _step_installing(self, req) -> dict:
        st = req["log"]["state"]
        sha = st.get("candidate_full") or req["candidate_sha"]
        head = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        if head == sha or head.startswith(req["candidate_sha"]):
            return {"merged": False, "head": head, "note": "already installed"}
        if head != req["prior_sha"]:
            raise StepFailed(f"router HEAD {head[:12]} is neither the prior {req['prior_sha'][:12]} "
                             f"nor the candidate")
        branch = self._branch()
        if branch != st.get("prior_branch", branch):
            raise StepFailed(f"the router checkout is on {branch or 'a detached HEAD'}, not "
                             f"{st.get('prior_branch') or 'the detached HEAD'} as at verify")
        dirty = self._tracked_dirty()
        if dirty:
            raise StepFailed(f"router checkout has dirty tracked files: {dirty[:300]}")
        st["install_started"] = {"from": head, "branch": branch, "to": sha}
        self._save(req, "installing", "merging", st["install_started"])
        _git(self.repo, "merge", "--ff-only", sha)
        return {"merged": True, "head": _git(self.repo, "rev-parse", "HEAD").stdout.strip()}

    def _running_since(self, s: dict) -> bool:
        """A coordinator started at s (s = {pid, started_ts}) still runs: our
        own child, or a pid file written since it started whose process is
        verified."""
        pid = int(s.get("pid") or 0)
        p = self._procs.get(pid)
        if p is not None and p.poll() is None:
            return True
        rec = read_pid_file(self.pid_path)
        if not rec or rec["mtime"] < float(s.get("started_ts") or 0) - 1:
            return False
        return self.verify_pid(rec) == "verified"

    def _step_starting(self, req) -> dict:
        st = req["log"]["state"]
        s = st.get("started")
        if s and self._running_since(s):
            return dict(s, note="already running")
        s = self.start_coordinator(f"{req['upgrade_id']}:new")
        st["started"] = s
        return s

    def health(self, started_ts: float, pid: int, timeout: float, *,
               expect_release: str = "", require_lead: bool = False) -> dict:
        """A fresh heartbeat from the started build within the timeout: its
        pid is the one written to the pid file after the start, that process
        is alive, and (when asked) it reports `expect_release` and
        lead_ready True. The store's owner beat counts only for
        <this host>:<that pid>, and only when no release/lead check is due."""
        end = time.time() + timeout
        host = socket.gethostname()
        last = "no heartbeat"
        while True:
            rec = read_pid_file(self.pid_path)
            spid = int(rec["pid"]) if rec and rec["pid"] and rec["mtime"] >= started_ts - 1 else 0
            hb = self._hb()
            if hb is not None and float(hb.get("ts") or 0) >= started_ts:
                try:
                    hpid = int(hb.get("pid") or 0)
                except (TypeError, ValueError):
                    hpid = 0
                if spid and hpid == spid and self._alive(hpid):
                    if hb.get("ok") is False:
                        raise StepFailed(f"health check reported not ok: "
                                         f"{str(hb.get('detail'))[:300]}")
                    if expect_release:
                        rel = str(hb.get("release") or hb.get("version") or "")
                        if rel != expect_release:
                            raise StepFailed(f"the started build reports release {rel!r}, not "
                                             f"the manifest's {expect_release!r}")
                    if require_lead and hb.get("lead_ready") is not True:
                        raise StepFailed(
                            "lead readiness failed: " + str(hb.get("lead_detail"))[:300]
                            if hb.get("lead_ready") is False else
                            "lead readiness unknown (lead_ready "
                            f"{hb.get('lead_ready')!r}: the readiness check errored or no "
                            f"project enables the lead): {str(hb.get('lead_detail'))[:200]}")
                    return {"source": "heartbeat", "heartbeat": hb, "pid": hpid}
                last = (f"heartbeat from pid {hpid}, not the started coordinator "
                        f"(pid file: {spid or 'none written since the start'})")
            if spid and not expect_release and not require_lead:
                try:
                    owner = _uj(self.store.meta("owner"), {})
                except Exception:                                # noqa: BLE001
                    owner = {}
                if owner.get("id") == f"{host}:{spid}" and \
                        float(owner.get("beat") or 0) >= started_ts and self._alive(spid):
                    return {"source": "owner", "owner": owner, "pid": spid}
            if pid and not self._alive(pid) and not (spid and self._alive(spid)):
                raise StepFailed(f"the coordinator (pid {pid}) exited before reporting healthy")
            if time.time() >= end:
                raise StepFailed(f"no healthy heartbeat within {timeout:.0f}s ({last})")
            time.sleep(self.poll)

    def _lead_required(self, man: dict) -> bool:
        return bool((man or {}).get("lead_enabled")) or \
            any(_profile_enables_lead(p) for p in self.profiles)

    def _step_checking(self, req) -> dict:
        st = req["log"]["state"]
        s = st.get("started") or {}
        man = req["manifest"] or {}
        h = self.health(float(s.get("started_ts") or 0), int(s.get("pid") or 0),
                        self.health_timeout, expect_release=str(man.get("release") or ""),
                        require_lead=self._lead_required(man))
        try:
            after = schema_versions(self.db_path)
        except sqlite3.Error as e:
            raise StepFailed(f"the coordinator DB does not open: {e}") from None
        lower = {k: (v, after.get(k)) for k, v in (st.get("schema_before") or {}).items()
                 if after.get(k) is None or after[k] < v}
        if lower:
            raise StepFailed(f"schema versions moved backwards or vanished: {lower}")
        return {"health": h["source"], "schema": after, "pid": h.get("pid")}

    def _release_pause(self, req) -> list:
        """Clear every pause this supervisor set for the request (an operator
        who took a pause over keeps it). The project ids released."""
        st = req["log"].get("state") or {}
        pauses = st.get("pauses")
        if not isinstance(pauses, dict):
            pauses = {req["project_id"]: bool(st.get("pause_owned"))}
        out = []
        for pid, owned in sorted(pauses.items()):
            if not owned:
                continue
            meta = _uj(self.store.meta(PROJECT_PAUSE_META + pid), {})
            if meta and (meta.get("actor") != ACTOR or meta.get("upgrade_id") != req["upgrade_id"]):
                continue                          # an operator took the pause over
            self.store.x("DELETE FROM meta WHERE key=?", (PROJECT_PAUSE_META + pid,))
            if self.store.project(pid):
                self.store.set_project_paused(pid, False, "")
            out.append(pid)
        return out

    def _step_resuming(self, req) -> dict:
        # the recovery case is the lead's: it sees `done` through get_request
        released = self._release_pause(req)
        return {"pause_released": bool(released), "released": released}

    # -- rollback ------------------------------------------------------------------

    def _rollback_reset(self, req) -> dict:
        """Put the router checkout back without ever discarding work: see the
        module docstring. StepFailed = a human must look."""
        st = req["log"]["state"]
        head = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        branch = self._branch()
        prior = req.get("prior_sha") or ""
        cand = st.get("candidate_full") or req["candidate_sha"]
        if not prior or head == prior:
            return {"from": head, "to": head, "note": "HEAD unchanged"}
        if head == cand and branch == st.get("prior_branch"):
            r = _git(self.repo, "reset", "--keep", prior, check=False)
            if r.returncode:
                raise StepFailed(f"git reset --keep {prior[:12]} refused (local changes to files "
                                 f"the candidate changed would be lost): the router checkout is "
                                 f"left at the candidate for a human: "
                                 f"{(r.stderr or r.stdout).strip()[:300]}")
            return {"from": head, "to": prior, "how": "reset --keep", "branch": branch}
        if not st.get("install_started"):
            return {"from": head, "to": head, "branch": branch,
                    "note": "HEAD moved without an install (someone else's commit): not reset"}
        raise StepFailed(f"router HEAD {head[:12]} on {branch or 'a detached HEAD'} is neither "
                         f"the prior {prior[:12]} nor the installed candidate {cand[:12]} on "
                         f"{st.get('prior_branch') or 'the detached HEAD'}: not reset; a human "
                         f"must restore the checkout")

    def _rollback_db(self, req) -> dict:
        """Restore the backup only when the new build migrated the DB."""
        st = req["log"]["state"]
        before = st.get("schema_before")
        bdb = os.path.join(req.get("backup_path") or "", DB_NAME)
        if before is None or not req.get("backup_path") or not os.path.isfile(bdb):
            return {"restored": False, "note": "no backup was taken (the failure came before "
                                               "it): no new build touched the DB"}
        try:
            after = schema_versions(self.db_path)
        except sqlite3.Error:
            after = None
        if after == before:
            return {"restored": False, "schema": after,
                    "note": "the new build did not migrate the DB: the current DB is kept"}
        src = sqlite3.connect(bdb)
        try:
            src.backup(self.store.conn)
        finally:
            src.close()
        bblobs = os.path.join(req["backup_path"], "evidence_blobs")
        if os.path.isdir(bblobs):
            shutil.copytree(bblobs, os.path.join(self.state_dir, "evidence_blobs"),
                            dirs_exist_ok=True)
        migrate(self.store)
        return {"restored": True, "from": bdb, "schema_before": before, "schema_after": after}

    def _rollback(self, req) -> None:
        st = req["log"]["state"]
        rb = st.setdefault("rollback", {})
        try:
            if not rb.get("stopped"):
                rb["stopped"] = self.stop_coordinator(req["upgrade_id"])
                self._save(req, "rolling_back", "stopped", rb["stopped"])
            if not rb.get("reset"):
                rb["reset"] = self._rollback_reset(req)
                self._save(req, "rolling_back", "reset", rb["reset"])
            if not rb.get("db"):
                rb["db"] = self._rollback_db(req)
                # the restored DB holds this row as it was at backup time: the
                # in-memory journal (and its mirror) is written back by _save
                self._save(req, "rolling_back",
                           "db_restored" if rb["db"]["restored"] else "db_kept", rb["db"])
            if not rb.get("restarted"):
                s = rb.get("restart_started")
                if s and self._running_since(s):
                    s = dict(s, adopted=True)        # started before a crash: adopt it
                else:
                    s = self.start_coordinator(f"{req['upgrade_id']}:old")
                    rb["restart_started"] = s
                    self._save(req, "rolling_back", "restart_started", s)
                try:
                    self.health(s["started_ts"], s["pid"], self.health_timeout)
                    s["healthy"] = True
                except StepFailed as e:
                    s["healthy"], s["health_error"] = False, str(e)[:300]
                rb["restarted"] = s
                self._save(req, "rolling_back", "restarted", s)
            released = self._release_pause(req)
            # reported on the request only; the lead reads it via get_request
            st["rollback_result"] = {
                "upgrade_id": req["upgrade_id"], "failure": st.get("failure"),
                "prior_sha": req.get("prior_sha") or "",
                "reset": rb.get("reset"), "db_restored": bool((rb.get("db") or {}).get("restored")),
                "restarted_healthy": bool((rb.get("restarted") or {}).get("healthy"))}
            self._save(req, "rolling_back", "done", {"pause_released": bool(released),
                                                     "released": released,
                                                     "failure": st.get("failure")},
                       status="rolled_back")
        except Exception as e:                                   # noqa: BLE001
            reason = f"{type(e).__name__}: {e}"[:1000]
            try:
                released = self._release_pause(req)
            except Exception as e2:                              # noqa: BLE001
                released = []
                reason += f"; releasing the pause failed: {type(e2).__name__}: {e2}"[:300]
            st["rollback_result"] = {"upgrade_id": req["upgrade_id"],
                                     "failure": st.get("failure"),
                                     "rollback_failed": reason, "needs_human": True,
                                     "pause_released": released,
                                     "parts_done": sorted(rb)}
            self._save(req, "rolling_back", "rollback_failed",
                       {"reason": reason, "needs_human": True, "pause_released": released},
                       status="failed")


class _Lock:
    """One supervisor per state dir (a stale lock of a dead process is taken over)."""

    def __init__(self, path: str):
        self.path = path

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as fh:
                    fh.write(str(os.getpid()))
                return self
            except FileExistsError:
                try:
                    with open(self.path, encoding="utf-8") as fh:
                        other = int(fh.read().strip() or 0)
                except (OSError, ValueError):
                    other = 0
                if other and other != os.getpid() and pid_alive(other):
                    raise RuntimeError(f"another supervisor (pid {other}) holds {self.path}")
                try:
                    os.remove(self.path)
                except OSError:
                    pass
        raise RuntimeError(f"could not take {self.path}")

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass
        return False


# --------------------------------------------------------------------------- CLI

def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m foundry_coordinator.supervisor",
                                 description="Install approved coordinator upgrades safely.")
    ap.add_argument("--state", default=os.environ.get("FOUNDRY_COORD_STATE")
                    or "~/.foundry-coordinator", help="coordinator state dir")
    ap.add_argument("--profiles", nargs="*", default=[],
                    help="project profiles (a [lead_manager] enabled one makes the health check "
                         "require lead readiness)")
    ap.add_argument("--coordinator-cmd", required=False, default="",
                    help="command that starts the coordinator (cwd = --repo)")
    ap.add_argument("--stop-cmd", default="", help="command that stops it (default: pid file)")
    ap.add_argument("--repo", default="", help="the Foundry-Router checkout to upgrade")
    ap.add_argument("--python", default="", help="python for the manifest tests")
    ap.add_argument("--stop-timeout", type=float, default=30)
    ap.add_argument("--health-timeout", type=float, default=120)
    ap.add_argument("--safe-timeout", type=float, default=3600)
    ap.add_argument("--test-timeout", type=float, default=900)
    ap.add_argument("--interval", type=float, default=5)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="advance open requests once, then exit")
    g.add_argument("--loop", action="store_true", help="keep consuming requests")
    g.add_argument("--status", action="store_true", help="print the upgrade requests")
    a = ap.parse_args(argv)
    state = os.path.abspath(os.path.expanduser(a.state))
    if a.status:
        s = Store(os.path.join(state, DB_NAME))
        migrate(s)
        for r in s.q("SELECT upgrade_id, case_id, project_id, status, candidate_sha, prior_sha, "
                     "updated_ts FROM upgrade_requests ORDER BY created_ts"):
            print(json.dumps(r, default=str))
        return 0
    if not a.repo or not a.coordinator_cmd:
        ap.error("--repo and --coordinator-cmd are required with --once/--loop")
    sup = Supervisor(state, a.repo, a.coordinator_cmd, stop_cmd=a.stop_cmd or None,
                     python=a.python or None, profiles=a.profiles,
                     stop_timeout=a.stop_timeout, health_timeout=a.health_timeout,
                     safe_timeout=a.safe_timeout, test_timeout=a.test_timeout)
    try:
        if a.loop:
            sup.loop(a.interval)
            return 0
        res = sup.run_once()
        for r in res:
            print(json.dumps(r, default=str))
        return 1 if any(r["status"] in ("failed", "rolled_back") for r in res) else 0
    finally:
        sup.close()


if __name__ == "__main__":
    sys.exit(main())
