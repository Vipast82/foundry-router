"""Tests for the tightened dispatcher role and client-visible narration.

Found in deployment: the brain answered a current-events question about a real
person from its own stale 9B training data via a perfectly well-formed
return_to_user(answer=...) — no error, mode: agent, status: ok, wrong facts.
Prose rules alone don't bind a small model, so the role is now enforced
structurally: a prose-only brain reply gets ONE corrective nudge back toward
delegation, and if it still insists, the forwarded answer is explicitly
flagged (think-note + guardrail log entry) as not worker-verified.
"""

from foundry_router.brain import prompts
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


class ScriptedBrain:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []
        self.cfg = AgentBrainConfig()

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return self.responses.pop(0)

    async def complete(self, prompt):
        return ""


class OneModelPool:
    def available_models(self):
        return {"m": ["b"]}

    def backend_info(self, model):
        return {"name": "b", "type": "ollama", "url": "http://x"} if model == "m" else None

    async def chat(self, model, messages, tools=None, options=None, max_tokens=4096):
        return ChatResult(content="worker says hi", prompt_tokens=5, completion_tokens=5), "b"


def _make(tmp_path, brain_responses):
    db = Database(tmp_path / "r.sqlite")
    registry = ModelRegistry(db)
    tool_registry = ToolRegistry(db, registry, MCPManager([], db))
    tool_registry.tools["ask_m"] = ToolDef(
        name="ask_m", kind="model", description="test model", model_id="m",
        parameters={"type": "object", "properties": {"prompt": {"type": "string"}},
                    "required": ["prompt"]})
    meridian = MeridianUsage(MeridianConfig(), client=None, db=db)
    guardrails = GuardrailEngine(GuardrailsConfig(), db, meridian)
    brain = ScriptedBrain(brain_responses)
    runner = AgentRunner(brain, OneModelPool(), tool_registry, registry,
                         guardrails, meridian)
    ctx = RequestContext(
        persona=PersonaStore(db).get("Foundry-Chat"),
        messages=[{"role": "user", "content": "is some famous person dead?"}],
        guard=RequestGuardState(),
        logger=RequestLogger(db, "Foundry-Chat", "Foundry-Chat", "agent", "q"))
    return runner, ctx, brain


async def _events(runner, ctx):
    return [ev async for ev in runner.run(ctx)]


async def test_prose_reply_gets_nudged_toward_delegation(tmp_path):
    runner, ctx, brain = _make(tmp_path, [
        ChatResult(content="He is alive, as of my knowledge."),  # prose — forbidden
        ChatResult(tool_calls=[{"id": "1", "name": "ask_m",
                                "arguments": {"prompt": "verify this"}}]),
        ChatResult(tool_calls=[{"id": "2", "name": "return_to_user",
                                "arguments": {"use_last_result": True}}]),
    ])
    events = await _events(runner, ctx)

    # the nudge went back to the brain as a corrective user message
    assert len(brain.calls) == 3
    nudge = brain.calls[1][-1]
    assert nudge["role"] == "user" and "ask_<model>" in nudge["content"]
    # the redirect was narrated to the client
    thinks = [ev.text for ev in events if ev.kind == "think"]
    assert any("redirecting" in t for t in thinks)
    # and the final answer came from the worker, not the brain's prose
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers[0].text == "worker says hi"


async def test_persistent_prose_is_accepted_but_flagged(tmp_path):
    runner, ctx, brain = _make(tmp_path, [
        ChatResult(content="He is alive."),
        ChatResult(content="He is alive."),  # insists after the nudge
    ])
    events = await _events(runner, ctx)
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers[0].text == "He is alive."  # degrade, don't loop or fail
    thinks = " | ".join(ev.text for ev in events if ev.kind == "think")
    assert "NOT produced or verified by a worker model" in thinks
    assert "brain answered directly" in " ".join(ctx.logger.guardrail_events)


async def test_narration_covers_every_wait_point(tmp_path):
    runner, ctx, brain = _make(tmp_path, [
        ChatResult(tool_calls=[{"id": "1", "name": "ask_m",
                                "arguments": {"prompt": "do it"}}]),
        ChatResult(tool_calls=[{"id": "2", "name": "return_to_user",
                                "arguments": {"use_last_result": True}}]),
    ])
    events = await _events(runner, ctx)
    thinks = " | ".join(ev.text for ev in events if ev.kind == "think")
    assert "candidate models across" in thinks          # context summary
    assert "Consulting routing brain" in thinks         # what we're waiting on
    assert "Brain decided in" in thinks                 # decision + timing
    assert "waiting on generation" in thinks            # worker wait point
    assert "responded in" in thinks                     # worker timing
    assert "Forwarding the worker's full output" in thinks


def test_prompt_and_schema_state_dispatcher_role():
    system = prompts.build_system_prompt(None, [], {}, "n/a", None)
    assert "NEVER answer the user from your own knowledge" in system
    assert "NEVER author the user's answer yourself" in system
    assert "NARRATE" in system
    rtu = next(t for t in prompts.CORE_TOOL_SPECS
               if t["function"]["name"] == "return_to_user")
    assert "status note" in rtu["function"]["parameters"]["properties"]["answer"]["description"]
