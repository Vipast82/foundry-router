"""Find-on-demand MCP tools (v0.181.109): a persona with mcp_tool_mode "finder" is shown
foundry_find_tools / foundry_call_tool instead of every granted tool; both only reach the
persona's grants ("*" = every server, "!name" = excluded)."""

import json
import types

import pytest

from foundry_router import tool_finder as TF
from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.mcp_client import MCPManager, ToolResult
from foundry_router.tools.sync import ToolDef, ToolRegistry

SERVERS = {
    "searxng": [("searxng_web_search", "Search the web with SearXNG and return results.")],
    "crawl4ai": [("crawl", "Fetch a web page and return its markdown."),
                 ("scrape", "Scrape structured data from a page.")],
    "comfy-mcp": [("generate_image", "Generate an image from a text prompt.")],
    "sandbox": [("run_python", "Execute Python code in a sandbox.")],
}


def _registry(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    reg = ToolRegistry(db, ModelRegistry(db), MCPManager([], db))
    for server, tools in SERVERS.items():
        for name, desc in tools:
            reg.tools[f"{server}__{name}"] = ToolDef(
                name=f"{server}__{name}", kind="mcp", description=desc,
                parameters={"type": "object", "properties": {"q": {"type": "string"}}},
                server=server, mcp_tool=name)
    return reg


def _persona(grants, mode=None):
    return {"virtual_name": "Foundry-Chat", "preferred_mcp_tools": json.dumps(grants),
            "mcp_tool_mode": mode}


def _servers(tools):
    return {t.server for t in tools}


def test_every_server_grant_with_exclusions(tmp_path):
    reg = _registry(tmp_path)
    assert _servers(reg.mcp_tools_for_persona(_persona(["*"]))) == set(SERVERS)
    got = reg.mcp_tools_for_persona(_persona(["*", "!sandbox", "!scrape"]))
    assert _servers(got) == {"searxng", "crawl4ai", "comfy-mcp"}
    assert "scrape" not in {t.mcp_tool for t in got} and "crawl" in {t.mcp_tool for t in got}
    # an exclusion also narrows a plain grant
    assert {t.mcp_tool for t in reg.mcp_tools_for_persona(_persona(["crawl4ai", "!crawl"]))} \
        == {"scrape"}


def test_finder_mode_offers_two_tools_and_full_mode_every_grant(tmp_path):
    reg = _registry(tmp_path)
    finder = reg.offered_mcp_tools(_persona(["*"], "finder"))
    assert [t.name for t in finder] == [TF.FIND_TOOL, TF.CALL_TOOL]
    assert len(reg.offered_mcp_tools(_persona(["*"]))) == 5
    assert reg.offered_mcp_tools(_persona([], "finder")) == []     # nothing granted: nothing
    specs = [s["function"]["name"] for s in reg.specs_for_persona(_persona(["*"], "finder"))]
    assert TF.FIND_TOOL in specs and "searxng__searxng_web_search" not in specs
    assert reg.get(TF.CALL_TOOL).server == TF.FINDER_SERVER        # the brain resolves it


def test_search_ranks_name_then_server_then_description(tmp_path):
    tools = _registry(tmp_path).mcp_tools_for_persona(_persona(["*"]))
    assert TF.search(tools, "web search")[0].mcp_tool == "searxng_web_search"
    assert TF.search(tools, "generate an image")[0].mcp_tool == "generate_image"
    assert TF.search(tools, "fetch a page")[0].mcp_tool == "crawl"
    assert TF.search(tools, "the a to") == []


@pytest.mark.asyncio
async def test_find_returns_schemas_and_call_runs_only_granted_tools(tmp_path):
    reg = _registry(tmp_path)
    granted = reg.mcp_tools_for_persona(_persona(["*", "!sandbox"]))
    ran = []

    async def call(td, args):
        ran.append((td.server, td.mcp_tool, args))
        return ToolResult([{"type": "text", "text": "ok"}])

    found = json.loads((await TF.run(granted, TF.FIND_TOOL, {"query": "web search"}, call)).text)
    first = found["tools"][0]
    assert first["name"] == "searxng__searxng_web_search" and "properties" in first["arguments"]
    res = await TF.run(granted, TF.CALL_TOOL,
                       {"name": first["name"], "arguments": {"q": "foundry"}}, call)
    assert res.text == "ok" and ran == [("searxng", "searxng_web_search", {"q": "foundry"})]
    # the bare tool name works too, and string arguments are parsed
    await TF.run(granted, TF.CALL_TOOL, {"name": "crawl", "arguments": '{"q": "x"}'}, call)
    assert ran[-1] == ("crawl4ai", "crawl", {"q": "x"})
    # an excluded tool is refused, even though it exists
    res = await TF.run(granted, TF.CALL_TOOL, {"name": "sandbox__run_python",
                                               "arguments": {}}, call)
    assert res.text.startswith("ERROR") and len(ran) == 2
    # nothing matched: the servers it can use
    text = (await TF.run(granted, TF.FIND_TOOL, {"query": "zzz"}, call)).text
    assert "searxng" in text and "sandbox" not in text


@pytest.mark.asyncio
async def test_call_tool_rich_routes_finder_calls_through_the_persona_gate(tmp_path):
    from foundry_router.brain.agent import call_tool_rich
    reg = _registry(tmp_path)
    persona = _persona(["searxng"], "finder")
    calls = []

    class FakeMCP:
        async def call_tool_rich(self, server, tool, args, **kw):
            calls.append((server, tool, args))
            return ToolResult([{"type": "text", "text": f"ran {server}/{tool}"}])

    mcp = FakeMCP()
    mcp.foundry_svc = types.SimpleNamespace(
        personas=types.SimpleNamespace(get=lambda n: persona if n == "Foundry-Chat" else None),
        tool_registry=reg)
    res = await call_tool_rich(mcp, TF.FINDER_SERVER, TF.CALL_TOOL,
                               {"name": "searxng__searxng_web_search",
                                "arguments": {"q": "a"}}, persona="Foundry-Chat")
    assert res.text == "ran searxng/searxng_web_search"
    res = await call_tool_rich(mcp, TF.FINDER_SERVER, TF.CALL_TOOL,
                               {"name": "crawl4ai__crawl", "arguments": {}},
                               persona="Foundry-Chat")
    assert res.text.startswith("ERROR") and len(calls) == 1          # not granted


def test_persona_rules_accept_the_mode_and_every_server():
    from foundry_router import persona_rules as PR
    assert PR.ENUMS["mcp_tool_mode"] == (None, "finder")
    assert PR.ALIASES["mcp_tool_mode"]["full"] is None


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
"""


def test_the_persona_endpoint_serves_the_finder_pair(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(AGG_CONFIG, encoding="utf-8")
    from fastapi.testclient import TestClient
    from foundry_router.main import create_app
    app = create_app(config_path=cfg_path, database_path=tmp_path / "t.sqlite")
    with TestClient(app) as c:
        svc = c.app.state.services
        for server, tools in SERVERS.items():
            for name, desc in tools:
                svc.tool_registry.tools[f"{server}__{name}"] = ToolDef(
                    name=f"{server}__{name}", kind="mcp", description=desc,
                    parameters={"type": "object", "properties": {}}, server=server,
                    mcp_tool=name)
        svc.personas.upsert("Finder-Chat", preferred_mcp_tools=["*", "!sandbox"],
                            mcp_tool_mode="finder", enabled=1)
        ran = []

        async def fake_rich(server, tool, args, **kw):
            ran.append((server, tool))
            return ToolResult([{"type": "text", "text": f"ran {server}/{tool}"}])
        svc.mcp.call_tool_rich = fake_rich
        h = {"Accept": "application/json, text/event-stream", "X-API-KEY": "s3cret"}
        url = "/mcp/persona/Finder-Chat/"
        r = c.post(url, headers=h, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}})
        assert r.status_code == 200, r.text
        r = c.post(url, headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert [t["name"] for t in r.json()["result"]["tools"]] == [TF.FIND_TOOL, TF.CALL_TOOL]
        r = c.post(url, headers=h, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": TF.CALL_TOOL,
                       "arguments": {"name": "crawl4ai__crawl", "arguments": {}}}})
        assert "ran crawl4ai/crawl" in json.dumps(r.json()), r.text
        r = c.post(url, headers=h, json={
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": TF.CALL_TOOL,
                       "arguments": {"name": "sandbox__run_python", "arguments": {}}}})
        assert "ERROR" in json.dumps(r.json()) and ran == [("crawl4ai", "crawl")]


@pytest.mark.asyncio
async def test_loose_call_shapes_from_small_models_are_accepted(tmp_path):
    """v0.181.110: the shapes a local model gets wrong cost no turn."""
    granted = _registry(tmp_path).mcp_tools_for_persona(_persona(["*", "!sandbox"]))
    ran = []

    async def call(td, args):
        ran.append((td.mcp_tool, args))
        return ToolResult([{"type": "text", "text": "ok"}])

    async def go(args):
        return (await TF.run(granted, TF.CALL_TOOL, args, call)).text

    assert await go({"tool": "crawl", "args": {"q": 1}}) == "ok"             # aliases
    assert await go({"name": "crawl", "q": 2}) == "ok"                       # args beside name
    assert await go({"name": "Crawl4ai/Crawl", "arguments": '```json\n{"q": 3}\n```'}) == "ok"
    assert await go({"name": "searxng.searxng_web_search", "parameters": None}) == "ok"
    assert ran == [("crawl", {"q": 1}), ("crawl", {"q": 2}), ("crawl", {"q": 3}),
                   ("searxng_web_search", {})]
    bad = await go({"name": "web_search", "arguments": {}})                  # no such tool
    assert bad.startswith("ERROR") and "searxng__searxng_web_search" in bad  # closest tools
    assert (await go({"name": "sandbox__run_python"})).startswith("ERROR")   # still gated
    assert (await go({"name": "crawl", "arguments": "not json"})).startswith("ERROR")
    found = await TF.run(granted, TF.FIND_TOOL, {"q": "generate image"}, call)
    assert "generate_image" in found.text                                     # query alias
    assert len(ran) == 4


@pytest.mark.asyncio
async def test_the_brain_routes_a_direct_tool_call_through_the_finder_gate(tmp_path):
    """A finder persona's brain that calls a granted tool by name (skipping
    foundry_call_tool) still passes the grant check; an ungranted one is refused."""
    from foundry_router.brain import agent as A
    seen = []

    async def fake_rich(mcp, server, tool, args, **kw):
        seen.append((server, tool, args))
        return ToolResult([{"type": "text", "text": f"via {server}/{tool}"}])

    reg = _registry(tmp_path)
    ag = A.AgentRunner.__new__(A.AgentRunner)
    ag.tool_registry = reg

    async def hb(coro, *a, **k):
        return await coro
    ag._with_heartbeat = hb
    ag.brain = types.SimpleNamespace(cfg=types.SimpleNamespace(mcp_result_limit_chars=4000))
    import foundry_router.brain.agent as mod
    orig = mod.call_tool_rich
    mod.call_tool_rich = fake_rich
    try:
        ctx = types.SimpleNamespace(persona=_persona(["searxng"], "finder"), media=[],
                                    logger=types.SimpleNamespace(
                                        record_tool_call=lambda *a, **k: None))
        out, _ = await ag._execute_tool("searxng__searxng_web_search", {"q": "x"}, ctx,
                                        lambda *a, **k: None, {"tool_trail": []}, {})
    finally:
        mod.call_tool_rich = orig
    assert seen and seen[0][0] == TF.FINDER_SERVER and seen[0][1] == TF.CALL_TOOL
    assert seen[0][2] == {"name": "searxng__searxng_web_search", "arguments": {"q": "x"}}
