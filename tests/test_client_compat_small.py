"""Client-compatibility fixes from reports/CLIENT_COMPATIBILITY_AUDIT_2026-10-24.md:
C2 (thinking capability from the served models), C4 (embedding models listed),
C5 (images on the direct path go to a vision model or get a clear refusal),
B4 (/v1/audio/speech via the TTS MCP servers; no STT -> a clear 501)."""
import json
import types

from foundry_router.facade.ollama_api import _persona_capabilities, persona_meta


# --------------------------------------------------------------------------- #
# C2: thinking capability                                                      #
# --------------------------------------------------------------------------- #

def _fleet(models: dict, types_: dict):
    """svc stub: models = id -> registry row, types_ = id -> backend type."""
    reg = types.SimpleNamespace(get=lambda m: models.get(m))
    pool = types.SimpleNamespace(
        available_models=lambda: {m: ["b"] for m in models},
        backend_info=lambda m: ({"name": "b", "type": types_[m]} if m in types_ else None))
    return types.SimpleNamespace(pool=pool, registry=reg)


def test_llamacpp_reasoning_model_advertises_thinking():
    # Strata: openai-compatible, registry caps without `thinking` (the live case)
    svc = _fleet({"qwen3.8-flash-next-iq3_xxs": {"capabilities": json.dumps(["completion"])}},
                 {"qwen3.8-flash-next-iq3_xxs": "openai-compatible"})
    assert "thinking" in _persona_capabilities(svc, {})


def test_unknown_family_on_llamacpp_does_not_advertise_thinking():
    svc = _fleet({"mistral-small": {"capabilities": "[]"}},
                 {"mistral-small": "openai-compatible"})
    assert "thinking" not in _persona_capabilities(svc, {})


def test_ollama_model_still_needs_its_declared_capability():
    svc = _fleet({"qwen3:8b": {"capabilities": json.dumps(["completion"])},
                  "deepseek-r1:7b": {"capabilities": json.dumps(["completion", "thinking"])}},
                 {"qwen3:8b": "ollama", "deepseek-r1:7b": "ollama"})
    only_qwen = {"model_allowlist": json.dumps(["qwen3:8b"])}
    assert "thinking" not in _persona_capabilities(svc, only_qwen)
    assert "thinking" in _persona_capabilities(svc, {})


def test_claude_route_advertises_thinking():
    svc = _fleet({"claude-sonnet-5": {}}, {"claude-sonnet-5": "anthropic-compatible"})
    assert "thinking" in _persona_capabilities(svc, {})


def test_strict_persona_thinking_from_its_served_model(monkeypatch):
    from foundry_router.facade import ollama_api as oa
    svc = _fleet({"qwen3.8-flash-next-iq3_xxs": {"context_length": 262144}},
                 {"qwen3.8-flash-next-iq3_xxs": "openai-compatible"})
    monkeypatch.setattr(oa, "strict_candidates", lambda s, p: ["qwen3.8-flash-next-iq3_xxs"])
    p = {"virtual_name": "Kilo-Local", "routing": "strict",
         "model_allowlist": json.dumps(["qwen3.8-flash-next-iq3_xxs"])}
    meta = persona_meta(svc, p)
    assert "thinking" in meta["capabilities"]
    entry = oa.openai_model_entry(svc, p, 0)
    assert entry["capabilities"]["reasoning"] is True


# --------------------------------------------------------------------------- #
# C4: embedding models discoverable                                            #
# --------------------------------------------------------------------------- #

class _EmbedPool:
    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, k):
        return getattr(self.inner, k)

    def available_models(self):
        return {**self.inner.available_models(), "nomic-embed-text:latest": ["o1"],
                "bge-m3": ["o1"], "chat-model:7b": ["o1"]}

    def backend_info(self, m):
        if m in ("nomic-embed-text:latest", "bge-m3", "chat-model:7b"):
            return {"name": "o1", "type": "ollama", "url": "http://o"}
        return self.inner.backend_info(m)


def _with_embedders(app):
    svc = app.state.services
    old = svc.pool
    svc.pool = _EmbedPool(old)
    svc.registry.mark_embedding("bge-m3")          # flagged by the backend's word
    return svc, old


def test_api_tags_lists_embedding_models(app, client):
    svc, old = _with_embedders(app)
    try:
        models = client.get("/api/tags").json()["models"]
    finally:
        svc.pool = old
    by = {m["name"]: m for m in models}
    assert by["nomic-embed-text:latest"]["capabilities"] == ["embedding"]
    assert by["bge-m3"]["capabilities"] == ["embedding"]
    assert "chat-model:7b" not in by                     # raw chat models stay hidden
    assert all(m.get("capabilities") != ["embedding"] or m["name"] in
               ("nomic-embed-text:latest", "bge-m3") for m in models)


def test_v1_models_lists_embedding_models(app, client):
    svc, old = _with_embedders(app)
    try:
        data = client.get("/v1/models").json()["data"]
        one = client.get("/v1/models/bge-m3").json()
    finally:
        svc.pool = old
    by = {m["id"]: m for m in data}
    assert by["nomic-embed-text:latest"]["capabilities"]["embedding"] is True
    assert one["id"] == "bge-m3" and one["capabilities"]["embedding"] is True
    assert "chat-model:7b" not in by


def test_embedding_listing_can_be_switched_off(app, client):
    svc, old = _with_embedders(app)
    cfg = svc.config_store.config.server
    cfg.list_embedding_models = False
    try:
        names = {m["name"] for m in client.get("/api/tags").json()["models"]}
    finally:
        cfg.list_embedding_models = True
        svc.pool = old
    assert "nomic-embed-text:latest" not in names


def test_show_on_a_non_ollama_embedder_says_embedding(app, client):
    svc = app.state.services
    old = svc.pool

    class P(_EmbedPool):
        def backend_info(self, m):
            if m == "bge-m3":
                return {"name": "lc", "type": "openai-compatible", "flavor": "llamacpp",
                        "url": "http://l"}
            return super().backend_info(m)
    svc.pool = P(old)
    svc.registry.mark_embedding("bge-m3")
    try:
        body = client.post("/api/show", json={"model": "bge-m3"}).json()
    finally:
        svc.pool = old
    assert body["capabilities"] == ["embedding"]


# --------------------------------------------------------------------------- #
# B4: audio endpoints                                                          #
# --------------------------------------------------------------------------- #

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24


class _Resp:
    def __init__(self, data, status=200, ctype="audio/wav"):
        self.data, self.status_code = data, status
        self.headers = {"content-type": ctype}

    async def aiter_bytes(self):
        yield self.data[:10]
        yield self.data[10:]


class _Stream:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self.resp

    async def __aexit__(self, *a):
        return False


class _Http:
    def __init__(self, data=WAV):
        self.data, self.urls = data, []

    def stream(self, method, url, timeout=None):
        self.urls.append(url)
        return _Stream(_Resp(self.data))


def _tts(app, text_result="Saved: http://192.168.0.115:8880/files/out_1.wav", blocks=None):
    """Kokoro + Chatterbox as Foundry MCP servers on 192.168.0.115 returning a URL."""
    from foundry_router.config import MCPServerConfig
    from foundry_router.tools.mcp_client import ToolResult
    svc = app.state.services
    calls = []
    old = (dict(svc.mcp.servers), svc.mcp.call_tool_rich, svc.http)
    svc.mcp.servers["kokoro-tts"] = MCPServerConfig(name="kokoro-tts",
                                                    url="http://192.168.0.115:8770/mcp")
    svc.mcp.servers["chatterbox-tts"] = MCPServerConfig(name="chatterbox-tts",
                                                        url="http://192.168.0.115:8771/mcp")

    async def fake_rich(server, tool, args, **kw):
        calls.append((server, tool, args))
        return ToolResult(blocks or [{"type": "text", "text": text_result}])
    svc.mcp.call_tool_rich = fake_rich
    svc.http = _Http()

    def restore():
        svc.mcp.servers, svc.mcp.call_tool_rich, svc.http = old
    return svc, calls, restore


def test_speech_openai_shape_returns_wav_from_kokoro(app, client):
    svc, calls, restore = _tts(app)
    try:
        r = client.post("/v1/audio/speech", json={"model": "tts-1", "input": "Hello there",
                                                  "voice": "alloy", "speed": 1.25,
                                                  "response_format": "mp3"})
        urls = svc.http.urls
    finally:
        restore()
    assert r.status_code == 200
    assert r.content == WAV and r.headers["content-type"] == "audio/wav"
    assert calls == [("kokoro-tts", "generate_speech",
                      {"text": "Hello there", "voice": "af_heart", "speed": 1.25})]
    assert urls == ["http://192.168.0.115:8880/files/out_1.wav"]


def test_speech_kokoro_voice_passes_and_chatterbox_takes_text_only(app, client):
    svc, calls, restore = _tts(app)
    try:
        assert client.post("/v1/audio/speech", json={"model": "kokoro", "input": "a",
                                                     "voice": "bm_george"}).status_code == 200
        assert client.post("/v1/audio/speech", json={"model": "chatterbox", "input": "b",
                                                     "voice": "nova"}).status_code == 200
    finally:
        restore()
    assert calls[0][2] == {"text": "a", "voice": "bm_george"}
    assert calls[1][:2] == ("chatterbox-tts", "generate_speech") and calls[1][2] == {"text": "b"}


def test_speech_audio_block_returned_directly(app, client):
    import base64
    blocks = [{"type": "audio", "data": base64.b64encode(WAV).decode(), "mimeType": "audio/wav"}]
    svc, calls, restore = _tts(app, blocks=blocks)
    try:
        r = client.post("/v1/audio/speech", json={"input": "hi"})
        urls = svc.http.urls
    finally:
        restore()
    assert r.status_code == 200 and r.content == WAV and urls == []


def test_speech_refuses_a_url_on_another_host(app, client):
    svc, calls, restore = _tts(app, text_result="here: http://evil.example/x.wav")
    try:
        r = client.post("/v1/audio/speech", json={"input": "hi"})
        urls = svc.http.urls
    finally:
        restore()
    assert r.status_code == 502 and r.json()["error"]["code"] == "audio_host_not_allowed"
    assert urls == []                                   # never fetched


def test_speech_errors_are_openai_shaped(app, client):
    svc, calls, restore = _tts(app)
    try:
        empty = client.post("/v1/audio/speech", json={"model": "tts-1", "input": ""})
        del svc.mcp.servers["kokoro-tts"]
        missing = client.post("/v1/audio/speech", json={"input": "hi"})
    finally:
        restore()
    assert empty.status_code == 400 and empty.json()["error"]["code"] == "missing_input"
    assert missing.status_code == 503
    assert missing.json()["error"]["code"] == "speech_server_missing"


def test_no_stt_says_so(client):
    r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", WAV, "audio/wav")},
                    data={"model": "whisper-1"})
    assert r.status_code == 501
    err = r.json()["error"]
    assert err["code"] == "no_stt_backend" and "speech-to-text" in err["message"]


def test_audio_needs_media_scope():
    from foundry_router.client_auth import scope_for
    assert scope_for("/v1/audio/speech") == "media"
    assert scope_for("/v1/audio/transcriptions") == "media"


def test_audio_url_from_the_real_kokoro_result_shape():
    from foundry_router.facade.audio_api import audio_url
    # what the live Kokoro MCP returned on 2026-10-25
    assert audio_url('{"result":"http://192.168.0.115:8944/kokoro_1792904487223.wav"}') \
        == "http://192.168.0.115:8944/kokoro_1792904487223.wav"
    assert audio_url("see https://h/x and https://h/y.mp3.") == "https://h/y.mp3"
    assert audio_url("no link") is None


# --------------------------------------------------------------------------- #
# C5: images on the direct path                                                #
# --------------------------------------------------------------------------- #

from foundry_router.pool.protocols import ChatResult  # noqa: E402

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


class TwoModelPool:
    """text-a (no vision) and vis-b (vision) on one local Ollama backend."""

    def __init__(self):
        self.called = []

    def available_models(self):
        return {"text-a": ["o1"], "vis-b": ["o1"]}

    def backend_info(self, m):
        return {"name": "o1", "type": "ollama", "url": "http://o"} \
            if m in ("text-a", "vis-b") else None

    def backend_status(self):
        return []

    async def loaded_models(self):
        return set()

    def active_calls(self):
        return []

    async def chat(self, model, messages, tools=None, **kw):
        self.called.append((model, messages))
        return ChatResult(content=f"answered by {model}", prompt_tokens=3,
                          completion_tokens=2, finish_reason="stop"), "o1"

    async def chat_stream(self, model, messages, tools=None, **kw):
        res, _ = await self.chat(model, messages, tools)
        yield {"content": res.content}
        yield res.done_frame()


def _vision_fleet(app, **persona):
    svc = app.state.services
    old = svc.pool
    pool = TwoModelPool()
    svc.pool = pool
    svc.config_store.config.agent_brain.direct_stream = False
    for m in ("text-a", "vis-b"):
        svc.registry.upsert_auto(m, source="discovery", relative_cost_tier="free")
    svc.registry.set_capabilities("text-a", ["completion", "tools"])
    svc.registry.set_capabilities("vis-b", ["completion", "tools", "vision"])
    svc.personas.upsert("Pic", execution_mode="direct", **persona)
    return svc, pool, lambda: setattr(svc, "pool", old)


TOOLS = [{"type": "function", "function": {"name": "write_to_file",
                                           "parameters": {"type": "object"}}}]


def test_strict_persona_image_goes_to_its_vision_model(app, client):
    svc, pool, restore = _vision_fleet(app, routing="strict",
                                       model_allowlist=["text-a", "vis-b"],
                                       pinned_models=["text-a", "vis-b"])
    try:
        plain = client.post("/api/chat", json={"model": "Pic", "stream": False, "tools": TOOLS,
                                               "messages": [{"role": "user", "content": "hi"}]})
        pic = client.post("/api/chat", json={"model": "Pic", "stream": False, "tools": TOOLS,
                                             "messages": [{"role": "user", "content": "what is this",
                                                           "images": [PNG]}]})
    finally:
        restore()
    assert plain.json()["message"]["content"].startswith("answered by text-a")
    assert pic.json()["message"]["content"].startswith("answered by vis-b")
    assert [m for m, _ in pool.called] == ["text-a", "vis-b"]


def test_openai_image_part_goes_to_a_vision_model(app, client):
    svc, pool, restore = _vision_fleet(app, routing="strict",
                                       model_allowlist=["text-a", "vis-b"],
                                       pinned_models=["text-a", "vis-b"])
    try:
        r = client.post("/v1/chat/completions", json={
            "model": "Pic", "tools": TOOLS,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "what is this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG}}]}]})
    finally:
        restore()
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"].startswith("answered by vis-b")


def test_image_with_no_vision_route_gets_a_clear_answer(app, client):
    svc, pool, restore = _vision_fleet(app, model_allowlist=["text-a"])
    try:
        r = client.post("/api/chat", json={"model": "Pic", "stream": False, "tools": TOOLS,
                                           "messages": [{"role": "user", "content": "look",
                                                         "images": [PNG]}]})
    finally:
        restore()
    text = r.json()["message"]["content"]
    assert "can't read the attached image" in text and "text-a" in text
    assert pool.called == []                             # no blind model call


def test_image_earlier_in_history_keeps_the_vision_model(app, client):
    svc, pool, restore = _vision_fleet(app, routing="strict",
                                       model_allowlist=["text-a", "vis-b"],
                                       pinned_models=["text-a", "vis-b"])
    try:
        client.post("/api/chat", json={"model": "Pic", "stream": False, "tools": TOOLS,
                                       "messages": [
                                           {"role": "user", "content": "look", "images": [PNG]},
                                           {"role": "assistant", "content": "a cat"},
                                           {"role": "user", "content": "and now?"}]})
    finally:
        restore()
    assert [m for m, _ in pool.called] == ["vis-b"]


def test_model_with_unknown_capabilities_is_trusted_with_images(app, client):
    svc, pool, restore = _vision_fleet(app, routing="strict",
                                       model_allowlist=["text-a", "vis-b"],
                                       pinned_models=["text-a", "vis-b"])
    svc.db.execute("UPDATE models SET capabilities=NULL WHERE id='text-a'")   # never probed
    try:
        client.post("/api/chat", json={"model": "Pic", "stream": False, "tools": TOOLS,
                                       "messages": [{"role": "user", "content": "look",
                                                     "images": [PNG]}]})
    finally:
        restore()
    assert [m for m, _ in pool.called] == ["text-a"]
