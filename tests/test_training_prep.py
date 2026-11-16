"""v0.181.159: the router prepares the dataset itself; unused message text is removed."""
import inspect
import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from foundry_router import training_prep, training_prep_worker
from foundry_router.capture import CaptureStore
from foundry_router.config import TrainingCaptureConfig


def _msgs(n, tag):
    out = [{"role": "system", "content": f"{tag} system " + "s" * 300}]
    for i in range(n):
        out.append({"role": "user" if i % 2 == 0 else "assistant",
                    "content": f"{tag} turn {i} " + "x" * 300})
    return out


def _call(uid, session, n, tag, project="p", ts=None, status="ok"):
    body = json.dumps({"request": {"messages": _msgs(n, tag), "model": "m"},
                       "response": {"content": f"{tag} answer after {n}"}})
    return {"record_uid": uid, "ts": ts or time.time(), "kind": "backend_call", "request_id": f"r-{uid}",
            "session_id": session, "project_id": project, "model": "m", "backend": "b", "status": status,
            "reconstruction": "complete", "body": body, "bytes": len(body.encode()), "truncated": 0,
            "redaction": "{}", "purpose": "agent_turn"}


def _capture(tmp_path):
    st = CaptureStore(tmp_path / "training" / "capture.sqlite")
    for n in (1, 3, 5):                                   # one conversation growing: s1
        st.insert(_call(f"a{n}", "s1", n, "alpha"))
    st.insert(_call("b1", "s2", 1, "beta"))               # another conversation
    st.insert(_call("c1", "s3", 1, "gamma", project=""))  # a plain chat, no project
    return st


def _args(tmp_path, st):
    root = tmp_path / "training"
    return ["--capture", str(st.path), "--store", str(root / "prepared"), "--out", str(root / "datasets" / "router"),
            "--status", str(root / "prep-status.json"), "--pause", "0"]


def test_the_worker_builds_one_example_per_conversation(tmp_path):
    st = _capture(tmp_path)
    assert training_prep_worker.main(_args(tmp_path, st)) == 0
    status = json.loads((tmp_path / "training" / "prep-status.json").read_text())
    assert status["state"] == "done" and status["records"] == 5 and status["through"] == 5
    ds = status["dataset"]
    assert ds["conversations"] == 3 and sum(ds["examples"].values()) == 3     # s1 once (its longest call), s2, s3
    out = tmp_path / "training" / "datasets" / "router" / "all-sessions"
    assert {f.name for f in out.iterdir()} >= {"train.jsonl", "validation.jsonl", "manifest.json", "README.md"}
    lines = [json.loads(x) for n in ("train.jsonl", "validation.jsonl")
             for x in (out / n).read_text(encoding="utf-8").splitlines() if x.strip()]
    texts = [json.dumps(x) for x in lines]
    assert sum("alpha answer after 5" in t for t in texts) == 1 and not any("alpha answer after 3" in t and "after 5" not in t for t in texts)
    assert any("gamma" in t for t in texts)                                # the plain chat is in, filed as "chat"

    # a second run reads only what is new, and the dataset then has it
    st.insert(_call("d1", "s4", 1, "delta"))
    assert training_prep_worker.main(_args(tmp_path, st)) == 0
    status = json.loads((tmp_path / "training" / "prep-status.json").read_text())
    assert status["from"] == 5 and status["records"] == 1 and status["dataset"]["conversations"] == 4


def test_a_stop_file_ends_the_run_and_the_next_one_carries_on(tmp_path):
    st = _capture(tmp_path)
    (tmp_path / "training" / "prep.stop").write_text("stop")
    assert training_prep_worker.main(_args(tmp_path, st)) == 0
    status = json.loads((tmp_path / "training" / "prep-status.json").read_text())
    assert status["state"] == "stopped" and status["records"] == 0
    (tmp_path / "training" / "prep.stop").unlink()
    assert training_prep_worker.main(_args(tmp_path, st)) == 0
    assert json.loads((tmp_path / "training" / "prep-status.json").read_text())["state"] == "done"


def test_a_failure_is_written_to_the_status(tmp_path):
    root = tmp_path / "training"
    root.mkdir()
    rc = training_prep_worker.main(["--capture", str(root / "missing.sqlite"), "--store", str(root / "prepared"),
                                    "--out", str(root / "o"), "--status", str(root / "prep-status.json")])
    status = json.loads((root / "prep-status.json").read_text())
    assert rc == 1 and status["state"] == "error" and "OperationalError" in status["error"]


def _svc(tmp_path, st, **cfg):
    conf = SimpleNamespace(training_capture=TrainingCaptureConfig(**cfg))
    return SimpleNamespace(config_store=SimpleNamespace(config=conf),
                           capture=SimpleNamespace(data_dir=tmp_path, store=st),
                           db=SimpleNamespace(path=str(tmp_path / "foundry.sqlite")))


def test_when_it_is_due_and_what_the_page_gets(tmp_path):
    st = _capture(tmp_path)
    svc = _svc(tmp_path, st)
    assert TrainingCaptureConfig().prep_hours == 24 and TrainingCaptureConfig().blob_sweep_days == 7
    s = training_prep.status(svc)
    assert s["run"] == {"state": "never"} and s["hours"] == 24 and s["files"] == []
    assert training_prep.due(svc) is True                              # never built
    assert training_prep.due(_svc(tmp_path, st, prep_hours=0)) is False   # only when asked
    assert training_prep_worker.main(_args(tmp_path, st)) == 0
    s = training_prep.status(svc)
    assert s["run"]["state"] == "done" and {f["name"] for f in s["files"]} >= {"train.jsonl", "manifest.json"}
    assert training_prep.due(svc) is False and training_prep.due(svc, now=time.time() + 25 * 3600) is True
    # a status left at "running" by a worker that is gone reads as stopped, and is due at once
    (tmp_path / "training" / "prep-status.json").write_text(json.dumps({"state": "running", "records": 7}))
    s = training_prep.status(svc)
    assert s["run"]["state"] == "stopped" and "continues" in s["run"]["error"] and training_prep.due(svc) is True


def test_the_router_starts_a_separate_process(tmp_path):
    st = _capture(tmp_path)
    svc = _svc(tmp_path, st)
    training_prep.start(svc)
    p = training_prep._proc["p"]
    assert p is not None and "foundry_router.training_prep_worker" in p.args
    assert p.wait(timeout=120) == 0
    assert training_prep.status(svc)["run"]["state"] == "done" and training_prep.running() is False


# -- blob clean-up ------------------------------------------------------------------------------

def _blob_count(st):
    return st.query("SELECT COUNT(*) AS n FROM blobs")[0]["n"]


def test_text_no_record_uses_is_removed_and_the_rest_reads_the_same(tmp_path):
    st = CaptureStore(tmp_path / "capture.sqlite")
    old = time.time() - 40 * 86400
    for n in range(1, 81):                                # long enough for runs of 32 ("$span")
        st.insert(_call(f"old{n}", "s-old", n, "old", ts=old))
    for n in range(1, 81):
        st.insert(_call(f"new{n}", "s-new", n, "new"))
    before = _blob_count(st)
    keep_body = st.query("SELECT body FROM capture_records WHERE record_uid='new80'")[0]["body"]
    whole = st.unpack_body(keep_body)
    assert "$span" in keep_body
    assert st.sweep_blobs(pause=0)["removed"] == 0        # every piece is still used
    assert st.prune(30) == 80 and st.pruned_since_sweep >= 80
    out = st.sweep_blobs(pause=0)
    assert out["state"] == "done" and out["removed"] > 80 and out["records"] >= 80
    assert _blob_count(st) == before - out["removed"]
    for r in st.query("SELECT body FROM capture_records"):
        st.unpack_body(r["body"])                         # nothing a kept record needs is gone
    assert st.unpack_body(keep_body) == whole
    assert not (tmp_path / "blob-sweep.sqlite").exists()
    assert json.loads(st._meta("blob_sweep"))["removed"] == out["removed"]
    # text removed and then needed again by a new record is stored again
    st.insert(_call("again", "s-old2", 3, "old"))
    assert st.unpack_body(st.query("SELECT body FROM capture_records WHERE record_uid='again'")[0]["body"]) \
        == json.loads(_call("again", "s", 3, "old")["body"])


def test_records_written_during_the_clean_up_keep_their_text(tmp_path):
    st = CaptureStore(tmp_path / "capture.sqlite")
    old = time.time() - 40 * 86400
    for n in range(1, 41):
        st.insert(_call(f"old{n}", "s-old", n, "shared", ts=old))
    st.prune(30)                                          # every record gone, all text unused
    stop = threading.Event()
    written = []

    def writer():
        i = 0
        while not stop.is_set() and i < 60:
            i += 1
            st.insert(_call(f"w{i}", "s-w", (i % 40) + 1, "shared"))   # reuses the unused text
            written.append(i)
            time.sleep(0.002)
    t = threading.Thread(target=writer)
    t.start()
    out = st.sweep_blobs(pause=0.001, step=5)
    stop.set()
    t.join()
    assert out["state"] == "done" and written
    for r in st.query("SELECT body FROM capture_records"):
        st.unpack_body(r["body"])                         # raises MissingBlob if a piece was lost


def test_when_the_clean_up_runs(tmp_path):
    st = CaptureStore(tmp_path / "capture.sqlite")
    st.insert(_call("a", "s", 2, "t"))
    assert st.sweep_due(0) is False                       # switched off
    assert st.sweep_due(7) is True                        # never run, and a restart assumes pruning
    st.sweep_start()
    for _ in range(200):
        if st.sweep.get("state") != "running":
            break
        time.sleep(0.02)
    assert st.sweep["state"] == "done"
    for _ in range(100):
        if st.pruned_since_sweep == 0:
            break
        time.sleep(0.02)
    assert st.sweep_due(7) is False                       # nothing pruned since
    st.pruned_since_sweep = 5
    assert st.sweep_due(7) is False and st.sweep_due(7, now=time.time() + 8 * 86400) is True
    st._compactor = SimpleNamespace(is_alive=lambda: True)
    assert st.sweep_blobs()["state"] == "skipped"         # never while the shrink runs


def test_routes_and_page():
    from foundry_router import main
    from foundry_router.ui import routes
    src = inspect.getsource(routes)
    for needle in ('@router.post("/admin/api/training/prep")', '@router.get("/admin/api/training/prep/file/{name}")',
                   '@router.post("/admin/api/training/capture/sweep")', '"prep": await asyncio.to_thread(training_prep.status, svc)',
                   '("prep_hours", 0, 8760)'):
        assert needle in src, needle
    msrc = inspect.getsource(main)
    assert "training_prep.due(self)" in msrc and "sweep_due(int(tc.blob_sweep_days or 0))" in msrc
    html = (Path(routes.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    for fid in ("prep-hours", "prep-max", "prep-unbound", "prep-save", "prep-run", "sweep-days", "sweep-save", "sweep-run"):
        assert f'id="{fid}"' in html, fid
    assert 'id="prep-stop"' in html and "training/prep/file/" in html
    docker = (Path(routes.__file__).parent.parent.parent / "Dockerfile").read_text()
    assert "COPY foundry_training/ ./foundry_training/" in docker       # the worker needs it in the image


def test_the_worker_does_not_redact_a_third_time(tmp_path, monkeypatch):
    """v0.181.161: capture redacted the records, the export redacts again."""
    from foundry_training import redact
    calls = []
    real = redact.Redactor.obj
    monkeypatch.setattr(redact.Redactor, "obj", lambda self, o: (calls.append(1), real(self, o))[1])
    from foundry_training import sessions
    at_export = []
    real_export = sessions.export

    def export(*a, **k):
        at_export.append(len(calls))                      # how often it ran before the export
        return real_export(*a, **k)
    monkeypatch.setattr(sessions, "export", export)
    st = _capture(tmp_path)
    assert training_prep_worker.main(_args(tmp_path, st)) == 0
    status = json.loads((tmp_path / "training" / "prep-status.json").read_text())
    assert status["state"] == "done" and status["records"] == 5
    assert at_export == [0]                               # reading the records redacted nothing
    assert len(calls) > 0                                 # the export still redacts
    payload, kept = training_prep_worker._AlreadyRedacted.obj({"a": 1})
    assert payload == {"a": 1} and kept.status() == "clean" and kept.kinds() == [] and kept.count == 0

