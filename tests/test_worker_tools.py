"""Worker-side tool calling (opt-out default): the selected worker owns its own
MCP tool loop instead of the brain running it; a tool-attached persona routes
here unless it sets brain_handles_tools; any tool failure hands the request to
the brain-mediated path; tool calls log through the same request_log mechanism."""

import json
import types

from foundry_router.brain.agent import AgentRunner, RequestContext
from foundry_router.config import (AgentBrainConfig, GuardrailsConfig, MeridianConfig)
from foundry_router.db import Database
from foundry_router.facade.ollama_api import _run_events
from foundry_router.guardrails import GuardrailEngine, RequestGuardState
from foundry_router.pool.protocols import ChatResult
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.sync import ToolDef, ToolRegistry
from foundry_router.usage import MeridianUsage, RequestLogger


# -- routing: opt-out default -----------------------------------------------------

def _ctx(preferred, brain_handles=0, mode="agent"):
    return types.SimpleNamespace(
        persona={"preferred_mcp_tools": json.dumps(preferred),
                 "brain_handles_tools": brain_handles},
        logger=types.SimpleNamespace(mode=mode))


class _FakeAgent:
    def run(self, ctx): return "brain"
    def run_pipeline(self, ctx): return "pipeline"
    def run_worker_tools(self, ctx): return "worker"


def test_routing_tool_persona_defaults_to_worker():
    svc = types.SimpleNamespace(agent=_FakeAgent())
    assert _run_events(svc, _ctx(["searxng"])) == "worker"


def test_routing_opt_out_to_brain():
    svc = types.SimpleNamespace(agent=_FakeAgent())
    assert _run_events(svc, _ctx(["searxng"], brain_handles=1)) == "brain"


def test_routing_toolless_persona_stays_on_brain():
    svc = types.SimpleNamespace(agent=_FakeAgent())
    assert _run_events(svc, _ctx([])) == "brain"


def test_routing_pipeline_untouched():
    svc = types.SimpleNamespace(agent=_FakeAgent())
    assert _run_events(svc, _ctx(["searxng"], mode="pipeline")) == "pipeline"


# -- the loop itself --------------------------------------------------------------

class FakeMCP:
    servers = {"searxng": None}

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def executes_code(self, server):
        return False

    async def call_tool(self, server, tool, args):
        self.calls.append((server, tool, args))
        if self.fail:
            raise RuntimeError("searxng BrokenResourceError")
        return "search result: foundry router is an LLM router"

    async def list_all(self):
        return {}


class WorkerPool:
    def __init__(self, script):
        self.script = list(script)
        self.tool_passes = []

    def available_models(self):
        return {"worker": ["b"]}

    def backend_info(self, m):
        return {"name": "b", "type": "ollama", "url": "http://x", "api_key": None} \
            if m == "worker" else None

    async def chat(self, model, messages, tools=None, options=None, max_tokens=4096):
        self.tool_passes.append(tools is not None)     # worker gets tool schemas
        return self.script.pop(0), "b"


class ScriptedBrain:
    def __init__(self, script):
        self.script = list(script)
        self.cfg = AgentBrainConfig()

    async def chat(self, messages, tools=None, **kwargs):
        return self.script.pop(0)

    async def complete(self, prompt):
        return ""


def _tool(name, **args):
    return ChatResult(tool_calls=[{"id": "1", "name": name, "arguments": args}])


def _make(tmp_path, worker_script, mcp_fail=False, brain_script=None):
    db = Database(tmp_path / "w.sqlite")
    registry = ModelRegistry(db)
    registry.upsert_auto("worker", source="discovery", relative_cost_tier="free")
    registry.upsert_benchmark("worker", "general_chat", 70, "measured",
                              "independent", confidence=0.9)
    mcp = FakeMCP(fail=mcp_fail)
    tool_registry = ToolRegistry(db, registry, mcp)
    tool_registry.tools["searxng_web_search"] = ToolDef(
        name="searxng_web_search", kind="mcp", description="[MCP:searxng] search",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        server="searxng", mcp_tool="search")
    pool = WorkerPool(worker_script)
    meridian = MeridianUsage(MeridianConfig(), client=None, db=db)
    runner = AgentRunner(ScriptedBrain(brain_script or []), pool, tool_registry,
                         registry, GuardrailEngine(GuardrailsConfig(), db, meridian),
                         meridian)
    ctx = RequestContext(
        persona={"virtual_name": "Foundry-Chat", "benchmark_category": "general_chat",
                 "preferred_mcp_tools": json.dumps(["searxng"])},
        messages=[{"role": "user", "content": "what is foundry router"}],
        guard=RequestGuardState(),
        logger=RequestLogger(db, "Foundry-Chat", "Foundry-Chat", "agent", "q"))
    return runner, ctx, pool, mcp


async def test_worker_owns_the_tool_loop(tmp_path):
    runner, ctx, pool, mcp = _make(tmp_path, worker_script=[
        _tool("searxng_web_search", query="foundry router"),
        ChatResult(content="Foundry Router is an LLM routing middleware."),
    ])
    events = [ev async for ev in runner.run_worker_tools(ctx)]
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers[0].text == "Foundry Router is an LLM routing middleware."
    assert len(mcp.calls) == 1 and mcp.calls[0][0] == "searxng"   # worker ran the tool
    assert all(passed for passed in pool.tool_passes)             # worker got schemas
    # logged through the same request_log.tool_calls mechanism
    assert ctx.logger.tool_calls[0]["server"] == "searxng"
    assert ctx.logger.tool_calls[0]["ok"] is True


async def test_worker_answers_without_tools_when_not_needed(tmp_path):
    runner, ctx, pool, mcp = _make(tmp_path, worker_script=[
        ChatResult(content="No search needed — here's the answer."),
    ])
    events = [ev async for ev in runner.run_worker_tools(ctx)]
    assert [ev for ev in events if ev.kind == "answer"][0].text \
        == "No search needed — here's the answer."
    assert mcp.calls == []                                        # no tool call made


async def test_tool_failure_hands_off_to_brain(tmp_path):
    runner, ctx, pool, mcp = _make(
        tmp_path,
        worker_script=[_tool("searxng_web_search", query="x")],
        mcp_fail=True,
        brain_script=[_tool("return_to_user", answer="brain completed it")])
    events = [ev async for ev in runner.run_worker_tools(ctx)]
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers[0].text == "brain completed it"               # brain took over
    assert ctx.logger.tool_calls[0]["ok"] is False               # failure still logged
    thinks = " | ".join(ev.text for ev in events if ev.kind == "think")
    assert "handing" in thinks.lower() and "brain" in thinks.lower()


async def test_unknown_tool_hands_off_to_brain(tmp_path):
    runner, ctx, pool, mcp = _make(
        tmp_path,
        worker_script=[_tool("nonexistent_tool", foo="bar")],
        brain_script=[_tool("return_to_user", answer="brain fallback")])
    events = [ev async for ev in runner.run_worker_tools(ctx)]
    assert [ev for ev in events if ev.kind == "answer"][0].text == "brain fallback"
