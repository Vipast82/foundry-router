"""contrib/studio-slot/slot-secrets.py: pulls a slot's bundle into private files, keeps them
when the router is away, removes them when the key is revoked, pins the HTTPS certificate."""

import importlib.util
import json
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VALUE = "test-value-not-a-real-key-0123456789"


def _mod():
    spec = importlib.util.spec_from_file_location("slot_secrets",
                                                  ROOT / "contrib/studio-slot/slot-secrets.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Fake:
    def __init__(self):
        self.status = 200
        self.secrets = [{"name": "ROBLOX_OPEN_CLOUD_KEY", "file": "roblox_open_cloud_key.txt",
                         "value": VALUE, "set_at": 1_800_000_000}]
        self.config = {"place_id": 116058790793121, "universe_id": 10773901724,
                       "rojo_never_place_ids": [138024886323256]}
        self.seen = []

    def handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                  "inm": self.headers.get("If-None-Match")})
                body = json.dumps({"secrets": fake.secrets, "config": fake.config}).encode()
                etag = '"%d"' % (hash(body) & 0xffffff)
                if fake.status != 200:
                    body = json.dumps({"error": {"message": "nope", "code": "x"}}).encode()
                    self.send_response(fake.status)
                elif self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                else:
                    self.send_response(200)
                    self.send_header("ETag", etag)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
        return H


@pytest.fixture()
def server():
    fake = Fake()
    srv = HTTPServer(("127.0.0.1", 0), fake.handler())
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    fake.url = f"http://127.0.0.1:{srv.server_port}"
    fake.srv = srv
    yield fake
    srv.shutdown()


@pytest.fixture()
def slot_env(tmp_path, monkeypatch):
    key = tmp_path / "slot-key.txt"
    key.write_text("fnd_test_slot_key_for_unit_tests\n")
    out = tmp_path / "run-secrets"
    monkeypatch.setenv("FOUNDRY_SLOT_KEY_FILE", str(key))
    monkeypatch.setenv("FOUNDRY_SECRETS_DIR", str(out))
    monkeypatch.setenv("FOUNDRY_PROJECT", "animaluprising")
    monkeypatch.setenv("FOUNDRY_SLOT", "slot-1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    return out


def test_pull_writes_private_files_and_slot_env(server, slot_env, monkeypatch, capsys):
    monkeypatch.setenv("FOUNDRY_URL", server.url)
    m = _mod()
    assert m.pull(allow_disk=True) == 0
    f = slot_env / "roblox_open_cloud_key.txt"
    assert f.read_text() == VALUE
    if os.name != "nt":
        assert (f.stat().st_mode & 0o777) == 0o600
    assert server.seen[0]["auth"] == "Bearer fnd_test_slot_key_for_unit_tests"
    assert "project=animaluprising" in server.seen[0]["path"] and "slot=slot-1" in server.seen[0]["path"]
    env = (Path(os.environ["HOME"]) / ".config/foundry-slot/slot.env").read_text()
    assert "placeId:116058790793121+universeId:10773901724" in env
    assert "ROJO_NEVER_PLACE_IDS='138024886323256'" in env
    err = capsys.readouterr().err
    assert VALUE not in err and "ROBLOX_OPEN_CLOUD_KEY" in err
    # second pull: If-None-Match, 304, files kept
    assert m.pull(allow_disk=True) == 0
    assert server.seen[1]["inm"] and f.read_text() == VALUE


def test_refuses_disk_unless_tmpfs(server, slot_env, monkeypatch):
    monkeypatch.setenv("FOUNDRY_URL", server.url)
    monkeypatch.delenv("FOUNDRY_SECRETS_ALLOW_DISK", raising=False)
    m = _mod()
    monkeypatch.setattr(m, "on_tmpfs", lambda p: False)
    assert m.pull() == 4 and not server.seen


def test_unreachable_keeps_files_revoked_removes_them(server, slot_env, monkeypatch):
    monkeypatch.setenv("FOUNDRY_URL", server.url)
    m = _mod()
    assert m.pull(allow_disk=True) == 0
    f = slot_env / "roblox_open_cloud_key.txt"
    server.status = 503                                       # store locked
    assert m.pull(allow_disk=True) == 0 and f.exists()
    monkeypatch.setenv("FOUNDRY_URL", "http://127.0.0.1:1")   # router away
    assert m.pull(allow_disk=True) == 0 and f.exists()
    monkeypatch.setenv("FOUNDRY_URL", server.url)
    server.status = 403                                       # key disabled
    assert m.pull(allow_disk=True) == 3 and not f.exists()


def test_a_secret_no_longer_served_is_removed(server, slot_env, monkeypatch):
    monkeypatch.setenv("FOUNDRY_URL", server.url)
    m = _mod()
    server.secrets.append({"name": "OTHER", "file": "other.txt", "value": "x" * 12, "set_at": 1})
    assert m.pull(allow_disk=True) == 0 and (slot_env / "other.txt").exists()
    server.secrets.pop()
    server.config = {}
    assert m.pull(allow_disk=True) == 0
    assert not (slot_env / "other.txt").exists()
    assert (slot_env / "roblox_open_cloud_key.txt").exists()


def test_not_configured_does_nothing(slot_env, monkeypatch):
    monkeypatch.delenv("FOUNDRY_URL", raising=False)
    assert _mod().pull(allow_disk=True) == 2


def test_https_pins_the_router_certificate(server, slot_env, tmp_path, monkeypatch):
    from foundry_router.tls import ensure_cert
    cert, key, fp = ensure_cert(tmp_path / "data")
    srv = HTTPServer(("127.0.0.1", 0), server.handler())
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        m = _mod()
        monkeypatch.setenv("FOUNDRY_URL", f"https://127.0.0.1:{srv.server_port}")
        monkeypatch.delenv("FOUNDRY_TLS_FINGERPRINT", raising=False)
        assert m.pull(allow_disk=True) == 0 and not server.seen      # no fingerprint: no request
        monkeypatch.setenv("FOUNDRY_TLS_FINGERPRINT", "00:" * 31 + "00")
        assert m.pull(allow_disk=True) == 0 and not server.seen      # wrong one: no request
        monkeypatch.setenv("FOUNDRY_TLS_FINGERPRINT", fp)
        assert m.pull(allow_disk=True) == 0 and len(server.seen) == 1
        assert (slot_env / "roblox_open_cloud_key.txt").read_text() == VALUE
    finally:
        srv.shutdown()
