"""v0.181.249: Media_Gen 5.21.1 in chat - generate_3d_model's GLB files, long jobs that no
chat waits for, files deleted at the source, missing-model refusals, the test button
taking new tool schemas in, and a server's own usage notes for the model."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from foundry_router import request_context, server_notes, tool_media
from foundry_router.config import MCPServerConfig
from foundry_router.tools.mcp_client import ToolCallError, ToolResult
from foundry_router.tools.sync import ToolDef
from tests.test_tool_media import MG, _QueueServer, _fetching, _queued, _store, png, run  # noqa: F401
from tests.test_tool_media import svc  # noqa: F401  (fixture)

GLB = b"glTF\x02\x00\x00\x00" + b"\x00" * 600
CHECK = ("job_id: e3c3aa7b2b4e\nSTATUS: done\n"
         "tool: generate_3d_model (FLUX 3 Klein Edit + TRELLIS.2 + Hi3D 1.0)\nlibrary: game\n"
         "Files (URLs on the remote server; download with curl or open in a browser, do not "
         "read them):\n"
         f"  glb: {MG}/game/3d/robot_3d_960e2ad5.glb\n"
         f"  glb_detail: {MG}/game/3d/robot_3d_960e2ad5_detail.glb\n"
         f"  picture_png: {MG}/game/image/robot_3d_picture_dea386d7.png\n")


def _check_result(svc, client, text=CHECK, files=None):
    files = files if files is not None else {
        f"{MG}/game/3d/robot_3d_960e2ad5.glb": GLB,
        f"{MG}/game/image/robot_3d_picture_dea386d7.png": png(tag=b"robot")}
    restore, fetched = _fetching(svc, files)
    try:
        sink: list = []
        res = run(client, tool_media.run_tool, svc, svc.mcp, "tts", "media_check_job",
                  {"job_id": "last"}, sink=sink)
    finally:
        restore()
    return res, sink, fetched


def _fake_check(text):
    async def fake(server, tool, args, **kw):
        return ToolResult([{"type": "text", "text": text}])
    return fake


# -- GLB hand-back ------------------------------------------------------------------------

def test_a_3d_result_picks_the_game_glb_and_the_picture_never_the_detailed_mesh():
    hosts = {"192.168.0.115"}
    assert tool_media.pick_urls([CHECK], hosts) == [
        f"{MG}/game/3d/robot_3d_960e2ad5.glb", f"{MG}/game/image/robot_3d_picture_dea386d7.png"]
    assert tool_media.detail_links([CHECK], hosts) == [
        (f"{MG}/game/3d/robot_3d_960e2ad5_detail.glb", "glb_detail")]
    # game_faces=0: only the detailed mesh - still never fetched
    only = f"job_id: e3c3aa7b2b4e\nSTATUS: done\n  glb_detail: {MG}/game/3d/x_detail.glb\n"
    assert tool_media.pick_urls([only], hosts) == []
    # an unlabelled *_detail.glb is the detailed mesh too
    assert tool_media.pick_urls([f"see {MG}/game/3d/y_detail.glb"], hosts) == []


def test_the_glb_is_stored_shown_as_download_and_3d_view_and_the_detail_is_a_link(svc, client):
    old = svc.mcp.call_tool_rich
    svc.mcp.call_tool_rich = _fake_check(CHECK)
    try:
        res, sink, fetched = _check_result(svc, client)
    finally:
        svc.mcp.call_tool_rich = old
    assert f"{MG}/game/3d/robot_3d_960e2ad5_detail.glb" not in fetched    # never pulled
    kinds = sorted(i.kind for i in sink if isinstance(i, tool_media.MediaItem))
    assert kinds == ["image", "model"]
    model = next(i for i in sink if getattr(i, "kind", "") == "model")
    assert model.mime == "model/gltf-binary" and model.label == "media_check_job 3D model"
    md = tool_media.render(svc, sink)
    assert "🧊 [media_check_job 3D model (glb, 1 KB)](http://testserver/media/a/" in md
    assert "?download=1) · [view in 3D](http://testserver/media/a/" in md and "/view)" in md
    assert f"(large file, opens on your network)]({MG}/game/3d/robot_3d_960e2ad5_detail.glb)" in md
    assert "![media_check_job image](http://testserver/media/a/" in md
    assert "1 3D model(s)" in res.text and "detailed mesh" in res.text
    # where each copy came from is kept for Media_Gen's delete notice
    row = svc.media.get(model.job_id)
    arts = {a["mime"]: a for a in json.loads(row["artifacts"])}
    assert arts["model/gltf-binary"]["source_job"] == "e3c3aa7b2b4e"
    assert arts["model/gltf-binary"]["source_key"] == "glb"
    assert arts["model/gltf-binary"]["file"].endswith(".glb")
    # the file itself: the real content type; ?download=1 saves it
    url = tool_media.link(svc, model).replace("http://testserver", "")
    r = client.get(url + "?download=1")
    assert r.status_code == 200 and r.content == GLB
    assert r.headers["content-type"] == "model/gltf-binary"
    assert r.headers["content-disposition"].startswith("attachment")


def test_a_file_named_glb_that_is_not_one_is_not_shown(svc, client):
    old = svc.mcp.call_tool_rich
    svc.mcp.call_tool_rich = _fake_check(CHECK)
    try:
        _, sink, _ = _check_result(svc, client, files={
            f"{MG}/game/3d/robot_3d_960e2ad5.glb": b"<html>not a model</html>"})
    finally:
        svc.mcp.call_tool_rich = old
    assert not [i for i in sink if getattr(i, "kind", "") == "model"]


def test_the_3d_view_page_and_its_404s(svc, client):
    old = svc.mcp.call_tool_rich
    svc.mcp.call_tool_rich = _fake_check(CHECK)
    try:
        _, sink, _ = _check_result(svc, client)
    finally:
        svc.mcp.call_tool_rich = old
    model = next(i for i in sink if getattr(i, "kind", "") == "model")
    image = next(i for i in sink if getattr(i, "kind", "") == "image")
    mpath = tool_media.link(svc, model).replace("http://testserver", "")
    token = mpath.rsplit("/", 1)[1]
    svc.config_store.config.client_auth.mode = "required"     # keyless, like the file
    try:
        r = client.get(mpath + "/view")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        page = r.text
        assert "<model-viewer" in page and f'src="../{token}"' in page
        assert svc.config_store.config.media.model_viewer_url in page
        csp = r.headers["content-security-policy"]
        assert "script-src https://cdn.jsdelivr.net" in csp and "default-src 'none'" in csp
        # a picture's token has no 3D page; a forged token neither
        ipath = tool_media.link(svc, image).replace("http://testserver", "")
        assert client.get(ipath + "/view").status_code == 404
        assert client.get("/media/a/AAAA.BBBB/view").status_code == 404
        # no viewer script set: no page, and the reply has the download link only
        svc.config_store.config.media.model_viewer_url = ""
        try:
            assert client.get(mpath + "/view").status_code == 404
            md = tool_media.render(svc, [model])
            assert "?download=1)" in md and "view in 3D" not in md
        finally:
            svc.config_store.config.media.model_viewer_url = (
                "https://cdn.jsdelivr.net/npm/@google/model-viewer@3.5.0/dist/model-viewer.min.js")
    finally:
        svc.config_store.config.client_auth.mode = "optional"


# -- files deleted on the media server ---------------------------------------------------

def test_files_deleted_from_the_library_take_foundrys_copies_with_them(svc, client):
    old = svc.mcp.call_tool_rich
    svc.mcp.call_tool_rich = _fake_check(CHECK)
    try:
        _, sink, _ = _check_result(svc, client)
    finally:
        svc.mcp.call_tool_rich = old
    model = next(i for i in sink if getattr(i, "kind", "") == "model")
    image = next(i for i in sink if getattr(i, "kind", "") == "image")
    mpath = tool_media.link(svc, model).replace("http://testserver", "")
    ipath = tool_media.link(svc, image).replace("http://testserver", "")
    assert client.get(mpath).status_code == 200 and client.get(ipath).status_code == 200
    # Media_Gen 5.20.1: the GLB was deleted on the Files page
    some = ("job_id: e3c3aa7b2b4e\nSTATUS: done\nFiles (URLs ...):\n"
            f"  picture_png: {MG}/game/image/robot_3d_picture_dea386d7.png\n"
            "Deleted from the library since this job ran (no link works any more): glb, "
            "glb_detail.\n")
    notes = tool_media.forget_deleted(svc, some)
    assert client.get(mpath).status_code == 404 and client.get(ipath).status_code == 200
    assert len(notes) == 1 and "glb, glb_detail of job e3c3aa7b2b4e" in notes[0]
    assert "deleted its 1 copied file(s) too" in notes[0] and "Don't show" in notes[0]
    # every file gone
    every = ("job_id: e3c3aa7b2b4e\nSTATUS: done\nFILES DELETED: every file of this job was "
             "deleted from the library.\nTell the user the result is no longer on the server; "
             "run the tool again to make a new one.")
    old = svc.mcp.call_tool_rich
    svc.mcp.call_tool_rich = _fake_check(every)
    try:
        res, sink2, fetched = _check_result(svc, client, text=every)
    finally:
        svc.mcp.call_tool_rich = old
    assert client.get(ipath).status_code == 404 and fetched == [] and sink2 == []
    assert "every file of job e3c3aa7b2b4e" in res.text
    # another job's notice touches nothing
    assert tool_media.forget_deleted(svc, "job_id: 000000000000\nFILES DELETED: all") != []
    assert tool_media.forget_deleted(svc, "job_id: e3c3aa7b2b4e\nSTATUS: done") == []


# -- long jobs: never held in a chat turn ----------------------------------------------------

def test_a_3d_job_is_not_waited_for(svc, client, monkeypatch):
    fake = _QueueServer(["running"], "job_id: 0123456789ab\nSTATUS: done")
    res, sink, said = _queued(svc, client, monkeypatch, fake, wait=30, tool="generate_3d_model")
    assert [c[0] for c in fake.calls] == ["generate_3d_model"]          # no check at all
    assert len(sink) == 1 and isinstance(sink[0], tool_media.PendingJob)
    assert "did not wait for job 0123456789ab" in res.text and "Do NOT call generate_3d_model" in res.text
    assert any("not waiting" in s for s in said)
    assert "⏳ tts/generate_3d_model is still running (job `0123456789ab`)" in tool_media.render(svc, sink)


def test_the_queued_replys_own_estimate_decides_for_an_unlisted_tool():
    m = SimpleNamespace(job_no_wait_tools=["generate_3d_model"])
    assert tool_media.too_long_to_wait(m, "make_thing", "usually takes about 10-25 minutes", 90) \
        == "takes about 10-25 minutes"
    assert tool_media.too_long_to_wait(m, "generate_image",
                                       "usually takes about 40-90 seconds per image", 90) == ""
    assert tool_media.too_long_to_wait(m, "generate_video", "takes about 2 hours", 90) \
        == "takes about 2 hours"
    assert tool_media.too_long_to_wait(m, "GENERATE_3D_MODEL", "job_id: x", 90) == "takes many minutes"
    assert tool_media.too_long_to_wait(SimpleNamespace(), "x", "", 90) == ""


# -- missing-model refusals ------------------------------------------------------------------

NOT_INSTALLED = ("Error executing tool generate_sfx: Stable Audio Small is not installed on the "
                 "media server: missing checkpoints/sa_small.safetensors (1.3 GB). Tell the user to "
                 "install it in the media-gen console (the tool's form has a 'Download the missing "
                 "files' button, or Models). Nothing was queued.")


def test_a_missing_model_refusal_is_relayed_and_never_retried_in_the_same_reply():
    from foundry_router.brain.agent import call_tool_rich
    calls = []

    async def fake(server, tool, args, **kw):
        calls.append((tool, dict(args)))
        tr = ToolResult([{"type": "text", "text": NOT_INSTALLED}], is_error=True)
        raise ToolCallError(f"MCP tool {server}/{tool} returned error: {NOT_INSTALLED}", tr)
    mcp = SimpleNamespace(call_tool_rich=fake, foundry_svc=None)

    async def go():
        request_context._request_id.set("req-3d-1")
        out = []
        for args in ({"prompt": "door creak", "model": "small"},
                     {"prompt": "door creak", "model": "small"},
                     {"prompt": "door creak"}):
            try:
                await call_tool_rich(mcp, "Media_Gen", "generate_sfx", args)
            except ToolCallError as e:
                out.append(str(e))
        request_context._request_id.set("req-3d-2")         # a new reply: "it's installed now"
        try:
            await call_tool_rich(mcp, "Media_Gen", "generate_sfx",
                                 {"prompt": "door creak", "model": "small"})
        except ToolCallError as e:
            out.append(str(e))
        return out
    out = asyncio.run(go())
    assert [c[1] for c in calls] == [{"prompt": "door creak", "model": "small"},
                                    {"prompt": "door creak"},
                                    {"prompt": "door creak", "model": "small"}]
    assert "missing-model refusal" in out[0] and "Do NOT call the tool again" in out[0]
    assert "refused again without calling the server" in out[1]
    assert out[0].count("missing-model refusal") == 1


def test_other_tool_errors_are_left_as_they_are():
    from foundry_router.brain.agent import call_tool_rich

    async def fake(server, tool, args, **kw):
        raise ToolCallError("MCP tool Media_Gen/generate_image returned error: queue full (20 jobs)",
                            ToolResult([{"type": "text", "text": "queue full"}], is_error=True))
    mcp = SimpleNamespace(call_tool_rich=fake, foundry_svc=None)

    async def go():
        request_context._request_id.set("req-3d-3")
        with pytest.raises(ToolCallError) as e:
            await call_tool_rich(mcp, "Media_Gen", "generate_image", {"prompt": "x"})
        return str(e.value)
    assert "Foundry" not in asyncio.run(go())


# -- settings ------------------------------------------------------------------------------

def test_the_3d_settings_are_saved_from_the_admin_page(svc, client):
    m = svc.config_store.config.media
    assert "generate_3d_model" in m.job_no_wait_tools
    assert m.model_viewer_url.startswith("https://cdn.jsdelivr.net/npm/@google/model-viewer@")
    keep = (list(m.job_no_wait_tools), m.model_viewer_url)
    try:
        r = client.post("/admin/api/config/media", json={
            "job_no_wait_tools": "generate_3d_model, create_short_film ,", "model_viewer_url": ""})
        got = r.json()["media"]
        assert got["job_no_wait_tools"] == ["generate_3d_model", "create_short_film"]
        assert got["model_viewer_url"] == ""
        bad = client.post("/admin/api/config/media", json={"model_viewer_url": "http://x/mv.js"})
        assert bad.status_code == 400
    finally:
        client.post("/admin/api/config/media", json={"job_no_wait_tools": keep[0],
                                                     "model_viewer_url": keep[1]})
    from pathlib import Path
    html = (Path(tool_media.__file__).parent / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    card = html[html.index("function mediaCard(md) {"):html.index("function bindMediaCard() {")]
    assert 'id="md-nowait"' in card and 'id="md-viewer"' in card
    save = html[html.index("function bindMediaCard() {"):html.index("async function cancelMediaJob(")]
    assert 'job_no_wait_tools: $("#md-nowait").value' in save
    assert 'model_viewer_url: $("#md-viewer").value' in save


# -- the test button takes new tools and schemas in -----------------------------------------

def test_testing_a_server_syncs_its_tools_and_reports_schema_changes(svc, client):
    svc.mcp.servers["mg"] = MCPServerConfig(name="mg", url="http://192.168.0.115:8190/mcp")
    schema_v1 = {"type": "object", "properties": {"library": {"enum": ["game", "personal"]}}}
    schema_v2 = {"type": "object", "properties": {"library": {"enum": ["game", "personal"]},
                                                  "change_picture": {"type": "string"}}}
    state = {"tools": [{"name": "mg_make", "description": "make", "input_schema": schema_v1}]}

    async def list_tools(name):
        return state["tools"] if name == "mg" else []

    async def list_all():
        return {"mg": state["tools"]}
    old = (svc.mcp.list_tools, svc.mcp.list_all)
    svc.mcp.list_tools, svc.mcp.list_all = list_tools, list_all
    try:
        first = client.post("/admin/api/mcp_servers/test", json={"name": "mg"}).json()
        assert first["ok"] and "mg_make" in first["sync"]["added"]
        state["tools"] = [{"name": "mg_make", "description": "make", "input_schema": schema_v2},
                          {"name": "mg_3d", "description": "3d", "input_schema": schema_v1}]
        second = client.post("/admin/api/mcp_servers/test", json={"name": "mg"}).json()
        assert second["sync"] == {"added": ["mg_3d"], "removed": [], "changed": ["mg_make"]}
        assert svc.tool_registry.tools["mg_make"].parameters == schema_v2
    finally:
        svc.mcp.list_tools, svc.mcp.list_all = old
        svc.mcp.servers.pop("mg", None)
        client.post("/admin/api/tools/sync")


# -- a server's own usage notes ------------------------------------------------------------

def test_server_instructions_reach_the_system_prompt_only_when_switched_on(svc, client, monkeypatch):
    svc.mcp.servers["mg"] = MCPServerConfig(name="mg", url="http://192.168.0.115:8190/mcp")
    svc.mcp.instructions["mg"] = "Every generate tool queues a job. Never call media_check_job in a loop."
    td = ToolDef(name="mg_make", kind="mcp", description="make", parameters={"type": "object"},
                 server="mg", mcp_tool="mg_make")
    monkeypatch.setattr(svc.tool_registry, "mcp_tools_for_persona", lambda p=None: [td])
    persona = {"virtual_name": "P"}
    msgs = [{"role": "system", "content": "You are P."}, {"role": "user", "content": "hi"}]
    try:
        assert server_notes.apply(svc, persona, msgs, None, direct=False) is msgs    # off
        svc.mcp.servers["mg"].pass_instructions = True
        out = server_notes.apply(svc, persona, msgs, None, direct=False)
        assert out is not msgs and msgs[0]["content"] == "You are P."            # not mutated
        assert out[0]["content"].startswith("You are P.\n\n[Notes from the mg tool server")
        assert "Never call media_check_job in a loop." in out[0]["content"]
        assert server_notes.apply(svc, persona, out, None, direct=False) is out  # once
        only_user = [{"role": "user", "content": "hi"}]
        assert server_notes.apply(svc, persona, only_user, None, direct=False)[0]["role"] == "system"
        svc.mcp.instructions["mg"] = ""
        assert server_notes.apply(svc, persona, msgs, None, direct=False) is msgs
    finally:
        svc.mcp.servers.pop("mg", None)
        svc.mcp.instructions.pop("mg", None)


def test_the_instructions_switch_is_saved_and_kept_from_the_connection_form(svc, client):
    body = {"name": "mgx", "url": "http://192.168.0.115:8190/mcp", "pass_instructions": True}
    try:
        assert client.post("/admin/api/mcp_servers", json=body).status_code == 200
        cfg = next(s for s in svc.config_store.config.mcp_servers if s.name == "mgx")
        assert cfg.pass_instructions is True
        # an older form without the field keeps it
        client.post("/admin/api/mcp_servers", json={"name": "mgx", "url": body["url"]})
        cfg = next(s for s in svc.config_store.config.mcp_servers if s.name == "mgx")
        assert cfg.pass_instructions is True
    finally:
        client.post("/admin/api/mcp_servers/delete", json={"name": "mgx"})
    from pathlib import Path
    html = (Path(tool_media.__file__).parent / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="mcp-instr"' in html and 'pass_instructions: $("#mcp-instr").checked' in html
    assert '$("#mcp-instr").checked = !!s.pass_instructions' in html
