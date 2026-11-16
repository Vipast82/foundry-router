"""v0.181.160: a main-model persona keeps its route order and its helpers when the
chat program brings its own tools (AnythingLLM @agent, Open WebUI tools)."""
import inspect
import json

import pytest

from foundry_router import lanes
from foundry_router.pool.base import AllBackendsFailed
from foundry_router.pool.protocols import ChatResult

TOOLS = [{"type": "function", "function": {"name": "write_to_file", "parameters": {"type": "object"}}}]
USER = {"role": "user", "content": "write a.md"}


class Pool:
    """Models big (first on the route), small (second) and helper h1, each on its
    own backend. `script[model]` is that model's replies in order."""

    def __init__(self, script, down=()):
        self.script = {m: list(v) for m, v in script.items()}
        self.down = set(down)
        self.asked = []
        self.backends = {}

    def available_models(self):
        return {m: [m + "-be"] for m in self.script}

    def backend_info(self, m):
        return ({"name": m + "-be", "type": "ollama", "url": "http://x", "api_key": None}
                if m in self.script else None)

    def backend_status(self):
        return []

    async def loaded_models(self):
        return set()

    def active_calls(self):
        return []

    async def chat(self, model, messages, tools=None, **kw):
        self.asked.append({"model": model, "tools": [t["function"]["name"] for t in (tools or [])],
                           "messages": [dict(m) for m in messages]})
        if model in self.down:
            raise AllBackendsFailed(f"{model} is down")
        return self.script[model].pop(0), model + "-be"

    async def chat_stream(self, model, messages, tools=None, **kw):
        res, _ = await self.chat(model, messages, tools=tools)
        if res.content:
            yield {"content": res.content}
        yield res.done_frame()


@pytest.fixture()
def main_persona(app, client):
    svc = app.state.services
    cfg = svc.config_store.config.agent_brain
    lanes._PINS.clear()
    name = "Main-With-Client-Tools"
    svc.personas.upsert(name, execution_mode="main", benchmark_category="general_chat",
                        pinned_models=json.dumps(["big", "small"]), enabled=1, expose_tool_trail=0,
                        helpers=json.dumps([{"role": "deep", "models": ["h1"], "what": "hard parts"},
                                            {"role": "fast", "models": ["h1"]}]))
    for i, m in enumerate(("big", "small", "h1")):
        svc.registry.upsert_auto(m, source="discovery", relative_cost_tier="free")
        svc.registry.upsert_benchmark(m, "general_chat", 70 - i, "measured", "independent", confidence=0.9)
    old = (svc.pool, svc.agent.pool, cfg.direct_stream, cfg.direct_stream_heartbeat_seconds)

    def use(pool):
        svc.pool = svc.agent.pool = pool
        cfg.direct_stream, cfg.direct_stream_heartbeat_seconds = False, 0
        return pool
    use.name, use.svc = name, svc
    yield use
    svc.pool, svc.agent.pool, cfg.direct_stream, cfg.direct_stream_heartbeat_seconds = old
    lanes._PINS.clear()


def _chat(client, name, messages=None):
    r = client.post("/api/chat", json={"model": name, "stream": False, "tools": TOOLS,
                                       "messages": messages or [USER]})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_route_order_decides_and_the_helper_is_offered(main_persona, client):
    pool = main_persona(Pool({"big": [ChatResult(content="done", finish_reason="stop")],
                              "small": [], "h1": []}))
    out = _chat(client, main_persona.name)
    assert out["message"]["content"] == "done"
    assert [a["model"] for a in pool.asked] == ["big"]                 # first on the persona's route
    assert pool.asked[0]["tools"] == ["write_to_file", "ask_helper"]   # the client's tool and the helper


def test_the_helper_runs_here_and_the_client_tool_goes_back(main_persona, client):
    pool = main_persona(Pool({
        "big": [ChatResult(content="", finish_reason="tool_calls", tool_calls=[
                    {"id": "h", "name": "ask_helper", "arguments": {"helper": "deep", "task": "outline it"}}]),
                ChatResult(content="writing", finish_reason="tool_calls", tool_calls=[
                    {"id": "w", "name": "write_to_file", "arguments": {"path": "a.md", "text": "OUTLINE"}}])],
        "small": [], "h1": [ChatResult(content="OUTLINE: 1, 2, 3")]}))
    out = _chat(client, main_persona.name)
    assert [a["model"] for a in pool.asked] == ["big", "h1", "big"]
    assert pool.asked[1]["tools"] == [] and len(pool.asked[1]["messages"]) == 2     # the helper sees only its task
    tool_msgs = [m for m in pool.asked[2]["messages"] if m.get("role") == "tool"]
    assert tool_msgs[-1]["content"] == "[answer from the deep helper, h1]\nOUTLINE: 1, 2, 3"
    calls = out["message"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["write_to_file"]      # only the client's own call goes back


def test_two_helpers_in_one_turn_and_a_dead_first_model(main_persona, client):
    pool = main_persona(Pool({
        "big": [],
        "small": [ChatResult(content="", finish_reason="tool_calls", tool_calls=[
                      {"id": "a", "name": "ask_helper", "arguments": {"helper": "deep", "task": "one"}},
                      {"id": "b", "name": "ask_helper", "arguments": {"helper": "fast", "task": "two"}}]),
                  ChatResult(content="both used", finish_reason="stop")],
        "h1": [ChatResult(content="A"), ChatResult(content="B")]}, down={"big"}))
    out = _chat(client, main_persona.name)
    assert out["message"]["content"] == "both used"
    assert [a["model"] for a in pool.asked if a["model"] != "h1"] == ["big", "small", "small"]   # handed over
    tools = [m for m in pool.asked[-1]["messages"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tools] == ["a", "b"]
    assert {m["content"].splitlines()[0] for m in tools} == {"[answer from the deep helper, h1]",
                                                             "[answer from the fast helper, h1]"}


def test_other_personas_are_untouched():
    from foundry_router.facade import ollama_api
    src = inspect.getsource(ollama_api._direct_dispatch_chat)
    assert 'main_mode = (persona.get("execution_mode") == "main") and not strict and not summary_req' in src
    assert "elif model_id is not None:" in src and "if main_ladder:" in src
    assert "main_mode, main_ladder = False, []" in src           # nothing on the route: the old pick
    assert "if main_mode:" in src and "svc.agent._digest(main_ctx" in src
