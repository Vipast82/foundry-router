"""Coding pipeline (spec §1): Prepare -> Execute -> Check, bounded retry,
per-persona check opt-out, and degrade paths."""

import json

from foundry_router.brain.agent import AgentRunner, RequestContext
from foundry_router.config import AgentBrainConfig, GuardrailsConfig, MeridianConfig
from foundry_router.db import Database
from foundry_router.guardrails import GuardrailEngine, RequestGuardState
from foundry_router.pool.protocols import ChatResult
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.tools.sync import ToolRegistry
from foundry_router.usage import MeridianUsage, RequestLogger

HAIKU = "claude-haiku-4-5"
CODER = "local-coder"


class ScriptedPool:
    """Per-model scripted responses; records (model, prompt) calls in order."""

    def __init__(self, types: dict, responses: dict):
        self.types = types
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[tuple[str, str]] = []

    def available_models(self):
        return {m: ["b"] for m in self.types}

    def backend_info(self, model):
        t = self.types.get(model)
        return {"name": "b", "type": t, "url": "http://x", "api_key": None} if t else None

    async def chat(self, model, messages, tools=None, options=None, max_tokens=4096):
        self.calls.append((model, messages[-1]["content"]))
        return ChatResult(content=self.responses[model].pop(0),
                          prompt_tokens=10, completion_tokens=10), "b"


class DummyBrain:
    def __init__(self):
        self.cfg = AgentBrainConfig()

    async def complete(self, prompt):
        return ""


def _runner(tmp_path, pool, persona_extra=None):
    db = Database(tmp_path / "p.sqlite")
    registry = ModelRegistry(db)
    registry.upsert_auto(CODER, source="discovery", relative_cost_tier="free")
    registry.upsert_benchmark(CODER, "coding", 80, "measured", "independent",
                              confidence=0.9)
    meridian = MeridianUsage(MeridianConfig(), client=None, db=db)
    runner = AgentRunner(DummyBrain(), pool,
                         ToolRegistry(db, registry, MCPManager([], db)),
                         registry, GuardrailEngine(GuardrailsConfig(), db, meridian),
                         meridian)
    persona = {"virtual_name": "Foundry-Coding", "benchmark_category": "coding",
               "execution_mode": "pipeline", "pipeline_check_enabled": 1,
               "guardrail_overrides": json.dumps({"max_paid_calls_per_request": 2})}
    persona.update(persona_extra or {})
    ctx = RequestContext(
        persona=persona,
        messages=[{"role": "user", "content": "write a snake game"}],
        guard=RequestGuardState(),
        logger=RequestLogger(db, "Foundry-Coding", "Foundry-Coding",
                             "pipeline", "write a snake game"))
    return runner, ctx


async def _run(runner, ctx):
    return [ev async for ev in runner.run_pipeline(ctx)]


async def test_prepare_execute_check_happy_path(tmp_path):
    pool = ScriptedPool(
        {HAIKU: "anthropic-compatible", CODER: "ollama"},
        {HAIKU: ["STRUCTURED SPEC: snake game, canvas, arrow keys",
                 '{"adequate": true, "feedback": ""}'],
         CODER: ["<html>the game code</html>"]})
    runner, ctx = _runner(tmp_path, pool)
    events = await _run(runner, ctx)

    models_called = [m for m, _ in pool.calls]
    assert models_called == [HAIKU, CODER, HAIKU]  # prepare, execute, check
    assert "write a snake game" in pool.calls[0][1]        # prepare sees raw request
    assert "STRUCTURED SPEC" in pool.calls[1][1]           # execute sees the spec
    assert "the game code" in pool.calls[2][1]             # check sees the code
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers[0].text == "<html>the game code</html>"
    thinks = " | ".join(ev.text for ev in events if ev.kind == "think")
    assert "Check passed" in thinks


async def test_check_failure_triggers_one_retry(tmp_path):
    pool = ScriptedPool(
        {HAIKU: "anthropic-compatible", CODER: "ollama"},
        {HAIKU: ["SPEC", '{"adequate": false, "feedback": "no collision detection"}'],
         CODER: ["buggy code", "fixed code with collisions"]})
    runner, ctx = _runner(tmp_path, pool)
    events = await _run(runner, ctx)

    coder_calls = [p for m, p in pool.calls if m == CODER]
    assert len(coder_calls) == 2                       # execute + exactly one retry
    assert "no collision detection" in coder_calls[1]  # feedback fed back
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers[0].text == "fixed code with collisions"


async def test_check_disabled_per_persona(tmp_path):
    pool = ScriptedPool(
        {HAIKU: "anthropic-compatible", CODER: "ollama"},
        {HAIKU: ["SPEC"], CODER: ["code"]})
    runner, ctx = _runner(tmp_path, pool,
                          persona_extra={"pipeline_check_enabled": 0})
    events = await _run(runner, ctx)
    assert [m for m, _ in pool.calls] == [HAIKU, CODER]  # no check call
    assert [ev for ev in events if ev.kind == "answer"][0].text == "code"


async def test_no_claude_degrades_to_raw_request(tmp_path):
    pool = ScriptedPool({CODER: "ollama"}, {CODER: ["code"]})
    runner, ctx = _runner(tmp_path, pool)
    events = await _run(runner, ctx)
    assert [m for m, _ in pool.calls] == [CODER]
    assert "write a snake game" in pool.calls[0][1]  # raw request used as spec
    assert [ev for ev in events if ev.kind == "answer"][0].text == "code"


async def test_no_local_coder_degrades_to_paid(tmp_path):
    pool = ScriptedPool({HAIKU: "anthropic-compatible"},
                        {HAIKU: ["SPEC", "paid-tier code"]})
    runner, ctx = _runner(tmp_path, pool)
    events = await _run(runner, ctx)
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers and answers[0].text == "paid-tier code"
