"""Client-aware persona output profiles (quality spec Phase 5): compat notes
are documentation metadata seeded onto the starter personas (never clobbering
operator edits), surfaced via the personas API for the GUI badges; the one
behavioral knob is output_style=plain_text, which steers the brain prompt and
worker-tool prompt toward markup-free output for messaging bridges."""

import json

from foundry_router.brain import prompts
from foundry_router.db import Database, STARTER_CLIENT_COMPAT
from foundry_router.personas import PersonaStore


def test_starter_personas_seeded_with_compat_notes(tmp_path):
    db = Database(tmp_path / "c.sqlite")
    store = PersonaStore(db)
    chat = store.get("Foundry-Chat")
    compat = json.loads(chat["client_compat"])
    assert "openwebui" in compat and "Artifacts" in compat["openwebui"]
    assert "anythingllm" in compat and "tool-calling" in compat["anythingllm"]
    # every starter with baseline notes got them
    for name in STARTER_CLIENT_COMPAT:
        assert json.loads(store.get(name)["client_compat"])


def test_compat_seed_never_clobbers_operator_edits(tmp_path):
    db = Database(tmp_path / "c2.sqlite")
    store = PersonaStore(db)
    store.upsert("Foundry-Chat", client_compat={"mycustomclient": "works great"})
    db.kv_del("persona_seed_v3_compat")      # simulate the seed running again
    db._seed_client_compat()
    compat = json.loads(store.get("Foundry-Chat")["client_compat"])
    assert compat == {"mycustomclient": "works great"}


def test_compat_and_style_roundtrip_through_api(client):
    client.post("/admin/api/personas", json={
        "virtual_name": "Hermes-Bridge",
        "description": "Discord bridge persona",
        "benchmark_category": "general_chat",
        "client_compat": {"hermes": "plain text only — Discord can't render HTML"},
        "output_style": "plain_text"})
    d = client.get("/admin/api/personas").json()
    row = next(p for p in d["personas"] if p["virtual_name"] == "Hermes-Bridge")
    assert json.loads(row["client_compat"])["hermes"].startswith("plain text")
    assert row["output_style"] == "plain_text"


def test_plain_text_style_steers_brain_prompt():
    persona = {"virtual_name": "Hermes", "output_style": "plain_text"}
    system = prompts.build_system_prompt(persona, [], {}, "n/a", None)
    assert "OUTPUT STYLE — PLAIN TEXT" in system
    assert "no HTML/SVG" in system
    # default personas get no steering block
    plain = prompts.build_system_prompt({"virtual_name": "Chat"}, [], {}, "n/a", None)
    assert "OUTPUT STYLE" not in plain


def test_plain_text_style_steers_worker_tool_prompt():
    styled = prompts.build_worker_tool_prompt(output_style="plain_text")
    assert "OUTPUT STYLE — PLAIN TEXT" in styled
    unstyled = prompts.build_worker_tool_prompt()
    assert "OUTPUT STYLE" not in unstyled
    # client workspace instructions still ride along after the style block
    both = prompts.build_worker_tool_prompt("Answer in French.",
                                            output_style="plain_text")
    assert both.index("OUTPUT STYLE") < both.index("Answer in French.")
