"""Hermes Agent integration: as a TOOL (<agent>_run/_status/_stop through the
MCP path — grants, aggregator, metrics) and as a persona BACKEND (streamed
answer, tool progress as thinking, session continuity), plus loop protection."""

import asyncio
import json

import httpx
import pytest

from foundry_router import request_context
from foundry_router.agents import AgentManager, conversation_key, progress_line
from foundry_router.config import AgentConfig
from foundry_router.db import Database
from tests.test_ollama_compat import FakePool

SEEN: list = []


def _sse(*frames) -> str:
    out = ""
    for ev, data in frames:
        if ev:
            out += f"event: {ev}\n"
        out += f"data: {data if isinstance(data, str) else json.dumps(data)}\n\n"
    return out


RUN_EVENTS = _sse(
    ("tool.started", {"tool": "terminal", "preview": "pytest -q"}),
    ("tool.completed", {"tool": "terminal", "duration": 1.2, "error": False}),
    ("tool.started", {"tool": "web_search", "preview": "hermes"}),
    ("assistant.delta", {"type": "text", "text": "All "}),
    ("assistant.delta", {"type": "text", "text": "green."}),
    ("run.completed", {"status": "completed", "output": "All green."}))

CHAT_STREAM = _sse(
    ("hermes.tool.progress", {"tool": "read_file", "emoji": "📖", "label": "reading",
                              "preview": "app.py"}),
    ("", {"id": "c1", "model": "hermes-agent", "choices": [
        {"index": 0, "delta": {"role": "assistant", "content": "Fixed "}}]}),
    ("", {"id": "c1", "choices": [{"index": 0, "delta": {"content": "it."},
                                   "finish_reason": "stop"}]}),
    ("", {"id": "c1", "choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 9,
                                                "total_tokens": 59, "cache_read_tokens": 30}}),
    ("", "[DONE]"))


def hermes_handler(request: httpx.Request, slow_events: bool = False):
    SEEN.append(request)
    p = request.url.path
    if p == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if p == "/v1/models":
        return httpx.Response(200, json={"data": [{"id": "hermes-agent"}]})
    if p == "/v1/skills":
        return httpx.Response(200, json={"data": [{"name": "github"}]})
    if p in ("/v1/capabilities", "/v1/toolsets"):
        return httpx.Response(200, json={"data": []})
    if p == "/v1/runs" and request.method == "POST":
        return httpx.Response(200, json={"run_id": "run_1", "status": "started"})
    if p == "/v1/runs/run_1/events":
        if slow_events:
            return httpx.Response(200, text=_sse(("tool.started", {"tool": "terminal"})),
                                  headers={"content-type": "text/event-stream"},
                                  stream=_SlowStream())
        return httpx.Response(200, text=RUN_EVENTS, headers={"content-type": "text/event-stream"})
    if p == "/v1/runs/run_1":
        return httpx.Response(200, json={
            "object": "hermes.run", "run_id": "run_1", "status": "completed",
            "session_id": "sess_9", "output": "All green.",
            "usage": {"input_tokens": 200, "output_tokens": 20, "cache_read_tokens": 150},
            "runtime": {"provider": "custom", "model": "qwen3-coder"}})
    if p == "/v1/runs/run_1/stop":
        return httpx.Response(200, json={"ok": True})
    if p == "/v1/chat/completions":
        return httpx.Response(200, text=CHAT_STREAM,
                              headers={"content-type": "text/event-stream",
                                       "x-hermes-session-id": request.headers.get(
                                           "x-hermes-session-id", "")})
    return httpx.Response(404)


class _SlowStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"event: tool.started\ndata: {\"tool\": \"terminal\"}\n\n"
        await asyncio.sleep(30)


def _mgr(tmp_path, slow=False, **kw):
    db = Database(tmp_path / "a.sqlite")
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: hermes_handler(r, slow_events=slow)))
    cfg = AgentConfig(name="hermes", url="http://hermes:8642", api_key="k", **kw)
    return AgentManager([cfg], http, db), db


def test_progress_line_and_conversation_key():
    assert progress_line({"tool": "terminal", "preview": "ls"}) == "🔧 terminal: ls"
    a = conversation_key([{"role": "system", "content": "s"}, {"role": "user", "content": "q1"}])
    b = conversation_key([{"role": "system", "content": "s"}, {"role": "user", "content": "q1"},
                          {"role": "assistant", "content": "x"}, {"role": "user", "content": "q2"}])
    c = conversation_key([{"role": "user", "content": "other"}])
    assert a == b != c
    assert conversation_key([], "oc-1").startswith("c-")


def test_tool_manifests_shape(tmp_path):
    mgr, _ = _mgr(tmp_path)
    m = mgr.tool_manifests()
    assert list(m) == ["agent-hermes"]
    names = [t["name"] for t in m["agent-hermes"]]
    assert names == ["hermes_run", "hermes_status", "hermes_stop"]
    assert m["agent-hermes"][0]["input_schema"]["required"] == ["task"]
    mgr2, _ = _mgr(tmp_path, expose_as_tool=False)
    assert mgr2.tool_manifests() == {}


async def test_probe_reads_health_models_skills(tmp_path):
    mgr, _ = _mgr(tmp_path)
    h = await mgr.probe("hermes")
    assert h["healthy"] and h["model"] == "hermes-agent"
    assert h["skills"] == [{"name": "github"}]
    assert mgr.healthy("hermes") is True


async def test_tool_run_via_runs_api_relays_progress_and_records(tmp_path):
    SEEN.clear()
    mgr, db = _mgr(tmp_path)
    progress = []

    async def cb(p, total, msg):
        progress.append(msg)
    res = await mgr.call_tool("agent-hermes", "hermes_run",
                              {"task": "run the tests", "context": "repo at /src"}, cb)
    assert res.text.startswith("All green.")
    assert "terminal" in res.text and "session_id=sess_9" in res.text
    body = json.loads([r for r in SEEN if r.url.path == "/v1/runs"][0].content)
    assert body == {"input": "run the tests", "instructions": "repo at /src"}
    assert SEEN[0].headers["authorization"] == "Bearer k"
    assert any("terminal" in (m or "") for m in progress)
    row = db.query_one("SELECT * FROM agent_runs")
    assert row["status"] == "completed" and row["mode"] == "tool"
    assert row["tool_calls"] == 2 and row["prompt_tokens"] == 200
    assert row["model"] == "qwen3-coder" and row["run_id"] == "run_1"
    s = mgr.summary()
    assert s["groups"][0]["completed"] == 1


async def test_tool_run_hands_off_then_status(tmp_path):
    mgr, db = _mgr(tmp_path, slow=True, tool_wait_seconds=5)
    mgr.agents["hermes"].tool_wait_seconds = 1   # below the 5s floor via the object
    import foundry_router.agents as ag
    orig = ag.asyncio.wait_for

    async def fast_wait(coro, timeout):
        return await orig(coro, timeout=min(timeout, 0.3))
    ag.asyncio.wait_for = fast_wait
    try:
        res = await mgr.call_tool("agent-hermes", "hermes_run", {"task": "long job"})
    finally:
        ag.asyncio.wait_for = orig
    assert "run_id=run_1" in res.text and "hermes_status" in res.text
    assert db.query_one("SELECT status FROM agent_runs")["status"] == "handed_off"
    st = await mgr.call_tool("agent-hermes", "hermes_status", {"run_id": "run_1",
                                                                 "wait_seconds": 0})
    assert st.text.startswith("All green.")
    assert db.query_one("SELECT status FROM agent_runs")["status"] == "completed"
    stop = await mgr.call_tool("agent-hermes", "hermes_stop", {"run_id": "run_1"})
    assert "Stop requested" in stop.text


async def test_loop_protection_refuses_agent_callers(tmp_path):
    mgr, _ = _mgr(tmp_path, caller_token="hermes-secret")
    assert request_context.agent_caller_from({"authorization": "Bearer hermes-secret"}) == "hermes"
    assert request_context.agent_caller_from({"authorization": "Bearer other"}) is None
    tok = request_context.set_agent_caller("hermes")
    try:
        with pytest.raises(Exception, match="loop protection"):
            await mgr.call_tool("agent-hermes", "hermes_run", {"task": "x"})
    finally:
        request_context._agent_caller.reset(tok)
    request_context.set_agent_tokens({})


async def test_backend_chat_stream_parses_progress_usage_and_session(tmp_path):
    SEEN.clear()
    mgr, db = _mgr(tmp_path)
    evs = [e async for e in mgr.chat_stream("hermes", [{"role": "user", "content": "fix"}],
                                            session_key="Foundry-Agent:f-abc")]
    assert evs[0]["progress"] and "read_file" in evs[0]["thinking"]
    assert "".join(e.get("content") or "" for e in evs) == "Fixed it."
    done = evs[-1]
    assert done["done"] and done["prompt_tokens"] == 50 and done["cached_tokens"] == 30
    assert done["session_id"] == "Foundry-Agent:f-abc" and done["tools"] == ["read_file"]
    req = [r for r in SEEN if r.url.path == "/v1/chat/completions"][0]
    assert req.headers["x-hermes-session-id"] == "Foundry-Agent:f-abc"
    assert json.loads(req.content)["model"] == "hermes-agent"
    row = db.query_one("SELECT * FROM agent_runs")
    assert row["mode"] == "backend" and row["status"] == "completed"


async def test_agent_tool_through_mcp_manager_logs_call(tmp_path):
    from foundry_router.tools.mcp_client import MCPManager
    mgr, db = _mgr(tmp_path)
    mcp = MCPManager([], db)
    mcp.agents = mgr
    assert "agent-hermes" in await mcp.list_all()
    res = await request_context.mcp_attributed(
        mcp.call_tool_rich("agent-hermes", "hermes_run", {"task": "t"}), "aggregator", "all")
    assert res.text.startswith("All green.")
    row = db.query_one("SELECT * FROM mcp_call_log")
    assert row["server"] == "agent-hermes" and row["ok"] == 1
    assert row["source"] == "aggregator" and row["session"] == "agent"
    assert db.query_one("SELECT caller FROM agent_runs")["caller"] == "aggregator:all"


def test_plain_message_flattens_client_tool_turns():
    pm = AgentManager._plain_message
    assert pm({"role": "tool", "tool_name": "ls", "content": "a b"})["role"] == "user"
    out = pm({"role": "assistant", "content": "",
              "tool_calls": [{"function": {"name": "read_file", "arguments": {}}}]})
    assert "read_file" in out["content"]
    assert pm({"role": "user", "content": [{"type": "text", "text": "hi"},
                                           {"type": "image_url"}]})["content"] == "hi"


# -- through the app -------------------------------------------------------------------

def _wire(app):
    svc = app.state.services
    svc.agents.http = httpx.AsyncClient(transport=httpx.MockTransport(hermes_handler))
    svc.agents.set_agents([AgentConfig(name="hermes", url="http://hermes:8642",
                                       caller_token="hermes-secret")])
    svc.personas.upsert("Foundry-Agent", description="served by hermes",
                        agent_backend="hermes", execution_mode="direct")
    return svc


def test_agent_persona_streams_via_ollama_and_openai(app, client):
    svc = _wire(app)
    r = client.post("/api/chat", json={"model": "Foundry-Agent",
                                       "messages": [{"role": "user", "content": "fix bug"}]})
    lines = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    thinking = "".join(l["message"].get("thinking") or "" for l in lines)
    assert "read_file" in thinking
    assert "".join(l["message"]["content"] for l in lines) == "Fixed it."
    fin = lines[-1]
    assert fin["done"] and fin["eval_count"] == 9
    assert fin["foundry"]["agent"] == "hermes" and fin["foundry"]["backend"] == "agent:hermes"

    r = client.post("/v1/chat/completions", json={
        "model": "Foundry-Agent", "messages": [{"role": "user", "content": "fix bug"}]}).json()
    msg = r["choices"][0]["message"]
    assert msg["content"] == "Fixed it." and "read_file" in (msg.get("reasoning_content") or "")
    assert r["usage"]["prompt_tokens"] == 50
    rows = svc.db.query("SELECT * FROM agent_runs")
    assert len(rows) == 2 and all(x["mode"] == "backend" for x in rows)
    request_context.set_agent_tokens({})


def test_agent_persona_loop_protection_routes_to_model(app, client):
    svc = _wire(app)
    fake = FakePool()
    real, svc.pool = svc.pool, fake
    svc.personas.upsert("Foundry-Agent", pinned_models=["raw"])
    try:
        SEEN.clear()
        r = client.post("/api/chat", headers={"Authorization": "Bearer hermes-secret"},
                        json={"model": "Foundry-Agent", "stream": False,
                              "messages": [{"role": "user", "content": "hi"}]})
    finally:
        svc.pool = real
    assert not [x for x in SEEN if x.url.path == "/v1/chat/completions"]
    assert fake.calls, r.text          # served by a model instead
    ev = svc.db.query("SELECT message FROM event_log WHERE source='agents'")
    assert any("loop protection" in e["message"] for e in ev)
    request_context.set_agent_tokens({})


def test_agent_tools_sync_into_registry_and_admin_api(app, client):
    svc = _wire(app)
    client.post("/admin/api/agents/probe", json={"name": "hermes"})
    r = client.post("/admin/api/agents", json={"name": "hermes", "url": "http://hermes:8642",
                                               "caller_token": "hermes-secret",
                                               "expose_as_tool": True}).json()
    assert r["ok"] and r["health"]["healthy"]
    names = {t["name"]: t for t in svc.tool_registry.status() if t["kind"] == "mcp"}
    assert names["hermes_run"]["server"] == "agent-hermes"
    lst = client.get("/admin/api/agents").json()["agents"][0]
    assert lst["caller_token"] is True           # kept, never echoed
    assert lst["personas"] == ["Foundry-Agent"]
    # grant to a persona like any MCP server
    svc.personas.upsert("Coder", preferred_mcp_tools=["agent-hermes"])
    tools = svc.tool_registry.mcp_tools_for_persona(svc.personas.get("Coder"))
    assert {t.name for t in tools} == {"hermes_run", "hermes_status", "hermes_stop"}
    tok = request_context.set_agent_caller("hermes")
    try:
        assert svc.tool_registry.mcp_tools_for_persona(svc.personas.get("Coder")) == []
    finally:
        request_context._agent_caller.reset(tok)
    runs = client.get("/admin/api/agent-runs").json()
    assert "groups" in runs and "recent" in runs
    request_context.set_agent_tokens({})


async def test_handed_off_run_is_finalized_by_watcher(tmp_path):
    mgr, db = _mgr(tmp_path)
    from foundry_router.agents import _Run
    run = _Run("hermes", "tool", "x", 1)
    run.run_id = "run_1"
    mgr._runs["run_1"] = run
    run.row_id = mgr._record(run, "handed_off")
    await mgr._watch(mgr.agents["hermes"], "run_1", interval=0.01)
    row = db.query_one("SELECT * FROM agent_runs")
    assert row["status"] == "completed" and row["model"] == "qwen3-coder" and not mgr._runs
