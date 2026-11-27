"""RemoteWorkspace (adapters/remote_git.py), Distributed Foundry phase b B3: the same answers as
Workspace on the same worktree, through the lane agent's routes; a real HTTP round trip too."""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time

import pytest

from foundry_client import lane as L
from foundry_coordinator.adapters.git import GitError, Workspace
from foundry_coordinator.adapters.remote_git import LaneAgent, LaneUnreachable, RemoteWorkspace


def git(cwd, *args, env=None):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True, env=env).stdout.strip()


ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


@pytest.fixture
def setup(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    for name, body in (("a.txt", "one\n"), ("dir/b.txt", "two\n")):
        p = src / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    git(src, "add", "-A", env=ENV)
    git(src, "commit", "-q", "-m", "first", env=ENV)
    (src / "a.txt").write_text("one\nmore\n", encoding="utf-8")
    git(src, "commit", "-q", "-am", "second\n\nCo-Authored-By: Claude <x@example.com>", env=ENV)
    root = tmp_path / "lanes"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(root / "pilot.git")], check=True)
    host = L.LaneHost(str(root), str(tmp_path / "state"))
    base = git(src, "rev-parse", "HEAD")
    first = git(src, "rev-parse", "HEAD~1")
    for lane in ("l1", "l2"):
        rid = host.start_job("pilot", lane, "worktree-add", {"base": base})["run_id"]
        t0 = time.time()
        while host.run_status(rid)["state"] == "running" and time.time() - t0 < 60:
            time.sleep(0.05)
        assert host.run_status(rid)["state"] == "done"
        wt = host.worktree_of("pilot", lane)
        git(wt, "config", "user.email", "t@example.com")
        git(wt, "config", "user.name", "t")
    return host, base, first


class AppAgent(LaneAgent):
    """LaneAgent through the real routes (FastAPI TestClient), without a socket."""

    def __init__(self, host):
        from fastapi.testclient import TestClient
        from foundry_client import api

        class Mgr:
            cfg = {"client": {}}

            def status(self):
                return {"components": []}
        self.c = TestClient(api.create_app(Mgr(), "CT", lane=host, lane_token="LT"),
                            base_url="http://127.0.0.1:47811")
        super().__init__(lambda: {"url": "http://127.0.0.1:47811", "token": "LT"}, "slot-t")

    def call(self, route, body, timeout=60):
        r = self.c.post("/api/lane/" + route, json=body, headers={"x-lane-token": "LT"})
        if r.status_code >= 400:
            raise GitError(f"slot-t lane agent: HTTP {r.status_code} {r.json().get('error')}")
        return r.json()


def pair(setup, lane="l1"):
    host, base, first = setup
    wt = host.worktree_of("pilot", lane)
    return Workspace(wt), RemoteWorkspace(AppAgent(host), "pilot", lane, wt), base, first


def test_reads_agree(setup):
    loc, rem, base, first = pair(setup)
    (open(os.path.join(loc.path, "new.txt"), "w", encoding="utf-8")).write("n\n")
    for name, args in (("head", ()), ("branch", ()), ("is_ancestor", (first, base)),
                       ("is_ancestor", (base, first)), ("changed_files", (first, base)),
                       ("commits", (first, base)), ("diff", (first, base)),
                       ("show_at", (base, "a.txt")), ("show_at", (base, "nope.txt")),
                       ("blob_at", (base, "a.txt")), ("ls_tree_at", (base, "dir")),
                       ("diff_files", (first, base)), ("file_diff", (first, base, "a.txt")),
                       ("status", ()), ("file_hash", ("a.txt",)), ("file_hash", ("dir",)),
                       ("file_hash", ("missing",)), ("tree", (base,)),
                       ("operator_commits", (first, base)), ("commit_files", (first, base)),
                       ("read", ("a.txt",)), ("read", ("missing",)), ("exists", ("dir/b.txt",))):
        assert getattr(rem, name)(*args) == getattr(loc, name)(*args), name
    sl, sr = loc.stat(["a.txt", "dir", "x"]), rem.stat(["a.txt", "dir", "x"])
    assert {k: (v["exists"], v["kind"], v["size"]) for k, v in sl.items()} == \
           {k: (v["exists"], v["kind"], v["size"]) for k, v in sr.items()}


def test_run_errors_agree(setup):
    loc, rem, *_ = pair(setup)
    for ws in (loc, rem):
        with pytest.raises(GitError, match=r"^git rev-parse nosuchref: "):
            ws.run("rev-parse", "nosuchref")
    assert rem.run("rev-parse", "nosuchref", check=False) == \
        loc.run("rev-parse", "nosuchref", check=False)


def test_commit_paths_agree(setup):
    host, base, first = setup
    loc = Workspace(host.worktree_of("pilot", "l1"))
    rem = RemoteWorkspace(AppAgent(host), "pilot", "l2", host.worktree_of("pilot", "l2"))
    for ws in (loc, rem):
        ws.write("docs/x.md", b"x\n")
        ws.write("other.txt", b"left alone\n")
    a = loc.commit_paths(["docs/x.md"], "add x", env={"GIT_AUTHOR_NAME": "c"})
    b = rem.commit_paths(["docs/x.md"], "add x", env={"GIT_AUTHOR_NAME": "c"})
    assert a and b
    assert loc.tree(a) == rem.tree(b)                       # the same content committed
    assert loc.status() == rem.status() == [("??", "other.txt")]
    assert rem.commit_paths(["docs/x.md"], "again") == ""   # nothing left to commit
    assert git(rem.path, "log", "-1", "--format=%an") == "c"


def test_a_refused_commit_unstages_its_paths(setup):
    host, *_ = setup
    rem = RemoteWorkspace(AppAgent(host), "pilot", "l2", host.worktree_of("pilot", "l2"))
    hooks = os.path.join(rem.path, ".githooks")
    os.makedirs(hooks)
    hook = os.path.join(hooks, "pre-commit")
    with open(hook, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/bin/sh\necho refused >&2\nexit 1\n")
    os.chmod(hook, 0o755)
    git(rem.path, "config", "core.hooksPath", hooks)
    rem.write("y.txt", b"y\n")
    with pytest.raises(GitError, match="unstaged the 1 path"):
        rem.commit_paths(["y.txt"], "add y")
    assert git(rem.path, "diff", "--cached", "--name-only") == ""


def test_a_lane_never_pushes(setup):
    _, rem, *_ = pair(setup)
    with pytest.raises(GitError, match="never pushes"):
        rem.push("origin", "main")
    with pytest.raises(GitError, match="never talks to a remote"):
        rem.remote_head("origin", "main")


def test_only_commit_variables_travel(setup, monkeypatch):
    host, *_ = setup
    seen = {}

    class Spy(AppAgent):
        def call(self, route, body, timeout=60):
            if route == "git":
                seen.update(body.get("env") or {})
            return super().call(route, body, timeout)
    rem = RemoteWorkspace(Spy(host), "pilot", "l1", host.worktree_of("pilot", "l1"))
    monkeypatch.setenv("FOUNDRY_KEY", "secret-value")
    rem.head()                                                # run() hands over git_env()
    assert "FOUNDRY_KEY" not in seen and "PATH" not in seen
    assert seen.get("GIT_TERMINAL_PROMPT") is None            # git's own; the agent sets it


def test_down_tunnel_is_a_git_error(setup):
    rem = RemoteWorkspace(LaneAgent(lambda: None, "slot-t"), "pilot", "l1", "/srv/x")
    with pytest.raises(LaneUnreachable, match="tunnel is down"):
        rem.head()
    assert issubclass(LaneUnreachable, GitError)


def test_a_real_http_round_trip(setup):
    """LaneAgent.call itself: urllib -> uvicorn -> the routes -> git."""
    import uvicorn
    from foundry_client import api
    host, base, _ = setup

    class Mgr:
        cfg = {"client": {}}

        def status(self):
            return {"components": []}
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(api.create_app(Mgr(), "CT", lane=host,
                                                          lane_token="LT"),
                                           host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        t0 = time.time()
        while not server.started and time.time() - t0 < 20:
            time.sleep(0.05)
        good = LaneAgent(lambda: {"url": f"http://127.0.0.1:{port}", "token": "LT"}, "slot-t")
        rem = RemoteWorkspace(good, "pilot", "l1", host.worktree_of("pilot", "l1"))
        assert rem.head() == base
        bad = LaneAgent(lambda: {"url": f"http://127.0.0.1:{port}", "token": "nope"}, "slot-t")
        with pytest.raises(GitError, match="HTTP 401"):
            RemoteWorkspace(bad, "pilot", "l1", "/x").head()
    finally:
        server.should_exit = True
        th.join(timeout=10)
