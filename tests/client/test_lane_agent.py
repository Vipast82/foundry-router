"""The lane agent (foundry_client/lane.py + its /api/lane routes), Distributed Foundry phase b
B1: docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md."""

from __future__ import annotations

import base64
import os
import subprocess
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


def add(h, lane="l1"):
    st = wait(h, h.start_job("pilot", lane, "worktree-add", {"base": h.base})["run_id"])
    assert st["state"] == "done", st
    return h.worktree_of("pilot", lane)


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
])
def test_git_allow_list(host, argv, why):
    add(host)
    with pytest.raises(L.LaneError, match=why):
        host.git("pilot", "l1", argv)


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
    rid = host.start_job("pilot", "l1", "verify",
                         {"argv": ["git", "--version"]})["run_id"]
    st = wait(host, rid)
    assert st["state"] == "done" and st["rc"] == 0 and "git version" in st["output"]
    later = host.run_status(rid, offset=st["offset"])
    assert later["output"] == "" and later["offset"] == st["offset"]


def test_cancel_stops_a_job(host):
    add(host)
    import sys
    rid = host.start_job("pilot", "l1", "verify",
                         {"argv": [sys.executable, "-c", "import time; time.sleep(60)"]})["run_id"]
    time.sleep(0.5)
    host.cancel(rid)
    assert wait(host, rid)["state"] == "cancelled"


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

def _app(host, lane_token="LT"):
    from fastapi.testclient import TestClient
    from foundry_client import api

    class Mgr:
        cfg = {"client": {"backend_url": "http://x"}}

        def status(self):
            return {"components": [{"name": "kilo", "state": "running", "healthy": True}]}
    return TestClient(api.create_app(Mgr(), "CT", lane=host, lane_token=lane_token),
                      base_url="http://127.0.0.1:47811")


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
