"""Scenario review of the training pipeline (fakes only). Coordinator closeout
manifest -> spool -> `auto` (queue + curate + export) -> library INDEX.json /
latest.jsonl -> upload to Foundry `/v1/foundry/training/datasets/{id}`.

"Foundry" here is the in-process test app of this repository (the user's own
router), reached through a patched urllib: every outbound request the pipeline
makes is recorded, so the tests can state exactly what leaves the training
store and where it goes. Nothing contacts a network or an external training
provider; the pipeline has no code path to one.

Tests marked xfail(strict=True, reason="DEFECT: ...") assert the CORRECT
behavior for a defect this review found."""

import io
import json
import time
import os
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from foundry_coordinator import training_hook as H
from foundry_training import auto as A
from foundry_training import cli
from tests.training.helpers import KILO_EXPORT, capture_db, capture_rows, coordinator, \
    kilo_export, pipeline, profile, workspace, write_json

PID = "fixture-game"
SESSION = json.loads(KILO_EXPORT.read_text(encoding="utf-8"))["info"]["id"]
KEY = "coord-training-key-123456"
URL = "http://foundry.test"
SECRETS = {"github": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
           "aws": "AKIA" + "ABCDEFGHIJKLMNOP",
           "known": "workstation-known-value-91c4e2"}


def _sprint(tmp_path, leak=False):
    """A finished sprint already ingested from Kilo/Foundry capture; the
    closeout manifest (written by the coordinator's real hook) is what makes
    the automatic run ingest the coordinator store, git and curate."""
    ws, base, head = workspace(tmp_path)
    prof = profile(tmp_path, PID, ws)
    st = coordinator(tmp_path, PID, "FX-1", SESSION, base, head)
    st.update_task(PID, "FX-1", head_sha=head, reviewed_sha=head)
    exp = kilo_export()
    if leak:
        for m in exp["messages"]:
            for part in m["parts"]:
                if part.get("tool") == "bash":
                    part["state"]["output"] = (part["state"]["output"] + "\nenv dump:\n"
                                               + "\n".join(SECRETS.values()))
    p = pipeline(tmp_path, [prof], known=[SECRETS["known"]] if leak else ())
    p.ingest_kilo(exports=[write_json(tmp_path / "s.json", exp)])
    p.ingest_foundry(capture_db=capture_db(tmp_path, capture_rows(exp)))
    spool = tmp_path / "spool"
    P = SimpleNamespace(id=PID, profile=SimpleNamespace(
        raw={"training": {"enabled": True, "spool": str(spool)}}, review={"required": True}))
    return SimpleNamespace(p=p, st=st, P=P, spool=str(spool), prof=prof, tmp=tmp_path)


def closeout(s, outcome="COMPLETE"):
    return H.finalize(s.st, s.P, s.st.task(PID, "FX-1"), outcome, str(s.tmp))


@pytest.fixture()
def sprint(tmp_path):
    return _sprint(tmp_path)


@pytest.fixture()
def foundry(app, client, monkeypatch):
    """Every pipeline HTTP call goes to this test's Foundry app and is recorded."""
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
        state["calls"].append({"method": req.get_method(), "url": req.full_url,
                               "headers": dict(req.header_items()), "body": req.data})
        if state["down"]:
            raise urllib.error.URLError("connection refused")
        path = req.full_url.split("foundry.test", 1)[1]
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


def run(s, **kw):
    return A.auto_run(s.p, s.spool, {"foundry_url": URL, **kw}, log=lambda m: None)


def index(rep):
    return json.loads((Path(rep["library"]) / "INDEX.json").read_text(encoding="utf-8"))


def dataset_posts(state):
    return [c for c in state["calls"] if c["method"] == "POST"
            and "/v1/foundry/training/datasets/" in c["url"]]


# ============================================================================ chain

def test_t1_closeout_to_spool_to_library_to_foundry_upload(sprint, foundry, app):
    m = closeout(sprint)
    # the manifest is a pointer list: ids, revisions, verdicts and the store path, no content
    assert set(m["sessions"]) == {SESSION} and m["coordinator_db"] == sprint.st.path
    spooled = list(Path(sprint.spool).glob(f"{PID}__FX-1__*.json"))
    assert len(spooled) == 1 and "messages" not in spooled[0].read_text()
    rep = run(sprint)
    assert not rep["errors"], rep["errors"]
    assert [q["manifest"] for q in rep["queue"]] == [spooled[0].name]
    assert not list(Path(sprint.spool).glob("*.json"))
    assert (Path(sprint.spool) / "done" / spooled[0].name).exists()
    idx = index(rep)
    sft = next(d for d in idx["datasets"] if (d["kind"], d["target"]) == ("sft", "chatml-tools"))
    assert sft["valid"] and sft["upload"]["status"] == "uploaded"
    lib = Path(rep["library"])
    latest = lib / PID / "sft" / "chatml-tools" / "latest.jsonl"
    assert latest.read_bytes() == Path(sft["data"]).read_bytes()
    assert json.loads((latest.parent / "latest.json").read_text())["dataset_id"] == \
        sft["dataset_id"]
    # what was sent, where: one POST per valid train dataset, to the configured Foundry only
    posts = dataset_posts(foundry)
    assert {p["url"] for p in posts} == {f"{URL}/v1/foundry/training/datasets/{d['dataset_id']}"
                                         for d in idx["datasets"] if d["valid"]}
    assert all(c["url"].startswith(URL + "/") for c in foundry["calls"])
    body = json.loads(posts[0]["body"])
    assert set(body) == {"manifest", "data", "sha256"}
    assert posts[0]["headers"].get("Authorization") == f"Bearer {KEY}"
    # Foundry stored the files on its own disk (no forwarding)
    rows = app.state.services.db.query("SELECT dataset_id, path FROM training_datasets")
    assert {r["dataset_id"] for r in rows} == {d["dataset_id"] for d in idx["datasets"]
                                               if d["valid"]}
    assert all(os.path.isfile(os.path.join(r["path"], "data.jsonl")) for r in rows)


def test_t1_what_leaves_the_machine_is_the_redacted_transcript(tmp_path, foundry):
    s = _sprint(tmp_path, leak=True)
    closeout(s)
    rep = run(s)
    assert not rep["errors"], rep["errors"]
    posts = dataset_posts(foundry)
    assert posts, "a dataset must have been uploaded for this check to mean anything"
    out = b"".join(c["body"] or b"" for c in foundry["calls"])
    for name, val in SECRETS.items():
        assert val.encode() not in out, f"{name} left the training store"
    assert KEY.encode() not in out                       # the bearer key is a header only
    # Honest accounting: the uploaded dataset IS conversation content (redacted):
    # system prompt, user prompt, tool calls and tool outputs of the local model.
    chat = next(json.loads(p["body"]) for p in posts
                if json.loads(p["body"])["manifest"]["target"] == "chatml-tools")
    line = json.loads(chat["data"].splitlines()[0])
    assert set(line) == {"messages", "tools", "meta"}
    roles = {m["role"] for m in line["messages"]}
    assert {"system", "user", "assistant", "tool"} <= roles
    canon = next(json.loads(p["body"]) for p in posts
                 if json.loads(p["body"])["manifest"]["target"] == "canonical")
    cline = json.loads(canon["data"].splitlines()[0])
    assert {"messages", "tools", "diff", "outcome", "flags", "provenance", "revision"} <= set(cline)
    # the status push also leaves: pipeline status and <=300-char episode previews
    other = sorted({c["url"].replace(URL, "") for c in foundry["calls"]
                    if c["method"] in ("PUT", "POST") and c not in posts})
    assert other == ["/v1/foundry/training/episodes/auto", "/v1/foundry/training/status/auto"]
    eps = json.loads(next(c["body"] for c in foundry["calls"]
                          if c["url"].endswith("/episodes/auto")))["episodes"]
    assert all(len(t["text"]) <= 300 for e in eps for t in e["turns"])


# ============================================================================ dedup / refusal

def test_t2_unchanged_episodes_make_no_new_dataset_and_no_new_upload(sprint, foundry):
    closeout(sprint)
    rep = run(sprint)
    n_ds, n_posts = len(index(rep)["datasets"]), len(dataset_posts(foundry))
    assert n_ds and n_posts
    closeout(sprint)                                      # the same task closes again, unchanged
    rep2 = run(sprint)
    assert [q.get("curation") is not None for q in rep2["queue"]] == [True]
    assert not rep2["exports"] and not rep2["uploads"]
    assert len(index(rep2)["datasets"]) == n_ds and len(dataset_posts(foundry)) == n_posts


def _refusing(monkeypatch, calls):
    def refusing(entry, url, key, timeout=120):
        calls.append(entry["dataset_id"])
        return {"status": "refused", "reason": "HTTP 422: secret detected"}
    monkeypatch.setattr(A, "upload", refusing)


def test_t2_refused_upload_is_not_retried(sprint, foundry, monkeypatch):
    calls = []
    _refusing(monkeypatch, calls)
    closeout(sprint)
    rep = run(sprint)
    n = len(calls)
    assert n and all(d["upload"]["status"] == "refused" for d in index(rep)["datasets"])
    run(sprint)
    run(sprint)
    assert len(calls) == n


# fixed in v0.121.3
def test_t2_upload_off_run_does_not_erase_a_permanent_refusal(sprint, foundry, monkeypatch):
    calls = []
    _refusing(monkeypatch, calls)
    closeout(sprint)
    run(sprint)
    n = len(calls)
    run(sprint, upload=False)
    run(sprint)
    assert len(calls) == n


# fixed in v0.121.3
def test_t2_upload_off_run_keeps_uploaded_status(sprint, foundry):
    closeout(sprint)
    rep = run(sprint)
    n = len(dataset_posts(foundry))
    run(sprint, upload=False)
    assert all(d["upload"]["status"] == "uploaded" for d in index(rep)["datasets"])
    run(sprint)
    assert len(dataset_posts(foundry)) == n


# ============================================================================ spool hygiene

def _other_manifest(s):
    st2 = coordinator(s.tmp / "b", "elsewhere", "T", "ses_b", "a" * 40, "b" * 40)
    P2 = SimpleNamespace(id="elsewhere", profile=SimpleNamespace(
        raw={"training": {"enabled": True, "spool": s.spool}}, review={}))
    H.finalize(st2, P2, st2.task("elsewhere", "T"), "FAILED", str(s.tmp))
    return list(Path(s.spool).glob("elsewhere__*.json"))[0]


def test_t3_other_projects_manifest_is_left_and_does_not_spin(sprint, monkeypatch, capsys):
    other = _other_manifest(sprint)
    closeout(sprint)
    from foundry_training.pipeline import Pipeline
    real, n = Pipeline.process_queue, []

    def counting(self, *a, **k):
        n.append(1)
        return real(self, *a, **k)
    monkeypatch.setattr(Pipeline, "process_queue", counting)
    assert cli.main(["--store", str(sprint.p.store.root), "--allow-in-repo", "--profiles",
                     sprint.prof, "auto", "--spool", sprint.spool, "--no-upload"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert other.exists()                                   # never marked done unread
    assert [p.name for p in Path(sprint.spool).glob("*.json")] == [other.name]
    assert len(list((Path(sprint.spool) / "done").glob(f"{PID}__*.json"))) == 1
    assert any(q.get("left") for q in out["queue"]) and out["exports"]
    assert len(n) <= 2                                      # bounded: no re-run for it
    assert not os.path.exists(os.path.join(sprint.spool, ".auto.lock"))


# fixed in v0.121.3
def test_t3_one_malformed_manifest_does_not_block_the_spool(sprint, foundry):
    os.makedirs(sprint.spool, exist_ok=True)
    (Path(sprint.spool) / "000-partial.json").write_text('{"project_id": "fixture-ga')
    closeout(sprint)
    rep = run(sprint)
    assert not list(Path(sprint.spool).glob(f"{PID}__*.json"))     # ours was processed
    assert rep["exports"]


# ============================================================================ lock

def test_t4_lock_of_a_dead_process_is_taken_over_and_a_live_one_respected(tmp_path):
    lock = str(tmp_path / ".auto.lock")
    Path(lock).write_text("999999")
    fd = cli._take_lock(lock)
    assert fd is not None
    os.close(fd)
    os.remove(lock)
    Path(lock).write_text(str(os.getpid()))
    assert cli._take_lock(lock) is None and Path(lock).read_text() == str(os.getpid())


# fixed in v0.121.3 (an empty lock younger than 30 s is its owner's, still writing)
def test_t4_freshly_created_lock_without_a_pid_yet_is_not_taken_over(tmp_path):
    lock = str(tmp_path / ".auto.lock")
    owner = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)   # live owner, pid not yet written
    try:
        fd = cli._take_lock(lock)
        if fd is not None:
            os.close(fd)
        assert fd is None
    finally:
        os.close(owner)


def test_t4_old_empty_lock_is_taken_over(tmp_path):
    lock = str(tmp_path / ".auto.lock")
    Path(lock).write_text("")
    old = time.time() - 120
    os.utime(lock, (old, old))
    fd = cli._take_lock(lock)
    assert fd is not None
    os.close(fd)


# ============================================================================ off switch

@pytest.mark.parametrize("val", ["0", "false", "OFF", " no "])
def test_t5_auto_off_switch_still_writes_the_manifest_but_spawns_nothing(sprint, monkeypatch,
                                                                         val):
    spawned, events = [], []
    monkeypatch.setattr(H, "_spawn", lambda cmd, log, env: spawned.append(cmd))
    monkeypatch.setenv("FOUNDRY_TRAINING_AUTO", val)
    sprint.P.profile.path = sprint.prof
    sprint.P.profile.raw["training"]["auto"] = {"enabled": True}
    closeout(sprint)
    store = SimpleNamespace(event=lambda *a, **k: events.append(a[2]))
    assert H.launch_auto(store, sprint.P, {"task_id": "FX-1"}, str(sprint.tmp)) is False
    assert spawned == [] and events == []
    assert len(list(Path(sprint.spool).glob(f"{PID}__*.json"))) == 1     # queued for later
    monkeypatch.setenv("FOUNDRY_TRAINING_AUTO", "1")
    assert H.launch_auto(store, sprint.P, {"task_id": "FX-1"}, str(sprint.tmp)) is True
    assert spawned and "--profiles" in spawned[0] and "auto" in spawned[0]
    assert events == ["training_auto_started"]
