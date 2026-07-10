"""Regression test for the duplicate-system-message crash found in deployment.

Clients like AnythingLLM send their own workspace system message in the
conversation history. The brain's chat template (Ornith and most instruct
templates) requires exactly ONE system message, strictly first — a second one
400s the backend with "System message must be at the beginning". The fix:
every message list sent to the brain has exactly our system message at index
0, and the client's system content is folded INTO it (not dropped — workspace
instructions like "answer in German" must reach the brain and, through it,
worker prompts).
"""

from foundry_router.brain.agent import AgentRunner, RequestContext
from foundry_router.config import GuardrailsConfig, MeridianConfig
from foundry_router.db import Database
from foundry_router.guardrails import GuardrailEngine, RequestGuardState
from foundry_router.personas import PersonaStore
from foundry_router.pool.protocols import ChatResult
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.tools.sync import ToolRegistry
from foundry_router.usage import MeridianUsage, RequestLogger


class RecordingBrain:
    """Captures exactly what the agent sends to the brain, answers directly
    (no tool calls) so the loop finishes in one step."""

    def __init__(self):
        self.calls: list[list[dict]] = []
        from foundry_router.config import AgentBrainConfig
        self.cfg = AgentBrainConfig()  # agent reads context-budget knobs off this

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return ChatResult(content="final answer")

    async def complete(self, prompt):
        return ""


class EmptyPool:
    def available_models(self):
        return {}

    def backend_info(self, model):
        return None


def _runner_and_ctx(tmp_path, messages):
    db = Database(tmp_path / "a.sqlite")
    registry = ModelRegistry(db)
    tool_registry = ToolRegistry(db, registry, MCPManager([], db))
    meridian = MeridianUsage(MeridianConfig(), client=None, db=db)
    guardrails = GuardrailEngine(GuardrailsConfig(), db, meridian)
    brain = RecordingBrain()
    runner = AgentRunner(brain, EmptyPool(), tool_registry, registry,
                         guardrails, meridian)
    persona = PersonaStore(db).get("Foundry-Chat")
    ctx = RequestContext(
        persona=persona, messages=messages, guard=RequestGuardState(),
        logger=RequestLogger(db, "Foundry-Chat", "Foundry-Chat", "agent", "hi"))
    return runner, ctx, brain


async def _collect(runner, ctx):
    return [ev async for ev in runner.run(ctx)]


async def test_client_system_message_does_not_duplicate(tmp_path):
    messages = [
        {"role": "system", "content": "Always answer in German."},  # AnythingLLM-style
        {"role": "user", "content": "hello"},
    ]
    runner, ctx, brain = _runner_and_ctx(tmp_path, messages)
    events = await _collect(runner, ctx)

    assert brain.calls, "brain was never called"
    sent = brain.calls[0]
    system_msgs = [m for m in sent if m["role"] == "system"]
    assert len(system_msgs) == 1, "must send exactly one system message"
    assert sent[0]["role"] == "system", "system message must be first"
    # the client's instructions are folded in, not dropped
    assert "Always answer in German." in sent[0]["content"]
    # and the user turn survives
    assert any(m["role"] == "user" and m["content"] == "hello" for m in sent)
    assert any(ev.kind == "answer" for ev in events)


async def test_no_client_system_message_still_single_system(tmp_path):
    runner, ctx, brain = _runner_and_ctx(
        tmp_path, [{"role": "user", "content": "hello"}])
    await _collect(runner, ctx)
    sent = brain.calls[0]
    assert [m["role"] for m in sent if m["role"] == "system"] == ["system"]
    assert "CLIENT WORKSPACE INSTRUCTIONS" not in sent[0]["content"]


async def test_fallback_conversation_keeps_client_system(tmp_path):
    """ctx.messages must NOT be mutated — the static fallback path forwards the
    raw conversation to a local model, which should still get the client's
    system message."""
    messages = [
        {"role": "system", "content": "Always answer in German."},
        {"role": "user", "content": "hello"},
    ]
    runner, ctx, brain = _runner_and_ctx(tmp_path, messages)
    await _collect(runner, ctx)
    assert ctx.messages[0]["role"] == "system"
    assert ctx.messages[0]["content"] == "Always answer in German."
