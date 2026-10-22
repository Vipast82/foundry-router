"""Foundry Client: settings, manager, safe point, local API guard, autostart."""
import copy
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

from foundry_client import api, autostart, config, secrets
from foundry_client.components import safe_point
from foundry_client.manager import Manager


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FOUNDRY_CLIENT_CONFIG", str(tmp_path / "client.toml"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(secrets, "_kr", lambda: None)          # never the real OS keyring
    c = copy.deepcopy(config.DEFAULTS)
    game = tmp_path / "game"
    game.mkdir()
    (tmp_path / "profiles").mkdir()
    c["client"]["state_dir"] = str(tmp_path / "state")
    c["workspace"].update(game=str(game), profiles=str(tmp_path / "profiles"))
    c["components"]["kilo"]["path"] = sys.executable          # any existing file
    c["components"]["supervisor"]["enabled"] = False
    return c


def _kilo(monkeypatch, ok):
    """What the Kilo health probe says (the real one does HTTP on the port)."""
    import foundry_client.components as C
    monkeypatch.setattr(C, "kilo_health", lambda port: (ok, f"port {port}"))


class FakeProc:
    n = 0

    def __init__(self, argv, **kw):
        FakeProc.n += 1
        self.pid, self.argv, self.kw, self.code = 1000 + FakeProc.n, argv, kw, None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = -15

    def wait(self, t=None):
        return self.code

    def kill(self):
        self.code = -9


def test_validate_rejects_secret_like_env_and_missing_folders(cfg):
    assert config.validate(cfg) == []
    bad = copy.deepcopy(cfg)
    bad["components"]["kilo"]["env"]["FOUNDRY_API_KEY"] = "x"
    bad["workspace"]["game"] = ""
    errs = config.validate(bad)
    assert any("secret" in e for e in errs) and any("workspace.game" in e for e in errs)


def test_save_and_load_round_trip(cfg, tmp_path):
    config.save(cfg)
    back = config.load()
    assert back["workspace"]["game"] == cfg["workspace"]["game"]
    assert back["components"]["kilo"]["port"] == 4096


def test_manager_starts_in_order_waits_for_secrets_and_restarts_crashes(cfg, monkeypatch):
    monkeypatch.setenv("FOUNDRY_CLIENT_SECRET_KILO_SERVER_PASSWORD", "pw")
    launched = []

    def popen(argv, **kw):
        p = FakeProc(argv, **kw)
        launched.append(p)
        return p
    import foundry_client.components as C
    _kilo(monkeypatch, False)
    m = Manager(cfg, popen=popen)
    m.start_all()
    m.step()
    st = {c["name"]: c for c in m.status()["components"]}
    assert st["kilo"]["state"] == "starting"
    assert st["coordinator"]["state"] == "waiting"             # after: kilo
    _kilo(monkeypatch, True)   # once kilo answers ...
    m.step()
    co = m.status("coordinator")                               # ... a missing secret blocks it
    assert co["state"] == "error" and "FOUNDRY_COORDINATOR_KEY" in co["detail"]
    kilo = launched[0]
    assert kilo.argv[1:4] == ["serve", "--hostname", "127.0.0.1"]
    assert kilo.kw["env"]["KILO_SERVER_PASSWORD"] == "pw"
    assert "pw" not in m.tail("kilo")                          # secrets never logged
    _kilo(monkeypatch, False)
    kilo.code = 1                                               # crash -> backoff restart
    m.step()
    assert m.status("kilo")["state"] == "error" and m.procs["kilo"].next_try > 0
    m.procs["kilo"].next_try = 0
    m.step()
    assert len([p for p in launched if p.argv[1] == "serve"]) == 2


def test_external_kilo_is_adopted_not_duplicated(cfg, monkeypatch):
    import foundry_client.components as C
    _kilo(monkeypatch, True)
    launched = []
    m = Manager(cfg, popen=lambda argv, **kw: launched.append(argv) or FakeProc(argv))
    m.start("kilo")
    assert not launched and "outside the client" in m.status("kilo")["detail"]


def _db(tmp_path, task_state):
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(d / "coordinator.sqlite")
    c.execute("CREATE TABLE sessions (session_id, project_id, task_id, status)")
    c.execute("CREATE TABLE tasks (project_id, task_id, state)")
    c.execute("INSERT INTO sessions VALUES ('s1','p','T1','running')")
    c.execute("INSERT INTO tasks VALUES ('p','T1',?)", (task_state,))
    c.commit()
    c.close()
    return str(d)


def test_safe_point_ignores_idle_sessions_of_paused_tasks(tmp_path):
    assert safe_point(_db(tmp_path, "PAUSED"))[0] is True
    ok, why = safe_point(_db(tmp_path / "b", "IMPLEMENTING"))
    assert ok is False and "1 session" in why


def test_safe_restart_waits_while_a_session_works(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("FOUNDRY_CLIENT_SECRET_KILO_SERVER_PASSWORD", "pw")
    import foundry_client.components as C
    _kilo(monkeypatch, False)
    cfg["client"]["state_dir"] = _db(tmp_path, "IMPLEMENTING")
    m = Manager(cfg, popen=lambda argv, **kw: FakeProc(argv))
    m.start("kilo")
    r = m.restart("kilo")
    assert m.status("kilo")["pending_restart"] == "safe"       # state stays the real health state
    assert "1 session" in r["result"] and "safe point" in r["result"]
    m.restart("kilo", force=True)
    assert m.procs["kilo"].restarts == 1


def test_api_needs_token_and_local_host_and_never_returns_secrets(cfg, monkeypatch):
    monkeypatch.setenv("FOUNDRY_CLIENT_SECRET_FOUNDRY_KEY", "s3cret")
    m = Manager(cfg, popen=lambda argv, **kw: FakeProc(argv))
    app = api.create_app(m, "t" * 40)
    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.get("/api/status").status_code == 401
    h = {"X-Client-Token": "t" * 40}
    assert c.get("/api/status", headers=h).status_code == 200
    evil = TestClient(app, base_url="http://attacker.example")
    assert evil.get("/api/status", headers=h).status_code == 403
    r = c.get("/api/secrets", headers=h).json()
    assert r["secrets"]["FOUNDRY_KEY"] is True and "s3cret" not in str(r)
    assert c.get("/", headers={}).status_code == 200            # the page itself is public
    bad = copy.deepcopy(cfg)
    bad["client"]["backend_url"] = "ftp://x"
    assert c.put("/api/settings", headers=h, json={"settings": bad}).status_code == 400


def test_autostart_writes_and_removes_the_entry(cfg):
    p = autostart.enable()
    assert p.is_file() and "run" in p.read_text(encoding="utf-8")
    assert autostart.is_enabled()
    autostart.disable()
    assert not autostart.is_enabled()


def test_secret_status_lists_known_names(monkeypatch):
    monkeypatch.setenv("FOUNDRY_CLIENT_SECRET_KILO_SERVER_PASSWORD", "x")
    st = secrets.status([])
    assert st["KILO_SERVER_PASSWORD"] is True and "FOUNDRY_COORDINATOR_KEY" in st
