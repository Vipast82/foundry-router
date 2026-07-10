"""Input-side twin of the tool-result cap (found in deployment: a ~23,000-char
pasted file blew the brain's context BEFORE its first routing decision —
silent truncation dropped the conversation into the static fallback).

The invariant, mirroring test_tool_result_limits:
  1. the BRAIN sees at most user_input_preview_chars of a large user message,
     plus a notice naming include_full_user_message (so it knows the rest
     exists and how to deliver it);
  2. the WORKER receives the complete original message — marker buried deep
     in the paste and all — when the brain sets that flag.
"""

from foundry_router.brain.agent import AgentRunner, RequestContext
from foundry_router.config import AgentBrainConfig, GuardrailsConfig, MeridianConfig
from foundry_router.db import Database
from foundry_router.guardrails import GuardrailEngine, RequestGuardState
from foundry_router.personas import PersonaStore
from foundry_router.pool.protocols import ChatResult
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.tools.sync import ToolDef, ToolRegistry, _ASK_PARAMS
from foundry_router.usage import MeridianUsage, RequestLogger

# The deployment test case: a marker value buried deep in a large paste.
MARKER = "837291"
BIG_MESSAGE = ("What is the marker value in this file?\n"
               + "x" * 22_000 + f"\nmarker_value = {MARKER}\n")


class ScriptedBrain:
    def __init__(self, responses, preview_chars=2000):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []
        self.cfg = AgentBrainConfig(user_input_preview_chars=preview_chars)

    async def chat(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return self.responses.pop(0)

    async def complete(self, prompt):
        return ""


class RecordingPool:
    def __init__(self):
        self.prompts: list[str] = []

    def available_models(self):
        return {"m": ["b"]}

    def backend_info(self, model):
        return {"name": "b", "type": "ollama", "url": "http://x"} if model == "m" else None

    async def chat(self, model, messages, tools=None, options=None, max_tokens=4096):
        self.prompts.append(messages[-1]["content"])
        return ChatResult(content=f"The marker is {MARKER}.",
                          prompt_tokens=10, completion_tokens=10), "b"


def _make(tmp_path, responses, preview_chars=2000):
    db = Database(tmp_path / "u.sqlite")
    registry = ModelRegistry(db)
    tool_registry = ToolRegistry(db, registry, MCPManager([], db))
    tool_registry.tools["ask_m"] = ToolDef(
        name="ask_m", kind="model", description="test model", model_id="m",
        parameters=_ASK_PARAMS)
    meridian = MeridianUsage(MeridianConfig(), client=None, db=db)
    brain = ScriptedBrain(responses, preview_chars=preview_chars)
    pool = RecordingPool()
    runner = AgentRunner(brain, pool, tool_registry, registry,
                         GuardrailEngine(GuardrailsConfig(), db, meridian), meridian)
    ctx = RequestContext(
        persona=PersonaStore(db).get("Foundry-Chat"),
        messages=[{"role": "user", "content": BIG_MESSAGE}],
        guard=RequestGuardState(),
        logger=RequestLogger(db, "Foundry-Chat", "Foundry-Chat", "agent", "marker test"))
    return runner, ctx, brain, pool


async def test_brain_sees_preview_worker_gets_full_message(tmp_path):
    runner, ctx, brain, pool = _make(tmp_path, [
        ChatResult(tool_calls=[{"id": "1", "name": "ask_m",
                                "arguments": {"prompt": "Find the marker value in the file.",
                                              "include_full_user_message": True}}]),
        ChatResult(tool_calls=[{"id": "2", "name": "return_to_user",
                                "arguments": {"use_last_result": True}}]),
    ], preview_chars=500)
    events = [ev async for ev in runner.run(ctx)]

    # half 1: the brain's view is capped, marker invisible, notice present
    user_view = next(m for m in brain.calls[0] if m["role"] == "user")
    assert MARKER not in user_view["content"]
    assert len(user_view["content"]) < 1200  # 500-char preview + notice
    assert "include_full_user_message" in user_view["content"]

    # half 2: the worker got the complete original, marker included
    assert len(pool.prompts) == 1
    assert MARKER in pool.prompts[0]
    assert "FULL ORIGINAL USER MESSAGE" in pool.prompts[0]

    # and the delivery was narrated + the answer flowed back
    thinks = " | ".join(ev.text for ev in events if ev.kind == "think")
    assert "complete original message" in thinks
    answers = [ev for ev in events if ev.kind == "answer"]
    assert MARKER in answers[0].text


async def test_small_messages_pass_through_untouched(tmp_path):
    runner, ctx, brain, pool = _make(tmp_path, [
        ChatResult(tool_calls=[{"id": "1", "name": "ask_m",
                                "arguments": {"prompt": "hi"}}]),
        ChatResult(tool_calls=[{"id": "2", "name": "return_to_user",
                                "arguments": {"use_last_result": True}}]),
    ], preview_chars=2000)
    ctx.messages = [{"role": "user", "content": "short question"}]
    [ev async for ev in runner.run(ctx)]
    user_view = next(m for m in brain.calls[0] if m["role"] == "user")
    assert user_view["content"] == "short question"  # no notice, no truncation


async def test_ctx_messages_never_mutated_for_fallback(tmp_path):
    """The static fallback forwards the raw conversation — the preview must be
    a copy, never an edit of ctx.messages."""
    runner, ctx, brain, pool = _make(tmp_path, [
        ChatResult(tool_calls=[{"id": "1", "name": "return_to_user",
                                "arguments": {"answer": "status note"}}]),
    ], preview_chars=100)
    [ev async for ev in runner.run(ctx)]
    assert ctx.messages[0]["content"] == BIG_MESSAGE  # full original intact


def test_ask_schema_advertises_the_flag():
    assert "include_full_user_message" in _ASK_PARAMS["properties"]
