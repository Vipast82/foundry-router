"""Setup API: client keys and backend limits, managed from the web UI
(Setup → Clients & keys, Setup → Helpers & limits) instead of config.yaml.

Every change goes through config_store.save, so it lands in config.yaml,
is validated before it is written, and shows up in config history (with
rollback) like any other edit. Keys made here are stored hashed and shown
exactly once (client_keys.py).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import client_auth, client_keys
from ..kilo_roles import CANONICAL, COORDINATOR_DIRECT_ROLES, coordinator_role_gaps

router = APIRouter()

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,39}$")
SCOPES = ("inference", "media", "admin", "orchestration", "telemetry", "operator")
# What each kind of client usually needs, offered as presets in the UI.
PRESETS = {
    "cline": {"scopes": ["inference"], "note": "Cline (VS Code), OpenAI-compatible provider"},
    "kilo": {"scopes": ["inference", "media"], "note": "Kilo Code / kilo serve"},
    # Direct reviews are the only inference the coordinator makes with its own
    # key; the worker and helpers run on Kilo's key. So: reviewer roles only,
    # by canonical id (legacy aliases don't authorize them). No admin, no Local.
    "coordinator": {"scopes": ["inference", "orchestration"],
                    "note": "foundry_coordinator (FOUNDRY_COORDINATOR_KEY)",
                    "personas": [CANONICAL[r] for r in COORDINATOR_DIRECT_ROLES],
                    "roles": {r: CANONICAL[r] for r in COORDINATOR_DIRECT_ROLES},
                    "roles_note": "reviewer (required: normal direct review) and deep_reviewer "
                                  "(high-risk tags). The primary worker (Local) and helpers use "
                                  "Kilo's own key, so this key doesn't need them."},
    "openwebui": {"scopes": ["inference"], "note": "Open WebUI / AnythingLLM / chat client"},
    "training": {"scopes": ["orchestration"], "note": "foundry_training pipeline (FOUNDRY_TRAINING_KEY)"},
    "admin": {"scopes": ["admin"], "note": "web UI admin key (needed if the UI is protected)"},
    "operator": {"scopes": ["operator"], "note": "a person using the coordinator control center"},
    "bridge": {"scopes": ["telemetry"], "note": "workstation telemetry bridge "
                                                "(python -m foundry_coordinator bridge)"},
}


def _svc(request: Request):
    return request.app.state.services


def _err(msg: str, status: int = 400):
    return JSONResponse({"error": msg}, status_code=status)


def _base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return f"{proto}://{host}".rstrip("/")


def _usage(svc) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        rows = svc.db.query("SELECT COALESCE(client_id, 'anonymous') AS client_id, COUNT(*) AS n, "
                            "MAX(ts) AS last FROM request_log WHERE ts >= ? GROUP BY 1", (since,))
    except Exception:                                             # noqa: BLE001
        return {}
    return {r["client_id"]: {"calls_24h": r["n"], "last_logged": r["last"]} for r in rows}


def _view(c, usage: dict, seen: dict) -> dict:
    return {"name": c.name, "scopes": list(c.scopes), "personas": list(c.personas),
            "projects": list(getattr(c, "projects", None) or []), "enabled": c.enabled, "note": c.note, "created": c.created,
            "route_footer": bool(getattr(c, "route_footer", False)),
            "mcp_with_client_tools": getattr(c, "mcp_with_client_tools", None),
            "key_kind": client_keys.key_kind(c),
            "key_hint": c.key_hint,
            "usage": usage.get(c.name) or {}, "seen": seen.get(c.name),
            # diagnostics only: an existing allowlist is never changed for you
            "role_gaps": coordinator_role_gaps(list(c.scopes), list(c.personas))}


def _admins(clients, exclude: str = "") -> list:
    return [c for c in clients if c.enabled and "admin" in c.scopes and c.name != exclude
            and (c.key or c.key_hash)]


@router.get("/admin/api/setup/clients")
async def list_clients(request: Request):
    svc = _svc(request)
    cfg = svc.config_store.config
    act = client_keys.activity.snapshot()
    usage = _usage(svc)
    base = _base_url(request)
    return {"auth": cfg.client_auth.model_dump(),
            "clients": [_view(c, usage, act["seen"]) for c in cfg.clients],
            "anonymous": usage.get("anonymous") or {},
            "rejected": act["rejected"],
            "presets": PRESETS, "scopes": list(SCOPES),
            "personas": sorted(p["virtual_name"] for p in svc.personas.list()
                               if (p.get("kind") or "chat") == "chat"),
            "you": client_auth.current().get("client_id"),
            "endpoints": {"openai": base + "/v1", "ollama": base, "foundry": base}}


def _find(cfg, name):
    return next((c for c in cfg.clients if c.name == name), None)


def _clean_scopes(v) -> list:
    scopes = [s for s in (v or []) if s in SCOPES]
    if not scopes:
        raise ValueError("pick at least one scope")
    return scopes


@router.post("/admin/api/setup/clients")
async def create_client(request: Request):
    """{name, scopes?, personas?, note?} -> a new client with a fresh key,
    returned once (only its hash is stored)."""
    svc = _svc(request)
    body = await request.json()
    name = str(body.get("name") or "").strip()
    if not NAME_RE.match(name):
        return _err("name: letters, digits and . _ : - (up to 40), starting with a letter or digit")
    if _find(svc.config_store.config, name):
        return _err(f"a client named {name!r} already exists (rotate its key instead)", 409)
    try:
        scopes = _clean_scopes(body.get("scopes") or ["inference"])
    except ValueError as e:
        return _err(str(e))
    key = client_keys.generate_key()
    entry = {"name": name, "key_hash": client_keys.hash_key(key),
             "key_hint": client_keys.hint(key), "scopes": scopes,
             "personas": [str(p) for p in body.get("personas") or []],
             "projects": [str(p) for p in body.get("projects") or []],
             "note": str(body.get("note") or "")[:200],
             "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def mutate(raw):
        raw.setdefault("clients", [])
        if raw["clients"] is None:
            raw["clients"] = []
        raw["clients"].append(entry)
    svc.config_store.save(mutate)
    svc.db.log_event("info", "client_auth", f"client {name} created (scopes {', '.join(scopes)})")
    return {"ok": True, "key": key, "client": entry | {"key_hash": None},
            "shown_once": "Copy this key now: Foundry stores only its hash."}


@router.post("/admin/api/setup/clients/{name}/rotate")
async def rotate_client(request: Request, name: str):
    """A new key for an existing client; the old one stops working at once.
    A client whose key was in config.yaml becomes web-managed."""
    svc = _svc(request)
    if not _find(svc.config_store.config, name):
        return _err(f"no client named {name!r}", 404)
    key = client_keys.generate_key()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def mutate(raw):
        for c in raw.get("clients") or []:
            if c.get("name") == name:
                c.pop("key", None)
                c.update(key_hash=client_keys.hash_key(key), key_hint=client_keys.hint(key),
                         created=now)
    svc.config_store.save(mutate)
    svc.db.log_event("info", "client_auth", f"client {name}: key rotated")
    return {"ok": True, "key": key, "key_hint": client_keys.hint(key),
            "shown_once": "Copy this key now: Foundry stores only its hash."}


@router.patch("/admin/api/setup/clients/{name}")
async def update_client(request: Request, name: str):
    """{scopes?, personas?, note?, enabled?}. The name is the client's
    identifier in logs and budget rules, so it isn't renamed here."""
    svc = _svc(request)
    cfg = svc.config_store.config
    cur = _find(cfg, name)
    if not cur:
        return _err(f"no client named {name!r}", 404)
    body = await request.json()
    upd = {}
    try:
        if "scopes" in body:
            upd["scopes"] = _clean_scopes(body["scopes"])
    except ValueError as e:
        return _err(str(e))
    if "personas" in body:
        upd["personas"] = [str(p) for p in body["personas"] or []]
    if "projects" in body:
        upd["projects"] = [str(p) for p in body["projects"] or []]
    if "note" in body:
        upd["note"] = str(body["note"] or "")[:200]
    if "enabled" in body:
        upd["enabled"] = bool(body["enabled"])
    if "route_footer" in body:
        upd["route_footer"] = bool(body["route_footer"])
    tool_warnings: list = []
    if "mcp_with_client_tools" in body:
        from .. import persona_rules
        v, errs, tool_warnings = persona_rules.validate_client_tools_policy(
            svc, body["mcp_with_client_tools"])
        if errs:
            return _err("; ".join(errs))
        upd["mcp_with_client_tools"] = v
    loses_admin = ("admin" in cur.scopes and ("admin" not in upd.get("scopes", cur.scopes)
                                              or upd.get("enabled") is False))
    if loses_admin and cfg.client_auth.protect_admin and not _admins(cfg.clients, exclude=name):
        return _err("this is the last admin key and the web UI is protected: turn protection "
                    "off first, or make another admin client", 409)

    def mutate(raw):
        for c in raw.get("clients") or []:
            if c.get("name") == name:
                c.update(upd)
    svc.config_store.save(mutate)
    return {"ok": True, "warnings": tool_warnings}


@router.delete("/admin/api/setup/clients/{name}")
async def delete_client(request: Request, name: str):
    svc = _svc(request)
    cfg = svc.config_store.config
    cur = _find(cfg, name)
    if not cur:
        return _err(f"no client named {name!r}", 404)
    if "admin" in cur.scopes and cfg.client_auth.protect_admin and \
            not _admins(cfg.clients, exclude=name):
        return _err("this is the last admin key and the web UI is protected: turn protection "
                    "off first, or make another admin client", 409)

    def mutate(raw):
        raw["clients"] = [c for c in raw.get("clients") or [] if c.get("name") != name]
    svc.config_store.save(mutate)
    svc.db.log_event("info", "client_auth", f"client {name} deleted")
    return {"ok": True}


@router.post("/admin/api/setup/client_auth")
async def set_client_auth(request: Request):
    """{mode?: optional|required, protect_admin?: bool, unknown_keys?: refuse|anonymous,
    keyless_mcp_with_client_tools?: default|off|all|dedupe|[names]}"""
    svc = _svc(request)
    cfg = svc.config_store.config
    body = await request.json()
    upd = {}
    if "mode" in body:
        if body["mode"] not in ("optional", "required"):
            return _err("mode must be optional or required")
        upd["mode"] = body["mode"]
    if "unknown_keys" in body:
        if body["unknown_keys"] not in ("refuse", "anonymous"):
            return _err("unknown_keys must be refuse or anonymous")
        upd["unknown_keys"] = body["unknown_keys"]
    warnings = []
    if "keyless_mcp_with_client_tools" in body:
        from .. import persona_rules
        v, errs, w = persona_rules.validate_client_tools_policy(
            svc, body["keyless_mcp_with_client_tools"])
        if errs:
            return _err("; ".join(errs))
        upd["keyless_mcp_with_client_tools"] = v
        warnings += w
    if "protect_admin" in body:
        upd["protect_admin"] = bool(body["protect_admin"])
        if upd["protect_admin"] and not cfg.client_auth.protect_admin:
            me = client_auth.current()
            if "admin" not in (me.get("scopes") or []):
                return _err("to protect the web UI, first make an admin client, then enter "
                            "its key in this browser (the UI asks for it) — otherwise this "
                            "change would lock you out", 409)
    if upd.get("mode") == "required":
        anon = _usage(svc).get("anonymous")
        if anon:
            warnings.append(f"{anon['calls_24h']} keyless request(s) in the last 24 h will be "
                            "refused from now on")

    def mutate(raw):
        ca = raw.get("client_auth") or {}
        ca.update(upd)
        raw["client_auth"] = ca
    new = svc.config_store.save(mutate)
    return {"ok": True, "auth": new.client_auth.model_dump(), "warnings": warnings}


@router.delete("/admin/api/setup/rejected")
async def clear_rejected(request: Request, fingerprint: str = ""):
    client_keys.activity.forget_rejected(fingerprint or None)
    return {"ok": True}


# -- backend limits -----------------------------------------------------------------

LIMIT_FIELDS = ("max_concurrent", "helpers_allowed", "queue_timeout_seconds")


@router.get("/admin/api/setup/limits")
async def get_limits(request: Request):
    svc = _svc(request)
    out = []
    for b in svc.config_store.config.backend_pool.internal.backends or []:
        out.append({"name": b.name, "type": b.type, "flavor": b.flavor,
                    "enabled": getattr(b, "enabled", True),
                    "subscription": bool(getattr(b, "subscription", False)),
                    **{k: getattr(b, k, None) for k in LIMIT_FIELDS}})
    streams = {}
    try:
        for s in svc.health.status():
            if s.get("streams") is not None:
                streams[s.get("name") or s.get("backend")] = s["streams"]
    except Exception:                                             # noqa: BLE001
        pass
    return {"backends": out, "streams": streams}


def _opt_int(v, lo, hi, field):
    if v is None or v == "":
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a whole number or empty (no limit)")
    if not lo <= n <= hi:
        raise ValueError(f"{field} must be between {lo} and {hi}")
    return n


@router.post("/admin/api/setup/limits")
async def set_limits(request: Request):
    """{name, max_concurrent?, helpers_allowed?, queue_timeout_seconds?}
    Empty / null = no limit (for max_concurrent and helpers_allowed)."""
    svc = _svc(request)
    body = await request.json()
    name = body.get("name")
    if not any(b.name == name for b in svc.config_store.config.backend_pool.internal.backends or []):
        return _err(f"no backend named {name!r}", 404)
    upd = {}
    try:
        if "max_concurrent" in body:
            upd["max_concurrent"] = _opt_int(body["max_concurrent"], 1, 256, "max_concurrent")
        if "helpers_allowed" in body:
            upd["helpers_allowed"] = _opt_int(body["helpers_allowed"], 0, 64, "helpers_allowed")
        if "queue_timeout_seconds" in body:
            q = float(body["queue_timeout_seconds"])
            if not 1 <= q <= 3600:
                raise ValueError("queue_timeout_seconds must be between 1 and 3600")
            upd["queue_timeout_seconds"] = q
    except (ValueError, TypeError) as e:
        return _err(str(e))

    def mutate(raw):
        for b in raw.setdefault("backend_pool", {}).setdefault("internal", {}).get("backends", []):
            if isinstance(b, dict) and b.get("name") == name:
                for k, v in upd.items():
                    if v is None:
                        b.pop(k, None)
                    else:
                        b[k] = v
    svc.config_store.save(mutate)
    await svc.rebuild_pool()
    svc.db.log_event("info", "backend_pool", f"backend {name} limits: " + ", ".join(
        f"{k}={'no limit' if v is None else v}" for k, v in upd.items()))
    return {"ok": True}
