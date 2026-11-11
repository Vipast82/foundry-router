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
    assert "execute_count" not in src and "self._prune_snapshots(cut)" in src


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


# ---- v0.181.126: what v0.181.125's own records showed on the live router ----------------------

def _snap(db, kind, session, received, confirmed, scope="session"):
    return db.execute("INSERT INTO cc_snapshots (kind, project_id, session_id, scope, hash, data, "
                      "source_ts, received_ts, confirmed_ts) VALUES (?,?,?,?,?,?,?,?,?)",
                      (kind, "proj", session, scope, f"h{received}", "{}", confirmed, received,
                       confirmed))


def test_old_snapshots_are_pruned_without_reading_every_snapshot(app):
    """One DELETE with a NOT IN subquery held the lock for 92.6 s (confirmed_ts is stored
    after the snapshot body). Same rows go; the statements are small and by id."""
    cc = app.state.services.control
    db = cc.db
    now = cc.clock()
    cut = now - cc.settings()["retention_days"] * 86400
    old, new = cut - 5000, now - 60
    a1 = _snap(db, "todo", "ses_a", old, old)                 # old, superseded: goes
    a2 = _snap(db, "todo", "ses_a", old + 10, old + 10)       # old, superseded: goes
    b1 = _snap(db, "todo", "ses_b", old + 20, old + 20)       # old but the newest of its kind: stays
    c1 = _snap(db, "diff", "ses_a", old + 30, new)            # received long ago, confirmed today: stays
    w1 = _snap(db, "todo", "ses_a", old + 40, old + 40, scope="workspace")   # other scope, its newest
    a3 = _snap(db, "todo", "ses_a", new, new)                 # recent
    c2 = _snap(db, "diff", "ses_a", new + 1, new + 1)         # recent
    statements = []
    real = db.execute_count
    db.execute_count = lambda sql, params=(): statements.append(sql) or real(sql, params)
    try:
        assert cc._prune_snapshots(cut) == 2
    finally:
        db.execute_count = real
    left = {r["id"] for r in db.query("SELECT id FROM cc_snapshots")}
    assert left == {b1, c1, a3, c2, w1} and a1 not in left and a2 not in left
    assert len(statements) == 1 and "id IN (" in statements[0] and "NOT IN" not in statements[0]
    assert cc._prune_snapshots(cut) == 0
    db.execute("DELETE FROM cc_snapshots")
    assert cc._prune_snapshots(cut) == 0                      # an empty table
    assert cc.prune() >= 0


def test_the_ends_of_a_table_are_asked_for_one_at_a_time(tmp_path):
    """SELECT MIN(rowid), MAX(rowid) reads the whole table (1.1 s on cc_parts)."""
    db = Database(tmp_path / "a.sqlite")
    _log_table(db, 50, old=10)
    asked = []
    real = db.query_one
    db.query_one = lambda sql, params=(): asked.append(sql) or real(sql, params)
    assert db.delete_in_steps("t_log", "ts < ?", (500.0,), ordered=True) == 10
    ends = [s for s in asked if "MIN(" in s or "MAX(" in s]
    assert len(ends) == 2 and not any("MIN(" in s and "MAX(" in s for s in ends)


def test_a_reader_reads_on_the_second_connection(tmp_path):
    db = Database(tmp_path / "a.sqlite")
    db.kv_set("k", "v")
    r = db.reader()
    assert r.query_one("SELECT value FROM kv WHERE key='k'") == {"value": "v"} and r.kv_get("k") == "v"
    assert r.kv_get("missing") is None and r.query("SELECT 1 AS x WHERE 0") == []
    assert db._ro is not None and r.path == db.path           # anything else is the database itself
    with db._lock:                                            # the request path's lock is not needed
        assert r.query_one("SELECT COUNT(*) AS n FROM kv")["n"] >= 1


def test_the_charts_are_read_off_the_event_loop(app, client):
    import inspect
    from foundry_router.ui import routes
    assert "asyncio.to_thread(server_history.query, svc.db.reader()" in inspect.getsource(routes.perf_servers)
    assert "asyncio.to_thread(ph.query_history, svc.db.reader()" in inspect.getsource(routes.perf_history_api)
    d = client.get("/admin/api/perf/servers?hours=1").json()
    assert d["backends"] == {} and "interval_s" in d
    h = client.get("/admin/api/perf-history?hours=1").json()
    assert "backends" in h


def test_backend_health_evidence_is_summed_by_the_database(app):
    svc = app.state.services
    now = time.time()

    def ev(backend, status, ago, model="m", detail=""):
        svc.db.execute("INSERT INTO usage_events (ts, backend, model, status, settled_ts, detail) "
                       "VALUES (?,?,?,?,?,?)", (now - ago, backend, model, status, now - ago + 1, detail))
    ev("strata", "settled", 7200, model="old-model")
    ev("strata", "error", 1800, detail="timeout")
    ev("strata", "settled", 600, model="new-model")
    ev("strata", "reserved", 10)                              # in flight: not evidence
    ev("meridian", "released", 300)
    ev("meridian", "settled", 90000)                          # older than the day asked for
    asked = []
    real = svc.db.query
    svc.db.query = lambda sql, params=(): asked.append(sql) or real(sql, params)
    try:
        out = svc.health._ledger_evidence(now - 86400)
    finally:
        svc.db.query = real
    assert out["strata"]["calls_1h"] == 2 and out["strata"]["errors_1h"] == 1
    assert out["strata"]["last_ok"] == {"at": out["strata"]["last_ok"]["at"], "model": "new-model",
                                        "source": "real call"}
    assert out["strata"]["last_error"]["detail"] == "timeout"
    assert out["meridian"] == {"last_ok": None, "calls_1h": 1, "errors_1h": 1,
                               "last_error": {"at": out["meridian"]["last_error"]["at"], "model": "m",
                                              "detail": "released"}}
    assert len(asked) == 2 and "GROUP BY backend" in asked[0]   # the day is never fetched row by row
    svc.db.execute("DELETE FROM usage_events")
    assert svc.health._ledger_evidence(now - 86400) == {}


# ---- v0.181.127 ---------------------------------------------------------------------------------

def test_plain_responses_skip_the_slow_encoder_and_other_types_still_convert(app, client):
    """3.2 s of event loop went into fastapi.encoders.jsonable_encoder for one large
    response. Plain dicts and lists pass through; anything else is encoded as before."""
    import datetime
    import enum
    from pathlib import Path

    from fastapi import routing

    from foundry_router import fastjson
    assert routing.jsonable_encoder.__wrapped__.__name__ == "jsonable_encoder"
    fastjson.install()                                        # a second install changes nothing
    assert routing.jsonable_encoder.__wrapped__.__name__ == "jsonable_encoder"
    big = {"rows": [{"id": i, "name": f"r{i}", "ok": True, "v": 1.5, "none": None, "t": [1, 2]}
                    for i in range(2000)]}
    assert routing.jsonable_encoder(big) is big               # untouched: no copy, no walk
    assert routing.jsonable_encoder([1, "a", {"k": (1, 2)}]) == [1, "a", {"k": (1, 2)}]

    class Colour(enum.Enum):
        RED = "red"
    when = datetime.datetime(2026, 11, 11, 15, 0, 0)
    mixed = {"when": when, "path": Path("a") / "b", "set": {1}, "colour": Colour.RED, "raw": b"x"}
    out = routing.jsonable_encoder(mixed)
    assert out is not mixed and out["when"] == "2026-11-11T15:00:00" and out["set"] == [1]
    assert out["colour"] == "red" and out["raw"] == "x" and out["path"].replace("\\", "/") == "a/b"
    assert fastjson.is_plain({"a": [1, {"b": None}]}) and not fastjson.is_plain({"a": when})
    assert not fastjson.is_plain({("t", 1): 1})               # a key JSON cannot carry
    # the filtering form (a response model) always goes to the real encoder
    assert routing.jsonable_encoder({"a": 1, "b": None}, exclude_none=True) == {"a": 1}

    @app.get("/_test/plain")
    async def plain():
        return {"n": 1, "rows": [{"x": None, "y": [1.5, "s"], 3: "int key"}]}

    @app.get("/_test/typed")
    async def typed():
        return {"when": when, "tags": {"a"}}
    assert client.get("/_test/plain").json() == {"n": 1, "rows": [{"x": None, "y": [1.5, "s"], "3": "int key"}]}
    assert client.get("/_test/typed").json() == {"when": "2026-11-11T15:00:00", "tags": ["a"]}
    assert client.get("/admin/api/models").status_code == 200


def test_a_stall_report_names_the_request_being_served():
    import asyncio

    seen = []

    async def app_call(scope):
        time.sleep(0.6)                                       # synchronous work while serving it

    async def run(monkey):
        lw = loopwatch.LoopWatch(on_stall=seen.append, threshold=0.25)
        lw.start()
        await asyncio.sleep(0.1)
        await app_call({"type": "http", "method": "GET", "path": "/admin/api/work"})
        await asyncio.sleep(0.15)
        lw.stop()
    old = loopwatch.CHECK_S, loopwatch.TICK_S
    loopwatch.CHECK_S, loopwatch.TICK_S = 0.05, 0.02
    try:
        asyncio.run(run(None))
    finally:
        loopwatch.CHECK_S, loopwatch.TICK_S = old
    assert len(seen) == 1 and seen[0]["request"] == "GET /admin/api/work"
    assert loopwatch._request(None) == ""


def test_a_stall_inside_a_library_keeps_the_foundry_frames_in_its_stack():
    import traceback as tb
    lib = [tb.FrameSummary("/usr/lib/python3.12/site-packages/fastapi/encoders.py", 289 + i,
                           "jsonable_encoder", line="x") for i in range(20)]
    ours = [tb.FrameSummary("/app/foundry_router/ui/routes.py", 1130, "work_view", line="return view()")]
    text = loopwatch._stack_text(ours + lib)
    assert text.index("work_view") < text.index("...") < text.index("jsonable_encoder")
    assert text.count("jsonable_encoder") == 14
    assert "..." not in loopwatch._stack_text(lib[:5] + ours)


def test_the_stall_event_names_the_request(app):
    svc = app.state.services
    svc.loopwatch.on_stall({"at": time.time(), "seconds": 3.2, "where": "__call__ (client_auth.py:256)",
                            "stack": "STACK", "others": [], "request": "GET /admin/api/work"})
    e = svc.db.query_one("SELECT message, detail FROM event_log WHERE source='stall'")
    assert e["message"] == "router stalled for 3.2 s in __call__ (client_auth.py:256) (GET /admin/api/work)"
    assert e["detail"].startswith("request: GET /admin/api/work")


def test_work_and_compare_are_read_off_the_event_loop_and_task_states_come_from_an_index(app, client):
    import inspect
    from foundry_router.ui import routes
    assert "asyncio.to_thread(scorecard.build, _svc(request).db.reader()" in inspect.getsource(routes.work_scorecard)
    assert "asyncio.to_thread(_svc(request).work.view" in inspect.getsource(routes.work_view)
    assert "asyncio.to_thread(compare.report" in inspect.getsource(routes.compare_clients)
    for path in ("/admin/api/work", "/admin/api/work/scorecard", "/admin/api/compare"):
        assert client.get(path).status_code == 200, path
    db = app.state.services.db
    plan = " ".join(r["detail"] for r in db.query(
        "EXPLAIN QUERY PLAN SELECT project_id, task_id, state FROM cc_tasks"))
    assert "COVERING INDEX" in plan and "idx_cc_tasks_state" in plan
