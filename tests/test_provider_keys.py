"""Provider keys (provider_keys.py): provider-keys.env beside config.yaml, write-only."""

import os

from foundry_router import provider_keys as P


def test_file_roundtrip_and_validation(tmp_path):
    P.set_value(tmp_path, "MERIDIAN_API_KEY", "test-value-1")
    P.set_value(tmp_path, "OTHER_KEY", "test-value-2")
    assert P.read(tmp_path) == {"MERIDIAN_API_KEY": "test-value-1", "OTHER_KEY": "test-value-2"}
    if os.name != "nt":
        assert (os.stat(P.path_for(tmp_path)).st_mode & 0o777) == 0o600
    assert P.clear(tmp_path, "OTHER_KEY") and not P.clear(tmp_path, "OTHER_KEY")
    for bad_name in ("lower", "1X", "A-B"):
        try:
            P.set_value(tmp_path, bad_name, "v")
            raise AssertionError("accepted " + bad_name)
        except P.ProviderKeyError:
            pass
    for bad_value in ("", "two\nlines"):
        try:
            P.set_value(tmp_path, "X_KEY", bad_value)
            raise AssertionError("accepted a bad value")
        except P.ProviderKeyError:
            pass


def test_refs_and_status_show_no_values(tmp_path, monkeypatch):
    raw = {"backend_pool": {"internal": {"backends": [
        {"name": "meridian", "api_key": "${MERIDIAN_API_KEY}"},
        {"name": "cp", "api_key": "${CLIPROXY_API_KEY}"}]}}}
    assert P.refs(raw)["MERIDIAN_API_KEY"] == ["backend_pool.internal.backends.meridian.api_key"]
    monkeypatch.delenv("MERIDIAN_API_KEY", raising=False)
    monkeypatch.setenv("CLIPROXY_API_KEY", "from-env-value")
    P.set_value(tmp_path, "MERIDIAN_API_KEY", "from-file-value")
    rows = {r["name"]: r for r in P.status(raw, tmp_path)}
    assert rows["MERIDIAN_API_KEY"]["source"] == "file"
    assert rows["CLIPROXY_API_KEY"]["source"] == "environment"
    assert rows["OPENROUTER_API_KEY"]["source"] == "unset"
    assert "from-file-value" not in repr(rows) and "from-env-value" not in repr(rows)


def test_api_sets_key_and_backend_uses_it(app, client, monkeypatch):
    monkeypatch.delenv("MERIDIAN_API_KEY", raising=False)
    svc = app.state.services

    def mutate(raw):
        raw.setdefault("backend_pool", {}).setdefault("internal", {})["backends"] = [
            {"name": "meridian", "type": "anthropic-compatible", "url": "http://127.0.0.1:9",
             "api_key": "${MERIDIAN_API_KEY}"}]
    svc.config_store.save(mutate)
    r = client.put("/admin/api/provider-keys/MERIDIAN_API_KEY", json={"value": "test-key-abc123"})
    assert r.status_code == 200, r.text
    assert "test-key-abc123" not in r.text
    b = svc.config_store.config.backend_pool.internal.backends[0]
    assert b.api_key == "test-key-abc123"
    # config.yaml keeps the reference, never the value
    assert "test-key-abc123" not in svc.config_store.path.read_text(encoding="utf-8")
    listing = client.get("/admin/api/provider-keys").json()
    assert {r["name"]: r["source"] for r in listing["keys"]}["MERIDIAN_API_KEY"] == "file"
    assert "test-key-abc123" not in str(listing)
    from foundry_router.configops import secret_values
    assert "test-key-abc123" in secret_values(svc)
    # a key the host .env sets is not overridden from the page
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-wins-value")
    assert client.put("/admin/api/provider-keys/OPENROUTER_API_KEY",
                      json={"value": "x" * 10}).status_code == 409
    assert client.delete("/admin/api/provider-keys/MERIDIAN_API_KEY").json()["removed"]
    assert svc.config_store.config.backend_pool.internal.backends[0].api_key == ""


def test_fresh_config_opens_the_wizard_and_finishing_closes_it(tmp_path):
    from foundry_router.main import create_app
    app = create_app(config_path=tmp_path / "c" / "config.yaml", database_path=tmp_path / "t.sqlite")
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        assert c.get("/admin/api/status").json()["setup_completed"] is False
        w = c.get("/admin/api/setup/wizard").json()
        assert w["completed"] is False and w["fresh"] is True
        assert set(w["steps"]) == {"admin", "secrets", "backends", "personas", "budgets",
                                   "game", "machines"}
        assert any(b["example"] for b in w["steps"]["backends"]["backends"])
        assert c.post("/admin/api/setup/wizard", json={"completed": True}).json()["completed"]
        assert c.get("/ui/setup").status_code == 200
    # an existing config without a setup section counts as set up
    app2 = create_app(config_path=tmp_path / "c" / "config.yaml", database_path=tmp_path / "t.sqlite")
    assert app2.state.services.config_store.config.setup.completed is True
