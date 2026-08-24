"""Per-MCP-server on/off switch: a disabled server is kept configured but
skipped everywhere — no discovery (so Tool Sync drops its tools), and calls to
it are refused. Flipping it back on restores it."""

import pytest

from foundry_router.config import MCPServerConfig
from foundry_router.db import Database
from foundry_router.tools.mcp_client import MCPManager, MCPUnavailable


def _mgr(tmp_path, servers):
    return MCPManager(servers, Database(tmp_path / "d.sqlite"))


async def test_list_all_skips_disabled(tmp_path):
    servers = [MCPServerConfig(name="on1", url="http://a/mcp"),
               MCPServerConfig(name="off1", url="http://b/mcp", enabled=False)]
    mgr = _mgr(tmp_path, servers)

    async def fake_list(name):
        return [{"name": f"{name}_tool", "description": "", "input_schema": {},
                 "read_only": None, "destructive": None}]
    mgr.list_tools = fake_list

    out = await mgr.list_all()
    assert set(out) == {"on1"}                 # disabled server excluded


async def test_call_tool_on_disabled_raises(tmp_path):
    mgr = _mgr(tmp_path, [MCPServerConfig(name="off1", url="http://b/mcp",
                                          enabled=False)])
    with pytest.raises(MCPUnavailable):
        await mgr.call_tool("off1", "some_tool", {})


async def test_enabled_default_true(tmp_path):
    mgr = _mgr(tmp_path, [MCPServerConfig(name="plain", url="http://a/mcp")])
    assert mgr.servers["plain"].enabled is True


# -- toggle route round-trips through config (no network: disabling skips dial) ----

TOGGLE_CONFIG = """
server: {host: 127.0.0.1, port: 11435}
agent_brain: {provider: ollama, endpoint: "http://127.0.0.1:9", model: b}
backend_pool: {mode: internal, internal: {backends: []}}
guardrails: {authority: internal}
registry: {research: {enabled: false}}
mcp_servers:
  - {name: musicsvr, url: "http://127.0.0.1:9/mcp", transport: streamable-http}
"""


@pytest.fixture()
def toggle_client(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(TOGGLE_CONFIG, encoding="utf-8")
    from foundry_router.main import create_app
    from fastapi.testclient import TestClient
    app = create_app(config_path=cfg, database_path=tmp_path / "t.sqlite")
    with TestClient(app) as c:
        yield c


def test_toggle_route_disables_and_persists(toggle_client):
    # starts enabled
    listed = toggle_client.get("/admin/api/mcp_servers").json()["servers"]
    assert next(s for s in listed if s["name"] == "musicsvr")["enabled"] is True
    # disable it (sync runs, but a disabled server is never dialed)
    r = toggle_client.post("/admin/api/mcp_servers/toggle",
                           json={"name": "musicsvr", "enabled": False}).json()
    assert r["ok"] and r["enabled"] is False
    # reflected in the list + on the live manager
    listed = toggle_client.get("/admin/api/mcp_servers").json()["servers"]
    assert next(s for s in listed if s["name"] == "musicsvr")["enabled"] is False
    svc = toggle_client.app.state.services
    assert svc.mcp.servers["musicsvr"].enabled is False
