"""Review fixes 2026-11-27 (Distributed Foundry) through the real TrackManager: bring-home only
with every file saved, the merge step's fetch throttled, a lane that could not be taken up is
removed again, the Studio gate fetches before it pushes, and {python} follows the slot's OS."""

from __future__ import annotations

import os
import subprocess
import types

import pytest

from foundry_coordinator.adapters.git import GitError
from foundry_coordinator.adapters.remote_git import RemoteWorkspace
from foundry_coordinator.tracks import manager as M
from tests.coordinator.test_lane_track import world  # noqa: F401  (the fixture)

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                          text=True).stdout.strip()


def lane(world, name="remote1"):
    c, P = world["c"], world["P"]
    row = c.tracks.create(P, name, ["T1"], actor="victor", slot="slot-4")
    sess = RemoteWorkspace(world["agent"], P.id, name, row["data"]["remote_worktree"])
    return row, sess


def test_bring_home_refuses_while_a_file_could_not_be_saved(world, monkeypatch):
    c, P = world["c"], world["P"]
    row, sess = lane(world)
    sess.write("wip.txt", b"keep me\n")
    from foundry_coordinator import lane_fetch as LF
    real = LF.save_dirty
    monkeypatch.setattr(LF, "save_dirty", lambda ws, dest: {
        **real(ws, dest), "ok": False, "failed": ["wip.txt"], "detail": "NOT saved: wip.txt"})
    out = c.tracks.bring_home(P, "remote1", actor="victor")
    assert not out["ok"] and "not every uncommitted file" in out["detail"]
    assert c.store.track(P.id, "remote1")["data"]["host"] == "slot-4"       # nothing changed


def test_after_the_merge_the_slot_worktree_goes_with_exactly_the_saved_files(world):
    c, P = world["c"], world["P"]
    row, sess = lane(world)
    slot_wt = row["data"]["remote_worktree"]
    sess.write("wip.txt", b"uncommitted\n")
    os.remove(os.path.join(slot_wt, "tasks.json"))                 # a deleted tracked file
    assert c.tracks.bring_home(P, "remote1", actor="victor")["ok"]
    row = c.store.track(P.id, "remote1")
    saves = row["data"]["home_saves"]
    assert open(os.path.join(saves, "wip.txt"), "rb").read() == b"uncommitted\n"
    c.tracks.remove_worktree(P, row, "merged")
    assert not os.path.exists(slot_wt)
    assert c.store.track(P.id, "remote1")["data"]["slot_worktree_removed"] is True


def test_a_file_changed_after_the_save_keeps_the_slot_worktree(world):
    c, P = world["c"], world["P"]
    row, sess = lane(world)
    slot_wt = row["data"]["remote_worktree"]
    assert c.tracks.bring_home(P, "remote1", actor="victor")["ok"]
    sess.write("late.txt", b"made after the save\n")
    c.tracks.remove_worktree(P, c.store.track(P.id, "remote1"), "merged")
    assert os.path.isdir(slot_wt)
    assert c.store.track(P.id, "remote1")["data"]["slot_worktree_removed"] is False


def test_the_merge_step_tries_an_away_slot_at_most_once_a_minute(world, monkeypatch):
    c, P = world["c"], world["P"]
    lane(world)
    from foundry_coordinator import lane_fetch as LF
    tries = []
    monkeypatch.setattr(LF, "fetch", lambda *a, **k: tries.append(1) or {
        "ok": False, "detail": "ssh: no route to host"})
    now = [5000.0]
    monkeypatch.setattr(c.tracks, "clock", lambda: now[0])
    for _ in range(5):
        out = c.tracks.bring_home(P, "remote1", actor="merge", why="merge")
        assert not out["ok"] and "not here yet" in out["detail"]
    assert len(tries) == 1
    now[0] += 61
    c.tracks.bring_home(P, "remote1", actor="merge", why="merge")
    assert len(tries) == 2
    # by hand (tracks bring-home) it always tries
    c.tracks.bring_home(P, "remote1", actor="victor")
    assert len(tries) == 3


def test_a_mirror_failure_after_the_slot_worktree_is_made_removes_it_again(world, monkeypatch):
    c, P = world["c"], world["P"]
    from foundry_coordinator import lane_fetch as LF
    real = LF.mirror_create
    monkeypatch.setattr(LF, "mirror_create", lambda *a, **k: {"ok": False, "path": "",
                                                               "detail": "disk full"})
    with pytest.raises(M.TrackError, match="removed again; try again") as e:
        c.tracks.create(P, "remote1", ["T1"], actor="victor", slot="slot-4")
    assert "tracks stop" not in str(e.value)
    slot_wt = os.path.join(world["lanes"], "game", "remote1")
    assert not os.path.exists(slot_wt) and "game/remote1" not in world["host"].lanes()
    monkeypatch.setattr(LF, "mirror_create", real)
    row = c.tracks.create(P, "remote1", ["T1"], actor="victor", slot="slot-4")
    assert os.path.isdir(row["data"]["remote_worktree"])


def test_a_lane_registers_the_projects_checks(world):
    row, _ = lane(world)
    checks = world["host"].lanes()["game/remote1"]["checks"]
    assert checks and set(checks) == {c.name for c in world["P"].profile.checks}


def test_a_failure_after_the_slot_answered_is_not_the_slot_away(world, monkeypatch):
    c, P = world["c"], world["P"]
    lane(world)
    from foundry_coordinator import lane_fetch as LF
    monkeypatch.setattr(LF, "fetch", lambda *a, **k: {
        "ok": False, "reached": True, "detail": "the read mirror did not fast-forward"})
    c.tracks.lane_fetch(P, c.store.track(P.id, "remote1"), force=True)
    assert not c.store.track(P.id, "remote1")["data"].get("away_since")


def test_two_projects_with_other_settings_for_one_slot_get_no_lane(world, monkeypatch):
    c, P = world["c"], world["P"]
    from foundry_coordinator import lane_tunnel
    monkeypatch.setattr(lane_tunnel, "_CONFLICTS", {
        ("game", "slot-4"): "slot slot-4 already has a lane tunnel for project au with other "
                            "settings (host)"})
    with pytest.raises(M.TrackError, match="already has a lane tunnel for project au"):
        c.tracks.create(P, "remote1", ["T1"], actor="victor", slot="slot-4")
    assert c.store.track(P.id, "remote1") is None


# ---------------------------------------------------------------------------- 5 Studio gate

class Stop(Exception):
    pass


def gate(world, fetch_out):
    """StudioCalls._start for a lane on a slot, up to the command it would run."""
    from foundry_coordinator.studio_calls import StudioCalls
    c = world["c"]
    row, _ = lane(world)
    TP = c.tracks.track_project(world["P"], c.store.track(world["P"].id, "remote1"))
    notes, seen = [], {}
    sc = StudioCalls.__new__(StudioCalls)
    sc.c = types.SimpleNamespace(_held=lambda *a: False, _acquire=lambda *a: False,
                                 _note=lambda P, t, text: notes.append(text), tracks=c.tracks,
                                 state_dir=str(world["root"].parent / "gate-state"))
    sc._slot = lambda P: {"container": "studio-slot"}
    sc._tier = lambda P, tid: "fast"
    sc._files = lambda P, t, head: []

    def command(P, tid, req, files, run_id, tier, head=""):
        seen["head"] = head
        raise Stop()
    sc._command = command
    calls = []
    c.tracks.lane_fetch = lambda P, row, force=False, retry_gap=0: calls.append(
        (force, retry_gap)) or fetch_out
    return sc, TP, notes, seen, calls


def test_the_studio_gate_fetches_the_lane_first_and_gates_its_fetched_tip(world):
    sc, TP, notes, seen, calls = gate(world, {"ok": True, "tip": "f" * 40})
    with pytest.raises(Stop):
        sc._start(TP, {"task_id": "T1"}, {"track": "remote1", "req": {}})
    assert calls == [(True, 60)] and seen["head"] == "f" * 40


def test_the_studio_gate_waits_while_the_lanes_commits_are_not_here(world):
    sc, TP, notes, seen, calls = gate(world, {"ok": False, "detail": "ssh: no route"})
    assert sc._start(TP, {"task_id": "T1"}, {"track": "remote1", "req": {}}) is False
    assert "head" not in seen and "not here yet" in notes[-1] and "no route" in notes[-1]


# ---------------------------------------------------------------------------- {python} on Windows

def test_a_windows_slots_checks_get_python_not_python3(monkeypatch):
    from foundry_coordinator.adapters import project as PR
    seen = []
    monkeypatch.setattr("foundry_coordinator.adapters.verify_cmd.run_check_remote",
                        lambda *a, **kw: seen.append(kw) or "R")
    a = PR.GenericAdapter.__new__(PR.GenericAdapter)
    prof = types.SimpleNamespace(id="pilot", workspace="C:/AU/.lanes/slot-w/l1",
                                 raw={"slots": {"slot-w": {"os": "windows", "lanes": True}}},
                                 checks=[types.SimpleNamespace(phase="verify")])
    prof.__dict__.update(lane_host="slot-w", track="l1",
                         remote_workspace="C:/Foundry/lanes/pilot/l1")
    a.p, a.state_dir = prof, "s"
    assert a.verify(types.SimpleNamespace(task_id="T1"), "verify", "sha", "base") == ["R"]
    assert seen == [{"slot_os": "windows"}]
