"""OpenAI-compatible API facade.

Many clients (security tools, IDE plugins, SDKs built on the OpenAI library)
speak the OpenAI wire protocol, not Ollama's — they call `GET /v1/models` and
`POST /v1/chat/completions`. This module exposes those plus the Responses API
(`/v1/responses`), legacy `/v1/completions` and `/v1/embeddings`, so Foundry is
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


def _error_obj(message) -> dict:
    from .translate import client_error
    text, overflow = client_error(message)
    return {"message": text,
            "type": "invalid_request_error" if overflow else "server_error",
            "code": "context_length_exceeded" if overflow else "backend_error"}


def _error_envelope(resp) -> JSONResponse:
    try:
        err = json.loads(resp.body).get("error")
    except Exception:
        err = None
    msg = err if isinstance(err, str) else (err or {}).get("message") if isinstance(err, dict) else "error"
    code = "model_not_found" if resp.status_code == 404 else "backend_error"
    if "context window exceeded" in (msg or "").lower():
        code = "context_length_exceeded"
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
                if "error" in obj and "message" not in obj:
                    # Mid-stream failure -> an OpenAI error event (the OpenAI SDKs
                    # raise it as an APIError), code context_length_exceeded for an
                    # overflow so agents (Cline, OpenCode) can compact and retry.
                    yield _sse({"error": _error_obj(obj["error"])})
                    break
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
        if "error" in obj and "message" not in obj:
            err = _error_obj(obj["error"])
            return JSONResponse({"error": err}, status_code=400
                                if err.get("code") == "context_length_exceeded" else 502)
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


# --------------------------------------------------------------------------- #
# /v1/embeddings                                                              #
# --------------------------------------------------------------------------- #

@router.post("/v1/embeddings")
async def embeddings(request: Request):
    """OpenAI embeddings on the same embed path as Ollama's /api/embed. `input`
    may be a string, a list of strings, or token arrays; encoding_format
    "base64" returns little-endian float32 bytes, as the OpenAI SDKs expect."""
    import base64
    import struct
    from .ollama_api import _do_embed, _embed_inputs
    svc = _svc(request)
    body = await request.json()
    inputs = _embed_inputs(body.get("input"))
    if not inputs:
        return JSONResponse({"error": {"message": "'input' is required",
                                       "type": "invalid_request_error",
                                       "code": "invalid_input"}}, status_code=400)
    res, err = await _do_embed(svc, body, inputs)
    if err is not None:
        return _error_envelope(err)
    b64 = body.get("encoding_format") == "base64"
    data = []
    for i, vec in enumerate(res.get("embeddings") or []):
        emb = (base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")
               if b64 else vec)
        data.append({"object": "embedding", "index": i, "embedding": emb})
    n = int(res.get("prompt_eval_count") or 0)
    return {"object": "list", "data": data, "model": body.get("model") or "",
            "usage": {"prompt_tokens": n, "total_tokens": n}}


# --------------------------------------------------------------------------- #
# /v1/completions (legacy text completion)                                    #
# --------------------------------------------------------------------------- #

@router.post("/v1/completions")
async def completions(request: Request):
    """Legacy text completion (autocomplete plugins, older SDKs). The prompt
    becomes a one-turn chat through the same dispatch as everything else, so
    personas, routing, guardrails and telemetry are identical; the reply comes
    back as `text_completion` objects. Reasoning is not part of this format and
    is dropped. A list prompt uses its first element (n=1); `suffix`
    (fill-in-the-middle) is passed to the model as context after the prompt."""
    svc = _svc(request)
    from .. import request_context
    from .ollama_api import _chat_dispatch
    request_context.capture(request.headers)
    body = await request.json()
    model_name = body.get("model") or ""
    prompt = body.get("prompt")
    if isinstance(prompt, list):
        prompt = prompt[0] if prompt and isinstance(prompt[0], str) else ""
    prompt = prompt or ""
    if body.get("suffix"):
        prompt = (f"{prompt}<|fill-in-here|>{body['suffix']}\n\n"
                  "Reply with only the text that replaces <|fill-in-here|>.")
    stream = bool(body.get("stream", False))
    chat = {k: v for k, v in body.items() if k not in ("prompt", "suffix", "echo", "best_of",
                                                        "logprobs", "n")}
    chat["messages"] = [{"role": "user", "content": prompt}]
    resp = await _chat_dispatch(svc, _to_ollama_body(chat))
    cid = "cmpl-" + uuid.uuid4().hex
    created = _now()
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

    def _c(text="", finish=None, usage=None):
        out = {"id": cid, "object": "text_completion", "created": created,
               "model": model_name,
               "choices": [{"index": 0, "text": text, "logprobs": None,
                            "finish_reason": finish}]}
        if usage is not None:
            out["usage"] = usage
        return out

    if stream:
        async def gen():
            async for obj in source:
                if "error" in obj and "message" not in obj:
                    yield _sse({"error": _error_obj(obj["error"])})
                    break
                msg = obj.get("message") or {}
                if msg.get("content"):
                    yield _sse(_c(msg["content"]))
                elif not obj.get("done"):
                    yield ": keep-alive\n\n"
                if obj.get("done"):
                    yield _sse(_c("", _finish_from(obj, False), _usage_from(obj)["usage"]))
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    text, final = [], {}
    async for obj in source:
        if "error" in obj and "message" not in obj:
            err = _error_obj(obj["error"])
            return JSONResponse({"error": err}, status_code=400
                                if err.get("code") == "context_length_exceeded" else 502)
        msg = obj.get("message") or {}
        if msg.get("content"):
            text.append(msg["content"])
        if obj.get("done"):
            final = obj
    return JSONResponse(_c("".join(text), _finish_from(final, False),
                           _usage_from(final)["usage"]))


# --------------------------------------------------------------------------- #
# /v1/responses (OpenAI Responses API)                                        #
# --------------------------------------------------------------------------- #

def _responses_to_chat(body: dict) -> dict:
    """A Responses API request -> an OpenAI chat-completions body, so it rides
    the existing translation and dispatch. Input items: role messages
    (input_text / input_image / output_text parts), function_call and
    function_call_output; reasoning items are dropped (the backend re-thinks).
    Only function tools are forwarded — hosted tools (web_search, file_search,
    computer use) don't exist on these backends."""
    msgs: list[dict] = []
    if body.get("instructions"):
        msgs.append({"role": "system", "content": body["instructions"]})
    items = body.get("input")
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    pending_calls: list[dict] = []

    def flush_calls():
        if pending_calls:
            msgs.append({"role": "assistant", "content": "", "tool_calls": list(pending_calls)})
            pending_calls.clear()

    for it in items or []:
        if not isinstance(it, dict):
            continue
        typ = it.get("type") or ("message" if it.get("role") else "")
        if typ == "function_call":
            pending_calls.append({"id": it.get("call_id") or it.get("id"), "type": "function",
                                  "function": {"name": it.get("name") or "",
                                               "arguments": it.get("arguments") or "{}"}})
            continue
        flush_calls()
        if typ == "function_call_output":
            out = it.get("output")
            if not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False)
            msgs.append({"role": "tool", "tool_call_id": it.get("call_id"), "content": out})
        elif typ == "message":
            content = it.get("content")
            if isinstance(content, list):
                parts = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") in ("input_text", "output_text", "text"):
                        parts.append({"type": "text", "text": p.get("text") or ""})
                    elif p.get("type") == "input_image":
                        url = p.get("image_url")
                        url = url.get("url") if isinstance(url, dict) else url
                        if url:
                            parts.append({"type": "image_url", "image_url": {"url": url}})
                content = parts
            msgs.append({"role": it.get("role") or "user", "content": content or ""})
    flush_calls()

    chat: dict = {"model": body.get("model") or "", "messages": msgs,
                  "stream": bool(body.get("stream", False))}
    tools = [{"type": "function",
              "function": {"name": t.get("name"), "description": t.get("description") or "",
                           "parameters": t.get("parameters") or {"type": "object",
                                                                 "properties": {}}}}
             for t in body.get("tools") or [] if isinstance(t, dict)
             and t.get("type") == "function" and t.get("name")]
    if tools:
        chat["tools"] = tools
        tc = body.get("tool_choice")
        if isinstance(tc, dict) and tc.get("type") == "function":
            tc = {"type": "function", "function": {"name": tc.get("name")}}
        if tc is not None:
            chat["tool_choice"] = tc
        if body.get("parallel_tool_calls") is not None:
            chat["parallel_tool_calls"] = body["parallel_tool_calls"]
    for k in ("temperature", "top_p", "seed"):
        if body.get(k) is not None:
            chat[k] = body[k]
    if body.get("max_output_tokens"):
        chat["max_tokens"] = body["max_output_tokens"]
    if isinstance(body.get("reasoning"), dict):
        chat["reasoning"] = body["reasoning"]
    fmt = ((body.get("text") or {}).get("format") or {}) if isinstance(body.get("text"), dict) else {}
    if fmt.get("type") == "json_schema":
        chat["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": fmt.get("name") or "output",
                                                   "schema": fmt.get("schema") or {}}}
    elif fmt.get("type") == "json_object":
        chat["response_format"] = {"type": "json_object"}
    return chat


def _resp_usage(done: dict) -> dict:
    u = _usage_from(done)["usage"]
    out = {"input_tokens": u["prompt_tokens"], "output_tokens": u["completion_tokens"],
           "total_tokens": u["total_tokens"],
           "input_tokens_details": {"cached_tokens": (u.get("prompt_tokens_details") or {})
                                    .get("cached_tokens", 0)},
           "output_tokens_details": {"reasoning_tokens": (u.get("completion_tokens_details") or {})
                                     .get("reasoning_tokens", 0)}}
    return out


def _resp_object(rid, created, model, status, output, usage=None, incomplete=None,
                 error=None) -> dict:
    return {"id": rid, "object": "response", "created_at": created, "status": status,
            "model": model, "output": output, "parallel_tool_calls": True,
            "error": error, "incomplete_details": incomplete,
            "usage": usage, "tool_choice": "auto", "tools": [], "text": {"format": {"type": "text"}}}


@router.post("/v1/responses")
async def responses(request: Request):
    """OpenAI Responses API on the same dispatch as chat completions.
    Stateless: send the full `input` each turn (previous_response_id is
    refused rather than silently ignored). Streams the standard typed events —
    response.created / output_item.added / output_text.delta /
    reasoning_summary_text.delta / function_call_arguments.* / completed."""
    svc = _svc(request)
    from .. import request_context
    from .ollama_api import _chat_dispatch
    request_context.capture(request.headers)
    body = await request.json()
    if body.get("previous_response_id"):
        return JSONResponse({"error": {
            "message": "previous_response_id is not supported: Foundry is stateless — send "
                       "the full conversation in `input` (store=false style).",
            "type": "invalid_request_error", "code": "unsupported_parameter",
            "param": "previous_response_id"}}, status_code=400)
    model_name = body.get("model") or ""
    stream = bool(body.get("stream", False))
    resp = await _chat_dispatch(svc, _to_ollama_body(_responses_to_chat(body)))
    rid = "resp_" + uuid.uuid4().hex
    created = _now()
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

    def _fc_items(tcs):
        return [{"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:24],
                 "call_id": t["id"], "name": t["function"]["name"],
                 "arguments": t["function"]["arguments"], "status": "completed"}
                for t in _openai_tool_calls(tcs)]

    def _status(done, has_calls):
        if not has_calls and done.get("done_reason") == "length":
            return "incomplete", {"reason": "max_output_tokens"}
        return "completed", None

    if not stream:
        text, thinking, tools, final = [], [], [], {}
        async for obj in source:
            if "error" in obj and "message" not in obj:
                err = _error_obj(obj["error"])
                return JSONResponse({"error": err}, status_code=400
                                    if err.get("code") == "context_length_exceeded" else 502)
            msg = obj.get("message") or {}
            if msg.get("thinking"):
                thinking.append(msg["thinking"])
            if msg.get("content"):
                text.append(msg["content"])
            if msg.get("tool_calls") and not (obj.get("done") and tools):
                tools.extend(msg["tool_calls"])
            if obj.get("done"):
                final = obj
        output: list = []
        if thinking:
            output.append({"type": "reasoning", "id": "rs_" + uuid.uuid4().hex[:24],
                           "summary": [{"type": "summary_text", "text": "".join(thinking)}]})
        if text:
            output.append({"type": "message", "id": "msg_" + uuid.uuid4().hex[:24],
                           "status": "completed", "role": "assistant",
                           "content": [{"type": "output_text", "text": "".join(text),
                                        "annotations": []}]})
        calls = _fc_items(tools)
        output += calls
        status, inc = _status(final, bool(calls))
        out = _resp_object(rid, created, model_name, status, output, _resp_usage(final), inc)
        out["output_text"] = "".join(text)
        return JSONResponse(out)

    async def gen():
        seq = 0

        def ev(name, payload):
            nonlocal seq
            payload = {"type": name, "sequence_number": seq, **payload}
            seq += 1
            return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        output: list = []
        cur = None            # the open item: {"kind", "index", "id", "text"}
        pending_tools: list = []

        def close_item():
            nonlocal cur
            evs = []
            if cur is None:
                return evs
            if cur["kind"] == "reasoning":
                item = {"type": "reasoning", "id": cur["id"],
                        "summary": [{"type": "summary_text", "text": cur["text"]}]}
                evs.append(ev("response.reasoning_summary_text.done",
                              {"item_id": cur["id"], "output_index": cur["index"],
                               "summary_index": 0, "text": cur["text"]}))
            else:
                part = {"type": "output_text", "text": cur["text"], "annotations": []}
                item = {"type": "message", "id": cur["id"], "status": "completed",
                        "role": "assistant", "content": [part]}
                evs.append(ev("response.output_text.done",
                              {"item_id": cur["id"], "output_index": cur["index"],
                               "content_index": 0, "text": cur["text"]}))
                evs.append(ev("response.content_part.done",
                              {"item_id": cur["id"], "output_index": cur["index"],
                               "content_index": 0, "part": part}))
            evs.append(ev("response.output_item.done", {"output_index": cur["index"],
                                                        "item": item}))
            output.append(item)
            cur = None
            return evs

        def open_item(kind):
            nonlocal cur
            idx = len(output)
            if kind == "reasoning":
                iid = "rs_" + uuid.uuid4().hex[:24]
                cur = {"kind": kind, "index": idx, "id": iid, "text": ""}
                return [ev("response.output_item.added", {"output_index": idx, "item": {
                            "type": "reasoning", "id": iid, "summary": []}})]
            iid = "msg_" + uuid.uuid4().hex[:24]
            cur = {"kind": kind, "index": idx, "id": iid, "text": ""}
            return [ev("response.output_item.added", {"output_index": idx, "item": {
                        "type": "message", "id": iid, "status": "in_progress",
                        "role": "assistant", "content": []}}),
                    ev("response.content_part.added", {
                        "item_id": iid, "output_index": idx, "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []}})]

        start = _resp_object(rid, created, model_name, "in_progress", [])
        yield ev("response.created", {"response": start})
        yield ev("response.in_progress", {"response": start})
        async for obj in source:
            if "error" in obj and "message" not in obj:
                for e in close_item():
                    yield e
                err = _error_obj(obj["error"])
                yield ev("error", {"code": err["code"], "message": err["message"], "param": None})
                yield ev("response.failed", {"response": _resp_object(
                    rid, created, model_name, "failed", output, error={
                        "code": err["code"], "message": err["message"]})})
                return
            msg = obj.get("message") or {}
            if msg.get("thinking"):
                if cur is None or cur["kind"] != "reasoning":
                    for e in close_item() + open_item("reasoning"):
                        yield e
                cur["text"] += msg["thinking"]
                yield ev("response.reasoning_summary_text.delta",
                         {"item_id": cur["id"], "output_index": cur["index"],
                          "summary_index": 0, "delta": msg["thinking"]})
            if msg.get("content"):
                if cur is None or cur["kind"] != "message":
                    for e in close_item() + open_item("message"):
                        yield e
                cur["text"] += msg["content"]
                yield ev("response.output_text.delta",
                         {"item_id": cur["id"], "output_index": cur["index"],
                          "content_index": 0, "delta": msg["content"]})
            if not obj.get("done"):
                if msg.get("tool_calls"):
                    pending_tools.extend(msg["tool_calls"])
                elif not msg.get("thinking") and not msg.get("content"):
                    yield ": keep-alive\n\n"
                continue
            for e in close_item():
                yield e
            for item in _fc_items(msg.get("tool_calls") or pending_tools):
                idx = len(output)
                yield ev("response.output_item.added", {"output_index": idx, "item": {
                    **item, "arguments": "", "status": "in_progress"}})
                yield ev("response.function_call_arguments.delta", {
                    "item_id": item["id"], "output_index": idx, "delta": item["arguments"]})
                yield ev("response.function_call_arguments.done", {
                    "item_id": item["id"], "output_index": idx, "arguments": item["arguments"]})
                yield ev("response.output_item.done", {"output_index": idx, "item": item})
                output.append(item)
            status, inc = _status(obj, any(i["type"] == "function_call" for i in output))
            name = "response.completed" if status == "completed" else "response.incomplete"
            yield ev(name, {"response": _resp_object(rid, created, model_name, status, output,
                                                     _resp_usage(obj), inc)})
    return StreamingResponse(gen(), media_type="text/event-stream")
