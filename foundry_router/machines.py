"""Machines and enrollment codes (Access → Machines, docs/INSTALL_SLOTS.md).

The router is the one place a machine is configured. Adding a machine here
  * (a Studio slot) writes `slots.<id>` into the project's settings as a new deployed revision
    (AutomationStore.put_slot), so the coordinator picks it up at its next config pull;
  * mints a one-time ENROLLMENT CODE (`FNDR-XXXX-XXXX-XXXX`, 60 random bits). Only its SHA-256 is
    stored. It works once and expires after `machines.code_minutes`.

The machine's installer (contrib/studio-slot/proxmox/setup-host.sh) trades the code for:
  * its client key, made at that moment (stored hashed; returned once, never shown in the UI);
  * its settings;
  * the router version (the Foundry release to install);
  * the workstation ssh public keys to authorize;
  * the HTTPS listener's fingerprint.
Nobody copies a key by hand.

    POST /v1/foundry/enroll {code, hostname?}      no client key: the code is the credential.
         Refused codes are rate-limited per address and audited; the same transport rule as
         secrets applies (secrets.allow_http).
    /admin/api/machines...                          the web UI.

Revoking a machine disables its client key. A new code re-enrolls it (a fresh key; the old one
stops working).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import client_auth, client_keys

router = APIRouter()
A = "/admin/api/machines"
SLOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
HOST_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"          # no 0/O, 1/I/L
FAIL_WINDOW_S, FAIL_MAX = 600, 10
KINDS = ("slot",)                                       # workstation / chat client: later phases

SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    name TEXT PRIMARY KEY, kind TEXT NOT NULL, project TEXT NOT NULL DEFAULT '',
    slot TEXT NOT NULL DEFAULT '', host TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
    created_ts REAL NOT NULL, created_by TEXT NOT NULL DEFAULT '',
    code_hash TEXT, code_expires REAL, code_made_ts REAL,
    enrolled_ts REAL, enrolled_addr TEXT, enrolled_hostname TEXT);
CREATE TABLE IF NOT EXISTS machine_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, machine TEXT NOT NULL DEFAULT '',
    event TEXT NOT NULL, addr TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '');
"""


class MachineError(Exception):
    def __init__(self, message: str, code: str = "invalid", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
    return f"FNDR-{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"


def norm_code(code: str) -> str:
    c = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
    return c[4:] if c.startswith("FNDR") and len(c) == 16 else c


def code_hash(code: str) -> str:
    return hashlib.sha256(("foundry-enroll:" + norm_code(code)).encode()).hexdigest()


def derived_slot(host: str, settings: dict) -> dict:
    """A remote slot's push targets and host folder from its host (setup-host.sh's layout)."""
    out = dict(settings)
    if host:
        out.setdefault("host", host)
        out.setdefault("au_push_url", f"ssh://{host}/srv/studio-slot/AnimalUprising.git")
        out.setdefault("foundry_push_url", f"ssh://{host}/srv/studio-slot/foundry.git")
        out.setdefault("foundry_host_dir", "/srv/studio-slot/foundry")
    return out


class Machines:
    def __init__(self, db, clock=time.time):
        self.db = db
        self.clock = clock
        self._fails: dict[str, list] = {}
        self._lock = threading.Lock()
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                db.execute(stmt)

    def event(self, machine: str, event: str, addr: str = "", detail: str = "") -> None:
        self.db.execute("INSERT INTO machine_events (ts, machine, event, addr, detail) VALUES "
                        "(?,?,?,?,?)", (self.clock(), machine[:80], event[:40], addr[:80],
                                        detail[:300]))

    def get(self, name: str) -> Optional[dict]:
        return self.db.query_one("SELECT * FROM machines WHERE name=?", (name,))

    def list(self) -> list[dict]:
        return self.db.query("SELECT * FROM machines ORDER BY kind, name")

    def events(self, limit: int = 50) -> list[dict]:
        return self.db.query("SELECT * FROM machine_events ORDER BY id DESC LIMIT ?",
                             (max(1, min(int(limit), 500)),))

    def add(self, name: str, kind: str, project: str, slot: str, host: str, note: str,
            actor: str) -> None:
        self.db.execute("INSERT INTO machines (name, kind, project, slot, host, note, created_ts, "
                        "created_by) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                        "kind=excluded.kind, project=excluded.project, slot=excluded.slot, "
                        "host=excluded.host, note=excluded.note",
                        (name, kind, project, slot, host, note[:200], self.clock(), actor[:80]))

    def mint(self, name: str, minutes: int) -> tuple[str, float]:
        code = new_code()
        exp = self.clock() + max(5, int(minutes)) * 60
        self.db.execute("UPDATE machines SET code_hash=?, code_expires=?, code_made_ts=? WHERE "
                        "name=?", (code_hash(code), exp, self.clock(), name))
        return code, exp

    def fail_check(self, addr: str) -> None:
        now = self.clock()
        with self._lock:
            xs = [t for t in self._fails.get(addr, []) if now - t < FAIL_WINDOW_S]
            self._fails[addr] = xs
            if len(xs) >= FAIL_MAX:
                raise MachineError("too many wrong enrollment codes from this address: wait "
                                   "10 minutes", "rate_limited", 429)

    def failed(self, addr: str) -> None:
        with self._lock:
            self._fails.setdefault(addr, []).append(self.clock())

    def redeem(self, code: str) -> dict:
        """The machine row for a valid, unused, unexpired code; the code is spent at once."""
        h = code_hash(code)
        row = self.db.query_one("SELECT * FROM machines WHERE code_hash=?", (h,))
        if not row:
            raise MachineError("unknown or already used enrollment code", "bad_code", 403)
        if float(row.get("code_expires") or 0) < self.clock():
            self.db.execute("UPDATE machines SET code_hash=NULL WHERE name=?", (row["name"],))
            raise MachineError("this enrollment code expired: make a new one on Access → "
                               "Machines", "expired_code", 403)
        n = self.db.execute_count("UPDATE machines SET code_hash=NULL WHERE name=? AND "
                                  "code_hash=?", (row["name"], h))
        if n != 1:
            raise MachineError("unknown or already used enrollment code", "bad_code", 403)
        return row

    def enrolled(self, name: str, addr: str, hostname: str) -> None:
        self.db.execute("UPDATE machines SET enrolled_ts=?, enrolled_addr=?, enrolled_hostname=? "
                        "WHERE name=?", (self.clock(), addr[:80], hostname[:80], name))

    def remove(self, name: str) -> None:
        self.db.execute("DELETE FROM machines WHERE name=?", (name,))


# ----------------------------------------------------------------------------- helpers

def _svc(request: Request):
    return request.app.state.services


def _cfg(request: Request):
    return _svc(request).config_store.config


def _err(e) -> JSONResponse:
    return JSONResponse({"error": {"message": str(e), "code": getattr(e, "code", "error")}},
                        status_code=getattr(e, "status", 400),
                        headers={"Cache-Control": "no-store"})


def _guard(fn):
    import functools

    @functools.wraps(fn)
    async def wrap(*a, **k):
        from .automation_store import StoreError
        try:
            return await fn(*a, **k)
        except (MachineError, StoreError) as e:
            return _err(e)
    return wrap


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
    except Exception:                                            # noqa: BLE001
        raise MachineError("the body must be JSON")
    if not isinstance(b, dict):
        raise MachineError("the body must be a JSON object")
    return b


def _actor() -> str:
    return str(client_auth.current().get("client_id") or "anonymous")


def _addr() -> str:
    return str(client_auth.current().get("client_addr") or "")


def _client(cfg, name: str):
    return next((c for c in cfg.clients if c.name == name), None)


def _tls(request: Request) -> dict:
    cfg = _cfg(request)
    tls = getattr(request.app.state, "tls", None) or {}
    fp = tls.get("fingerprint")
    if not fp and cfg.secrets.https_enabled:
        from .config import data_dir
        from .tls import fingerprint, paths
        fp = fingerprint(paths(data_dir())[0])
    return {"enabled": bool(cfg.secrets.https_enabled), "port": cfg.secrets.https_port,
            "fingerprint": fp or "", "allow_http": bool(cfg.secrets.allow_http)}


def _install_commands(request: Request, m: dict, code: str) -> dict:
    """What to run where (shown with the code)."""
    from . import __version__
    host = (request.headers.get("host") or "").split(":")[0]
    url = f"http://{host}:{_cfg(request).server.port}" if host else ""
    raw = (f"https://raw.githubusercontent.com/Vipast82/foundry-router/v{__version__}/"
           "contrib/studio-slot/proxmox")
    return {
        "router_url": url,
        "proxmox_host": f"curl -fsSLO {raw}/create-slot-lxc.sh && bash create-slot-lxc.sh",
        "inside_lxc": (f"apt-get update -qq && apt-get install -y -qq curl && "
                       f"curl -fsSLO {raw}/setup-host.sh && bash setup-host.sh"),
        "code": code,
    }


def _view(svc, m: dict, act_seen: dict) -> dict:
    cfg = svc.config_store.config
    c = _client(cfg, m["name"])
    from .secrets_api import slot_settings
    pulls = []
    try:
        pulls = svc.secret_store.pulls(1, client=m["name"])
    except Exception:                                            # noqa: BLE001
        pass
    seen = act_seen.get(m["name"]) or {}
    settings = slot_settings(svc, m["project"], m["slot"]) if m["kind"] == "slot" else {}
    return {**{k: m.get(k) for k in ("name", "kind", "project", "slot", "host", "note",
                                     "created_ts", "created_by", "enrolled_ts", "enrolled_addr",
                                     "enrolled_hostname")},
            "code_pending": bool(m.get("code_hash")) and float(m.get("code_expires") or 0) > time.time(),
            "code_expires": m.get("code_expires") if m.get("code_hash") else None,
            "key": ("none" if c is None else "enabled" if c.enabled else "disabled"),
            "last_seen": seen.get("last_ts"), "last_addr": seen.get("addr"),
            "last_pull": pulls[0] if pulls else None,
            "settings": settings,
            "vnc": _vnc_view(svc, m, settings) if m["kind"] == "slot" else None}


VNC_SECRET = "SLOT_VNC_PASSWORD"          # entrypoint.sh reads its default file name
VNC_FILE = "slot_vnc_password.txt"


def _vnc_view(svc, m: dict, settings: dict) -> dict:
    """The slot's noVNC: on the LAN or not, its port and URL, and whether a VNC password is set
    for it (the SLOT_VNC_PASSWORD secret, readable by this slot's key, default file name)."""
    from .secrets_api import _keys_view, readers
    lan = settings.get("vnc_lan", True) is not False
    port = int(settings.get("vnc_port") or 6080)
    ip = (m.get("host") or "").split("@")[-1]
    password = False
    try:
        keys = _keys_view(svc.config_store.config)
        for s in svc.secret_store.list():
            if (s["name"] == VNC_SECRET and s.get("set") and s.get("file") == VNC_FILE
                    and m["name"] in readers(keys, s["group"], s["name"])):
                password = True
    except Exception:                                            # noqa: BLE001
        pass
    return {"lan": lan, "port": port, "password": password,
            "url": f"http://{ip}:{port}/" if lan and ip else ""}


# ----------------------------------------------------------------------------- admin API

@router.get(A)
@_guard
async def list_machines(request: Request):
    svc = _svc(request)
    act = client_keys.activity.snapshot()["seen"]
    from .secrets_api import _projects
    return JSONResponse({
        "machines": [_view(svc, m, act) for m in svc.machines.list()],
        "settings": svc.config_store.config.machines.model_dump(),
        "projects": _projects(svc), "kinds": list(KINDS), "tls": _tls(request),
        "events": svc.machines.events(30)}, headers={"Cache-Control": "no-store"})


def _slot_settings_from(b: dict) -> dict:
    out = {}
    for k in ("container", "place_id", "universe_id", "rojo_never_place_ids", "enabled",
              "checkout_timeout_s", "vnc_lan", "vnc_port"):
        if k in b and b[k] not in (None, ""):
            out[k] = b[k]
    for k in ("place_id", "universe_id", "checkout_timeout_s", "vnc_port"):
        if k in out:
            try:
                out[k] = int(out[k])
            except (TypeError, ValueError):
                raise MachineError(f"{k}: a whole number")
    if "vnc_port" in out and not 1 <= out["vnc_port"] <= 65535:
        raise MachineError("vnc_port: a port number (1-65535)")
    if "vnc_lan" in out:
        out["vnc_lan"] = out["vnc_lan"] is True or str(out["vnc_lan"]).lower() in ("1", "true", "on")
    if "rojo_never_place_ids" in out:
        v = out["rojo_never_place_ids"]
        v = re.split(r"[\s,]+", v) if isinstance(v, str) else v
        try:
            out["rojo_never_place_ids"] = [int(x) for x in v if str(x).strip()]
        except (TypeError, ValueError):
            raise MachineError("rojo_never_place_ids: whole numbers")
    link = str(b.get("place_link") or "")
    if link:                                 # a roblox-studio: link or a games URL: take the ids
        p = re.search(r"placeId[:=](\d+)", link) or re.search(r"/games/(\d+)", link)
        u = re.search(r"universeId[:=](\d+)", link)
        if p:
            out["place_id"] = int(p.group(1))
        if u:
            out["universe_id"] = int(u.group(1))
    return out


@router.post(A)
@_guard
async def add_machine(request: Request):
    """{kind: slot, project, slot, ip, user?, place_link? | place_id, universe_id, container?,
    note?} -> the machine, its settings and a one-time code (shown once)."""
    svc = _svc(request)
    cfg = svc.config_store.config
    b = await _body(request)
    kind = str(b.get("kind") or "slot")
    if kind not in KINDS:
        raise MachineError(f"kind: one of {', '.join(KINDS)}")
    project = str(b.get("project") or "").strip()
    slot = str(b.get("slot") or "").strip()
    ip = str(b.get("ip") or "").strip()
    user = str(b.get("user") or "studio").strip()
    if not project:
        raise MachineError("project: which game this slot works for")
    if not SLOT_RE.match(slot):
        raise MachineError("slot: an id like slot-2 (letters, digits, _ and -)")
    if not HOST_RE.match(ip) or not HOST_RE.match(user):
        raise MachineError("ip: the slot host's address (e.g. 192.168.0.52)")
    name = slot
    other = _client(cfg, name)
    known = svc.machines.get(name)
    if other is not None and known is None:
        raise MachineError(f"a client key named {name!r} already exists (not a machine): "
                           "rename or delete it on Clients & Keys first", "name_taken", 409)
    # off until its installer reports the container ready (POST /v1/foundry/secrets/ready), so
    # no gate is sent to a slot that is still being built
    settings = derived_slot(f"{user}@{ip}", {"container": "studio-slot",
                                             **_slot_settings_from(b), "enabled": False})
    rev = svc.automation.put_slot(project, slot, settings, _actor(),
                                  note=f"Access → Machines: slot {slot} on {ip}")
    svc.machines.add(name, kind, project, slot, f"{user}@{ip}", str(b.get("note") or ""),
                     _actor())
    code, exp = svc.machines.mint(name, cfg.machines.code_minutes)
    svc.machines.event(name, "added", _addr(), f"{project}/{slot} on {ip}, settings rev "
                                               f"{rev.get('rev')}")
    m = svc.machines.get(name)
    return JSONResponse({"machine": _view(svc, m, {}), "code": code, "code_expires": exp,
                         "install": _install_commands(request, m, code),
                         "settings_rev": rev.get("rev")},
                        headers={"Cache-Control": "no-store"})


@router.post(A + "/{name}/code")
@_guard
async def new_machine_code(request: Request, name: str):
    """A fresh one-time code (reinstall / re-enroll). Enrolling with it replaces the key."""
    svc = _svc(request)
    m = svc.machines.get(name)
    if not m:
        raise MachineError(f"no machine {name!r}", "not_found", 404)
    code, exp = svc.machines.mint(name, svc.config_store.config.machines.code_minutes)
    svc.machines.event(name, "code_made", _addr())
    return JSONResponse({"code": code, "code_expires": exp,
                         "install": _install_commands(request, m, code)},
                        headers={"Cache-Control": "no-store"})


@router.post(A + "/{name}/vnc")
@_guard
async def machine_vnc(request: Request, name: str):
    """{vnc_lan, vnc_port}: the slot's noVNC settings (a new deployed settings revision).
    The slot host applies them when its installer runs again (New code)."""
    svc = _svc(request)
    m = svc.machines.get(name)
    if not m or m["kind"] != "slot":
        raise MachineError(f"no slot {name!r}", "not_found", 404)
    b = await _body(request)
    s = {k: v for k, v in _slot_settings_from(b).items() if k in ("vnc_lan", "vnc_port")}
    if not s:
        raise MachineError("vnc_lan and/or vnc_port")
    rev = svc.automation.put_slot(m["project"], m["slot"], s, _actor(),
                                  note=f"Access → Machines: {name} VNC")
    svc.machines.event(name, "vnc_changed", _addr(),
                       ", ".join(f"{k}={v}" for k, v in s.items()))
    from .secrets_api import slot_settings
    return {"ok": True, "settings_rev": rev.get("rev"),
            "vnc": _vnc_view(svc, m, slot_settings(svc, m["project"], m["slot"]))}


@router.post(A + "/{name}/revoke")
@_guard
async def revoke_machine(request: Request, name: str):
    """Disable the machine's key (it loses access at once) and cancel a pending code."""
    svc = _svc(request)
    if not svc.machines.get(name):
        raise MachineError(f"no machine {name!r}", "not_found", 404)
    svc.db.execute("UPDATE machines SET code_hash=NULL WHERE name=?", (name,))

    def mutate(raw):
        for c in raw.get("clients") or []:
            if c.get("name") == name:
                c["enabled"] = False
    svc.config_store.save(mutate)
    svc.machines.event(name, "revoked", _addr())
    return {"ok": True}


@router.delete(A + "/{name}")
@_guard
async def delete_machine(request: Request, name: str, remove_slot: bool = True):
    """Forget the machine, delete its key and (default) remove its slot from the settings."""
    svc = _svc(request)
    m = svc.machines.get(name)
    if not m:
        raise MachineError(f"no machine {name!r}", "not_found", 404)
    if m["kind"] == "slot" and remove_slot:
        svc.automation.put_slot(m["project"], m["slot"], None, _actor(),
                                note=f"Access → Machines: slot {m['slot']} removed")

    def mutate(raw):
        raw["clients"] = [c for c in raw.get("clients") or [] if c.get("name") != name]
    svc.config_store.save(mutate)
    svc.machines.remove(name)
    svc.machines.event(name, "deleted", _addr())
    return {"ok": True}


@router.post(A + "/settings")
@_guard
async def machine_settings(request: Request):
    """{slot_ssh_keys?: [public keys], code_minutes?}"""
    svc = _svc(request)
    b = await _body(request)
    upd = {}
    if "slot_ssh_keys" in b:
        keys = b["slot_ssh_keys"]
        keys = keys.splitlines() if isinstance(keys, str) else list(keys or [])
        keys = [str(k).strip() for k in keys if str(k).strip()]
        for k in keys:
            if not re.match(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+|sk-ssh-ed25519@openssh\.com)"
                            r" [A-Za-z0-9+/=]{40,}( [^\n]{0,200})?$", k):
                raise MachineError("slot_ssh_keys: one PUBLIC key per line (ssh-ed25519 AAAA… "
                                   "comment); never a private key")
        upd["slot_ssh_keys"] = keys
    if "code_minutes" in b:
        try:
            upd["code_minutes"] = max(5, min(1440, int(b["code_minutes"])))
        except (TypeError, ValueError):
            raise MachineError("code_minutes: a whole number")

    def mutate(raw):
        raw.setdefault("machines", {})
        if raw["machines"] is None:
            raw["machines"] = {}
        raw["machines"].update(upd)
    svc.config_store.save(mutate)
    return {"ok": True}


# ----------------------------------------------------------------------------- enroll

@router.post("/v1/foundry/enroll")
async def enroll(request: Request):
    svc = _svc(request)
    cfg = svc.config_store.config
    addr = _addr()
    try:
        svc.machines.fail_check(addr)
        if request.scope.get("scheme") != "https" and not cfg.secrets.allow_http:
            raise MachineError("this router hands out keys only over HTTPS: use the HTTPS port "
                               f"{cfg.secrets.https_port}", "https_only", 403)
        b = await _body(request)
        try:
            m = svc.machines.redeem(str(b.get("code") or ""))
        except MachineError:
            svc.machines.failed(addr)
            svc.machines.event("", "enroll_refused", addr, "bad or expired code")
            raise
    except MachineError as e:
        return _err(e)
    name = m["name"]
    key = client_keys.generate_key()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    hostname = re.sub(r"[^A-Za-z0-9_.-]", "", str(b.get("hostname") or ""))[:64]
    entry = {"name": name, "key_hash": client_keys.hash_key(key),
             "key_hint": client_keys.hint(key), "scopes": ["secrets"],
             "projects": [m["project"]] if m["project"] else [],
             "slots": [m["slot"]] if m["slot"] else [], "secret_names": [], "enabled": True,
             "note": f"Studio slot {m['slot']} ({hostname or addr}, enrolled {now[:10]})",
             "created": now}

    def mutate(raw):
        raw.setdefault("clients", [])
        if raw["clients"] is None:
            raw["clients"] = []
        raw["clients"] = [c for c in raw["clients"] if c.get("name") != name] + [entry]
    svc.config_store.save(mutate)
    svc.machines.enrolled(name, addr, hostname)
    svc.machines.event(name, "enrolled", addr, hostname)
    try:
        svc.control.raise_alert(
            f"machine_enrolled:{name}:{int(time.time())}", "machine_enrolled", "info",
            f"{name} enrolled", f"Machine {name} ({m['project']}/{m['slot']}) enrolled from "
            f"{hostname or '?'} at {addr or '?'} and got its key. If you didn't run its "
            "installer, revoke it on Access → Machines.", {"project_id": m["project"]})
    except Exception:                                            # noqa: BLE001
        pass
    from . import __version__
    from .secrets_api import slot_settings
    body = {"machine": name, "kind": m["kind"], "project": m["project"], "slot": m["slot"],
            "key": key, "router_version": __version__,
            "settings": slot_settings(svc, m["project"], m["slot"]),
            "vnc": _vnc_view(svc, m, slot_settings(svc, m["project"], m["slot"])),
            "ssh_authorized_keys": list(cfg.machines.slot_ssh_keys or []),
            "tls": _tls(request)}
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


@router.post("/v1/foundry/secrets/ready")
async def machine_ready(request: Request):
    """A slot's installer, once its container runs and pulled its secrets: switch the slot on
    (secrets-scope key; only the caller's own slot)."""
    svc = _svc(request)
    c = client_auth.current()
    name = str(c.get("client_id") or "")
    m = svc.machines.get(name)
    if not m or m["kind"] != "slot" or m["slot"] not in (c.get("slots") or []):
        return _err(MachineError("this key is not an enrolled slot", "forbidden", 403))
    try:
        rev = svc.automation.put_slot(m["project"], m["slot"], {"enabled": True}, name,
                                      note=f"slot {m['slot']} ready")
    except Exception as e:                                       # noqa: BLE001
        return _err(e)
    svc.machines.event(name, "ready", _addr())
    return {"ok": True, "settings_rev": rev.get("rev")}


__all__ = ["router", "Machines", "derived_slot", "new_code", "norm_code", "code_hash"]
