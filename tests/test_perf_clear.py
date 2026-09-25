"""Clean-slate tooling for performance data: run labels stamped on samples,
per-model × backend × run breakdown, clearing history (all or filtered),
resetting the Live-view rolling stats, the admin endpoints that drive the GUI,
and the shared telemetry recorder."""

from foundry_router import perf_history as ph
from foundry_router import telemetry
from foundry_router.db import Database
from foundry_router.pool.protocols import ChatResult
from foundry_router.registry.models_db import ModelRegistry


def _db(tmp_path):
    return Database(tmp_path / "c.sqlite")


def _sample(db, model="m", backend="b1", **kw):
    ph.record_sample(db, model=model, backend=backend, prompt_tokens=100,
                     completion_tokens=50, eval_duration_ns=1_000_000_000,
                     wall_ms=2000, **kw)


def test_run_label_stamped_and_filterable(tmp_path):
    db = _db(tmp_path)
    _sample(db)                                   # unlabelled
    ph.set_run_label(db, "2x4060ti-16gb")
    _sample(db)
    ph.set_run_label(db, "2x2080ti-22gb")
    _sample(db)
    _sample(db, finish_reason="length")
    assert ph.get_run_label(db) == "2x2080ti-22gb"
    h = ph.query_history(db, hours=24)
    assert {r["run_label"] for r in h["run_labels"]} == {"", "2x4060ti-16gb", "2x2080ti-22gb"}
    assert h["current_run_label"] == "2x2080ti-22gb"
    new = ph.query_history(db, hours=24, run_label="2x2080ti-22gb")
    assert new["summary"]["samples"] == 2 and new["summary"]["truncations"] == 1
    assert ph.query_history(db, hours=24, run_label="")["summary"]["samples"] == 1
    # breakdown: one row per run for the same model
    assert len(h["by_model"]) == 3
    g = next(g for g in h["by_model"] if g["run_label"] == "2x2080ti-22gb")
    assert g["n"] == 2 and g["p50_decode_tps"] == 50.0 and g["truncations"] == 1
    ph.set_run_label(db, "")
    assert ph.get_run_label(db) == ""


def test_breakdown_splits_backends(tmp_path):
    db = _db(tmp_path)
    _sample(db, backend="llamacpp-1")
    _sample(db, backend="vllm-1")
    h = ph.query_history(db, hours=24)
    assert {g["backend"] for g in h["by_model"]} == {"llamacpp-1", "vllm-1"}
    assert ph.query_history(db, hours=24, backend="vllm-1")["summary"]["samples"] == 1
    # filter option lists span the window regardless of the active filter
    assert len(ph.query_history(db, hours=24, backend="vllm-1")["backends"]) == 2


def test_clear_samples_all_and_filtered(tmp_path):
    db = _db(tmp_path)
    _sample(db, model="a")
    _sample(db, model="b")
    _sample(db, model="b", backend="b2")
    assert ph.clear_samples(db, model="b", backend="b2") == 1
    assert ph.clear_samples(db, model="b") == 1
    assert ph.query_history(db, hours=24)["summary"]["samples"] == 1
    assert ph.clear_samples(db) == 1
    assert ph.query_history(db, hours=24)["summary"]["samples"] == 0


def test_reset_perf_stats(tmp_path):
    db = _db(tmp_path)
    reg = ModelRegistry(db)
    for m in ("a", "b"):
        reg.note_inference(m, 100, 1_000_000_000, 5_000_000_000, prompt_count=100,
                           prompt_eval_duration_ns=100_000_000, draft_n=10,
                           draft_n_accepted=5, cached_tokens=50)
        reg.note_ttft(m, 300)
        reg.note_finish(m, "length")
    assert reg.benchmarks("a")                      # observed latency score exists
    assert reg.reset_perf_stats("a", latency_benchmark=True) == 1
    a, b = reg.get("a"), reg.get("b")
    assert a["eval_tps_avg"] is None and a["eval_samples"] == 0
    assert a["truncations"] == 0 and a["ttft_ms_avg"] is None
    assert a["spec_draft_total"] == 0 and a["cache_prompt_total"] == 0
    assert not [x for x in reg.benchmarks("a") if x["category"] == "latency"]
    assert b["eval_tps_avg"] and b["truncations"] == 1   # other model untouched
    # truncations only
    reg.reset_perf_stats(None, speed=False, truncations=True)
    b = reg.get("b")
    assert b["truncations"] == 0 and b["eval_tps_avg"]


def test_telemetry_record_call_logs_truncation(tmp_path):
    db = _db(tmp_path)
    reg = ModelRegistry(db)
    res = ChatResult(prompt_tokens=100, completion_tokens=50,
                     eval_duration_ns=1_000_000_000, finish_reason="length",
                     prefill_tokens=40, prompt_eval_duration_ns=100_000_000)
    telemetry.record_call(db, reg, model="m", backend="b", result=res,
                          persona="p", mode="agent", ttft_ms=120, wall_ms=1500,
                          max_tokens=50)
    row = reg.get("m")
    assert row["truncations"] == 1 and round(row["ttft_ms_avg"]) == 120
    assert round(row["prompt_tps_avg"]) == 400       # prefill_tokens / time
    s = db.query("SELECT * FROM perf_samples")[0]
    assert s["mode"] == "agent" and s["finish_reason"] == "length" and s["ttft_ms"] == 120
    assert any("TRUNCATED" in e["message"] for e in db.query("SELECT * FROM event_log"))


# -- admin endpoints -------------------------------------------------------------------

def test_perf_endpoints(app, client):
    svc = app.state.services
    r = client.post("/admin/api/perf/run-label", json={"run_label": "rig-a"}).json()
    assert r["ok"] and r["run_label"] == "rig-a"
    assert client.get("/admin/api/perf/run-label").json()["run_label"] == "rig-a"
    db = svc.db
    _sample(db, model="x")
    _sample(db, model="y", finish_reason="length")
    svc.registry.note_finish("y", "length")
    h = client.get("/admin/api/perf-history", params={"hours": 24}).json()
    assert h["summary"]["samples"] == 2 and h["current_run_label"] == "rig-a"
    assert client.get("/admin/api/perf-history",
                      params={"hours": 24, "run_label": "nope"}).json()["summary"]["samples"] == 0
    out = client.post("/admin/api/perf/clear", json={"model": "y"}).json()
    assert out["ok"] and out["samples"] == 1
    assert svc.registry.get("y")["truncations"] == 0
    out = client.post("/admin/api/perf/clear", json={"requests": True}).json()
    assert out["samples"] == 1 and "requests" in out
    act = client.get("/admin/api/activity").json()
    assert act["run_label"] == "rig-a" and act["servers"] == []


def test_openai_facade_passthrough_records_telemetry_and_finish(app, client):
    """/v1/chat/completions with a raw backend model used to record nothing and
    always answer finish_reason=stop — hiding truncations from the client."""
    from foundry_router.pool.protocols import ChatResult
    svc = app.state.services

    class _Pool:
        def backend_info(self, m):
            return {"name": "llamacpp-1", "type": "openai-compatible"} if m == "raw" else None

        async def chat(self, model, messages, **kw):
            return ChatResult(content="cut", prompt_tokens=10, completion_tokens=5,
                              eval_duration_ns=100_000_000, finish_reason="length",
                              timing_source="server"), "llamacpp-1"
    real = svc.pool
    svc.pool = _Pool()
    try:
        r = client.post("/v1/chat/completions", json={
            "model": "raw", "messages": [{"role": "user", "content": "hi"}],
            "top_k": 20, "min_p": 0.05}).json()
    finally:
        svc.pool = real
    assert r["choices"][0]["finish_reason"] == "length"
    s = svc.db.query("SELECT * FROM perf_samples")
    assert len(s) == 1 and s[0]["backend"] == "llamacpp-1" and s[0]["mode"] == "passthrough"
    assert svc.registry.get("raw")["truncations"] == 1
