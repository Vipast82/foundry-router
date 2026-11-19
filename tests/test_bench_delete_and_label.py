"""v0.181.198: removing old benchmark runs, and filing each run's perf samples
under its own label.

Thirty runs had accumulated with no way to remove one, and the only run against
the brain lane showed 0 per-call samples on the Performance page because the
benchmark recorded none until v0.181.196 - and even then they were filed under
whatever global run label happened to be set, so they could not be read as one
benchmark.
"""
import pytest

from foundry_router import bench
from foundry_router.db import Database


def _svc(tmp_path, name="b.sqlite"):
    db = Database(tmp_path / name)
    return type("S", (), {"db": db})(), db


def _run(db, model="m", label="", status="done", cases=2):
    db.execute("INSERT INTO bench_runs (ts, suite, model, label, cases, scored, "
               "status) VALUES (datetime('now'),'baseline',?,?,?,?,?)",
               (model, label, cases, cases, status))
    return db.query_one("SELECT MAX(id) AS id FROM bench_runs")["id"]


def _case(db, run_id, case_id="C1"):
    db.execute("INSERT INTO bench_results (run_id, case_id, score) VALUES (?,?,1.0)",
               (run_id, case_id))


# -- the label ---------------------------------------------------------------------

def test_a_run_label_names_the_run_and_the_model():
    assert bench.run_label(40, "Qwen3.6-35B") == "bench40 Qwen3.6-35B"
    assert bench.run_label(41, "Qwen3.6-35B", "lessons-on") == \
        "bench41 Qwen3.6-35B lessons-on"


def test_the_label_survives_odd_input():
    """It goes into a database column and a dropdown; it must not be empty or
    unbounded."""
    assert bench.run_label(None, "") == "bench"
    long = bench.run_label(1, "x" * 200, "y" * 200)
    assert len(long) < 80 and long.startswith("bench1 ")


def test_the_bench_passes_its_own_label_rather_than_setting_the_global_one():
    """Setting the global label for the duration would file every other request
    the router served meanwhile under this benchmark - a sprint running beside
    it included."""
    import inspect
    src = inspect.getsource(bench)
    assert 'extra={"run_label": label}' in src
    assert "set_run_label" not in src, "the global label must not be touched"


def test_telemetry_lets_an_explicit_run_label_through():
    """record_sample always took one; record_call's whitelist dropped it, so the
    bench's label never reached the row."""
    import inspect

    from foundry_router import telemetry
    src = inspect.getsource(telemetry.record_call)
    i = src.index("(extra or {}).items()")
    assert "run_label" in src[i:i + 400]


# -- deleting ----------------------------------------------------------------------

def test_deleting_a_run_takes_its_cases(tmp_path):
    svc, db = _svc(tmp_path)
    keep, drop = _run(db), _run(db)
    _case(db, keep, "K1")
    _case(db, drop, "D1")
    _case(db, drop, "D2")
    out = bench.delete_runs(svc, [drop])
    assert out["deleted"] == 1 and out["cases"] == 2
    assert db.query_one("SELECT COUNT(*) AS n FROM bench_runs")["n"] == 1
    left = db.query("SELECT case_id FROM bench_results")
    assert [r["case_id"] for r in left] == ["K1"]


def test_deleting_takes_the_perf_samples_that_run_recorded(tmp_path):
    """Or the Performance page keeps offering a run whose scores are gone."""
    svc, db = _svc(tmp_path, "perf.sqlite")
    rid = _run(db, model="Qwen3.6-35B", label="try1")
    lbl = bench.run_label(rid, "Qwen3.6-35B", "try1")
    for _ in range(3):
        db.execute("INSERT INTO perf_samples (ts, model, backend, run_label) "
                   "VALUES (0, 'Qwen3.6-35B', 'brain-lane', ?)", (lbl,))
    db.execute("INSERT INTO perf_samples (ts, model, backend, run_label) "
               "VALUES (0, 'other', 'strata', 'Strata27_Qwen3.8_Unsloth')")
    out = bench.delete_runs(svc, [rid])
    assert out["samples"] == 3
    rows = db.query("SELECT run_label FROM perf_samples")
    assert [r["run_label"] for r in rows] == ["Strata27_Qwen3.8_Unsloth"], \
        "another run's samples must survive"


def test_the_samples_can_be_kept(tmp_path):
    svc, db = _svc(tmp_path, "keep.sqlite")
    rid = _run(db, model="M")
    db.execute("INSERT INTO perf_samples (ts, model, backend, run_label) "
               "VALUES (0,'M','b',?)", (bench.run_label(rid, "M"),))
    out = bench.delete_runs(svc, [rid], drop_samples=False)
    assert out["deleted"] == 1 and out["samples"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM perf_samples")["n"] == 1


def test_the_running_benchmark_is_refused_not_skipped_quietly(tmp_path):
    """Deleting the rows a run is still writing would leave it half gone."""
    svc, db = _svc(tmp_path, "live.sqlite")
    rid = _run(db, status="running")
    bench._state.update({"state": "running", "run_id": rid})
    try:
        out = bench.delete_runs(svc, [rid])
    finally:
        bench._state.update({"state": "idle", "run_id": None})
    assert out["refused"] == [rid] and out["deleted"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM bench_runs")["n"] == 1


def test_a_finished_run_is_deletable_even_while_another_is_running(tmp_path):
    svc, db = _svc(tmp_path, "mixed.sqlite")
    old = _run(db)
    live = _run(db, status="running")
    bench._state.update({"state": "running", "run_id": live})
    try:
        out = bench.delete_runs(svc, [old])
    finally:
        bench._state.update({"state": "idle", "run_id": None})
    assert out["deleted"] == 1 and not out["refused"]


def test_an_id_that_is_not_there_is_reported_rather_than_counted(tmp_path):
    svc, _db = _svc(tmp_path, "missing.sqlite")
    out = bench.delete_runs(svc, [999])
    assert out["missing"] == [999] and out["deleted"] == 0


@pytest.mark.parametrize("ids", [[], None, ["", None], ["abc"]])
def test_nothing_is_deleted_without_an_id(tmp_path, ids):
    """A delete-all by accident is the thing this must never do."""
    svc, db = _svc(tmp_path, f"none{abs(hash(str(ids)))}.sqlite")
    _run(db)
    out = bench.delete_runs(svc, ids)
    assert out["deleted"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM bench_runs")["n"] == 1


def test_the_ui_asks_before_deleting_and_names_the_runs():
    """"Delete 3 runs?" cannot tell a baseline from a smoke test.

    Anchored on the handler rather than on a byte offset from the button: the
    first version read a fixed window after the markup, and adding another
    button between the two broke it while the confirmation had not moved."""
    html = open("foundry_router/ui/static/index.html", encoding="utf-8").read()
    i = html.index('$("#bench-delete").onclick')
    body = html[i:i + 1200]
    assert "confirm(" in body, "deleting runs must ask first"
    assert "cannot be undone" in body
    assert "dataset.what" in body          # the model and suite, per run
    assert "bench/delete" in body
    # and the button itself exists to be wired to
    assert 'id="bench-delete"' in html
