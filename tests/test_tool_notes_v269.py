"""v0.181.269: media.tool_notes adds a note to a tool's description for every chat model.
The Media_Gen chat test: the chat model sent create_comic without characters (round 10,
round 12's bookshop), so the comic guessed the cast and made the shop bell a character."""
from pathlib import Path

from foundry_router import tool_finder
from foundry_router.config import MediaConfig
from tests.test_tool_media import run, svc  # noqa: F401

LONG = "Comic page and/or voiced motion comic: AI-drawn panels with real lettering. " * 10


class _Pool:
    def available_models(self):
        return {}


def _sync(svc, client, tools):
    old = svc.mcp.list_all

    async def fake():
        return {"Media_Gen": tools}
    svc.mcp.list_all = fake
    try:
        run(client, svc.tool_registry.sync, _Pool())
    finally:
        svc.mcp.list_all = old


def _tool(name, desc=LONG):
    return {"name": name, "description": desc, "input_schema": {"type": "object", "properties": {}}}


def test_the_default_note_asks_for_characters_and_keeps_sounds_out():
    note = MediaConfig().tool_notes["create_comic"]
    assert "characters" in note and "description" in note
    assert "sfx" in note and "bell" in note


def test_the_film_note_keeps_wordless_out_of_a_talking_film():
    """v0.181.272: round 14's chat model put "wordless" in a talking film's tone; Media_Gen
    5.28.13 makes such a film silent."""
    note = MediaConfig().tool_notes["create_short_film"]
    for word in ("wordless", "silent", "no dialogue", "tone", "unless the user asked"):
        assert word in note
    assert len("[MCP:Media_Gen] Foundry note: " + note) < 400     # all of it in the finder


def test_the_note_is_first_so_the_finder_shows_it(svc, client):
    _sync(svc, client, [_tool("create_comic"), _tool("generate_image", "Make a picture.")])
    comic = svc.tool_registry.get("create_comic")
    assert comic.description.startswith("[MCP:Media_Gen] Foundry note: Always pass characters")
    assert LONG.strip() in comic.description                     # the server's text is kept
    assert svc.tool_registry.get("generate_image").description == "[MCP:Media_Gen] Make a picture."
    shown = tool_finder._entry(comic)["description"]             # cut to 400 characters
    assert "Always pass characters" in shown and "sfx" in shown
    assert "Foundry note" in comic.spec()["function"]["description"]


def test_saving_notes_applies_them_without_a_sync(svc, client):
    _sync(svc, client, [_tool("create_comic"), _tool("generate_image", "Make a picture.")])
    keep = dict(svc.config_store.config.media.tool_notes)
    try:
        r = client.post("/admin/api/config/media",
                        json={"tool_notes": "generate_image: say which library\n\n"})
        assert r.status_code == 200
        assert r.json()["media"]["tool_notes"] == {"generate_image": "say which library"}
        assert svc.tool_registry.get("generate_image").description == (
            "[MCP:Media_Gen] Foundry note: say which library\nMake a picture.")
        assert "Foundry note" not in svc.tool_registry.get("create_comic").description
        assert client.post("/admin/api/config/media",
                           json={"tool_notes": "no colon here"}).status_code == 400
        assert client.post("/admin/api/config/media", json={"tool_notes": 5}).status_code == 400
        r = client.post("/admin/api/config/media", json={"tool_notes": {}})
        assert r.json()["media"]["tool_notes"] == {}
        assert "Foundry note" not in svc.tool_registry.get("generate_image").description
    finally:
        client.post("/admin/api/config/media", json={"tool_notes": keep})
    assert "Foundry note" in svc.tool_registry.get("create_comic").description


def test_a_renamed_tool_keeps_its_note_by_its_own_name(svc, client):
    """A name taken by another tool becomes Media_Gen_<name>; the note follows the tool."""
    old = svc.mcp.list_all

    async def fake():
        return {"Other": [_tool("create_comic", "other comic")], "Media_Gen": [_tool("create_comic")]}
    svc.mcp.list_all = fake
    try:
        run(client, svc.tool_registry.sync, _Pool())
    finally:
        svc.mcp.list_all = old
    assert "Foundry note" in svc.tool_registry.get("Media_Gen_create_comic").description
    assert svc.tool_registry.refresh_notes() == 0                # already applied


def test_the_setting_has_a_ui_control():
    html = (Path(tool_finder.__file__).parent / "ui" / "static" / "index.html").read_text(
        encoding="utf-8")
    card = html[html.index("function mediaCard(md) {"):html.index("function bindMediaCard() {")]
    assert 'id="md-notes"' in card and "c.tool_notes" in card
    save = html[html.index("function bindMediaCard() {"):html.index("async function cancelMediaJob(")]
    assert 'tool_notes: $("#md-notes").value' in save
