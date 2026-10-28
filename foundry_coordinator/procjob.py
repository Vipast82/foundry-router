"""Check subprocesses that die with the coordinator (E, 2026-10-28).

A restart or deploy of the coordinator used to orphan the Lune suite and done_check it
had started on a worker thread: the old process kept running and writing its log while
the new coordinator started the same check again (into the same log). `run()` is a
drop-in for `subprocess.run` for those checks:

  Windows  each run gets its own job object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. The
           only handle is the coordinator's, so when the coordinator exits (or is killed)
           the kernel closes it and every process of the run (Lune's workers included)
           is killed. A timeout terminates the whole job, not just the direct child.
  other    the run is its own process group (killed as a group on timeout), and on Linux
           the child gets SIGKILL when the coordinator dies (PR_SET_PDEATHSIG).
  all      a pid file per run under <state>/procs/ (pid + create time); `reap()` at
           startup kills a leftover run whose process still has that create time (needs
           psutil; without it nothing is killed: a reused pid is never guessed at).

The studio_gate runner is NOT run here: it is started detached on purpose and adopted
by pid after a restart (fast_lane.py, tracks/merge.py).

Tests that monkeypatch `subprocess.run` keep working: `run()` hands the call to the
patched function.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from typing import Optional

_REAL_RUN = subprocess.run
_lock = threading.Lock()
_registry_dir: Optional[str] = None

KILL_ON_JOB_CLOSE = 0x2000
JOB_EXTENDED_LIMIT_INFO = 9


def set_registry(state_dir: str) -> None:
    """Where the pid files go (<state_dir>/procs); the engine sets it at startup."""
    global _registry_dir
    _registry_dir = os.path.join(state_dir, "procs") if state_dir else None


# ------------------------------------------------------------------ Windows job objects

def _win_job():
    """A new job object that kills its processes when its last handle closes, or None."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, JOB_EXTENDED_LIMIT_INFO, ctypes.byref(info),
                                           ctypes.sizeof(info)):
            k32.CloseHandle(job)
            return None
        return [k32, job]
    except Exception:                                             # noqa: BLE001
        return None


def _win_assign(job, proc) -> bool:
    if job is None:
        return False
    k32, h = job
    try:
        return bool(k32.AssignProcessToJobObject(h, int(proc._handle)))
    except Exception:                                             # noqa: BLE001
        return False


def _win_close(job, kill: bool) -> None:
    if job is None or job[1] is None:
        return
    k32, h = job
    job[1] = None                              # closed once, never a recycled handle
    try:
        if kill:
            k32.TerminateJobObject(h, 1)
        k32.CloseHandle(h)                     # KILL_ON_JOB_CLOSE: leftovers die here too
    except Exception:                                             # noqa: BLE001
        pass


# ------------------------------------------------------------------ pid files

def _create_time(pid: int) -> Optional[float]:
    try:
        import psutil
        return float(psutil.Process(pid).create_time())
    except Exception:                                             # noqa: BLE001
        return None


def _register(proc, cmd) -> Optional[str]:
    d = _registry_dir
    if not d:
        return None
    try:
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{proc.pid}-{uuid.uuid4().hex[:8]}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": proc.pid, "create_time": _create_time(proc.pid),
                       "owner": os.getpid(), "owner_ct": _create_time(os.getpid()), "cmd": [str(c) for c in cmd][:12],
                       "ts": time.time()}, f)
        return path
    except OSError:
        return None


def _unregister(path: Optional[str]) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def reap(state_dir: str, log=lambda m: None) -> list[int]:
    """Kill the check processes a previous coordinator left running (their pid file is
    still there and the process has the recorded create time). Returns the pids killed."""
    d = os.path.join(state_dir, "procs")
    killed: list[int] = []
    try:
        names = os.listdir(d)
    except OSError:
        return killed
    for n in names:
        path = os.path.join(d, n)
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            _unregister(path)
            continue
        pid, ct = rec.get("pid"), rec.get("create_time")
        owner = rec.get("owner")
        if owner == os.getpid():
            continue                                # one of ours, still running
        oct_ = rec.get("owner_ct")
        if owner and oct_ is not None and _create_time(int(owner)) == oct_:
            continue                                # its coordinator is still alive
        if pid and ct is not None and _create_time(int(pid)) == ct:
            try:
                import psutil
                p = psutil.Process(int(pid))
                for c in p.children(recursive=True):
                    c.kill()
                p.kill()
                killed.append(int(pid))
                log(f"killed a leftover check process {pid}: {' '.join(rec.get('cmd') or [])}"[:300])
            except Exception:                                     # noqa: BLE001
                pass
        _unregister(path)
    return killed


# ------------------------------------------------------------------ run

def run(cmd, *, timeout: Optional[float] = None, capture_output: bool = False,
        input=None, check: bool = False, **kw) -> subprocess.CompletedProcess:
    """subprocess.run, with the run's processes tied to the coordinator's life."""
    if subprocess.run is not _REAL_RUN:              # a test replaced subprocess.run
        return subprocess.run(cmd, timeout=timeout, capture_output=capture_output,
                              input=input, check=check, **kw)
    if capture_output:
        kw["stdout"] = subprocess.PIPE
        kw["stderr"] = subprocess.PIPE
    if input is not None:
        kw["stdin"] = subprocess.PIPE
    if os.name != "nt":
        kw.setdefault("start_new_session", True)
        if sys.platform.startswith("linux") and "preexec_fn" not in kw:
            kw["preexec_fn"] = _pdeathsig
    job = _win_job()
    proc = subprocess.Popen(cmd, **kw)
    _win_assign(job, proc)
    reg = _register(proc, cmd if isinstance(cmd, (list, tuple)) else [cmd])
    try:
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc, job)
            out, err = proc.communicate()
            raise subprocess.TimeoutExpired(proc.args, timeout, output=out, stderr=err)
        except BaseException:
            _kill(proc, job)
            raise
        rc = proc.poll()
        if check and rc:
            raise subprocess.CalledProcessError(rc, proc.args, output=out, stderr=err)
        return subprocess.CompletedProcess(proc.args, rc, out, err)
    finally:
        _win_close(job, kill=True)       # the direct child is done; its leftovers go too
        _unregister(reg)


def _kill(proc, job) -> None:
    if job is not None:
        _win_close(job, kill=True)
    try:
        if os.name != "nt":
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:                                             # noqa: BLE001
        try:
            proc.kill()
        except Exception:                                         # noqa: BLE001
            pass
    proc.wait()


def _pdeathsig() -> None:                                         # pragma: no cover
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6").prctl(1, signal.SIGKILL)       # PR_SET_PDEATHSIG
    except Exception:                                             # noqa: BLE001
        pass
