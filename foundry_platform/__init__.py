"""foundry_platform: the one place that knows which OS Foundry runs on.

docs/DISTRIBUTED_FOUNDRY.md §7.1 (phase a, Victor 2026-11-27 "Approve, start a"). The
coordinator, the Foundry Client and (from phase b) the slot's lane agent share one code base
for Windows and Linux; every "which OS is this" question in them goes through here, so a
Windows slot and a Linux junction need no forks in the logic.

Rules:
  * stdlib only, importable before anything else (the slot runs it from /opt/foundry);
  * every predicate is read at CALL time (`is_windows()` looks at sys.platform each time), so a
    test that patches sys.platform sees its patch;
  * the constants (NO_WINDOW, CREATE_NO_WINDOW) are fixed at import, exactly like the per-module
    copies they replace (`0x08000000 if os.name == "nt" else 0`).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
from typing import Iterator, Optional

# Windows process creation flags (also defined on POSIX, where nothing uses them)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# the copies this replaces were `0x08000000 if os.name == "nt" else 0`
NO_WINDOW = CREATE_NO_WINDOW if os.name == "nt" else 0


# ----------------------------------------------------------------------------- which OS

def is_windows(sysmod=None) -> bool:
    """`sysmod`: a module's own `sys` reference, for modules whose tests replace that reference
    with a stand-in (foundry_client/autostart.py); default the real sys."""
    return (sysmod or sys).platform == "win32"


def is_linux(sysmod=None) -> bool:
    return str((sysmod or sys).platform).startswith("linux")


def is_macos() -> bool:
    return sys.platform == "darwin"


def os_kind() -> str:
    """"windows", "linux", "macos", or sys.platform for anything else. The value a slot's
    `os` setting takes ([slots.<id>] os = "linux" | "windows")."""
    if is_windows():
        return "windows"
    if is_linux():
        return "linux"
    if is_macos():
        return "macos"
    return sys.platform


# ----------------------------------------------------------------------------- processes

def no_window_flags() -> int:
    """creationflags that keep a child from opening a console window (0 off Windows)."""
    return CREATE_NO_WINDOW if is_windows() else 0


def no_window_kw() -> dict:
    """Popen/run keywords for the same: {"creationflags": ...} on Windows, {} elsewhere."""
    return {"creationflags": CREATE_NO_WINDOW} if is_windows() else {}


def detached_kw() -> dict:
    """Popen keywords for a child that outlives this process and gets no console: Windows
    DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW, POSIX a new session."""
    if is_windows():
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}
    return {"start_new_session": True}


def group_kw() -> dict:
    """Popen keywords for a child whose whole process tree kill_tree() can end: POSIX a new
    session (its own process group), Windows no console window (taskkill /T follows the
    parent ids there, so no group is needed)."""
    if is_windows():
        return {"creationflags": CREATE_NO_WINDOW}
    return {"start_new_session": True}


def kill_tree(proc, timeout: float = 5.0) -> None:
    """End `proc` (a Popen) and every process it started. POSIX: SIGTERM to its process group
    when it leads one (started with group_kw()), SIGKILL after `timeout`; else the process
    alone. Windows: `taskkill /T /F` on its tree, then Popen.kill() in case taskkill is
    missing. Never raises; a process that already ended is left alone."""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:                                             # noqa: BLE001
        return
    pid = int(getattr(proc, "pid", 0) or 0)
    if is_windows():
        if pid:
            try:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30, **no_window_kw())
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=timeout)
        except Exception:                                         # noqa: BLE001
            pass
        return
    import signal
    try:
        group = os.getpgid(pid) if pid else 0
    except OSError:
        group = 0
    leads = bool(group) and group == pid
    try:
        if leads:
            os.killpg(group, signal.SIGTERM)
        else:
            proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=timeout)
    except Exception:                                             # noqa: BLE001
        pass
    try:
        if leads:
            os.killpg(group, signal.SIGKILL)       # children that ignored SIGTERM, too
        elif proc.poll() is None:
            proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=timeout)
    except Exception:                                             # noqa: BLE001
        pass


def job_shell(script: str, login: bool = True) -> list:
    """argv that runs a job script in the platform's job shell: `bash -lc` on POSIX,
    PowerShell 7 (`pwsh`, else Windows PowerShell) on Windows. The script is passed as one
    argument, never through another shell."""
    if is_windows():
        exe = shutil.which("pwsh") or shutil.which("powershell") or "pwsh"
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script]
    return ["bash", "-lc" if login else "-c", script]


def script_ext() -> str:
    """The extension of a small launcher script this platform runs directly."""
    return ".cmd" if is_windows() else ".sh"


def service_kind() -> str:
    """How the Foundry Client starts at boot here (foundry_client/autostart.py)."""
    return "scheduled-task" if is_windows() else "systemd-user"


# ----------------------------------------------------------------------------- paths

def default_root() -> str:
    """Where a Foundry machine keeps its files when nothing says otherwise. Short on Windows
    (a 113-character temp path broke git there; memory note parallel-pytest-basetemp)."""
    return "C:\\Foundry" if is_windows() else "/srv/foundry"


def lanes_root() -> str:
    """Where a lane host keeps its lane worktrees (phase d), under default_root()."""
    return os.path.join(default_root(), "lanes")


# ----------------------------------------------------------------------------- files

def read_text(path: str, errors: str = "strict") -> str:
    """A text file read as UTF-8 always (this workstation defaults to cp1252: memory note
    workstation-cp1252-default), its newlines as stored (no CRLF translation on either OS)."""
    with open(path, encoding="utf-8", errors=errors, newline="") as f:
        return f.read()


def write_text(path: str, text: str) -> None:
    """Write UTF-8 text atomically (temp file + replace), with the newlines given."""
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


class LockTimeout(TimeoutError):
    pass


@contextlib.contextmanager
def file_lock(path: str, timeout: Optional[float] = None, poll: float = 0.1) -> Iterator[None]:
    """An exclusive advisory lock on `path` (created if missing), held for the `with` block:
    fcntl.flock on POSIX, msvcrt.locking on Windows. `timeout` None waits forever, 0 tries once;
    LockTimeout when it runs out. The lock dies with the process on both systems."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    try:
        while True:
            if _try_lock(fd):
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise LockTimeout(f"{path} is locked by another process")
            time.sleep(poll)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _try_lock(fd: int) -> bool:
    if is_windows():
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    if is_windows():
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


# ----------------------------------------------------------------------------- facts

def facts() -> dict:
    """What `foundry-client doctor --platform` reports first (no secrets, no network)."""
    import platform as _p
    return {"os": os_kind(), "release": _p.release(), "machine": _p.machine(),
            "python": sys.version.split()[0], "encoding": sys.getfilesystemencoding(),
            "preferred_encoding": __import__("locale").getpreferredencoding(False),
            "service": service_kind(), "root": default_root(),
            "job_shell": job_shell("")[0]}


__all__ = ["CREATE_NO_WINDOW", "DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "NO_WINDOW",
           "is_windows", "is_linux", "is_macos", "os_kind", "no_window_flags", "no_window_kw",
           "detached_kw", "group_kw", "kill_tree", "job_shell", "script_ext", "service_kind", "default_root",
           "lanes_root", "read_text", "write_text", "LockTimeout", "file_lock", "facts"]
