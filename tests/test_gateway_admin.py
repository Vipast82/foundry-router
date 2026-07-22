"""Docker MCP Gateway admin (gateway-server admin spec): operator-only,
backend-initiated MCP calls to browse/attach/detach catalog servers, gateway
detection by tool presence (no hardcoded name), tolerant catalog parsing, and
the security invariant that gateway root-admin tools are never persona-grantable
nor shown as a grant."""

import json

import pytest

from foundry_router.db import Database
from foundry_router.gateway_admin import (attached_view, add_server, find_servers,
                                          parse_catalog, remove_server, _arg_name)
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.tools.sync import (ToolDef, ToolRegistry, is_gateway_admin_tool,
                                       is_gateway_management_tool)

GATEWAY = "general-mcp-gateway"
MGMT = ["mcp-find", "mcp-add", "mcp-remove", "mcp-create-profile",
        "mcp-activate-profile", "mcp-config-set"]
MEMORY = ["read_graph", "search_nodes", "create_entities", "delete_entities"]


def _registry(tmp_path, gateway_tools=None, extra=None):
    db = Database(tmp_path / "g.sqlite")
    reg = ToolRegistry(db, ModelRegistry(db), MCPManager([], db))
    for name in (gateway_tools if gateway_tools is not None else MGMT + MEMORY):
        schema = ({"type": "object", "properties": {"query": {"type": "string"}}}
                  if name == "mcp-find" else
                  {"type": "object", "properties": {"ref": {"type": "string"}}}
                  if name in ("mcp-add", "mcp-remove") else {})
        reg.tools[name] = ToolDef(name=name, kind="mcp", description="d",
                                  parameters=schema, server=GATEWAY, mcp_tool=name)
    for server, tools in (extra or {}).items():
        for name in tools:
            reg.tools[f"{server}_{name}"] = ToolDef(
                name=f"{server}_{name}", kind="mcp", description="d",
                parameters={}, server=server, mcp_tool=name)
    return reg, db


# -- gateway detection + admin-tool classification --------------------------------

def test_gateway_detected_by_tool_presence(tmp_path):
    reg, _ = _registry(tmp_path)
    assert reg.gateway_servers() == [GATEWAY]


def test_non_gateway_server_not_detected(tmp_path):
    reg, _ = _registry(tmp_path, gateway_tools=[], extra={"searxng": ["web_search"]})
    assert reg.gateway_servers() == []


def test_admin_tool_classification():
    for t in ("mcp-add", "mcp-remove", "mcp-create-profile",
              "mcp-activate-profile", "mcp-config-set"):
        assert is_gateway_admin_tool(t)
    for t in ("mcp-find", "mcp-discover", "read_graph"):
        assert not is_gateway_admin_tool(t)
    assert is_gateway_management_tool("mcp-find")   # management incl. read-only


# -- security: admin tools never persona-grantable --------------------------------

def test_admin_tools_excluded_even_under_whole_server_grant(tmp_path):
    reg, _ = _registry(tmp_path)
    persona = {"preferred_mcp_tools": json.dumps([GATEWAY])}   # whole server
    got = {t.mcp_tool for t in reg.mcp_tools_for_persona(persona)}
    assert "read_graph" in got and "delete_entities" in got    # memory tools ok
    for admin in ("mcp-add", "mcp-remove", "mcp-create-profile",
                  "mcp-activate-profile", "mcp-config-set"):
        assert admin not in got                                # never exposed


def test_admin_tools_excluded_even_if_explicitly_scoped(tmp_path):
    reg, _ = _registry(tmp_path)
    persona = {"preferred_mcp_tools": json.dumps([
        {"server": GATEWAY, "tools": ["read_graph", "mcp-add"]}])}
    got = {t.mcp_tool for t in reg.mcp_tools_for_persona(persona)}
    assert got == {"read_graph"}                               # mcp-add dropped


def test_status_flags_admin_tools(tmp_path):
    reg, _ = _registry(tmp_path)
    by_name = {s["name"]: s for s in reg.status()}
    assert by_name["mcp-add"]["is_admin"] is True
    assert by_name["read_graph"]["is_admin"] is False


# -- catalog parsing (tolerant of shape) ------------------------------------------

def test_parse_catalog_list_shape():
    rows = parse_catalog(json.dumps([
        {"name": "playwright", "ref": "docker/playwright", "description": "browser",
         "tools": ["navigate", "click"]},
        {"name": "github", "requiresSecrets": True, "tools": 30},
    ]))
    assert rows[0]["name"] == "playwright" and rows[0]["ref"] == "docker/playwright"
    assert rows[0]["tools"] == 2 and rows[0]["requires_secrets"] is False
    assert rows[1]["requires_secrets"] is True and rows[1]["tools"] == 30


def test_parse_catalog_wrapped_and_secret_variants():
    assert parse_catalog(json.dumps({"results": [{"name": "x"}]}))[0]["name"] == "x"
    # secrets signalled by a non-empty secrets list
    assert parse_catalog(json.dumps([{"name": "g", "secrets": ["GITHUB_PAT"]}]))[0]["requires_secrets"]
    # …or by a textual note
    assert parse_catalog(json.dumps([{"name": "g", "note": "Requires Secrets"}]))[0]["requires_secrets"]
    # prose-wrapped JSON still parses
    assert parse_catalog('Here you go:\n```json\n[{"name":"y"}]\n```')[0]["name"] == "y"


def test_parse_catalog_garbage_is_empty():
    assert parse_catalog("total nonsense, no json") == []
    assert parse_catalog("") == []


def test_parse_catalog_extracts_publisher_tools_config():
    rows = parse_catalog(json.dumps([
        {"name": "playwright-mcp-server", "publisher": "microsoft",
         "toolCount": 21, "config": ["browser", "headless"]},
        {"name": "playwright", "ref": "docker/playwright",
         "tools": {"navigate": {}, "click": {}}},
        {"name": "plain"},
    ]))
    # explicit publisher + alt tool-count key + non-empty config -> required
    assert rows[0]["publisher"] == "microsoft"
    assert rows[0]["tools"] == 21
    assert rows[0]["config_required"] is True
    assert rows[0]["requires_secrets"] is False   # config != secrets
    # publisher derived from a namespaced ref; tools counted from a dict
    assert rows[1]["publisher"] == "docker" and rows[1]["tools"] == 2
    # nothing known -> unknown config (None), no publisher, no tool count
    assert rows[2]["config_required"] is None
    assert rows[2]["publisher"] == "" and rows[2]["tools"] is None


def test_parse_catalog_keeps_raw_payload_per_row():
    item = {"name": "x", "weird_field": {"nested": [1, 2]}}
    row = parse_catalog(json.dumps([item]))[0]
    assert row["raw"] == item                     # untouched, for the UI's raw view


def test_config_present_but_empty_is_false_not_unknown():
    row = parse_catalog(json.dumps([{"name": "x", "config": []}]))[0]
    assert row["config_required"] is False        # field exists, empty -> 'none'


def test_arg_name_reads_schema_then_falls_back():
    tool = ToolDef(name="mcp-find", kind="mcp", description="", server=GATEWAY,
                   mcp_tool="mcp-find",
                   parameters={"type": "object", "properties": {"q": {"type": "string"}}})
    assert _arg_name(tool, ["query", "q"]) == "q"          # schema wins
    assert _arg_name(None, ["query", "q"]) == "query"      # no schema -> preference


# -- attached view ----------------------------------------------------------------

def test_attached_view_groups_namespaced_tools(tmp_path):
    reg, _ = _registry(tmp_path)
    # simulate playwright attached with namespaced tools
    for t in ["playwright.navigate", "playwright.click"]:
        reg.tools[t] = ToolDef(name=t, kind="mcp", description="", parameters={},
                               server=GATEWAY, mcp_tool=t)

    class Svc:
        tool_registry = reg
    view = attached_view(Svc())[0]
    assert view["gateway"] == GATEWAY
    groups = {g["name"]: g for g in view["groups"]}
    assert groups["playwright"]["removable"] is True
    assert set(groups["playwright"]["tools"]) == {"playwright.navigate", "playwright.click"}
    assert groups["(ungrouped)"]["removable"] is False   # base memory tools


# -- orchestration: add/remove call the gateway then re-sync ----------------------

class FakeMCP:
    def __init__(self):
        self.calls = []

    async def call_tool(self, server, tool, arguments):
        self.calls.append((server, tool, arguments))
        if tool == "mcp-find":
            return json.dumps([{"name": "playwright", "ref": "docker/playwright",
                                "tools": ["navigate"]}])
        return f"ok: {tool} {arguments}"


class Svc:
    def __init__(self, reg, db):
        self.tool_registry = reg
        self.mcp = FakeMCP()
        self.db = db
        self.pool = object()
        self.synced = 0

        async def _sync(pool):
            self.synced += 1
            return {"added": ["nav"], "removed": [], "total": 5}
        reg.sync = _sync


async def test_find_uses_schema_arg_name(tmp_path):
    reg, db = _registry(tmp_path)
    svc = Svc(reg, db)
    r = await find_servers(svc, GATEWAY, "playwright")
    assert svc.mcp.calls[0] == (GATEWAY, "mcp-find", {"query": "playwright"})
    assert r["results"][0]["ref"] == "docker/playwright"


async def test_add_then_resyncs(tmp_path):
    reg, db = _registry(tmp_path)
    svc = Svc(reg, db)
    r = await add_server(svc, GATEWAY, "docker/playwright")
    assert svc.mcp.calls[0] == (GATEWAY, "mcp-add", {"ref": "docker/playwright"})
    assert svc.synced == 1 and r["sync"]["added"] == ["nav"]
    assert db.query_one("SELECT * FROM event_log WHERE source='gateway'")


async def test_remove_then_resyncs(tmp_path):
    reg, db = _registry(tmp_path)
    svc = Svc(reg, db)
    await remove_server(svc, GATEWAY, "docker/playwright")
    assert svc.mcp.calls[0] == (GATEWAY, "mcp-remove", {"ref": "docker/playwright"})
    assert svc.synced == 1


# -- endpoints --------------------------------------------------------------------

def test_gateway_endpoints_no_gateway(client):
    # fresh app has no MCP tools -> no gateway detected
    assert client.get("/admin/api/gateway/servers").json()["gateways"] == []
    r = client.post("/admin/api/gateway/find", json={"query": "x"})
    assert r.status_code == 404
    assert client.post("/admin/api/gateway/add", json={}).status_code == 400  # ref required


def test_gateway_endpoints_with_injected_gateway(client, app):
    svc = app.state.services
    # inject gateway tools into the live registry + a fake mcp
    for name in MGMT + MEMORY:
        schema = {"type": "object", "properties": {"query": {"type": "string"}}} \
            if name == "mcp-find" else \
            {"type": "object", "properties": {"ref": {"type": "string"}}} \
            if name in ("mcp-add", "mcp-remove") else {}
        svc.tool_registry.tools[name] = ToolDef(name=name, kind="mcp", description="",
                                                parameters=schema, server=GATEWAY,
                                                mcp_tool=name)
    svc.mcp = FakeMCP()

    async def _sync(pool):
        return {"added": ["playwright.navigate"], "removed": [], "total": 9}
    svc.tool_registry.sync = _sync

    assert client.get("/admin/api/gateway/servers").json()["gateways"] == [GATEWAY]
    found = client.post("/admin/api/gateway/find", json={"query": "playwright"}).json()
    assert found["results"][0]["name"] == "playwright"
    added = client.post("/admin/api/gateway/add",
                        json={"ref": "docker/playwright"}).json()
    assert added["sync"]["added"] == ["playwright.navigate"]
