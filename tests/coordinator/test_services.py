"""Service registry (Team Lead 5): the needs -> services map, `ready()` with fake probes, the
`services` table, the engine hook `blocks()` (waiting, backoff, client start request), plan
`## Needs` parsing, and the MCP probes against a fixture kilo.jsonc."""
import json
import os

import pytest

from foundry_automation import service_map as SM
from foundry_coordinator import client_requests, services as S
from foundry_coordinator.store import Store

KILO = """{
  // the project file: stubs for servers the global Kilo config defines
  "snapshot": false,
  "mcp": {
    "Roblox_Studio": {"type": "local", "command": ["C:\\\\Windows\\\\System32\\\\cmd.exe", "/d", "/c", "%BIN%"], "enabled": true},
    "robloxstudio-mcp": {"enabled": true},
    "blender-mcp": {"enabled": false},
    "acestep-music": {"enabled": false},
    "kokoro-tts": {"enabled": false},
    "Media_Gen": {"enabled": false},
    "foundry-media": {"type": "local", "command": ["powershell.exe", "-NoProfile", "-File", "%PS1%"], "enabled": true},
    "roblox-docs": {"type": "remote", "url": "http://192.168.0.114:8931/sse", "enabled": true}
  }
}"""
GLOBAL = """{
  "mcp": {
    "robloxstudio-mcp": {"type": "local", "command": ["C:\\\\Windows\\\\System32\\\\cmd.exe", "/c", "npx.cmd", "-y", "x"]},
    "blender-mcp": {"type": "local", "command": ["C:\\\\Windows\\\\System32\\\\cmd.exe", "/c", "uvx.exe", "blender-mcp"]},
    "acestep-music": {"type": "remote", "url": "http://192.168.0.115:8937/sse"},
    "kokoro-tts": {"type": "remote", "url": "http://192.168.0.115:8934/sse"},
    "Media_Gen": {"type": "remote", "url": "http://192.168.0.206:8190/mcp"}
  }
}"""


# ----------------------------------------------------------------------------- the map

def test_needs_map_services_per_need():
    assert S.services_for(["studio"]) == ["rojo", "robloxstudio-mcp", "kilo"]
    assert S.services_for("blender") == ["blender-mcp"]
    # 2026-10-27: one Media_Gen server replaced the four audio servers
    assert S.services_for(["audio"]) == ["Media_Gen"]
    assert S.services_for(["images"]) == ["Media_Gen", "comfy-mcp", "foundry-media"]
    assert S.normalize_needs("media-gen sfx") == ["audio"]
    assert S.services_for(["gpu"]) == ["local-model"]
    assert S.services_for(["backend"]) == ["router"] and S.services_for(["none"]) == []
    # words are normalised: aliases, case, unknowns dropped, order-free
    assert S.normalize_needs("UI, Blender; nonsense music") == ["audio", "blender", "studio"]
    assert S.normalize_needs(None) == [] and S.normalize_needs(["needs:gpu"]) == ["gpu"]


def test_every_mapped_service_is_in_the_catalog_and_knows_who_needs_it():
    for need, groups in SM.NEEDS_SERVICES.items():
        for g in groups:
            for name in (g if isinstance(g, tuple) else (g,)):
                assert name in SM.SERVICES and need in SM.SERVICES[name].needed_by
                assert need in SM.needed_by(name)
    assert SM.mcp_for(["studio", "blender"]) == ["blender-mcp", "Roblox_Studio", "robloxstudio-mcp"]


# ----------------------------------------------------------------------------- ready()

def probe_of(**states):
    """Fake probe: name -> (ok, detail); unlisted services are up."""
    def probe(name):
        v = states.get(name.replace("-", "_"), True)
        return v, f"fake {name}"
    return probe


def test_ready_without_a_db_uses_the_probe_directly():
    ok, missing = S.ready(["studio"], probe=probe_of())
    assert ok and missing == []
    ok, missing = S.ready(["studio", "blender"], probe=probe_of(rojo=False, blender_mcp=False))
    assert not ok and missing == ["blender-mcp", "rojo"]
    assert S.ready([], probe=probe_of(rojo=False)) == (True, [])


def test_unknown_is_not_missing_and_an_any_of_group_needs_one_member():
    assert S.ready(["audio"], probe=probe_of(Media_Gen=None))[0] is True
    assert S.ready(["audio"], probe=probe_of(Media_Gen=False)) == (False, ["Media_Gen"])
    assert S.ready(["images"], probe=probe_of(comfy_mcp=False))[0] is True        # Media_Gen is up
    ok, missing = S.ready(["images"], probe=probe_of(Media_Gen=False, comfy_mcp=False,
                                                      foundry_media=False))
    assert not ok and missing == ["Media_Gen/comfy-mcp/foundry-media"]


def test_ready_stores_probes_and_trusts_them_for_the_ttl(tmp_path):
    db = Store(str(tmp_path / "c.sqlite"))
    calls = []

    def probe(name):
        calls.append(name)
        return (name != "rojo"), f"fake {name}"
    assert S.ready(["studio"], db=db, now=1000.0, probe=probe) == (False, ["rojo"])
    n = len(calls)
    assert S.ready(["studio"], db=db, now=1010.0, probe=probe) == (False, ["rojo"])
    assert len(calls) == n                                   # served from the table
    assert S.ready(["studio"], db=db, now=1100.0, probe=probe)[0] is False
    assert len(calls) == 2 * n                               # stale: probed again
    rows = {r["name"]: r for r in S.snapshot(db, probe=probe, now=1100.0)}
    assert rows["rojo"]["status"] == "down" and rows["kilo"]["status"] == "up"
    assert rows["rojo"]["provider"] == "client" and rows["rojo"]["component"] == "rojo"
    assert "studio" in rows["rojo"]["needed_by"] and rows["blender-mcp"]["status"] in ("up", "unknown")


def test_a_probe_that_raises_is_unknown_not_a_crash(tmp_path):
    db = Store(str(tmp_path / "c.sqlite"))

    def probe(name):
        raise RuntimeError("boom")
    assert S.ready(["studio"], db=db, now=1.0, probe=probe) == (True, [])
    assert {r["status"] for r in S.snapshot(db, probe=probe, now=1.0)} == {"unknown"}


# ----------------------------------------------------------------------------- real probe on a fixture

@pytest.fixture()
def kilo_files(tmp_path):
    bin_ = tmp_path / "mcp.bat"
    bin_.write_text("@echo off")
    ps1 = tmp_path / "media.ps1"
    ps1.write_text("")
    proj = tmp_path / "kilo.jsonc"
    proj.write_text(KILO.replace("%BIN%", str(bin_).replace("\\", "\\\\"))
                    .replace("%PS1%", str(ps1).replace("\\", "\\\\")), encoding="utf-8")
    g = tmp_path / "global.jsonc"
    g.write_text(GLOBAL, encoding="utf-8")
    return proj, g


def fake_fs():
    present = {"cmd.exe", "uvx.exe", "powershell.exe"}
    return dict(which=lambda n: n if os.path.basename(n).lower() in present else None,
                isfile=lambda p: os.path.basename(p).lower() in present or os.path.exists(p))


def test_make_probe_resolves_stubs_against_the_global_config(kilo_files):
    proj, g = kilo_files
    up_ports = {("192.168.0.206", 8190)}
    mcp = SM.merged_mcp(project_path=str(proj), global_path=str(g))
    assert mcp["acestep-music"]["url"].endswith(":8937/sse") and mcp["acestep-music"]["enabled"] is False
    fs = fake_fs()
    probe = S.make_probe(mcp=mcp, overrides={}, port_fn=lambda h, p, *a: (h, p) in up_ports,
                         which=fs["which"], isfile=fs["isfile"])
    assert probe("Media_Gen")[0] is True                     # remote, port open
    assert probe("kokoro-tts") == (None, "not in the service catalog")   # replaced by Media_Gen
    ok, why = probe("robloxstudio-mcp")                      # cmd.exe wrapper -> npx.cmd, not found
    assert ok is False and "npx.cmd" in why
    assert probe("blender-mcp")[0] is False                  # uvx found, but the add-on socket is closed
    assert "9876" in probe("blender-mcp")[1]
    assert probe("foundry-media")[0] is True                 # powershell -File script exists
    assert probe("comfy-mcp")[0] is None                     # defined nowhere: unknown, not down
    assert probe("kilo")[0] is False                         # port check, closed
    assert probe("rojo", )[1].startswith("port 34872")


def test_probe_honours_a_profile_override_and_enabled_false():
    probe = S.make_probe(mcp={}, overrides={"rojo": {"port": 1234}, "kilo": {"enabled": False}},
                         port_fn=lambda h, p, *a: p == 1234)
    assert probe("rojo") == (True, "port 1234 is open")
    assert probe("kilo")[0] is None


def test_command_target_looks_through_shell_wrappers():
    assert SM.command_target(["C:\\Windows\\System32\\cmd.exe", "/d", "/c", "C:\\x\\mcp.bat"])[1] == "C:\\x\\mcp.bat"
    assert SM.command_target(["powershell.exe", "-NoProfile", "-File", "s.ps1"])[1] == "s.ps1"
    assert SM.command_target(["C:\\mcp\\comfy-mcp.exe"]) == ("C:\\mcp\\comfy-mcp.exe",) * 2


# ----------------------------------------------------------------------------- plan needs

def test_parse_needs_reads_only_the_needs_block():
    plan = "# AS-B1\n## Goal\nx\n## Needs\n- studio\n- `blender`, audio\n## Files\n- a.lua\n"
    assert S.parse_needs(plan) == ["audio", "blender", "studio"]
    assert S.parse_needs("## Needs\nnone\n") == [] and S.parse_needs("no block") == []
    # the old free-text line is deliberately not guessed at
    assert S.parse_needs("**NEEDS (MCP):** ON git, Roblox_Studio. OFF blender-mcp.") == []


class Spec:
    def __init__(self, data=None, tags=(), spec=""):
        self.data, self.tags, self.spec = data or {}, list(tags), spec


def test_needs_of_precedence():
    assert S.needs_of({"data": {"needs": ["gpu"]}}, Spec(data={"needs": ["audio"]})) == ["gpu"]
    assert S.needs_of({"data": {}}, Spec(data={"needs": ["audio"]})) == ["audio"]
    assert S.needs_of({"data": {}}, Spec(tags=["tier:A", "needs:blender"])) == ["blender"]
    assert S.needs_of({"data": {}}, Spec(spec="## Needs\nimages")) == ["images"]
    assert S.needs_of({"data": {}}, Spec()) == [] and S.needs_of({}, None) == []


# ----------------------------------------------------------------------------- the engine hook

class FakeEngine:
    def __init__(self, tmp_path):
        self.store = Store(str(tmp_path / "c.sqlite"))
        self.state_dir = str(tmp_path / "state")
        self.t = 1000.0
        self.notes = []
        self.clock = lambda: self.t

    def _note(self, P, t, text):
        self.notes.append(text)
        self.store.update_task(P.id, t["task_id"], next_action=text)


class P_:
    id = "au"
    profile = None


@pytest.fixture()
def eng(tmp_path, monkeypatch):
    e = FakeEngine(tmp_path)
    e.store.x("INSERT INTO tasks (project_id, task_id, state, data) VALUES ('au','T1','READY','{}')")
    return e


def task(e):
    return e.store.task("au", "T1")


def test_blocks_waits_with_backoff_and_asks_the_client(eng, monkeypatch):
    down = {"rojo"}
    monkeypatch.setattr(S, "make_probe", lambda profile=None, **k: (
        lambda name: ((name not in down), f"fake {name}")))
    t = task(eng)
    assert S.blocks(eng, P_, t, Spec(data={"needs": ["studio"]})) is True
    t = task(eng)
    assert t["data"]["needs"] == ["studio"] and t["data"]["svc_wait"]["missing"] == ["rojo"]
    assert "waiting for rojo" in eng.notes[-1] and "no attempt" in eng.notes[-1]
    assert t["data"]["svc_wait"]["n"] == 1
    # a start request for the client component, once
    reqs = client_requests.pending(eng.state_dir, "start_service")
    assert len(reqs) == 1 and reqs[0]["detail"] == {"service": "rojo", "component": "rojo"}
    # inside the backoff window the engine does not re-probe, it repeats the note
    eng.t += 10
    down.clear()
    assert S.blocks(eng, P_, task(eng), Spec()) is True
    assert "next check in" in eng.notes[-1]
    # after the window the service is up: the task goes on and the wait is cleared
    eng.t += 40
    assert S.blocks(eng, P_, task(eng), Spec()) is False
    assert "svc_wait" not in task(eng)["data"]
    kinds = [e["kind"] for e in eng.store.events("au")]
    assert "service_wait" in kinds and "service_ready" in kinds


def test_backoff_doubles_and_is_capped(eng, monkeypatch):
    monkeypatch.setattr(S, "make_probe", lambda profile=None, **k: (lambda name: (False, "down")))
    spec = Spec(data={"needs": ["blender"]})
    delays = []
    for _ in range(7):
        eng.t += 10_000
        assert S.blocks(eng, P_, task(eng), spec)
        w = task(eng)["data"]["svc_wait"]
        delays.append(w["retry_at"] - eng.t)
    assert delays == [30, 60, 120, 240, 480, 600, 600]


def test_no_needs_costs_nothing(eng, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no probe for a task without needs")
    monkeypatch.setattr(S, "make_probe", boom)
    assert S.blocks(eng, P_, task(eng), Spec()) is False and eng.notes == []


def test_client_start_request_is_rate_limited_per_service(eng, monkeypatch):
    monkeypatch.setattr(S, "make_probe", lambda profile=None, **k: (lambda name: (False, "down")))
    spec = Spec(data={"needs": ["studio"]})
    S.blocks(eng, P_, task(eng), spec)
    first = {r["id"]: r["count"] for r in client_requests.pending(eng.state_dir, "start_service")}
    eng.t += 100
    S.blocks(eng, P_, task(eng), spec)
    again = {r["id"]: r["count"] for r in client_requests.pending(eng.state_dir, "start_service")}
    assert first == again                                     # nobody was asked twice within 10 min
    names = sorted(r["detail"]["service"] for r in client_requests.pending(eng.state_dir))
    assert names == ["kilo", "rojo"]                          # robloxstudio-mcp has no client component


def test_media_gen_tools_are_denied_per_family():
    """Media_Gen serves audio and images (2026-10-27): a track with only one of the two
    needs keeps that family's tools and is denied the other family's."""
    from foundry_coordinator.kilo_config import track_rules
    from foundry_automation import role_rules as RR
    rules = {role: {} for role in RR.RULE_ROLES}
    img = track_rules(rules, ["images"])["implementer"]["deny_tools"]
    assert "Media_Gen_generate_sfx" in img and "Media_Gen_generate_speech" in img
    assert "Media_Gen_generate_image" not in img and "Media_Gen_remove_background" not in img
    aud = track_rules(rules, ["audio"])["implementer"]["deny_tools"]
    assert "Media_Gen_generate_image" in aud and "Media_Gen_generate_sfx" not in aud
    both = track_rules(rules, ["audio", "images"])["implementer"]["deny_tools"]
    assert not [t for t in both if t.startswith("Media_Gen_")]
    assert "Media_Gen" in SM.CONTROLLED_MCP and "kokoro-tts" in SM.CONTROLLED_MCP
