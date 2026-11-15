"""v0.181.158: A2A in both directions, and helpers asked at the same time."""
import asyncio
import inspect
import json
import time
from pathlib import Path

import httpx
import pytest

from foundry_router import a2a, helpers
from foundry_router.config import A2AConfig, AgentBrainConfig, AppConfig
from foundry_router.pool.protocols import ChatResult
from tests.test_helpers import _last_tool_text, _setup
from tests.test_main_mode import _run


# -- the protocol ---------------------------------------------------------------------------

def test_messages_of_both_versions():
    m1 = a2a.message("hi", a2a.V1, agent=False)
    assert m1["role"] == "ROLE_USER" and m1["parts"] == [{"text": "hi"}] and "kind" not in m1
    m3 = a2a.message("hi", a2a.V03, agent=True, context_id="c")
    assert m3["role"] == "agent" and m3["kind"] == "message" and m3["contextId"] == "c"
    assert m3["parts"] == [{"kind": "text", "text": "hi"}]
    assert a2a.parts_text([{"text": "a"}, {"kind": "text", "text": "b"}, {"url": "x"}, "junk"]) == "a\nb"
    assert a2a.send_result("ok", a2a.V1, "c")["message"]["role"] == "ROLE_AGENT"
    assert a2a.send_result("ok", a2a.V03, "c")["kind"] == "message"


def test_calls_are_told_apart_by_method():
    rid, kind, ver, params, err = a2a.parse_call(
        {"jsonrpc": "2.0", "id": 7, "method": "SendMessage",
         "params": {"message": {"role": "ROLE_USER", "parts": [{"text": "q"}], "contextId": "c1"}}})
    assert (rid, kind, ver, err) == (7, "send", a2a.V1, None)
    assert a2a.incoming_text(params) == ("q", "c1")
    assert a2a.parse_call({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}})[1:3] == ("send", a2a.V03)
    assert a2a.parse_call({"jsonrpc": "2.0", "id": 1, "method": "tasks/get"})[1] == "get"
    assert a2a.parse_call({"jsonrpc": "2.0", "id": 1, "method": "nope"})[4]["error"]["code"] == -32601
    assert a2a.parse_call({"method": "SendMessage"})[4]["error"]["code"] == -32600
    assert a2a.parse_call("x")[4]["error"]["code"] == -32600


def test_the_card_and_where_a_card_points():
    c = a2a.card("http://r:1/", {"virtual_name": "Foundry-Chat", "description": "main chat"}, "9.9",
                 [{"virtual_name": "Foundry-Chat"}, {"virtual_name": "Foundry-Max"}])
    for k in ("name", "description", "supportedInterfaces", "version", "capabilities",
              "defaultInputModes", "defaultOutputModes", "skills"):
        assert k in c, k                                       # the fields 1.0 requires
    assert c["supportedInterfaces"][0] == {"url": "http://r:1/v1/a2a/Foundry-Chat",
                                           "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    assert [s["id"] for s in c["skills"]] == ["Foundry-Chat", "Foundry-Max"]
    assert a2a.endpoint(c) == ("http://r:1/v1/a2a/Foundry-Chat", a2a.V1)
    assert a2a.endpoint({"url": "http://old/a2a", "preferredTransport": "JSONRPC"}) == ("http://old/a2a", a2a.V03)
    assert a2a.endpoint({"supportedInterfaces": [
        {"url": "g", "protocolBinding": "GRPC"}, {"url": "j", "protocolBinding": "JSONRPC", "protocolVersion": "0.3"}]}) \
        == ("j", a2a.V03)
    with pytest.raises(a2a.A2AError):
        a2a.endpoint({"supportedInterfaces": [{"url": "g", "protocolBinding": "GRPC"}]})


def test_conversations_are_kept_and_bounded():
    c = a2a.Contexts()
    c.add("k", "q1", "a1")
    c.add("k", "q2", "a2")
    assert [m["content"] for m in c.history("k")] == ["q1", "a1", "q2", "a2"] and c.history("other") == []
    for i in range(a2a.MAX_CONTEXTS + 5):
        c.add(f"x{i}", "q", "a")
    assert c.history("k") == [] and len(c._c) == a2a.MAX_CONTEXTS


def _agent_server(version, script):
    """A remote agent: the card, then the scripted results for each RPC call."""
    seen = []

    def handler(request):
        if request.method == "GET":
            if request.url.path == "/.well-known/agent-card.json" and version == a2a.V1:
                return httpx.Response(200, json={"name": "R", "skills": [], "supportedInterfaces": [
                    {"url": "http://agent/rpc", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}]})
            if request.url.path == "/.well-known/agent.json" and version == a2a.V03:
                return httpx.Response(200, json={"name": "R", "skills": [], "url": "http://agent/rpc"})
            return httpx.Response(404)
        body = json.loads(request.content)
        seen.append((body["method"], body["params"], dict(request.headers)))
        step = script.pop(0)
        if isinstance(step, int):
            return httpx.Response(step, text="no")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], **step})
    return httpx.MockTransport(handler), seen


@pytest.mark.asyncio
async def test_asking_a_1_0_agent_that_answers_with_a_message():
    tr, seen = _agent_server(a2a.V1, [{"result": {"message": {"role": "ROLE_AGENT", "parts": [{"text": "42"}]}}}])
    async with httpx.AsyncClient(transport=tr) as c:
        assert await a2a.ask(c, {"url": "http://agent", "token": "tk"}, "what?") == "42"
    method, params, headers = seen[0]
    assert method == "SendMessage" and params["message"]["role"] == "ROLE_USER"
    assert params["message"]["parts"] == [{"text": "what?"}]
    assert headers["authorization"] == "Bearer tk" and headers["a2a-version"] == "1.0"


@pytest.mark.asyncio
async def test_a_task_is_asked_for_until_it_is_done():
    tr, seen = _agent_server(a2a.V03, [
        {"result": {"kind": "task", "id": "t1", "status": {"state": "working"}}},
        {"result": {"kind": "task", "id": "t1", "status": {"state": "completed"},
                    "artifacts": [{"artifactId": "a", "parts": [{"kind": "text", "text": "the report"}]}]}}])
    async with httpx.AsyncClient(transport=tr) as c:
        assert await a2a.ask(c, {"url": "http://agent"}, "go", poll_seconds=0) == "the report"
    assert [m for m, _, _ in seen] == ["message/send", "tasks/get"] and seen[1][1] == {"id": "t1"}
    assert seen[0][1]["message"]["kind"] == "message" and "authorization" not in seen[0][2]
    # 1.0 wraps the task and writes states in capitals
    tr, _ = _agent_server(a2a.V1, [{"result": {"task": {"id": "t", "status": {
        "state": "TASK_STATE_COMPLETED", "message": {"parts": [{"text": "done"}]}}}}}])
    async with httpx.AsyncClient(transport=tr) as c:
        assert await a2a.ask(c, {"url": "http://agent"}, "go") == "done"


@pytest.mark.asyncio
async def test_failures_are_named():
    for script, needle in (([{"error": {"code": -32603, "message": "boom"}}], "boom (code -32603)"),
                           ([500], "HTTP 500"),
                           ([{"result": {"task": {"id": "t", "status": {"state": "TASK_STATE_FAILED"}}}}], "ended as failed"),
                           ([{"result": {"message": {"parts": []}}}], "no text")):
        tr, _ = _agent_server(a2a.V1, script)
        async with httpx.AsyncClient(transport=tr) as c:
            with pytest.raises(a2a.A2AError) as e:
                await a2a.ask(c, {"url": "http://agent"}, "go")
        assert needle in str(e.value), (needle, str(e.value))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as c:
        with pytest.raises(a2a.A2AError) as e:
            await a2a.ask(c, {"url": "http://nowhere"}, "go")
        assert "no agent card found" in str(e.value)
    assert a2a.find_agent([{"name": "A", "enabled": False}, {"name": "b"}], "B") == {"name": "b"}
    assert a2a.find_agent([{"name": "A", "enabled": False}], "a") is None


# -- a remote agent as a helper -------------------------------------------------------------

def test_a_helper_row_may_name_a_remote_agent():
    rows, errors = helpers.clean([{"role": "research", "agent": " lab-agent ", "what": "deep research",
                                   "tools": True, "digest_over": 5000},
                                  {"role": "both", "agent": "x", "models": ["m"]},
                                  {"role": "none"}])
    assert rows == [{"role": "research", "models": [], "what": "deep research", "agent": "lab-agent"}]
    assert errors == ["helpers[both]: give models or a remote agent, not both",
                      "helpers[none]: pick at least one model"]
    text = helpers.describe(rows)
    assert "- research: deep research (a remote agent: lab-agent)" in text and "same turn" in text


def _with_agents(runner, agents):
    cfg = AppConfig(a2a=A2AConfig(agents=agents))
    runner.guardrails.config_fn = lambda: cfg


def test_the_task_goes_to_the_agent_and_its_answer_comes_back(tmp_path):
    from tests.test_main_mode import _tool
    runner, ctx, pool, _ = _setup(tmp_path, {
        "main": [_tool("ask_helper", helper="research", task="find it", context="here"),
                 ChatResult(content="The agent found it.")]},
        [{"role": "research", "agent": "lab", "instruction": "Be brief."}])
    _with_agents(runner, [{"name": "Lab", "url": "http://lab", "token": "s3cret"}])
    sent = []

    async def fake(agent, text):
        sent.append((agent["name"], agent["token"], text))
        return " found: 7 "
    runner._a2a_ask = fake
    evs = _run(runner.run_main(ctx))
    assert [e.text for e in evs if e.kind == "answer"] == ["The agent found it."]
    assert sent == [("Lab", "s3cret", "Be brief.\n\nfind it\n\n---\nhere")]
    assert _last_tool_text(pool) == "[answer from the research helper, remote agent lab]\nfound: 7"
    assert [c["model"] for c in pool.seen] == ["main", "main"]          # no model of ours was asked
    think = " ".join(e.text for e in evs if e.kind == "think")
    assert "remote agent lab" in think and "s3cret" not in think


def test_a_missing_paid_or_failing_agent_is_told_to_the_main_model(tmp_path):
    from tests.test_main_mode import _tool
    for agents, row, fail, needle in (
            ([], {}, None, "no enabled remote agent named 'lab'"),
            ([{"name": "lab", "url": "http://lab", "paid": True}], {"paid": False}, None, "marked as paid"),
            ([{"name": "lab", "url": "http://lab"}], {}, a2a.A2AError("HTTP 502"), "failed: A2AError: HTTP 502")):
        sub = tmp_path / str(len(needle))
        sub.mkdir()
        runner, ctx, pool, _ = _setup(sub, {
            "main": [_tool("ask_helper", helper="r", task="t"), ChatResult(content="done")]},
            [{"role": "r", "agent": "lab", **row}])
        _with_agents(runner, agents)

        async def fake(agent, text, fail=fail):
            raise fail
        runner._a2a_ask = fake
        _run(runner.run_main(ctx))
        out = _last_tool_text(pool)
        assert out.startswith("ERROR:") and needle in out and "Do this part yourself" in out, out


# -- helpers at the same time ---------------------------------------------------------------

class _SlowPool:
    pass


def _two_calls():
    return ChatResult(tool_calls=[
        {"id": "a", "name": "ask_helper", "arguments": {"helper": "one", "task": "first"}},
        {"id": "b", "name": "ask_helper", "arguments": {"helper": "two", "task": "second"}}])


def _slow(pool, delay=0.25):
    real = pool.chat
    running = {"now": 0, "most": 0}

    async def chat(model, messages, tools=None, **kw):
        if model != "main":
            running["now"] += 1
            running["most"] = max(running["most"], running["now"])
            await asyncio.sleep(delay)
            running["now"] -= 1
        return await real(model, messages, tools=tools)
    pool.chat = chat
    return running


def test_helpers_asked_in_one_turn_run_together(tmp_path):
    runner, ctx, pool, _ = _setup(tmp_path, {
        "main": [_two_calls(), ChatResult(content="both in")],
        "h1": [ChatResult(content="ONE")], "h2": [ChatResult(content="TWO")]},
        [{"role": "one", "models": ["h1"]}, {"role": "two", "models": ["h2"]}])
    running = _slow(pool)
    t0 = time.monotonic()
    evs = _run(runner.run_main(ctx))
    took = time.monotonic() - t0
    assert running["most"] == 2 and took < 0.45                          # together, not 2 × 0.25 s
    assert [e.text for e in evs if e.kind == "answer"] == ["both in"]
    tools = [m for m in pool.seen[-1]["messages"] if m.get("role") == "tool"]
    assert [(m["tool_call_id"], m["content"]) for m in tools] == [          # handed back in the order asked
        ("a", "[answer from the one helper, h1]\nONE"), ("b", "[answer from the two helper, h2]\nTWO")]
    assert any("asks 2 helpers at the same time" in e.text for e in evs if e.kind == "think")
    assert [t["tool"] for t in ctx.logger.tool_calls] == ["ask_helper", "ask_helper"]


def test_switched_off_they_run_one_after_another(tmp_path):
    runner, ctx, pool, _ = _setup(tmp_path, {
        "main": [_two_calls(), ChatResult(content="both in")],
        "h1": [ChatResult(content="ONE")], "h2": [ChatResult(content="TWO")]},
        [{"role": "one", "models": ["h1"]}, {"role": "two", "models": ["h2"]}])
    runner.brain.cfg.helpers_parallel = False
    running = _slow(pool, 0.05)
    evs = _run(runner.run_main(ctx))
    assert running["most"] == 1
    assert not any("at the same time" in e.text for e in evs if e.kind == "think")


def test_calls_made_together_each_count_against_the_limit(tmp_path):
    two_same = ChatResult(tool_calls=[
        {"id": "a", "name": "ask_helper", "arguments": {"helper": "one", "task": "first"}},
        {"id": "b", "name": "ask_helper", "arguments": {"helper": "one", "task": "second"}}])
    runner, ctx, pool, _ = _setup(tmp_path, {
        "main": [two_same, ChatResult(content="done")], "h1": [ChatResult(content="ONE")]},
        [{"role": "one", "models": ["h1"], "max_calls": 1}])
    _slow(pool, 0.05)
    _run(runner.run_main(ctx))
    tools = [m["content"] for m in pool.seen[-1]["messages"] if m.get("role") == "tool"]
    assert sum("answer from the one helper" in t for t in tools) == 1
    assert sum("already asked 1 time(s)" in t for t in tools) == 1


# -- the router as an agent -----------------------------------------------------------------

def _app(serve=True, personas=()):
    from types import SimpleNamespace

    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from foundry_router import a2a_routes
    app = FastAPI()
    app.include_router(a2a_routes.router)
    chats = []

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        chats.append((body, request.headers.get("authorization")))
        if body["messages"][-1]["content"] == "fail":
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": {"message": "no model is reachable"}}, status_code=503)
        return {"choices": [{"message": {"content": f"{body['model']} heard {len(body['messages'])} message(s)"}}]}
    rows = [{"virtual_name": "Foundry-Chat", "description": "main chat"}, {"virtual_name": "Foundry-Max"},
            {"virtual_name": "Kilo-Thing"}]
    cfg = SimpleNamespace(a2a=A2AConfig(serve=serve, personas=list(personas), public_url="http://router:1"))
    app.state.services = SimpleNamespace(
        config_store=SimpleNamespace(config=cfg),
        personas=SimpleNamespace(list=lambda enabled_only=False: rows))
    a2a_routes._contexts = a2a.Contexts()
    return TestClient(app), chats


def _call(client, method, params=None, path="/v1/a2a", **kw):
    return client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, **kw).json()


def test_off_by_default_nothing_is_served():
    assert A2AConfig().serve is False and AgentBrainConfig().helpers_parallel is True
    client, chats = _app(serve=False)
    assert client.get("/.well-known/agent-card.json").status_code == 404
    assert client.post("/v1/a2a", json={}).status_code == 404 and chats == []


def test_another_agent_calls_a_persona_and_continues_the_conversation():
    client, chats = _app(personas=["Foundry-Max"])
    card = client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "Foundry-Chat" and card["url"] == "http://router:1/v1/a2a/Foundry-Chat"
    assert [s["id"] for s in card["skills"]] == ["Foundry-Chat", "Foundry-Max"]
    assert client.get("/.well-known/agent.json").json()["name"] == "Foundry-Chat"
    assert client.get("/v1/a2a/Foundry-Max/agent-card.json").json()["name"] == "Foundry-Max"
    assert client.get("/v1/a2a/Kilo-Thing/agent-card.json").status_code == 404        # not offered

    out = _call(client, "SendMessage", {"message": {"role": "ROLE_USER", "parts": [{"text": "hello"}]}},
                headers={"Authorization": "Bearer client-key"})
    m = out["result"]["message"]
    assert m["role"] == "ROLE_AGENT" and m["parts"] == [{"text": "Foundry-Chat heard 1 message(s)"}]
    assert chats[0][0]["stream"] is False and chats[0][1] == "Bearer client-key"        # the caller's key is passed on
    again = _call(client, "SendMessage", {"message": {"role": "ROLE_USER", "parts": [{"text": "more"}],
                                                      "contextId": m["contextId"]}})
    assert again["result"]["message"]["parts"][0]["text"] == "Foundry-Chat heard 3 message(s)"
    assert again["result"]["message"]["contextId"] == m["contextId"]

    old = _call(client, "message/send", {"message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}},
                path="/v1/a2a/Foundry-Max")
    assert old["result"]["kind"] == "message" and old["result"]["role"] == "agent"
    assert old["result"]["parts"] == [{"kind": "text", "text": "Foundry-Max heard 1 message(s)"}]

    r = client.post("/v1/a2a", json={"jsonrpc": "2.0", "id": 5, "method": "SendStreamingMessage",
                                     "params": {"message": {"parts": [{"text": "s"}]}}})
    assert r.headers["content-type"].startswith("text/event-stream")
    ev = json.loads(r.text.split("data: ", 1)[1])
    assert ev["id"] == 5 and ev["result"]["message"]["parts"][0]["text"].startswith("Foundry-Chat heard")


def test_what_it_cannot_do_is_answered_as_an_error():
    client, _ = _app()
    assert _call(client, "GetTask", {"id": "t"})["error"]["code"] == a2a.ERR_TASK_NOT_FOUND
    assert _call(client, "CancelTask", {"id": "t"})["error"]["code"] == a2a.ERR_UNSUPPORTED
    assert _call(client, "SendMessage", {"message": {"parts": [{"url": "x"}]}})["error"]["code"] == a2a.ERR_PARAMS
    assert _call(client, "SendMessage", {"message": {"parts": [{"text": "x"}]}},
                 path="/v1/a2a/Kilo-Thing")["error"]["code"] == a2a.ERR_PARAMS
    out = _call(client, "SendMessage", {"message": {"parts": [{"text": "fail"}]}})
    assert out["error"]["code"] == a2a.ERR_INTERNAL and "no model is reachable" in out["error"]["message"]
    assert client.post("/v1/a2a", content=b"{").json()["error"]["code"] == -32700


def test_settings_route_and_page():
    from foundry_router import client_auth
    from foundry_router.ui import routes
    src = inspect.getsource(routes)
    assert '@router.post("/admin/api/a2a")' in src and 'a["token"] = stored.get(a["name"].lower(), "")' in src
    assert 'a["token"] = _mask(a["token"])' in src and '"helpers_parallel"}' in src
    assert "from .a2a_routes import router as a2a_router" in (Path(routes.__file__).parent.parent / "main.py").read_text(encoding="utf-8")
    assert "/v1/a2a".startswith("/v1/") and 'path.startswith("/api/") or path.startswith("/v1/")' in inspect.getsource(client_auth)
    html = (Path(routes.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    for fid in ("a2a-serve", "a2a-default", "a2a-personas", "a2a-public", "a2a-agents", "a2a-save", "a2a-test",
                "brain-hpar"):
        assert f'id="{fid}"' in html, fid
    assert 'data-jform="a2aAgents"' in html and 'a2aAgents: {kind: "rows", required: "name"' in html
    assert '{key: "agent", label: "or a remote agent (A2A), by its name"' in html
    assert 'helpers_parallel: $("#brain-hpar").checked' in html
