"""MCP-layer pacing + 429-backoff (generalized from research-agent-only). Every
caller of a shared rate-limited server — the research sweep, the worker-side
tool loop, and the brain's own tool calls — funnels through MCPManager.call_tool,
so the pacing gate and 429 retry now live there and cover all three (found live:
worker-side tool calling hammered SearXNG with 429s the research fix never
reached)."""

import time
from contextlib import asynccontextmanager

import pytest

from foundry_router.config import MCPServerConfig
from foundry_router.db import Database
from foundry_router.tools.mcp_client import MCPManager, _is_rate_limited


class FakeBlock:
    def __init__(self, text):
        self.text = text


class FakeResult:
    def __init__(self, text="", is_error=False):
        self.content = [FakeBlock(text)] if text else []
        self.isError = is_error


class FakeSession:
    def __init__(self, behavior):
        self.behavior = behavior           # behavior(call_n) -> FakeResult | Exception
        self.calls = 0
        self.at = []                       # monotonic ts of each call

    async def call_tool(self, tool, args):
        self.calls += 1
        self.at.append(time.monotonic())
        out = self.behavior(self.calls)
        if isinstance(out, Exception):
            raise out
        return out


def _mgr(tmp_path, cfg, behavior):
    db = Database(tmp_path / "m.sqlite")
    mgr = MCPManager([cfg], db)
    session = FakeSession(behavior)

    @asynccontextmanager
    async def fake_session(name):
        yield session

    mgr._session = fake_session
    return mgr, session, db


def _429():
    return RuntimeError("Client error '429 Too Many Requests' for url '.../mcp'")


# -- 429 detection ----------------------------------------------------------------

def test_is_rate_limited_detects_429():
    assert _is_rate_limited(_429())
    assert _is_rate_limited(ExceptionGroup("g", [_429()]))     # wrapped
    assert _is_rate_limited(RuntimeError("too many requests"))
    assert not _is_rate_limited(RuntimeError("connection refused"))


# -- 429 backoff + retry (covers worker/brain/research uniformly) ------------------

async def test_429_backs_off_then_succeeds(tmp_path):
    cfg = MCPServerConfig(name="searxng", url="http://x",
                          rate_limit_retries=3, rate_limit_backoff_seconds=0)
    mgr, session, db = _mgr(
        tmp_path, cfg, lambda n: _429() if n == 1 else FakeResult("results"))
    assert await mgr.call_tool("searxng", "web_search", {}) == "results"
    assert session.calls == 2                                  # retried once
    warns = db.query("SELECT * FROM event_log WHERE source='mcp' "
                     "AND message LIKE '%rate-limited%'")
    assert len(warns) == 1


async def test_429_gives_up_after_retries(tmp_path):
    cfg = MCPServerConfig(name="s", url="http://x",
                          rate_limit_retries=2, rate_limit_backoff_seconds=0)
    mgr, session, _ = _mgr(tmp_path, cfg, lambda n: _429())
    with pytest.raises(Exception):
        await mgr.call_tool("s", "t", {})
    assert session.calls == 2                                  # exactly the ceiling


async def test_non_429_is_not_retried(tmp_path):
    cfg = MCPServerConfig(name="s", url="http://x", rate_limit_retries=5,
                          rate_limit_backoff_seconds=0)
    mgr, session, _ = _mgr(tmp_path, cfg, lambda n: RuntimeError("connection refused"))
    with pytest.raises(RuntimeError):
        await mgr.call_tool("s", "t", {})
    assert session.calls == 1                                  # no retry on a non-429


# -- pacing -----------------------------------------------------------------------

async def test_calls_to_a_server_are_paced(tmp_path):
    cfg = MCPServerConfig(name="s", url="http://x", pace_seconds=0.15)
    mgr, session, _ = _mgr(tmp_path, cfg, lambda n: FakeResult("ok"))
    await mgr.call_tool("s", "t", {})       # first: no wait
    await mgr.call_tool("s", "t", {})       # second: waits ~pace
    assert session.at[1] - session.at[0] >= 0.14


async def test_pace_zero_disables(tmp_path):
    cfg = MCPServerConfig(name="s", url="http://x", pace_seconds=0)
    mgr, session, _ = _mgr(tmp_path, cfg, lambda n: FakeResult("ok"))
    await mgr.call_tool("s", "t", {})
    await mgr.call_tool("s", "t", {})
    assert session.at[1] - session.at[0] < 0.1
