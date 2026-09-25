"""OpenAI-compatible API facade.

Many clients (security tools, IDE plugins, SDKs built on the OpenAI library)
speak the OpenAI wire protocol, not Ollama's — they call `GET /v1/models` and
`POST /v1/chat/completions`. This module exposes exactly those two so Foundry is
a drop-in "OpenAI-compatible" endpoint, while reusing the *same* routing brain,
personas, guardrails, and request logging as the Ollama facade: a persona name
is the OpenAI `model`, and generation is produced by the identical agent event
stream, just re-dressed in OpenAI's response shape.

Every request is translated into an Ollama /api/chat body and dispatched
through the SAME function as the Ollama facade (_chat_dispatch), so OpenAI-
protocol clients (OpenCode, Open WebUI's OpenAI connections, AnythingLLM's
generic-OpenAI provider, SDKs) get identical behaviour: client `tools` switch a
persona to direct dispatch and tool_calls come back; reasoning streams as
`reasoning_content`; a truncated reply reports finish_reason "length"; usage
carries cached / reasoning token details plus llama.cpp-style `timings`.
"""

from __future__ import annotations

import json
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..insights import normalize_rating, record_feedback
from .ollama_api import _svc

router = APIRouter()


@router.post("/v1/feedback")
async def feedback(request: Request) -> JSONResponse:
    """Generic response-feedback ingest (quality-tracking spec Phase 1).
    Clients don't push thumbs to an Ollama/OpenAI-compatible backend natively,
    so this is the wire-in point for anything that CAN call out (an Open WebUI
    Function/filter, a script, curl). Body: {"rating": "up"|"down"|+1|-1,
    "persona"?: str, "message"?: str (the user prompt, for request matching),
    "comment"?: str}. Matching to a logged request is best-effort — unmatched
    feedback still counts toward the persona's trend."""
    body = await request.json()
    rating = normalize_rating(body.get("rating"))
    if rating is None:
        return JSONResponse(
            {"error": {"message": "rating must be up/down/+1/-1",
                       "type": "invalid_request_error", "code": "bad_rating"}},
            status_code=400)
    out = record_feedback(
        _svc(request).db, rating,
        persona=(body.get("persona") or body.get("model") or None),
        comment=str(body.get("comment") or ""),
        message=body.get("message"), source="api")
    return JSONResponse({"ok": True, **out})


def _now() -> int:
    return int(time.time())


_PASSTHROUGH_SAMPLING = (
    "temperature", "top_p", "seed", "stop", "presence_penalty", "frequency_penalty",
    "logit_bias", "top_k", "min_p", "typical_p", "repeat_penalty",
    "repetition_penalty", "repeat_last_n", "min_tokens", "dry_multiplier",
    "dry_base", "dry_allowed_length", "dry_penalty_last_n", "xtc_probability",
    "xtc_threshold", "top_n_sigma", "mirostat", "mirostat_tau", "mirostat_eta",
    "chat_template_kwargs")


def _options(body: dict) -> dict | None:
    """Map the OpenAI sampling fields clients commonly send onto Ollama options."""
    opts: dict = {}
    # Standard + the common local-runner extensions (llama.cpp / vLLM). Each
    # backend protocol forwards only what its server understands, so passing
    # them all through here is safe — previously everything but temperature /
    # top_p was silently dropped on this facade.
    for k in _PASSTHROUGH_SAMPLING:
        if body.get(k) is not None:
            opts[k] = body[k]
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
               ptoks: int = 0, ctoks: int = 0, finish: str = "stop") -> dict:
    return {"id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "finish_reason": finish or "stop",
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


def _to_ollama_messages(raw: list) -> list[dict]:
    """OpenAI chat messages -> the Ollama-shaped messages _chat_dispatch takes.
    Text parts are joined, base64 data-URI images become Ollama `images`,
    assistant tool_calls keep their ids (so tool results still pair up on a
    Claude backend), `developer` is a system message."""
    out = []
    for m in raw or []:
        role = m.get("role") or "user"
        if role == "developer":
            role = "system"
        content = m.get("content")
        images: list[str] = []
        if isinstance(content, list):
            texts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in ("text", "input_text"):
                    texts.append(part.get("text") or "")
                elif part.get("type") in ("image_url", "input_image"):
                    url = part.get("image_url")
                    url = url.get("url") if isinstance(url, dict) else url
                    if isinstance(url, str) and url.startswith("data:") and "," in url:
                        images.append(url.split(",", 1)[1])
            content = "\n".join(t for t in texts if t)
        mm: dict = {"role": role, "content": content or ""}
        if images:
            mm["images"] = images
        if m.get("tool_calls"):
            mm["tool_calls"] = [
                {"id": tc.get("id"), "type": "function",
                 "function": {"name": (tc.get("function") or {}).get("name"),
                              "arguments": (tc.get("function") or {}).get("arguments") or "{}"}}
                for tc in m["tool_calls"] if isinstance(tc, dict)]
        if role == "tool":
            if m.get("tool_call_id"):
                mm["tool_call_id"] = m["tool_call_id"]
            if m.get("name"):
                mm["tool_name"] = m["name"]
        if m.get("reasoning_content") and role == "assistant":
            mm["thinking"] = m["reasoning_content"]
        out.append(mm)
    return out


def _think_from(body: dict):
    """OpenAI-family reasoning controls -> Foundry's think value."""
    if body.get("reasoning_effort") is not None:
        return body["reasoning_effort"]
    r = body.get("reasoning")
    if isinstance(r, dict) and r.get("effort") is not None:
        return r["effort"]
    t = body.get("thinking")
    if isinstance(t, dict) and t.get("type") in ("enabled", "disabled"):
        return t["type"] == "enabled"
    ctk = body.get("chat_template_kwargs")
    if isinstance(ctk, dict) and ctk.get("enable_thinking") is not None:
        return bool(ctk["enable_thinking"])
    return None


def _to_ollama_body(body: dict) -> dict:
    ob: dict = {"model": body.get("model") or "", "stream": bool(body.get("stream", False)),
                "messages": _to_ollama_messages(body.get("messages") or [])}
    tools = body.get("tools") or None
    if tools and body.get("tool_choice") != "none":
        ob["tools"] = tools
    opts = _options(body) or {}
    # Tool-use controls ride in options (the one channel every dispatch path
    # forwards); each backend protocol translates or drops them.
    if tools and body.get("tool_choice") not in (None, "none"):
        opts["tool_choice"] = body["tool_choice"]
    if tools and body.get("parallel_tool_calls") is not None:
        opts["parallel_tool_calls"] = bool(body["parallel_tool_calls"])
    if opts:
        ob["options"] = opts
    rf = body.get("response_format")
    if isinstance(rf, dict):
        if rf.get("type") == "json_object":
            ob["format"] = "json"
        elif rf.get("type") == "json_schema":
            js = rf.get("json_schema") or {}
            ob["format"] = js.get("schema") if isinstance(js.get("schema"), dict) else js or "json"
    think = _think_from(body)
    if think is not None:
        ob["think"] = think
    return ob


def _openai_tool_calls(tcs) -> list[dict]:
    out = []
    for i, tc in enumerate(tcs or []):
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        out.append({"index": i, "id": tc.get("id") or "call_" + uuid.uuid4().hex[:24],
                    "type": "function",
                    "function": {"name": fn.get("name") or "",
                                 "arguments": args if isinstance(args, str)
                                 else json.dumps(args or {})}})
    return out


def _finish_from(done: dict, had_tools: bool) -> str:
    extra = (done.get("foundry") or {}).get("finish_reason")
    if had_tools:
        return "tool_calls"
    if done.get("done_reason") == "length":
        return "length"
    if extra in ("refusal", "content_filter"):
        return "content_filter"
    return "stop"


def _usage_from(done: dict) -> dict:
    """OpenAI usage (+ cached / reasoning details) and llama.cpp-style
    `timings` from an Ollama final chunk. Clients that don't know `timings`
    ignore it; Open WebUI and others show it in their usage details."""
    extra = done.get("foundry") or {}
    pt, ct = int(done.get("prompt_eval_count") or 0), int(done.get("eval_count") or 0)
    usage: dict = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}
    if extra.get("cached_tokens"):
        usage["prompt_tokens_details"] = {"cached_tokens": int(extra["cached_tokens"])}
    if extra.get("reasoning_tokens"):
        usage["completion_tokens_details"] = {"reasoning_tokens": int(extra["reasoning_tokens"])}
    pe, ev = int(done.get("prompt_eval_duration") or 0), int(done.get("eval_duration") or 0)
    timings = {"prompt_n": pt, "prompt_ms": round(pe / 1e6, 1),
               "predicted_n": ct, "predicted_ms": round(ev / 1e6, 1)}
    if pe:
        timings["prompt_per_second"] = round(pt / (pe / 1e9), 2)
    if ev and ct:
        timings["predicted_per_second"] = round(ct / (ev / 1e9), 2)
    if done.get("load_duration"):
        timings["load_ms"] = round(int(done["load_duration"]) / 1e6, 1)
    if done.get("total_duration"):
        timings["total_ms"] = round(int(done["total_duration"]) / 1e6, 1)
    return {"usage": usage, "timings": timings,
            **({"served_by": extra["served_by"]} if extra.get("served_by") else {})}


async def _ndjson_objects(resp):
    buf = b""
    async for piece in resp.body_iterator:
        buf += piece if isinstance(piece, bytes) else piece.encode("utf-8")
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if line.strip():
                yield json.loads(line)
    if buf.strip():
        yield json.loads(buf)


def _error_envelope(resp) -> JSONResponse:
    try:
        err = json.loads(resp.body).get("error")
    except Exception:
        err = None
    msg = err if isinstance(err, str) else (err or {}).get("message") if isinstance(err, dict) else "error"
    code = "model_not_found" if resp.status_code == 404 else "backend_error"
    return JSONResponse({"error": {"message": msg or "error", "type": "invalid_request_error"
                                   if resp.status_code < 500 else "api_error", "code": code}},
                        status_code=resp.status_code)


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    svc = _svc(request)
    from .. import request_context
    from .ollama_api import _chat_dispatch
    request_context.capture(request.headers)
    body = await request.json()
    model_name = body.get("model") or ""
    stream = bool(body.get("stream", False))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
    cid = "chatcmpl-" + uuid.uuid4().hex
    created = _now()
    resp = await _chat_dispatch(svc, _to_ollama_body(body))
    if not isinstance(resp, StreamingResponse):
        if resp.status_code != 200:
            return _error_envelope(resp)
        objs = [json.loads(resp.body)]

        async def _one():
            for o in objs:
                yield o
        source = _one()
    else:
        source = _ndjson_objects(resp)

    if stream:
        async def gen():
            yield _sse(_chunk(cid, created, model_name, delta={"role": "assistant"}))
            pending_tools: list = []
            async for obj in source:
                msg = obj.get("message") or {}
                if msg.get("thinking"):
                    yield _sse(_chunk(cid, created, model_name,
                                      delta={"reasoning_content": msg["thinking"]}))
                if msg.get("content"):
                    yield _sse(_chunk(cid, created, model_name,
                                      delta={"content": msg["content"]}))
                if not obj.get("done"):
                    if msg.get("tool_calls"):
                        pending_tools.extend(msg["tool_calls"])
                    elif not msg.get("thinking") and not msg.get("content"):
                        # Foundry's invisible keep-alive: an SSE comment line —
                        # bytes for proxies / idle timers, ignored by every
                        # OpenAI client (SSE spec: lines starting ':' are comments).
                        yield ": keep-alive\n\n"
                    continue
                tcs = _openai_tool_calls(msg.get("tool_calls") or pending_tools)
                if tcs:
                    yield _sse(_chunk(cid, created, model_name, delta={"tool_calls": tcs}))
                u = _usage_from(obj)
                fin = _chunk(cid, created, model_name, finish=_finish_from(obj, bool(tcs)))
                fin["timings"] = u["timings"]
                if u.get("served_by"):
                    fin["served_by"] = u["served_by"]
                if not include_usage:
                    fin["usage"] = u["usage"]
                yield _sse(fin)
                if include_usage:
                    yield _sse({"id": cid, "object": "chat.completion.chunk",
                                "created": created, "model": model_name,
                                "choices": [], "usage": u["usage"]})
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    content, reasoning, final, tools = [], [], {}, []
    async for obj in source:
        msg = obj.get("message") or {}
        if msg.get("thinking"):
            reasoning.append(msg["thinking"])
        if msg.get("content"):
            content.append(msg["content"])
        if msg.get("tool_calls"):
            if obj.get("done") and tools:
                pass                   # already collected mid-stream; don't duplicate
            else:
                tools.extend(msg["tool_calls"])
        if obj.get("done"):
            final = obj
    tcs = _openai_tool_calls(tools)
    message: dict = {"role": "assistant", "content": "".join(content) or (None if tcs else "")}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tcs:
        message["tool_calls"] = [{k: v for k, v in t.items() if k != "index"} for t in tcs]
    u = _usage_from(final)
    out = {"id": cid, "object": "chat.completion", "created": created, "model": model_name,
           "choices": [{"index": 0, "finish_reason": _finish_from(final, bool(tcs)),
                        "message": message}],
           "usage": u["usage"], "timings": u["timings"]}
    if u.get("served_by"):
        out["served_by"] = u["served_by"]
    return JSONResponse(out)
