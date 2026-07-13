"""Multi-signal within-tier selection: adequacy (observed outcome-judge
quality) and reliability (call success) as scoring signals, source/recency
confidence calibration, per-persona weight overrides, a context-length gate,
and — the concrete operator requirement — permissive/uncensored models avoided
for normal work and preferred only by a permissive persona (a strong standard
model like ornith:35b must beat an abliterated model like qwen3-14b unless the
request is for content standard models refuse)."""

import pytest

from foundry_router.db import Database
from foundry_router.registry.models_db import (ModelRegistry, _effective_confidence,
                                              _recency_factor)


def _reg(tmp_path, name="s.sqlite"):
    return ModelRegistry(Database(tmp_path / name))


def _order(reg, ids, category="general_chat", **kw):
    return [r["id"] for r in reg.ranked_for_category(category, ids, **kw)]


# -- the headline case: ornith:35b over qwen3-14b-abliterated ----------------------

def test_standard_model_beats_higher_scoring_permissive_by_default(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_auto("ornith:35b", source="discovery", relative_cost_tier="free")
    reg.upsert_auto("qwen3-14b-abliterated", source="discovery",
                    relative_cost_tier="free", content_policy="permissive")
    reg.upsert_benchmark("ornith:35b", "general_chat", 64, "estimated",
                         "community_report", confidence=0.5)
    reg.upsert_benchmark("qwen3-14b-abliterated", "general_chat", 75, "estimated",
                         "community_report", confidence=0.55)
    # default avoid: the standard model wins despite the abliterated one scoring
    # higher — permissive models are for refused content, not general quality
    assert _order(reg, ["ornith:35b", "qwen3-14b-abliterated"],
                  permissive_mode="avoid")[0] == "ornith:35b"
    # a permissive persona explicitly wants it
    assert _order(reg, ["ornith:35b", "qwen3-14b-abliterated"],
                  permissive_mode="prefer")[0] == "qwen3-14b-abliterated"


def test_permissive_still_reachable_as_last_resort(tmp_path):
    # avoid SINKS permissive, it doesn't remove it — if it's the only option it
    # still appears (a permissive request routed to a normal persona degrades,
    # not fails)
    reg = _reg(tmp_path)
    reg.upsert_auto("only-wild", source="discovery", relative_cost_tier="free",
                    content_policy="permissive")
    assert _order(reg, ["only-wild"], permissive_mode="avoid") == ["only-wild"]


# -- adequacy signal (observed outcome-judge quality) -----------------------------

def test_adequacy_lifts_a_repeatedly_adequate_model(tmp_path):
    reg = _reg(tmp_path)
    for m in ("steady", "flashy"):
        reg.upsert_auto(m, source="discovery", relative_cost_tier="free")
    # flashy scores a bit higher on the category...
    reg.upsert_benchmark("steady", "general_chat", 70, "estimated",
                         "community_report", confidence=0.6)
    reg.upsert_benchmark("flashy", "general_chat", 76, "estimated",
                         "community_report", confidence=0.6)
    # ...but steady has a long track record of adequate real answers
    for _ in range(40):
        reg.record_outcome("steady", adequate=True)
    for _ in range(40):
        reg.record_outcome("flashy", adequate=False)
    assert _order(reg, ["steady", "flashy"])[0] == "steady"


def test_single_verdict_does_not_tank_a_model(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_auto("m", source="discovery", relative_cost_tier="free")
    reg.upsert_benchmark("m", "general_chat", 90, "measured", "independent",
                         confidence=0.9)
    base = reg.ranked_for_category("general_chat", ["m"])[0]["_composite"]
    reg.record_outcome("m", adequate=False)   # one bad verdict, low confidence
    after = reg.ranked_for_category("general_chat", ["m"])[0]["_composite"]
    # it dips, but nowhere near collapse — confidence-weighted, low sample
    assert after < base
    assert after > 0.8 * base


# -- reliability multiplier (flaky models penalized) ------------------------------

def test_flaky_model_is_penalized(tmp_path):
    reg = _reg(tmp_path)
    for m in ("solid", "flaky"):
        reg.upsert_auto(m, source="discovery", relative_cost_tier="free")
        reg.upsert_benchmark(m, "general_chat", 80, "measured", "independent",
                             confidence=0.9)
    # identical category scores, but flaky fails a third of its calls
    for _ in range(10):
        reg.record_call_outcome("solid", ok=True)
    for _ in range(7):
        reg.record_call_outcome("flaky", ok=True)
    for _ in range(7):
        reg.record_call_outcome("flaky", ok=False)
    assert _order(reg, ["solid", "flaky"]) == ["solid", "flaky"]


def test_reliability_needs_minimum_sample(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_auto("m", source="discovery", relative_cost_tier="free")
    reg.upsert_benchmark("m", "general_chat", 80, "measured", "independent",
                         confidence=1.0)
    reg.record_call_outcome("m", ok=False)   # 1 failure, below min sample
    comp = reg.ranked_for_category("general_chat", ["m"])[0]["_composite"]
    assert comp == pytest.approx(80.0)       # no penalty yet


# -- confidence calibration: source + recency -------------------------------------

def test_source_calibration_prefers_trustworthy_provenance(tmp_path):
    reg = _reg(tmp_path)
    for m in ("measured-m", "guessed-m"):
        reg.upsert_auto(m, source="discovery", relative_cost_tier="free")
    # same raw score & stored confidence, different provenance
    reg.upsert_benchmark("measured-m", "general_chat", 80, "measured",
                         "independent", confidence=0.7)
    reg.upsert_benchmark("guessed-m", "general_chat", 80, "estimated",
                         "estimated", confidence=0.7)
    # single-signal composite is the raw score for both (calibration scales the
    # WEIGHT), so tie on score — but effective confidence differs, which matters
    # the moment a second signal competes. Assert the calibration directly:
    ind = _effective_confidence({"confidence": 0.7, "source_type": "independent",
                                 "last_updated": None})
    est = _effective_confidence({"confidence": 0.7, "source_type": "estimated",
                                 "last_updated": None})
    assert ind > est


def test_recency_decays_old_numbers(tmp_path):
    assert _recency_factor(None) == 1.0
    from datetime import datetime, timedelta, timezone
    fresh = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    assert _recency_factor(fresh) == 1.0
    assert _recency_factor(old) < 1.0


# -- per-persona weight overrides -------------------------------------------------

def test_persona_can_weight_latency(tmp_path):
    reg = _reg(tmp_path)
    for m in ("thorough", "snappy"):
        reg.upsert_auto(m, source="discovery", relative_cost_tier="free")
        reg.upsert_benchmark(m, "general_chat", 75, "measured", "independent",
                             confidence=0.9)
    # snappy is much faster (warm tokens/sec), thorough is slow
    reg.upsert_benchmark("snappy", "latency", 100, "measured", "observed",
                         confidence=0.9)
    reg.upsert_benchmark("thorough", "latency", 20, "measured", "observed",
                         confidence=0.9)
    # default: latency weight 0 -> equal category score -> input order kept
    assert set(_order(reg, ["thorough", "snappy"])) == {"thorough", "snappy"}
    # a latency-sensitive persona weights it -> snappy leads
    assert _order(reg, ["thorough", "snappy"],
                  weights={"latency": 1.0})[0] == "snappy"


# -- context-length gate ----------------------------------------------------------

def test_context_gate_sinks_models_that_cannot_fit(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_auto("small-ctx", source="discovery", relative_cost_tier="free",
                    context_length=4096)
    reg.upsert_auto("big-ctx", source="discovery", relative_cost_tier="free",
                    context_length=131072)
    # small-ctx scores higher, but can't hold the request
    reg.upsert_benchmark("small-ctx", "general_chat", 95, "measured",
                         "independent", confidence=0.9)
    reg.upsert_benchmark("big-ctx", "general_chat", 60, "measured",
                         "independent", confidence=0.9)
    assert _order(reg, ["small-ctx", "big-ctx"], min_context=32000) == \
        ["big-ctx", "small-ctx"]
    # unknown context_length is never gated (no data != too small)
    reg.upsert_auto("unknown-ctx", source="discovery", relative_cost_tier="free")
    reg.upsert_benchmark("unknown-ctx", "general_chat", 99, "measured",
                         "independent", confidence=0.9)
    assert _order(reg, ["unknown-ctx", "big-ctx"],
                  min_context=32000)[0] == "unknown-ctx"


# -- tier order is still sacred ---------------------------------------------------

def test_all_signals_never_cross_tiers(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_auto("local", source="discovery", relative_cost_tier="free")
    reg.upsert_auto("claude", source="discovery", relative_cost_tier="very_high")
    reg.upsert_benchmark("local", "general_chat", 30, "estimated",
                         "community_report", confidence=0.4)
    reg.upsert_benchmark("claude", "general_chat", 99, "measured", "vendor",
                         confidence=0.95)
    for _ in range(20):                       # claude also perfectly adequate/reliable
        reg.record_outcome("claude", adequate=True)
        reg.record_call_outcome("claude", ok=True)
    assert _order(reg, ["local", "claude"]) == ["local", "claude"]


# -- persona weight override survives the admin API round-trip --------------------

def test_selection_weights_persona_roundtrip(client):
    client.post("/admin/api/personas", json={
        "virtual_name": "Foundry-Chat", "selection_weights": {"latency": 0.5}})
    import json
    d = client.get("/admin/api/personas").json()
    chat = next(p for p in d["personas"] if p["virtual_name"] == "Foundry-Chat")
    assert json.loads(chat["selection_weights"]) == {"latency": 0.5}
