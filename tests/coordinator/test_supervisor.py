"""supervisor.py: the journaled upgrade step machine, with a throwaway git
repo as the router checkout, a fake coordinator process (fakes_supervisor/)
and a temp state dir holding a real coordinator Store DB."""

from __future__ import annotations

import json
import socket
import os
import subprocess
import sys
import time

import pytest

from foundry_coordinator import supervisor as SV
from foundry_coordinator.store import Store
from foundry_coordinator.telemetry import PROJECT_PAUSE_META, project_pause_status

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes_supervisor",
                    "fake_coordinator.py")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PASSING = "def test_ok():\n    assert 1 + 1 == 2\n"
FAILING = "def test_ok():\n    assert 1 + 1 == 3\n"


class Crash(BaseException):
    """A simulated supervisor crash (not an ordinary step failure)."""


def g(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def write(root, rel, text):
    p = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def kill(pid):
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


class Env:
    def __init__(self, tmp_path):
        self.repo = str(tmp_path / "router")
        self.state = str(tmp_path / "state")
        os.makedirs(self.repo)
        os.makedirs(self.state)
        g(self.repo, "init", "-q", "-b", "main")
        g(self.repo, "config", "core.autocrlf", "false")
        write(self.repo, "BUILD", "v1\n")
        write(self.repo, "README.md", "router\n")
        write(self.repo, "foundry_router/__init__.py", '__version__ = "1.0.0"\n')
        write(self.repo, "tests/test_ok.py", PASSING)
        g(self.repo, "add", "-A")
        g(self.repo, "commit", "-q", "-m", "v1")
        self.prior = g(self.repo, "rev-parse", "HEAD")
        self.cmd = f'"{sys.executable}" "{FAKE}" --state "{self.state}" --max-seconds 90 --pid-json'
        st = Store(os.path.join(self.state, "coordinator.sqlite"))
        st.register_project("p1", "P1", "profile.toml", "h", "1", self.repo)
        st.x("CREATE TABLE recovery_cases (case_id TEXT PRIMARY KEY, project_id TEXT, state TEXT, "
             "upgrade_id TEXT, rollback_result TEXT, updated_ts REAL)")
        st.x("INSERT INTO recovery_cases (case_id, project_id, state) VALUES ('c1','p1','UPGRADING')")
        st.set_meta("schema:obligations", "3")
        # evidence blobs to back up
        write(self.state, "evidence_blobs/ab/abcdef", "blob")
        self.store = st
        self.sups = []

    def candidate(self, files: dict, msg="candidate") -> str:
        g(self.repo, "checkout", "-q", "-b", "lead/c1")
        for rel, text in files.items():
            write(self.repo, rel, text)
        g(self.repo, "add", "-A")
        g(self.repo, "commit", "-q", "-m", msg)
        sha = g(self.repo, "rev-parse", "HEAD")
        g(self.repo, "checkout", "-q", "main")
        return sha

    def files(self, sha) -> list:
        r = subprocess.run(["git", "-C", self.repo, "diff", "--name-only", self.prior, sha],
                           capture_output=True, text=True)
        return sorted(f for f in r.stdout.split("\n") if f) if r.returncode == 0 else []

    def request(self, sha, tests=("tests/test_ok.py",), broader=("tests",),
                regression="tests/test_ok.py::test_ok", **man) -> str:
        """A manifest as repair.validate writes it (review APPROVED, files =
        the actual diff, focused + broader + regression tests)."""
        m = {"candidate_sha": sha, "review_verdict": "APPROVED", "files": self.files(sha),
             "tests": {"focused": list(tests), "broader": list(broader),
                       "regression": regression}}
        m.update(man)
        return SV.request_upgrade(self.store, case_id="c1", project_id="p1", task_id="t1",
                                  candidate_sha=sha, manifest=m)

    def sup(self, **kw) -> SV.Supervisor:
        kw.setdefault("stop_timeout", 5)
        kw.setdefault("health_timeout", 15)
        kw.setdefault("poll", 0.05)
        kw.setdefault("test_timeout", 120)
        kw.setdefault("log", lambda m: None)
        s = SV.Supervisor(self.state, self.repo, self.cmd, **kw)
        self.sups.append(s)
        return s

    def start_old(self) -> int:
        s = self.sup()
        info = s.start_coordinator("old")
        s.health(info["started_ts"], info["pid"], 15)
        # the fake's own pid (a venv launcher may stand in front of it)
        return self.heartbeat()["pid"]

    def heartbeat(self) -> dict:
        with open(os.path.join(self.state, "coordinator.heartbeat"), encoding="utf-8") as fh:
            return json.load(fh)

    def pid(self) -> int:
        rec = SV.read_pid_file(os.path.join(self.state, "coordinator.pid"))
        return int(rec["pid"]) if rec else 0

    def events(self) -> list[str]:
        try:
            return open(os.path.join(self.state, "fake_events.log")).read().split("\n")
        except OSError:
            return []

    def head(self) -> str:
        return g(self.repo, "rev-parse", "HEAD")

    def req(self, uid) -> dict:
        return SV.get_request(self.store, uid)

    def cleanup(self):
        for line in self.events():
            if line.startswith("start "):
                kill(int(line.split()[1]))
        kill(self.pid())
        for s in self.sups:
            for p in list(s._procs.values()):
                try:
                    p.kill()
                    p.wait(5)
                except Exception:                                # noqa: BLE001
                    pass
            s.close()
        self.store.conn.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.cleanup()


def entries(req, step=None, event=None):
    return [x for x in req["log"]["entries"]
            if (step is None or x["step"] == step) and (event is None or x["event"] == event)]


def merges(req):
    return [x for x in entries(req, "installing", "done") if x["detail"].get("merged")]


# ---------------------------------------------------------------------------- happy path

def test_happy_path(env):
    old = env.start_old()
    assert env.heartbeat()["build"] == "v1"
    sha = env.candidate({"BUILD": "v2\n", "tests/test_ok.py": PASSING + "\n# v2\n"})
    uid = env.request(sha, release="1.0.0")
    seen = {}

    def hook(step, when, req):
        if when == "done" and step == "waiting_safe_point":
            seen["pause"] = project_pause_status(env.store.q, "p1")
            seen["meta"] = json.loads(env.store.meta(PROJECT_PAUSE_META + "p1"))
        if when == "done" and step == "stopping":
            seen["old_alive"] = SV.pid_alive(old)

    res = env.sup(hook=hook).run_once()
    assert res[0]["status"] == "done", res
    r = env.req(uid)
    # 1. safe point under the supervisor's own pause
    assert seen["pause"]["paused"] and seen["pause"]["safe_to_upgrade"]
    assert seen["meta"]["actor"] == "supervisor" and seen["meta"]["mode"] == "safe"
    # 2. backup: DB and evidence blobs
    bdir = r["backup_path"]
    assert os.path.isfile(os.path.join(bdir, "coordinator.sqlite"))
    assert os.path.isfile(os.path.join(bdir, "evidence_blobs", "ab", "abcdef"))
    assert r["prior_sha"] == env.prior
    # 3-4. verified, then the old build stopped (gracefully)
    assert entries(r, "verifying", "done")[0]["detail"]["release"] == "1.0.0"
    assert seen["old_alive"] is False
    assert entries(r, "stopping", "done")[0]["detail"]["how"] == "graceful"
    assert f"stop {old}" in env.events()
    # 5-7. installed, started and healthy
    assert env.head() == sha and len(merges(r)) == 1
    new = env.pid()
    assert new and new != old and SV.pid_alive(new)
    assert env.heartbeat()["build"] == "v2" and env.heartbeat()["pid"] == new
    # 8. resumed: the pause is cleared; the request reports done. The
    # supervisor never writes the recovery case: the lead's UPGRADING step
    # polls get_request and moves (and journals) the case itself
    assert not env.store.project("p1")["paused"]
    assert env.store.meta(PROJECT_PAUSE_META + "p1") is None
    assert r["status"] == "done" and r["rollback_result"] is None and r["failure"] is None
    assert entries(r, "resuming", "done")[0]["detail"] == {"pause_released": True,
                                                           "released": ["p1"]}
    case = env.store.q("SELECT * FROM recovery_cases WHERE case_id='c1'")[0]
    assert case["state"] == "UPGRADING"
    assert case["upgrade_id"] is None and case["updated_ts"] is None
    # every step journaled once, in order
    done = [x["step"] for x in entries(r, event="done")]
    assert done == list(SV.STEPS)
    # a second pass does nothing
    assert env.sup().run_once() == []


def test_supervisor_never_writes_recovery_cases(env, monkeypatch):
    """Every statement the supervisor runs during a full upgrade and a full
    rollback: none touches recovery_cases (the lead owns the case state)."""
    seen = []
    real_x = Store.x

    def spy(self, sql, *a, **kw):
        seen.append(sql)
        return real_x(self, sql, *a, **kw)
    monkeypatch.setattr(Store, "x", spy)
    env.start_old()
    sha = env.candidate({"BUILD": "v2\n", "FAIL_HEALTH": "1\n"})
    env.request(sha)
    assert env.sup().run_once()[0]["status"] == "rolled_back"
    assert seen and not [q for q in seen if "recovery_cases" in q]


def test_get_request_journal_is_an_object(env):
    uid = env.request("a" * 40)
    r = env.req(uid)
    assert r["log"] == {"seq": 0, "entries": [], "state": {}}
    assert r["rollback_result"] is None and r["status"] == "requested"
    # a list-shaped log (an older writer) never reaches the step machine as a list
    env.store.x("UPDATE upgrade_requests SET log='[]' WHERE upgrade_id=?", (uid,))
    assert env.req(uid)["log"] == {"seq": 0, "entries": [], "state": {}}


def test_waits_for_a_safe_point(env):
    env.start_old()
    sha = env.candidate({"BUILD": "v2\n"})
    uid = env.request(sha)
    env.store.x("INSERT INTO tasks (project_id, task_id, state, session_id, data) "
                "VALUES ('p1','t1','IMPLEMENTING','s1','{}')")
    env.store.x("INSERT INTO sessions (session_id, project_id, task_id, prompt_ts, created_ts) "
                "VALUES ('s1','p1','t1',1,1)")
    res = env.sup().run_once()
    assert res[0]["status"] == "waiting_safe_point"
    assert env.store.project("p1")["paused"]                  # nothing new starts meanwhile
    assert env.head() == env.prior
    env.store.x("UPDATE sessions SET ended_ts=2 WHERE session_id='s1'")
    assert env.sup().run_once()[0]["status"] == "done"
    assert env.head() == sha and not env.store.project("p1")["paused"]


def test_operator_pause_is_not_cleared(env):
    env.start_old()
    sha = env.candidate({"BUILD": "v2\n"})
    env.request(sha)
    env.store.set_meta(PROJECT_PAUSE_META + "p1", json.dumps({"mode": "safe", "actor": "victor"}))
    env.store.set_project_paused("p1", True, "victor's pause")
    assert env.sup().run_once()[0]["status"] == "done"
    assert env.store.project("p1")["paused"]                  # still the operator's
    assert json.loads(env.store.meta(PROJECT_PAUSE_META + "p1"))["actor"] == "victor"


# ---------------------------------------------------------------------------- rollback

def test_health_failure_rolls_back(env):
    old = env.start_old()
    # the new build migrates the DB, writes a marker into it and reports unhealthy
    sha = env.candidate({"BUILD": "v2\n", "FAIL_HEALTH": "1\n",
                         "SCHEMA.json": json.dumps({"new_build_marker": "1",
                                                    "schema:obligations": "4"})})
    uid = env.request(sha)
    res = env.sup().run_once()
    assert res[0]["status"] == "rolled_back", res
    r = env.req(uid)
    assert r["status"] == "rolled_back"
    assert "not ok" in r["log"]["state"]["failure"]["reason"]
    assert r["log"]["state"]["failure"]["step"] == "checking"
    # prior sha restored in the router checkout
    assert env.head() == env.prior
    assert not os.path.exists(os.path.join(env.repo, "FAIL_HEALTH"))
    # the new build migrated the DB: it is the backup again (the new build's
    # write is gone), the journal is kept
    assert env.store.meta("new_build_marker") is None
    assert env.store.meta("schema:obligations") == "3"
    assert [x["event"] for x in entries(r, "rolling_back")] == \
        ["stopped", "reset", "db_restored", "restart_started", "restarted", "done"]
    # the old build runs again
    new = env.pid()
    assert new and new != old and SV.pid_alive(new)
    hb = env.heartbeat()
    assert hb["build"] == "v1" and hb["ok"] is True and hb["pid"] == new
    assert entries(r, "rolling_back", "restarted")[0]["detail"]["healthy"] is True
    assert not env.store.project("p1")["paused"]
    # the rollback is reported on the request only (get_request); the case row
    # is not written by the supervisor (the restored backup holds it as before)
    rb = r["rollback_result"]
    assert rb["upgrade_id"] == uid and rb["failure"]["step"] == "checking"
    assert rb["prior_sha"] == env.prior and rb["db_restored"] is True
    assert rb["restarted_healthy"] is True and rb["reset"]["to"] == env.prior
    assert r["failure"]["step"] == "checking"
    case = env.store.q("SELECT * FROM recovery_cases WHERE case_id='c1'")[0]
    assert case["state"] == "UPGRADING" and case["rollback_result"] is None


def test_schema_downgrade_rolls_back(env):
    env.start_old()
    sha = env.candidate({"BUILD": "v2\n", "SCHEMA.json": json.dumps({"schema:obligations": "1"})})
    uid = env.request(sha)
    assert env.sup().run_once()[0]["status"] == "rolled_back"
    r = env.req(uid)
    assert "schema versions moved backwards" in r["log"]["state"]["failure"]["reason"]
    assert env.head() == env.prior
    assert env.store.meta("schema:obligations") == "3"        # restored from the backup


# ---------------------------------------------------------------------------- crash/resume

def test_crash_after_verify_resumes(env):
    old = env.start_old()
    sha = env.candidate({"BUILD": "v2\n"})
    uid = env.request(sha)

    def crash(step, when, req):
        if step == "verifying" and when == "advanced":
            raise Crash()

    with pytest.raises(Crash):
        env.sup(hook=crash).run_once()
    r = env.req(uid)
    assert r["status"] == "stopping" and env.head() == env.prior
    assert SV.pid_alive(old)                                  # nothing stopped yet
    assert not r["backup_path"]                               # no backup before the stop
    res = env.sup().run_once()
    assert res[0]["status"] == "done"
    r = env.req(uid)
    assert len(entries(r, "verifying", "done")) == 1
    assert len(merges(r)) == 1 and env.head() == sha


def test_crash_after_backup_resumes(env):
    old = env.start_old()
    sha = env.candidate({"BUILD": "v2\n"})
    uid = env.request(sha)
    seen = {}

    def crash(step, when, req):
        if step == "backing_up" and when == "done":
            seen["old_alive_at_backup"] = SV.pid_alive(old)
        if step == "backing_up" and when == "advanced":
            raise Crash()

    with pytest.raises(Crash):
        env.sup(hook=crash).run_once()
    r = env.req(uid)
    # verify -> stop -> back up the quiescent DB -> install
    assert seen["old_alive_at_backup"] is False
    assert r["status"] == "installing" and env.head() == env.prior
    assert not SV.pid_alive(old)
    res = env.sup().run_once()
    assert res[0]["status"] == "done"
    r = env.req(uid)
    assert len(entries(r, "backing_up", "done")) == 1
    assert len(merges(r)) == 1 and env.head() == sha
    assert env.heartbeat()["build"] == "v2"


@pytest.mark.parametrize("when", ["advanced", "done"])
def test_crash_after_install_does_not_install_twice(env, when):
    env.start_old()
    sha = env.candidate({"BUILD": "v2\n"})
    uid = env.request(sha)

    def crash(step, w, req):
        if step == "installing" and w == when:
            raise Crash()

    with pytest.raises(Crash):
        env.sup(hook=crash).run_once()
    assert env.head() == sha                                  # merged before the crash
    r = env.req(uid)
    # "done": merged but not journaled; "advanced": journaled
    assert r["status"] == ("installing" if when == "done" else "starting")
    assert env.sup().run_once()[0]["status"] == "done"
    r = env.req(uid)
    assert len(merges(r)) == (1 if when == "advanced" else 0)
    installs = entries(r, "installing", "done")
    assert len(installs) == 1
    if when == "done":
        assert installs[0]["detail"]["note"] == "already installed"
    assert g(env.repo, "rev-list", "--count", "HEAD") == "2"  # one candidate commit, once
    assert env.heartbeat()["build"] == "v2"
    assert sum(1 for e in env.events() if e.startswith("start ") and e.endswith(" v2")) == 1


def test_crash_after_stop_resumes(env):
    old = env.start_old()
    sha = env.candidate({"BUILD": "v2\n"})
    uid = env.request(sha)

    def crash(step, w, req):
        if step == "stopping" and w == "advanced":
            raise Crash()

    with pytest.raises(Crash):
        env.sup(hook=crash).run_once()
    assert not SV.pid_alive(old) and env.req(uid)["status"] == "backing_up"
    assert env.sup().run_once()[0]["status"] == "done"
    assert env.head() == sha and env.heartbeat()["build"] == "v2"


# ---------------------------------------------------------------------------- refusals

def test_verify_failure_stops_nothing(env):
    old = env.start_old()
    sha = env.candidate({"BUILD": "v2\n", "tests/test_ok.py": FAILING})
    uid = env.request(sha)
    res = env.sup().run_once()
    assert res[0]["status"] == "failed"
    r = env.req(uid)
    assert r["log"]["state"]["failure"]["step"] == "verifying"
    fail = entries(r, "verifying", "failed")[0]["detail"]
    assert "manifest tests failed" in fail["reason"] and fail["stopped"] is False
    assert not entries(r, "stopping")
    assert SV.pid_alive(old) and env.pid() == old             # the old build was never stopped
    assert f"stop {old}" not in env.events()
    assert env.head() == env.prior
    assert not env.store.project("p1")["paused"]              # the supervisor's pause cleared


def test_verify_refuses_missing_candidate_and_no_tests(env):
    env.start_old()
    uid = env.request("f" * 40)
    assert env.sup().run_once()[0]["status"] == "failed"
    assert "not in the router repository" in entries(env.req(uid), "verifying",
                                                      "failed")[0]["detail"]["reason"]
    sha = env.candidate({"BUILD": "v2\n"})
    uid = SV.request_upgrade(env.store, case_id="c2", project_id="p1", task_id="t1",
                             candidate_sha=sha, manifest={"tests": {"focused": []}})
    assert env.sup().run_once()[0]["status"] == "failed"
    assert "names no tests" in entries(env.req(uid), "verifying", "failed")[0]["detail"]["reason"]


def test_dirty_tracked_file_refuses_install(env):
    old = env.start_old()
    sha = env.candidate({"BUILD": "v2\n"})
    uid = env.request(sha)
    write(env.repo, "README.md", "local edit\n")              # dirty tracked file
    write(env.repo, "untracked.txt", "left alone\n")
    assert env.sup().run_once()[0]["status"] == "failed"
    r = env.req(uid)
    assert "dirty tracked files" in entries(r, "verifying", "failed")[0]["detail"]["reason"]
    assert SV.pid_alive(old) and env.head() == env.prior
    assert open(os.path.join(env.repo, "README.md")).read() == "local edit\n"


def test_dirty_tracked_file_at_install_rolls_back_without_reset(env):
    old = env.start_old()
    sha = env.candidate({"BUILD": "v2\n"})
    uid = env.request(sha)

    def dirty(step, when, req):
        if step == "stopping" and when == "advanced":
            write(env.repo, "README.md", "edited during the upgrade\n")

    assert env.sup(hook=dirty).run_once()[0]["status"] == "rolled_back"
    r = env.req(uid)
    assert "dirty tracked files" in r["log"]["state"]["failure"]["reason"]
    assert not merges(r) and env.head() == env.prior
    assert entries(r, "rolling_back", "reset")[0]["detail"]["note"] == "HEAD unchanged"
    # the operator's edit survives (no reset --hard when nothing was installed)
    assert open(os.path.join(env.repo, "README.md")).read() == "edited during the upgrade\n"
    new = env.pid()
    assert new != old and SV.pid_alive(new) and env.heartbeat()["build"] == "v1"


# ---------------------------------------------------------------------------- processes

def test_stop_kills_after_timeout(env):
    write(env.repo, "IGNORE_STOP", "1\n")                     # untracked: this build won't exit
    old = env.start_old()
    s = env.sup(stop_timeout=0.5)
    out = s.stop_coordinator()
    assert out["how"] == "killed" and out["was_running"]
    assert not SV.pid_alive(old)
    assert not os.path.exists(os.path.join(env.state, "coordinator.pid"))


def test_stop_target_from_store_owner(env):
    import socket
    old = env.start_old()
    os.remove(os.path.join(env.state, "coordinator.pid"))     # started by hand: no pid file
    env.store.set_meta("owner", json.dumps({"id": f"{socket.gethostname()}:{old}",
                                            "beat": time.time()}))
    s = env.sup()
    assert s.stop_target() == (old, "store_owner")
    out = s.stop_coordinator()
    assert out["how"] == "graceful" and not SV.pid_alive(old)
    assert out["owner_released"] and env.store.meta("owner") is None


def test_unidentifiable_running_coordinator_refused_before_stop(env):
    old = env.start_old()
    os.remove(os.path.join(env.state, "coordinator.pid"))
    env.store.set_meta("owner", json.dumps({"id": "otherhost:1", "beat": time.time()}))
    sha = env.candidate({"BUILD": "v2\n"})
    uid = env.request(sha)
    assert env.sup().run_once()[0]["status"] == "failed"
    assert "configure --stop-cmd" in entries(env.req(uid), "verifying",
                                             "failed")[0]["detail"]["reason"]
    assert SV.pid_alive(old) and env.head() == env.prior
    kill(old)


def test_cli_once(env):
    old = env.start_old()
    sha = env.candidate({"BUILD": "v2\n"})
    uid = env.request(sha)
    r = subprocess.run([sys.executable, "-m", "foundry_coordinator.supervisor",
                        "--state", env.state, "--repo", env.repo, "--coordinator-cmd", env.cmd,
                        "--stop-timeout", "5", "--health-timeout", "15", "--once"],
                       cwd=REPO_ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["upgrade_id"] == uid and out["status"] == "done"
    assert env.head() == sha and not SV.pid_alive(old)
    deadline = time.time() + 10
    while time.time() < deadline and env.heartbeat().get("build") != "v2":
        time.sleep(0.1)
    assert env.heartbeat()["build"] == "v2"
    st = subprocess.run([sys.executable, "-m", "foundry_coordinator.supervisor",
                         "--state", env.state, "--status"], cwd=REPO_ROOT, capture_output=True,
                        text=True, timeout=60)
    assert json.loads(st.stdout.strip().splitlines()[-1])["status"] == "done"


# ---------------------------------------------------------------------------- review B regressions

def _read(env, rel):
    with open(os.path.join(env.repo, rel), encoding="utf-8") as fh:
        return fh.read()


def test_user_commit_during_upgrade_is_not_reset_away(env):
    """HEAD moved (someone committed) without an install: the rollback never
    resets, the commit and an uncommitted tracked edit survive, the old build
    restarts."""
    env.start_old()
    sha = env.candidate({"BUILD": "v2\n"})
    uid = env.request(sha)
    mine = {}

    def hook(step, when, req):
        if step == "stopping" and when == "advanced":
            write(env.repo, "README.md", "user commit\n")
            g(env.repo, "commit", "-q", "-am", "user work")
            mine["sha"] = g(env.repo, "rev-parse", "HEAD")
            write(env.repo, "BUILD", "user uncommitted edit\n")   # tracked, dirty

    res = env.sup(hook=hook).run_once()
    r = env.req(uid)
    assert res[0]["status"] == "rolled_back"
    assert env.head() == mine["sha"] != env.prior               # the user's commit is kept
    assert _read(env, "BUILD") == "user uncommitted edit\n"     # and the uncommitted edit
    reset = entries(r, "rolling_back", "reset")[0]["detail"]
    assert "without an install" in reset["note"] and reset["to"] == mine["sha"]
    assert not merges(r)
    assert r["rollback_result"]["restarted_healthy"] is True
    assert not env.store.project("p1")["paused"]


def test_tracked_edit_after_install_survives_rollback(env):
    """A tracked file the candidate didn't change, edited after the install:
    `git reset --keep` restores the prior commit and keeps the edit."""
    env.start_old()
    sha = env.candidate({"BUILD": "v2\n", "FAIL_HEALTH": "1\n"})
    uid = env.request(sha)

    def hook(step, when, req):
        if step == "starting" and when == "advanced":
            write(env.repo, "README.md", "user edit while new build checks\n")

    assert env.sup(hook=hook).run_once()[0]["status"] == "rolled_back"
    assert env.head() == env.prior
    assert _read(env, "README.md") == "user edit while new build checks\n"
    assert not os.path.exists(os.path.join(env.repo, "FAIL_HEALTH"))
    reset = entries(env.req(uid), "rolling_back", "reset")[0]["detail"]
    assert reset["how"] == "reset --keep" and reset["to"] == env.prior


def test_local_change_to_a_candidate_file_hands_the_rollback_to_a_human(env):
    """`reset --keep` refuses to discard a local edit of a file the candidate
    changed: the rollback ends failed, nothing is lost, the pause is
    released and the journal says a human must look."""
    env.start_old()
    sha = env.candidate({"BUILD": "v2\n", "FAIL_HEALTH": "1\n"})
    uid = env.request(sha)

    def hook(step, when, req):
        if step == "starting" and when == "advanced":
            write(env.repo, "BUILD", "precious local edit\n")

    assert env.sup(hook=hook).run_once()[0]["status"] == "failed"
    r = env.req(uid)
    assert env.head() == sha                                     # not reset
    assert _read(env, "BUILD") == "precious local edit\n"
    rb = r["rollback_result"]
    assert rb["needs_human"] is True and "reset --keep" in rb["rollback_failed"]
    last = entries(r, "rolling_back", "rollback_failed")[-1]["detail"]
    assert last["needs_human"] is True and last["pause_released"] == ["p1"]
    assert not env.store.project("p1")["paused"]                 # never held forever
    assert env.store.meta(PROJECT_PAUSE_META + "p1") is None


def test_head_moved_after_install_is_not_reset(env):
    env.start_old()
    sha = env.candidate({"BUILD": "v2\n", "FAIL_HEALTH": "1\n"})
    uid = env.request(sha)
    mine = {}

    def hook(step, when, req):
        if step == "starting" and when == "advanced":
            write(env.repo, "README.md", "a commit on top of the candidate\n")
            g(env.repo, "commit", "-q", "-am", "on top")
            mine["sha"] = g(env.repo, "rev-parse", "HEAD")

    assert env.sup(hook=hook).run_once()[0]["status"] == "failed"
    assert env.head() == mine["sha"]                             # the commit is kept
    rb = env.req(uid)["rollback_result"]
    assert rb["needs_human"] and "a human" in rb["rollback_failed"]
    assert not env.store.project("p1")["paused"]


def test_health_rejects_foreign_owner_beat(env):
    s = env.sup(health_timeout=2)
    # a coordinator that never heartbeats (hung at import)
    s.coordinator_cmd = f'"{sys.executable}" -c "import time; time.sleep(20)"'
    info = s.start_coordinator("new")
    # some OTHER process (another host sharing the store) beats the owner record
    env.store.set_meta("owner", json.dumps({"id": "otherhost:4242", "beat": time.time() + 1}))
    with pytest.raises(SV.StepFailed, match="no healthy heartbeat"):
        s.health(info["started_ts"], info["pid"], 2)
    # a heartbeat file from some other pid isn't the started coordinator either
    with open(os.path.join(env.state, "coordinator.heartbeat"), "w") as fh:
        json.dump({"pid": 4242, "ts": time.time() + 1, "ok": True}, fh)
    with pytest.raises(SV.StepFailed, match="not the started coordinator"):
        s.health(info["started_ts"], info["pid"], 1)
    # the owner record of THIS host and the started pid does count
    env.store.set_meta("owner", json.dumps({"id": f"{socket.gethostname()}:{info['pid']}",
                                            "beat": time.time() + 1}))
    assert s.health(info["started_ts"], info["pid"], 2)["source"] == "owner"
    # ... but not when the release or the lead readiness must be checked
    with pytest.raises(SV.StepFailed):
        s.health(info["started_ts"], info["pid"], 1, require_lead=True)


def test_health_checks_release_and_lead_readiness(env):
    s = env.sup()
    info = s.start_coordinator("x")
    h = s.health(info["started_ts"], info["pid"], 15, expect_release="1.0.0", require_lead=True)
    assert h["heartbeat"]["version"] == "1.0.0" and h["pid"] == env.pid()
    with pytest.raises(SV.StepFailed, match="reports release '1.0.0'"):
        s.health(info["started_ts"], info["pid"], 5, expect_release="9.9.9")


@pytest.mark.parametrize("marker, why", [("LEAD_NOT_READY", "lead readiness failed"),
                                         ("LEAD_UNKNOWN", "lead readiness unknown")])
def test_lead_not_ready_rolls_back_when_the_manifest_enables_it(env, marker, why):
    env.start_old()
    sha = env.candidate({"BUILD": "v2\n", marker: "1\n"})
    uid = env.request(sha, lead_enabled=True)
    assert env.sup().run_once()[0]["status"] == "rolled_back"
    assert why in env.req(uid)["failure"]["reason"]
    assert env.head() == env.prior


def test_lead_readiness_not_required_when_nothing_enables_it(env):
    env.start_old()
    sha = env.candidate({"BUILD": "v2\n", "LEAD_UNKNOWN": "1\n"})
    uid = env.request(sha)
    assert env.sup().run_once()[0]["status"] == "done"
    assert env.req(uid)["status"] == "done" and env.head() == sha


def test_lead_not_ready_rolls_back_when_a_profile_enables_it(env, tmp_path):
    prof = tmp_path / "profile.toml"
    prof.write_text("[lead_manager]\nenabled = true\n", encoding="utf-8")
    env.start_old()
    sha = env.candidate({"BUILD": "v2\n", "LEAD_NOT_READY": "1\n"})
    uid = env.request(sha)
    assert env.sup(profiles=[str(prof)]).run_once()[0]["status"] == "rolled_back"
    assert "lead readiness failed" in env.req(uid)["failure"]["reason"]


def test_release_mismatch_rolls_back(env):
    env.start_old()
    sha = env.candidate({"BUILD": "v2\n", "foundry_router/__init__.py": '__version__ = "2.0.0"\n'})
    uid = env.request(sha, release="2.0.0")
    # the started build reports the version it runs: the candidate's
    assert env.sup().run_once()[0]["status"] == "done"
    assert env.heartbeat()["version"] == "2.0.0"
    assert env.req(uid)["status"] == "done"


def test_stale_pid_file_never_kills_an_unrelated_process(env):
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        pidf = os.path.join(env.state, "coordinator.pid")
        # 1. a legacy pid file naming a live process with no heartbeat: refused
        with open(pidf, "w") as fh:
            fh.write(str(victim.pid))
        s = env.sup(stop_timeout=0.5)
        with pytest.raises(SV.StepFailed, match="can't be verified"):
            s.stop_coordinator()
        with pytest.raises(SV.StepFailed):
            s.start_coordinator("x")                  # nor started over it
        assert victim.poll() is None
        # 2. a JSON pid file whose pid was reused (another creation time): ignored
        with open(pidf, "w") as fh:
            json.dump({"pid": victim.pid, "created": 1000.0, "exe": sys.executable}, fh)
        out = s.stop_coordinator()
        assert out["how"] == "none" and out["ignored_pid"] == {"pid": victim.pid,
                                                               "why": "reused"}
        assert victim.poll() is None and not os.path.exists(pidf)
        # 3. the full upgrade refuses before anything is stopped
        with open(pidf, "w") as fh:
            fh.write(str(victim.pid))
        sha = env.candidate({"BUILD": "v2\n"})
        uid = env.request(sha)
        assert env.sup().run_once()[0]["status"] == "failed"
        assert "can't be verified" in env.req(uid)["failure"]["reason"]
        assert victim.poll() is None and env.head() == env.prior
    finally:
        victim.kill()
        victim.wait(10)


def test_legacy_pid_file_with_a_fresh_heartbeat_is_stopped(env):
    env.cmd = env.cmd.replace(" --pid-json", "")
    old = env.start_old()
    assert SV.read_pid_file(os.path.join(env.state, "coordinator.pid"))["legacy"] is True
    s = env.sup()
    assert s.stop_target() == (old, "pid_file")
    assert s.stop_coordinator("up-x")["how"] == "graceful" and not SV.pid_alive(old)


def test_pid_file_format_and_identity(tmp_path):
    p = str(tmp_path / "coordinator.pid")
    rec = SV.write_pid_file(p)
    got = SV.read_pid_file(p)
    assert got["pid"] == os.getpid() and got["legacy"] is False
    assert got["created"] == rec["created"] and got["created"] is not None
    assert SV.pid_matches(got) is True
    assert SV.pid_matches(dict(got, created=got["created"] - 100)) is False
    assert SV.pid_matches(dict(got, exe=got["exe"] + ".other")) is False
    with pytest.raises(FileExistsError):
        SV.write_pid_file(p, exclusive=True)
    with open(p, "w") as fh:
        fh.write("1234\n")
    assert SV.read_pid_file(p) == {"pid": 1234, "created": None, "exe": "", "legacy": True,
                                   "mtime": os.path.getmtime(p)}
    with open(p, "w") as fh:
        fh.write("garbage")
    assert SV.read_pid_file(p) is None


def test_pid_alive_is_false_for_a_process_that_exited_with_259():
    p = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(259)"])
    p.wait(30)
    assert p.returncode == 259
    assert SV.pid_alive(p.pid) is False                       # the handle is still open
    assert SV.pid_alive(os.getpid()) is True


def test_all_projects_paused_and_other_projects_progress_kept(env):
    env.start_old()
    env.store.register_project("p2", "P2", "profile2.toml", "h", "1", env.repo)
    sha = env.candidate({"BUILD": "v2\n", "FAIL_HEALTH": "1\n"})
    uid = env.request(sha)
    seen = {}

    def hook(step, when, req):
        if step == "waiting_safe_point" and when == "done":
            seen["p2_paused"] = env.store.project("p2")["paused"]
            seen["p2_meta"] = json.loads(env.store.meta(PROJECT_PAUSE_META + "p2"))
        if step == "starting" and when == "advanced":
            # progress recorded after the backup (the new build does no migration)
            env.store.set_meta("p2_progress", "paid call made")

    assert env.sup(hook=hook).run_once()[0]["status"] == "rolled_back"
    r = env.req(uid)
    assert seen["p2_paused"] and seen["p2_meta"]["actor"] == "supervisor"
    assert env.store.meta("p2_progress") == "paid call made"   # not rolled back
    db = entries(r, "rolling_back", "db_kept")[0]["detail"]
    assert db["restored"] is False and "did not migrate" in db["note"]
    assert r["rollback_result"]["db_restored"] is False
    assert not env.store.project("p1")["paused"] and not env.store.project("p2")["paused"]
    assert sorted(entries(r, "rolling_back", "done")[0]["detail"]["released"]) == ["p1", "p2"]


def test_a_busy_other_project_blocks_the_safe_point(env):
    env.start_old()
    env.store.register_project("p2", "P2", "profile2.toml", "h", "1", env.repo)
    sha = env.candidate({"BUILD": "v2\n"})
    uid = env.request(sha)
    env.store.x("INSERT INTO tasks (project_id, task_id, state, session_id, data) "
                "VALUES ('p2','t9','IMPLEMENTING','s9','{}')")
    env.store.x("INSERT INTO sessions (session_id, project_id, task_id, prompt_ts, created_ts) "
                "VALUES ('s9','p2','t9',1,1)")
    assert env.sup().run_once()[0]["status"] == "waiting_safe_point"
    assert env.store.project("p1")["paused"] and env.store.project("p2")["paused"]
    assert env.head() == env.prior
    env.store.x("UPDATE sessions SET ended_ts=2 WHERE session_id='s9'")
    assert env.sup().run_once()[0]["status"] == "done"
    assert env.req(uid)["status"] == "done"
    assert not env.store.project("p2")["paused"]


def test_crash_during_rollback_restart_adopts_the_running_old_build(env):
    env.start_old()
    sha = env.candidate({"BUILD": "v2\n", "FAIL_HEALTH": "1\n"})
    uid = env.request(sha)
    s = env.sup()
    real = s.health
    calls = {"n": 0}

    def health(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:              # the rollback's restart of the old build
            raise Crash()
        return real(*a, **k)
    s.health = health
    with pytest.raises(Crash):
        s.run_once()
    res = env.sup().run_once()
    r = env.req(uid)
    assert res[0]["status"] == "rolled_back", r["log"]["entries"][-3:]
    restarted = entries(r, "rolling_back", "restarted")[-1]["detail"]
    assert restarted["adopted"] is True and restarted["healthy"] is True
    # old v1, new v2, the rollback's v1: started once, adopted, never twice
    assert sorted(e.split()[2] for e in env.events()
                  if e.startswith("start ")) == ["v1", "v1", "v2"]
    assert not env.store.project("p1")["paused"]                # the pause is released
    assert SV.pid_alive(env.pid()) and env.heartbeat()["build"] == "v1"


@pytest.mark.parametrize("change, why", [
    ({"review_verdict": "CHANGES_REQUIRED"}, "not APPROVED"),
    ({"review_verdict": None}, "not APPROVED"),
    ({"files": ["BUILD"]}, "not the actual"),
    ({"tests": {"focused": ["--co"], "broader": ["tests"],
                "regression": "tests/test_ok.py::test_ok"}}, "test ids are refused"),
    ({"tests": {"focused": ["tests/test_ok.py"], "broader": ["tests"],
                "regression": "tests/test_ok.py"}}, "test ids are refused"),
    ({"tests": {"focused": ["tests/test_ok.py"], "broader": [],
                "regression": "tests/test_ok.py::test_ok"}}, "names no tests"),
    ({"tests": {"focused": ["tests/test_ok.py"], "broader": ["tests"],
                "regression": "tests/test_ok.py::test_missing"}}, "manifest tests failed"),
])
def test_verify_rechecks_the_request_row(env, change, why):
    old = env.start_old()
    sha = env.candidate({"BUILD": "v2\n", "README.md": "changed\n"})
    change = dict(change)
    t = change.pop("tests", None)
    if t:
        change.update(tests=t["focused"], broader=t["broader"], regression=t["regression"])
    uid = env.request(sha, **change)
    assert env.sup().run_once()[0]["status"] == "failed"
    assert why in env.req(uid)["failure"]["reason"]
    assert SV.pid_alive(old) and env.head() == env.prior        # nothing stopped


def test_stop_request_names_the_upgrade_and_supervisor(env):
    old = env.start_old()
    s = env.sup()
    assert s.stop_coordinator("up-c1-abc")["how"] == "graceful"
    line = next(e for e in env.events() if e.startswith(f"stopreq {old} "))
    req = json.loads(line.split(" ", 2)[2])
    assert req["upgrade_id"] == "up-c1-abc" and req["supervisor_pid"] == os.getpid()
    assert req["pid"] == old
    assert not os.path.exists(os.path.join(env.state, "coordinator.stop"))


def test_stop_never_kills_the_process_tree(env, monkeypatch):
    write(env.repo, "IGNORE_STOP", "1\n")
    old = env.start_old()
    calls = []
    real_run = subprocess.run

    def spy(cmd, *a, **k):
        calls.append(cmd)
        return real_run(cmd, *a, **k)
    monkeypatch.setattr(SV.subprocess, "run", spy)
    out = env.sup(stop_timeout=0.5).stop_coordinator()
    assert out["how"] == "killed" and not SV.pid_alive(old)
    assert not [c for c in calls if isinstance(c, list) and c and "taskkill" in str(c[0])]


def test_cancel_request_only_cancels_a_request_not_yet_started(tmp_path):
    from foundry_coordinator import supervisor as SV
    from foundry_coordinator.store import Store
    st = Store(str(tmp_path / "c.sqlite"))
    SV.migrate(st)
    uid = SV.request_upgrade(st, case_id="rc-1", project_id="game", task_id="T1",
                             candidate_sha="a" * 40, manifest={"candidate_sha": "a" * 40})
    assert SV.cancel_request(st, uid, reason="task cancelled") is True
    assert SV.get_request(st, uid)["status"] == "cancelled"
    assert "cancelled" in SV.TERMINAL
    assert SV.cancel_request(st, uid) is False                    # already terminal
    uid2 = SV.request_upgrade(st, case_id="rc-2", project_id="game", task_id="T1",
                              candidate_sha="b" * 40, manifest={"candidate_sha": "b" * 40})
    st.x("UPDATE upgrade_requests SET status='verifying' WHERE upgrade_id=?", (uid2,))
    assert SV.cancel_request(st, uid2) is False                   # in progress: left alone
    assert SV.get_request(st, uid2)["status"] == "verifying"
