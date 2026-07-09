"""Persona system (design doc §4.8): seeding, tags round-trip, admin CRUD."""


def test_starter_personas_seeded(client):
    d = client.get("/admin/api/personas").json()
    names = {p["virtual_name"] for p in d["personas"]}
    assert {"Foundry-Coding", "Foundry-Chat", "Foundry-Research", "Foundry-RAG"} <= names


def test_new_persona_appears_in_tags_without_code_change(client):
    r = client.post("/admin/api/personas", json={
        "virtual_name": "Foundry-Creative",
        "description": "creative work, prefers local generation",
        "benchmark_category": "general_chat",
        "local_bias_strength": "strong",
        "preferred_mcp_tools": ["comfyui_mcp"],
    })
    assert r.status_code == 200
    tags = {m["name"] for m in client.get("/api/tags").json()["models"]}
    assert "Foundry-Creative" in tags


def test_disabled_persona_leaves_tags(client):
    client.post("/admin/api/personas", json={
        "virtual_name": "Foundry-RAG", "enabled": 0})
    tags = {m["name"] for m in client.get("/api/tags").json()["models"]}
    assert "Foundry-RAG" not in tags
    # but the row still exists (toggle off without deleting, per schema)
    d = client.get("/admin/api/personas").json()
    assert any(p["virtual_name"] == "Foundry-RAG" for p in d["personas"])


def test_seeding_does_not_clobber_edits(tmp_path):
    """INSERT OR IGNORE semantics: a restart must not reset user edits."""
    from foundry_router.db import Database
    from foundry_router.personas import PersonaStore
    path = tmp_path / "p.sqlite"
    store = PersonaStore(Database(path))
    store.upsert("Foundry-Chat", description="my edited description")
    # simulate restart: new Database instance re-runs seeding on same file
    store2 = PersonaStore(Database(path))
    assert store2.get("Foundry-Chat")["description"] == "my edited description"


def test_guardrail_override_parsing(client):
    d = client.get("/admin/api/personas").json()
    coding = next(p for p in d["personas"] if p["virtual_name"] == "Foundry-Coding")
    import json as j
    assert j.loads(coding["guardrail_overrides"])["max_paid_calls_per_request"] == 2
