"""Context-length auto-detection (item 1) and the pre-dispatch size guard
(item 2), unified through the registry's context_length column: local models
get their real trained window from Ollama /api/show, Claude gets a conservative
ceiling, and one guard rejects any request that wouldn't fit BEFORE dispatch —
so an oversized escalation degrades cleanly instead of earning a raw API error."""

import types

import pytest

from foundry_router.config import (AgentBrainConfig, GuardrailsConfig,
                                   MeridianConfig)
from foundry_router.db import Database
from foundry_router.guardrails import GuardrailEngine
from foundry_router.main import Services
from foundry_router.pool.base import ContextTooLarge
from foundry_router.pool.protocols import (ChatResult, OllamaProtocol,
                                           context_length_from_model_info)
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.usage import CLAUDE_DEFAULT_CONTEXT, MeridianUsage

from foundry_router.brain.agent import AgentRunner, estimate_tokens


# -- item 1: /api/show context extraction -----------------------------------------

def test_context_length_from_model_info_matches_by_suffix():
    assert context_length_from_model_info({"qwen2.context_length": 131072}) == 131072
    assert context_length_from_model_info(
        {"general.architecture": "llama", "llama.context_length": 8192}) == 8192
    assert context_length_from_model_info({"no_such_key": 5}) is None
    assert context_length_from_model_info({}) is None
    assert context_length_from_model_info({"qwen2.context_length": 0}) is None  # guard bad val


class ShowClient:
    def __init__(self, model_info):
        self._mi = model_info

    async def post(self, url, json=None, timeout=None):
        return types.SimpleNamespace(status_code=200, text="",
                                     json=lambda: {"model_info": self._mi})


async def test_show_context_length_reads_gguf_metadata():
    proto = OllamaProtocol("http://x", None,
                           ShowClient({"qwen2.context_length": 131072}))
    assert await proto.show_context_length("deepseek-r1:32b") == 131072


# -- item 1: population wiring -----------------------------------------------------

class FakeBackend:
    def __init__(self, models, ctx_map, typ="ollama"):
        self.healthy = True
        self.models = models
        self.config = types.SimpleNamespace(type=typ, name="b")
        self._ctx = ctx_map

        async def _probe(model):
            return ctx_map.get(model)
        self.protocol = types.SimpleNamespace(show_context_length=_probe)


class FakePool:
    def __init__(self, *backends):
        self.backends = {f"b{i}": b for i, b in enumerate(backends)}


class _Svc:
    populate_context_lengths = Services.populate_context_lengths

    def __init__(self, pool, registry):
        self.pool = pool
        self.registry = registry


async def test_populate_fills_missing_context_only(tmp_path):
    registry = ModelRegistry(Database(tmp_path / "p.sqlite"))
    registry.upsert_auto("deepseek-r1:32b", source="discovery", relative_cost_tier="free")
    registry.upsert_auto("already:1b", source="discovery", context_length=4096)
    pool = FakePool(FakeBackend(["deepseek-r1:32b", "already:1b"],
                                {"deepseek-r1:32b": 131072, "already:1b": 999}))
    await _Svc(pool, registry).populate_context_lengths()
    assert registry.get("deepseek-r1:32b")["context_length"] == 131072   # filled
    assert registry.get("already:1b")["context_length"] == 4096          # not re-probed


async def test_populate_skips_non_ollama_backends(tmp_path):
    registry = ModelRegistry(Database(tmp_path / "p2.sqlite"))
    registry.upsert_auto("claude", source="discovery", relative_cost_tier="high")
    pool = FakePool(FakeBackend(["claude"], {"claude": 123}, typ="anthropic-compatible"))
    await _Svc(pool, registry).populate_context_lengths()
    assert registry.get("claude")["context_length"] is None   # never probed


# -- item 2: pre-dispatch size guard ----------------------------------------------

class RecordingPool:
    def __init__(self):
        self.sent = False

    def backend_info(self, m):
        return {"name": "b", "type": "ollama", "url": "http://x", "api_key": None}

    async def chat(self, model, messages, tools=None, options=None, max_tokens=4096):
        self.sent = True
        return ChatResult(content="ok"), "b"


def _runner(tmp_path, pool):
    db = Database(tmp_path / "d.sqlite")
    registry = ModelRegistry(db)
    meridian = MeridianUsage(MeridianConfig(), client=None, db=db)
    brain = types.SimpleNamespace(cfg=AgentBrainConfig())
    runner = AgentRunner(brain, pool, None, registry,
                         GuardrailEngine(GuardrailsConfig(), db, meridian), meridian)
    return runner, registry


async def test_oversized_request_rejected_before_dispatch(tmp_path):
    pool = RecordingPool()
    runner, registry = _runner(tmp_path, pool)
    registry.upsert_auto("claude", source="discovery", relative_cost_tier="high",
                         context_length=CLAUDE_DEFAULT_CONTEXT)      # 200_000
    big = "x" * (210_000 * 4)                                        # ~210k tokens
    with pytest.raises(ContextTooLarge):
        await runner._dispatch_worker("claude", big)
    assert pool.sent is False                                        # never sent to Claude


async def test_fitting_request_dispatches(tmp_path):
    pool = RecordingPool()
    runner, registry = _runner(tmp_path, pool)
    registry.upsert_auto("local", source="discovery", relative_cost_tier="free",
                         context_length=262_144)
    result, backend = await runner._dispatch_worker("local", "x" * 4000)  # ~1k tokens
    assert pool.sent and result.content == "ok"


async def test_unknown_context_length_is_not_gated(tmp_path):
    pool = RecordingPool()
    runner, registry = _runner(tmp_path, pool)
    registry.upsert_auto("mystery", source="discovery", relative_cost_tier="free")
    # context_length None => can't gate what we don't know => send it (degrade
    # gracefully, same as the ranking gate)
    result, backend = await runner._dispatch_worker("mystery", "x" * (400_000 * 4))
    assert pool.sent


def test_estimate_tokens():
    assert estimate_tokens("x" * 400) == 100
    assert estimate_tokens("") == 1        # floor at 1
