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
from .. import telemetry
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
        out.append({"role": role, "content": m.get("content") or "",
                    # Ollama multimodal convention: images: ["<base64>", ...].
                    # This function is the universal entry point — dropping the
                    # field here silently blinded the whole app (found live).
                    **({"images": m["images"]} if m.get("images") else {}),
                    **({"tool_calls": m["tool_calls"]} if m.get("tool_calls") else {}),
                    **({"tool_call_id": m["tool_call_id"]} if m.get("tool_call_id") else {}),
                    # Ollama names the tool a result belongs to with `tool_name`
                    # (canonical `name`, which the Ollama adapter maps back).
                    **({"name": m.get("tool_name") or m.get("name")}
                       if role == "tool" and (m.get("tool_name") or m.get("name")) else {})})
    return out


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

    exec_mode = persona.get("execution_mode") or "agent"
    # `direct` = thin proxy: pick ONE model per the persona's static policy and
    # forward the client's request verbatim. Triggered by client-supplied tools
    # (Kilo/Cline agent loops) OR by an explicit `direct` execution_mode — the
    # latter is essential for agentic clients like Cline that DON'T attach a
    # `tools` field on every turn (a plan-mode / no-tools turn would otherwise
    # fall through to the brain loop and leak its internal ask_<model> delegation
    # calls into the client, which Cline can't parse).
    if client_tools or exec_mode == "direct":
        return await _direct_dispatch_chat(svc, persona, model_name, messages,
                                           client_tools, options, stream, user_text,
                                           client_think=client_think,
                                           client_format=client_format)

    # Pipeline personas (Foundry-Coding) run the Prepare->Execute->Check
    # mode instead of the generic brain loop — a distinct execution mode,
    # like direct-dispatch, bookended by the paid steps.
    if exec_mode == "pipeline":
        return await _agent_chat(svc, persona, model_name, messages, stream,
                                 user_text, mode="pipeline")

    return await _agent_chat(svc, persona, model_name, messages, stream, user_text)


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
            if ev.kind == "think":
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
            "total_duration_ns": total_ns}


async def _fallback_chunks(svc, ctx: RequestContext, model_name: str):
    """§4.2 brain-unreachable path: static rule picks a conservative default,
    conversation forwarded directly, real token streaming where the backend
    supports it."""
    fb_model = pick_fallback_model(svc.pool, svc.registry, ctx.persona,
                                   _last_user_text(ctx.messages))
    if fb_model is None:
        yield tr.chat_chunk(model_name, "",
                            thinking="Routing brain unreachable and no backend is "
                                     "reachable either — cannot serve this request.\n")
        yield tr.chat_chunk(model_name,
                            "[foundry-router] No models are currently reachable.")
        return
    yield tr.chat_chunk(model_name, "",
                        thinking=f"Routing brain unreachable — static fallback rule "
                                 f"selected {fb_model} (no model call needed).\n")
    backend = (svc.pool.backend_info(fb_model) or {}).get("name") or "fallback"
    t_fb = time.monotonic()
    ttft_ms = None
    try:
        async for chunk in svc.pool.chat_stream(fb_model, ctx.messages):
            if chunk.get("done"):
                res = ChatResult.from_done_frame(chunk)
                ctx.logger.record_model_call(fb_model, backend, res.prompt_tokens,
                                             res.completion_tokens, 0.0)
                telemetry.record_call(
                    svc.db, svc.registry, model=fb_model, backend=backend, result=res,
                    persona=ctx.logger.persona, mode="fallback", ttft_ms=ttft_ms,
                    wall_ms=(time.monotonic() - t_fb) * 1000.0)
                continue
            if (chunk.get("content") or chunk.get("thinking")) and ttft_ms is None:
                ttft_ms = (time.monotonic() - t_fb) * 1000.0
            if chunk.get("content") or chunk.get("thinking"):
                yield tr.chat_chunk(model_name, chunk.get("content") or "",
                                    thinking=chunk.get("thinking") or None)
    except AllBackendsFailed as e:
        yield tr.chat_chunk(model_name, f"\n[foundry-router] fallback failed too: {e}")


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


async def _stream_with_heartbeat(agen, hb: float, start: float):
    """Wrap an async chunk stream: yield ("chunk", c) for each real upstream
    chunk, and ("beat", elapsed_s) whenever none arrives within `hb` seconds —
    so the caller can emit a keep-alive during a silent prompt-eval / buffered-
    reasoning gap. hb <= 0 disables the beats (pure passthrough). The pending
    read is shielded, so a beat doesn't drop the chunk that's still coming."""
    it = agen.__aiter__()
    while True:
        fut = asyncio.ensure_future(it.__anext__())
        while True:
            try:
                if hb and hb > 0:
                    chunk = await asyncio.wait_for(asyncio.shield(fut), hb)
                else:
                    chunk = await fut
            except asyncio.TimeoutError:
                yield "beat", int(time.monotonic() - start)
                continue
            except StopAsyncIteration:
                return
            yield "chunk", chunk
            break


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

async def _direct_dispatch_chat(svc, persona, model_name, messages, client_tools,
                                options, stream, user_text, client_think=None,
                                client_format=None):
    # DESIGN DECISION: when a coding client sends its own tool definitions
    # (Kilo/Cline agent loops), the routing agent would have to interleave two
    # tool protocols in one conversation. Instead the persona's static policy
    # picks one model and the client's tools are forwarded verbatim — the
    # client stays in charge of its own agent loop, the router just picks who
    # answers. Revisit if per-turn re-routing inside coding sessions matters.
    logger = RequestLogger(svc.db, persona["virtual_name"], model_name,
                           "direct", user_text)
    eff = svc.guardrails.effective(persona)
    model_id = None
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
        model_id = _escalate_if_local_busy(svc, persona, model_id, user_text)

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

    t0 = time.monotonic_ns()
    brain_cfg = svc.config_store.config.agent_brain
    keep_alive = brain_cfg.worker_keep_alive     # keep a heavy model warm between turns
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
    if (svc.pool.backend_info(model_id) or {}).get("type") == "ollama":
        nctx = _direct_num_ctx(svc, persona, model_id)
        if nctx:
            options = dict(options or {})
            client_ctx = options.get("num_ctx")
            try:
                client_ctx = int(client_ctx) if client_ctx else 0
            except (TypeError, ValueError):
                client_ctx = 0
            options["num_ctx"] = min(nctx, client_ctx) if client_ctx else nctx

    async def _run():
        """The worker call + all post-call bookkeeping. Raises AllBackendsFailed."""
        res, backend = await svc.pool.chat(
            model_id, prompts.sanitize_history(messages),
            tools=client_tools, options=options, keep_alive=keep_alive,
            max_tokens=brain_cfg.worker_max_tokens,
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
        logger.finish("ok")
        telemetry.record_call(
            svc.db, svc.registry, model=model_id, backend=backend, result=res,
            persona=logger.persona, mode=logger.mode,
            wall_ms=(time.monotonic_ns() - t0) / 1e6,
            max_tokens=brain_cfg.worker_max_tokens)
        return res

    def _on_error(e: BaseException) -> None:
        if "invalid tool call" in str(e):
            svc.registry.record_tool_call(model_id, ok=False)
        if "does not support chat" in str(e).lower():
            svc.registry.mark_embedding(model_id)
        binfo = svc.pool.backend_info(model_id)
        if (binfo and binfo.get("type") == "anthropic-compatible"
                and looks_like_window_exhaustion(str(e))):
            svc.meridian_usage.note_observed_exhaustion(binfo["url"])
        logger.finish("error", str(e))

    def _finalize(res):
        tool_calls = [{"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                      for tc in res.tool_calls] or None
        return tool_calls, tr.result_stats(res, time.monotonic_ns() - t0)

    # LIVE STREAMING (opt-in): forward the worker's tokens as they generate — each
    # chunk is real proof the backend is working, resets the read timeout (no
    # total-time wall), and shows the client typing live. Enabled for local Ollama
    # AND openai-dialect backends (llama.cpp / Unsloth / vLLM / OpenRouter), which
    # now stream with full tool + reasoning fidelity. Claude/anthropic stays on the
    # blocking path below so subscription-usage accounting runs on every call.
    binfo0 = svc.pool.backend_info(model_id) or {}
    _btype = binfo0.get("type")
    if brain_cfg.direct_stream and stream and _btype in ("ollama", "openai-compatible"):
        backend_name = binfo0.get("name") or model_id
        _tag = "local" if _btype == "ollama" else (binfo0.get("flavor") or "openai")

        async def sgen():
            yield tr.chat_chunk(model_name, "", done=False,
                                thinking=f"⚙️ {_tag} · {model_id} — streaming…\n")
            acc_tools: list = []
            ttft_recorded = False        # time-to-first-token, measured once
            ttft_ms = None               # captured value, for the perf-history row
            hb = float(brain_cfg.direct_stream_heartbeat_seconds or 0)
            hb_start = time.monotonic()
            try:
                _src = svc.pool.chat_stream(
                    model_id, prompts.sanitize_history(messages),
                    tools=client_tools, options=options, keep_alive=keep_alive,
                    max_tokens=brain_cfg.worker_max_tokens,
                    think=_think_for(svc, model_id, persona, client_think), fmt=fmt)
                async for _kind, _payload in _stream_with_heartbeat(_src, hb, hb_start):
                    if _kind == "beat":
                        yield tr.chat_chunk(
                            model_name, "", done=False,
                            thinking=f"⚙️ {model_id} — still working… {_payload}s\n")
                        continue
                    chunk = _payload
                    if chunk.get("done"):
                        pt = chunk.get("prompt_tokens") or 0
                        ct = chunk.get("completion_tokens") or 0
                        finals = acc_tools or (chunk.get("tool_calls") or [])
                        tcs_out = [{"function": {"name": t["name"], "arguments": t["arguments"]}}
                                   for t in finals] or None
                        svc.registry.record_tool_call(model_id, ok=True)
                        cost = estimate_cost_usd(svc.registry.get(model_id), pt, ct)
                        logger.record_model_call(model_id, backend_name, pt, ct, cost)
                        logger.finish("ok")
                        # Perf + truncation telemetry (a "length" finish = the
                        # reply was cut at the max-token cap — the exact reason a
                        # client then asks to "continue"; flagged in Events).
                        telemetry.record_call(
                            svc.db, svc.registry, model=model_id, backend=backend_name,
                            result=ChatResult.from_done_frame(chunk, tool_calls=finals),
                            persona=logger.persona, mode=logger.mode, ttft_ms=ttft_ms,
                            wall_ms=(time.monotonic_ns() - t0) / 1e6,
                            max_tokens=brain_cfg.worker_max_tokens)
                        yield tr.chat_chunk(
                            model_name, "", done=True, tool_calls=tcs_out,
                            stats=tr.result_stats(ChatResult.from_done_frame(chunk),
                                                  time.monotonic_ns() - t0))
                    else:
                        if chunk.get("tool_calls"):
                            acc_tools.extend(chunk["tool_calls"])   # deliver at done
                        c = chunk.get("content") or ""
                        th = chunk.get("thinking") or ""
                        if (c or th or chunk.get("tool_calls")) and not ttft_recorded:
                            # First generated token of ANY kind (answer, reasoning
                            # or tool call) — wall time since dispatch is the
                            # time-to-first-token, prefill-dominated. Counting only
                            # answer text folded a thinking model's whole reasoning
                            # phase into "TTFT". Recorded with the call's telemetry.
                            ttft_recorded = True
                            ttft_ms = (time.monotonic_ns() - t0) / 1e6
                        if c or th:
                            yield tr.chat_chunk(model_name, c, done=False,
                                                thinking=th or None)
            except Exception as e:                                # noqa: BLE001
                logger.finish("error", str(e))
                yield tr.chat_chunk(model_name,
                                    f"[router: stream failed — {str(e)[:200]}]",
                                    done=False)
                yield tr.chat_chunk(model_name, "", done=True, stats={
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "total_duration_ns": time.monotonic_ns() - t0})
        return StreamingResponse(sgen(), media_type="application/x-ndjson")

    if not stream:
        try:
            result = await _run()
        except AllBackendsFailed as e:
            _on_error(e)
            return JSONResponse({"error": str(e)}, status_code=502)
        tool_calls, stats = _finalize(result)
        msg: dict = {"role": "assistant", "content": result.content}
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
        task = asyncio.create_task(_run())
        btype = (svc.pool.backend_info(model_id) or {}).get("type") or ""
        where = "Claude" if btype == "anthropic-compatible" else "local"
        # IMMEDIATE beat so the client shows activity from the first moment (not a
        # silent "thinking…") — in the NATIVE thinking field, so content stays
        # clean. Names WHICH model is answering (local vs Claude).
        if hb:
            yield tr.chat_chunk(model_name, "", done=False,
                                thinking=f"⚙️ routing to {where} · {model_id} — working…\n")
        waited = 0.0
        while hb:
            done, _ = await asyncio.wait({task}, timeout=hb)
            if done:
                break
            waited += hb
            yield tr.chat_chunk(
                model_name, "", done=False,
                thinking=f"⚙️ {where} · {model_id} — still working ({int(waited)}s)…\n")
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
        if err is not None:
            yield tr.chat_chunk(model_name,
                                f"[router: worker call failed — {err[:200]}]",
                                done=False)
            yield tr.chat_chunk(model_name, "", done=True, stats={
                "prompt_tokens": 0, "completion_tokens": 0,
                "total_duration_ns": time.monotonic_ns() - t0})
            return
        tool_calls, stats = _finalize(result)
        yield tr.chat_chunk(model_name, result.content, tool_calls=tool_calls)
        yield tr.chat_chunk(model_name, "", done=True, stats=stats)
    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ---- passthrough (raw backend model requested by name) ----------------------------

async def _passthrough_chat(svc, model_name, messages, client_tools, options,
                            stream, user_text, think=None, fmt=None, keep_alive=None):
    """A raw backend model requested by name: no routing, the client's request
    forwarded as-is — tools, options, think, format and keep_alive included —
    with the backend's real stats (durations, done_reason) handed back and the
    call recorded in the Live / Performance telemetry."""
    logger = RequestLogger(svc.db, "", model_name, "passthrough", user_text)
    max_tokens = svc.config_store.config.agent_brain.worker_max_tokens
    t0 = time.monotonic_ns()
    if not stream:
        try:
            result, backend = await svc.pool.chat(
                model_name, messages, tools=client_tools, options=options,
                max_tokens=max_tokens, keep_alive=keep_alive, think=think, fmt=fmt)
        except AllBackendsFailed as e:
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
        tool_calls = [{"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                      for tc in result.tool_calls]
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return JSONResponse({"model": model_name, "created_at": tr.now_iso(),
                             "message": msg, "done": True,
                             **tr._stats(tr.result_stats(result, time.monotonic_ns() - t0))})

    async def gen():
        status, error = "ok", ""
        # The backend that will actually serve the stream (first candidate) —
        # logged instead of a placeholder so per-backend perf splits work.
        backend_name = (svc.pool.backend_info(model_name) or {}).get("name") or ""
        ttft_ms = None
        acc_tools: list = []
        final = None
        try:
            async for chunk in svc.pool.chat_stream(model_name, messages,
                                                    tools=client_tools, options=options,
                                                    keep_alive=keep_alive, think=think,
                                                    max_tokens=max_tokens, fmt=fmt):
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
            status, error = "error", str(e)
            yield tr.chat_chunk(model_name, f"\n[foundry-router] {e}")
        finally:
            logger.finish(status, error)
        if final is not None:
            tcs = [{"function": {"name": t["name"], "arguments": t["arguments"]}}
                   for t in final.tool_calls] or None
            yield tr.chat_chunk(model_name, "", done=True, tool_calls=tcs,
                                stats=tr.result_stats(final, time.monotonic_ns() - t0))
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
