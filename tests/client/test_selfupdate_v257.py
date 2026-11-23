"""The Foundry Client updates its stack to a newer release, on demand or automatically
(v0.181.257). 2026-11-23, Victor: "Can you build this update feature in to the client, so it
can update on demand / autoupdate", and for the timing "Make a gap": new sessions wait, the
running ones finish, then the update. These run a real git repository with release tags and a
real coordinator store; only the stack (heartbeat, restarts, the client restart) is faked."""
import copy
import json
import os
import subprocess
import sys
import threading

import pytest
from fastapi.testclient import TestClient

from foundry_client import api, config, secrets, selfupdate as U
from foundry_client.manager import Manager
from foundry_coordinator.store import Store
from foundry_coordinator.telemetry import PROJECT_PAUSE_META


def g(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def commit(repo, files: dict, msg: str) -> str:
    for p, text in files.items():
        f = repo / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    g(repo, "add", "-A")
    g(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
    return g(repo, "rev-parse", "HEAD")


def release(repo, ver: str, extra: dict = None) -> str:
    sha = commit(repo, {"foundry_router/__init__.py": f'__version__ = "{ver}"\n', **(extra or {})},
                 f"release {ver}")
    g(repo, "tag", f"v{ver}")
    return sha


@pytest.fixture()
def repos(tmp_path):
    """origin (main with v0.1.1) and the deploy clone, detached at v0.1.1."""
    origin = tmp_path / "origin"
    origin.mkdir()
    g(origin, "init", "-q", "-b", "main")
    commit(origin, {"app.py": "one\n", "kept.py": "a\n"}, "base")
    release(origin, "0.1.0")
    release(origin, "0.1.1")
    deploy = tmp_path / "deploy"
    subprocess.run(["git", "clone", "-q", str(origin), str(deploy)], check=True)
    g(deploy, "-c", "advice.detachedHead=false", "checkout", "-q", "--detach", "v0.1.1")
    return origin, deploy


class FakeUpdateCheck:
    def __init__(self, latest="0.1.2"):
        self.latest = latest

    def snapshot(self):
        return {"latest": self.latest, "available": True, "error": ""}


class FakeMgr:
    def __init__(self, cfg):
        self.cfg = cfg
        self.update = FakeUpdateCheck()
        self.events = []

    def _event(self, name, text):
        self.events.append(text)


class FakeOps:
    """The stack: a heartbeat file the fake coordinator writes when restarted."""

    def __init__(self, cfg, deploy, busy=(), new_release=None):
        self.cfg, self.deploy = cfg, deploy
        self.busy_left = list(busy)
        self.calls = []
        self.hb = {"pid": 100, "release": "0.1.1", "ts": 0.0, "ok": True}
        self.new_release = new_release      # None: the checked-out release beats
        self.wanted = {"coordinator": True, "supervisor": True}
        self.runs_from = str(deploy)
        self.smoke_err = ""
        self.on_restart = None
        self.pauses_seen = []

    def store(self):
        return Store(os.path.join(self.cfg["client"]["state_dir"], "coordinator.sqlite"))

    def busy(self):
        s = self.store()
        try:
            self.pauses_seen.append({p["project_id"]: bool(p["paused"]) for p in s.projects()})
        finally:
            s.conn.close()
        return self.busy_left.pop(0) if self.busy_left else ""

    def heartbeat(self):
        return dict(self.hb)

    def restart(self, name):
        self.calls.append(("restart", name))
        if name == "coordinator":
            rel = self.new_release if self.new_release is not None else \
                U.release_at(str(self.deploy), "HEAD")
            self.hb = {"pid": self.hb["pid"] + 1, "release": rel, "ts": 10 ** 10, "ok": True}
            if self.on_restart:
                self.on_restart()

    def stop(self, name):
        self.calls.append(("stop", name))
        self.wanted[name] = False

    def start(self, name):
        self.calls.append(("start", name))
        self.wanted[name] = True
        self.restart(name)

    def component_wanted(self, name):
        return self.wanted.get(name, False)

    def supervisor_busy(self):
        return False

    def client_runs_from(self):
        return self.runs_from

    def frozen(self):
        return False

    def smoke(self, deploy):
        self.calls.append(("smoke", deploy))
        return self.smoke_err

    def restart_client(self, deploy):
        self.calls.append(("restart_client", deploy))

    def event(self, text):
        pass


@pytest.fixture()
def env(tmp_path, repos, monkeypatch):
    monkeypatch.setenv("FOUNDRY_CLIENT_CONFIG", str(tmp_path / "client.toml"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(secrets, "_kr", lambda: None)
    origin, deploy = repos
    cfg = copy.deepcopy(config.DEFAULTS)
    state = tmp_path / "state"
    state.mkdir()
    cfg["client"]["state_dir"] = str(state)
    cfg["workspace"]["deploy"] = str(deploy)
    s = Store(str(state / "coordinator.sqlite"))
    s.register_project("au", "AU", "p", "h", "1", str(tmp_path))
    s.register_project("op", "OP", "p", "h", "1", str(tmp_path))
    s.conn.close()
    return cfg, origin, deploy


def updater(cfg, ops, **kw):
    m = FakeMgr(cfg)
    kw.setdefault("current", "0.1.1")
    return U.Updater(m, ops=ops, sleep=lambda s: None, poll=0, health_timeout=0.2, **kw), m


def run(up, tag="v0.1.2"):
    """Start the job and run it on this thread (no background thread)."""
    up._spawn = lambda: None
    assert up.start(tag).get("ok")
    return up.run_job()


def paused(cfg):
    s = Store(os.path.join(cfg["client"]["state_dir"], "coordinator.sqlite"))
    try:
        return {p["project_id"]: bool(p["paused"]) for p in s.projects()}
    finally:
        s.conn.close()


# ------------------------------------------------------------------ the whole update

def test_update_makes_a_gap_installs_restarts_coordinator_then_client_and_finishes(env):
    cfg, origin, deploy = env
    sha = release(origin, "0.1.2", {"app.py": "two\n"})
    ops = FakeOps(cfg, deploy, busy=["1 session(s) working", "1 session(s) working"])
    up, _ = updater(cfg, ops)
    job = run(up)
    # every project was paused while it waited for the running session to finish
    assert ops.pauses_seen[0] == {"au": True, "op": True}
    assert job["phase"] == "verifying_client"
    assert g(deploy, "rev-parse", "HEAD") == sha
    assert ops.calls == [("restart", "coordinator"), ("restart", "supervisor"),
                         ("smoke", str(deploy)), ("restart_client", str(deploy))]
    assert paused(cfg) == {"au": False, "op": False}           # resumed before the client restart
    st = json.load(open(up.path(), encoding="utf-8"))
    assert st["job"]["phase"] == "verifying_client"
    texts = " | ".join(x["text"] for x in st["job"]["log"])
    assert "waiting: 1 session(s) working" in texts and "coordinator running 0.1.2" in texts
    # the fresh client finds the job and finishes it on the new release
    up2, _ = updater(cfg, ops, current="0.1.2")
    done = up2.run_job()
    assert done["status"] == "done" and done["error"] == ""
    s = up2.status()
    assert s["job"] is None and s["history"][0]["status"] == "done"
    assert s["history"][0]["prior_release"] == "0.1.1" and s["installed"] == "0.1.2"
    # the old HEAD is kept, and the database was backed up before the checkout moved
    assert g(deploy, "rev-parse", f"refs/foundry/pre-update/{done['id']}") == \
        g(origin, "rev-parse", "v0.1.1")
    assert os.path.isfile(done["backup"])


def test_a_client_on_the_old_release_after_its_restart_says_so(env):
    cfg, origin, deploy = env
    release(origin, "0.1.2")
    ops = FakeOps(cfg, deploy)
    up, _ = updater(cfg, ops)
    run(up)
    up2, _ = updater(cfg, ops, current="0.1.1")
    done = up2.run_job()
    assert done["status"] == "done" and "runs 0.1.1" in done["error"]


def test_an_operator_pause_is_kept_after_the_update(env):
    cfg, origin, deploy = env
    release(origin, "0.1.2")
    s = Store(os.path.join(cfg["client"]["state_dir"], "coordinator.sqlite"))
    s.set_project_paused("op", True, "Victor's pause")
    s.conn.close()
    ops = FakeOps(cfg, deploy)
    up, _ = updater(cfg, ops)
    job = run(up)
    assert job["pauses"] == {"au": False, "op": False}            # op was never the job's
    assert paused(cfg) == {"au": False, "op": True}


def test_a_client_not_run_from_the_deploy_checkout_finishes_without_restarting_itself(env):
    cfg, origin, deploy = env
    release(origin, "0.1.2")
    ops = FakeOps(cfg, deploy)
    ops.runs_from = str(deploy.parent)
    up, _ = updater(cfg, ops)
    job = run(up)
    assert job["status"] == "done" and ("restart_client", str(deploy)) not in ops.calls
    assert "not the deploy checkout" in job["log"][-1]["text"]


def test_new_code_that_does_not_import_keeps_this_client_running(env):
    cfg, origin, deploy = env
    release(origin, "0.1.2")
    ops = FakeOps(cfg, deploy)
    ops.smoke_err = "SyntaxError: bad"
    up, _ = updater(cfg, ops)
    job = run(up)
    assert job["status"] == "done" and "SyntaxError" in job["error"]
    assert not any(c[0] == "restart_client" for c in ops.calls)


# ------------------------------------------------------------------ checks before anything changes

def test_a_deploy_snapshot_that_kept_its_own_file_is_refused_and_nothing_changes(env):
    cfg, origin, deploy = env
    commit(deploy, {"kept.py": "deploy copy\n"}, "deploy snapshot, kept.py kept")
    head = g(deploy, "rev-parse", "HEAD")
    release(origin, "0.1.2", {"kept.py": "b\n"})
    ops = FakeOps(cfg, deploy)
    up, _ = updater(cfg, ops)
    job = run(up)
    assert job["status"] == "failed" and "kept.py" in job["error"]
    assert "nothing was changed" in job["log"][-1]["text"]
    assert g(deploy, "rev-parse", "HEAD") == head and ops.calls == []
    assert paused(cfg) == {"au": False, "op": False}


def test_a_deploy_snapshot_of_mains_own_files_is_fine(env):
    """A snapshot commit that only copies main's files (the usual deploy snapshot) is not a
    local change: every blob it holds is in the release's history."""
    cfg, origin, deploy = env
    commit(origin, {"kept.py": "b\n"}, "main changes kept.py")
    g(deploy, "fetch", "-q", "origin")
    g(deploy, "checkout", "-q", "origin/main", "--", "kept.py")
    commit(deploy, {}, "deploy snapshot: kept.py from main")
    sha = release(origin, "0.1.2", {"app.py": "two\n"})
    ops = FakeOps(cfg, deploy)
    up, _ = updater(cfg, ops)
    job = run(up)
    assert job["phase"] == "verifying_client" and g(deploy, "rev-parse", "HEAD") == sha


def test_local_changes_names_only_files_whose_content_main_never_had(repos):
    origin, deploy = repos
    commit(deploy, {"kept.py": "mine\n", "new_local.py": "x\n"}, "local")
    head = g(deploy, "rev-parse", "HEAD")
    sha = release(origin, "0.1.2", {"app.py": "two\n"})
    g(deploy, "fetch", "-q", "origin", "--tags")
    assert U.local_changes(str(deploy), head, sha) == ["kept.py", "new_local.py"]


@pytest.mark.parametrize("case", ["missing", "older", "same", "side_branch", "wrong_version",
                                  "dirty"])
def test_the_release_checks_refuse_and_change_nothing(env, case):
    cfg, origin, deploy = env
    tag = "v0.1.2"
    if case == "older":
        tag = "v0.1.0"
    elif case == "same":
        tag = "v0.1.1"
    elif case == "side_branch":
        g(origin, "checkout", "-q", "-b", "side")
        release(origin, "0.1.2")
        g(origin, "checkout", "-q", "main")
    elif case == "wrong_version":
        commit(origin, {"foundry_router/__init__.py": '__version__ = "0.1.9"\n'}, "x")
        g(origin, "tag", "v0.1.2")
    elif case == "dirty":
        release(origin, "0.1.2")
        (deploy / "app.py").write_text("edited\n", encoding="utf-8")
    head = g(deploy, "rev-parse", "HEAD")
    ops = FakeOps(cfg, deploy)
    up, _ = updater(cfg, ops)
    job = run(up, tag)
    assert job["status"] == "failed", job
    want = {"missing": "not in the repository", "older": "not newer",
            "same": "already on", "side_branch": "not on main",
            "wrong_version": "names release 0.1.9", "dirty": "modified files"}[case]
    assert want in job["error"]
    assert g(deploy, "rev-parse", "HEAD") == head and ops.calls == []


# ------------------------------------------------------------------ the gap

def test_no_gap_within_the_limit_gives_up_and_releases_the_pauses(env):
    cfg, origin, deploy = env
    release(origin, "0.1.2")
    cfg["client"]["update_gap_max_minutes"] = 5
    ops = FakeOps(cfg, deploy, busy=["2 session(s) working"] * 1000)
    t = [0.0]
    up, _ = updater(cfg, ops, clock=lambda: t[0])
    up.sleep = lambda s: t.__setitem__(0, t[0] + 30)
    up._spawn = lambda: None
    cfg["client"]["auto_update"] = True
    assert up.maybe_auto()["job"]["auto"]
    job = up.run_job()
    assert job["status"] == "failed" and "no gap within 5 min" in job["error"]
    assert "2 session(s) working" in job["error"]
    assert paused(cfg) == {"au": False, "op": False}
    assert g(deploy, "rev-parse", "HEAD") == g(origin, "rev-parse", "v0.1.1") and ops.calls == []
    # busy, not broken: the automatic update tries again at the next check
    assert up.load()["auto_failed"] == [] and up.maybe_auto()["ok"]


def test_cancel_while_waiting_for_the_gap_releases_the_pauses(env):
    cfg, origin, deploy = env
    release(origin, "0.1.2")
    ops = FakeOps(cfg, deploy, busy=["1 session(s) working"] * 1000)
    up, _ = updater(cfg, ops)
    n = [0]

    def sleep(s):
        n[0] += 1
        if n[0] == 3:
            assert up.status()["cancellable"]
            assert up.cancel()["ok"]
    up.sleep = sleep
    job = run(up)
    assert job["status"] == "cancelled" and paused(cfg) == {"au": False, "op": False}
    assert ops.calls == []
    assert up.cancel()["error"] == "no update is running"


# ------------------------------------------------------------------ rollback

def test_a_coordinator_that_never_beats_the_new_release_rolls_back(env):
    cfg, origin, deploy = env
    prior = g(deploy, "rev-parse", "HEAD")
    release(origin, "0.1.2")
    ops = FakeOps(cfg, deploy, new_release="0.1.1")      # the old code still answers
    up, _ = updater(cfg, ops)
    job = run(up)
    assert job["status"] == "rolled_back" and "no heartbeat from a coordinator on 0.1.2" in job["error"]
    assert g(deploy, "rev-parse", "HEAD") == prior
    assert ops.calls == [("restart", "coordinator"), ("restart", "coordinator")]
    assert paused(cfg) == {"au": False, "op": False}
    assert "checkout back on 0.1.1" in job["log"][-1]["text"]


def test_a_rollback_restores_the_database_only_when_the_new_build_migrated_it(env):
    cfg, origin, deploy = env
    release(origin, "0.1.2")
    db = os.path.join(cfg["client"]["state_dir"], "coordinator.sqlite")
    ops = FakeOps(cfg, deploy, new_release="broken")

    def migrate():
        if ops.migrated:
            return
        ops.migrated = True
        s = Store(db)
        s.set_meta("schema:new_thing", "1")
        s.set_meta("work", "done after the backup")
        s.conn.close()
    ops.migrated = False
    ops.on_restart = migrate
    up, _ = updater(cfg, ops)
    job = run(up)
    assert job["status"] == "rolled_back"
    assert "database restored" in job["log"][-1]["text"]
    s = Store(db)
    try:
        assert s.meta("schema:new_thing") is None
    finally:
        s.conn.close()
    assert ("stop", "coordinator") in ops.calls and ("start", "coordinator") in ops.calls


# ------------------------------------------------------------------ automatic updates

def test_automatic_updates_start_only_when_on_newer_and_not_failed_before(env):
    cfg, origin, deploy = env
    release(origin, "0.1.2")
    ops = FakeOps(cfg, deploy)
    up, m = updater(cfg, ops)
    up._spawn = lambda: None
    assert config.DEFAULTS["client"]["auto_update"] is False      # ships off
    assert up.maybe_auto() is None
    cfg["client"]["auto_update"] = True
    m.update.latest = "0.1.1"
    assert up.maybe_auto() is None                                  # not newer than installed
    m.update.latest = "0.1.2"
    r = up.maybe_auto()
    assert r["ok"] and r["job"]["auto"] and r["job"]["tag"] == "v0.1.2"
    assert up.maybe_auto() is None                                  # one at a time
    ops.new_release = "0.1.1"
    job = up.run_job()
    assert job["status"] == "rolled_back"
    assert up.maybe_auto() is None                                  # a failed release is not retried
    assert up.start("v0.1.2")["ok"]                                 # but Update now can


def test_an_update_is_resumed_by_the_next_client(env):
    cfg, origin, deploy = env
    release(origin, "0.1.2")
    ops = FakeOps(cfg, deploy)
    up, _ = updater(cfg, ops)
    up._spawn = lambda: None
    up.start("v0.1.2")
    up2, _ = updater(cfg, ops)
    ran = threading.Event()
    up2._spawn = ran.set
    up2.resume_on_start()
    assert ran.is_set()
    assert up2.start("v0.1.2")["error"] == "an update is already running"


# ------------------------------------------------------------------ settings, API, page

def test_update_settings_validate():
    c = copy.deepcopy(config.DEFAULTS)
    assert not [e for e in config.validate(c) if "update" in e]
    c["client"].update(update_check_minutes=1, update_gap_max_minutes="x", auto_update="yes")
    errs = " ".join(config.validate(c))
    assert "update_check_minutes" in errs and "update_gap_max_minutes" in errs
    assert "auto_update must be true or false" in errs


def test_the_api_starts_reports_and_cancels_updates(env, monkeypatch):
    cfg, origin, deploy = env
    release(origin, "0.1.2")
    for n in cfg["components"]:
        cfg["components"][n]["enabled"] = False
    m = Manager(cfg, popen=lambda argv, **kw: None)
    m.updater._spawn = lambda: None
    app = api.create_app(m, "t" * 40)
    c = TestClient(app, base_url="http://127.0.0.1")
    h = {"X-Client-Token": "t" * 40}
    assert c.post("/api/update/apply", json={}, headers=h).status_code == 409   # no release seen
    r = c.post("/api/update/apply", json={"tag": "v0.1.2"}, headers=h)
    assert r.status_code == 200 and r.json()["job"]["tag"] == "v0.1.2"
    st = c.get("/api/status", headers=h).json()["self_update"]
    assert st["job"]["phase"] == "checking" and st["cancellable"] and st["installed"] == "0.1.1"
    assert c.post("/api/update/apply", json={}, headers=h).status_code == 409   # already running
    assert c.post("/api/update/cancel", headers=h).json()["ok"]
    assert c.get("/api/update", headers=h).json()["history"][0]["status"] == "cancelled"
    # settings the page sends are saved
    r = c.put("/api/settings", json={"settings": {"client": {"auto_update": True,
                                                             "update_check_minutes": 30}}},
              headers=h)
    assert r.status_code == 200 and m.cfg["client"]["auto_update"] is True


def test_the_page_has_update_now_cancel_and_the_update_settings():
    page = open(os.path.join(os.path.dirname(api.__file__), "static", "client.html"),
                encoding="utf-8").read()
    for s in ('data-do="update-apply"', 'data-do="update-cancel"', '"update/apply"',
              '"update/cancel"', "client.auto_update", "client.update_check_minutes",
              "client.update_gap_max_minutes"):
        assert s in page, s
    assert "the client never updates itself" not in page


def test_the_real_ops_reports_the_gap_from_the_client_safe_point(env):
    cfg, origin, deploy = env
    m = FakeMgr(cfg)
    ops = U.Ops(m)
    assert ops.busy() == ""
    s = Store(os.path.join(cfg["client"]["state_dir"], "coordinator.sqlite"))
    from foundry_coordinator import supervisor as SUP
    SUP.migrate(s)
    SUP.request_upgrade(s, case_id="c1", project_id="au", task_id="T", candidate_sha="ab" * 20,
                        manifest={})
    s.conn.close()
    assert ops.busy() == "1 Lead upgrade(s) open"
