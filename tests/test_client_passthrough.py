"""End-to-end information passthrough: Meridian (Anthropic Messages) specifics
reach Foundry — in-stream error frames, effort, attribution + session headers,
redacted thinking, usage updates, engine metrics — and everything Foundry knows
reaches OpenAI-protocol clients (OpenCode, Open WebUI, AnythingLLM): tools,
reasoning_content, finish_reason, usage details, timings, the serving model."""

import json

import httpx
import pytest

from foundry_router import request_context
from foundry_router.pool import prom
from foundry_router.pool.protocols import AnthropicProtocol, ProtocolError
from tests.test_ollama_compat import FakePool

_SEEN: list = []


def _client(handler):
    def _h(request):
        _SEEN.append(request)
        return handler(request)
    return httpx.AsyncClient(transport=httpx.MockTransport(_h))


async def _drain(agen):
    return [c async for c in agen]


def _sse(*events) -> str:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


# -- Meridian / Anthropic --------------------------------------------------------------

async def test_meridian_in_stream_error_frame_raises():
    # Meridian commits HTTP 200, then reports a refusal / exhausted account as
    # an `event: error` frame. That must not look like an empty success.
    body = _sse({"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
                {"type": "error", "error": {"type": "overloaded_error",
                                            "message": "account limit reached"}})
    proto = AnthropicProtocol("http://m", "k", _client(lambda r: httpx.Response(200, text=body)))
    with pytest.raises(ProtocolError, match="account limit reached"):
        await _drain(proto.chat_stream("claude-sonnet-5", [{"role": "user", "content": "hi"}]))


async def test_meridian_non_stream_error_body_raises():
    proto = AnthropicProtocol("http://m", None, _client(lambda r: httpx.Response(
        200, json={"type": "error", "error": {"type": "api_error", "message": "boom"}})))
    with pytest.raises(ProtocolError, match="boom"):
        await proto.chat("claude-sonnet-5", [{"role": "user", "content": "hi"}])


async def test_meridian_headers_effort_and_session_affinity():
    _SEEN.clear()
    ok = {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
          "usage": {"input_tokens": 1, "output_tokens": 1}}
    proto = AnthropicProtocol("http://m", "k", _client(lambda r: httpx.Response(200, json=ok)),
                              meridian_profile="victor", meridian_agent="opencode",
                              meridian_session_affinity=True)
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task A"}]

    class H(dict):
        def get(self, k, d=None):
            return super().get(k.lower(), d)
    request_context.capture(H({"x-request-id": "req-1"}))
    await proto.chat("claude-opus-4-8", msgs, think="high")
    r = _SEEN[-1]
    body = json.loads(r.content)
    assert body["output_config"] == {"effort": "high"}
    assert body["thinking"]["type"] == "enabled"
    assert r.headers["x-meridian-profile"] == "victor"
    assert r.headers["x-meridian-agent"] == "opencode"
    assert r.headers["x-meridian-source"] == "foundry-router"
    assert r.headers["x-request-id"] == "req-1"
    key1 = r.headers["x-session-affinity"]
    # later turn of the SAME conversation -> same key; a different one -> new key
    await proto.chat("claude-opus-4-8", msgs + [{"role": "assistant", "content": "x"},
                                                {"role": "user", "content": "more"}])
    assert _SEEN[-1].headers["x-session-affinity"] == key1
    await proto.chat("claude-opus-4-8", [{"role": "user", "content": "task B"}])
    assert _SEEN[-1].headers["x-session-affinity"] != key1
    # a client's own session header wins and is forwarded as-is
    request_context.capture(H({"x-opencode-session": "oc-123"}))
    await proto.chat("claude-opus-4-8", msgs)
    assert _SEEN[-1].headers["x-opencode-session"] == "oc-123"
    assert "x-session-affinity" not in _SEEN[-1].headers
    request_context.capture(H({}))


async def test_meridian_stream_redacted_thinking_usage_and_timing():
    body = _sse(
        {"type": "message_start", "message": {"usage": {
            "input_tokens": 3, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 100}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "redacted_thinking"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "thinking"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "thinking_delta", "thinking": "hmm"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "signature_delta", "signature": "x"}},
        {"type": "content_block_start", "index": 2, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "hi"}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "!"}},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 7}},
        {"type": "message_stop"})
    proto = AnthropicProtocol("http://m", None, _client(lambda r: httpx.Response(200, text=body)))
    chunks = await _drain(proto.chat_stream("claude-sonnet-5", [{"role": "user", "content": "hi"}]))
    thinking = "".join(c.get("thinking") or "" for c in chunks)
    assert "redacted" in thinking and "hmm" in thinking
    done = chunks[-1]
    assert done["prompt_tokens"] == 1003 and done["cached_tokens"] == 900
    assert done["completion_tokens"] == 7 and done["finish_reason"] == "length"
    assert done["timing_source"] == "estimated" and done["eval_duration_ns"] > 0


def test_prom_summarize_meridian():
    text = ('meridian_requests_total{model="opus",mode="stream",status="200"} 18\n'
            'meridian_requests_total{model="opus",mode="stream",status="429"} 2\n'
            'meridian_request_duration_ms_sum{phase="ttfb"} 30000\n'
            'meridian_request_duration_ms_count{phase="ttfb"} 20\n'
            'meridian_request_duration_ms_sum{phase="total"} 200000\n'
            'meridian_request_duration_ms_count{phase="total"} 20\n'
            'meridian_request_duration_ms_sum{phase="queue_wait"} 400\n'
            'meridian_request_duration_ms_count{phase="queue_wait"} 20\n')
    s = prom.summarize("meridian", prom.parse(text))
    assert s["requests_total"] == 20 and s["requests_failed"] == 2
    assert s["mean_ttft_ms"] == 1500.0 and s["mean_e2e_ms"] == 10000.0
    assert s["mean_queue_ms"] == 20.0


async def test_meridian_server_metrics_health_and_metrics():
    def h(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "version": "1.74.0",
                                             "mode": "passthrough",
                                             "auth": {"loggedIn": True, "subscriptionType": "max"}})
        return httpx.Response(200, text='meridian_requests_total{status="200"} 5\n')
    proto = AnthropicProtocol("http://m", None, _client(h))
    m = await proto.server_metrics()
    assert m["flavor"] == "meridian" and m["status"] == "healthy"
    assert m["subscription"] == "max" and m["requests_total"] == 5 and m["metrics_ok"]


# -- OpenAI-protocol clients (OpenCode / Open WebUI / AnythingLLM) -----------------------

def _swap(app):
    svc = app.state.services
    fake = FakePool()
    real, svc.pool = svc.pool, fake
    return svc, fake, real


def test_openai_facade_forwards_tools_and_returns_everything(app, client):
    svc, fake, real = _swap(app)
    tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    from foundry_router.pool.protocols import ChatResult
    from tests.test_ollama_compat import RES

    async def with_tool(model, messages, **kw):
        fake.calls.append(("chat", kw))
        return ChatResult(**{**RES, "finish_reason": "tool_calls",
                             "tool_calls": [{"id": "c", "name": "t", "arguments": {"a": 1}}]}), "ollama-1"
    fake.chat = with_tool
    try:
        r = client.post("/v1/chat/completions", json={
            "model": "raw", "tools": tools, "reasoning_effort": "high",
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "x", "schema": {"type": "object"}}},
            "messages": [
                {"role": "developer", "content": "be brief"},
                {"role": "user", "content": [
                    {"type": "text", "text": "look"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0"}}]},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "t", "arguments": "{\"a\":1}"}}]},
                {"role": "tool", "tool_call_id": "call_1", "content": "42"}]}).json()
    finally:
        svc.pool = real
    kind, kw = fake.calls[0]
    assert kw["tools"] == tools and kw["think"] == "high"
    assert kw["fmt"] == {"type": "object"}
    ch = r["choices"][0]
    assert ch["finish_reason"] == "tool_calls"
    assert ch["message"]["tool_calls"][0]["function"] == {"name": "t", "arguments": "{\"a\": 1}"}
    assert ch["message"]["reasoning_content"] == "hmm"
    assert r["usage"]["prompt_tokens"] == 120 and r["usage"]["completion_tokens"] == 40
    assert r["timings"]["predicted_per_second"] == 50.0      # 40 tok / 0.8s decode
    assert r["served_by"] == "raw"


def test_openai_facade_stream_reasoning_tools_usage_chunk(app, client):
    svc, fake, real = _swap(app)
    try:
        r = client.post("/v1/chat/completions", json={
            "model": "raw", "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "hi"}]})
    finally:
        svc.pool = real
    events = [json.loads(l[5:]) for l in r.text.splitlines()
              if l.startswith("data:") and l.strip() != "data: [DONE]"]
    deltas = [e["choices"][0]["delta"] for e in events if e.get("choices")]
    assert any(d.get("reasoning_content") == "hmm" for d in deltas)
    assert "".join(d.get("content") or "" for d in deltas) == "hello"
    assert any(d.get("tool_calls") for d in deltas)
    fin = [e for e in events if e.get("choices") and e["choices"][0].get("finish_reason")][-1]
    assert fin["choices"][0]["finish_reason"] == "tool_calls" and fin["timings"]
    usage = [e for e in events if not e.get("choices")][-1]["usage"]
    assert usage["completion_tokens"] == 40
    assert r.text.rstrip().endswith("data: [DONE]")


def test_openai_facade_truncation_is_length(app, client):
    svc, fake, real = _swap(app)

    async def no_tools(model, messages, **kw):
        from foundry_router.pool.protocols import ChatResult
        from tests.test_ollama_compat import RES
        return ChatResult(**RES), "ollama-1"
    fake.chat = no_tools
    try:
        r = client.post("/v1/chat/completions", json={
            "model": "raw", "messages": [{"role": "user", "content": "hi"}]}).json()
    finally:
        svc.pool = real
    assert r["choices"][0]["finish_reason"] == "length"


def test_openai_facade_unknown_model_error_envelope(client):
    r = client.post("/v1/chat/completions", json={"model": "nope", "messages": []})
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"
