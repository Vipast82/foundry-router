"""Lane tunnels: one ssh port-forward per slot that holds lanes (Distributed Foundry phase b, B3).

docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md. A slot with `[slots.<id>] lanes = true` runs the
lane agent (foundry_client/lane.py) and its own kilo / opencode servers. They listen only on
the slot host's 127.0.0.1 (compose publishes the container ports there, like noVNC), so the
coordinator reaches them through ONE long-lived

    ssh -N -o ExitOnForwardFailure=yes -L <a>:127.0.0.1:<agent> -L <k>:127.0.0.1:<kilo>
        -L <o>:127.0.0.1:<opencode> <host>

per slot, with Foundry's own key and known_hosts (slot_ssh, via slots.ssh_cmd). Windows
OpenSSH has no ControlMaster, and one ssh per git call would cost 0.2-1 s each.

When the tunnel starts, the lane secrets (the agent token, the kilo and opencode passwords)
are read once from the slot's `lanes_secrets` file (a tmpfs) over the same ssh + docker exec
the slot commands already use (decision Db1). They are kept in memory only: never logged,
never in the store, never in an event.

A slot with no `host` (its docker runs on this machine, or a test) needs no ssh: the
endpoints are its published 127.0.0.1 ports directly.

Nothing here runs on the tick thread: `MANAGER.configure()` starts one daemon thread that
checks the tunnels every few seconds. With no slot set to lanes, or the project's
[automation] remote_lanes off, nothing starts and no secrets are read.

A tunnel is healthy when ssh runs, the forwarded port accepts AND the agent answers
GET /api/lane/health with the token held here (every HEALTH_EVERY_S). A slot's agent makes a
new token at every start, so a 401 re-reads the secrets once (the tunnel stays); no answer
twice, or a 401 the new secrets do not cure, restarts the tunnel. ssh's own messages go to
<state>/logs/lane-tunnel-<slot>.log (never a pipe nobody reads: a full pipe stops ssh).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

import foundry_platform as _plat

from . import slots as SL

CHECK_EVERY_S = 5.0
HEALTH_EVERY_S = 30.0
HEALTH_TIMEOUT_S = 10.0
HEALTH_MISSES = 2                 # no answer this many checks in a row: the tunnel restarts
REFRESH_GAP_S = 10.0              # secrets re-read at most this often per slot on a 401
LOG_MAX_BYTES = 1024 * 1024
BACKOFF_S = (5, 10, 20, 40, 80, 120)
SECRETS_FILE = "/run/secrets/lane/lane.json"
WINDOWS_SECRETS_FILE = "C:/Foundry/secrets/lane/lane.json"     # install/slot.ps1
PORTS = {"agent": 47811, "kilo": 4096, "opencode": 4097}
SECRETS_TIMEOUT_S = 30
READY_WAIT_S = 10
# the settings a tunnel is made from: two projects may share a slot's tunnel only when these
# are the same (the tunnel manager is keyed by slot id)
TUNNEL_KEYS = ("host", "ssh", "container", "os", "lanes_secrets", "agent_port", "kilo_port",
               "opencode_port")


def lane_slots(profile) -> dict:
    """{slot id: settings} of the enabled slots with `lanes = true`, in a project with
    [automation] remote_lanes on; {} otherwise (no tunnel, no secrets read)."""
    try:
        from .automation import get as auto_get
        if auto_get(profile, "remote_lanes") is not True:
            return {}
    except Exception:                                             # noqa: BLE001
        return {}
    return {sid: s for sid, s in SL.configured(profile).items() if s.get("lanes") is True}


def default_log_dir() -> str:
    """<the coordinator's state folder>/logs (slot_ssh knows the running one's)."""
    from . import slot_ssh
    return os.path.join(os.path.dirname(slot_ssh.ssh_dir()), "logs")


def probe_health(url: str, token: str, timeout: float = HEALTH_TIMEOUT_S) -> int:
    """GET <url>/api/lane/health with the lane token: the HTTP status, 0 for no answer."""
    req = urllib.request.Request(url.rstrip("/") + "/api/lane/health",
                                 headers={"X-Lane-Token": token or ""})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.status or 200)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return 0


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def port_open(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def remote_ports(slot: dict) -> dict:
    """The slot-side ports: [slots.<id>] agent_port / kilo_port / opencode_port or the
    defaults (the ports compose publishes on the slot host's 127.0.0.1)."""
    return {k: int(slot.get(f"{k}_port") or v) for k, v in PORTS.items()}


def tunnel_argv(slot: dict, local: dict) -> list:
    remote = remote_ports(slot)
    fwd = []
    for k in ("agent", "kilo", "opencode"):
        # bound to this machine's loopback explicitly (never every address, whatever
        # GatewayPorts says)
        fwd += ["-L", f"127.0.0.1:{local[k]}:127.0.0.1:{remote[k]}"]
    return SL.ssh_cmd(slot) + ["-N", "-o", "ExitOnForwardFailure=yes",
                               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                               *fwd, str(slot["host"])]


def secrets_argv(slot: dict) -> list:
    """Read the slot's lane secrets: `cat` inside its container (docker exec over ssh), or on
    the host itself for a slot without a container (a Windows slot, phase b-5)."""
    path = str(slot.get("lanes_secrets") or (WINDOWS_SECRETS_FILE if slot.get("os") == "windows"
                                              else SECRETS_FILE))
    # a Windows slot's OpenSSH runs Git for Windows' bash (install/slot.ps1), so `cat` works
    # there too; it has no container
    if slot.get("container") and slot.get("os") != "windows":
        return SL.exec_argv(slot, ["cat", path])
    return SL.host_argv(slot, ["cat", path])


class Tunnel:
    def __init__(self, sid: str, slot: dict):
        self.sid = sid
        self.slot = slot
        self.proc = None
        self.local: dict = {}
        self.secrets: dict = {}
        self.state = "down"            # down | starting | up | backoff
        self.since = 0.0
        self.error = ""
        self.failures = 0
        self.next_try = 0.0
        self.probed = 0.0              # last health probe
        self.misses = 0                # health probes in a row with no answer
        self.refreshed = -1e18         # last secrets re-read on a 401

    @property
    def remote(self) -> bool:
        return bool(str(self.slot.get("host") or "").strip())


class TunnelManager:
    def __init__(self, popen: Callable = subprocess.Popen, run: Callable = subprocess.run,
                 clock: Callable = time.time, pick_port: Callable = free_port,
                 is_open: Callable = port_open, sleep: Callable = time.sleep,
                 probe: Callable = probe_health, log_dir: Callable = default_log_dir):
        self.popen, self.run, self.clock = popen, run, clock
        self.pick_port, self.is_open, self.sleep = pick_port, is_open, sleep
        self.probe, self.log_dir = probe, log_dir
        self._lock = threading.RLock()
        self._tunnels: dict[str, Tunnel] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ setup

    def configure(self, slots: dict, start_thread: bool = True) -> None:
        """The slots that should have a tunnel ({sid: settings}); others are closed."""
        with self._lock:
            for sid in list(self._tunnels):
                if sid not in slots:
                    self._close(self._tunnels.pop(sid))
            for sid, s in slots.items():
                t = self._tunnels.get(sid)
                if t is None:
                    self._tunnels[sid] = Tunnel(sid, dict(s))
                elif t.slot != s:
                    self._close(t)
                    self._tunnels[sid] = Tunnel(sid, dict(s))
        if start_thread and slots and self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="lane-tunnels")
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:                                    # noqa: BLE001
                pass
            self._stop.wait(CHECK_EVERY_S)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for t in self._tunnels.values():
                self._close(t)

    # ------------------------------------------------------------------ the check

    def poll(self) -> None:
        with self._lock:
            tunnels = list(self._tunnels.values())
        for t in tunnels:
            self._check(t)

    def _check(self, t: Tunnel) -> None:
        now = self.clock()
        if t.state == "up":
            alive = (not t.remote or (t.proc is not None and t.proc.poll() is None))
            if not (alive and self.is_open(t.local["agent"])):
                self._fail(t, "the ssh tunnel exited" if t.remote and t.proc is not None
                           and t.proc.poll() is not None else "the lane agent stopped answering")
                return
            if now - t.probed >= HEALTH_EVERY_S:
                self._health(t)
            return
        if now < t.next_try:
            return
        self._start(t)

    def _health(self, t: Tunnel) -> None:
        """The agent answers with the token held here: 200 fine; 401 (it restarted with a new
        token) the secrets are read again once; no answer HEALTH_MISSES times, or a 401 the
        new secrets do not cure, restarts the tunnel (its _start reads the secrets too)."""
        t.probed = self.clock()
        url = f"http://127.0.0.1:{t.local['agent']}"
        st = self.probe(url, t.secrets.get("token", ""))
        if st == 401:
            self._reread(t)
            st = self.probe(url, t.secrets.get("token", ""))
            if st == 401:
                self._fail(t, "the lane agent refuses the token, also after reading the "
                              "secrets again")
                return
        if 200 <= st < 300:
            t.misses = 0
            return
        t.misses += 1
        if t.misses >= HEALTH_MISSES:
            self._fail(t, f"the lane agent did not answer its health check "
                          f"({'no answer' if not st else f'HTTP {st}'}, {t.misses} times)")

    def _reread(self, t: Tunnel) -> bool:
        """Read the slot's secrets again for a tunnel that is up: True when the token changed."""
        t.refreshed = self.clock()
        try:
            secrets = self._read_secrets(t.slot)
        except RuntimeError as e:
            t.error = str(e)
            return False
        with self._lock:
            changed = secrets.get("token") != t.secrets.get("token")
            if t.state == "up":
                t.secrets = secrets
        return changed

    def refresh(self, sid: str, used_token: str = "") -> bool:
        """A lane call got 401 with `used_token` (remote_git.LaneAgent): the slot's agent
        restarted and made a new token. True when the token held now differs from the one used
        (read again here, at most every REFRESH_GAP_S, or by another caller meanwhile)."""
        with self._lock:
            t = self._tunnels.get(sid)
            if t is None or t.state != "up":
                return False
            cur = t.secrets.get("token", "")
            if used_token and cur and cur != used_token:
                return True
            if self.clock() - t.refreshed < REFRESH_GAP_S:
                return False
        return self._reread(t)

    def _start(self, t: Tunnel) -> None:
        t.state, t.error = "starting", ""
        try:
            secrets = self._read_secrets(t.slot)
        except RuntimeError as e:
            self._fail(t, str(e))
            return
        if t.remote:
            local = {k: self.pick_port() for k in PORTS}
            log, start = self._open_log(t.sid)
            try:
                t.proc = self.popen(tunnel_argv(t.slot, local), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=log if log is not None else subprocess.DEVNULL,
                                    **_plat.no_window_kw())
            except OSError as e:
                self._fail(t, f"ssh could not start: {e}"[:300])
                return
            finally:
                if log is not None:
                    log.close()                 # ssh holds its own handle
            waited = 0.0
            while waited < READY_WAIT_S and not self.is_open(local["agent"]):
                if t.proc.poll() is not None:
                    self._fail(t, "ssh: " + self._log_tail(t.sid, start))
                    return
                self.sleep(0.25)
                waited += 0.25
            if not self.is_open(local["agent"]):
                self._fail(t, f"the tunnel did not open in {READY_WAIT_S} s")
                return
        else:
            local = remote_ports(t.slot)
            if not self.is_open(local["agent"]):
                self._fail(t, f"no lane agent on 127.0.0.1:{local['agent']}")
                return
        with self._lock:
            t.local, t.secrets = local, secrets
            t.state, t.since, t.failures, t.error = "up", self.clock(), 0, ""
            t.probed, t.misses = self.clock(), 0

    # ------------------------------------------------------------------ ssh's log

    def log_path(self, sid: str) -> str:
        return os.path.join(self.log_dir(), f"lane-tunnel-{sid}.log")

    def _open_log(self, sid: str) -> tuple:
        """(file, offset this start's lines begin at) for ssh's stderr, or (None, 0). The file
        starts over once it is over LOG_MAX_BYTES."""
        try:
            path = self.log_path(sid)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            mode = "wb" if os.path.isfile(path) and os.path.getsize(path) > LOG_MAX_BYTES else "ab"
            f = open(path, mode)
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} tunnel to {sid}\n".encode("utf-8"))
            f.flush()
            return f, f.tell()
        except OSError:
            return None, 0

    def _log_tail(self, sid: str, start: int) -> str:
        """What ssh wrote since `start` (its last 300 characters): the startup error."""
        try:
            with open(self.log_path(sid), "rb") as f:
                f.seek(start)
                return f.read().decode("utf-8", "replace").strip()[-300:]
        except OSError:
            return "(no ssh log)"

    def _read_secrets(self, slot: dict) -> dict:
        try:
            r = self.run(secrets_argv(slot), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, timeout=SECRETS_TIMEOUT_S, **_plat.no_window_kw())
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"reading the lane secrets timed out after {SECRETS_TIMEOUT_S} s")
        except OSError as e:
            raise RuntimeError(f"reading the lane secrets: {e}"[:300])
        if r.returncode != 0:
            # stderr only: stdout could hold part of a secret
            raise RuntimeError("reading the lane secrets failed: "
                               + (r.stderr or b"").decode("utf-8", "replace").strip()[-200:])
        try:
            vals = json.loads((r.stdout or b"").decode("utf-8"))
        except ValueError:
            raise RuntimeError("the lane secrets file is not JSON (is the lane agent running?)")
        if not isinstance(vals, dict) or not vals.get("token"):
            raise RuntimeError("the lane secrets file has no token")
        return {k: str(v) for k, v in vals.items() if isinstance(v, str)}

    def _fail(self, t: Tunnel, why: str) -> None:
        with self._lock:
            self._close(t)
            t.failures += 1
            t.error = why
            t.state = "backoff"
            t.since = self.clock()
            t.next_try = t.since + BACKOFF_S[min(t.failures, len(BACKOFF_S)) - 1]

    def _close(self, t: Tunnel) -> None:
        p, t.proc = t.proc, None
        if p is not None and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:                                    # noqa: BLE001
                try:
                    p.kill()
                except Exception:                                # noqa: BLE001
                    pass
        t.local, t.secrets = {}, {}
        if t.state == "up":
            t.state = "down"

    # ------------------------------------------------------------------ answers

    def endpoint(self, sid: str, kind: str = "agent") -> Optional[dict]:
        """{"url", "token" | "password"} for the slot's agent / kilo / opencode while its
        tunnel is up, else None. Read on every call, so new local ports are picked up."""
        with self._lock:
            t = self._tunnels.get(sid)
            if t is None or t.state != "up" or kind not in t.local:
                return None
            url = f"http://127.0.0.1:{t.local[kind]}"
            if kind == "agent":
                return {"url": url, "token": t.secrets.get("token", "")}
            return {"url": url, "password": t.secrets.get(f"{kind}_password", "")}

    def view(self) -> dict:
        """{sid: {state, since, error, failures}} for the board (no ports' secrets)."""
        with self._lock:
            return {sid: {"state": t.state, "since": t.since, "error": t.error,
                          "failures": t.failures, "remote": t.remote}
                    for sid, t in self._tunnels.items()}


MANAGER = TunnelManager()
_BY_PROJECT: dict[str, dict] = {}
_BY_PROJECT_LOCK = threading.Lock()
_CONFLICTS: dict = {}               # (project, slot) -> why it has no tunnel


def _tunnel_view(s: dict) -> dict:
    return {k: s.get(k) for k in TUNNEL_KEYS}


def ensure(profile) -> dict:
    """Record a profile's lane slots and configure the process-wide manager with every
    project's (a coordinator runs several projects; one project with none closes nothing of
    another's). No-op while no project has a lane slot.

    The manager has one tunnel per slot id. Two projects may share it when their settings for
    that slot agree on everything a tunnel is made from (TUNNEL_KEYS); a project whose
    settings differ from the project that named the slot first gets no tunnel to it (and no
    lanes there: conflict() says why) instead of silently using the other's."""
    global _CONFLICTS
    slots = lane_slots(profile)
    pid = str(getattr(profile, "id", "") or "")
    with _BY_PROJECT_LOCK:
        if slots:
            _BY_PROJECT[pid] = slots
        elif pid in _BY_PROJECT:
            _BY_PROJECT.pop(pid)
        else:
            return slots
        union: dict = {}
        owner: dict = {}
        conflicts: dict = {}
        for p, ss in _BY_PROJECT.items():
            for sid, s in ss.items():
                if sid in union and _tunnel_view(union[sid]) != _tunnel_view(s):
                    diff = [k for k in TUNNEL_KEYS if union[sid].get(k) != s.get(k)]
                    conflicts[(p, sid)] = (f"slot {sid} already has a lane tunnel for project "
                                           f"{owner[sid]} with other settings "
                                           f"({', '.join(diff)}): make them the same in both "
                                           f"projects, or use another slot id")
                    continue
                union.setdefault(sid, s)
                owner.setdefault(sid, p)
        _CONFLICTS = conflicts
    MANAGER.configure(union)
    return {sid: s for sid, s in slots.items() if (pid, sid) not in conflicts}


def conflict(project: str, sid: str) -> str:
    """Why `project` has no tunnel to `sid` ("" when it has, or never asked)."""
    with _BY_PROJECT_LOCK:
        return str(_CONFLICTS.get((str(project or ""), str(sid or ""))) or "")


__all__ = ["TunnelManager", "MANAGER", "ensure", "conflict", "lane_slots", "tunnel_argv",
           "secrets_argv", "remote_ports", "probe_health", "PORTS", "TUNNEL_KEYS"]
