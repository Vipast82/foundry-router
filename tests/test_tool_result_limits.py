"""Regression tests for the context-budget knobs (found in deployment: a
22,600-char tool result fed back to a num_ctx=6144 local brain silently
truncated the conversation, dropping the user's message and surfacing as a
spurious "no user query found" template error).

The invariant, both halves:
  1. the BRAIN sees at most tool_result_limit_chars of a tool result, plus an
     explicit truncation notice (so it knows to forward, not retype);
  2. the USER still receives the complete, untruncated output via
     return_to_user(use_last_result=true).
"""

from foundry_router.brain.agent import AgentRunner, RequestContext
from foundry_router.config import AgentBrainConfig, GuardrailsConfig, MeridianConfig
from foundry_router.db import Database
from foundry_router.guardrails import GuardrailEngine, RequestGuardState
from foundry_router.personas import PersonaStore
from foundry_router.pool.protocols import ChatResult
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.tools.sync import ToolDef, ToolRegistry
from foundry_router.usage import MeridianUsage, RequestLogger

BIG = "x" * 10_000  # a worker result far larger than the brain's budget


class ScriptedBrain:
    """Plays back a fixed sequence of responses; records what it was sent."""

    def __init__(self, responses, cfg=None):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []
        self.cfg = cfg or AgentBrainConfig()

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return self.responses.pop(0)

    async def complete(self, prompt):
        return ""


class OneModelPool:
    """Serves a single local model 'm' whose output is BIG."""

    def available_models(self):
        return {"m": ["b"]}

    def backend_info(self, model):
        return {"name": "b", "type": "ollama", "url": "http://x"} if model == "m" else None

    async def chat(self, model, messages, tools=None, options=None, max_tokens=4096):
        return ChatResult(content=BIG, prompt_tokens=10, completion_tokens=100), "b"


def _make_runner(tmp_path, limit_chars):
    db = Database(tmp_path / "t.sqlite")
    registry = ModelRegistry(db)
    tool_registry = ToolRegistry(db, registry, MCPManager([], db))
    tool_registry.tools["ask_m"] = ToolDef(
        name="ask_m", kind="model", description="test model", model_id="m",
        parameters={"type": "object", "properties": {"prompt": {"type": "string"}},
                    "required": ["prompt"]})
    meridian = MeridianUsage(MeridianConfig(), client=None, db=db)
    guardrails = GuardrailEngine(GuardrailsConfig(), db, meridian)
    brain = ScriptedBrain(
        responses=[
            ChatResult(tool_calls=[{"id": "c1", "name": "ask_m",
                                    "arguments": {"prompt": "do the thing"}}]),
            ChatResult(tool_calls=[{"id": "c2", "name": "return_to_user",
                                    "arguments": {"use_last_result": True}}]),
        ],
        cfg=AgentBrainConfig(tool_result_limit_chars=limit_chars))
    runner = AgentRunner(brain, OneModelPool(), tool_registry, registry,
                         guardrails, meridian)
    persona = PersonaStore(db).get("Foundry-Chat")
    ctx = RequestContext(
        persona=persona, messages=[{"role": "user", "content": "make it big"}],
        guard=RequestGuardState(),
        logger=RequestLogger(db, "Foundry-Chat", "Foundry-Chat", "agent", "make it big"))
    return runner, ctx, brain


async def test_brain_sees_truncated_preview_user_gets_full_result(tmp_path):
    runner, ctx, brain = _make_runner(tmp_path, limit_chars=100)
    events = [ev async for ev in runner.run(ctx)]

    # half 2: the user gets the COMPLETE output
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers and answers[0].text == BIG

    # half 1: the brain's second call carries only the capped preview + notice
    assert len(brain.calls) == 2
    tool_msgs = [m for m in brain.calls[1] if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    content = tool_msgs[0]["content"]
    assert len(content) < 500  # 100-char preview + notice, nowhere near 10k
    assert content.startswith("x" * 100)
    assert "use_last_result" in content  # the notice tells the brain how to forward


async def test_limit_is_config_not_constant(tmp_path):
    """Raising the config knob must widen what the brain sees — proves the
    value is read from AgentBrainConfig, not a baked-in constant."""
    runner, ctx, brain = _make_runner(tmp_path, limit_chars=20_000)
    [ev async for ev in runner.run(ctx)]
    tool_msgs = [m for m in brain.calls[1] if m["role"] == "tool"]
    assert tool_msgs[0]["content"] == BIG  # under the limit -> untouched, no notice
