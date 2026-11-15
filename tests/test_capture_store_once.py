"""The router's capture store keeps each message once (v0.181.145) and can shrink a
store written before that, while capture keeps running."""

import json
import sqlite3
import time

from foundry_router.capture import CaptureStore


def _msgs(n, tag="t"):
    return [{"role": "user" if i % 2 == 0 else "assistant",
             "content": f"{tag} message {i} " + "x" * 400} for i in range(n)]


TOOLS = [{"type": "function", "function": {"name": "read", "description": "d" * 400}}]


def _row(n, uid, tag="t"):
    body = json.dumps({"request": {"messages": _msgs(n, tag), "tools": TOOLS, "model": "m"},
                       "response": {"content": f"reply {n}"}})
    return {"record_uid": uid, "ts": time.time(), "kind": "backend_call", "request_id": f"r{uid}",
            "project_id": "p", "body": body, "bytes": len(body.encode()), "truncated": 0,
            "redaction": "{}", "purpose": "agent_turn"}


def _whole(n, tag="t"):
    return {"request": {"messages": _msgs(n, tag), "tools": TOOLS, "model": "m"},
            "response": {"content": f"reply {n}"}}


def test_new_records_are_stored_once_and_read_back_whole(tmp_path):
    st = CaptureStore(tmp_path / "capture.sqlite")
    for n in range(1, 41):
        st.insert(_row(n, f"u{n}"))
    rows = st.query("SELECT id, body, bytes FROM capture_records ORDER BY id")
    assert len(rows) == 40
    assert "x" * 400 not in rows[-1]["body"]                           # references, not text
    assert rows[-1]["bytes"] == len(json.dumps(_whole(40)).encode())   # the real size is kept
    assert st.unpack_body(rows[-1]["body"]) == _whole(40)
    assert st.unpack_body(rows[9]["body"]) == _whole(10)
    blobs = st.query("SELECT COUNT(*) AS n FROM blobs")[0]["n"]
    assert blobs == 40 + 1                                              # 40 messages and the tool list
    stored = st.query("SELECT SUM(LENGTH(z)) AS n FROM blobs")[0]["n"] + sum(len(r["body"]) for r in rows)
    assert stored < sum(r["bytes"] for r in rows) / 4      # short test messages; real ones are far larger


def test_records_come_back_whole_through_capture(tmp_path):
    from types import SimpleNamespace

    from foundry_router.capture import Capture
    cfg = SimpleNamespace(store_path=str(tmp_path / "c.sqlite"), enabled=True, projects=[],
                          exclude_projects=[], include_unbound=False, retention_days=30,
                          backends=[], exclude_backends=[], personas=[], exclude_personas=[],
                          max_body_bytes=8_000_000)
    cap = Capture(lambda: cfg, tmp_path)
    cap.store.insert(_row(12, "a"))
    recs = cap.records()
    assert recs[0]["body"] == _whole(12)
    s = cap.status()
    assert s["store_bytes"] > 0 and s["compaction"]["state"] == "idle" and s["packed_through"] == 0


def _old_store(tmp_path, n=30):
    """A store as v0.181.144 wrote it: whole bodies, no blobs."""
    path = tmp_path / "capture.sqlite"
    st = CaptureStore(path)
    st.pack_body = lambda text: text                                    # write records whole
    for i in range(1, n + 1):
        st.insert(_row(i + 40, f"old{i}", "old"))
    st.conn.close()
    return path


def test_an_old_store_is_shrunk_and_reads_the_same(tmp_path):
    path = _old_store(tmp_path)
    st = CaptureStore(path)
    before = {r["id"]: json.loads(r["body"]) for r in st.query("SELECT id, body FROM capture_records")}
    assert st.query("SELECT COUNT(*) AS n FROM blobs")[0]["n"] == 0
    size0 = st.size_bytes()
    out = st.compact(batch=7, pause=0, verify_every=3)
    assert out["state"] == "done" and out["records"] == 30 and out["packed"] == 30 and out["verified"] >= 9
    assert out["bytes_after"] < size0 / 3                               # the space went back to the disk
    after = {r["id"]: st.unpack_body(r["body"]) for r in st.query("SELECT id, body FROM capture_records")}
    assert after == before
    assert st._meta("packed_through") == "30"
    # a record written during or after the run is packed on the way in, and a second run has nothing to do
    st.insert(_row(71, "new", "old"))
    again = st.compact(pause=0)
    assert again["records"] == 1 and again["packed"] == 0
    # another process (the training pipeline reading the file) gets whole records
    from foundry_training.adapters.foundry import from_db
    got = list(from_db(str(path)))
    assert len(got) == 31 and got[0]["body"] == before[1] and got[-1]["body"] == _whole(71, "old")


def test_shrinking_continues_after_a_stop_and_never_replaces_a_bad_record(tmp_path, monkeypatch):
    path = _old_store(tmp_path, n=20)
    st = CaptureStore(path)
    st._meta("packed_through", "12")                                    # an earlier run got this far
    out = st.compact(pause=0, reclaim=False)
    assert out["from"] == 12 and out["records"] == 8
    raw = sqlite3.connect(path)
    assert "x" * 400 in raw.execute("SELECT body FROM capture_records WHERE id=5").fetchone()[0]      # untouched
    assert "x" * 400 not in raw.execute("SELECT body FROM capture_records WHERE id=15").fetchone()[0]
    raw.close()
    # a record that would not read back the same stops the run before it is replaced
    path2 = _old_store(tmp_path / "b", n=6) if (tmp_path / "b").mkdir() is None else None
    st2 = CaptureStore(path2)
    monkeypatch.setattr(type(st2.blobs), "unpack", lambda self, obj: {"wrong": True})
    out = st2.compact(pause=0, verify_every=1)
    assert out["state"] == "error" and "does not read back the same" in out["error"]
    raw = sqlite3.connect(path2)
    assert all("x" * 400 in r[0] for r in raw.execute("SELECT body FROM capture_records"))
    raw.close()


def test_capture_keeps_writing_while_the_store_shrinks(tmp_path):
    path = _old_store(tmp_path, n=40)
    st = CaptureStore(path)
    st.compact_start()
    for i in range(10):
        st.insert_later(_row(5, f"live{i}", "live"))
    assert st.flush(30)
    st._compactor.join(60)
    assert st.compaction["state"] == "done"
    assert st.query("SELECT COUNT(*) AS n FROM capture_records")[0]["n"] == 50
    assert st.compact_start()["state"] in ("running", "done")


def test_the_admin_page_and_route():
    import inspect
    from pathlib import Path

    from foundry_router.ui import routes
    assert '@router.post("/admin/api/training/capture/compact")' in inspect.getsource(routes)
    html = (Path(routes.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="cap-compact"' in html and 'api("training/capture/compact", {method: "POST", body: {}})' in html
    assert "c.store_bytes" in html


def test_the_shrink_yields_the_disk_and_can_be_stopped(tmp_path):
    """v0.181.146: the first live run froze the router three times in ten minutes
    (ordinary queries waited 6-23 s for the disk)."""
    import inspect
    import threading
    path = _old_store(tmp_path, n=40)
    st = CaptureStore(path)
    sig = inspect.signature(st.compact)
    assert sig.parameters["batch"].default == 8 and sig.parameters["yield_factor"].default == 3.0
    seen = []
    real = time.sleep
    try:
        time.sleep = lambda s: (seen.append(s), st.compact_stop() if len(seen) == 2 else None)
        out = st.compact()
    finally:
        time.sleep = real
    assert out["state"] == "stopped" and out["records"] == 16 and len(seen) == 2    # two batches, then stopped
    assert all(s >= 0.05 for s in seen)
    assert st._meta("packed_through") == "16"
    out = st.compact(pause=0, yield_factor=0)                            # started again: continues
    assert out["state"] == "done" and out["from"] == 16 and out["records"] == 24
    assert threading.active_count() >= 1
    from pathlib import Path

    from foundry_router.ui import routes
    assert 'body.get("stop")' in inspect.getsource(routes.training_capture_compact)
    html = (Path(routes.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="cap-compact-stop"' in html and "body: {stop: true}" in html


def test_the_shrink_reads_on_its_own_connection(tmp_path):
    """v0.181.148: it shared the store's read-only connection, whose lock is held for
    the whole of each query; a request reading capture records on the event loop
    waited 61 s behind it on the live router."""
    import threading
    path = _old_store(tmp_path, n=24)
    st = CaptureStore(path)
    st.ro_query("SELECT 1")                                    # the shared connection exists
    waited = []

    def reader():
        t0 = time.monotonic()
        st.ro_query("SELECT COUNT(*) AS n FROM capture_records", flush=False)
        waited.append(time.monotonic() - t0)

    with st._ro_lock:                                           # someone holds the shared one...
        done = threading.Thread(target=st.compact, kwargs={"pause": 0, "yield_factor": 0})
        done.start()
        done.join(30)                                           # ...and the shrink still finishes
        assert not done.is_alive() and st.compaction["state"] == "done"
    reader()
    assert waited[0] < 2
    import inspect
    src = inspect.getsource(CaptureStore.compact)
    assert 'rd = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro"' in src
    assert "self.ro_query(\"SELECT id, body" not in src


def test_a_long_count_blocks_neither_capture_nor_the_page(tmp_path):
    """v0.181.151: with the record count holding the shared read connection's lock,
    the first captured request after a restart froze the event loop for 12.2 s
    (starting the writer thread took the same lock), and the Training page hung
    (its status read went through that connection)."""
    import threading
    st = CaptureStore(tmp_path / "capture.sqlite")
    st._meta("packed_through", "7")
    done = []
    with st._ro_lock:                                           # the count is running
        t = threading.Thread(target=lambda: (st.insert_later(_row(3, "a")), done.append("insert")))
        t.start()
        t.join(5)
        assert done == ["insert"]                               # handed to the writer at once
        t = threading.Thread(target=lambda: done.append(st._meta("packed_through")))
        t.start()
        t.join(5)
        assert done == ["insert", "7"]                          # the page's read did not wait
    assert st.flush(10) and st.query("SELECT COUNT(*) AS n FROM capture_records")[0]["n"] == 1

