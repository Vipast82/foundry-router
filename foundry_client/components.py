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

from foundry_automation import service_map as SM

from . import secrets
from .config import SERVICE_COMPONENTS, expand
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
    return [console_python(sys.executable), "-m", "foundry_client"]


def console_python(exe: str) -> str:
    """python.exe beside a pythonw.exe (Windows source install). A windowless parent
    gives every console child it starts (each `git` of the coordinator's tick) a new,
    briefly flashing window; started from python.exe with CREATE_NO_WINDOW the child
    owns one hidden console that its own children reuse."""
    p = Path(exe)
    if sys.platform == "win32" and p.name.lower() == "pythonw.exe":
        con = p.with_name("python.exe")
        if con.is_file():
            return str(con)
    return exe


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

    labels = {"blender": "Blender (blender-mcp add-on)", "acestep-music": "ACE-Step music",
              "stable-audio": "Stable Audio", "kokoro-tts": "Kokoro speech",
              "chatterbox-tts": "Chatterbox speech"}
    for name in SERVICE_COMPONENTS:
        c = comps.get(name) or {}
        prog = expand(c.get("path") or "")
        sport = int(c.get("port") or 0)
        # blender: no health check (its socket opens only after "Connect" in Blender), so a
        # started Blender is never restarted as unhealthy; the others answer on their port
        health = (lambda p=sport: _port_health(p)) if sport and name != "blender" else None
        out[name] = Spec(name, labels[name], [prog, *c.get("args", [])],
                         cwd=expand(c.get("cwd") or ""), env=c.get("env") or {},
                         secrets=c.get("secrets") or [], health=health, port=sport,
                         # a Blender already listening on its add-on port is adopted, never
                         # started twice (2026-10-28: Blender ran outside the client)
                         owner_pid=(lambda p=sport: port_owner_pid(p)) if sport else None,
                         problem="" if prog and os.path.isfile(prog) else
                         f"set the program path for {labels[name]} in Settings")
    return out


RETIRED_MEDIA = ("acestep-music", "stable-audio", "kokoro-tts", "chatterbox-tts")


def _port_health(port: int) -> tuple:
    return (True, f"port {port} is open") if port_open(port) else (False, f"port {port} is closed")


# --------------------------------------------------------------------------- MCP + services report

def mcp_report(cfg: dict, global_path: Optional[str] = None, port_fn=None) -> list[dict]:
    """The MCP servers Kilo can see for this workstation's project (kilo.jsonc over the global
    Kilo config) and whether each looks available: a command's binary exists, a URL's host
    accepts a connection. Not a handshake (service_map documents the assumptions)."""
    path = kilo_config_path(cfg)
    servers = SM.merged_mcp(project_path=path if path and os.path.isfile(path) else "",
                            global_path=global_path)
    from concurrent.futures import ThreadPoolExecutor
    names = list(servers)
    if not names:
        return []
    fn = port_fn or SM.port_open
    with ThreadPoolExecutor(max_workers=min(8, len(names))) as ex:
        return list(ex.map(lambda n: SM.probe_server(n, servers[n], port_fn=fn), names))


def services_report(cfg: dict, mcp_rows: list[dict], comp_states: dict,
                    port_fn=None, http_fn=None) -> list[dict]:
    """The registry as this client sees it: every catalog service with its status, who provides
    it (a client component when one exists here, else external / backend), and whether that
    component is switched on. `comp_states`: {component: {"state", "enabled"}}."""
    rows = {r["name"]: r for r in mcp_rows}
    ov = {}
    out = []
    for name, svc in SM.SERVICES.items():
        ok, detail = SM.check_service(svc, rows, ov, port_fn or SM.port_open, http_fn)
        comp = comp_states.get(svc.component) if svc.component else None
        provider = "client" if comp is not None else ("backend" if svc.provider == "backend"
                                                      else "external")
        out.append({"name": name, "label": svc.label, "kind": svc.kind, "check": svc.check,
                    "provider": provider, "component": svc.component if comp is not None else "",
                    "enabled": bool(comp.get("enabled")) if comp is not None else None,
                    "component_state": (comp or {}).get("state") if comp is not None else None,
                    "needed_by": SM.needed_by(name),
                    "status": "up" if ok is True else "down" if ok is False else "unknown",
                    "detail": str(detail)[:200], "note": svc.note})
    return out


def _hb_health(state: str) -> tuple[bool, str]:
    """Coordinator health: a fresh heartbeat (< 90 s) whose process still exists.
    After a force-stop the dead coordinator's last heartbeat stays fresh for up to
    90 s; checking its pid keeps a restarted client from adopting it as "started
    outside the client" instead of launching a new one. A heartbeat without a pid
    (older coordinators) is judged by age alone."""
    age = heartbeat_age(state)
    if age is None:
        return False, "no heartbeat yet"
    pid = _heartbeat_raw_pid(state)
    if pid and not pid_alive(pid):
        return False, f"heartbeat {int(age)} s ago, but its process (pid {pid}) has ended"
    busy = _heartbeat_busy(state)
    return age < 90, f"heartbeat {int(age)} s ago" + (f"; busy: {busy}" if busy else "")


def _heartbeat_busy(state_dir: str) -> str:
    """The long step the coordinator's keep-alive beat names ("verify au/GF-GATE1, 4 min")."""
    p = Path(expand(state_dir)) / "coordinator.heartbeat"
    try:
        hb = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(hb, dict):
        return ""
    if not hb.get("busy"):
        # 0.147.0: verify / closeout checks run in the background; name them
        checks = [str(c)[:60] for c in (hb.get("checks") or []) if c][:3]
        return ("checks: " + ", ".join(checks)) if checks else ""
    mins = int(hb.get("busy_s") or 0) // 60
    return f"{str(hb['busy'])[:80]}, {mins} min"


def _heartbeat_raw_pid(state_dir: str) -> Optional[int]:
    """The pid written in the coordinator's heartbeat, alive or not."""
    p = Path(expand(state_dir)) / "coordinator.heartbeat"
    try:
        return int(json.loads(p.read_text(encoding="utf-8")).get("pid") or 0) or None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def heartbeat_pid(state_dir: str) -> Optional[int]:
    """The pid in the coordinator's heartbeat, if that process is alive."""
    pid = _heartbeat_raw_pid(state_dir)
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
    if code == 200 and not isinstance(body, dict):
        # Rojo 7 answers with msgpack, not JSON: decode it when the library is there,
        # otherwise a 200 is healthy with an unknown session id
        body = _rojo_msgpack(port)
    if code == 200:
        body = body if isinstance(body, dict) else {}
        sid = str(body.get("sessionId") or "")
        info = {"sessionId": sid, "projectName": body.get("projectName") or "",
                "serverVersion": body.get("serverVersion") or ""}
        name = f" {info['projectName']}" if info["projectName"] else ""
        return True, f"port {port},{name} session {sid[:8]}", info
    if code == 0:
        return False, f"port {port} is open but /api/rojo does not answer ({err})"
    return False, f"port {port} is open but /api/rojo returned HTTP {code or err}"


def _rojo_msgpack(port: int) -> dict:
    try:
        import urllib.request
        import msgpack
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/rojo", timeout=3) as r:
            raw = r.read()
        out = msgpack.unpackb(raw, raw=False)
        return out if isinstance(out, dict) else {}
    except Exception:                                            # noqa: BLE001
        return {}


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
