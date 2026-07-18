"""Central MCP usage counters + persisted pacing. The Usage-tab card only sees
per-request tool calls, so a background research sweep's searxng/crawl4ai use was
invisible. MCPManager now counts every call (any caller) and the MCP tab shows it.
And `pace_seconds` (the SearXNG throttle) now survives a UI edit instead of being
silently dropped by the upsert endpoint."""

from foundry_router.config import MCPServerConfig
from foundry_router.db import Database
from foundry_router.tools.mcp_client import MCPManager


def _mgr(tmp_path):
    db = Database(tmp_path / "m.sqlite")
    return MCPManager([MCPServerConfig(name="searxng", url="http://x/mcp")], db)


def test_usage_counts_all_callers(tmp_path):
    m = _mgr(tmp_path)
    assert m.usage() == {}                        # idle until something calls
    m._record_usage("searxng", "web_search", ok=True)
    m._record_usage("searxng", "web_search", ok=True)
    m._record_usage("searxng", "web_url_read", ok=False, rate_limited=True,
                    error="429 Too Many Requests")
    u = m.usage()["searxng"]
    assert u["calls"] == 3 and u["ok"] == 2 and u["fail"] == 1
    assert u["rate_limited"] == 1
    assert u["tools"] == {"web_search": 2, "web_url_read": 1}
    assert "429" in u["last_error"] and u["last_ts"]


def test_usage_snapshot_is_a_copy(tmp_path):
    m = _mgr(tmp_path)
    m._record_usage("searxng", "web_search", ok=True)
    snap = m.usage()
    snap["searxng"]["tools"]["web_search"] = 999     # mutate the snapshot
    assert m.usage()["searxng"]["tools"]["web_search"] == 1   # internal intact


# -- endpoints --------------------------------------------------------------------

def test_list_mcp_includes_usage_key(client):
    client.post("/admin/api/mcp_servers", json={"name": "s1", "url": "http://x/mcp"})
    s = {x["name"]: x for x in client.get("/admin/api/mcp_servers").json()["servers"]}["s1"]
    assert "usage" in s and s["usage"] is None       # present, idle


def test_pace_seconds_survives_upsert(client):
    client.post("/admin/api/mcp_servers",
                json={"name": "searxng", "url": "http://x/mcp", "pace_seconds": 3})
    s = {x["name"]: x for x in client.get("/admin/api/mcp_servers").json()["servers"]}["searxng"]
    assert s["pace_seconds"] == 3.0                  # not dropped by the endpoint
