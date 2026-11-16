"""v0.181.162: records made on another machine (Kilo, coordinator, git) are sent to
the router, folded into its prepared store, and copied to the NAS with everything else."""
import gzip
import inspect
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from foundry_router import training_mirror as tm
from foundry_router.config import ClientConfig, TrainingCaptureConfig
from foundry_router.db import Database
from foundry_training.schema import Event
from foundry_training.store import TrainingStore
from tests.test_budgets import STATE, ledgered  # noqa: F401 (fixture)

COORD = {"Authorization": "Bearer coord-key"}


def _svc(tmp_path):
    db = Database(tmp_path / "main.sqlite")
    return SimpleNamespace(db=db, capture=SimpleNamespace(data_dir=tmp_path / "data"))


def _event(adapter, rid, **kw):
    e = dict(source_adapter=adapter, source_record_id=rid, event_type="assistant_message",
             project_id="p", timestamp=1794800000.0, facts={"text": "x" * 500}, actor_role="assistant")
    e.update(kw)
    return e


# -- the spool ----------------------------------------------------------------------------

def test_a_batch_is_spooled_and_the_cursor_comes_back(tmp_path):
    svc = _svc(tmp_path)
    assert tm.cursor(svc, "kilo") == "" and tm.waiting(svc)["files"] == 0
    out = tm.accept(svc, "kilo", [_event("kilo", "a"), _event("kilo", "b")], "17")
    assert out["accepted"] == 2 and out["cursor"] == "17"
    assert tm.cursor(svc, "kilo") == "17" and tm.cursor(svc, "git") == ""
    w = tm.waiting(svc)
    assert w["files"] == 1 and w["bytes"] > 0 and w["oldest"] is not None
    body = json.loads(gzip.open(tm.pending(svc)[0], "rt", encoding="utf-8").read())
    assert body["source"] == "kilo" and len(body["events"]) == 2
    assert not list(tm.spool_dir(svc).glob("*.part"))               # written, then renamed
    assert tm.status(svc)["sources"] == {"kilo": {"workstation": "17"}}
    tm.accept(svc, "kilo", [_event("kilo", "c")], "3", sender="archive-v2")
    assert tm.cursor(svc, "kilo") == "17" and tm.cursor(svc, "kilo", "archive-v2") == "3"
    assert tm.status(svc)["sources"]["kilo"] == {"workstation": "17", "archive-v2": "3"}


def test_what_is_refused(tmp_path):
    svc = _svc(tmp_path)
    for source, events, why in (("", [], "short names"), ("Kilo!", [], "short names"),
                                ("kilo", {"a": 1}, "must be a list"),
                                ("kilo", [{}] * (tm.MAX_EVENTS + 1), "at most")):
        with pytest.raises(ValueError) as e:
            tm.accept(svc, source, events)
        assert why in str(e.value)
    with pytest.raises(ValueError) as e:
        tm.accept(svc, "kilo", [_event("kilo", str(i), facts={"t": "x" * 40000}) for i in range(900)])
    assert "MB" in str(e.value)
    assert tm.waiting(svc)["files"] == 0                            # nothing was written


def test_the_spool_is_folded_into_the_store(tmp_path):
    svc = _svc(tmp_path)
    store = TrainingStore(str(tmp_path / "prepared"), allow_in_repo=True)
    tm.accept(svc, "kilo", [_event("kilo", "a"), _event("kilo", "b")], "2")
    tm.accept(svc, "coordinator", [_event("coordinator", "c", event_type="task_transition")], "9")
    out = tm.ingest(store, tm.spool_dir(svc))
    assert out == {"files": 2, "events": 3, "new": 3, "bad": 0,
                   "sources": {"kilo": {"events": 2, "new": 2},
                               "coordinator": {"events": 1, "new": 1}}}
    rows = store.norm.q("SELECT source_adapter, event_type, facts FROM events ORDER BY source_record_id")
    assert [r["source_adapter"] for r in rows] == ["kilo", "kilo", "coordinator"]
    assert store.unpack(json.loads(rows[0]["facts"]))["text"] == "x" * 500   # the text came through
    assert tm.waiting(svc)["files"] == 0                            # the spool is empty again

    # the same records again: stored once
    tm.accept(svc, "kilo", [_event("kilo", "a")], "3")
    again = tm.ingest(store, tm.spool_dir(svc))
    assert again["events"] == 1 and again["new"] == 0
    assert store.norm.q("SELECT COUNT(*) n FROM events")[0]["n"] == 3


def test_a_batch_that_cannot_be_read_is_kept_aside(tmp_path):
    svc = _svc(tmp_path)
    store = TrainingStore(str(tmp_path / "prepared"), allow_in_repo=True)
    tm.accept(svc, "kilo", [_event("kilo", "good")], "1")
    bad = tm.spool_dir(svc) / "kilo-bad.json.gz"
    with gzip.open(bad, "wt", encoding="utf-8") as f:
        f.write('{"source": "kilo", "events": [{"nonsense": true}]}')
    said = []
    out = tm.ingest(store, tm.spool_dir(svc), log=said.append)
    assert out["files"] == 1 and out["new"] == 1 and out["bad"] == 1
    assert list(tm.spool_dir(svc).glob("*.bad")) and not tm.pending(svc)
    assert any("kept as .bad" in s for s in said)
    # an unknown field in an otherwise good event is dropped, not a failure
    tm.accept(svc, "git", [{**_event("git", "g1"), "something_new": 1}], "1")
    assert tm.ingest(store, tm.spool_dir(svc))["new"] == 1


def test_the_worker_folds_the_spool_in_before_it_reads(tmp_path):
    from foundry_router import training_prep_worker
    from tests.test_training_prep import _args, _capture
    svc = _svc(tmp_path)
    tm.accept(svc, "kilo", [_event("kilo", "k1", session_id="s9")], "4")
    st = _capture(tmp_path)
    assert training_prep_worker.main(_args(tmp_path, st) + ["--incoming", str(tm.spool_dir(svc))]) == 0
    status = json.loads((tmp_path / "training" / "prep-status.json").read_text())
    assert status["state"] == "done" and status["mirror"]["new"] == 1
    store = TrainingStore(str(tmp_path / "training" / "prepared"), allow_in_repo=True)
    assert store.norm.q("SELECT COUNT(*) n FROM events WHERE source_adapter='kilo'")[0]["n"] == 1
    assert not tm.pending(svc)


# -- the endpoints ------------------------------------------------------------------------

def _client(ledgered, client):
    ledgered.config_store.config.clients.append(
        ClientConfig(name="coordinator", key="coord-key", scopes=["orchestration", "admin"]))
    ledgered.config_store.config.training_capture = TrainingCaptureConfig(enabled=True)
    return client


def test_the_workstation_posts_its_records(ledgered, client):  # noqa: F811
    c = _client(ledgered, client)
    assert c.get("/v1/foundry/training/events/cursor", headers=COORD,
                 params={"source": "kilo"}).json() == {
        "source": "kilo", "sender": "workstation", "cursor": "",
        "waiting": {"files": 0, "bytes": 0, "oldest": None}}
    r = c.post("/v1/foundry/training/events", headers=COORD,
               json={"source": "kilo", "cursor": "120", "events": [_event("kilo", "x")]})
    assert r.status_code == 200 and r.json()["accepted"] == 1 and r.json()["cursor"] == "120"
    got = c.get("/v1/foundry/training/events/cursor", headers=COORD, params={"source": "kilo"}).json()
    assert got["cursor"] == "120" and got["waiting"]["files"] == 1
    bad = c.post("/v1/foundry/training/events", headers=COORD, json={"source": "", "events": []})
    assert bad.status_code == 400 and "short names" in bad.json()["error"]
    # a second store on the same machine keeps its own position
    assert c.post("/v1/foundry/training/events", headers=COORD,
                  json={"source": "kilo", "sender": "archive-v2", "cursor": "7",
                        "events": [_event("kilo", "y")]}).json()["cursor"] == "7"
    assert c.get("/v1/foundry/training/events/cursor", headers=COORD,
                 params={"source": "kilo", "sender": "archive-v2"}).json()["cursor"] == "7"
    assert c.post("/v1/foundry/training/events", json={"source": "kilo", "events": []}).status_code == 401


# -- the sender ---------------------------------------------------------------------------

def test_the_sender_walks_each_source_and_resumes(tmp_path, ledgered, client):  # noqa: F811
    from foundry_training import mirror
    c = _client(ledgered, client)
    local = TrainingStore(str(tmp_path / "ws"), allow_in_repo=True)
    local.put_events([Event(**_event("kilo", f"k{i}")) for i in range(5)])
    local.put_events([Event(**_event("git", "g1", event_type="commit"))])
    local.put_events([Event(**_event("foundry", "f1"))])          # the router made this itself

    def send(url, path, key, body=None, method="GET", timeout=120):
        r = c.request(method, path, headers=COORD, json=body)
        assert r.status_code == 200, r.text
        return r.json()
    mirror._send = send
    out = mirror.run(local, "http://router", "k", batch=2, log=lambda s: None)
    assert out["errors"] == [] and out["sent"] == 6                # kilo 5 + git 1, not foundry
    assert out["sources"]["kilo"]["sent"] == 5 and out["sources"]["kilo"]["batches"] == 3
    assert out["sources"]["coordinator"] == {"from": 0, "to_send": 0, "sent": 0, "batches": 0}
    again = mirror.run(local, "http://router", "k", batch=2, log=lambda s: None)
    assert again["sent"] == 0                                      # the router's cursor is respected
    local.put_events([Event(**_event("kilo", "k9"))])
    assert mirror.run(local, "http://router", "k", batch=2, log=lambda s: None)["sent"] == 1

    svc = ledgered
    store = TrainingStore(str(tmp_path / "prepared"), allow_in_repo=True)
    folded = tm.ingest(store, tm.spool_dir(svc))
    assert folded["new"] == 7 and set(folded["sources"]) == {"kilo", "git"}
    row = store.norm.q("SELECT facts FROM events WHERE source_record_id='k0'")[0]
    assert store.unpack(json.loads(row["facts"]))["text"] == "x" * 500


def test_the_sender_sends_the_text_put_back_together(tmp_path):
    from foundry_training import mirror
    local = TrainingStore(str(tmp_path / "ws"), allow_in_repo=True)
    big = {"messages": [{"role": "user", "content": "y" * 9000}]}
    local.put_events([Event(**_event("kilo", "k1", facts=big))])
    stored = local.norm.q("SELECT facts FROM events")[0]["facts"]
    assert "$blob" in stored and "y" * 9000 not in stored          # stored once, as a reference
    rows = mirror.rows_after(local, "kilo", 0, 10)
    assert len(rows) == 1 and rows[0][1]["facts"] == big           # sent whole
    assert "event_id" not in rows[0][1] and rows[0][1]["source_record_id"] == "k1"


# -- the archive --------------------------------------------------------------------------

def test_the_prepared_store_is_archived_too(tmp_path):
    from foundry_router import training_archive as ta
    from tests.test_training_archive import _fill, _svc as _arc_svc
    svc, cfg = _arc_svc(tmp_path, archive=str(tmp_path / "arc"), archive_prepared=True)
    (tmp_path / "arc").mkdir()
    _fill(svc, tmp_path)
    store = TrainingStore(str(tmp_path / "data" / "training" / "prepared"), allow_in_repo=True)
    store.put_events([Event(**_event("kilo", "k1"))])
    out = ta.run(svc)
    assert out["ok"], out["errors"]
    assert out["prepared"]["files"] >= 2 and out["prepared"]["bytes"] > 0
    copied = tmp_path / "arc" / "prepared" / "normalized.sqlite"
    assert copied.is_file()
    import sqlite3
    con = sqlite3.connect(f"file:{copied.as_posix()}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    con.close()
    assert ta.status(svc)["prepared"] is True
    cfg.archive_prepared = False
    assert "prepared" not in ta.run(svc)


def test_wiring():
    from foundry_router import training_prep
    from foundry_router.facade import work_api
    from foundry_router.ui import routes
    src = inspect.getsource(work_api)
    assert '@router.post("/v1/foundry/training/events")' in src
    assert '@router.get("/v1/foundry/training/events/cursor")' in src
    assert "training_mirror.accept" in src
    prep = inspect.getsource(training_prep)
    assert '"incoming": root / "incoming"' in prep and '"--incoming", str(d["incoming"])' in prep
    assert '"mirror": training_mirror.status(svc)' in prep
    assert 'waiting", {}).get("files")' in prep                    # a waiting batch makes a run due
    assert '"archive_prepared"' in inspect.getsource(routes)
    html = (Path(routes.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="arc-prep"' in html and "archive_prepared: $(\"#arc-prep\").checked" in html
    assert "from other machines:" in html
