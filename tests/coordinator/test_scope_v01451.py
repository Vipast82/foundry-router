"""0.145.1: docs/memory is the coordinator's (a session edit there is advisory, never a
scope FAIL), and a track that merged the main branch is measured from that merge."""

from __future__ import annotations

from foundry_coordinator import states as S
from tests.coordinator.helpers import by_task, git, run_until
from tests.coordinator.test_tracks_manager import env as tracks_env


def _writes_scope_and_memory(ctx):
    if ctx["phase"] != "implement":
        return {"final": '```coordinator-report\n{"status": "DONE", "summary": "ok"}\n```'}
    return {"writes": {"src/a.txt": "OK", "docs/memory/KNOWN_ISSUES.md": "# notes\n- direct\n"},
            "commit": f"{ctx['task_id']}: work",
            "final": '```coordinator-report\n{"status": "DONE", "summary": "did it"}\n```'}


def test_a_session_memory_edit_is_advisory_not_a_scope_fail(tmp_path):
    c, P, _ = tracks_env(tmp_path, scripts={"game": by_task({"T1": _writes_scope_and_memory})})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: c.store.task("game", "T1")["state"] == S.COMPLETE, limit=40)
    rows = c.store.q("SELECT check_name, result, required, detail FROM verifications WHERE "
                     "task_id='T1' AND phase='verify'")
    by = {r["check_name"]: r for r in rows}
    assert by["coordinator.scope"]["result"] == "PASS"
    assert by["coordinator.memory_direct_edit"]["result"] == "FAIL"
    assert not by["coordinator.memory_direct_edit"]["required"]
    assert "docs/memory/KNOWN_ISSUES.md" in by["coordinator.memory_direct_edit"]["detail"]


def test_a_track_that_merged_main_is_measured_from_the_merge(tmp_path):
    c, P, root = tracks_env(tmp_path, extra="\n[automation]\nmax_agents = 2\n")
    row = c.tracks.create(P, "blender", ["B1"], actor="victor")
    TP = c.tracks.track_project(P, row)
    wt = row["worktree"]
    base = git(wt, "rev-parse", "HEAD")
    import os
    # the track's own work
    os.makedirs(f"{wt}/art", exist_ok=True)
    open(f"{wt}/art/own.txt", "w").write("track work")
    git(wt, "add", "art/own.txt")
    git(wt, "commit", "-q", "--no-verify", "-m", "B1: own work")
    # the main sprint moves main, and the track merges it in
    os.makedirs(f"{root}/src", exist_ok=True)
    open(f"{root}/src/main_only.txt", "w").write("main sprint work")
    git(root, "add", "-A")
    git(root, "commit", "-q", "--no-verify", "-m", "T1: main work")
    main_tip = git(root, "rev-parse", "HEAD")
    git(wt, "merge", "-q", "--no-edit", "--no-ff", "main")
    head = git(wt, "rev-parse", "HEAD")
    eb = c.tracks.effective_base(TP, base, head)
    assert eb == main_tip
    changed = TP.ws.changed_files(eb, head)
    assert changed == ["art/own.txt"]
    assert "src/main_only.txt" in TP.ws.changed_files(base, head)     # what it used to see
    # the main checkout and an unmerged track keep their base
    assert c.tracks.effective_base(P, base, head) == base


def test_lune_timeout_default_and_setting():
    from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
    a = object.__new__(AnimalUprisingAdapter)
    a.cfg = {}
    assert a._lune_timeout() == 1800
    a.cfg = {"lune_timeout_s": 2400}
    assert a._lune_timeout() == 2400
    a.cfg = {"lune_timeout_s": "x"}
    assert a._lune_timeout() == 1800
