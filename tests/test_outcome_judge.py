"""Outcome-based escalation (spec §2): both judge configurations tested
independently (they're meant to be genuinely interchangeable), adequate
short-circuit, escalation on inadequate, and fail-open on judge failure."""

import json

from foundry_router.brain.agent import AgentRunner, RequestContext
from foundry_router.config import AgentBrainConfig, GuardrailsConfig, MeridianConfig
from foundry_router.db import Database
from foundry_router.guardrails import GuardrailEngine, RequestGuardState
from foundry_router.pool.protocols import ChatResult
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.tools.sync import ToolDef, ToolRegistry
from foundry_router.usage import MeridianUsage, RequestLogger

SMALL, BIG, HAIKU = "local-small", "local-big", "claude-haiku-4-5"

INADEQUATE = '{"adequate": false, "reasoning": "misses the requested edge cases"}'
ADEQUATE = '{"adequate": true, "reasoning": "covers the request"}'


class ScriptedPool:
    def __init__(self, types, responses):
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
                          prompt_tokens=5, completion_tokens=5), "b"


class ScriptedBrain:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.cfg = AgentBrainConfig()

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return self.responses.pop(0)

    async def complete(self, prompt):
        return "not json at all"


def _tool_call(name, **args):
    return ChatResult(tool_calls=[{"id": "x", "name": name, "arguments": args}])


def _make(tmp_path, pool, brain_responses, judge_mode):
    db = Database(tmp_path / "j.sqlite")
    registry = ModelRegistry(db)
    for m, score in ((SMALL, 60), (BIG, 90)):
        registry.upsert_auto(m, source="discovery", relative_cost_tier="free")
        registry.upsert_benchmark(m, "general_chat", score, "measured",
                                  "independent", confidence=0.9)
    tool_registry = ToolRegistry(db, registry, MCPManager([], db))
    for m in pool.types:
        name = "ask_" + m.replace("-", "_")
        tool_registry.tools[name] = ToolDef(
            name=name, kind="model", description="", model_id=m,
            parameters={"type": "object", "properties": {"prompt": {"type": "string"}}})
    meridian = MeridianUsage(MeridianConfig(), client=None, db=db)
    brain = ScriptedBrain(brain_responses)
    runner = AgentRunner(brain, pool, tool_registry, registry,
                         GuardrailEngine(GuardrailsConfig(), db, meridian), meridian)
    ctx = RequestContext(
        persona={"virtual_name": "Foundry-RAG", "benchmark_category": "general_chat",
                 "outcome_judge": judge_mode},
        messages=[{"role": "user", "content": "explain X thoroughly"}],
        guard=RequestGuardState(),
        logger=RequestLogger(db, "Foundry-RAG", "Foundry-RAG", "agent", "explain X"))
    return runner, ctx, brain


async def _run(runner, ctx):
    return [ev async for ev in runner.run(ctx)]


async def test_local_large_judge_escalates_on_inadequate(tmp_path):
    pool = ScriptedPool(
        {SMALL: "ollama", BIG: "ollama"},
        {SMALL: ["weak answer"],
         BIG: [INADEQUATE,          # judge verdict (best local != executor)
               "strong answer"]})   # escalation target
    runner, ctx, brain = _make(tmp_path, pool, [
        _tool_call("ask_local_small", prompt="explain X"),
        _tool_call("return_to_user", use_last_result=True),   # intercepted by judge
        _tool_call("ask_local_big", prompt="explain X properly"),
        _tool_call("return_to_user", use_last_result=True),
    ], judge_mode="local_large")
    events = await _run(runner, ctx)

    # the judge ran on local-big (best local excluding the executor)
    judge_call = pool.calls[1]
    assert judge_call[0] == BIG and "weak answer" in judge_call[1]
    # the brain received the escalation instruction as a tool result
    escalation_msgs = [m for m in brain.calls[2]
                       if m.get("role") == "tool" and "OUTCOME JUDGE" in m["content"]]
    assert escalation_msgs and "INADEQUATE" in escalation_msgs[0]["content"]
    # and the final answer is the escalated one
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers[0].text == "strong answer"


async def test_paid_judge_uses_cheapest_claude_and_adequate_short_circuits(tmp_path):
    pool = ScriptedPool(
        {SMALL: "ollama", HAIKU: "anthropic-compatible"},
        {SMALL: ["decent answer"], HAIKU: [ADEQUATE]})
    runner, ctx, brain = _make(tmp_path, pool, [
        _tool_call("ask_local_small", prompt="explain X"),
        _tool_call("return_to_user", use_last_result=True),
    ], judge_mode="paid")
    events = await _run(runner, ctx)

    assert pool.calls[1][0] == HAIKU              # cheapest Claude judged
    assert len(brain.calls) == 2                  # no escalation round
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers[0].text == "decent answer"     # local answer forwarded free
    thinks = " | ".join(ev.text for ev in events if ev.kind == "think")
    assert "adequate" in thinks


async def test_judge_runs_at_most_once(tmp_path):
    pool = ScriptedPool(
        {SMALL: "ollama", BIG: "ollama"},
        {SMALL: ["weak answer", "second weak answer"],
         BIG: [INADEQUATE]})
    runner, ctx, brain = _make(tmp_path, pool, [
        _tool_call("ask_local_small", prompt="try"),
        _tool_call("return_to_user", use_last_result=True),   # judged: inadequate
        _tool_call("ask_local_small", prompt="try again"),    # brain escalates... locally
        _tool_call("return_to_user", use_last_result=True),   # NOT judged again
    ], judge_mode="local_large")
    events = await _run(runner, ctx)
    # judge (BIG) called exactly once despite two return_to_user attempts
    assert [m for m, _ in pool.calls].count(BIG) == 1
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers[0].text == "second weak answer"


async def test_judge_fails_open(tmp_path):
    # judge_mode local_large but no OTHER local model exists -> falls to the
    # brain, whose output isn't parseable JSON -> default adequate, answer flows
    pool = ScriptedPool({SMALL: "ollama"}, {SMALL: ["only answer"]})
    runner, ctx, brain = _make(tmp_path, pool, [
        _tool_call("ask_local_small", prompt="q"),
        _tool_call("return_to_user", use_last_result=True),
    ], judge_mode="local_large")
    events = await _run(runner, ctx)
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers[0].text == "only answer"


async def test_no_judge_configured_no_extra_calls(tmp_path):
    pool = ScriptedPool({SMALL: "ollama", BIG: "ollama"}, {SMALL: ["answer"], BIG: []})
    runner, ctx, brain = _make(tmp_path, pool, [
        _tool_call("ask_local_small", prompt="q"),
        _tool_call("return_to_user", use_last_result=True),
    ], judge_mode=None)
    events = await _run(runner, ctx)
    assert [m for m, _ in pool.calls] == [SMALL]  # nothing judged
    assert [ev for ev in events if ev.kind == "answer"][0].text == "answer"
