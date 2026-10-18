"""Multi-route helper admission (work_api.helper_admission + quota_gate +
orchestration pinned routes).

The first route of a persona's chain decides; only when it is NOT admitted
(UNKNOWN / DEFER) may a later route of a STRICT chain be admitted, and only one
that is ADMIT itself, not local, has a free helper stream, sees images when the
helper needs them and is in the approved models when given. That admission is
pinned: the bound session's calls go only to that route, and the call-time
gate refuses (optional_deferred), never re-routes, once it stops being
admitted. The first route admitted -> unchanged, nothing pinned.

Fixture fleet (tests/test_persona_profiles.py): Foundry-Kilo-Review is strict,
claude-sonnet-5-5 (meridian) then gpt-6.1-sol (cliproxy). No real quota is
read: the gate's evaluate() is scripted per backend."""

import json

import pytest

from foundry_router.config import ClientConfig
from foundry_router.quota_gate import ADMIT, DEFER, UNKNOWN
from tests.test_persona_profiles import HITS, STATE, fleet  # noqa: F401 (fixture)

COORD = {"Authorization": "Bearer coord-key"}
REVIEW = "Foundry-Kilo-Review"
SOL = {"backend": "cliproxy", "model": "gpt-6.1-sol"}


@pytest.fixture()
def gated(fleet):
    svc = fleet
    svc.config_store.config.helper_policy.optional_enabled = True
    svc.config_store.config.clients += [
        ClientConfig(name="coordinator", key="coord-key", scopes=["inference", "orchestration"])]
    states = {"meridian": (UNKNOWN, "usage endpoint HTTP 429"), "cliproxy": (ADMIT, "5-hour 12%"),
              "strata": (ADMIT, "local")}

    async def evaluate(backend, model):
        st, why = states[backend]
        return {"backend": backend, "model": model, "state": st, "reason": why,
                "next_eligible": None, "accounts": [], "cutoff_pct":
                svc.config_store.config.helper_policy.cutoff_pct,
                "source": "local" if backend == "strata" else "fixture"}
    svc.quota_gate.evaluate = evaluate               # evaluate_fresh() uses it too
    svc.pool.ledger = svc.ledger                     # as main.py wires the real pool
    real_get = svc.registry.get
    vision = {"claude-sonnet-5-5", "gpt-6.1-sol", "qwen3.8-flash-next-iq3_s"}
    svc.registry.get = lambda m: ({**(real_get(m) or {}), "capabilities": '["vision"]'}
                                  if m in vision else real_get(m))
    yield svc, states, vision
    svc.registry.get = real_get


def _adm(client, persona=REVIEW, **q):
    r = client.get("/v1/foundry/helpers/admission", headers=COORD,
                   params={"persona": persona, **q})
    assert r.status_code == 200, r.text
    return r.json()


def _bind(client, sid, purpose="visual_observation", pin=None, persona=REVIEW):
    r = client.post("/v1/foundry/work/sessions", headers=COORD, json={
        "session_id": sid, "project_id": "g", "task_id": "T1", "run_id": "r1", "role": "helper",
        "persona": persona, "purpose": purpose, "meta": {"pinned_route": pin} if pin else {}})
    return r


def _chat(client, sid, persona=REVIEW):
    return client.post("/v1/chat/completions", headers={"x-session-id": sid}, json={
        "model": persona, "stream": False, "messages": [{"role": "user", "content": "hi"}]})


# ------------------------------------------------------------------ admission

def test_unknown_first_route_admits_and_pins_an_eligible_vision_fallback(gated, client):
    svc, _, _ = gated
    a = _adm(client, vision="true", purpose="visual_observation")
    assert a["admit"] is True and a["pinned"] is True and a["state"] == ADMIT
    assert a["route"] == SOL and (a["backend"], a["model"]) == ("cliproxy", "gpt-6.1-sol")
    assert a["first_route"]["backend"] == "meridian" and a["first_route"]["state"] == UNKNOWN
    assert "first route meridian UNKNOWN (usage endpoint HTTP 429" in a["reason"]
    assert "fallback cliproxy admitted" in a["reason"]
    # every route is still reported, with its own image modality
    assert [(r["backend"], r["vision"]) for r in a["routes"]] == [("meridian", True),
                                                                  ("cliproxy", True)]
    # the configured cutoff is what every decision carries (never a constant)
    assert all(r["cutoff_pct"] == svc.config_store.config.helper_policy.cutoff_pct
               for r in a["routes"])


def test_first_route_admitted_is_unchanged_and_not_pinned(gated, client):
    """Documented choice: no pin when the first route admits. The chain and its
    per-route call-time gating behave exactly as before."""
    _, states, _ = gated
    states["meridian"] = (ADMIT, "5-hour 20%")
    a = _adm(client, vision="true")
    assert a["admit"] is True and a["pinned"] is False
    assert a["route"] == {"backend": "meridian", "model": "claude-sonnet-5-5"}
    assert a["reason"] == "5-hour 20%" and "first_route" not in a


def test_no_eligible_fallback_defers_with_the_first_routes_reason(gated, client):
    _, states, _ = gated
    states["meridian"] = (DEFER, "5-hour 93.0% (at or above 90%)")
    states["cliproxy"] = (DEFER, "weekly 95%")
    a = _adm(client)
    assert a["admit"] is False and a["pinned"] is False and a["state"] == DEFER
    assert a["reason"] == "5-hour 93.0% (at or above 90%)"
    assert a["backend"] == "meridian"
    assert a["fallbacks_ineligible"] == ["cliproxy/gpt-6.1-sol: not admitted (DEFER)"]


def test_non_vision_fallback_is_not_used_for_a_vision_helper(gated, client):
    _, _, vision = gated
    vision.discard("gpt-6.1-sol")
    a = _adm(client, vision="true")
    assert a["admit"] is False and a["pinned"] is False and a["state"] == UNKNOWN
    assert a["reason"] == "usage endpoint HTTP 429"
    assert a["fallbacks_ineligible"] == ["cliproxy/gpt-6.1-sol: no image input"]
    # the visual purpose alone implies the image requirement
    assert _adm(client, purpose="visual_observation")["admit"] is False
    # a text helper may still use it
    assert _adm(client)["pinned"] is True


def test_fallback_outside_the_approved_models_is_not_used(gated, client):
    a = _adm(client, vision="true", models="claude-sonnet-5-5,some-other")
    assert a["admit"] is False
    assert a["fallbacks_ineligible"] == ["cliproxy/gpt-6.1-sol: not in the approved models"]
    assert _adm(client, vision="true", models="gpt-6.1-sol")["pinned"] is True


def test_fallback_with_no_free_helper_stream_is_not_used(gated, client):
    svc, _, _ = gated
    real = svc.ledger.backend_streams
    svc.ledger.backend_streams = lambda: [
        {"backend": "cliproxy", "helpers_allowed": 0, "streams_allowed": 1,
         "active": [{"session": "main", "client": "kilo", "persona": "x", "idle_s": 1}]}]
    try:
        a = _adm(client, vision="true")
    finally:
        svc.ledger.backend_streams = real
    assert a["admit"] is False
    assert a["fallbacks_ineligible"] == ["cliproxy/gpt-6.1-sol: no free helper stream"]


def test_local_fallback_is_never_used(gated, client):
    svc, _, _ = gated
    svc.personas.upsert("Helper-Local-Fallback", routing="strict", enabled=1,
                        execution_mode="direct", fallback_policy="refuse",
                        pinned_models=json.dumps(["claude-sonnet-5-5",
                                                  "qwen3.8-flash-next-iq3_s"]),
                        model_allowlist=json.dumps(["claude-sonnet-5-5",
                                                    "qwen3.8-flash-next-iq3_s"]))
    a = _adm(client, persona="Helper-Local-Fallback")
    assert [r["backend"] for r in a["routes"]] == ["meridian", "strata"]
    assert a["admit"] is False and a["reason"] == "usage endpoint HTTP 429"
    assert a["fallbacks_ineligible"] == ["strata/qwen3.8-flash-next-iq3_s: local backend "
                                         "(held by the primary)"]


def test_flexible_persona_never_gets_a_pinned_fallback(gated, client):
    """Strict routing is what honours a pin, so a flexible persona keeps
    today's first-route answer."""
    svc, _, _ = gated
    svc.personas.upsert(REVIEW, routing="flexible")
    a = _adm(client)
    assert a["admit"] is False and a["pinned"] is False and "fallbacks_ineligible" not in a


def test_optional_helpers_off_never_pins(gated, client):
    svc, _, _ = gated
    svc.config_store.config.helper_policy.optional_enabled = False
    a = _adm(client)
    assert a["admit"] is False and a["state"] == "blocked" and a["pinned"] is False


# ------------------------------------------------------------------ pinned call path

def test_pinned_session_calls_only_the_fallback_and_is_refused_when_it_defers(gated, client):
    svc, states, _ = gated
    a = _adm(client, vision="true")
    assert _bind(client, "vis-1", pin=a["route"]).status_code == 200
    assert svc.work.pinned_route("vis-1") == SOL
    HITS.clear()
    r = _chat(client, "vis-1")
    assert r.status_code == 200, r.text
    assert HITS == [("cliproxy", "gpt-6.1-sol")]        # never the unknown first route
    assert r.headers.get("x-foundry-served-backend") == "cliproxy"
    # quota flips: the call-time recheck refuses on the pinned route, no re-route
    states["cliproxy"] = (DEFER, "5-hour 91% (at or above the cutoff)")
    states["meridian"] = (ADMIT, "recovered")          # even an admitted first route is not used
    HITS.clear()
    r = _chat(client, "vis-1")
    assert r.status_code >= 400
    assert HITS == []
    denied = svc.db.query("SELECT backend, model, rule FROM usage_events WHERE status='denied' "
                          "AND session_id='vis-1'")
    assert {"backend": "cliproxy", "model": "gpt-6.1-sol", "rule": "helper-policy"} in denied
    assert not any(d["backend"] == "meridian" for d in denied)


def test_pinned_backend_failure_is_not_rerouted(gated, client):
    svc, states, _ = gated
    states["meridian"] = (ADMIT, "fine")
    assert _bind(client, "vis-2", pin=SOL).status_code == 200
    STATE["fail"] = ("cliproxy",)
    HITS.clear()
    assert _chat(client, "vis-2").status_code >= 400
    assert all(h[0] == "cliproxy" for h in HITS) and HITS


def test_pin_outside_the_strict_chain_is_refused_not_served(gated, client):
    """A pin can only narrow a strict chain: a route the persona's allowlist
    doesn't have is refused, nothing is served."""
    assert _bind(client, "vis-3", pin={"backend": "cliproxy", "model": "gpt-6-luna"}
                 ).status_code == 200
    HITS.clear()
    r = _chat(client, "vis-3")
    assert r.status_code >= 400 and HITS == []
    err = r.json()["error"]
    assert err["code"] == "route_unavailable" and "pinned by helper admission" in err["message"]


def test_pin_on_another_backend_than_a_backend_pinned_persona_is_refused(gated, client):
    svc, _, _ = gated
    svc.personas.upsert(REVIEW, backend="meridian")
    assert _bind(client, "vis-4", pin=SOL).status_code == 200
    HITS.clear()
    assert _chat(client, "vis-4").status_code >= 400 and HITS == []


def test_call_gate_refuses_a_pinned_session_off_its_route(gated):
    """Belt and braces at the gate: whatever routing did, a pinned session's call
    on any other route is refused (optional_deferred)."""
    import asyncio
    from foundry_router.budget import CallCtx
    svc, _, _ = gated
    svc.work.bind({"session_id": "vis-5", "project_id": "g", "purpose": "visual_observation",
                   "meta": {"pinned_route": SOL}})
    gate = svc.quota_gate
    d = asyncio.run(gate.admit_call(CallCtx(purpose="visual_observation", session_id="vis-5",
                                            backend="meridian", model="claude-sonnet-5-5")))
    assert d and d["code"] == "optional_deferred" and "re-route" in d["message"]
    assert asyncio.run(gate.admit_call(CallCtx(purpose="visual_observation", session_id="vis-5",
                                               **SOL))) is None


def test_bad_pinned_route_is_refused_at_bind(gated, client):
    assert _bind(client, "vis-6", pin={"backend": "cliproxy"}).status_code == 400
    assert _bind(client, "vis-6", pin={"backend": "a b", "model": "m"}).status_code == 400


def test_required_work_is_unaffected(gated, client):
    """Implementer/review sessions are never pinned and keep the whole chain."""
    _, states, _ = gated
    states["meridian"] = (UNKNOWN, "429")
    r = client.post("/v1/foundry/work/sessions", headers=COORD, json={
        "session_id": "rev-1", "project_id": "g", "task_id": "T1", "run_id": "r1",
        "role": "reviewer", "persona": REVIEW, "purpose": "review"})
    assert r.status_code == 200
    HITS.clear()
    assert _chat(client, "rev-1").status_code == 200
    assert HITS == [("meridian", "claude-sonnet-5-5")]   # required work: no optional gate
