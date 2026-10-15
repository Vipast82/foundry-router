"""Saving the backend list (or brain settings) from the admin UI must not
replace stored API keys with the masked values the UI displays."""


def test_masked_backend_key_round_trip_keeps_the_real_key(app, client, monkeypatch):
    svc = app.state.services
    monkeypatch.setattr(svc, "rebuild_pool", _noop)
    ok = client.post("/admin/api/config/backends", json=[
        {"name": "meridian", "type": "anthropic-compatible", "url": "http://m:3456",
         "api_key": "ca-real-secret-123"}])
    assert ok.status_code == 200
    shown = client.get("/admin/api/config").json()["backend_pool"]["internal"]["backends"]
    assert shown[0]["api_key"] == "ca***"                     # what the editor displays
    shown.append({"name": "strata", "type": "openai-compatible", "flavor": "llamacpp",
                  "url": "http://192.168.0.206:34201", "api_key": None})
    assert client.post("/admin/api/config/backends", json=shown).status_code == 200
    saved = {b.name: b for b in svc.config_store.config.backend_pool.internal.backends}
    assert saved["meridian"].api_key == "ca-real-secret-123"   # not "ca***"
    assert saved["strata"].api_key is None and saved["strata"].flavor == "llamacpp"


def test_a_newly_typed_key_still_replaces_the_stored_one(app, client, monkeypatch):
    svc = app.state.services
    monkeypatch.setattr(svc, "rebuild_pool", _noop)
    client.post("/admin/api/config/backends", json=[
        {"name": "m", "type": "anthropic-compatible", "url": "http://m", "api_key": "old-key-1"}])
    client.post("/admin/api/config/backends", json=[
        {"name": "m", "type": "anthropic-compatible", "url": "http://m", "api_key": "new-key-2"}])
    assert svc.config_store.config.backend_pool.internal.backends[0].api_key == "new-key-2"


def test_masked_brain_key_is_ignored(app, client, monkeypatch):
    svc = app.state.services
    monkeypatch.setattr(svc, "rebuild_brain", lambda: None)
    client.post("/admin/api/config/brain", json={"api_key": "sk-brain-real"})
    client.post("/admin/api/config/brain", json={"api_key": "sk***", "max_tokens": 4000})
    assert svc.config_store.config.agent_brain.api_key == "sk-brain-real"
    assert svc.config_store.config.agent_brain.max_tokens == 4000


async def _noop():
    return None
