"""Per-tool granularity for a persona's preferred MCP servers (per-tool-grant
spec). A persona can grant a whole server (bare string — the old format, still
works) or scope to specific tools ({"server", "tools":[...]}). Both the
brain-mediated and worker-tools paths resolve through one method, so a scoped
tool the persona didn't pick is ABSENT from the model's tool list, not merely
blocked after a call."""

import json

from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.tools.sync import (ToolDef, ToolRegistry, is_write_tool,
                                       parse_preferred_mcp)

# the general-mcp-gateway example from the spec
GATEWAY_TOOLS = [
    "read_graph", "search_nodes", "open_nodes",            # read
    "create_entities", "create_relations", "add_observations",
    "delete_entities", "delete_observations", "delete_relations",  # write
    "mcp-add", "mcp-remove", "mcp-exec", "code-mode",
    "mcp-config-set", "mcp-create-profile", "mcp-activate-profile",  # meta-write
    "mcp-find", "mcp-discover",                             # informational
]


def _registry(tmp_path, servers=None):
    """A ToolRegistry pre-populated with MCP ToolDefs for the given servers."""
    servers = servers or {"general-mcp-gateway": GATEWAY_TOOLS,
                          "crawl4ai": ["scrape", "crawl"]}
    db = Database(tmp_path / "t.sqlite")
    reg = ToolRegistry(db, ModelRegistry(db), MCPManager([], db))
    for server, tools in servers.items():
        for name in tools:
            reg.tools[name] = ToolDef(name=name, kind="mcp", description="d",
                                      parameters={}, server=server, mcp_tool=name)
    # one model tool, always offered regardless of persona
    reg.tools["ask_local"] = ToolDef(name="ask_local", kind="model",
                                     description="d", parameters={}, model_id="local")
    return reg, db


def _names(tooldefs):
    return {t.mcp_tool for t in tooldefs}


# -- parsing ----------------------------------------------------------------------

def test_parse_bare_and_scoped_and_whole_object():
    whole, scoped, bare = parse_preferred_mcp({"preferred_mcp_tools": json.dumps([
        "crawl4ai",
        {"server": "general-mcp-gateway", "tools": ["read_graph", "open_nodes"]},
        {"server": "searxng"},                 # object w/o tools = whole server
    ])})
    assert bare == {"crawl4ai"}
    assert scoped == {"general-mcp-gateway": {"read_graph", "open_nodes"}}
    assert whole == {"searxng"}


def test_parse_tolerates_garbage():
    whole, scoped, bare = parse_preferred_mcp({"preferred_mcp_tools": "not json"})
    assert (whole, scoped, bare) == (set(), {}, set())
    assert parse_preferred_mcp(None) == (set(), {}, set())


# -- whole-server grant (back-compat) ---------------------------------------------

def test_bare_server_grants_all_tools(tmp_path):
    reg, _ = _registry(tmp_path)
    persona = {"preferred_mcp_tools": json.dumps(["general-mcp-gateway"])}
    got = _names(reg.mcp_tools_for_persona(persona))
    assert got == set(GATEWAY_TOOLS)           # every tool, dynamic


def test_whole_server_grows_with_sync(tmp_path):
    reg, _ = _registry(tmp_path, {"gw": ["a", "b"]})
    persona = {"preferred_mcp_tools": json.dumps(["gw"])}
    assert _names(reg.mcp_tools_for_persona(persona)) == {"a", "b"}
    reg.tools["c"] = ToolDef(name="c", kind="mcp", description="", parameters={},
                             server="gw", mcp_tool="c")
    assert _names(reg.mcp_tools_for_persona(persona)) == {"a", "b", "c"}  # picked up


# -- scoped grant -----------------------------------------------------------------

def test_scoped_grant_exposes_only_listed_tools(tmp_path):
    reg, _ = _registry(tmp_path)
    persona = {"virtual_name": "Foundry-Agent", "preferred_mcp_tools": json.dumps([
        {"server": "general-mcp-gateway",
         "tools": ["read_graph", "search_nodes", "open_nodes"]}])}
    got = _names(reg.mcp_tools_for_persona(persona))
    assert got == {"read_graph", "search_nodes", "open_nodes"}
    # the acceptance-test invariant: the dangerous tool is absent entirely
    assert "delete_entities" not in got
    assert "mcp-exec" not in got


def test_specs_for_persona_excludes_unscoped_mcp_but_keeps_model_tools(tmp_path):
    reg, _ = _registry(tmp_path)
    persona = {"preferred_mcp_tools": json.dumps([
        {"server": "general-mcp-gateway", "tools": ["read_graph"]}])}
    names = {s["function"]["name"] for s in reg.specs_for_persona(persona)}
    assert "read_graph" in names
    assert "delete_entities" not in names
    assert "ask_local" in names                # model tools always offered


def test_no_persona_or_empty_grants_no_mcp(tmp_path):
    reg, _ = _registry(tmp_path)
    assert reg.mcp_tools_for_persona(None) == []
    assert reg.mcp_tools_for_persona({"preferred_mcp_tools": "[]"}) == []


# -- scoped tool vanished upstream: drop silently + log once ----------------------

def test_missing_scoped_tool_dropped_and_logged_once(tmp_path):
    reg, db = _registry(tmp_path)
    persona = {"virtual_name": "Foundry-Agent", "preferred_mcp_tools": json.dumps([
        {"server": "general-mcp-gateway", "tools": ["read_graph", "ghost_tool"]}])}
    got = _names(reg.mcp_tools_for_persona(persona))
    assert got == {"read_graph"}               # ghost dropped, no error
    reg.mcp_tools_for_persona(persona)         # resolve again
    logs = db.query("SELECT * FROM event_log WHERE source='tool_sync' "
                    "AND message LIKE '%ghost_tool%'")
    assert len(logs) == 1                       # logged ONCE, not per request
    assert "Foundry-Agent" in logs[0]["message"]


def test_missing_scoped_relogs_after_reappearance(tmp_path):
    reg, db = _registry(tmp_path, {"gw": ["a"]})
    persona = {"virtual_name": "P", "preferred_mcp_tools": json.dumps([
        {"server": "gw", "tools": ["b"]}])}
    reg.mcp_tools_for_persona(persona)          # b missing -> log
    reg.tools["b"] = ToolDef(name="b", kind="mcp", description="", parameters={},
                             server="gw", mcp_tool="b")
    assert _names(reg.mcp_tools_for_persona(persona)) == {"b"}   # reappeared
    del reg.tools["b"]
    reg.mcp_tools_for_persona(persona)          # gone again -> logs again
    logs = db.query("SELECT * FROM event_log WHERE message LIKE '%gw/b%'")
    assert len(logs) == 2


# -- write heuristic (stretch goal) -----------------------------------------------

def test_write_heuristic_matches_spec_examples():
    reads = ["read_graph", "search_nodes", "open_nodes", "mcp-find", "mcp-discover"]
    writes = ["create_entities", "create_relations", "add_observations",
              "delete_entities", "delete_observations", "delete_relations",
              "mcp-add", "mcp-remove", "mcp-exec", "code-mode",
              "mcp-config-set", "mcp-create-profile", "mcp-activate-profile"]
    assert all(not is_write_tool(t) for t in reads)
    assert all(is_write_tool(t) for t in writes)


def test_status_flags_write_tools(tmp_path):
    reg, _ = _registry(tmp_path)
    by_name = {s["name"]: s for s in reg.status()}
    assert by_name["delete_entities"]["is_write"] is True
    assert by_name["read_graph"]["is_write"] is False
    assert "is_write" not in by_name["ask_local"]   # model tools: not applicable
