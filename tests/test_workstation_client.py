"""A new workstation from its enrollment, and moving the work (LINUX_WORKSTATION_PLAN W3/W4):
foundry_client/workstation.py and foundry_client/move.py."""

import json
import os
import sqlite3
import subprocess
import tomllib
from pathlib import Path

import pytest

from foundry_client import config, move, secrets, workstation as W

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "foundry_coordinator" / "projects" / "animaluprising" / "project.example.toml"


def _enr(**kw):
    return {"machine": "ws-linux", "kind": "workstation", "project": "animaluprising",
            "keys": {"FOUNDRY_COORDINATOR_KEY": "fnd_coord", "FOUNDRY_KEY_KILO": "fnd_kilo",
                     "FOUNDRY_KEY": "fnd_kilo", "FOUNDRY_CLIENT_KEY": "fnd_relay"},
            "router_http": "http://192.168.0.112:11435", "implementer": "Foundry-Kilo-Local",
            "kilo_provider": {"provider": {"foundry": {"npm": "@ai-sdk/openai-compatible",
                                                       "options": {"baseURL": "http://192.168.0.112:11435/v1",
                                                                   "apiKey": "{env:FOUNDRY_KEY_KILO}"},
                                                       "models": {}}},
                              "enabled_providers": ["foundry"], "subagent_depth": 1},
            "game": {"id": "animaluprising", "clone_url": "https://github.com/me/game.git",
                     "branch": "master", "remote": "origin", "repo_key": None,
                     "repo_key_problem": ""},
            "studio": None, **kw}


@pytest.fixture(autouse=True)
def _never_the_real_client(tmp_path, monkeypatch):
    """No test here may read or write this PC's own client.toml or keyring."""
    monkeypatch.setenv("FOUNDRY_CLIENT_CONFIG", str(tmp_path / "guard-client.toml"))
    monkeypatch.setenv("FOUNDRY_CLIENT_SECRET_STORE", "file")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    secrets.configure({"client": {"secret_store": "file",
                                  "secrets_file": str(tmp_path / "guard-secrets.json")}})
    yield
    secrets.configure({"client": {}})


@pytest.fixture
def filestore(tmp_path, monkeypatch):
    monkeypatch.delenv("FOUNDRY_CLIENT_SECRET_STORE", raising=False)
    secrets.configure({"client": {"secret_store": "file",
                                  "secrets_file": str(tmp_path / "secrets.json")}})
    yield tmp_path / "secrets.json"
    secrets.configure({"client": {}})


def test_the_file_secret_store_is_private_and_write_only(filestore):
    secrets.set_secret("FOUNDRY_KEY", "v1")
    assert secrets.get("FOUNDRY_KEY") == "v1" and secrets.store_in_use() == "file"
    if os.name != "nt":
        assert oct(filestore.stat().st_mode & 0o777) == "0o600"
    assert secrets.status(["FOUNDRY_KEY"])["FOUNDRY_KEY"] is True
    secrets.set_secret("FOUNDRY_KEY", "")
    assert secrets.get("FOUNDRY_KEY") is None


def test_apply_stores_the_keys_and_writes_client_toml(filestore, tmp_path):
    cfgp = tmp_path / "client.toml"
    res = W.apply(_enr(), root=str(tmp_path / "srv"), secret_store="file",
                  home=str(tmp_path / "home"), cfg_path=cfgp)
    assert secrets.get("FOUNDRY_COORDINATOR_KEY") == "fnd_coord"
    assert secrets.get("FOUNDRY_CLIENT_KEY") == "fnd_relay"
    assert len(secrets.get("KILO_SERVER_PASSWORD") or "") >= 24
    assert "fnd_coord" not in json.dumps(res)                    # no values in the summary
    c = config.load(cfgp)
    root = (tmp_path / "srv").as_posix()
    assert c["workspace"]["game"] == f"{root}/game/animaluprising"
    assert c["workspace"]["deploy"] == f"{root}/app"
    assert c["client"]["backend_url"] == "http://192.168.0.112:11435"
    assert c["client"]["secret_store"] == "file"
    assert "FOUNDRY_KEY" in c["components"]["kilo"]["secrets"]
    media = json.loads((tmp_path / "srv" / "config" / "foundry-media.json").read_text())
    assert media["default_dir"].endswith("game/animaluprising/assets/generated/_unapproved")
    assert res["repo"]["kind"] == "none"
    with pytest.raises(ValueError):
        W.apply({**_enr(), "kind": "slot"}, root=str(tmp_path / "srv"), cfg_path=cfgp)


def test_repo_keys_ssh_and_https(tmp_path):
    ssh = W.repo_access(_enr(game={"clone_url": "git@github.com:me/g.git",
                                   "repo_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n"
                                               "-----END OPENSSH PRIVATE KEY-----"}),
                        str(tmp_path))
    assert ssh["kind"] == "ssh" and Path(ssh["path"]).read_text().startswith("-----BEGIN")
    assert "-i " in ssh["ssh_command"]
    https = W.repo_access(_enr(game={"clone_url": "https://github.com/me/g.git",
                                     "repo_key": "ghp_token"}), str(tmp_path))
    assert https["kind"] == "https"
    assert (tmp_path / ".git-credentials").read_text().strip() == \
        "https://x-access-token:ghp_token@github.com"


def test_project_toml_from_the_real_example_gets_linux_paths():
    L = W.layout("/srv/foundry", "animaluprising")
    raw = tomllib.loads(W.project_toml(EXAMPLE.read_text(encoding="utf-8"), _enr(), L))
    assert raw["workspace"]["path"] == "/srv/foundry/game/animaluprising"
    assert raw["workspace"]["branch"] == "master"
    assert raw["foundry"]["url"] == "http://192.168.0.112:11435"
    ext = raw["client"]["permissions"]["external_directory"]
    assert ext["/srv/foundry"] == "allow" and not any(k.startswith("C:") for k in ext)
    assert raw["id"] == "animaluprising"


def test_kilo_jsonc_has_the_provider_model_and_media_adapter():
    L = W.layout("/srv/foundry", "animaluprising")
    d = json.loads(W.kilo_jsonc(_enr(), L))
    assert d["provider"]["foundry"]["options"]["apiKey"] == "{env:FOUNDRY_KEY_KILO}"
    assert d["model"] == "foundry/Foundry-Kilo-Local" and d["enabled_providers"] == ["foundry"]
    assert d["mcp"]["foundry-media"]["command"][0] == "/srv/foundry/venv/bin/python"


def test_rewriter_handles_every_separator_and_keeps_neighbours():
    rw = move.Rewriter({r"C:\Roblox\AnimalUprising": "/srv/foundry/game/animaluprising",
                        r"C:\Users\shock/.foundry-coordinator": "/srv/foundry/state"})
    assert rw(r"C:\Roblox\AnimalUprising\.foundry-tracks\ux") == \
        "/srv/foundry/game/animaluprising/.foundry-tracks/ux"
    assert rw("c:/roblox/animaluprising/src/x.luau") == "/srv/foundry/game/animaluprising/src/x.luau"
    assert rw('{"p": "C:\\\\Roblox\\\\AnimalUprising\\\\docs"}') == \
        '{"p": "/srv/foundry/game/animaluprising/docs"}'
    assert rw(r"C:\Roblox\AnimalUprising-precode\x") == r"C:\Roblox\AnimalUprising-precode\x"
    assert rw(r"C:\Users\shock\.foundry-coordinator\config\animaluprising.json") == \
        "/srv/foundry/state/config/animaluprising.json"
    assert rw("no paths here") == "no paths here"


def test_rewrite_sqlite_touches_text_only_and_skips_fts(tmp_path):
    db = tmp_path / "c.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tracks (name TEXT, worktree TEXT, n INTEGER)")
    con.execute("INSERT INTO tracks VALUES ('ux', 'C:\\Roblox\\AnimalUprising\\.foundry-tracks\\ux', 3)")
    con.execute("CREATE VIRTUAL TABLE memory_fts USING fts5(body)")
    con.execute("INSERT INTO memory_fts VALUES ('C:\\Roblox\\AnimalUprising')")
    con.commit()
    con.close()
    out = move.rewrite_sqlite(str(db), move.Rewriter({r"C:\Roblox\AnimalUprising": "/g"}))
    assert out == {"tracks.worktree": 1}
    con = sqlite3.connect(db)
    assert con.execute("SELECT worktree, n FROM tracks").fetchone() == ("/g/.foundry-tracks/ux", 3)


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True)
    game = tmp_path / "old" / "game"
    subprocess.run(["git", "clone", "-q", str(origin), str(game)], check=True,
                   capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(game, "config", k, v)
    (game / "a.txt").write_text("1")
    _git(game, "add", ".")
    _git(game, "commit", "-qm", "one")
    _git(game, "push", "-q", "origin", "master")
    # a track branch that was never pushed, in a worktree under the game
    _git(game, "worktree", "add", "-q", "-b", "track/ux", str(game / ".foundry-tracks" / "ux"))
    wt = game / ".foundry-tracks" / "ux"
    (wt / "b.txt").write_text("2")
    _git(wt, "add", ".")
    _git(wt, "commit", "-qm", "track work")
    (wt / "kilo.jsonc").write_text(json.dumps({"mcp": {
        "Roblox_Studio": {"type": "local", "command": ["C:\\Windows\\System32\\cmd.exe", "/c", "x.bat"]},
        "searxng": {"type": "remote", "url": "http://x/mcp"}}}))
    return origin, game, wt


def test_export_then_import_carries_state_branches_and_worktrees(tmp_path, filestore):
    origin, game, wt = _repo(tmp_path)
    state = tmp_path / "old" / "state"
    (state / "config").mkdir(parents=True)
    (state / "config" / "animaluprising.json").write_text(json.dumps({"p": str(game)}))
    (state / "coordinator.pid").write_text("1")                 # skipped
    (state / "repairs").mkdir()
    (state / "repairs" / "x").write_text("skipped")
    con = sqlite3.connect(state / "coordinator.sqlite")
    con.execute("CREATE TABLE tracks (name TEXT, worktree TEXT)")
    con.execute("INSERT INTO tracks VALUES ('ux', ?)", (str(wt),))
    con.commit()
    con.close()
    profiles = tmp_path / "old" / "profiles"
    (profiles / "animaluprising").mkdir(parents=True)
    (profiles / "animaluprising" / "project.toml").write_text(
        f'id = "animaluprising"\n[workspace]\npath = "{game.as_posix()}"\n')
    cfgp = tmp_path / "old-client.toml"
    cfg = config._merge(config.DEFAULTS, {
        "client": {"state_dir": str(state)},
        "workspace": {"game": str(game), "profiles": str(profiles), "project": "animaluprising"}})
    config.save(cfg, cfgp)
    out = tmp_path / "move.fwx"
    released = []
    # refuses with uncommitted changes to tracked files
    (game / "a.txt").write_text("changed")
    with pytest.raises(move.MoveError, match="uncommitted"):
        move.export(str(out), "a long passphrase", cfgp)
    _git(game, "checkout", "--", "a.txt")
    res = move.export(str(out), "a long passphrase", cfgp, release=lambda p: released.append(p) or {})
    assert res["bundle"] is True and res["worktrees"] == 2 and released == ["animaluprising"]
    assert res["coordinator_switched_off"] is True
    assert config.load(cfgp)["components"]["coordinator"]["enabled"] is False
    assert not (tmp_path / "guard-client.toml").exists()
    # the export never holds the pid file or repair worktrees, and is encrypted
    blob = out.read_bytes()
    assert b"track work" not in blob and b"coordinator.pid" not in blob
    # the new workstation: a fresh clone (only master), then import
    root = tmp_path / "srv"
    L = W.layout(str(root), "animaluprising")
    subprocess.run(["git", "clone", "-q", str(origin), L["game"]], check=True, capture_output=True)
    os.makedirs(os.path.join(L["profiles"], "animaluprising"))
    new_cfg = config._merge(config.DEFAULTS, {"client": {"state_dir": L["state"]},
                                              "workspace": {"game": L["game"]}})
    with pytest.raises(Exception, match="wrong passphrase"):
        move.import_(str(out), "not the passphrase", str(root), new_cfg)
    r = move.import_(str(out), "a long passphrase", str(root), new_cfg)
    assert "track/ux" in r["branches"] and not r["branches_missing"]
    newwt = L["game"] + "/.foundry-tracks/ux"
    assert r["worktrees"] == [{"path": newwt, "branch": "track/ux", "ok": True, "error": ""}]
    assert Path(newwt, "b.txt").read_text() == "2"
    con = sqlite3.connect(os.path.join(L["state"], "coordinator.sqlite"))
    assert con.execute("SELECT worktree FROM tracks").fetchone()[0] == newwt
    cfgcache = json.loads(Path(L["state"], "config", "animaluprising.json").read_text())
    assert cfgcache["p"] == L["game"]
    assert not Path(L["state"], "coordinator.pid").exists()
    assert not Path(L["state"], "repairs").exists()
    ptoml = Path(L["profiles"], "animaluprising", "project.toml").read_text()
    assert f'path = "{L["game"]}"' in ptoml
    k = json.loads(Path(newwt, "kilo.jsonc").read_text())
    assert k["mcp"]["Roblox_Studio"]["enabled"] is False and "enabled" not in k["mcp"]["searxng"]
    assert r["kilo_servers_off"] == {newwt: ["Roblox_Studio"]}


def test_export_refuses_while_the_coordinator_runs(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "coordinator.heartbeat").write_text(json.dumps({"ts": 1000}))
    assert move.coordinator_running(str(state), clock=lambda: 1030) is True
    assert move.coordinator_running(str(state), clock=lambda: 5000) is False
