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
