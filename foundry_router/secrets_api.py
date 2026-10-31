"""Central secret store API (secret_store.py).

  pull   GET  /v1/foundry/secrets/bundle?project=&slot=   (client key, `secrets` scope)
         The secret values this key may read, plus the slot's non-secret settings
         from the project's deployed revision (`slots.<id>`). ETag / If-None-Match
         gives 304 when nothing changed, so a slot can pull at every gate run.
         Groups a key reads: `shared`, plus `project` when it is in the key's
         `projects` (a key with no projects reads `shared` only). `secret_names`
         and `slots` on the key narrow it further. Every pull is audited, refusals
         too. Values are never logged.

  admin  /admin/api/secrets...                            (the web UI, Setup → Secrets)
         Metadata, set / rotate / clear / delete, passphrase (set, unlock, lock,
         change), settings, pull log. No route returns a value.

Transport (Victor 2026-10-31): `secrets.allow_http` off = pulls, value writes and
passphrase entry only over the HTTPS listener (`secrets.https_enabled`).
The admin key is optional (`client_auth.protect_admin`). While it is off, the
page shows a warning, and new secret access raises an alert
(`secrets.alert_on_new_access`).
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from . import client_auth
from .secret_store import SHARED, GROUP_RE, SecretStoreError, check_group

router = APIRouter()
A = "/admin/api/secrets"


def _svc(request: Request):
    return request.app.state.services


def _store(request: Request):
    return _svc(request).secret_store


def _cfg(request: Request):
    return _svc(request).config_store.config


def _err(e: SecretStoreError) -> JSONResponse:
    return JSONResponse({"error": {"message": str(e), "code": e.code}}, status_code=e.status,
                        headers={"Cache-Control": "no-store"})


def _transport(request: Request) -> str:
    return "https" if request.scope.get("scheme") == "https" else "http"


def _need_transport(request: Request) -> None:
    """Value traffic (pulls, value writes, passphrases) over plain HTTP only when allowed."""
    sec = _cfg(request).secrets
    if _transport(request) == "http" and not sec.allow_http:
        where = (f"the HTTPS port {sec.https_port}" if sec.https_enabled
                 else "HTTPS (turn the HTTPS listener on first)")
        raise SecretStoreError(f"this router serves secrets only over HTTPS: use {where}",
                               "https_only", 403)


def _actor() -> str:
    c = client_auth.current()
    return str(c.get("client_id") or "anonymous")


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
    except Exception:                                            # noqa: BLE001
        raise SecretStoreError("the body must be JSON", "invalid_body")
    if not isinstance(b, dict):
        raise SecretStoreError("the body must be a JSON object", "invalid_body")
    return b


def _alert(request: Request, key: str, kind: str, severity: str, title: str, detail: str,
           project: str = "") -> None:
    try:
        _svc(request).control.raise_alert(key, kind, severity, title, detail,
                                          {"project_id": project} if project else {})
    except Exception:                                            # noqa: BLE001
        pass


def _resolve(request: Request, key: str, why: str) -> None:
    try:
        _svc(request).control._resolve(key, why)
    except Exception:                                            # noqa: BLE001
        pass


def _guard(fn):
    import functools

    @functools.wraps(fn)
    async def wrap(*a, **k):
        try:
            return await fn(*a, **k)
        except SecretStoreError as e:
            return _err(e)
    return wrap


# ----------------------------------------------------------------------------- pull

def slot_settings(svc, project: str, slot: str) -> dict:
    """The slot's settings in the project's deployed revision ({} when none)."""
    if not (project and slot):
        return {}
    try:
        dep = svc.automation.deployed(project)
    except Exception:                                            # noqa: BLE001
        return {}
    slots = (((dep or {}).get("doc") or {}).get("settings") or {}).get("slots") or {}
    s = slots.get(slot) if isinstance(slots, dict) else None
    return dict(s) if isinstance(s, dict) else {}


def _etag(served: list[dict], config: dict) -> str:
    h = hashlib.sha256(json.dumps({"s": [[s["group"], s["name"], s["file"], s["set_at"],
                                          hashlib.sha256(s["value"].encode()).hexdigest()]
                                         for s in served], "c": config},
                                  sort_keys=True, default=str).encode()).hexdigest()[:24]
    return f'"{h}"'


@router.get("/v1/foundry/secrets/bundle")
async def pull_bundle(request: Request, project: str = "", slot: str = ""):
    svc = _svc(request)
    store = svc.secret_store
    c = client_auth.current()
    who = str(c.get("client_id") or "anonymous")
    addr = str(c.get("client_addr") or "")
    agent = str(c.get("user_agent") or "")
    tr = _transport(request)
    project, slot = str(project or "").strip(), str(slot or "").strip()

    def refuse(e: SecretStoreError):
        store.record_pull(who, addr, agent, tr, project, slot, [], "refused", f"{e.code}: {e}")
        return _err(e)

    try:
        _need_transport(request)
        if project and not GROUP_RE.match(project):
            raise SecretStoreError("project: a project id", "invalid_project")
        allowed = c.get("projects") or []
        if project and project not in allowed:
            raise SecretStoreError(f"this key may not read project {project!r}", "forbidden", 403)
        slots = c.get("slots") or []
        if slots and slot not in slots:
            raise SecretStoreError(f"this key may not read slot {slot or '(none)'!r}",
                                   "forbidden", 403)
    except SecretStoreError as e:
        return refuse(e)
    groups = [SHARED] + ([project] if project and project != SHARED else [])
    config = slot_settings(svc, project, slot)
    names = list(c.get("secret_names") or [])
    wanted = [str(n) for n in (config.get("secrets") or []) if isinstance(n, str)]
    if wanted:                       # the slot's own list narrows what its key may read
        names = [n for n in names if n in wanted] if names else wanted
    try:
        served = store.bundle(groups, names or None) if (names or not wanted) else []
    except SecretStoreError as e:
        if e.code != "uninitialized":
            if e.status == 503:
                _alert(request, "secrets_locked", "secrets_locked", "high",
                       "Secret store is locked: slots can't pull their keys",
                       f"{who} tried to pull its secrets ({project or 'shared'}"
                       f"{'/' + slot if slot else ''}) at {addr or 'an unknown address'}, but "
                       "the store is locked (the router restarted). Unlock it on Setup → "
                       "Secrets. Slots keep the files they pulled last until then.", project)
            store.record_pull(who, addr, agent, tr, project, slot, [], "locked", e.code)
            return _err(e)
        # no passphrase was ever set, so there are no values: the slot still gets its settings
        # (a LOCKED store answers 503 above, so slots keep their files)
        served = []
    etag = _etag(served, config)
    first = not store.has_pulled(who)
    if (request.headers.get("if-none-match") or "").strip() == etag:
        store.record_pull(who, addr, agent, tr, project, slot, served, "unchanged")
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-store"})
    store.record_pull(who, addr, agent, tr, project, slot, served, "ok")
    if first and _cfg(request).secrets.alert_on_new_access:
        _alert(request, f"secrets_first_pull:{who}", "secrets_access", "info",
               f"{who} pulled secrets for the first time",
               f"From {addr or 'an unknown address'} over {tr.upper()}: "
               f"{', '.join(s['group'] + '/' + s['name'] for s in served) or 'no values'}. "
               "If you don't know this key, disable it on Access → Clients & Keys.", project)
    body = {"project": project, "slot": slot, "transport": tr,
            "secrets": [{"name": s["name"], "file": s["file"], "value": s["value"],
                         "set_at": s["set_at"]} for s in served],
            "config": config, "etag": etag}
    return JSONResponse(body, headers={"ETag": etag, "Cache-Control": "no-store"})


# ----------------------------------------------------------------------------- admin

def _keys_view(cfg) -> list[dict]:
    return [{"name": c.name, "enabled": c.enabled, "projects": list(c.projects or []),
             "secret_names": list(getattr(c, "secret_names", None) or []),
             "slots": list(getattr(c, "slots", None) or []), "note": c.note}
            for c in cfg.clients if "secrets" in (c.scopes or [])]


def readers(keys: list[dict], group: str, name: str) -> list[str]:
    """Names of the enabled keys that can read group/name."""
    out = []
    for k in keys:
        if not k["enabled"]:
            continue
        if group != SHARED and group not in k["projects"]:
            continue
        if k["secret_names"] and name not in k["secret_names"]:
            continue
        out.append(k["name"])
    return out


def _projects(svc) -> list[str]:
    try:
        ps = [p if isinstance(p, str) else p.get("project") or p.get("id")
              for p in svc.automation.projects()]
    except Exception:                                            # noqa: BLE001
        ps = []
    return sorted({str(p) for p in ps if p and GROUP_RE.match(str(p)) and p != "router"})


@router.get(A)
@_guard
async def overview(request: Request):
    svc = _svc(request)
    cfg = _cfg(request)
    store = svc.secret_store
    keys = _keys_view(cfg)
    items = store.list()
    for s in items:
        s["readers"] = readers(keys, s["group"], s["name"])
    tls = getattr(request.app.state, "tls", None)
    if not tls and cfg.secrets.https_enabled:
        from .config import data_dir
        from .tls import fingerprint, paths
        tls = {"port": cfg.secrets.https_port, "fingerprint": fingerprint(paths(data_dir())[0]),
               "listening": False}
    elif tls:
        tls = {**tls, "listening": True}
    return JSONResponse({
        "status": store.status(), "settings": cfg.secrets.model_dump(),
        "secrets": items, "keys": keys,
        "groups": [SHARED] + [p for p in sorted(set(_projects(svc)) | set(store.groups()))
                              if p != SHARED],
        "protect_admin": bool(cfg.client_auth.protect_admin),
        "transport": _transport(request), "tls": tls}, headers={"Cache-Control": "no-store"})


@router.post(A + "/init")
@_guard
async def init(request: Request):
    _need_transport(request)
    b = await _body(request)
    st = _store(request).initialize(b.get("passphrase"), _actor())
    _svc(request).db.log_event("info", "secrets", f"secret store passphrase set by {_actor()}")
    return st


@router.post(A + "/unlock")
@_guard
async def unlock(request: Request):
    _need_transport(request)
    b = await _body(request)
    try:
        st = _store(request).unlock(b.get("passphrase"))
    except SecretStoreError as e:
        if e.code == "wrong_passphrase":
            _svc(request).db.log_event("warning", "secrets",
                                       f"wrong secret store passphrase from {_actor()}")
        raise
    _resolve(request, "secrets_locked", "secret store unlocked")
    _svc(request).db.log_event("info", "secrets", f"secret store unlocked by {_actor()}")
    return st


@router.post(A + "/lock")
@_guard
async def lock(request: Request):
    _svc(request).db.log_event("info", "secrets", f"secret store locked by {_actor()}")
    return _store(request).lock()


@router.post(A + "/passphrase")
@_guard
async def change_passphrase(request: Request):
    _need_transport(request)
    b = await _body(request)
    st = _store(request).change_passphrase(b.get("old"), b.get("new"), _actor())
    _svc(request).db.log_event("info", "secrets", f"secret store passphrase changed by {_actor()}")
    return st


@router.post(A + "/define")
@_guard
async def define(request: Request):
    """{group, name, file?, description?}: add an entry (no value) or edit its file/description."""
    b = await _body(request)
    return _store(request).define(str(b.get("group") or ""), str(b.get("name") or ""),
                                  str(b.get("file") or ""), str(b.get("description") or ""))


@router.put(A + "/{group}/{name}")
@_guard
async def set_value(request: Request, group: str, name: str):
    """{value, file?, description?}: set or rotate. The value is in the body, never the URL,
    and is never echoed back."""
    _need_transport(request)
    b = await _body(request)
    store = _store(request)
    if b.get("file") or b.get("description"):
        try:
            cur = store.meta(check_group(group), name)
        except SecretStoreError:
            cur = {"file": "", "description": ""}
        store.define(group, name, str(b.get("file") or cur["file"]),
                     str(b.get("description") if b.get("description") is not None
                         else cur["description"]))
    m = store.set_value(group, name, b.get("value"), _actor())
    _svc(request).db.log_event("info", "secrets", f"secret {group}/{name} set by {_actor()}")
    return m


@router.post(A + "/{group}/{name}/clear")
@_guard
async def clear(request: Request, group: str, name: str):
    m = _store(request).clear(group, name, _actor())
    _svc(request).db.log_event("info", "secrets", f"secret {group}/{name} cleared by {_actor()}")
    return m


@router.delete(A + "/{group}/{name}")
@_guard
async def delete(request: Request, group: str, name: str):
    _store(request).delete(group, name)
    _svc(request).db.log_event("info", "secrets", f"secret {group}/{name} deleted by {_actor()}")
    return {"ok": True}


@router.get(A + "/pulls")
@_guard
async def pulls(request: Request, limit: int = 100, client: str = "", project: str = ""):
    return {"pulls": _store(request).pulls(limit, client, project)}


SETTINGS = {"allow_http": bool, "https_enabled": bool, "https_port": int,
            "pull_log_days": int, "alert_on_new_access": bool}


@router.post(A + "/settings")
@_guard
async def settings(request: Request):
    b = await _body(request)
    svc = _svc(request)
    upd = {}
    for k, t in SETTINGS.items():
        if k in b:
            try:
                upd[k] = t(b[k])
            except (TypeError, ValueError):
                raise SecretStoreError(f"{k}: expected {t.__name__}", "invalid_setting")
    cur = svc.config_store.config.secrets
    new = cur.model_copy(update=upd)
    if not new.allow_http and not new.https_enabled:
        raise SecretStoreError("turn the HTTPS listener on before refusing plain HTTP",
                               "invalid_setting")
    from .config import SecretsConfig
    try:
        SecretsConfig(**new.model_dump())
    except Exception as e:                                       # noqa: BLE001
        raise SecretStoreError(str(e).splitlines()[0], "invalid_setting")

    def mutate(raw):
        raw.setdefault("secrets", {})
        if raw["secrets"] is None:
            raw["secrets"] = {}
        raw["secrets"].update(upd)
    svc.config_store.save(mutate)
    restart = any(k in upd and upd[k] != getattr(cur, k) for k in ("https_enabled", "https_port"))
    return {"ok": True, "restart_needed": restart,
            "note": "Restart the router (docker compose up -d) to start or move the HTTPS "
                    "listener." if restart else ""}


def new_access_alert(svc, client_name: str, how: str) -> None:
    """Called by Setup → Clients & keys when a key gets the secrets scope."""
    cfg = svc.config_store.config
    if not cfg.secrets.alert_on_new_access:
        return
    try:
        svc.control.raise_alert(
            f"secrets_scope:{client_name}:{int(__import__('time').time())}", "secrets_access",
            "info", f"{client_name} can now read secrets",
            f"Client key {client_name} was {how} with the secrets scope. If you didn't do this, "
            "disable it on Access → Clients & Keys"
            + ("" if cfg.client_auth.protect_admin else
               " (the web UI has no admin key, so anyone on the network can make keys)") + ".",
            {})
    except Exception:                                            # noqa: BLE001
        pass


__all__ = ["router", "slot_settings", "readers", "new_access_alert"]
