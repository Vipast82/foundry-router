"""The closeout push goes in chunks when it is over [automation] push_chunk_mb, without
holding the tick (engine._publish + pushjob.py; item 20 of COORDINATOR_AUTOMATION_NEEDS)."""

from __future__ import annotations

import json
import os
import subprocess

from foundry_coordinator import automation, pushjob
from foundry_coordinator.adapters.base import FAIL, PASS
from tests.coordinator.test_budget_policy import game, task


def git(cwd, *a):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True,
                          check=True, env=env).stdout.strip()


def frames(ws, n, kb):
    for i in range(n):
        with open(os.path.join(ws, f"frame{i}.bin"), "wb") as f:
            f.write(os.urandom(kb * 1024))
        git(ws, "add", "-A")
        git(ws, "commit", "-q", "-m", f"T1: frame {i}")
    return git(ws, "rev-parse", "HEAD")


def events(c, kind):
    return [json.loads(r["detail"]) for r in c.store.q(
        "SELECT detail FROM events WHERE kind=? ORDER BY id", (kind,))]


def world(tmp_path):
    c, P, clk = game(tmp_path)
    ws = P.profile.workspace
    branch = git(ws, "rev-parse", "--abbrev-ref", "HEAD")
    P.profile.publish.update(mode="push", remote="origin", branch=branch)
    git(ws, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    return c, P, ws, branch


def test_replay_a_push_over_the_limit_runs_in_the_background_and_the_retry_finishes(tmp_path):
    c, P, ws, branch = world(tmp_path)
    head = frames(ws, 4, 700)                                 # about 2.8 MB to send
    automation.set_live(P, "push_chunk_mb", 1)
    results = []
    assert c._publish(P, task(c), head, results, push=True) == ""
    [r] = [r for r in results if r.check == "coordinator.published"]
    assert r.result == FAIL and r.detail.startswith("pushing in chunks")
    [e] = events(c, "push_chunked")
    assert e["limit_mb"] == 1 and e["mb"] >= 2 and "goes in chunks in the background" in e["text"]
    job = pushjob.status(P.ws, "origin", branch)
    # the tick did not wait; a retry while it is still sending starts nothing new
    if job["state"] == "running":
        more = []
        assert c._publish(P, task(c), head, more, push=True, attempt=1) == ""
        assert more[-1].detail.startswith("pushing in chunks")
    job["thread"].join(120)
    assert job["state"] == "done" and job["chunks"] >= 3, job
    assert len(events(c, "push_chunked")) == 1
    # the retry finds the remote at the head: published, and the chunked push is reported
    done = []
    assert c._publish(P, task(c), head, done, push=True, attempt=2) == f"origin/{branch}@{head[:12]}"
    assert done[-1].result == PASS
    [d] = events(c, "push_chunked_done")
    assert d["chunks"] == job["chunks"] and len(d["deleted"]) == job["chunks"] - 1
    assert "foundry-push" not in git(ws, "ls-remote", "origin")
    c._publish(P, task(c), head, [], push=True, attempt=3)
    assert len(events(c, "push_chunked_done")) == 1            # said once


def test_a_push_under_the_limit_and_a_limit_of_zero_push_as_before(tmp_path):
    c, P, ws, branch = world(tmp_path)
    head = frames(ws, 1, 64)
    results = []
    assert c._publish(P, task(c), head, results, push=True) == f"origin/{branch}@{head[:12]}"
    assert results[-1].result == PASS and events(c, "push_chunked") == []
    head = frames(ws, 2, 700)
    automation.set_live(P, "push_chunk_mb", 0)                # never chunk
    assert c._publish(P, task(c), head, [], push=True) == f"origin/{branch}@{head[:12]}"
    assert events(c, "push_chunked") == [] and pushjob.status(P.ws, "origin", branch) is None


def test_a_plain_push_the_remote_refuses_for_its_size_is_chunked(tmp_path, monkeypatch):
    from foundry_coordinator.adapters.git import GitError
    c, P, ws, branch = world(tmp_path)
    head = frames(ws, 2, 64)                                  # small: the limit is not reached

    def refuse(remote, br):
        raise GitError("git push: remote: fatal: pack exceeds maximum allowed size (2.00 GiB)")
    monkeypatch.setattr(P.ws, "push", refuse)
    results = []
    assert c._publish(P, task(c), head, results, push=True) == ""
    assert results[-1].detail.startswith("pushing in chunks")
    job = pushjob.status(P.ws, "origin", branch)
    job["thread"].join(120)
    assert job["state"] == "done"
    assert c._publish(P, task(c), head, [], push=True, attempt=1) == f"origin/{branch}@{head[:12]}"
    # a refusal that is not about size is a plain failure: push wait, no chunks
    head2 = frames(ws, 1, 8)
    pushjob.clear(P.ws, "origin", branch)
    monkeypatch.setattr(P.ws, "push", lambda remote, br: (_ for _ in ()).throw(
        GitError("git push: Permission denied (publickey)")))
    out = []
    assert c._publish(P, task(c), head2, out, push=True) == ""
    assert "expected" in out[-1].detail and pushjob.status(P.ws, "origin", branch) is None
