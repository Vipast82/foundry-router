"""Ollama-compatible API facade (design doc §4.1).

Implements the subset of Ollama's API clients actually use:
  POST /api/chat        — primary endpoint, streaming + non-streaming
  GET  /api/tags        — advertises enabled personas as installed models
  POST /api/generate    — legacy completion endpoint
  GET  /  /api/version  — connect-time health pings
  POST /api/show, GET /api/ps — stubs some clients call

Pure translation: Ollama request in -> Agent Brain events out -> Ollama-format
stream back. Routing decisions all live behind AgentRunner.

A request is served in one of four modes:
  agent       persona selected, no client-side tools -> full routing agent
  direct      persona selected, client sent its own `tools` (Kilo/Cline) ->
              one model is chosen by static policy and the tools are forwarded
              verbatim, because the routing agent can't hold two tool-calling
              conversations in one (DESIGN DECISION, see note below)
  passthrough model name matches a raw backend model -> forwarded untouched
  fallback    brain unreachable mid-agent-mode -> static rule (§4.2)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .. import __version__
from .. import context_guard, keepalive, request_context, telemetry
from ..brain import prompts
from ..brain.agent import RequestContext
from ..brain.fallback import guess_category, pick_fallback_model
from ..brain.user_intent import parse_confirmation
from ..guardrails import RequestGuardState
from ..pool.base import AllBackendsFailed
from ..pool.protocols import ChatResult
from ..usage import (RequestLogger, estimate_cost_usd,
                     log_subscription_usage, looks_like_window_exhaustion)
from . import translate as tr

log = logging.getLogger(__name__)

router = APIRouter()


def _svc(request: Request):
    return request.app.state.services


def _canonical_messages(raw: list[dict]) -> list[dict]:
    out = []
    for m in raw or []:
        role = m.get("role") or "user"
        if role not in ("system", "user", "assistant", "tool"):
            role = "user"
        if role == "assistant":
            # Foundry's own status lines (keep-alive, routing notes) come back
            # as the turn's thinking / text — never feed them to a model, it
            # imitates them (fake "still working" clocks as its reasoning).
            m = dict(m)
            if m.get("thinking"):
                m["thinking"] = prompts.scrub_router_lines(m["thinking"])
            if isinstance(m.get("content"), str) and m["content"]:
                m["content"] = prompts.scrub_router_lines(m["content"])
        out.append({"role": role, "content": m.get("content") or "",
                    # Ollama multimodal convention: images: ["<base64>", ...].
                    # This function is the universal entry point — dropping the
                    # field here silently blinded the whole app (found live).
                    **({"images": m["images"]} if m.get("images") else {}),
                    **({"tool_calls": m["tool_calls"]} if m.get("tool_calls") else {}),
                    **({"thinking": m["thinking"]}
                       if role == "assistant" and m.get("thinking") else {}),
                    **({"tool_call_id": m["tool_call_id"]} if m.get("tool_call_id") else {}),
                    # Ollama names the tool a result belongs to with `tool_name`
                    # (canonical `name`, which the Ollama adapter maps back).
                    **({"name": m.get("tool_name") or m.get("name")}
                       if role == "tool" and (m.get("tool_name") or m.get("name")) else {})})
    # Pair tool calls with their results (stable ids) once, at the door, so
    # every backend format sees a consistent history.
    from ..pool.protocols import pair_tool_call_ids
    return pair_tool_call_ids(out)


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m["role"] == "user":
            return m["content"]
    return ""


def _model_not_found(name: str) -> JSONResponse:
    return JSONResponse({"error": f"model '{name}' not found"}, status_code=404)


# --------------------------------------------------------------------------- #
# Health / discovery endpoints                                                #
# --------------------------------------------------------------------------- #

@router.api_route("/", methods=["GET", "HEAD"])
async def root() -> PlainTextResponse:
    # Byte-for-byte what a real Ollama answers — several clients string-match it.
    return PlainTextResponse("Ollama is running")


@router.get("/api/version")
async def version() -> dict:
    # Clients gate features on Ollama's version number; we advertise one whose
    # API surface we match (do NOT bump this to our version). Foundry's own
    # version rides alongside in a separate field.
    return {"version": "0.9.0", "foundry_router": __version__}


@router.get("/api/tags")
async def tags(request: Request) -> dict:
    svc = _svc(request)
    # DESIGN DECISION (see design doc §7): /api/tags exposes only the virtual
    # persona names. Raw backend model names are still ACCEPTED by /api/chat
    # (passthrough mode) for anyone who wants to bypass routing — they're just
    # not advertised, keeping client dropdowns policy-only.
    return {"models": [tr.persona_tag_entry(p) for p in svc.personas.list(enabled_only=True)]}


@router.get("/api/ps")
async def ps(request: Request) -> dict:
    """Models resident on the backends right now, in Ollama's /api/ps shape
    (name/model/size/size_vram/expires_at/context_length/details). These are
    the RAW backend models — the thing actually occupying VRAM — so clients
    that show "running models" (Open WebUI) see the real fleet state. Backends
    that report no VRAM bytes (llama.cpp / vLLM) show size_vram 0."""
    svc = _svc(request)
    try:
        detail = await svc.pool.loaded_models_detail()
    except Exception:
        detail = []
    out = []
    for d in detail:
        name = d.get("model")
        if not name:
            continue
        out.append({"name": name, "model": name,
                    "size": int(d.get("size") or d.get("size_vram") or 0),
                    "digest": d.get("digest") or "",
                    "details": d.get("details") or {},
                    "expires_at": d.get("expires_at") or "",
                    "size_vram": int(d.get("size_vram") or 0),
                    "context_length": int(d.get("context") or 0),
                    "backend": d.get("backend") or ""})
    return {"models": out}


def _embed_inputs(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [x if isinstance(x, str) else json.dumps(x) for x in v]
    return []


async def _do_embed(svc, body: dict, inputs: list[str]):
    """Shared by /api/embed and /api/embeddings: route to the backend serving
    the (raw) embedding model with the same failover as chat. Personas are
    chat policies, not embedders, so only real backend model names resolve."""
    from ..usage import RequestLogger
    model = body.get("model") or ""
    if svc.pool.backend_info(model) is None:
        return None, _model_not_found(model)
    logger = RequestLogger(svc.db, "", model, "embed",
                           (inputs[0] if inputs else "")[:200])
    t0 = time.monotonic_ns()
    try:
        res, backend = await svc.pool.embed(
            model, inputs, options=body.get("options") or None,
            keep_alive=body.get("keep_alive"), truncate=body.get("truncate"),
            dimensions=body.get("dimensions"))
    except AllBackendsFailed as e:
        logger.finish("error", str(e))
        return None, JSONResponse({"error": str(e)}, status_code=502)
    logger.record_model_call(model, backend, res.get("prompt_eval_count") or 0, 0, 0.0)
    logger.finish("ok")
    res["total_duration"] = res.get("total_duration") or (time.monotonic_ns() - t0)
    return res, None


@router.post("/api/embed")
async def embed(request: Request):
    """Ollama /api/embed — `input` is a string or a list; returns
    {"model", "embeddings", "total_duration", "load_duration", "prompt_eval_count"}.
    Works against Ollama, llama.cpp (--embeddings) and vLLM embedding models."""
    svc = _svc(request)
    body = await request.json()
    res, err = await _do_embed(svc, body, _embed_inputs(body.get("input")))
    if err is not None:
        return err
    return {"model": body.get("model"), "embeddings": res["embeddings"],
            "total_duration": res["total_duration"],
            "load_duration": res.get("load_duration") or 0,
            "prompt_eval_count": res.get("prompt_eval_count") or 0}


@router.post("/api/embeddings")
async def embeddings_legacy(request: Request):
    """Ollama's legacy single-prompt endpoint: {"prompt"} -> {"embedding"}."""
    svc = _svc(request)
    body = await request.json()
    res, err = await _do_embed(svc, body, _embed_inputs(body.get("prompt") or ""))
    if err is not None:
        return err
    embs = res["embeddings"]
    return {"embedding": embs[0] if embs else []}


def _persona_context_length(svc, persona: dict):
    """Report the context length for a virtual persona.

    Priority order (highest wins):
      1. ``context_window`` override on the persona (admin-set in the web UI,
         pins a fixed value regardless of backend discovery) — lets operators
         tell clients
         "this persona can absorb up to N tokens" even when workers vary in
         size (found live: a Foundry persona reported 2048 because one tiny
         fallback worker had that window; the real workers held 32K-128K).
      2. The **maximum** known context_length among routable candidates —
         AnythingLLM / Open WebUI size their token budget from /api/show, so
         reporting the largest available window lets long-context work (RAG,
         document analysis) actually use it instead of being bottlenecked by
         the smallest worker that happens to be healthy.

    Why MAX not MIN: MIN was a "safe floor" in v1 but it punished operators
    running heterogeneous workers — a single small model capped every persona's
    reported budget even though every larger worker could handle more. Foundry's
    job is to *rout* to the best available worker, so clients should know the
    ceiling they can reach, not the floor of the weakest backend.
    """
    # 1) Persona-level override (admin-set via the web UI). A 0/blank/negative
    #    value means "auto" and falls through to backend discovery.
    cw = persona.get("context_window")
    if cw:
        try:
            v = int(cw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass

    # 2) Max context across routable candidates.
    category = persona.get("benchmark_category") or "general_chat"
    available = list(svc.pool.available_models().keys())
    ranked = svc.registry.ranked_for_category(category, available, limit=50, per_tier=50)
    lengths = [int(r["context_length"]) for r in ranked if r.get("context_length")]
    return max(lengths) if lengths else None


# Capabilities beyond the always-on baseline that clients gate features on, and
# the order they're advertised in.
_CAP_ORDER = ["completion", "chat", "tools", "vision", "thinking", "insert"]
_EXTRA_CAPS = ("vision", "thinking", "insert")


def _persona_capabilities(svc, persona: dict) -> list[str]:
    """Capabilities to advertise on /api/show: the baseline (completion/chat/
    tools) plus any of vision/thinking/insert that a REACHABLE model declares —
    honoring the persona's model_allowlist. Foundry steers each request to a
    capable worker (e.g. images -> a vision model), so the persona exposes the
    union its fleet can reach. Vision is also honored from the legacy 'vision'
    tag for models tagged before capability auto-probing existed."""
    import json as _json

    def _jl(v):
        try:
            out = _json.loads(v or "[]")
            return out if isinstance(out, list) else []
        except (_json.JSONDecodeError, TypeError):
            return []

    caps = {"completion", "chat", "tools"}
    allow = set(_jl(persona.get("model_allowlist")))
    allow_bases = {str(a).split(":")[0] for a in allow}
    for mid in svc.pool.available_models():
        if allow and not (mid in allow or str(mid).split(":")[0] in allow_bases):
            continue
        meta = svc.registry.get(mid) or {}
        mcaps = _jl(meta.get("capabilities"))
        caps.update(c for c in _EXTRA_CAPS if c in mcaps)
        if "vision" in _jl(meta.get("tags")):        # legacy / manual tag
            caps.add("vision")
    return [c for c in _CAP_ORDER if c in caps]


async def _raw_show(svc, name: str):
    """/api/show for a RAW backend model (passthrough by name): an Ollama
    backend's own /api/show is proxied verbatim; for llama.cpp / vLLM /
    Claude a minimal Ollama-shaped answer is built from the registry (context
    length + capabilities), so clients sizing their token budget still work."""
    info = svc.pool.backend_info(name)
    if info is None:
        return None
    if info.get("type") == "ollama":
        try:
            r = await svc.http.post(f"{info['url'].rstrip('/')}/api/show",
                                    json={"model": name}, timeout=15)
            if r.status_code < 400:
                return r.json()
        except Exception:
            pass
    meta = svc.registry.get(name) or {}
    try:
        caps = json.loads(meta.get("capabilities") or "[]") or []
    except (TypeError, ValueError):
        caps = []
    arch = info.get("flavor") or info.get("type") or "remote"
    model_info: dict = {"general.architecture": arch}
    if meta.get("context_length"):
        model_info["general.context_length"] = int(meta["context_length"])
        model_info[f"{arch}.context_length"] = int(meta["context_length"])
    return {"modelfile": f"# served by backend {info.get('name')} ({arch})\n",
            "parameters": "", "template": "{{ .Prompt }}",
            "details": {"parent_model": "", "format": "", "family": arch,
                        "families": [arch], "parameter_size": "",
                        "quantization_level": ""},
            "model_info": model_info,
            "capabilities": sorted(set(caps) | {"completion", "tools"})}


@router.post("/api/show")
async def show(request: Request) -> JSONResponse:
    svc = _svc(request)
    body = await request.json()
    name = body.get("model") or body.get("name") or ""
    persona = svc.personas.get(name)
    if persona is None:
        raw = await _raw_show(svc, name)
        return JSONResponse(raw) if raw is not None else _model_not_found(name)
    return JSONResponse(tr.show_response(
        persona, context_length=_persona_context_length(svc, persona),
        capabilities=_persona_capabilities(svc, persona)))


# --------------------------------------------------------------------------- #
# /api/chat                                                                   #
# --------------------------------------------------------------------------- #

@router.post("/api/chat")
async def chat(request: Request):
    svc = _svc(request)
    from .. import request_context
    request_context.capture(request.headers)
    return await _chat_dispatch(svc, await request.json())


async def _chat_dispatch(svc, body: dict):
    """/api/chat semantics for an already-parsed body — shared with
    /api/generate, which adapts its prompt into a chat so it gets the exact
    same routing, telemetry and response stats."""
    model_name = body.get("model") or ""
    stream = body.get("stream", True)
    client_tools = body.get("tools") or None
    messages = _canonical_messages(body.get("messages") or [])
    options = body.get("options") or None
    user_text = _last_user_text(messages)
    # Ollama structured output: "json" or a JSON schema object.
    client_format = body.get("format") or None
    # Client-set reasoning effort (Q2 passthrough): Ollama-native top-level
    # `think`, or an OpenAI-style `reasoning_effort` (top-level or in options).
    # Highest precedence when resolving the worker's think level. Logged so the
    # operator can see exactly what a client like Cline actually sends.
    client_think = body.get("think")
    if client_think is None:
        client_think = body.get("reasoning_effort") or (options or {}).get("reasoning_effort")
    if client_think is not None:
        svc.db.log_event("info", "facade",
                         f"client set reasoning effort: think={client_think!r} "
                         f"model={model_name}")

    persona = svc.personas.get(model_name)

    if persona is None:
        if svc.pool.backend_info(model_name) is not None:
            return await _passthrough_chat(svc, model_name, messages, client_tools,
                                           options, stream, user_text,
                                           think=client_think, fmt=client_format,
                                           keep_alive=body.get("keep_alive"))
        return _model_not_found(model_name)

    # AGENT-BACKED persona: an external agent (Hermes) answers the whole turn.
    agent_name = (persona.get("agent_backend") or "").strip()
    if agent_name and getattr(svc, "agents", None) is not None:
        refusal = _agent_backend_refusal(svc, agent_name)
        if refusal is None:
            return await _agent_backend_chat(svc, persona, agent_name, model_name,
                                             messages, stream, user_text, client_tools)
        # Loop protection / agent down: serve the persona with its normal model
        # policy instead, and say why in the event log.
        svc.db.log_event("warning", "agents",
                         f"persona {persona['virtual_name']}: agent {agent_name!r} "
                         f"not used — {refusal}; routing to a model instead")

    exec_mode = persona.get("execution_mode") or "agent"
    # `direct` = thin proxy: pick ONE model per the persona's static policy and
    # forward the client's request verbatim. Triggered by client-supplied tools
    # (Kilo/Cline agent loops) OR by an explicit `direct` execution_mode — the
    # latter is essential for agentic clients like Cline that DON'T attach a
    # `tools` field on every turn (a plan-mode / no-tools turn would otherwise
    # fall through to the brain loop and leak its internal ask_<model> delegation
    # calls into the client, which Cline can't parse).
    # PASSTHROUGH routing: the brain is off (routing_mode=passthrough), not
    # configured, or known-down (auto mode). Don't wait on it — a persona
    # without MCP tools is served by direct dispatch (static policy pick +
    # guardrails + failover, request forwarded as-is); one WITH MCP tools
    # still runs the worker-owned tool loop, which never needed the brain.
    brain_skip = svc.brain.skip_reason() if hasattr(svc.brain, "skip_reason") else None
    if brain_skip and not client_tools and exec_mode == "agent" \
            and not _persona_has_mcp_tools(persona):
        return await _direct_dispatch_chat(svc, persona, model_name, messages,
                                           client_tools, options, stream, user_text,
                                           client_think=client_think,
                                           client_format=client_format,
                                           log_mode="passthrough", note=brain_skip,
                                           client_keep_alive=body.get("keep_alive"))

    if client_tools or exec_mode == "direct":
        return await _direct_dispatch_chat(svc, persona, model_name, messages,
                                           client_tools, options, stream, user_text,
                                           client_think=client_think,
                                           client_format=client_format,
                                           client_keep_alive=body.get("keep_alive"))

    # Pipeline personas (Foundry-Coding) run the Prepare->Execute->Check
    # mode instead of the generic brain loop — a distinct execution mode,
    # like direct-dispatch, bookended by the paid steps.
    if exec_mode == "pipeline":
        return await _agent_chat(svc, persona, model_name, messages, stream,
                                 user_text, mode="pipeline")

    return await _agent_chat(svc, persona, model_name, messages, stream, user_text)


# ---- agent-backed personas (Hermes) ---------------------------------------------

def _agent_backend_refusal(svc, agent_name: str):
    """Why an agent-backed persona can't use its agent right now, or None."""
    caller = request_context.agent_caller()
    if caller:
        return (f"request came from agent {caller!r} (loop protection — an agent "
                f"can't be served by an agent)")
    if svc.agents.get(agent_name) is None:
        return "agent is not configured or disabled"
    if svc.agents.healthy(agent_name) is False:
        return "agent is unreachable (health check failed)"
    return None


async def _agent_backend_chat(svc, persona, agent_name, model_name, messages, stream,
                              user_text, client_tools=None):
    """Forward the conversation to an external agent and relay its answer:
    content streams as content, the agent's tool activity as thinking lines,
    and one Hermes session per client conversation so it keeps its memory.
    Client tools are not forwarded — the agent runs its own tools."""
    agents = svc.agents
    logger = RequestLogger(svc.db, persona["virtual_name"], model_name, "agent", user_text)
    if client_tools:
        svc.db.log_event("info", "agents",
                         f"persona {persona['virtual_name']}: {len(client_tools)} client "
                         f"tool(s) not forwarded — agent {agent_name} uses its own tools")
    session_key = ""
    a = agents.get(agent_name)
    if a is not None and a.session_continuity:
        from ..agents import conversation_key
        hdrs = request_context.client_headers()
        client_session = next((hdrs[k] for k in request_context.SESSION_HEADERS
                               if hdrs.get(k)), "")
        session_key = persona["virtual_name"] + ":" + conversation_key(messages, client_session)
    hb = float(getattr(svc.config_store.config.agent_brain, "heartbeat_seconds", 25) or 25)
    t0 = time.monotonic_ns()
    backend_label = f"agent:{agent_name}"

    def _stats(done: dict) -> dict:
        return {"prompt_tokens": done.get("prompt_tokens") or 0,
                "completion_tokens": done.get("completion_tokens") or 0,
                "cached_tokens": done.get("cached_tokens") or 0,
                "total_duration_ns": time.monotonic_ns() - t0,
                "done_reason": done.get("finish_reason") or "stop",
                "timing_source": "wall",
                "served_by": done.get("agent_model") or agent_name,
                "backend": backend_label, "agent": agent_name,
                "agent_tools": len(done.get("tools") or []),
                "agent_session": done.get("session_id") or ""}

    def _finish_log(done: dict, status: str, error: str = "") -> None:
        logger.record_model_call(done.get("agent_model") or agent_name, backend_label,
                                 done.get("prompt_tokens") or 0,
                                 done.get("completion_tokens") or 0, 0.0)
        logger.finish(status, error)

    stream_iter = agents.chat_stream(agent_name, messages, session_key=session_key,
                                     caller=persona["virtual_name"])
    if not stream:
        content, thinking, done = [], [], {}
        try:
            async for ev in stream_iter:
                if ev.get("done"):
                    done = ev
                elif ev.get("content"):
                    content.append(ev["content"])
                elif ev.get("thinking"):
                    thinking.append(ev["thinking"])
        except Exception as e:                                     # noqa: BLE001
            _finish_log(done, "error", str(e))
            return JSONResponse({"error": f"agent {agent_name}: {e}"}, status_code=502)
        _finish_log(done, "ok")
        msg = {"role": "assistant", "content": "".join(content)}
        if thinking:
            msg["thinking"] = "".join(thinking)
        return JSONResponse({"model": model_name, "created_at": tr.now_iso(),
                             "message": msg, "done": True, **tr._stats(_stats(done))})

    async def gen():
        done: dict = {}
        status, error = "ok", ""
        start = time.monotonic()
        pacer = keepalive.Pacer(keepalive.visible_every(svc.config_store.config.agent_brain))
        try:
            async for kind, ev in _stream_with_heartbeat(stream_iter, hb, start):
                if kind == "beat":
                    # invisible keep-alive, with a visible status only at milestones
                    yield tr.chat_chunk(model_name, "",
                                        thinking=pacer.line(f"agent {agent_name}", ev))
                    continue
                if ev.get("done"):
                    done = ev
                    continue
                if ev.get("content") or ev.get("thinking"):
                    yield tr.chat_chunk(model_name, ev.get("content") or "",
                                        thinking=ev.get("thinking") or None)
        except Exception as e:                                     # noqa: BLE001
            status, error = "error", str(e)
            yield tr.chat_chunk(model_name, f"\n[foundry-router] agent {agent_name}: {e}")
        finally:
            _finish_log(done, status, error)
        yield tr.chat_chunk(model_name, "", done=True, stats=_stats(done))
    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ---- agent mode ---------------------------------------------------------------

def _build_ctx(svc, persona: dict, model_name: str, messages: list[dict],
               user_text: str, mode: str = "agent") -> RequestContext:
    pending = prompts.find_pending_question(svc.db, messages)
    ctx = RequestContext(
        persona=persona,
        messages=prompts.sanitize_history(messages),
        guard=RequestGuardState(),
        logger=RequestLogger(svc.db, persona["virtual_name"], model_name,
                             mode, user_text),
        pending_question=pending,
    )
    # Paid-usage confirmation handshake: the previous turn paused a
    # user-requested paid dispatch to ask "continue?" — restore the steering
    # and read the reply. "yes" arms guard.user_approved_paid, which bypasses
    # tier conservation (never the dollar caps) for THIS request only.
    pending_paid = prompts.find_pending_paid(svc.db, messages)
    if pending_paid:
        decision = parse_confirmation(user_text)
        target = pending_paid.get("target") or "the requested model"
        ctx.user_model_request = {"target": target,
                                  "model_ids": pending_paid.get("model_ids") or [],
                                  "paid": True, "confirmed": decision}
        ctx.paid_confirmation = decision
        if decision is True:
            ctx.guard.user_approved_paid = True
            ctx.guard.credits_warned = True
            svc.db.log_event("info", "guardrails",
                             f"user CONFIRMED spending paid usage for {target}",
                             user_text[:200])
        elif decision is False:
            svc.db.log_event("info", "guardrails",
                             f"user DECLINED paid usage for {target} — "
                             f"routing locally", user_text[:200])
    return ctx


def _direct_mcp_tools(svc, persona: dict, client_tools) -> dict:
    """name -> ToolDef of the persona's attached MCP tools to offer in direct
    mode (merged with the client's tools). Empty when the persona has none
    attached or turned mcp_tools_in_direct off. A client tool with the same
    name wins — the client's own toolbox is never shadowed."""
    if not persona or not _persona_has_mcp_tools(persona):
        return {}
    flag = persona.get("mcp_tools_in_direct")
    if flag is not None and not int(flag):
        return {}
    try:
        tools = svc.tool_registry.mcp_tools_for_persona(persona)
    except Exception:
        return {}
    client_names = {((t or {}).get("function") or {}).get("name") for t in (client_tools or [])}
    return {t.name: t for t in tools if t.name not in client_names}


async def _failover_list(svc, persona, first: str, user_text: str, eff,
                         limit: int = 3) -> list[str]:
    """`first` plus the next models this persona's policy allows, for
    model-level failover in direct / passthrough dispatch. Local alternatives
    are free; a paid alternative is included only if the usage/cost guardrail
    clears it right now."""
    from ..brain.fallback import fallback_candidates
    out = [first]
    try:
        cands = fallback_candidates(svc.pool, svc.registry, persona, user_text, limit=6)
    except Exception:
        cands = []
    for mid in cands:
        if mid in out:
            continue
        info = svc.pool.backend_info(mid) or {}
        if info.get("type") == "anthropic-compatible" or (
                info.get("type") == "openai-compatible"
                and info.get("flavor") not in ("llamacpp", "vllm", "unsloth")):
            v = await svc.guardrails.check_paid_call(mid, info, svc.registry.get(mid),
                                                     RequestGuardState(), eff)
            if not v.allowed:
                continue
        out.append(mid)
        if len(out) >= limit:
            break
    return out


def _persona_has_mcp_tools(persona: dict) -> bool:
    try:
        return bool(json.loads(persona.get("preferred_mcp_tools") or "[]"))
    except (json.JSONDecodeError, TypeError):
        return False


def _run_events(svc, ctx: RequestContext):
    """Select the event source for this request's execution mode."""
    if ctx.logger.mode == "pipeline":
        return svc.agent.run_pipeline(ctx)
    persona = ctx.persona or {}
    # Worker-side tool calling is the opt-out default: a persona with MCP tools
    # attached lets the selected worker own the tool loop, unless it explicitly
    # sets brain_handles_tools. A tool-less persona has nothing to hand off, so
    # it stays on the brain-mediated path (which is a no-op difference there).
    try:
        has_tools = bool(json.loads(persona.get("preferred_mcp_tools") or "[]"))
    except (json.JSONDecodeError, TypeError):
        has_tools = False
    if has_tools and not persona.get("brain_handles_tools"):
        return svc.agent.run_worker_tools(ctx)
    return svc.agent.run(ctx)


async def _agent_events_to_chat_chunks(svc, ctx: RequestContext, model_name: str):
    """The heart of §4.5, corrected to the REAL Ollama wire format: think
    events stream as `thinking`-typed chunks (message.thinking populated,
    content empty) — the native reasoning field clients render as a
    collapsible panel. Literal <think> tags glued into content rendered as
    raw text in every client (found live). The final answer is the only
    thing that ever lands in content."""
    t0 = time.monotonic_ns()

    # Semantic cache (quality spec Phase 3): an eligible repeated question
    # skips the whole routing loop — served with a visible ⚡ badge, logged as
    # mode "cache". Eligibility is narrow (single-turn, non-agent persona);
    # any cache failure degrades to a normal routed request.
    from ..semcache import cache_badge
    sem = getattr(svc, "semcache", None)
    cacheable = False
    if sem is not None and ctx.persona is not None:
        cacheable, _why = sem.eligibility(ctx.persona, ctx.messages)
    if cacheable:
        try:
            hit = await sem.lookup(ctx.persona, _last_user_text(ctx.messages))
        except Exception:
            log.exception("semantic cache lookup failed")
            hit = None
        if hit:
            ctx.logger.mode = "cache"
            yield tr.chat_chunk(
                model_name, "",
                thinking=f"Semantic cache hit (similarity "
                         f"{hit['similarity']:.0%}) — serving the stored "
                         f"answer, no model call.\n")
            body = hit["answer"] + cache_badge(hit["similarity"], hit["age_seconds"])
            for piece in tr.chunk_text(body):
                yield tr.chat_chunk(model_name, piece)
            ctx.logger.finish("ok")
            yield tr.chat_chunk(model_name, "", done=True,
                                stats={"total_duration_ns": time.monotonic_ns() - t0})
            return

    status, error = "ok", ""
    answers: list[str] = []
    try:
        async for ev in _run_events(svc, ctx):
            if ev.kind == "keepalive":
                yield tr.chat_chunk(model_name, "")      # bytes only, nothing rendered
            elif ev.kind == "think_raw":
                # A model's own reasoning streamed token by token — verbatim.
                # (Appending "\n" per event put one token per line.)
                yield tr.chat_chunk(model_name, "", thinking=ev.text)
            elif ev.kind == "think":
                yield tr.chat_chunk(model_name, "", thinking=ev.text + "\n")
            elif ev.kind == "answer":
                # Safety net for literal <think> tags in answer text: worker
                # output is scrubbed at the dispatch layer, but a brain-prose
                # answer (post-nudge) never passes through it — reroute any
                # reasoning to the native field here, last exit before the wire.
                reasoning, clean = prompts.split_think(ev.text)
                if reasoning:
                    yield tr.chat_chunk(model_name, "", thinking=reasoning + "\n")
                answers.append(clean)
                for piece in tr.chunk_text(clean):
                    yield tr.chat_chunk(model_name, piece)
            elif ev.kind == "ask_user":
                status = "asked_user"
                # Pending state is stored SERVER-SIDE (§4.6) keyed by the
                # conversation fingerprint — the next request resumes from it.
                # Nothing internal is written into visible content (found live:
                # the old HTML-comment marker rendered raw in AnythingLLM).
                prompts.store_pending_question(svc.db, ctx.messages, ev.text)
                yield tr.chat_chunk(model_name, ev.text)
            elif ev.kind == "brain_down":
                ctx.logger.mode = "fallback"
                svc.db.log_event("error", "brain",
                                 "brain unreachable — static fallback engaged", ev.text)
                async for chunk in _fallback_chunks(svc, ctx, model_name):
                    yield chunk
            elif ev.kind == "error":
                status, error = "error", ev.text
                yield tr.chat_chunk(model_name, f"\n[foundry-router] {ev.text}")
    except Exception as e:  # last-ditch: never leave a stream unterminated
        log.exception("stream failed")
        status, error = "error", str(e)
        yield tr.chat_chunk(model_name, f"\n[foundry-router] internal error: {e}")
    finally:
        ctx.logger.finish(status, error)
    # Store a clean routed answer for future hits (only status ok — never an
    # error apology or an ask_user question). Store failures are non-fatal.
    if cacheable and status == "ok" and any(a.strip() for a in answers):
        try:
            await sem.store(ctx.persona, _last_user_text(ctx.messages),
                            "\n\n".join(a for a in answers if a.strip()))
        except Exception:
            log.exception("semantic cache store failed")
    yield tr.chat_chunk(model_name, "", done=True,
                        stats=_logger_stats(ctx.logger, time.monotonic_ns() - t0))


def _logger_stats(logger, total_ns: int) -> dict:
    """Final-chunk stats for a routed (agent / pipeline / fallback) request:
    the tokens of every model call it made. A routed turn can span several
    models (brain + worker + review), so there's no single decode duration —
    eval_duration falls back to wall time, i.e. an honest end-to-end rate."""
    used = getattr(logger, "models_used", None) or []
    return {"prompt_tokens": sum(int(m.get("prompt_tokens") or 0) for m in used),
            "completion_tokens": sum(int(m.get("completion_tokens") or 0) for m in used),
            "total_duration_ns": total_ns,
            # which real model(s) answered this persona turn, in call order
            "served_by": ", ".join(dict.fromkeys(m.get("model") for m in used
                                                 if m.get("model")))}


async def _fallback_chunks(svc, ctx: RequestContext, model_name: str):
    """§4.2 brain-unreachable path: the persona's static policy ranks candidate
    models (local first — the blind path never reaches for paid) and the
    conversation is forwarded directly with the persona's sampling + thinking,
    streaming. A candidate that fails BEFORE producing output is skipped for
    the next one — so a dead brain plus a dead backend still gets an answer
    from whatever is up."""
    from .. import sampling
    from ..brain.fallback import fallback_candidates
    cands = fallback_candidates(svc.pool, svc.registry, ctx.persona,
                                _last_user_text(ctx.messages))
    if not cands:
        yield tr.chat_chunk(model_name, "",
                            thinking="Routing brain unreachable and no backend is "
                                     "reachable either — cannot serve this request.\n")
        yield tr.chat_chunk(model_name,
                            "[foundry-router] No models are currently reachable.")
        return
    brain_cfg = svc.config_store.config.agent_brain
    options = sampling.resolve_options(brain_cfg.sampling_defaults, ctx.persona, None)
    errors: list[str] = []
    for i, fb_model in enumerate(cands):
        yield tr.chat_chunk(model_name, "",
                            thinking=(f"Routing brain unavailable — static policy selected "
                                      f"{fb_model} (no brain call needed).\n" if i == 0 else
                                      f"{cands[i - 1]} failed — failing over to {fb_model}.\n"))
        backend = (svc.pool.backend_info(fb_model) or {}).get("name") or "fallback"
        t_fb = time.monotonic()
        ttft_ms = None
        produced = False
        try:
            hb = float(brain_cfg.heartbeat_seconds or 0)
            pacer = keepalive.Pacer(keepalive.visible_every(brain_cfg))
            src = svc.pool.chat_stream(
                fb_model, ctx.messages, options=options,
                think=_think_for(svc, fb_model, ctx.persona),
                max_tokens=brain_cfg.worker_max_tokens)
            async for kind, chunk in _stream_with_heartbeat(src, hb, t_fb):
                if kind == "beat":
                    yield tr.chat_chunk(model_name, "",
                                        thinking=pacer.line(fb_model, chunk))
                    continue
                if chunk.get("done"):
                    res = ChatResult.from_done_frame(chunk)
                    ctx.logger.record_model_call(fb_model, backend, res.prompt_tokens,
                                                 res.completion_tokens, 0.0)
                    telemetry.record_call(
                        svc.db, svc.registry, model=fb_model, backend=backend, result=res,
                        persona=ctx.logger.persona, mode="fallback", ttft_ms=ttft_ms,
                        wall_ms=(time.monotonic() - t_fb) * 1000.0,
                        max_tokens=brain_cfg.worker_max_tokens)
                    continue
                c, th = chunk.get("content") or "", chunk.get("thinking") or ""
                if (c or th) and ttft_ms is None:
                    ttft_ms = (time.monotonic() - t_fb) * 1000.0
                if c or th:
                    produced = True
                    yield tr.chat_chunk(model_name, c, thinking=th or None)
            return
        except AllBackendsFailed as e:
            errors.append(f"{fb_model}: {e}")
            if produced:
                yield tr.chat_chunk(model_name, f"\n[foundry-router] fallback stream broke: {e}")
                return
    yield tr.chat_chunk(model_name, "\n[foundry-router] every fallback model failed: "
                                    + " | ".join(errors)[:600])


async def _agent_chat(svc, persona, model_name, messages, stream, user_text,
                      mode: str = "agent"):
    ctx = _build_ctx(svc, persona, model_name, messages, user_text, mode=mode)
    if stream:
        return StreamingResponse(_agent_events_to_chat_chunks(svc, ctx, model_name),
                                 media_type="application/x-ndjson")
    # Non-streaming: collapse the same event stream into one message —
    # narration accumulates in the native `thinking` field, the answer alone
    # lands in `content`.
    parts: list[str] = []
    thinking_parts: list[str] = []
    final: dict = {}
    async for raw in _agent_events_to_chat_chunks(svc, ctx, model_name):
        obj = json.loads(raw)
        if obj.get("done"):
            final = obj
            continue
        msg = obj["message"]
        if msg.get("thinking"):
            thinking_parts.append(msg["thinking"])
        if msg.get("content"):
            parts.append(msg["content"])
    message: dict = {"role": "assistant", "content": "".join(parts)}
    if thinking_parts:
        message["thinking"] = "".join(thinking_parts)
    body = {"model": model_name, "created_at": tr.now_iso(), "message": message}
    body.update({k: v for k, v in final.items() if k not in body})
    body["done"] = True
    return JSONResponse(body)


def _think_for(svc, model_id: str, persona=None, client_think=None):
    """The `think` value for a direct-dispatch worker. Precedence:
    persona.reasoning_effort (when force_reasoning_effort is set) > client request
    > persona.reasoning_effort > global agent_brain default. Then gated to models
    that support thinking (Ollama by capability, Claude always). Ollama gets a
    bool/level; Anthropic turns it into a budget block.

    The force flag exists because some clients (e.g. Cline) send their own
    `think` value — which would otherwise override the persona — and the operator
    may want the persona to win so thinking is controlled entirely in Foundry."""
    from .. import thinking
    p = persona or {}
    p_eff = p.get("reasoning_effort")
    if p.get("force_reasoning_effort") and p_eff:
        eff = p_eff                                    # persona wins over the client
    elif client_think not in (None, ""):
        eff = client_think                             # client passthrough
    else:
        eff = p_eff or getattr(svc.config_store.config.agent_brain,
                               "reasoning_effort", None)
    meta = svc.registry.get(model_id) or {}
    btype = (svc.pool.backend_info(model_id) or {}).get("type", "")
    return thinking.think_value(eff, model_id, meta.get("capabilities"), btype)


def _think_label(svc, model_id: str, persona=None, client_think=None) -> str:
    """What reasoning setting this call sends, and who decided it — so a turn
    with no thinking says why (persona forces off, client sent think:false,
    model has no thinking capability)."""
    try:
        v = _think_for(svc, model_id, persona, client_think)
    except Exception:                                             # noqa: BLE001
        return "think ?"
    p = persona or {}
    if p.get("force_reasoning_effort") and p.get("reasoning_effort"):
        who = "persona (forced)"
    elif client_think not in (None, ""):
        who = "client"
    elif p.get("reasoning_effort"):
        who = "persona"
    else:
        who = "default"
    if v is None:
        return "think: model default"
    if v is False or str(v).lower() in ("off", "false", "none"):
        return f"think: off by {who}"
    return f"think: {'on' if v is True else v} by {who}"


def _available_paid(svc, persona) -> list:
    """Reachable non-local (paid) chat models, honoring the persona allowlist."""
    import json as _json
    try:
        allow = _json.loads((persona or {}).get("model_allowlist") or "[]")
    except (_json.JSONDecodeError, TypeError):
        allow = []
    allowset = set(allow) | {str(a).split(":")[0] for a in allow}
    out = []
    for m in svc.pool.available_models():
        if (svc.pool.backend_info(m) or {}).get("type") == "ollama":
            continue
        meta = svc.registry.get(m)
        if meta and meta.get("embedding"):
            continue
        if allowset and not (m in allowset or str(m).split(":")[0] in allowset):
            continue
        out.append(m)
    return out


def _escalate_if_local_busy(svc, persona, model_id, user_text):
    """Load-aware escalation (opt-in per persona): if the LOCAL model we're about
    to use already has a call in flight, swap to the best available PAID model.
    The usage guardrail runs immediately after and gates that swap on quota%/cost
    — so a busy-local request goes to Claude only while the window/spend allows,
    and otherwise falls through to a local model (queuing) via the normal deny
    path. No-op when the persona hasn't opted in, the pick is already paid, the
    model is idle, or no paid model is reachable."""
    if not (persona or {}).get("escalate_when_local_busy"):
        return model_id
    if (svc.pool.backend_info(model_id) or {}).get("type") != "ollama":
        return model_id
    if not any(a["model"] == model_id and a["count"] >= 1
               for a in svc.pool.active_calls()):
        return model_id
    paid = _available_paid(svc, persona)
    if not paid:
        return model_id
    category = (persona or {}).get("benchmark_category") or guess_category(user_text)
    ranked = svc.registry.ranked_for_category(category, paid, limit=1)
    paid_id = ranked[0]["id"] if ranked else paid[0]
    svc.db.log_event("info", "routing",
                     f"local {model_id} busy → escalating to paid {paid_id} "
                     f"(gated by usage/cost guardrail)")
    return paid_id


# Stream wrapper (heartbeat beats, stall watchdog, upstream close) lives in
# keepalive so the agent / worker loops share it.
from ..keepalive import StreamStalled, _is_output, stream_with_heartbeat as _stream_with_heartbeat  # noqa: E402,F401


def _paid_pin_order(svc, persona) -> list:
    """Reachable PAID models named in the persona's `pinned_models`, in pin order,
    restricted to the model_allowlist — the manual priority list a prefer_paid
    direct persona (Cline PLAN) cascades through (Opus 4.8 -> Sonnet 5 -> local).

    Only prefer_paid personas start in the paid tier, so paid pins are meaningful
    only there; for a local-first persona this returns [] and the existing local
    pin handling (pick_fallback_model) stands. Local pins are excluded here — they
    belong to the local fallback, not the paid cascade — and embedding models are
    dropped (they can't serve /api/chat)."""
    if (persona or {}).get("local_bias_strength") != "prefer_paid":
        return []
    allow = _jl_list((persona or {}).get("model_allowlist"))
    allowset = set(allow) | {str(a).split(":")[0] for a in allow}
    avail = set(svc.pool.available_models())
    out = []
    for p in _jl_list((persona or {}).get("pinned_models")):
        if p not in avail:
            continue
        if (svc.pool.backend_info(p) or {}).get("type") == "ollama":
            continue                                   # local pins -> local fallback
        if allow and not (p in allowset or str(p).split(":")[0] in allowset):
            continue
        meta = svc.registry.get(p)
        if meta and meta.get("embedding"):
            continue
        out.append(p)
    return out


def _direct_num_ctx(svc, persona, model_id) -> "int | None":
    """The num_ctx to load an OLLAMA worker with on the DIRECT path — the persona's
    context_window, capped at the model's trained max. Mirrors the agent path's
    _num_ctx_for so the operator's context_window BOUNDS what Cline actually loads,
    not just what /api/show advertises. Without this a client sending a huge prompt
    makes Ollama cold-load a giant KV cache and time out (e.g. a model with a 262K
    unpinned context). None => inject nothing (persona set no context_window)."""
    cw = (persona or {}).get("context_window")
    try:
        cw = int(cw) if cw else 0
    except (TypeError, ValueError):
        cw = 0
    if cw <= 0:
        return None
    model_max = (svc.registry.get(model_id) or {}).get("context_length")
    try:
        return min(cw, int(model_max)) if model_max else cw
    except (TypeError, ValueError):
        return cw


def _local_fallback(svc, persona) -> "str | None":
    """Best reachable LOCAL model for the persona: allowlisted locals ranked by the
    persona's category, else any local. The guardrail-denied / usage-exhaustion
    landing spot for direct dispatch — a Cline PLAN degrades to its chosen local
    coder (e.g. qwen3.8:27b), else any local, else None if nothing local is up."""
    allow = set(_jl_list((persona or {}).get("model_allowlist")))
    allow_bases = {str(a).split(":")[0] for a in allow}
    local = [m for m in svc.pool.available_models()
             if (svc.pool.backend_info(m) or {}).get("type") == "ollama"]
    if allow:
        scoped = [m for m in local
                  if m in allow or str(m).split(":")[0] in allow_bases]
        local = scoped or local          # degrade to any local if none listed
    ranked = svc.registry.ranked_for_category(
        (persona or {}).get("benchmark_category") or "general_chat", local, limit=1)
    return ranked[0]["id"] if ranked else (local[0] if local else None)


def _jl_list(v) -> list:
    """json.loads a persona JSON field, tolerating None / bad JSON, always a list."""
    try:
        out = json.loads(v or "[]")
        return out if isinstance(out, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


# ---- direct dispatch (client brought its own tools) ------------------------------

def _output_cap(svc, persona) -> int:
    """Max output tokens for a worker call: the persona's max_output_tokens
    when set, else agent_brain.worker_max_tokens. Reasoning tokens count
    against it — a coding turn that thinks AND writes a big file edit needs
    room, or the reply is cut before its tool call completes (Cline then says
    'output-token limit reached before a tool call')."""
    try:
        v = int((persona or {}).get("max_output_tokens") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else int(svc.config_store.config.agent_brain.worker_max_tokens or 8192)


def _truncation_note(res, cap: int, persona, svc=None, model_id: str = "") -> str:
    """Thinking line (and Events entry) for a reply cut at an output limit.

    Compares what the reply actually got with the cap Foundry sent: if the
    backend stopped it well SHORT of that, the limit is on the server
    (llama.cpp -n/--n-predict, a llama-swap default, Ollama num_predict, or
    the context filling up) — raising Foundry's cap then changes nothing."""
    if (getattr(res, "finish_reason", "") or "").lower() not in ("length", "max_tokens"):
        return ""
    got = int(getattr(res, "completion_tokens", 0) or 0)
    where = ("this persona's max output tokens" if (persona or {}).get("max_output_tokens")
             else "Global settings → worker_max_tokens")
    incomplete = " — the tool call was incomplete" if not getattr(res, "tool_calls", None) else ""
    if got and cap and got < cap * 0.9:
        note = (f"⚠️ reply cut at {got:,} output tokens{incomplete}, but Foundry allowed "
                f"{cap:,} — the BACKEND stopped it. Check the server's own limit "
                f"(llama.cpp -n / --n-predict, llama-swap cmd, Ollama num_predict) or "
                f"whether the context window filled up.\n")
    else:
        note = (f"⚠️ reply cut at the {cap:,}-token output limit ({where}){incomplete}. "
                f"Reasoning counts toward this limit; raise it if this repeats.\n")
    if svc is not None:
        try:
            svc.db.log_event("warning", "truncation",
                             f"{model_id or '?'}: reply cut at {got or '?'} output tokens "
                             f"(cap sent {cap}){incomplete}",
                             f"persona={(persona or {}).get('virtual_name', '')}")
        except Exception:                                         # noqa: BLE001
            pass
    return note


def _guard_window(svc, persona, model_id: str) -> int:
    """The context window the chosen model will actually be served with: the
    persona's context_window and the model's known window (llama.cpp n_ctx /
    vLLM max_model_len / Ollama GGUF / registry), whichever is smaller."""
    vals = []
    for v in ((persona or {}).get("context_window"),
              (svc.registry.get(model_id) or {}).get("context_length")):
        try:
            if v and int(v) > 0:
                vals.append(int(v))
        except (TypeError, ValueError):
            pass
    return min(vals) if vals else 0


def _apply_context_guard(svc, persona, model_id, messages, tools, options, logger=None,
                         out_cap: int = 0):
    """Trim what's sent to the model so it fits its window. Returns
    (messages, note or '')."""
    cfg = svc.config_store.config.agent_brain
    if getattr(cfg, "context_guard", "trim") == "off":
        return messages, ""
    window = _guard_window(svc, persona, model_id)
    if not window:
        return messages, ""
    reserve = int(getattr(cfg, "context_guard_reserve_tokens", 0) or 0)
    if not reserve:
        try:
            reserve = int((options or {}).get("num_predict") or 0)
        except (TypeError, ValueError):
            reserve = 0
        reserve = reserve or out_cap or int(cfg.worker_max_tokens or 8192)
        reserve = min(reserve, window // 4)
    out, rep = context_guard.fit(messages, tools, window, reserve, model_id)
    if not rep:
        return messages, ""
    note = context_guard.describe(rep)
    svc.db.log_event("warning", "context", f"{model_id}: {note}",
                     json.dumps(rep))
    if logger is not None:
        logger.record_guardrail(note)
    return out, note


def _prompt_accounting(svc, model_id, res, convo, tools, stats: dict) -> dict:
    """Make the prompt size the client sees the TOTAL it sent. Clients (Cline)
    decide when to auto-compact from this number; Ollama's prompt_eval_count
    counts only tokens it re-processed (cached ones excluded), so after the
    first turn it can be a fraction of the real context — and the client
    never compacts. Totals from other backends calibrate the estimator."""
    try:
        btype = (svc.pool.backend_info(model_id) or {}).get("type")
        if btype == "ollama":
            est = context_guard.estimate(convo, tools, model_id)
            if res.prompt_tokens and res.prompt_tokens < est * 0.6:
                stats["prompt_evaluated"] = res.prompt_tokens
                stats["prompt_tokens"] = est
        elif res.prompt_tokens:
            context_guard.learn(model_id, convo, tools, res.prompt_tokens)
    except Exception:                                             # noqa: BLE001
        pass
    return stats


def _local_down_notes(svc, model_id: str) -> list[str]:
    """When a request lands on Claude because the local backend(s) are marked
    down, say so — otherwise a mid-session switch from local to Sonnet looks
    random."""
    info = svc.pool.backend_info(model_id) or {}
    if info.get("type") != "anthropic-compatible":
        return []
    out = []
    for b in getattr(svc.pool, "backend_status", lambda: [])():
        if b.get("type") == "anthropic-compatible" or b.get("healthy"):
            continue
        err = (b.get("last_error") or "health checks failing")[:140]
        out.append(f"local backend {b['name']} is marked down ({err}) → using {model_id}")
    return out[:2]


async def _direct_dispatch_chat(svc, persona, model_name, messages, client_tools,
                                options, stream, user_text, client_think=None,
                                client_format=None, log_mode: str = "direct",
                                note: str = "", client_keep_alive=None):
    # DESIGN DECISION: when a coding client sends its own tool definitions
    # (Kilo/Cline agent loops), the routing agent would have to interleave two
    # tool protocols in one conversation. Instead the persona's static policy
    # picks one model and the client's tools are forwarded verbatim — the
    # client stays in charge of its own agent loop, the router just picks who
    # answers. Revisit if per-turn re-routing inside coding sessions matters.
    logger = RequestLogger(svc.db, persona["virtual_name"], model_name,
                           log_mode, user_text)
    eff = svc.guardrails.effective(persona)
    model_id = None
    route_notes: list[str] = []      # why this model (shown as thinking)
    pins = _paid_pin_order(svc, persona)
    if pins:
        # Paid-pin priority cascade (prefer_paid persona, e.g. Cline PLAN): try each
        # pinned paid model in pin order — Opus 4.8 -> Sonnet 5 -> ... — each probed
        # with its OWN guardrail state so no credits/paid-call bleed between them.
        # The first one conservation allows wins; if every pinned paid is denied
        # (window/spend exhaustion), degrade to the local fallback below. This is
        # the manual priority order the operator asked for, still fully gated.
        for cand in pins:
            v = await svc.guardrails.check_paid_call(
                cand, svc.pool.backend_info(cand), svc.registry.get(cand),
                RequestGuardState(), eff)
            if v.allowed:
                model_id = cand
                break
            logger.record_guardrail(f"denied pinned {cand}: {v.reason}")
        if model_id is None:
            model_id = _local_fallback(svc, persona)
            if model_id is None:
                logger.finish("error", "all pinned paid models denied and no "
                                       "local model is reachable")
                return JSONResponse(
                    {"error": "all pinned paid models denied by the usage/cost "
                              "guardrail and no local model is reachable"},
                    status_code=503)
    else:
        # No paid pins: the original single-pick policy (unpinned / local-first
        # personas, e.g. Cline ACT). allow_paid_first lets a prefer_paid persona
        # start in the paid tier; the guardrail below still enforces conservation.
        model_id = pick_fallback_model(svc.pool, svc.registry, persona, user_text,
                                       allow_paid_first=True)
        if model_id is None:
            logger.finish("error", "no backends reachable")
            return _model_not_found(model_name)
        # Load-aware: if the chosen local model is busy, try paid (guardrail gates it).
        _picked = model_id
        model_id = _escalate_if_local_busy(svc, persona, model_id, user_text)
        if model_id != _picked:
            route_notes.append(f"local {_picked} is busy with another request → "
                               f"escalated to {model_id} (escalate when local busy)")

        verdict = await svc.guardrails.check_paid_call(
            model_id, svc.pool.backend_info(model_id), svc.registry.get(model_id),
            RequestGuardState(), eff)
        if not verdict.allowed:
            # Denied (window exhausted / conserved / spend cap): fall back to a
            # LOCAL model, preferring the persona's allowlisted locals. Only error
            # if literally nothing local is reachable.
            logger.record_guardrail(f"denied {model_id}: {verdict.reason}")
            model_id = _local_fallback(svc, persona)
            if model_id is None:
                logger.finish("error", verdict.reason)
                return JSONResponse({"error": f"guardrail denied paid call and no "
                                              f"local model is reachable: {verdict.reason}"},
                                    status_code=503)

    if not route_notes:
        route_notes.extend(_local_down_notes(svc, model_id))
    for n in route_notes:
        logger.record_guardrail(n)
    t0 = time.monotonic_ns()
    brain_cfg = svc.config_store.config.agent_brain
    # keep a heavy model warm between turns; a client-sent keep_alive wins
    keep_alive = (client_keep_alive if client_keep_alive is not None
                  else brain_cfg.worker_keep_alive)
    hb = brain_cfg.heartbeat_seconds or 0
    # Merge sampling defaults (global < persona < client) + resolve the persona's
    # structured-output format for this worker.
    from .. import sampling
    options = sampling.resolve_options(brain_cfg.sampling_defaults, persona, options)
    # Structured output would force JSON and break a tool-calling turn, so only
    # apply the persona's format when the client isn't driving its own tools.
    # A client-sent Ollama `format` (json / JSON schema) wins over the persona's.
    fmt = None if client_tools else (client_format if client_format is not None
                                     else sampling.resolve_format(persona))
    # Bound the Ollama worker's loaded context to the persona's context_window
    # (capped at the model's trained max). The agent path already did this; the
    # DIRECT path did not, so a client like Cline sending a huge prompt made
    # Ollama cold-load a giant KV cache and time out. The persona bound wins over
    # a larger client-sent num_ctx (prevents client-driven blowups) but yields to
    # a smaller one (never forces MORE context than the client asked for).
    base_options = options

    def _opts_for(mid):
        opts = base_options
        if (svc.pool.backend_info(mid) or {}).get("type") == "ollama":
            nctx = _direct_num_ctx(svc, persona, mid)
            if nctx:
                opts = dict(opts or {})
                client_ctx = opts.get("num_ctx")
                try:
                    client_ctx = int(client_ctx) if client_ctx else 0
                except (TypeError, ValueError):
                    client_ctx = 0
                opts["num_ctx"] = min(nctx, client_ctx) if client_ctx else nctx
        return opts
    options = _opts_for(model_id)
    # Model failover: if the chosen model's backend(s) fail before answering,
    # try the next model this persona's policy allows (paid ones only if the
    # guardrail clears them) instead of returning an error.
    failover = await _failover_list(svc, persona, model_id, user_text, eff)
    # Persona MCP tools in direct mode (opt-in per persona by attaching tools):
    # merged with the client's own tools. Calls to a Foundry-owned tool are run
    # here and the model continues; calls to a client tool go back to the
    # client exactly as before. A persona with no MCP tools (e.g. a Cline
    # persona that relies on Cline's own MCP servers) is completely unchanged.
    mcp_defs = _direct_mcp_tools(svc, persona, client_tools)
    all_tools = (list(client_tools or []) + [td.spec() for td in mcp_defs.values()]) or None
    if mcp_defs:
        fmt = None                        # structured output would break tool calls
    tool_cap = int(getattr(brain_cfg, "worker_tool_max_steps", 6) or 6)
    base_convo = prompts.sanitize_history(messages)
    out_cap = _output_cap(svc, persona)
    # Never send more than the model's window (Cline's auto-compact can lag).
    base_convo, _guard_note = _apply_context_guard(svc, persona, model_id, base_convo,
                                                   all_tools, options, logger, out_cap)
    if _guard_note:
        route_notes.append(_guard_note)

    async def _exec_foundry_tools(calls: list, narrate=None) -> list:
        """Run the Foundry-owned tool calls of one model turn; returns the tool
        messages to append. Failures come back to the model as tool errors
        (it can recover or answer without the tool) instead of aborting."""
        from ..brain.agent import call_tool_rich, _model_has_vision
        limit = int(getattr(brain_cfg, "worker_tool_result_chars", 0) or 24000)
        out = []
        for tc in calls:
            td = mcp_defs[tc["name"]]
            if tc.get("arguments_error"):
                out.append({"role": "tool", "name": tc["name"], "tool_call_id": tc.get("id"),
                            "content": f"ERROR: the arguments were not valid JSON "
                                       f"({tc['arguments_error'][:200]}). Call the tool again "
                                       f"with a JSON object matching its schema."})
                continue
            if narrate:
                narrate(f"🔧 {td.server}/{td.mcp_tool}…\n")
            t_call = time.monotonic()
            try:
                rich = await request_context.mcp_attributed(
                    call_tool_rich(svc.mcp, td.server, td.mcp_tool, tc.get("arguments") or {}),
                    "direct", f"{persona['virtual_name']} · {model_id}")
                dur = int((time.monotonic() - t_call) * 1000)
                logger.record_tool_call(tc["name"], td.server, dur, ok=True, caller=model_id,
                                        arguments=tc.get("arguments"),
                                        executes_code=svc.mcp.executes_code(td.server))
                text = rich.text or "(empty tool result)"
                msg = {"role": "tool", "name": tc["name"], "tool_call_id": tc.get("id"),
                       "content": text[:limit] + (f"\n…[truncated: {len(text) - limit} more chars]"
                                                  if len(text) > limit else "")}
                if rich.images() and _model_has_vision(svc.registry, model_id):
                    msg["images"] = rich.images()[:4]
                if narrate:
                    narrate(f"🔧 {td.server}/{td.mcp_tool} → {len(text)} chars in {dur}ms\n")
            except Exception as e:                            # noqa: BLE001
                dur = int((time.monotonic() - t_call) * 1000)
                from ..errors import describe_exception
                detail = describe_exception(e)
                logger.record_tool_call(tc["name"], td.server, dur, ok=False, error=detail,
                                        caller=model_id, arguments=tc.get("arguments"))
                msg = {"role": "tool", "name": tc["name"], "tool_call_id": tc.get("id"),
                       "content": f"ERROR: tool {td.server}/{td.mcp_tool} failed: {detail[:400]}"}
                if narrate:
                    narrate(f"🔧 {td.server}/{td.mcp_tool} failed: {detail[:120]}\n")
            out.append(msg)
        return out

    def _split_calls(res):
        """(foundry-owned calls, client calls) of one model turn."""
        own = [tc for tc in (res.tool_calls or []) if tc.get("name") in mcp_defs]
        rest = [tc for tc in (res.tool_calls or []) if tc.get("name") not in mcp_defs]
        return own, rest

    def _assistant_turn(res, own):
        return {"role": "assistant", "content": res.content or "",
                "tool_calls": [{"id": tc.get("id"), "type": "function",
                                "function": {"name": tc["name"],
                                             "arguments": tc.get("arguments") or {}}}
                               for tc in own]}

    last_backend = ""

    async def _run(narrate=None):
        """The worker call(s) + all post-call bookkeeping: one call, or — with
        persona MCP tools — a bounded loop that runs Foundry-owned tool calls
        and re-asks the model. Raises AllBackendsFailed."""
        nonlocal last_backend
        convo = list(base_convo)
        said: list[str] = []          # text / reasoning from earlier tool rounds
        thought: list[str] = []
        for rnd in range(tool_cap + 1):
            t_call = time.monotonic_ns()
            res, backend = await svc.pool.chat(
                model_id, convo,
                tools=all_tools, options=options, keep_alive=keep_alive,
                max_tokens=out_cap,
                think=_think_for(svc, model_id, persona, client_think), fmt=fmt)
            # Empirical tool-calling reliability: direct dispatch is where worker
            # models actually exercise tool calling (client-supplied tools).
            svc.registry.record_tool_call(model_id, ok=True)
            binfo = svc.pool.backend_info(model_id)
            if binfo and binfo.get("type") == "anthropic-compatible":
                log_subscription_usage(svc.db, model_id, backend,
                                       res.prompt_tokens, res.completion_tokens)
                svc.meridian_usage.note_successful_call(binfo["url"])
            cost = estimate_cost_usd(svc.registry.get(model_id),
                                     res.prompt_tokens, res.completion_tokens)
            logger.record_model_call(model_id, backend, res.prompt_tokens,
                                     res.completion_tokens, cost)
            telemetry.record_call(
                svc.db, svc.registry, model=model_id, backend=backend, result=res,
                persona=logger.persona, mode=logger.mode,
                wall_ms=(time.monotonic_ns() - t_call) / 1e6,
                max_tokens=out_cap)
            last_backend = backend
            own, rest = _split_calls(res)
            if not own or rnd >= tool_cap:
                if own:
                    logger.record_guardrail(f"persona tool loop hit the {tool_cap}-round cap")
                res.tool_calls = rest          # never hand Foundry-owned calls to the client
                # Keep what the model said / reasoned in earlier tool rounds
                # (live streaming shows it as it happens; buffered must too).
                if said:
                    res.content = "\n\n".join(said + [res.content or ""]).strip()
                if thought:
                    res.thinking = "\n".join(thought + [res.thinking or ""]).strip()
                logger.finish("ok")
                return res
            if (res.content or "").strip():
                said.append(res.content.strip())
            if (res.thinking or "").strip():
                thought.append(res.thinking.strip())
            convo = convo + [_assistant_turn(res, own)] + await _exec_foundry_tools(own, narrate)
        raise AllBackendsFailed("persona tool loop ended unexpectedly")

    def _on_error(e: BaseException, finish: bool = True) -> None:
        if "invalid tool call" in str(e):
            svc.registry.record_tool_call(model_id, ok=False)
        if "does not support chat" in str(e).lower():
            svc.registry.mark_embedding(model_id)
        binfo = svc.pool.backend_info(model_id)
        if (binfo and binfo.get("type") == "anthropic-compatible"
                and looks_like_window_exhaustion(str(e))):
            svc.meridian_usage.note_observed_exhaustion(binfo["url"])
        if finish:
            logger.finish("error", str(e))

    async def _run_with_failover(narrate=None):
        """_run() over the failover list: a model whose backends all fail is
        recorded and the next allowed model is tried. Re-raises the last error
        when every candidate failed."""
        nonlocal model_id, options
        last = None
        for i, mid in enumerate(failover):
            if i:
                logger.record_guardrail(f"failover: {failover[i - 1]} failed ({str(last)[:120]}) "
                                        f"-> {mid}")
                if narrate:
                    why = str(last).split(": ", 1)[-1][:160] if last else ""
                    narrate(f"⚠️ {failover[i - 1]} failed" + (f" ({why})" if why else "")
                            + f" — failing over to {mid}\n")
            model_id, options = mid, _opts_for(mid)
            try:
                return await _run(narrate)
            except AllBackendsFailed as e:
                last = e
                _on_error(e, finish=False)
        raise last if last else AllBackendsFailed("no candidate model")

    def _finalize(res):
        tool_calls = [{**({"id": tc["id"]} if tc.get("id") else {}),
                       "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                      for tc in res.tool_calls] or None
        return tool_calls, _prompt_accounting(
            svc, model_id, res, base_convo, all_tools,
            tr.result_stats(res, time.monotonic_ns() - t0, model=model_id,
                            backend=last_backend))

    # LIVE STREAMING (opt-in): forward the worker's tokens as they generate — each
    # chunk is real proof the backend is working, resets the read timeout (no
    # total-time wall), and shows the client typing live. Enabled for local Ollama
    # AND openai-dialect backends (llama.cpp / Unsloth / vLLM / OpenRouter) AND
    # Claude via Meridian — each streams with full tool + reasoning fidelity, and
    # the Claude subscription accounting runs on the stream's done frame.
    binfo0 = svc.pool.backend_info(model_id) or {}
    _btype = binfo0.get("type")
    if brain_cfg.direct_stream and stream and _btype in ("ollama", "openai-compatible",
                                                           "anthropic-compatible"):
        backend_name = binfo0.get("name") or model_id
        _tag = ("local" if _btype == "ollama" else
                "Claude" if _btype == "anthropic-compatible" else
                (binfo0.get("flavor") or "openai"))

        async def sgen():
            nonlocal model_id, options, binfo0, _btype, backend_name
            if note:
                yield tr.chat_chunk(model_name, "", done=False,
                                    thinking=f"⚙️ passthrough ({note}) — {persona['virtual_name']} "
                                             f"policy picked {model_id}\n")
            for n in route_notes:
                yield tr.chat_chunk(model_name, "", done=False, thinking=f"⚠️ {n}\n")
            _rid = (request_context.request_id() or "")[:8]
            yield tr.chat_chunk(model_name, "", done=False,
                                thinking=f"⚙️ {_tag} · {model_id} — streaming… "
                                         f"[{_think_label(svc, model_id, persona, client_think)} · "
                                         f"Foundry {__version__} · req {_rid}]\n")
            if mcp_defs:
                yield tr.chat_chunk(model_name, "", done=False,
                                    thinking=f"🔧 {len(mcp_defs)} persona MCP tool(s) available "
                                             f"alongside {len(client_tools or [])} client tool(s)\n")
            hb = float(brain_cfg.direct_stream_heartbeat_seconds or 0)
            stall = float(getattr(brain_cfg, "direct_stream_stall_seconds", 0) or 0)
            pacer = keepalive.Pacer(keepalive.visible_every(brain_cfg))   # one per request
            fail_reason = ""
            for attempt, mid in enumerate(failover):
                if attempt:
                    logger.record_guardrail(f"failover: {model_id} failed ({fail_reason}) -> {mid}")
                    yield tr.chat_chunk(model_name, "", done=False,
                                        thinking=f"⚠️ {model_id} failed"
                                                 + (f" ({fail_reason})" if fail_reason else "")
                                                 + f" — failing over to {mid}\n")
                    model_id, options = mid, _opts_for(mid)
                    binfo0 = svc.pool.backend_info(mid) or {}
                    _btype = binfo0.get("type")
                    backend_name = binfo0.get("name") or mid
                produced = False         # any output sent -> no failover possible
                convo = list(base_convo)
                err = None
                try:
                    for rnd in range(tool_cap + 1):
                        acc_tools: list = []
                        final = None
                        ttft_ms = None
                        t_round = time.monotonic_ns()
                        # status clock = this backend call's start — the same
                        # clock Live's "Models generating now" shows for it
                        hb_start = time.monotonic()
                        _src = svc.pool.chat_stream(
                            model_id, convo,
                            tools=all_tools, options=options, keep_alive=keep_alive,
                            max_tokens=out_cap,
                            think=_think_for(svc, model_id, persona, client_think), fmt=fmt)
                        prog: dict = {}
                        async for _kind, _payload in _stream_with_heartbeat(_src, hb, hb_start,
                                                                            stall, prog):
                            if _kind == "beat":
                                # Keep-alive bytes every beat; a visible status
                                # line only at milestones (30s, 60s, then every
                                # heartbeat_visible_seconds) — not a new line
                                # in the client's thinking panel every 5s.
                                yield tr.chat_chunk(
                                    model_name, "", done=False,
                                    thinking=pacer.line(
                                        f"{_tag} · {model_id}", _payload,
                                        keepalive.progress_detail(prog)
                                        or ("reading the prompt" if ttft_ms is None else "")))
                                continue
                            chunk = _payload
                            if chunk.get("done"):
                                final = chunk
                                continue
                            if chunk.get("tool_calls"):
                                acc_tools.extend(chunk["tool_calls"])   # deliver at done
                            c = chunk.get("content") or ""
                            th = chunk.get("thinking") or ""
                            if (c or th or chunk.get("tool_calls")) and ttft_ms is None:
                                # First generated token of ANY kind — the
                                # time-to-first-token, prefill-dominated.
                                ttft_ms = (time.monotonic_ns() - t_round) / 1e6
                            if c or th:
                                produced = True
                                yield tr.chat_chunk(model_name, c, done=False,
                                                    thinking=th or None)
                        final = final or {}
                        finals = acc_tools or (final.get("tool_calls") or [])
                        res = ChatResult.from_done_frame(final, tool_calls=finals)
                        pt, ct = res.prompt_tokens, res.completion_tokens
                        svc.registry.record_tool_call(model_id, ok=True)
                        if _btype == "anthropic-compatible":
                            log_subscription_usage(svc.db, model_id, backend_name, pt, ct)
                            svc.meridian_usage.note_successful_call(binfo0.get("url"))
                        cost = estimate_cost_usd(svc.registry.get(model_id), pt, ct)
                        logger.record_model_call(model_id, backend_name, pt, ct, cost)
                        # Perf + truncation telemetry, per model call.
                        telemetry.record_call(
                            svc.db, svc.registry, model=model_id, backend=backend_name,
                            result=res, persona=logger.persona, mode=logger.mode,
                            ttft_ms=ttft_ms, wall_ms=(time.monotonic_ns() - t_round) / 1e6,
                            max_tokens=out_cap)
                        own, rest = _split_calls(res)
                        if own and rnd < tool_cap:
                            # Foundry-owned tools: run them, feed the results
                            # back, and keep streaming the model's next turn.
                            produced = True
                            notes: list[str] = []
                            convo = convo + [_assistant_turn(res, own)] \
                                + await _exec_foundry_tools(own, notes.append)
                            for n in notes:
                                yield tr.chat_chunk(model_name, "", done=False, thinking=n)
                            continue
                        if own:
                            logger.record_guardrail(
                                f"persona tool loop hit the {tool_cap}-round cap")
                        logger.finish("ok")
                        tcs_out = [{**({"id": t["id"]} if t.get("id") else {}),
                                    "function": {"name": t["name"], "arguments": t["arguments"]}}
                                   for t in rest] or None
                        res.tool_calls = rest
                        _tn = _truncation_note(res, out_cap, persona, svc, model_id)
                        if _tn:
                            yield tr.chat_chunk(model_name, "", done=False, thinking=_tn)
                        yield tr.chat_chunk(
                            model_name, "", done=True, tool_calls=tcs_out,
                            stats=_prompt_accounting(
                                svc, model_id, res, convo, all_tools,
                                tr.result_stats(res, time.monotonic_ns() - t0,
                                                model=model_id, backend=backend_name)))
                        return
                except StreamStalled as e:
                    # The backend went silent (a hung / queued Claude session,
                    # a wedged llama.cpp slot). The upstream request is already
                    # closed; hand the turn to the next allowed model if the
                    # client hasn't seen any output yet.
                    msg = (f"{model_id} produced no output for {e.seconds}s "
                           f"(direct_stream_stall_seconds={int(stall)}) — abandoned")
                    svc.db.log_event("warning", "routing", msg, backend_name)
                    logger.record_guardrail(msg)
                    if not produced and attempt + 1 < len(failover):
                        fail_reason = f"no output for {e.seconds}s"
                        continue
                    logger.finish("error", msg)
                    err = RuntimeError(msg)
                except AllBackendsFailed as e:
                    if not produced and attempt + 1 < len(failover):
                        _on_error(e, finish=False)
                        fail_reason = str(e).split(": ", 1)[-1][:160]
                        continue
                    _on_error(e)      # exhaustion detection, embedding flag, log
                    err = e
                except Exception as e:                            # noqa: BLE001
                    logger.finish("error", str(e))
                    err = e
                yield tr.chat_chunk(model_name,
                                    f"[router: stream failed — {str(err)[:200]}]",
                                    done=False)
                yield tr.chat_chunk(model_name, "", done=True, stats={
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "total_duration_ns": time.monotonic_ns() - t0})
                return
        return StreamingResponse(sgen(), media_type="application/x-ndjson")

    if not stream:
        try:
            result = await _run_with_failover()
        except AllBackendsFailed as e:
            _on_error(e)
            return JSONResponse({"error": str(e)}, status_code=502)
        tool_calls, stats = _finalize(result)
        msg: dict = {"role": "assistant", "content": result.content}
        if result.thinking:
            msg["thinking"] = result.thinking
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return JSONResponse({"model": model_name, "created_at": tr.now_iso(),
                             "message": msg, "done": True, **tr._stats(stats)})

    async def gen():
        # KEEP-ALIVE: the worker call can run for MINUTES (cold-loading a large
        # model + processing a big context). Poll it with asyncio.wait() (NOT
        # wait_for/shield — that would re-raise the task's exception here and tear
        # the stream, which Cline reports as "stream terminated"), emitting an
        # empty keep-alive chunk every `hb`s so the reverse proxy and client don't
        # idle-timeout the connection.
        notes: list[str] = []
        task = asyncio.create_task(_run_with_failover(narrate=notes.append))
        btype = (svc.pool.backend_info(model_id) or {}).get("type") or ""
        where = "Claude" if btype == "anthropic-compatible" else "local"
        # IMMEDIATE beat so the client shows activity from the first moment (not a
        # silent "thinking…") — in the NATIVE thinking field, so content stays
        # clean. Names WHICH model is answering (local vs Claude).
        if note:
            yield tr.chat_chunk(model_name, "", done=False,
                                thinking=f"⚙️ passthrough ({note}) — {persona['virtual_name']} "
                                         f"policy picked {model_id}\n")
        for n in route_notes:
            yield tr.chat_chunk(model_name, "", done=False, thinking=f"⚠️ {n}\n")
        if hb:
            yield tr.chat_chunk(model_name, "", done=False,
                                thinking=f"⚙️ routing to {where} · {model_id} — working… "
                                         f"[{_think_label(svc, model_id, persona, client_think)} · "
                                         f"Foundry {__version__} · req "
                                         f"{(request_context.request_id() or '')[:8]}]\n")
        started = time.monotonic()
        pacer = keepalive.Pacer(keepalive.visible_every(brain_cfg))
        while hb:
            done, _ = await asyncio.wait({task}, timeout=hb)
            if done:
                break
            while notes:
                yield tr.chat_chunk(model_name, "", done=False, thinking=notes.pop(0))
            yield tr.chat_chunk(
                model_name, "", done=False,
                thinking=pacer.line(f"{where} · {model_id}", time.monotonic() - started))
        # Retrieve the result (or the failure) OUTSIDE the poll loop, so any error
        # becomes a clean in-band message + done, never a torn stream.
        err = None
        try:
            result = await task
        except AllBackendsFailed as e:
            _on_error(e)
            err = str(e)
        except Exception as e:                                # noqa: BLE001
            logger.finish("error", str(e))
            err = str(e)
        while notes:
            yield tr.chat_chunk(model_name, "", done=False, thinking=notes.pop(0))
        if err is not None:
            yield tr.chat_chunk(model_name,
                                f"[router: worker call failed — {err[:200]}]",
                                done=False)
            yield tr.chat_chunk(model_name, "", done=True, stats={
                "prompt_tokens": 0, "completion_tokens": 0,
                "total_duration_ns": time.monotonic_ns() - t0})
            return
        tool_calls, stats = _finalize(result)
        _tn = _truncation_note(result, out_cap, persona, svc, model_id)
        if _tn:
            yield tr.chat_chunk(model_name, "", thinking=_tn)
        if result.thinking:
            # The model's own reasoning (Claude extended thinking, a local
            # model's think block) — to the native thinking pane, not content.
            yield tr.chat_chunk(model_name, "", thinking=result.thinking)
        yield tr.chat_chunk(model_name, result.content, tool_calls=tool_calls)
        yield tr.chat_chunk(model_name, "", done=True, stats=stats)
    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ---- passthrough (raw backend model requested by name) ----------------------------

def _note_exhaustion(svc, model: str, err: BaseException) -> None:
    """A Claude-window-exhaustion-shaped failure from Meridian is real usage
    signal (its quota sources can be blind) — record it on every path."""
    binfo = svc.pool.backend_info(model) or {}
    if binfo.get("type") == "anthropic-compatible" and looks_like_window_exhaustion(str(err)):
        try:
            svc.meridian_usage.note_observed_exhaustion(binfo["url"])
        except Exception:
            pass


async def _passthrough_chat(svc, model_name, messages, client_tools, options,
                            stream, user_text, think=None, fmt=None, keep_alive=None):
    """A raw backend model requested by name: no routing, the client's request
    forwarded as-is — tools, options, think, format and keep_alive included —
    with the backend's real stats (durations, done_reason) handed back and the
    call recorded in the Live / Performance telemetry."""
    logger = RequestLogger(svc.db, "", model_name, "passthrough", user_text)
    max_tokens = svc.config_store.config.agent_brain.worker_max_tokens
    t0 = time.monotonic_ns()
    messages, guard_note = _apply_context_guard(svc, None, model_name, messages,
                                                client_tools, options, logger)
    if not stream:
        try:
            result, backend = await svc.pool.chat(
                model_name, messages, tools=client_tools, options=options,
                max_tokens=max_tokens, keep_alive=keep_alive, think=think, fmt=fmt)
        except AllBackendsFailed as e:
            _note_exhaustion(svc, model_name, e)
            logger.finish("error", str(e))
            return JSONResponse({"error": str(e)}, status_code=502)
        logger.record_model_call(model_name, backend, result.prompt_tokens,
                                 result.completion_tokens,
                                 estimate_cost_usd(svc.registry.get(model_name),
                                                   result.prompt_tokens,
                                                   result.completion_tokens))
        telemetry.record_call(
            svc.db, svc.registry, model=model_name, backend=backend, result=result,
            persona=logger.persona, mode=logger.mode, wall_ms=logger.elapsed_ms,
            max_tokens=max_tokens)
        logger.finish("ok")
        msg: dict = {"role": "assistant", "content": result.content}
        if result.thinking:
            msg["thinking"] = result.thinking
        tool_calls = [{**({"id": tc["id"]} if tc.get("id") else {}),
                       "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                      for tc in result.tool_calls]
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return JSONResponse({"model": model_name, "created_at": tr.now_iso(),
                             "message": msg, "done": True,
                             **tr._stats(_prompt_accounting(
                                 svc, model_name, result, messages, client_tools,
                                 tr.result_stats(result, time.monotonic_ns() - t0,
                                                 model=model_name, backend=backend)))})

    async def gen():
        status, error = "ok", ""
        # The backend that will actually serve the stream (first candidate) —
        # logged instead of a placeholder so per-backend perf splits work.
        backend_name = (svc.pool.backend_info(model_name) or {}).get("name") or ""
        ttft_ms = None
        acc_tools: list = []
        final = None
        brain_cfg = svc.config_store.config.agent_brain
        hb = float(brain_cfg.direct_stream_heartbeat_seconds or brain_cfg.heartbeat_seconds or 0)
        pacer = keepalive.Pacer(keepalive.visible_every(brain_cfg))
        start = time.monotonic()
        if guard_note:
            yield tr.chat_chunk(model_name, "", thinking=f"⚠️ {guard_note}\n")
        try:
            src = svc.pool.chat_stream(model_name, messages,
                                       tools=client_tools, options=options,
                                       keep_alive=keep_alive, think=think,
                                       max_tokens=max_tokens, fmt=fmt)
            prog: dict = {}
            async for kind, chunk in _stream_with_heartbeat(src, hb, start, 0, prog):
                if kind == "beat":
                    # A raw model reading a long prompt (or writing a long tool
                    # call) sends nothing visible: keep the connection alive.
                    yield tr.chat_chunk(model_name, "",
                                        thinking=pacer.line(
                                            model_name, chunk,
                                            keepalive.progress_detail(prog)
                                            or ("reading the prompt" if ttft_ms is None
                                                else "")))
                    continue
                if chunk.get("done"):
                    tools = acc_tools or (chunk.get("tool_calls") or [])
                    final = ChatResult.from_done_frame(chunk, tool_calls=tools)
                    logger.record_model_call(model_name, backend_name,
                                             final.prompt_tokens, final.completion_tokens,
                                             estimate_cost_usd(svc.registry.get(model_name),
                                                               final.prompt_tokens,
                                                               final.completion_tokens))
                    # Perf + spec/cache telemetry on the raw-passthrough path
                    # too, so a model driven by name (not via a persona) still
                    # shows decode/prefill tok/s, draft acceptance and cache
                    # hit in the live view.
                    telemetry.record_call(
                        svc.db, svc.registry, model=model_name, backend=backend_name,
                        result=final, persona=logger.persona, mode=logger.mode,
                        ttft_ms=ttft_ms, wall_ms=logger.elapsed_ms, max_tokens=max_tokens)
                    continue
                if chunk.get("tool_calls"):
                    acc_tools.extend(chunk["tool_calls"])       # delivered at done
                c, th = chunk.get("content") or "", chunk.get("thinking") or ""
                if (c or th or chunk.get("tool_calls")) and ttft_ms is None:
                    ttft_ms = logger.elapsed_ms
                if c or th:
                    yield tr.chat_chunk(model_name, c, thinking=th or None)
        except AllBackendsFailed as e:
            _note_exhaustion(svc, model_name, e)
            status, error = "error", str(e)
            yield tr.chat_chunk(model_name, f"\n[foundry-router] {e}")
        finally:
            logger.finish(status, error)
        if final is not None:
            tcs = [{**({"id": t["id"]} if t.get("id") else {}),
                                    "function": {"name": t["name"], "arguments": t["arguments"]}}
                   for t in final.tool_calls] or None
            yield tr.chat_chunk(model_name, "", done=True, tool_calls=tcs,
                                stats=_prompt_accounting(
                                    svc, model_name, final, messages, client_tools,
                                    tr.result_stats(final, time.monotonic_ns() - t0,
                                                    model=model_name, backend=backend_name)))
        else:
            yield tr.chat_chunk(model_name, "", done=True,
                                stats={"total_duration_ns": time.monotonic_ns() - t0})
    return StreamingResponse(gen(), media_type="application/x-ndjson")


# --------------------------------------------------------------------------- #
# /api/generate (legacy)                                                      #
# --------------------------------------------------------------------------- #

@router.post("/api/generate")
async def generate(request: Request):
    """Legacy completion endpoint, adapted onto /api/chat: the prompt (+system,
    images) becomes a one-turn chat dispatched through _chat_dispatch — so it
    gets identical routing, options / think / format / keep_alive handling,
    telemetry and real response stats — and each chat chunk is re-shaped into
    a generate chunk ("response" instead of "message").

    Ollama's empty-prompt convention is honoured without a model call: an empty
    prompt means "load the model" (done_reason "load"), and with keep_alive 0
    "unload" — clients such as Open WebUI use it to warm/evict models."""
    svc = _svc(request)
    from .. import request_context
    request_context.capture(request.headers)
    body = await request.json()
    model_name = body.get("model") or ""
    stream = body.get("stream", True)
    prompt = body.get("prompt") or ""
    persona = svc.personas.get(model_name)
    if persona is None and svc.pool.backend_info(model_name) is None:
        return _model_not_found(model_name)
    if not prompt and not body.get("images"):
        ka = body.get("keep_alive")
        reason = "unload" if ka in (0, "0", "0s", "0m") else "load"
        return JSONResponse({"model": model_name, "created_at": tr.now_iso(),
                             "response": "", "done": True, "done_reason": reason})
    messages = [{"role": "user", "content": prompt}]
    if body.get("images"):  # /api/generate carries images at the top level
        messages[0]["images"] = body["images"]
    if body.get("system"):
        messages.insert(0, {"role": "system", "content": body["system"]})
    chat_body = {"model": model_name, "messages": messages, "stream": bool(stream)}
    for k in ("options", "format", "think", "keep_alive"):
        if body.get(k) is not None:
            chat_body[k] = body[k]
    resp = await _chat_dispatch(svc, chat_body)

    def _reshape(obj: dict) -> dict:
        msg = obj.get("message") or {}
        out = {"model": model_name, "created_at": obj.get("created_at") or tr.now_iso(),
               "response": msg.get("content") or "", "done": bool(obj.get("done"))}
        if msg.get("thinking"):
            out["thinking"] = msg["thinking"]
        if obj.get("done"):
            for k, v in obj.items():
                if k not in ("message", "model", "created_at", "done"):
                    out[k] = v
        return out

    if isinstance(resp, StreamingResponse):
        async def gen():
            buf = b""
            async for piece in resp.body_iterator:
                buf += piece if isinstance(piece, bytes) else piece.encode("utf-8")
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        yield (json.dumps(_reshape(json.loads(line)), ensure_ascii=False)
                               + "\n").encode("utf-8")
            if buf.strip():
                yield (json.dumps(_reshape(json.loads(buf)), ensure_ascii=False)
                       + "\n").encode("utf-8")
        if stream:
            return StreamingResponse(gen(), media_type="application/x-ndjson")
        # stream:false but the path streamed anyway — collapse it.
        parts, thinking, final = [], [], {}
        async for line in gen():
            o = json.loads(line)
            parts.append(o.get("response") or "")
            if o.get("thinking"):
                thinking.append(o["thinking"])
            if o.get("done"):
                final = o
        out = {**final, "response": "".join(parts), "done": True}
        if thinking:
            out["thinking"] = "".join(thinking)
        return JSONResponse(out)
    if resp.status_code != 200:
        return resp
    return JSONResponse(_reshape(json.loads(resp.body)))
