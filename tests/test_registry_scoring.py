"""Tests for the ingestion/scoring/selection redesign:
cost tier as a hard floor, two-level (tier, score) ranking with per-tier caps,
name-heuristic tags/content-policy, additive DB migration, and research
honesty (prerequisites + per-model status)."""

import json
import sqlite3

from foundry_router.db import Database
from foundry_router.brain import prompts
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.registry.openrouter_ingest import cost_tier
from foundry_router.registry.tagging import content_policy_from_name, tags_from_name


# -- migration ----------------------------------------------------------------

def test_migration_adds_columns_to_old_database(tmp_path):
    """A DB created before the tags/policy/research columns existed must be
    upgraded in place — CREATE TABLE IF NOT EXISTS alone never alters it."""
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE models (id TEXT PRIMARY KEY, display_name TEXT)")
    conn.commit()
    conn.close()

    db = Database(path)  # runs _migrate()
    cols = {r["name"] for r in db.query("PRAGMA table_info(models)")}
    assert {"tags", "content_policy", "research_status", "research_note"} <= cols


# -- tagging heuristics ---------------------------------------------------------

def test_tags_from_real_fleet_names():
    assert "coding" in tags_from_name("deepseek-coder:33b")
    assert "vision" in tags_from_name("llava:13b")
    assert "reasoning" in tags_from_name("deepseek-r1:32b")
    assert "tool-calling" in tags_from_name("ornith:9b-q4_K_M")
    assert tags_from_name("qwen3.5:9b") == []  # no false positives on plain names


def test_content_policy_detection_from_real_fleet_names():
    assert content_policy_from_name(
        "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest") == "permissive"
    assert content_policy_from_name("aratan/gemma-4-E4B-it-heretic:latest") == "permissive"
    assert content_policy_from_name("glm-4.7-flash:latest") is None  # unknown, not "standard"


def test_zero_cost_maps_to_free_tier():
    assert cost_tier(0) == "free"
    assert cost_tier(0.0005) == "low"
    assert cost_tier(None) is None


# -- two-level ranking -------------------------------------------------------------

def _seed(registry, model_id, tier, score=None, category="coding"):
    registry.upsert_auto(model_id, source="discovery", relative_cost_tier=tier,
                         display_name=model_id)
    if score is not None:
        registry.upsert_benchmark(model_id, category, score, "measured",
                                  "independent", confidence=1.0)


def test_free_tier_outranks_premium_regardless_of_score(tmp_path):
    registry = ModelRegistry(Database(tmp_path / "r.sqlite"))
    _seed(registry, "local-small", "free", score=55)
    _seed(registry, "claude-big", "very_high", score=99)
    ranked = registry.ranked_for_category("coding", ["local-small", "claude-big"])
    assert [r["id"] for r in ranked] == ["local-small", "claude-big"]


def test_score_orders_within_a_tier(tmp_path):
    registry = ModelRegistry(Database(tmp_path / "r2.sqlite"))
    _seed(registry, "local-a", "free", score=40)
    _seed(registry, "local-b", "free", score=90)
    ranked = registry.ranked_for_category("coding", ["local-a", "local-b"])
    assert [r["id"] for r in ranked] == ["local-b", "local-a"]


def test_per_tier_cap_keeps_premium_visible(tmp_path):
    """The escalation-dropout guard: many local models must not push paid
    tiers off the brain's candidate list (the earlier LIMIT 12 bug, inverted)."""
    registry = ModelRegistry(Database(tmp_path / "r3.sqlite"))
    locals_ = [f"local-{i}" for i in range(10)]
    for i, m in enumerate(locals_):
        _seed(registry, m, "free", score=50 + i)
    _seed(registry, "claude-opus", "very_high", score=99)
    ranked = registry.ranked_for_category("coding", locals_ + ["claude-opus"],
                                          limit=20, per_tier=5)
    ids = [r["id"] for r in ranked]
    assert len([i for i in ids if i.startswith("local-")]) == 5  # capped
    assert "claude-opus" in ids                                  # still visible
    assert ids[0].startswith("local-")                           # free leads


def test_unknown_tier_sorts_between_cheap_and_premium(tmp_path):
    registry = ModelRegistry(Database(tmp_path / "r4.sqlite"))
    _seed(registry, "cheap-known", "low", score=50)
    _seed(registry, "premium-known", "very_high", score=99)
    ranked = registry.ranked_for_category(
        "coding", ["cheap-known", "mystery-model", "premium-known"])
    ids = [r["id"] for r in ranked]
    assert ids.index("cheap-known") < ids.index("mystery-model") < ids.index("premium-known")


# -- prompt renders the tiered view + procedure ---------------------------------------

def test_prompt_groups_by_tier_and_states_procedure(tmp_path):
    registry = ModelRegistry(Database(tmp_path / "r5.sqlite"))
    registry.upsert_auto("local-m", source="discovery", relative_cost_tier="free",
                         tags=json.dumps(["coding"]), content_policy="permissive")
    rows = registry.ranked_for_category("coding", ["local-m"])
    system = prompts.build_system_prompt(None, rows, {"local-m": "ask_local_m"},
                                         "n/a", None)
    assert "[FREE / LOCAL" in system
    assert "tags: coding" in system
    assert "PERMISSIVE" in system
    assert "SELECTION PROCEDURE" in system
    assert "CHEAPEST tier that suffices" in system


# -- research honesty -----------------------------------------------------------------

def test_research_status_lifecycle_and_no_requeue_hammering(tmp_path):
    registry = ModelRegistry(Database(tmp_path / "r6.sqlite"))
    registry.set_research_status("m1", "failed", "search MCP tool unreachable")
    row = registry.get("m1")
    assert row["research_status"] == "failed"
    assert "unreachable" in row["research_note"]
    # a fresh failure marks the model as recently-attempted — the sweep must
    # not re-queue it every cycle
    assert registry.stale_or_missing(["m1"], stale_days=14) == []


def test_research_endpoint_refuses_when_prerequisites_missing(client):
    """The button must never report success for work that can't happen — the
    test deployment has research disabled and no MCP servers."""
    r = client.post("/admin/api/models/research", json={"model_id": "some-model"})
    assert r.status_code == 409
    body = r.json()
    assert body["ok"] is False and body["queued"] is False
    assert "disabled" in body["error"] or "MCP" in body["error"]


def test_models_list_reports_research_readiness(client):
    d = client.get("/admin/api/models").json()
    assert d["research_ready"] is False
    assert d["research_reason"]
