"""OpenCode beside Kilo (v0.181.247): the first opencode.jsonc, the router's export and
session header, the Foundry Client's OpenCode component and reload request, and training
capture of OpenCode sessions."""

import base64
import json
import os

import pytest

from foundry_automation import jsonc
from foundry_coordinator import agent_config as AC
from foundry_coordinator import client_requests as CR
from foundry_coordinator.kilo_config import agent_config_paths, kilo_path


class _Prof:
    def __init__(self, ws, client, raw=None):
        self.workspace, self.client, self.raw, self.id = ws, client, raw or {}, "pilot"

    def persona(self, role, task):
        return "Foundry-Kilo-Local"

    def secret(self, t, k):
        return ""


KILO_SRC = """// a Kilo config
{"provider": {"foundry": {"npm": "@ai-sdk/openai-compatible", "name": "Foundry Router",
  "options": {"baseURL": "http://192.168.0.112:11435/v1", "apiKey": "{env:FOUNDRY_KEY_KILO}"},
  "whitelist": ["Foundry-Kilo-Local"],
  "models": {"Foundry-Kilo-Local": {"limit": {"context": 262144, "output": 8192},
             "attachment": false, "modalities": {"input": ["text"], "output": ["text"]}}}}},
 "enabled_providers": ["foundry"], "subagent_depth": 1, "indexing": {"enabled": true}}
"""


def test_paths_follow_the_client_kind(tmp_path):
    assert kilo_path(_Prof(str(tmp_path), {"kind": "kilo"})).endswith("kilo.jsonc")
    assert kilo_path(_Prof(str(tmp_path), {"kind": "opencode"})).endswith("opencode.jsonc")
    mixed = _Prof(str(tmp_path), {"kind": "kilo"}, {"clients": {"oc": {"kind": "opencode"}}})
    assert [os.path.basename(p) for p in agent_config_paths(mixed)] == ["kilo.jsonc",
                                                                       "opencode.jsonc"]
    assert CR.reload_kind(_Prof("", {"kind": "kilo"})) == "reload_kilo"
    assert CR.reload_kind(_Prof("", {"kind": "opencode"})) == "reload_opencode"
    assert CR.reload_kind(None, "/w/opencode.jsonc") == "reload_opencode"
    assert "reload_opencode" in CR.KINDS


def test_provider_block_points_the_key_at_opencode_and_drops_literals():
    p = AC.provider_block(KILO_SRC)
    assert p["options"]["apiKey"] == "{env:FOUNDRY_KEY_OPENCODE}"
    assert "whitelist" not in p and "Foundry-Kilo-Local" in p["models"]
    lit = KILO_SRC.replace("{env:FOUNDRY_KEY_KILO}", "sk-literal-secret-key")
    assert "sk-literal" not in json.dumps(AC.provider_block(lit))


@pytest.mark.parametrize("dialect", ["oc1", "oc2"])
def test_first_opencode_config_has_the_safety_keys_and_no_kilo_keys(tmp_path, dialect):
    src = tmp_path / "kilo.jsonc"
    src.write_text(KILO_SRC, encoding="utf-8")
    prof = _Prof(str(tmp_path), {"kind": "opencode", "agent": "build"},
                 {"role_rules": {"implementer": {"agent": "build"}, "lead": {"agent": "lead"}},
                  "helpers": {"agent": "au-helper"}})
    out = AC.write(prof, provider_from=str(src), dialect=dialect, apply=True)
    cfg = jsonc.parse((tmp_path / "opencode.jsonc").read_text(encoding="utf-8"))
    assert out["applied"] and cfg["share"] == "disabled" and cfg["autoupdate"] is False
    assert cfg["enabled_providers"] == ["foundry"] and cfg["snapshot"] is False
    assert cfg["model"] == "foundry/Foundry-Kilo-Local" and cfg["default_agent"] == "build"
    assert "build" in cfg["agent"] and "lead" in cfg["agent"]
    assert "indexing" not in cfg                       # Kilo-only keys never copied
    if dialect == "oc2":
        assert cfg["update"] == "disable" and cfg["experimental"]["subagent_depth"] == 1
    else:
        assert cfg["subagent_depth"] == 1 and "update" not in cfg
    with pytest.raises(AC.AgentConfigError, match="exists"):
        AC.write(prof, provider_from=str(src), dialect=dialect, apply=True)
    with pytest.raises(AC.AgentConfigError, match="not opencode"):
        AC.write(_Prof(str(tmp_path), {"kind": "kilo"}), provider_from=str(src))


def test_regenerate_edits_an_opencode_file_like_kilos(tmp_path):
    from foundry_coordinator.kilo_config import plan
    src = tmp_path / "kilo.jsonc"
    src.write_text(KILO_SRC, encoding="utf-8")
    prof = _Prof(str(tmp_path), {"kind": "opencode"},
                 {"role_rules": {"implementer": {"agent": "build"}}})
    AC.write(prof, provider_from=str(src), apply=True)
    text = (tmp_path / "opencode.jsonc").read_text(encoding="utf-8")
    p = plan(text, {"implementer": {"agent": "build"}}, str(tmp_path))
    assert "build" in p["changed"]
    new = jsonc.parse(p["text"])
    assert isinstance(new["agent"]["build"]["permission"], dict)
    assert new["share"] == "disabled"                  # untouched


def test_router_export_formats():
    from foundry_router.ui.routes import AGENT_CONFIG_FORMATS, agent_config_block
    block = {"provider": {"foundry": {"options": {"baseURL": "http://r/v1",
                                                  "apiKey": "{env:FOUNDRY_KEY_KILO}"},
                                      "models": {}}},
             "enabled_providers": ["foundry"], "subagent_depth": 1}
    assert agent_config_block(block, "kilo") is block                  # Kilo: unchanged
    oc = agent_config_block(block, "opencode")
    assert oc["provider"]["foundry"]["options"]["apiKey"] == "{env:FOUNDRY_KEY_OPENCODE}"
    assert oc["share"] == "disabled" and oc["experimental"] == {"subagent_depth": 1}
    assert "subagent_depth" not in oc and oc["update"] == "disable"
    oc1 = agent_config_block(block, "opencode1")
    assert oc1["subagent_depth"] == 1 and "experimental" not in oc1
    assert block["provider"]["foundry"]["options"]["apiKey"] == "{env:FOUNDRY_KEY_KILO}"
    assert AGENT_CONFIG_FORMATS == ("kilo", "opencode", "opencode1")


def test_router_session_header_prefers_opencodes_own_id():
    from foundry_router.client_auth import SESSION_FALLBACK_HEADERS
    assert SESSION_FALLBACK_HEADERS[0] == "x-opencode-session-id"
    assert SESSION_FALLBACK_HEADERS[1] == "x-session-id"
    from foundry_router.ui.setup_routes import PRESETS
    assert PRESETS["opencode"]["scopes"] == ["inference", "media"]
    from foundry_router.control_center import CLIENT_COMPONENTS
    assert "opencode" in CLIENT_COMPONENTS and "kilo" in CLIENT_COMPONENTS


def test_client_opencode_component_is_off_by_default_and_on_the_page():
    from foundry_client import config
    d = config.DEFAULTS["components"]["opencode"]
    assert d["enabled"] is False and d["port"] == 4097
    assert d["env"]["OPENCODE_DISABLE_CLAUDE_CODE"] == "true"
    assert "OPENCODE_SERVER_PASSWORD" in d["secrets"]
    assert "opencode" in config.COMPONENTS
    from foundry_client.client_requests import action_of
    assert action_of({"action": "reload_opencode"}) == "reload_opencode"
    assert action_of({"action": "restart-opencode"}) == "reload_opencode"
    assert action_of({"action": "reload_kilo"}) == "reload_kilo"
    html = open(os.path.join(os.path.dirname(config.__file__), "static", "client.html"),
                encoding="utf-8").read()
    for key in ("components.opencode.enabled", "components.opencode.path",
                "components.opencode.port", "components.opencode.cwd"):
        assert key in html


def test_client_builds_the_opencode_spec_only_when_enabled(tmp_path, monkeypatch):
    from foundry_client import components as C, config
    cfg = json.loads(json.dumps(config.DEFAULTS))
    cfg["workspace"]["game"] = str(tmp_path)
    cfg["workspace"]["profiles"] = str(tmp_path)
    cfg["components"]["opencode"]["enabled"] = True
    real = C.find_binary
    monkeypatch.setattr(C, "find_binary", lambda path, names, extra:
                        "opencode.cmd" if "opencode" in names else real(path, names, extra))
    spec = C.build(cfg)["opencode"]
    assert spec.argv[:2] == ["opencode.cmd", "serve"] and "4097" in spec.argv
    cfg["components"]["opencode"]["port"] = 4096
    assert any("differ" in e for e in config.validate(cfg))


def test_client_opencode_health_tells_apart_closed_and_wrong_password(monkeypatch):
    from foundry_client import components as C
    monkeypatch.setattr(C, "port_open", lambda port: False)
    assert C.opencode_health(4097) == (False, "port 4097 is closed")
    monkeypatch.setattr(C, "port_open", lambda port: True)
    monkeypatch.setattr(C, "opencode_password", lambda: "pw")
    calls = []

    def fake_get(url, headers, timeout):
        calls.append((url, headers))
        return (200, {"version": "2.3.2"}, "") if url.endswith("/api/info") else (404, None, "")
    monkeypatch.setattr(C, "get_json", fake_get)
    ok, detail = C.opencode_health(4097)
    assert ok and "2.3.2" in detail
    assert calls[0][1]["Authorization"] == "Basic " + base64.b64encode(b"opencode:pw").decode()
    monkeypatch.setattr(C, "get_json", lambda url, h, t: (401, None, "HTTP 401"))
    assert "rejects the OpenCode server password" in C.opencode_health(4097)[1]


def test_schema_has_controls_for_every_new_setting():
    from foundry_automation import schema as S
    f = S.find_field("client.kind")
    assert "opencode" in f.enum
    for key in ("client.api", "clients.oc.kind", "clients.oc.url", "clients.oc.password_env",
                "clients.oc.api", "clients.oc.agent", "clients.oc.agents",
                "clients.oc.worker_client", "clients.oc.config", "client_roles.helper",
                "client_roles.implementer", "client_roles.lead"):
        assert S.find_field(key) is not None, key
    assert S.is_local_key("clients.oc.url") and S.is_local_key("clients.oc.password_env")
    assert "clients" in S.TABLES_BY_NAME and "client_roles" in S.TABLES_BY_NAME


def test_training_tags_opencode_sessions_and_reads_them_through_the_api():
    from foundry_coordinator.adapters.oc2 import Oc2Client
    from foundry_coordinator.devtools.fake_opencode import FakeOpenCode2
    from foundry_training.adapters import kilo, opencode
    fake = FakeOpenCode2(password="pw")
    url = fake.start()
    try:
        c = Oc2Client(url, password="pw")
        sid = c.start_session(directory="/w", title="t", persona="P", agent="build",
                              metadata={"project": "pilot", "task": "T1"}, permissions=[])
        c.prompt(sid, "go", directory="/w", persona="P", agent="build")
        fake.finish(sid, "done", tools=[{"name": "read", "input": {"filePath": "a.py"},
                                         "output": "x = 1"}])
        s = opencode.from_api(url, sid, "pw", "/w", api="oc2")
        assert s["_adapter"] == "opencode" and s["info"]["id"] == sid
        evs = kilo.normalize(s, "pilot", "raw", {"status": "clean"})
        assert {e.source_adapter for e in evs} == {"opencode"}
        kinds = [e.event_type for e in evs]
        assert "session_start" in kinds and "tool_call" in kinds and "user_message" in kinds
        recs = list(kilo.session_records([s], [], "api"))
        assert recs[0][3]["adapter"] == "opencode"
    finally:
        fake.stop()


def test_kilo_training_events_are_still_tagged_kilo():
    from foundry_training.adapters import kilo
    s = {"info": {"id": "ses_1", "time": {"created": 1000}}, "messages": []}
    assert {e.source_adapter for e in kilo.normalize(s, "p", "r", {})} == {"kilo"}
