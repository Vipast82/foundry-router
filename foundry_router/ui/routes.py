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


def _is_masked(value) -> bool:
    """A value that is the UI's masked echo of a stored secret ("ca***" / "***"),
    i.e. the user did not type a new one. Saving it must keep the stored value —
    writing it would replace a working key with five characters."""
    return isinstance(value, str) and value.endswith("***") and len(value) <= 5


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
                  "health": getattr(svc, "_brain_health", None),
                  **svc.brain.mode_status()},
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
        if b.get("management_key"):
            b["management_key"] = _mask(b["management_key"])
    if cfg.get("agent_brain", {}).get("api_key"):
        cfg["agent_brain"]["api_key"] = _mask(cfg["agent_brain"]["api_key"])
    if cfg.get("backend_pool", {}).get("litellm", {}).get("api_key"):
        cfg["backend_pool"]["litellm"]["api_key"] = _mask(cfg["backend_pool"]["litellm"]["api_key"])
    if cfg.get("mcp_aggregator", {}).get("token"):
        cfg["mcp_aggregator"]["token"] = _mask(cfg["mcp_aggregator"]["token"])
    for c in cfg.get("clients") or []:
        # client keys are secrets too (managed ones are only a hash, still hidden)
        for k in ("key", "key_hash"):
            if c.get(k):
                c[k] = _mask(c[k])
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
        internal = raw.setdefault("backend_pool", {}).setdefault("internal", {})
        stored = {x.get("name"): x for x in (internal.get("backends") or [])
                  if isinstance(x, dict)}
        for b in backends:
            for k in ("api_key", "management_key"):
                if _is_masked(b.get(k)):
                    # The editor shows keys masked; keep the stored value (as
                    # written in config.yaml, ${VAR} reference included) for an
                    # unchanged one.
                    b[k] = (stored.get(b.get("name")) or {}).get(k)
        internal["backends"] = backends
    svc.config_store.save(mutate)
    await svc.rebuild_pool()
    return {"ok": True, "backends": svc.pool.backend_status()}


@router.post("/admin/api/backends/enabled")
async def set_backend_enabled(request: Request):
    """Park / unpark one backend: {"name": ..., "enabled": bool}. A disabled
    backend stays in config.yaml but is never probed, routed to or alerted on."""
    from .. import router_ops
    svc = _svc(request)
    body = await request.json()
    try:
        status = await router_ops.set_backend_enabled(svc, body.get("name"),
                                                      bool(body.get("enabled")))
    except router_ops.OpError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)
    return {"ok": True, "backends": status}


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
               "user_input_preview_chars", "heartbeat_seconds", "worker_keep_alive",
               "direct_stream", "direct_stream_heartbeat_seconds",
               "direct_stream_stall_seconds", "heartbeat_visible_seconds",
               "context_guard", "context_guard_reserve_tokens", "context_guard_sticky",
               "stream_worker_reasoning", "reasoning_effort", "sampling_defaults",
               "routing_mode", "worker_tool_result_chars"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if _is_masked(updates.get("api_key")):
        updates.pop("api_key")                       # masked echo: keep the stored key
    if updates.get("routing_mode") not in (None, "auto", "brain", "passthrough"):
        return JSONResponse({"error": "routing_mode must be auto, brain or passthrough"},
                            status_code=400)

    def mutate(raw):
        raw.setdefault("agent_brain", {}).update(updates)
    svc.config_store.save(mutate)
    svc.rebuild_brain()
    if "routing_mode" in updates:
        svc.db.log_event("info", "admin", f"routing mode set to {updates['routing_mode']}")
    return {"ok": True}


@router.post("/admin/api/config/backend-timeout")
async def set_backend_timeout(request: Request):
    """The httpx read timeout for backend (Ollama/Claude) calls — the max gap
    Foundry waits for the next streamed token before giving up (ReadTimeout).
    Baked into the shared client at startup, so a change needs a restart."""
    svc = _svc(request)
    body = await request.json()
    try:
        secs = int(body.get("request_timeout_seconds"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "request_timeout_seconds must be an integer"},
                            status_code=400)
    if secs < 30:
        return JSONResponse({"error": "request_timeout_seconds must be >= 30"},
                            status_code=400)

    def mutate(raw):
        raw.setdefault("backend_pool", {})["request_timeout_seconds"] = secs
    svc.config_store.save(mutate)
    return {"ok": True, "restart_required": True}


@router.get("/admin/api/mcp-aggregator")
async def mcp_aggregator_status(request: Request):
    """Current Foundry-MCP endpoints + a paste-ready AnythingLLM config block."""
    return _svc(request).mcp_aggregator.describe()


@router.get("/admin/api/activity")
async def activity(request: Request):
    """Live snapshot of what's running right now: models with a call in flight,
    MCP tools mid-execution (e.g. a multi-minute music generation), and which
    models are resident in VRAM. The basis for the operator activity view and
    load-aware decisions."""
    svc = _svc(request)
    try:
        loaded = sorted(await svc.pool.loaded_models())
    except Exception:
        loaded = []
    try:
        loaded_detail = await svc.pool.loaded_models_detail()
    except Exception:
        loaded_detail = []
    # Brain: its own model/endpoint, cached health, and (Ollama brains) whether
    # it's warm + how much VRAM it holds.
    bcfg = svc.config_store.config.agent_brain
    brain = {"model": bcfg.model, "provider": bcfg.provider,
             "health": getattr(svc, "_brain_health", None),
             **svc.brain.mode_status(),
             **(await svc.brain.loaded_detail())}
    backends = [{"name": b["name"], "type": b["type"], "flavor": b.get("flavor") or "",
                 "healthy": b["healthy"], "enabled": b.get("enabled", True),
                 "models": len(b.get("models") or []),
                 "last_error": b.get("last_error") or ""}
                for b in svc.pool.backend_status()]
    # Recent finished requests (a short tail) so the Live view shows what just
    # completed alongside what's running.
    recent = svc.db.query(
        "SELECT ts, persona, client_model, mode, status, duration_ms, models_used "
        "FROM request_log ORDER BY id DESC LIMIT 8")

    # Measured performance per model (decode + prefill tok/s, cold-load, and the
    # last-call snapshot), joined onto what's active/loaded so the Live view shows
    # it at a glance. Backend-agnostic: identical numbers whether the call was
    # served by Ollama or llama.cpp.
    # Per-model EFFECTIVE tok/s over a recent window: output tokens ÷ wall time
    # of the requests that used the model (end-to-end, incl. prefill + queue).
    # This is the rate the operator actually experiences, shown next to the pure
    # decode rate so the gap between them (prefill/queue overhead) is visible.
    # Sourced from perf_samples (one row per MODEL call, with its own wall time)
    # rather than request_log, so an agent-mode request's brain/tool time isn't
    # charged to the worker model — and so clearing perf data resets it too.
    eff_rows = svc.db.query(
        "SELECT model, SUM(completion_tokens) AS tok, SUM(wall_ms) AS ms FROM "
        "(SELECT model, completion_tokens, wall_ms FROM perf_samples "
        " WHERE wall_ms IS NOT NULL ORDER BY id DESC LIMIT 300) GROUP BY model")
    _eff: dict = {r["model"]: {"tok": int(r["tok"] or 0), "ms": int(r["ms"] or 0)}
                  for r in eff_rows if r.get("model")}
    # Where each model's latest timing came from — "estimated" (router-timed
    # stream: vLLM/OpenRouter report no per-request timings) is flagged in the UI.
    _tsrc = {r["model"]: r.get("timing_src") or "" for r in svc.db.query(
        "SELECT model, timing_src FROM perf_samples WHERE id IN "
        "(SELECT MAX(id) FROM perf_samples WHERE decode_tps IS NOT NULL "
        " GROUP BY model)")}

    def _eff_tps(model_id):
        a = _eff.get(model_id)
        return round(a["tok"] / (a["ms"] / 1000), 1) if a and a["ms"] else 0.0

    def _perf(model_id):
        row = svc.registry.get(model_id) or {}

        def r1(k):
            return round(row.get(k) or 0.0, 1)
        return {
            "decode_tps": r1("eval_tps_avg"),
            "prompt_tps": r1("prompt_tps_avg"),
            # end-to-end effective rate for this model over the recent window
            "eff_tps": _eff_tps(model_id),
            # wall time (ms): decode = generation time, prefill = context-processing
            # time (~TTFT on a warm model). "how long" next to "how fast".
            "decode_ms": round(row.get("decode_ms_avg") or 0.0),
            "prefill_ms": round(row.get("prefill_ms_avg") or 0.0),
            "last_decode_ms": round(row.get("last_decode_ms") or 0.0),
            "last_prefill_ms": round(row.get("last_prefill_ms") or 0.0),
            "cold_load_ms": round(row.get("cold_load_ms_avg") or 0.0),
            "ttft_ms": round(row.get("ttft_ms_avg") or 0.0),
            "last_ttft_ms": round(row.get("last_ttft_ms") or 0.0),
            "last_decode_tps": r1("last_eval_tps"),
            "last_prompt_tps": r1("last_prompt_tps"),
            "last_cold_load_ms": round(row.get("last_cold_load_ms") or 0.0),
            "last_prompt_tokens": row.get("last_prompt_tokens") or 0,
            "last_eval_tokens": row.get("last_eval_tokens") or 0,
            # trained context window (tokens) — used in the VRAM/context cell.
            "context_length": row.get("context_length") or 0,
            "samples": row.get("eval_samples") or 0,
            "truncations": row.get("truncations") or 0,
            "last_finish": row.get("last_finish_reason") or "",
            "last_at": row.get("last_inference_at") or "",
            # Speculative-decoding acceptance (llama.cpp draft model). Rate is
            # token-weighted across the accumulated totals; None when this model
            # has never run speculation (no draft model / backend can't report),
            # so the UI can hide the stat instead of showing a misleading 0%.
            "spec_accept_pct": (
                round(100.0 * (row.get("spec_accept_total") or 0)
                      / row["spec_draft_total"], 1)
                if row.get("spec_draft_total") else None),
            "last_spec_accept_pct": (round(row["last_spec_accept_pct"], 1)
                                     if row.get("last_spec_accept_pct") is not None
                                     else None),
            "spec_samples": row.get("spec_samples") or 0,
            "spec_draft_total": row.get("spec_draft_total") or 0,
            # KV prefix-cache hit rate — cached prompt tokens / prompt tokens,
            # token-weighted. None until a call reports cache telemetry.
            "cache_hit_pct": (
                round(100.0 * (row.get("cache_hit_total") or 0)
                      / row["cache_prompt_total"], 1)
                if row.get("cache_prompt_total") else None),
            "last_cache_hit_pct": (round(row["last_cache_hit_pct"], 1)
                                   if row.get("last_cache_hit_pct") is not None
                                   else None),
            "cache_samples": row.get("cache_samples") or 0,
            "timing_src": _tsrc.get(model_id, ""),
        }

    active_models = svc.pool.active_calls()
    for m in active_models:
        m["perf"] = _perf(m["model"])
        m["tps"] = m["perf"]["decode_tps"]            # back-compat
    for ld in loaded_detail:
        ld["perf"] = _perf(ld["model"])
        ld["tps"] = ld["perf"]["decode_tps"]

    # Rolling recent-performance summary + per-tool aggregate over a short window,
    # so the dashboard shows throughput and where time/failures go without opening
    # the logs. One small query; JSON is parsed here, not in the browser.
    import json as _json
    window = svc.db.query(
        "SELECT status, duration_ms, models_used, tool_calls FROM request_log "
        "ORDER BY id DESC LIMIT 40")
    tok_total = dur_total = ok = err = 0
    tools_agg: dict = {}
    series: list = []          # chronological per-request points for the sparklines
    for r in window:
        if r.get("status") == "ok":
            ok += 1
        elif r.get("status") == "error":
            err += 1
        rdur = int(r.get("duration_ms") or 0)
        dur_total += rdur
        rtok = 0
        try:
            for m in _json.loads(r.get("models_used") or "[]"):
                ct = int(m.get("completion_tokens") or 0)
                tok_total += ct
                rtok += ct
        except (ValueError, TypeError):
            pass
        # one point per request: effective tok/s and wall seconds (only ok rows
        # with real numbers, so the sparkline traces genuine throughput/latency).
        if r.get("status") == "ok" and rdur > 0:
            series.append({"eff": round(rtok / (rdur / 1000), 1),
                           "s": round(rdur / 1000, 2)})
        try:
            for tc in _json.loads(r.get("tool_calls") or "[]"):
                key = f"{tc.get('server', '')}/{tc.get('tool', '')}"
                agg = tools_agg.setdefault(key, {"tool": key, "count": 0,
                                                 "ms_total": 0, "fails": 0})
                agg["count"] += 1
                agg["ms_total"] += int(tc.get("duration_ms") or 0)
                if tc.get("ok") is False:
                    agg["fails"] += 1
        except (ValueError, TypeError):
            pass
    tool_stats = sorted(
        ({"tool": v["tool"], "count": v["count"], "fails": v["fails"],
          "avg_ms": round(v["ms_total"] / v["count"]) if v["count"] else 0}
         for v in tools_agg.values()),
        key=lambda t: t["count"], reverse=True)[:8]
    trunc_total = (svc.db.query_one(
        "SELECT COALESCE(SUM(truncations),0) AS t FROM models") or {}).get("t") or 0
    # Session DECODE rate, shown beside the effective rate so the gap between them
    # (all the prefill + queue + tool time that decode tok/s hides) is legible.
    # Token-weighted across the models that ran in the window, using each model's
    # measured decode rate; models with no rate yet just don't contribute.
    dec_tok = dec_time = 0.0
    for mid, agg in _eff.items():
        dtps = (svc.registry.get(mid) or {}).get("eval_tps_avg") or 0
        if dtps and agg["tok"]:
            dec_tok += agg["tok"]
            dec_time += agg["tok"] / dtps
    summary = {
        "window": len(window),
        # Effective throughput = output tokens / wall time across the window
        # (includes prefill + queue, so it reads lower than pure decode tok/s —
        # it's the real end-to-end rate the operator experiences).
        "eff_tps": round(tok_total / (dur_total / 1000), 1) if dur_total else 0,
        # Pure decode rate over the same traffic — what the GPU sustains once
        # generating, with prefill/queue removed. eff_tps < decode_tps always.
        "decode_tps": round(dec_tok / dec_time, 1) if dec_time else 0,
        "avg_s": round(dur_total / 1000 / len(window), 1) if window else 0,
        "ok": ok, "err": err, "out_tokens": tok_total,
        "truncations_total": int(trunc_total),
        # Oldest→newest per-request points for the inline sparklines.
        "series": list(reversed(series)),
    }

    # Engine-level telemetry straight from llama.cpp / vLLM (/metrics, /slots):
    # KV-cache fill, queue depth, busy slots, preemptions, lifetime throughput.
    try:
        servers = await svc.pool.server_metrics()
    except Exception:
        servers = []
    servers = sorted(servers + await _cliproxy_servers(svc), key=lambda s: s.get("backend") or "")
    from .. import perf_history as _ph
    return {"models": active_models,
            # each in-flight call with its request id — the same id + clock
            # the client's "still working" line shows
            "calls": getattr(svc.pool, "active_requests", lambda: [])(),
            "version": __import__("foundry_router").__version__,
            "servers": servers,
            "run_label": _ph.get_run_label(svc.db),
            "tools": svc.mcp.active_calls(),
            "loaded": loaded,
            "loaded_detail": loaded_detail,
            "brain": brain,
            "backends": backends,
            "recent": recent,
            "summary": summary,
            "tool_stats": tool_stats}


@router.get("/admin/api/perf-history")
async def perf_history_api(request: Request):
    """Time-series performance samples for the Performance tab: raw per-call
    points within a window (for charting) plus window-wide summary averages.
    `hours` sets the window; `model` narrows to one model (omit for the fleet)."""
    svc = _svc(request)
    from .. import perf_history as ph
    try:
        hours = float(request.query_params.get("hours") or 72)
    except (TypeError, ValueError):
        hours = 72
    hours = max(0.5, min(hours, 24 * 30))          # clamp: 30 min … 30 days
    q = request.query_params
    model = q.get("model") or None
    backend = q.get("backend") or None
    # run_label absent = every run; present (even "") = that run only ("" is
    # the unlabelled samples recorded before a label was set).
    run_label = q.get("run_label") if "run_label" in q else None
    out = ph.query_history(svc.db, hours=hours, model=model, backend=backend,
                           run_label=run_label)
    # The backend filter lists every configured backend, not only those with
    # samples in the window, so one with no traffic yet shows as (0).
    seen = {b.get("backend") for b in out.get("backends") or []}
    out["backends"] = list(out.get("backends") or []) + [
        {"backend": n, "n": 0} for n in _configured_backends(svc) if n not in seen]
    return out


def _configured_backends(svc) -> list[str]:
    return sorted(list(getattr(svc.pool, "backends", {}) or {})
                  + [b.name for b in getattr(svc.pool, "disabled", []) or []])


@router.get("/admin/api/perf/servers")
async def perf_servers(request: Request, hours: float = 24, backend: str = "",
                       metrics: str = ""):
    """Engine metrics history (server_history.py): per backend and metric, the
    series sampled from the engines' own /metrics (Strata / llama.cpp KV fill,
    slots, tokens/s; Meridian counters and latencies)."""
    from .. import server_history
    svc = _svc(request)
    out = server_history.query(svc.db, hours, backend,
                               [m for m in metrics.split(",") if m] or None)
    out["interval_s"] = svc.config_store.config.perf.server_sample_seconds
    out["retention_days"] = svc.config_store.config.perf.retention_days
    return out


@router.get("/admin/api/config/perf")
async def get_perf_settings(request: Request):
    return _svc(request).config_store.config.perf.model_dump()


@router.post("/admin/api/config/perf")
async def set_perf_settings(request: Request):
    """{retention_days?, server_sample_seconds?, subscription_rates?: [{match, model?,
    usd_per_call}]}: live."""
    svc = _svc(request)
    body = await request.json()
    updates = {k: body[k] for k in ("retention_days", "server_sample_seconds",
                                    "subscription_rates") if k in body}

    def mutate(raw):
        raw.setdefault("perf", {}).update(updates)
    try:
        cfg = svc.config_store.save(mutate)
    except Exception as e:                                        # noqa: BLE001
        from ..errors import describe_exception
        return JSONResponse({"error": describe_exception(e)}, status_code=400)
    return {"ok": True, "perf": cfg.perf.model_dump()}


@router.get("/admin/api/perf/run-label")
async def perf_run_label_get(request: Request):
    from .. import perf_history as ph
    return {"run_label": ph.get_run_label(_svc(request).db)}


@router.post("/admin/api/perf/run-label")
async def perf_run_label_set(request: Request):
    """Set the label stamped on every new perf sample (e.g. "2x2080ti-22gb"),
    so before/after a hardware or config change can be compared per model.
    Blank clears it. Existing samples keep the label they were recorded with."""
    svc = _svc(request)
    from .. import perf_history as ph
    body = await request.json()
    label = ph.set_run_label(svc.db, str(body.get("run_label") or ""))
    svc.db.log_event("info", "admin",
                     f"performance run label set to {label!r}" if label
                     else "performance run label cleared")
    return {"ok": True, "run_label": label}


@router.get("/admin/api/perf/advisor")
async def perf_advisor(request: Request, hours: float = 24):
    """Plain-English performance findings (severity, what, why, how to fix,
    evidence) from history, live engine metrics, events, health and config."""
    from .. import perf_advisor as _pa
    return await _pa.advise(_svc(request), hours=max(1.0, min(float(hours), 720.0)))


@router.post("/admin/api/perf/clear")
async def perf_clear(request: Request):
    """Wipe performance data so the dashboards start clean (e.g. after a GPU
    swap). Scope: every model, or one `model`; perf-history samples can also be
    narrowed to a `backend` / `run_label`. Targets (booleans, defaults shown):
      samples=true       Performance-tab history (perf_samples)
      live=true          Live-view rolling averages (tok/s, TTFT, load, spec, cache)
      truncations=true   truncation counters (+ their Events warnings)
      requests=false     the Usage Log (request_log) — feeds Live's effective
                         tok/s and "recently finished"; all-models scope only
      benchmarks=false   the observed `latency` score derived from old tok/s
    Registry metadata, personas and config are never touched."""
    svc = _svc(request)
    from .. import perf_history as ph
    b = await request.json()
    model = (b.get("model") or "").strip() or None
    backend = (b.get("backend") or "").strip() or None
    run_label = b.get("run_label") if "run_label" in b else None
    want = {k: bool(b.get(k, d)) for k, d in (("samples", True), ("live", True),
                                              ("truncations", True),
                                              ("requests", False),
                                              ("benchmarks", False))}
    out: dict = {"ok": True}
    if want["samples"]:
        out["samples"] = ph.clear_samples(svc.db, model=model, backend=backend,
                                          run_label=run_label)
    if want["live"] or want["truncations"] or want["benchmarks"]:
        out["models_reset"] = svc.registry.reset_perf_stats(
            model, speed=want["live"], truncations=want["truncations"],
            latency_benchmark=want["benchmarks"])
    if want["truncations"]:
        if model:
            out["events"] = svc.db.execute(
                "DELETE FROM event_log WHERE source='facade' AND "
                "message LIKE ? AND message LIKE '%TRUNCATED%'", (f"{model} %",))
        else:
            out["events"] = svc.db.execute(
                "DELETE FROM event_log WHERE source='facade' "
                "AND message LIKE '%TRUNCATED%'")
    if want["requests"] and not model:
        out["requests"] = svc.db.execute("DELETE FROM request_log")
    scope = f"model {model}" if model else "all models"
    if backend:
        scope += f", backend {backend}"
    if run_label is not None:
        scope += f", run {run_label or '(unlabelled)'}"
    targets = ", ".join(k for k, v in want.items() if v) or "nothing"
    svc.db.log_event("info", "admin", f"performance data cleared ({scope}: {targets})",
                     str({k: v for k, v in out.items() if k != "ok"}))
    return out


@router.get("/admin/api/mcp-metrics")
async def mcp_metrics_api(request: Request):
    """Aggregated MCP tool usage (every call through Foundry, any caller):
    totals, per tool / server / source, hourly series. Filters: hours, source,
    server."""
    svc = _svc(request)
    from .. import mcp_metrics
    q = request.query_params
    try:
        hours = max(0.5, min(float(q.get("hours") or 24), 24 * 30))
    except (TypeError, ValueError):
        hours = 24
    out = mcp_metrics.summary(svc.db, hours=hours, source=q.get("source") or None,
                              server=q.get("server") or None)
    out["live"] = {"in_flight": svc.mcp.active_calls(),
                   "sessions": svc.mcp.session_status() if hasattr(svc.mcp, "session_status") else {}}
    return out


@router.get("/admin/api/mcp-metrics/recent")
async def mcp_metrics_recent(request: Request):
    svc = _svc(request)
    from .. import mcp_metrics
    q = request.query_params
    try:
        hours = float(q.get("hours")) if q.get("hours") else None
        limit = int(q.get("limit") or 100)
    except (TypeError, ValueError):
        hours, limit = None, 100
    return {"calls": mcp_metrics.recent(
        svc.db, limit=limit, hours=hours, source=q.get("source") or None,
        server=q.get("server") or None, tool=q.get("tool") or None,
        errors_only=q.get("errors") in ("1", "true"))}


@router.post("/admin/api/mcp-metrics/clear")
async def mcp_metrics_clear(request: Request):
    svc = _svc(request)
    from .. import mcp_metrics
    b = await request.json() if (await request.body()) else {}
    n = mcp_metrics.clear(svc.db, server=(b.get("server") or None))
    svc.db.log_event("info", "admin", f"MCP metrics cleared ({n} call(s)"
                     + (f", server {b['server']}" if b.get("server") else "") + ")")
    return {"ok": True, "removed": n}


@router.get("/admin/api/mcp-context")
async def mcp_context_api(request: Request):
    """Context cost of MCP tool definitions per persona / aggregator endpoint.
    `ctx` = the backend context window (tokens) to express costs as a %."""
    svc = _svc(request)
    from .. import mcp_metrics
    try:
        ctx = int(request.query_params.get("ctx") or 0) or None
    except (TypeError, ValueError):
        ctx = None
    return mcp_metrics.context_budget(svc, ctx)


@router.get("/admin/api/pricing")
async def pricing_list(request: Request):
    """Editable paid-service token rates for the cost calculator."""
    svc = _svc(request)
    from .. import pricing
    return {"services": pricing.list_services(svc.db)}


@router.post("/admin/api/pricing")
async def pricing_upsert(request: Request):
    """Add or edit one service's rates (USD per 1M tokens)."""
    svc = _svc(request)
    from .. import pricing
    b = await request.json()
    name = (b.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    try:
        pricing.upsert_service(
            svc.db, id=b.get("id"), name=name,
            input_per_1m=float(b.get("input_per_1m") or 0),
            output_per_1m=float(b.get("output_per_1m") or 0),
            cached_input_per_1m=(None if b.get("cached_input_per_1m") in (None, "")
                                 else float(b["cached_input_per_1m"])),
            enabled=bool(b.get("enabled", True)), notes=b.get("notes") or "",
            locked=(None if b.get("locked") is None else bool(b.get("locked"))))
    except (TypeError, ValueError):
        return JSONResponse({"error": "rates must be numbers"}, status_code=400)
    return {"ok": True}


@router.post("/admin/api/pricing/update")
async def pricing_update(request: Request):
    """Refresh unlocked service prices from OpenRouter now (brain-matched, with
    a heuristic fallback). Body: {dry_run?: bool, force?: bool}."""
    svc = _svc(request)
    from .. import pricing_update
    b = await request.json() if (await request.body()) else {}
    return await pricing_update.update_prices(
        svc.db, svc.http, brain=svc.brain, force=bool(b.get("force")),
        dry_run=bool(b.get("dry_run")))


@router.get("/admin/api/pricing/settings")
async def pricing_settings(request: Request):
    from .. import pricing_update
    return pricing_update.settings(_svc(request).db)


@router.post("/admin/api/pricing/settings")
async def pricing_settings_set(request: Request):
    from .. import pricing_update
    b = await request.json()
    try:
        return pricing_update.save_settings(
            _svc(request).db, auto=b.get("auto"),
            days=(int(b["days"]) if b.get("days") not in (None, "") else None))
    except (TypeError, ValueError):
        return JSONResponse({"error": "days must be a number"}, status_code=400)


@router.post("/admin/api/pricing/delete")
async def pricing_delete(request: Request):
    svc = _svc(request)
    from .. import pricing
    b = await request.json()
    if b.get("id"):
        pricing.delete_service(svc.db, int(b["id"]))
    return {"ok": True}


@router.get("/admin/api/cost-compare")
async def cost_compare(request: Request):
    """Price the selected window's tokens against every enabled service, plus a
    rough self-hosting electricity estimate, for a local-vs-cloud comparison."""
    svc = _svc(request)
    from .. import pricing
    q = request.query_params

    def _f(key, default):
        try:
            return float(q.get(key) or default)
        except (TypeError, ValueError):
            return default
    hours = max(0.5, min(_f("hours", 72), 24 * 30))

    def _kind(model_id):
        info = svc.pool.backend_info(model_id) or {}
        t = info.get("type") or ""
        if t == "openai-compatible" and info.get("flavor") in ("llamacpp", "vllm", "unsloth"):
            return "openai-compatible-local"
        if t == "openai-compatible" and info.get("subscription"):
            return "openai-compatible-subscription"
        return t
    return pricing.compute_costs(
        svc.db, hours=hours, model=(q.get("model") or None),
        watts=max(0.0, _f("watts", pricing.DEFAULT_WATTS)),
        kwh_rate=max(0.0, _f("kwh", pricing.DEFAULT_KWH_RATE)),
        backend=(q.get("backend") or None),
        run_label=(q.get("run_label") if "run_label" in q else None),
        backend_type=_kind)


@router.post("/admin/api/mcp-aggregator")
async def set_mcp_aggregator(request: Request):
    svc = _svc(request)
    body = await request.json()
    allowed = {"enabled", "token", "token_header", "base_path", "advertise_url",
               "profiles", "progress_heartbeat_seconds", "poll_guard_threshold",
               "persona_endpoints"}
    updates = {k: v for k, v in body.items() if k in allowed}

    def mutate(raw):
        raw.setdefault("mcp_aggregator", {}).update(updates)
    svc.config_store.save(mutate)
    # Mounting new endpoints requires re-running the app lifespan, so the
    # aggregator's endpoint set only changes on restart. Report that back.
    return {"ok": True, "restart_required": True}


@router.post("/admin/api/restart")
async def restart_service(request: Request):
    """Restart Foundry by terminating the process — a supervisor (Docker
    `restart: unless-stopped`, systemd, etc.) brings it back. Used to (re)mount
    Foundry-MCP endpoints after a config change. No-op-safe: if nothing
    supervises the process it simply stops, so the UI warns about that."""
    import asyncio
    import os
    import signal
    svc = _svc(request)
    svc.db.log_event("warning", "main", "service restart requested from admin UI")

    async def _terminate():
        await asyncio.sleep(0.4)          # let the HTTP response flush first
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_terminate())
    return {"ok": True, "restarting": True}


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
        backends.append({"backend": s.config.name, "healthy": s.healthy, **snap,
                         "raw": svc.meridian_usage.last_raw(s.config.url)})
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
        tele = await svc.meridian_usage.telemetry(s.config.url, s.config.api_key)
        out.append({"backend": s.config.name, "url": s.config.url,
                    **health, "telemetry": tele})
    return {"backends": out, "checked": utcnow()}


def _cliproxy_infos(svc) -> list[dict]:
    out = []
    for s in getattr(svc.pool, "backends", {}).values():
        c = s.config
        info = {"name": c.name, "url": c.url, "flavor": c.effective_flavor,
                "management_key": c.management_key, "management_url": c.management_url,
                "usage_limit_pct": c.usage_limit_pct, "healthy": s.healthy}
        if c.effective_flavor == "cliproxyapi":
            out.append(info)
    return out


async def _cliproxy_servers(svc) -> list[dict]:
    """CLIProxyAPI has no engine /metrics; its Live row is the signed-in
    accounts and their usage windows (management API, cached), or what to set."""
    out = []
    cu = getattr(svc, "cliproxy_usage", None)
    for info in _cliproxy_infos(svc):
        row = {"backend": info["name"], "flavor": "cliproxyapi", "metrics_ok": False}
        if cu is None or not cu.configured(info):
            row["metrics_error"] = "no management_key — set it on the backend for account usage"
        else:
            try:
                snap = await cu.snapshot(info)
            except Exception as e:                                 # noqa: BLE001
                snap = {"ok": False, "error": str(e)[:200], "accounts": []}
            row["metrics_ok"] = bool(snap.get("ok"))
            row["metrics_error"] = snap.get("error") or ""
            row["accounts"] = [{"provider": a.get("provider"), "label": a.get("label"),
                                "plan": a.get("plan"),
                                "windows": [{"name": w.get("name"), "used": w.get("used")}
                                            for w in a.get("windows") or []]}
                               for a in snap.get("accounts") or []]
        out.append(row)
    return out


@router.get("/admin/api/subscription-usage")
async def subscription_usage(request: Request):
    """Usage windows each CLIProxyAPI account reported (ChatGPT/Codex 5-hour
    and weekly, Claude Code 5h/7d), and the limit at which Foundry stops
    using that account's models."""
    svc = _svc(request)
    out = []
    for info in _cliproxy_infos(svc):
        if not info["management_key"]:
            out.append({"backend": info["name"], "ok": False, "configured": False,
                        "error": "no management_key set on this backend", "accounts": [],
                        "limit_pct": svc.cliproxy_usage.limit_for(info)})
            continue
        refresh = request.query_params.get("refresh") in ("1", "true")
        snap = await svc.cliproxy_usage.snapshot(info, force=refresh)
        out.append({**snap, "configured": True})
    return {"backends": out, "limit_pct": svc.config_store.config.subscription_usage.limit_pct,
            "poll_seconds": svc.config_store.config.subscription_usage.poll_seconds}


@router.post("/admin/api/config/subscription_usage")
async def set_subscription_usage(request: Request):
    svc = _svc(request)
    body = await request.json()
    updates = {}
    if "limit_pct" in body:
        try:
            updates["limit_pct"] = max(1.0, min(100.0, float(body["limit_pct"])))
        except (TypeError, ValueError):
            return JSONResponse({"error": "limit_pct must be a number"}, status_code=400)
    if "poll_seconds" in body:
        try:
            updates["poll_seconds"] = max(0, int(body["poll_seconds"]))
        except (TypeError, ValueError):
            return JSONResponse({"error": "poll_seconds must be an integer"}, status_code=400)
    for k, hi in (("reserve_pct", 50.0), ("inflight_pct", 20.0),
                  ("inflight_ttl_seconds", 3600.0), ("cache_seconds", 3600.0)):
        if k in body:
            try:
                updates[k] = max(0.0, min(hi, float(body[k])))
            except (TypeError, ValueError):
                return JSONResponse({"error": f"{k} must be a number"}, status_code=400)
    if "fail_closed" in body:
        updates["fail_closed"] = bool(body["fail_closed"])

    def mutate(raw):
        raw.setdefault("subscription_usage", {}).update(updates)
    cfg = svc.config_store.save(mutate)
    return {"ok": True, "subscription_usage": cfg.subscription_usage.model_dump()}


def _cutoff_rows(svc) -> list[dict]:
    from .. import cutoffs
    return cutoffs.rows(svc)


@router.get("/admin/api/config/cutoffs")
async def get_cutoffs(request: Request):
    """The usage cutoff (percent used) per backend — one place for Claude and
    GPT. `claude` / `gpt` are the headline values: the first Claude (Meridian)
    backend's cutoff and the GPT default every CLIProxyAPI backend without its
    own override uses. (cutoffs.py; the operator chat uses the same code.)"""
    from .. import cutoffs
    return cutoffs.view(_svc(request))


@router.post("/admin/api/config/cutoffs")
async def set_cutoffs(request: Request):
    """{"claude": 90, "gpt": 95, "backends": {"<name>": 80 | null}}. `claude`
    sets the cutoff on every Meridian backend (null clears it back to the
    conserve_* ladder); `gpt` sets the global default for CLIProxyAPI backends
    (subscription_usage.limit_pct); `backends` sets/clears one backend's own."""
    from .. import cutoffs
    body = await request.json()
    try:
        return cutoffs.apply(_svc(request), body)
    except cutoffs.CutoffError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status)


# --------------------------------------------------------------------------- #
# Config history / rollback, snapshots, export / import, diagnostics          #
# --------------------------------------------------------------------------- #

@router.get("/admin/api/config/versions")
async def config_versions(request: Request):
    svc = _svc(request)
    return {"versions": svc.config_history.versions(),
            "config_version": svc.config_store.raw.get("config_version")}


@router.get("/admin/api/config/versions/{vid}")
async def config_version_detail(request: Request, vid: int):
    svc = _svc(request)
    y = svc.config_history.redacted_yaml(vid)
    if y is None:
        return JSONResponse({"error": "no such version"}, status_code=404)
    return {"id": vid, "yaml": y, "diff_to_current": svc.config_history.diff(vid)}


@router.post("/admin/api/config/versions/{vid}/restore")
async def config_version_restore(request: Request, vid: int):
    svc = _svc(request)
    try:
        r = await svc.config_history.restore(vid)
    except KeyError:
        return JSONResponse({"error": "no such version"}, status_code=404)
    except Exception as e:                                        # noqa: BLE001
        return JSONResponse({"error": f"that version doesn't load: {e}"}, status_code=400)
    svc.db.log_event("warning", "config", f"config restored from version {vid}",
                     json.dumps(r))
    return {"ok": True, **r}


@router.get("/admin/api/snapshots")
async def state_snapshots(request: Request):
    from .. import configops
    return {"snapshots": configops.snapshots(_svc(request))}


@router.post("/admin/api/snapshots")
async def take_snapshot(request: Request):
    from .. import configops
    body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
    sid = configops.snapshot(_svc(request), (body or {}).get("reason") or "manual snapshot")
    return {"ok": True, "id": sid}


@router.post("/admin/api/snapshots/{sid}/restore")
async def restore_state_snapshot(request: Request, sid: int):
    from .. import configops
    svc = _svc(request)
    try:
        r = configops.restore_snapshot(svc, sid)
    except KeyError:
        return JSONResponse({"error": "no such snapshot"}, status_code=404)
    svc.db.log_event("warning", "config", f"personas/pauses restored from snapshot {sid}",
                     json.dumps(r))
    return {"ok": True, **r}


@router.get("/admin/api/export")
async def export_bundle(request: Request, secrets: int = 0):
    from .. import configops
    from fastapi.responses import Response
    bundle = configops.export_bundle(_svc(request), include_secrets=bool(secrets))
    name = f"foundry-export-{utcnow()[:19].replace(':', '').replace('-', '')}.json"
    return Response(json.dumps(bundle, indent=1, default=str), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/admin/api/import")
async def import_bundle(request: Request):
    """{bundle, sections: [...], persona_mode: merge|add}."""
    from .. import configops
    svc = _svc(request)
    body = await request.json()
    try:
        r = await configops.import_bundle(svc, body.get("bundle") or {},
                                          body.get("sections") or list(configops.EXPORT_SECTIONS),
                                          body.get("persona_mode") or "merge")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:                                        # noqa: BLE001
        return JSONResponse({"error": f"import failed: {e}"}, status_code=400)
    svc.db.log_event("warning", "config", "configuration imported", json.dumps(r, default=str)[:2000])
    return {"ok": True, **r}


@router.get("/admin/api/diagnostics")
async def diagnostics(request: Request):
    from .. import configops
    from fastapi.responses import Response
    data = await configops.diagnostics_zip(_svc(request))
    name = f"foundry-diagnostics-{utcnow()[:19].replace(':', '').replace('-', '')}.zip"
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/admin/api/compare")
async def compare_clients(request: Request, hours: float = 168, group: str = "client"):
    """Client (or persona) comparison from Foundry's own records."""
    from .. import compare
    return compare.report(_svc(request), hours=max(1.0, min(2160.0, hours)),
                          group="persona" if group == "persona" else "client")


@router.get("/admin/api/work")
async def work_view(request: Request, project: str = "", hours: float = 72):
    """Coordinator tasks, bound sessions with their measured usage, and
    lifecycle events (orchestration.py)."""
    return _svc(request).work.view(project, hours=max(1.0, min(2160.0, hours)))


@router.get("/admin/api/work/usage")
async def work_usage(request: Request, project: str = "", task: str = "", run: str = "",
                     session: str = ""):
    return _svc(request).work.usage(project, task, run, session)


@router.get("/admin/api/work/scorecard")
async def work_scorecard(request: Request, project: str = "", task: str = "",
                         hours: float = 24 * 14, limit: int = 100):
    """Per-sprint scorecard (scorecard.py): wall time, model calls, tokens by role,
    gates run / passed, reviews, vision rounds, helpers used / integrated, speeds
    (measured apart from estimated), cost and outcome."""
    from .. import scorecard
    return scorecard.build(_svc(request).db, project, task,
                           hours=max(0.0, min(float(hours), 24 * 365)), limit=limit)


def _episode_card(r) -> dict:
    """A stored episode card; a row that does not parse (an older Foundry cut
    long cards mid-JSON) becomes a stub from its columns instead of a 500."""
    from ..jsonfit import loads_or
    d = loads_or(r["data"], None)
    if isinstance(d, dict):
        return d
    return {"episode_id": r["episode_id"], "project_id": r["project_id"],
            "task_id": r["task_id"], "kind": r["kind"], "status": r["status"],
            "reasons": ["card_unreadable: re-push from the workstation"], "turns": []}


@router.get("/admin/api/training")
async def training_view(request: Request, project: str = "", status: str = ""):
    """Training data: Foundry's own capture status, and what the training
    pipeline reported (counts, gaps, datasets, episode summaries)."""
    from ..jsonfit import loads_or
    svc = _svc(request)
    pipes = []
    for r in svc.db.query("SELECT * FROM training_status ORDER BY updated_ts DESC"):
        d = loads_or(r["data"], None)
        if not isinstance(d, dict):              # one bad row must not blank the whole tab
            d = {"error": "stored status does not parse; it is replaced on the next push"}
        pipes.append({**d, "pipeline_id": r["pipeline_id"], "updated_ts": r["updated_ts"]})
    q, args = "SELECT * FROM training_episodes WHERE 1=1", []
    if project:
        q += " AND project_id=?"
        args.append(project)
    if status:
        q += " AND status=?"
        args.append(status)
    eps = []
    for r in svc.db.query(q + " ORDER BY updated_ts DESC LIMIT 300", tuple(args)):
        d = _episode_card(r)
        eps.append({k: d.get(k) for k in ("episode_id", "project_id", "task_id", "kind", "status",
                                          "reasons", "split", "outcome", "sources", "turns",
                                          "summary")})
    # what the Capture card renders its backend / persona switches from
    try:
        personas = sorted(p["virtual_name"] for p in svc.personas.list(enabled_only=True))
    except Exception:                                             # noqa: BLE001
        personas = []
    return {"capture": svc.capture.status(), "pipelines": pipes, "episodes": eps,
            "options": {"backends": _configured_backends(svc), "personas": personas}}


_CAPTURE_LISTS = ("projects", "exclude_projects", "clients", "backends", "exclude_backends",
                  "personas", "exclude_personas", "allowed_email_domains")


@router.post("/admin/api/training/capture")
async def training_capture_set(request: Request):
    """Capture switches from the Training tab: enabled, include_unbound,
    retention_days and the project / client / backend / persona lists (globs;
    empty include = all). Live: capture re-reads its config on every call."""
    svc = _svc(request)
    body = await request.json()
    updates: dict = {}
    for k in ("enabled", "include_unbound"):
        if k in body:
            updates[k] = bool(body[k])
    if "retention_days" in body:
        try:
            updates["retention_days"] = max(1, min(3650, int(body["retention_days"])))
        except (TypeError, ValueError):
            return JSONResponse({"error": "retention_days must be a number"}, status_code=400)
    for k in _CAPTURE_LISTS:
        if k in body:
            v = body[k]
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                return JSONResponse({"error": f"{k} must be a list of strings"}, status_code=400)
            updates[k] = [x.strip() for x in v if x.strip()][:200]

    def mutate(raw):
        raw.setdefault("training_capture", {}).update(updates)
    try:
        cfg = svc.config_store.save(mutate)
    except Exception as e:                                        # noqa: BLE001
        from ..errors import describe_exception
        return JSONResponse({"error": describe_exception(e)}, status_code=400)
    svc.db.log_event("info", "admin", "training capture settings changed: "
                     + ", ".join(sorted(updates)) if updates else "training capture saved")
    return {"ok": True, "training_capture": cfg.training_capture.model_dump(),
            "capture": svc.capture.status()}


@router.get("/admin/api/training/capture/download")
async def training_capture_download(request: Request, hours: float = 0, backend: str = "",
                                    persona: str = "", project: str = "", quality: str = "",
                                    format: str = "chatml", limit: int = 20000):
    """Captured calls as JSONL (already redacted): `format=chatml` is one
    {messages, tools, meta} line per successful backend call, `raw` the capture
    rows as stored. `quality=ok` keeps calls the router saw succeed with no
    thumbs-down or negative label (router_ok); `quality=verified` also needs a
    coordinator label (gate PASS, review APPROVED, vision approved)."""
    import time as _time
    from fastapi.responses import StreamingResponse
    from .. import training_quality as tq
    svc = _svc(request)
    cap = svc.capture
    if quality not in ("", "ok", "verified"):
        return JSONResponse({"error": "quality must be ok or verified"}, status_code=400)
    if format not in ("chatml", "raw"):
        return JSONResponse({"error": "format must be chatml or raw"}, status_code=400)
    since = _time.time() - hours * 3600 if hours else 0.0
    limit = max(1, min(200000, int(limit)))

    def lines():
        if cap._store is None and not cap.cfg.enabled:
            return
        after, sent = 0, 0
        while sent < limit:
            rows = cap.records(after, 500, project, backend=backend, persona=persona,
                               kind="backend_call", since=since, db=svc.db)
            if not rows:
                return
            after = rows[-1]["id"]
            for r in rows:
                if not tq.passes(r, quality):
                    continue
                if format == "raw":
                    out = r
                else:
                    out = tq.chatml_example(r)
                    if out is None:
                        continue
                    out["meta"] = {k: r.get(k) for k in ("request_id", "ts", "backend", "model",
                                                         "persona", "project_id", "role")}
                    out["meta"]["quality"] = r.get("quality")
                yield json.dumps(out, default=str, ensure_ascii=False) + "\n"
                sent += 1
                if sent >= limit:
                    return
    name = f"foundry-captured-calls-{utcnow()[:10]}.jsonl"
    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/admin/api/training/datasets")
async def training_datasets(request: Request, project: str = ""):
    """Fine-tuning datasets uploaded by the training pipeline (newest first)."""
    import os
    from ..jsonfit import loads_or
    q, args = "SELECT * FROM training_datasets", ()
    if project:
        q, args = q + " WHERE project_id=?", (project,)
    out = []
    for r in _svc(request).db.query(q + " ORDER BY uploaded_ts DESC LIMIT 500", args):
        m = loads_or(r.pop("manifest"), None)
        if not isinstance(m, dict):              # an older Foundry cut long manifests
            try:
                with open(os.path.join(r["path"], "manifest.json"), encoding="utf-8") as f:
                    m = loads_or(f.read(), None)
            except OSError:
                m = None
        m = m if isinstance(m, dict) else {}
        r["validation_ok"] = bool((m.get("validation") or {}).get("ok"))
        r["episodes"] = m.get("episode_count") or len(m.get("episodes") or [])
        r["created"] = m.get("created")
        out.append(r)
    return {"datasets": out}


@router.get("/admin/api/training/datasets/{dataset_id}/{name}")
async def training_dataset_file(request: Request, dataset_id: str, name: str):
    """Download a stored dataset file (data.jsonl or manifest.json)."""
    from fastapi.responses import FileResponse
    import os
    if name not in ("data.jsonl", "manifest.json"):
        return JSONResponse({"error": "file must be data.jsonl or manifest.json"}, status_code=400)
    r = _svc(request).db.query_one("SELECT path FROM training_datasets WHERE dataset_id=?",
                                   (dataset_id,))
    if not r or not os.path.isfile(os.path.join(r["path"], name)):
        return JSONResponse({"error": "unknown dataset"}, status_code=404)
    return FileResponse(os.path.join(r["path"], name),
                        media_type="application/json" if name.endswith(".json")
                        else "application/x-ndjson", filename=f"{dataset_id}-{name}")


@router.get("/admin/api/training/episodes/{episode_id}")
async def training_episode(request: Request, episode_id: str):
    rows = _svc(request).db.query("SELECT * FROM training_episodes WHERE episode_id=?",
                                  (episode_id,))
    if not rows:
        return JSONResponse({"error": "unknown episode"}, status_code=404)
    return _episode_card(rows[0])


@router.get("/admin/api/routes")
async def routes_graph(request: Request, hours: float = 24, all: int = 0, active: int = 0):
    """Routing graph from the live configuration + traffic / denials /
    failures / image jobs / brain calls in the last `hours`, plus calls in
    flight. active=1: only what was observed (no configuration)."""
    from .. import routes_view
    return routes_view.graph(_svc(request), hours=max(0.02, min(720.0, hours)),
                             show_all=bool(all), active=bool(active))


@router.get("/admin/api/brain/metrics")
async def brain_metrics(request: Request, hours: float = 24, buckets: int = 24):
    """Brain calls over `hours`: per hour, latency p50/p95, tokens/s, outcomes
    (failed / skipped = the request fell back to the static rule), by purpose,
    a time series and recent failures."""
    from .. import observability
    return observability.brain_metrics(_svc(request).db, hours, max(4, min(96, buckets)))


@router.get("/admin/api/clients/activity")
async def clients_activity(request: Request, hours: float = 24, buckets: int = 24):
    """Who uses the router: per client key (or "untracked · <IP>" for requests
    without a key) requests, errors, tokens, personas, models, IPs, user
    agents, last seen, a request series and calls in flight."""
    from .. import observability
    return observability.clients_activity(_svc(request), hours, max(4, min(96, buckets)))


@router.get("/admin/api/live")
async def live_calls(request: Request):
    """Backend calls and brain calls in flight right now, with who asked."""
    from .. import observability
    return observability.live(_svc(request))


@router.get("/admin/api/routes/edge")
async def routes_edge(request: Request, kind: str, src: str, dst: str, hours: float = 24):
    from .. import routes_view
    try:
        return {"requests": routes_view.edge_requests(_svc(request), kind, src, dst, hours)}
    except (KeyError, ValueError):
        return JSONResponse({"error": "unknown edge"}, status_code=400)


@router.get("/admin/api/requests/{request_id}/trace")
async def request_trace(request: Request, request_id: str):
    from .. import routes_view
    t = routes_view.trace(_svc(request), request_id)
    if not t:
        return JSONResponse({"error": "nothing recorded for that request id"}, status_code=404)
    return t


@router.get("/admin/api/health/functional")
async def functional_health(request: Request):
    """Per backend: reachable (HTTP) vs serving (a real or probe generation
    recently succeeded), last success / failure, calls and errors in the last
    hour; plus the brain and the image-job backlog."""
    svc = _svc(request)
    media = {}
    try:
        m = svc.media.summary()
        media = {"active": m["active"], "failed_24h": (m["last_24h"] or {}).get("failed", 0),
                 "backend": m["backend"], "backend_error": m["backend_error"]}
    except Exception:                                             # noqa: BLE001
        pass
    return {"backends": svc.health.status(), "brain": getattr(svc, "_brain_health", None),
            "queues": getattr(svc.pool, "queue_status", lambda: [])(),
            "breakers": getattr(svc.pool, "breaker_status", lambda: [])(),
            "media": media, "fresh_minutes": svc.config_store.config.health.fresh_minutes,
            "probe_minutes": svc.config_store.config.health.probe_minutes,
            "probe_paid": svc.config_store.config.health.probe_paid}


@router.post("/admin/api/health/probe")
async def functional_probe(request: Request):
    """{backend, model?}: one tiny real generation on exactly that backend."""
    svc = _svc(request)
    body = await request.json()
    try:
        return await svc.health.probe(body.get("backend") or "", body.get("model") or None)
    except KeyError:
        return JSONResponse({"error": "no such backend"}, status_code=404)


@router.post("/admin/api/backends/{name}/reset-breaker")
async def reset_breaker(request: Request, name: str):
    """Close a backend's circuit breaker now (after fixing the backend)
    instead of waiting for the cooldown and a trial call."""
    pool = _svc(request).pool
    if not getattr(pool, "reset_breaker", None) or not pool.reset_breaker(name):
        return JSONResponse({"error": "no such backend"}, status_code=404)
    return {"ok": True}


@router.get("/admin/api/budgets")
async def budgets_monitor(request: Request):
    """Budget ledger monitor: accounts (provider-reported vs measured),
    calls in flight, client/persona breakdowns, rules and their use, denials,
    pause switches."""
    from .. import budget
    return await budget.monitor(_svc(request))


@router.post("/admin/api/budgets/pause")
async def budgets_pause(request: Request):
    """{scope: account|backend|persona|client, value, paused: bool, note}."""
    svc = _svc(request)
    body = await request.json()
    try:
        svc.ledger.set_pause(body.get("scope") or "", str(body.get("value") or "").strip(),
                             bool(body.get("paused", True)), str(body.get("note") or "")[:200])
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    svc.db.log_event("warning" if body.get("paused", True) else "info", "budgets",
                     f"{body.get('scope')} {body.get('value')!r} "
                     f"{'PAUSED' if body.get('paused', True) else 'resumed'}")
    return {"ok": True, "pauses": svc.ledger.pauses()}


@router.post("/admin/api/config/budgets")
async def set_budgets(request: Request):
    """{enabled?, retention_days?, rules?: [BudgetRule]} — rules replace the list."""
    from ..config import BudgetsConfig
    svc = _svc(request)
    body = await request.json()
    cur = svc.config_store.config.budgets.model_dump()
    for k in ("enabled", "retention_days", "rules"):
        if k in body:
            cur[k] = body[k]
    try:
        validated = BudgetsConfig.model_validate(cur)
    except Exception as e:                                        # noqa: BLE001
        return JSONResponse({"error": f"invalid budgets: {e}"}, status_code=400)
    names = [r.name for r in validated.rules]
    if len(names) != len(set(names)):
        return JSONResponse({"error": "rule names must be unique"}, status_code=400)
    # Only what differs from the defaults goes into config.yaml (keeps the file
    # and its version diffs readable); a rule always keeps its name.
    dumped = validated.model_dump(exclude_defaults=True)
    if "rules" in cur:
        dumped["rules"] = [{"name": r.name, **r.model_dump(exclude_defaults=True)}
                           for r in validated.rules]

    def mutate(raw):
        raw["budgets"] = dumped
    cfg = svc.config_store.save(mutate)
    return {"ok": True, "budgets": cfg.budgets.model_dump()}


@router.get("/admin/api/media")
async def media_overview(request: Request):
    """Image jobs: budget use, models (last success = verified), config and
    the recent jobs of every client."""
    svc = _svc(request)
    base = str(request.base_url).rstrip("/")
    limit = int(request.query_params.get("limit", 30))
    return {"summary": svc.media.summary(),
            "config": svc.config_store.config.media.model_dump(),
            "jobs": [svc.media.view(r, base) for r in svc.media.list_jobs(limit)]}


@router.get("/admin/api/media/artifacts/{job_id}/{index}")
async def media_artifact_admin(request: Request, job_id: str, index: int):
    hit = _svc(request).media.artifact_path(job_id, index)
    if hit is None:
        return JSONResponse({"error": "no such artifact"}, status_code=404)
    return FileResponse(hit[0], media_type=hit[1]["mime"])


@router.post("/admin/api/media/jobs/{job_id}/cancel")
async def media_cancel_admin(request: Request, job_id: str):
    svc = _svc(request)
    row = await svc.media.cancel(job_id)
    if row is None:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return svc.media.view(row)


_MEDIA_INTS = {"max_concurrent": (1, 8), "max_queued": (1, 100), "jobs_per_5h": (0, 10000),
               "jobs_per_day": (0, 100000), "client_jobs_per_day": (0, 100000),
               "max_images_per_job": (1, 10), "job_timeout_seconds": (30, 3600),
               "max_attempts": (1, 5), "dedupe_minutes": (0, 1440), "retention_days": (0, 3650)}


@router.post("/admin/api/config/media")
async def set_media(request: Request):
    svc = _svc(request)
    body = await request.json()
    updates: dict = {}
    for k, (lo, hi) in _MEDIA_INTS.items():
        if k in body:
            try:
                updates[k] = max(lo, min(hi, int(body[k])))
            except (TypeError, ValueError):
                return JSONResponse({"error": f"{k} must be an integer"}, status_code=400)
    for k in ("enabled", "allow_paid_api", "mcp_tools"):
        if k in body:
            updates[k] = bool(body[k])
    for k in ("backend", "default_model", "draft_model"):
        if k in body:
            updates[k] = str(body[k] or "").strip()
    if "models" in body:
        models = body["models"]
        if isinstance(models, str):
            models = [m.strip() for m in models.replace("\n", ",").split(",")]
        if not isinstance(models, list):
            return JSONResponse({"error": "models must be a list"}, status_code=400)
        updates["models"] = [str(m).strip() for m in models if str(m).strip()]
    allowed = updates.get("models", svc.config_store.config.media.models)
    for k in ("default_model", "draft_model"):
        v = updates.get(k)
        if v and v not in allowed:
            return JSONResponse({"error": f"{k} {v!r} is not in models"}, status_code=400)

    def mutate(raw):
        raw.setdefault("media", {}).update(updates)
    cfg = svc.config_store.save(mutate)
    return {"ok": True, "media": cfg.media.model_dump()}


@router.post("/admin/api/config/meridian")
async def set_meridian(request: Request):
    svc = _svc(request)
    body = await request.json()
    allowed = {"quota_path", "min_window_fraction", "conserve_premium_at",
               "conserve_strong_at", "conserve_fable_at", "usage_credits",
               "confirm_user_paid_at", "quota_poll_seconds", "usage_profile"}
    updates = {k: v for k, v in body.items() if k in allowed}

    def mutate(raw):
        raw.setdefault("meridian", {}).update(updates)
    cfg = svc.config_store.save(mutate)
    svc.meridian_usage.cfg = cfg.meridian
    svc.meridian_usage.clear_cache()
    return {"ok": True, "meridian": cfg.meridian.model_dump()}


# --------------------------------------------------------------------------- #
# Meridian re-authentication (do it from the UI, not by SSHing into the host)  #
# --------------------------------------------------------------------------- #

@router.get("/admin/api/meridian/auth")
async def meridian_auth_settings(request: Request):
    """Companion-service config for the re-auth panel (URL + whether a token is
    set). The panel is always shown; without a companion URL only Refresh token
    (the direct HTTP path) is available."""
    from ..meridian_auth import auth_settings
    svc = _svc(request)
    return {**auth_settings(svc.db),
            "usage_profile": svc.config_store.config.meridian.usage_profile or ""}


@router.post("/admin/api/meridian/auth_config")
async def set_meridian_auth_config(request: Request):
    """Set the auth companion URL (+ optional bearer token — blank keeps the
    existing one). Same kv-stored pattern as the gateway inspect companion."""
    from ..meridian_auth import auth_settings, set_auth_settings
    svc = _svc(request)
    body = await request.json()
    set_auth_settings(svc.db, str(body.get("url") or ""),
                      body.get("token") if "token" in body else None)
    return {"ok": True, **auth_settings(svc.db)}


@router.post("/admin/api/meridian/refresh")
async def meridian_refresh(request: Request):
    """Refresh the Meridian access token — the common 'token expired' case. Tries
    Meridian's own HTTP endpoint (POST /auth/refresh) on every anthropic-
    compatible backend directly (no companion, no SSH); reports per-backend."""
    from ..meridian_auth import refresh_token_direct
    svc = _svc(request)
    out = []
    for s in getattr(svc.pool, "backends_of_type", lambda t: [])("anthropic-compatible"):
        res = await refresh_token_direct(svc, s.config.url, s.config.api_key)
        out.append({"backend": s.config.name, **res})
    svc.meridian_usage.clear_cache()
    return {"backends": out}


@router.post("/admin/api/meridian/reauth/start")
async def meridian_reauth_start(request: Request):
    """Begin a full OAuth re-login via the companion service: returns the sign-in
    URL to open plus a session_id to finish with. 400 if no companion is set."""
    from ..meridian_auth import login_start
    body = await request.json()
    profile = str(body.get("profile") or "").strip()
    if not profile:
        return JSONResponse({"error": "profile is required (e.g. 'victor')"},
                            status_code=400)
    res = await login_start(_svc(request), profile)
    if res.get("configured") is False:
        return JSONResponse(
            {"error": "auth companion service not configured — set its URL below "
                      "(runs contrib/meridian-auth-service on the Meridian host)"},
            status_code=400)
    return res


@router.post("/admin/api/meridian/reauth/complete")
async def meridian_reauth_complete(request: Request):
    """Finish the re-login: hand the pasted code back to the held login process."""
    from ..meridian_auth import login_complete
    svc = _svc(request)
    body = await request.json()
    session_id = str(body.get("session_id") or "")
    code = str(body.get("code") or "").strip()
    if not session_id or not code:
        return JSONResponse({"error": "session_id and code are required"},
                            status_code=400)
    res = await login_complete(svc, session_id, code)
    if res.get("configured") is False:
        return JSONResponse({"error": "auth companion service not configured"},
                            status_code=400)
    svc.meridian_usage.clear_cache()
    return res


# --------------------------------------------------------------------------- #
# Model registry (§4.9 item 2)                                                #
# --------------------------------------------------------------------------- #

@router.get("/admin/api/models")
async def list_models(request: Request):
    from .. import thinking
    svc = _svc(request)
    reachable = set(svc.pool.available_models().keys())
    rows = svc.registry.list_models()
    for r in rows:
        r["reachable"] = r["id"] in reachable
    # Reachable-but-unregistered models should be visible too
    for mid in sorted(reachable - {r["id"] for r in rows}):
        rows.append({"id": mid, "reachable": True, "source": None})
    # Curated thinking-level menu per model (Q1): the effort levels that are
    # actually meaningful for each — empty = model doesn't support thinking.
    for r in rows:
        btype = (svc.pool.backend_info(r["id"]) or {}).get("type", "")
        r["thinking_levels"] = thinking.supported_levels(
            r["id"], r.get("capabilities"), btype)
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
    from ..registry.models_db import score_source
    svc = _svc(request)
    bench = svc.registry.benchmarks(model_id)
    for b in bench:
        # score_source (seed|researched|observed|conflation|manual_override) so
        # the UI can flag a seed ESTIMATE instead of rendering it as observed data.
        b["score_source"] = score_source(b)
    # Cross-model duplicate flags: named benchmarks whose exact value also sits
    # on an unrelated model (mis-attribution signature) — surfaced for review.
    return {"benchmarks": bench,
            "named": svc.registry.named_benchmarks(model_id),
            "cross_model_flags": svc.registry.cross_model_named_flags(model_id)}


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
    # the same rules the operator chat's persona tools use (persona_rules.py)
    from .. import persona_rules
    clean, errors, warnings = persona_rules.validate(svc, body, allow_unknown=True)
    if errors:
        return JSONResponse({"error": "; ".join(errors), "errors": errors,
                             "warnings": warnings}, status_code=400)
    svc.personas.upsert(name, **clean)
    return {"ok": True, "persona": svc.personas.get(name), "warnings": warnings}


@router.get("/admin/api/scoreboard")
async def scoreboard(request: Request, hours: float = 168, persona: str = ""):
    """Turn-assist scoreboard: per conversation (turns, tool calls, errors per
    100 calls, interventions, frontier calls), errors by context zone."""
    from .. import interventions
    return interventions.scoreboard(_svc(request).db, hours=hours, persona=persona)


@router.get("/admin/api/interventions")
async def list_interventions(request: Request, hours: float = 24, limit: int = 200,
                             conversation: str = ""):
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    q, args = "SELECT * FROM interventions WHERE ts >= ?", [since]
    if conversation:
        q += " AND conversation_key = ?"
        args.append(conversation)
    rows = _svc(request).db.query(q + " ORDER BY id DESC LIMIT ?", (*args, int(limit)))
    return {"interventions": rows}


@router.get("/admin/api/interventions/export")
async def export_interventions(request: Request, hours: float = 24 * 30, kinds: str = ""):
    """JSONL fine-tuning material: trigger → intervention (→ what came next)."""
    from fastapi.responses import PlainTextResponse
    from .. import interventions
    body = interventions.export_jsonl(_svc(request).db, hours=hours,
                                      kinds=[k for k in kinds.split(",") if k] or None)
    return PlainTextResponse(body, media_type="application/x-ndjson", headers={
        "Content-Disposition": "attachment; filename=foundry-interventions.jsonl"})


# ---- data management (Logs -> Data) ------------------------------------------------

@router.get("/admin/api/data/tables")
async def data_tables(request: Request):
    from .. import datatools
    svc = _svc(request)
    cfg = svc.config_store.config.data_retention
    return {"tables": datatools.stats(svc.db, cfg.days), "retention": cfg.model_dump(),
            "formats": list(datatools.FORMATS)}


@router.get("/admin/api/data/export")
async def data_export(request: Request, table: str, format: str = "jsonl",
                      hours: float = 0, limit: int = 0):
    """One table as JSONL / CSV / JSON. Extra query params that name a column
    filter on it (e.g. &conversation_key=… &persona=…)."""
    from fastapi.responses import PlainTextResponse
    from .. import datatools
    svc = _svc(request)
    reserved = {"table", "format", "hours", "limit"}
    where = {k: v for k, v in request.query_params.items() if k not in reserved}
    try:
        body = datatools.export(svc.db, table, format, hours, limit, where)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    media = {"csv": "text/csv", "json": "application/json"}.get(format, "application/x-ndjson")
    return PlainTextResponse(body, media_type=media, headers={
        "Content-Disposition": f"attachment; filename=foundry-{table}.{format}"})


@router.get("/admin/api/data/export-all")
async def data_export_all(request: Request, format: str = "jsonl", hours: float = 0,
                          tables: str = ""):
    """Every exportable table (or a comma list) as one zip + manifest.json."""
    from fastapi.responses import Response
    from .. import datatools
    try:
        blob = datatools.export_all(_svc(request).db, format, hours,
                                    [t for t in tables.split(",") if t] or None)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return Response(blob, media_type="application/zip", headers={
        "Content-Disposition": f"attachment; filename=foundry-data-{format}.zip"})


@router.post("/admin/api/data/clear")
async def data_clear(request: Request):
    """{"table": ..., "older_than_days": N (0 = everything)} — log tables only."""
    from .. import datatools
    body = await request.json()
    try:
        n = datatools.clear(_svc(request).db, body.get("table") or "",
                            float(body.get("older_than_days") or 0))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "deleted": n}


@router.post("/admin/api/data/prune")
async def data_prune(request: Request):
    from .. import datatools
    svc = _svc(request)
    return {"ok": True, "pruned": datatools.prune(svc.db, svc.config_store.config.data_retention.days)}


@router.post("/admin/api/config/data_retention")
async def set_data_retention(request: Request):
    svc = _svc(request)
    body = await request.json()
    updates = {k: v for k, v in body.items() if k in ("days", "prune_interval_minutes")}

    def mutate(raw):
        raw.setdefault("data_retention", {}).update(updates)
    try:
        cfg = svc.config_store.save(mutate)
    except Exception as e:
        from ..errors import describe_exception
        return JSONResponse({"error": describe_exception(e)}, status_code=400)
    return {"ok": True, "data_retention": cfg.data_retention.model_dump()}


# ---- turn assist global settings (Routing -> Guardrails) ---------------------------

@router.get("/admin/api/config/turn_assist")
async def get_turn_assist(request: Request):
    """Global turn-assist settings, the built-in defaults/rules they override,
    and how often each pre-judge rule / detector has fired."""
    from .. import turn_assist as ta
    svc = _svc(request)
    cfg = svc.config_store.config.turn_assist
    hits = svc.db.query(
        "SELECT kind, detail, COUNT(*) AS n, MAX(ts) AS last FROM interventions "
        "WHERE kind IN ('prejudge_block', 'prejudge_fix') OR kind LIKE 'supervisor_%' "
        "GROUP BY kind, detail ORDER BY n DESC LIMIT 300")
    by_rule: dict = {}
    for h in hits:
        key = h["detail"] if h["kind"].startswith("prejudge") else h["kind"]
        d = by_rule.setdefault(key, {"n": 0, "last": ""})
        d["n"] += h["n"]
        d["last"] = max(d["last"], h["last"] or "")
    block, fix = ta.rule_tables({})
    return {"turn_assist": cfg.model_dump(), "builtin_defaults": ta.ASSIST_DEFAULTS,
            "builtin_block_rules": block, "builtin_fix_rules": fix,
            "builtin_error_patterns": ta.ERROR_PATTERNS, "hits": by_rule}


@router.post("/admin/api/config/turn_assist")
async def set_turn_assist(request: Request):
    """Replace any of: defaults (obj), block_rules, fix_rules, error_patterns
    (lists). Regexes are validated before saving. Applies live."""
    import re as _re
    svc = _svc(request)
    body = await request.json()
    updates = {k: v for k, v in body.items()
               if k in ("defaults", "block_rules", "fix_rules", "error_patterns")}
    for k in ("block_rules", "fix_rules"):
        for r in updates.get(k) or []:
            try:
                _re.compile(r.get("pattern") or "")
            except (_re.error, AttributeError) as e:
                return JSONResponse({"error": f"{k}: bad pattern {r!r}: {e}"}, status_code=400)
    for p in updates.get("error_patterns") or []:
        try:
            _re.compile(p)
        except _re.error as e:
            return JSONResponse({"error": f"error_patterns: bad pattern {p!r}: {e}"},
                                status_code=400)

    def mutate(raw):
        raw.setdefault("turn_assist", {}).update(updates)
    try:
        cfg = svc.config_store.save(mutate)
    except Exception as e:
        from ..errors import describe_exception
        return JSONResponse({"error": describe_exception(e)}, status_code=400)
    return {"ok": True, "turn_assist": cfg.turn_assist.model_dump()}


@router.get("/admin/api/subscription-budgets")
async def subscription_budgets(request: Request):
    """Calls used vs call_budget_5h / call_budget_daily per subscription backend."""
    from datetime import datetime, timedelta, timezone
    svc = _svc(request)
    out = []
    for b in svc.config_store.config.backend_pool.internal.backends:
        if not b.is_subscription:
            continue
        row = {"backend": b.name, "type": b.type, "enabled": b.enabled}
        for key, hours in (("5h", 5), ("daily", 24)):
            since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            n = svc.db.query_one(
                "SELECT COUNT(*) AS n FROM claude_usage_log WHERE backend=? AND ts >= ?",
                (b.name, since))
            cap = getattr(b, f"call_budget_{key}")
            used = int(n["n"]) if n else 0
            row[key] = {"used": used, "budget": cap,
                        "remaining": (max(0, cap - used) if cap else None)}
        out.append(row)
    return {"backends": out}


@router.post("/admin/api/personas/validate")
async def validate_persona(request: Request):
    """Real round trips through Foundry's own route (text, streaming, a tool
    call with a boolean argument, image input, length cut-off; one image job
    for image personas). Works on disabled personas. Stores the result."""
    from .. import persona_validate
    body = await request.json()
    try:
        return await persona_validate.validate(_svc(request), request.app,
                                               body.get("virtual_name") or "")
    except KeyError:
        return JSONResponse({"error": "no such persona"}, status_code=404)


@router.get("/admin/api/personas/meta")
async def personas_meta(request: Request):
    """Per persona: what clients are told (effective context, max output,
    capabilities, served model, backend, validation status)."""
    from ..facade.ollama_api import persona_meta
    svc = _svc(request)
    return {"personas": {p["virtual_name"]: persona_meta(svc, p) for p in svc.personas.list()}}


@router.get("/admin/api/personas/kilo-config")
async def kilo_provider_config(request: Request):
    """A Kilo / OpenCode custom-provider block for Foundry's chat personas,
    with each persona's effective context/output limits and modalities (Kilo
    sizes compaction from these, not from /v1/models). Disabled personas are
    included, marked, so the block doesn't change when you enable one."""
    from ..facade.ollama_api import persona_meta
    from ..kilo_roles import display_name
    svc = _svc(request)
    cfg = svc.config_store.config
    base = request.query_params.get("base_url") or (
        f"http://{cfg.server.host if cfg.server.host not in ('0.0.0.0', '::', '') else 'FOUNDRY_HOST'}"
        f":{cfg.server.port}")
    prefix = request.query_params.get("prefix", "Foundry-Kilo")
    models = {}
    explain = {}
    for p in svc.personas.list():
        if (p.get("kind") or "chat") == "media" or not p["virtual_name"].startswith(prefix):
            continue
        m = persona_meta(svc, p)
        entry = {"name": display_name(p["virtual_name"])
                 + ("" if p.get("enabled") else " (disabled in Foundry)"),
                 "tool_call": "tools" in m["capabilities"],
                 "attachment": "vision" in m["capabilities"],
                 "reasoning": bool(p.get("reasoning_effort")),
                 "modalities": {"input": m["input_modalities"], "output": ["text"]}}
        limit = {k: v for k, v in (("context", m["context_length"]),
                                   ("output", m["max_output_tokens"])) if v}
        if limit:
            entry["limit"] = limit
        models[p["virtual_name"]] = entry
        src = m.get("limit_sources") or {}
        explain[p["virtual_name"]] = {
            "context": m["context_length"], "output": m["max_output_tokens"],
            "context_source": src.get("context") or ("persona declaration / largest routable "
                                                     "window (flexible routing)"),
            "declared_context": src.get("declared", p.get("context_window")),
            "model_windows": src.get("model_windows") or {},
            "unknown_windows": src.get("unknown_windows") or [],
            "unreachable_pins": src.get("unreachable_pins") or [],
            "enabled": bool(p.get("enabled")), "validation": m.get("validation") or "not validated"}
    block = {
        "provider": {"foundry": {
            "npm": "@ai-sdk/openai-compatible", "name": "Foundry Router",
            "options": {"baseURL": base.rstrip("/") + "/v1", "apiKey": "{env:FOUNDRY_KEY_KILO}"},
            "models": models}},
        # Only Foundry: Kilo's `task` tool otherwise falls back to the Kilo
        # Gateway (paid credits) when a model isn't found under the current
        # provider. And helpers may not start helpers of their own.
        "enabled_providers": ["foundry"],
        "subagent_depth": 1}
    if request.query_params.get("explain") in ("1", "true"):
        # Kilo's config schema rejects unknown keys, so the explanation is a
        # separate envelope, never part of the block itself.
        return {"config": block, "limits": explain,
                "policy": "Strict personas: context = the smallest of the persona's declared "
                          "context_window and the known window of every model in its ordered "
                          "chain (a fallback pin can serve any request); output = the persona's "
                          "max_output_tokens capped by that. Models with no known window don't "
                          "lower it. An already-saved kilo.jsonc is not updated by changing a "
                          "persona: regenerate and merge it."}
    return block


@router.get("/admin/api/profiles")
async def list_persona_profiles(request: Request):
    """Default persona sets and what applying each would do right now."""
    from .. import profiles
    return {"profiles": profiles.list_profiles(_svc(request))}


@router.post("/admin/api/profiles/{profile_id}/apply")
async def apply_persona_profile(request: Request, profile_id: str):
    from .. import profiles
    try:
        return profiles.apply(_svc(request), profile_id)
    except KeyError:
        return JSONResponse({"error": f"unknown profile {profile_id!r}"}, status_code=404)


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
    # Preserve the on/off state across edits: the add/edit form doesn't carry it
    # (that's the toggle's job), so an unspecified `enabled` keeps the current
    # value rather than silently re-enabling a disabled server.
    existing = next((s for s in svc.config_store.config.mcp_servers
                     if s.name == body["name"]), None)
    enabled = body.get("enabled")
    if enabled is None:
        enabled = existing.enabled if existing else True
    entry = {"name": body["name"], "url": body["url"],
             "transport": body.get("transport", "streamable-http"),
             "enabled": bool(enabled),
             "timeout_seconds": int(body.get("timeout_seconds") or 300),
             # Pacing / 429 handling — persist so a UI edit doesn't silently
             # drop a throttle set in config.yaml (SearXNG needs a gap here).
             "pace_seconds": float(body.get("pace_seconds") or 0.0),
             "rate_limit_retries": int(body.get("rate_limit_retries") or 3),
             "rate_limit_backoff_seconds": float(
                 body.get("rate_limit_backoff_seconds") or 30.0),
             # Code-execution flag + force-merged call defaults (code-sandbox
             # spec). Both operator-controlled; default to off/empty so a
             # normal server is unchanged.
             "executes_code": bool(body.get("executes_code")),
             "call_defaults": body.get("call_defaults")
                              if isinstance(body.get("call_defaults"), dict) else {},
             # Long-running generation (tool_media.py): chat calls become tool
             # jobs. Kept when an older UI posts without the field.
             "long_running": bool(body["long_running"]) if "long_running" in body
                             else bool(getattr(existing, "long_running", False))}
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


@router.post("/admin/api/mcp_servers/toggle")
async def toggle_mcp(request: Request):
    """Flip a server's on/off switch and re-sync. Disabling drops its tools from
    the registry immediately (workers, brain, and the Foundry-MCP aggregator
    stop seeing them); enabling rediscovers them. The connection is untouched."""
    svc = _svc(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    enabled = bool(body.get("enabled"))

    def mutate(raw):
        for s in raw.get("mcp_servers") or []:
            if s.get("name") == name:
                s["enabled"] = enabled

    cfg = svc.config_store.save(mutate)
    svc.mcp.set_servers(cfg.mcp_servers)
    result = await svc.tool_registry.sync(svc.pool)   # add/drop its tools now
    return {"ok": True, "name": name, "enabled": enabled, "tool_sync": result}


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
# External agents (Hermes Agent) — as tools and as persona backends           #
# --------------------------------------------------------------------------- #

def _agent_public(svc, a) -> dict:
    d = a.model_dump()
    d["api_key"] = bool(a.api_key)          # never echo secrets back
    d["caller_token"] = bool(a.caller_token)
    d["health"] = svc.agents.health(a.name)
    d["server"] = "agent-" + a.name
    d["personas"] = [p["virtual_name"] for p in svc.personas.list()
                     if (p.get("agent_backend") or "") == a.name]
    return d


@router.get("/admin/api/agents")
async def list_agents(request: Request):
    svc = _svc(request)
    return {"agents": [_agent_public(svc, a) for a in svc.config_store.config.agents],
            "active": svc.agents.active()}


@router.post("/admin/api/agents")
async def upsert_agent(request: Request):
    """Add / edit an agent. Empty api_key / caller_token keep the stored value;
    send "-" to clear one."""
    import re
    svc = _svc(request)
    b = await request.json()
    name = (b.get("name") or "").strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,40}", name):
        return JSONResponse({"error": "name required (letters, digits, - _ .)"},
                            status_code=400)
    existing = next((a for a in svc.config_store.config.agents if a.name == name), None)

    def secret(key):
        v = (b.get(key) or "").strip()
        if v == "-":
            return None
        return v or (getattr(existing, key) if existing else None)
    entry = {"name": name, "kind": "hermes",
             "url": (b.get("url") or "http://localhost:8642").strip(),
             "api_key": secret("api_key"), "caller_token": secret("caller_token"),
             "enabled": bool(b.get("enabled", existing.enabled if existing else True)),
             "model": (b.get("model") or "").strip(),
             "timeout_seconds": max(30, int(b.get("timeout_seconds") or 1800)),
             "tool_wait_seconds": max(5, int(b.get("tool_wait_seconds") or 600)),
             "expose_as_tool": bool(b.get("expose_as_tool", True)),
             "session_continuity": bool(b.get("session_continuity", True))}

    def mutate(raw):
        lst = raw.setdefault("agents", []) or []
        lst[:] = [x for x in lst if x.get("name") != name] + [entry]
        raw["agents"] = lst
    cfg = svc.config_store.save(mutate)
    svc.agents.set_agents(cfg.agents)
    health = await svc.agents.probe(name)
    result = await svc.tool_registry.sync(svc.pool)       # add/drop its tools now
    return {"ok": True, "health": health, "tool_sync": result}


@router.post("/admin/api/agents/delete")
async def delete_agent(request: Request):
    svc = _svc(request)
    name = ((await request.json()).get("name") or "").strip()

    def mutate(raw):
        raw["agents"] = [x for x in (raw.get("agents") or []) if x.get("name") != name]
    cfg = svc.config_store.save(mutate)
    svc.agents.set_agents(cfg.agents)
    await svc.tool_registry.sync(svc.pool)
    return {"ok": True}


@router.post("/admin/api/agents/probe")
async def probe_agent(request: Request):
    svc = _svc(request)
    name = ((await request.json()).get("name") or "").strip()
    if name:
        return {"agents": [await svc.agents.probe(name)]}
    return {"agents": await svc.agents.probe_all()}


@router.get("/admin/api/agent-runs")
async def agent_runs(request: Request, hours: float = 24, limit: int = 50):
    svc = _svc(request)
    return {**svc.agents.summary(hours), "recent": svc.agents.recent(limit)}


@router.post("/admin/api/agent-runs/clear")
async def clear_agent_runs(request: Request):
    svc = _svc(request)
    return {"ok": True, "deleted": svc.agents.clear()}


# --------------------------------------------------------------------------- #
# Docker MCP Gateway admin (gateway-server admin spec) — operator-only,       #
# backend-initiated MCP calls; NEVER routed through a persona/model tool-loop #
# --------------------------------------------------------------------------- #

def _gateway_or_400(svc, body: dict):
    """Resolve the target gateway connection (explicit, or the sole detected
    one). Returns (name, None) or (None, error-response)."""
    gws = svc.tool_registry.gateway_servers()
    gw = body.get("gateway") or (gws[0] if len(gws) == 1 else None)
    if not gw:
        return None, JSONResponse(
            {"error": "no MCP gateway connection detected — a connection must "
                      "expose the Docker MCP Gateway management tools "
                      "(mcp-find/mcp-add)" if not gws else
                      "multiple gateways detected; specify 'gateway'"},
            status_code=404 if not gws else 400)
    return gw, None


@router.get("/admin/api/gateway/servers")
async def gateway_servers(request: Request):
    """Detected gateway connection(s) + a Tool-Sync-derived view of what's
    currently attached to each. No gateway call — pure local registry read."""
    from ..gateway_admin import attached_view, inspect_settings
    svc = _svc(request)
    return {"gateways": svc.tool_registry.gateway_servers(),
            "attached": attached_view(svc),
            "inspect": inspect_settings(svc.db)}


@router.post("/admin/api/gateway/inspect_config")
async def gateway_inspect_config(request: Request):
    """Set the inspect companion service URL (+ optional bearer token — blank
    keeps the existing token). Empty url clears it."""
    from ..gateway_admin import inspect_settings, set_inspect_settings
    svc = _svc(request)
    body = await request.json()
    set_inspect_settings(svc.db, str(body.get("url") or ""),
                         token=body.get("token") if "token" in body else None)
    return {"ok": True, **inspect_settings(svc.db)}


@router.post("/admin/api/gateway/config_set")
async def gateway_config_set(request: Request):
    """Set non-secret config for a catalog server via the gateway's
    mcp-config-set tool (operator-only admin call). Values come from the
    config-schema form the UI generated. Returns the raw gateway response."""
    import asyncio

    from ..errors import describe_exception
    from ..gateway_admin import config_set
    svc = _svc(request)
    body = await request.json()
    server = str(body.get("server") or "").strip()
    values = body.get("values")
    if not server or not isinstance(values, dict) or not values:
        return JSONResponse({"error": "server and non-empty values required"},
                            status_code=400)
    gw, err = _gateway_or_400(svc, body)
    if err:
        return err
    try:
        return await asyncio.wait_for(config_set(svc, gw, server, values), timeout=60)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "mcp-config-set timed out"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": describe_exception(e)}, status_code=502)


@router.post("/admin/api/gateway/inspect")
async def gateway_inspect(request: Request):
    """Fetch richer per-server detail via the inspect companion service (host
    CLI `docker mcp catalog server inspect`, exposed by contrib/
    gateway-admin-service). 400 when the service isn't configured."""
    import asyncio

    from ..errors import describe_exception
    from ..gateway_admin import inspect_server
    svc = _svc(request)
    body = await request.json()
    server = str(body.get("server") or "").strip()
    if not server:
        return JSONResponse({"error": "server required"}, status_code=400)
    try:
        out = await asyncio.wait_for(
            inspect_server(svc, server, body.get("catalog") or None), timeout=40)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "inspect timed out"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": describe_exception(e)}, status_code=502)
    if not out.get("configured"):
        return JSONResponse(
            {"error": "inspect companion service not configured — set its URL in "
                      "MCP > Gateway Servers (see docs/GATEWAY_SERVERS.md)"},
            status_code=400)
    return out


@router.post("/admin/api/gateway/find")
async def gateway_find(request: Request):
    """Search the Docker MCP Catalog via the gateway's mcp-find tool."""
    import asyncio

    from ..errors import describe_exception
    from ..gateway_admin import find_servers
    svc = _svc(request)
    body = await request.json()
    gw, err = _gateway_or_400(svc, body)
    if err:
        return err
    try:
        return await asyncio.wait_for(
            find_servers(svc, gw, str(body.get("query") or "")), timeout=45)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "mcp-find timed out after 45s"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": describe_exception(e)}, status_code=502)


@router.post("/admin/api/gateway/add")
async def gateway_add(request: Request):
    """Attach a secret-free catalog server (mcp-add), then re-sync so the caller
    can confirm the new tools appeared."""
    import asyncio

    from ..errors import describe_exception
    from ..gateway_admin import add_server
    svc = _svc(request)
    body = await request.json()
    ref = str(body.get("ref") or "").strip()
    if not ref:
        return JSONResponse({"error": "ref required"}, status_code=400)
    gw, err = _gateway_or_400(svc, body)
    if err:
        return err
    try:
        return await asyncio.wait_for(add_server(svc, gw, ref), timeout=120)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "mcp-add timed out after 120s"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": describe_exception(e)}, status_code=502)


@router.post("/admin/api/gateway/remove")
async def gateway_remove(request: Request):
    """Detach a catalog server (mcp-remove), then re-sync. Disruptive — any
    persona granted tools from that server loses them; the UI confirms first."""
    import asyncio

    from ..errors import describe_exception
    from ..gateway_admin import remove_server
    svc = _svc(request)
    body = await request.json()
    ref = str(body.get("ref") or "").strip()
    if not ref:
        return JSONResponse({"error": "ref required"}, status_code=400)
    gw, err = _gateway_or_400(svc, body)
    if err:
        return err
    try:
        return await asyncio.wait_for(remove_server(svc, gw, ref), timeout=120)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "mcp-remove timed out after 120s"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": describe_exception(e)}, status_code=502)


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


@router.get("/admin/api/tool_calls")
async def tool_calls(request: Request, limit: int = 100, executed_code: int = 0):
    """Durable per-event tool-call audit (tool_call_log). With executed_code=1,
    only code-execution calls — each carrying the actual submitted code — the
    strongest audit trail in the system, per the code-sandbox spec."""
    svc = _svc(request)
    where = "WHERE executed_code=1" if executed_code else ""
    rows = svc.db.query(
        f"SELECT * FROM tool_call_log {where} ORDER BY id DESC LIMIT ?",
        (min(limit, 500),))
    return {"tool_calls": rows}


@router.get("/admin/api/events")
async def event_log(request: Request, limit: int = 200):
    svc = _svc(request)
    return {"events": svc.db.query(
        "SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (min(limit, 2000),))}


_LOGO_KEY = "ui_logo"


_RESEARCH_NUM = ("sweep_hours", "stale_days", "max_pages_per_model",
                 "corpus_char_limit", "context_window", "page_char_cap",
                 "search_snippet_chars")


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
        if "model" in b:
            # Dedicated research/extraction model (empty => reuse the routing
            # brain). A bad pick can't stall the sweep — _research_llm falls
            # back to the brain on failure.
            r["model"] = (str(b["model"]).strip() or None)
        for k in _RESEARCH_NUM:
            if b.get(k) is not None:
                r[k] = int(b[k])
        if "extra_queries" in b:
            # Operator-added search templates (one per line in the UI). Keep only
            # non-blank strings; bound the count so a paste can't balloon a sweep.
            raw_q = b["extra_queries"]
            if isinstance(raw_q, str):
                raw_q = raw_q.splitlines()
            r["extra_queries"] = [str(q).strip() for q in (raw_q or [])
                                  if str(q).strip()][:20]
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
    """Ollama-type backends only — the targets these operations can act on.
    Each carries the server's /api/version (diagnostic; empty if unreachable)."""
    admin = _svc(request).ollama_admin
    backends = admin.backends()
    for b in backends:
        b["version"] = await admin.version(b["name"])
    return {"backends": backends, "jobs": admin.job_snapshot()}


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


# --------------------------------------------------------------------------- #
# Unified Host Admin (backend-type-aware): Ollama CRUD + openai-dialect       #
# read-only diagnostics (llama.cpp / Unsloth / vLLM / OpenAI) behind one tab. #
# --------------------------------------------------------------------------- #

@router.get("/admin/api/host/backends")
async def host_backends(request: Request):
    """Every backend that has an admin surface, each tagged with its flavor so the
    Host Admin panel picks the right controls: Ollama flavors get full model CRUD
    (via the /admin/api/ollama/* routes); openai-dialect flavors (llamacpp /
    unsloth / vllm / openai) get read-only diagnostics (/admin/api/host/diag)."""
    svc = _svc(request)
    out = []
    for b in svc.ollama_admin.backends():
        out.append({"name": b["name"], "url": b["url"], "flavor": "ollama",
                    "healthy": b["healthy"], "crud": True,
                    "version": await svc.ollama_admin.version(b["name"])})
    for b in svc.openai_admin.backends():
        version = ""
        st = getattr(svc.pool, "backends", {}).get(b["name"])
        probe = getattr(getattr(st, "protocol", None), "server_version", None)
        if probe and b["healthy"]:
            try:
                version = await probe()
            except Exception:
                version = ""
        out.append({"name": b["name"], "url": b["url"], "flavor": b["flavor"],
                    "healthy": b["healthy"], "crud": False, "version": version})
    for s in getattr(svc.pool, "backends", {}).values():
        if s.config.type == "anthropic-compatible":
            out.append({"name": s.config.name, "url": s.config.url, "flavor": "meridian",
                        "healthy": getattr(s, "healthy", False), "crud": False, "version": ""})
    out.sort(key=lambda b: b["name"])
    return {"backends": out, "jobs": svc.ollama_admin.job_snapshot()}


# --------------------------------------------------------------------------- #
# Foundry Client tab: downloads, connected clients, install guide (v0.139.0). #
# Same posture as the rest of the dashboard: open on the internal network,    #
# covered by client_auth.protect_admin when on. Downloads are links to the    #
# public GitHub release matching the running version — the router serves no   #
# binaries and holds no GitHub credentials (client_guide.py docstring).       #
# --------------------------------------------------------------------------- #

@router.get("/admin/api/client/install")
async def client_install(request: Request, refresh: int = 0):
    """The running router version, whether its installers are downloadable
    (release state: available / no_assets / untagged / unreachable, with the
    latest release as a labeled fallback), and every heart-beating Foundry
    Client with its version compared against the router's."""
    from ..client_guide import install_status
    return await install_status(_svc(request), __version__, refresh=bool(refresh))


@router.get("/admin/api/client/guide")
async def client_install_guide(request: Request):
    """The install guide rendered from docs/CLIENT_MANAGER.md (one source of
    truth; client_guide.py extracts and renders the install sections)."""
    from ..client_guide import guide_html
    return guide_html()


@router.get("/admin/api/host/diag")
async def host_diag(request: Request, backend: str):
    """Read-only diagnostics for an openai-dialect backend (llama.cpp / Unsloth /
    vLLM / OpenAI). Each probe is individually guarded, so a partial report comes
    back even when some endpoints are unimplemented or the host is half-up."""
    from ..errors import describe_exception
    svc = _svc(request)
    st = getattr(svc.pool, "backends", {}).get(backend)
    if st is not None and st.config.type == "anthropic-compatible":
        # Meridian: its own /health + /metrics view (the Live tab's), plus models.
        try:
            m = await st.protocol.server_metrics() or {}
        except Exception as e:                                     # noqa: BLE001
            m = {"metrics_ok": False, "metrics_error": describe_exception(e)}
        return {"ok": True, "report": {
            "backend": backend, "flavor": "meridian", "url": st.config.url,
            "models": {"ok": True, "models": sorted(st.models)},
            "metrics": {"ok": bool(m.get("metrics_ok")), "summary": m,
                        "error": m.get("metrics_error") or ""}}}
    try:
        return {"ok": True, "report": await svc.openai_admin.diagnostics(backend)}
    except Exception as e:
        return {"ok": False, "error": describe_exception(e)}
