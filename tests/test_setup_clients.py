"""Setup → Clients & keys and Helpers & limits: keys made in the web UI work,
are stored only as a hash, rotate and delete cleanly; unknown keys are
listed (by fingerprint, never the key) and can be allowed through during a
rollout; the UI can't lock itself out; backend limits save from a form."""

import yaml

from foundry_router import client_keys
from foundry_router.config import BackendConfig


def _models(client, key):
    return client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})


def test_create_use_rotate_and_delete_a_client(app, client):
    svc = app.state.services
    r = client.post("/admin/api/setup/clients", json={"name": "cline", "scopes": ["inference"],
                                                      "note": "VS Code"})
    assert r.status_code == 200
    key = r.json()["key"]
    assert key.startswith("fnd_") and len(key) > 40
    # stored hashed: never in config.yaml, the config API or history
    text = svc.config_store.path.read_text()
    assert key not in text and client_keys.hash_key(key) in text
    assert key not in client.get("/admin/api/config").text
    assert _models(client, key).status_code == 200
    view = client.get("/admin/api/setup/clients").json()
    c, = [x for x in view["clients"] if x["name"] == "cline"]
    assert c["key_kind"] == "managed" and c["key_hint"].startswith("fnd_") and key not in str(view)
    assert c["seen"]["count"] >= 1
    # rotate: the old key stops working at once
    new = client.post("/admin/api/setup/clients/cline/rotate").json()["key"]
    assert new != key
    assert _models(client, key).status_code == 401 and _models(client, new).status_code == 200
    # disable, then delete
    assert client.patch("/admin/api/setup/clients/cline", json={"enabled": False}).status_code == 200
    assert _models(client, new).status_code == 401
    assert client.delete("/admin/api/setup/clients/cline").status_code == 200
    assert not [x for x in client.get("/admin/api/setup/clients").json()["clients"]
                if x["name"] == "cline"]


def test_names_and_scopes_are_validated(client):
    assert client.post("/admin/api/setup/clients", json={"name": "bad name!"}).status_code == 400
    assert client.post("/admin/api/setup/clients", json={"name": "x", "scopes": ["root"]}).status_code == 400
    assert client.post("/admin/api/setup/clients", json={"name": "kilo"}).status_code == 200
    assert client.post("/admin/api/setup/clients", json={"name": "kilo"}).status_code == 409


def test_unknown_keys_are_listed_by_fingerprint_and_can_be_allowed(app, client):
    client_keys.activity.forget_rejected()
    client.post("/admin/api/setup/clients", json={"name": "kilo"})
    wrong = "sk-placeholder-from-cline-123456"
    r = _models(client, wrong)
    assert r.status_code == 401 and "Clients & keys" in r.json()["error"]["message"]
    rej = client.get("/admin/api/setup/clients").json()["rejected"]
    assert rej[0]["fingerprint"] == client_keys.fingerprint(wrong)
    assert rej[0]["looks_like"] == "an OpenAI-style key" and rej[0]["outcome"] == "refused"
    assert wrong not in str(rej)
    # rollout switch: unknown keys pass as anonymous in optional mode
    assert client.post("/admin/api/setup/client_auth",
                       json={"unknown_keys": "anonymous"}).status_code == 200
    assert _models(client, wrong).status_code == 200
    # ...never in required mode
    client.post("/admin/api/setup/client_auth", json={"mode": "required"})
    assert _models(client, wrong).status_code == 401
    assert client.get("/v1/models").status_code == 401
    client.post("/admin/api/setup/client_auth", json={"mode": "optional", "unknown_keys": "refuse"})


def test_web_ui_protection_cannot_lock_you_out(app, client):
    r = client.post("/admin/api/setup/client_auth", json={"protect_admin": True})
    assert r.status_code == 409                      # this browser has no admin key yet
    key = client.post("/admin/api/setup/clients", json={"name": "admin", "scopes": ["admin"]}).json()["key"]
    h = {"Authorization": f"Bearer {key}"}
    assert client.post("/admin/api/setup/client_auth", json={"protect_admin": True},
                       headers=h).status_code == 200
    assert client.get("/admin/api/setup/clients").status_code == 401
    # the last admin key can't be removed or demoted while protection is on
    assert client.delete("/admin/api/setup/clients/admin", headers=h).status_code == 409
    assert client.patch("/admin/api/setup/clients/admin", json={"scopes": ["inference"]},
                        headers=h).status_code == 409
    assert client.post("/admin/api/setup/client_auth", json={"protect_admin": False},
                       headers=h).status_code == 200


def test_config_written_keys_still_work_and_rotate_into_managed(app, client):
    svc = app.state.services

    def mutate(raw):
        raw["clients"] = [{"name": "legacy", "key": "legacy-key-123456"}]
    svc.config_store.save(mutate)
    assert _models(client, "legacy-key-123456").status_code == 200
    c, = client.get("/admin/api/setup/clients").json()["clients"]
    assert c["key_kind"] == "config"
    new = client.post("/admin/api/setup/clients/legacy/rotate").json()["key"]
    assert _models(client, "legacy-key-123456").status_code == 401
    assert _models(client, new).status_code == 200
    raw = yaml.safe_load(svc.config_store.path.read_text())
    assert "key" not in raw["clients"][0] and raw["clients"][0]["key_hash"].startswith("sha256:")


def test_backend_limits_save_from_the_form(app, client):
    svc = app.state.services

    def mutate(raw):
        raw.setdefault("backend_pool", {}).setdefault("internal", {})["backends"] = [
            {"name": "strata", "type": "openai-compatible", "url": "http://127.0.0.1:9"}]
    svc.config_store.save(mutate)
    r = client.post("/admin/api/setup/limits", json={"name": "strata", "helpers_allowed": 2,
                                                     "max_concurrent": 3,
                                                     "queue_timeout_seconds": 120})
    assert r.status_code == 200, r.text
    b, = client.get("/admin/api/setup/limits").json()["backends"]
    assert (b["helpers_allowed"], b["max_concurrent"], b["queue_timeout_seconds"]) == (2, 3, 120)
    # empty = no limit
    client.post("/admin/api/setup/limits", json={"name": "strata", "helpers_allowed": ""})
    assert client.get("/admin/api/setup/limits").json()["backends"][0]["helpers_allowed"] is None
    assert client.post("/admin/api/setup/limits", json={"name": "strata",
                                                        "helpers_allowed": -1}).status_code == 400
    assert client.post("/admin/api/setup/limits", json={"name": "nope"}).status_code == 404
    assert isinstance(svc.config_store.config.backend_pool.internal.backends[0], BackendConfig)
