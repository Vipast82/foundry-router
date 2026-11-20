"""v0.181.212: the cache hits the ceiling missed, and the profile a restart left
"running".

Probing the brain lane directly showed its server reports timings.prompt_n as
the whole prompt even when it reused its KV cache, and sends no cache_n: the
same 3,428-token prompt twice gave 943 t/s cold and 44,869 t/s cached, under
v0.181.211's 50,000 t/s ceiling. A rate is now also judged against what that
model on that backend has measured.

And a profile whose thread died in a restart said "running" for ever with no
score, though five of its six suites had finished.
"""
import json

import pytest

from foundry_router import benchcard, benchprofile as bp, perf_history as ph
from foundry_router.db import Database


@pytest.fixture(autouse=True)
def _fresh():
    ph.reset_cold_history()
    yield
    ph.reset_cold_history()


def _rec(db, tps_tokens, ms, model="Qwen3.6-35B", backend="brain-lane", cached=0):
    ph.record_sample(db, model=model, backend=backend, prompt_tokens=tps_tokens,
                     completion_tokens=8, eval_duration_ns=200_000_000,
                     prompt_eval_duration_ns=int(ms * 1e6), prefill_tokens=tps_tokens,
                     cached_tokens=cached, timing_source="server", run_label="t")
    return db.query("SELECT prefill_tps, prefill_ms FROM perf_samples "
                    "ORDER BY id DESC LIMIT 1")[0]


def _cold(db, n=10, **k):
    for i in range(n):
        _rec(db, 3428, 3633.3 + i, **k)        # ~943 t/s, as measured


def test_the_probe_that_found_it(tmp_path):
    """The two real calls: 3,428 tokens in 3,633 ms, then in 76 ms."""
    db = Database(tmp_path / "p.sqlite")
    _cold(db)
    cold = _rec(db, 3428, 3633.3)
    hit = _rec(db, 3428, 76.4)
    assert 900 < cold["prefill_tps"] < 1000
    assert hit["prefill_tps"] is None, "44,869 t/s is a cache hit, not a speed"
    assert hit["prefill_ms"] == 76.4, "the duration is still kept"


def test_nothing_is_judged_without_history(tmp_path):
    db = Database(tmp_path / "p.sqlite")
    _cold(db, n=3)
    assert _rec(db, 3428, 76.4)["prefill_tps"] is not None


def test_a_backend_that_reports_its_cache_is_believed(tmp_path):
    """Its prefill count already leaves the cache out, so its rate is honest."""
    db = Database(tmp_path / "p.sqlite")
    _cold(db, backend="strata", model="flash")
    assert _rec(db, 3428, 76.4, backend="strata", model="flash",
                cached=3000)["prefill_tps"] is not None


def test_another_model_on_the_same_backend_has_its_own_history(tmp_path):
    db = Database(tmp_path / "p.sqlite")
    _cold(db)
    # a small fast model swapped onto the lane is not a cache hit
    assert _rec(db, 3428, 300.0, model="tiny-4b")["prefill_tps"] is not None


def test_a_sustained_change_is_accepted_not_refused_for_ever(tmp_path):
    """Eight in a row far above the history is new hardware, not eight hits."""
    db = Database(tmp_path / "p.sqlite")
    _cold(db)
    got = [_rec(db, 3428, 500.0)["prefill_tps"] for _ in range(10)]
    assert got[:7] == [None] * 7
    assert all(x is not None for x in got[7:]), got


def test_the_history_survives_a_restart(tmp_path):
    db = Database(tmp_path / "p.sqlite")
    _cold(db)
    ph.reset_cold_history()                   # what a restart does to memory
    assert _rec(db, 3428, 76.4)["prefill_tps"] is None


def test_short_prompts_are_never_judged(tmp_path):
    db = Database(tmp_path / "p.sqlite")
    _cold(db)
    assert _rec(db, 200, 5.0)["prefill_tps"] is not None


def test_one_database_does_not_judge_another(tmp_path):
    a = Database(tmp_path / "a.sqlite")
    b = Database(tmp_path / "b.sqlite")
    _cold(a)
    assert _rec(b, 3428, 76.4)["prefill_tps"] is not None


def _s(tps, tokens=3428, cached=0):
    return {"prompt_tokens": tokens, "prefill_tokens": tokens, "cached_tokens": cached,
            "prefill_tps": tps, "decode_tps": 40.0, "eff_tps": 30.0, "prefill_ms": 1.0,
            "decode_ms": 1.0, "wall_ms": 100, "completion_tokens": 8,
            "spec_accept_pct": 95.0, "timing_src": "server", "backend": "brain-lane"}


def test_the_card_leaves_out_the_smaller_hits_already_recorded():
    ss = [_s(900.0 + i) for i in range(10)] + [_s(44869.1)]
    sp = benchcard.speed(ss)
    assert sp["unreported_cache"] == 1
    assert 900 <= sp["prefill_tps"] < 1000


# -- a profile a restart cut off ---------------------------------------------------

def _svc(tmp_path):
    cfg = type("C", (), {"bench": type("B", (), {"suite_weights": {}})()})()
    return type("S", (), {"db": Database(tmp_path / "b.sqlite"),
                          "config_store": type("CS", (), {"config": cfg})()})()


def test_a_profile_a_restart_cut_off_keeps_what_it_finished(tmp_path):
    svc = _svc(tmp_path)
    db = svc.db
    db.execute("INSERT INTO bench_profiles (id, ts, model, label, suites, repeats, status) "
               "VALUES (2, datetime('now'), 'brain', '', ?, 1, 'running')",
               (json.dumps(["baseline", "needle", "agent"]),))
    for rid, suite, score in ((47, "baseline", 62.9), (49, "needle", 97.8)):
        db.execute("INSERT INTO bench_runs (id, ts, suite, model, label, cases, scored, "
                   "score, seconds, status, profile_id) VALUES (?, datetime('now'), ?, "
                   "'brain', '', 4, 4, ?, 2.0, 'done', 2)", (rid, suite, score))
    assert bp.reconcile(svc) == 1
    row = db.query_one("SELECT status, score, note FROM bench_profiles WHERE id=2")
    assert row["status"] == "interrupted"
    assert row["score"] == round((62.9 + 97.8) / 2, 1)
    assert "not run: agent" in row["note"]
    card = bp.build_card(svc, 2)
    assert "restarted" in card["note"]
    assert bp.reconcile(svc) == 0, "a second start finds nothing to do"


def test_the_router_reconciles_profiles_at_startup():
    import inspect

    from foundry_router import main
    src = inspect.getsource(main)
    assert "benchprofile.reconcile(self)" in src
