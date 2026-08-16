"""Registry data-integrity guards (registry-integrity spec):
 #1 score_source distinguishes a reference-seed ESTIMATE from a researched/
    observed/manual value so seeds are never shown as measured data;
 #2 cross-model duplicate detection flags the same named-benchmark value on
    unrelated models — the mis-attribution signature (a leaderboard number
    scraped onto the wrong model), the actual cause of the ornith / Qwen3.8
    'SWE-Bench Verified 75.6' collision."""

from foundry_router.db import Database
from foundry_router.registry.models_db import (ModelRegistry, score_source,
                                              _lineage_tokens)
from foundry_router.registry.reference_seed import SEED_SOURCE_URL
from foundry_router.registry.models_db import CONFLATION_DEMOTED_URL


# -- score_source derivation (#1) -------------------------------------------------

def test_score_source_classifies_provenance():
    assert score_source({"source_type": "manual_override"}) == "manual_override"
    assert score_source({"source_type": "observed",
                         "source_url": "observed:live-traffic"}) == "observed"
    assert score_source({"source_type": "community_report",
                         "source_url": SEED_SOURCE_URL}) == "seed"
    assert score_source({"source_type": "community_report",
                         "source_url": CONFLATION_DEMOTED_URL}) == "conflation"
    assert score_source({"source_type": "vendor",
                         "source_url": "https://arxiv.org/x"}) == "researched"
    # a bare seed-prefixed url (future seed versions) still reads as seed
    assert score_source({"source_url": "reference-seed:fable-6-2027"}) == "seed"


def test_seed_benchmark_reads_as_seed(tmp_path):
    reg = ModelRegistry(Database(tmp_path / "s.sqlite"))
    reg.upsert_benchmark("m", "coding", 78.0, score_type="estimated",
                         source_type="community_report", source_url=SEED_SOURCE_URL,
                         confidence=0.5)
    assert score_source(reg.benchmarks("m")[0]) == "seed"


# -- lineage detection ------------------------------------------------------------

def test_lineage_tokens_ignore_quant_and_repackager_noise():
    # a base and its GGUF quant share the family token -> related
    assert _lineage_tokens("qwen3.8-27b") & _lineage_tokens("hf.co/unsloth/Qwen3.8-27B-GGUF")
    # unrelated models share nothing
    assert not (_lineage_tokens("ornith:35b") & _lineage_tokens("hf.co/unsloth/Qwen3.8-27B-GGUF"))
    # pure size/version tokens don't create false lineage
    assert not (_lineage_tokens("foo-27b") & _lineage_tokens("bar-27b"))


# -- cross-model duplicate flag (#2) ----------------------------------------------

def _named(reg, model, name, score, category="coding"):
    reg.upsert_named_benchmark(model, name, category, score, "percent",
                               source_url="http://x")


def test_identical_value_on_unrelated_models_is_flagged(tmp_path):
    reg = ModelRegistry(Database(tmp_path / "c.sqlite"))
    # the reported bug: same SWE-Bench Verified 75.6 on two unrelated models
    _named(reg, "ornith:35b", "SWE-Bench Verified", 75.6)
    _named(reg, "hf.co/unsloth/Qwen3.8-27B-GGUF", "SWE-Bench Verified", 75.6)
    flags = reg.cross_model_named_flags("ornith:35b")
    assert "SWE-Bench Verified" in flags
    assert "hf.co/unsloth/Qwen3.8-27B-GGUF" in flags["SWE-Bench Verified"]
    # symmetric
    assert "SWE-Bench Verified" in reg.cross_model_named_flags(
        "hf.co/unsloth/Qwen3.8-27B-GGUF")


def test_same_value_on_related_variants_not_flagged(tmp_path):
    reg = ModelRegistry(Database(tmp_path / "r.sqlite"))
    # a base model and its own quant legitimately share a benchmark
    _named(reg, "qwen3.8-27b", "SWE-Bench Pro", 61.7)
    _named(reg, "hf.co/unsloth/Qwen3.8-27B-GGUF", "SWE-Bench Pro", 61.7)
    assert reg.cross_model_named_flags("qwen3.8-27b") == {}


def test_different_values_not_flagged(tmp_path):
    reg = ModelRegistry(Database(tmp_path / "d.sqlite"))
    _named(reg, "ornith:35b", "SWE-Bench Verified", 75.6)
    _named(reg, "hf.co/unsloth/Qwen3.8-27B-GGUF", "SWE-Bench Verified", 61.7)
    assert reg.cross_model_named_flags("ornith:35b") == {}


def test_single_model_never_flagged(tmp_path):
    reg = ModelRegistry(Database(tmp_path / "o.sqlite"))
    _named(reg, "ornith:35b", "SWE-Bench Verified", 75.6)
    assert reg.cross_model_named_flags("ornith:35b") == {}


# -- endpoints --------------------------------------------------------------------

def test_benchmarks_endpoint_carries_source_and_flags(client, app):
    reg = app.state.services.registry
    reg.upsert_benchmark("m1", "coding", 78.0, score_type="estimated",
                         source_type="community_report", source_url=SEED_SOURCE_URL,
                         confidence=0.5)
    _named(reg, "m1", "SWE-Bench Verified", 75.6)
    _named(reg, "totally-different", "SWE-Bench Verified", 75.6)
    d = client.get("/admin/api/models/benchmarks?model_id=m1").json()
    assert d["benchmarks"][0]["score_source"] == "seed"
    assert "SWE-Bench Verified" in d["cross_model_flags"]


def test_research_config_accepts_model(client):
    r = client.post("/admin/api/config/research", json={"model": "qwen3.8:27b"})
    assert r.status_code == 200
    cfg = client.get("/admin/api/config").json()
    assert cfg["registry"]["research"]["model"] == "qwen3.8:27b"
    # empty clears back to brain default (None)
    client.post("/admin/api/config/research", json={"model": ""})
    cfg = client.get("/admin/api/config").json()
    assert cfg["registry"]["research"]["model"] is None


# -- embedding-model scoring + cleanup --------------------------------------------

def test_clear_embedding_benchmarks(tmp_path):
    reg = ModelRegistry(Database(tmp_path / "e.sqlite"))
    reg.upsert_auto("nomic-embed-text", source="discovery", embedding=1)
    reg.upsert_benchmark("nomic-embed-text", "agentic", 0.78448,
                         score_type="estimated", source_type="community_report",
                         source_url="http://x", confidence=0.4)
    _named(reg, "nomic-embed-text", "MMLU", 62.0)
    # a chat model's rows are untouched
    reg.upsert_benchmark("chatty", "coding", 80.0, score_type="measured",
                         source_type="vendor", source_url="http://y", confidence=0.9)
    removed = reg.clear_embedding_benchmarks()
    assert removed == 1
    assert reg.benchmarks("nomic-embed-text") == []
    assert reg.named_benchmarks("nomic-embed-text") == []
    assert len(reg.benchmarks("chatty")) == 1        # non-embedding untouched


# -- research write path: embedding skip, no-info filter, seed guard --------------

def _agent(tmp_path):
    from foundry_router.config import ResearchConfig
    from foundry_router.registry.research_agent import ResearchAgent
    db = Database(tmp_path / "w.sqlite")
    reg = ModelRegistry(db)

    async def _llm(p):
        return ""
    return ResearchAgent(ResearchConfig(), db, reg, mcp_manager=None, llm=_llm,
                         available_models=lambda: []), reg, db


def test_research_skips_scoring_embedding_models(tmp_path):
    agent, reg, _ = _agent(tmp_path)
    reg.upsert_auto("nomic-embed-text", source="discovery", embedding=1)
    data = {"reasoning_style": "dense vector embeddings", "good_for": "RAG",
            "benchmarks": [{"category": "agentic", "score": 0.78448,
                            "score_type": "estimated"}],
            "named_benchmarks": [{"name": "MMLU", "score": 62.0}]}
    wrote = agent._write_extraction("nomic-embed-text", data, "0.78448 62.0")
    assert wrote == 0
    assert reg.benchmarks("nomic-embed-text") == []       # no chat scores
    assert reg.named_benchmarks("nomic-embed-text") == []
    assert reg.get("nomic-embed-text")["good_for"] == "RAG"   # qualitative kept


def test_no_info_reasoning_style_treated_as_blank(tmp_path):
    agent, reg, _ = _agent(tmp_path)
    data = {"reasoning_style": "No available information in the provided research "
                              "text describes how hermes3 reasons.",
            "good_for": "coding", "benchmarks": []}
    agent._write_extraction("hermes3:8b", data, "")
    # the narration is dropped (None) rather than stored
    assert reg.get("hermes3:8b")["reasoning_style"] is None
    assert reg.get("hermes3:8b")["good_for"] == "coding"


def test_seed_estimate_not_overwritten_by_weaker_research(tmp_path):
    from foundry_router.registry.reference_seed import SEED_SOURCE_URL
    agent, reg, _ = _agent(tmp_path)
    # a good curated seed estimate
    reg.upsert_benchmark("claude-sonnet-4-6", "reasoning", 92.0,
                         score_type="estimated", source_type="community_report",
                         source_url=SEED_SOURCE_URL, confidence=0.6)
    # a weak ESTIMATED research pass tries to clobber it with 15
    data = {"benchmarks": [{"category": "reasoning", "score": 15.0,
                            "score_type": "estimated", "source_type": "community_report",
                            "confidence": 0.4}]}
    agent._write_extraction("claude-sonnet-4-6", data, "reasoning 15")
    kept = reg.benchmarks("claude-sonnet-4-6")[0]
    assert kept["score"] == 92.0 and score_source(kept) == "seed"   # seed held


def test_fraction_scores_normalized_to_percent(tmp_path):
    from foundry_router.registry.research_agent import _normalize_percent
    assert _normalize_percent(0.9) == 90.0
    assert _normalize_percent(0.78448) == 78.448
    assert _normalize_percent(75.6) == 75.6          # already a percent, unchanged
    assert _normalize_percent(1.0) == 1.0            # ambiguous — left alone
    assert _normalize_percent(0.0) == 0.0

    agent, reg, _ = _agent(tmp_path)
    data = {"benchmarks": [{"category": "coding", "score": 0.9,
                            "score_type": "measured", "source_type": "vendor",
                            "confidence": 0.8}],
            "named_benchmarks": [{"name": "SWE-Bench Verified", "score": 0.9}]}
    agent._write_extraction("claude-fable-5", data, "coding 0.9 SWE-Bench 0.9")
    assert reg.benchmarks("claude-fable-5")[0]["score"] == 90.0
    assert reg.named_benchmarks("claude-fable-5")[0]["score"] == 90.0


def test_named_to_category_leak_dropped_when_category_mismatches(tmp_path):
    agent, reg, _ = _agent(tmp_path)
    # Terminal-Bench (a CODING benchmark) 74.6 copied into tool_calling -> leak;
    # SWE-Bench Pro (CODING) 80.3 copied into agentic -> leak;
    # HumanEval (CODING) 85.2 into coding -> consistent, kept.
    text = "Terminal-Bench 74.6 SWE-Bench Pro 80.3 HumanEval 85.2 general_chat 79"
    data = {
        "named_benchmarks": [
            {"name": "Terminal-Bench", "score": 74.6},
            {"name": "SWE-Bench Pro", "score": 80.3},
            {"name": "HumanEval", "score": 85.2},
        ],
        "benchmarks": [
            {"category": "tool_calling", "score": 74.6, "score_type": "measured"},
            {"category": "agentic", "score": 80.3, "score_type": "measured"},
            {"category": "coding", "score": 85.2, "score_type": "measured"},
            {"category": "general_chat", "score": 79.0, "score_type": "measured"},
        ],
    }
    agent._write_extraction("some-model", data, text)
    cats = {b["category"]: b["score"] for b in reg.benchmarks("some-model")}
    assert "tool_calling" not in cats          # Terminal-Bench leak dropped
    assert "agentic" not in cats               # SWE-Bench Pro leak dropped
    assert cats.get("coding") == 85.2          # HumanEval -> coding kept (consistent)
    assert cats.get("general_chat") == 79.0    # unrelated category kept


def test_category_consistent_named_match_is_kept(tmp_path):
    agent, reg, _ = _agent(tmp_path)
    # GPQA Diamond (REASONING) 89.2 and reasoning 89.2 -> same category, keep
    data = {"named_benchmarks": [{"name": "GPQA Diamond", "score": 89.2}],
            "benchmarks": [{"category": "reasoning", "score": 89.2,
                            "score_type": "measured"}]}
    agent._write_extraction("qwen3.8", data, "GPQA Diamond 89.2 reasoning 89.2")
    assert reg.benchmarks("qwen3.8")[0]["score"] == 89.2


def test_measured_research_still_supersedes_seed(tmp_path):
    from foundry_router.registry.reference_seed import SEED_SOURCE_URL
    agent, reg, _ = _agent(tmp_path)
    reg.upsert_benchmark("qwen3.8", "coding", 70.0, score_type="estimated",
                         source_type="community_report", source_url=SEED_SOURCE_URL,
                         confidence=0.6)
    # a MEASURED (verbatim) research number does replace the seed
    data = {"benchmarks": [{"category": "coding", "score": 81.5,
                            "score_type": "measured", "source_type": "vendor",
                            "confidence": 0.8}]}
    agent._write_extraction("qwen3.8", data, "coding score is 81.5 on the eval")
    kept = reg.benchmarks("qwen3.8")[0]
    assert kept["score"] == 81.5 and score_source(kept) == "researched"
