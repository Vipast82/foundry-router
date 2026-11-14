"""Images and audio from persona MCP tools reach the chat user (tool_media.py):
stored with a sha256 and limits, served by a signed expiring link a browser can
load without a key, appended to the reply on the brain and direct paths (Ollama
and OpenAI shapes, buffered and streaming) without touching tool calls, and
long-running tools become jobs the model checks with tool_job_status."""
import asyncio
import base64
import hashlib
import json
import re
import struct
import time
import zlib

import httpx
import pytest

from foundry_router import tool_media
from foundry_router.config import MCPServerConfig
from foundry_router.media import sniff_media
from foundry_router.pool.protocols import ChatResult
from foundry_router.tools.mcp_client import ToolResult
from foundry_router.tools.sync import ToolDef


def png(w=8, h=6, tag=b"x") -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"tEXt", b"k\x00" + tag) + chunk(b"IEND", b""))


def wav(n=4000) -> bytes:
    data = b"\x00\x01" * n
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16) + b"data"
            + struct.pack("<I", len(data)) + data)


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


LINK_RE = re.compile(r"\((http://testserver/media/a/[A-Za-z0-9_\-.]+)\)")


@pytest.fixture()
def svc(app, client):
    s = app.state.services
    s.mcp.servers["tts"] = MCPServerConfig(name="tts", url="http://192.168.0.115:8934/sse")
    s.mcp.servers["comfy"] = MCPServerConfig(name="comfy", url="http://192.168.0.115:8188/mcp",
                                             long_running=True)
    from foundry_router import request_context
    request_context._origin.set("http://testserver")
    yield s
    s.mcp.servers.pop("tts", None)
    s.mcp.servers.pop("comfy", None)


def run(client, coro_fn, *a, **kw):
    return client.portal.call(lambda: coro_fn(*a, **kw))


# -- sniffing / signing -----------------------------------------------------------

def test_sniff_media_types():
    assert sniff_media(png())[0] == "image/png"
    assert sniff_media(b"GIF89a" + struct.pack("<HH", 3, 4) + b"\x00" * 8) == ("image/gif", 3, 4)
    assert sniff_media(wav())[0] == "audio/wav"
    assert sniff_media(b"ID3\x04" + b"\x00" * 20)[0] == "audio/mpeg"
    assert sniff_media(b"OggS" + b"\x00" * 20)[0] == "audio/ogg"
    assert sniff_media(b"fLaC" + b"\x00" * 20)[0] == "audio/flac"
    assert sniff_media(b"<html>not media</html>")[0] is None


def test_sign_verify_expiry_and_forgery(svc):
    tok = tool_media.sign(svc.db, "img_0123456789abcdef", 2, 3600)
    assert tool_media.verify(svc.db, tok) == ("img_0123456789abcdef", 2)
    assert tool_media.verify(svc.db, tok, now=time.time() + 7200) is None     # expired
    p, s = tok.split(".")
    forged = tool_media._b64(tool_media._unb64(p).replace(b".2.", b".3.")) + "." + s
    assert tool_media.verify(svc.db, forged) is None
    assert tool_media.verify(svc.db, "garbage") is None
    assert tool_media.verify(svc.db, "") is None
    # the secret survives in the DB (links keep working after a restart)
    assert svc.db.kv_get(tool_media._SECRET_KV)


# -- store + public link -----------------------------------------------------------

def _store(client, svc, rich, server="tts", tool="speak"):
    return run(client, tool_media.store_result, svc, server, tool, {"t": 1}, rich)


def test_image_block_stored_and_served_without_a_key(svc, client):
    raw = png()
    cap = _store(client, svc, ToolResult([{"type": "image", "data": b64(raw),
                                           "mimeType": "image/png"}]))
    assert len(cap.items) == 1 and cap.items[0].kind == "image"
    row = svc.media.get(cap.items[0].job_id)
    assert row["op"] == "tool" and row["status"] == "succeeded" and row["charged"] == 0
    art = json.loads(row["artifacts"])[0]
    assert art["sha256"] == hashlib.sha256(raw).hexdigest() and art["width"] == 8
    url = tool_media.link(svc, cap.items[0])
    assert url.startswith("http://testserver/media/a/")
    # a client that requires keys still serves the signed link keyless
    svc.config_store.config.client_auth.mode = "required"
    try:
        r = client.get(url.replace("http://testserver", ""))
        assert r.status_code == 200 and r.content == raw
        assert r.headers["content-type"] == "image/png"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["content-disposition"].startswith("inline")
        assert r.headers["x-content-sha256"] == art["sha256"]
        # ...but the keyed artifact route is still closed
        assert client.get(f"/media/artifacts/{row['id']}/0").status_code == 401
    finally:
        svc.config_store.config.client_auth.mode = "optional"


def test_public_link_failures_are_bare_404s(svc, client):
    cap = _store(client, svc, ToolResult([{"type": "image", "data": b64(png())}]))
    it = cap.items[0]
    expired = tool_media.sign(svc.db, it.job_id, it.index, 60, now=time.time() - 3600)
    for path in (f"/media/a/{expired}", "/media/a/nope.nope",
                 "/media/a/" + tool_media.sign(svc.db, it.job_id, 5, 3600)):
        r = client.get(path)
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert client.get("/media/a/").status_code in (404, 405)                 # no listing


def test_audio_range_request_for_seeking(svc, client):
    raw = wav()
    cap = _store(client, svc, ToolResult([{"type": "audio", "data": b64(raw),
                                           "mimeType": "audio/wav"}]))
    url = tool_media.link(svc, cap.items[0]).replace("http://testserver", "")
    r = client.get(url, headers={"range": "bytes=0-99"})
    assert r.status_code == 206 and r.content == raw[:100]
    assert r.headers["content-type"] == "audio/wav"


def test_size_cap_skips_and_tells_the_model(svc, client):
    svc.config_store.config.media.tool_output_max_mb = 0.001      # ~1 KB
    try:
        cap = _store(client, svc, ToolResult([{"type": "audio", "data": b64(wav())}]))
    finally:
        svc.config_store.config.media.tool_output_max_mb = 100.0
    assert cap.items == [] and "tool_output_max_mb" in cap.notes[0]


def test_tool_outputs_off_stores_nothing(svc, client):
    svc.config_store.config.media.tool_outputs = False
    try:
        cap = _store(client, svc, ToolResult([{"type": "image", "data": b64(png())}]))
    finally:
        svc.config_store.config.media.tool_outputs = True
    assert cap.items == []


def test_resource_blob_and_duplicate_bytes(svc, client):
    raw = png(tag=b"r")
    cap = _store(client, svc, ToolResult([
        {"type": "resource", "uri": "file:///x.png", "mimeType": "image/png", "blob": b64(raw)},
        {"type": "image", "data": b64(raw)}]))
    assert len(cap.items) == 1                                     # same sha256 once


def test_media_url_fetched_only_from_the_tool_servers_host(svc, client):
    fetched = []

    async def handler(req):
        fetched.append(str(req.url))
        return httpx.Response(200, content=wav())
    old = svc.http
    svc.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        text = ("Speech ready: http://192.168.0.115:8934/files/out.wav. Mirror: "
                "http://evil.example/steal.wav and docs http://192.168.0.115:8934/readme")
        cap = _store(client, svc, ToolResult([{"type": "text", "text": text}]))
    finally:
        svc.http = old
    assert fetched == ["http://192.168.0.115:8934/files/out.wav"]
    assert len(cap.items) == 1 and cap.items[0].mime == "audio/wav"
    assert cap.items[0].label == "speak audio"


def test_extra_fetch_host_and_failed_fetch_note(svc, client):
    async def handler(req):
        return httpx.Response(404)
    old = svc.http
    svc.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    svc.config_store.config.media.tool_fetch_hosts = ["files.lan"]
    try:
        cap = _store(client, svc, ToolResult([{"type": "text",
                                               "text": "at http://files.lan/a.mp3"}]))
    finally:
        svc.http = old
        svc.config_store.config.media.tool_fetch_hosts = []
    assert cap.items == [] and "HTTP 404" in cap.notes[0]


def test_tool_rows_never_count_against_the_image_budget(svc, client):
    before = svc.media._count(0)
    for _ in range(3):
        _store(client, svc, ToolResult([{"type": "image", "data": b64(png(tag=b"%d" % _))}]))
    assert svc.media._count(0) == before


def test_total_cap_prunes_oldest_tool_files(svc, client):
    a = _store(client, svc, ToolResult([{"type": "audio", "data": b64(wav(3000))}])).items[0]
    b = _store(client, svc, ToolResult([{"type": "audio", "data": b64(wav(3001))}])).items[0]
    svc.db.execute("UPDATE media_jobs SET created_ts=created_ts-100 WHERE id=?", (a.job_id,))
    svc.config_store.config.media.tool_output_max_total_gb = 7000 / 1024 ** 3
    try:
        assert svc.media.prune_tool_outputs() == 1
    finally:
        svc.config_store.config.media.tool_output_max_total_gb = 5.0
    assert svc.media.get(a.job_id) is None and svc.media.get(b.job_id) is not None


def test_render_markdown_shapes(svc, client):
    img = _store(client, svc, ToolResult([{"type": "image", "data": b64(png())}]), tool="draw")
    aud = _store(client, svc, ToolResult([{"type": "audio", "data": b64(wav())}]))
    md = tool_media.render(svc, img.items + aud.items + img.items
                           + [tool_media.PendingJob("img_00", "comfy/run")])
    assert md.count("![draw image](http://testserver/media/a/") == 1     # deduped
    assert "🔊 [speak audio (wav, 7 KB)](http://testserver/media/a/" in md
    assert "<audio" not in md
    assert "⏳ comfy/run is still running (job `img_00`)" in md
    svc.config_store.config.media.audio_html = True
    try:
        assert '<audio controls src="http://testserver/media/a/' in tool_media.render(
            svc, aud.items)
    finally:
        svc.config_store.config.media.audio_html = False
    assert tool_media.render(svc, []) == ""


def test_public_base_url_overrides_the_request(svc, client):
    img = _store(client, svc, ToolResult([{"type": "image", "data": b64(png())}]))
    svc.config_store.config.media.public_base_url = "https://foundry.example/"
    try:
        assert tool_media.link(svc, img.items[0]).startswith("https://foundry.example/media/a/")
    finally:
        svc.config_store.config.media.public_base_url = ""


def test_origin_from_headers_is_proxy_aware_and_rejects_junk():
    from foundry_router.request_context import _origin_from
    assert _origin_from({"host": "192.168.0.112:11435"}) == "http://192.168.0.112:11435"
    assert _origin_from({"host": "x", "x-forwarded-host": "f.example",
                         "x-forwarded-proto": "https"}) == "https://f.example"
    assert _origin_from({"host": "evil.com/<script>"}) == ""
    assert _origin_from({"host": "a", "x-forwarded-proto": "javascript"}) == ""


# -- run_tool: what the model sees ---------------------------------------------------

def test_run_tool_model_sees_note_user_gets_sink(svc, client):
    async def fake(server, tool, args, **kw):
        return ToolResult([{"type": "text", "text": "done"},
                           {"type": "image", "data": b64(png()), "mimeType": "image/png"}])
    svc.mcp.call_tool_rich, old = fake, svc.mcp.call_tool_rich
    try:
        sink = []
        res = run(client, tool_media.run_tool, svc, svc.mcp, "tts", "draw", {}, sink=sink)
    finally:
        svc.mcp.call_tool_rich = old
    assert len(sink) == 1 and res.media_items == sink
    assert "[image: image/png" in res.text and "shown to the user automatically" in res.text
    assert "/media/a/" not in res.text                 # the model never gets the link
    assert res.images()                                # bytes still there for vision models


# -- video, one copy per result, queued jobs (v0.181.136) ------------------------------

def mp4(n=3000) -> bytes:
    return b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * n


MG = "http://192.168.0.115:8934/files"


def _fetching(svc, files):
    """svc.http that serves `files` ({url: bytes}); returns (restore, fetched list)."""
    fetched = []

    async def handler(req):
        fetched.append(str(req.url))
        body = files.get(str(req.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)
    old = svc.http
    svc.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def restore():
        svc.http = old
    return restore, fetched


def test_a_video_comes_back_as_a_link_that_plays_and_one_that_downloads(svc, client):
    """The chat got the tool server's own address for a video (mp4 was not a
    file type Foundry brought back), which only opens on the same network."""
    raw = mp4()
    restore, fetched = _fetching(svc, {f"{MG}/personal/video/fox_e513.mp4": raw})
    try:
        text = ("job_id: e51378bdd98f\nSTATUS: done\nFiles (URLs on the remote server):\n"
                f"  mp4: {MG}/personal/video/fox_e513.mp4\nvideo: 1280x704, 5.0 s, sound: yes")
        cap = _store(client, svc, ToolResult([{"type": "text", "text": text}]),
                     tool="media_check_job")
    finally:
        restore()
    assert len(cap.items) == 1
    it = cap.items[0]
    assert it.kind == "video" and it.mime == "video/mp4" and it.label == "media_check_job video"
    md = tool_media.render(svc, cap.items)
    url = tool_media.link(svc, it)
    assert f"🎬 [media_check_job video (mp4, 2 KB)]({url}) · [download]({url}?download=1)" in md
    assert "<video" not in md and "192.168.0.115" not in md       # the stored copy, not the GPU host
    svc.config_store.config.media.video_html = True
    try:
        assert f'<video controls preload="metadata" src="{url}"></video>' in tool_media.render(
            svc, cap.items)
    finally:
        svc.config_store.config.media.video_html = False
    path = url.replace("http://testserver", "")
    r = client.get(path)
    assert r.status_code == 200 and r.content == raw and r.headers["content-type"] == "video/mp4"
    assert r.headers["content-disposition"].startswith("inline")
    d = client.get(path + "?download=1")
    assert d.status_code == 200 and d.headers["content-disposition"].startswith("attachment")
    assert d.headers["content-disposition"].endswith('.mp4"')
    part = client.get(path, headers={"Range": "bytes=0-9"})          # a player seeks
    assert part.status_code == 206 and part.content == raw[:10]
    note = tool_media.model_note(cap.items, [])
    assert "1 video(s)" in note and "shown to the user automatically" in note


def test_a_page_named_mp4_is_not_a_video_and_an_m4a_stays_sound(svc, client):
    m4a = b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00" + b"\x00" * 500
    restore, _ = _fetching(svc, {f"{MG}/a/x.mp4": b"<html>sign in</html>", f"{MG}/a/song.m4a": m4a})
    try:
        cap = _store(client, svc, ToolResult([{"type": "text", "text":
                     f"video {MG}/a/x.mp4 and sound {MG}/a/song.m4a"}]))
    finally:
        restore()
    assert [(i.kind, i.mime) for i in cap.items] == [("audio", "audio/mp4")]
    assert tool_media._video_mime(b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 20, "mov") == "video/quicktime"
    assert tool_media._video_mime(mp4(), "m4a") is None              # the name decides


def test_one_copy_per_result_and_side_files_stay_links(svc, client):
    """Media_Gen lists a sound as ogg, mp3 and flac: the chat showed three links to
    the same second of sound. A finished presenter video lists five files."""
    hosts = {"192.168.0.115"}
    sfx = (f"Files:\n  ogg: {MG}/game/sfx/poison_9b.ogg\n  mp3: {MG}/game/sfx/poison_9b.mp3\n"
           f"  flac: {MG}/game/sfx/poison_9b.flac\nUse the .ogg or .mp3 in Roblox.")
    assert tool_media.pick_urls([sfx], hosts) == [f"{MG}/game/sfx/poison_9b.mp3"]
    presenter = (f"  mp4: {MG}/personal/video/host_f34a.mp4\n  srt: {MG}/personal/video/host_f34a.srt\n"
                 f"  vtt: {MG}/personal/video/host_f34a.vtt\n"
                 f"  uncaptioned_mp4: {MG}/personal/music_video/host_ee7c.mp4\n"
                 f"  narration_flac: {MG}/personal/video/host_f34a_parts/narration.flac")
    assert tool_media.pick_urls([presenter], hosts) == [f"{MG}/personal/video/host_f34a.mp4"]
    # links without such labels are all kept, in order; another host never is
    plain = (f"two pictures {MG}/a.png and {MG}/b.png, also http://evil.example/c.png and "
             f"http://192.168.0.115:8934/view?filename=d.png&type=output")
    assert tool_media.pick_urls([plain], hosts) == [
        f"{MG}/a.png", f"{MG}/b.png", "http://192.168.0.115:8934/view?filename=d.png&type=output"]
    assert tool_media.pick_urls([f"Speech ready: {MG}/out.wav. Docs {MG}/readme"], hosts) == [
        f"{MG}/out.wav"]
    restore, fetched = _fetching(svc, {f"{MG}/game/sfx/poison_9b.mp3": b"ID3" + b"\x00" * 400})
    try:
        cap = _store(client, svc, ToolResult([{"type": "text", "text": sfx}]))
    finally:
        restore()
    assert fetched == [f"{MG}/game/sfx/poison_9b.mp3"] and len(cap.items) == 1


def test_a_file_its_server_marks_private_is_never_copied(svc, client):
    """Victor 2026-11-14: pictures of a Media_Gen private library stay out of Foundry.
    Media_Gen sends every file of a private library with Cache-Control: private, no-store."""
    read = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            read.append(1)                       # the body of a private file is never pulled
            yield png(tag=b"secret")

    async def handler(req):
        if "/nsfw/" in str(req.url):
            return httpx.Response(200, stream=Body(), headers={"cache-control": "private, no-store"})
        return httpx.Response(200, content=png(tag=b"open"))
    old = svc.http
    svc.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    text = f"Files:\n  png: {MG}/nsfw/image/a_1234.png"
    try:
        cap = _store(client, svc, ToolResult([{"type": "text", "text": text}]), tool="media_check_job")
        assert cap.items == [] and read == []
        assert len(cap.notes) == 1 and f"{MG}/nsfw/image/a_1234.png is a private file" in cap.notes[0]
        assert "NOT shown under your reply" in cap.notes[0] and "couldn't fetch" not in cap.notes[0]
        note = tool_media.model_note(cap.items, cap.notes)
        assert "shown to the user automatically" not in note            # nothing was shown
        # a file without the mark, in the same result, is shown as before
        both = _store(client, svc, ToolResult([{"type": "text", "text":
                      text + f"\n  png: {MG}/personal/image/b_5678.png"}]), tool="media_check_job")
        assert len(both.items) == 1 and len(both.notes) == 1 and read == []
        # the switch on the admin page turns the rule off
        r = client.post("/admin/api/config/media", json={"tool_fetch_no_store": True})
        assert r.json()["media"]["tool_fetch_no_store"] is True
        try:
            on = _store(client, svc, ToolResult([{"type": "text", "text": text}]),
                        tool="media_check_job")
            assert len(on.items) == 1 and on.notes == [] and read == [1]
        finally:
            client.post("/admin/api/config/media", json={"tool_fetch_no_store": False})
    finally:
        svc.http = old
    assert svc.config_store.config.media.tool_fetch_no_store is False     # off unless switched on
    from pathlib import Path
    html = (Path(tool_media.__file__).parent / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="md-nostore"' in html and 'tool_fetch_no_store: $("#md-nostore").checked' in html


class _QueueServer:
    """Media_Gen's shape: a generate tool answers at once with a job id; the check
    tool reports queued, running, then done with a file link."""

    def __init__(self, states, final):
        self.states, self.final, self.calls = list(states), final, []

    async def __call__(self, server, tool, args, **kw):
        self.calls.append((tool, dict(args or {})))
        if tool != "media_check_job":
            return ToolResult([{"type": "text", "text":
                                "job_id: 0123456789ab\nSTATUS: queued\nposition 1 in the queue"}])
        if self.states:
            return ToolResult([{"type": "text", "text":
                                f"job_id: 0123456789ab\nSTATUS: {self.states.pop(0)}"}])
        return ToolResult([{"type": "text", "text": self.final}])


def _queued(svc, client, monkeypatch, fake, *, wait=5, tool="generate_image", has_tool=True):
    monkeypatch.setattr(tool_media, "_server_has_tool", lambda s, server, t: has_tool)
    m = svc.config_store.config.media
    old = (m.job_wait_seconds, m.fallback_poll_seconds, svc.mcp.call_tool_rich)
    m.job_wait_seconds, m.fallback_poll_seconds, svc.mcp.call_tool_rich = wait, 0.05, fake
    said, sink = [], []
    try:
        res = run(client, tool_media.run_tool, svc, svc.mcp, "tts", tool, {"prompt": "a fox"},
                  sink=sink, progress=said.append)
    finally:
        m.job_wait_seconds, m.fallback_poll_seconds, svc.mcp.call_tool_rich = old
    return res, sink, said


def test_a_queued_job_is_waited_for_and_its_picture_is_in_the_same_reply(svc, client, monkeypatch):
    """Media_Gen queues every job: the chat showed a job id, and the picture came
    only when the user asked again."""
    raw = png(tag=b"fox")
    fake = _QueueServer(["queued", "running\nprogress: 40%"],
                        f"job_id: 0123456789ab\nSTATUS: done\nFiles:\n  png: {MG}/personal/image/fox_0123.png")
    restore, fetched = _fetching(svc, {f"{MG}/personal/image/fox_0123.png": raw})
    try:
        res, sink, said = _queued(svc, client, monkeypatch, fake)
    finally:
        restore()
    assert [c[0] for c in fake.calls] == ["generate_image"] + ["media_check_job"] * 3   # never resubmitted
    assert all(c[1] == {"job_id": "0123456789ab"} for c in fake.calls[1:])
    assert len(sink) == 1 and sink[0].kind == "image" and res.media_items == sink
    assert "![generate_image image](http://testserver/media/a/" in tool_media.render(svc, sink)
    assert "STATUS: queued" in res.text and "Foundry waited" in res.text and "STATUS: done" in res.text
    assert "1 image(s) from this tool are shown to the user automatically" in res.text
    told = "".join(said)
    assert "queued job 0123456789ab for generate_image" in told and "running 40%" in told
    assert "job 0123456789ab: done after" in told


def test_a_reply_that_queues_several_jobs_is_waited_for_as_a_whole(svc, client, monkeypatch):
    """Media_Gen's variations=2 answers with two job ids. Both pictures are shown; a
    job that is not done in time is the only one reported as still running."""
    a, b = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
    left = {a: ["running"], b: ["running"] * 3}
    calls = []

    async def fake(server, tool, args, **kw):
        calls.append((tool, (args or {}).get("job_id")))
        if tool != "media_check_job":
            return ToolResult([{"type": "text", "text":
                                f"job_id: {a}  (seed 5, queue position 1)\n"
                                f"job_id: {b}  (seed 6, queue position 2)\nSTATUS: queued"}])
        jid = args["job_id"]
        if left[jid]:
            return ToolResult([{"type": "text", "text": f"job_id: {jid}\nSTATUS: {left[jid].pop(0)}"}])
        return ToolResult([{"type": "text", "text":
                            f"job_id: {jid}\nSTATUS: done\nFiles:\n  png: {MG}/personal/image/v_{jid[:4]}.png"}])
    files = {f"{MG}/personal/image/v_aaaa.png": png(tag=b"a"), f"{MG}/personal/image/v_bbbb.png": png(tag=b"b")}
    restore, fetched = _fetching(svc, files)
    try:
        res, sink, said = _queued(svc, client, monkeypatch, fake)
    finally:
        restore()
    assert [c for c in calls if c[0] != "media_check_job"] == [("generate_image", None)]
    assert calls.count(("media_check_job", a)) == 2 and calls.count(("media_check_job", b)) == 4
    assert [i.kind for i in sink] == ["image", "image"] and sorted(fetched) == sorted(files)
    assert f"Foundry waited" in res.text and res.text.count("STATUS: done") == 2
    # one of the two is too slow: its picture is shown, the other is named as still running
    left = {a: [], b: ["running"] * 500}
    calls.clear()
    restore, fetched = _fetching(svc, files)
    try:
        res, sink, said = _queued(svc, client, monkeypatch, fake, wait=0.4)
    finally:
        restore()
    assert [type(i).__name__ for i in sink] == ["PendingJob", "MediaItem"] and sink[0].job_id == b
    assert f"job {b} was still running" in res.text and f'job_id "{b}"' in res.text
    assert res.text.count("STATUS: done") == 1 and "1 image(s) from this tool are shown" in res.text


def test_a_job_that_outlasts_the_wait_is_reported_as_still_running(svc, client, monkeypatch):
    fake = _QueueServer(["running"] * 200, "")
    res, sink, said = _queued(svc, client, monkeypatch, fake, wait=0.3, tool="generate_video")
    assert [c[0] for c in fake.calls].count("generate_video") == 1
    assert len(sink) == 1 and isinstance(sink[0], tool_media.PendingJob)
    assert sink[0].job_id == "0123456789ab" and sink[0].label == "tts/generate_video"
    assert "⏳ tts/generate_video is still running (job `0123456789ab`)" in tool_media.render(svc, sink)
    assert "call media_check_job once with job_id \"0123456789ab\"" in res.text
    assert "Do NOT call generate_video again" in res.text


def test_a_failed_job_is_the_result_and_nothing_else_is_waited_for(svc, client, monkeypatch):
    fake = _QueueServer([], "job_id: 0123456789ab\nSTATUS: failed\nreason: out of memory")
    res, sink, _ = _queued(svc, client, monkeypatch, fake)
    assert sink == [] and "STATUS: failed" in res.text and "out of memory" in res.text
    # the check tool itself, a server without it, and the setting switched off: no waiting
    for kw in ({"tool": "media_check_job"}, {"has_tool": False}, {"wait": 0}):
        fake = _QueueServer(["running"] * 50, "")
        res, sink, said = _queued(svc, client, monkeypatch, fake, **kw)
        assert len(fake.calls) == 1 and sink == [] and said == [], kw
    svc.config_store.config.media.job_check_tool = ""
    try:
        fake = _QueueServer(["running"] * 50, "")
        _queued(svc, client, monkeypatch, fake)
        assert len(fake.calls) == 1
    finally:
        svc.config_store.config.media.job_check_tool = "media_check_job"
    # a reply that names a job already finished, or no job at all, is left as it is
    async def done(server, tool, args, **kw):
        return ToolResult([{"type": "text", "text": "job_id: 0123456789ab\nSTATUS: cancelled"}])
    res, sink, said = _queued(svc, client, monkeypatch, done, tool="media_cancel_job")
    assert said == [] and "Foundry" not in res.text


def test_the_chat_file_settings_are_saved_from_the_admin_page(svc, client):
    m = svc.config_store.config.media
    assert (m.job_check_tool, m.job_wait_seconds, m.video_html) == ("media_check_job", 90, False)
    r = client.post("/admin/api/config/media", json={
        "job_wait_seconds": 240, "job_check_tool": " media_check_job ", "video_html": True,
        "public_base_url": "https://foundry.example.com/", "tool_output_max_mb": 250,
        "public_link_ttl_hours": 48})
    got = r.json()["media"]
    try:
        assert r.status_code == 200 and got["job_wait_seconds"] == 240 and got["video_html"] is True
        assert got["job_check_tool"] == "media_check_job" and got["tool_output_max_mb"] == 250.0
        assert got["public_base_url"] == "https://foundry.example.com" and got["public_link_ttl_hours"] == 48
        bad = client.post("/admin/api/config/media", json={"public_base_url": "foundry.example.com"})
        assert bad.status_code == 400 and "http://" in bad.json()["error"]
        assert client.post("/admin/api/config/media", json={"job_wait_seconds": 99999}).json()[
            "media"]["job_wait_seconds"] == 1800
    finally:
        client.post("/admin/api/config/media", json={
            "job_wait_seconds": 90, "video_html": False, "public_base_url": "",
            "tool_output_max_mb": 100, "public_link_ttl_hours": 168})
    from pathlib import Path
    html = (Path(tool_media.__file__).parent / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    card = html[html.index("function mediaCard(md) {"):html.index("function bindMediaCard() {")]
    for field in ('id="md-out"', 'id="md-check"', 'id="md-wait"', 'id="md-maxmb"', 'id="md-base"',
                  'id="md-ttl"', 'id="md-vid"', 'id="md-aud"'):
        assert field in card, field
    save = html[html.index("function bindMediaCard() {"):html.index("async function cancelMediaJob(")]
    for key in ("tool_outputs:", "job_check_tool:", "job_wait_seconds:", "tool_output_max_mb:",
                "public_base_url:", "public_link_ttl_hours:", "video_html:", "audio_html:"):
        assert key in save, key


# -- long-running jobs -----------------------------------------------------------------

def _slow_tool(delay, result):
    calls = []

    async def fake(server, tool, args, **kw):
        calls.append((server, tool, args))
        await asyncio.sleep(delay)
        return result
    fake.calls = calls
    return fake


def test_long_job_finishing_inline_is_a_normal_result(svc, client):
    fake = _slow_tool(0.05, ToolResult([{"type": "text", "text": "img ok"},
                                        {"type": "image", "data": b64(png())}]))
    svc.mcp.call_tool_rich, old = fake, svc.mcp.call_tool_rich
    svc.config_store.config.media.tool_inline_wait_seconds = 5
    lines = []
    try:
        sink = []
        res = run(client, tool_media.run_tool, svc, svc.mcp, "comfy", "run", {"p": "cat"},
                  sink=sink, progress=lines.append)
    finally:
        svc.mcp.call_tool_rich = old
        svc.config_store.config.media.tool_inline_wait_seconds = 90
    assert "img ok" in res.text and len(sink) == 1 and sink[0].kind == "image"
    row = svc.media.get(sink[0].job_id)
    assert row["op"] == "tool_job" and row["status"] == "succeeded"
    assert any("running as job" in ln for ln in lines)


def test_long_job_hands_back_a_job_id_then_status_returns_the_media(svc, client):
    fake = _slow_tool(0.4, ToolResult([{"type": "text", "text": "STATUS: done"},
                                       {"type": "audio", "data": b64(wav())}]))
    svc.mcp.call_tool_rich, old = fake, svc.mcp.call_tool_rich
    svc.config_store.config.media.tool_inline_wait_seconds = 0
    try:
        sink = []
        res = run(client, tool_media.run_tool, svc, svc.mcp, "comfy", "run", {"p": "dog"},
                  sink=sink)
        jid = re.search(r"img_[0-9a-f]{16}", res.text).group(0)
        assert "Do NOT call run again" in res.text
        assert isinstance(sink[0], tool_media.PendingJob) and sink[0].job_id == jid
        # the same call again while it runs reuses the job: no second generation
        sink2 = []
        res2 = run(client, tool_media.run_tool, svc, svc.mcp, "comfy", "run", {"p": "dog"},
                   sink=sink2)
        assert jid in res2.text and len(fake.calls) == 1
        # check without waiting: still running
        st = run(client, tool_media.run_tool, svc, svc.mcp, tool_media.JOBS_SERVER,
                 tool_media.STATUS_TOOL, {"job_id": jid, "wait_seconds": 0}, sink=[])
        assert "still running" in st.text
        # long-poll until done: finished text + the audio for the reply
        got = []
        st = run(client, tool_media.run_tool, svc, svc.mcp, tool_media.JOBS_SERVER,
                 tool_media.STATUS_TOOL, {"job_id": jid, "wait_seconds": 5}, sink=got)
    finally:
        svc.mcp.call_tool_rich = old
        svc.config_store.config.media.tool_inline_wait_seconds = 90
    assert "finished" in st.text and "STATUS: done" in st.text
    assert len(got) == 1 and got[0].kind == "audio" and st.media_items == got
    assert len(fake.calls) == 1                      # polling never resubmitted


def test_job_status_is_scoped_to_the_client(svc, client):
    from foundry_router import client_auth
    jid = svc.media.new_tool_row("tool_job", "comfy", "run", {}, status="running",
                                 client={"client_id": "someone-else"})
    st = run(client, tool_media.job_status, svc, {"job_id": jid})
    assert "No tool job" in st
    tok = client_auth._client.set({"client_id": "someone-else"})
    try:
        row = svc.media.get(jid)
        assert tool_media._owned(row)
    finally:
        client_auth._client.reset(tok)


def test_failed_long_job_reports_the_error(svc, client):
    async def boom(server, tool, args, **kw):
        raise RuntimeError("GPU on fire")
    svc.mcp.call_tool_rich, old = boom, svc.mcp.call_tool_rich
    svc.config_store.config.media.tool_inline_wait_seconds = 5
    try:
        with pytest.raises(RuntimeError, match="GPU on fire"):
            run(client, tool_media.run_tool, svc, svc.mcp, "comfy", "run", {}, sink=[])
    finally:
        svc.mcp.call_tool_rich = old
        svc.config_store.config.media.tool_inline_wait_seconds = 90


def test_restart_marks_running_tool_jobs_interrupted(svc, client):
    jid = svc.media.new_tool_row("tool_job", "comfy", "run", {}, status="running")
    svc.media._recover()
    row = svc.media.get(jid)
    assert row["status"] == "failed" and row["error_code"] == "interrupted"


def test_status_tool_offered_only_with_a_long_running_server(svc, client):
    reg = svc.tool_registry
    for name, server in (("comfy__run", "comfy"), ("tts__speak", "tts")):
        reg.tools[name] = ToolDef(name=name, kind="mcp", description="", parameters={},
                                  server=server, mcp_tool=name.split("__")[1])
    names = [t.name for t in reg.mcp_tools_for_persona(
        {"preferred_mcp_tools": json.dumps(["tts"])})]
    assert tool_media.STATUS_TOOL not in names
    names = [t.name for t in reg.mcp_tools_for_persona(
        {"preferred_mcp_tools": json.dumps(["tts", "comfy"])})]
    assert names[-1] == tool_media.STATUS_TOOL                     # last: stable order
    assert reg.get(tool_media.STATUS_TOOL).server == tool_media.JOBS_SERVER


# -- chat paths ---------------------------------------------------------------------------

class ToolThenAnswerPool:
    """Calls the persona's draw tool once, then answers (optionally with a
    client tool call alongside)."""

    def __init__(self, client_call=False):
        self.client_call = client_call
        self.convos = []

    def available_models(self):
        return {"local-a": ["o1"]}

    def backend_info(self, m):
        return {"name": "o1", "type": "ollama", "url": "http://o"} if m == "local-a" else None

    def backend_status(self):
        return []

    async def loaded_models(self):
        return set()

    def active_calls(self):
        return []

    def _turn(self, messages, tools):
        self.convos.append(messages)
        if not any(m.get("role") == "tool" for m in messages):
            return ChatResult(content="", tool_calls=[
                {"id": "c1", "name": "comfy__draw", "arguments": {"prompt": "cat"}}],
                prompt_tokens=5, completion_tokens=2, finish_reason="tool_calls")
        calls = ([{"id": "c2", "name": "write_to_file", "arguments": {"path": "a"}}]
                 if self.client_call else [])
        return ChatResult(content="Here is your cat.", tool_calls=calls,
                          prompt_tokens=9, completion_tokens=8,
                          finish_reason="tool_calls" if calls else "stop")

    async def chat(self, model, messages, tools=None, **kw):
        return self._turn(messages, tools), "o1"

    async def chat_stream(self, model, messages, tools=None, **kw):
        res = self._turn(messages, tools)
        if res.content:
            yield {"content": res.content}
        yield res.done_frame()


@pytest.fixture()
def direct(svc, client):
    cfg = svc.config_store.config.agent_brain
    persona = next(p for p in svc.personas.list(enabled_only=True))
    svc.personas.upsert(persona["virtual_name"], preferred_mcp_tools=json.dumps(["tts"]),
                        mcp_tools_in_direct=1)
    td = ToolDef(name="comfy__draw", kind="mcp", description="draw",
                 parameters={"type": "object"}, server="tts", mcp_tool="draw")
    svc.tool_registry.tools[td.name] = td

    async def fake_rich(server, tool, args, **kw):
        return ToolResult([{"type": "text", "text": "drawn"},
                           {"type": "image", "data": b64(png()), "mimeType": "image/png"}])
    old = (svc.mcp.call_tool_rich, svc.pool, cfg.direct_stream,
           cfg.direct_stream_heartbeat_seconds)
    svc.mcp.call_tool_rich = fake_rich

    def use(pool, stream=False):
        svc.pool = pool
        cfg.direct_stream, cfg.direct_stream_heartbeat_seconds = stream, 0
    use.persona = persona["virtual_name"]
    yield use
    (svc.mcp.call_tool_rich, svc.pool, cfg.direct_stream,
     cfg.direct_stream_heartbeat_seconds) = old


BODY_TOOLS = [{"type": "function", "function": {"name": "write_to_file"}}]


def _chat(client, model, stream, **extra):
    return client.post("/api/chat", json={
        "model": model, "stream": stream, "tools": BODY_TOOLS,
        "messages": [{"role": "user", "content": "draw a cat"}], **extra})


def test_direct_buffered_reply_gets_a_loadable_image(direct, client):
    pool = ToolThenAnswerPool()
    direct(pool)
    msg = _chat(client, direct.persona, False).json()["message"]
    assert msg["content"].startswith("Here is your cat.")
    url = LINK_RE.search(msg["content"]).group(1)
    assert msg["content"].count("![draw image](") == 1
    r = client.get(url.replace("http://testserver", ""))
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    tool_msg = next(m for m in pool.convos[1] if m.get("role") == "tool")
    assert "shown to the user automatically" in tool_msg["content"]
    assert "/media/a/" not in json.dumps(pool.convos[1])


def test_direct_client_tool_call_turn_keeps_its_tool_calls(direct, client):
    direct(ToolThenAnswerPool(client_call=True))
    msg = _chat(client, direct.persona, False).json()["message"]
    assert [t["function"]["name"] for t in msg["tool_calls"]] == ["write_to_file"]
    assert LINK_RE.search(msg["content"])                   # links ride in content only
    assert "/media/a/" not in json.dumps(msg["tool_calls"])


def test_direct_json_format_request_gets_no_links(direct, client):
    direct(ToolThenAnswerPool())
    msg = _chat(client, direct.persona, False, format="json").json()["message"]
    assert "/media/a/" not in msg["content"]


def test_direct_streaming_link_chunk_before_done(direct, client):
    direct(ToolThenAnswerPool(), stream=True)
    r = _chat(client, direct.persona, True)
    frames = [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]
    content = "".join((f.get("message") or {}).get("content") or "" for f in frames)
    assert content.startswith("Here is your cat.") and LINK_RE.search(content)
    assert frames[-1]["done"] is True
    link_i = next(i for i, f in enumerate(frames)
                  if "/media/a/" in ((f.get("message") or {}).get("content") or ""))
    assert link_i < len(frames) - 1


def test_direct_streaming_long_tool_keeps_the_stream_alive(direct, client, svc):
    async def slow(server, tool, args, **kw):
        await asyncio.sleep(0.35)
        return ToolResult([{"type": "text", "text": "drawn"}])
    svc.mcp.call_tool_rich = slow
    direct(ToolThenAnswerPool(), stream=True)
    svc.config_store.config.agent_brain.direct_stream_heartbeat_seconds = 0.1
    r = _chat(client, direct.persona, True)
    frames = [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]
    # keep-alive frames flowed while the tool ran (stream didn't go silent)
    idle = [f for f in frames if not f.get("done") and not (f.get("message") or {}).get("content")]
    assert len(idle) >= 3
    assert frames[-1]["done"] is True


def test_openai_shape_gets_the_link_too(direct, client):
    direct(ToolThenAnswerPool())
    r = client.post("/v1/chat/completions", json={
        "model": direct.persona, "stream": False, "tools": BODY_TOOLS,
        "messages": [{"role": "user", "content": "draw a cat"}]}).json()
    content = r["choices"][0]["message"]["content"]
    assert content.startswith("Here is your cat.") and LINK_RE.search(content)


def test_brain_path_appends_links_and_skips_the_cache(svc, client, monkeypatch):
    from foundry_router.brain.agent import AgentEvent
    from foundry_router.facade import ollama_api
    img = _store(client, svc, ToolResult([{"type": "image", "data": b64(png())}]), tool="draw")

    async def fake_events(_svc, ctx):
        ctx.media.extend(img.items)
        yield AgentEvent("answer", "A cat, as asked.")
    monkeypatch.setattr(ollama_api, "_run_events", fake_events)
    persona = next(p for p in svc.personas.list(enabled_only=True))
    resp = client.portal.call(lambda: ollama_api._agent_chat(
        svc, persona, persona["virtual_name"],
        [{"role": "user", "content": "draw a cat"}], False, "draw a cat"))
    content = json.loads(resp.body)["message"]["content"]
    assert content.startswith("A cat, as asked.") and LINK_RE.search(content)


def test_aggregator_adds_a_link_block_and_keeps_the_image(svc, client):
    td = ToolDef(name="tts__draw", kind="mcp", description="", parameters={},
                 server="tts", mcp_tool="draw")
    res = ToolResult([{"type": "image", "data": b64(png()), "mimeType": "image/png"}])
    run(client, svc.mcp_aggregator._add_media_links, td, {}, res)
    assert res.blocks[0]["type"] == "image"
    assert res.blocks[-1]["type"] == "text" and "/media/a/" in res.blocks[-1]["text"]
    content = svc.mcp_aggregator._to_mcp_content(res)
    assert [c.type for c in content] == ["image", "text"]


def test_status_tool_follows_the_client_tool_policy(svc, client):
    """tool_job_status is offered only when a long-running tool survives the
    v0.142 tool policy (off / list / dedupe), and always last."""
    from foundry_router.facade.ollama_api import _direct_mcp_split
    reg = svc.tool_registry
    for name, server in (("comfy__run", "comfy"), ("tts__speak", "tts")):
        reg.tools[name] = ToolDef(name=name, kind="mcp", description="", parameters={},
                                  server=server, mcp_tool=name.split("__")[1])
    ctools = [{"type": "function", "function": {"name": "write_to_file"}}]
    base = {"virtual_name": "P", "preferred_mcp_tools": json.dumps(["tts", "comfy"]),
            "mcp_tools_in_direct": 1}

    def names(policy, client_tools=ctools):
        offered, _n, _why = _direct_mcp_split(svc, {**base, "mcp_with_client_tools": policy},
                                              client_tools)
        return list(offered)
    assert names("all")[-1] == tool_media.STATUS_TOOL
    assert names("off") == []
    assert names(json.dumps(["tts"])) == ["tts__speak"]
    assert names(json.dumps(["comfy"])) == ["comfy__run", tool_media.STATUS_TOOL]
    assert names("off", client_tools=None)[-1] == tool_media.STATUS_TOOL   # no client tools
