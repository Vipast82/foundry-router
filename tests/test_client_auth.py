"""Client keys, scopes, persona restrictions and correlation ids."""

import pytest

from foundry_router.config import ClientAuthConfig, ClientConfig


@pytest.fixture()
def keyed(app, client):
    svc = app.state.services
    cfg = svc.config_store.config
    saved = (cfg.clients, cfg.client_auth)
    cfg.clients = [
        ClientConfig(name="kilo", key="kilo-key", scopes=["inference", "media"],
                     personas=["Kilo-Local"]),
        ClientConfig(name="openwebui", key="owui-key"),
        ClientConfig(name="admin", key="admin-key", scopes=["inference", "media", "admin"]),
        ClientConfig(name="off", key="off-key", enabled=False)]
    cfg.client_auth = ClientAuthConfig()
    svc.personas.upsert("Kilo-Local", execution_mode="direct", model_allowlist=["x"])
    svc.personas.upsert("Other-P", execution_mode="direct", model_allowlist=["x"])
    yield svc
    cfg.clients, cfg.client_auth = saved


def _ids(r):
    return {m["id"] for m in r.json()["data"]}


def test_optional_mode_keeps_keyless_clients_working(keyed, client):
    r = client.get("/v1/models")
    assert r.status_code == 200 and {"Kilo-Local", "Other-P"} <= _ids(r)
    assert client.get("/api/tags").status_code == 200
    assert client.get("/admin/api/status").status_code != 401


def test_wrong_or_disabled_key_is_refused_never_anonymous(keyed, client):
    for k in ("typo-key", "off-key"):
        r = client.get("/v1/models", headers={"Authorization": f"Bearer {k}"})
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"
        assert r.headers.get("x-request-id")


def test_client_persona_restriction_filters_and_enforces(keyed, client):
    h = {"Authorization": "Bearer kilo-key"}
    assert _ids(client.get("/v1/models", headers=h)) == {"Kilo-Local"}
    names = {m["name"] for m in client.get("/api/tags", headers=h).json()["models"]}
    assert names == {"Kilo-Local"}
    r = client.post("/v1/chat/completions", headers=h, json={
        "model": "Other-P", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 403 and r.json()["error"]["code"] == "model_not_allowed"
    # x-api-key works as well (Anthropic-style clients)
    r = client.post("/api/chat", headers={"x-api-key": "kilo-key"}, json={
        "model": "some-raw-backend-model", "stream": False,
        "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 403


def test_required_mode_and_scopes(keyed, client):
    keyed.config_store.config.client_auth = ClientAuthConfig(mode="required", protect_admin=True)
    r = client.get("/v1/models")
    assert r.status_code == 401 and r.json()["error"]["code"] == "missing_api_key"
    assert client.get("/v1/models", headers={"Authorization": "Bearer owui-key"}).status_code == 200
    # media scope
    r = client.post("/v1/images/generations", headers={"Authorization": "Bearer owui-key"},
                    json={"prompt": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_scope"
    # admin protected
    r = client.get("/admin/api/status", headers={"Authorization": "Bearer owui-key"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "admin_key_required"
    assert client.get("/admin/api/status").status_code == 401
    assert client.get("/admin/api/status",
                      headers={"Authorization": "Bearer admin-key"}).status_code == 200
    # liveness probe and the static UI shell stay open
    assert client.get("/api/version").status_code == 200
    assert client.get("/ui").status_code == 200


def test_client_and_correlation_ids_recorded(keyed, client):
    client.post("/v1/chat/completions", headers={
        "Authorization": "Bearer owui-key", "x-foundry-session": "sess-1",
        "x-foundry-task": "task-9", "x-foundry-parent-task": "task-1"},
        json={"model": "Other-P", "messages": [{"role": "user", "content": "x"}]})
    row = keyed.db.query("SELECT client_id, session_id, task_id, parent_task_id "
                         "FROM request_log ORDER BY id DESC LIMIT 1")[0]
    assert (row["client_id"], row["session_id"], row["task_id"], row["parent_task_id"]) == \
        ("openwebui", "sess-1", "task-9", "task-1")


def test_hermes_agent_token_still_accepted(keyed, client):
    from foundry_router import request_context
    request_context.set_agent_tokens({"hermes-tok": "hermes"})
    try:
        keyed.config_store.config.client_auth = ClientAuthConfig(mode="required")
        r = client.get("/v1/models", headers={"Authorization": "Bearer hermes-tok"})
        assert r.status_code == 200
    finally:
        request_context.set_agent_tokens({})
