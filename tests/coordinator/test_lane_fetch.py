"""A lane on a slot: create, fetch back fast-forward only, save dirty files
(foundry_coordinator/lane_fetch.py), Distributed Foundry phase b B6. The "slot" is this
machine: its lane store is a local path and its agent a LaneHost behind the real routes."""

from __future__ import annotations

import os
import subprocess

import pytest

from foundry_client import lane as L
from foundry_coordinator import lane_fetch as F
from foundry_coordinator.adapters.git import GitError
from foundry_coordinator.adapters.remote_git import LaneAgent, RemoteWorkspace

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True, env=ENV).stdout.strip()


class AppAgent(LaneAgent):
    def __init__(self, host):
        from fastapi.testclient import TestClient
        from foundry_client import api

        class Mgr:
            cfg = {"client": {}}

            def status(self):
                return {"components": []}
        self.c = TestClient(api.create_app(Mgr(), "CT", lane=host, lane_token="LT"),
                            base_url="http://127.0.0.1:47811")
        super().__init__(lambda: {"url": "x", "token": "LT"}, "slot-t")

    def call(self, route, body, timeout=60):
        r = self.c.post("/api/lane/" + route, json=body, headers={"x-lane-token": "LT"})
        if r.status_code >= 400:
            raise GitError(f"HTTP {r.status_code} {r.json().get('error')}")
        return r.json()


@pytest.fixture
def world(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q", "-b", "master")
    (main / "a.txt").write_text("one\n", encoding="utf-8")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "first")
    root = tmp_path / "lanes"
    root.mkdir()
    # the slot's lane store: an empty bare repository (setup-host.sh makes it --shared)
    subprocess.run(["git", "init", "-q", "--bare", str(root / "pilot.git")], check=True)
    slot = {"container": "c", "lanes": True, "lanes_root": str(root).replace("\\", "/")}
    host = L.LaneHost(str(root), str(tmp_path / "state"))
    return {"main": str(main), "slot": slot, "host": host, "agent": AppAgent(host),
            "base": git(main, "rev-parse", "HEAD"), "tmp": tmp_path}


def make(w, lane="l1"):
    return F.create(w["main"], w["slot"], w["agent"], "pilot", lane, w["base"], f"track/{lane}",
                    sleep=lambda s: None)


def test_create_pushes_the_base_and_makes_the_worktree(world):
    r = make(world)
    assert r["ok"], r
    assert r["worktree"] == F.worktree_path(world["slot"], "pilot", "l1")
    assert os.path.isfile(os.path.join(r["worktree"], "a.txt"))
    store = os.path.join(world["slot"]["lanes_root"], "pilot.git")
    assert git(store, "rev-parse", "refs/heads/slot/_base/l1") == world["base"]
    assert git(r["worktree"], "rev-parse", "--abbrev-ref", "HEAD") == "track/l1"


def test_a_second_create_is_refused_and_the_base_never_forced(world):
    assert make(world)["ok"]
    r = make(world)
    assert not r["ok"] and ("one writer" in r["detail"] or "base push" in r["detail"])


def test_fetch_brings_commits_fast_forward_only(world):
    wt = make(world)["worktree"]
    ws = RemoteWorkspace(world["agent"], "pilot", "l1", wt)
    ws.write("b.txt", b"two\n")
    assert ws.commit_paths(["b.txt"], "add b", env={"GIT_AUTHOR_NAME": "t",
                                                     "GIT_AUTHOR_EMAIL": "t@example.com"})
    r = F.fetch(world["main"], world["slot"], "slot-t", "pilot", "l1")
    assert r["ok"] and r["moved"], r
    assert git(world["main"], "rev-parse", "refs/heads/track/l1") == ws.head()
    assert git(world["main"], "rev-parse", "refs/lanes/slot-t/l1") == ws.head()
    again = F.fetch(world["main"], world["slot"], "slot-t", "pilot", "l1")
    assert again["ok"] and not again["moved"]


def test_a_diverged_tip_keeps_both(world):
    wt = make(world)["worktree"]
    ws = RemoteWorkspace(world["agent"], "pilot", "l1", wt)
    ws.write("b.txt", b"two\n")
    ws.commit_paths(["b.txt"], "add b", env={"GIT_AUTHOR_NAME": "t",
                                             "GIT_AUTHOR_EMAIL": "t@example.com"})
    assert F.fetch(world["main"], world["slot"], "slot-t", "pilot", "l1")["ok"]
    # somebody committed to track/l1 here as well
    git(world["main"], "branch", "-f", "side", "track/l1")
    git(world["main"], "checkout", "-q", "track/l1")
    with open(os.path.join(world["main"], "c.txt"), "w", encoding="utf-8") as f:
        f.write("c\n")
    git(world["main"], "add", "c.txt")
    git(world["main"], "commit", "-q", "-m", "here")
    git(world["main"], "checkout", "-q", "master")
    here = git(world["main"], "rev-parse", "track/l1")
    ws.write("d.txt", b"d\n")
    ws.commit_paths(["d.txt"], "add d", env={"GIT_AUTHOR_NAME": "t",
                                             "GIT_AUTHOR_EMAIL": "t@example.com"})
    r = F.fetch(world["main"], world["slot"], "slot-t", "pilot", "l1")
    assert not r["ok"] and r.get("diverged"), r
    assert git(world["main"], "rev-parse", "track/l1") == here           # nothing overwritten
    assert git(world["main"], "rev-parse", "refs/lanes/slot-t/l1") == ws.head()


def test_save_dirty_copies_changed_files_here(world):
    wt = make(world)["worktree"]
    ws = RemoteWorkspace(world["agent"], "pilot", "l1", wt)
    ws.write("new.txt", b"n\n")
    ws.write("a.txt", b"changed\n")
    dest = str(world["tmp"] / "saves")
    r = F.save_dirty(ws, dest)
    assert r["ok"] and r["files"] == 2
    assert open(os.path.join(dest, "new.txt"), "rb").read() == b"n\n"
    assert "a.txt" in open(os.path.join(dest, "_list.txt"), encoding="utf-8").read()
    assert ws.status()                                                    # untouched on the slot


def test_bad_names_and_an_unreachable_agent(world):
    assert not F.push_base(world["main"], world["slot"], "pilot", "../x", world["base"])["ok"]
    assert not F.fetch(world["main"], world["slot"], "slot-t", "pilot", "Bad Name")["ok"]
    down = LaneAgent(lambda: None, "slot-t")
    r = F.wait_job(down, "pilot", "l9", "worktree-add", {}, sleep=lambda s: None)
    assert not r["ok"] and r["state"] == "unreachable"


def test_store_url_for_a_remote_slot():
    s = {"host": "studio@10.0.0.9", "lanes_root": "/srv/foundry-lanes"}
    assert F.store_url(s, "pilot") == "ssh://studio@10.0.0.9/srv/foundry-lanes/pilot.git"
    assert F.worktree_path(s, "pilot", "l1") == "/srv/foundry-lanes/pilot/l1"
