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

from . import secrets
from .config import expand
from .probe import basic_auth, get_json, pid_alive, port_owner_pid

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
    # (ok, detail) or (ok, detail, info dict)
    health: Optional[Callable[[], tuple]] = None
    safe_restart: bool = False
    after: list = field(default_factory=list)
    port: int = 0                  # the port it listens on (0 = none)
    owner_pid: Optional[Callable[[], Optional[int]]] = None   # who runs it, if not launched by us
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


def safe_point(state_dir: str) -> tuple[bool, str]:
    """True when no coordinator session is actively working (read-only DB probe)."""
    db = Path(expand(state_dir)) / "coordinator.sqlite"
    if not db.is_file():
        return True, "no coordinator database"
    try:
        c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        q = ("SELECT count(*) FROM sessions s JOIN tasks t ON t.project_id=s.project_id AND "
             "t.task_id=s.task_id WHERE s.status NOT IN ('ended','aborted','created') AND "
             f"t.state IN ({','.join('?' * len(ACTIVE))})")
        live = c.execute(q, ACTIVE).fetchone()[0]
        c.close()
    except sqlite3.Error as e:
        return False, f"cannot read the coordinator database ({e})"
    return live == 0, ("safe" if live == 0 else f"{live} session(s) working")


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
                       health=lambda: kilo_health(port),
                       port=port, owner_pid=lambda: port_owner_pid(port),
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
        owner_pid=lambda: heartbeat_pid(state),
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

    r = comps["rojo"]
    rojo = find_binary(r.get("path", ""), ["rojo"], ["~/.rokit/bin/rojo.exe", "~/.rokit/bin/rojo",
                                                     "~/.aftman/bin/rojo.exe", "~/.aftman/bin/rojo"])
    rport = int(r.get("port") or 34872)
    project = expand(r.get("project") or "") or game
    out["rojo"] = Spec("rojo", "Rojo", [rojo, "serve", "--port", str(rport), *r["args"]],
                       cwd=project, env=r.get("env") or {}, secrets=r.get("secrets") or [],
                       health=lambda: rojo_health(rport), port=rport,
                       owner_pid=lambda: port_owner_pid(rport),
                       problem="" if rojo else "rojo was not found; set its path in Settings")
    return out


def _hb_health(state: str) -> tuple[bool, str]:
    age = heartbeat_age(state)
    if age is None:
        return False, "no heartbeat yet"
    return age < 90, f"heartbeat {int(age)} s ago"


def heartbeat_pid(state_dir: str) -> Optional[int]:
    """The pid in the coordinator's heartbeat, if that process is alive."""
    p = Path(expand(state_dir)) / "coordinator.heartbeat"
    try:
        pid = int(json.loads(p.read_text(encoding="utf-8")).get("pid") or 0)
    except (OSError, ValueError, TypeError):
        return None
    return pid if pid and pid_alive(pid) else None


_pw_cache: dict = {"at": 0.0, "v": ""}


def kilo_password() -> str:
    """The Kilo server password from the secret store (re-read at most every 30 s)."""
    now = time.monotonic()
    if now - _pw_cache["at"] > 30:
        try:
            _pw_cache.update(at=now, v=secrets.get("KILO_SERVER_PASSWORD") or "")
        except Exception:                                        # noqa: BLE001
            _pw_cache.update(at=now, v="")
    return _pw_cache["v"]


def kilo_health(port: int) -> tuple:
    """Kilo answers GET /config (HTTP basic auth, user kilo). Telling a closed port, a wrong
    password and a wedged server apart is the point: a bare TCP connect says none of it."""
    if not port_open(port):
        return False, f"port {port} is closed"
    code, _body, err = get_json(f"http://127.0.0.1:{port}/config", basic_auth(kilo_password()), 3)
    if code == 200:
        return True, f"port {port}, /config ok"
    if code in (401, 403):
        return False, f"port {port} is open but rejects the Kilo server password (HTTP {code})"
    if code == 0:
        return False, f"port {port} is open but /config does not answer ({err})"
    return False, f"port {port} is open but /config returned HTTP {code}"


def rojo_health(port: int) -> tuple:
    """Rojo answers GET /api/rojo with its session id, project name and version. The session
    id changes whenever Rojo restarts: Studio's plugin must then be reconnected by hand."""
    if not port_open(port):
        return False, f"port {port} is closed"
    code, body, err = get_json(f"http://127.0.0.1:{port}/api/rojo", None, 3)
    if code == 200 and isinstance(body, dict):
        sid = str(body.get("sessionId") or "")
        info = {"sessionId": sid, "projectName": body.get("projectName") or "",
                "serverVersion": body.get("serverVersion") or ""}
        name = f" {info['projectName']}" if info["projectName"] else ""
        return True, f"port {port},{name} session {sid[:8]}", info
    if code == 0:
        return False, f"port {port} is open but /api/rojo does not answer ({err})"
    return False, f"port {port} is open but /api/rojo returned HTTP {code or err}"


def project_ids(profiles: str) -> list[str]:
    """Project ids found in the coordinator profiles folder (<dir>/<project>/project.toml)."""
    import tomllib
    out = []
    root = Path(expand(profiles)) if profiles else None
    if not root or not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        f = d / "project.toml"
        if f.is_file():
            try:
                out.append(str(tomllib.loads(f.read_text(encoding="utf-8")).get("id") or d.name))
            except (OSError, ValueError):
                out.append(d.name)
    return out


def kilo_config_path(cfg: dict) -> str:
    ws = cfg["workspace"]
    return expand(ws.get("kilo_config") or "") or (
        os.path.join(expand(ws["game"]), "kilo.jsonc") if ws.get("game") else "")


def _q(x: str) -> str:
    return f'"{x}"' if (" " in x and not x.startswith('"')) else x
