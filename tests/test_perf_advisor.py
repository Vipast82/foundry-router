"""Performance advisor: each rule fires on the data shape it's for, and a
healthy setup produces no findings."""

import asyncio

from foundry_router import perf_advisor


def _add(db, **kw):
    row = dict(model="qwen", backend="llama", persona="act", mode="direct",
               prompt_tokens=100000, prefill_tokens=500, completion_tokens=300,
               reasoning_tokens=None, cached_tokens=99500, draft_n=300, draft_n_accepted=210,
               decode_tps=51.0, prefill_tps=300.0, decode_ms=6000, prefill_ms=1500,
               ttft_ms=2000, wall_ms=8000, load_ms=None, cache_hit_pct=99.5,
               spec_accept_pct=70.0, finish_reason="tool_calls", timing_src="server")
    row.update(kw)
    cols = ",".join(row)
    db.execute(f"INSERT INTO perf_samples (ts,{cols}) VALUES "
               f"(strftime('%Y-%m-%dT%H:%M:%fZ','now'),{','.join('?' * len(row))})",
               tuple(row.values()))


def _ids(res):
    return {f["id"] for f in res["findings"]}


class Pool:
    def __init__(self, servers=None, down=False):
        self.servers, self.down = servers or [], down

    async def server_metrics(self):
        return self.servers

    def backend_status(self):
        return [{"name": "llama", "type": "openai-compatible", "flavor": "llamacpp",
                 "healthy": not self.down, "url": "http://l", "last_error": "ConnectError"}]


def _run(svc, pool, hours=24):
    real, svc.pool = svc.pool, pool
    try:
        return asyncio.run(perf_advisor.advise(svc, hours))
    finally:
        svc.pool = real


def test_healthy_setup_has_no_findings(app):
    svc = app.state.services
    for _ in range(12):
        _add(svc.db)
    res = _run(svc, Pool())
    assert res["requests_analysed"] == 12
    assert not [f for f in res["findings"] if f["severity"] in ("critical", "warning")], res


def test_history_rules_fire(app):
    svc = app.state.services
    for i in range(10):   # the live pattern + other problems
        _add(svc.db, ttft_ms=55000, prefill_ms=2000, wall_ms=62000,
             cache_hit_pct=20.0, spec_accept_pct=25.0,
             finish_reason="length" if i < 4 else "tool_calls",
             prefill_tokens=8000, prefill_tps=90.0)
    res = _run(svc, Pool())
    ids = _ids(res)
    assert {"server_wait", "cache_miss", "low_spec", "truncation", "slow_prefill"} <= ids
    sw = next(f for f in res["findings"] if f["id"] == "server_wait")
    assert sw["severity"] == "critical" and "--cache-ram 0" in " ".join(sw["fix"])
    assert res["findings"][0]["severity"] == "critical"          # sorted by severity


def test_live_engine_backend_config_and_event_rules(app):
    svc = app.state.services
    svc.config_store.config.backend_pool.request_timeout_seconds = 600   # < stall 900
    svc.db.log_event("warning", "truncation",
                     "qwen: reply cut at 8192 output tokens (cap sent 32768)", "")
    svc.db.log_event("warning", "backend_pool", "backend llama marked unhealthy", "x")
    pool = Pool(servers=[{"backend": "llama", "flavor": "llamacpp", "requests_waiting": 2,
                          "requests_running": 1, "kv_cache_usage_pct": 95.0}], down=True)
    res = _run(svc, pool)
    ids = _ids(res)
    assert {"queued", "kv_full", "backend_down", "timeouts_order",
            "backend_output_cap", "backend_flap"} <= ids
    cap = next(f for f in res["findings"] if f["id"] == "backend_output_cap")
    assert "--n-predict" in " ".join(cap["fix"])
    for f in res["findings"]:                                    # every finding is actionable
        assert f["title"] and f["summary"] and f["why"] and f["fix"]


def test_advisor_endpoint(app, client):
    r = client.get("/admin/api/perf/advisor", params={"hours": 24}).json()
    assert "findings" in r and "counts" in r and r["hours"] == 24
