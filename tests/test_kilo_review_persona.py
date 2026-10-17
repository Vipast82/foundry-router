"""Canonical Foundry-Kilo-Review: seeded complete on fresh databases, added to
upgrades only when missing, never overwriting edits; strict ordered routing that
fails closed; exact client authorization (legacy aliases don't count); the
generated Kilo config names the role and explains its limits; setup presets
and diagnostics."""

import json

from foundry_router.config import ClientConfig
from foundry_router.db import KILO_REVIEW_PERSONA, KILO_REVIEW_SEED_TAG, Database
from foundry_router.facade.ollama_api import persona_meta, strict_candidates, strict_chain
from foundry_router.kilo_roles import coordinator_role_gaps, display_name
from foundry_router.personas import PersonaStore
from tests.test_persona_profiles import HITS, KILO, STATE, _chat, fleet  # noqa: F401

REVIEW = "Foundry-Kilo-Review"
POLICY = {"execution_mode": "direct", "routing": "strict", "backend": None,
          "pinned_models": ["claude-sonnet-5-5", "gpt-6.1-sol"],
          "model_allowlist": ["claude-sonnet-5-5", "gpt-6.1-sol"], "reasoning_effort": "medium",
          "fallback_policy": "refuse", "context_window": 200000, "max_output_tokens": 4096,
          "capabilities": ["text", "tools"], "kind": "chat", "enabled": 1}


def _policy(row):
    out = {k: row[k] for k in POLICY}
    for k in ("pinned_models", "model_allowlist", "capabilities"):
        out[k] = json.loads(out[k])
    return out


def _pre_v11(path, customize=None):
    """A database as it was before this release: no seed flag, and either no
    Review persona or the operator's own one."""
    db = Database(path)
    db.execute("DELETE FROM personas WHERE virtual_name=?", (REVIEW,))
    db.execute("DELETE FROM kv WHERE key='persona_seed_v11_kilo_review'")
    db.execute("DELETE FROM event_log WHERE message LIKE 'persona seed v11%'")
    if customize is not None:
        PersonaStore(db).upsert(REVIEW, **customize)
    db.close()


# -- seeding -------------------------------------------------------------------

def test_fresh_database_gets_the_complete_policy_unvalidated(tmp_path):
    db = Database(tmp_path / "d.sqlite")
    row = PersonaStore(db).get(REVIEW)
    assert _policy(row) == POLICY
    assert row["validation"] is None                  # created, not validated
    assert row["profile"] == KILO_REVIEW_SEED_TAG
    assert set(json.loads(row["client_compat"])) == {"foundry_coordinator", "Kilo"}
    assert "image_input" not in row["capabilities"]
    assert set(KILO_REVIEW_PERSONA) - {"virtual_name"} <= set(row)   # every field is a column


def test_upgrade_adds_a_missing_review_persona_once(tmp_path):
    path = tmp_path / "d.sqlite"
    _pre_v11(path)
    db = Database(path)
    assert _policy(PersonaStore(db).get(REVIEW)) == POLICY
    first = PersonaStore(db).get(REVIEW)["updated_at"]
    db.close()
    db = Database(path)                               # repeated upgrade / restart
    assert len(db.query("SELECT * FROM personas WHERE virtual_name=?", (REVIEW,))) == 1
    assert PersonaStore(db).get(REVIEW)["updated_at"] == first
    events = db.query("SELECT message FROM event_log WHERE message LIKE 'persona seed v11%'")
    assert len(events) == 1


def test_upgrade_keeps_a_customized_or_disabled_review_persona(tmp_path):
    path = tmp_path / "d.sqlite"
    mine = {"description": "my reviewer", "enabled": 0, "routing": "strict", "backend": "cpa",
            "pinned_models": ["gpt-6.1-sol"], "model_allowlist": ["gpt-6.1-sol"],
            "context_window": 32768, "max_output_tokens": 2000, "reasoning_effort": "high"}
    _pre_v11(path, mine)
    db = Database(path)
    row = PersonaStore(db).get(REVIEW)
    assert row["description"] == "my reviewer" and row["enabled"] == 0
    assert row["backend"] == "cpa" and json.loads(row["pinned_models"]) == ["gpt-6.1-sol"]
    assert row["context_window"] == 32768 and row["max_output_tokens"] == 2000
    assert row["reasoning_effort"] == "high" and row["profile"] in (None, "")
    assert db.query("SELECT message FROM event_log WHERE message LIKE 'persona seed v11%'")[0][
        "message"].endswith("left unchanged")


def test_a_deleted_review_persona_is_not_recreated(tmp_path):
    path = tmp_path / "d.sqlite"
    db = Database(path)
    PersonaStore(db).delete(REVIEW)
    db.close()
    assert PersonaStore(Database(path)).get(REVIEW) is None


def test_legacy_personas_are_untouched(tmp_path):
    path = tmp_path / "d.sqlite"
    db = Database(path)
    PersonaStore(db).upsert("Foundry-Kilo-Sonnet", description="legacy", enabled=1)
    db.execute("DELETE FROM kv WHERE key='persona_seed_v11_kilo_review'")
    db.close()
    db = Database(path)
    assert PersonaStore(db).get("Foundry-Kilo-Sonnet")["description"] == "legacy"


# -- routing ----------------------------------------------------------------------

def test_ordered_chain_and_fail_closed(fleet, client):
    row = fleet.personas.get(REVIEW)
    assert strict_chain(row) == ["claude-sonnet-5-5", "gpt-6.1-sol"]
    assert strict_candidates(fleet, row) == ["claude-sonnet-5-5", "gpt-6.1-sol"]
    assert _chat(client, REVIEW).status_code == 200
    assert HITS == [("meridian", "claude-sonnet-5-5")]
    HITS.clear()
    STATE["fail"] = ("meridian",)                     # first pin fails: the second serves
    assert _chat(client, REVIEW).status_code == 200
    assert HITS[-1] == ("cliproxy", "gpt-6.1-sol")
    HITS.clear()
    STATE["fail"] = ("meridian", "cliproxy")          # both fail: refuse, nothing local
    r = _chat(client, REVIEW)
    assert r.status_code >= 400
    assert not [h for h in HITS if h[0] == "strata"]


def test_unreachable_pins_are_reported_not_replaced(fleet, client):
    for b in ("meridian", "cliproxy"):
        fleet.pool.backends[b].models = [m for m in fleet.pool.backends[b].models
                                         if m not in ("claude-sonnet-5-5", "gpt-6.1-sol")]
    row = fleet.personas.get(REVIEW)
    assert strict_candidates(fleet, row) == []
    meta = persona_meta(fleet, row)
    assert meta["limit_sources"]["unreachable_pins"] == ["claude-sonnet-5-5", "gpt-6.1-sol"]
    r = _chat(client, REVIEW)
    assert r.status_code >= 400 and HITS == []


# -- authorization ----------------------------------------------------------------

def test_legacy_alias_does_not_authorize_the_canonical_reviewer(fleet, client):
    cfg = fleet.config_store.config
    cfg.clients.append(ClientConfig(name="coordinator", key="coord-legacy", scopes=[
        "inference", "orchestration"], personas=["Foundry-Kilo-Sonnet", "Foundry-Kilo-Opus"]))
    h = {"Authorization": "Bearer coord-legacy"}
    r = client.post("/v1/chat/completions", headers=h, json={
        "model": REVIEW, "stream": False, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403 and "model_not_allowed" in r.text
    assert REVIEW not in {m["id"] for m in client.get("/v1/models", headers=h).json()["data"]}
    # explicitly authorized (only the canonical reviewer, not Local): it works
    cfg.clients[-1].personas = ["Foundry-Kilo-Sonnet", "Foundry-Kilo-Opus", REVIEW]
    ids = {m["id"] for m in client.get("/v1/models", headers=h).json()["data"]}
    assert REVIEW in ids and "Foundry-Kilo-Local" not in ids
    r = client.post("/v1/chat/completions", headers=h, json={
        "model": REVIEW, "stream": False, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200


def test_kilo_key_still_sees_its_worker_model(fleet, client):
    client.post(f"/admin/api/profiles/{KILO}/apply")
    fleet.config_store.config.clients.append(ClientConfig(
        name="kilo", key="kilo-key", scopes=["inference"],
        personas=["Foundry-Kilo-Local", REVIEW]))
    ids = {m["id"] for m in client.get("/v1/models", headers={
        "Authorization": "Bearer kilo-key"}).json()["data"]}
    assert {"Foundry-Kilo-Local", REVIEW} <= ids


# -- generated Kilo config ------------------------------------------------------------

def test_kilo_config_names_the_role_and_explains_limits(fleet, client):
    client.post(f"/admin/api/profiles/{KILO}/apply")
    fleet.registry.upsert_auto("claude-sonnet-5-5", source="discovery", context_length=200000)
    fleet.registry.upsert_auto("gpt-6.1-sol", source="discovery", context_length=32768)
    plain = client.get("/admin/api/personas/kilo-config?base_url=http://f:1").json()
    models = plain["provider"]["foundry"]["models"]
    assert set(plain) == {"provider", "enabled_providers", "subagent_depth"}   # mergeable as is
    assert models[REVIEW]["name"] == "Foundry-Kilo-Review (normal reviewer)"
    assert "legacy alias" in models["Foundry-Kilo-Sonnet"]["name"]
    # the fallback pin's smaller window caps the whole chain, and says so
    assert models[REVIEW]["limit"] == {"context": 32768, "output": 4096}
    ex = client.get("/admin/api/personas/kilo-config?explain=1&base_url=http://f:1").json()
    assert ex["config"] == plain
    lim = ex["limits"][REVIEW]
    assert lim["context_source"] == "model window of gpt-6.1-sol (registry)"
    assert lim["declared_context"] == 200000 and lim["validation"] == "not validated"
    assert "already-saved kilo.jsonc" in ex["policy"]


def test_kilo_config_uses_the_declaration_when_no_window_is_smaller(fleet, client):
    fleet.registry.upsert_auto("claude-sonnet-5-5", source="discovery", context_length=400000)
    ex = client.get("/admin/api/personas/kilo-config?explain=1&base_url=http://f:1").json()
    assert ex["config"]["provider"]["foundry"]["models"][REVIEW]["limit"]["context"] == 200000
    assert ex["limits"][REVIEW]["context_source"] == "persona declaration"
    lim = ex["limits"][REVIEW]
    assert "gpt-6.1-sol" in lim["unknown_windows"] or lim["model_windows"]["gpt-6.1-sol"] >= 200000


# -- setup presets and diagnostics -------------------------------------------------------

def test_coordinator_preset_allowlist_is_role_based(client):
    d = client.get("/admin/api/setup/clients").json()
    pre = d["presets"]["coordinator"]
    assert pre["scopes"] == ["inference", "orchestration"]
    assert REVIEW in pre["personas"] and pre["roles"]["reviewer"] == REVIEW
    assert "Foundry-Kilo-Local" not in pre["personas"]
    assert not any(p.startswith("Foundry-Kilo-Sonnet") for p in pre["personas"])
    assert REVIEW in d["personas"]                    # offered in the picker


def test_existing_client_missing_the_reviewer_is_diagnosed_not_changed(client, app):
    cfg = app.state.services.config_store.config
    cfg.clients.append(ClientConfig(name="coordinator", key="ck", scopes=[
        "inference", "orchestration"], personas=["Foundry-Kilo-Sonnet", "Foundry-Kilo-Deep-Review"]))
    cfg.clients.append(ClientConfig(name="other", key="ok", scopes=["inference"],
                                    personas=["Foundry-Chat"]))
    rows = {c["name"]: c for c in client.get("/admin/api/setup/clients").json()["clients"]}
    gaps = rows["coordinator"]["role_gaps"]
    assert [g["persona"] for g in gaps] == [REVIEW]
    assert "legacy aliases don't authorize" in gaps[0]["message"]
    assert rows["other"]["role_gaps"] == []           # not a coordinator key
    assert cfg.clients[-2].personas == ["Foundry-Kilo-Sonnet", "Foundry-Kilo-Deep-Review"]


def test_role_helpers():
    assert coordinator_role_gaps(["inference", "orchestration"], []) == []   # all allowed
    assert coordinator_role_gaps(["inference", "orchestration"], [REVIEW]) == []
    assert display_name("Foundry-Kilo-Local") == "Foundry-Kilo-Local (primary worker)"
    assert display_name("Foundry-Chat") == "Foundry-Chat"
