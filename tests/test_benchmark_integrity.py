"""Benchmark-row data integrity (found live: ornith:35b's `agentic` row held
its `reasoning` score, 27.8, both stamped vendor/0.95). Root cause is not a
variable-reuse write collision — it's the research extractor assigning one
number it found to multiple distinct categories, then self-reporting it as
high-confidence vendor data. Two defenses: a cross-category duplicate-score
guard at research write time, and an operator 'reset scores to seed' path to
correct a row that already got corrupted."""

import json

import pytest

from foundry_router.config import ResearchConfig
from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.registry.reference_seed import apply_seed_to_model
from foundry_router.registry.research_agent import ResearchAgent


# -- the research write guard -----------------------------------------------------

def _agent(tmp_path):
    db = Database(tmp_path / "r.sqlite")
    registry = ModelRegistry(db)

    async def _llm(prompt):
        return ""

    agent = ResearchAgent(ResearchConfig(), db, registry, None,
                          llm=_llm, available_models=lambda: [])
    return agent, registry, db


def test_same_score_across_categories_is_demoted(tmp_path):
    """The exact live signature: one number on two categories, self-reported as
    vendor/measured/0.95. It must NOT land as trustworthy vendor data."""
    agent, registry, db = _agent(tmp_path)
    data = {"benchmarks": [
        {"category": "reasoning", "score": 27.8, "score_type": "measured",
         "source_type": "vendor", "confidence": 0.95},
        {"category": "agentic", "score": 27.8, "score_type": "measured",
         "source_type": "vendor", "confidence": 0.95},
    ]}
    # the corpus even contains the number, so measured_score_in_text alone
    # would have let it through
    agent._write_extraction("ornith:35b", data, text="benchmark: 27.8 overall")
    rows = {b["category"]: b for b in registry.benchmarks("ornith:35b")}
    for cat in ("reasoning", "agentic"):
        assert rows[cat]["source_type"] == "community_report"   # trust stripped
        assert rows[cat]["score_type"] == "estimated"
        assert rows[cat]["confidence"] < 0.5                     # 0.95 * 0.4


def test_distinct_scores_are_kept_as_reported(tmp_path):
    agent, registry, db = _agent(tmp_path)
    data = {"benchmarks": [
        {"category": "reasoning", "score": 72, "score_type": "measured",
         "source_type": "vendor", "confidence": 0.9},
        {"category": "agentic", "score": 80, "score_type": "measured",
         "source_type": "vendor", "confidence": 0.9},
    ]}
    agent._write_extraction("m", data, text="reasoning 72 and agentic 80")
    rows = {b["category"]: b for b in registry.benchmarks("m")}
    assert rows["reasoning"]["score"] == 72 and rows["reasoning"]["source_type"] == "vendor"
    assert rows["agentic"]["score"] == 80 and rows["agentic"]["source_type"] == "vendor"


def test_ungrounded_scores_skipped_for_thin_source(tmp_path):
    """Item 3: a low-web-presence model (ornith) has no real per-category
    numbers, so the extractor fabricates. If NOT ONE score appears verbatim in
    the sources, record none — an honest 'no data' state instead of feeding the
    conflation guard every pass."""
    agent, registry, db = _agent(tmp_path)
    data = {"benchmarks": [
        {"category": "coding", "score": 73.4, "score_type": "estimated",
         "source_type": "community_report", "confidence": 0.5},
        {"category": "reasoning", "score": 61.0, "score_type": "estimated",
         "source_type": "community_report", "confidence": 0.5},
    ]}
    wrote = agent._write_extraction(
        "ornith:35b", data, text="A short bio page with no benchmark numbers.")
    assert wrote == 0
    assert registry.benchmarks("ornith:35b") == []      # absent, not fabricated


def test_one_grounded_number_keeps_the_estimate_set(tmp_path):
    # a single real quoted number means the model has coverage -> estimates for
    # the other categories are trustworthy enough to keep
    agent, registry, db = _agent(tmp_path)
    data = {"benchmarks": [
        {"category": "coding", "score": 73.4, "score_type": "measured",
         "source_type": "independent", "confidence": 0.8},
        {"category": "reasoning", "score": 61.0, "score_type": "estimated",
         "source_type": "community_report", "confidence": 0.5},
    ]}
    agent._write_extraction("m", data, text="Its SWE-bench score is 73.4 per the report.")
    cats = {b["category"] for b in registry.benchmarks("m")}
    assert cats == {"coding", "reasoning"}


def test_duplicate_within_one_category_is_not_flagged(tmp_path):
    # two entries for the SAME category isn't a cross-category conflation
    agent, registry, db = _agent(tmp_path)
    data = {"benchmarks": [
        {"category": "coding", "score": 55, "score_type": "measured",
         "source_type": "independent", "confidence": 0.8},
        {"category": "coding", "score": 55, "score_type": "measured",
         "source_type": "independent", "confidence": 0.8},
    ]}
    agent._write_extraction("m", data, text="coding 55")
    rows = registry.benchmarks("m")
    assert len(rows) == 1 and rows[0]["source_type"] == "independent"


# -- operator reset path ----------------------------------------------------------

def test_reset_clears_automatic_keeps_manual(tmp_path):
    registry = ModelRegistry(Database(tmp_path / "x.sqlite"))
    registry.upsert_benchmark("m", "agentic", 27.8, "measured", "vendor",
                              confidence=0.95)                     # corrupted auto row
    registry.upsert_benchmark("m", "coding", 90, "measured", "manual_override",
                              confidence=1.0)                      # hand-set, must survive
    removed = registry.reset_benchmarks("m")
    assert removed == 1
    cats = {b["category"]: b for b in registry.benchmarks("m")}
    assert "agentic" not in cats                                   # auto row gone
    assert cats["coding"]["source_type"] == "manual_override"      # manual kept


def test_reset_then_reseed_restores_clean_defaults(tmp_path):
    registry = ModelRegistry(Database(tmp_path / "y.sqlite"))
    # ornith's model row is research_agent-sourced (so the whole-registry seed
    # would skip it) and its agentic row is corrupted
    registry.upsert_auto("ornith:35b", source="research_agent")
    registry.upsert_benchmark("ornith:35b", "agentic", 27.8, "measured",
                              "vendor", confidence=0.95)
    registry.reset_benchmarks("ornith:35b")
    reseeded = apply_seed_to_model(registry, "ornith:35b")
    assert reseeded > 0
    rows = {b["category"]: b for b in registry.benchmarks("ornith:35b")}
    # seed intent restored: agentic 80, reasoning 72, and they're DISTINCT again
    assert rows["agentic"]["score"] == 80
    assert rows["reasoning"]["score"] == 72
    assert rows["agentic"]["score"] != rows["reasoning"]["score"]


def test_reset_benchmarks_endpoint(client):
    # seed a corrupted collision through the registry, then reset via the API
    from foundry_router.registry.models_db import ModelRegistry
    # use the app's own registry so the endpoint sees it
    import foundry_router.ui.routes as routes  # noqa: F401
    r0 = client.post("/admin/api/models/update",
                     json={"id": "ornith:35b", "good_for": "x"})
    assert r0.status_code == 200
    # (endpoint smoke: a model with no seed match still succeeds, removing 0)
    r = client.post("/admin/api/models/reset_benchmarks",
                    json={"model_id": "ornith:35b"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and "benchmarks" in body
