"""Engine metrics history (W4): server_samples written from pool.server_metrics(),
the /admin/api/perf/servers endpoint, and the perf.retention_days setting."""

import asyncio
import time
from types import SimpleNamespace

from foundry_router import perf_history as ph
from foundry_router import server_history as sh
from foundry_router.config import PerfConfig
from foundry_router.db import Database

STRATA = {"backend": "strata", "flavor": "llamacpp", "metrics_ok": True, "metrics_dialect": "vllm",
          "kv_cache_usage_pct": 41.5, "requests_running": 1, "requests_waiting": 0,
          "avg_decode_tps": 55.2, "avg_prompt_tps": 1800.0, "live_decode_tps": 49.0,
          "prefix_cache_hit_pct": 96.9, "slots_total": 1, "slots_busy": 1,
          "slots": [{"id": 0, "busy": True}], "swap_running": []}
MERIDIAN = {"backend": "meridian", "flavor": "meridian", "metrics_ok": True, "status": "healthy",
            "version": "1.2", "logged_in": True, "requests_total": 1000, "requests_failed": 10,
            "mean_ttft_ms": 4401.0, "mean_e2e_ms": 10871.0}


def _svc(tmp_path, servers):
    async def server_metrics():
        return servers
    return SimpleNamespace(db=Database(tmp_path / "s.sqlite"),
                           pool=SimpleNamespace(server_metrics=server_metrics))


def test_flatten_keeps_numbers_only():
    names = dict(sh.flatten(STRATA))
    assert names["kv_cache_usage_pct"] == 41.5 and names["slots_busy"] == 1
    assert names["avg_decode_tps"] == 55.2 and names["live_decode_tps"] == 49.0
    assert not {"slots", "flavor", "backend", "metrics_ok", "swap_running"} & set(names)
    m = dict(sh.flatten(MERIDIAN))
    assert m["requests_total"] == 1000 and "logged_in" not in m and "version" not in m


def test_sample_once_writes_one_row_per_backend_and_metric(tmp_path):
    svc = _svc(tmp_path, [STRATA, MERIDIAN, {"backend": "broken", "metrics_ok": False,
                                              "metrics_error": "refused"}])
    ph.record_sample(svc.db, model="qwen-local", backend="strata", prompt_tokens=1)
    n = asyncio.run(sh.sample_once(svc, now=1000.0))
    rows = svc.db.query("SELECT * FROM server_samples")
    assert n == len(rows) and {r["backend"] for r in rows} == {"strata", "meridian"}
    kv = [r for r in rows if r["backend"] == "strata" and r["metric"] == "kv_cache_usage_pct"]
    assert kv[0]["value"] == 41.5 and kv[0]["ts"] == 1000.0
    assert kv[0]["model"] == "qwen-local"            # the model the backend last served
    assert any(r["metric"] == "mean_ttft_ms" and r["value"] == 4401.0 for r in rows
               if r["backend"] == "meridian")


def test_query_buckets_and_filters(tmp_path):
    svc = _svc(tmp_path, [STRATA])
    now = time.time()
    for i in range(120):                              # two hours, one sample a minute
        svc.db.executemany("INSERT INTO server_samples (ts, backend, model, metric, value) "
                           "VALUES (?,?,?,?,?)",
                           [(now - 7200 + i * 60, "strata", "q", "kv_cache_usage_pct", float(i)),
                            (now - 7200 + i * 60, "strata", "q", "avg_decode_tps", 50.0),
                            (now - 7200 + i * 60, "meridian", "", "requests_total", float(i))])
    out = sh.query(svc.db, hours=3, max_points=20)
    assert set(out["backends"]) == {"strata", "meridian"}
    kv = out["backends"]["strata"]["series"]["kv_cache_usage_pct"]
    assert 5 < len(kv) <= 25 and kv == sorted(kv)     # downsampled, chronological
    assert out["backends"]["strata"]["model"] == "q"
    one = sh.query(svc.db, hours=3, backend="strata", metrics=["avg_decode_tps"])
    assert list(one["backends"]) == ["strata"]
    assert list(one["backends"]["strata"]["series"]) == ["avg_decode_tps"]
    assert sh.query(svc.db, hours=1)["samples"] < out["samples"]


def test_endpoint_and_retention_setting(app, client):
    svc = app.state.services
    now = time.time()
    svc.db.executemany("INSERT INTO server_samples (ts, backend, model, metric, value) VALUES "
                       "(?,?,?,?,?)", [(now - 60, "strata", "q", "slots_busy", 1.0),
                                       (now - 40 * 86400, "strata", "q", "slots_busy", 0.0)])
    r = client.get("/admin/api/perf/servers?hours=24").json()
    assert r["backends"]["strata"]["series"]["slots_busy"][0][1] == 1.0
    assert r["interval_s"] == 60 and r["retention_days"] == 14        # defaults
    assert client.get("/admin/api/config/perf").json()["retention_days"] == 14
    s = client.post("/admin/api/config/perf", json={"retention_days": 7, "server_sample_seconds": 30,
                                                    "subscription_rates": [
                                                        {"match": "meridian", "usd_per_call": 0.04}]})
    assert s.status_code == 200 and s.json()["perf"]["retention_days"] == 7
    assert svc.config_store.config.perf.server_sample_seconds == 30
    assert sh.prune(svc.db, svc.config_store.config.perf.retention_days) == 1   # the 40-day row
    assert svc.db.query_one("SELECT COUNT(*) AS n FROM server_samples")["n"] == 1


def test_perf_retention_days_replaces_the_hardcoded_14(tmp_path, monkeypatch):
    db = Database(tmp_path / "r.sqlite")
    old = "2020-01-01T00:00:00+00:00"
    for _ in range(2):
        db.execute("INSERT INTO perf_samples (ts, model) VALUES (?, 'm')", (old,))
    cfg = PerfConfig()
    assert cfg.retention_days == 14 and cfg.server_sample_seconds == 60
    ph.configure(retention=lambda: cfg.retention_days)
    try:
        monkeypatch.setattr(ph.random, "random", lambda: 0.0)          # force the opportunistic prune
        ph.record_sample(db, model="m", prompt_tokens=1)
        assert db.query_one("SELECT COUNT(*) AS n FROM perf_samples")["n"] == 1   # old rows gone
        recent = (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 5 * 86400)))
        db.execute("INSERT INTO perf_samples (ts, model) VALUES (?, 'm')", (recent,))
        cfg.retention_days = 3                                          # live change
        assert ph.retention_days() == 3
        assert ph.prune(db) == 1                                        # the 5-day-old row
    finally:
        ph.configure(retention=14)
