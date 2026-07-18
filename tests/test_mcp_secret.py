"""MCP server auth token stored in the DB (set from the UI) instead of
config.yaml — merged into the server's connection headers at session time, so
an operator can attach auth without editing the config file, and it survives
config saves. The token value is write-only (never returned to the UI)."""

from foundry_router.config import MCPServerConfig
from foundry_router.db import Database
from foundry_router.tools.mcp_client import MCPManager


def _mgr(tmp_path):
    db = Database(tmp_path / "m.sqlite")
    return MCPManager([MCPServerConfig(name="searxng", url="http://x/mcp")], db)


def test_set_and_meta_never_leaks_token(tmp_path):
    m = _mgr(tmp_path)
    assert m.secret_meta("searxng") == {"has_token": False, "token_header": "Authorization"}
    m.set_secret("searxng", "s3cr3t")
    meta = m.secret_meta("searxng")
    assert meta["has_token"] is True and meta["token_header"] == "Authorization"
    assert "s3cr3t" not in str(meta)          # value is write-only


def test_authorization_gets_bearer_prefix(tmp_path):
    m = _mgr(tmp_path)
    m.set_secret("searxng", "abc123")
    assert m._secret_headers("searxng") == {"Authorization": "Bearer abc123"}


def test_existing_scheme_not_double_prefixed(tmp_path):
    m = _mgr(tmp_path)
    m.set_secret("searxng", "Bearer already")
    assert m._secret_headers("searxng") == {"Authorization": "Bearer already"}


def test_custom_header_sends_raw_value(tmp_path):
    m = _mgr(tmp_path)
    m.set_secret("searxng", "abc123", header="x-api-key")
    assert m._secret_headers("searxng") == {"x-api-key": "abc123"}


def test_empty_token_clears(tmp_path):
    m = _mgr(tmp_path)
    m.set_secret("searxng", "abc")
    m.set_secret("searxng", "")               # clear
    assert m.secret_meta("searxng")["has_token"] is False
    assert m._secret_headers("searxng") == {}


def test_secret_merges_over_config_headers(tmp_path):
    db = Database(tmp_path / "m.sqlite")
    m = MCPManager([MCPServerConfig(name="s", url="http://x",
                                    headers={"X-Static": "keep"})], db)
    m.set_secret("s", "tok", header="x-api-key")
    # emulate the header assembly done in _session
    merged = {**(m.servers["s"].headers or {}), **m._secret_headers("s")}
    assert merged == {"X-Static": "keep", "x-api-key": "tok"}


# -- endpoints --------------------------------------------------------------------

def test_token_endpoint_roundtrip(client):
    client.post("/admin/api/mcp_servers",
                json={"name": "searxng", "url": "http://x/mcp"})
    r = client.post("/admin/api/mcp_servers/token",
                    json={"server": "searxng", "token": "abc123"})
    assert r.status_code == 200 and r.json()["has_token"] is True
    # the list reports presence, not the value
    servers = {s["name"]: s for s in client.get("/admin/api/mcp_servers").json()["servers"]}
    assert servers["searxng"]["has_token"] is True
    assert "abc123" not in client.get("/admin/api/mcp_servers").text
    # missing server is rejected
    assert client.post("/admin/api/mcp_servers/token", json={"token": "x"}).status_code == 400
