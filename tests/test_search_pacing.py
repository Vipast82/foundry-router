"""Search-call pacing (research sweep bursts 20+ searches, SearXNG fans each to
external engines that 429 the burst — not a fixable local limit, so pace the
call pattern). A global minimum gap between search calls, plus a longer
escalating backoff specifically on 429 (retrying straight into an active rate
limit just 429s again)."""

import time

import pytest

from foundry_router.config import ResearchConfig, ResearchToolRef
from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.registry.research_agent import ResearchAgent, _is_rate_limited


def _agent(tmp_path, mcp, **cfg_kwargs):
    db = Database(tmp_path / "r.sqlite")
    cfg = ResearchConfig(search=ResearchToolRef(server="searxng", tool="web_search"),
                         **cfg_kwargs)

    async def _llm(p):
        return ""

    agent = ResearchAgent(cfg, db, ModelRegistry(db), mcp,
                          llm=_llm, available_models=lambda: [])
    return agent, db


class RecordingMCP:
    def __init__(self, behavior=None):
        self.times: list[float] = []
        self.behavior = behavior or (lambda n: "ok")   # n = 1-indexed call number

    async def call_tool(self, server, tool, args):
        self.times.append(time.monotonic())
        out = self.behavior(len(self.times))
        if isinstance(out, Exception):
            raise out
        return out


def _http_429():
    return RuntimeError("Client error '429 Too Many Requests' for url '.../mcp'")


# -- 429 detection ----------------------------------------------------------------

def test_is_rate_limited_detects_429():
    assert _is_rate_limited(_http_429())
    assert _is_rate_limited(ExceptionGroup("g", [_http_429()]))     # wrapped
    assert _is_rate_limited(RuntimeError("too many requests"))
    assert not _is_rate_limited(RuntimeError("connection refused"))


# -- pacing -----------------------------------------------------------------------

async def test_successive_searches_are_paced(tmp_path):
    mcp = RecordingMCP()
    agent, _ = _agent(tmp_path, mcp, search_pace_seconds=0.15)
    await agent._search("q1")          # first call: no wait (last_ts=0 vs now)
    await agent._search("q2")
    await agent._search("q3")
    gaps = [mcp.times[i] - mcp.times[i - 1] for i in range(1, len(mcp.times))]
    assert all(g >= 0.14 for g in gaps)   # each spaced by ~pace


async def test_pace_zero_disables(tmp_path):
    mcp = RecordingMCP()
    agent, _ = _agent(tmp_path, mcp, search_pace_seconds=0)
    await agent._search("q1")
    await agent._search("q2")
    assert mcp.times[1] - mcp.times[0] < 0.1   # effectively no delay


# -- 429 backoff + retry ----------------------------------------------------------

async def test_429_backs_off_then_succeeds(tmp_path):
    # first call 429s, second succeeds; backoff set to 0 so the test is fast
    mcp = RecordingMCP(behavior=lambda n: _http_429() if n == 1 else "results")
    agent, db = _agent(tmp_path, mcp, search_pace_seconds=0,
                       search_429_backoff_seconds=0, search_retry_attempts=3)
    assert await agent._search("q") == "results"
    assert len(mcp.times) == 2                  # retried once
    warns = db.query("SELECT * FROM event_log WHERE source='research' "
                     "AND message LIKE '%rate-limited%'")
    assert len(warns) == 1


async def test_429_gives_up_after_attempts(tmp_path):
    mcp = RecordingMCP(behavior=lambda n: _http_429())   # always 429
    agent, _ = _agent(tmp_path, mcp, search_pace_seconds=0,
                      search_429_backoff_seconds=0, search_retry_attempts=2)
    with pytest.raises(Exception):
        await agent._search("q")
    assert len(mcp.times) == 2                  # exactly search_retry_attempts


async def test_non_429_error_is_not_retried(tmp_path):
    # a plain failure propagates immediately — no wasted retries into a
    # non-rate-limit error
    mcp = RecordingMCP(behavior=lambda n: RuntimeError("connection refused"))
    agent, _ = _agent(tmp_path, mcp, search_pace_seconds=0, search_retry_attempts=3)
    with pytest.raises(RuntimeError):
        await agent._search("q")
    assert len(mcp.times) == 1                  # no retry on a non-429 failure
