"""tracks/manager.py + the engine hooks: a track is a git worktree and branch
under <workspace>/.tracks/<name>; sprints queued on it run in that worktree
(their commits land on track/<name>, the main checkout never moves);
max_agents is a live [automation] setting (the main sprint counts first, a
track takes slot 1+); states planned/running/merging/merged/stopped/failed with
track_* events; the kilo.jsonc fallback copy; the CLI `tracks analyze`."""

from __future__ import annotations

import json
import os

import pytest

from foundry_coordinator import states as S
from foundry_coordinator.automation import get as auto_get
from foundry_coordinator.tracks.manager import TrackError
from tests.coordinator.helpers import (by_task, coordinator, git, make_repo, run_until,
                                       write_ok, write_profile)

TASKS = [
    {"id": "T1", "title": "main sprint", "spec": "Write src/a.txt.", "scope": ["src/a.txt"]},
    {"id": "B1", "title": "blender sprint", "spec": "Write art/b.txt.", "scope": ["art/b.txt"],
     "tags": ["needs:blender"]},
    {"id": "B2", "title": "blender sprint 2", "spec": "Write art/b2.txt.", "scope": ["art/b2.txt"],
     "deps": ["B1"], "tags": ["needs:blender"]},
    {"id": "A1", "title": "audio sprint", "spec": "Write docs/a.txt.", "scope": ["docs/a.txt"],
     "tags": ["needs:audio"]},
    {"id": "X1", "title": "shares a file with T1", "spec": "Write src/a.txt.",
     "scope": ["src/a.txt", "src/x.txt"]},
]
EXTRA = """
[automation]
max_agents = 1
"""


def env(tmp_path, extra=EXTRA, scripts=None, max_writers=None):
    root, remote = make_repo(tmp_path, "game", TASKS,
                             extra_files={"kilo.jsonc": '{"mcp": {}}\n'})
    prof = write_profile(tmp_path, "game", root, extra=extra, publish="none", review=False)
    scripts = scripts or {"game": by_task({
        "T1": write_ok("src/a.txt"), "B1": write_ok("art/b.txt"), "B2": write_ok("art/b2.txt"),
        "A1": write_ok("docs/a.txt"), "X1": write_ok("src/x.txt")})}
    c = coordinator(tmp_path, [prof], scripts, max_writers=max_writers)
    return c, c.projects["game"], root


def tracks_events(c, kind=None):
    return [e for e in c.store.events("game", 200)
            if e["kind"].startswith("track") and (kind is None or e["kind"] == kind)]


# ------------------------------------------------------------------ creation

def test_create_makes_a_worktree_and_branch_under_dot_tracks(tmp_path):
    c, P, root = env(tmp_path)
    row = c.tracks.create(P, "blender", ["B1", "B2"], actor="victor")
    assert row["state"] == "planned" and row["kind"] == "parallel"
    assert row["branch"] == "track/blender" and row["sprints"] == ["B1", "B2"]
    assert row["needs"] == ["blender"]                        # from the task tags
    wt = row["worktree"]
    assert os.path.isdir(wt) and os.path.normcase(wt).startswith(
        os.path.normcase(os.path.join(os.path.realpath(root), ".tracks")))
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "track/blender"
    assert git(wt, "rev-parse", "HEAD") == row["base_sha"] == git(root, "rev-parse", "main")
    # the kilo.jsonc fallback copy (no services.track_kilo_config here)
    assert os.path.isfile(os.path.join(wt, "kilo.jsonc"))
    assert row["data"]["kilo_config"].endswith("kilo.jsonc")
    # .tracks/ is excluded, so the main checkout stays clean
    assert git(root, "status", "--porcelain") == ""
    assert tracks_events(c, "track_created")[0]["detail"]["sprints"] == ["B1", "B2"]
    # the main project's Project and the track Project differ only in workspace/branch
    TP = c.tracks.track_project(P, row)
    assert TP.id == "game" and TP.profile.workspace == wt and TP.profile.branch == "track/blender"
    assert TP.profile.resources == ["blender"] and TP.profile.publish == {"mode": "none"}
    assert c.tracks.is_track(TP) and not c.tracks.is_track(P)


def test_create_refuses_bad_names_unknown_sprints_and_duplicates(tmp_path):
    c, P, _ = env(tmp_path)
    with pytest.raises(TrackError, match="track name"):
        c.tracks.create(P, "Bad Name", ["B1"])
    with pytest.raises(TrackError, match="sprint ZZ"):
        c.tracks.create(P, "z", ["ZZ"])
    with pytest.raises(TrackError, match="at least one sprint"):
        c.tracks.create(P, "empty", [])
    c.tracks.create(P, "blender", ["B1"])
    with pytest.raises(TrackError, match="already exists"):
        c.tracks.create(P, "blender", ["B2"])
    c.approve("game", "A1", [], "victor")
    with pytest.raises(TrackError, match="already QUEUED on the main checkout"):
        c.tracks.create(P, "audio", ["A1"])


# ------------------------------------------------------------------ running on a track

def test_a_track_sprint_runs_in_the_worktree_and_master_does_not_move(tmp_path):
    c, P, root = env(tmp_path, extra="\n[automation]\nmax_agents = 2\n")
    assert c.max_writers == 2
    base = git(root, "rev-parse", "main")
    c.tracks.create(P, "blender", ["B1", "B2"], actor="victor")
    row = c.tracks.start(P, "blender", "victor")
    assert row["state"] == "running" and row["current_sprint"] == "B1"
    assert c.store.task_track("game", "B1") == "blender"
    assert c.store.queue_entry("game", "B2")["deps"] == ["B1"]   # a chain
    assert run_until(c, lambda: c.store.task("game", "B2")["state"] == S.COMPLETE, limit=80)
    wt = row["worktree"]
    assert os.path.isfile(os.path.join(wt, "art", "b2.txt"))
    assert not os.path.exists(os.path.join(root, "art", "b.txt"))
    assert git(root, "rev-parse", "main") == base                # main never moved
    assert len(git(wt, "log", "--oneline", f"{base}..HEAD").splitlines()) == 2
    # finished: the track waits to merge at the main sprint's next quiet point
    c.tick()
    row = c.store.track("game", "blender")
    assert row["state"] in ("merging", "merged")
    assert tracks_events(c, "track_ready_to_merge")


def test_max_agents_one_keeps_a_track_sprint_waiting_and_a_live_change_lets_it_run(tmp_path):
    c, P, _ = env(tmp_path)                                    # max_agents = 1
    c.tracks.create(P, "blender", ["B1"])
    c.tracks.start(P, "blender", "victor")
    c.approve("game", "T1", [], "victor")
    for _ in range(4):
        c.tick()
    b1 = c.store.task("game", "B1")
    assert b1["state"] == S.READY and "writer slot" in (b1["next_action"] or "")
    assert "max_agents 1" in b1["next_action"]
    assert c.store.task("game", "T1")["state"] not in (S.QUEUED, S.READY)   # main went first
    # the chat raises the cap live: no restart, no profile reload
    assert c.tracks.set_max_agents(P, 2, "victor") == 2
    assert auto_get(P, "max_agents") == 2 and c.max_writers == 2
    assert c.store.meta("max_agents:game") == "2"
    assert run_until(c, lambda: c.store.task("game", "B1")["state"] == S.COMPLETE, limit=60)
    ev = [e for e in c.store.events("game", 100) if e["kind"] == "max_agents_changed"][0]
    assert ev["detail"] == {"from": 1, "to": 2, "actor": "victor", "override": False}
    with pytest.raises(TrackError):
        c.tracks.set_max_agents(P, 0, "victor")


def test_the_flag_overrides_the_setting_and_a_track_never_takes_slot_zero(tmp_path):
    c, P, _ = env(tmp_path, extra="\n[automation]\nmax_agents = 3\n", max_writers=2)
    assert c.max_writers == 2 and c.tracks.status(P)["max_agents_override"] == 2
    row = c.tracks.create(P, "blender", ["B1"])
    TP = c.tracks.track_project(P, row)
    assert list(c.tracks.writer_slots(TP)) == [1] and list(c.tracks.writer_slots(P)) == [0, 1]


def test_restart_keeps_the_chat_set_max_agents(tmp_path):
    c, P, _ = env(tmp_path)
    c.tracks.set_max_agents(P, 3, "victor")
    store_path = c.store.path
    c2 = coordinator(tmp_path, [tmp_path / "profiles" / "game" / "project.toml"],
                     {"game": by_task({})}, store_path=store_path, max_writers=None)
    assert c2.max_writers == 3
    # the CLI flag still overrides a stored value
    c3 = coordinator(tmp_path, [tmp_path / "profiles" / "game" / "project.toml"],
                     {"game": by_task({})}, store_path=store_path, max_writers=1)
    assert c3.max_writers == 1


# ------------------------------------------------------------------ stop / fail

def test_stop_cancels_the_running_sprint_and_keeps_the_worktree(tmp_path):
    c, P, root = env(tmp_path, extra="\n[automation]\nmax_agents = 2\n")
    c.tracks.create(P, "blender", ["B1", "B2"])
    c.tracks.start(P, "blender", "victor")
    assert run_until(c, lambda: c.store.task("game", "B1")["state"] == S.IMPLEMENTING, limit=10)
    row = c.tracks.stop(P, "blender", "victor", "not now")
    assert row["state"] == "stopped" and row["data"]["stopped"]["reason"] == "not now"
    assert c.store.task("game", "B1")["state"] == S.CANCELLED
    assert c.store.task("game", "B2")["state"] in (S.CANCELLED, S.QUEUED, S.BLOCKED)
    assert os.path.isdir(row["worktree"])                       # kept for inspection
    assert not [l for l in c.store.locks() if l["task_id"] == "track:blender"]
    ev = tracks_events(c, "track_stopped")[0]["detail"]
    assert ev["cancelled"] == ["B1", "B2"] and ev["actor"] == "victor"   # the queued one too
    with pytest.raises(TrackError, match="already stopped"):
        c.tracks.stop(P, "blender")
    # the stopped track's task runs with the main project again (no live track)
    assert c.tracks.project_for(P, {"task_id": "B1"}) is P


def test_a_failed_sprint_fails_the_track(tmp_path):
    def boom(ctx):
        return {"final": '```coordinator-report\n{"status": "BLOCKED", "blocker": "no"}\n```'}
    c, P, _ = env(tmp_path, extra="\n[automation]\nmax_agents = 2\n",
                  scripts={"game": by_task({"B1": boom})})
    c.tracks.create(P, "blender", ["B1"])
    c.tracks.start(P, "blender", "victor")
    assert run_until(c, lambda: c.store.task("game", "B1")["state"] == S.BLOCKED, limit=20)
    c.cancel("game", "B1", "give up", "victor")
    c.tick()
    row = c.store.track("game", "blender")
    assert row["state"] == "failed" and "B1 CANCELLED" in row["data"]["failed"]["why"]
    assert tracks_events(c, "track_failed")


# ------------------------------------------------------------------ analysis / proposal

def test_analyze_and_propose_through_the_manager_and_start_plan(tmp_path):
    c, P, _ = env(tmp_path, extra="\n[automation]\nmax_agents = 3\n")
    c.approve("game", "T1", [], "victor")
    c.tick()                                                     # T1 is the main sprint
    an = c.tracks.analyze(P)
    assert an["tracks"][0]["kind"] == "main" and "T1" in an["tracks"][0]["sprints"]
    assert "X1" in an["tracks"][0]["sprints"]                   # shares src/a.txt with T1
    plan = c.tracks.propose(P, actor="victor")
    assert plan["max_agents"] == 3 and plan["agents"] == 3
    names = sorted(t["name"] for t in plan["tracks"])
    assert names == ["a1", "b1"] and plan["plan_id"].startswith("trp-")
    assert c.tracks.plan(plan["plan_id"])["project"] == "game"
    rows = c.tracks.start_plan(P, plan["plan_id"], "victor")
    assert sorted(r["name"] for r in rows) == ["a1", "b1"]
    assert all(r["state"] == "running" for r in rows)
    assert c.store.task_track("game", "B2") == "b1"
    with pytest.raises(TrackError, match="already started"):
        c.tracks.start_plan(P, plan["plan_id"], "victor")
    with pytest.raises(TrackError, match="no proposal"):
        c.tracks.start_plan(P, "trp-nope", "victor")
    st = c.tracks.status(P)
    assert {t["name"] for t in st["tracks"]} == {"a1", "b1"}
    assert st["main"]["sprint"] == "T1" and st["max_agents"] == 3
    b1 = next(t for t in st["tracks"] if t["name"] == "b1")
    assert [s["task_id"] for s in b1["sprints"]] == ["B1", "B2"]


def test_cli_tracks_analyze_prints_and_status_json(tmp_path, capsys):
    from foundry_coordinator import cli
    c, P, _ = env(tmp_path)
    rc = cli._tracks(c, type("A", (), {"tcmd": "analyze", "project": "game", "json": False})())
    out = capsys.readouterr().out
    assert rc == 0 and "main" in out and "t1" in out and "max_agents 1" in out
    rc = cli._tracks(c, type("A", (), {"tcmd": "status", "project": "game", "json": True})())
    st = json.loads(capsys.readouterr().out)
    assert rc == 0 and st["max_agents"] == 1 and st["tracks"] == []
    rc = cli._tracks(c, type("A", (), {"tcmd": "stop", "project": "game", "name": "nope",
                                       "reason": "", "by": "v"})())
    assert rc == 2 and "no track nope" in capsys.readouterr().err
