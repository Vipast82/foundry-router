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
