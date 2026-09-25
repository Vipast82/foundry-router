"""MCP tool passthrough end to end: full-fidelity tool results, pooled sessions,
per-call metrics with attribution, aggregator annotations / rich content /
persona endpoints, persona MCP tools merged into direct mode, tool_choice
translation, malformed-argument handling, metrics + context budget APIs."""

import json
import time
import types
from contextlib import asynccontextmanager

import httpx
import pytest

from foundry_router import mcp_metrics, request_context
from foundry_router.config import MCPServerConfig
from foundry_router.db import Database
from foundry_router.pool.protocols import (AnthropicProtocol, ChatResult, OllamaProtocol,
                                           OpenAIProtocol, _anthropic_tool_choice, _tool_call)
from foundry_router.tools.mcp_client import MCPManager, ToolResult


# -- ToolResult ------------------------------------------------------------------------

def _res(content, structured=None, is_error=False):
    return types.SimpleNamespace(content=content, structuredContent=structured, isError=is_error)


def test_tool_result_keeps_every_block():
    r = ToolResult.from_mcp(_res([
        types.SimpleNamespace(type="text", text="caption"),
        types.SimpleNamespace(type="image", data="aGVsbG8=" * 100, mimeType="image/png"),
        types.SimpleNamespace(type="audio", data="AAAA", mimeType="audio/wav"),
        types.SimpleNamespace(type="resource", resource=types.SimpleNamespace(
            uri="file:///a.txt", mimeType="text/plain", text="file body", blob=None)),
    ]))
    assert r.content_types == ["text", "image", "audio", "resource"]
    assert r.images() and "[image: image/png" in r.text and "file body" in r.text
    s = ToolResult.from_mcp(_res([], structured={"temp": 21}))
    assert json.loads(s.text) == {"temp": 21} and "structured" in s.content_types


# -- MCPManager: pooled sessions + metrics ---------------------------------------------

class _Sess:
    def __init__(self, fail_first=False):
        self.calls, self.fail_first = 0, fail_first

    async def call_tool(self, tool, args, **kw):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            raise RuntimeError("stream closed")
        return _res([types.SimpleNamespace(type="text", text=f"ok {tool}")])


def _mgr(tmp_path, fail_first=False, persistent=True):
    db = Database(tmp_path / "m.sqlite")
    mgr = MCPManager([MCPServerConfig(name="s", url="http://x",
                                      persistent_session=persistent)], db)
    opened = {"n": 0}
    sess = _Sess(fail_first)

    @asynccontextmanager
    async def fake_session(name):
        opened["n"] += 1
        yield sess

    mgr._session = fake_session
    return mgr, db, opened, sess


async def test_pooled_session_is_reused_and_logged(tmp_path):
    mgr, db, opened, _ = _mgr(tmp_path)
    tok = request_context.set_mcp_attribution("aggregator", "persona:Foundry-Research", "AnythingLLM/1.16")
    try:
        assert await mgr.call_tool("s", "t", {"q": "x"}) == "ok t"
        assert await mgr.call_tool("s", "t", {"q": "y"}) == "ok t"
    finally:
        request_context.reset_mcp_attribution(tok)
    assert opened["n"] == 1                                  # one handshake, two calls
    rows = db.query("SELECT * FROM mcp_call_log ORDER BY id")
    assert [r["session"] for r in rows] == ["new", "reused"]
    assert rows[0]["source"] == "aggregator" and rows[0]["client"] == "AnythingLLM/1.16"
    assert rows[0]["ok"] == 1 and rows[0]["result_tokens"] >= 1
    await mgr.close_sessions()


async def test_stale_pooled_session_retries_fresh(tmp_path):
    mgr, db, opened, sess = _mgr(tmp_path)
    await mgr.call_tool("s", "t", {})                       # opens the pool (call 1 ok)
    sess.fail_first, sess.calls = True, 0                   # the reused session now breaks once
    assert await mgr.call_tool("s", "t", {}) == "ok t"      # retried on a fresh session
    assert opened["n"] == 2
    await mgr.close_sessions()


async def test_failed_call_logged_with_error(tmp_path):
    mgr, db, _, sess = _mgr(tmp_path, persistent=False)

    async def boom(tool, args, **kw):
        return _res([types.SimpleNamespace(type="text", text="bad input")], is_error=True)
    sess.call_tool = boom
    with pytest.raises(RuntimeError):
        await mgr.call_tool("s", "t", {})
    r = db.query("SELECT * FROM mcp_call_log")[0]
    assert r["ok"] == 0 and "bad input" in r["error"] and r["session"] == "per-call"


def test_metrics_summary_and_clear(tmp_path):
    db = Database(tmp_path / "x.sqlite")
    for i, (ok, ms) in enumerate([(1, 100), (1, 300), (0, 900)]):
        db.execute("INSERT INTO mcp_call_log (ts, source, caller, server, tool, ok, duration_ms, "
                   "result_tokens, session) VALUES (datetime('now'),'direct','Cline',?,?,?,?,?,?)",
                   ("searxng", "search", ok, ms, 500, "reused"))
    s = mcp_metrics.summary(db, hours=1)
    assert s["totals"]["calls"] == 3 and s["totals"]["errors"] == 1
    assert s["by_tool"][0]["p50_ms"] == 300 and s["by_tool"][0]["max_ms"] == 900
    assert s["totals"]["reuse_pct"] == 100.0
    assert s["by_source"][0]["source"] == "direct"
    assert mcp_metrics.clear(db, server="searxng") == 3


# -- protocols: tool_choice + malformed arguments ---------------------------------------

_SEEN: list = []


def _cap(handler):
    def h(r):
        _SEEN.append(json.loads(r.content))
        return handler(r)
    return httpx.AsyncClient(transport=httpx.MockTransport(h))


TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {}}}]


async def test_tool_choice_translated_per_backend():
    ok = lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "x"}}], "usage": {}})
    await OpenAIProtocol("http://x", None, _cap(ok), flavor="vllm").chat(
        "m", [{"role": "user", "content": "hi"}], tools=TOOLS,
        options={"tool_choice": "required", "parallel_tool_calls": False})
    assert _SEEN[-1]["tool_choice"] == "required" and _SEEN[-1]["parallel_tool_calls"] is False
    assert _anthropic_tool_choice({"type": "function", "function": {"name": "t"}}, False) == \
        {"type": "tool", "name": "t", "disable_parallel_tool_use": True}
    assert _anthropic_tool_choice("required", None) == {"type": "any"}
    assert _anthropic_tool_choice("auto", None) is None
    body = AnthropicProtocol("http://m", None, None)._payload(
        "claude", [{"role": "user", "content": "hi"}], TOOLS,
        {"tool_choice": "required"}, 1024, "high", None)
    assert body["tool_choice"] == {"type": "auto"}          # forced choice + thinking -> auto
    body = OllamaProtocol("http://o", None, None)._payload(
        "m", [{"role": "user", "content": "hi"}], TOOLS,
        {"tool_choice": "required", "temperature": 0.2}, None, False)
    assert "tool_choice" not in body["options"] and body["options"]["temperature"] == 0.2


def test_malformed_arguments_are_flagged():
    tc = _tool_call("id", "t", '{"path": "a.py"')
    assert tc["arguments"] == {} and tc["arguments_error"].startswith('{"path"')
    assert "arguments_error" not in _tool_call("id", "t", '{"a": 1}')


# -- aggregator ------------------------------------------------------------------------

def test_aggregator_relays_annotations_and_images():
    from foundry_router.facade.mcp_aggregator import MCPAggregator
    from foundry_router.tools.sync import ToolDef
    agg = MCPAggregator(types.SimpleNamespace())
    td = ToolDef(name="fs__write", kind="mcp", description="write", parameters={},
                 server="fs", mcp_tool="write", destructive=True,
                 annotations={"title": "Write file", "idempotentHint": False})
    tool = agg._tool_meta(td)
    assert tool.annotations.destructiveHint is True and tool.annotations.title == "Write file"
    content = agg._to_mcp_content(ToolResult([{"type": "text", "text": "saved"},
                                              {"type": "image", "data": "aGk=", "mimeType": "image/png"}]))
    assert [c.type for c in content] == ["text", "image"]


# -- direct mode: persona MCP tools merged with client tools -----------------------------

class ToolLoopPool:
    """Turn 1 calls the persona's MCP tool, turn 2 calls the CLIENT's tool."""
    def __init__(self):
        self.seen_tools = []
        self.convos = []

    def available_models(self):
        return {"local-a": ["o1"]}

    def backend_info(self, m):
        return {"name": "o1", "type": "ollama", "url": "http://o"} if m == "local-a" else None

    async def loaded_models(self):
        return set()

    def active_calls(self):
        return []

    async def chat(self, model, messages, tools=None, **kw):
        self.seen_tools.append([t["function"]["name"] for t in tools or []])
        self.convos.append(messages)
        if not any(m.get("role") == "tool" for m in messages):
            return ChatResult(content="Let me search first.", thinking="need fresh data",
                              tool_calls=[
                {"id": "c1", "name": "searxng__search", "arguments": {"q": "x"}}],
                prompt_tokens=5, completion_tokens=2), "o1"
        return ChatResult(content="done", tool_calls=[
            {"id": "c2", "name": "write_to_file", "arguments": {"path": "a"}}],
            prompt_tokens=9, completion_tokens=3, finish_reason="tool_calls"), "o1"


def test_direct_mode_merges_persona_tools_and_runs_them(app, client):
    from foundry_router.tools.sync import ToolDef
    svc = app.state.services
    persona = next(p for p in svc.personas.list(enabled_only=True))
    svc.personas.upsert(persona["virtual_name"], preferred_mcp_tools=json.dumps(["searxng"]),
                        mcp_tools_in_direct=1)
    td = ToolDef(name="searxng__search", kind="mcp", description="search",
                 parameters={"type": "object"}, server="searxng", mcp_tool="search")
    svc.tool_registry.tools[td.name] = td
    calls = []

    async def fake_rich(server, tool, args, **kw):
        calls.append((server, tool, args, request_context.mcp_attribution()))
        return ToolResult([{"type": "text", "text": "search results"}])
    real_rich, svc.mcp.call_tool_rich = getattr(svc.mcp, "call_tool_rich"), fake_rich
    pool = ToolLoopPool()
    real, svc.pool = svc.pool, pool
    try:
        r = client.post("/api/chat", json={
            "model": persona["virtual_name"], "stream": False,
            "tools": [{"type": "function", "function": {"name": "write_to_file"}}],
            "messages": [{"role": "user", "content": "go"}]}).json()
    finally:
        svc.pool = real
        svc.mcp.call_tool_rich = real_rich
    assert pool.seen_tools[0] == ["write_to_file", "searxng__search"]      # merged
    assert calls and calls[0][:3] == ("searxng", "search", {"q": "x"})     # Foundry ran it
    assert calls[0][3]["source"] == "direct"
    assert any(m.get("role") == "tool" and m["content"] == "search results"
               for m in pool.convos[1])                                    # result fed back
    tcs = r["message"]["tool_calls"]
    assert [t["function"]["name"] for t in tcs] == ["write_to_file"]       # client's call only
    # what the model said / reasoned in the Foundry-tool round isn't lost
    assert r["message"]["content"] == "Let me search first.\n\ndone"
    assert "need fresh data" in r["message"]["thinking"]
    assert r["foundry"]["backend"] == "o1"


def test_persona_without_tools_keeps_client_tools_only(app, client):
    svc = app.state.services
    persona = next(p for p in svc.personas.list(enabled_only=True)
                   if not json.loads(p.get("preferred_mcp_tools") or "[]"))
    pool = ToolLoopPool()
    real, svc.pool = svc.pool, pool
    try:
        client.post("/api/chat", json={
            "model": persona["virtual_name"], "stream": False,
            "tools": [{"type": "function", "function": {"name": "write_to_file"}}],
            "messages": [{"role": "user", "content": "go"}, {"role": "tool", "content": "x"}]})
    finally:
        svc.pool = real
    assert pool.seen_tools[0] == ["write_to_file"]


def test_mcp_metrics_and_context_endpoints(app, client):
    from foundry_router.tools.sync import ToolDef
    svc = app.state.services
    svc.tool_registry.tools["searxng__search"] = ToolDef(
        name="searxng__search", kind="mcp", description="Search the web " * 20,
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
        server="searxng", mcp_tool="search")
    c = client.get("/admin/api/mcp-context", params={"ctx": 262144}).json()
    top = c["scopes"][0]
    assert top["kind"] == "aggregator" and top["tools"] >= 1 and top["tokens"] > 50
    assert top["pct"] is not None
    m = client.get("/admin/api/mcp-metrics", params={"hours": 1}).json()
    assert "totals" in m and "live" in m
    assert client.post("/admin/api/mcp-metrics/clear", json={}).json()["ok"]


def test_specific_aggregator_endpoints_mount_before_base(tmp_path):
    """Starlette Mounts match by prefix: a base /mcp mounted first swallowed
    /mcp/p/<profile> and /mcp/persona/<name> and served every tool on them."""
    from fastapi.testclient import TestClient
    from starlette.routing import Mount
    from foundry_router.main import create_app
    from tests.conftest import TEST_CONFIG
    cfg = tmp_path / "c.yaml"
    cfg.write_text(TEST_CONFIG + "mcp_aggregator: {enabled: true, profiles: {search: [searxng]}}\n")
    app = create_app(config_path=cfg, database_path=tmp_path / "t.sqlite")
    with TestClient(app):
        paths = [r.path for r in app.router.routes if isinstance(r, Mount)]
    assert "/mcp" in paths
    base = paths.index("/mcp")
    assert paths.index("/mcp/p/search") < base
    assert paths.index("/mcp/persona") < base
