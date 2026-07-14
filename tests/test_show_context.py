"""Persona /api/show now reports a real context_length in model_info, derived
from the persona's routable candidates (safe floor = min known context) — a
virtual persona otherwise returns nothing there and clients fall back to a tiny
internal guess for token budgeting."""

from foundry_router.facade import translate as tr


def test_show_response_includes_context_length():
    out = tr.show_response({"virtual_name": "Foundry-Chat", "description": ""},
                           context_length=131072)
    mi = out["model_info"]
    # both keys so clients find it via general.* or <arch>.*
    assert mi["general.context_length"] == 131072
    assert mi["foundry-router.context_length"] == 131072
    assert mi["general.architecture"] == "foundry-router"


def test_show_response_omits_when_unknown():
    out = tr.show_response({"virtual_name": "Foundry-Chat"}, context_length=None)
    assert "general.context_length" not in out["model_info"]


def test_api_show_returns_model_info(client):
    r = client.post("/api/show", json={"model": "Foundry-Chat"})
    assert r.status_code == 200
    body = r.json()
    assert body["model_info"]["general.architecture"] == "foundry-router"
    assert "tools" in body["capabilities"]


def test_persona_context_length_is_safe_floor(tmp_path):
    # the derivation helper: minimum known context across the persona's candidates
    import types
    from foundry_router.db import Database
    from foundry_router.facade.ollama_api import _persona_context_length
    from foundry_router.registry.models_db import ModelRegistry

    registry = ModelRegistry(Database(tmp_path / "s.sqlite"))
    registry.upsert_auto("big", source="discovery", relative_cost_tier="free",
                         context_length=262144)
    registry.upsert_auto("small", source="discovery", relative_cost_tier="free",
                         context_length=32768)
    registry.upsert_auto("unknown", source="discovery", relative_cost_tier="free")  # no ctx

    pool = types.SimpleNamespace(
        available_models=lambda: {"big": ["b"], "small": ["b"], "unknown": ["b"]})
    svc = types.SimpleNamespace(pool=pool, registry=registry)
    ctx = _persona_context_length(svc, {"benchmark_category": "general_chat"})
    assert ctx == 32768        # the floor — never over-promises what a candidate can hold
