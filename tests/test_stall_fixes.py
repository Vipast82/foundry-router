"""v0.181.125: the freezes the stall watchdog named on the live router, fixed at their sources.

What it reported on 2026-11-11 (v0.181.124, the first build with the watchdog):

* 128.7 s at startup, the loop waiting for the database lock: a retention DELETE in a
  worker thread was reading a whole table with the lock held;
* about 5 s every couple of minutes: a telemetry batch from the coordinator, redacted and
  written on the event loop inside one transaction;
* 49.2 s when the Training tab was opened: the capture store's counts query;
* 4 s when the Data tab was opened: counting every table.
"""

import sys
import threading
import time

from foundry_router import db as dbmod
from foundry_router import loopwatch
from foundry_router.control_center import CONTRACT
from foundry_router.config import ClientConfig, TrainingCaptureConfig
from foundry_router.db import Database

COORD_KEY = "coord-key-123456789"
COORD = {"Authorization": f"Bearer {COORD_KEY}"}


def _log_table(db, n, old):
    db.execute("CREATE TABLE t_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, v TEXT)")
    db.executemany("INSERT INTO t_log (ts, v) VALUES (?,?)",
                   [((100.0 if i < old else 900.0), f"row {i}") for i in range(n)])


def test_a_delete_in_steps_removes_the_same_rows_as_one_delete(tmp_path):
    db = Database(tmp_path / "a.sqlite")
    _log_table(db, 1000, old=350)
    statements = []
    real = db.execute_count
    db.execute_count = lambda sql, params=(): statements.append(sql) or real(sql, params)
    assert db.delete_in_steps("t_log", "ts < ?", (500.0,), step=100) == 350
    assert db.query_one("SELECT COUNT(*) AS n, MIN(ts) AS oldest FROM t_log") == {"n": 650, "oldest": 900.0}
    # the whole table was walked, a slice of 100 rowids at a time
    assert len(statements) == 10 and all("rowid >= ? AND rowid < ?" in s for s in statements)


def test_an_ordered_delete_stops_at_the_first_slice_with_nothing_old(tmp_path):
    """An hourly prune of a log reads its old end, not the whole table."""
    db = Database(tmp_path / "a.sqlite")
    _log_table(db, 1000, old=250)
    statements = []
    real = db.execute_count
    db.execute_count = lambda sql, params=(): statements.append(sql) or real(sql, params)
    assert db.delete_in_steps("t_log", "ts < ?", (500.0,), ordered=True, step=100) == 250
    assert len(statements) == 4                      # 3 slices with old rows, 1 that had none
    statements.clear()
    assert db.delete_in_steps("t_log", "ts < ?", (500.0,), ordered=True, step=100) == 0
    assert len(statements) == 1                      # nothing old: one slice read
    # a gap in the rowids (an earlier prune) is not mistaken for "nothing old"
    db.execute("DELETE FROM t_log WHERE id BETWEEN 300 AND 520")
    db.execute("UPDATE t_log SET ts = 100.0 WHERE id BETWEEN 521 AND 530")
    db.execute("UPDATE t_log SET ts = 100.0 WHERE id BETWEEN 251 AND 299")
    assert db.delete_in_steps("t_log", "ts < ?", (500.0,), ordered=True, step=100) == 59


def test_an_empty_table_is_left_alone(tmp_path):
    db = Database(tmp_path / "a.sqlite")
    db.execute("CREATE TABLE t_log (id INTEGER PRIMARY KEY, ts REAL)")
    assert db.delete_in_steps("t_log", "ts < ?", (1.0,)) == 0


def test_retention_prunes_go_through_the_stepped_delete(app):
    """Every prune that runs on the hourly timers uses delete_in_steps (one DELETE over a
    large table held the lock for two minutes)."""
    svc = app.state.services
    seen = []
    real = svc.db.delete_in_steps

    def spy(table, where, params=(), **kw):
        seen.append((table, bool(kw.get("ordered"))))
        return real(table, where, params, **kw)
    svc.db.delete_in_steps = spy
    from foundry_router import datatools, perf_history, server_history
    svc.db.execute("INSERT INTO event_log (ts, level, source, message, detail) VALUES "
                   "('2020-01-01T00:00:00+00:00', 'info', 'test', 'ancient', '')")
    assert datatools.prune(svc.db, {"event_log": 30}) == {"event_log": 1}
    perf_history.prune(svc.db, 14)
    server_history.prune(svc.db, 14)
    svc.ledger.prune()
    svc.work.prune(90)
    svc.control.prune()
    tables = dict(seen)
    assert tables["event_log"] and tables["perf_samples"] and tables["server_samples"]
    assert tables["cc_parts"] and tables["cc_feed"] and tables["cc_coord_events"]
    assert tables["cc_sessions"] is False and tables["usage_events"] is False
    assert "work_events" in tables and "cc_batches" in tables
    import inspect
    from foundry_router import control_center
    src = inspect.getsource(control_center.ControlCenter.prune)
    assert src.count("execute_count") == 1 and "cc_snapshots" in src   # only the keep-the-newest one


def test_a_call_that_holds_the_lock_for_long_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "SLOW_CALL_SECONDS", 0.15)
    db = Database(tmp_path / "a.sqlite")
    db.query("SELECT 1")
    assert not db.slow_calls                         # a fast call leaves no record
    db._conn.create_function("nap", 1, lambda s: time.sleep(s) or 1)
    got = []
    waiter = threading.Thread(target=lambda: got.append(db.query("SELECT 2 AS x")), name="waiter")
    holder = threading.Thread(target=lambda: db.query("SELECT   nap(0.5)\n AS slept"), name="holder")
    holder.start()
    time.sleep(0.1)
    waiter.start()
    holder.join()
    waiter.join()
    assert got == [[{"x": 2}]]
    by = {c["thread"]: c for c in db.slow_calls}
    assert by["holder"]["held"] >= 0.4 and by["holder"]["what"] == "SELECT nap(0.5) AS slept"
    assert by["waiter"]["waited"] >= 0.2 and by["waiter"]["held"] < 0.15


def test_a_slow_read_on_the_second_connection_does_not_hold_the_lock(tmp_path):
    db = Database(tmp_path / "a.sqlite")
    db.execute("CREATE TABLE t (v TEXT)")
    db.execute("INSERT INTO t VALUES ('one')")
    assert db.ro_query("SELECT v FROM t") == [{"v": "one"}]
    db._ro.create_function("nap", 1, lambda s: time.sleep(s) or 1)
    reader = threading.Thread(target=lambda: db.ro_query("SELECT nap(0.5)"))
    reader.start()
    time.sleep(0.1)
    t0 = time.monotonic()
    db.execute("INSERT INTO t VALUES ('two')")       # the request path: not kept waiting
    assert time.monotonic() - t0 < 0.3
    reader.join()
    assert [r["v"] for r in db.ro_query("SELECT v FROM t ORDER BY rowid")] == ["one", "two"]
    try:                                             # it is read-only
        db.ro_query("INSERT INTO t VALUES ('three')")
        raise AssertionError("the second connection accepted a write")
    except Exception as e:                           # noqa: BLE001
        assert "readonly" in str(e).lower() or "read-only" in str(e).lower()
    db.close()
    assert Database(":memory:").ro_query("SELECT 1 AS x") == [{"x": 1}]


def _batch(*obs):
    return {"contract": CONTRACT, "source": {"id": "bridge:test", "kind": "bridge"},
            "sent_ts": time.time(), "observations": list(obs)}


def test_a_telemetry_batch_is_redacted_before_the_transaction_and_off_the_loop(app, client):
    svc = app.state.services
    svc.config_store.config.clients.append(
        ClientConfig(name="coordinator", key=COORD_KEY, scopes=["inference", "orchestration"]))
    cc = svc.control
    order, threads = [], []
    red = cc._redactor()
    real_obj, real_tx = type(red).obj, cc._tx

    def obj(self, o, *a, **k):
        order.append("redact")
        threads.append(threading.current_thread())
        assert not svc.db._lock.locked()             # the lock is not held while regexes run
        return real_obj(self, o, *a, **k)

    def tx():
        order.append("tx")
        return real_tx()
    type(red).obj = obj
    cc._tx = tx
    try:
        r = client.post("/v1/foundry/telemetry/batch", headers=COORD, json=_batch(
            {"type": "task", "project_id": "proj", "task_id": "T-1", "state": "IMPLEMENTING",
             "session_id": "ses_1", "run_id": "T-1-a1", "attempt": 1, "phase": "implement",
             "blocker": "password=hunter2hunter2"},
            {"type": "task", "project_id": "proj", "task_id": "T-2", "state": "QUEUED"},
            {"type": "no-such-type", "project_id": "proj"}))
    finally:
        type(red).obj = real_obj
        cc._tx = real_tx
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == {"task": 2} and r.json()["skipped"] == {"unknown:no-such-type": 1}
    assert order == ["redact", "redact", "tx"]
    assert all(t is not threading.main_thread() for t in threads)
    stored = svc.db.query_one("SELECT data FROM cc_tasks WHERE task_id='T-1'")["data"]
    assert "hunter2hunter2" not in stored


def test_the_training_page_never_waits_for_a_long_count(app):
    svc = app.state.services
    svc.config_store.config.training_capture = TrainingCaptureConfig(enabled=True)
    cap = svc.capture
    cap.store.insert({"record_uid": "u1", "ts": time.time(), "kind": "backend_call",
                      "project_id": "game", "body": "{}", "redaction": "{}", "truncated": 0})
    # a small store is counted while the page waits (a second and a half at most)
    st = cap.status(wait=False)
    assert st["counts"] == [{"project_id": "game", "kind": "backend_call", "n": 1, "truncated": 0,
                             "redacted": 0, "last_ts": st["counts"][0]["last_ts"]}]
    assert st["counts_pending"] is False and st["counts_at"]
    # a large store: the count takes long, the page gets the last counts at once
    cap._counts["took"] = 60.0
    real = cap._count
    started = []
    cap._count = lambda: started.append(1) or time.sleep(0.4) or real()
    t0 = time.monotonic()
    st = cap.status(wait=False)
    assert time.monotonic() - t0 < 0.2 and not started and st["counts"][0]["n"] == 1
    cap._counts["at"] -= cap.COUNTS_FRESH_SECONDS + 5      # older than five minutes: recount
    cap.store.insert({"record_uid": "u2", "ts": time.time(), "kind": "backend_call",
                      "project_id": "game", "body": "{}", "redaction": "{}", "truncated": 1})
    cap._counter = None
    cap._count_in_background().join(5)
    assert started == [1]
    assert cap.status(wait=False)["counts"][0]["n"] == 2
    # inserts never wait for a count: it reads on its own connection
    assert cap.store._ro is not None and cap.store._ro is not cap.store.conn


def test_capture_prune_deletes_in_small_batches(app):
    svc = app.state.services
    svc.config_store.config.training_capture = TrainingCaptureConfig(enabled=True)
    store = svc.capture.store
    old, new = time.time() - 40 * 86400, time.time()
    for i in range(450):
        store.insert({"record_uid": f"old{i}", "ts": old, "kind": "backend_call", "body": "{}"})
    for i in range(30):
        store.insert({"record_uid": f"new{i}", "ts": new, "kind": "backend_call", "body": "{}"})
    assert store.prune(30) == 450 and store.prune(30) == 0
    assert store.query("SELECT COUNT(*) AS n FROM capture_records") == [{"n": 30}]
    assert store.prune(0) == 0


def test_the_slow_admin_reads_run_off_the_event_loop(app, client):
    import inspect
    from foundry_router.ui import routes
    assert "asyncio.to_thread(datatools.stats" in inspect.getsource(routes.data_tables)
    assert "asyncio.to_thread(svc.capture.status, False)" in inspect.getsource(routes.training_view)
    from foundry_router import control_api, datatools
    assert "asyncio.to_thread(cc.ingest" in inspect.getsource(control_api.telemetry_batch)
    assert "ro_query" in inspect.getsource(datatools.stats)
    d = client.get("/admin/api/data/tables").json()
    assert {t["table"] for t in d["tables"]} >= {"request_log", "event_log"}
    assert client.get("/admin/api/training").status_code == 200
    s = client.get("/admin/api/health/stalls").json()
    assert s["slow_db"] == [] and s["stalls"] == []


def test_a_stall_report_names_the_thread_that_holds_the_lock(tmp_path):
    db = Database(tmp_path / "a.sqlite")
    db._lock.acquire()
    t = threading.Thread(target=lambda: db.query("SELECT 1"), name="prune-worker")
    t.start()
    time.sleep(0.15)                                  # it is now waiting inside db.query
    try:
        others = loopwatch._others(sys._current_frames(), threading.get_ident())
    finally:
        db._lock.release()
        t.join()
    assert any(o.startswith("prune-worker: query (db.py:") for o in others), others
    # the stall event carries them, and the database calls that held the lock meanwhile
    lw = loopwatch.LoopWatch()
    lw._report({"wall": time.time(), "since": 0, "where": "execute (db.py:1)", "stack": "S",
                "others": others}, 3.0)
    assert lw.stalls[0]["others"] == others


def test_the_stall_event_lists_other_threads_and_slow_database_calls(app):
    svc = app.state.services
    svc.db.slow_calls.append({"at": time.time(), "held": 12.5, "waited": 0.0,
                              "what": "DELETE FROM cc_parts WHERE received_ts < ?",
                              "thread": "asyncio_0"})
    svc.loopwatch.on_stall({"at": time.time() - 1, "seconds": 12.6, "where": "execute (db.py:900)",
                            "stack": "STACK", "others": ["asyncio_0: prune (control_center.py:4215)"]})
    e = svc.db.query_one("SELECT message, detail FROM event_log WHERE source='stall'")
    assert e["message"] == "router stalled for 12.6 s in execute (db.py:900)"
    assert "other threads: asyncio_0: prune (control_center.py:4215)" in e["detail"]
    assert "database lock: asyncio_0 held 12.5 s, waited 0.0 s: DELETE FROM cc_parts" in e["detail"]
    assert e["detail"].rstrip().endswith("STACK")
