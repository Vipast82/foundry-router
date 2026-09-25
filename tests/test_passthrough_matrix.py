"""End-to-end passthrough matrix: every client format x routing path x backend.

Fake Ollama, llama.cpp (OpenAI dialect) and Meridian (Anthropic Messages)
backends each answer with reasoning + text + a tool call + usage. Every
combination of

  client : Ollama /api/chat (stream / non-stream), OpenAI /v1/chat/completions
           (stream / non-stream)
  path   : raw model by name, persona direct (live streaming), persona direct
           (buffered)
  backend: ollama, llama.cpp, meridian

must hand the client the thinking, the text, the tool call (name, arguments,
id) and a done/finish reason — and every backend must receive the client's
tools and a correctly paired tool-call history (ids matched, tool names set).
"""

import json

import httpx
import pytest

from foundry_router.config import BackendConfig, BackendPoolConfig
from foundry_router.pool.internal import InternalPool

TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
ARGS = {"path": "a.lua"}
SEEN: dict = {}


def _ollama(req: httpx.Request):
    if req.url.path == "/api/tags":
        return httpx.Response(200, json={"models": [{"name": "olm"}]})
    body = json.loads(req.content)
    SEEN["ollama"] = body
    tc = [{"function": {"name": "read_file", "arguments": ARGS}}]
    done = {"model": "olm", "done": True, "done_reason": "stop", "prompt_eval_count": 11,
            "eval_count": 7, "eval_duration": 700_000_000, "message": {"role": "assistant", "content": ""}}
    if body.get("stream"):
        lines = [{"message": {"role": "assistant", "content": "", "thinking": "T-olm"}, "done": False},
                 {"message": {"role": "assistant", "content": "Hello"}, "done": False},
                 {"message": {"role": "assistant", "content": "", "tool_calls": tc}, "done": False},
                 done]
        return httpx.Response(200, text="\n".join(json.dumps(x) for x in lines) + "\n")
    return httpx.Response(200, json={**done, "message": {"role": "assistant", "content": "Hello",
                                                         "thinking": "T-olm", "tool_calls": tc}})


def _sse(frames) -> str:
    return "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"


def _llama(req: httpx.Request):
    if req.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "llm"}]})
    body = json.loads(req.content)
    SEEN["llama"] = body
    if body.get("stream"):
        return httpx.Response(200, text=_sse([
            {"choices": [{"delta": {"reasoning_content": "T-llm"}}]},
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_L1", "function": {
                "name": "read_file", "arguments": "{\"path\":"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
                "arguments": " \"a.lua\"}"}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 8}}]))
    return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "Hello", "reasoning_content": "T-llm",
        "tool_calls": [{"id": "call_L1", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps(ARGS)}}]}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8}})


def _meridian(req: httpx.Request):
    p = req.url.path
    if p.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "claude-sonnet-5"}]})
    if "quota" in p or p.endswith("/health"):
        return httpx.Response(200, json={"buckets": []})
    body = json.loads(req.content)
    SEEN["meridian"] = body
    if body.get("stream"):
        evs = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 13}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "T-mer"}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hello"}},
            {"type": "content_block_start", "index": 2, "content_block": {
                "type": "tool_use", "id": "toolu_M1", "name": "read_file"}},
            {"type": "content_block_delta", "index": 2, "delta": {
                "type": "input_json_delta", "partial_json": "{\"path\": "}},
            {"type": "content_block_delta", "index": 2, "delta": {
                "type": "input_json_delta", "partial_json": "\"a.lua\"}"}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 9}},
            {"type": "message_stop"}]
        return httpx.Response(200, text="".join(
            f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in evs))
    return httpx.Response(200, json={
        "content": [{"type": "thinking", "thinking": "T-mer"}, {"type": "text", "text": "Hello"},
                    {"type": "tool_use", "id": "toolu_M1", "name": "read_file", "input": ARGS}],
        "stop_reason": "tool_use", "usage": {"input_tokens": 13, "output_tokens": 9}})


def _router(req):
    host = req.url.host
    return {"olm-host": _ollama, "llm-host": _llama, "mer-host": _meridian}[host](req)


BACKENDS = {"ollama": "olm", "llama": "llm", "meridian": "claude-sonnet-5"}


@pytest.fixture()
def wired(app, client):
    svc = app.state.services
    http = httpx.AsyncClient(transport=httpx.MockTransport(_router))
    pool = InternalPool([
        BackendConfig(name="ollama", type="ollama", url="http://olm-host"),
        BackendConfig(name="llama", type="openai-compatible", url="http://llm-host", flavor="llamacpp"),
        BackendConfig(name="meridian", type="anthropic-compatible", url="http://mer-host")],
        BackendPoolConfig(), http, svc.db)
    for name, model in BACKENDS.items():
        s = pool.backends[name]
        s.healthy, s.ever_checked, s.models = True, True, [model]
    real, svc.pool = svc.pool, pool
    svc.meridian_usage.client = http
    for model in BACKENDS.values():
        svc.personas.upsert(f"P-{model}", execution_mode="direct",
                            model_allowlist=[model], pinned_models=[])
    yield svc
    svc.pool = real


def _ollama_client(client, model, stream):
    r = client.post("/api/chat", json={"model": model, "stream": stream, "tools": TOOLS,
                                       "messages": [{"role": "user", "content": "read a.lua"}]})
    assert r.status_code == 200, r.text
    objs = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    thinking = "".join(o["message"].get("thinking") or "" for o in objs)
    content = "".join(o["message"].get("content") or "" for o in objs)
    tcs = [tc for o in objs for tc in (o["message"].get("tool_calls") or [])]
    done = objs[-1]
    assert done["done"] is True
    return thinking, content, [(t["function"]["name"], t["function"]["arguments"], t.get("id"))
                               for t in tcs], done


def _openai_client(client, model, stream):
    r = client.post("/v1/chat/completions", json={
        "model": model, "stream": stream, "tools": TOOLS,
        "messages": [{"role": "user", "content": "read a.lua"}]})
    assert r.status_code == 200, r.text
    if not stream:
        d = r.json()
        m = d["choices"][0]["message"]
        return (m.get("reasoning_content") or "", m.get("content") or "",
                [(t["function"]["name"], json.loads(t["function"]["arguments"]), t.get("id"))
                 for t in m.get("tool_calls") or []], d["choices"][0])
    events = [json.loads(l[5:]) for l in r.text.splitlines()
              if l.startswith("data:") and l.strip() != "data: [DONE]"]
    deltas = [e["choices"][0]["delta"] for e in events if e.get("choices")]
    fin = [e["choices"][0] for e in events if e.get("choices") and e["choices"][0].get("finish_reason")][-1]
    tcs = [t for d in deltas for t in (d.get("tool_calls") or [])]
    return ("".join(d.get("reasoning_content") or "" for d in deltas),
            "".join(d.get("content") or "" for d in deltas),
            [(t["function"]["name"], json.loads(t["function"]["arguments"]), t.get("id")) for t in tcs],
            fin)


@pytest.mark.parametrize("backend", list(BACKENDS))
@pytest.mark.parametrize("path", ["raw", "direct-stream", "direct-buffered"])
@pytest.mark.parametrize("fmt,stream", [("ollama", True), ("ollama", False),
                                        ("openai", True), ("openai", False)])
def test_thinking_content_tool_calls_reach_client(wired, client, backend, path, fmt, stream):
    model = BACKENDS[backend]
    cfg = wired.config_store.config.agent_brain
    cfg.direct_stream = path == "direct-stream"
    name = model if path == "raw" else f"P-{model}"
    fn = _ollama_client if fmt == "ollama" else _openai_client
    thinking, content, tcs, fin = fn(client, name, stream)
    tag = {"ollama": "T-olm", "llama": "T-llm", "meridian": "T-mer"}[backend]
    assert tag in thinking, f"thinking lost: {thinking!r}"
    assert content == "Hello", f"content: {content!r}"
    assert len(tcs) == 1, f"tool calls: {tcs}"
    name_, args, tid = tcs[0]
    assert name_ == "read_file" and args == ARGS
    assert tid, "tool call id missing"
    want_ct = {"ollama": 7, "llama": 8, "meridian": 9}[backend]
    if fmt == "openai":
        assert fin["finish_reason"] == "tool_calls"
    else:
        assert fin["eval_count"] == want_ct, f"eval_count {fin.get('eval_count')}"
        assert fin["prompt_eval_count"] > 0
        assert (fin.get("foundry") or {}).get("served_by") == model
        assert (fin.get("foundry") or {}).get("backend") == backend
    # the backend got the client's tools
    sent = SEEN[backend]
    tool_names = [t.get("name") or (t.get("function") or {}).get("name") for t in sent.get("tools") or []]
    assert "read_file" in tool_names


HISTORY = [
    {"role": "user", "content": "read a.lua and b.lua"},
    # Cline over the Ollama API: tool calls and results WITHOUT ids
    {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "read_file", "arguments": {"path": "a.lua"}}},
        {"function": {"name": "read_file", "arguments": {"path": "b.lua"}}}]},
    {"role": "tool", "tool_name": "read_file", "content": "AAA"},
    {"role": "tool", "tool_name": "read_file", "content": "BBB"},
    {"role": "user", "content": "now summarize"},
]


@pytest.mark.parametrize("backend", list(BACKENDS))
def test_tool_history_pairs_ids_for_every_backend(wired, client, backend):
    wired.config_store.config.agent_brain.direct_stream = True
    r = client.post("/api/chat", json={"model": f"P-{BACKENDS[backend]}", "tools": TOOLS,
                                       "messages": HISTORY})
    assert r.status_code == 200
    sent = SEEN[backend]
    if backend == "ollama":
        tools = [m for m in sent["messages"] if m["role"] == "tool"]
        assert [m.get("tool_name") for m in tools] == ["read_file", "read_file"]
        assert [m["content"] for m in tools] == ["AAA", "BBB"]
    elif backend == "llama":
        a = next(m for m in sent["messages"] if m.get("tool_calls"))
        ids = [tc["id"] for tc in a["tool_calls"]]
        res = [m["tool_call_id"] for m in sent["messages"] if m["role"] == "tool"]
        assert ids == res and len(set(ids)) == 2
    else:
        a = next(m for m in sent["messages"] if m["role"] == "assistant")
        uses = [b["id"] for b in a["content"] if b["type"] == "tool_use"]
        nxt = sent["messages"][sent["messages"].index(a) + 1]
        assert nxt["role"] == "user"                    # ONE user turn after tool_use
        results = [b["tool_use_id"] for b in nxt["content"] if b["type"] == "tool_result"]
        assert uses == results and len(set(uses)) == 2
        assert any(b.get("type") == "text" and "summarize" in b["text"] for b in nxt["content"])
        roles = [m["role"] for m in sent["messages"]]
        assert all(x != y for x, y in zip(roles, roles[1:]))   # strictly alternating


def test_same_history_same_ids_every_turn():
    from foundry_router.pool.protocols import pair_tool_call_ids
    a = pair_tool_call_ids(HISTORY)
    b = pair_tool_call_ids(json.loads(json.dumps(HISTORY)))
    assert [tc["id"] for tc in a[1]["tool_calls"]] == [tc["id"] for tc in b[1]["tool_calls"]]
    assert a[2]["tool_call_id"] == a[1]["tool_calls"][0]["id"]
    assert a[3]["tool_call_id"] == a[1]["tool_calls"][1]["id"]


def test_openai_usage_reaches_client(wired, client):
    wired.config_store.config.agent_brain.direct_stream = True
    for model, ct in (("llm", 8), ("claude-sonnet-5", 9), ("olm", 7)):
        d = client.post("/v1/chat/completions", json={
            "model": f"P-{model}", "tools": TOOLS,
            "messages": [{"role": "user", "content": "x"}]}).json()
        assert d["usage"]["completion_tokens"] == ct and d["usage"]["prompt_tokens"] > 0
        r = client.post("/v1/chat/completions", json={
            "model": model, "stream": True, "stream_options": {"include_usage": True},
            "tools": TOOLS, "messages": [{"role": "user", "content": "x"}]})
        ev = [json.loads(l[5:]) for l in r.text.splitlines()
              if l.startswith("data:") and l.strip() != "data: [DONE]"]
        assert [e for e in ev if not e.get("choices")][-1]["usage"]["completion_tokens"] == ct


@pytest.mark.parametrize("path", ["raw", "direct-stream", "direct-buffered"])
@pytest.mark.parametrize("fmt", ["ollama", "openai"])
def test_backend_error_reaches_client_cleanly(wired, client, path, fmt):
    """A backend 500 must end the client's stream with a readable error — never
    a torn connection or a silent empty answer."""
    import tests.test_passthrough_matrix as m
    orig = m._llama

    def broken(req):
        if req.url.path.endswith("/models"):
            return orig(req)
        return httpx.Response(500, text="CUDA error: out of memory")
    m._llama = broken
    wired.config_store.config.agent_brain.direct_stream = path == "direct-stream"
    name = "llm" if path == "raw" else "P-llm"
    try:
        if fmt == "ollama":
            r = client.post("/api/chat", json={"model": name, "messages": [
                {"role": "user", "content": "x"}]})
            text = r.text
            assert r.status_code in (200, 502)
            if r.status_code == 200:
                objs = [json.loads(x) for x in text.splitlines() if x.strip()]
                assert objs[-1]["done"] is True
        else:
            r = client.post("/v1/chat/completions", json={"model": name, "stream": True,
                                                          "messages": [{"role": "user", "content": "x"}]})
            text = r.text
            assert r.status_code in (200, 502)
            if r.status_code == 200:
                assert text.rstrip().endswith("data: [DONE]")
    finally:
        m._llama = orig
    # the reason reaches the client: as the error, or on the failover line
    assert "out of memory" in text, text[:400]


def test_generate_carries_thinking(wired, client):
    r = client.post("/api/generate", json={"model": "olm", "prompt": "hi"})
    objs = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    assert "T-olm" in "".join(o.get("thinking") or "" for o in objs)
    assert "Hello" in "".join(o.get("response") or "" for o in objs)
    assert objs[-1]["done"] and objs[-1]["eval_count"] == 7
