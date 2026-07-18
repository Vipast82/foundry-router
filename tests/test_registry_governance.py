"""Tests for the registry governance controls (enable/disable), empirical
tool-calling reliability, and the reference research seed."""

import json

from foundry_router.brain import prompts
from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.registry.reference_seed import apply_reference_seed, best_match
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.tools.sync import ToolRegistry


class OneModelPool:
    def __init__(self, models):
        self._models = models

    def available_models(self):
        return {m: ["b"] for m in self._models}


# -- governance enable/disable (item 2) ----------------------------------------

def test_disabled_model_excluded_from_ranking_entirely(tmp_path):
    registry = ModelRegistry(Database(tmp_path / "g.sqlite"))
    registry.upsert_auto("model-a", source="discovery", relative_cost_tier="free")
    registry.upsert_auto("model-b", source="discovery", relative_cost_tier="free")
    registry.set_enabled("model-b", False)
    ranked = registry.ranked_for_category("coding", ["model-a", "model-b"])
    ids = [r["id"] for r in ranked]
    assert "model-a" in ids
    assert "model-b" not in ids  # excluded, not deprioritized — and NOT
    # resurrected as an unknown-model filler either


async def test_disabled_model_gets_no_ask_tool(tmp_path):
    db = Database(tmp_path / "g2.sqlite")
    registry = ModelRegistry(db)
    registry.upsert_auto("model-a", source="discovery")
    registry.upsert_auto("model-b", source="discovery")
    registry.set_enabled("model-b", False)
    tool_registry = ToolRegistry(db, registry, MCPManager([], db))
    await tool_registry.sync(OneModelPool(["model-a", "model-b"]))
    names = {t.name for t in tool_registry.enabled()}
    assert "ask_model_a" in names
    assert "ask_model_b" not in names


def test_toggle_endpoint(client):
    client.post("/admin/api/models/update", json={"id": "gov-model"})
    r = client.post("/admin/api/models/toggle", json={"id": "gov-model", "enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    models = {m["id"]: m for m in client.get("/admin/api/models").json()["models"]}
    assert models["gov-model"]["enabled"] == 0


# -- empirical tool-calling reliability (item 3) ---------------------------------

def test_reliability_counters_and_prompt_warning(tmp_path):
    registry = ModelRegistry(Database(tmp_path / "rel.sqlite"))
    for _ in range(2):
        registry.record_tool_call("flaky-model", ok=True)
    for _ in range(2):
        registry.record_tool_call("flaky-model", ok=False)
    row = registry.get("flaky-model")
    assert row["tool_calls_ok"] == 2 and row["tool_calls_failed"] == 2
    assert ModelRegistry.tool_reliability(row) == 0.5
    line = prompts._model_line({**row, "score": None}, "ask_flaky_model")
    assert "unreliable" in line and "2/4" in line


def test_reliability_needs_minimum_sample(tmp_path):
    registry = ModelRegistry(Database(tmp_path / "rel2.sqlite"))
    registry.record_tool_call("new-model", ok=False)
    assert ModelRegistry.tool_reliability(registry.get("new-model")) is None
    line = prompts._model_line({**registry.get("new-model"), "score": None}, "ask_new_model")
    assert "unreliable" not in line  # one bad call is not a verdict


# -- reference seed (item 4) -------------------------------------------------------

def test_best_match_prefers_specific_over_family():
    assert "coding" in best_match("qwen/qwen3-coder-480b")["tags"]
    assert best_match("qwen/qwen3-14b") is not None
    assert best_match("completely-unknown-model-xyz") is None


def test_seed_fills_catalog_and_respects_real_data(tmp_path):
    registry = ModelRegistry(Database(tmp_path / "seed.sqlite"))
    registry.upsert_auto("anthropic/claude-opus-4.6", source="openrouter_api",
                         relative_cost_tier="very_high")
    registry.upsert_auto("meta-llama/llama-3.3-70b-instruct", source="openrouter_api")
    # a manually-curated row must be untouched
    registry.manual_update("mistralai/mistral-large-2411", good_for="my own words")
    # a research_agent row must be untouched
    registry.upsert_auto("deepseek/deepseek-r1", source="discovery")
    registry.db.execute("UPDATE models SET source='research_agent', "
                        "good_for='researched text' WHERE id='deepseek/deepseek-r1'")

    applied = apply_reference_seed(registry)
    # opus + llama fully seeded; the manual row is untouched; the research row
    # keeps its real good_for but is SUPPLEMENTED for the gaps it left blank
    # (reasoning_style/tags), so it counts too.
    assert applied == 3

    opus = registry.get("anthropic/claude-opus-4.6")
    assert opus["good_for"] and "architecture" in opus["good_for"]
    assert json.loads(opus["tags"])
    cats = {b["category"]: b for b in registry.benchmarks("anthropic/claude-opus-4.6")}
    assert "coding" in cats and cats["coding"]["score_type"] == "estimated"
    assert "reference-seed" in cats["coding"]["source_url"]

    assert registry.get("mistralai/mistral-large-2411")["good_for"] == "my own words"
    deepseek = registry.get("deepseek/deepseek-r1")
    assert deepseek["good_for"] == "researched text"   # real research NOT overwritten
    assert deepseek["source"] == "research_agent"      # provenance preserved
    assert deepseek["reasoning_style"]                 # the blank gap WAS filled from seed


def test_seed_is_idempotent_and_never_clobbers_benchmarks(tmp_path):
    registry = ModelRegistry(Database(tmp_path / "seed2.sqlite"))
    registry.upsert_auto("openai/gpt-5", source="openrouter_api")
    # a pre-existing real benchmark row for one category
    registry.upsert_benchmark("openai/gpt-5", "coding", 99, "measured",
                              "independent", confidence=1.0)
    assert apply_reference_seed(registry) == 1
    assert apply_reference_seed(registry) == 0  # second run: no-op
    cats = {b["category"]: b for b in registry.benchmarks("openai/gpt-5")}
    assert cats["coding"]["score"] == 99          # real row survived
    assert cats["coding"]["score_type"] == "measured"
    assert "reasoning" in cats                     # missing categories filled
    assert cats["reasoning"]["score_type"] == "estimated"
