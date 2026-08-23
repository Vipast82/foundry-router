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
