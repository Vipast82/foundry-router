"""Request-level permissive fallback (correction to 637ebc7): content policy is
NOT a ranking input — a standard model is routed on merit and only when it
actually REFUSES a specific request does the router retry once on the best
permissive model. This makes permissive models a per-request safety net for
refused content, available to every persona, rather than a blanket per-persona
avoidance that penalized capable permissive models on ordinary requests."""

from foundry_router.brain.agent import (AgentRunner, RequestContext,
                                        _looks_like_refusal)
from foundry_router.config import AgentBrainConfig, GuardrailsConfig, MeridianConfig
from foundry_router.db import Database
from foundry_router.guardrails import GuardrailEngine, RequestGuardState
from foundry_router.pool.protocols import ChatResult
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.tools.sync import _ASK_PARAMS, ToolDef, ToolRegistry
from foundry_router.usage import MeridianUsage, RequestLogger

REFUSAL = "I'm sorry, but I can't help with that request."
GOOD = "Sure — that plant is a Boston fern."


# -- detector --------------------------------------------------------------------

def test_refusal_detection_precision():
    assert _looks_like_refusal(REFUSAL)
    assert _looks_like_refusal("I cannot create that for you.")
    assert _looks_like_refusal("I must decline this request.")
    assert not _looks_like_refusal(GOOD)
    assert not _looks_like_refusal("")
    # a genuine answer that only mentions a caveat far in isn't a refusal
    long_answer = ("Your plant is a healthy fern. " * 30
                   + "I can't guarantee it survives a hard frost.")
    assert not _looks_like_refusal(long_answer)


# -- integration -----------------------------------------------------------------

class PolicyPool:
    def __init__(self, responses):
        self.responses = responses           # model -> content
        self.calls: list[str] = []

    def available_models(self):
        return {m: ["b"] for m in self.responses}

    def backend_info(self, m):
        return {"name": "b", "type": "ollama", "url": "http://x", "api_key": None} \
            if m in self.responses else None

    async def chat(self, model, messages, tools=None, options=None, max_tokens=4096):
        self.calls.append(model)
        return ChatResult(content=self.responses[model]), "b"


class ScriptedBrain:
    def __init__(self, responses):
        self.responses = list(responses)
        self.cfg = AgentBrainConfig()

    async def chat(self, messages, tools=None, **kwargs):
        return self.responses.pop(0)

    async def complete(self, prompt):
        return ""


def _make(tmp_path, pool_responses, brain_responses, policies):
    db = Database(tmp_path / "r.sqlite")
    registry = ModelRegistry(db)
    for m in pool_responses:
        registry.upsert_auto(m, source="discovery", relative_cost_tier="free",
                             content_policy=policies.get(m))
    tool_registry = ToolRegistry(db, registry, MCPManager([], db))
    for m in pool_responses:
        name = "ask_" + m.replace("-", "_").replace(":", "_")
        tool_registry.tools[name] = ToolDef(name=name, kind="model", description="",
                                            model_id=m, parameters=_ASK_PARAMS)
    pool = PolicyPool(pool_responses)
    meridian = MeridianUsage(MeridianConfig(), client=None, db=db)
    runner = AgentRunner(ScriptedBrain(brain_responses), pool, tool_registry,
                         registry, GuardrailEngine(GuardrailsConfig(), db, meridian),
                         meridian)
    ctx = RequestContext(
        persona={"virtual_name": "Foundry-Chat", "benchmark_category": "general_chat"},
        messages=[{"role": "user", "content": "identify this plant"}],
        guard=RequestGuardState(),
        logger=RequestLogger(db, "Foundry-Chat", "Foundry-Chat", "agent", "plant"))
    return runner, ctx, pool


def _tc(name, **args):
    return ChatResult(tool_calls=[{"id": "x", "name": name, "arguments": args}])


async def test_refusal_falls_back_to_permissive(tmp_path):
    runner, ctx, pool = _make(
        tmp_path,
        pool_responses={"standard": REFUSAL, "wild": GOOD},
        brain_responses=[_tc("ask_standard", prompt="identify this plant"),
                         _tc("return_to_user", use_last_result=True)],
        policies={"wild": "permissive"})
    events = [ev async for ev in runner.run(ctx)]
    # standard refused -> router retried on the permissive model, unbidden
    assert pool.calls == ["standard", "wild"]
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers[0].text == GOOD
    thinks = " | ".join(ev.text for ev in events if ev.kind == "think")
    assert "declined" in thinks and "wild" in thinks


async def test_no_fallback_when_standard_answers(tmp_path):
    runner, ctx, pool = _make(
        tmp_path,
        pool_responses={"standard": GOOD, "wild": "should not be called"},
        brain_responses=[_tc("ask_standard", prompt="identify this plant"),
                         _tc("return_to_user", use_last_result=True)],
        policies={"wild": "permissive"})
    events = [ev async for ev in runner.run(ctx)]
    assert pool.calls == ["standard"]          # no wasted permissive call
    assert [ev for ev in events if ev.kind == "answer"][0].text == GOOD


async def test_refusal_with_no_permissive_returns_original(tmp_path):
    runner, ctx, pool = _make(
        tmp_path,
        pool_responses={"standard": REFUSAL, "other": GOOD},  # 'other' not permissive
        brain_responses=[_tc("ask_standard", prompt="q"),
                         _tc("return_to_user", use_last_result=True)],
        policies={})
    events = [ev async for ev in runner.run(ctx)]
    assert pool.calls == ["standard"]          # nothing permissive to retry on
    assert [ev for ev in events if ev.kind == "answer"][0].text == REFUSAL


async def test_permissive_also_refuses_delivers_original(tmp_path):
    runner, ctx, pool = _make(
        tmp_path,
        pool_responses={"standard": REFUSAL, "wild": "I cannot help with that either."},
        brain_responses=[_tc("ask_standard", prompt="q"),
                         _tc("return_to_user", use_last_result=True)],
        policies={"wild": "permissive"})
    events = [ev async for ev in runner.run(ctx)]
    assert pool.calls == ["standard", "wild"]  # tried once, no loop
    # both declined -> the original standard response is what the user gets
    assert [ev for ev in events if ev.kind == "answer"][0].text == REFUSAL
