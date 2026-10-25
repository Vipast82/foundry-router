"""Persona tool loop round cap (direct mode, v0.139.2): when the cap is reached
while the model still wants Foundry's own tools, those calls are answered "not
run" and the model gets one more round to answer, so a client (AnythingLLM's
@agent, Open WebUI) never ends a turn with no answer and no tool call. The
tool list stays the same on that round (prompt cache). A model that still won't
answer gets a visible note instead of silence."""
import json

import pytest

from foundry_router.pool.protocols import ChatResult
from foundry_router.tools.mcp_client import ToolResult
from foundry_router.tools.sync import ToolDef

WRAP_MARK = "Tool-round limit reached"


def _has_wrap(messages):
    return any(m.get("role") == "user" and WRAP_MARK in str(m.get("content")) for m in messages)


class LoopPool:
    """Always calls the persona's search tool; answers once asked to wrap up
    (unless `stubborn`)."""

    def __init__(self, stubborn=False):
        self.stubborn = stubborn
        self.seen_tools, self.convos = [], []

    def available_models(self):
        return {"local-a": ["o1"]}

    def backend_info(self, m):
        return {"name": "o1", "type": "ollama", "url": "http://o"} if m == "local-a" else None

    def backend_status(self):
        return []

    async def loaded_models(self):
        return set()

    def active_calls(self):
        return []

    def _turn(self, messages, tools):
        self.seen_tools.append([t["function"]["name"] for t in tools or []])
        self.convos.append(messages)
        n = len(self.convos)
        if _has_wrap(messages) and not self.stubborn:
            return ChatResult(content="Final answer: Strata forks are A and B.",
                              prompt_tokens=9, completion_tokens=8, finish_reason="stop")
        return ChatResult(content=f"searching more ({n})", tool_calls=[
            {"id": f"c{n}", "name": "searxng__search", "arguments": {"q": str(n)}}],
            prompt_tokens=5, completion_tokens=2, finish_reason="tool_calls")

    async def chat(self, model, messages, tools=None, **kw):
        return self._turn(messages, tools), "o1"

    async def chat_stream(self, model, messages, tools=None, **kw):
        res = self._turn(messages, tools)
        if res.content:
            yield {"content": res.content}
        yield res.done_frame()


@pytest.fixture()
def looped(app, client):
    """A direct persona with one Foundry-owned MCP tool, cap 2, fake MCP + pool."""
    svc = app.state.services
    cfg = svc.config_store.config.agent_brain
    persona = next(p for p in svc.personas.list(enabled_only=True))
    svc.personas.upsert(persona["virtual_name"], preferred_mcp_tools=json.dumps(["searxng"]),
                        mcp_tools_in_direct=1, mcp_with_client_tools="all")
    td = ToolDef(name="searxng__search", kind="mcp", description="search",
                 parameters={"type": "object"}, server="searxng", mcp_tool="search")
    svc.tool_registry.tools[td.name] = td
    ran = []

    async def fake_rich(server, tool, args, **kw):
        ran.append(args)
        return ToolResult([{"type": "text", "text": "results"}])
    old = (svc.mcp.call_tool_rich, svc.pool, cfg.worker_tool_max_steps, cfg.direct_stream,
           cfg.direct_stream_heartbeat_seconds)
    svc.mcp.call_tool_rich = fake_rich
    cfg.worker_tool_max_steps = 2

    def use(pool, stream=False):
        svc.pool = pool
        cfg.direct_stream, cfg.direct_stream_heartbeat_seconds = stream, 0
    use.persona = persona["virtual_name"]
    use.ran = ran
    use.svc = svc
    yield use
    (svc.mcp.call_tool_rich, svc.pool, cfg.worker_tool_max_steps, cfg.direct_stream,
     cfg.direct_stream_heartbeat_seconds) = old


BODY_TOOLS = [{"type": "function", "function": {"name": "write_to_file"}}]


def _post(client, model, stream):
    return client.post("/api/chat", json={
        "model": model, "stream": stream, "tools": BODY_TOOLS,
        "messages": [{"role": "user", "content": "research strata"}]})


def _last_request(svc):
    return svc.db.query_one("SELECT guardrail_events FROM request_log ORDER BY id DESC LIMIT 1")


def test_buffered_cap_asks_for_a_final_answer(looped, client):
    pool = LoopPool()
    looped(pool)
    r = _post(client, looped.persona, False).json()
    # rounds 1..2 run the tool, round 3 hits the cap, round 4 is the wrap-up
    assert len(looped.ran) == 2 and len(pool.convos) == 4
    assert r["message"]["content"].endswith("Final answer: Strata forks are A and B.")
    assert "searching more (1)" in r["message"]["content"]          # interim text kept
    assert not r["message"].get("tool_calls")                        # nothing handed out
    wrap = pool.convos[3]
    assert _has_wrap(wrap) and not _has_wrap(pool.convos[2])
    # the pending call stays paired with a "not run" result
    call = next(m for m in wrap if m.get("role") == "assistant" and m.get("tool_calls")
                and m["tool_calls"][0]["id"] == "c3")
    assert call
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "c3"
               and "NOT RUN" in m["content"] for m in wrap)
    assert pool.seen_tools[3] == pool.seen_tools[0]                  # same tools: cache hit
    ge = _last_request(looped.svc)["guardrail_events"]
    assert "asked the model for its final answer" in ge


def test_buffered_cap_still_no_answer_shows_a_note(looped, client):
    looped(LoopPool(stubborn=True))
    r = _post(client, looped.persona, False).json()
    msg = r["message"]
    assert "stopped after 2 tool rounds" in msg["content"]
    assert not msg.get("tool_calls")
    assert "still no final answer" in _last_request(looped.svc)["guardrail_events"]


def _stream_lines(r):
    return [json.loads(x) for x in r.text.splitlines() if x.strip()]


def test_streaming_cap_asks_for_a_final_answer(looped, client):
    pool = LoopPool()
    looped(pool, stream=True)
    lines = _stream_lines(_post(client, looped.persona, True))
    text = "".join(l["message"].get("content") or "" for l in lines)
    thinking = "".join(l["message"].get("thinking") or "" for l in lines)
    assert text.endswith("Final answer: Strata forks are A and B.")
    assert "tool-round limit (2) reached" in thinking
    assert len(pool.convos) == 4 and _has_wrap(pool.convos[3])
    assert not lines[-1]["message"].get("tool_calls")


def test_streaming_cap_still_no_answer_shows_a_note(looped, client):
    looped(LoopPool(stubborn=True), stream=True)
    lines = _stream_lines(_post(client, looped.persona, True))
    text = "".join(l["message"].get("content") or "" for l in lines)
    assert "stopped after 2 tool rounds" in text
    assert not lines[-1]["message"].get("tool_calls")


def test_client_calls_at_the_cap_still_go_to_the_client(looped, client):
    """Own + client calls together at the cap: the client's call is handed out
    (the client's own loop continues), no wrap-up round."""
    class Mixed(LoopPool):
        def _turn(self, messages, tools):
            res = super()._turn(messages, tools)
            if len(self.convos) == 3:
                res.tool_calls = res.tool_calls + [
                    {"id": "w1", "name": "write_to_file", "arguments": {"path": "a"}}]
            return res
    pool = Mixed()
    looped(pool)
    r = _post(client, looped.persona, False).json()
    assert [t["function"]["name"] for t in r["message"]["tool_calls"]] == ["write_to_file"]
    assert len(pool.convos) == 3
