"""Named-benchmark tiebreakers: real, verifiable benchmarks (SWE-Bench Verified
et al.) stored separately from the composite categories (heterogeneous scales),
recognized during research, and used ONLY to break a near-tie between
candidates already close on composite. The motivating case: ornith:35b's real
SWE-Bench Verified 75.6 settling a close coding request that its synthesized
composite score alone couldn't."""

import pytest

from foundry_router.config import ResearchConfig
from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.registry.research_agent import (ResearchAgent,
                                                   match_known_benchmark)


def _reg(tmp_path, n="n.sqlite"):
    return ModelRegistry(Database(tmp_path / n))


def _agent(tmp_path):
    db = Database(tmp_path / "r.sqlite")
    registry = ModelRegistry(db)

    async def _llm(p):
        return ""

    return ResearchAgent(ResearchConfig(), db, registry, None,
                         llm=_llm, available_models=lambda: []), registry


# -- name matching ----------------------------------------------------------------

def test_match_known_benchmark_longest_substring():
    assert match_known_benchmark("SWE-Bench Verified")[0] == "SWE-Bench Verified"
    assert match_known_benchmark("swe-bench")[0] == "SWE-Bench"       # specific vs family
    assert match_known_benchmark("HumanEval+")[0] == "HumanEval+"
    assert match_known_benchmark("humaneval")[0] == "HumanEval"
    assert match_known_benchmark("BFCL")[1:] == ("tool_calling", "percent")
    assert match_known_benchmark("Chatbot Arena")[1:] == ("general_chat", "elo")
    assert match_known_benchmark("some unknown bench") is None
    assert match_known_benchmark("") is None
    assert match_known_benchmark(None) is None


# -- storage ----------------------------------------------------------------------

def test_upsert_named_benchmark_replaces(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_named_benchmark("m", "SWE-Bench Verified", "coding", 75.6, "percent",
                               source_url="http://x")
    reg.upsert_named_benchmark("m", "SWE-Bench Verified", "coding", 76.0, "percent")
    rows = reg.named_benchmarks("m")
    assert len(rows) == 1 and rows[0]["score"] == 76.0 and rows[0]["scale"] == "percent"


# -- extraction -------------------------------------------------------------------

def test_extraction_writes_recognized_verbatim_named(tmp_path):
    agent, reg = _agent(tmp_path)
    data = {"named_benchmarks": [
        {"name": "SWE-Bench Verified", "score": 75.6, "source_url": "http://ornith"},
        {"name": "Terminal-Bench", "score": 64.2},
        {"name": "Made-Up-Bench", "score": 99},   # unrecognized -> skipped
        {"name": "MMLU", "score": 40},             # recognized but not in text -> skipped
    ]}
    text = "SWE-Bench Verified 75.6 and Terminal-Bench 64.2 per the eval."
    agent._write_extraction("ornith:35b", data, text=text)
    named = {n["benchmark_name"]: n for n in reg.named_benchmarks("ornith:35b")}
    assert set(named) == {"SWE-Bench Verified", "Terminal-Bench"}
    assert named["SWE-Bench Verified"]["category"] == "coding"


def test_named_survives_when_generic_scores_are_skipped(tmp_path):
    # motivating case: generic scores are ungrounded (grounding gate drops them),
    # but the real named SWE-Bench number IS in the text and must be captured
    agent, reg = _agent(tmp_path)
    data = {
        "benchmarks": [{"category": "coding", "score": 50, "score_type": "estimated",
                        "source_type": "community_report", "confidence": 0.5}],
        "named_benchmarks": [{"name": "SWE-Bench Verified", "score": 75.6}],
    }
    agent._write_extraction("ornith:35b", data, text="Its SWE-Bench Verified is 75.6.")
    assert reg.benchmarks("ornith:35b") == []                       # 50 not in text -> skipped
    assert reg.named_benchmarks("ornith:35b")[0]["score"] == 75.6   # named survived


# -- tiebreaker in ranking --------------------------------------------------------

def _cand(reg, mid, comp, named=None, tier="free"):
    reg.upsert_auto(mid, source="discovery", relative_cost_tier=tier)
    reg.upsert_benchmark(mid, "coding", comp, "measured", "independent", confidence=1.0)
    if named is not None:
        reg.upsert_named_benchmark(mid, "SWE-Bench Verified", "coding", named, "percent")


def test_named_benchmark_breaks_a_near_tie(tmp_path):
    reg = _reg(tmp_path)
    _cand(reg, "ornith", 70.0, named=75.6)   # marginally lower composite, strong SWE-Bench
    _cand(reg, "rival", 71.0, named=40.0)    # marginally higher composite, weak SWE-Bench
    ranked = reg.ranked_for_category("coding", ["ornith", "rival"])
    assert [r["id"] for r in ranked][0] == "ornith"
    winner = next(r for r in ranked if r["id"] == "ornith")
    assert "SWE-Bench Verified" in (winner.get("_tiebreak") or "")   # auditable


def test_no_tiebreak_when_composite_gap_exceeds_epsilon(tmp_path):
    reg = _reg(tmp_path)
    _cand(reg, "ornith", 55.0, named=99.0)   # far lower composite despite huge SWE-Bench
    _cand(reg, "rival", 80.0, named=10.0)
    ranked = reg.ranked_for_category("coding", ["ornith", "rival"])
    assert [r["id"] for r in ranked][0] == "rival"                   # composite still rules


def test_no_named_data_leaves_order_unchanged(tmp_path):
    reg = _reg(tmp_path)
    _cand(reg, "a", 70.0)
    _cand(reg, "b", 71.0)
    assert [r["id"] for r in reg.ranked_for_category("coding", ["a", "b"])] == ["b", "a"]


def test_tiebreak_never_crosses_tiers(tmp_path):
    reg = _reg(tmp_path)
    _cand(reg, "local", 40.0, tier="free")
    _cand(reg, "claude", 41.0, named=99.0, tier="high")   # elite SWE-Bench, pricier tier
    ranked = reg.ranked_for_category("coding", ["local", "claude"])
    assert [r["id"] for r in ranked] == ["local", "claude"]          # tier-first is sacred


def test_non_percent_scale_excluded_from_tiebreak(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_auto("a", source="discovery", relative_cost_tier="free")
    reg.upsert_benchmark("a", "general_chat", 70, "measured", "independent", confidence=1.0)
    reg.upsert_auto("b", source="discovery", relative_cost_tier="free")
    reg.upsert_benchmark("b", "general_chat", 71, "measured", "independent", confidence=1.0)
    reg.upsert_named_benchmark("a", "Chatbot Arena", "general_chat", 1400, "elo")
    # ELO isn't naively comparable -> no tiebreak -> composite order stands
    assert [r["id"] for r in reg.ranked_for_category("general_chat", ["a", "b"])] == ["b", "a"]
