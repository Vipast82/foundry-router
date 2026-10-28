"""Lead hand-back (2026-10-28, GF-GATE1 / HK-CLOSE1).

The repeat guard stopped a sprint with an automation decision (recovery_escalation:
"findings repeat with no materially different strategy"). The Lead manager opened a
case, its readiness check failed `route_chain` because the last-resort local Qwen
route was down (routes [opus, astra] != expected [opus, astra, qwen]: "an ordered
subset ... retried later", transient), and the case went DEFERRED with a doubling
backoff (300, 600, 1200, 2400 s). While a lead case is "working", the decision sweep
follows the case's deadline (hours away: decision_timeout_deferred), so the capped
self-repair never ran and the task stayed BLOCKED "lead recovery: DEFERRED" until the
operator unblocked it by hand (rc-26312bd26f734429, rc-61685c258202a1a4,
rc-8ce16c523c8b75ea, rc-7550934e3efaaf90; no lead call was ever made).

Now:
  * a down last-resort local route (allow_local_route, the tail of expected_models)
    does not block the lead: the primary routes serve it;
  * a lead case that cannot make its call (transient readiness, a quota / admission
    refusal, infrastructure) hands an automation decision back to the capped
    self-repair at once: the case is cancelled, the decision is answered with a
    concrete strategy from the latest refused submission and the repeated findings,
    and the task continues without the operator;
  * a case already DEFERRED (from before) is handed back on its next step or when
    the decision's own timeout comes due;
  * product decisions, self_repair = false and a case with a lead call in flight
    keep the old behaviour.
"""

from __future__ import annotations

import json

from foundry_coordinator import decision_policy as DP
from foundry_coordinator import states as S
from tests.coordinator.test_lead_manager import (ASTRA, OPUS, drive, env, open_case,  # noqa: F401
                                                 state_is, task)

QWEN = "qwen3.8-flash-next-iq3_s"

# the live HK-CLOSE1 decision evidence (dec-f00bb47c0c5f2d8c), trimmed
REPEATS = [
    {"check_id": "gate:HK-CLOSE1", "class": "missing_evidence", "count": 2,
     "fingerprint": "091911e42973e2c0", "path": "tests/regressionindex.lua",
     "text": "check gate:HK-CLOSE1: stale (dependency changed since c8483407106d: "
             "tests/RegressionIndex.lua, tests/checks/HK_CLOSE1_check.lua)"},
    {"check_id": "", "class": "missing_evidence", "count": 2,
     "fingerprint": "53acf74842a970e5", "path": "",
     "text": "obligation build.gate (GATE HK-CLOSE1 PASS bound to the reviewed commit): "
             "check gate:HK-CLOSE1: no fresh verified record at 84f6c8fa510c"}]
REFUSED = {"head": "84f6c8fa510c87d86fec23049eaf911c90e202e6", "items": [
    {"action": "changes_required", "class": "missing_evidence",
     "item": {"obligation_id": "build.gate",
              "reason": "check gate:HK-CLOSE1: no fresh verified record at 84f6c8fa510c",
              "title": "GATE HK-CLOSE1 PASS bound to the reviewed commit"}},
    {"action": "changes_required", "class": "missing_evidence",
     "item": {"check_id": "gate:HK-CLOSE1",
              "detail": "dependency changed since c8483407106d: tests/RegressionIndex.lua"}}]}


def _events(c, kind):
    return [json.loads(r["detail"]) for r in c.store.q(
        "SELECT detail FROM events WHERE kind=? ORDER BY id", (kind,))]


def _repeat_stop(e, kind="recovery_escalation", evidence=None, options=None,
                 recommendation=DP.PARK):
    """The repeat guard's stop, the way the engine leaves it: an open decision, the
    task BLOCKED on it (decision_wait), the latest refused submission journaled."""
    c, P = e["c"], e["P"]
    c.store.update_task("game", "T1", state="CHANGES_REQUIRED", blocker="")
    c.store.event("game", "T1", "submission_refused", detail=REFUSED)
    c.store.merge_data("game", "T1", findings=[
        "reviewer (major) docs/progress/HK-CLOSE1_PLAN.md:1: the plan file was re-encoded",
        "verifier: check gate:HK-CLOSE1: no fresh verified record at 84f6c8fa510c"])
    ev = evidence if evidence is not None else {
        "adviser": "adviser:unavailable", "attempt": 6,
        "last_strategy": "Repair the unresolved obligations build.gate",
        "recommended": DP.PARK, "rejected_guidance": [], "repeats": REPEATS}
    did = c.obligations.open_decision(
        "game", "T1", kind, "Findings repeat across attempts with no materially different "
        "strategy: 091911e42973e2c0, 53acf74842a970e5", evidence=ev,
        options=options or [DP.STRATEGY, DP.MORE_BUDGET, DP.PARK],
        recommendation=recommendation, blocks=["T1"])
    c._lc_wait(P, task(e), [did], "findings repeat with no materially different strategy "
               "(adviser consulted at most once)", S.READY)
    assert task(e)["state"] == S.BLOCKED
    return did


def _lead_case(e, did):
    """engine._lead_from_wait: the lead case for the stop (trigger decision_<kind>)."""
    c = e["c"]
    c._lead_from_wait(e["P"], task(e), [c.obligations.decision(did)])
    cid = (task(e)["data"] or {}).get("recovery_case")
    assert cid
    return c.lead.case(cid)


def _local_tail_down(e):
    """The live 2026-10-28 chain: Qwen (last-resort local) is down."""
    e["ff"].info["routes"] = e["ff"].info["routes"][:2]          # [opus, astra]


# ------------------------------------------------------------------ readiness

def test_a_down_last_resort_local_route_does_not_block_the_lead(env):  # noqa: F811
    P = env["P"]
    P.profile.lead_manager["expected_models"] = [OPUS, ASTRA, QWEN]
    P.profile.lead_manager["allow_local_route"] = True
    _local_tail_down(env)
    lm = env["lm"]
    res = {r["name"]: r for r in lm.readiness(P, fresh=True)}
    assert res["route_chain"]["ok"] is True, res["route_chain"]
    assert "last-resort fallback" in res["route_chain"]["detail"]
    # a primary route down is still a transient outage (Review A #7)
    env["ff"].info["routes"] = env["ff"].info["routes"][1:]      # [astra]
    res = {r["name"]: r for r in lm.readiness(P, fresh=True)}
    assert res["route_chain"]["ok"] is False and res["route_chain"]["cls"] == "transient"
    # without allow_local_route the tail is a normal route: transient as before
    P.profile.lead_manager["allow_local_route"] = False
    env["ff"].info["routes"] = env["ff"].info["routes"][:0] + [
        {"backend": "meridian", "model": OPUS, "source": "subscription"},
        {"backend": "cliproxy", "model": ASTRA, "source": "subscription"}]
    res = {r["name"]: r for r in lm.readiness(P, fresh=True)}
    assert res["route_chain"]["ok"] is False and res["route_chain"]["cls"] == "transient"


# ------------------------------------------------------------------ the hand-back

def test_hk_close1_replay_a_lead_that_cannot_run_hands_the_stop_to_the_self_repair(env):  # noqa: F811,E501
    """The live sequence: open_case -> new_failure -> readiness failed (route_chain,
    transient) -> (was: defer 300 s, 600 s, ... until the operator). Now the case
    hands back and the task continues with a concrete strategy, no operator."""
    c = env["c"]
    env["ff"].info["routes"] = env["ff"].info["routes"][:1]      # astra down: transient
    did = _repeat_stop(env)
    d = c.obligations.decision(did)
    assert d["category"] == "automation" and json.loads(d["recommended"]) == DP.SELF_REPAIR
    case = _lead_case(env, did)
    case = c.lead.step(env["P"], task(env), case)
    assert case["state"] == "CANCELLED", case
    kinds = [a["kind"] for a in c.lead.actions(case["case_id"])]
    assert "readiness" in kinds and "hand_back" in kinds and "defer" not in kinds
    assert not env["fake"].prompts                                # no lead call was made
    d = c.obligations.decision(did)
    assert d["status"] == "answered", d
    strategy = d["answer"]["strategy"]
    assert "materially different approach" in strategy
    # built from the latest refused submission and the repeated findings
    assert "still refused: GATE HK-CLOSE1 PASS bound to the reviewed commit" in strategy
    assert "check gate:HK-CLOSE1: stale" in strategy
    assert "Repair the unresolved obligations build.gate" in strategy
    assert _events(c, "lead_handback")[-1]["decisions"] == [did]
    assert not _events(c, "decision_timeout_deferred")
    steps = [x["step"] for x in (task(env)["data"] or {}).get(DP.LOG_KEY) or []]
    assert steps == ["different_approach"]
    # the engine detaches the cancelled case and resumes the task on the answer (the
    # live profile runs the lifecycle engine)
    env["P"].profile.lifecycle = {**(env["P"].profile.lifecycle or {}), "enabled": True}
    for _ in range(3):
        c.tick()
        if task(env)["state"] != S.BLOCKED:
            break
    t = task(env)
    assert t["state"] != S.BLOCKED, t["blocker"]
    assert "recovery_case" not in (t["data"] or {})
    assert not _events(c, "sprint_parked")


def test_a_refused_lead_call_hands_back_instead_of_deferring(env, monkeypatch):  # noqa: F811
    """A quota / admission refusal of the lead call itself (status deferred) takes the
    same fallback instead of a backoff."""
    c = env["c"]
    did = _repeat_stop(env)
    case = _lead_case(env, did)
    L = c.lead
    monkeypatch.setattr(L, "_session_call", lambda *a, **k: {
        "status": "deferred", "reason": "Foundry refused the lead call (budget)",
        "retry": 3600})
    case = L.step(env["P"], task(env), case)
    assert case["state"] == "CANCELLED"
    d = c.obligations.decision(did)
    assert d["status"] == "answered" and "materially different approach" in \
        d["answer"]["strategy"]


def test_a_case_already_deferred_hands_back_on_its_next_step_and_at_the_timeout(env):  # noqa: F811,E501
    """The live store's cases were DEFERRED with the next check up to 40 min away and the
    decision's timeout pushed to the case deadline (hours)."""
    c, P = env["c"], env["P"]
    did = _repeat_stop(env)
    case = _lead_case(env, did)
    L = c.lead
    # the pre-fix state: DEFERRED back to DIAGNOSING, next check far away
    L._act(case, "readiness", status="failed", result="route_chain: ... retried later")
    case = L._defer(P, case, L.settings(P), "lead not ready: route_chain", 2400,
                    back="DIAGNOSING")
    assert case["state"] == "DEFERRED"
    # (a) the decision's own timeout: the sweep no longer follows the case's deadline
    clock = c.clock
    d = c.obligations.decision(did)
    c.store.x("UPDATE decisions SET deadline_ts=? WHERE decision_id=?", (clock() - 1, did))
    DP.sweep(c, force=True)
    assert not _events(c, "decision_timeout_deferred")
    assert c.lead.case(case["case_id"])["state"] == "CANCELLED"
    d = c.obligations.decision(did)
    assert d["status"] == "answered" and "materially different approach" in \
        d["answer"]["strategy"]


def test_a_deferred_case_step_hands_back_before_its_backoff_ends(env):  # noqa: F811
    c, P = env["c"], env["P"]
    did = _repeat_stop(env)
    case = _lead_case(env, did)
    L = c.lead
    case = L._defer(P, case, L.settings(P), "lead not ready: route_chain", 2400,
                    back="DIAGNOSING")
    case = L.step(P, task(env), case)
    assert case["state"] == "CANCELLED"
    assert c.obligations.decision(did)["status"] == "answered"


def test_the_handed_back_decision_never_reopens_a_lead_case(env, monkeypatch):  # noqa: F811
    """If the sweep cannot answer at once, the task waits for the sweep; no new lead
    case is opened for the same decision on every tick."""
    c = env["c"]
    env["ff"].info["routes"] = env["ff"].info["routes"][:1]
    did = _repeat_stop(env)
    monkeypatch.setattr(DP, "sweep", lambda c, force=False: [])
    case = _lead_case(env, did)
    case = c.lead.step(env["P"], task(env), case)
    assert case["state"] == "CANCELLED"
    assert c.obligations.decision(did)["status"] == "open"
    c._lead_detach(env["P"], task(env), "cancelled")
    c._lead_from_wait(env["P"], task(env), [c.obligations.decision(did)])
    assert not (task(env)["data"] or {}).get("recovery_case")


def test_the_self_repair_strategy_is_the_next_assignment_strategy(tmp_path):
    """The answered repeat question carries the self-repair strategy (the refused
    items, the repeated findings): the repeat guard's next assignment uses it as the
    session's Strategy instead of re-asking or the generic default."""
    from types import SimpleNamespace
    from foundry_coordinator.store import Store
    from tests.coordinator.test_recovery import _escalated
    store = Store(str(tmp_path / "c.sqlite"))
    ob, rec, P, item, did = _escalated(store)
    store.event("game", "UI-01", "submission_refused", detail={"items": [
        {"action": "changes_required", "class": "product_defect",
         "item": {"check_id": "au.gate", "title": "inventory gate PASS at head",
                  "reason": "inventory serializer drops the last slot"}}]})
    c = SimpleNamespace(store=store, obligations=ob, clock=lambda: 1e9, lead=None)
    plan = DP.self_repair_plan(c, P, ob.decision(did))
    assert plan["step"] == "different_approach"
    # (a plain Obligations store has no timeout policy: the same answer, applied)
    ob.answer(did, plan["answer"], "victor", "operator", channel="control")
    a = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws)
    assert "decision" not in a and a["escalation_decision"] == did
    assert "materially different approach" in a["strategy"]
    assert "still refused: inventory gate PASS at head inventory serializer drops the "         "last slot" in a["strategy"]


# ------------------------------------------------------------------ unchanged

def test_self_repair_off_still_defers(env):  # noqa: F811
    c, P = env["make"](extra="""
[automation]
self_repair = false
""")
    env["c"], env["P"] = c, P
    env["ff"].info["routes"] = env["ff"].info["routes"][:1]
    did = _repeat_stop(env)
    case = _lead_case(env, did)
    case = c.lead.step(P, task(env), case)
    assert case["state"] == "DEFERRED"
    assert c.obligations.decision(did)["status"] == "open"
    assert not _events(c, "lead_handback")


def test_a_product_decision_case_is_not_handed_back(env):  # noqa: F811
    c, P = env["c"], env["P"]
    env["ff"].info["routes"] = env["ff"].info["routes"][:1]
    case = open_case(env)                       # a check_failed case, no decision wait
    case = drive(env, case, state_is("DEFERRED", "NEEDS_VICTOR"), lm=c.lead)
    assert case["state"] == "DEFERRED"
    assert not _events(c, "lead_handback")


def test_a_case_with_a_lead_call_in_flight_is_not_handed_back(env):  # noqa: F811
    c, P = env["c"], env["P"]
    did = _repeat_stop(env)
    case = _lead_case(env, did)
    c.lead._act(case, "lead_call", op_key=f"lead:{case['case_id']}:1", status="started")
    assert c.lead.handback_decisions(P, c.lead.case(case["case_id"])) == []
    assert c.lead.hand_back(P, c.lead.case(case["case_id"]), "x") is False
