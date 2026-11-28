"""Distributed Foundry phase d: a remote lane is brought home (fetched, saved, its mirror a full
checkout) and then merges as a local track; its slot worktree goes after the merge; a slot that
is away gets one notice and nothing is moved by itself."""

from __future__ import annotations

import os
import subprocess
import types

import pytest

from foundry_coordinator.adapters.git import Workspace
from foundry_coordinator.adapters.remote_git import RemoteWorkspace
from tests.coordinator.test_lane_track import world  # noqa: F401  (the fixture)

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True).stdout.strip()


def lane_with_a_commit(world, name="remote1"):
    c, P = world["c"], world["P"]
    row = c.tracks.create(P, name, ["T1"], actor="victor", slot="slot-4")
    sess = RemoteWorkspace(world["agent"], P.id, name, row["data"]["remote_worktree"])
    sess.write("src/a.txt", b"on the slot\n")
    sha = sess.commit_paths(["src/a.txt"], "T1: a", env=ENV)
    return row, sess, sha


def test_bring_home_makes_it_a_local_track(world):
    c, P = world["c"], world["P"]
    row, sess, sha = lane_with_a_commit(world)
    sess.write("wip.txt", b"uncommitted\n")
    out = c.tracks.bring_home(P, "remote1", actor="victor")
    assert out["ok"], out
    row = c.store.track(P.id, "remote1")
    d = row["data"]
    assert "host" not in d and d["was_host"] == "slot-4" and d["home_saves"]
    assert open(os.path.join(d["home_saves"], "wip.txt"), "rb").read() == b"uncommitted\n"
    # the mirror is now the track's full worktree, on its branch at the lane's tip
    assert git(row["worktree"], "rev-parse", "HEAD") == sha
    assert git(row["worktree"], "config", "--worktree", "--get", "core.sparseCheckout") in ("", "false")
    TP = c.tracks.track_project(P, row)
    assert type(TP.ws) is Workspace and TP.ws.path == row["worktree"]
    assert not TP.profile.__dict__["lane_host"]
    assert c.tracks.bring_home(P, "remote1")["detail"].endswith("already here")


def test_merging_brings_a_remote_lane_home_first():
    from foundry_coordinator.tracks.merge import MergeManager
    calls, waits = [], []
    mm = MergeManager.__new__(MergeManager)
    rows = {"l1": {"name": "l1", "state": "merging", "branch": "track/l1",
                   "data": {"host": "slot-4", "merge_stage": "wait"}}}
    mm.tm = types.SimpleNamespace(
        main_of=lambda P: P,
        bring_home=lambda P, name, actor, why: calls.append((name, why)) or {
            "ok": False, "detail": "slot-4: its last commits are not here yet"})
    mm.store = types.SimpleNamespace(track=lambda pid, name: rows[name])
    mm._wait = lambda P, row, why: waits.append(why)
    mm.step(types.SimpleNamespace(id="pilot"), rows["l1"])
    assert calls == [("l1", "merge")] and "not here yet" in waits[0]


def test_after_the_merge_the_slot_worktree_goes(world):
    c, P = world["c"], world["P"]
    row, sess, sha = lane_with_a_commit(world)
    assert c.tracks.bring_home(P, "remote1")["ok"]
    row = c.store.track(P.id, "remote1")
    slot_wt = row["data"]["remote_worktree"]
    c.tracks.remove_worktree(P, row, "merged")
    assert not os.path.exists(slot_wt)
    assert c.store.track(P.id, "remote1")["data"]["slot_worktree_removed"] is True


def test_a_slot_that_is_away_gets_one_notice_and_nothing_moves(world, monkeypatch):
    c, P = world["c"], world["P"]
    row, sess, sha = lane_with_a_commit(world)
    from foundry_coordinator import lane_fetch as LF
    monkeypatch.setattr(LF, "fetch", lambda *a, **k: {"ok": False, "detail": "ssh: no route"})
    now = [1000.0]
    monkeypatch.setattr(c.tracks, "clock", lambda: now[0])
    for minutes in (0, 10, 31, 40):
        now[0] = 1000.0 + minutes * 60
        c.tracks.lane_fetch(P, c.store.track(P.id, "remote1"), force=True)
    notes = [e for e in c.store.events("game", 300) if e["kind"] == "operator_notice"
             and (e["detail"] or {}).get("kind") == "slot_away"]
    assert len(notes) == 1 and "bring-home game remote1 --force-local" in notes[0]["detail"]["text"]
    assert c.store.track(P.id, "remote1")["data"]["host"] == "slot-4"     # nothing moved
    # bring-home refuses without the fetch, unless the owner forces it
    assert not c.tracks.bring_home(P, "remote1")["ok"]
    forced = c.tracks.bring_home(P, "remote1", force_local=True)
    assert forced["ok"] and c.store.track(P.id, "remote1")["data"]["home_forced"] is True


def test_when_the_slot_is_back_the_mark_goes(world, monkeypatch):
    c, P = world["c"], world["P"]
    row, sess, sha = lane_with_a_commit(world)
    from foundry_coordinator import lane_fetch as LF
    real = LF.fetch
    monkeypatch.setattr(LF, "fetch", lambda *a, **k: {"ok": False, "detail": "down"})
    c.tracks.lane_fetch(P, c.store.track(P.id, "remote1"), force=True)
    assert c.store.track(P.id, "remote1")["data"].get("away_since")
    monkeypatch.setattr(LF, "fetch", real)
    c.tracks.lane_fetch(P, c.store.track(P.id, "remote1"), force=True)
    assert not c.store.track(P.id, "remote1")["data"].get("away_since")
    assert [e for e in c.store.events("game", 300) if e["kind"] == "track_lane_back"]


def test_the_cli_has_bring_home():
    from foundry_coordinator import cli
    import inspect
    src = inspect.getsource(cli)
    assert 'add_parser("bring-home"' in src and "--force-local" in src
