"""BUILD_RERUN_PLAN (build replay R4): "Re-run on a slot" runs a journal entry's commit, script
copy and arguments again on a Blender slot; the outputs stay in the journal (a new `replay`
entry), never in a sprint's worktree. Off by default; one re-run at a time."""

from __future__ import annotations

import io
import json
import os
import tarfile
import threading
import time
from types import SimpleNamespace

import pytest

from foundry_coordinator import build_journal as BJ
from foundry_coordinator import build_rerun as BR
from foundry_coordinator import control as CTRL

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
COMMIT = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"
SLOTS = {"slot-1": {"host": "studio@10.0.0.5", "container": "studio-slot-1"}}


def profile(tmp_path, on=True, **bl):
    return SimpleNamespace(id="proj", workspace=str(tmp_path / "ws"), raw={
        "blender": {"journal_rerun": on, "pool": ["slot-1:2", "local:1"], **bl},
        "slots": SLOTS})


def journal_entry(state, commit=COMMIT, run_id="slot-1"):
    d = BJ.run_dir(state, "proj", "AS-K1", run_id)
    os.makedirs(d, exist_ok=True)
    e = {"run_id": run_id, "args": ["-b", "-P", "tools/blender/build_k.py", "--", "--save",
                                    "tmp/k.blend"],
         "where": {"slot": "slot-1", "commit": commit} if commit else {"slot": "slot-1"},
         "script": {"path": "tools/blender/build_k.py", "copy": True}}
    with open(os.path.join(d, "journal.json"), "w", encoding="utf-8") as f:
        json.dump(e, f)
    with open(os.path.join(d, "script.txt"), "w", encoding="utf-8") as f:
        f.write("print('the script as it was at the run')\n")
    return e


class Store:
    def __init__(self):
        self.events = []

    def event(self, pid, tid, kind, detail=None):
        self.events.append((kind, detail or {}))


def test_plan_refuses_when_off_without_commit_or_slot(tmp_path):
    state = str(tmp_path / "state")
    journal_entry(state)
    with pytest.raises(BR.RerunError, match="journal_rerun"):
        BR.plan(profile(tmp_path, on=False), state, "proj", "AS-K1", "slot-1")
    with pytest.raises(BR.RerunError, match="no journal entry"):
        BR.plan(profile(tmp_path), state, "proj", "AS-K1", "nope")
    journal_entry(state, commit="", run_id="local-1")
    with pytest.raises(BR.RerunError, match="no commit"):
        BR.plan(profile(tmp_path), state, "proj", "AS-K1", "local-1")
    noslot = profile(tmp_path, pool=["local:1"])
    with pytest.raises(BR.RerunError, match="no Blender slot"):
        BR.plan(noslot, state, "proj", "AS-K1", "slot-1")
    p = BR.plan(profile(tmp_path), state, "proj", "AS-K1", "slot-1")
    assert p["commit"] == COMMIT and p["slot"]["id"] == "slot-1" and p["slot"]["jobs"] == 2
    assert p["script_copy"].endswith("script.txt")


def test_one_rerun_at_a_time_and_events(tmp_path):
    state = str(tmp_path / "state")
    journal_entry(state)
    gate, st = threading.Event(), Store()

    def slow(*a):
        gate.wait(5)
        return {"exit": 0}
    out = BR.start(profile(tmp_path), state, "proj", "AS-K1", "slot-1", store=st, runner=slow)
    assert out["replay_run_id"].startswith("replay-") and out["slot"] == "slot-1"
    with pytest.raises(BR.RerunError, match="one at a time"):
        BR.start(profile(tmp_path), state, "proj", "AS-K1", "slot-1", runner=slow)
    gate.set()
    for _ in range(100):
        if not BR.running():
            break
        time.sleep(0.05)
    assert not BR.running()
    kinds = [k for k, _ in st.events]
    assert kinds[0] == "build_rerun_started" and "build_rerun_done" in kinds


class FakeProc:
    def __init__(self, tar_bytes, err):
        self.stdin = io.BytesIO()
        self.stdin.close = lambda: None
        self.stdout = io.BytesIO(tar_bytes)
        self.stderr = io.BytesIO(err)

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def test_run_pushes_the_commit_sends_the_script_copy_and_records_a_replay(tmp_path, monkeypatch):
    state = str(tmp_path / "state")
    journal_entry(state)
    os.makedirs(tmp_path / "ws")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        ti = tarfile.TarInfo("docs/evidence/AS-K1/views/K_front.png")
        ti.size = len(PNG)
        tf.addfile(ti, io.BytesIO(PNG))
    err = (b"foundry-blender: running on slot job 1 of 2 with 4 threads: blender -b\n"
           b"MESH_REPORT PASS: 3 meshes\nfoundry-blender: blender exit 0\n")
    calls = {}

    def fake_run(argv, **kw):
        calls["push"] = argv
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    def fake_popen(argv, **kw):
        calls["exec"] = argv
        return FakeProc(buf.getvalue(), err)
    monkeypatch.setattr(BR.subprocess, "run", fake_run)
    monkeypatch.setattr(BR.subprocess, "Popen", fake_popen)
    prof = profile(tmp_path, library="")
    p = BR.plan(prof, state, "proj", "AS-K1", "slot-1")
    out = BR.run(prof, state, "proj", "AS-K1", "slot-1", "replay-x", p)
    assert out["exit"] == 0 and out["files_back"] == 1 and out["recorded"]
    assert calls["push"][-1] == f"{COMMIT}:refs/heads/{BR.BRANCH}"
    new = BJ.run_dir(state, "proj", "AS-K1", "replay-x")
    e = json.load(open(os.path.join(new, "journal.json"), encoding="utf-8"))
    assert e["source"] == "replay" and e["where"]["replay_of"] == "slot-1"
    assert e["where"]["commit"] == COMMIT
    assert any("journal's copy" in n for n in e["notes"])
    assert os.path.isfile(os.path.join(new, "out", "docs", "evidence", "AS-K1", "views",
                                       "K_front.png"))
    assert not os.path.exists(tmp_path / "ws" / "docs")       # never the worktree
    assert open(os.path.join(new, "out", "tools", "blender", "build_k.py"),
                encoding="utf-8").read().startswith("print('the script as it was")


def test_the_control_action_rejects_when_off(tmp_path):
    state = str(tmp_path / "state")
    journal_entry(state)
    reported = []
    me = SimpleNamespace(
        c=SimpleNamespace(projects={"proj": SimpleNamespace(profile=profile(tmp_path, on=False))},
                          state_dir=state),
        store=Store(), _report=lambda a, st, d, r=None: reported.append((st, d)))
    a = {"project_id": "proj", "task_id": "AS-K1", "payload": {"run_id": "slot-1"}}
    CTRL.ControlChannel._do_build_rerun(me, a, {}, "victor")
    assert reported[0][0] == "rejected" and "journal_rerun" in reported[0][1]
