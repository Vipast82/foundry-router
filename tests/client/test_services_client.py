"""Foundry Client services (Team Lead 5): MCP probing on a fixture kilo.jsonc, the services
report, the optional managed service components (off by default), the heartbeat carrying
MCP + services, client_service_enable through the relay, and start_service from the
coordinator's client-requests."""
import copy
import json
import sys

import pytest

from foundry_automation import service_map as SM
from foundry_client import components as C
from foundry_client import config
from foundry_client import relay as R
from foundry_client.client_requests import RequestQueue
from foundry_client.manager import Manager

from .test_relay import FakeRouter


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    from foundry_client import secrets
    monkeypatch.setenv("FOUNDRY_CLIENT_CONFIG", str(tmp_path / "client.toml"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(secrets, "_kr", lambda: None)
    c = copy.deepcopy(config.DEFAULTS)
    game = tmp_path / "game"
    game.mkdir()
    (tmp_path / "profiles").mkdir()
    c["client"]["state_dir"] = str(tmp_path / "state")
    c["workspace"].update(game=str(game), profiles=str(tmp_path / "profiles"))
    c["components"]["kilo"]["path"] = sys.executable
    c["components"]["supervisor"]["enabled"] = False
    c["components"]["coordinator"]["enabled"] = False
    c["components"]["kilo"]["enabled"] = False
    return c


def write_kilo(cfg, tmp_path):
    exe = tmp_path / "tool.exe"
    exe.write_text("")
    proj = {"mcp": {
        "local-ok": {"type": "local", "command": [str(exe), "--x"], "enabled": True},
        "local-gone": {"type": "local", "command": [str(tmp_path / "nope.exe")], "enabled": True},
        "wrapped": {"type": "local", "command": ["cmd.exe", "/c", str(exe)], "enabled": False},
        "wrapped-gone": {"type": "local", "command": ["cmd.exe", "/c", str(tmp_path / "x.bat")]},
        "remote-up": {"type": "remote", "url": "http://10.0.0.5:8937/sse", "enabled": False},
        "remote-down": {"type": "remote", "url": "https://example.invalid/mcp"},
        "stub": {"enabled": False},
    }}
    p = tmp_path / "kilo.jsonc"
    p.write_text("// project\n" + json.dumps(proj), encoding="utf-8")
    cfg["workspace"]["kilo_config"] = str(p)
    return p


def test_mcp_report_probes_each_server_cheaply(cfg, tmp_path, monkeypatch):
    write_kilo(cfg, tmp_path)
    rows = {r["name"]: r for r in C.mcp_report(cfg, global_path="",
                                               port_fn=lambda h, p, *a: (h, p) == ("10.0.0.5", 8937))}
    assert rows["local-ok"]["available"] is True and rows["local-ok"]["enabled"] is True
    assert rows["local-gone"]["available"] is False and "not found" in rows["local-gone"]["detail"]
    assert rows["wrapped"]["available"] is True and rows["wrapped"]["enabled"] is False
    assert rows["wrapped-gone"]["available"] is False
    assert rows["remote-up"]["available"] is True and rows["remote-up"]["target"] == "10.0.0.5:8937"
    assert rows["remote-down"]["available"] is False and "443" in rows["remote-down"]["detail"]
    assert rows["stub"]["available"] is None and rows["stub"]["type"] == "stub"
    assert set(rows) == {"local-ok", "local-gone", "wrapped", "wrapped-gone", "remote-up",
                         "remote-down", "stub"}


def test_a_stub_is_resolved_from_the_global_config(cfg, tmp_path):
    write_kilo(cfg, tmp_path)
    g = tmp_path / "global.jsonc"
    g.write_text('{"mcp": {"stub": {"type": "remote", "url": "http://10.0.0.9:1/sse"}}}')
    row = {r["name"]: r for r in C.mcp_report(cfg, global_path=str(g),
                                              port_fn=lambda *a: True)}["stub"]
    assert row["available"] is True and row["enabled"] is False and row["target"] == "10.0.0.9:1"


def test_no_kilo_file_is_an_empty_report_not_an_error(cfg):
    cfg["workspace"]["kilo_config"] = ""
    assert C.mcp_report(cfg, global_path="") == []


def test_services_report_marks_client_provided_services(cfg, tmp_path):
    rows = [{"name": "robloxstudio-mcp", "available": True, "detail": "npx.cmd found"},
            {"name": "kokoro-tts", "available": False, "detail": "host:1 is not reachable"}]
    comp = {"rojo": {"state": "running", "enabled": True}, "kilo": {"state": "stopped", "enabled": False}}
    out = {r["name"]: r for r in C.services_report(cfg, rows, comp, port_fn=lambda h, p, *a: p == 34872,
                                                   http_fn=lambda url: "")}
    assert out["rojo"]["status"] == "up" and out["rojo"]["provider"] == "client"
    assert out["rojo"]["enabled"] is True and out["rojo"]["component_state"] == "running"
    assert out["kilo"]["status"] == "down" and out["kilo"]["enabled"] is False
    assert out["robloxstudio-mcp"]["status"] == "up" and out["robloxstudio-mcp"]["provider"] == "external"
    assert out["kokoro-tts"]["status"] == "down" and out["kokoro-tts"]["needed_by"] == ["audio"]
    assert out["local-model"]["provider"] == "backend" and out["local-model"]["status"] == "up"
    assert out["blender-mcp"]["status"] == "unknown"             # nothing defines it here
    assert out["blender-mcp"]["provider"] == "external"          # no `blender` component in this comp map


def test_optional_service_components_exist_but_are_off(cfg):
    for name in config.SERVICE_COMPONENTS:
        assert cfg["components"][name]["enabled"] is False
    assert config.validate(cfg) == []
    specs = C.build(cfg)
    assert set(config.SERVICE_COMPONENTS) <= set(specs)
    assert "set the program path" in specs["blender"].problem
    assert specs["blender"].health is None                       # its socket opens only after "Connect"
    cfg["components"]["blender"]["enabled"] = True
    assert any("blender.path is required" in e for e in config.validate(cfg))
    cfg["components"]["kokoro-tts"].update(path=sys.executable, port=8934, args=["--serve"])
    s = C.build(cfg)["kokoro-tts"]
    assert s.argv == [sys.executable, "--serve"] and s.port == 8934 and s.problem == ""
    assert s.health is not None


def mk(cfg):
    return Manager(cfg, popen=lambda argv, **kw: type("P", (), {
        "pid": 77, "poll": lambda self: None, "terminate": lambda self: None,
        "wait": lambda self, t=None: 0, "kill": lambda self: None})())


def test_manager_mcp_status_is_cached_and_survives_a_failing_probe(cfg):
    m = mk(cfg)
    calls = []
    m.clock = lambda: 100.0

    def probe(c):
        calls.append(1)
        return [{"name": "x", "type": "remote", "enabled": True, "available": True, "detail": "d",
                 "target": "h:1"}]
    st = m.mcp_status(probe=probe)
    assert st["mcp"][0]["name"] == "x" and {s["name"] for s in st["services"]} >= {"rojo", "kilo"}
    m.mcp_status(probe=probe)
    assert len(calls) == 1                                       # cached 30 s
    m.clock = lambda: 200.0
    m._mcp_cache = None

    def boom(c):
        raise RuntimeError("no")
    st = m.mcp_status(probe=boom)
    assert st["mcp"] == [] and st["services"]
    assert any("MCP probe failed" in e["text"] for e in m.events)


def test_heartbeat_carries_mcp_and_services(cfg):
    m = mk(cfg)
    m.mcp_status = lambda *a, **k: {"mcp": [{"name": "x", "type": "local", "enabled": True,
                                             "available": False, "detail": "x not found",
                                             "target": "x", "secret": "dropped"}],
                                    "services": [{"name": "rojo", "status": "up"}], "ts": 1}
    rt = FakeRouter()
    r = R.Relay(m, cfg, "k", source_id="client:t", post=rt, clock=lambda: 1.0, log=lambda s: None)
    body = r.beat_payload()
    assert body["mcp"] == [{"name": "x", "type": "local", "enabled": True, "available": False,
                            "detail": "x not found", "target": "x"}]
    assert body["services"] == [{"name": "rojo", "status": "up"}]
    assert "client_service_enable" in body["source"]["capabilities"]["actions"]


# ----------------------------------------------------------------------------- relay verbs

def relay_with(cfg, actions, saved):
    m = mk(cfg)
    rt = FakeRouter(actions)
    r = R.Relay(m, cfg, "k", source_id="client:t", post=rt, clock=lambda: 1.0, log=lambda s: None,
                save=lambda new: saved.append(copy.deepcopy(new)))
    return m, rt, r


def test_service_enable_saves_the_setting_and_starts_then_stops(cfg):
    cfg["components"]["kokoro-tts"].update(path=sys.executable)
    saved = []
    m, rt, r = relay_with(cfg, [{"action_id": "a1", "kind": "client_service_enable",
                                 "payload": {"component": "kokoro-tts", "enabled": True}}], saved)
    for a in r.claim():
        r.handle(a)
    assert rt.reports[0][1]["status"] == "applied", rt.reports
    assert saved[-1]["components"]["kokoro-tts"]["enabled"] is True
    assert m.procs["kokoro-tts"].want is True
    rt.actions = [{"action_id": "a2", "kind": "client_service_enable",
                   "payload": {"component": "kokoro-tts", "enabled": False}}]
    for a in r.claim():
        r.handle(a)
    assert rt.reports[1][1]["status"] == "applied" and m.procs["kokoro-tts"].want is False
    assert saved[-1]["components"]["kokoro-tts"]["enabled"] is False


def test_service_enable_refuses_what_it_should(cfg):
    saved = []
    m, rt, r = relay_with(cfg, [
        {"action_id": "b1", "kind": "client_service_enable", "payload": {"component": "kilo", "enabled": True}},
        {"action_id": "b2", "kind": "client_service_enable", "payload": {"component": "blender", "enabled": "yes"}},
        {"action_id": "b3", "kind": "client_service_enable", "payload": {"component": "blender", "enabled": True}},
    ], saved)
    for a in r.claim():
        r.handle(a)
    st = {aid: body["status"] for aid, body in rt.reports}
    assert st["b1"] == "rejected" and st["b2"] == "rejected"      # not togglable / not a boolean
    assert st["b3"] == "failed" and "path is required" in dict(rt.reports)["b3"]["detail"]
    assert saved == []                                            # nothing invalid was written
    assert m.procs["blender"].want is False


# ----------------------------------------------------------------------------- coordinator requests

def write_req(cfg, name, body):
    d = __import__("pathlib").Path(cfg["client"]["state_dir"]) / "client-requests"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(body))
    return d


def result_of(d, name):
    return json.loads((d / "results" / name).read_text())


def test_start_service_request_starts_a_stopped_enabled_component(cfg):
    cfg["components"]["rojo"].update(enabled=True, path=sys.executable)
    m = mk(cfg)
    d = write_req(cfg, "start_service-au-rojo.json", {
        "kind": "start_service", "id": "r1", "project": "au",
        "detail": {"service": "rojo", "component": "rojo"}})
    RequestQueue(m).poll()
    res = result_of(d, "start_service-au-rojo.json")
    assert res["status"] == "done" and res["component"] == "rojo", res
    assert m.procs["rojo"].want is True


def test_start_service_request_never_enables_a_disabled_component_or_an_external_one(cfg):
    m = mk(cfg)
    d = write_req(cfg, "start_service-au-blender.json", {
        "kind": "start_service", "detail": {"service": "blender-mcp", "component": "blender"}})
    write_req(cfg, "start_service-au-kokoro.json", {
        "kind": "start_service", "component": "not-a-component"})
    RequestQueue(m).poll()
    r = result_of(d, "start_service-au-blender.json")
    assert r["status"] == "failed" and "switched off" in r["message"]
    r = result_of(d, "start_service-au-kokoro.json")
    assert r["status"] == "failed" and "not a component" in r["message"]
    assert m.procs["blender"].want is False
