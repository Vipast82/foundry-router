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

import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .. import __version__
from ..brain import prompts
from ..brain.agent import RequestContext
from ..brain.fallback import pick_fallback_model
from ..guardrails import RequestGuardState
from ..pool.base import AllBackendsFailed
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
                    **({"tool_call_id": m["tool_call_id"]} if m.get("tool_call_id") else {})})
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

@router.get("/")
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
async def ps() -> dict:
    return {"models": []}


@router.post("/api/show")
async def show(request: Request) -> JSONResponse:
    svc = _svc(request)
    body = await request.json()
    name = body.get("model") or body.get("name") or ""
    persona = svc.personas.get(name)
    if persona is None:
        return _model_not_found(name)
    return JSONResponse(tr.show_response(persona))


# --------------------------------------------------------------------------- #
# /api/chat                                                                   #
# --------------------------------------------------------------------------- #

@router.post("/api/chat")
async def chat(request: Request):
    svc = _svc(request)
    body = await request.json()
    model_name = body.get("model") or ""
    stream = body.get("stream", True)
    client_tools = body.get("tools") or None
    messages = _canonical_messages(body.get("messages") or [])
    options = body.get("options") or None
    user_text = _last_user_text(messages)

    persona = svc.personas.get(model_name)

    if persona is None:
        if svc.pool.backend_info(model_name) is not None:
            return await _passthrough_chat(svc, model_name, messages, client_tools,
                                           options, stream, user_text)
        return _model_not_found(model_name)

    if client_tools:
        return await _direct_dispatch_chat(svc, persona, model_name, messages,
                                           client_tools, options, stream, user_text)

    # Pipeline personas (Foundry-Coding) run the Prepare->Execute->Check
    # mode instead of the generic brain loop — a distinct execution mode,
    # like direct-dispatch, bookended by the paid steps.
    if (persona.get("execution_mode") or "agent") == "pipeline":
        return await _agent_chat(svc, persona, model_name, messages, stream,
                                 user_text, mode="pipeline")

    return await _agent_chat(svc, persona, model_name, messages, stream, user_text)


# ---- agent mode ---------------------------------------------------------------

def _build_ctx(svc, persona: dict, model_name: str, messages: list[dict],
               user_text: str, mode: str = "agent") -> RequestContext:
    pending = prompts.find_pending_question(svc.db, messages)
    return RequestContext(
        persona=persona,
        messages=prompts.sanitize_history(messages),
        guard=RequestGuardState(),
        logger=RequestLogger(svc.db, persona["virtual_name"], model_name,
                             mode, user_text),
        pending_question=pending,
    )


def _run_events(svc, ctx: RequestContext):
    """Select the event source for this request's execution mode."""
    if ctx.logger.mode == "pipeline":
        return svc.agent.run_pipeline(ctx)
    return svc.agent.run(ctx)


async def _agent_events_to_chat_chunks(svc, ctx: RequestContext, model_name: str):
    """The heart of §4.5, corrected to the REAL Ollama wire format: think
    events stream as `thinking`-typed chunks (message.thinking populated,
    content empty) — the native reasoning field clients render as a
    collapsible panel. Literal <think> tags glued into content rendered as
    raw text in every client (found live). The final answer is the only
    thing that ever lands in content."""
    t0 = time.monotonic_ns()
    status, error = "ok", ""
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
    yield tr.chat_chunk(model_name, "", done=True,
                        stats={"total_duration_ns": time.monotonic_ns() - t0})


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
    try:
        ptoks = ctoks = 0
        async for chunk in svc.pool.chat_stream(fb_model, ctx.messages):
            if chunk.get("done"):
                ptoks = chunk.get("prompt_tokens", 0)
                ctoks = chunk.get("completion_tokens", 0)
            elif chunk.get("content"):
                yield tr.chat_chunk(model_name, chunk["content"])
        ctx.logger.record_model_call(fb_model, "fallback", ptoks, ctoks, 0.0)
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
    async for raw in _agent_events_to_chat_chunks(svc, ctx, model_name):
        obj = json.loads(raw)
        if obj.get("done"):
            continue
        msg = obj["message"]
        if msg.get("thinking"):
            thinking_parts.append(msg["thinking"])
        if msg.get("content"):
            parts.append(msg["content"])
    message: dict = {"role": "assistant", "content": "".join(parts)}
    if thinking_parts:
        message["thinking"] = "".join(thinking_parts)
    return JSONResponse({"model": model_name, "created_at": tr.now_iso(),
                         "message": message,
                         "done": True, "done_reason": "stop", **tr._stats(None)})


# ---- direct dispatch (client brought its own tools) ------------------------------

async def _direct_dispatch_chat(svc, persona, model_name, messages, client_tools,
                                options, stream, user_text):
    # DESIGN DECISION: when a coding client sends its own tool definitions
    # (Kilo/Cline agent loops), the routing agent would have to interleave two
    # tool protocols in one conversation. Instead the persona's static policy
    # picks one model and the client's tools are forwarded verbatim — the
    # client stays in charge of its own agent loop, the router just picks who
    # answers. Revisit if per-turn re-routing inside coding sessions matters.
    logger = RequestLogger(svc.db, persona["virtual_name"], model_name,
                           "direct", user_text)
    model_id = pick_fallback_model(svc.pool, svc.registry, persona, user_text)
    if model_id is None:
        logger.finish("error", "no backends reachable")
        return _model_not_found(model_name)

    guard = RequestGuardState()
    verdict = await svc.guardrails.check_paid_call(
        model_id, svc.pool.backend_info(model_id), svc.registry.get(model_id),
        guard, svc.guardrails.effective(persona))
    if not verdict.allowed:
        # Denied (window exhausted / spend cap): re-pick among local-only
        # models; only error out if literally nothing local is reachable.
        logger.record_guardrail(f"denied {model_id}: {verdict.reason}")
        local = [m for m in svc.pool.available_models()
                 if (svc.pool.backend_info(m) or {}).get("type") == "ollama"]
        ranked = svc.registry.ranked_for_category(
            persona.get("benchmark_category") or "general_chat", local, limit=1)
        model_id = ranked[0]["id"] if ranked else (local[0] if local else None)
        if model_id is None:
            logger.finish("error", verdict.reason)
            return JSONResponse({"error": f"guardrail denied paid call and no "
                                          f"local model is reachable: {verdict.reason}"},
                                status_code=503)

    t0 = time.monotonic_ns()
    try:
        result, backend = await svc.pool.chat(
            model_id, prompts.sanitize_history(messages),
            tools=client_tools, options=options,
            max_tokens=svc.config_store.config.agent_brain.worker_max_tokens)
        # Empirical tool-calling reliability: direct dispatch is where worker
        # models actually exercise tool calling (client-supplied tools).
        svc.registry.record_tool_call(model_id, ok=True)
        svc.registry.note_inference(model_id, result.completion_tokens,
                                    result.eval_duration_ns, result.load_duration_ns)
        binfo = svc.pool.backend_info(model_id)
        if binfo and binfo.get("type") == "anthropic-compatible":
            log_subscription_usage(svc.db, model_id, backend,
                                   result.prompt_tokens, result.completion_tokens)
            svc.meridian_usage.note_successful_call(binfo["url"])
    except AllBackendsFailed as e:
        if "invalid tool call" in str(e):
            svc.registry.record_tool_call(model_id, ok=False)
        binfo = svc.pool.backend_info(model_id)
        if (binfo and binfo.get("type") == "anthropic-compatible"
                and looks_like_window_exhaustion(str(e))):
            svc.meridian_usage.note_observed_exhaustion(binfo["url"])
        logger.finish("error", str(e))
        return JSONResponse({"error": str(e)}, status_code=502)
    cost = estimate_cost_usd(svc.registry.get(model_id),
                             result.prompt_tokens, result.completion_tokens)
    logger.record_model_call(model_id, backend, result.prompt_tokens,
                             result.completion_tokens, cost)
    logger.finish("ok")

    tool_calls = [{"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                  for tc in result.tool_calls] or None
    stats = {"prompt_tokens": result.prompt_tokens,
             "completion_tokens": result.completion_tokens,
             "total_duration_ns": time.monotonic_ns() - t0}
    if not stream:
        msg: dict = {"role": "assistant", "content": result.content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return JSONResponse({"model": model_name, "created_at": tr.now_iso(),
                             "message": msg, "done": True, "done_reason": "stop",
                             **tr._stats(stats)})

    async def gen():
        yield tr.chat_chunk(model_name, result.content, tool_calls=tool_calls)
        yield tr.chat_chunk(model_name, "", done=True, stats=stats)
    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ---- passthrough (raw backend model requested by name) ----------------------------

async def _passthrough_chat(svc, model_name, messages, client_tools, options,
                            stream, user_text):
    logger = RequestLogger(svc.db, "", model_name, "passthrough", user_text)
    try:
        if client_tools or not stream:
            result, backend = await svc.pool.chat(
                model_name, messages, tools=client_tools, options=options,
                max_tokens=svc.config_store.config.agent_brain.worker_max_tokens)
            logger.record_model_call(model_name, backend, result.prompt_tokens,
                                     result.completion_tokens,
                                     estimate_cost_usd(svc.registry.get(model_name),
                                                       result.prompt_tokens,
                                                       result.completion_tokens))
            svc.registry.note_inference(model_name, result.completion_tokens,
                                        result.eval_duration_ns, result.load_duration_ns)
            logger.finish("ok")
            tool_calls = [{"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                          for tc in result.tool_calls] or None
            stats = {"prompt_tokens": result.prompt_tokens,
                     "completion_tokens": result.completion_tokens}
            if not stream:
                msg: dict = {"role": "assistant", "content": result.content}
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                return JSONResponse({"model": model_name, "created_at": tr.now_iso(),
                                     "message": msg, "done": True,
                                     "done_reason": "stop", **tr._stats(stats)})

            async def gen_one():
                yield tr.chat_chunk(model_name, result.content, tool_calls=tool_calls)
                yield tr.chat_chunk(model_name, "", done=True, stats=stats)
            return StreamingResponse(gen_one(), media_type="application/x-ndjson")

        async def gen():
            status, error = "ok", ""
            try:
                async for chunk in svc.pool.chat_stream(model_name, messages,
                                                        options=options):
                    if chunk.get("done"):
                        logger.record_model_call(model_name, "stream",
                                                 chunk.get("prompt_tokens", 0),
                                                 chunk.get("completion_tokens", 0), 0.0)
                    elif chunk.get("content"):
                        yield tr.chat_chunk(model_name, chunk["content"])
            except AllBackendsFailed as e:
                status, error = "error", str(e)
                yield tr.chat_chunk(model_name, f"\n[foundry-router] {e}")
            finally:
                logger.finish(status, error)
            yield tr.chat_chunk(model_name, "", done=True)
        return StreamingResponse(gen(), media_type="application/x-ndjson")
    except AllBackendsFailed as e:
        logger.finish("error", str(e))
        return JSONResponse({"error": str(e)}, status_code=502)


# --------------------------------------------------------------------------- #
# /api/generate (legacy)                                                      #
# --------------------------------------------------------------------------- #

@router.post("/api/generate")
async def generate(request: Request):
    """Legacy completion endpoint: adapt to a one-message chat, then re-shape
    chat chunks into generate chunks ("response" instead of "message")."""
    svc = _svc(request)
    body = await request.json()
    model_name = body.get("model") or ""
    stream = body.get("stream", True)
    prompt = body.get("prompt") or ""
    messages = [{"role": "user", "content": prompt}]
    if body.get("images"):  # /api/generate carries images at the top level
        messages[0]["images"] = body["images"]
    if body.get("system"):
        messages.insert(0, {"role": "system", "content": body["system"]})

    persona = svc.personas.get(model_name)
    if persona is None and svc.pool.backend_info(model_name) is None:
        return _model_not_found(model_name)

    async def chat_source():
        if persona is not None:
            ctx = _build_ctx(svc, persona, model_name, messages, prompt)
            async for raw in _agent_events_to_chat_chunks(svc, ctx, model_name):
                yield raw
        else:
            t0 = time.monotonic_ns()
            try:
                async for chunk in svc.pool.chat_stream(model_name, messages):
                    if not chunk.get("done") and chunk.get("content"):
                        yield tr.chat_chunk(model_name, chunk["content"])
            except AllBackendsFailed as e:
                yield tr.chat_chunk(model_name, f"[foundry-router] {e}")
            yield tr.chat_chunk(model_name, "", done=True,
                                stats={"total_duration_ns": time.monotonic_ns() - t0})

    if stream:
        async def gen():
            async for raw in chat_source():
                obj = json.loads(raw)
                if obj.get("done"):
                    yield tr.generate_chunk(model_name, "", done=True)
                    continue
                msg = obj["message"]
                if msg.get("thinking"):
                    yield tr.generate_chunk(model_name, "", thinking=msg["thinking"])
                if msg.get("content"):
                    yield tr.generate_chunk(model_name, msg["content"])
        return StreamingResponse(gen(), media_type="application/x-ndjson")

    parts: list[str] = []
    thinking_parts: list[str] = []
    async for raw in chat_source():
        obj = json.loads(raw)
        if obj.get("done"):
            continue
        msg = obj["message"]
        if msg.get("thinking"):
            thinking_parts.append(msg["thinking"])
        if msg.get("content"):
            parts.append(msg["content"])
    body: dict = {"model": model_name, "created_at": tr.now_iso(),
                  "response": "".join(parts), "done": True,
                  "done_reason": "stop", **tr._stats(None)}
    if thinking_parts:
        body["thinking"] = "".join(thinking_parts)
    return JSONResponse(body)
