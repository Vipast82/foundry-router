"""Research settings are GUI-editable (Phase 2a): the config endpoint persists
them AND applies them to the running Research Agent live (no restart), and the
search+fetch test probe runs through the configured tools. Closes the last
config.yaml-only / SSH-required gap for this class of change."""


def test_research_config_saves_and_applies_live(client):
    svc = client.app.state.services
    r = client.post("/admin/api/config/research", json={
        "enabled": True, "search_prefix": "!google !bing",
        "sweep_hours": 12, "max_pages_per_model": 6,
        "search": {"server": "searxng", "tool": "web_search", "query_param": "q"},
        "fetch": {"server": "crawl4ai", "tool": "md", "url_param": "url"}})
    assert r.status_code == 200 and r.json()["ok"] is True
    # persisted to config
    got = client.get("/admin/api/config").json()["registry"]["research"]
    assert got["search_prefix"] == "!google !bing" and got["sweep_hours"] == 12
    assert got["max_pages_per_model"] == 6
    # applied live to the running agent (not just written to disk)
    assert svc.research.cfg.search_prefix == "!google !bing"
    assert svc.research.cfg.max_pages_per_model == 6
    assert svc.research.cfg.search.query_param == "q"


def test_research_config_rejects_bad_value(client):
    # a non-int where the schema wants one is a 400, not a 500
    r = client.post("/admin/api/config/research", json={"sweep_hours": "notanumber"})
    assert r.status_code == 400


def test_research_test_probe_reports_cleanly(client):
    # no MCP servers configured in the test app -> the probe reports a search
    # error instead of throwing
    body = client.post("/admin/api/research/test").json()
    assert body["ok"] is False
    assert body["search_ok"] is False and body["search_error"]
