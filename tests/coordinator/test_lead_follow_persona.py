"""2026-11-03: the owner moved the lead persona from Opus to Sonnet in the router to save API
spend. [lead_manager] expected_models still named Opus, so readiness failed on the route
chain and every lead call was discarded as a served-model mismatch. An empty
expected_models now means: follow the lead persona's strict route chain in the router."""

from types import SimpleNamespace as NS

from foundry_automation.schema import lead_manager_table
from foundry_coordinator.lead import LeadManager

CHAIN = [{"backend": "anthropic", "model": "claude-sonnet-5-5", "source": "paid"},
         {"backend": "cliproxyapi", "model": "gpt-6-astra", "source": "subscription"},
         {"backend": "strata", "model": "qwen3.8-flash-next-iq3_s", "source": "local"}]
SETTINGS = {"expected_models": [], "allow_local_route": True, "enabled": True,
            "persona": "Foundry-Kilo-Lead-Manager", "kilo_client": "kilo"}


def _lead():
    lm = LeadManager.__new__(LeadManager)
    lm.settings = lambda P: dict(SETTINGS)
    lm._routes, lm._ready = {}, {}
    lm.clock = lambda: 1000.0
    return lm


def _project(served, denied=0):
    info = {"exists": True, "enabled": True, "strict": True, "client_allowed": True,
            "client": "kilo", "routes": CHAIN}
    return NS(id="au", client=NS(transcript=lambda sid, directory="": []),
              foundry=NS(persona_info=lambda persona, client="": info,
                         served_models=lambda sid: {
                             "served_models": sorted({m for _, m in served}), "pending": 0,
                             "denied": denied,
                             "routes": [{"backend": b, "model": m} for b, m in served]}))


def test_the_spend_cap_failover_to_meridian_is_accepted():
    # 2026-11-03 HK-ASSET1: anthropic denied at the daily cap, same Sonnet served on Meridian
    lm = _lead()
    v, why, _ = lm._served_check(_project([("meridian", "claude-sonnet-5-5")], denied=3),
                                 "ses1", "Foundry-Kilo-Lead-Manager", "/ws")
    assert v == "ok", why


def test_another_backend_without_a_router_denial_is_still_a_mismatch():
    lm = _lead()
    v, why, _ = lm._served_check(_project([("meridian", "claude-sonnet-5-5")]),
                                 "ses1", "Foundry-Kilo-Lead-Manager", "/ws")
    assert v == "mismatch" and "not routes of the persona's chain" in why


def test_a_failover_never_admits_a_local_model_off_the_chain():
    lm = _lead()
    v, why, _ = lm._served_check(_project([("ollama", "qwen3.8-flash-next-iq3_s")], denied=2),
                                 "ses1", "Foundry-Kilo-Lead-Manager", "/ws")
    assert v == "mismatch", why


def test_an_empty_list_is_valid_in_the_settings():
    out = lead_manager_table("<p>", {"enabled": True, "expected_models": []})
    assert out["expected_models"] == []


def test_readiness_follows_the_persona_chain():
    lm = _lead()
    rows = {r["name"]: r for r in lm._readiness(_project([]))}
    assert rows["route_chain"]["ok"], rows["route_chain"]
    assert "follows the persona's route chain" in rows["route_chain"]["detail"]


def test_a_call_served_by_the_new_model_is_accepted():
    lm = _lead()
    P = _project([("anthropic", "claude-sonnet-5-5")])
    v, why, _ = lm._served_check(P, "ses1", "Foundry-Kilo-Lead-Manager", "/ws")
    assert v == "ok", why
    assert lm._expected(P) == ["claude-sonnet-5-5", "gpt-6-astra", "qwen3.8-flash-next-iq3_s"]


def test_a_model_outside_the_chain_is_still_a_mismatch():
    lm = _lead()
    v, why, _ = lm._served_check(_project([("anthropic", "claude-opus-5-5")]),
                                 "ses1", "Foundry-Kilo-Lead-Manager", "/ws")
    assert v == "mismatch" and "claude-opus-5-5" in why


def test_a_named_list_still_pins_the_lead():
    lm = _lead()
    lm.settings = lambda P: {**SETTINGS, "expected_models": ["claude-opus-5-5"]}
    v, _, _ = lm._served_check(_project([("anthropic", "claude-sonnet-5-5")]),
                               "ses1", "Foundry-Kilo-Lead-Manager", "/ws")
    assert v == "mismatch"
