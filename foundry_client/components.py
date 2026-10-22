"""What each managed component is: how to start it, how to tell it is healthy, and
whether a restart must wait for a safe point (no live coding session)."""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import expand

# task states in which a live session may be writing (a paused/blocked task's idle
# session does not block a restart; the old deploy probe waited on those forever)
ACTIVE = ("READY", "IMPLEMENTING", "VERIFYING", "REVIEWING", "CHANGES_REQUIRED", "APPROVED",
          "CLOSING")


@dataclass
class Spec:
    name: str
    label: str
    argv: list
    cwd: str = ""
    env: dict = field(default_factory=dict)
    secrets: list = field(default_factory=list)
    health: Optional[Callable[[], tuple[bool, str]]] = None
    safe_restart: bool = False
    after: list = field(default_factory=list)
    problem: str = ""              # why it can't start (missing binary, missing folder)


def self_argv() -> list:
    """How to run this client itself (frozen bundle or source). A bundle ships a
    windowless tray program (FoundryClient) and a console twin (foundry-client); child
    processes and commands use the console one so their output reaches the logs."""
    if getattr(sys, "frozen", False):
        d = Path(sys.executable).parent
        con = d / ("foundry-client.exe" if sys.platform == "win32" else "foundry-client")
        return [str(con if con.is_file() else sys.executable)]
    return [sys.executable, "-m", "foundry_client"]


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_binary(configured: str, names: list[str], extra: list[str] = ()) -> str:
    if configured:
        p = expand(configured)
        return p if os.path.isfile(p) else ""
    for n in names:
        w = shutil.which(n)
        if w:
            return w
    for p in extra:
        p = expand(p)
        if os.path.isfile(p):
            return p
    return ""


def heartbeat_age(state_dir: str) -> Optional[float]:
    p = Path(expand(state_dir)) / "coordinator.heartbeat"
    try:
        hb = json.loads(p.read_text(encoding="utf-8"))
        return time.time() - float(hb.get("ts") or 0)
    except (OSError, ValueError):
        return None


def pid_alive(pid) -> bool:
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)              # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259                # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def safe_point(state_dir: str, game_dir: str = "") -> tuple[bool, str]:
    """True when nothing of the coordinator's is working (read-only DB probe):
    no live session of an active task, no lead or repair session in flight
    (recovery_actions started), no studio_gate runner (a fast-lane run whose pid is
    alive, or the runner lock under `game_dir/.plan/tmp/gate` held by a live pid).
    W1.9: the old probe counted implementer sessions only, so a restart could kill
    a lead session editing a BLOCKED task or the detached gate runner."""
    db = Path(expand(state_dir)) / "coordinator.sqlite"
    busy: list[str] = []
    if db.is_file():
        try:
            c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                q = ("SELECT count(*) FROM sessions s JOIN tasks t ON t.project_id=s.project_id "
                     "AND t.task_id=s.task_id WHERE s.status NOT IN ('ended','aborted','created') "
                     f"AND t.state IN ({','.join('?' * len(ACTIVE))})")
                live = c.execute(q, ACTIVE).fetchone()[0]
                if live:
                    busy.append(f"{live} session(s) working")
                busy += _lead_busy(c)
                busy += _runner_busy(c)
            finally:
                c.close()
        except sqlite3.Error as e:
            return False, f"cannot read the coordinator database ({e})"
    else:
        if not game_dir:
            return True, "no coordinator database"
    lock = _lock_busy(game_dir)
    if lock:
        busy.append(lock)
    return (not busy), ("safe" if not busy else "; ".join(busy))


def _lead_busy(c) -> list[str]:
    """Lead / repair sessions in flight (the lead edits a BLOCKED task, so the task
    state alone never shows it)."""
    try:
        n = c.execute("SELECT count(*) FROM recovery_actions WHERE kind IN "
                      "('lead_call','repair_session') AND status='started'").fetchone()[0]
    except sqlite3.Error:
        return []                                   # no lead tables: no lead
    return [f"{n} lead session(s) in flight"] if n else []


def _runner_busy(c) -> list[str]:
    """A fast-lane studio_gate runner the coordinator started and whose pid lives."""
    try:
        rows = c.execute("SELECT task_id, data FROM tasks WHERE state='VERIFYING'").fetchall()
    except sqlite3.Error:
        return []
    out = []
    for task_id, data in rows:
        try:
            run = ((json.loads(data or "{}").get("fast_lane") or {}).get("run")) or {}
        except (TypeError, ValueError):
            continue
        if run.get("pid") and pid_alive(run.get("pid")):
            out.append(f"gate runner pid {run['pid']} ({task_id})")
    return out


def _lock_busy(game_dir: str) -> str:
    if not game_dir:
        return ""
    p = Path(expand(game_dir)) / ".plan" / "tmp" / "gate" / "runner.lock"
    try:
        cur = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    pid = (cur or {}).get("pid") if isinstance(cur, dict) else None
    if pid and pid_alive(pid):
        return f"gate runner lock held by pid {pid} ({(cur or {}).get('sprint') or '?'})"
    return ""


def build(cfg: dict) -> dict[str, Spec]:
    cl, ws, comps = cfg["client"], cfg["workspace"], cfg["components"]
    state = expand(cl["state_dir"])
    game, profiles, deploy = expand(ws["game"]), expand(ws["profiles"]), expand(ws["deploy"])
    out: dict[str, Spec] = {}

    k = comps["kilo"]
    kilo = find_binary(k.get("path", ""), ["kilo", "kilo.cmd"],
                       ["~/AppData/Roaming/npm/kilo.cmd", "~/.npm-global/bin/kilo"])
    port = int(k.get("port") or 4096)
    out["kilo"] = Spec("kilo", "Kilo server",
                       [kilo, "serve", "--hostname", "127.0.0.1", "--port", str(port), *k["args"]],
                       cwd=game, env=k.get("env") or {}, secrets=k.get("secrets") or [],
                       health=lambda: (port_open(port), f"port {port}"),
                       safe_restart=bool(k.get("safe_restart", True)),
                       problem="" if kilo else "kilo was not found; set its path in Settings")

    c = comps["coordinator"]
    me = self_argv()
    out["coordinator"] = Spec(
        "coordinator", "Coordinator",
        [*me, "coordinator", "--state", state, "--profiles", profiles, "run", "--poll",
         str(c.get("poll_seconds") or 3), "--control", "--telemetry", *c["args"]],
        cwd=deploy or profiles, env=c.get("env") or {}, secrets=c.get("secrets") or [],
        health=lambda: _hb_health(state), safe_restart=bool(c.get("safe_restart", True)),
        after=c.get("after") or [],
        problem="" if profiles and os.path.isdir(profiles) else
        "set the coordinator profiles folder in Settings")

    s = comps["supervisor"]
    restart_coord = " ".join(_q(x) for x in [*me, "restart", "coordinator", "--wait"])
    out["supervisor"] = Spec(
        "supervisor", "Upgrade supervisor",
        [*me, "supervisor", "--state", state, "--repo", deploy, "--profiles", profiles,
         "--coordinator-cmd", restart_coord,
         *([] if getattr(sys, "frozen", False) else ["--python", sys.executable]),
         "--loop", *s["args"]],
        cwd=deploy, env=s.get("env") or {}, secrets=s.get("secrets") or [],
        after=s.get("after") or [],
        problem="" if deploy and os.path.isdir(os.path.join(deploy, ".git")) else
        "the upgrade supervisor needs a git release checkout (workspace.deploy)")

    m = comps["media"]
    out["media"] = Spec("media", "Media adapter", [expand(x) for x in m.get("command") or []],
                        cwd=expand(m.get("cwd", "")), env=m.get("env") or {},
                        secrets=m.get("secrets") or [],
                        problem="" if m.get("command") else "set the media adapter command")

    r = comps["rojo"]
    rojo = find_binary(r.get("path", ""), ["rojo"], ["~/.rokit/bin/rojo.exe", "~/.rokit/bin/rojo",
                                                     "~/.aftman/bin/rojo.exe", "~/.aftman/bin/rojo"])
    rport = int(r.get("port") or 34872)
    project = expand(r.get("project") or "") or game
    out["rojo"] = Spec("rojo", "Rojo", [rojo, "serve", "--port", str(rport), *r["args"]],
                       cwd=project, env=r.get("env") or {}, secrets=r.get("secrets") or [],
                       health=lambda: (port_open(rport), f"port {rport}"),
                       problem="" if rojo else "rojo was not found; set its path in Settings")
    return out


def _hb_health(state: str) -> tuple[bool, str]:
    age = heartbeat_age(state)
    if age is None:
        return False, "no heartbeat yet"
    return age < 90, f"heartbeat {int(age)} s ago"


def _q(x: str) -> str:
    return f'"{x}"' if (" " in x and not x.startswith('"')) else x
