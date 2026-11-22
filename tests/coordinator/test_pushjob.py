"""A push too big for one go is sent in chunks and the staging branches are deleted
(pushjob.py; item 20 of COORDINATOR_AUTOMATION_NEEDS). Real git, a local bare remote."""

from __future__ import annotations

import os
import subprocess

import pytest

from foundry_coordinator import pushjob
from foundry_coordinator.adapters.git import Workspace

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True,
                          check=True, env=ENV).stdout.strip()


def commit(root, name, kb=64):
    with open(os.path.join(root, name), "wb") as f:
        f.write(os.urandom(kb * 1024))                        # random: it does not compress
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", name)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    root = str(tmp_path / "work")
    os.makedirs(root)
    git(root, "init", "-q", "-b", "master")
    git(root, "remote", "add", "origin", str(remote))
    base = commit(root, "base.bin", kb=4)
    git(root, "push", "-q", "origin", "HEAD:refs/heads/master")
    return Workspace(root), root, str(remote), base


def remote_refs(remote):
    out = subprocess.run(["git", "-C", remote, "for-each-ref", "--format=%(refname) %(objectname)"],
                         capture_output=True, text=True, check=True).stdout.split("\n")
    return dict(l.split() for l in out if l.strip())


def test_replay_a_push_over_the_limit_goes_in_chunks_and_the_staging_branches_are_deleted(repo):
    """2026-11-17 FIN-5a1: more than the remote's per-push limit; pushed by hand in 13
    chunks through foundry-push/* branches, which were left on the remote."""
    ws, root, remote, base = repo
    shas = [commit(root, f"frame{i}.bin") for i in range(6)]  # about 64 KB each
    head = shas[-1]
    m = pushjob.measure(ws, "origin", head, base)
    assert m["base"] == base and 6 * 60_000 < m["bytes"] < 6 * 80_000
    steps = pushjob.plan(ws, head, base, m["have"], chunk_bytes=150_000)
    assert steps == [shas[1], shas[3], shas[5]]               # two frames a chunk
    seen = []
    real = ws.run

    def run(*a, **k):
        if a and a[0] == "push":
            seen.append(a[2] if a[2] != "--delete" else "delete " + a[3])
        return real(*a, **k)
    ws.run = run
    job = pushjob.start(ws, "origin", "master", head, m, 150_000, run_in_thread=False)
    assert job["state"] == "done" and job["chunks"] == 3 and job["sent"] == 3
    assert seen[:3] == [f"+{shas[1]}:refs/heads/foundry-push/master-1",
                        f"+{shas[3]}:refs/heads/foundry-push/master-2",
                        f"{head}:refs/heads/master"]
    # the branch is at the head, and no staging branch is left
    assert remote_refs(remote) == {"refs/heads/master": head}
    assert job["deleted"] == ["foundry-push/master-1", "foundry-push/master-2"]
    assert pushjob.words(job) == "pushed in 3 chunk(s); deleted 2 staging branch(es)"
    assert pushjob.status(ws, "origin", "master") is job


def test_staging_branches_left_from_before_go_when_they_are_part_of_the_branch(repo):
    ws, root, remote, base = repo
    old = commit(root, "old.bin")
    git(root, "push", "-q", "origin", f"{old}:refs/heads/foundry-push/chunk-07")
    # one that is NOT part of the branch (somebody else's work) is left alone
    git(root, "checkout", "-q", "-b", "side", base)
    stray = commit(root, "stray.bin")
    git(root, "push", "-q", "origin", f"{stray}:refs/heads/foundry-push/other")
    git(root, "checkout", "-q", "master")
    head = commit(root, "new.bin")
    git(root, "push", "-q", "origin", "HEAD:refs/heads/master")
    deleted, kept = pushjob.sweep(ws, "origin", head)
    assert deleted == ["foundry-push/chunk-07"] and kept == ["foundry-push/other"]
    assert set(remote_refs(remote)) == {"refs/heads/master", "refs/heads/foundry-push/other"}


def test_what_is_already_on_a_staging_branch_is_not_counted_or_sent_again(repo):
    """A restart in the middle: the chunks already on the remote are not part of the size."""
    ws, root, remote, base = repo
    shas = [commit(root, f"frame{i}.bin") for i in range(4)]
    git(root, "push", "-q", "origin", f"{shas[2]}:refs/heads/foundry-push/master-1")
    m = pushjob.measure(ws, "origin", shas[3], base)
    assert shas[2] in m["have"] and m["bytes"] < 80_000       # only the last frame is left
    assert pushjob.plan(ws, shas[3], base, m["have"], 150_000) == [shas[3]]


def test_one_commit_bigger_than_the_limit_is_pushed_on_its_own(repo):
    ws, root, remote, base = repo
    a = commit(root, "small.bin", kb=8)
    big = commit(root, "big.bin", kb=300)
    c = commit(root, "small2.bin", kb=8)
    m = pushjob.measure(ws, "origin", c, base)
    assert pushjob.plan(ws, c, base, m["have"], 100_000) == [a, big, c]


def test_a_failed_chunk_says_how_far_it_got_and_a_new_branch_has_no_base(repo, tmp_path):
    ws, root, remote, base = repo
    shas = [commit(root, f"frame{i}.bin") for i in range(4)]
    m = pushjob.measure(ws, "origin", shas[-1], base)
    real = ws.run
    n = {"push": 0}

    def run(*a, **k):
        if a and a[0] == "push":
            n["push"] += 1
            if n["push"] == 2:
                from foundry_coordinator.adapters.git import GitError
                raise GitError("git push: remote: fatal: pack exceeds maximum allowed size")
        return real(*a, **k)
    ws.run = run
    job = pushjob.start(ws, "origin", "master", shas[-1], m, 100_000, run_in_thread=False)
    assert job["state"] == "failed" and job["sent"] == 1 and "pack exceeds" in job["error"]
    assert pushjob.words(job).startswith("the chunked push failed after 1 of ")
    assert pushjob.size_refusal(job["error"]) and pushjob.size_refusal("git push timed out")
    assert not pushjob.size_refusal("Permission denied (publickey)")
    ws.run = real
    # a branch the remote does not have: measured from the root
    m2 = pushjob.measure(ws, "origin", shas[-1], None)
    assert m2["base"] is None and m2["bytes"] > 0
    pushjob.clear(ws, "origin", "master")
    assert pushjob.status(ws, "origin", "master") is None


def test_a_background_push_is_never_recorded_as_the_command_a_step_is_in(repo):
    """The crash guard names the command a hung step was in; the push thread's git calls go
    through the same runner and must not overwrite it."""
    import threading

    from foundry_coordinator import crashguard
    ws, root, remote, base = repo
    with crashguard.step("tick", "game", "T1", "CLOSING"):
        ws.run("status", "--porcelain")
        mine = dict(crashguard._cmd)
        t = threading.Thread(target=lambda: ws.run("ls-remote", "origin"))
        t.start()
        t.join(60)
        assert crashguard._cmd == mine and "status --porcelain" in mine["text"]
        ws.run("rev-parse", "HEAD")                            # the step's own next command
        assert "rev-parse HEAD" in crashguard._cmd["text"]
