"""Which model serves the sprints, and the pause when the paid ones are out (modelwatch.py;
items 7b and 7c of COORDINATOR_AUTOMATION_NEEDS). The case is 2026-11-20: Claude backed
off, GPT without credits, sprints written for Sonnet running on the local Qwen, unnoticed."""

from __future__ import annotations

import json

from foundry_coordinator import automation, modelwatch
from foundry_coordinator import states as S
from tests.coordinator.test_budget_policy import game, task

PERSONA = "Foundry-Kilo-Implementer"
ROUTES = [{"backend": "meridian", "model": "claude-sonnet-5-5"},
          {"backend": "chatgpt", "model": "gpt-6.1-sol"},
          {"backend": "strata", "model": "qwen3.8-flash"}]


def window(used, resets_in_s=3600, name="5h", applies=True):
    return {"name": name, "kind": "session", "used": used, "resets_in_s": resets_in_s,
            "applies": applies}


def admission(claude=(40.0, 9000), gpt=(30.0, 5400), claude_cut=100.0, gpt_cut=85.0,
              gpt_unavailable=False):
    return {"routes": [
        {"backend": "meridian", "model": "claude-sonnet-5-5", "state": "admit",
         "reason": "5h 40.0% (below 85%)", "source": "Meridian /v1/quota",
         "primary_cutoff_pct": claude_cut,
         "accounts": [{"account": "meridian:claude", "label": "Claude", "unavailable": False,
                       "windows": [window(*claude), window(99.0, 60, "fable weekly", False)]}]},
        {"backend": "chatgpt", "model": "gpt-6.1-sol", "state": "defer", "reason": "no credits",
         "source": "CLIProxyAPI", "primary_cutoff_pct": gpt_cut,
         "accounts": [{"account": "chatgpt:codex#1", "label": "codex #1",
                       "unavailable": gpt_unavailable, "windows": [window(*gpt)]}]},
        {"backend": "strata", "model": "qwen3.8-flash", "state": "admit", "source": "local",
         "reason": "local backend", "accounts": [], "primary_cutoff_pct": None}]}


class Router:
    def __init__(self, served=None, ans=None, routes=None):
        self.served, self.ans, self.routes = served or [], ans or admission(), routes or ROUTES
        self.calls = {"served": 0, "admission": 0, "persona": 0}
        self.url = ""

    def served_models(self, sid):
        self.calls["served"] += 1
        return {"session_id": sid, "routes": list(self.served),
                "served_models": [r["model"] for r in self.served]}

    def persona_info(self, persona, client=""):
        self.calls["persona"] += 1
        return {"name": persona, "exists": True, "routes": list(self.routes)}

    def helper_admission(self, persona, **kw):
        self.calls["admission"] += 1
        return self.ans


def working(tmp_path, router):
    c, P, clk = game(tmp_path)
    P.foundry = router
    c.store.update_task("game", "T1", state=S.IMPLEMENTING, session_id="ses_1")
    c.store.x("INSERT INTO sessions (session_id, project_id, task_id, run_id, role, phase, "
              "attempt, client, persona, status, created_ts, data) VALUES "
              "(?,?,?,?,?,?,?,?,?,?,?,?)", ("ses_1", "game", "T1", "r1", "implementer",
                                            "implement", 1, "kilo", PERSONA, "running", clk.t, "{}"))
    return c, P, clk


def notices(c, kind):
    return [d for d in (json.loads(r["detail"]) for r in c.store.q(
        "SELECT detail FROM events WHERE kind='operator_notice' ORDER BY id"))
        if d.get("kind") == kind]


# ------------------------------------------------------------------ 7b the alert

def test_replay_a_sonnet_sprint_served_by_the_local_model_raises_one_alert(tmp_path):
    router = Router(served=[ROUTES[0], ROUTES[2]],
                    ans=admission(claude=(100.0, 1500), gpt_unavailable=True))
    c, P, clk = working(tmp_path, router)
    [a] = modelwatch.sweep(c, P)
    assert a["alert_class"] == "watch" and a["kind"] == "lower_model"
    assert a["title"] == "T1 is on a lower model than its persona's first"
    assert a["expected"] == "meridian/claude-sonnet-5-5" and a["served"] == ["strata/qwen3.8-flash"]
    assert (a["rank"], a["of"]) == (3, 3)
    assert a["text"].startswith("T1 is being served by strata/qwen3.8-flash, route 3 of 3 of "
                                "Foundry-Kilo-Implementer; expected meridian/claude-sonnet-5-5.")
    assert "meridian/claude-sonnet-5-5: admit" in a["text"]
    assert "chatgpt/gpt-6.1-sol: defer (no credits)" in a["text"]
    # once per session and route, and the ledger is read every five minutes, not every tick
    for _ in range(30):
        clk.advance(20)
        assert modelwatch.sweep(c, P) == []
    assert router.calls["served"] == 3 and len(notices(c, "lower_model")) == 1
    # it falls further: a route that is not on the list at all is its own alert
    router.served.append({"backend": "brain-lane", "model": "qwen3.8-brain"})
    clk.advance(301)
    [b] = modelwatch.sweep(c, P)
    assert b["served"] == ["brain-lane/qwen3.8-brain"] and b["rank"] == 0
    assert "a route that is not on Foundry-Kilo-Implementer's list" in b["text"]


def test_the_first_route_a_local_first_persona_and_the_switch_off_are_quiet(tmp_path):
    router = Router(served=[ROUTES[0]])
    c, P, clk = working(tmp_path, router)
    assert modelwatch.sweep(c, P) == []                       # served by its first route
    # a persona that starts on the local model is expected to run there
    router.routes = [ROUTES[2], ROUTES[0]]
    router.served = [ROUTES[2]]
    c.__dict__.pop("_persona_routes", None)
    clk.advance(301)
    assert modelwatch.sweep(c, P) == []
    # switched off: the router is not even asked
    router.routes, router.served = ROUTES, [ROUTES[2]]
    c.__dict__.pop("_persona_routes", None)
    automation.set_live(P, "served_model_alert", False)
    n = dict(router.calls)
    clk.advance(301)
    assert modelwatch.sweep(c, P) == [] and router.calls == n
    automation.set_live(P, "served_model_alert", True)
    # nothing served yet, a session that is not working, or a router that fails: no alert
    router.served = []
    clk.advance(301)
    assert modelwatch.sweep(c, P) == []
    router.served_models = lambda sid: 1 / 0
    clk.advance(301)
    assert modelwatch.sweep(c, P) == [] and notices(c, "lower_model") == []


# ------------------------------------------------------------------ 7c the pause

def test_a_route_is_out_only_on_what_the_router_can_read():
    now = 1000.0
    routes = admission(claude=(100.0, 1500))["routes"]
    assert modelwatch.route_out(routes[0], now) == {
        "until": 2500.0, "why": "5h 100% (cutoff 100%)"}      # the fable window does not apply
    assert modelwatch.route_out(admission(claude=(99.9, 1500))["routes"][0], now) is None
    # the helper cutoff (85) is not the sprint's: 90% of a 100% cutoff still serves
    assert modelwatch.route_out(admission(claude=(90.0, 1500))["routes"][0], now) is None
    assert modelwatch.route_out(admission(gpt=(86.0, 5400))["routes"][1], now)["until"] == 6400.0
    un = modelwatch.route_out(admission(gpt_unavailable=True)["routes"][1], now)
    assert un == {"until": None, "why": "codex #1: marked unavailable"}
    assert modelwatch.route_out(routes[2], now) is None       # the local model is never "out"
    # nothing readable: never a guess
    for broken in ({**routes[0], "accounts": []}, {**routes[0], "primary_cutoff_pct": None},
                   {**routes[0], "accounts": [{"windows": [window(None)]}]}):
        assert modelwatch.route_out(broken, now) is None


def test_replay_sprints_wait_for_the_reset_instead_of_running_locally(tmp_path):
    """11-20: Claude at its cutoff, GPT without credits. With the pause on, the sprint waits
    for the earliest reset the router reports; with it off (the default) it starts as before."""
    router = Router(ans=admission(claude=(100.0, 1500), gpt_unavailable=True))
    c, P, clk = game(tmp_path)
    P.foundry = router
    P.profile.roles["implementer"] = PERSONA
    t = task(c)
    assert modelwatch.start_hold(c, P, t, PERSONA) == ""      # off by default
    assert router.calls["admission"] == 0
    automation.set_live(P, "pause_when_paid_models_out", True)
    note = modelwatch.start_hold(c, P, t, PERSONA)
    assert note.startswith("waiting: every paid model of Foundry-Kilo-Implementer is at its "
                           "cutoff; starts again at ") and "no attempt is used" in note
    [n] = notices(c, "paid_models_out")
    assert n["until"] == clk.t + 1500 and "strata/qwen3.8-flash" in n["text"]
    assert "meridian/claude-sonnet-5-5: 5h 100% (cutoff 100%)" in n["why"]
    assert "codex #1: marked unavailable" in n["why"]
    # the engine holds the start and charges nothing
    c._start_implementation(P, t)
    t = task(c)
    assert t["state"] == S.READY and not t.get("session_id") and t["next_action"] == note
    # asked of the router every two minutes, one notice per outage
    for _ in range(10):
        clk.advance(20)
        modelwatch.start_hold(c, P, t, PERSONA)
    assert router.calls["admission"] == 2 and len(notices(c, "paid_models_out")) == 1
    # Claude's window resets: sprints start again, and it is said once
    router.ans = admission(claude=(2.0, 18000), gpt_unavailable=True)
    clk.advance(121)
    assert modelwatch.start_hold(c, P, t, PERSONA) == ""
    assert c.store.q("SELECT COUNT(*) AS n FROM events WHERE kind='paid_models_back'")[0]["n"] == 1


def test_one_paid_route_still_serving_or_a_local_first_persona_never_pauses(tmp_path):
    c, P, clk = game(tmp_path)
    automation.set_live(P, "pause_when_paid_models_out", True)
    P.foundry = Router(ans=admission(claude=(100.0, 1500)))   # GPT still serves
    assert modelwatch.start_hold(c, P, task(c), PERSONA) == ""
    local_first = admission(claude=(100.0, 1500), gpt_unavailable=True)
    local_first["routes"] = [local_first["routes"][2]] + local_first["routes"][:2]
    P.foundry = Router(ans=local_first)
    c.__dict__.pop("_admission", None)
    assert modelwatch.start_hold(c, P, task(c), PERSONA) == ""
    # the router does not answer: no pause on a guess
    P.foundry = Router()
    P.foundry.helper_admission = lambda persona, **kw: 1 / 0
    c.__dict__.pop("_admission", None)
    assert modelwatch.start_hold(c, P, task(c), PERSONA) == ""
    assert modelwatch.paid_out(None, clk.t) is None and modelwatch.paid_out({"routes": []}, 0) is None


def test_the_settings_are_typed_in_the_schema_and_the_pause_ships_off():
    from foundry_automation import schema
    for k, default in (("served_model_alert", True), ("pause_when_paid_models_out", False)):
        assert schema.find_field(f"automation.{k}") is not None and schema._AUTO_DESC[k]
        assert schema.AUTOMATION_DEFAULTS[k] is default
