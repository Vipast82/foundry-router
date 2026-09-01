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
from ..brain import prompts
from ..brain.agent import RequestContext
from ..brain.fallback import guess_category, pick_fallback_model
from ..brain.user_intent import parse_confirmation
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


@router.post("/api/show")
async def show(request: Request) -> JSONResponse:
    svc = _svc(request)
    body = await request.json()
    name = body.get("model") or body.get("name") or ""
    persona = svc.personas.get(name)
    if persona is None:
        return _model_not_found(name)
    return JSONResponse(tr.show_response(
        persona, context_length=_persona_context_length(svc, persona),
        capabilities=_persona_capabilities(svc, persona)))


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
                                           options, stream, user_text)
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
                                           client_think=client_think)

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


def _think_for(svc, model_id: str, persona=None, client_think=None):
    """The `think` value for a direct-dispatch worker, resolved by precedence:
    client request > persona.reasoning_effort > global agent_brain default —
    then gated to models that support thinking (Ollama by capability, Claude
    always). Ollama gets a bool/level; Anthropic turns it into a budget block."""
    from .. import thinking
    if client_think in (None, ""):
        eff = ((persona or {}).get("reasoning_effort")
               or getattr(svc.config_store.config.agent_brain, "reasoning_effort", None))
    else:
        eff = client_think
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


# ---- direct dispatch (client brought its own tools) ------------------------------

async def _direct_dispatch_chat(svc, persona, model_name, messages, client_tools,
                                options, stream, user_text, client_think=None):
    # DESIGN DECISION: when a coding client sends its own tool definitions
    # (Kilo/Cline agent loops), the routing agent would have to interleave two
    # tool protocols in one conversation. Instead the persona's static policy
    # picks one model and the client's tools are forwarded verbatim — the
    # client stays in charge of its own agent loop, the router just picks who
    # answers. Revisit if per-turn re-routing inside coding sessions matters.
    logger = RequestLogger(svc.db, persona["virtual_name"], model_name,
                           "direct", user_text)
    # allow_paid_first: a prefer_paid persona (Cline PLAN) starts in the paid tier
    # here; the guardrail below still enforces conservation, so this can't bypass
    # usage limits. Local-bias personas (Cline ACT) stay local-first.
    model_id = pick_fallback_model(svc.pool, svc.registry, persona, user_text,
                                   allow_paid_first=True)
    if model_id is None:
        logger.finish("error", "no backends reachable")
        return _model_not_found(model_name)
    # Load-aware: if the chosen local model is busy, try paid (guardrail gates it).
    model_id = _escalate_if_local_busy(svc, persona, model_id, user_text)

    guard = RequestGuardState()
    verdict = await svc.guardrails.check_paid_call(
        model_id, svc.pool.backend_info(model_id), svc.registry.get(model_id),
        guard, svc.guardrails.effective(persona))
    if not verdict.allowed:
        # Denied (window exhausted / conserved / spend cap): fall back to a LOCAL
        # model — preferring the persona's allowlisted locals (so a Cline PLAN
        # degrades to its chosen local coder, e.g. qwen3.8:27b), else any local.
        # Only error if literally nothing local is reachable.
        logger.record_guardrail(f"denied {model_id}: {verdict.reason}")
        import json as _json
        try:
            allow = set(_json.loads(persona.get("model_allowlist") or "[]"))
        except (_json.JSONDecodeError, TypeError):
            allow = set()
        allow_bases = {str(a).split(":")[0] for a in allow}
        local = [m for m in svc.pool.available_models()
                 if (svc.pool.backend_info(m) or {}).get("type") == "ollama"]
        if allow:
            scoped = [m for m in local
                      if m in allow or str(m).split(":")[0] in allow_bases]
            local = scoped or local          # degrade to any local if none listed
        ranked = svc.registry.ranked_for_category(
            persona.get("benchmark_category") or "general_chat", local, limit=1)
        model_id = ranked[0]["id"] if ranked else (local[0] if local else None)
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
    fmt = None if client_tools else sampling.resolve_format(persona)

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
        svc.registry.note_inference(model_id, res.completion_tokens,
                                    res.eval_duration_ns, res.load_duration_ns)
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
        return tool_calls, {"prompt_tokens": res.prompt_tokens,
                            "completion_tokens": res.completion_tokens,
                            "total_duration_ns": time.monotonic_ns() - t0}

    # LIVE STREAMING (opt-in, Ollama backends only): forward the worker's tokens
    # as they generate — each chunk is real proof the backend is working, resets
    # the read timeout (no total-time wall), and shows the client typing live.
    # Claude / non-streaming backends fall through to the blocking path below.
    binfo0 = svc.pool.backend_info(model_id) or {}
    if brain_cfg.direct_stream and stream and binfo0.get("type") == "ollama":
        backend_name = binfo0.get("name") or model_id

        async def sgen():
            yield tr.chat_chunk(model_name, "", done=False,
                                thinking=f"⚙️ local · {model_id} — streaming…\n")
            acc_tools: list = []
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
                        svc.registry.note_inference(
                            model_id, ct, chunk.get("eval_duration_ns") or 0,
                            chunk.get("load_duration_ns") or 0)
                        cost = estimate_cost_usd(svc.registry.get(model_id), pt, ct)
                        logger.record_model_call(model_id, backend_name, pt, ct, cost)
                        logger.finish("ok")
                        yield tr.chat_chunk(
                            model_name, "", done=True, tool_calls=tcs_out,
                            stats={"prompt_tokens": pt, "completion_tokens": ct,
                                   "total_duration_ns": time.monotonic_ns() - t0})
                    else:
                        if chunk.get("tool_calls"):
                            acc_tools.extend(chunk["tool_calls"])   # deliver at done
                        c = chunk.get("content") or ""
                        th = chunk.get("thinking") or ""
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
                             "message": msg, "done": True, "done_reason": "stop",
                             **tr._stats(stats)})

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
