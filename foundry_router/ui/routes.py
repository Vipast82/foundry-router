"""Admin web UI backend (design doc §4.9).

UI stack choice: a single static HTML file with vanilla JS calling these JSON
endpoints — runs anywhere, zero build toolchain, nothing to maintain but one
file, which is the best usability-per-dependency for a small internal admin
panel (§2 minimalism).

Mounted on the same FastAPI app ("/ui" + "/admin/api/*"), not a second service.
No auth — internal-network-only posture, stated in the README.

What's live vs. restart-required:
  live      personas, model registry edits, tool enable/disable, guardrail
            values, MCP server list, backend list (pool is rebuilt in place)
  restart   backend_pool.mode, agent_brain.* (brain client is rebuilt live too,
            actually — only server.host/port truly need a restart)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

log = logging.getLogger(__name__)

router = APIRouter()
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _svc(request: Request):
    return request.app.state.services


def _mask(value):
    if isinstance(value, str) and len(value) > 4:
        return value[:2] + "***"
    return "***" if value else value


# --------------------------------------------------------------------------- #
# Page                                                                        #
# --------------------------------------------------------------------------- #

@router.get("/ui")
async def ui_page():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


# --------------------------------------------------------------------------- #
# Status / backends (§4.9 item 1)                                             #
# --------------------------------------------------------------------------- #

@router.get("/admin/api/status")
async def status(request: Request):
    svc = _svc(request)
    cfg = svc.config_store.config
    return {
        "version": "0.1.0",
        "pool_mode": cfg.backend_pool.mode,
        "guardrail_authority": cfg.guardrails.authority,
        "brain": {"provider": cfg.agent_brain.provider, "model": cfg.agent_brain.model,
                  "endpoint": cfg.agent_brain.endpoint},
        "backends": svc.pool.backend_status(),
        "tool_count": len(svc.tool_registry.enabled()),
        "last_tool_sync": svc.tool_registry.last_sync,
        "mcp_servers": [s.name for s in cfg.mcp_servers],
    }


@router.get("/admin/api/config")
async def get_config(request: Request):
    svc = _svc(request)
    cfg = svc.config_store.config.model_dump()
    for b in cfg.get("backend_pool", {}).get("internal", {}).get("backends", []):
        if b.get("api_key"):
            b["api_key"] = _mask(b["api_key"])
    if cfg.get("agent_brain", {}).get("api_key"):
        cfg["agent_brain"]["api_key"] = _mask(cfg["agent_brain"]["api_key"])
    if cfg.get("backend_pool", {}).get("litellm", {}).get("api_key"):
        cfg["backend_pool"]["litellm"]["api_key"] = _mask(cfg["backend_pool"]["litellm"]["api_key"])
    return cfg


@router.post("/admin/api/config/backends")
async def set_backends(request: Request):
    """Replace the internal backend list. Send api_key as a ${VAR} reference,
    not the secret itself — the value is written verbatim into config.yaml."""
    svc = _svc(request)
    backends = await request.json()
    if not isinstance(backends, list):
        return JSONResponse({"error": "expected a JSON list of backends"}, status_code=400)
    for b in backends:
        if not all(b.get(k) for k in ("name", "type", "url")):
            return JSONResponse({"error": "each backend needs name, type, url"},
                                status_code=400)

    def mutate(raw):
        raw.setdefault("backend_pool", {}).setdefault("internal", {})["backends"] = backends
    svc.config_store.save(mutate)
    await svc.rebuild_pool()
    return {"ok": True, "backends": svc.pool.backend_status()}


@router.post("/admin/api/config/pool_mode")
async def set_pool_mode(request: Request):
    svc = _svc(request)
    body = await request.json()
    mode = body.get("mode")
    if mode not in ("internal", "olla", "litellm"):
        return JSONResponse({"error": "mode must be internal|olla|litellm"}, status_code=400)

    def mutate(raw):
        raw.setdefault("backend_pool", {})["mode"] = mode
        if mode == "olla" and body.get("url"):
            raw["backend_pool"].setdefault("olla", {})["url"] = body["url"]
        if mode == "litellm" and body.get("url"):
            raw["backend_pool"].setdefault("litellm", {})["url"] = body["url"]
    svc.config_store.save(mutate)
    await svc.rebuild_pool()
    return {"ok": True, "mode": mode}


@router.post("/admin/api/config/brain")
async def set_brain(request: Request):
    svc = _svc(request)
    body = await request.json()
    allowed = {"provider", "endpoint", "model", "api_key", "keep_alive", "max_tokens",
               "tool_result_limit_chars", "mcp_result_limit_chars", "worker_max_tokens",
               "user_input_preview_chars"}
    updates = {k: v for k, v in body.items() if k in allowed}

    def mutate(raw):
        raw.setdefault("agent_brain", {}).update(updates)
    svc.config_store.save(mutate)
    svc.rebuild_brain()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Guardrails (§4.9 item 3)                                                    #
# --------------------------------------------------------------------------- #

@router.post("/admin/api/config/guardrails")
async def set_guardrails(request: Request):
    svc = _svc(request)
    body = await request.json()
    allowed = {"authority", "max_steps_per_request", "max_paid_calls_per_request",
               "daily_spend_cap_usd", "weekly_spend_cap_usd"}
    updates = {k: v for k, v in body.items() if k in allowed}

    def mutate(raw):
        raw.setdefault("guardrails", {}).update(updates)
    cfg = svc.config_store.save(mutate)
    # The engine holds the same config object tree — refresh its reference.
    svc.guardrails.cfg = cfg.guardrails
    svc.guardrails.pool_mode = cfg.backend_pool.mode
    return {"ok": True, "guardrails": cfg.guardrails.model_dump()}


# --------------------------------------------------------------------------- #
# Model registry (§4.9 item 2)                                                #
# --------------------------------------------------------------------------- #

@router.get("/admin/api/models")
async def list_models(request: Request):
    svc = _svc(request)
    reachable = set(svc.pool.available_models().keys())
    rows = svc.registry.list_models()
    for r in rows:
        r["reachable"] = r["id"] in reachable
    # Reachable-but-unregistered models should be visible too
    for mid in sorted(reachable - {r["id"] for r in rows}):
        rows.append({"id": mid, "reachable": True, "source": None})
    # Research readiness rides along so the UI can grey out the button with a
    # real reason instead of reporting success for work that can't happen.
    ready, reason = (svc.research.prerequisites() if svc.research
                     else (False, "research agent not initialized"))
    return {"models": rows, "research_ready": ready, "research_reason": reason}


@router.post("/admin/api/models/update")
async def update_model(request: Request):
    svc = _svc(request)
    body = await request.json()
    model_id = body.pop("id", None)
    if not model_id:
        return JSONResponse({"error": "id required"}, status_code=400)
    svc.registry.manual_update(model_id, **body)
    return {"ok": True, "model": svc.registry.get(model_id)}


@router.post("/admin/api/models/toggle")
async def toggle_model(request: Request):
    """Governance enable/disable (registry redesign item 2): excluded from
    ranking and tool generation entirely — an immediate tool sync makes the
    ask_* tool appear/disappear right away."""
    svc = _svc(request)
    body = await request.json()
    model_id = body.get("id")
    if not model_id:
        return JSONResponse({"error": "id required"}, status_code=400)
    enabled = bool(body.get("enabled", True))
    svc.registry.set_enabled(model_id, enabled)
    await svc.tool_registry.sync(svc.pool)
    return {"ok": True, "id": model_id, "enabled": enabled}


@router.post("/admin/api/models/seed")
async def apply_seed(request: Request):
    from ..registry.reference_seed import apply_reference_seed
    svc = _svc(request)
    count = apply_reference_seed(svc.registry)
    return {"ok": True, "applied": count}


@router.get("/admin/api/models/benchmarks")
async def model_benchmarks(request: Request, model_id: str):
    svc = _svc(request)
    return {"benchmarks": svc.registry.benchmarks(model_id)}


@router.post("/admin/api/models/refresh")
async def refresh_models(request: Request):
    from ..registry.openrouter_ingest import poll_openrouter
    svc = _svc(request)
    count = await poll_openrouter(svc.db, svc.registry, svc.http, force=True)
    return {"ok": True, "ingested": count}


@router.post("/admin/api/models/research")
async def research_model(request: Request):
    svc = _svc(request)
    body = await request.json()
    model_id = body.get("model_id") or ""
    # Never report success for an action whose outcome depends on something
    # that hasn't happened: check the prerequisites before queueing.
    ready, reason = (svc.research.prerequisites() if svc.research
                     else (False, "research agent not initialized"))
    if not ready:
        return JSONResponse({"ok": False, "queued": False, "error": reason},
                            status_code=409)
    queued = svc.research.enqueue(model_id)
    return {"ok": True, "queued": queued,
            "note": None if queued else "already queued"}


# --------------------------------------------------------------------------- #
# Personas (§4.9 item 4)                                                      #
# --------------------------------------------------------------------------- #

@router.get("/admin/api/personas")
async def list_personas(request: Request):
    return {"personas": _svc(request).personas.list()}


@router.post("/admin/api/personas")
async def upsert_persona(request: Request):
    svc = _svc(request)
    body = await request.json()
    name = body.pop("virtual_name", None)
    if not name:
        return JSONResponse({"error": "virtual_name required"}, status_code=400)
    svc.personas.upsert(name, **body)
    return {"ok": True, "persona": svc.personas.get(name)}


@router.post("/admin/api/personas/delete")
async def delete_persona(request: Request):
    svc = _svc(request)
    body = await request.json()
    svc.personas.delete(body.get("virtual_name") or "")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Tool registry (§4.9 item 5)                                                 #
# --------------------------------------------------------------------------- #

@router.get("/admin/api/tools")
async def list_tools(request: Request):
    svc = _svc(request)
    return {"tools": svc.tool_registry.status(),
            "last_sync": svc.tool_registry.last_sync}


@router.post("/admin/api/tools/toggle")
async def toggle_tool(request: Request):
    svc = _svc(request)
    body = await request.json()
    name = body.get("name")
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    svc.tool_registry.set_disabled(name, bool(body.get("disabled", True)))
    return {"ok": True}


@router.post("/admin/api/tools/sync")
async def force_sync(request: Request):
    svc = _svc(request)
    result = await svc.tool_registry.sync(svc.pool)
    return {"ok": True, **result}


# --------------------------------------------------------------------------- #
# MCP connections (§4.9 item 8) — a connection list, not a tool list          #
# --------------------------------------------------------------------------- #

@router.get("/admin/api/mcp_servers")
async def list_mcp(request: Request):
    cfg = _svc(request).config_store.config
    return {"servers": [s.model_dump() for s in cfg.mcp_servers]}


@router.post("/admin/api/mcp_servers")
async def upsert_mcp(request: Request):
    svc = _svc(request)
    body = await request.json()
    if not body.get("name") or not body.get("url"):
        return JSONResponse({"error": "name and url required"}, status_code=400)
    entry = {"name": body["name"], "url": body["url"],
             "transport": body.get("transport", "streamable-http")}
    if body.get("headers"):
        entry["headers"] = body["headers"]

    def mutate(raw):
        servers = raw.setdefault("mcp_servers", []) or []
        servers[:] = [s for s in servers if s.get("name") != entry["name"]] + [entry]
        raw["mcp_servers"] = servers
    cfg = svc.config_store.save(mutate)
    svc.mcp.set_servers(cfg.mcp_servers)
    result = await svc.tool_registry.sync(svc.pool)  # discover its tools now
    return {"ok": True, "tool_sync": result}


@router.post("/admin/api/mcp_servers/delete")
async def delete_mcp(request: Request):
    svc = _svc(request)
    body = await request.json()
    name = body.get("name") or ""

    def mutate(raw):
        raw["mcp_servers"] = [s for s in (raw.get("mcp_servers") or [])
                              if s.get("name") != name]
    cfg = svc.config_store.save(mutate)
    svc.mcp.set_servers(cfg.mcp_servers)
    await svc.tool_registry.sync(svc.pool)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Logs (§4.9 items 6 & 7)                                                     #
# --------------------------------------------------------------------------- #

@router.get("/admin/api/usage")
async def usage_log(request: Request, limit: int = 100):
    svc = _svc(request)
    rows = svc.db.query(
        "SELECT * FROM request_log ORDER BY id DESC LIMIT ?", (min(limit, 1000),))
    for r in rows:
        for k in ("models_used", "guardrail_events"):
            try:
                r[k] = json.loads(r[k]) if r[k] else []
            except (json.JSONDecodeError, TypeError):
                pass
    return {"requests": rows}


@router.get("/admin/api/events")
async def event_log(request: Request, limit: int = 200):
    svc = _svc(request)
    return {"events": svc.db.query(
        "SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (min(limit, 2000),))}
