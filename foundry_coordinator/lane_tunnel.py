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
checks the tunnels every few seconds. With no slot set to lanes, nothing starts.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from typing import Callable, Optional

import foundry_platform as _plat

from . import slots as SL

CHECK_EVERY_S = 5.0
BACKOFF_S = (5, 10, 20, 40, 80, 120)
SECRETS_FILE = "/run/secrets/lane/lane.json"
PORTS = {"agent": 47811, "kilo": 4096, "opencode": 4097}
SECRETS_TIMEOUT_S = 30
READY_WAIT_S = 10


def lane_slots(profile) -> dict:
    """{slot id: settings} of the enabled slots with `lanes = true`."""
    return {sid: s for sid, s in SL.configured(profile).items() if s.get("lanes") is True}


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
        fwd += ["-L", f"{local[k]}:127.0.0.1:{remote[k]}"]
    return SL.ssh_cmd(slot) + ["-N", "-o", "ExitOnForwardFailure=yes",
                               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                               *fwd, str(slot["host"])]


def secrets_argv(slot: dict) -> list:
    """Read the slot's lane secrets: `cat` inside its container (docker exec over ssh), or on
    the host itself for a slot without a container (a Windows slot, phase b-5)."""
    path = str(slot.get("lanes_secrets") or SECRETS_FILE)
    if slot.get("container"):
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

    @property
    def remote(self) -> bool:
        return bool(str(self.slot.get("host") or "").strip())


class TunnelManager:
    def __init__(self, popen: Callable = subprocess.Popen, run: Callable = subprocess.run,
                 clock: Callable = time.time, pick_port: Callable = free_port,
                 is_open: Callable = port_open, sleep: Callable = time.sleep):
        self.popen, self.run, self.clock = popen, run, clock
        self.pick_port, self.is_open, self.sleep = pick_port, is_open, sleep
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
            if alive and self.is_open(t.local["agent"]):
                return
            self._fail(t, "the ssh tunnel exited" if t.remote and t.proc is not None
                       and t.proc.poll() is not None else "the lane agent stopped answering")
            return
        if now < t.next_try:
            return
        self._start(t)

    def _start(self, t: Tunnel) -> None:
        t.state, t.error = "starting", ""
        try:
            secrets = self._read_secrets(t.slot)
        except RuntimeError as e:
            self._fail(t, str(e))
            return
        if t.remote:
            local = {k: self.pick_port() for k in PORTS}
            try:
                t.proc = self.popen(tunnel_argv(t.slot, local), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    **_plat.no_window_kw())
            except OSError as e:
                self._fail(t, f"ssh could not start: {e}"[:300])
                return
            waited = 0.0
            while waited < READY_WAIT_S and not self.is_open(local["agent"]):
                if t.proc.poll() is not None:
                    err = (t.proc.stderr.read() if t.proc.stderr else b"") or b""
                    self._fail(t, "ssh: " + err.decode("utf-8", "replace").strip()[-300:])
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


def ensure(profile) -> dict:
    """Record a profile's lane slots and configure the process-wide manager with every
    project's (a coordinator runs several projects; one project with none closes nothing of
    another's). No-op while no project has a lane slot."""
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
        for s in _BY_PROJECT.values():
            union.update(s)
    MANAGER.configure(union)
    return slots


__all__ = ["TunnelManager", "MANAGER", "ensure", "lane_slots", "tunnel_argv", "secrets_argv",
           "remote_ports", "PORTS"]
