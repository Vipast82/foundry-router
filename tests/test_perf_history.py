"""perf_samples time-series: recording derives the rates/percentages correctly
and stores gaps (NULL) for metrics a backend didn't report; query_history
windows, filters by model, and summarizes."""

from foundry_router import perf_history as ph
from foundry_router.db import Database


def _db(tmp_path):
    return Database(tmp_path / "p.sqlite")


def test_record_sample_derives_rates_and_pcts(tmp_path):
    db = _db(tmp_path)
    ph.record_sample(
        db, model="qwen", backend="llama-mtp", persona="cline-act", mode="direct",
        prompt_tokens=1000, completion_tokens=200, cached_tokens=900,
        draft_n=10, draft_n_accepted=8,
        eval_duration_ns=4_000_000_000, prompt_eval_duration_ns=500_000_000,
        ttft_ms=850, wall_ms=12000, finish_reason="stop")
    r = db.query("SELECT * FROM perf_samples")[0]
    assert round(r["decode_tps"]) == 50          # 200 / 4.0s
    assert round(r["prefill_tps"]) == 2000       # 1000 / 0.5s
    assert round(r["decode_ms"]) == 4000
    assert round(r["prefill_ms"]) == 500
    assert round(r["eff_tps"]) == 17             # 200 / 12.0s end-to-end
    assert r["cache_hit_pct"] == 90.0            # 900 / 1000
    assert r["spec_accept_pct"] == 80.0          # 8 / 10
    assert r["ttft_ms"] == 850.0


def test_record_sample_stores_gaps_not_zeros(tmp_path):
    # Ollama / plain OpenAI: no spec, no cache, no timings reported → NULL, so a
    # chart draws a gap rather than a misleading 0.
    db = _db(tmp_path)
    ph.record_sample(db, model="m", prompt_tokens=100, completion_tokens=10,
                     wall_ms=2000)
    r = db.query("SELECT * FROM perf_samples")[0]
    assert r["spec_accept_pct"] is None
    assert r["cache_hit_pct"] is None
    assert r["decode_tps"] is None               # no eval_duration
    assert round(r["eff_tps"]) == 5              # still have wall time


def test_query_history_filters_and_summarizes(tmp_path):
    db = _db(tmp_path)
    for i in range(3):
        ph.record_sample(db, model="qwen", prompt_tokens=1000, completion_tokens=100,
                         eval_duration_ns=2_000_000_000, draft_n=10, draft_n_accepted=6,
                         wall_ms=5000)
    ph.record_sample(db, model="other", prompt_tokens=50, completion_tokens=5,
                     eval_duration_ns=1_000_000_000, wall_ms=1000)
    allh = ph.query_history(db, hours=72)
    assert allh["summary"]["samples"] == 4
    assert {m["model"] for m in allh["models"]} == {"qwen", "other"}
    only = ph.query_history(db, hours=72, model="qwen")
    assert only["summary"]["samples"] == 3
    assert only["summary"]["spec_accept_pct"] == 60.0      # only qwen had draft
    assert only["series"][0]["ts"] <= only["series"][-1]["ts"]   # chronological


def test_query_history_window_excludes_old(tmp_path):
    import datetime as _dt
    db = _db(tmp_path)
    ph.record_sample(db, model="m", prompt_tokens=10, completion_tokens=1, wall_ms=100)
    # Backdate 3h using the REAL stored format (ISO8601 with a 'T' separator, as
    # utcnow() writes) — NOT SQLite's space-separated datetime(). A naive
    # `ts >= datetime('now',?)` string compare would wrongly KEEP this row in a
    # 1h window because 'T' > ' ', so this guards the same-day window boundary.
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=3)).isoformat()
    db.execute("UPDATE perf_samples SET ts=?", (old,))
    assert ph.query_history(db, hours=1)["summary"]["samples"] == 0    # 3h > 1h window
    assert ph.query_history(db, hours=6)["summary"]["samples"] == 1    # within 6h
