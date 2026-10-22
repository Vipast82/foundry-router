"""Foundry Client, workstream W5: retired media component, adopted processes, merge-style
settings, hung components, safe-point deadline, real health probes, client-requests, kilo.jsonc
apply, update check, Rojo session changes, doctor, systemd unit, router link."""
import copy
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from foundry_client import api, autostart, config, doctor, secrets
from foundry_client import components as C
from foundry_client import manager as M
from foundry_client.link import RouterLink
from foundry_client.manager import Manager
from foundry_client.update import UpdateCheck, parse_version


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FOUNDRY_CLIENT_CONFIG", str(tmp_path / "client.toml"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(secrets, "_kr", lambda: None)
    for k in ("KILO_SERVER_PASSWORD", "FOUNDRY_COORDINATOR_KEY"):
        monkeypatch.setenv(f"FOUNDRY_CLIENT_SECRET_{k}", "x")
    c = copy.deepcopy(config.DEFAULTS)
    game = tmp_path / "game"
    game.mkdir()
    (tmp_path / "profiles").mkdir()
    c["client"]["state_dir"] = str(tmp_path / "state")
    c["workspace"].update(game=str(game), profiles=str(tmp_path / "profiles"))
    c["components"]["kilo"]["path"] = sys.executable
    c["components"]["supervisor"]["enabled"] = False
    c["components"]["coordinator"]["enabled"] = False
    return c


class FakeProc:
    n = 0

    def __init__(self, argv, **kw):
        FakeProc.n += 1
        self.pid, self.argv, self.kw, self.code = 5000 + FakeProc.n, argv, kw, None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = -15

    def wait(self, t=None):
        return self.code

    def kill(self):
        self.code = -9


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def killed(monkeypatch):
    """Record kills instead of running taskkill; a killed FakeProc ends."""
    log = []

    def kill_pid(pid, p, *a):
        log.append(pid)
        if p is not None:
            p.code = -9
    monkeypatch.setattr(Manager, "_kill_pid", staticmethod(kill_pid))
    return log


def _health(monkeypatch, fn):
    monkeypatch.setattr(C, "kilo_health", fn)


def mk(cfg, clock=None, **kw):
    return Manager(cfg, popen=lambda argv, **k: FakeProc(argv, **k), clock=clock or Clock(), **kw)


# --------------------------------------------------------------- media removal and migration

def test_media_is_not_a_component_any_more():
    assert "media" not in config.COMPONENTS and "media" not in config.DEFAULTS["components"]


def test_load_drops_the_media_table_quietly_and_keeps_a_copy(tmp_path, monkeypatch):
    p = tmp_path / "client.toml"
    p.write_text('[workspace]\ngame = "G"\n\n[components.media]\nenabled = true\n'
                 'command = ["py", "x.py"]\n\n[components.kilo]\nport = 4100\n', encoding="utf-8")
    c = config.load(p)
    assert "media" not in c["components"] and c["workspace"]["game"] == "G"
    assert c["components"]["kilo"]["port"] == 4100
    assert "media" not in p.read_text(encoding="utf-8")          # rewritten without it
    assert "[components.media]" in (tmp_path / "client.toml.before-migration").read_text(
        encoding="utf-8")
    assert config.load(p)["workspace"]["game"] == "G"             # and stable on the next load


def test_old_settings_page_posting_media_is_accepted(cfg):
    m = mk(cfg)
    c = TestClient(api.create_app(m, "t" * 40), base_url="http://127.0.0.1")
    body = copy.deepcopy(cfg)
    body["components"]["media"] = {"enabled": True, "command": []}
    r = c.put("/api/settings", headers={"X-Client-Token": "t" * 40}, json={"settings": body})
    assert r.status_code == 200 and "media" not in m.cfg["components"]


# --------------------------------------------------------------- settings merge

def test_put_settings_merges_onto_current_settings_not_defaults(cfg):
    cfg["workspace"]["game"] = cfg["workspace"]["game"]
    m = mk(cfg)
    config.save(cfg)
    c = TestClient(api.create_app(m, "t" * 40), base_url="http://127.0.0.1")
    h = {"X-Client-Token": "t" * 40}
    cfg_kilo_port = 4242
    m.cfg["components"]["kilo"]["port"] = cfg_kilo_port
    r = c.put("/api/settings", headers=h, json={"settings": {"components": {"kilo": {
        "enabled": False}}}})
    assert r.status_code == 200
    assert m.cfg["workspace"]["game"] == cfg["workspace"]["game"]       # not reset to ""
    assert m.cfg["components"]["kilo"]["port"] == cfg_kilo_port
    assert m.cfg["components"]["kilo"]["enabled"] is False
    assert config.load()["workspace"]["profiles"] == cfg["workspace"]["profiles"]


# --------------------------------------------------------------- stale detail

def test_disabled_or_stopped_component_does_not_keep_an_old_error(cfg, monkeypatch, killed):
    _health(monkeypatch, lambda port: (False, "closed"))
    m = mk(cfg)
    m.start("kilo")
    m.procs["kilo"].p.code = 0                                   # exits "with code 0"
    m.step()
    assert "exited with code 0" in m.status("kilo")["detail"]
    m.stop("kilo")
    st = m.status("kilo")
    assert st["state"] == "stopped" and "exited" not in st["detail"] and st["last_exit"] == ""
    cfg["components"]["kilo"]["enabled"] = False
    m.reload(cfg)
    m.step()
    assert m.status("kilo")["detail"] == "disabled"


# --------------------------------------------------------------- adopted processes

def test_adopted_process_is_stopped_by_pid_and_restart_is_truthful(cfg, monkeypatch, killed):
    alive = {"pid": 777}
    state = {"up": True}
    _health(monkeypatch, lambda port: (state["up"], f"port {port}"))
    monkeypatch.setattr(M, "pid_alive", lambda pid: alive["pid"] == pid)
    m = mk(cfg)
    m.procs["kilo"].spec.owner_pid = lambda: alive["pid"]
    m.start("kilo")
    assert m.procs["kilo"].adopted and "outside the client" in m.status("kilo")["detail"]

    def die(pid, p, *a):
        killed.append(pid)
        alive["pid"] = None
        state["up"] = False
    monkeypatch.setattr(Manager, "_kill_pid", staticmethod(die))
    st = m.restart("kilo", force=True)
    assert killed == [777]
    assert st["restarts"] == 1 and st["result"] == "restarted"
    assert m.procs["kilo"].p is not None and not m.procs["kilo"].adopted   # now ours


def test_restart_of_an_adopted_process_that_will_not_die_reports_failure(cfg, monkeypatch):
    _health(monkeypatch, lambda port: (True, "up"))
    monkeypatch.setattr(M, "pid_alive", lambda pid: True)
    monkeypatch.setattr(M, "STOP_WAIT", 0.3)
    monkeypatch.setattr(Manager, "_kill_pid", staticmethod(lambda pid, p, *a: None))
    m = mk(cfg)
    m.procs["kilo"].spec.owner_pid = lambda: 888
    m.start("kilo")
    st = m.restart("kilo", force=True)
    assert st["restarts"] == 0                                   # nothing was restarted
    assert st["result"].startswith("failed") and "888" in st["result"]


def test_stop_kills_an_adopted_process(cfg, monkeypatch, killed):
    up = {"v": True}
    _health(monkeypatch, lambda port: (up["v"], "up"))
    gone = {"v": False}
    monkeypatch.setattr(M, "pid_alive", lambda pid: not gone["v"])
    m = mk(cfg)
    m.procs["kilo"].spec.owner_pid = lambda: 9
    m.start("kilo")
    monkeypatch.setattr(Manager, "_kill_pid", staticmethod(lambda pid, p, *a: gone.update(v=True)))
    st = m.stop("kilo")
    assert st["state"] == "stopped" and not m.procs["kilo"].adopted


def test_client_exit_leaves_external_processes_running(cfg, monkeypatch, killed):
    _health(monkeypatch, lambda port: (True, "up"))
    m = mk(cfg)
    m.procs["kilo"].spec.owner_pid = lambda: 9
    m.start("kilo")
    m.shutdown(stop_components=True)
    assert killed == []


# --------------------------------------------------------------- hung components, safe-point deadline

def test_component_unhealthy_for_too_long_is_restarted(cfg, monkeypatch, killed):
    clk = Clock()
    ok = {"v": False}                                            # not up yet: the client launches it
    _health(monkeypatch, lambda port: (ok["v"], "up" if ok["v"] else "port open, /config dead"))
    monkeypatch.setattr(M, "safe_point", lambda d, g="": (True, "safe"))
    cfg["client"]["unhealthy_restart_seconds"] = 120
    m = mk(cfg, clk)
    m.start("kilo")
    first = m.procs["kilo"].p
    assert first is not None
    ok["v"] = True
    clk.t += 200
    m.step()
    assert m.status("kilo")["state"] == "running"
    ok["v"] = False
    m.step()
    assert m.status("kilo")["state"] == "unhealthy"
    clk.t += 100
    m.step()
    assert m.procs["kilo"].p is first                            # not yet: 100 s < 120 s
    clk.t += 60
    m.step()                                                     # 160 s unhealthy -> queued
    m.step()
    assert m.procs["kilo"].restarts == 1 and m.procs["kilo"].p is not first
    assert any("unhealthy for" in e["text"] for e in m.events)


def test_unhealthy_restart_can_be_switched_off(cfg, monkeypatch, killed):
    clk = Clock()
    _health(monkeypatch, lambda port: (False, "dead"))
    cfg["client"]["unhealthy_restart_seconds"] = 0
    m = mk(cfg, clk)
    m.start("kilo")
    assert m.procs["kilo"].p is not None
    clk.t += 10_000
    m.step()
    clk.t += 10_000
    m.step()
    m.step()
    assert m.procs["kilo"].restarts == 0


def test_pending_safe_restart_keeps_health_and_warns_after_the_deadline(cfg, monkeypatch, killed):
    clk = Clock()
    _health(monkeypatch, lambda port: (True, "up"))
    monkeypatch.setattr(M, "safe_point", lambda d, g="": (False, "2 session(s) working"))
    cfg["client"]["safe_restart_max_wait_minutes"] = 60
    m = mk(cfg, clk)
    m.start("kilo")
    r = m.restart("kilo")
    assert r["state"] in ("starting", "running") and "2 session(s)" in r["result"]
    clk.t += 59 * 60
    m.step()
    assert not m.alerts
    clk.t += 2 * 60
    m.step()
    assert "restart_wait:kilo" in m.alerts and "61 min" in m.alerts["restart_wait:kilo"]["text"]
    assert m.procs["kilo"].pending_restart == "safe"             # still waiting: never forced
    m.cancel_restart("kilo")
    assert not m.alerts and m.status("kilo")["pending_restart"] == ""


# --------------------------------------------------------------- real health probes

class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def do_GET(self):
        _Handler.seen.append((self.path, self.headers.get("Authorization")))
        code, body = _Handler.routes.get(self.path, (404, {}))
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *a):
        pass


_REAL_PORT_OPEN = C.port_open            # captured before the autouse fixture stubs it


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setattr(C, "port_open", _REAL_PORT_OPEN)
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    _Handler.routes, _Handler.seen = {}, []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def test_kilo_health_uses_config_with_the_password(server, monkeypatch):
    port = server.server_address[1]
    monkeypatch.setattr(C, "_pw_cache", {"at": 1e18, "v": "pw"})
    _Handler.routes = {"/config": (200, {})}
    ok, why = C.kilo_health(port)
    assert ok and "/config" in why
    path, auth = _Handler.seen[-1]
    assert path == "/config" and auth.startswith("Basic ")
    _Handler.routes = {"/config": (401, {})}
    ok, why = C.kilo_health(port)
    assert not ok and "password" in why
    _Handler.routes = {"/config": (500, {})}
    ok, why = C.kilo_health(port)
    assert not ok and "HTTP 500" in why
    server.shutdown()
    server.server_close()
    ok, why = C.kilo_health(port)
    assert not ok and "closed" in why


def test_rojo_health_reads_api_rojo(server):
    port = server.server_address[1]
    _Handler.routes = {"/api/rojo": (200, {"sessionId": "abcdef12-0000", "projectName": "AU",
                                           "serverVersion": "7.4"})}
    ok, why, info = C.rojo_health(port)
    assert ok and info["sessionId"] == "abcdef12-0000" and "AU" in why
    _Handler.routes = {}
    ok, why, *_ = C.rojo_health(port)
    assert not ok and "HTTP 404" in why


def test_rojo_session_change_raises_a_reconnect_alert(cfg, monkeypatch, killed):
    cfg["components"]["rojo"].update(enabled=True, path=sys.executable)
    sid = {"v": "aaaa1111"}
    monkeypatch.setattr(C, "rojo_health", lambda port: (True, "up", {"sessionId": sid["v"]}))
    m = mk(cfg)
    m.start("rojo")
    m.step()
    assert not m.alerts                                          # first sight is not a change
    m.step()
    assert not m.alerts
    sid["v"] = "bbbb2222"
    m.step()
    a = m.status()["alerts"]
    assert len(a) == 1 and a[0]["key"] == "rojo_reconnect" and "plugin" in a[0]["text"].lower()
    c = TestClient(api.create_app(m, "t" * 40), base_url="http://127.0.0.1")
    r = c.post("/api/alerts/rojo_reconnect/dismiss", headers={"X-Client-Token": "t" * 40})
    assert r.json() == {"dismissed": True} and not m.alerts


# --------------------------------------------------------------- client-requests

def _req(m, name, body):
    d = m.requests.root()
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(body), encoding="utf-8")


def _result(m, name):
    return json.loads((m.requests.root() / "results" / name).read_text(encoding="utf-8"))


def test_reload_kilo_request_waits_for_a_safe_point_then_reports(cfg, monkeypatch, killed):
    _health(monkeypatch, lambda port: (True, "up"))
    safe = {"v": (False, "1 session(s) working")}
    monkeypatch.setattr(M, "safe_point", lambda d, g="": safe["v"])
    m = mk(cfg)
    m.start("kilo")
    _req(m, "r1.json", {"action": "reload_kilo", "id": "x1"})
    assert m.requests.poll() == 1
    res = _result(m, "r1.json")
    assert res["status"] == "pending" and "1 session(s)" in res["message"]
    assert (m.requests.root() / "processing" / "r1.json").is_file()
    safe["v"] = (True, "safe")
    m.step()
    m.requests.poll()
    res = _result(m, "r1.json")
    assert res["status"] == "done" and res["id"] == "x1" and m.procs["kilo"].restarts == 1
    assert (m.requests.root() / "done" / "r1.json").is_file()
    assert not (m.requests.root() / "processing" / "r1.json").exists()


def test_other_request_kinds_and_bad_ones(cfg, monkeypatch, killed):
    cfg["components"]["rojo"].update(enabled=True, path=sys.executable)
    cfg["components"]["coordinator"]["enabled"] = True
    monkeypatch.setattr(C, "rojo_health", lambda port: (True, "up", {"sessionId": "s"}))
    monkeypatch.setattr(M, "safe_point", lambda d, g="": (True, "safe"))
    m = mk(cfg)
    m.start("rojo")
    _req(m, "a.json", {"kind": "restart-rojo"})
    _req(m, "b.json", {"action": "format_disk"})
    cfg2 = copy.deepcopy(cfg)
    cfg2["components"]["kilo"]["enabled"] = False
    m.reload(cfg2)
    _req(m, "c.json", {"action": "reload_kilo"})
    (m.requests.root() / "d.json").write_text("not json at all", encoding="utf-8")
    m.requests.poll()
    assert _result(m, "a.json")["status"] == "done" and "Reconnect the Rojo plugin" in \
        _result(m, "a.json")["message"]
    assert _result(m, "b.json")["status"] == "failed" and "unknown action" in \
        _result(m, "b.json")["message"]
    assert _result(m, "c.json")["status"] == "failed" and "disabled" in _result(m, "c.json")["message"]
    assert _result(m, "d.json")["status"] == "failed"
    assert m.procs["rojo"].restarts == 1


# --------------------------------------------------------------- kilo.jsonc apply

KILO_JSONC = '// my config\n{\n  "agent": {"reviewer": {"permission": {"edit": "deny"}}},\n}\n'


def _gen_ok(**over):
    out = {"path": "p", "changed": ["reviewer"], "unchanged": [], "missing": [], "applied": True,
           "backup": "", **over}

    class R:
        returncode, stdout, stderr = 0, "log line\n" + json.dumps(out, indent=2), ""
    return R


def test_apply_kilo_config_runs_the_generator_then_reloads_kilo(cfg, tmp_path, monkeypatch, killed):
    (tmp_path / "game" / "kilo.jsonc").write_text(KILO_JSONC, encoding="utf-8")
    proj = tmp_path / "profiles" / "au"
    proj.mkdir()
    (proj / "project.toml").write_text('id = "animaluprising"\n', encoding="utf-8")
    _health(monkeypatch, lambda port: (True, "up"))
    monkeypatch.setattr(M, "safe_point", lambda d, g="": (True, "safe"))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return _gen_ok(applied="--apply" in argv)
    m = mk(cfg, run=run)
    m.start("kilo")
    res = m.apply_kilo_config()
    assert res["ok"] and res["applied"] and res["changed"] == ["reviewer"]
    argv = calls[0]
    assert argv[argv.index("coordinator"):][:1] == ["coordinator"]
    assert "kilo-config" in argv and "animaluprising" in argv and "--apply" in argv
    assert argv[argv.index("--path") + 1].endswith("kilo.jsonc")
    assert "--profiles" in argv and "--state" in argv
    assert res["reload"] == "restarted" and m.procs["kilo"].restarts == 1
    dry = m.apply_kilo_config(dry_run=True)
    assert "--apply" not in calls[1] and "reload" not in dry


def test_apply_kilo_config_refuses_a_broken_file_and_ambiguous_projects(cfg, tmp_path):
    called = []
    m = mk(cfg, run=lambda *a, **k: called.append(1))
    assert "not found" in m.apply_kilo_config()["error"]
    (tmp_path / "game" / "kilo.jsonc").write_text('{"agent": ', encoding="utf-8")
    r = m.apply_kilo_config("p")
    assert not r["ok"] and "not valid JSONC" in r["error"] and not called
    (tmp_path / "game" / "kilo.jsonc").write_text(KILO_JSONC, encoding="utf-8")
    for n in ("a", "b"):
        (tmp_path / "profiles" / n).mkdir()
        (tmp_path / "profiles" / n / "project.toml").write_text(f'id = "{n}"\n', encoding="utf-8")
    r = m.apply_kilo_config()
    assert not r["ok"] and "which project" in r["error"] and not called


def test_apply_kilo_config_surfaces_a_generator_failure_and_restores_a_bad_write(
        cfg, tmp_path, monkeypatch):
    kj = tmp_path / "game" / "kilo.jsonc"
    kj.write_text(KILO_JSONC, encoding="utf-8")

    class Fail:
        returncode, stdout, stderr = 2, "", "error: no role rules"
    m = mk(cfg, run=lambda *a, **k: Fail)
    r = m.apply_kilo_config("p")
    assert not r["ok"] and "no role rules" in r["error"]
    bak = tmp_path / "kilo.jsonc.bak"
    bak.write_text(KILO_JSONC, encoding="utf-8")

    def corrupting(argv, **kw):
        kj.write_text("{ broken", encoding="utf-8")
        return _gen_ok(backup=str(bak))
    m2 = mk(cfg, run=corrupting)
    r = m2.apply_kilo_config("p")
    assert not r["ok"] and "backup was restored" in r["error"]
    assert kj.read_text(encoding="utf-8") == KILO_JSONC


def test_apply_kilo_config_request_file(cfg, tmp_path, monkeypatch, killed):
    (tmp_path / "game" / "kilo.jsonc").write_text(KILO_JSONC, encoding="utf-8")
    m = mk(cfg, run=lambda *a, **k: _gen_ok(changed=[], applied=False))
    _req(m, "k.json", {"action": "apply_kilo_config", "project": "p"})
    m.requests.poll()
    for _ in range(50):
        m.requests.poll()
        if (m.requests.root() / "done" / "k.json").exists():
            break
        threading.Event().wait(0.05)
    assert _result(m, "k.json")["status"] == "done"
    assert "up to date" in _result(m, "k.json")["message"]


# --------------------------------------------------------------- update check and router link

def test_update_check_compares_with_the_latest_release():
    seen = []

    def get(url, headers=None, timeout=0):
        seen.append(url)
        return 200, {"tag_name": "v0.140.1", "html_url": "https://github.com/x/y/releases/tag/v0.140.1",
                     "name": "Client 0.140.1", "published_at": "2026-11-01T00:00:00Z"}, ""
    u = UpdateCheck(lambda: "Vipast82/foundry-router", get=get, current="0.133.2")
    s = u.poll()
    assert s["available"] and s["latest"] == "0.140.1" and s["url"].endswith("v0.140.1")
    assert seen == ["https://api.github.com/repos/Vipast82/foundry-router/releases/latest"]
    assert not UpdateCheck(lambda: "a/b", get=get, current="0.140.1").poll()["available"]
    assert not UpdateCheck(lambda: "a/b", get=get, current="1.0.0").poll()["available"]
    assert parse_version("v0.9.0") < parse_version("0.10.0") < parse_version("1.0")
    err = UpdateCheck(lambda: "a/b", get=lambda *a, **k: (0, None, "offline"), current="0.1").poll()
    assert err["error"] == "offline" and not err["available"]
    none = UpdateCheck(lambda: "a/b", get=lambda *a, **k: (404, None, "HTTP 404"), current="0.1").poll()
    assert none["error"] == "" and not none["available"]


def test_router_link_reports_reachability_version_and_warmup():
    answers = {"/health": (200, {"status": "ok", "version": "0.140.0", "warming_up": True}, "")}

    def get(url, headers=None, timeout=0):
        return answers.get(url[url.index("/", 8):], (0, None, "connection refused"))
    link = RouterLink(lambda: "http://r:11435/", get=get)
    s = link.poll()
    assert s["reachable"] and s["version"] == "0.140.0" and s["warming_up"]
    answers.clear()
    answers["/api/version"] = (200, {"version": "0.9.0", "foundry_router": "0.130.0"}, "")
    assert link.poll()["version"] == "0.130.0"                   # an older router: no /health
    answers.clear()
    s = link.poll()
    assert not s["reachable"] and "refused" in s["error"]
    snap = link.snapshot()
    assert snap["url"] == "http://r:11435/" and snap["client_version"]


def test_status_and_observation_carry_router_update_and_alerts(cfg):
    m = mk(cfg)
    m.link.get = lambda *a, **k: (200, {"version": "1.2.3"}, "")
    m.link.poll()
    m._alert("x", "something to do")
    st = m.status()
    assert st["router"]["reachable"] and st["alerts"][0]["key"] == "x" and "available" in st["update"]
    ob = m.observation()
    assert ob["router"]["version"] == "1.2.3" and ob["components"][0]["name"] == "kilo"
    assert "pid" not in ob["components"][0] and "secrets" not in json.dumps(ob)


# --------------------------------------------------------------- doctor

def test_doctor_checks_kilo_config_and_media_adapter_entry(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "kilo_health", lambda port: (True, "ok"))
    monkeypatch.setattr(doctor, "kilo_health", lambda port: (True, "ok"))
    monkeypatch.setattr(doctor, "_version", lambda exe: "")
    monkeypatch.delenv("FOUNDRY_KEY", raising=False)
    m = mk(cfg)
    m.link.get = lambda *a, **k: (200, {"version": "1"}, "")
    by = lambda: {c["id"]: c for c in doctor.run(m)["checks"]}          # noqa: E731
    kj = tmp_path / "game" / "kilo.jsonc"

    kj.write_text('{"agent": {}}', encoding="utf-8")
    d = by()
    assert d["kilo_config"]["status"] == "ok" and d["media_adapter"]["status"] == "info"

    adapter = tmp_path / "foundry_media_mcp.py"
    adapter.write_text("print(1)", encoding="utf-8")
    kj.write_text(json.dumps({"mcp": {"foundry-media": {"type": "local", "command":
                                                        [sys.executable, str(adapter)]}}}),
                  encoding="utf-8")
    d = by()
    assert d["media_adapter"]["status"] == "fail" and "FOUNDRY_KEY" in d["media_adapter"]["detail"]

    kj.write_text(json.dumps({"mcp": {"foundry-media": {
        "type": "local", "command": [sys.executable, str(adapter)],
        "environment": {"FOUNDRY_KEY": "k"}}}}), encoding="utf-8")
    assert by()["media_adapter"]["status"] == "ok"

    kj.write_text(json.dumps({"mcp": {"foundry-media": {
        "type": "local", "command": [sys.executable, str(tmp_path / "missing_media.py")],
        "environment": {"FOUNDRY_KEY": "k"}}}}), encoding="utf-8")
    assert "does not exist" in by()["media_adapter"]["detail"]

    kj.write_text('{"mcp": ', encoding="utf-8")
    d = by()
    assert d["kilo_config"]["status"] == "fail" and "media_adapter" not in d
    assert doctor.run(m)["ok"] is False


def test_doctor_covers_binaries_secrets_ports_router_and_rojo(cfg, monkeypatch):
    cfg["components"]["rojo"].update(enabled=True, path="")
    monkeypatch.setattr(C, "find_binary", lambda *a, **k: "")
    monkeypatch.setattr(doctor, "kilo_health", lambda port: (False, "port closed"))
    monkeypatch.setattr(doctor, "_version", lambda exe: "")
    monkeypatch.delenv("FOUNDRY_CLIENT_SECRET_KILO_SERVER_PASSWORD")
    m = mk(cfg)
    m.link.get = lambda *a, **k: (0, None, "refused")
    d = {c["id"]: c for c in doctor.run(m)["checks"]}
    assert d["rojo_bin"]["status"] == "fail" and d["secrets"]["status"] == "fail"
    assert "KILO_SERVER_PASSWORD" in d["secrets"]["detail"]
    assert d["router"]["status"] == "fail" and d["kilo_api"]["status"] == "fail"
    assert d["rojo_plugin"]["status"] == "warn" and d["port_api"]["status"] == "ok"


def test_doctor_api_route(cfg, monkeypatch):
    monkeypatch.setattr(doctor, "kilo_health", lambda port: (True, "ok"))
    monkeypatch.setattr(doctor, "_version", lambda exe: "")
    m = mk(cfg)
    m.link.get = lambda *a, **k: (200, {"version": "1"}, "")
    c = TestClient(api.create_app(m, "t" * 40), base_url="http://127.0.0.1")
    r = c.get("/api/doctor", headers={"X-Client-Token": "t" * 40}).json()
    assert {x["id"] for x in r["checks"]} >= {"kilo_bin", "secrets", "router", "kilo_config"}


# --------------------------------------------------------------- systemd unit, AppImage

def test_systemd_unit_and_install_service(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(autostart, "sys", type("S", (), {"platform": "linux", "frozen": False,
                                                          "executable": "/usr/bin/python3"})())
    text = autostart.service_text()
    assert "ExecStart=" in text and "run --headless" in text and "WantedBy=default.target" in text
    assert "Restart=on-failure" in text
    p = autostart.install_service(activate=False)
    assert p == tmp_path / "xdg" / "systemd" / "user" / "foundry-client.service" and p.is_file()
    autostart.remove_service()
    assert not p.exists()


def test_install_service_refuses_on_windows(monkeypatch):
    monkeypatch.setattr(autostart, "sys", type("S", (), {"platform": "win32"})())
    with pytest.raises(RuntimeError):
        autostart.install_service()


def test_appimage_autostart_uses_the_appimage_path_not_the_temp_mount(tmp_path, monkeypatch):
    img = tmp_path / "FoundryClient.AppImage"
    img.write_text("x")
    monkeypatch.setenv("APPIMAGE", str(img))
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    line = autostart._launch_line()
    assert line == f"{img} run" and "/tmp/.mount_" not in line
    assert autostart._root() == ""
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "gone.AppImage"))      # stale variable
    assert autostart._launch_argv() == autostart.self_argv()


def test_apply_kilo_config_with_the_real_coordinator_generator(cfg, tmp_path):
    """No stand-in: the bundled `foundry-client coordinator kilo-config` runs for real."""
    import tomli_w
    from foundry_automation import jsonc
    from foundry_automation import role_rules as RR
    from tests.coordinator.helpers import write_profile
    ws = tmp_path / "ws"
    ws.mkdir()
    prof = write_profile(tmp_path, "game", ws, extra=tomli_w.dumps({"role_rules": RR.all_defaults()}))
    prof.write_text(prof.read_text(encoding="utf-8").replace(
        'kind = "fake"', 'kind = "kilo"\nurl = "http://127.0.0.1:1"'), encoding="utf-8")
    kj = ws / "kilo.jsonc"
    kj.write_text('{\n // keep me\n "agent": {"code": {"permission": {"task": {"*": "deny"}}}}\n}\n',
                  encoding="utf-8")
    cfg["workspace"].update(game=str(ws), profiles=str(tmp_path / "profiles"))
    m = Manager(cfg, popen=lambda argv, **k: FakeProc(argv, **k))
    res = m.apply_kilo_config(reload=False)
    assert res["ok"] and res["applied"] and res["changed"] == ["code"], res
    assert "// keep me" in kj.read_text(encoding="utf-8")
    assert jsonc.parse(kj.read_text(encoding="utf-8"))["agent"]["code"]["permission"]["*"]
    assert (tmp_path / "ws").glob("kilo.jsonc.bak-*")
    again = m.apply_kilo_config(reload=False)
    assert again["ok"] and again["changed"] == [] and not again["applied"]


def test_a_wrong_password_on_an_open_port_is_reported_not_launched_over(cfg, monkeypatch):
    _health(monkeypatch, lambda port: (False, "port 4096 is open but rejects the Kilo server "
                                              "password (HTTP 401)"))
    monkeypatch.setattr(C, "port_open", lambda port, **kw: True)
    launched = []
    m = Manager(cfg, popen=lambda argv, **k: launched.append(argv) or FakeProc(argv))
    m.start("kilo")
    st = m.status("kilo")
    assert not launched and st["state"] == "error"
    assert "password" in st["detail"] and "another process" in st["detail"]
