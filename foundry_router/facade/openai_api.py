"""OpenAI-compatible API facade.

Many clients (security tools, IDE plugins, SDKs built on the OpenAI library)
speak the OpenAI wire protocol, not Ollama's — they call `GET /v1/models` and
`POST /v1/chat/completions`. This module exposes exactly those two so Foundry is
a drop-in "OpenAI-compatible" endpoint, while reusing the *same* routing brain,
personas, guardrails, and request logging as the Ollama facade: a persona name
is the OpenAI `model`, and generation is produced by the identical agent event
stream, just re-dressed in OpenAI's response shape.

Scope note: the routing agent owns tool calling, so a `tools` array in an
OpenAI request is not forwarded to the model here (same reason Ollama-side
direct-dispatch exists) — the agent decides tools itself. Simple completion
clients (the common case) are unaffected.
"""

from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..pool.base import AllBackendsFailed
from . import translate as tr
from .ollama_api import (_agent_events_to_chat_chunks, _build_ctx,
                        _canonical_messages, _last_user_text, _svc)

router = APIRouter()


def _now() -> int:
    return int(time.time())


def _options(body: dict) -> dict | None:
    """Map the OpenAI sampling fields clients commonly send onto Ollama options."""
    opts: dict = {}
    if body.get("temperature") is not None:
        opts["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        opts["top_p"] = body["top_p"]
    # OpenAI's token cap is on completion length -> Ollama's num_predict.
    cap = body.get("max_completion_tokens") or body.get("max_tokens")
    if cap:
        opts["num_predict"] = int(cap)
    return opts or None


def _chunk(cid: str, created: int, model: str, *, delta: dict | None = None,
          finish: str | None = None) -> dict:
    return {"id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}


def _completion(cid: str, created: int, model: str, content: str,
               ptoks: int = 0, ctoks: int = 0) -> dict:
    return {"id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": ptoks, "completion_tokens": ctoks,
                      "total_tokens": ptoks + ctoks}}


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj) + "\n\n"


def _not_found(name: str) -> JSONResponse:
    # OpenAI's error envelope — clients pattern-match .error.code == model_not_found.
    return JSONResponse(
        {"error": {"message": f"model '{name}' not found", "type": "invalid_request_error",
                   "code": "model_not_found"}}, status_code=404)


@router.get("/v1/models")
async def list_models(request: Request) -> dict:
    """Enabled personas, in OpenAI's model-list shape (same policy-only set the
    Ollama /api/tags advertises)."""
    svc = _svc(request)
    created = _now()
    data = [{"id": p["virtual_name"], "object": "model", "created": created,
             "owned_by": "foundry-router"}
            for p in svc.personas.list(enabled_only=True)]
    return {"object": "list", "data": data}


@router.get("/v1/models/{model}")
async def retrieve_model(request: Request, model: str):
    svc = _svc(request)
    if svc.personas.get(model) is None:
        return _not_found(model)
    return {"id": model, "object": "model", "created": _now(),
            "owned_by": "foundry-router"}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    svc = _svc(request)
    body = await request.json()
    model_name = body.get("model") or ""
    stream = bool(body.get("stream", False))
    messages = _canonical_messages(body.get("messages") or [])
    user_text = _last_user_text(messages)
    options = _options(body)
    cid = "chatcmpl-" + uuid.uuid4().hex
    created = _now()

    persona = svc.personas.get(model_name)
    if persona is None:
        if svc.pool.backend_info(model_name) is not None:
            return await _passthrough(svc, model_name, messages, options, stream,
                                      cid, created)
        return _not_found(model_name)

    # Persona path: reuse the exact Ollama agent event stream (agent / worker-
    # tools / pipeline / brain-down fallback are all selected inside), then
    # re-dress each chunk as OpenAI. `thinking` narration has no standard OpenAI
    # field, so only the answer content crosses over.
    mode = "pipeline" if (persona.get("execution_mode") or "agent") == "pipeline" else "agent"
    ctx = _build_ctx(svc, persona, model_name, messages, user_text, mode=mode)

    if stream:
        async def gen():
            yield _sse(_chunk(cid, created, model_name, delta={"role": "assistant"}))
            async for raw in _agent_events_to_chat_chunks(svc, ctx, model_name):
                obj = json.loads(raw)
                if obj.get("done"):
                    continue
                content = (obj.get("message") or {}).get("content")
                if content:
                    yield _sse(_chunk(cid, created, model_name, delta={"content": content}))
            yield _sse(_chunk(cid, created, model_name, finish="stop"))
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    parts: list[str] = []
    async for raw in _agent_events_to_chat_chunks(svc, ctx, model_name):
        obj = json.loads(raw)
        if obj.get("done"):
            continue
        content = (obj.get("message") or {}).get("content")
        if content:
            parts.append(content)
    return JSONResponse(_completion(cid, created, model_name, "".join(parts)))


async def _passthrough(svc, model, messages, options, stream, cid, created):
    """A raw backend model requested by name — forwarded untouched, OpenAI shape."""
    if stream:
        async def gen():
            yield _sse(_chunk(cid, created, model, delta={"role": "assistant"}))
            try:
                async for chunk in svc.pool.chat_stream(model, messages, options=options):
                    if chunk.get("content"):
                        yield _sse(_chunk(cid, created, model,
                                          delta={"content": chunk["content"]}))
            except AllBackendsFailed as e:
                yield _sse(_chunk(cid, created, model,
                                  delta={"content": f"[foundry-router] {e}"}))
            yield _sse(_chunk(cid, created, model, finish="stop"))
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    try:
        result, _ = await svc.pool.chat(model, messages, options=options)
    except AllBackendsFailed as e:
        return JSONResponse(
            {"error": {"message": str(e), "type": "api_error", "code": "backend_error"}},
            status_code=502)
    return JSONResponse(_completion(cid, created, model, result.content,
                                    result.prompt_tokens, result.completion_tokens))
