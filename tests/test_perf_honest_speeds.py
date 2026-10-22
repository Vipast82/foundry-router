"""Honest speeds (W4): measured (server timing) and estimated samples are aggregated
apart, never averaged together; the run label stamps local backends only; a flat
per-call allocation for subscription plans comes from config (default 0)."""

from foundry_router import perf_history as ph
from foundry_router import usage
from foundry_router.db import Database


def _db(tmp_path):
    return Database(tmp_path / "h.sqlite")


def _mixed(db):
    for _ in range(4):                                # Strata: llama.cpp timings, 50 tok/s
        ph.record_sample(db, model="qwen", backend="strata", prompt_tokens=1000,
                         completion_tokens=100, eval_duration_ns=2_000_000_000,
                         prompt_eval_duration_ns=500_000_000, ttft_ms=600, wall_ms=3000,
                         timing_source="server")
    for _ in range(4):                                # Claude: tokens / stream time = an artifact
        ph.record_sample(db, model="claude-sonnet-5-5", backend="meridian", prompt_tokens=1000,
                         completion_tokens=100, eval_duration_ns=100_000_000,
                         ttft_ms=300, wall_ms=9000, timing_source="estimated")


def test_headline_speeds_are_measured_only(tmp_path):
    db = _db(tmp_path)
    _mixed(db)
    h = ph.query_history(db, hours=72)
    s = h["summary"]
    assert s["decode_tps"] == 50.0                    # not the 500 / 1000 blend with Claude's estimate
    assert s["ttft_ms"] == 600.0 and s["prefill_tps"] == 2000.0
    assert s["samples"] == 8 and s["measured"] == 4 and s["estimated"] == 4
    assert s["by_timing"]["estimated"]["decode_tps"] == 1000.0     # kept, labelled apart
    assert s["by_timing"]["measured"]["n"] == 4
    assert s["eff_tps"] is not None                   # tokens / wall time is measured for everyone
    by = {g["model"]: g for g in h["by_model"]}
    q, c = by["qwen"], by["claude-sonnet-5-5"]
    assert q["decode_tps"] == 50.0 and q["p50_decode_tps"] == 50.0 and q["estimated_speed"] is None
    assert c["decode_tps"] is None and c["p50_decode_tps"] is None and c["p95_ttft_ms"] is None
    assert c["estimated_speed"]["n"] == 4 and c["estimated_speed"]["decode_tps"] == 1000.0
    assert c["estimated_speed"]["ttft_ms"] == 300.0 and c["measured"] == 0
    assert c["eff_tps"] == 11.11                      # 100 tokens / 9 s, honest for every backend


def test_unlabelled_unknown_timing_is_its_own_bucket(tmp_path):
    db = _db(tmp_path)
    ph.record_sample(db, model="m", backend="b", prompt_tokens=10, completion_tokens=5,
                     wall_ms=1000)                    # no timing source at all
    g = ph.query_history(db, hours=72)["by_model"][0]
    assert g["decode_tps"] is None and g["unmeasured_speed"]["n"] == 1
    assert g["unmeasured_speed"]["eff_tps"] == 5.0


def test_run_label_applies_to_local_backends_only(tmp_path):
    db = _db(tmp_path)
    ph.set_run_label(db, "2x4060ti-16gb")
    ph.configure(backend_kind=lambda n: {"strata": "local", "meridian": "subscription"}.get(n))
    try:
        for b in ("strata", "meridian", "unknown-backend"):
            ph.record_sample(db, model="m", backend=b, prompt_tokens=1)
        labels = {r["backend"]: r["run_label"] for r in
                  db.query("SELECT backend, run_label FROM perf_samples")}
        assert labels == {"strata": "2x4060ti-16gb", "meridian": "",
                          "unknown-backend": "2x4060ti-16gb"}      # unknown keeps today's behaviour
        ph.record_sample(db, model="m", backend="meridian", prompt_tokens=1, run_label="forced")
        assert db.query("SELECT run_label FROM perf_samples ORDER BY id DESC LIMIT 1")[0][
            "run_label"] == "forced"                                # an explicit label is respected
    finally:
        ph.configure(backend_kind=lambda n: None)


def test_perf_samples_carry_project_task_run_role(tmp_path):
    from foundry_router import client_auth
    db = _db(tmp_path)
    tok = client_auth._client.set({"client_id": "kilo", "project_id": "game",
                                   "work_task_id": "T1", "run_id": "T1-a1", "role": "implementer"})
    try:
        ph.record_sample(db, model="m", backend="strata", prompt_tokens=1)
    finally:
        client_auth._client.reset(tok)
    r = db.query("SELECT project_id, work_task_id, run_id, role, client_id FROM perf_samples")[0]
    assert (r["project_id"], r["work_task_id"], r["run_id"], r["role"]) == (
        "game", "T1", "T1-a1", "implementer")
    ph.record_sample(db, model="m", prompt_tokens=1)
    assert db.query("SELECT project_id FROM perf_samples ORDER BY id DESC LIMIT 1")[0][
        "project_id"] is None                                       # unbound: NULL, not ''


def test_subscription_rate_per_call_defaults_to_zero():
    usage.set_subscription_rates(lambda: [])
    log = usage.RequestLogger.__new__(usage.RequestLogger)
    log.models_used, log.est_cost = [], 0.0
    log.record_model_call("claude-sonnet-5-5", "meridian", 100, 10, 0.0)
    assert log.models_used[0].get("sub_cost_usd") is None and log.est_cost == 0.0
    usage.set_subscription_rates(lambda: [
        {"match": "meridian", "model": "claude-opus*", "usd_per_call": 0.20},
        {"match": "merid*", "usd_per_call": 0.04},
        {"match": "cliproxy*", "usd_per_call": 0.01}])
    try:
        log.record_model_call("claude-opus-5-5", "meridian", 100, 10, 0.0)
        log.record_model_call("claude-sonnet-5-5", "meridian", 100, 10, 0.0)
        log.record_model_call("gpt-6.1-sol", "cliproxy", 100, 10, 0.0)
        log.record_model_call("qwen", "strata", 100, 10, 0.0)
        log.record_model_call("paid-model", "meridian", 100, 10, 0.5)       # priced: left alone
        got = [m.get("sub_cost_usd") for m in log.models_used[1:]]
        assert got == [0.2, 0.04, 0.01, None, None]
        assert log.est_cost == 0.5                  # the allocation never feeds the dollar caps
    finally:
        usage.set_subscription_rates(lambda: [])
