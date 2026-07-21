"""Eval harness (quality spec Phase 4): shape-based checks (never exact
match), seeded editable prompt set, runs through the persona's real routing
path with optional LLM-as-judge, and per-persona score trends with deltas."""

import json

from foundry_router.evalharness import (EvalHarness, default_categories,
                                        ensure_seed, run_shape_checks)
from foundry_router.db import Database


# -- shape checks -----------------------------------------------------------------

def test_shape_checks_code_and_refusal():
    ok, detail = run_shape_checks(["code_block", "no_refusal"],
                                  "Here you go:\n```python\nprint(1)\n```")
    assert ok and "✓ code_block" in detail
    ok, detail = run_shape_checks(["no_refusal"], "I'm sorry, but I can't help.")
    assert not ok and "✗ no_refusal" in detail
    ok, _ = run_shape_checks(["code_block"], "no code here")
    assert not ok


def test_shape_checks_citation_json_length_mentions():
    assert run_shape_checks(["cites_source"],
                            "Latest is 3.13, see https://python.org")[0]
    assert not run_shape_checks(["cites_source"], "Latest is 3.13")[0]
    assert run_shape_checks(["valid_json"], 'Sure: {"mission": "Apollo 11", "year": 1969}')[0]
    assert not run_shape_checks(["valid_json"], "no json")[0]
    assert run_shape_checks(["min_length:10", "max_length:100"], "x" * 50)[0]
    assert not run_shape_checks(["min_length:100"], "short")[0]
    assert run_shape_checks(["mentions:1912", "mentions:Sheffield"],
                            "It opened in 1912 in sheffield.")[0]
    assert not run_shape_checks(["mentions:1912"], "It opened long ago.")[0]


def test_unknown_check_is_skipped_not_failed():
    ok, detail = run_shape_checks(["definitely_not_a_check", "no_refusal"],
                                  "A fine answer.")
    assert ok                                  # typo can't brick a run
    assert "unknown check, skipped" in detail


def test_empty_response_fails_no_refusal():
    assert not run_shape_checks(["no_refusal"], "")[0]


# -- seeding + category mapping ---------------------------------------------------

def test_seed_is_idempotent_and_editable(tmp_path):
    db = Database(tmp_path / "e.sqlite")
    ensure_seed(db)
    n = db.query_one("SELECT COUNT(*) AS n FROM eval_prompts")["n"]
    assert n >= 8
    # operator edits survive re-seeding
    db.execute("UPDATE eval_prompts SET prompt='edited' WHERE id=1")
    db.execute("DELETE FROM eval_prompts WHERE id=2")
    ensure_seed(db)
    assert db.query_one("SELECT COUNT(*) AS n FROM eval_prompts")["n"] == n - 1
    assert db.query_one("SELECT prompt FROM eval_prompts WHERE id=1")["prompt"] == "edited"


def test_default_categories_mapping():
    assert default_categories({"virtual_name": "Foundry-Coding",
                               "benchmark_category": "coding"}) == ["coding"]
    assert default_categories({"virtual_name": "Foundry-Research",
                               "benchmark_category": "agentic"}) == ["research"]
    assert default_categories({"virtual_name": "Foundry-Chat",
                               "benchmark_category": "general_chat"}) == ["chat"]
    # RAG shares general_chat, so the name wins
    assert default_categories({"virtual_name": "Foundry-RAG",
                               "benchmark_category": "general_chat"}) == ["rag"]


# -- runs (injected answers — scoring logic under test, not routing) ---------------

GOOD_CHAT = ("RAM is your computer's short-term working memory. " * 10
             + 'Also: {"mission": "Apollo 11", "year": 1969}')


async def _canned(persona, prompt):
    return GOOD_CHAT


async def _empty(persona, prompt):
    return ""


async def test_run_scores_and_stores_results(app):
    svc = app.state.services
    harness = EvalHarness(svc, answer_fn=_canned)
    run_id = await harness.run("Foundry-Chat")           # judge: none
    row = svc.db.query_one("SELECT * FROM eval_runs WHERE id=?", (run_id,))
    assert row["status"] == "done"
    assert row["persona"] == "Foundry-Chat"
    assert row["judge_model"] == ""
    assert row["prompts_run"] >= 3
    assert row["avg_judge_score"] is None                # shape-only run
    results = harness.results(run_id)
    assert len(results) == row["prompts_run"]
    assert all(r["shape_detail"] for r in results)
    # the canned answer passes chat checks (long, JSON present, no refusal)
    assert row["shape_pass_rate"] > 0


async def test_run_trend_deltas(app):
    svc = app.state.services
    good = EvalHarness(svc, answer_fn=_canned)
    bad = EvalHarness(svc, answer_fn=_empty)
    await bad.run("Foundry-Chat")
    await good.run("Foundry-Chat")
    runs = good.runs("Foundry-Chat")
    assert len(runs) == 2
    newest = runs[0]                                     # newest first
    assert newest["shape_delta"] is not None and newest["shape_delta"] > 0


async def test_run_unknown_persona_raises(app):
    import pytest
    harness = EvalHarness(app.state.services, answer_fn=_canned)
    with pytest.raises(ValueError):
        await harness.run("Ghost-Persona")


# -- endpoints --------------------------------------------------------------------

def test_eval_endpoints_and_sync_run(client, app):
    prompts = client.get("/admin/api/eval/prompts").json()["prompts"]
    assert len(prompts) >= 8                             # seeded at startup
    # prompt CRUD
    r = client.post("/admin/api/eval/prompts", json={
        "category": "chat", "prompt": "Name three primary colors.",
        "checks": ["no_refusal", "mentions:red"]})
    assert r.status_code == 200
    assert client.post("/admin/api/eval/prompts", json={"prompt": ""}).status_code == 400
    # unknown persona 404s
    assert client.post("/admin/api/eval/run",
                       json={"persona": "Ghost"}).status_code == 404
    # synchronous run produces a score report even in this degraded test env
    # (brain unreachable, no backends): shape checks all fail, run completes
    app.state.services.evals._answer_fn = _empty
    d = client.post("/admin/api/eval/run",
                    json={"persona": "Foundry-Chat", "wait": True}).json()
    assert d["run"]["status"] == "done"
    assert d["run"]["shape_pass_rate"] == 0
    assert len(d["results"]) == d["run"]["prompts_run"]
    listed = client.get("/admin/api/eval/runs?persona=Foundry-Chat").json()["runs"]
    assert listed and listed[0]["id"] == d["run"]["id"]
