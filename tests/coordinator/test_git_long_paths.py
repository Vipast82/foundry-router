"""2026-11-22: 14 lead / tracks tests failed on any run whose --basetemp was about 110
characters or longer (a session scratchpad); with a short one they passed. Nothing in the
tests was wrong. Git on Windows dies on a deep worktree in two ways:

- "fatal: failed to stat '<sha>..<sha>': Filename too long" - git checks whether a
  revision argument is also a file, and that check fails past 260 characters. The track
  merge then never left `merging` and the verification step raised. The coordinator now
  ends its range arguments with "--" and runs git with core.longpaths (adapters.git.git_env),
  which also covers git's own rebase and merge.
- "fatal: '$GIT_DIR' too big" - `git worktree add` refuses a path of 220 characters or
  more, and the supervisor's check worktree sat under <backup_root>/<upgrade_id>/, so the
  upgrade failed at "verifying" instead of being checked and rolled back.

Each test builds its paths to a fixed depth, so it fails on the old code with any basetemp.
"""

import os
import subprocess
import sys

import pytest

from foundry_coordinator import supervisor as SV
from foundry_coordinator.adapters import git as G
from foundry_coordinator.tracks import manager as TM
from tests.coordinator.helpers import GIT_ENV, git

windows = pytest.mark.skipif(os.name != "nt", reason="the 260-character limit is Windows'")


def deep_repo(tmp_path, length: int):
    """A repo with two commits and a worktree whose path is exactly `length` characters
    (under git's 220-character worktree limit), plus the two commit ids."""
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for i, text in enumerate(("one\n", "two\n")):
        (repo / "a.txt").write_text(text)
        git(repo, "add", "a.txt")
        git(repo, "commit", "-q", "-m", f"c{i}")
    base = str(tmp_path / "w")
    if len(base) + 2 > length:
        pytest.skip(f"the basetemp is already {len(base)} characters deep")
    wt = base + "w" * (length - len(base))
    git(repo, "worktree", "add", "-q", "--detach", wt, "main")
    return wt, git(repo, "rev-parse", "main~1"), git(repo, "rev-parse", "main")


def test_git_env_turns_on_longpaths_once_and_keeps_what_is_there(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")   # read through foundry_platform (v0.181.263)
    env = G.git_env({"PATH": "x"}, GIT_TERMINAL_PROMPT="0")
    assert env["GIT_CONFIG_PARAMETERS"] == G.LONGPATHS and env["GIT_TERMINAL_PROMPT"] == "0"
    # appended to parameters already set, never twice, and GIT_CONFIG_COUNT is untouched
    env = G.git_env({"GIT_CONFIG_PARAMETERS": "'core.abbrev'='12'", "GIT_CONFIG_COUNT": "1",
                     "GIT_CONFIG_KEY_0": "core.abbrev", "GIT_CONFIG_VALUE_0": "12"})
    assert env["GIT_CONFIG_PARAMETERS"] == f"'core.abbrev'='12' {G.LONGPATHS}"
    assert env["GIT_CONFIG_COUNT"] == "1" and env["GIT_CONFIG_KEY_0"] == "core.abbrev"
    assert G.git_env(env)["GIT_CONFIG_PARAMETERS"] == env["GIT_CONFIG_PARAMETERS"]
    # the caller's dict is not changed
    src = {"PATH": "x"}
    G.git_env(src)
    assert src == {"PATH": "x"}


def test_git_env_changes_nothing_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")   # read through foundry_platform (v0.181.263)
    assert "GIT_CONFIG_PARAMETERS" not in G.git_env({"PATH": "x"})


@windows
def test_workspace_reads_a_range_in_a_deep_worktree(tmp_path):
    wt, a, b = deep_repo(tmp_path, 200)
    # what failed: a plain `git diff a..b` there stats the 81-character range as a file
    plain = subprocess.run(["git", "-C", wt, "diff", "--name-only", f"{a}..{b}"],
                           capture_output=True, text=True, env=GIT_ENV)
    assert plain.returncode != 0 and "Filename too long" in plain.stderr, plain
    ws = G.Workspace(wt)
    assert ws.changed_files(a, b) == ["a.txt"]
    assert [s for s, _ in ws.commits(a, b)] == [b]
    assert ws.commit_files(a, b) == [(b, ["a.txt"])]
    assert [f["path"] for f in ws.diff_files(a, b)] == ["a.txt"]
    assert "two" in ws.diff(a, b)[0]
    assert ws.operator_commits(a, b) == set()


@windows
def test_a_track_rebase_in_a_deep_worktree_runs(tmp_path):
    """git's own rebase stats a symmetric range inside git, where no "--" reaches."""
    wt, a, b = deep_repo(tmp_path, 200)
    TM._git(wt, "checkout", "-q", "-b", "side", a)
    with open(os.path.join(wt, "b.txt"), "w") as fh:
        fh.write("side\n")
    TM._git(wt, "add", "b.txt")
    subprocess.run(["git", "-C", wt, "commit", "-q", "-m", "side"], check=True, env=GIT_ENV,
                   capture_output=True)
    r = TM._git(wt, "rebase", "main", check=False)
    assert r.returncode == 0, r.stderr
    assert TM._git(wt, "merge-base", "--is-ancestor", b, "HEAD", check=False).returncode == 0


def test_the_supervisor_check_worktree_path_leaves_out_the_upgrade_id(tmp_path, monkeypatch):
    repo = tmp_path / "router"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "x.py").write_text("x = 1\n")
    git(repo, "add", "x.py")
    git(repo, "commit", "-q", "-m", "x")
    sup = SV.Supervisor(str(tmp_path / "state"), str(repo), "unused", log=lambda m: None)
    seen = []
    real = SV._git

    def spy(cwd, *args, **kw):
        if args[:2] == ("worktree", "add"):
            seen.append(args[-2])
        return real(cwd, *args, **kw)

    monkeypatch.setattr(SV, "_git", spy)
    from foundry_coordinator import repair as RP
    monkeypatch.setattr(RP, "run_pytest", lambda py, cwd, ids, t: {"ok": True, "cwd": cwd})
    uid = "up-rc-5506b3fd7a75def5-78dfc6dc6fdd"
    res = sup._run_tests_at({"upgrade_id": uid}, git(repo, "rev-parse", "HEAD"), [])
    assert res["ok"] and seen and res["cwd"] == seen[0]
    assert uid not in seen[0] and os.path.dirname(seen[0]) == sup.backup_root
    # 11 characters below the backup root, so the 220-character limit leaves the most room
    assert len(seen[0]) - len(sup.backup_root) == len(os.sep + "v-") + 8
    assert not os.path.exists(seen[0])                                    # removed after
