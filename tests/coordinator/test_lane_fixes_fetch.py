"""Review fixes 2026-11-27 (Distributed Foundry, lane_fetch / remote_git / verify): every saved
file is really saved, reads are whole, checks are judged on all of their output, divergence is
judged by ancestry, a write never undoes a session's edit, and a 401 refreshes the token. The
"slot" is this machine, as in test_lane_fetch.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types

import pytest

from foundry_client import lane as L
from foundry_coordinator import lane_fetch as F
from foundry_coordinator.adapters.base import PASS, UNKNOWN
from foundry_coordinator.adapters.git import GitError
from foundry_coordinator.adapters.remote_git import LaneAgent, LaneHTTPError, RemoteWorkspace
from foundry_coordinator.adapters.verify_cmd import run_check_remote
from tests.coordinator.test_lane_fetch import AppAgent, git, make, world  # noqa: F401

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def lane_ws(world, mirror=""):
    wt = make(world)["worktree"]
    return wt, RemoteWorkspace(world["agent"], "pilot", "l1", wt, mirror=mirror)


# ---------------------------------------------------------------------------- 1 save_dirty

def test_save_dirty_copies_big_and_oddly_named_files_whole(world, monkeypatch):
    wt, ws = lane_ws(world)
    monkeypatch.setattr(L, "MAX_READ", 1000)                  # several pieces per file
    big = bytes(range(256)) * 20
    ws.write("big.bin", big)
    name = "caf\u00e9 menu (1).txt"                          # git status quotes it
    with open(os.path.join(wt, name), "w", encoding="utf-8") as f:
        f.write("odd\n")
    os.remove(os.path.join(wt, "a.txt"))                       # a deleted tracked file
    dest = str(world["tmp"] / "saves")
    r = F.save_dirty(ws, dest)
    assert r["ok"], r
    assert sorted(r["saved"]) == sorted(["big.bin", name]) and r["deleted"] == ["a.txt"]
    assert open(os.path.join(dest, "big.bin"), "rb").read() == big
    assert open(os.path.join(dest, name), encoding="utf-8").read() == "odd\n"
    assert sorted(F.saved_list(dest)) == sorted(["big.bin", name, "a.txt"])


def test_a_file_that_cannot_be_read_is_never_listed_as_saved(world):
    wt, ws = lane_ws(world)
    ws.write("good.txt", b"g\n")
    ws.write("bad.txt", b"b\n")
    real = ws.read

    def read(rel, max_bytes=None):
        if rel == "bad.txt":
            raise GitError("bad.txt: changed on the slot while it was read")
        return real(rel, max_bytes)
    ws.read = read
    dest = str(world["tmp"] / "saves")
    r = F.save_dirty(ws, dest)
    assert not r["ok"] and r["saved"] == ["good.txt"] and r["failed"] == ["bad.txt"]
    assert "NOT saved: bad.txt" in r["detail"]
    d = json.load(open(os.path.join(dest, F.SAVED_LIST), encoding="utf-8"))
    assert d["saved"] == ["good.txt"] and d["failed"][0]["path"] == "bad.txt"
    assert F.saved_list(dest) == ["good.txt"]
    # and the slot refuses to remove the worktree with only that list
    head = git(wt, "rev-parse", "HEAD")
    job = F.wait_job(world["agent"], "pilot", "l1", "worktree-remove",
                     {"tip": head, "saved": F.saved_list(dest)}, sleep=lambda s: None)
    assert not job["ok"] and "bad.txt" in job["output"] and os.path.isdir(wt)


def test_a_slot_name_that_leads_outside_the_save_folder_is_refused(tmp_path):
    class Ws:
        def run(self, *args, **kw):
            return "?? ../../escape.txt\0?? ok.txt\0"

        def read(self, rel, max_bytes=None):
            return b"x"
    dest = tmp_path / "a" / "b" / "saves"
    r = F.save_dirty(Ws(), str(dest))
    assert not r["ok"] and r["saved"] == ["ok.txt"] and r["failed"] == ["../../escape.txt"]
    assert not (tmp_path / "a" / "escape.txt").exists()
    assert not list(tmp_path.rglob("escape.txt"))


def test_status_failing_is_not_ok(tmp_path):
    class Ws:
        def run(self, *args, **kw):
            raise GitError("lane agent unreachable")
    r = F.save_dirty(Ws(), str(tmp_path / "s"))
    assert not r["ok"] and r["saved"] == [] and "unreachable" in r["detail"]


# ---------------------------------------------------------------------------- 1 whole reads

def test_a_read_bigger_than_one_reply_comes_whole(world, monkeypatch):
    wt, ws = lane_ws(world)
    data = os.urandom(10_000)
    ws.write("blob.bin", data)
    monkeypatch.setattr(L, "MAX_READ", 3000)
    assert ws.read("blob.bin") == data
    assert ws.read("blob.bin", max_bytes=4500) == data[:4500]
    assert ws.read("blob.bin", max_bytes=10) == data[:10]


def test_an_agent_that_sends_only_part_raises_never_a_short_copy(world, monkeypatch):
    wt, ws = lane_ws(world)
    ws.write("blob.bin", b"x" * 100)
    monkeypatch.setattr(L, "MAX_READ", 10)
    real = world["agent"].call

    def old_agent(route, body, timeout=60):                  # no ranged reads, no size
        out = real(route, body, timeout)
        if route == "read":
            out.pop("size", None)
        return out
    monkeypatch.setattr(world["agent"], "call", old_agent)
    with pytest.raises(GitError, match="only the first 10 bytes"):
        ws.read("blob.bin")


# ---------------------------------------------------------------------------- 6 full output

def test_a_check_is_judged_on_all_of_its_output(world):
    """A pass line at the start of 60 kB of output (wait_job kept the last 20 kB)."""
    script = "print('SUITE PASS'); print('x' * 60000)"
    checks = {"long": [sys.executable, "-c", script]}
    r = F.create(world["main"], world["slot"], world["agent"], "pilot", "l1", world["base"],
                 "track/l1", sleep=lambda s: None, checks=checks)
    assert r["ok"], r
    c = types.SimpleNamespace(name="long", command=checks["long"], timeout_seconds=60,
                              pass_regex=r"^SUITE PASS$", fail_regex=None, evidence=None,
                              required=True)
    res = run_check_remote(c, world["agent"], "pilot", "l1", r["worktree"], "T1", world["base"],
                           "", str(world["tmp"] / "coord"), sleep=lambda s: None)
    assert res.result == PASS, res.detail
    assert "SUITE PASS" in open(res.evidence, encoding="utf-8").read()


class Script:
    """A lane agent that answers from a script of replies (no slot)."""

    def __init__(self, status):
        self.name = "slot-t"
        self.calls = []
        self.status = status

    def call(self, route, body, timeout=60):
        self.calls.append(route)
        if route == "run":
            return {"run_id": "r1"}
        if route == "run_status":
            return self.status()
        return {"ok": True}


def test_a_run_the_agent_forgot_fails_at_once():
    def gone():
        raise LaneHTTPError("slot-t lane agent: HTTP 404 no run r1", 404)
    naps = []
    r = F.wait_job(Script(gone), "pilot", "l1", "verify", {}, sleep=naps.append)
    assert r["state"] == "lost" and not r["ok"] and naps == []
    assert "no longer knows run r1" in r["output"]


def test_a_job_past_its_wait_is_cancelled_on_the_slot():
    a = Script(lambda: {"state": "running", "output": "x", "offset": 1})
    r = F.wait_job(a, "pilot", "l1", "verify", {}, wait_s=0, sleep=lambda s: None)
    assert r["state"] == "waiting" and r["cancelled"] and "run/r1/cancel" in a.calls


def test_a_refused_job_and_its_check_are_unknown(world):
    make(world)
    c = types.SimpleNamespace(name="never-registered", command=["rm", "-rf", "/"],
                              timeout_seconds=60, pass_regex=None, fail_regex=None,
                              evidence=None, required=True)
    res = run_check_remote(c, world["agent"], "pilot", "l1", "/x", "T1", world["base"], "",
                           str(world["tmp"] / "coord"), sleep=lambda s: None)
    assert res.result == UNKNOWN and "refused" in res.detail and "not registered" in res.detail


def test_the_check_is_sent_by_name_with_its_template_and_python_is_the_slots():
    seen = {}

    class A:
        name = "slot-w"

        def call(self, route, body, timeout=60):
            if route == "run":
                seen.update(body["args"])
                return {"run_id": "r"}
            return {"state": "done", "rc": 0, "output": "ok\n", "offset": 3}
    c = types.SimpleNamespace(name="unit", command=["{python}", "-m", "pytest", "{task}"],
                              timeout_seconds=60, pass_regex=None, fail_regex=None,
                              evidence=None, required=True)
    import tempfile
    res = run_check_remote(c, A(), "pilot", "l1", "C:/Foundry/lanes/pilot/l1", "T1", "a" * 40,
                           "", tempfile.mkdtemp(prefix="dff-v-"), sleep=lambda s: None,
                           slot_os="windows")
    assert seen["check"] == "unit" and seen["command"] == ["{python}", "-m", "pytest", "{task}"]
    assert seen["subs"] == {"task": "T1", "sha": "a" * 40, "base": ""}
    assert "argv" not in seen
    assert open(res.evidence, encoding="utf-8").read().startswith("$ python -m pytest T1")


def test_create_registers_the_projects_checks(world):
    prof = types.SimpleNamespace(checks=[
        types.SimpleNamespace(name="lint", command=["selene", "src"]),
        types.SimpleNamespace(name="unit", command=["{python}", "-m", "pytest"])])
    assert F.checks_of(prof) == {"lint": ["selene", "src"], "unit": ["{python}", "-m", "pytest"]}
    r = F.create(world["main"], world["slot"], world["agent"], "pilot", "l1", world["base"],
                 "track/l1", sleep=lambda s: None, checks=F.checks_of(prof))
    assert r["ok"] and world["host"].lanes()["pilot/l1"]["checks"] == F.checks_of(prof)


# ---------------------------------------------------------------------------- 9 keepalives

def test_lane_git_over_ssh_has_keepalives_and_a_short_timeout(world, monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append(kw)
        return subprocess.CompletedProcess(argv, 128, "", "ssh: connect: timed out")
    monkeypatch.setattr(F, "subprocess", types.SimpleNamespace(
        run=run, TimeoutExpired=subprocess.TimeoutExpired, DEVNULL=subprocess.DEVNULL))
    slot = {"host": "studio@10.0.0.9", "ssh": ["ssh", "-o", "BatchMode=yes"],
            "lanes_root": "/srv/foundry-lanes"}
    r = F.fetch(world["main"], slot, "slot-4", "pilot", "l1")
    assert not r["ok"] and not r.get("reached")
    cmd = seen[0]["env"]["GIT_SSH_COMMAND"]
    assert "ServerAliveInterval=15" in cmd and "ServerAliveCountMax=4" in cmd
    assert seen[0]["timeout"] == F.FETCH_TIMEOUT_S <= 300
    assert F.git_env({"lanes_root": "/x"}) is None             # a local store: no ssh


# ---------------------------------------------------------------------------- 11 ancestry

def slot_commit(ws, name, text):
    ws.write(name, text)
    return ws.commit_paths([name], f"add {name}", env=ENV)


@pytest.mark.parametrize("rewrite", ["amend", "reset"])
def test_a_rewrite_on_the_slot_is_divergence_by_ancestry(world, rewrite):
    wt, ws = lane_ws(world)
    first = slot_commit(ws, "b.txt", b"b\n")
    second = slot_commit(ws, "c.txt", b"c\n")
    assert F.fetch(world["main"], world["slot"], "slot-t", "pilot", "l1")["ok"]
    # a session rewrites the slot's branch (git there, not through the agent)
    if rewrite == "amend":
        subprocess.run(["git", "-C", wt, "commit", "-q", "--amend", "-m", "reworded"], check=True,
                       env={**os.environ, **ENV})
    else:
        subprocess.run(["git", "-C", wt, "reset", "-q", "--hard", first], check=True)
    now = git(wt, "rev-parse", "HEAD")
    r = F.fetch(world["main"], world["slot"], "slot-t", "pilot", "l1")
    assert not r["ok"] and r["diverged"] and r["reached"], r
    assert "rewritten" in r["detail"]
    assert git(world["main"], "rev-parse", "refs/lanes/slot-t/l1") == second    # kept
    assert git(world["main"], "rev-parse", "refs/lanes/slot-t/_incoming/l1") == now
    assert git(world["main"], "rev-parse", "refs/heads/track/l1") == second     # untouched


def test_a_mirror_that_cannot_fast_forward_reached_the_slot(world):
    wt, ws = lane_ws(world)
    mirror = os.path.join(str(world["tmp"]), "mirror")
    subprocess.run(["git", "-C", world["main"], "worktree", "add", "-q", "-b", "track/l1",
                    mirror, world["base"]], check=True)
    slot_commit(ws, "b.txt", b"from the slot\n")
    with open(os.path.join(mirror, "b.txt"), "w", encoding="utf-8") as f:
        f.write("in the way\n")                              # an untracked file in the way
    r = F.fetch(world["main"], world["slot"], "slot-t", "pilot", "l1", mirror=mirror)
    assert not r["ok"] and r["reached"] and not r.get("diverged")
    assert "did not fast-forward" in r["detail"]


# ---------------------------------------------------------------------------- 10 stale writes

@pytest.fixture
def mirrored(world):
    """A lane on the slot and its read mirror here at the same commit (as phase c makes)."""
    wt = make(world)["worktree"]
    mirror = os.path.join(str(world["tmp"]), "mirror")
    subprocess.run(["git", "-C", world["main"], "worktree", "add", "-q", "-b", "track/l1",
                    mirror, world["base"]], check=True)

    def fetch():
        return F.fetch(world["main"], world["slot"], "slot-t", "pilot", "l1", mirror=mirror)
    ws = RemoteWorkspace(world["agent"], "pilot", "l1", wt, mirror=mirror, fetch=fetch)
    sess = RemoteWorkspace(world["agent"], "pilot", "l1", wt)     # a session, on the slot
    return {"wt": wt, "mirror": mirror, "ws": ws, "sess": sess, "fetch": fetch}


def test_a_write_never_undoes_a_sessions_edit_on_the_slot(mirrored):
    m = mirrored
    with open(os.path.join(m["wt"], "a.txt"), "w", encoding="utf-8") as f:
        f.write("the session's edit\n")
    with pytest.raises(GitError, match="changed there since"):
        m["ws"].write("a.txt", b"the coordinator's\n")
    assert open(os.path.join(m["wt"], "a.txt"), encoding="utf-8").read() == "the session's edit\n"
    # a new file, and the coordinator's own second write of it, go through
    m["ws"].write("notes/n.md", b"one\n")
    m["ws"].write("notes/n.md", b"two\n")
    assert open(os.path.join(m["wt"], "notes", "n.md"), "rb").read() == b"two\n"


def test_a_write_waits_for_the_slots_newest_commit(mirrored):
    m = mirrored
    slot_commit(m["sess"], "a.txt", b"committed on the slot\n")   # the mirror is behind now
    m["ws"].write("other.txt", b"fine\n")                          # fetched first, then judged
    assert git(m["mirror"], "rev-parse", "HEAD") == git(m["wt"], "rev-parse", "HEAD")
    # without a way to fetch, an older mirror never writes over the slot
    stale = RemoteWorkspace(m["ws"].agent, "pilot", "l1", m["wt"], mirror=m["mirror"])
    slot_commit(m["sess"], "c.txt", b"c\n")
    with pytest.raises(GitError, match="older copy"):
        stale.write("other.txt", b"x\n")


def test_uploads_before_a_commit_refuse_a_conflict_and_send_nothing(mirrored):
    m = mirrored
    with open(os.path.join(m["mirror"], "a.txt"), "w", encoding="utf-8") as f:
        f.write("mirror's\n")                                    # the coordinator wrote here
    with open(os.path.join(m["mirror"], "new.txt"), "w", encoding="utf-8") as f:
        f.write("new\n")
    with open(os.path.join(m["wt"], "a.txt"), "w", encoding="utf-8") as f:
        f.write("session's\n")                                   # and a session there
    with pytest.raises(GitError, match="a.txt"):
        m["ws"]._upload_mirror_changes(["a.txt", "new.txt"])
    assert not os.path.exists(os.path.join(m["wt"], "new.txt"))   # nothing was sent
    assert open(os.path.join(m["wt"], "a.txt"), encoding="utf-8").read() == "session's\n"
    # the slot's file back as committed: the coordinator's copy goes
    subprocess.run(["git", "-C", m["wt"], "checkout", "--", "a.txt"], check=True)
    assert sorted(m["ws"]._upload_mirror_changes(["a.txt", "new.txt"])) == ["a.txt", "new.txt"]
    assert open(os.path.join(m["wt"], "a.txt"), encoding="utf-8").read() == "mirror's\n"


# ---------------------------------------------------------------------------- 2 401 -> refresh

def test_a_401_refreshes_the_secrets_once_and_sends_the_call_again(monkeypatch):
    tokens = {"t": "old"}
    asked = []

    def refresh(name, used):
        asked.append((name, used))
        tokens["t"] = "new"
        return True
    a = LaneAgent(lambda: {"url": "http://127.0.0.1:1", "token": tokens["t"]}, "slot-4",
                  refresh=refresh)
    sent = []

    def call(route, body, timeout):
        sent.append(tokens["t"])
        if tokens["t"] == "old":
            e = LaneHTTPError("slot-4 lane agent: HTTP 401 wrong lane token", 401)
            e.token = "old"
            raise e
        return {"ok": True}
    monkeypatch.setattr(a, "_call", call)
    assert a.call("git", {}) == {"ok": True}
    assert asked == [("slot-4", "old")] and sent == ["old", "new"]
    # a refresh that finds nothing new: the 401 stands, and only one try is made
    tokens["t"] = "old"
    a.refresh = lambda name, used: False
    sent.clear()
    with pytest.raises(LaneHTTPError, match="401"):
        a.call("git", {})
    assert sent == ["old"]


def test_other_errors_never_refresh(monkeypatch):
    a = LaneAgent(lambda: {"url": "x", "token": "t"}, "slot-4",
                  refresh=lambda *x: pytest.fail("refreshed on a 404"))

    def call(route, body, timeout):
        raise LaneHTTPError("HTTP 404 no lane", 404)
    monkeypatch.setattr(a, "_call", call)
    with pytest.raises(LaneHTTPError):
        a.call("read", {})


def test_the_default_refresh_asks_the_tunnel_manager(monkeypatch):
    from foundry_coordinator import lane_tunnel
    seen = []
    monkeypatch.setattr(lane_tunnel.MANAGER, "refresh",
                        lambda sid, used: seen.append((sid, used)) or False)
    a = LaneAgent(lambda: {"url": "x", "token": "t"}, "slot-4")

    def call(route, body, timeout):
        e = LaneHTTPError("HTTP 401", 401)
        e.token = "t"
        raise e
    monkeypatch.setattr(a, "_call", call)
    with pytest.raises(LaneHTTPError):
        a.call("git", {})
    assert seen == [("slot-4", "t")]
