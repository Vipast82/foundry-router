"""Persona /api/show reports a real context_length in model_info: a persona
`context_window` override wins if set, else the MAX known context across the
persona's routable candidates (so a heterogeneous fleet advertises the ceiling
it can reach, not the floor of the weakest worker). A virtual persona otherwise
returns nothing there and clients fall back to a tiny internal guess."""

import types

from foundry_router.db import Database
from foundry_router.facade import translate as tr
from foundry_router.facade.ollama_api import _persona_context_length
from foundry_router.registry.models_db import ModelRegistry


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


def _svc(tmp_path):
    registry = ModelRegistry(Database(tmp_path / "s.sqlite"))
    registry.upsert_auto("big", source="discovery", relative_cost_tier="free",
                         context_length=262144)
    registry.upsert_auto("small", source="discovery", relative_cost_tier="free",
                         context_length=32768)
    registry.upsert_auto("unknown", source="discovery", relative_cost_tier="free")  # no ctx
    pool = types.SimpleNamespace(
        available_models=lambda: {"big": ["b"], "small": ["b"], "unknown": ["b"]})
    return types.SimpleNamespace(pool=pool, registry=registry)


def test_context_length_falls_back_to_max_candidate(tmp_path):
    # no override -> advertise the ceiling the persona can reach, not the floor
    ctx = _persona_context_length(_svc(tmp_path), {"benchmark_category": "general_chat"})
    assert ctx == 262144


def test_context_window_override_wins(tmp_path):
    ctx = _persona_context_length(
        _svc(tmp_path), {"benchmark_category": "general_chat", "context_window": 131072})
    assert ctx == 131072          # pinned, ignores the 262144 candidate max


def test_context_window_zero_and_negative_mean_auto(tmp_path):
    for bad in (0, -5, "", None):
        ctx = _persona_context_length(
            _svc(tmp_path), {"benchmark_category": "general_chat", "context_window": bad})
        assert ctx == 262144      # falls through to the discovered max


def test_context_window_persists_through_persona_endpoint(client):
    client.post("/admin/api/personas",
                json={"virtual_name": "Foundry-Chat", "context_window": 200000})
    personas = {p["virtual_name"]: p
                for p in client.get("/admin/api/personas").json()["personas"]}
    assert personas["Foundry-Chat"]["context_window"] == 200000
