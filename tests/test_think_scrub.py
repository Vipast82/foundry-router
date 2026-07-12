"""Worker-emitted <think> tags leaking into visible answer content (found
live: a stray ", etc. </think>" rendered in AnythingLLM immediately after the
collapsed narration box). The router never constructs these tags — reasoning
workers emit them in content when their backend's think-parsing misfires or
re-emits a stray closing tag — so they are scrubbed at THE single dispatch
path (_dispatch_worker) and rerouted to the native thinking field, with a
facade safety net for brain-prose answers that never pass through dispatch.
"""

import json

from foundry_router.brain.agent import AgentRunner, RequestContext
from foundry_router.brain.prompts import split_think
from foundry_router.config import AgentBrainConfig, GuardrailsConfig, MeridianConfig
from foundry_router.db import Database
from foundry_router.guardrails import GuardrailEngine, RequestGuardState
from foundry_router.pool.protocols import ChatResult, OllamaProtocol
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.tools.sync import _ASK_PARAMS, ToolDef, ToolRegistry
from foundry_router.usage import MeridianUsage, RequestLogger


# -- unit: the splitter -----------------------------------------------------------

def test_split_complete_block():
    reasoning, clean = split_think("<think>plan the answer</think>The answer.")
    assert reasoning == "plan the answer"
    assert clean == "The answer."


def test_split_dangling_closer_the_live_bug():
    # Exactly the observed artifact: the opener was consumed upstream, the
    # reasoning tail + stray closing tag landed in content.
    reasoning, clean = split_think(
        "checking sunlight, water, etc. </think>Your plant is a fern.")
    assert reasoning == "checking sunlight, water, etc."
    assert clean == "Your plant is a fern."
    assert "</think>" not in clean


def test_split_dangling_opener_strips_tag_keeps_text():
    reasoning, clean = split_think("<think>reply that never closed its tag")
    assert reasoning == ""
    assert clean == "reply that never closed its tag"
    assert "<think>" not in clean


def test_split_fully_wrapped_returns_text_not_emptiness():
    # A model that wrapped its entire reply in think tags: an empty answer is
    # the one outcome always worse than an unpolished one.
    reasoning, clean = split_think("<think>the whole reply lives in here</think>")
    assert clean == "the whole reply lives in here"
    assert reasoning == ""


def test_split_no_tags_is_a_passthrough():
    assert split_think("plain answer") == ("", "plain answer")


# -- Ollama protocol: native thinking field captured --------------------------------

class FakeResp:
    def __init__(self, data):
        self._data = data
        self.status_code = 200

    def json(self):
        return self._data


class CaptureClient:
    def __init__(self, data):
        self._data = data

    async def post(self, url, json=None, headers=None):
        return FakeResp(self._data)


async def test_ollama_protocol_captures_native_thinking():
    client = CaptureClient({"message": {"content": "the answer",
                                        "thinking": "native reasoning"},
                            "prompt_eval_count": 1, "eval_count": 1})
    proto = OllamaProtocol("http://x", None, client)
    result = await proto.chat("m", [{"role": "user", "content": "q"}])
    assert result.content == "the answer"
    assert result.thinking == "native reasoning"


# -- integration: tags never survive to the answer event ------------------------------

class TaggedPool:
    """Worker whose output carries the live bug's exact shape."""

    def available_models(self):
        return {"local-model": ["b"]}

    def backend_info(self, m):
        return {"name": "b", "type": "ollama", "url": "http://x", "api_key": None}

    async def chat(self, model, messages, tools=None, options=None, max_tokens=4096):
        return ChatResult(
            content="internal reasoning, etc. </think>The plant is a fern."), "b"


class ScriptedBrain:
    def __init__(self, responses):
        self.responses = list(responses)
        self.cfg = AgentBrainConfig()

    async def chat(self, messages, tools=None, **kwargs):
        return self.responses.pop(0)

    async def complete(self, prompt):
        return ""


def _make(tmp_path, brain_responses):
    db = Database(tmp_path / "t.sqlite")
    registry = ModelRegistry(db)
    registry.upsert_auto("local-model", source="discovery", relative_cost_tier="free")
    tool_registry = ToolRegistry(db, registry, MCPManager([], db))
    tool_registry.tools["ask_local_model"] = ToolDef(
        name="ask_local_model", kind="model", description="",
        model_id="local-model", parameters=_ASK_PARAMS)
    pool = TaggedPool()
    meridian = MeridianUsage(MeridianConfig(), client=None, db=db)
    brain = ScriptedBrain(brain_responses)
    runner = AgentRunner(brain, pool, tool_registry, registry,
                         GuardrailEngine(GuardrailsConfig(), db, meridian), meridian)
    ctx = RequestContext(
        persona={"virtual_name": "Foundry-Chat", "benchmark_category": "general_chat"},
        messages=[{"role": "user", "content": "what plant is this?"}],
        guard=RequestGuardState(),
        logger=RequestLogger(db, "Foundry-Chat", "Foundry-Chat", "agent", "plant"))
    return runner, ctx


async def test_worker_think_tags_never_reach_the_answer(tmp_path):
    runner, ctx = _make(tmp_path, [
        ChatResult(tool_calls=[{"id": "1", "name": "ask_local_model",
                                "arguments": {"prompt": "identify the plant"}}]),
        ChatResult(tool_calls=[{"id": "2", "name": "return_to_user",
                                "arguments": {"use_last_result": True}}]),
    ])
    events = [ev async for ev in runner.run(ctx)]
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers[0].text == "The plant is a fern."
    assert "think>" not in answers[0].text
    # the scrubbed reasoning surfaces as narration instead of vanishing
    thinks = " | ".join(ev.text for ev in events if ev.kind == "think")
    assert "internal reasoning, etc." in thinks
