"""v0.181.258: Media_Gen 5.27 tested through chat. 3D models and documents attached in a chat
reach media-gen as uploads (they were dropped: "check this .glb for Roblox" and "a podcast
about this PDF" never reached the tool); a "still running" line no longer stays under a reply
whose own check found the job done; "roughly 15-40 minutes" is read as an estimate; the chat
wait checks a job every job_check_every_seconds (10), not every 3 s."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from foundry_router import media_attachments as MA
from foundry_router import tool_media
from foundry_router.tools.mcp_client import ToolResult
from foundry_router.tools.sync import ToolDef
from tests.test_media_attachments import _Svc, clean_cache, convo, durl, fake_upload  # noqa: F401
from tests.test_tool_media import _QueueServer, _queued, run, svc  # noqa: F401

GLB = b"glTF\x02\x00\x00\x00" + b"\x00" * 60
FBX = b"Kaydara FBX Binary  \x00\x1a\x00" + b"\x00" * 40
STL = b"solid cube\n facet normal 0 0 1\nendsolid cube\n"
PDF = b"%PDF-1.4\n%\xe2\xe3\n1 0 obj\n<<>>\nendobj\n"
DOCX = b"PK\x03\x04" + b"\x00" * 40


def filepart(data, mime, name):
    return {"type": "file", "file": {"filename": name, "file_data": durl(mime, data)}}


# -- 3D models and documents are attachments too ------------------------------------------

def test_3d_models_and_documents_are_extracted_with_their_own_kinds():
    raw, msgs = convo({"role": "user", "content": [
        {"type": "text", "text": "check these"},
        filepart(GLB, "model/gltf-binary", "robot.glb"),
        filepart(GLB, "application/octet-stream", "plain.glb"),      # generic type, named .glb
        filepart(FBX, "application/octet-stream", ""),               # no name: sniffed
        filepart(STL, "application/octet-stream", "cube.stl"),
        filepart(PDF, "application/pdf", "volcanoes.pdf"),
        filepart(DOCX, "application/zip", "notes.docx"),             # a docx is a zip
        filepart(b"# Notes\nhello", "text/plain", "readme.md"),
        filepart(b"MZ\x90\x00", "application/octet-stream", "setup.exe")]})   # never sent
    atts = MA.extract(raw, msgs)
    got = [(a.kind, a.number, a.mime) for a in atts]
    assert got == [
        ("model", 1, "model/gltf-binary"), ("model", 2, "model/gltf-binary"),
        ("model", 3, "application/x-fbx"), ("model", 4, "model/stl"),
        ("document", 1, "application/pdf"),
        ("document", 2, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("document", 3, "text/markdown")]
    assert atts[2].name == "attachment-model3.fbx"                # named from the sniffed type
    assert MA.kind_of("application/x-msdownload", "setup.exe") == ""
    assert MA.kind_of("", "") == "" and MA.kind_of("image/png") == "image"


def test_the_reference_line_names_3d_models_and_documents():
    raw, msgs = convo({"role": "user", "content": [
        {"type": "text", "text": "podcast about this, and check the model"},
        filepart(PDF, "application/pdf", "volcanoes.pdf"),
        filepart(GLB, "model/gltf-binary", "robot.glb")]})
    atts = MA.extract(raw, msgs)
    atts[0].ref, atts[1].error = "upload:aaaa_volcanoes.pdf", "too large for media-gen's upload"
    out = MA.inject(msgs, atts)[0]["content"]
    assert "[Attached for media tools: document 1 = upload:aaaa_volcanoes.pdf]" in out
    assert "[3D model 1 couldn't be passed to media tools: too large for media-gen's upload]" in out
    assert "3D models/documents" in MA.GUIDE and "documents" in MA.GUIDE


def test_a_url_to_a_glb_is_fetched_and_uploaded_as_a_model():
    raw, msgs = convo({"role": "user", "content": [
        {"type": "file", "file": {"file_url": "http://foundry:11435/media/a/tok/robot.glb",
                                  "filename": "robot.glb"}}]})
    atts = MA.extract(raw, msgs)
    assert [a.kind for a in atts] == ["model"]

    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, content=GLB,
                                  headers={"content-type": "application/octet-stream"})
        files = json.loads(req.content)["files"]
        assert files[0]["type"] == "model/gltf-binary"
        return httpx.Response(200, json={"uploads": [
            {"ref": "upload:" + f["name"], "kind": "model", "file": f["name"],
             "sha256": hashlib.sha256(GLB).hexdigest()} for f in files], "errors": []})
    asyncio.run(MA.resolve(_Svc(), atts, httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    assert atts[0].ref == "upload:robot.glb" and atts[0].mime == "model/gltf-binary"


def test_a_fetched_file_that_is_nothing_media_gen_takes_gets_a_note():
    raw, msgs = convo({"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "http://foundry:11435/media/a/tok"}}]})
    atts = MA.extract(raw, msgs)

    def handler(req):
        return httpx.Response(200, content=b"MZ\x90\x00", headers={"content-type": "x/y"})
    asyncio.run(MA.resolve(_Svc(), atts, httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    assert atts[0].error == "not an image, audio, video, 3D model or document file"


def test_a_glb_and_a_pdf_reach_the_model_through_the_chat_api(app, client, fake_upload):
    from tests.test_media_attachments import _CapturePool
    svc = app.state.services
    svc.tool_registry.tools["Media_Gen__check_3d_model"] = ToolDef(
        name="Media_Gen__check_3d_model", kind="mcp", description="check a model",
        parameters={"type": "object", "properties": {"model": {"type": "string"}}},
        server="Media_Gen", mcp_tool="check_3d_model")
    svc.personas.upsert("Mediaful3D", execution_mode="direct", model_allowlist=["qwen"],
                        preferred_mcp_tools=["Media_Gen"])
    pool, real = _CapturePool(), svc.pool
    svc.pool = pool
    try:
        client.post("/v1/chat/completions", json={"model": "Mediaful3D", "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "check this model for Roblox and podcast the pdf"},
                filepart(GLB, "model/gltf-binary", "robot.glb"),
                filepart(PDF, "application/pdf", "volcanoes.pdf")]}]})
        user = next(m for m in pool.sent[-1] if m["role"] == "user")
        g, p = hashlib.sha256(GLB).hexdigest()[:8], hashlib.sha256(PDF).hexdigest()[:8]
        assert (f"[Attached for media tools: 3D model 1 = upload:{g}_robot.glb; "
                f"document 1 = upload:{p}_volcanoes.pdf]") in user["content"]
        assert fake_upload.hits == [2]
    finally:
        svc.pool = real


# -- the "still running" line after the model's own check -------------------------------

def test_a_pending_line_is_dropped_when_a_later_check_finds_the_job_done(svc, client, monkeypatch):
    """Live 2.1: Foundry's 90 s wait ran out on the second of two pictures, the model checked
    it itself and got STATUS: done, and "still running (job 2850acf4cb07)" stayed under a
    reply that said both were done."""
    fake = _QueueServer(["running"] * 50, "job_id: 0123456789ab\nSTATUS: done")
    res, sink, _ = _queued(svc, client, monkeypatch, fake, wait=0.2)
    assert any(isinstance(i, tool_media.PendingJob) for i in sink)
    assert "still running (job `0123456789ab`)" in tool_media.render(svc, sink)
    # the model's own check, in the same reply
    m = svc.config_store.config.media
    old = svc.mcp.call_tool_rich

    async def done(server, tool, args, **kw):
        return ToolResult([{"type": "text", "text": "job_id: 0123456789ab\nSTATUS: done\n"
                                                     "tool: generate_image"}])
    svc.mcp.call_tool_rich = done
    try:
        run(client, tool_media.run_tool, svc, svc.mcp, "tts", m.job_check_tool,
            {"job_id": "0123456789ab"}, sink=sink)
    finally:
        svc.mcp.call_tool_rich = old
    assert not any(isinstance(i, tool_media.PendingJob) for i in sink)
    assert "still running" not in tool_media.render(svc, sink)


def test_only_the_finished_job_loses_its_line(svc):
    a = tool_media.PendingJob("aaaaaaaaaaaa", "Media_Gen/generate_image")
    b = tool_media.PendingJob("bbbbbbbbbbbb", "Media_Gen/generate_image")
    keep = object()
    sink = [a, keep, b]
    tool_media.job_ended(sink, ["aaaaaaaaaaaa"])
    assert sink == [keep, b]
    out = tool_media.render(svc, [b])
    assert "`aaaaaaaaaaaa`" not in out and "still running (job `bbbbbbbbbbbb`)" in out
    tool_media.job_ended(None, ["x"])                       # no sink: nothing to do
    tool_media.job_ended(sink, [])
    assert sink == [keep, b]


def test_a_check_that_still_says_running_keeps_the_line(svc, client, monkeypatch):
    sink = [tool_media.PendingJob("0123456789ab", "tts/generate_image")]
    old = svc.mcp.call_tool_rich

    async def running(server, tool, args, **kw):
        return ToolResult([{"type": "text", "text": "job_id: 0123456789ab\nSTATUS: running"}])
    svc.mcp.call_tool_rich = running
    try:
        run(client, tool_media.run_tool, svc, svc.mcp, "tts", "media_check_job",
            {"job_id": "0123456789ab"}, sink=sink)
    finally:
        svc.mcp.call_tool_rich = old
    assert isinstance(sink[0], tool_media.PendingJob)


def test_foundrys_own_job_check_ends_the_line_too(monkeypatch):
    # the job's owner is the caller (another test may leave a client identity behind)
    monkeypatch.setattr(tool_media, "_owned", lambda row: True)
    rows = {"img_0123456789abcdef": {"id": "img_0123456789abcdef", "op": "tool_job",
                                     "status": "failed", "error": "boom", "client_id": "anonymous"}}
    fake = SimpleNamespace(media=SimpleNamespace(get=rows.get))
    sink = [tool_media.PendingJob("img_0123456789abcdef", "comfy/x")]
    res = asyncio.run(tool_media.run_tool(fake, None, tool_media.JOBS_SERVER,
                                          tool_media.STATUS_TOOL,
                                          {"job_id": "img_0123456789abcdef"}, sink=sink))
    assert "failed" in res.text and sink == []


# -- estimates and the wait --------------------------------------------------------------

def test_roughly_is_an_estimate_and_animate_is_never_waited_for():
    m = SimpleNamespace(job_no_wait_tools=[])
    assert tool_media.too_long_to_wait(m, "animate_3d_model",
                                       "queued; this takes roughly 15-40 minutes", 90) \
        == "takes roughly 15-40 minutes"
    assert tool_media.too_long_to_wait(m, "x", "around 3 minutes", 90) == "takes around 3 minutes"
    assert tool_media.too_long_to_wait(m, "x", "usually takes about 10-25 minutes", 90) \
        == "takes about 10-25 minutes"
    assert tool_media.too_long_to_wait(m, "x", "roughly 30 seconds", 90) == ""
    from foundry_router.config import MediaConfig
    assert "animate_3d_model" in MediaConfig().job_no_wait_tools
    assert MediaConfig().job_check_every_seconds == 10


def test_the_wait_checks_at_the_configured_interval(svc, client, monkeypatch):
    fake = _QueueServer(["running"] * 100, "job_id: 0123456789ab\nSTATUS: done")
    monkeypatch.setattr(tool_media, "_server_has_tool", lambda s, server, t: True)
    m = svc.config_store.config.media
    old = (m.job_wait_seconds, m.job_check_every_seconds, svc.mcp.call_tool_rich)
    m.job_wait_seconds, m.job_check_every_seconds, svc.mcp.call_tool_rich = 1.0, 0.3, fake
    try:
        run(client, tool_media.run_tool, svc, svc.mcp, "tts", "generate_image", {"prompt": "x"},
            sink=[])
    finally:
        m.job_wait_seconds, m.job_check_every_seconds, svc.mcp.call_tool_rich = old
    checks = sum(1 for c in fake.calls if c[0] == "media_check_job")
    assert 2 <= checks <= 4                                # every 0.3 s for 1 s, not every 0.05


def test_the_check_interval_is_saved_from_the_admin_page(svc, client):
    m = svc.config_store.config.media
    keep = m.job_check_every_seconds
    try:
        r = client.post("/admin/api/config/media", json={"job_check_every_seconds": 15})
        assert r.status_code == 200 and r.json()["media"]["job_check_every_seconds"] == 15
        low = client.post("/admin/api/config/media", json={"job_check_every_seconds": 1})
        assert low.json()["media"]["job_check_every_seconds"] == 2      # clamped to 2-60
        assert client.post("/admin/api/config/media",
                           json={"job_check_every_seconds": "x"}).status_code == 400
    finally:
        client.post("/admin/api/config/media", json={"job_check_every_seconds": keep})
    html = (Path(tool_media.__file__).parent / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    card = html[html.index("function mediaCard(md) {"):html.index("function bindMediaCard() {")]
    assert 'id="md-poll"' in card
    save = html[html.index("function bindMediaCard() {"):html.index("async function cancelMediaJob(")]
    assert 'job_check_every_seconds: +$("#md-poll").value' in save
