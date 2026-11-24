"""v0.181.259: a listing tool's links stay links. "Show me the pictures in the game library"
(list_assets) made Foundry copy and show every picture and 12 MB GLB the listing named."""
from pathlib import Path

from foundry_router import tool_media
from foundry_router.tools.mcp_client import ToolResult
from tests.test_tool_media import MG, _fetching, png, run, svc  # noqa: F401

LISTING = (f"2026-11-24 09:32  image  0.2 MB  {MG}/game/image/icons_atlas1.png  (job 9ea3f6aa6305)\n"
           f"2026-11-24 09:28  3d     5.5 MB  {MG}/game/3d/fox.glb  (job fb37b4e6cb33)\n"
           "The links work as inputs to any tool; a job_id works too.")
DONE = (f"job_id: 0123456789ab\nSTATUS: done\nFiles:\n  png: {MG}/game/image/icons_atlas1.png\n")


def _call(svc, client, tool, text):
    files = {f"{MG}/game/image/icons_atlas1.png": png(tag=b"atlas"),
             f"{MG}/game/3d/fox.glb": b"glTF\x02\x00\x00\x00" + b"\x00" * 600}
    restore, fetched = _fetching(svc, files)
    old = svc.mcp.call_tool_rich

    async def fake(server, t, args, **kw):
        return ToolResult([{"type": "text", "text": text}])
    svc.mcp.call_tool_rich = fake
    sink: list = []
    try:
        res = run(client, tool_media.run_tool, svc, svc.mcp, "tts", tool, {"library": "game"},
                  sink=sink)
    finally:
        svc.mcp.call_tool_rich = old
        restore()
    return res, sink, fetched


def test_a_listing_is_not_copied_or_shown(svc, client):
    res, sink, fetched = _call(svc, client, "list_assets", LISTING)
    assert sink == [] and not fetched
    assert f"{MG}/game/image/icons_atlas1.png" in res.text          # the links stay for the model
    assert "shown to the user automatically" not in res.text
    res, sink, fetched = _call(svc, client, "search_library", LISTING)
    assert sink == [] and not fetched


def test_a_tool_that_made_the_file_still_shows_it(svc, client):
    res, sink, fetched = _call(svc, client, "media_check_job", DONE)
    assert len(sink) == 1 and sink[0].kind == "image" and fetched


def test_the_list_is_a_setting_with_a_ui_control(svc, client):
    m = svc.config_store.config.media
    assert {"list_assets", "search_library", "media_list_jobs"} <= set(m.no_fetch_tools)
    assert tool_media.fetches_files(svc, "generate_image")
    assert not tool_media.fetches_files(svc, "LIST_ASSETS")
    keep = list(m.no_fetch_tools)
    try:
        r = client.post("/admin/api/config/media", json={"no_fetch_tools": "list_assets, ,x"})
        assert r.json()["media"]["no_fetch_tools"] == ["list_assets", "x"]
        assert tool_media.fetches_files(svc, "search_library")
        assert client.post("/admin/api/config/media",
                           json={"no_fetch_tools": 5}).status_code == 400
    finally:
        client.post("/admin/api/config/media", json={"no_fetch_tools": keep})
    html = (Path(tool_media.__file__).parent / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    card = html[html.index("function mediaCard(md) {"):html.index("function bindMediaCard() {")]
    assert 'id="md-nofetch"' in card
    save = html[html.index("function bindMediaCard() {"):html.index("async function cancelMediaJob(")]
    assert 'no_fetch_tools: $("#md-nofetch").value' in save
