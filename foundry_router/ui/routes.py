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
from ..db import utcnow

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
                "confirm_user_paid_at": svc.meridian_usage.cfg.confirm_user_paid_at,
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
               "confirm_user_paid_at", "quota_poll_seconds"}
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
    # Latest feedback rating per listed request, so the GUI thumbs reflect
    # what's already recorded (a re-click updates rather than surprises).
    if rows:
        ids = [r["id"] for r in rows]
        marks = {f["request_log_id"]: f["rating"] for f in svc.db.query(
            f"SELECT request_log_id, rating FROM response_feedback "
            f"WHERE request_log_id IN ({','.join('?' * len(ids))}) "
            f"ORDER BY id", ids)}
        for r in rows:
            r["feedback"] = marks.get(r["id"])
    return {"requests": rows}


@router.post("/admin/api/feedback")
async def gui_feedback(request: Request):
    """Thumbs up/down from the router's own Usage tab — works regardless of
    which client served the conversation (quality-tracking spec Phase 1)."""
    from ..insights import normalize_rating, record_feedback
    svc = _svc(request)
    body = await request.json()
    rating = normalize_rating(body.get("rating"))
    if rating is None:
        return JSONResponse({"error": "rating must be up/down/+1/-1"},
                            status_code=400)
    out = record_feedback(svc.db, rating,
                          request_log_id=body.get("request_log_id"),
                          comment=str(body.get("comment") or ""), source="gui")
    return {"ok": True, **out}


# --------------------------------------------------------------------------- #
# Eval harness (quality spec Phase 4)                                         #
# --------------------------------------------------------------------------- #

@router.get("/admin/api/eval/prompts")
async def eval_prompts(request: Request):
    return {"prompts": _svc(request).db.query(
        "SELECT * FROM eval_prompts ORDER BY category, id")}


@router.post("/admin/api/eval/prompts")
async def upsert_eval_prompt(request: Request):
    svc = _svc(request)
    b = await request.json()
    category = str(b.get("category") or "chat")
    prompt = str(b.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt required"}, status_code=400)
    checks = b.get("checks") or []
    if isinstance(checks, str):
        try:
            checks = json.loads(checks)
        except json.JSONDecodeError:
            return JSONResponse({"error": "checks must be a JSON list"},
                                status_code=400)
    enabled = 1 if b.get("enabled", 1) else 0
    if b.get("id"):
        svc.db.execute(
            "UPDATE eval_prompts SET category=?, prompt=?, checks=?, enabled=? "
            "WHERE id=?", (category, prompt, json.dumps(checks), enabled, b["id"]))
    else:
        svc.db.execute(
            "INSERT INTO eval_prompts (category, prompt, checks, enabled, "
            "created_at) VALUES (?,?,?,?,?)",
            (category, prompt, json.dumps(checks), enabled, utcnow()))
    return {"ok": True}


@router.post("/admin/api/eval/prompts/delete")
async def delete_eval_prompt(request: Request):
    svc = _svc(request)
    pid = (await request.json()).get("id")
    svc.db.execute("DELETE FROM eval_prompts WHERE id=?", (pid,))
    return {"ok": True}


@router.post("/admin/api/eval/run")
async def eval_run(request: Request):
    """Start a harness run for a persona. Default is fire-and-forget (the GUI
    polls the runs list); wait=true runs synchronously and returns the scored
    row — the shape scripts/the operator convention want."""
    import asyncio
    svc = _svc(request)
    b = await request.json()
    persona = str(b.get("persona") or "")
    if svc.personas.get(persona) is None:
        return JSONResponse({"error": f"no persona named {persona!r}"},
                            status_code=404)
    judge = str(b.get("judge_model") or "")
    categories = b.get("categories") or None
    if b.get("wait"):
        run_id = await svc.evals.run(persona, judge, categories)
        row = svc.db.query_one("SELECT * FROM eval_runs WHERE id=?", (run_id,))
        return {"ok": True, "run": row,
                "results": svc.evals.results(run_id)}
    task = asyncio.get_running_loop().create_task(
        svc.evals.run(persona, judge, categories))
    svc._eval_tasks = [t for t in svc._eval_tasks if not t.done()] + [task]
    return {"ok": True, "started": True}


@router.get("/admin/api/eval/runs")
async def eval_runs(request: Request, persona: str = ""):
    return {"runs": _svc(request).evals.runs(persona or None)}


@router.get("/admin/api/eval/results")
async def eval_results(request: Request, run_id: int):
    return {"results": _svc(request).evals.results(run_id)}


@router.get("/admin/api/semcache")
async def semcache_stats(request: Request):
    return _svc(request).semcache.stats()


@router.post("/admin/api/semcache/clear")
async def semcache_clear(request: Request):
    svc = _svc(request)
    n = svc.semcache.clear()
    svc.db.log_event("info", "semcache", f"cache cleared ({n} entries removed)")
    return {"ok": True, "removed": n}


@router.post("/admin/api/config/semantic_cache")
async def set_semantic_cache(request: Request):
    """Persist semantic-cache settings AND apply them live (the cache object
    reads self.cfg per call) — same live-edit pattern as MCP pacing."""
    svc = _svc(request)
    body = await request.json()
    allowed = {"enabled", "embed_url", "embed_model", "embed_api_key",
               "min_similarity", "default_ttl_seconds", "category_ttls",
               "max_entries"}
    updates = {k: v for k, v in body.items() if k in allowed}

    def mutate(raw):
        raw.setdefault("semantic_cache", {}).update(updates)
    try:
        cfg = svc.config_store.save(mutate)
    except Exception as e:
        from ..errors import describe_exception
        return JSONResponse({"error": describe_exception(e)}, status_code=400)
    svc.semcache.cfg = cfg.semantic_cache
    return {"ok": True, "semantic_cache": cfg.semantic_cache.model_dump()}


@router.post("/admin/api/semcache/test")
async def semcache_test(request: Request):
    """Live embedding-endpoint probe (the 'Test embedding' button): a real
    embed call reporting reachability, vector dimension, and latency. Tests the
    values in the request body (what's in the form) so it works before Save;
    omitted fields fall back to the saved config."""
    import asyncio
    svc = _svc(request)
    b = await request.json()
    try:
        return await asyncio.wait_for(
            svc.semcache.test_embed(url=b.get("embed_url"),
                                    model=b.get("embed_model"),
                                    api_key=b.get("embed_api_key")),
            timeout=25)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "timed out after 25s",
                "sqlite_vec": svc.semcache._vec_loaded}


@router.get("/admin/api/insights")
async def insights(request: Request, days: int = 7):
    """On-demand statistical digest (quality-tracking spec Phase 1): feedback
    trends, guardrail patterns, tool-call reliability, review outcomes — the
    operator reads and decides; nothing here mutates prompts or config."""
    from ..insights import generate_digest, render_report
    digest = generate_digest(_svc(request).db, days=max(1, min(days, 90)))
    return {"digest": digest, "report": render_report(digest)}


@router.get("/admin/api/events")
async def event_log(request: Request, limit: int = 200):
    svc = _svc(request)
    return {"events": svc.db.query(
        "SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (min(limit, 2000),))}


_LOGO_KEY = "ui_logo"


_RESEARCH_NUM = ("sweep_hours", "stale_days", "max_pages_per_model", "corpus_char_limit")


@router.get("/admin/api/devlog")
async def devlog(request: Request, after: int = 0, level: str = "",
                 q: str = "", limit: int = 500):
    """App logs from the in-memory ring buffer (Dev-Log view). `after` enables
    incremental live-tail; `level` is a minimum severity; `q` is free text.
    `max_id` is the buffer's current head so a filtered poll still advances."""
    buf = getattr(_svc(request), "logbuffer", None)
    if buf is None:
        return {"records": [], "max_id": 0}
    recs = buf.snapshot(after=after, level=level or None, q=q or None,
                        limit=min(limit, 1000))
    return {"records": recs, "max_id": buf.max_id()}


@router.post("/admin/api/devlog/clear")
async def clear_devlog(request: Request):
    buf = getattr(_svc(request), "logbuffer", None)
    if buf is not None:
        buf.clear()
    return {"ok": True}


@router.post("/admin/api/backends/test")
async def test_backend(request: Request):
    """Cheap liveness probe of one backend: fetch its model list (free, no
    generation), reporting reachability, model count, latency, and any error —
    the same verify-in-place pattern as the MCP/Ollama/brain tests."""
    import asyncio
    import time

    from ..errors import describe_exception
    svc = _svc(request)
    name = ((await request.json()).get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    state = getattr(svc.pool, "backends", {}).get(name)
    if state is None:
        return JSONResponse({"error": f"no backend named {name!r}"}, status_code=404)
    t0 = time.monotonic()
    try:
        models = await asyncio.wait_for(state.protocol.list_models(), timeout=15)
        return {"ok": True, "models": len(models), "sample": models[:8],
                "latency_ms": int((time.monotonic() - t0) * 1000), "error": ""}
    except asyncio.TimeoutError:
        return {"ok": False, "models": 0, "sample": [], "error": "timed out after 15s"}
    except Exception as e:
        return {"ok": False, "models": 0, "sample": [], "error": describe_exception(e)}


@router.post("/admin/api/config/research")
async def set_research_config(request: Request):
    """Persist research settings to config.yaml AND apply them to the running
    Research Agent (it reads self.cfg per sweep), so no restart/SSH is needed."""
    svc = _svc(request)
    b = await request.json()

    def mutate(raw):
        r = raw.setdefault("registry", {}).setdefault("research", {})
        if "enabled" in b:
            r["enabled"] = bool(b["enabled"])
        if "search_prefix" in b:
            r["search_prefix"] = str(b["search_prefix"] or "")
        for k in _RESEARCH_NUM:
            if b.get(k) is not None:
                r[k] = int(b[k])
        for ref in ("search", "fetch"):
            if isinstance(b.get(ref), dict):
                r[ref] = {k: v for k, v in b[ref].items() if v not in (None, "")}
    try:
        cfg = svc.config_store.save(mutate)
    except Exception as e:
        from ..errors import describe_exception
        return JSONResponse({"error": describe_exception(e)}, status_code=400)
    svc.research.cfg = cfg.registry.research   # live apply
    return {"ok": True}


@router.post("/admin/api/research/test")
async def test_research(request: Request):
    """Live search+fetch probe through the configured MCP tools (the GUI button)."""
    import asyncio
    svc = _svc(request)
    try:
        return await asyncio.wait_for(svc.research.test_pipeline(), timeout=60)
    except asyncio.TimeoutError:
        return {"ok": False, "search_error": "timed out after 60s", "sample": ""}


@router.get("/admin/api/branding")
async def get_branding(request: Request):
    """The custom header logo (a data: URI stored in the DB), or null."""
    return {"logo": _svc(request).db.kv_get(_LOGO_KEY)}


@router.post("/admin/api/branding/logo")
async def set_branding_logo(request: Request):
    """Store (data:image/... URI) or clear (logo=null) the header logo. Kept in
    the DB kv so it survives restarts and needs no file mount. Size-guarded to
    keep the row (and every /branding response) small."""
    svc = _svc(request)
    logo = (await request.json()).get("logo")
    if logo is None:
        svc.db.kv_del(_LOGO_KEY)
        return {"ok": True, "logo": None}
    if not isinstance(logo, str) or not logo.startswith("data:image/"):
        return JSONResponse({"error": "logo must be a data:image/... URI"}, status_code=400)
    if len(logo) > 900_000:   # ~640 KB image after base64 inflation
        return JSONResponse({"error": "logo too large (max ~640 KB image)"}, status_code=400)
    svc.db.kv_set(_LOGO_KEY, logo)
    return {"ok": True}


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
    admin = _svc(request).ollama_admin
    try:
        models = await admin.tags(backend)
    except Exception as e:
        return {"ok": False, "models": [], "loaded": [], "error": describe_exception(e)}
    try:
        loaded = await admin.loaded(backend)     # non-fatal — just a warm-status hint
    except Exception:
        loaded = []
    return {"ok": True, "models": models, "loaded": loaded}


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
