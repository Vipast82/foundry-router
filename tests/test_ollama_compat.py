"""Ollama API compatibility of the facade (what Cline / Open WebUI / AnythingLLM
talk to): real backend timings and done_reason reach the client, raw-model
passthrough forwards think/format/keep_alive and streams reasoning + tools,
/api/generate shares the chat path (stats, options, preload), and /api/ps,
/api/embed, /api/embeddings, HEAD / and raw-model /api/show exist."""

import json

from foundry_router.facade import translate as tr
from foundry_router.pool.protocols import ChatResult

RES = dict(content="hello", thinking="hmm", prompt_tokens=120, completion_tokens=40,
           eval_duration_ns=800_000_000, prompt_eval_duration_ns=60_000_000,
           load_duration_ns=5_000_000, finish_reason="length", timing_source="server")


class FakePool:
    def __init__(self):
        self.calls = []

    def backend_info(self, m):
        return ({"name": "ollama-1", "type": "openai-compatible", "url": "http://x",
                 "flavor": "llamacpp"} if m in ("raw", "nomic") else None)

    def available_models(self):
        return {"raw": ["ollama-1"]}

    async def chat(self, model, messages, **kw):
        self.calls.append(("chat", kw))
        return ChatResult(**RES), "ollama-1"

    async def chat_stream(self, model, messages, **kw):
        self.calls.append(("stream", kw))
        yield {"content": "", "done": False, "thinking": "hmm"}
        yield {"content": "hel", "done": False}
        yield {"content": "", "done": False,
               "tool_calls": [{"id": "1", "name": "t", "arguments": {"a": 1}}]}
        yield {"content": "lo", "done": False}
        yield ChatResult(**RES).done_frame()

    async def loaded_models_detail(self):
        return [{"model": "raw", "size_vram": 8 * 2**30, "size": 9 * 2**30,
                 "context": 32768, "backend": "ollama-1", "expires_at": "x"}]

    async def embed(self, model, inputs, **kw):
        self.calls.append(("embed", kw))
        return {"embeddings": [[0.1, 0.2] for _ in inputs], "prompt_eval_count": 7,
                "total_duration": 0, "load_duration": 0}, "ollama-1"


def _swap(app):
    svc = app.state.services
    fake = FakePool()
    real, svc.pool = svc.pool, fake
    return svc, fake, real


def _lines(r):
    return [json.loads(x) for x in r.text.splitlines() if x.strip()]


def test_stats_pass_real_durations_and_done_reason():
    st = tr._stats(tr.result_stats(ChatResult(**RES), 1_000_000_000))
    assert st["eval_duration"] == 800_000_000            # decode time, not wall
    assert st["prompt_eval_duration"] == 60_000_000
    assert st["load_duration"] == 5_000_000
    assert st["done_reason"] == "length"                  # truncation visible
    assert st["eval_count"] == 40 and st["prompt_eval_count"] == 120
    # unknown decode time -> wall time stands in (never a divide-by-zero)
    assert tr._stats({"total_duration_ns": 9})["eval_duration"] == 9
    assert tr._stats({"done_reason": "tool_calls"})["done_reason"] == "stop"


def test_passthrough_non_stream_forwards_everything(app, client):
    svc, fake, real = _swap(app)
    try:
        r = client.post("/api/chat", json={
            "model": "raw", "stream": False, "think": "high", "format": "json",
            "keep_alive": "10m", "messages": [{"role": "user", "content": "hi"}]}).json()
    finally:
        svc.pool = real
    kw = fake.calls[0][1]
    assert kw["think"] == "high" and kw["fmt"] == "json" and kw["keep_alive"] == "10m"
    assert r["message"]["thinking"] == "hmm"
    assert r["done_reason"] == "length" and r["eval_duration"] == 800_000_000
    assert svc.db.query("SELECT * FROM perf_samples")


def test_passthrough_stream_carries_thinking_tools_and_stats(app, client):
    svc, fake, real = _swap(app)
    try:
        r = client.post("/api/chat", json={
            "model": "raw", "tools": [{"type": "function", "function": {"name": "t"}}],
            "messages": [{"role": "user", "content": "hi"},
                         {"role": "tool", "tool_name": "t", "content": "42"}]})
    finally:
        svc.pool = real
    lines = _lines(r)
    assert fake.calls[0][0] == "stream" and fake.calls[0][1]["tools"]
    assert any(l["message"].get("thinking") == "hmm" for l in lines)
    assert "".join(l["message"]["content"] for l in lines) == "hello"
    done = lines[-1]
    assert done["done"] and done["message"]["tool_calls"][0]["function"]["name"] == "t"
    assert done["done_reason"] == "length" and done["eval_duration"] == 800_000_000


def test_generate_uses_chat_path_with_stats_and_options(app, client):
    svc, fake, real = _swap(app)
    try:
        r = client.post("/api/generate", json={
            "model": "raw", "prompt": "hi", "stream": False, "system": "be brief",
            "options": {"temperature": 0.1}, "think": False}).json()
        s = _lines(client.post("/api/generate", json={"model": "raw", "prompt": "hi"}))
    finally:
        svc.pool = real
    assert fake.calls[0][1]["options"] == {"temperature": 0.1}
    assert fake.calls[0][1]["think"] is False
    assert r["response"] == "hello" and r["thinking"] == "hmm"
    assert r["eval_count"] == 40 and r["done_reason"] == "length"
    assert "".join(x["response"] for x in s) == "hello"
    assert s[-1]["done"] and s[-1]["eval_duration"] == 800_000_000


def test_generate_empty_prompt_is_load_or_unload(app, client):
    svc, fake, real = _swap(app)
    try:
        load = client.post("/api/generate", json={"model": "raw"}).json()
        unload = client.post("/api/generate", json={"model": "raw", "keep_alive": 0}).json()
    finally:
        svc.pool = real
    assert load["done_reason"] == "load" and unload["done_reason"] == "unload"
    assert fake.calls == []                                # no model call


def test_ps_lists_resident_models(app, client):
    svc, fake, real = _swap(app)
    try:
        m = client.get("/api/ps").json()["models"][0]
    finally:
        svc.pool = real
    assert m["name"] == "raw" and m["size_vram"] == 8 * 2**30
    assert m["context_length"] == 32768


def test_embed_and_legacy_embeddings(app, client):
    svc, fake, real = _swap(app)
    try:
        e = client.post("/api/embed", json={"model": "nomic", "input": ["a", "b"],
                                            "truncate": True}).json()
        legacy = client.post("/api/embeddings", json={"model": "nomic",
                                                      "prompt": "a"}).json()
        missing = client.post("/api/embed", json={"model": "nope", "input": "a"})
    finally:
        svc.pool = real
    assert len(e["embeddings"]) == 2 and e["prompt_eval_count"] == 7
    assert fake.calls[0][1]["truncate"] is True
    assert legacy["embedding"] == [0.1, 0.2]
    assert missing.status_code == 404


def test_head_root_and_raw_show(app, client):
    assert client.head("/").status_code == 200
    svc, fake, real = _swap(app)
    try:
        svc.registry.upsert_auto("raw", source="discovery", context_length=65536)
        show = client.post("/api/show", json={"model": "raw"}).json()
    finally:
        svc.pool = real
    assert show["model_info"]["general.context_length"] == 65536
    assert "tools" in show["capabilities"]


async def test_ollama_protocol_embed_and_ps_context():
    import httpx
    from foundry_router.pool.protocols import OllamaProtocol

    def h(request):
        if request.url.path == "/api/embed":
            body = json.loads(request.content)
            assert body["input"] == ["x"] and body["truncate"] is True
            return httpx.Response(200, json={"embeddings": [[1.0]], "prompt_eval_count": 3,
                                             "total_duration": 10, "load_duration": 2})
        return httpx.Response(200, json={"models": [
            {"name": "qwen3:32b", "size": 20, "size_vram": 18, "context_length": 16384}]})
    proto = OllamaProtocol("http://x", None, httpx.AsyncClient(transport=httpx.MockTransport(h)))
    e = await proto.embed("m", ["x"], truncate=True)
    assert e["embeddings"] == [[1.0]] and e["prompt_eval_count"] == 3
    d = (await proto.loaded_models_detail())[0]
    assert d["context"] == 16384 and d["size_vram"] == 18
