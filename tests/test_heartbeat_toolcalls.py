"""Streaming keep-alive heartbeats (item 4: a 422s failover-then-cold-load
chain produced a real answer after the client had already closed the idle
connection — flowing bytes reset proxy/client idle clocks) and per-request
MCP tool-call logging (item 5: nothing showed which MCP tools a request
actually invoked, or whether an MCP failure was the server's fault or the
backend's)."""

import asyncio
import json

from foundry_router.brain.agent import AgentRunner, RequestContext
from foundry_router.config import AgentBrainConfig, GuardrailsConfig, MeridianConfig
from foundry_router.db import Database
from foundry_router.guardrails import GuardrailEngine, RequestGuardState
from foundry_router.pool.protocols import ChatResult
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.tools.sync import _ASK_PARAMS, ToolDef, ToolRegistry
from foundry_router.usage import MeridianUsage, RequestLogger


class SlowPool:
    def __init__(self, delay=0.0):
        self.delay = delay

    def available_models(self):
        return {"slow-model": ["b"]}

    def backend_info(self, m):
        return {"name": "b", "type": "ollama", "url": "http://x", "api_key": None}

    async def chat(self, model, messages, tools=None, options=None, max_tokens=4096):
        await asyncio.sleep(self.delay)
        return ChatResult(content="done"), "b"


class FakeMCP:
    def __init__(self, fail=False):
        self.fail = fail

    async def call_tool(self, server, tool, arguments):
        if self.fail:
            raise RuntimeError("boom")
        return "search results"


class ScriptedBrain:
    def __init__(self, responses):
        self.responses = list(responses)
        self.cfg = AgentBrainConfig()

    async def chat(self, messages, tools=None, **kwargs):
        return self.responses.pop(0)

    async def complete(self, prompt):
        return ""


def _make(tmp_path, brain_responses, pool, heartbeat=0.0, mcp_fail=False,
          persona=None):
    db = Database(tmp_path / "h.sqlite")
    registry = ModelRegistry(db)
    registry.upsert_auto("slow-model", source="discovery", relative_cost_tier="free")
    tool_registry = ToolRegistry(db, registry, MCPManager([], db))
    tool_registry.mcp = FakeMCP(fail=mcp_fail)
    tool_registry.tools["ask_slow_model"] = ToolDef(
        name="ask_slow_model", kind="model", description="",
        model_id="slow-model", parameters=_ASK_PARAMS)
    tool_registry.tools["searxng_web_search"] = ToolDef(
        name="searxng_web_search", kind="mcp", description="",
        parameters={}, server="searxng", mcp_tool="web_search")
    meridian = MeridianUsage(MeridianConfig(), client=None, db=db)
    brain = ScriptedBrain(brain_responses)
    brain.cfg.heartbeat_seconds = heartbeat
    runner = AgentRunner(brain, pool, tool_registry, registry,
                         GuardrailEngine(GuardrailsConfig(), db, meridian), meridian)
    ctx = RequestContext(
        persona=persona or {"virtual_name": "Foundry-Chat",
                            "benchmark_category": "general_chat"},
        messages=[{"role": "user", "content": "do the thing"}],
        guard=RequestGuardState(),
        logger=RequestLogger(db, "Foundry-Chat", "Foundry-Chat", "agent", "thing"))
    return runner, ctx, db


ASK_THEN_RETURN = lambda: [
    ChatResult(tool_calls=[{"id": "1", "name": "ask_slow_model",
                            "arguments": {"prompt": "work"}}]),
    ChatResult(tool_calls=[{"id": "2", "name": "return_to_user",
                            "arguments": {"use_last_result": True}}]),
]


# -- heartbeats ----------------------------------------------------------------------

async def test_heartbeat_narration_during_slow_worker(tmp_path):
    runner, ctx, _ = _make(tmp_path, ASK_THEN_RETURN(), SlowPool(0.08),
                           heartbeat=0.02)
    events = [ev async for ev in runner.run(ctx)]
    beats = [ev for ev in events if "Still working" in ev.text]
    assert beats and all(ev.kind == "think" for ev in beats)
    assert "slow-model" in beats[0].text  # says WHAT it's waiting on
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers[0].text == "done"      # result unaffected by the wrapper


async def test_heartbeat_zero_disables(tmp_path):
    runner, ctx, _ = _make(tmp_path, ASK_THEN_RETURN(), SlowPool(0.05),
                           heartbeat=0)
    events = [ev async for ev in runner.run(ctx)]
    assert not any("Still working" in ev.text for ev in events)
    assert [ev for ev in events if ev.kind == "answer"][0].text == "done"


async def test_pipeline_execute_heartbeat(tmp_path):
    # The originally-observed failure mode was pipeline Execute dying on cold
    # loads; the keep-alive must flow there too (yielded, not emitted).
    runner, ctx, _ = _make(tmp_path, [], SlowPool(0.08), heartbeat=0.02,
                           persona={"virtual_name": "Foundry-Coding",
                                    "benchmark_category": "coding",
                                    "execution_mode": "pipeline"})
    events = [ev async for ev in runner.run_pipeline(ctx)]
    assert any("Still working" in ev.text for ev in events)
    assert [ev for ev in events if ev.kind == "answer"][0].text == "done"


# -- MCP tool-call logging -------------------------------------------------------------

MCP_THEN_RETURN = lambda: [
    ChatResult(tool_calls=[{"id": "1", "name": "searxng_web_search",
                            "arguments": {"query": "ferns"}}]),
    ChatResult(tool_calls=[{"id": "2", "name": "return_to_user",
                            "arguments": {"use_last_result": True}}]),
]


async def test_mcp_success_recorded_per_request(tmp_path):
    runner, ctx, db = _make(tmp_path, MCP_THEN_RETURN(), SlowPool())
    events = [ev async for ev in runner.run(ctx)]
    assert ctx.logger.tool_calls == [{
        "tool": "web_search", "server": "searxng",
        "duration_ms": ctx.logger.tool_calls[0]["duration_ms"], "ok": True}]
    thinks = " | ".join(ev.text for ev in events if ev.kind == "think")
    assert "completed in" in thinks
    assert [ev for ev in events if ev.kind == "answer"][0].text == "search results"


async def test_mcp_failure_logged_with_readable_error(tmp_path):
    runner, ctx, db = _make(tmp_path, [
        ChatResult(tool_calls=[{"id": "1", "name": "searxng_web_search",
                                "arguments": {"query": "ferns"}}]),
        ChatResult(tool_calls=[{"id": "2", "name": "return_to_user",
                                "arguments": {"answer": "Search is down."}}]),
    ], SlowPool(), mcp_fail=True)
    events = [ev async for ev in runner.run(ctx)]
    call = ctx.logger.tool_calls[0]
    assert call["ok"] is False
    assert "RuntimeError: boom" in call["error"]  # describe_exception, not str(e)
    # Events entry at the same layer as backend_pool warnings
    rows = db.query("SELECT * FROM event_log WHERE source='mcp' AND level='warning'")
    assert len(rows) == 1 and "searxng/web_search" in rows[0]["message"]


def test_request_log_tool_calls_roundtrip(tmp_path):
    db = Database(tmp_path / "r.sqlite")
    logger = RequestLogger(db, "P", "P", "agent", "hi")
    logger.record_tool_call("web_search", "searxng", 1234, ok=True)
    logger.record_tool_call("md", "crawl4ai", 200, ok=False, error="boom")
    logger.finish("ok")
    row = db.query_one("SELECT * FROM request_log")
    calls = json.loads(row["tool_calls"])
    assert calls[0] == {"tool": "web_search", "server": "searxng",
                        "duration_ms": 1234, "ok": True}
    assert calls[1]["error"] == "boom" and calls[1]["ok"] is False
