"""Docker MCP Gateway admin (gateway-server admin spec): operator-only,
backend-initiated MCP calls to browse/attach/detach catalog servers, gateway
detection by tool presence (no hardcoded name), tolerant catalog parsing, and
the security invariant that gateway root-admin tools are never persona-grantable
nor shown as a grant."""

import json

import pytest

from foundry_router.db import Database
from foundry_router.gateway_admin import (attached_view, add_server, config_set,
                                          find_servers, inspect_server,
                                          inspect_settings, parse_catalog,
                                          parse_inspect, remove_server,
                                          set_inspect_settings, _arg_name,
                                          _config_schema)
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.tools.sync import (ToolDef, ToolRegistry, is_gateway_admin_tool,
                                       is_gateway_management_tool, tool_write_badge)

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


# -- inspect companion service ----------------------------------------------------

def test_inspect_settings_roundtrip(tmp_path):
    db = Database(tmp_path / "i.sqlite")
    assert inspect_settings(db) == {"url": "", "has_token": False}
    set_inspect_settings(db, "http://host:8899", token="sekret")
    assert inspect_settings(db) == {"url": "http://host:8899", "has_token": True}
    # blank token keeps the existing one; empty url clears
    set_inspect_settings(db, "http://host:8899", token=None)
    assert inspect_settings(db)["has_token"] is True
    set_inspect_settings(db, "")
    assert inspect_settings(db)["url"] == ""


def test_parse_inspect_json_summary():
    p = parse_inspect(json.dumps({"name": "playwright", "publisher": "microsoft",
                                  "tools": ["a", "b", "c"], "secrets": ["TOKEN"]}))
    assert p["tool_count"] == 3 and p["publisher"] == "microsoft"
    assert p["requires_secrets"] is True
    assert parse_inspect("not json") == {}


INSPECT_YAML = """\
snapshot:
  server:
    title: Playwright MCP
    image: mcp/playwright
    tools:
      - name: browser_navigate
        description: Navigate to a URL
        annotations:
          readOnlyHint: false
          destructiveHint: false
      - name: browser_snapshot
        description: Read the page
        annotations:
          readOnlyHint: true
      - name: browser_close
        description: Close the browser
        annotations:
          destructiveHint: true
      - name: mystery_tool
        description: no annotations here
    config:
      - name: playwright
        description: Data location
        properties:
          data:
            type: string
"""


def test_parse_inspect_yaml_structured():
    p = parse_inspect(INSPECT_YAML)
    assert p["title"] == "Playwright MCP"
    assert p["image"] == "mcp/playwright"
    assert p["publisher"] == "mcp"                  # namespace of the image
    assert p["tool_count"] == 4
    by = {t["name"]: t for t in p["tools"]}
    # ground-truth badges from annotations
    assert by["browser_navigate"]["kind"] == "write" and by["browser_navigate"]["source"] == "annotation"
    assert by["browser_snapshot"]["kind"] == "read-only" and by["browser_snapshot"]["source"] == "annotation"
    assert by["browser_close"]["kind"] == "destructive"
    # no annotation -> heuristic guess, marked as such
    assert by["mystery_tool"]["source"] == "heuristic"
    # config schema surfaced for the form
    assert p["config_required"] is True
    assert p["config_schema"][0]["properties"] == {"data": {"type": "string"}}


class FakeHTTP:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers or {}})

        class R:
            def __init__(s, p): s._p = p
            def raise_for_status(s): pass
            def json(s): return s._p
        return R(self.payload)


async def test_inspect_server_unconfigured(tmp_path):
    db = Database(tmp_path / "u.sqlite")

    class S:
        pass
    s = S(); s.db = db; s.http = FakeHTTP({})
    out = await inspect_server(s, "playwright")
    assert out == {"configured": False}
    assert s.http.calls == []                    # no call when no URL set


async def test_inspect_server_calls_companion(tmp_path):
    db = Database(tmp_path / "c.sqlite")
    set_inspect_settings(db, "http://host:8899/", token="k")

    class S:
        pass
    s = S(); s.db = db
    s.http = FakeHTTP({"ok": True, "raw": json.dumps(
        {"name": "playwright", "tools": ["navigate", "click"], "publisher": "docker"})})
    out = await inspect_server(s, "playwright", catalog="docker-mcp")
    call = s.http.calls[0]
    assert call["url"] == "http://host:8899/inspect"
    assert call["json"] == {"server": "playwright", "catalog": "docker-mcp"}
    assert call["headers"]["Authorization"] == "Bearer k"
    assert out["configured"] and out["ok"]
    assert out["parsed"]["tool_count"] == 2 and out["parsed"]["publisher"] == "docker"


def test_inspect_endpoints(client, app):
    # not configured -> 400
    assert client.post("/admin/api/gateway/inspect",
                       json={"server": "playwright"}).status_code == 400
    # configure via endpoint, then it's reported in /servers
    r = client.post("/admin/api/gateway/inspect_config",
                    json={"url": "http://host:8899", "token": "k"})
    assert r.status_code == 200 and r.json()["has_token"] is True
    assert client.get("/admin/api/gateway/servers").json()["inspect"]["url"] == "http://host:8899"


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


# -- config schema + config-set (Part 3) ------------------------------------------

def test_config_schema_extraction_shapes():
    # list of blocks with properties
    blocks = _config_schema({"config": [
        {"name": "playwright-mcp-server", "description": "Data location",
         "properties": {"data": {"type": "string"}}}]})
    assert blocks[0]["properties"] == {"data": {"type": "string"}}
    assert blocks[0]["name"] == "playwright-mcp-server"
    # a single block
    assert _config_schema({"config_schema": {"properties": {"x": {"type": "number"}}}})[0]["properties"] == {"x": {"type": "number"}}
    # a bare {prop: {type}} map
    assert _config_schema({"config": {"data": {"type": "string"}}})[0]["properties"] == {"data": {"type": "string"}}
    # empty / absent -> []
    assert _config_schema({"config": []}) == []
    assert _config_schema({"name": "x"}) == []


def test_parse_catalog_includes_config_schema():
    row = parse_catalog(json.dumps([{
        "name": "playwright-mcp-server",
        "config": [{"name": "playwright-mcp-server", "description": "Data location",
                    "properties": {"data": {"type": "string"}}}]}]))[0]
    assert row["config_required"] is True
    assert row["config_schema"][0]["properties"] == {"data": {"type": "string"}}


async def test_config_set_uses_object_arg_from_schema(tmp_path):
    # mcp-config-set schema advertises {server, config:object} -> map goes in config
    reg, db = _registry(tmp_path, gateway_tools=["mcp-find", "mcp-add"])
    reg.tools["mcp-config-set"] = ToolDef(
        name="mcp-config-set", kind="mcp", description="", server=GATEWAY,
        mcp_tool="mcp-config-set",
        parameters={"type": "object", "properties": {
            "server": {"type": "string"}, "config": {"type": "object"}}})
    svc = Svc(reg, db)
    out = await config_set(svc, GATEWAY, "playwright-mcp-server", {"data": "/tmp/pw"})
    server, tool, args = svc.mcp.calls[0]
    assert tool == "mcp-config-set"
    assert args == {"server": "playwright-mcp-server", "config": {"data": "/tmp/pw"}}
    assert "raw" in out
    assert db.query_one("SELECT * FROM event_log WHERE source='gateway'")


async def test_config_set_flattens_when_no_object_arg(tmp_path):
    # no object-typed param -> values flattened as top-level args
    reg, db = _registry(tmp_path, gateway_tools=["mcp-find", "mcp-add"])
    reg.tools["mcp-config-set"] = ToolDef(
        name="mcp-config-set", kind="mcp", description="", server=GATEWAY,
        mcp_tool="mcp-config-set",
        parameters={"type": "object", "properties": {"server": {"type": "string"}}})
    svc = Svc(reg, db)
    await config_set(svc, GATEWAY, "pw", {"data": "/x"})
    _, _, args = svc.mcp.calls[0]
    assert args == {"server": "pw", "data": "/x"}


def test_config_set_endpoint_validates(client):
    assert client.post("/admin/api/gateway/config_set",
                       json={"server": "x", "values": {}}).status_code == 400
    assert client.post("/admin/api/gateway/config_set",
                       json={"values": {"a": 1}}).status_code == 400


# -- annotation -> registry -> badge flow (Part 1) --------------------------------

def test_tool_write_badge_prefers_annotation():
    assert tool_write_badge("read_graph", destructive=True)["kind"] == "destructive"
    w = tool_write_badge("read_graph", read_only=False)   # annotation says write
    assert w == {"kind": "write", "source": "annotation", "is_write": True}
    r = tool_write_badge("delete_everything", read_only=True)  # annotation overrides scary name
    assert r["kind"] == "read-only" and r["is_write"] is False
    # no annotation -> name heuristic, marked as guess
    g = tool_write_badge("delete_entities")
    assert g["source"] == "heuristic" and g["is_write"] is True


def test_status_reports_annotation_badges(tmp_path):
    reg, _ = _registry(tmp_path, gateway_tools=[])
    reg.tools["nav"] = ToolDef(name="nav", kind="mcp", description="", parameters={},
                               server="pw", mcp_tool="browser_navigate", read_only=False)
    reg.tools["snap"] = ToolDef(name="snap", kind="mcp", description="", parameters={},
                                server="pw", mcp_tool="browser_snapshot", read_only=True)
    reg.tools["guess"] = ToolDef(name="guess", kind="mcp", description="", parameters={},
                                 server="pw", mcp_tool="delete_entities")  # no annotation
    by = {s["mcp_tool"]: s for s in reg.status()}
    assert by["browser_navigate"]["is_write"] and by["browser_navigate"]["badge_source"] == "annotation"
    assert not by["browser_snapshot"]["is_write"]
    assert by["delete_entities"]["is_write"] and by["delete_entities"]["badge_source"] == "heuristic"
