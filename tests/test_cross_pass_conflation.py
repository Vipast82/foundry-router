"""Cross-pass conflation reconcile (item 1): the in-pass guard only sees one
extraction's own output, so two categories that landed on the identical score
on SEPARATE research runs slip through as full-trust (found live: claude-opus-4-8
agentic 83.4 == tool_calling 83.4, both vendor, never flagged). The reconcile
scans all stored rows and demotes cross-category score collisions, idempotently.
Plus item 2: deeper research query set."""

from foundry_router.config import ResearchConfig
from foundry_router.db import Database
from foundry_router.registry.models_db import CONFLATION_DEMOTED_URL, ModelRegistry
from foundry_router.registry.research_agent import _QUERIES


def _reg(tmp_path, n="c.sqlite"):
    return ModelRegistry(Database(tmp_path / n))


# -- item 1: cross-pass detection -------------------------------------------------

def test_cross_pass_collision_demoted(tmp_path):
    reg = _reg(tmp_path)
    # two categories, identical score, written on separate passes as full vendor
    reg.upsert_benchmark("claude-opus-4-8", "agentic", 83.4, "measured", "vendor",
                         confidence=0.95)
    reg.upsert_benchmark("claude-opus-4-8", "tool_calling", 83.4, "measured",
                         "vendor", confidence=0.95)
    n = reg.reconcile_cross_category_collisions("claude-opus-4-8")
    assert n == 2
    rows = {r["category"]: r for r in reg.benchmarks("claude-opus-4-8")}
    for cat in ("agentic", "tool_calling"):
        assert rows[cat]["source_type"] == "community_report"    # demoted
        assert rows[cat]["score_type"] == "estimated"
        assert rows[cat]["confidence"] < 0.5
        assert rows[cat]["source_url"] == CONFLATION_DEMOTED_URL


def test_distinct_scores_untouched(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_benchmark("m", "coding", 73, "measured", "vendor", confidence=0.9)
    reg.upsert_benchmark("m", "reasoning", 61, "measured", "vendor", confidence=0.9)
    assert reg.reconcile_cross_category_collisions("m") == 0
    rows = {r["category"]: r for r in reg.benchmarks("m")}
    assert rows["coding"]["source_type"] == "vendor"             # unchanged


def test_reconcile_is_idempotent(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_benchmark("m", "agentic", 50, "measured", "vendor", confidence=0.9)
    reg.upsert_benchmark("m", "coding", 50, "measured", "vendor", confidence=0.9)
    reg.reconcile_cross_category_collisions("m")
    conf_after_first = {r["category"]: r["confidence"]
                        for r in reg.benchmarks("m")}
    # a second sweep must NOT re-demote (would keep shrinking confidence)
    assert reg.reconcile_cross_category_collisions("m") == 0
    conf_after_second = {r["category"]: r["confidence"]
                         for r in reg.benchmarks("m")}
    assert conf_after_first == conf_after_second


def test_manual_override_not_demoted(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_benchmark("m", "coding", 80, "measured", "manual_override", confidence=1.0)
    reg.upsert_benchmark("m", "reasoning", 80, "measured", "vendor", confidence=0.9)
    reg.reconcile_cross_category_collisions("m")
    rows = {r["category"]: r for r in reg.benchmarks("m")}
    assert rows["coding"]["source_type"] == "manual_override"    # hand-set kept
    assert rows["reasoning"]["source_type"] == "community_report"  # the auto one demoted


def test_reconcile_all_sweeps_every_model(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_benchmark("a", "agentic", 40, "measured", "vendor", confidence=0.9)
    reg.upsert_benchmark("a", "coding", 40, "measured", "vendor", confidence=0.9)
    reg.upsert_benchmark("b", "coding", 55, "measured", "vendor", confidence=0.9)  # no collision
    assert reg.reconcile_all_cross_category_collisions() == 2    # only model a's pair


# -- item 2: deeper research queries ----------------------------------------------

def test_research_queries_are_category_targeted():
    joined = " ".join(_QUERIES).lower()
    assert len(_QUERIES) >= 5
    assert "coding" in joined and "agentic" in joined and "reasoning" in joined


def test_corpus_limit_configurable():
    assert ResearchConfig().corpus_char_limit == 24000
    assert ResearchConfig().max_pages_per_model >= 5
