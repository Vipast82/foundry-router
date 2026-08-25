"""Long media jobs: the MCP client transport's sse_read_timeout must follow the
server's per-call budget, not the SDK's 300s default — otherwise a 9-minute
music render's result never arrives ('Connection closed') even with a high
per-server timeout_seconds."""

import contextlib

import pytest

from foundry_router.config import MCPServerConfig
from foundry_router.db import Database
from foundry_router.tools.mcp_client import MCPManager


class _FakeSession:
    def __init__(self, read, write):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def initialize(self):
        pass


@pytest.fixture()
def capture(monkeypatch):
    cap = {}

    @contextlib.asynccontextmanager
    async def fake_transport(url, **kwargs):
        cap["url"] = url
        cap.update(kwargs)
        yield (object(), object())

    monkeypatch.setattr("mcp.client.sse.sse_client", fake_transport, raising=True)
    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client",
                        fake_transport, raising=True)
    monkeypatch.setattr("mcp.ClientSession", _FakeSession, raising=True)
    return cap


async def test_sse_read_timeout_follows_server_budget(tmp_path, capture):
    mgr = MCPManager([MCPServerConfig(name="acestep", url="http://x/sse",
                                      transport="sse", timeout_seconds=1800)],
                     Database(tmp_path / "d.sqlite"))
    async with mgr._session("acestep"):
        pass
    assert capture["sse_read_timeout"] == 1800.0     # not the SDK's 300s default
    assert capture["timeout"] == 30.0                # connect timeout stays short


async def test_streamable_also_gets_read_timeout(tmp_path, capture):
    mgr = MCPManager([MCPServerConfig(name="gw", url="http://x/mcp",
                                      transport="streamable-http",
                                      timeout_seconds=900)],
                     Database(tmp_path / "d.sqlite"))
    async with mgr._session("gw"):
        pass
    assert capture["sse_read_timeout"] == 900.0


async def test_short_budget_caps_connect_timeout(tmp_path, capture):
    mgr = MCPManager([MCPServerConfig(name="fast", url="http://x/sse",
                                      transport="sse", timeout_seconds=10)],
                     Database(tmp_path / "d.sqlite"))
    async with mgr._session("fast"):
        pass
    assert capture["sse_read_timeout"] == 10.0
    assert capture["timeout"] == 10.0                # min(30, 10)
