"""0.145.3: a track that merged master is not asked to reword master's commits
(GF-NAME1 journaled commits_not_reworded for GF-GATE1's commits every verify)."""

from __future__ import annotations

import os

from foundry_coordinator import commits as CM
from tests.coordinator.helpers import git
from tests.coordinator.test_tracks_manager import env as tracks_env


def test_reword_skips_commits_a_track_merged_from_main(tmp_path):
    c, P, root = tracks_env(tmp_path, extra="\n[automation]\nmax_agents = 2\n")
    row = c.tracks.create(P, "blender", ["B1"], actor="victor")
    TP = c.tracks.track_project(P, row)
    wt = row["worktree"]
    base = git(wt, "rev-parse", "HEAD")
    os.makedirs(f"{wt}/art", exist_ok=True)
    open(f"{wt}/art/own.txt", "w").write("track work")
    git(wt, "add", "art/own.txt")
    git(wt, "commit", "-q", "--no-verify", "-m", "B1: own work")
    # another sprint's commit on main, without this task's prefix, merged into the track
    os.makedirs(f"{root}/src", exist_ok=True)
    open(f"{root}/src/main_only.txt", "w").write("main sprint work")
    git(root, "add", "src/main_only.txt")
    git(root, "commit", "-q", "--no-verify", "-m", "T1: main work")
    git(wt, "merge", "-q", "--no-edit", "--no-ff", "main")
    t = {"task_id": "B1", "base_sha": base, "data": {"task_base": base}}
    out = CM.reword(c, TP, t, None)
    assert "skipped" not in out, out
    assert not [e for e in c.store.events("game", 50) if e["kind"] == "commits_not_reworded"]
