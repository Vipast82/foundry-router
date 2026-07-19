"""Embedding-only models (nomic-embed-text, bge, ...) can't serve /api/chat, so
routing one for a chat request earns an immediate 400 and a wasteful brain
fallback (observed live). They're now detected by name + learned from the 400,
and excluded from chat candidacy everywhere: ranked_for_category and the blind
fallback picker."""

import types

from foundry_router.brain.agent import _flag_if_not_chat
from foundry_router.brain.fallback import pick_fallback_model
from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.registry.tagging import is_embedding_name


def test_is_embedding_name():
    for good in ["nomic-embed-text:latest", "mxbai-embed-large", "bge-m3",
                 "snowflake-arctic-embed2", "all-minilm:l6-v2", "gte-large"]:
        assert is_embedding_name(good), good
    for chat in ["llama3.1:8b", "qwen2.5-coder:32b", "ornith:35b",
                 "claude-sonnet-4-6", "deepseek-r1:32b"]:
        assert not is_embedding_name(chat), chat


def _reg(tmp_path):
    reg = ModelRegistry(Database(tmp_path / "r.sqlite"))
    reg.upsert_auto("qwen2.5:14b", source="discovery", relative_cost_tier="free")
    reg.upsert_auto("nomic-embed-text:latest", source="discovery", relative_cost_tier="free")
    return reg


def test_ranked_excludes_embedding(tmp_path):
    reg = _reg(tmp_path)
    ids = [r["id"] for r in reg.ranked_for_category(
        "general_chat", ["qwen2.5:14b", "nomic-embed-text:latest"])]
    assert "qwen2.5:14b" in ids
    assert "nomic-embed-text:latest" in ids     # not flagged yet -> still a candidate
    reg.mark_embedding("nomic-embed-text:latest")
    ids = [r["id"] for r in reg.ranked_for_category(
        "general_chat", ["qwen2.5:14b", "nomic-embed-text:latest"])]
    assert ids == ["qwen2.5:14b"]               # now excluded


def test_mark_embedding_is_idempotent(tmp_path):
    reg = _reg(tmp_path)
    assert reg.mark_embedding("nomic-embed-text:latest") is True    # newly flagged
    assert reg.mark_embedding("nomic-embed-text:latest") is False   # already flagged


def test_flag_if_not_chat_only_on_that_error(tmp_path):
    reg = _reg(tmp_path)
    assert _flag_if_not_chat(reg, "nomic-embed-text:latest",
                             RuntimeError('"nomic-embed-text:latest" does not support chat'))
    assert reg.get("nomic-embed-text:latest")["embedding"] == 1
    assert not _flag_if_not_chat(reg, "qwen2.5:14b", RuntimeError("connection refused"))
    assert not reg.get("qwen2.5:14b").get("embedding")


def test_upsert_auto_persists_embedding_flag(tmp_path):
    """Regression: the discovery-time flag was dropped because 'embedding' wasn't
    in MODEL_FIELDS, so nomic-embed-text kept being picked as a worker until a
    400 reactively flagged it. Discovery re-flagging must now stick — and must
    not downgrade a researched row's provenance."""
    reg = ModelRegistry(Database(tmp_path / "r.sqlite"))
    reg.upsert_auto("nomic-embed-text:latest", source="research_agent",
                    good_for="embeddings")
    assert not reg.get("nomic-embed-text:latest").get("embedding")
    reg.upsert_auto("nomic-embed-text:latest", source="discovery", embedding=1)
    row = reg.get("nomic-embed-text:latest")
    assert row["embedding"] == 1                     # flag persisted (was silently dropped)
    assert row["source"] == "research_agent"         # provenance not downgraded


def test_manual_embedding_override_respected(tmp_path):
    reg = ModelRegistry(Database(tmp_path / "r.sqlite"))
    reg.manual_update("odd-embed-name:latest", embedding=0)   # operator: "not embedding-only"
    reg.upsert_auto("odd-embed-name:latest", source="discovery", embedding=1)
    assert reg.get("odd-embed-name:latest")["embedding"] == 0  # manual wins over the heuristic


def test_fallback_picker_skips_embedding(tmp_path):
    reg = _reg(tmp_path)
    reg.mark_embedding("nomic-embed-text:latest")

    pool = types.SimpleNamespace(
        available_models=lambda: {"qwen2.5:14b": ["b"], "nomic-embed-text:latest": ["b"]},
        backend_info=lambda m: {"type": "ollama"})
    # even if the embedding model sorts first alphabetically, it must not be picked
    picked = pick_fallback_model(pool, reg, {"benchmark_category": "general_chat"}, "hi")
    assert picked == "qwen2.5:14b"
