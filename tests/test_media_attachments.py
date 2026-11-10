"""Chat attachments for media-gen (v0.181.117): attachments become `upload:` references the
model can pass to Media_Gen tools; failures become notes; requests without attachments are
untouched."""

import asyncio
import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from foundry_router import media_attachments as MA
from foundry_router.facade.ollama_api import _canonical_messages
from foundry_router.facade.openai_api import _to_ollama_messages
from foundry_router.tools.sync import ToolDef

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
PNG2 = b"\x89PNG\r\n\x1a\n" + b"\x01" * 40
JPG = b"\xff\xd8\xff\xe0" + b"\x02" * 40
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 30


def durl(mime, data):
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def img(data, mime="image/png"):
    return {"type": "image_url", "image_url": {"url": durl(mime, data)}}


def convo(*turns):
    """OpenAI messages -> (raw Ollama-shaped as the facade passes them, canonical)."""
    raw = _to_ollama_messages(list(turns))
    return raw, _canonical_messages(raw)


@pytest.fixture(autouse=True)
def clean_cache():
    MA._CACHE.clear()
    yield
    MA._CACHE.clear()


# ------------------------------------------------------------------ extraction

def test_extraction_from_openai_messages():
    raw, msgs = convo(
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": "plain text, nothing attached"},
        {"role": "user", "content": [{"type": "text", "text": "two pictures"},
                                     img(PNG), img(JPG, "image/jpeg")]},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": [
            {"type": "text", "text": "and these"},
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(WAV).decode(),
                                                    "format": "wav"}},
            {"type": "file", "file": {"file_data": durl("application/pdf", b"%PDF-1"),
                                      "filename": "notes.pdf"}}]})
    atts = MA.extract(raw, msgs)
    assert [(a.msg, a.kind, a.number) for a in atts] == [
        (2, "image", 1), (2, "image", 2), (4, "image", 3), (4, "audio", 1)]
    assert atts[0].mime == "image/png" and atts[0].data == PNG
    assert atts[1].mime == "image/jpeg"
    assert atts[2].url == "https://example.com/cat.png" and atts[2].data is None
    assert atts[3].data == WAV and atts[3].mime == "audio/wav"
    # the pdf is not media: never extracted; the models' messages carry no side keys
    assert all("_media_urls" not in m and "_media_parts" not in m for m in msgs)
    assert msgs[2]["images"] and msgs[2]["content"] == "two pictures"


def test_string_content_and_ollama_images():
    msgs = _canonical_messages([{"role": "user", "content": "hi"},
                                {"role": "user", "content": "photo",
                                 "images": [base64.b64encode(PNG).decode()]}])
    atts = MA.extract(msgs, msgs)
    assert [(a.msg, a.kind, a.mime) for a in atts] == [(1, "image", "image/png")]


# ------------------------------------------------------------------ injection

def test_injection_and_numbering_keeps_pictures():
    raw, msgs = convo({"role": "user", "content": [{"type": "text", "text": "use this"}, img(PNG)]},
                      {"role": "assistant", "content": "sure"},
                      {"role": "user", "content": [{"type": "text", "text": "and this one"},
                                                   img(PNG2)]})
    atts = MA.extract(raw, msgs)
    atts[0].ref, atts[1].error = "upload:aaaa_attachment-image1.png", "media-gen unreachable (X)"
    out = MA.inject(msgs, atts)
    assert out is not msgs and msgs[0]["content"] == "use this"          # original untouched
    assert out[0]["content"] == ("use this\n\n[Attached for media tools: image 1 = "
                                 "upload:aaaa_attachment-image1.png]")
    assert out[2]["content"].startswith(
        "and this one\n\n[image 2 couldn't be passed to media tools: media-gen unreachable (X)]")
    assert MA.GUIDE in out[2]["content"] and MA.GUIDE not in out[0]["content"]
    assert out[0]["images"] == msgs[0]["images"]                        # picture stays


# ------------------------------------------------------------------ cache + failures

def test_sha256_cache_is_an_lru():
    for i in range(MA.CACHE_SIZE + 5):
        MA.cache_put(f"sha{i}", f"upload:{i}")
    assert len(MA._CACHE) == MA.CACHE_SIZE
    assert MA.cache_get("sha0") is None and MA.cache_get(f"sha{MA.CACHE_SIZE + 4}")
    MA.cache_get("sha10")                                    # touched: kept on the next put
    MA.cache_put("new", "upload:new")
    assert MA.cache_get("sha10") == "upload:10" and MA.cache_get("sha5") is None


class _Svc:
    """Just what resolve() reads."""
    def __init__(self, url="http://media/upload", hosts=()):
        from types import SimpleNamespace
        self.config_store = SimpleNamespace(config=SimpleNamespace(
            media=SimpleNamespace(attachments_upload_url=url,
                                  attachments_fetch_hosts=list(hosts)),
            mcp_servers=[]))


def test_failure_notes_unreachable_rejected_too_big(monkeypatch):
    raw, msgs = convo({"role": "user", "content": [img(PNG), img(JPG, "image/jpeg")]})
    atts = MA.extract(raw, msgs)

    def boom(req):
        raise httpx.ConnectError("refused", request=req)
    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    asyncio.run(MA.resolve(_Svc(), atts, client))
    assert all(a.error.startswith("media-gen unreachable") for a in atts)

    atts = MA.extract(raw, msgs)

    def half(req):
        files = json.loads(req.content)["files"]
        return httpx.Response(200, json={
            "uploads": [{"ref": "upload:ok_" + files[0]["name"], "kind": "image",
                         "file": files[0]["name"], "sha256": "x"}],
            "errors": [f"{files[1]['name']}: not an image, video or audio file"]})
    asyncio.run(MA.resolve(_Svc(), atts, httpx.AsyncClient(transport=httpx.MockTransport(half))))
    assert atts[0].ref.startswith("upload:ok_") and atts[1].error == "not an image, video or audio file"

    monkeypatch.setattr(MA, "MAX_ATTACHMENT_BYTES", 10)
    atts = MA.extract(raw, msgs)
    asyncio.run(MA.resolve(_Svc(), atts, httpx.AsyncClient(transport=httpx.MockTransport(half))))
    assert all(a.error.startswith("too large") for a in atts)


def test_public_urls_pass_as_themselves_own_urls_are_fetched():
    raw, msgs = convo({"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://public.example/x.png"}},
        {"type": "image_url", "image_url": {"url": "http://owui.lan/api/files/1/content"}},
        {"type": "image_url", "image_url": {"url": "http://foundry:11435/media/a/tok"}}]})
    atts = MA.extract(raw, msgs)
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.method == "GET":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        files = json.loads(req.content)["files"]
        return httpx.Response(200, json={"uploads": [
            {"ref": "upload:" + f["name"], "kind": "image", "file": f["name"],
             "sha256": hashlib.sha256(PNG).hexdigest()} for f in files], "errors": []})
    asyncio.run(MA.resolve(_Svc(hosts=["owui.lan"]), atts,
                           httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    assert atts[0].ref == "https://public.example/x.png"            # never fetched or uploaded
    assert atts[1].ref.startswith("upload:") and atts[2].ref.startswith("upload:")
    assert "https://public.example/x.png" not in seen and "http://media/upload" in seen


def test_upload_url_from_the_media_gen_server(monkeypatch):
    from types import SimpleNamespace
    svc = _Svc(url="")
    svc.config_store.config.mcp_servers = [
        SimpleNamespace(name="Media_Gen", url="http://192.168.0.206:8190/mcp/", enabled=True)]
    monkeypatch.delenv("MEDIA_GEN_UPLOAD_URL", raising=False)
    assert MA.upload_url(svc) == "http://192.168.0.206:8190/upload"
    monkeypatch.setenv("MEDIA_GEN_UPLOAD_URL", "http://other:1/upload")
    assert MA.upload_url(svc) == "http://other:1/upload"


# ------------------------------------------------------------------ integration

class _FakeUpload(BaseHTTPRequestHandler):
    hits: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        uploads, errors = [], []
        for f in body["files"]:
            data, _ = MA.decode_data(f["data"])
            sha = hashlib.sha256(data).hexdigest()
            if not MA.kind_of(f.get("type") or ""):
                errors.append(f"{f['name']}: not an image, video or audio file")
                continue
            uploads.append({"ref": f"upload:{sha[:8]}_{f['name'].replace('-', '_')}",
                            "kind": MA.kind_of(f["type"]), "file": f["name"], "sha256": sha})
        _FakeUpload.hits.append(len(body["files"]))
        out = json.dumps({"uploads": uploads, "errors": errors}).encode()
        self.send_response(200 if uploads else 400)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture()
def fake_upload(monkeypatch):
    _FakeUpload.hits = []
    srv = HTTPServer(("127.0.0.1", 0), _FakeUpload)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("MEDIA_GEN_UPLOAD_URL", f"http://127.0.0.1:{srv.server_port}/upload")
    yield _FakeUpload
    srv.shutdown()


def test_resolve_against_a_fake_upload_server(fake_upload):
    raw, msgs = convo({"role": "user", "content": [img(PNG), img(PNG)]})   # same bytes twice
    atts = MA.extract(raw, msgs)
    asyncio.run(MA.resolve(_Svc(url=""), atts))
    sha = hashlib.sha256(PNG).hexdigest()
    assert atts[0].ref == f"upload:{sha[:8]}_attachment_image1.png" and atts[1].ref
    again = MA.extract(raw, msgs)
    asyncio.run(MA.resolve(_Svc(url=""), again))
    assert sum(fake_upload.hits) <= 2 and len(fake_upload.hits) == 1        # cached the 2nd time


class _CapturePool:
    def __init__(self):
        self.sent = []

    def backend_info(self, m):
        return {"name": "b1", "type": "openai-compatible", "url": "http://b", "flavor": "llamacpp"}

    def available_models(self):
        return {"qwen": ["b1"]}

    def active_calls(self):
        return []

    def backend_status(self):
        return []

    async def chat(self, model, messages, **kw):
        from foundry_router.pool.protocols import ChatResult
        self.sent.append(messages)
        return ChatResult(content="ok", prompt_tokens=10, completion_tokens=2), "b1"


def test_through_the_chat_api(app, client, fake_upload):
    """A persona with a Media_Gen tool: the user's picture reaches the model with its
    reference line; a second turn reuses the cached reference; a persona with the setting
    off, or a request without attachments, is untouched."""
    svc = app.state.services
    svc.tool_registry.tools["Media_Gen__generate_video"] = ToolDef(
        name="Media_Gen__generate_video", kind="mcp", description="video",
        parameters={"type": "object", "properties": {"image": {"type": "string"}}},
        server="Media_Gen", mcp_tool="generate_video")
    svc.personas.upsert("Mediaful", execution_mode="direct", model_allowlist=["qwen"],
                        preferred_mcp_tools=["Media_Gen"])
    pool, real = _CapturePool(), svc.pool
    svc.pool = pool
    turn1 = {"role": "user", "content": [
        {"type": "text", "text": "make a video of this person waving"}, img(PNG)]}
    try:
        client.post("/v1/chat/completions", json={"model": "Mediaful", "messages": [turn1]})
        sent = pool.sent[-1]
        user = next(m for m in sent if m["role"] == "user")
        sha = hashlib.sha256(PNG).hexdigest()
        assert f"[Attached for media tools: image 1 = upload:{sha[:8]}_attachment_image1.png]" \
            in user["content"]
        assert user.get("images")                                    # the picture is still there
        assert fake_upload.hits == [1]
        client.post("/v1/chat/completions", json={"model": "Mediaful", "messages": [
            turn1, {"role": "assistant", "content": "queued"},
            {"role": "user", "content": "now a talking presenter of the same photo"}]})
        assert fake_upload.hits == [1]                               # cached: not re-uploaded
        assert "upload:" in next(m for m in pool.sent[-1] if m["role"] == "user")["content"]
        # no attachments: unchanged
        client.post("/v1/chat/completions", json={"model": "Mediaful",
                                                  "messages": [{"role": "user", "content": "hi"}]})
        assert [m["content"] for m in pool.sent[-1] if m["role"] == "user"] == ["hi"]
        # setting off: unchanged
        svc.personas.upsert("Mediaful", media_attachments=0)
        client.post("/v1/chat/completions", json={"model": "Mediaful", "messages": [turn1]})
        assert "Attached for media tools" not in next(
            m for m in pool.sent[-1] if m["role"] == "user")["content"]
    finally:
        svc.pool = real
