"""Foundry-MCP aggregator: it re-exposes Foundry's connected MCP tools as one
Streamable-HTTP MCP server, scoped by profile, so an MCP client (AnythingLLM)
gains all of Foundry's tools alongside its own. These cover the exposure/scope
logic and that the endpoints mount + enforce the shared secret."""

import types

import pytest

from foundry_router.config import MCPAggregatorConfig
from foundry_router.facade.mcp_aggregator import MCPAggregator
from foundry_router.tools.sync import ToolDef


def _tool(name, server, mcp_tool, disabled=False, kind="mcp"):
    return ToolDef(name=name, kind=kind, description=f"{name} desc",
                   parameters={"type": "object", "properties": {}},
                   server=server, mcp_tool=mcp_tool, disabled=disabled)


class _FakeReg:
    def __init__(self, tools):
        self._t = {t.name: t for t in tools}

    def enabled(self):
        return list(self._t.values())

    def get(self, name):
        return self._t.get(name)


class _FakeMCP:
    def __init__(self):
        self.calls = []

    async def call_tool(self, server, tool, args):
        self.calls.append((server, tool, args))
        return f"ran {server}/{tool}"


def _agg(tools, server=None, **cfg):
    server = server or types.SimpleNamespace(host="127.0.0.1", port=11435)
    svc = types.SimpleNamespace(
        tool_registry=_FakeReg(tools),
        mcp=_FakeMCP(),
        config_store=types.SimpleNamespace(
            config=types.SimpleNamespace(mcp_aggregator=MCPAggregatorConfig(**cfg),
                                         server=server)),
        db=types.SimpleNamespace(log_event=lambda *a, **k: None),
    )
    return MCPAggregator(svc), svc


# -- tool exposure / scoping -------------------------------------------------------

def test_visible_tools_all_mcp():
    tools = [_tool("acestep-music__generate", "acestep-music", "generate"),
             _tool("kokoro-tts__say", "kokoro-tts", "say")]
    agg, _ = _agg(tools)
    names = {t.name for t in agg._visible_tools(None)}
    assert names == {"acestep-music__generate", "kokoro-tts__say"}


def test_visible_tools_excludes_model_and_disabled():
    tools = [_tool("acestep-music__generate", "acestep-music", "generate"),
             _tool("kokoro-tts__say", "kokoro-tts", "say", disabled=True),
             _tool("ask_qwen", None, None, kind="model")]
    agg, _ = _agg(tools)
    names = {t.name for t in agg._visible_tools(None)}
    assert names == {"acestep-music__generate"}


def test_visible_tools_scoped_to_profile():
    tools = [_tool("acestep-music__generate", "acestep-music", "generate"),
             _tool("kokoro-tts__say", "kokoro-tts", "say")]
    agg, _ = _agg(tools)
    names = {t.name for t in agg._visible_tools({"acestep-music"})}
    assert names == {"acestep-music__generate"}


# -- dispatch: resolve namespaced id -> Foundry MCP client -------------------------

async def test_dispatch_routes_to_original_tool():
    tools = [_tool("acestep-music__generate", "acestep-music", "generate")]
    agg, svc = _agg(tools)
    out = await agg._dispatch("acestep-music__generate", {"prompt": "shanty"}, None)
    assert out == "ran acestep-music/generate"
    assert svc.mcp.calls == [("acestep-music", "generate", {"prompt": "shanty"})]


async def test_dispatch_unknown_tool_raises():
    agg, _ = _agg([])
    with pytest.raises(ValueError):
        await agg._dispatch("nope__x", {}, None)


async def test_poll_guard_stops_identical_repeat():
    tools = [_tool("acestep-music__check_music_job", "acestep-music", "check")]
    agg, svc = _agg(tools, poll_guard_threshold=3)
    args = {"job_id": "abc"}
    # first 3 identical calls execute for real
    for _ in range(3):
        out = await agg._dispatch("acestep-music__check_music_job", args, None)
        assert out == "ran acestep-music/check"
    assert len(svc.mcp.calls) == 3
    # the 4th is short-circuited by the guard — NOT executed
    out = await agg._dispatch("acestep-music__check_music_job", args, None)
    assert "POLL GUARD" in out and "STOP polling" in out
    assert len(svc.mcp.calls) == 3                       # still 3 — no new server call


async def test_poll_guard_distinct_args_not_blocked():
    tools = [_tool("acestep-music__check_music_job", "acestep-music", "check")]
    agg, svc = _agg(tools, poll_guard_threshold=2)
    for i in range(4):
        out = await agg._dispatch("acestep-music__check_music_job",
                                  {"job_id": f"job{i}"}, None)   # different args each time
        assert out == "ran acestep-music/check"
    assert len(svc.mcp.calls) == 4                       # all executed, none guarded


async def test_poll_guard_off_when_zero():
    tools = [_tool("acestep-music__check_music_job", "acestep-music", "check")]
    agg, svc = _agg(tools, poll_guard_threshold=0)
    for _ in range(6):
        await agg._dispatch("acestep-music__check_music_job", {"job_id": "abc"}, None)
    assert len(svc.mcp.calls) == 6                       # guard disabled


async def test_dispatch_scope_blocks_out_of_profile_tool():
    tools = [_tool("kokoro-tts__say", "kokoro-tts", "say")]
    agg, _ = _agg(tools)
    # tool exists but the profile only exposes acestep-music
    with pytest.raises(ValueError):
        await agg._dispatch("kokoro-tts__say", {}, {"acestep-music"})


def test_describe_builds_client_configs():
    agg, _ = _agg([], enabled=True, token="secret",
                  advertise_url="http://192.168.1.50:8080",
                  profiles={"music": ["acestep-music"]})
    d = agg.describe()
    assert d["base_url"] == "http://192.168.1.50:8080"
    # planned endpoints come from config (all + each profile), pre-restart
    paths = {e["path"] for e in d["planned_endpoints"]}
    assert paths == {"/mcp", "/mcp/p/music"}
    allm = d["anythingllm_config"]["mcpServers"]
    assert allm["foundry"]["type"] == "streamable"
    assert allm["foundry"]["url"] == "http://192.168.1.50:8080/mcp/"
    assert allm["foundry-music"]["url"] == "http://192.168.1.50:8080/mcp/p/music/"
    assert allm["foundry"]["headers"] == {"X-API-KEY": "secret"}
    cln = d["cline_config"]["mcpServers"]
    assert cln["foundry"]["type"] == "streamableHttp"
    assert cln["foundry"]["url"] == "http://192.168.1.50:8080/mcp/"
    assert cln["foundry"]["autoApprove"] == []
    assert cln["foundry"]["headers"] == {"X-API-KEY": "secret"}


def test_describe_base_url_falls_back_to_server():
    agg, _ = _agg([], enabled=True,
                  server=types.SimpleNamespace(host="0.0.0.0", port=8080))
    d = agg.describe()
    assert d["base_url"] == "http://HOST:8080"      # 0.0.0.0 -> HOST placeholder


# -- mount + auth through the real app ---------------------------------------------

AGG_CONFIG = """
server: {host: 127.0.0.1, port: 11435}
agent_brain:
  provider: ollama
  endpoint: "http://127.0.0.1:9"
  model: "test-brain"
backend_pool:
  mode: internal
  internal: {backends: []}
guardrails: {authority: internal}
registry: {research: {enabled: false}}
mcp_servers: []
mcp_aggregator:
  enabled: true
  token: "s3cret"
  token_header: "X-API-KEY"
  profiles:
    music: ["acestep-music"]
"""


@pytest.fixture()
def agg_client(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(AGG_CONFIG, encoding="utf-8")
    from foundry_router.main import create_app
    from fastapi.testclient import TestClient
    app = create_app(config_path=cfg_path, database_path=tmp_path / "t.sqlite")
    with TestClient(app) as c:
        yield c


def test_endpoints_mounted_and_auth_enforced(agg_client):
    # Wrong/absent token -> 401 (proves the guard runs and the route exists).
    r = agg_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                        headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    r2 = agg_client.post("/mcp/p/music",
                         json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                         headers={"Accept": "application/json, text/event-stream",
                                  "X-API-KEY": "wrong"})
    assert r2.status_code == 401


def test_describe_reports_mounted_endpoints(agg_client):
    svc = agg_client.app.state.services
    d = svc.mcp_aggregator.describe()
    paths = {e["path"] for e in d["endpoints"]}
    assert "/mcp" in paths and "/mcp/p/music" in paths
