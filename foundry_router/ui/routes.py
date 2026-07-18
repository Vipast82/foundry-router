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

from .. import __version__

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
        "version": __version__,
        "pool_mode": cfg.backend_pool.mode,
        "guardrail_authority": cfg.guardrails.authority,
        "brain": {"provider": cfg.agent_brain.provider, "model": cfg.agent_brain.model,
                  "endpoint": cfg.agent_brain.endpoint,
                  "health": getattr(svc, "_brain_health", None)},
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
               "user_input_preview_chars", "heartbeat_seconds"}
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
# Claude usage window (§4.7) — real quota data + our observed consumption     #
# --------------------------------------------------------------------------- #

@router.get("/admin/api/quota")
async def quota(request: Request):
    from ..usage import observed_subscription_usage
    svc = _svc(request)
    backends = []
    for s in getattr(svc.pool, "backends_of_type", lambda t: [])("anthropic-compatible"):
        snap = await svc.meridian_usage.snapshot(s.config.url, s.config.api_key)
        backends.append({"backend": s.config.name, "healthy": s.healthy, **snap})
    return {"backends": backends,
            "observed": observed_subscription_usage(svc.db),
            "thresholds": {
                "conserve_premium_at": svc.meridian_usage.cfg.conserve_premium_at,
                "conserve_strong_at": svc.meridian_usage.cfg.conserve_strong_at,
                "conserve_fable_at": svc.meridian_usage.cfg.conserve_fable_at,
                "usage_credits": svc.meridian_usage.cfg.usage_credits,
                "min_window_fraction": svc.meridian_usage.cfg.min_window_fraction}}


@router.get("/admin/api/brain/health")
async def brain_health(request: Request):
    """On-demand brain reachability probe — a free GET of the endpoint's model
    list (never a paid generation), returning whether the endpoint is up and the
    configured model is actually present. Refreshes the cached snapshot the UI
    header/status read, and the Backends tab's Test brain button drives it."""
    svc = _svc(request)
    return await svc.refresh_brain_health()


@router.get("/admin/api/meridian/health")
async def meridian_health(request: Request):
    """On-demand auth-validity probe (spec item 2): the free read-only quota
    call answers 'is the Claude subscription login still valid' without
    burning a real generation on finding out. Shares plumbing with the
    background poll loop — same fetch, same edge-triggered alerting."""
    svc = _svc(request)
    from ..db import utcnow
    out = []
    for s in getattr(svc.pool, "backends_of_type", lambda t: [])("anthropic-compatible"):
        health = await svc.meridian_usage.auth_health(s.config.url, s.config.api_key)
        out.append({"backend": s.config.name, "url": s.config.url, **health})
    return {"backends": out, "checked": utcnow()}


@router.post("/admin/api/config/meridian")
async def set_meridian(request: Request):
    svc = _svc(request)
    body = await request.json()
    allowed = {"quota_path", "min_window_fraction", "conserve_premium_at",
               "conserve_strong_at", "conserve_fable_at", "usage_credits",
               "quota_poll_seconds"}
    updates = {k: v for k, v in body.items() if k in allowed}

    def mutate(raw):
        raw.setdefault("meridian", {}).update(updates)
    cfg = svc.config_store.save(mutate)
    svc.meridian_usage.cfg = cfg.meridian
    svc.meridian_usage.clear_cache()
    return {"ok": True, "meridian": cfg.meridian.model_dump()}


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
    return {"benchmarks": svc.registry.benchmarks(model_id),
            "named": svc.registry.named_benchmarks(model_id)}


@router.post("/admin/api/models/named_benchmark/add")
async def add_named_benchmark(request: Request):
    """Manually add a named benchmark (real vendor data the research pipeline
    hasn't captured yet — e.g. a brand-new vendor page SearXNG can't find).
    Tagged source='manual' so a later research pass never clobbers it."""
    svc = _svc(request)
    b = await request.json()
    model_id = (b.get("model_id") or "").strip()
    name = (b.get("benchmark_name") or "").strip()
    if not model_id or not name:
        return JSONResponse({"error": "model_id and benchmark_name required"},
                            status_code=400)
    try:
        score = float(b.get("score"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "score must be a number"}, status_code=400)
    svc.registry.upsert_named_benchmark(
        model_id, name, (b.get("category") or "coding"), score,
        (b.get("scale") or "percent"), source_url=str(b.get("source_url") or "")[:500],
        source="manual")
    return {"ok": True, "named": svc.registry.named_benchmarks(model_id)}


@router.post("/admin/api/models/named_benchmark/delete")
async def delete_named_benchmark(request: Request):
    svc = _svc(request)
    b = await request.json()
    model_id, name = b.get("model_id") or "", b.get("benchmark_name") or ""
    svc.registry.delete_named_benchmark(model_id, name)
    return {"ok": True, "named": svc.registry.named_benchmarks(model_id)}


@router.post("/admin/api/models/reset_benchmarks")
async def reset_benchmarks(request: Request):
    """Clear a model's automatic benchmark rows (research/seed/observed; manual
    overrides preserved) and re-apply the reference-seed defaults — the fix for
    a row stamped with a wrong number (e.g. an extractor conflating two
    categories into one score)."""
    from ..registry.reference_seed import apply_seed_to_model
    svc = _svc(request)
    body = await request.json()
    model_id = body.get("model_id") or ""
    if not model_id:
        return JSONResponse({"error": "model_id required"}, status_code=400)
    removed = svc.registry.reset_benchmarks(model_id)
    reseeded = apply_seed_to_model(svc.registry, model_id)
    return {"ok": True, "removed": removed, "reseeded": reseeded,
            "benchmarks": svc.registry.benchmarks(model_id)}


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


@router.post("/admin/api/personas/clone")
async def clone_persona(request: Request):
    svc = _svc(request)
    body = await request.json()
    source, new_name = body.get("source") or "", body.get("new_name") or ""
    if not source or not new_name:
        return JSONResponse({"error": "source and new_name required"}, status_code=400)
    cloned = svc.personas.clone(source, new_name)
    if cloned is None:
        return JSONResponse({"error": "source missing or new_name already exists"},
                            status_code=409)
    return {"ok": True, "persona": cloned}


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
    svc = _svc(request)
    cfg = svc.config_store.config
    # Tools discovered per server on the last Tool Sync — a passive "is it
    # working" signal (0 = unreachable or exposes nothing).
    counts: dict = {}
    for t in svc.tool_registry.status():
        if t.get("kind") == "mcp" and t.get("server"):
            counts[t["server"]] = counts.get(t["server"], 0) + 1
    usage = svc.mcp.usage()
    return {"servers": [{**s.model_dump(), **svc.mcp.secret_meta(s.name),
                         "tool_count": counts.get(s.name, 0),
                         "usage": usage.get(s.name)}
                        for s in cfg.mcp_servers]}


@router.post("/admin/api/mcp_servers/test")
async def test_mcp(request: Request):
    """Live connectivity probe of one server — opens a real session (using the
    stored auth token) and lists its tools, so the operator can confirm a new
    connection works without waiting for a request to route through it."""
    import asyncio

    from ..errors import describe_exception
    svc = _svc(request)
    name = ((await request.json()).get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    try:
        tools = await asyncio.wait_for(svc.mcp.list_tools(name), timeout=20)
        return {"ok": True, "tool_count": len(tools),
                "tools": [t["name"] for t in tools][:50], "error": ""}
    except asyncio.TimeoutError:
        return {"ok": False, "tool_count": 0, "tools": [],
                "error": "timed out after 20s — server unreachable?"}
    except Exception as e:
        return {"ok": False, "tool_count": 0, "tools": [],
                "error": describe_exception(e)}


@router.post("/admin/api/mcp_servers/token")
async def set_mcp_token(request: Request):
    """Store a server's auth token in the DB (not config.yaml). Sent as
    'Authorization: Bearer <token>' by default, or as the raw value of a named
    header (x-api-key, etc.). An empty token clears it. Applies immediately —
    the next tool call to that server uses it (sessions are opened per call)."""
    svc = _svc(request)
    b = await request.json()
    server = (b.get("server") or "").strip()
    if not server:
        return JSONResponse({"error": "server required"}, status_code=400)
    svc.mcp.set_secret(server, (b.get("token") or "").strip(),
                       (b.get("header") or "Authorization").strip())
    return {"ok": True, "server": server, **svc.mcp.secret_meta(server)}


@router.post("/admin/api/mcp_servers")
async def upsert_mcp(request: Request):
    svc = _svc(request)
    body = await request.json()
    if not body.get("name") or not body.get("url"):
        return JSONResponse({"error": "name and url required"}, status_code=400)
    entry = {"name": body["name"], "url": body["url"],
             "transport": body.get("transport", "streamable-http"),
             "timeout_seconds": int(body.get("timeout_seconds") or 300),
             # Pacing / 429 handling — persist so a UI edit doesn't silently
             # drop a throttle set in config.yaml (SearXNG needs a gap here).
             "pace_seconds": float(body.get("pace_seconds") or 0.0),
             "rate_limit_retries": int(body.get("rate_limit_retries") or 3),
             "rate_limit_backoff_seconds": float(
                 body.get("rate_limit_backoff_seconds") or 30.0)}
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
    svc.mcp.delete_secret(name)   # drop its DB token too — no orphan
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
        for k in ("models_used", "guardrail_events", "tool_calls"):
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


@router.post("/admin/api/usage/clear")
async def clear_usage(request: Request):
    """Wipe the routed-request history. Records a single audit line in the
    (separate) event log so the clear itself is accountable."""
    svc = _svc(request)
    n = svc.db.execute("DELETE FROM request_log")
    svc.db.log_event("info", "admin", f"usage log cleared ({n} request(s) removed)")
    return {"ok": True, "removed": n}


@router.post("/admin/api/events/clear")
async def clear_events(request: Request):
    """Wipe the troubleshooting event log. Does not self-log (that would
    immediately repopulate the tab the operator just emptied)."""
    svc = _svc(request)
    n = svc.db.execute("DELETE FROM event_log")
    return {"ok": True, "removed": n}


# --------------------------------------------------------------------------- #
# Ollama model management (proxies the Ollama REST API to a chosen backend)   #
# --------------------------------------------------------------------------- #

@router.get("/admin/api/ollama/backends")
async def ollama_backends(request: Request):
    """Ollama-type backends only — the targets these operations can act on."""
    return {"backends": _svc(request).ollama_admin.backends(),
            "jobs": _svc(request).ollama_admin.job_snapshot()}


@router.get("/admin/api/ollama/tags")
async def ollama_tags(request: Request, backend: str):
    from ..errors import describe_exception
    try:
        return {"ok": True, "models": await _svc(request).ollama_admin.tags(backend)}
    except Exception as e:
        return {"ok": False, "models": [], "error": describe_exception(e)}


@router.post("/admin/api/ollama/show")
async def ollama_show(request: Request):
    from ..errors import describe_exception
    b = await request.json()
    if not b.get("backend") or not b.get("model"):
        return JSONResponse({"error": "backend and model required"}, status_code=400)
    try:
        return {"ok": True, "info": await _svc(request).ollama_admin.show(
            b["backend"], b["model"])}
    except Exception as e:
        return {"ok": False, "error": describe_exception(e)}


@router.post("/admin/api/ollama/copy")
async def ollama_copy(request: Request):
    from ..errors import describe_exception
    b = await request.json()
    if not b.get("backend") or not b.get("source") or not b.get("destination"):
        return JSONResponse({"error": "backend, source, destination required"},
                            status_code=400)
    try:
        await _svc(request).ollama_admin.copy(b["backend"], b["source"], b["destination"])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": describe_exception(e)}


@router.post("/admin/api/ollama/rename")
async def ollama_rename(request: Request):
    from ..errors import describe_exception
    b = await request.json()
    if not b.get("backend") or not b.get("source") or not b.get("destination"):
        return JSONResponse({"error": "backend, source, destination required"},
                            status_code=400)
    try:
        await _svc(request).ollama_admin.rename(b["backend"], b["source"], b["destination"])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": describe_exception(e)}


@router.post("/admin/api/ollama/delete")
async def ollama_delete(request: Request):
    from ..errors import describe_exception
    b = await request.json()
    if not b.get("backend") or not b.get("model"):
        return JSONResponse({"error": "backend and model required"}, status_code=400)
    try:
        await _svc(request).ollama_admin.delete(b["backend"], b["model"])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": describe_exception(e)}


@router.post("/admin/api/ollama/pull")
async def ollama_pull(request: Request):
    b = await request.json()
    if not b.get("backend") or not b.get("model"):
        return JSONResponse({"error": "backend and model required"}, status_code=400)
    try:
        key = _svc(request).ollama_admin.start_pull(b["backend"], b["model"])
        return {"ok": True, "job": key}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/admin/api/ollama/push")
async def ollama_push(request: Request):
    b = await request.json()
    if not b.get("backend") or not b.get("model"):
        return JSONResponse({"error": "backend and model required"}, status_code=400)
    try:
        key = _svc(request).ollama_admin.start_push(b["backend"], b["model"])
        return {"ok": True, "job": key}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/admin/api/ollama/create")
async def ollama_create(request: Request):
    b = await request.json()
    if not b.get("backend") or not b.get("model"):
        return JSONResponse({"error": "backend and model required"}, status_code=400)
    if not (b.get("from") or b.get("modelfile")):
        return JSONResponse({"error": "either 'from' (base model) or a raw "
                                      "'modelfile' is required"}, status_code=400)
    try:
        key = _svc(request).ollama_admin.start_create(
            b["backend"], b["model"], from_model=b.get("from", ""),
            system=b.get("system", ""), parameters=b.get("parameters") or None,
            modelfile=b.get("modelfile", ""))
        return {"ok": True, "job": key}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.get("/admin/api/ollama/jobs")
async def ollama_jobs(request: Request):
    """Progress of running/finished pull/create/push jobs — polled by the UI."""
    return {"jobs": _svc(request).ollama_admin.job_snapshot()}
