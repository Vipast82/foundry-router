"""Keep-alive + end-to-end passthrough audit: sparse visible status lines
(bytes every beat), tool-call generation counted as output (no false stall,
honest status), OpenAI SSE keep-alive comments, request ids end to end, a
client's output cap reaching Claude, client keep_alive in direct mode, and
streamed reasoning kept verbatim."""

import asyncio
import json

import httpx

from foundry_router import keepalive, request_context
from foundry_router.pool.protocols import AnthropicProtocol, OpenAIProtocol


def test_pacer_milestones_are_sparse():
    p = keepalive.Pacer(60)
    shown = [t for t in range(5, 400, 5) if p.due(t)]
    assert shown == [30, 60, 120, 180, 240, 300, 360]
    assert not any(keepalive.Pacer(0).due(t) for t in range(0, 600, 5))
    assert keepalive.fmt_elapsed(125) == "2m 05s"
    assert "still working · 2m 05s" in keepalive.status_line("local · qwen", 125)


async def test_tool_call_progress_is_output_not_stall_and_beats_continue():
    async def gen():
        for i in range(12):                      # ~1.2s of tool-call streaming
            await asyncio.sleep(0.1)
            yield {"content": "", "done": False, "progress": {"tool_chars": 100 * i,
                                                              "tool": "write_to_file"}}
        yield {"content": "", "done": True, "tool_calls": [{"name": "write_to_file"}]}
    prog: dict = {}
    kinds, details = [], []
    async for kind, payload in keepalive.stream_with_heartbeat(gen(), hb=0.25, start=0,
                                                               stall=0.5, progress=prog):
        kinds.append(kind)
        if kind == "beat":
            details.append(keepalive.progress_detail(prog))
    assert kinds.count("beat") >= 3              # keep-alives kept flowing
    assert kinds[-1] == "chunk"                  # ...and no StreamStalled at 0.5s
    assert any("writing write_to_file" in d and "chars" in d for d in details)


def _sse(*events) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


async def test_openai_adapter_reports_tool_call_progress():
    body = _sse(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {
            "name": "write_to_file", "arguments": "{\"path\":"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
            "arguments": "\"a.lua\"}"}}]}, "finish_reason": "tool_calls"}]})
    seen = []

    def h(r):
        seen.append(r)
        return httpx.Response(200, text=body)
    request_context.capture({"x-request-id": "rid-42"})
    proto = OpenAIProtocol("http://llama:8080", None, httpx.AsyncClient(transport=httpx.MockTransport(h)))
    chunks = [c async for c in proto.chat_stream("qwen", [{"role": "user", "content": "x"}])]
    prog = [c for c in chunks if c.get("progress")]
    assert prog and prog[-1]["progress"]["tool_chars"] == len('{"path":"a.lua"}')
    assert chunks[-1]["tool_calls"][0]["name"] == "write_to_file"
    assert seen[0].headers["x-request-id"] == "rid-42"     # vLLM adopts it


async def test_anthropic_adapter_progress_and_client_output_cap():
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in [
        {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": "t1", "name": "write_to_file"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta", "partial_json": "{\"path\": \"a\"}"}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
         "usage": {"output_tokens": 9}},
        {"type": "message_stop"}])
    seen = []

    def h(r):
        seen.append(r)
        return httpx.Response(200, text=body)
    proto = AnthropicProtocol("http://m", None, httpx.AsyncClient(transport=httpx.MockTransport(h)))
    chunks = [c async for c in proto.chat_stream(
        "claude-sonnet-5", [{"role": "user", "content": "x"}],
        options={"num_predict": 1234}, max_tokens=8192)]
    assert any(c.get("progress", {}).get("tool") == "write_to_file" for c in chunks)
    assert json.loads(seen[0].content)["max_tokens"] == 1234      # client cap wins


def test_request_id_round_trip(app, client):
    r = client.post("/api/chat", headers={"X-Request-Id": "trace-abc"},
                    json={"model": "nope", "messages": []})
    assert r.headers["x-request-id"] == "trace-abc"
    r2 = client.get("/api/version")
    assert len(r2.headers["x-request-id"]) == 32              # generated when absent


def test_openai_stream_keepalive_is_sse_comment(app, client):
    from fastapi.responses import StreamingResponse
    from foundry_router.facade import translate as tr
    import foundry_router.facade.ollama_api as oa

    async def fake_dispatch(svc, body):
        async def gen():
            yield tr.chat_chunk("m", "")                       # invisible keep-alive
            yield tr.chat_chunk("m", "", thinking="⏳ still working · 30s\n")
            yield tr.chat_chunk("m", "hi")
            yield tr.chat_chunk("m", "", done=True, stats={"total_duration_ns": 1})
        return StreamingResponse(gen(), media_type="application/x-ndjson")
    orig, oa._chat_dispatch = oa._chat_dispatch, fake_dispatch
    try:
        r = client.post("/v1/chat/completions", json={"model": "m", "stream": True,
                                                      "messages": [{"role": "user", "content": "x"}]})
    finally:
        oa._chat_dispatch = orig
    assert ": keep-alive" in r.text
    assert "still working" in r.text and '"content": "hi"' in r.text


def test_streamed_reasoning_is_verbatim(app, client):
    from foundry_router.brain.agent import AgentEvent
    import foundry_router.facade.ollama_api as oa

    async def events(svc, ctx):
        for t in ("Let", " me", " think"):
            yield AgentEvent("think_raw", t)
        yield AgentEvent("keepalive", "")
        yield AgentEvent("answer", "ok")
    orig, oa._run_events = oa._run_events, events
    svc = app.state.services
    svc.personas.upsert("Chat", execution_mode="agent")
    try:
        r = client.post("/api/chat", json={"model": "Chat", "messages": [
            {"role": "user", "content": "hi"}]})
    finally:
        oa._run_events = orig
    lines = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    thinking = "".join(l["message"].get("thinking") or "" for l in lines)
    assert "Let me think" in thinking                      # not one token per line
