"""Extraction write-path robustness: the research LLM's JSON can hand back a
list where a scalar column is expected (found live: ProgrammingError "type
'list' is not supported" crashing whole research passes on qwen3.6:35b and
hermes3:8b). A malformed shape must degrade to a usable string, never crash the
write. Plus the prompt now instructs against reusing one score across
categories (the broader conflation root cause)."""

import pytest

from foundry_router.config import ResearchConfig
from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry, _scalar
from foundry_router.registry.research_agent import (EXTRACTION_PROMPT,
                                                    ResearchAgent,
                                                    match_known_benchmark)


def _agent(tmp_path):
    db = Database(tmp_path / "r.sqlite")
    registry = ModelRegistry(db)

    async def _llm(p):
        return ""

    return ResearchAgent(ResearchConfig(), db, registry, None,
                         llm=_llm, available_models=lambda: []), registry


# -- the scalar coercion ----------------------------------------------------------

def test_scalar_coerces_non_bindable():
    assert _scalar(["coding", "chat"]) == "coding, chat"
    assert _scalar({"a": 1}) == '{"a": 1}'
    assert _scalar("plain") == "plain"
    assert _scalar(42) == 42
    assert _scalar(None) is None


def test_upsert_auto_survives_list_field(tmp_path):
    reg = ModelRegistry(Database(tmp_path / "m.sqlite"))
    reg.upsert_auto("m", source="discovery", relative_cost_tier="free")  # row exists -> UPDATE path
    # good_for as a list is exactly the live crash shape (parameter 2 in UPDATE)
    reg.upsert_auto("m", source="research_agent",
                    reasoning_style="reasons step by step",
                    good_for=["coding", "chat", "reasoning"])
    row = reg.get("m")
    assert row["good_for"] == "coding, chat, reasoning"       # coerced, not crashed


# -- full extraction path with malformed shapes -----------------------------------

def test_write_extraction_survives_list_good_for(tmp_path):
    agent, reg = _agent(tmp_path)
    reg.upsert_auto("hermes3:8b", source="discovery", relative_cost_tier="free")
    data = {
        "reasoning_style": "concise",
        "good_for": ["coding", "reasoning"],          # the crash trigger
        "benchmarks": [{"category": "coding", "score": 55, "score_type": "measured",
                        "source_type": "independent", "confidence": 0.8}],
    }
    # must not raise
    agent._write_extraction("hermes3:8b", data, text="coding 55 reported")
    assert reg.get("hermes3:8b")["good_for"] == "coding, reasoning"


def test_write_extraction_survives_garbage_shapes(tmp_path):
    agent, reg = _agent(tmp_path)
    reg.upsert_auto("m", source="discovery", relative_cost_tier="free")
    data = {
        "good_for": {"nested": "dict"},
        "benchmarks": ["not a dict", {"category": ["coding"], "score": [1, 2]}],
        "named_benchmarks": ["bad", {"name": ["SWE-Bench"], "score": 70},
                             {"name": "SWE-Bench Verified", "score": 75.6}],
    }
    # none of these malformed entries may crash the write
    agent._write_extraction("m", data, text="SWE-Bench Verified 75.6")
    # the one well-formed named benchmark still lands
    named = {n["benchmark_name"] for n in reg.named_benchmarks("m")}
    assert "SWE-Bench Verified" in named


def test_match_known_benchmark_rejects_non_string():
    assert match_known_benchmark(["SWE-Bench"]) is None
    assert match_known_benchmark(None) is None
    assert match_known_benchmark(42) is None
    assert match_known_benchmark("SWE-Bench Verified")[0] == "SWE-Bench Verified"


# -- item 2: prompt discourages cross-category score reuse ------------------------

def test_extraction_prompt_forbids_score_reuse():
    p = EXTRACTION_PROMPT.lower()
    assert "same" in p and "two different categories" in p
    assert "omit" in p            # omission preferred over a duplicated guess
