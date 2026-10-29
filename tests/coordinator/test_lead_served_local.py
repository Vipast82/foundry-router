"""2026-10-29 AS-REG1: Claude hit its usage cutoff and the lead's calls stepped down to the
local Qwen listed in [lead_manager] expected_models (allow_local_route = true). The
after-call served-model check still called a local route a mismatch and discarded the
lead's reports. It now honours allow_local_route like the readiness check."""

from types import SimpleNamespace as NS

from foundry_coordinator.lead import LeadManager

EXPECTED = ["claude-opus-5-5", "gpt-6-astra", "qwen3.8-flash-next-iq3_s"]
CHAIN = [{"backend": "meridian", "model": "claude-opus-5-5", "source": "subscription"},
         {"backend": "cliproxyapi", "model": "gpt-6-astra", "source": "subscription"},
         {"backend": "strata", "model": "qwen3.8-flash-next-iq3_s", "source": "local"}]


def _check(allow_local, routes):
    lm = LeadManager.__new__(LeadManager)
    lm.settings = lambda P: {"expected_models": EXPECTED, "allow_local_route": allow_local}
    lm._routes = {"au": CHAIN}
    models = sorted({m for _, m in routes})
    P = NS(id="au", client=NS(transcript=lambda sid, directory="": []),
           foundry=NS(served_models=lambda sid: {
               "served_models": models, "pending": 0,
               "routes": [{"backend": b, "model": m} for b, m in routes]}))
    return lm._served_check(P, "ses1", "Foundry-Kilo-Lead-Manager", "/ws")


def test_a_local_step_down_is_accepted_when_allowed():
    v, why, _ = _check(True, [("meridian", "claude-opus-5-5"),
                              ("strata", "qwen3.8-flash-next-iq3_s")])
    assert v == "ok", why


def test_a_local_route_is_still_a_mismatch_when_not_allowed():
    v, why, _ = _check(False, [("meridian", "claude-opus-5-5"),
                               ("strata", "qwen3.8-flash-next-iq3_s")])
    assert v == "mismatch" and "local or unknown source" in why


def test_an_unknown_backend_is_a_mismatch_even_when_local_is_allowed():
    v, why, _ = _check(True, [("elsewhere", "claude-opus-5-5")])
    assert v == "mismatch" and "not routes of the persona's chain" in why
