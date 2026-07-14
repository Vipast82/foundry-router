"""Batch fixes (2026-07-14): server-side pending-question state (no marker leak
into content), ClawEval pinned to agentic, manual named-benchmark entry that
research won't clobber, and the raised research retry ceiling."""

import json

from foundry_router.brain import prompts
from foundry_router.config import ResearchConfig
from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.registry.research_agent import KNOWN_BENCHMARKS, match_known_benchmark


# -- #1: pending question is server-side, nothing leaks into content --------------

def test_pending_question_roundtrip_server_side(tmp_path):
    db = Database(tmp_path / "p.sqlite")
    # ask_user fired on a conversation of [U1]
    convo = [{"role": "user", "content": "book me a flight"}]
    prompts.store_pending_question(db, convo, "Which city are you flying to?")
    # the resuming request echoes [U1, assistant-question, U2-reply]
    resuming = [
        {"role": "user", "content": "book me a flight"},
        {"role": "assistant", "content": "Which city are you flying to?"},
        {"role": "user", "content": "Boston"},
    ]
    assert prompts.find_pending_question(db, resuming) == "Which city are you flying to?"


def test_pending_question_consumed_once(tmp_path):
    db = Database(tmp_path / "p2.sqlite")
    convo = [{"role": "user", "content": "hi"}]
    prompts.store_pending_question(db, convo, "Q?")
    resuming = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "Q?"},
                {"role": "user", "content": "reply"}]
    assert prompts.find_pending_question(db, resuming) == "Q?"
    assert prompts.find_pending_question(db, resuming) is None   # consumed


def test_no_pending_question_returns_none(tmp_path):
    db = Database(tmp_path / "p3.sqlite")
    assert prompts.find_pending_question(
        db, [{"role": "user", "content": "fresh conversation"}]) is None


# -- #2: ClawEval -> agentic, no longer split across coding/agentic ---------------

def test_claweval_maps_to_agentic():
    canonical, category, scale = match_known_benchmark("ClawEval")
    assert (canonical, category, scale) == ("ClawEval", "agentic", "percent")
    # every known benchmark now has exactly one category (no ambiguity to split)
    assert KNOWN_BENCHMARKS["claweval"][1] == "agentic"


# -- #3: manual named benchmark survives research; research doesn't clobber --------

def test_manual_named_benchmark_not_clobbered_by_research(tmp_path):
    reg = ModelRegistry(Database(tmp_path / "n.sqlite"))
    reg.upsert_named_benchmark("ornith:35b", "SWE-Bench Verified", "coding", 75.6,
                               "percent", source_url="https://ornith.online",
                               source="manual")
    # a later research pass reports a different number for the same benchmark
    reg.upsert_named_benchmark("ornith:35b", "SWE-Bench Verified", "coding", 40.0,
                               "percent", source="research")
    rows = reg.named_benchmarks("ornith:35b")
    assert len(rows) == 1
    assert rows[0]["score"] == 75.6 and rows[0]["source"] == "manual"   # kept


def test_research_overwrites_research(tmp_path):
    reg = ModelRegistry(Database(tmp_path / "n2.sqlite"))
    reg.upsert_named_benchmark("m", "MMLU", "reasoning", 70, "percent", source="research")
    reg.upsert_named_benchmark("m", "MMLU", "reasoning", 72, "percent", source="research")
    rows = reg.named_benchmarks("m")
    assert len(rows) == 1 and rows[0]["score"] == 72


def test_manual_named_benchmark_delete(tmp_path):
    reg = ModelRegistry(Database(tmp_path / "n3.sqlite"))
    reg.upsert_named_benchmark("m", "BFCL", "tool_calling", 77, "percent", source="manual")
    reg.delete_named_benchmark("m", "BFCL")
    assert reg.named_benchmarks("m") == []


def test_add_named_benchmark_endpoint(client):
    r = client.post("/admin/api/models/named_benchmark/add", json={
        "model_id": "ornith:35b", "benchmark_name": "Terminal-Bench",
        "category": "coding", "score": 64.2, "scale": "percent",
        "source_url": "https://ornith.online"})
    assert r.status_code == 200
    named = {n["benchmark_name"]: n for n in r.json()["named"]}
    assert named["Terminal-Bench"]["score"] == 64.2
    assert named["Terminal-Bench"]["source"] == "manual"
    # bad input rejected
    assert client.post("/admin/api/models/named_benchmark/add",
                       json={"model_id": "x"}).status_code == 400


# -- #4: retry ceiling raised (now on the MCP server config, generalized) ---------

def test_mcp_server_has_rate_limit_retry():
    from foundry_router.config import MCPServerConfig
    s = MCPServerConfig(name="searxng", url="http://x")
    assert s.rate_limit_retries >= 1        # 429-backoff available on every MCP server
    assert hasattr(s, "pace_seconds")
