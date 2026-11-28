"""Follow-ups from the 2026-11-27 Distributed Foundry review: commit rewording goes through the
task's workspace (a remote lane's commit-tree runs on its slot, message on stdin), a remote
lane's Studio gate gets its own Lune cache folder, apply-kilo-config reads the asked
project's kilo.jsonc."""

from __future__ import annotations

from types import SimpleNamespace

from foundry_client import lane as L
from foundry_coordinator import commits as CM
from foundry_coordinator import slots as SL


def test_reword_sends_commit_tree_through_the_workspace_with_the_message_on_stdin():
    seen = {}

    class WS:
        path = "/srv/foundry-lanes/game/l1"          # a slot path: never run here

        def _proc(self, args, timeout=None, env=None, input=None):
            seen.update(args=args, env=env, input=input)
            return 0, "abc123\n", ""
    P = SimpleNamespace(ws=WS())
    out = CM._run_env(P, {"GIT_AUTHOR_NAME": "t"}, "commit-tree", "TREE", "-p", "PARENT",
                      "-F", "-", input="T1: subject\n")
    assert out.strip() == "abc123"
    assert seen["args"] == ["commit-tree", "TREE", "-p", "PARENT", "-F", "-"]
    assert seen["input"] == "T1: subject\n" and seen["env"]["GIT_AUTHOR_NAME"] == "t"
    # the lane agent accepts exactly this argv (-F - only)
    L.check_git_argv(seen["args"], branch="track/l1")


def test_reword_failure_raises_a_git_error():
    class WS:
        def _proc(self, args, timeout=None, env=None, input=None):
            return 128, "", "fatal: bad tree"
    import pytest
    from foundry_coordinator.adapters.git import GitError
    with pytest.raises(GitError, match="bad tree"):
        CM._run_env(SimpleNamespace(ws=WS()), {}, "commit-tree", "x", "-F", "-", input="m\n")


def test_a_remote_lanes_mirror_gets_its_own_slot_lune_cache():
    assert SL.lune_lane("C:/Roblox/AnimalUprising/.lanes/slot-4/ux") == "slot-4-ux"
    assert SL.lune_lane(r"C:\Roblox\AnimalUprising\.lanes\slot-4\ux") == "slot-4-ux"
    assert SL.lune_lane("C:/Roblox/AnimalUprising/.lanes/slot-4") == "main"
    assert SL.lune_lane("C:/x/.lanes/s;rm/a b") == "s_rm-a_b"
    assert SL.lune_lane("C:/Roblox/AnimalUprising/.tracks/ux") == "ux"


def test_apply_kilo_config_reads_the_asked_projects_file(tmp_path, monkeypatch):
    from foundry_client import manager as M
    seen = []
    monkeypatch.setattr(M, "kilo_config_path", lambda cfg, project="": seen.append(project) or "")
    mgr = SimpleNamespace(cfg={})
    out = M.Manager.apply_kilo_config(mgr, project="second")
    assert seen == ["second"] and out["ok"] is False
