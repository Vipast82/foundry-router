"""OpenAI-compatible facade: GET /v1/models + POST /v1/chat/completions, so
OpenAI-protocol clients (e.g. PhishGuard) can use Foundry. Reuses the same
persona routing as the Ollama facade — a persona name is the OpenAI `model`.

The test brain is unreachable (conftest points it at a dead port), so a chat
exercises the brain-down fallback and still returns a well-formed OpenAI
response — which is exactly what we assert (shape, not model quality)."""

import json


def _persona(client, name="Foundry-Chat"):
    client.post("/admin/api/personas",
                json={"virtual_name": name, "benchmark_category": "general_chat"})
    return name


def test_v1_models_lists_personas_in_openai_shape(client):
    _persona(client)
    body = client.get("/v1/models").json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert "Foundry-Chat" in ids
    assert all(m["object"] == "model" for m in body["data"])


def test_v1_models_empty_is_still_valid_list(client):
    body = client.get("/v1/models").json()
    assert body["object"] == "list" and isinstance(body["data"], list)


def test_unknown_model_returns_openai_error(client):
    r = client.post("/v1/chat/completions",
                    json={"model": "does-not-exist", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


def test_chat_completion_non_stream_shape(client):
    _persona(client)
    r = client.post("/v1/chat/completions", json={
        "model": "Foundry-Chat",
        "messages": [{"role": "user", "content": "classify this"}]})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    msg = body["choices"][0]["message"]
    assert msg["role"] == "assistant" and isinstance(msg["content"], str)
    assert body["choices"][0]["finish_reason"] == "stop"
    assert "usage" in body


def test_chat_completion_stream_is_sse_with_done(client):
    _persona(client)
    with client.stream("POST", "/v1/chat/completions", json={
            "model": "Foundry-Chat", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        lines = [ln for ln in r.iter_lines()]
    data = [ln[len("data: "):] for ln in lines if ln.startswith("data: ")]
    assert data and data[-1] == "[DONE]"
    first = json.loads(data[0])
    assert first["object"] == "chat.completion.chunk"
    assert first["choices"][0]["delta"].get("role") == "assistant"
