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
