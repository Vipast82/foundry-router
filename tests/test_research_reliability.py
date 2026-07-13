"""Research pass reliability: the brain call retries a transient timeout
instead of failing the whole pass (item 1: ornith:35b needed manual requeuing
after a cold-load ReadTimeout), and search/TaskGroup failures are unwrapped via
describe_exception instead of the useless 'unhandled errors in a TaskGroup'
(item 2)."""

import pytest

import foundry_router.registry.research_agent as ra
from foundry_router.config import ResearchConfig, ResearchToolRef
from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.registry.research_agent import ResearchAgent


def _reg(tmp_path):
    db = Database(tmp_path / "r.sqlite")
    return db, ModelRegistry(db)


# -- item 1: brain-call retry -----------------------------------------------------

async def test_extraction_retries_transient_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "LLM_RETRY_BACKOFF_SECONDS", 0)   # no real sleeping
    db, registry = _reg(tmp_path)
    calls = {"n": 0}

    async def flaky(prompt):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("brain cold-loading")   # the live failure shape
        return "recovered"

    agent = ResearchAgent(ResearchConfig(), db, registry, None,
                          llm=flaky, available_models=lambda: [])
    assert await agent._extract_with_retry("p") == "recovered"
    assert calls["n"] == 3                              # succeeded on 3rd try
    warns = db.query("SELECT * FROM event_log WHERE source='research' "
                     "AND level='warning'")
    assert len(warns) == 2                              # two failures logged


async def test_extraction_raises_after_exhausting(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "LLM_RETRY_BACKOFF_SECONDS", 0)
    db, registry = _reg(tmp_path)

    async def always_fail(prompt):
        raise RuntimeError("permanent")

    agent = ResearchAgent(ResearchConfig(), db, registry, None,
                          llm=always_fail, available_models=lambda: [])
    with pytest.raises(RuntimeError):
        await agent._extract_with_retry("p")
    warns = db.query("SELECT * FROM event_log WHERE source='research' "
                     "AND level='warning'")
    assert len(warns) == ra.LLM_RETRY_ATTEMPTS          # every attempt logged


# -- item 2: describe_exception unwrapping ----------------------------------------

async def test_search_taskgroup_error_is_unwrapped(tmp_path):
    db, registry = _reg(tmp_path)

    class GroupMCP:
        async def call_tool(self, server, tool, args):
            # what searxng dropping mid-sweep actually raises up the stack
            raise ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [RuntimeError("searxng BrokenResourceError")])

    cfg = ResearchConfig(search=ResearchToolRef(server="searxng", tool="web_search"))

    async def _llm(p):
        return ""

    agent = ResearchAgent(cfg, db, registry, GroupMCP(),
                          llm=_llm, available_models=lambda: [])
    await agent.research_model("m")
    note = registry.get("m")["research_note"]
    # the real cause survives; the useless wrapper phrasing does not
    assert "RuntimeError" in note and "BrokenResourceError" in note
    assert "TaskGroup" not in note
