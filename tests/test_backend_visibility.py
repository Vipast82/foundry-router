"""Every backend shows up wherever it can: Host Admin (Meridian too), the Live
engine view (CLIProxyAPI's accounts), the Performance filter (backends with no
samples yet), and performance samples for assist / research calls."""

from foundry_router.config import BackendConfig
from tests.test_persona_profiles import HITS, STATE, fleet  # noqa: F401 (fixture)


def test_meridian_flavor_label():
    assert BackendConfig(name="m", type="anthropic-compatible", url="http://m").effective_flavor == "meridian"
    assert BackendConfig(name="o", type="openai-compatible", url="http://o").effective_flavor == "openai"
    assert BackendConfig(name="l", type="ollama", url="http://l").effective_flavor == "ollama"


def test_host_admin_lists_meridian_with_read_only_diagnostics(fleet, client, monkeypatch):
    mer = fleet.pool.backends["meridian"]
    monkeypatch.setattr(mer.config, "type", "anthropic-compatible")

    async def metrics():
        return {"flavor": "meridian", "metrics_ok": True, "requests_total": 7}
    monkeypatch.setattr(mer.protocol, "server_metrics", metrics, raising=False)
    bes = {b["name"]: b for b in client.get("/admin/api/host/backends").json()["backends"]}
    assert bes["meridian"]["flavor"] == "meridian" and bes["meridian"]["crud"] is False
    rep = client.get("/admin/api/host/diag", params={"backend": "meridian"}).json()["report"]
    assert rep["flavor"] == "meridian" and rep["metrics"]["summary"]["requests_total"] == 7
    assert "claude-sonnet-5-5" in rep["models"]["models"]


def test_live_view_has_a_cliproxyapi_row(fleet, client):
    servers = {s["backend"]: s for s in client.get("/admin/api/activity").json()["servers"]}
    row = servers["cliproxy"]
    assert row["flavor"] == "cliproxyapi" and row["metrics_ok"] is False
    assert "management_key" in row["metrics_error"]


def test_perf_filter_lists_backends_without_samples(fleet, client):
    d = client.get("/admin/api/perf-history", params={"hours": 1}).json()
    names = {b["backend"]: b["n"] for b in d["backends"]}
    assert {"strata", "meridian", "cliproxy"} <= set(names)
    assert names["cliproxy"] == 0


def test_assist_calls_are_recorded_for_performance(fleet, client):
    import asyncio
    from foundry_router import frontier_assist
    ans, why = asyncio.run(frontier_assist.call_frontier(
        fleet, {"frontier_models": ["claude-sonnet-5-5"]}, {"virtual_name": "x"},
        "conv-1", "system", "question", max_tokens=200))
    assert ans is not None, why
    rows = fleet.db.query("SELECT model, backend, mode FROM perf_samples ORDER BY id DESC LIMIT 1")
    assert rows and (rows[0]["model"], rows[0]["mode"]) == ("claude-sonnet-5-5", "assist")
