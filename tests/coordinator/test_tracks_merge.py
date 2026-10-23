"""tracks/merge.py: a finished track merges only at a quiescent point of the
main sprint (rebase onto master, fast-forward, the project's checks on the
merged files in the main checkout, track_merged, worktree removed; the merge
commits are the coordinator's for the main sprint's scope checks); a rebase
conflict opens a decision whose default stops the track; a red merge gate
fails the track with the findings; the operator can request the merge early."""

from __future__ import annotations

import os

import pytest

from foundry_coordinator import states as S
from foundry_coordinator.bookkeeping import KEY as COORD_COMMITS
from foundry_coordinator.tracks.manager import TrackError
from foundry_coordinator.tracks.merge import DECISION_DEFAULT
from tests.coordinator.helpers import (by_task, coordinator, git, make_repo, run_until,
                                       write_ok, write_profile)

TASKS = [
    {"id": "T1", "title": "main sprint", "spec": "Write src/a.txt.", "scope": ["src/a.txt"]},
    {"id": "T2", "title": "main sprint 2", "spec": "Write src/c.txt.", "scope": ["src/c.txt"]},
    {"id": "B1", "title": "track sprint", "spec": "Write art/b.txt.", "scope": ["art/b.txt"],
     "tags": ["needs:blender"]},
    {"id": "C1", "title": "conflicting sprint", "spec": "Write src/a.txt.", "scope": ["src/a.txt"]},
]
EXTRA = """
[automation]
max_agents = 2
decision_timeout_minutes = 30
"""


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def env(tmp_path, scripts=None, extra=EXTRA):
    root, _ = make_repo(tmp_path, "game", TASKS)
    prof = write_profile(tmp_path, "game", root, extra=extra, publish="none", review=False)
    clock = Clock()
    scripts = scripts or {"game": by_task({
        "T1": write_ok("src/a.txt"), "T2": write_ok("src/c.txt"), "B1": write_ok("art/b.txt"),
        "C1": write_ok("src/a.txt", "OK by C1")})}
    c = coordinator(tmp_path, [prof], scripts, clock=clock, max_writers=None)
    return c, c.projects["game"], clock, root


def events(c, kind):
    return [e for e in c.store.events("game", 300) if e["kind"] == kind]


def finish_track(c, P, name, sprints, limit=80):
    c.tracks.create(P, name, sprints)
    c.tracks.start(P, name, "victor")
    assert run_until(c, lambda: all(c.store.task("game", s)["state"] == S.COMPLETE
                                    for s in sprints), limit=limit)


def test_happy_path_merges_at_a_quiet_point_and_cleans_up(tmp_path):
    c, P, clock, root = env(tmp_path)
    base = git(root, "rev-parse", "main")
    finish_track(c, P, "blender", ["B1"])
    assert run_until(c, lambda: c.store.track("game", "blender")["state"] == "merged", limit=6)
    row = c.store.track("game", "blender")
    assert git(root, "rev-parse", "main") == row["merged_sha"] != base
    assert os.path.isfile(os.path.join(root, "art", "b.txt"))
    assert not os.path.isdir(row["worktree"]) and row["data"]["worktree_removed"]
    assert git(root, "rev-parse", "--verify", "refs/heads/track/blender")   # the branch stays
    assert row["data"]["merged_files"] == ["art/b.txt"]
    assert row["data"]["gate"]["result"] == "PASS" and row["data"]["gate"]["studio"] is False
    assert row["data"]["gate"]["checks"] == {"project-check": "PASS"}
    kinds = [e["kind"] for e in c.store.events("game", 300)]
    for k in ("track_rebased", "track_fast_forwarded", "track_merged", "track_worktree_removed"):
        assert k in kinds, k
    m = events(c, "track_merged")[0]["detail"]
    assert m["files"] == 1 and m["sprints"] == ["B1"] and m["gate"] == "PASS"
    assert git(root, "status", "--porcelain") == ""


def test_merge_waits_while_the_main_sprint_is_live_then_exempts_its_commits(tmp_path):
    c, P, clock, root = env(tmp_path)
    c.approve("game", "T1", [], "victor")
    c.tick()                                                     # T1 QUEUED -> READY
    c.tick()                                                     # T1 READY -> IMPLEMENTING
    assert c.store.task("game", "T1")["state"] == S.IMPLEMENTING
    c.tracks.create(P, "blender", ["B1"])
    c.tracks.start(P, "blender", "victor")
    # the scripted T1 session answers on its first poll; keep it "live" by
    # holding the task IMPLEMENTING: the track finishes and must wait
    main_sid = c.store.task("game", "T1")["session_id"]
    sess = c.projects["game"].client.sessions[main_sid]
    sess["pending"] = False                                      # never answers (busy)
    assert run_until(c, lambda: c.store.task("game", "B1")["state"] == S.COMPLETE, limit=60)
    for _ in range(3):
        c.tick()
    row = c.store.track("game", "blender")
    assert row["state"] == "merging" and row["data"]["merge_stage"] == "wait"
    assert "main sprint busy: T1 IMPLEMENTING" in row["data"]["merge_wait"]
    assert events(c, "track_merge_waiting")
    assert not os.path.isfile(os.path.join(root, "art", "b.txt"))
    # the main session finishes its turn: T1 moves on (VERIFYING...), still not quiet
    sess["pending"] = True
    assert run_until(c, lambda: c.store.task("game", "T1")["state"] == S.COMPLETE, limit=60)
    # quiet now: the merge lands; T1 is COMPLETE so nothing to exempt, but a
    # second main sprint queued before the merge gets the merge commits as the
    # coordinator's own (B1 only_coordinator)
    c.approve("game", "T2", [], "victor")
    assert run_until(c, lambda: c.store.track("game", "blender")["state"] == "merged", limit=10)
    shas = c.store.track("game", "blender")["data"]["merged_commits"]
    assert shas and set(shas) <= set(c.store.task("game", "T2")["data"].get(COORD_COMMITS) or [])
    assert run_until(c, lambda: c.store.task("game", "T2")["state"] == S.COMPLETE, limit=60)


def test_conflict_opens_a_decision_whose_default_stops_the_track(tmp_path):
    c, P, clock, root = env(tmp_path)
    c.tracks.create(P, "conf", ["C1"])
    c.tracks.start(P, "conf", "victor")
    # the main sprint changes the same file meanwhile
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: c.store.task("game", "T1")["state"] == S.COMPLETE
                     and c.store.task("game", "C1")["state"] == S.COMPLETE, limit=80)
    assert run_until(c, lambda: (c.store.track("game", "conf")["data"].get("decision") or {})
                     .get("status") == "open", limit=6)
    row = c.store.track("game", "conf")
    assert row["state"] == "merging" and row["data"]["conflict"]["files"] == ["src/a.txt"]
    d = row["data"]["decision"]
    assert d["default"] == DECISION_DEFAULT and d["deadline_ts"] == clock() + 30 * 60
    assert "src/a.txt" in d["question"]
    assert row["data"]["conflict"]["lead_repair"] == "no lead repair available"
    assert events(c, "track_conflict")[0]["detail"]["default"] == DECISION_DEFAULT
    # the worktree is left rebase-free (aborted) and master untouched by the track
    assert git(row["worktree"], "status", "--porcelain") == ""
    assert "OK by C1" not in open(os.path.join(root, "src", "a.txt")).read()
    with pytest.raises(TrackError, match="option must be"):
        c.tracks.merger.decide(P, "conf", "something else", "victor")
    # the timeout applies the default: stop this track
    clock.t += 31 * 60
    c.tick()
    row = c.store.track("game", "conf")
    assert row["state"] == "stopped" and row["data"]["decision"]["status"] == "answered"
    assert row["data"]["decision"]["by"] == "coordinator (decision timeout)"
    assert os.path.isdir(row["worktree"])


def test_conflict_decision_retry_after_the_operator_fixed_master(tmp_path):
    c, P, clock, root = env(tmp_path)
    c.tracks.create(P, "conf", ["C1"])
    c.tracks.start(P, "conf", "victor")
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: (c.store.track("game", "conf")["data"].get("decision") or {})
                     .get("status") == "open", limit=90)
    # the operator removes master's edit, then retries the merge
    git(root, "rm", "-q", "src/a.txt")
    git(root, "commit", "-q", "-m", "T1: drop src/a.txt for the track")
    row = c.tracks.merger.decide(P, "conf", "retry the merge", "victor")
    assert row["state"] == "merging" and row["data"]["merge_stage"] == "wait"
    assert run_until(c, lambda: c.store.track("game", "conf")["state"] == "merged", limit=8)
    assert "OK by C1" in open(os.path.join(root, "src", "a.txt")).read()


def test_a_red_merge_gate_fails_the_track_with_findings(tmp_path):
    c, P, clock, root = env(tmp_path)
    c.tracks.create(P, "blender", ["B1"])
    c.tracks.start(P, "blender", "victor")
    # master gains a file verify.py rejects (an operator commit); the track's own
    # checks in its worktree never see it, the merge result does
    os.makedirs(os.path.join(root, "src"), exist_ok=True)
    with open(os.path.join(root, "src", "a.txt"), "w") as f:
        f.write("not ok at all")
    git(root, "add", "src/a.txt")
    git(root, "commit", "-q", "-m", "T1: a bad file on master")
    assert run_until(c, lambda: c.store.task("game", "B1")["state"] == S.COMPLETE, limit=80)
    assert run_until(c, lambda: c.store.track("game", "blender")["state"] == "failed", limit=6)
    row = c.store.track("game", "blender")
    assert row["data"]["gate"]["result"] == "FAIL"
    assert "merge gate red" in row["data"]["failed"]["why"]
    assert row["data"]["gate"]["checks"] == {"project-check": "FAIL"}
    assert git(root, "rev-parse", "main") == row["data"]["merge_head"]   # landed, not reverted
    assert os.path.isdir(row["worktree"])
    # the operator may retry the merge gate after fixing master (every sprint is COMPLETE)
    row = c.tracks.merger.request(P, "blender", "victor")
    assert row["state"] == "merging" and row["data"]["merge_stage"] == "wait"


def test_merge_request_before_the_sprints_finish_is_refused_and_after_is_honored(tmp_path):
    c, P, clock, root = env(tmp_path)
    c.tracks.create(P, "blender", ["B1"])
    c.tracks.start(P, "blender", "victor")
    with pytest.raises(TrackError, match="not finished"):
        c.tracks.merger.request(P, "blender", "victor")
    assert run_until(c, lambda: c.store.track("game", "blender")["state"] == "merged", limit=60)
    with pytest.raises(TrackError, match="already merged"):
        c.tracks.merger.request(P, "blender", "victor")
    with pytest.raises(TrackError, match="no track"):
        c.tracks.merger.request(P, "nope", "victor")


def test_merge_waits_on_a_dirty_main_checkout(tmp_path):
    c, P, clock, root = env(tmp_path)
    c.tracks.create(P, "blender", ["B1"])
    c.tracks.start(P, "blender", "victor")
    os.makedirs(os.path.join(root, "src"), exist_ok=True)
    with open(os.path.join(root, "src", "a.txt"), "w") as f:
        f.write("operator edit, uncommitted")
    assert run_until(c, lambda: c.store.task("game", "B1")["state"] == S.COMPLETE, limit=80)
    for _ in range(3):
        c.tick()
    row = c.store.track("game", "blender")
    assert row["state"] == "merging" and "uncommitted changes" in row["data"]["merge_wait"]
    os.remove(os.path.join(root, "src", "a.txt"))
    assert run_until(c, lambda: c.store.track("game", "blender")["state"] == "merged", limit=6)
