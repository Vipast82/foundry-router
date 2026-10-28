"""Self-driving part 1 (Victor 2026-10-28, reports/SELF_DRIVING_PLAN.md): only product
decisions can stop a sprint.

  * every decision carries a category (product | user_action | automation), stored on
    the row and exposed in the decision payloads and notices;
  * an automation decision never times out to PARK: its default is a capped self-repair
    (a policy ledger extension, one lead repair, a "different approach" session, the
    frontier implementer, a backoff retry for tooling); it parks only, quietly, when the
    caps are exhausted;
  * the GF-GATE1 06:01 case: a verified head that was never reviewed is reviewed through
    a +1 review_calls extension authorized by `policy`, not parked;
  * product and user_action decisions keep their behaviour.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from foundry_coordinator import decision_policy as DP
from foundry_coordinator import states as S
from foundry_coordinator.obligations import POLICY_ACTOR, LedgerRefused, Obligations
from foundry_coordinator.store import Store
from foundry_coordinator.telemetry import decisions_view
from tests.coordinator.helpers import run_until
from tests.coordinator.test_lead_manager import (drive, env, open_case, state_is,  # noqa: F401
                                                 task)
from tests.coordinator.test_lifecycle_e2e import LifecycleAdapter, green_t1, report
from tests.coordinator.test_lifecycle_engine import Clock, _game, _state

P0 = SimpleNamespace(profile=SimpleNamespace(raw={}))
LEAD_OPTIONS = [DP.PARK, DP.LEAD_ROUND, "give a specific instruction and resume the case"]
FRONTIER = "Foundry-Kilo-Frontier"
FP = f"""
[frontier_primary]
persona = "{FRONTIER}"
"""


def _events(c, kind):
    return [json.loads(r["detail"]) for r in c.store.q(
        "SELECT detail FROM events WHERE kind=? ORDER BY id", (kind,))]


def _lead_esc(c, n, reason="max_fingerprint_repeats"):
    """A lead escalation the way lead._escalate opens one (a new case each time)."""
    return c.obligations.open_decision(
        "game", "T1", "lead_escalation", f"[lead case lc-{n}] the findings repeat ({n})",
        evidence={"reason": reason, "case_id": f"lc-{n}", "recommended": DP.PARK,
                  "trigger": "check core.tests failed: expected OK"},
        options=LEAD_OPTIONS, recommendation=DP.PARK, blocks=[f"lead_case:lc-{n}"])


def _time_out(c, clock, did):
    d = c.obligations.decision(did)
    clock.now = max(clock.now, float(d["deadline_ts"] or clock.now)) + 1
    DP.sweep(c, force=True)
    return c.obligations.decision(did)


# ------------------------------------------------------------------ categories

def test_every_decision_gets_a_category():
    C = DP.category
    # product: blocker_kind product, plan / scope questions, 2D asks, approvals, a lead's
    # own question
    assert C("approval", {}) == "product"
    assert C("scope_request", {}) == "product"
    assert C("obligation_conflict", {}) == "product"
    assert C("implementer_blocked", {"blocker_kind": "product"}) == "product"
    assert C("implementer_blocked", {}) == "product"          # unknown kind = product
    assert C("lead_escalation", {"reason": "lead_asks"}) == "product"
    assert C("lead_escalation", {"reason": "scope_core"}) == "product"
    assert C("whatever", {}, "T1 needs 2D image assets (icons ...)") == "product"
    # user_action: only the owner can physically do it
    assert C("implementer_blocked", {"blocker_kind": "user_action"}) == "user_action"
    assert C("lead_escalation", {"reason": "lead_asks", "user_action": "click Connect"}) == \
        "user_action"
    # automation: tooling, budget, infra, repeated findings, review access, lead
    # escalations of those
    for kind, ev in (("recovery_escalation", {}), ("no_progress", {}), ("review_access", {}),
                     ("budget_extension", {}), ("reset", {}),
                     ("implementer_blocked", {"blocker_kind": "tooling"}),
                     ("lead_escalation", {"reason": "max_fingerprint_repeats"}),
                     ("lead_escalation", {"reason": "lead_hard_cap"}),
                     ("lead_escalation", {"reason": "readiness"})):
        assert C(kind, ev) == "automation", (kind, ev)
    # meta carries it, and an automation decision's default is the self-repair
    m = DP.meta(P0, "lead_escalation", {"reason": "lead_hard_cap", "recommended": DP.PARK},
                LEAD_OPTIONS)
    assert m["category"] == "automation" and json.loads(m["recommended"]) == DP.SELF_REPAIR
    assert not DP.is_park(DP.SELF_REPAIR)
    from foundry_coordinator.lead import LeadManager
    assert LeadManager.ESCALATION_OPTIONS[1] == DP.LEAD_ROUND     # the lead-repair answer
    # product keeps its own default (here the lead's unmatched free text still parks)
    m = DP.meta(P0, "lead_escalation", {"reason": "lead_asks", "recommended": "zzz"},
                ["layout A", "layout B"])
    assert m["category"] == "product" and json.loads(m["recommended"]) == DP.PARK


def test_the_category_is_stored_on_the_decision_and_exposed_in_the_payload(tmp_path):
    c, _, _, _, _ = _game(tmp_path, green_t1)
    c.approve("game", "T1", [], "victor")
    ob = c.obligations
    a = ob.open_decision("game", "T1", "approval", "T1 needs 2D image assets",
                         options=["generate", "provide", "hold"], recommendation="generate")
    b = ob.open_decision("game", "T1", "recovery_escalation", "Findings repeat: fp1",
                         options=[DP.STRATEGY, DP.MORE_BUDGET, DP.PARK], recommendation=DP.PARK)
    u = ob.open_decision("game", "T1", "implementer_blocked", "plug in the controller",
                         evidence={"blocker_kind": "user_action"}, options=["done", "skip"],
                         recommendation="done")
    assert [ob.decision(x)["category"] for x in (a, b, u)] == \
        ["product", "automation", "user_action"]
    # an obligations store without a policy hook classifies too (never NULL)
    plain = Obligations(Store(str(tmp_path / "p.sqlite")))
    assert plain.decision(plain.open_decision("game", "T1", "no_progress", "q"))["category"] \
        == "automation"
    rows = c.store.q("SELECT * FROM decisions WHERE project_id='game'")
    view = {x["decision_id"]: x["category"] for x in decisions_view(rows)}
    assert view == {a: "product", b: "automation", u: "user_action"}
    # an older row without the column value is classified on the way out
    c.store.x("UPDATE decisions SET category=NULL WHERE decision_id=?", (b,))
    rows = c.store.q("SELECT * FROM decisions WHERE decision_id=?", (b,))
    assert decisions_view(rows)[0]["category"] == "automation"


# ------------------------------------------------------------------ the policy authorizer

def test_policy_extensions_are_audited_and_capped_per_task_per_day(tmp_path):
    now = [1000.0]
    ok = {"v": True}
    ob = Obligations(Store(str(tmp_path / "c.sqlite")), clock=lambda: now[0],
                     policy_cap=lambda p, ctr: 2 if ctr == "review_calls" else 0,
                     policy_validator=lambda p, t, ref, ctr: ok["v"])
    ob.authorize("game", "T1", "review_calls", 1, "head a never reviewed", POLICY_ACTOR,
                 "policy:review_calls:aaa")
    for bad in (
            # the same reference twice
            lambda: ob.authorize("game", "T1", "review_calls", 1, "r", POLICY_ACTOR,
                                 "policy:review_calls:aaa"),
            # only the policy actor writes it (never an operator unblock in disguise)
            lambda: ob.authorize("game", "T1", "review_calls", 1, "r", "victor",
                                 "policy:review_calls:bbb"),
            # one unit, of the counter the reference names
            lambda: ob.authorize("game", "T1", "review_calls", 2, "r", POLICY_ACTOR,
                                 "policy:review_calls:bbb"),
            lambda: ob.authorize("game", "T1", "review_calls", 1, "r", POLICY_ACTOR,
                                 "policy:sessions:bbb"),
            # a counter without a cap
            lambda: ob.authorize("game", "T1", "attempt_ordinary", 1, "r", POLICY_ACTOR,
                                 "policy:attempt_ordinary:bbb")):
        with pytest.raises(LedgerRefused):
            bad()
    ok["v"] = False                                    # the engine's condition fails
    with pytest.raises(LedgerRefused):
        ob.authorize("game", "T1", "review_calls", 1, "r", POLICY_ACTOR, "policy:review_calls:b")
    ok["v"] = True
    ob.authorize("game", "T1", "review_calls", 1, "r", POLICY_ACTOR, "policy:review_calls:b")
    with pytest.raises(LedgerRefused, match="cap reached"):
        ob.authorize("game", "T1", "review_calls", 1, "r", POLICY_ACTOR, "policy:review_calls:c")
    # another task has its own cap; a day later the cap is free again
    ob.authorize("game", "T2", "review_calls", 1, "r", POLICY_ACTOR, "policy:review_calls:c")
    now[0] += 86400 + 1
    ob.authorize("game", "T1", "review_calls", 1, "r", POLICY_ACTOR, "policy:review_calls:c")
    assert ob.policy_extensions("game", "T1", "review_calls") == 1
    rows = [r for r in ob.ledger("game", "T1") if r["counter"] == "authorized:review_calls"]
    assert len(rows) == 3 and {r["actor"] for r in rows} == {"policy"}
    # no policy cap hook at all: nothing is authorized by policy
    bare = Obligations(Store(str(tmp_path / "b.sqlite")))
    with pytest.raises(LedgerRefused):
        bare.authorize("game", "T1", "review_calls", 1, "r", POLICY_ACTOR,
                       "policy:review_calls:aaa")


# ------------------------------------------------------------------ GF-GATE1 06:01 replay

def _exhaust_reviews(c):
    P = c.projects["game"]
    lim = c.obligations.effective_limits(P, c.store.task("game", "T1"))["review_calls"]
    for i in range(lim["max"]):
        c.obligations.charge("game", "T1", "review_calls", 1, f"review:old:{i}", "engine")
    assert c.obligations.effective_limits(P, c.store.task("game", "T1"))[
        "review_calls"]["remaining"] == 0


def test_gf_gate1_replay_a_verified_never_reviewed_head_is_reviewed_by_policy(tmp_path):
    """2026-10-28 06:01: GF-GATE1 passed verification and was refused at its very first
    review ("review budget used 3/3"); the lead escalated and the timeout parked it. Now
    the never-reviewed, verified head gets its review through a policy extension."""
    c, _, ff, _, _ = _game(tmp_path, green_t1)
    c.approve("game", "T1", [], "victor")
    _exhaust_reviews(c)
    assert run_until(c, lambda: _state(c) in (S.COMPLETE, S.BLOCKED, S.FAILED), limit=80)
    t = c.store.task("game", "T1")
    assert t["state"] == S.COMPLETE, (t["state"], t["blocker"])
    assert len(ff.chats) == 1                         # exactly the one review
    rows = [r for r in c.obligations.ledger("game", "T1")
            if r["counter"] == "authorized:review_calls"]
    assert len(rows) == 1 and rows[0]["actor"] == "policy" and rows[0]["delta"] == 1
    head = rows[0]["decision_id"].split(":", 2)[2]
    assert rows[0]["decision_id"] == f"policy:review_calls:{head}"
    steps = _events(c, "self_repair")
    assert [s["step"] for s in steps] == ["policy_extension"] and steps[0]["head"] == head
    # that head is reviewed now: the policy condition no longer holds for it
    assert not c._policy_ref_ok("game", "T1", f"policy:review_calls:{head}", "review_calls")
    assert not c.store.q("SELECT 1 FROM decisions WHERE project_id='game'")


def test_the_review_extension_respects_its_cap(tmp_path):
    c, _, ff, _, _ = _game(tmp_path, green_t1, extra="""
        [automation]
        self_repair_max_reviews_per_day = 0
        """)
    c.approve("game", "T1", [], "victor")
    _exhaust_reviews(c)
    assert run_until(c, lambda: _state(c) in (S.COMPLETE, S.BLOCKED, S.FAILED), limit=80)
    t = c.store.task("game", "T1")
    assert t["state"] == S.BLOCKED and "review budget used" in t["blocker"]
    assert not ff.chats
    assert _events(c, "self_repair_refused")[0]["counter"] == "review_calls"


# ------------------------------------------------------------------ the timeout default

def test_a_lead_escalation_times_out_to_the_findings_ladder_never_a_park(tmp_path):
    """The second half of 06:01 (the lead's escalation timed out to "stop the task"): an
    automation escalation self-repairs: one lead repair with the finding text, then a
    different approach; with no frontier persona the ladder then parks quietly."""
    clock = Clock()
    c, _, _, _, _ = _game(tmp_path, green_t1, clock=clock)
    c.approve("game", "T1", [], "victor")
    answers = []
    for n in range(3):
        did = _lead_esc(c, n)
        d = c.obligations.decision(did)
        assert d["category"] == "automation" and json.loads(d["recommended"]) == DP.SELF_REPAIR
        d = _time_out(c, clock, did)
        assert d["status"] == "answered" and d["authority"] == "timeout_default"
        answers.append(d["answer"])
    lead_round, approach, parked = answers
    assert lead_round["option"] == DP.LEAD_ROUND and "core.tests failed" in lead_round["note"]
    assert "materially different approach" in approach["strategy"]
    assert "core.tests failed" in approach["strategy"]
    assert not DP.is_park(lead_round) and not DP.is_park(approach)
    assert parked["stop"] is True and parked["self_repair"]["exhausted"] is True
    assert [s["step"] for s in _events(c, "self_repair")] == \
        ["lead_repair", "different_approach", "exhausted"]
    notices = [n for n in _events(c, "operator_notice") if n.get("kind") == "lead_escalation"]
    assert len(notices) == 3 and all(n["digest"] and n["category"] == "automation"
                                     for n in notices)


def test_the_last_findings_step_is_one_frontier_session(tmp_path, monkeypatch):
    clock = Clock()
    c, _, _, _, _ = _game(tmp_path, green_t1, clock=clock, extra=FP)
    c.approve("game", "T1", [], "victor")
    answers = [_time_out(c, clock, _lead_esc(c, n))["answer"] for n in range(4)]
    assert answers[2]["self_repair"]["persona"] == FRONTIER
    assert FRONTIER in answers[2]["strategy"]
    assert answers[3]["self_repair"]["exhausted"] is True
    t = c.store.task("game", "T1")
    assert t["data"][DP.FRONTIER_KEY]["persona"] == FRONTIER

    # the next implementation session (and only that one) runs on the frontier persona
    monkeypatch.setattr(LifecycleAdapter, "pending_build_steps",
                        lambda self, spec, revision, ws: ["finish the UI"]
                        if ws.show_at(revision, "src/ui.txt") is None else [], raising=False)
    turns = []

    def script(ctx):
        turns.append(ctx["session_id"])
        if len(turns) == 1:
            return {"writes": {"src/core.txt": "OK"}, "commit": "T1: partial core",
                    "final": report(summary="more BUILD remains")}
        return green_t1(ctx)
    c2, _, _, _, _ = _game(tmp_path / "b", script, extra=FP)
    c2.approve("game", "T1", [], "victor")
    c2.store.merge_data("game", "T1", **{DP.FRONTIER_KEY: {"persona": FRONTIER,
                                                           "decision_id": "dec-x"}})
    assert run_until(c2, lambda: _state(c2) == S.IMPLEMENTING, limit=10)
    t = c2.store.task("game", "T1")
    assert t["data"][DP.FRONTIER_KEY]["run_id"] == t["run_id"]
    assert c2.frontier.persona(c2.projects["game"], t) == FRONTIER
    assert run_until(c2, lambda: _state(c2) == S.COMPLETE, limit=80)
    impl = [s["persona"] for s in c2.store.sessions("game", "T1") if s["role"] == "implementer"]
    assert impl == [FRONTIER, "Foundry-Kilo-Local"]


def test_a_budget_stop_gets_a_capped_policy_extension(tmp_path, monkeypatch):
    clock = Clock()
    c, _, _, _, _ = _game(tmp_path, green_t1, clock=clock)
    c.approve("game", "T1", [], "victor")
    monkeypatch.setattr(DP, "repair_trigger", lambda c, d: ("budget", "attempt_ordinary"))
    answers = [_time_out(c, clock, _lead_esc(c, n, "lead_hard_cap"))["answer"]
               for n in range(3)]
    assert all("attempt_ordinary budget was extended" in a["strategy"] for a in answers[:2])
    rows = [r for r in c.obligations.ledger("game", "T1")
            if r["counter"] == "authorized:attempt_ordinary"]
    assert len(rows) == 2 and {r["actor"] for r in rows} == {POLICY_ACTOR}
    assert all(r["decision_id"].startswith("policy:attempt_ordinary:dec-") for r in rows)
    # the cap (2 attempts per task per day) is spent: the third parks quietly
    assert answers[2]["stop"] is True and answers[2]["self_repair"]["exhausted"] is True
    assert "cap reached" in answers[2]["reason"]
    # a review budget stop is verified and reviewed again, not re-implemented
    monkeypatch.setattr(DP, "repair_trigger", lambda c, d: ("budget", "review_calls"))
    a = _time_out(c, clock, _lead_esc(c, 9, "lead_hard_cap"))["answer"]
    assert a["self_repair"]["action"] == "reverify" and a["option"] == DP.REVERIFY


def test_a_tooling_decision_retries_with_backoff_then_parks_quietly(tmp_path):
    clock = Clock()
    c, _, _, _, _ = _game(tmp_path, green_t1, clock=clock, extra="""
        [automation]
        decision_timeout_minutes = 5
        """)
    c.approve("game", "T1", [], "victor")

    def access(n):
        return c.obligations.open_decision(
            "game", "T1", "review_access", f"reviewer could not access the packet ({n})",
            evidence={"recommended": DP.PARK}, options=["access fixed: review again", DP.PARK],
            recommendation=DP.PARK, blocks=[f"T1-{n}"])
    first = _time_out(c, clock, access(0))
    assert first["answer"]["option"] == "access fixed: review again"
    did = access(1)
    d = _time_out(c, clock, did)                  # 5 min later: inside the 10 min backoff
    assert d["status"] == "open" and _events(c, "self_repair_deferred")
    clock.now += 600
    d = _time_out(c, clock, did)
    assert d["status"] == "answered" and d["answer"]["self_repair"]["retry"] == 2
    clock.now += 3600
    assert _time_out(c, clock, access(2))["answer"]["self_repair"]["retry"] == 3
    clock.now += 3600
    last = _time_out(c, clock, access(3))["answer"]
    assert last["stop"] is True and last["self_repair"]["exhausted"] is True


def test_an_exhausted_self_repair_parks_quietly(tmp_path):
    clock = Clock()
    c, _, _, _, _ = _game(tmp_path, green_t1, clock=clock)
    P = c.projects["game"]
    c.approve("game", "T1", [], "victor")
    did = c.obligations.open_decision(
        "game", "T1", "recovery_escalation", "Findings repeat: fp-core",
        evidence={"recommended": DP.PARK}, options=[DP.STRATEGY, DP.MORE_BUDGET, DP.PARK],
        recommendation=DP.PARK, blocks=["T1"])
    c._lc_wait(P, c.store.task("game", "T1"), [did], "findings repeat", S.READY)
    # the different approach was already tried today; no frontier persona configured
    c.store.merge_data("game", "T1", self_repair_log=[
        {"step": "different_approach", "ts": clock.now, "decision_id": "dec-old"}])
    d = _time_out(c, clock, did)
    assert d["answer"]["self_repair"]["exhausted"] is True
    c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.BLOCKED and t["blocker"].startswith("parked")
    parked = _events(c, "sprint_parked")[-1]
    assert parked["alert_class"] == "self_repair_exhausted" and parked["digest"] is True
    notice = [n for n in _events(c, "operator_notice") if n.get("kind") == "sprint_parked"][-1]
    assert notice["alert_class"] == "self_repair_exhausted" and notice["digest"] is True
    assert notice["category"] == "automation"


def test_a_recovery_escalation_resumes_with_a_different_approach(tmp_path):
    clock = Clock()
    c, _, _, _, _ = _game(tmp_path, green_t1, clock=clock)
    P = c.projects["game"]
    c.approve("game", "T1", [], "victor")
    did = c.obligations.open_decision(
        "game", "T1", "recovery_escalation", "Findings repeat: fp-core",
        evidence={"recommended": DP.PARK}, options=[DP.STRATEGY, DP.MORE_BUDGET, DP.PARK],
        recommendation=DP.PARK, blocks=["T1"])
    c._lc_wait(P, c.store.task("game", "T1"), [did], "findings repeat", S.READY)
    d = _time_out(c, clock, did)
    assert "materially different approach" in d["answer"]["strategy"]
    c.tick()
    assert _state(c) != S.BLOCKED
    assert not _events(c, "sprint_parked")


# ------------------------------------------------------------------ unchanged classes

def test_product_and_user_action_decisions_keep_their_behaviour(tmp_path):
    clock = Clock()
    c, _, _, _, _ = _game(tmp_path, green_t1, clock=clock)
    c.approve("game", "T1", [], "victor")
    pd = c.obligations.open_decision(
        "game", "T1", "implementer_blocked", "which layout?",
        evidence={"blocker_kind": "product", "recommended": "layout B"},
        options=["layout A", "layout B"], recommendation="layout B", blocks=["T1"])
    d = _time_out(c, clock, pd)
    assert d["answer"] == "layout B" and d["category"] == "product"
    ud = c.obligations.open_decision(
        "game", "T1", "implementer_blocked", "plug in the controller",
        evidence={"blocker_kind": "user_action"}, options=["done", "skip"],
        recommendation="done", blocks=["T1-u"])
    d = c.obligations.decision(ud)
    assert d["deadline_ts"] is None and d["recommended"] is None
    clock.now += 86400
    DP.sweep(c, force=True)
    assert c.obligations.decision(ud)["status"] == "open"
    assert not _events(c, "self_repair")


def test_self_repair_off_restores_park(tmp_path):
    clock = Clock()
    c, _, _, _, _ = _game(tmp_path, green_t1, clock=clock, extra="""
        [automation]
        self_repair = false
        """)
    c.approve("game", "T1", [], "victor")
    d = _time_out(c, clock, _lead_esc(c, 0))
    assert d["answer"] == DP.PARK and not _events(c, "self_repair")


# ------------------------------------------------------------------ the lead consumer

def test_the_lead_applies_a_self_repair_answer(env, monkeypatch):  # noqa: F811
    env["ff"].info["strict"] = False             # readiness fails: NEEDS_VICTOR, no call
    case = drive(env, open_case(env), state_is("NEEDS_VICTOR"))
    c = env["c"]
    d = c.obligations.decision(case["decision_id"])
    assert d["category"] == "automation" and json.loads(d["recommended"]) == DP.SELF_REPAIR
    monkeypatch.setattr(DP, "repair_trigger", lambda c, d: ("budget", "review_calls"))
    c.store.x("UPDATE decisions SET deadline_ts=? WHERE decision_id=?", (1.0, d["decision_id"]))
    DP.sweep(c, force=True)
    d = c.obligations.decision(d["decision_id"])
    assert d["status"] == "answered" and d["answer"]["self_repair"]["action"] == "reverify"
    case = env["lm"].step(env["P"], task(env), case)
    assert case["state"] == "RESUMING"
    assert case["proposed_action"]["kind"] == "reverify"
