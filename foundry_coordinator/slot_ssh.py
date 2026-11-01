"""The coordinator's own ssh identity for Studio slots, kept in sync with the router (v0.162.0).

At start and then every INTERVAL seconds (`run` adds `SlotSsh.poll` to the tick hooks):
  * makes Foundry's ssh key if this PC has none: <state_dir>/ssh/id_ed25519 (ed25519, no
    passphrase: the coordinator runs unattended). Your personal ~/.ssh keys are not touched;
  * registers its PUBLIC half with the router under this PC's name
    (POST /v1/foundry/control/workstation-ssh, the coordinator's orchestration key). Slot hosts
    allow every registered workstation key, so a new main PC only has to start its coordinator;
  * writes the slots' ssh HOST keys the router got from each slot host into
    <state_dir>/ssh/known_hosts (Foundry's own file, not ~/.ssh/known_hosts).

`ssh_options(host)` is what slots.ssh_cmd uses for every slot command (docker exec, Kilo's
Studio MCP, the git pushes):
  * Foundry's key first (then ssh's usual keys, so a slot that only knows a personal key still
    works until its key refresh runs);
  * a slot whose host key the router knows is checked STRICTLY against Foundry's known_hosts:
    a reinstalled slot or a reused IP is updated from the router, anything else is refused;
  * a slot that has not reported its host key yet (installed before v0.162.0) keeps the old
    trust on first use in ~/.ssh/known_hosts.
State for the Foundry Client: <state_dir>/ssh/state.json (no secrets: the public fingerprint,
counts, the last error).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from typing import Callable, Optional

INTERVAL = 600                       # seconds between registrations (also on every start)
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_dir: Optional[str] = None


def configure(state_dir: str) -> None:
    global _dir
    _dir = os.path.join(state_dir, "ssh")


def ssh_dir() -> str:
    """<state_dir>/ssh: the running coordinator's, else the CLI's default state folder."""
    if _dir:
        return _dir
    base = os.environ.get("FOUNDRY_COORD_STATE") or "~/.foundry-coordinator"
    return os.path.join(os.path.abspath(os.path.expanduser(base)), "ssh")


def key_path() -> str:
    return os.path.join(ssh_dir(), "id_ed25519")


def known_hosts_path() -> str:
    return os.path.join(ssh_dir(), "known_hosts")


def state_path() -> str:
    return os.path.join(ssh_dir(), "state.json")


def _ssh_path(p: str, option: bool = False) -> str:
    """A path for ssh: forward slashes (Windows OpenSSH and MSYS both read them). Inside an -o
    value ssh splits on spaces, so there it is double-quoted when it has one."""
    p = p.replace("\\", "/")
    return f'"{p}"' if option and " " in p else p


def known_hosts() -> set:
    """The host names/addresses Foundry's known_hosts has a key for."""
    out = set()
    try:
        with open(known_hosts_path(), encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and not parts[0].startswith("#"):
                    out.update(parts[0].split(","))
    except OSError:
        pass
    return out


def ssh_options(host: str) -> list:
    """ssh options for a slot at `host` (user@address or address)."""
    addr = str(host or "").split("@")[-1]
    out = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    if os.path.isfile(key_path()):
        # an -i turns off ssh's default keys, so they are listed after Foundry's: a slot that
        # only allows a personal key (installed before v0.162.0) still lets the coordinator in
        out += ["-i", _ssh_path(key_path())]
        for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
            p = os.path.join(os.path.expanduser("~"), ".ssh", name)
            if os.path.isfile(p):
                out += ["-i", _ssh_path(p)]
    if addr and addr in known_hosts():
        out += ["-o", "UserKnownHostsFile=" + _ssh_path(known_hosts_path(), option=True),
                "-o", "StrictHostKeyChecking=yes"]
    else:
        out += ["-o", "StrictHostKeyChecking=accept-new"]
    return out


def ensure_key(name: str = "") -> str:
    """Foundry's key pair (made once); returns the public key line."""
    k = key_path()
    if not (os.path.isfile(k) and os.path.isfile(k + ".pub")):
        os.makedirs(ssh_dir(), exist_ok=True)
        for p in (k, k + ".pub"):
            try:
                os.remove(p)
            except OSError:
                pass
        exe = shutil.which("ssh-keygen") or "ssh-keygen"
        r = subprocess.run([exe, "-q", "-t", "ed25519", "-N", "", "-C",
                            f"foundry-coordinator@{name or socket.gethostname()}", "-f", k],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=60, creationflags=_NO_WINDOW)
        if r.returncode != 0 or not os.path.isfile(k + ".pub"):
            raise RuntimeError("ssh-keygen failed: "
                               + (r.stdout or b"").decode("utf-8", "replace").strip()[-300:])
    with open(k + ".pub", encoding="utf-8") as f:
        return f.read().strip()


def write_known_hosts(slots: list) -> int:
    """Foundry's known_hosts from the router's slot list; returns the number of keys."""
    lines = []
    for s in slots or []:
        host = str(s.get("host") or "").strip()
        if not host or any(c.isspace() or c == "," for c in host):
            continue
        for k in s.get("host_keys") or []:
            parts = str(k).split()
            if len(parts) >= 2:
                lines.append(f"{host} {parts[0]} {parts[1]}")
    os.makedirs(ssh_dir(), exist_ok=True)
    tmp = known_hosts_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Foundry: Studio slot host keys from the router (slot_ssh.py); rewritten "
                "at every registration\n" + "".join(x + "\n" for x in lines))
    os.replace(tmp, known_hosts_path())
    return len(lines)


def _write_state(d: dict) -> None:
    try:
        os.makedirs(ssh_dir(), exist_ok=True)
        tmp = state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, state_path())
    except OSError:
        pass


def read_state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class SlotSsh:
    """Registers this PC's key with the router and refreshes the slots' host keys."""

    def __init__(self, coordinator, log: Callable[[str], None] = lambda m: None,
                 clock: Callable[[], float] = time.time, name: str = ""):
        self.c = coordinator
        self.log = log
        self.clock = clock
        self.name = name or socket.gethostname()
        self._last = 0.0
        self._last_error = None
        configure(coordinator.state_dir)

    def _client(self):
        for pid in sorted(self.c.projects):
            f = getattr(self.c.projects[pid], "foundry", None)
            if f is not None:
                return f
        return None

    def poll(self) -> None:
        if self.clock() - self._last < INTERVAL:
            return
        self._last = self.clock()
        self.sync()

    def sync(self) -> dict:
        state = {"ts": self.clock(), "name": self.name, "key": key_path().replace("\\", "/")}
        try:
            client = self._client()
            if client is None:
                raise RuntimeError("no router client ([foundry] url and key in project.toml)")
            pub = ensure_key(self.name)
            data, _ = client._req("POST", "/v1/foundry/control/workstation-ssh",
                                  {"name": self.name, "public_key": pub}, timeout=20)
            n = write_known_hosts(data.get("slots") or [])
            state.update({"ok": True, "status": data.get("status"),
                          "fingerprint": data.get("fingerprint"),
                          "slots": len(data.get("slots") or []), "host_keys": n, "error": ""})
            if data.get("status") in ("new", "changed") or self._last_error:
                self.log(f"slot ssh: key {data.get('fingerprint')} registered as {self.name} "
                         f"({data.get('status')}); {n} slot host key(s)")
            self._last_error = None
        except Exception as e:                                   # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"[:300]
            prev = read_state()
            state.update({k: prev.get(k) for k in ("fingerprint", "slots", "host_keys")})
            state.update({"ok": False, "error": msg})
            if msg != self._last_error:
                self.log(f"slot ssh: could not register with the router ({msg}); slots keep "
                         "the last known host keys")
            self._last_error = msg
        _write_state(state)
        return state


__all__ = ["SlotSsh", "ssh_options", "ensure_key", "write_known_hosts", "known_hosts",
           "key_path", "known_hosts_path", "read_state", "configure", "INTERVAL"]
