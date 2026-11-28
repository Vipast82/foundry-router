"""The workstation installers' Python side (foundry_client/workstation.py): re-runs keep what
is there, repair without a code, several projects on one install, OpenCode, the generic
profile template, the Windows layout."""

import json
import shutil
import tomllib
from pathlib import Path

import pytest

from foundry_client import components, config, secrets, workstation as W

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "foundry_coordinator" / "projects" / "_template" / "project.example.toml"


def _enr(project="animaluprising", keys=True, **kw):
    k = {"FOUNDRY_COORDINATOR_KEY": "fnd_coord", "FOUNDRY_KEY_KILO": "fnd_kilo",
         "FOUNDRY_KEY": "fnd_kilo", "FOUNDRY_CLIENT_KEY": "fnd_relay",
         "FOUNDRY_KEY_OPENCODE": "fnd_oc"} if keys else {}
    return {"machine": "ws-a", "kind": "workstation", "project": project, "keys": k,
            "router_http": "http://router.test:11435", "implementer": "Foundry-Kilo-Local",
            "kilo_provider": {"provider": {"foundry": {"npm": "@ai-sdk/openai-compatible",
                                                       "options": {"baseURL": "http://router.test:11435/v1",
                                                                   "apiKey": "{env:FOUNDRY_KEY_KILO}"},
                                                       "models": {}}},
                              "enabled_providers": ["foundry"]},
            "game": {"id": project, "clone_url": "https://github.com/me/game.git",
                     "branch": "main", "remote": "origin", "repo_key": None,
                     "repo_key_problem": ""},
            "studio": None, **kw}


@pytest.fixture(autouse=True)
def _never_the_real_client(tmp_path, monkeypatch):
    """No test here may read or write this PC's own client.toml or keyring."""
    monkeypatch.setenv("FOUNDRY_CLIENT_CONFIG", str(tmp_path / "client.toml"))
    monkeypatch.setenv("FOUNDRY_CLIENT_SECRET_STORE", "file")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    secrets.configure({"client": {"secret_store": "file",
                                  "secrets_file": str(tmp_path / "secrets.json")}})
    yield
    secrets.configure({"client": {}})


def _apply(tmp_path, enr, root="srv"):
    cfgp = tmp_path / "client.toml"
    secrets.configure({"client": {"secret_store": "file",
                                  "secrets_file": str(tmp_path / "secrets.json")}})
    return W.apply(enr, root=str(tmp_path / root), secret_store="file",
                   home=str(tmp_path / "home"), cfg_path=cfgp)


def test_windows_layout_runs_the_venv_python_from_scripts():
    L = W.layout("C:\\Foundry", "animaluprising")
    assert L["python"] == "C:/Foundry/venv/Scripts/python.exe"
    assert L["game"] == "C:/Foundry/game/animaluprising"
    d = json.loads(W.kilo_jsonc(_enr(), L))
    assert d["mcp"]["foundry-media"]["command"][0] == "C:/Foundry/venv/Scripts/python.exe"
    assert d["mcp"]["foundry-media"]["command"][-1] == \
        "C:/Foundry/config/foundry-media-animaluprising.json"
    assert W.layout("/srv/foundry", "x")["python"] == "/srv/foundry/venv/bin/python"
    assert W.layout("/srv/foundry", "x", windows=True)["python"].endswith("Scripts/python.exe")


def test_a_rerun_keeps_the_passwords_and_your_client_toml_edits(tmp_path):
    first = _apply(tmp_path, _enr())
    assert first["keys_changed"] is True
    pw = secrets.get("KILO_SERVER_PASSWORD")
    assert pw and len(pw) >= 24
    cfgp = tmp_path / "client.toml"
    raw = tomllib.loads(cfgp.read_text(encoding="utf-8"))
    raw["client"]["backend_url"] = "http://elsewhere.test:11435"    # edited by hand
    raw["components"]["kilo"]["port"] = 4196
    raw["components"]["kilo"]["secrets"].append("MY_EXTRA")
    config.save(config._merge(config.DEFAULTS, raw), cfgp)
    again = _apply(tmp_path, _enr())
    assert secrets.get("KILO_SERVER_PASSWORD") == pw
    assert "KILO_SERVER_PASSWORD (new, random)" not in again["secrets_stored"]
    c = config.load(cfgp)
    assert c["client"]["backend_url"] == "http://elsewhere.test:11435"
    assert c["components"]["kilo"]["port"] == 4196
    assert "MY_EXTRA" in c["components"]["kilo"]["secrets"]
    assert "FOUNDRY_KEY" in c["components"]["kilo"]["secrets"]


def test_repair_without_keys_keeps_the_stored_ones_and_needs_an_enrolment(tmp_path):
    with pytest.raises(W.NotEnrolled):
        _apply(tmp_path, _enr(keys=False))
    _apply(tmp_path, _enr())
    res = _apply(tmp_path, _enr(keys=False, keys_kept=["ws-a"]))
    assert res["keys_changed"] is False and res["secrets_stored"] == []
    assert secrets.get("FOUNDRY_COORDINATOR_KEY") == "fnd_coord"


def test_a_second_project_is_added_beside_the_first(tmp_path):
    _apply(tmp_path, _enr())
    res = _apply(tmp_path, _enr("othergame", keys=False, keys_kept=["ws-a"]))
    assert res["default_project"] == "animaluprising"
    assert res["projects"] == ["animaluprising", "othergame"]
    c = config.load(tmp_path / "client.toml")
    root = (tmp_path / "srv").as_posix()
    assert c["workspace"]["project"] == "animaluprising"
    assert c["workspace"]["game"] == f"{root}/game/animaluprising"
    assert c["workspace"]["projects"]["othergame"]["game"] == f"{root}/game/othergame"
    ws2 = config.project_workspace(c, "othergame")
    assert ws2["game"] == f"{root}/game/othergame" and ws2["project"] == "othergame"
    assert config.project_workspace(c, "")["game"] == f"{root}/game/animaluprising"
    assert config.project_workspace(c, "unknown")["game"] == f"{root}/game/animaluprising"
    assert components.kilo_config_path(c, "othergame").replace("\\", "/") == \
        f"{root}/game/othergame/kilo.jsonc"
    assert components.kilo_config_path(c).replace("\\", "/") == f"{root}/game/animaluprising/kilo.jsonc"
    # each project's media adapter has its own config; the default keeps foundry-media.json
    cdir = tmp_path / "srv" / "config"
    assert (cdir / "foundry-media-othergame.json").is_file()
    assert "animaluprising" in (cdir / "foundry-media.json").read_text(encoding="utf-8")


def test_an_old_client_toml_loads_unchanged(tmp_path):
    p = tmp_path / "old.toml"
    p.write_text('[client]\nbackend_url = "http://r.test:11435"\n[workspace]\n'
                 'game = "C:/Roblox/AnimalUprising"\nprofiles = "C:/x/projects"\n', encoding="utf-8")
    c = config.load(p)
    assert c["workspace"]["projects"] == {} and config.validate(c) == []
    assert config.project_workspace(c, "animaluprising")["game"] == "C:/Roblox/AnimalUprising"
    bad = config._merge(c, {"workspace": {"projects": {"x": "not a table"}}})
    assert any("workspace.projects" in e for e in config.validate(bad))


def test_opencode_project_enables_its_server_and_password(tmp_path):
    res = _apply(tmp_path, _enr(client_kind="opencode", coding_clients=["opencode"]))
    assert res["coding_clients"] == ["opencode"]
    assert secrets.get("OPENCODE_SERVER_PASSWORD") and secrets.get("FOUNDRY_KEY_OPENCODE") == "fnd_oc"
    c = config.load(tmp_path / "client.toml")
    assert c["components"]["opencode"]["enabled"] is True
    assert "OPENCODE_SERVER_PASSWORD" in c["components"]["coordinator"]["secrets"]
    L = W.layout("/srv/foundry", "animaluprising")
    raw = tomllib.loads(W.project_toml(TEMPLATE.read_text(encoding="utf-8"),
                                       _enr(client_kind="opencode"), L))
    assert raw["client"]["kind"] == "opencode" and raw["client"]["url"] == "http://127.0.0.1:4097"
    assert raw["client"]["password_env"] == "OPENCODE_SERVER_PASSWORD"


def test_a_kilo_project_does_not_turn_opencode_on(tmp_path):
    _apply(tmp_path, _enr())
    c = config.load(tmp_path / "client.toml")
    assert c["components"]["opencode"]["enabled"] is False
    assert not secrets.get("OPENCODE_SERVER_PASSWORD")


def _release(tmp_path) -> Path:
    """A minimal release checkout: only the generic template in its profiles folder."""
    root = tmp_path / "srv"
    t = root / "app" / "foundry_coordinator" / "projects" / "_template"
    t.mkdir(parents=True)
    shutil.copy(TEMPLATE, t / "project.example.toml")
    (root / "game").mkdir(parents=True, exist_ok=True)
    return root


def test_a_project_without_its_own_profile_starts_from_the_template(tmp_path):
    from foundry_coordinator import profile as P
    root = _release(tmp_path)
    (root / "game" / "newgame").mkdir()
    out = W.write_profile(_enr("newgame"), str(root))
    assert out["written"][0] == "project.toml (from the generic template)"
    prof = P.load(out["project_toml"])
    assert prof.id == "newgame" and prof.tasks["source"] == "toml" and prof.adapter == ""
    assert prof.workspace.replace("\\", "/").endswith("/game/newgame")
    assert prof.client["kind"] == "kilo"
    # a re-run keeps both files
    assert W.write_profile(_enr("newgame"), str(root))["written"] == []


def test_an_opencode_project_gets_opencode_jsonc_from_agent_config(tmp_path):
    root = _release(tmp_path)
    (root / "game" / "ocgame").mkdir()
    out = W.write_profile(_enr("ocgame", client_kind="opencode"), str(root))
    assert "opencode.jsonc" in out["written"], out
    from foundry_automation import jsonc
    doc = jsonc.parse((root / "game" / "ocgame" / "opencode.jsonc").read_text(encoding="utf-8"))
    assert doc["provider"]["foundry"]["options"]["apiKey"] == "{env:FOUNDRY_KEY_OPENCODE}"
    again = W.write_profile(_enr("ocgame", client_kind="opencode"), str(root))
    assert "opencode.jsonc" not in again["written"] and again["problems"] == []


class _Resp:
    def __init__(self, status, body):
        self.status, self._b = status, json.dumps(body).encode()

    def read(self):
        return self._b


class _Conn:
    def __init__(self, status, body):
        self.resp = _Resp(status, body)
        self.sent = None

    def request(self, method, path, body=None, headers=None):
        self.sent = (method, path, headers)

    def getresponse(self):
        return self.resp

    def close(self):
        pass


def test_fetch_reads_the_answer_again_with_the_machines_key(tmp_path, monkeypatch):
    out = tmp_path / "answer.json"
    with pytest.raises(W.NotEnrolled):
        W.fetch(str(out), cfg_path=tmp_path / "client.toml")
    _apply(tmp_path, _enr())
    conn = _Conn(200, {"machine": "ws-a", "kind": "workstation", "project": "animaluprising",
                       "keys": {}, "router_version": "0.0.1", "coding_clients": ["kilo"],
                       "router_http": ""})
    monkeypatch.setattr(W, "_connect", lambda router, fp="", timeout=30: conn)
    res = W.fetch(str(out), router="http://router.test:11435", cfg_path=tmp_path / "client.toml")
    method, path, headers = conn.sent
    assert method == "GET" and path == "/v1/foundry/control/workstation-enrollment?project=animaluprising"
    assert headers["Authorization"] == "Bearer fnd_coord"
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["keys"] == {} and saved["router"] == "http://router.test:11435"
    assert saved["router_http"] == "http://router.test:11435"
    assert "fnd_coord" not in out.read_text(encoding="utf-8") and "fnd_coord" not in json.dumps(res)
    monkeypatch.setattr(W, "_connect", lambda router, fp="", timeout=30: _Conn(
        403, {"error": {"message": "this key does not cover project 'x'"}}))
    with pytest.raises(RuntimeError, match="does not cover"):
        W.fetch(str(out), project="x", cfg_path=tmp_path / "client.toml")


def test_fetch_check_says_whether_this_machine_is_enrolled(tmp_path, capsys):
    assert W.main(["fetch", "--check"]) == 3
    _apply(tmp_path, _enr())
    assert W.main(["fetch", "--check"]) == 0
    st = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert st["enrolled"] is True and st["projects"] == ["animaluprising"]
    assert "fnd_" not in json.dumps(st)


def test_an_https_router_must_match_the_fingerprint(monkeypatch):
    import http.client

    class FakeSock:
        def getpeercert(self, binary_form=True):
            return b"cert"

    class FakeHTTPS:
        def __init__(self, *a, **k):
            self.sock = FakeSock()
            self.closed = False

        def connect(self):
            pass

        def close(self):
            self.closed = True

    monkeypatch.setattr(http.client, "HTTPSConnection", FakeHTTPS)
    import hashlib
    good = hashlib.sha256(b"cert").hexdigest().upper()
    assert W._connect("https://r.test:11436", ":".join(good[i:i + 2] for i in range(0, 64, 2)))
    with pytest.raises(OSError, match="fingerprint"):
        W._connect("https://r.test:11436", "AB" * 32)
    with pytest.raises(OSError):
        W._connect("https://r.test:11436", "")


def test_merge_settings_fills_a_new_file_and_keeps_edits(tmp_path):
    p = tmp_path / "client.toml"
    want = {"client": {"backend_url": "http://r.test:11435", "secret_store": "file"},
            "workspace": {"game": "C:/Foundry/lanes"},
            "lane": {"enabled": True, "root": "C:/Foundry/lanes", "listen": "127.0.0.1"},
            "components": {"coordinator": {"enabled": False}, "supervisor": {"enabled": False},
                           "opencode": {"enabled": True, "port": 4097}}}
    assert W.merge_settings(want, p)["fresh"] is True
    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    raw["components"]["opencode"]["port"] = 4197
    config.save(config._merge(config.DEFAULTS, raw), p)
    assert W.merge_settings(want, p)["fresh"] is False
    c = config.load(p)
    assert c["components"]["opencode"]["port"] == 4197 and c["lane"]["enabled"] is True
