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
    if req.url.path.endswith("/embeddings"):
        body = json.loads(req.content)
        SEEN["llama_embed"] = body
        inp = body.get("input")
        n = len(inp) if isinstance(inp, list) else 1
        return httpx.Response(200, json={"data": [{"embedding": [0.5, -1.0, 2.0], "index": i}
                                                  for i in range(n)],
                                         "usage": {"prompt_tokens": 7}})
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
                # ends with a done chunk (answer from a failover model) or an
                # Ollama error line — never a torn stream or error-as-answer
                assert objs[-1].get("done") is True or "error" in objs[-1]
                assert not any("[router:" in (o.get("message") or {}).get("content", "")
                               for o in objs)
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


LLAMA_OVERFLOW = ('{"error":{"code":400,"message":"the request exceeds the available context '
                  'size, try increasing it","type":"exceed_context_size_error"}}')


@pytest.mark.parametrize("path", ["raw", "direct-stream", "direct-buffered"])
def test_context_overflow_is_a_recognisable_error_for_cline(wired, client, path):
    """llama.cpp's overflow must reach Cline as an ERROR whose text matches
    Cline's context-window patterns — it then compacts and retries itself."""
    import tests.test_passthrough_matrix as m
    orig = m._llama

    def full(req):
        if req.url.path.endswith("/models"):
            return orig(req)
        return httpx.Response(400, text=LLAMA_OVERFLOW)
    m._llama = full
    wired.config_store.config.agent_brain.direct_stream = path == "direct-stream"
    wired.personas.upsert("P-llm-only", execution_mode="direct", model_allowlist=["llm"],
                          pinned_models=[])
    name = "llm" if path == "raw" else "P-llm-only"
    import foundry_router.facade.ollama_api as oa
    orig_fo = oa._failover_list

    async def no_failover(svc, persona, first, *a, **k):
        return [first]
    oa._failover_list = no_failover
    try:
        r = client.post("/api/chat", json={"model": name, "messages": [
            {"role": "user", "content": "x"}]})
        objs = [json.loads(x) for x in r.text.splitlines() if x.strip()]
        err = objs[-1]["error"]
        assert err.startswith("context window exceeded:") and "available context size" in err
        assert not any(o.get("done") for o in objs)           # no done after the error
        o = client.post("/v1/chat/completions", json={"model": name, "stream": True,
                                                      "messages": [{"role": "user", "content": "x"}]})
        ev = [json.loads(l[5:]) for l in o.text.splitlines()
              if l.startswith("data:") and l.strip() != "data: [DONE]"]
        e = [x for x in ev if "error" in x][0]["error"]
        assert e["code"] == "context_length_exceeded"
        ns = client.post("/v1/chat/completions", json={"model": name,
                                                       "messages": [{"role": "user", "content": "x"}]})
        assert ns.status_code == 400 and ns.json()["error"]["code"] == "context_length_exceeded"
    finally:
        m._llama = orig
        oa._failover_list = orig_fo


def test_developer_role_is_treated_as_system():
    from foundry_router.facade.ollama_api import _canonical_messages
    out = _canonical_messages([{"role": "developer", "content": "rules"},
                               {"role": "user", "content": "hi"}])
    assert out[0]["role"] == "system"


def test_turn_timeline_records_client_tool_time(wired, client):
    """Turn 2 of a conversation records how long the client took (running the
    tool turn 1 asked for), which tool it was, and Foundry's own prep time."""
    import time as _t
    first = [{"role": "system", "content": "sys"}, {"role": "user", "content": "read a.lua"}]
    r = client.post("/api/chat", json={"model": "P-llm", "stream": True, "tools": TOOLS,
                                       "messages": first})
    assert r.status_code == 200
    _t.sleep(0.3)                                   # "Cline runs read_file"
    r = client.post("/api/chat", json={"model": "P-llm", "stream": True, "tools": TOOLS,
                                       "messages": first + [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": ARGS}}]},
        {"role": "tool", "content": "file body"}]})
    assert r.status_code == 200
    row = wired.db.query("SELECT client_gap_ms, client_tool, router_ms, prev_reply_at, "
                         "arrived_at, sent_at, first_token_at, clock_diff_ms FROM perf_samples "
                         "ORDER BY id DESC LIMIT 1")[0]
    assert row["client_gap_ms"] >= 300 and row["client_tool"] == "read_file"
    assert row["router_ms"] is not None and row["router_ms"] >= 0
    # system-clock timestamps, in order, and agreeing with the stopwatch
    from datetime import datetime
    ts = [datetime.fromisoformat(row[k].replace("Z", "+00:00"))
          for k in ("prev_reply_at", "arrived_at", "sent_at", "first_token_at")]
    assert ts == sorted(ts)
    assert abs((ts[1] - ts[0]).total_seconds() * 1000 - row["client_gap_ms"]) < 100
    assert row["clock_diff_ms"] is not None and row["clock_diff_ms"] < 100
    thinking = "".join(json.loads(l)["message"].get("thinking") or ""
                       for l in r.text.splitlines() if l.strip())
    assert " UTC]" in thinking and "arrived " in thinking


def test_disabled_backend_is_not_probed_routed_or_alerted(app, client):
    svc = app.state.services
    hits = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: (hits.append(r.url.host), _router(r))[1]))
    pool = InternalPool([
        BackendConfig(name="llama", type="openai-compatible", url="http://llm-host", flavor="llamacpp"),
        BackendConfig(name="ollama", type="ollama", url="http://olm-host", enabled=False)],
        BackendPoolConfig(), http, svc.db)
    import asyncio
    asyncio.run(pool.check_all())
    assert "olm-host" not in hits and "ollama" not in pool.backends
    st = {b["name"]: b for b in pool.backend_status()}
    assert st["ollama"]["enabled"] is False and st["llama"]["enabled"] is True
    assert pool._candidates("olm") == []
    from foundry_router import perf_advisor

    class S:
        pass
    s = S(); s.pool = pool
    assert not [f for f in perf_advisor._backend_rules(s) if f["scope"] == "ollama"]


def test_enable_toggle_endpoint(app, client):
    r = client.post("/admin/api/config/backends", json=[
        {"name": "b1", "type": "ollama", "url": "http://olm-host"}])
    assert r.status_code == 200
    r = client.post("/admin/api/backends/enabled", json={"name": "b1", "enabled": False})
    assert r.status_code == 200
    b = next(x for x in r.json()["backends"] if x["name"] == "b1")
    assert b["enabled"] is False
    assert app.state.services.config_store.config.backend_pool.internal.backends[0].enabled is False
    r = client.post("/admin/api/backends/enabled", json={"name": "b1", "enabled": True})
    assert next(x for x in r.json()["backends"] if x["name"] == "b1")["enabled"] is True
    assert client.post("/admin/api/backends/enabled",
                       json={"name": "nope", "enabled": True}).status_code == 404


def test_cline_compaction_summary_gets_thinking_off(wired, client):
    """Cline's compaction summarizer must reach the backend with thinking off
    (else reasoning eats its 8k output budget -> 'Compaction skipped'), and
    the outcome is logged."""
    r = client.post("/api/chat", json={"model": "P-llm", "stream": True, "messages": [
        {"role": "system", "content": "Summarize the provided coding session into a concise "
                                      "continuation note with detailed next steps."},
        {"role": "user", "content": "<conversation>…</conversation>"}]})
    assert r.status_code == 200
    body = SEEN["llama"]
    assert body.get("chat_template_kwargs", {}).get("enable_thinking") is False
    assert "reasoning_effort" not in body
    thinking = "".join(json.loads(l)["message"].get("thinking") or ""
                       for l in r.text.splitlines() if l.strip())
    assert "compaction summary request" in thinking
    ev = wired.db.query("SELECT message FROM event_log WHERE source='compaction' "
                        "ORDER BY id DESC LIMIT 1")
    assert ev and "chars of summary" in ev[0]["message"]


def test_normal_turn_is_not_treated_as_summary(wired, client):
    client.post("/api/chat", json={"model": "P-llm", "stream": True, "tools": TOOLS,
                                   "messages": [{"role": "system", "content": "You are Cline"},
                                                {"role": "user", "content": "read a.lua"}]})
    assert "enable_thinking" not in (SEEN["llama"].get("chat_template_kwargs") or {})


_SUMMARY = [{"role": "system", "content": "Summarize the provided coding session into a "
                                          "concise continuation note with detailed next steps."},
            {"role": "user", "content": "<conversation>…</conversation>"}]


@pytest.mark.parametrize("setup", ["persona_force_high", "global_high", "client_think_true",
                                   "agent_mode_persona", "raw_model"])
def test_summary_thinking_off_beats_every_thinking_setting(wired, client, setup):
    model = "P-llm"
    body = {"stream": True, "messages": _SUMMARY}
    if setup == "persona_force_high":
        wired.personas.upsert("P-llm", reasoning_effort="high", force_reasoning_effort=True)
    elif setup == "global_high":
        wired.config_store.config.agent_brain.reasoning_effort = "high"
    elif setup == "client_think_true":
        body["think"] = True
    elif setup == "agent_mode_persona":
        wired.personas.upsert("P-agent", execution_mode="agent",
                              model_allowlist=["llm"], pinned_models=[])
        model = "P-agent"
    elif setup == "raw_model":
        model = "llm"
    try:
        r = client.post("/api/chat", json={"model": model, **body})
    finally:
        wired.config_store.config.agent_brain.reasoning_effort = None
    assert r.status_code == 200, r.text
    sent = SEEN["llama"]
    assert sent.get("chat_template_kwargs", {}).get("enable_thinking") is False, sent
    assert "reasoning_effort" not in sent


def test_forced_and_global_settings_still_apply_to_normal_turns(wired, client):
    turn = {"model": "P-llm", "stream": True, "tools": TOOLS,
            "messages": [{"role": "user", "content": "read a.lua"}]}
    # persona force OFF beats a client asking for thinking — even on a model
    # whose family Foundry doesn't recognise
    wired.personas.upsert("P-llm", reasoning_effort="off", force_reasoning_effort=True)
    client.post("/api/chat", json={**turn, "think": True})
    assert SEEN["llama"].get("chat_template_kwargs", {}).get("enable_thinking") is False
    # global OFF applies when neither persona nor client set anything
    wired.personas.upsert("P-llm", reasoning_effort=None, force_reasoning_effort=False)
    wired.config_store.config.agent_brain.reasoning_effort = "off"
    try:
        client.post("/api/chat", json=turn)
    finally:
        wired.config_store.config.agent_brain.reasoning_effort = None
    assert SEEN["llama"].get("chat_template_kwargs", {}).get("enable_thinking") is False
    # nothing set -> model default (no field)
    client.post("/api/chat", json=turn)
    assert "enable_thinking" not in (SEEN["llama"].get("chat_template_kwargs") or {})


def test_forced_level_reaches_a_recognised_thinking_model():
    from foundry_router import thinking
    q = "/cache/Qwen3.8-27B-UD-Q5_K_XL-fixed-template.gguf"
    assert thinking.think_value("high", q, None, "openai-compatible") == "high"
    assert thinking.think_value("off", q, None, "openai-compatible") is False
    assert thinking.think_value("off", "some-unknown-model", None, "openai-compatible") is False
    assert thinking.think_value("off", "some-unknown-model", [], "ollama") is None


def test_guard_leaves_room_for_cline_to_compact(wired, client):
    """With a 262k window the guard must not trim a ~240k conversation (Cline
    compacts at 90% = ~236k); it only trims what truly won't fit."""
    from foundry_router import context_guard
    from foundry_router.facade.ollama_api import _apply_context_guard
    wired.personas.upsert("P-llm", context_window=262144, max_output_tokens=32768)
    persona = wired.personas.get("P-llm")
    big = [{"role": "system", "content": "s"}, {"role": "user", "content": "task"}] + [
        {"role": "user" if i % 2 else "assistant", "content": "x" * 32000} for i in range(24)]
    est = context_guard.estimate(big, None, "llm")
    assert 235_000 < est < 245_000
    out, note = _apply_context_guard(wired, persona, "llm", big, None, None, None, 32768)
    assert note == "" and out is big
    huge = big + [{"role": "user", "content": "x" * 64000}]
    out, note = _apply_context_guard(wired, persona, "llm", huge, None, None, None, 32768)
    assert note and context_guard.estimate(out, None, "llm") < 262144 - 8192


def test_openai_client_images_and_reasoning_effort_reach_llamacpp(wired, client):
    """Cline's OpenAI Compatible provider: image_url data URIs and
    reasoning_effort must arrive at llama.cpp intact."""
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    wired.personas.upsert("P-llm", reasoning_effort=None, force_reasoning_effort=False)
    r = client.post("/v1/chat/completions", json={
        "model": "P-llm", "stream": True, "stream_options": {"include_usage": True},
        "reasoning_effort": "high", "tools": TOOLS, "max_tokens": 32768,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "what is in this image?"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}}]}]})
    assert r.status_code == 200
    sent = SEEN["llama"]
    parts = sent["messages"][-1]["content"]
    assert any(p.get("type") == "image_url" and png in p["image_url"]["url"] for p in parts)
    # reasoning effort is honoured (when the family is recognised it's sent as
    # reasoning_effort; 'llm' is a test name, so only check it isn't forced off)
    assert (sent.get("chat_template_kwargs") or {}).get("enable_thinking") is not False
    events = [json.loads(l[5:]) for l in r.text.splitlines()
              if l.startswith("data:") and l.strip() != "data: [DONE]"]
    assert any(e.get("usage") for e in events)                    # include_usage honoured
    assert any((e.get("choices") or [{}])[0].get("delta", {}).get("reasoning_content")
               for e in events)                                   # thinking streamed



# -- full API surface: OpenAI + Ollama endpoints ---------------------------------

def test_openai_embeddings_float_and_base64(wired, client):
    import base64, struct
    r = client.post("/v1/embeddings", json={"model": "llm", "input": ["a", "b"]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["object"] == "list" and len(d["data"]) == 2
    assert d["data"][1]["index"] == 1 and d["data"][0]["embedding"] == [0.5, -1.0, 2.0]
    assert d["usage"]["prompt_tokens"] == 7
    r = client.post("/v1/embeddings", json={"model": "llm", "input": "a",
                                            "encoding_format": "base64"})
    raw = base64.b64decode(r.json()["data"][0]["embedding"])
    assert list(struct.unpack("<3f", raw)) == [0.5, -1.0, 2.0]
    assert client.post("/v1/embeddings", json={"model": "nope", "input": "a"}).status_code == 404
    assert client.post("/v1/embeddings", json={"model": "llm"}).status_code == 400


def test_openai_legacy_completions(wired, client):
    r = client.post("/v1/completions", json={"model": "P-llm", "prompt": "say hi",
                                             "max_tokens": 50})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["object"] == "text_completion" and d["choices"][0]["text"] == "Hello"
    assert "usage" in d and d["choices"][0]["finish_reason"] in ("stop", "length")
    assert SEEN["llama"]["messages"][-1]["content"] == "say hi"
    r = client.post("/v1/completions", json={"model": "P-llm", "prompt": "say hi",
                                             "stream": True})
    events = [json.loads(l[5:]) for l in r.text.splitlines()
              if l.startswith("data:") and l.strip() != "data: [DONE]"]
    assert "".join(e["choices"][0]["text"] for e in events if e.get("choices")) == "Hello"
    assert r.text.rstrip().endswith("data: [DONE]")


def _resp_events(text):
    out = []
    for block in text.split("\n\n"):
        lines = [l for l in block.splitlines() if l.startswith("data:")]
        if lines:
            out.append(json.loads(lines[0][5:]))
    return out


def test_openai_responses_non_stream_with_tools_and_history(wired, client):
    r = client.post("/v1/responses", json={
        "model": "P-llm", "instructions": "be brief",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "read a.lua"}]},
                  {"type": "function_call", "call_id": "call_1", "name": "read_file",
                   "arguments": "{\"path\": \"a.lua\"}"},
                  {"type": "function_call_output", "call_id": "call_1", "output": "file body"},
                  {"role": "user", "content": "again"}],
        "tools": [{"type": "function", "name": "read_file", "description": "read",
                   "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}},
                  {"type": "web_search"}],
        "reasoning": {"effort": "high"}, "max_output_tokens": 1000})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["object"] == "response" and d["status"] == "completed"
    kinds = [o["type"] for o in d["output"]]
    assert kinds == ["reasoning", "message", "function_call"]
    assert d["output"][1]["content"][0]["text"] == "Hello" and d["output_text"] == "Hello"
    fc = d["output"][2]
    assert fc["name"] == "read_file" and json.loads(fc["arguments"]) == {"path": "a.lua"}
    assert d["usage"]["input_tokens"] > 0
    sent = SEEN["llama"]
    roles = [m["role"] for m in sent["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "user"]
    assert sent["messages"][3]["tool_call_id"] == sent["messages"][2]["tool_calls"][0]["id"]
    assert [t["function"]["name"] for t in sent["tools"]] == ["read_file"]   # hosted tool dropped


def test_openai_responses_stream_events(wired, client):
    r = client.post("/v1/responses", json={"model": "P-llm", "input": "read a.lua",
                                           "stream": True, "tools": [
        {"type": "function", "name": "read_file", "parameters": {"type": "object"}}]})
    evs = _resp_events(r.text)
    types = [e["type"] for e in evs]
    assert types[0] == "response.created" and types[-1] == "response.completed"
    assert "response.reasoning_summary_text.delta" in types
    assert "response.output_text.delta" in types
    assert "response.function_call_arguments.done" in types
    seqs = [e["sequence_number"] for e in evs]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    done = evs[-1]["response"]
    assert [o["type"] for o in done["output"]] == ["reasoning", "message", "function_call"]
    text = "".join(e["delta"] for e in evs if e["type"] == "response.output_text.delta")
    assert text == "Hello"


def test_openai_responses_refuses_previous_response_id(wired, client):
    r = client.post("/v1/responses", json={"model": "P-llm", "input": "x",
                                           "previous_response_id": "resp_1"})
    assert r.status_code == 400 and r.json()["error"]["param"] == "previous_response_id"


def test_ollama_root_probe_and_management_endpoints(wired, client):
    r = client.get("/")
    assert r.status_code == 200 and r.text == "Ollama is running"
    assert client.head("/").status_code == 200
    r = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/ui"
    r = client.post("/api/pull", json={"model": "P-llm", "stream": False})
    assert r.status_code == 200 and r.json()["status"] == "success"
    r = client.post("/api/pull", json={"model": "P-llm"})
    assert json.loads(r.text.strip())["status"] == "success"
    assert client.post("/api/pull", json={"model": "nope"}).status_code == 404
    for method, path in (("post", "/api/create"), ("post", "/api/copy"),
                         ("delete", "/api/delete"), ("post", "/api/push")):
        r = getattr(client, method)(path, json={"model": "x"}) if method == "post" \
            else client.request("DELETE", path, json={"model": "x"})
        assert r.status_code == 400 and "Foundry" in r.json()["error"]


def test_ollama_embed_endpoints_still_work(wired, client):
    r = client.post("/api/embed", json={"model": "llm", "input": ["a"]})
    assert r.status_code == 200 and r.json()["embeddings"] == [[0.5, -1.0, 2.0]]
    r = client.post("/api/embeddings", json={"model": "llm", "prompt": "a"})
    assert r.json()["embedding"] == [0.5, -1.0, 2.0]
