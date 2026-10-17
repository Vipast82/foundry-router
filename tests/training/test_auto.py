"""Automatic post-sprint processing (foundry_training auto): a curated sprint
becomes validated fine-tuning datasets in one central library folder and is
uploaded to Foundry's real endpoint; if Foundry can't take it, it stays in the
library and is retried. The coordinator launches it in the background after a
closeout manifest. No model is called anywhere."""

import io
import json
import os
import urllib.error
from pathlib import Path

import pytest

from foundry_training import auto as A
from tests.training.helpers import KILO_EXPORT, capture_db, capture_rows, coordinator, \
    kilo_export, pipeline, profile, workspace

PID = "fixture-game"
SESSION = json.loads(KILO_EXPORT.read_text(encoding="utf-8"))["info"]["id"]
KEY = "coord-training-key-123456"


@pytest.fixture()
def sprint(tmp_path):
    """One finished sprint, curated: a selected local-model trajectory."""
    ws, base, head = workspace(tmp_path)
    prof = profile(tmp_path, PID, ws)
    st = coordinator(tmp_path, PID, "FX-1", SESSION, base, head)
    p = pipeline(tmp_path, [prof])
    p.ingest_kilo(exports=[str(KILO_EXPORT)])
    p.ingest_foundry(capture_db=capture_db(tmp_path, capture_rows(kilo_export())))
    p.ingest_coordinator(st.path)
    p.ingest_git(PID)
    p.curate(PID)
    spool = tmp_path / "spool"
    spool.mkdir()
    return p, str(spool), tmp_path, prof


@pytest.fixture()
def foundry(app, client, monkeypatch):
    """Route the pipeline's HTTP calls into this test's real Foundry app."""
    from foundry_router.config import ClientConfig
    app.state.services.config_store.config.clients += [
        ClientConfig(name="coordinator", key=KEY, scopes=["inference", "orchestration"])]
    state = {"down": False, "calls": []}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        if state["down"]:
            raise urllib.error.URLError("connection refused")
        path = req.full_url.split("foundry.test", 1)[1]
        state["calls"].append(path)
        r = client.request(req.get_method(), path, content=req.data,
                           headers=dict(req.header_items()))
        if r.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, r.status_code, "err", {},
                                         io.BytesIO(r.content))
        return Resp(r.content)
    monkeypatch.setattr(A.urllib.request, "urlopen", urlopen)
    import foundry_training.pipeline as PL
    monkeypatch.setattr(PL.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("FOUNDRY_TRAINING_KEY", KEY)
    return state


def run(p, spool, **kw):
    return A.auto_run(p, spool, {"foundry_url": "http://foundry.test", **kw}, log=lambda m: None)


def test_sprint_becomes_library_datasets_and_is_uploaded(sprint, foundry, app, client):
    p, spool, tmp, _ = sprint
    rep = run(p, spool)
    assert not rep["errors"], rep["errors"]
    lib = Path(rep["library"])
    idx = json.loads((lib / "INDEX.json").read_text())
    kinds = {(d["kind"], d["target"]) for d in idx["datasets"]}
    assert {("sft", "chatml-tools"), ("sft", "canonical")} <= kinds
    sft = next(d for d in idx["datasets"] if (d["kind"], d["target"]) == ("sft", "chatml-tools"))
    assert sft["valid"] and sft["examples"] == 1 and sft["upload"]["status"] == "uploaded"
    latest = lib / PID / "sft" / "chatml-tools" / "latest.jsonl"
    line = json.loads(latest.read_text(encoding="utf-8").splitlines()[0])
    assert line["messages"][0]["role"] == "system" and line["tools"]        # fine-tuning format
    assert (lib / "README.md").exists()
    assert (lib / PID / "sft" / "chatml-tools" / sft["dataset_id"] / "manifest.json").exists()
    # Foundry stored it and serves it to the admin UI
    rows = app.state.services.db.query("SELECT * FROM training_datasets")
    assert {r["dataset_id"] for r in rows} >= {sft["dataset_id"]}
    stored = next(r for r in rows if r["dataset_id"] == sft["dataset_id"])
    assert os.path.isfile(os.path.join(stored["path"], "data.jsonl")) and stored["examples"] == 1
    lst = client.get("/admin/api/training/datasets").json()["datasets"]
    assert sft["dataset_id"] in {d["dataset_id"] for d in lst}
    dl = client.get(f"/admin/api/training/datasets/{sft['dataset_id']}/data.jsonl")
    assert dl.status_code == 200 and json.loads(dl.text.splitlines()[0])["messages"]
    # the Training tab's status was refreshed too
    assert any("/training/status/" in c for c in foundry["calls"])
    # a second run with no new episodes makes no new dataset and uploads nothing again
    before = len(idx["datasets"])
    rep2 = run(p, spool)
    idx2 = json.loads((lib / "INDEX.json").read_text())
    assert len(idx2["datasets"]) == before and not rep2["exports"] and not rep2["uploads"]


def test_unreachable_foundry_keeps_the_dataset_and_retries(sprint, foundry, app):
    p, spool, *_ = sprint
    foundry["down"] = True
    rep = run(p, spool)
    idx = json.loads((Path(rep["library"]) / "INDEX.json").read_text())
    assert idx["datasets"] and all(d["upload"]["status"] == "failed" for d in idx["datasets"])
    assert "unreachable" in idx["datasets"][0]["upload"]["reason"]
    assert Path(idx["datasets"][0]["data"]).exists()                       # safe on disk
    foundry["down"] = False
    run(p, spool)
    idx = json.loads((Path(rep["library"]) / "INDEX.json").read_text())
    assert all(d["upload"]["status"] == "uploaded" for d in idx["datasets"])


def test_no_key_or_upload_off_stays_local(sprint, foundry, monkeypatch):
    p, spool, *_ = sprint
    monkeypatch.delenv("FOUNDRY_TRAINING_KEY")
    rep = run(p, spool)
    idx = json.loads((Path(rep["library"]) / "INDEX.json").read_text())
    assert idx["datasets"][0]["upload"]["status"] == "not_configured"
    rep = run(p, spool, upload=False)
    idx = json.loads((Path(rep["library"]) / "INDEX.json").read_text())
    assert idx["datasets"][0]["upload"]["status"] == "disabled"


def test_foundry_endpoint_refuses_unsafe_or_inconsistent_uploads(client, app):
    from foundry_router.config import ClientConfig
    app.state.services.config_store.config.clients += [
        ClientConfig(name="coordinator", key=KEY, scopes=["inference", "orchestration"])]
    h = {"Authorization": f"Bearer {KEY}"}
    m = {"dataset_id": "train-sft-x-12345678", "purpose": "train", "projects": ["g"],
         "kind": "sft", "target": "chatml-tools"}
    good = json.dumps({"messages": [{"role": "user", "content": "hi"}]})
    url = "/v1/foundry/training/datasets/train-sft-x-12345678"
    secret = json.dumps({"messages": [{"role": "user", "content":
                                       "key sk-ant-api03-" + "A" * 40}]})
    assert client.post(url, json={"manifest": m, "data": secret}, headers=h).status_code == 422
    assert client.post(url, json={"manifest": {**m, "purpose": "eval"}, "data": good},
                       headers=h).status_code == 400
    assert client.post(url, json={"manifest": m, "data": good, "sha256": "0" * 64},
                       headers=h).status_code == 400
    assert client.post(url, json={"manifest": m, "data": good}, headers=h).status_code == 200
    r = client.post(url, json={"manifest": m, "data": good}, headers=h)
    assert r.status_code == 200 and r.json()["duplicate"]
    assert client.post(url, json={"manifest": m, "data": good + "\n" + good},
                       headers=h).status_code == 409
    assert client.post(url, json={"manifest": m, "data": good},
                       headers={"Authorization": "Bearer nope"}).status_code in (401, 403)


def test_coordinator_launches_the_run_after_a_closeout(tmp_path, monkeypatch):
    from foundry_coordinator import training_hook as H

    class Prof:
        path = str(tmp_path / "project.toml")
        raw = {"training": {"enabled": True}}

    class P:
        id, profile = "g", Prof()

    events, spawned = [], []

    class Store:
        def event(self, *a, **k):
            events.append((a, k))
    monkeypatch.setattr(H, "_spawn", lambda cmd, log, env: spawned.append((cmd, log)))
    monkeypatch.setenv("FOUNDRY_TRAINING_AUTO", "1")
    assert H.launch_auto(Store(), P(), {"task_id": "T"}, str(tmp_path))
    cmd, log = spawned[0]
    assert cmd[1:3] == ["-m", "foundry_training"] and Prof.path in cmd and "auto" in cmd
    assert log.endswith("auto.log") and events[0][0][2] == "training_auto_started"
    monkeypatch.setenv("FOUNDRY_TRAINING_AUTO", "0")                       # operator/test off switch
    assert not H.launch_auto(Store(), P(), {"task_id": "T"}, str(tmp_path))
    monkeypatch.setenv("FOUNDRY_TRAINING_AUTO", "1")
    Prof.raw = {"training": {"enabled": True, "auto": {"enabled": False}}}
    assert not H.launch_auto(Store(), P(), {"task_id": "T"}, str(tmp_path))
    Prof.raw = {"training": {"enabled": False}}
    assert not H.launch_auto(Store(), P(), {"task_id": "T"}, str(tmp_path))
    assert len(spawned) == 1


def test_cli_auto_runs_once_at_a_time(sprint, monkeypatch, capsys):
    from foundry_training import cli
    p, spool, tmp, prof = sprint
    open(os.path.join(spool, ".auto.lock"), "w").close()
    assert cli.main(["--store", str(p.store.root), "--allow-in-repo", "--profiles", prof, "auto",
                     "--spool", spool, "--no-upload"]) == 0
    assert "in progress" in capsys.readouterr().out
    os.remove(os.path.join(spool, ".auto.lock"))
    assert cli.main(["--store", str(p.store.root), "--allow-in-repo", "--profiles", prof, "auto",
                     "--spool", spool, "--no-upload"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["exports"] and not os.path.exists(os.path.join(spool, ".auto.lock"))
