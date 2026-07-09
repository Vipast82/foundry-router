"""Ollama-API facade shape tests — the compatibility layer clients depend on
and the easiest thing to silently break."""

import json


def test_root_matches_real_ollama(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.text == "Ollama is running"  # some clients string-match this


def test_version_shape(client):
    r = client.get("/api/version")
    assert r.status_code == 200
    assert "version" in r.json()


def test_tags_advertises_personas_with_stub_fields(client):
    r = client.get("/api/tags")
    assert r.status_code == 200
    models = r.json()["models"]
    names = {m["name"] for m in models}
    assert {"Foundry-Coding", "Foundry-Chat", "Foundry-Research", "Foundry-RAG"} <= names
    for m in models:
        # §4.8: clients may expect size/digest/modified_at even though they're
        # meaningless for a virtual entry — must be present, not omitted.
        for field in ("name", "model", "modified_at", "size", "digest", "details"):
            assert field in m, f"missing stub field {field}"
        assert m["details"]["family"] == "foundry-router"


def test_show_persona(client):
    r = client.post("/api/show", json={"model": "Foundry-Chat"})
    assert r.status_code == 200
    body = r.json()
    assert "modelfile" in body
    assert "tools" in body["capabilities"]


def test_persona_lookup_tolerates_latest_suffix(client):
    r = client.post("/api/show", json={"model": "Foundry-Chat:latest"})
    assert r.status_code == 200


def test_unknown_model_404s_like_ollama(client):
    r = client.post("/api/chat", json={
        "model": "does-not-exist",
        "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 404
    assert "not found" in r.json()["error"]


def test_ps_stub(client):
    assert client.get("/api/ps").json() == {"models": []}
