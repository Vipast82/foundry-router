"""reasoning_effort → Ollama `think`: the operator sets one global reasoning
level on the Agent Brain and every thinking-capable worker gets it as Ollama's
top-level `think` field, while non-reasoning locals are left untouched (so an
unsupported field never reaches a model that would reject it)."""

import types

import httpx
import pytest

from foundry_router.config import (AgentBrainConfig, GuardrailsConfig,
                                    MeridianConfig)
from foundry_router.db import Database
from foundry_router.guardrails import GuardrailEngine
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.usage import MeridianUsage
from foundry_router.brain.agent import AgentRunner
from foundry_router.pool.protocols import OllamaProtocol


def _runner(tmp_path, effort):
    db = Database(tmp_path / "d.sqlite")
    registry = ModelRegistry(db)
    meridian = MeridianUsage(MeridianConfig(), client=None, db=db)
    brain = types.SimpleNamespace(cfg=AgentBrainConfig(reasoning_effort=effort))
    runner = AgentRunner(brain, None, None, registry,
                         GuardrailEngine(GuardrailsConfig(), db, meridian), meridian)
    return runner, registry


# -- _think_for gating -------------------------------------------------------------

def test_think_for_thinking_model_gets_level(tmp_path):
    runner, registry = _runner(tmp_path, "high")
    registry.upsert_auto("qwen", source="discovery")
    registry.set_capabilities("qwen", ["completion", "tools", "thinking"])
    assert runner._think_for("qwen") == "high"


def test_think_for_non_thinking_model_is_none(tmp_path):
    runner, registry = _runner(tmp_path, "high")
    registry.upsert_auto("plain", source="discovery")
    registry.set_capabilities("plain", ["completion", "tools"])
    assert runner._think_for("plain") is None


def test_think_for_unset_effort_is_none(tmp_path):
    runner, registry = _runner(tmp_path, None)
    registry.upsert_auto("qwen", source="discovery")
    registry.set_capabilities("qwen", ["thinking"])
    assert runner._think_for("qwen") is None


def test_think_for_off_disables(tmp_path):
    runner, registry = _runner(tmp_path, "off")
    registry.upsert_auto("qwen", source="discovery")
    registry.set_capabilities("qwen", ["thinking"])
    assert runner._think_for("qwen") is False


def test_think_for_on_enables_bool(tmp_path):
    runner, registry = _runner(tmp_path, "true")
    registry.upsert_auto("qwen", source="discovery")
    registry.set_capabilities("qwen", ["thinking"])
    assert runner._think_for("qwen") is True


def test_think_for_unknown_model_is_none(tmp_path):
    runner, _ = _runner(tmp_path, "high")
    assert runner._think_for("never-seen") is None


# -- payload wiring: think reaches the Ollama wire body ----------------------------

def test_ollama_payload_includes_think():
    proto = OllamaProtocol("http://x", None, client=None)
    payload = proto._payload("m", [{"role": "user", "content": "hi"}],
                             None, None, None, stream=False, think="low")
    assert payload["think"] == "low"


def test_ollama_payload_omits_think_when_none():
    proto = OllamaProtocol("http://x", None, client=None)
    payload = proto._payload("m", [{"role": "user", "content": "hi"}],
                             None, None, None, stream=False, think=None)
    assert "think" not in payload


# -- thinking module: level menus + normalization ----------------------------------

from foundry_router import thinking


def test_supported_levels_curated_by_family():
    assert thinking.supported_levels("gpt-oss:20b", ["thinking"]) == \
        ["off", "low", "medium", "high"]
    assert thinking.supported_levels("qwen3.8:27b", ["thinking"]) == \
        ["off", "low", "medium", "high", "max"]
    # thinking-capable but unrecognized family -> boolean only
    assert thinking.supported_levels("mystery-r:8b", ["thinking"]) == ["off", "on"]
    # no thinking capability -> empty menu
    assert thinking.supported_levels("llama3:8b", ["completion"]) == []


def test_supported_levels_anthropic_always_full():
    # Claude via Meridian: full budgeted menu regardless of caps
    assert thinking.supported_levels("claude-opus-5", None,
                                     backend_type="anthropic-compatible") == \
        ["off", "low", "medium", "high", "max"]


def test_normalize_poles_and_levels():
    assert thinking.normalize(None) is None
    assert thinking.normalize("") is None
    assert thinking.normalize("off") is False
    assert thinking.normalize("on") is True
    assert thinking.normalize(True) is True
    assert thinking.normalize("HIGH") == "high"
    assert thinking.normalize("xhigh") == "max"       # old label aliased to Ollama's top level


# -- Claude extended-thinking budget mapping (conservative) ------------------------

def test_claude_thinking_conservative_budgets():
    assert thinking.claude_thinking("low", 4096)[0]["budget_tokens"] == 2048
    assert thinking.claude_thinking("medium", 4096)[0]["budget_tokens"] == 8192
    assert thinking.claude_thinking("high", 4096)[0]["budget_tokens"] == 16384
    assert thinking.claude_thinking("max", 4096)[0]["budget_tokens"] == 32768


def test_claude_thinking_raises_max_tokens_above_budget():
    # Anthropic requires max_tokens > budget_tokens; medium=8192 > the 4096
    # caller default, so max_tokens must be bumped past the budget.
    block, mt = thinking.claude_thinking("medium", 4096)
    assert mt > block["budget_tokens"]
    assert mt == 4096 + 8192


def test_claude_thinking_off_and_unset_send_nothing():
    assert thinking.claude_thinking(False, 4096) is None
    assert thinking.claude_thinking(None, 4096) is None


def test_claude_thinking_true_defaults_medium():
    assert thinking.claude_thinking(True, 4096)[0]["budget_tokens"] == 8192


# -- persona precedence: persona effort overrides the global default ---------------

def test_persona_effort_overrides_global(tmp_path):
    runner, registry = _runner(tmp_path, "low")            # global = low
    registry.upsert_auto("qwen", source="discovery")
    registry.set_capabilities("qwen", ["thinking"])
    runner._req_effort = "high"                             # persona pin
    assert runner._think_for("qwen") == "high"


def test_persona_effort_off_beats_global_on(tmp_path):
    runner, registry = _runner(tmp_path, "high")           # global = high
    registry.upsert_auto("qwen", source="discovery")
    registry.set_capabilities("qwen", ["thinking"])
    runner._req_effort = "off"                              # persona forces off
    assert runner._think_for("qwen") is False


# -- Anthropic protocol emits the thinking block on the wire -----------------------

from foundry_router.pool.protocols import AnthropicProtocol


class _FakeResp:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _CaptureClient:
    def __init__(self, data):
        self.payload = None
        self._data = data

    async def post(self, url, json=None, headers=None):
        self.payload = json
        return _FakeResp(self._data)


async def test_anthropic_emits_thinking_block_and_drops_temperature():
    client = _CaptureClient({"content": [{"type": "text", "text": "hi"}],
                             "usage": {"input_tokens": 1, "output_tokens": 1}})
    proto = AnthropicProtocol("http://m", "key", client)
    await proto.chat("claude-opus-5", [{"role": "user", "content": "design this"}],
                     options={"temperature": 0.7}, max_tokens=4096, think="high")
    p = client.payload
    assert p["thinking"] == {"type": "enabled", "budget_tokens": 16384}
    assert p["max_tokens"] == 4096 + 16384        # budget added on top
    assert "temperature" not in p                 # forbidden while thinking on


async def test_anthropic_no_think_keeps_temperature():
    client = _CaptureClient({"content": [{"type": "text", "text": "hi"}],
                             "usage": {"input_tokens": 1, "output_tokens": 1}})
    proto = AnthropicProtocol("http://m", "key", client)
    await proto.chat("claude-opus-5", [{"role": "user", "content": "hi"}],
                     options={"temperature": 0.7}, max_tokens=4096, think=None)
    assert "thinking" not in client.payload
    assert client.payload["temperature"] == 0.7


async def test_anthropic_returns_thinking_text():
    client = _CaptureClient({"content": [
        {"type": "thinking", "thinking": "let me reason"},
        {"type": "text", "text": "the answer"}],
        "usage": {"input_tokens": 1, "output_tokens": 1}})
    proto = AnthropicProtocol("http://m", "key", client)
    res, _unused = None, None
    res = await proto.chat("claude-opus-5", [{"role": "user", "content": "q"}],
                           think="medium")
    assert res.thinking == "let me reason"
    assert res.content == "the answer"


# -- facade resolver precedence: client > persona > global -------------------------

from foundry_router.facade import ollama_api


def _fake_svc(global_effort, caps, backend_type="ollama"):
    reg = types.SimpleNamespace(get=lambda m: {"capabilities": caps})
    pool = types.SimpleNamespace(backend_info=lambda m: {"type": backend_type})
    cfg = types.SimpleNamespace(agent_brain=AgentBrainConfig(reasoning_effort=global_effort))
    return types.SimpleNamespace(registry=reg, pool=pool,
                                 config_store=types.SimpleNamespace(config=cfg))


def test_facade_client_beats_persona_and_global():
    svc = _fake_svc("low", ["thinking"])
    persona = {"reasoning_effort": "medium"}
    assert ollama_api._think_for(svc, "qwen3.8", persona, client_think="high") == "high"


def test_facade_persona_beats_global():
    svc = _fake_svc("low", ["thinking"])
    persona = {"reasoning_effort": "xhigh"}       # old label -> aliased to "max"
    assert ollama_api._think_for(svc, "qwen3.8", persona, client_think=None) == "max"


def test_facade_global_when_no_persona_or_client():
    svc = _fake_svc("medium", ["thinking"])
    assert ollama_api._think_for(svc, "qwen3.8", {}, client_think=None) == "medium"


def test_facade_client_think_wins_by_default():
    # Cline sends think:false — without force, the client wins over the persona.
    svc = _fake_svc("", ["thinking"])
    persona = {"reasoning_effort": "medium"}
    assert ollama_api._think_for(svc, "qwen3.8", persona, client_think=False) is False


def test_facade_force_reasoning_overrides_client():
    # With force_reasoning_effort, the persona wins over the client's think:false.
    svc = _fake_svc("", ["thinking"])
    persona = {"reasoning_effort": "low", "force_reasoning_effort": 1}
    assert ollama_api._think_for(svc, "qwen3.8", persona, client_think=False) == "low"


def test_facade_none_for_non_thinking_model():
    svc = _fake_svc("high", ["completion"])
    assert ollama_api._think_for(svc, "llama3", {}, client_think="high") is None
