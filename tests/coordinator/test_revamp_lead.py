"""Revamp B9: the lead is the last resort. Deterministic auto-heal first, an
invalid report goes back into the same lead session, a hard per-task cap, no
guard violation for a deploy, and a write-access fix is always re-verified.
Fakes only (scripted Kilo, fake Foundry, temp git repos)."""

from __future__ import annotations

import json

from foundry_coordinator import autoheal
from foundry_coordinator import states as S
from foundry_coordinator.lead import router_moved_by_deploy
from tests.coordinator.test_lead_e2e import (DONE, TID, Clock, _GameWriter, _game_with, acts,
                                             case, game_assignment, hist, lead_reply, lead_toml,
                                             make_game, task)

INFRA = "Kilo did not admit the prompt within the admission window (HTTP 503)"


def blocked(text):
    return {"final": "```coordinator-report\n" + json.dumps(
        {"status": "BLOCKED", "blocker": text}) + "\n```"}


def events(e, kind):
    return [json.loads(r["detail"]) for r in e["c"].store.q(
        "SELECT detail FROM events WHERE kind=? ORDER BY rowid", (kind,))]


def drive(e, pred, clock, n=80, step=40):
    for _ in range(n):
        e["c"].tick()
        if pred():
            return True
        clock.advance(step)
    return False


# ------------------------------------------------------------------ auto-heal

def test_catalogue_separates_tooling_from_product_stops():
    assert autoheal.plan("session_blocked", INFRA) == ("kilo_not_admitted", "restart")
    assert autoheal.plan("session_blocked", "rojo serve exited with code -1")[1] == "restart"
    assert autoheal.plan("session_blocked", "Studio MCP not connected (plugin timeout)")
    assert autoheal.plan("fast_lane_gate_error", "runner crashed") == ("gate_tooling", "verify")
    assert autoheal.plan("checks_unknown", "x")[1] == "verify"
    # a product / scope question is never retried blindly
    assert autoheal.plan("session_blocked", "which layout did Victor approve?") is None
    assert autoheal.plan("attempts_exhausted", INFRA) is None


def test_an_infra_stop_is_retried_free_before_any_lead_case(tmp_path):
    clock = Clock()
    log = []

    def primary(ctx):
        log.append(ctx)
        if len(log) == 1:
            return blocked(INFRA)
        return {"writes": {"src/a.txt": "OK healed\n"}, "commit": f"{TID}: fix", "final": DONE}
    e = make_game(tmp_path, primary, extra=lead_toml(), clock=clock)
    e["c"].approve("game", TID, [], "victor")
    assert drive(e, lambda: task(e)["state"] == S.COMPLETE, clock), hist(e)
    assert not e["c"].lead.cases("game", TID)                 # no paid lead call
    assert not e["client"].lead_starts()
    [ah] = events(e, "auto_heal")
    assert ah["signature"] == "kilo_not_admitted" and ah["action"] == "restart"
    assert ah["delay_s"] == 60
    assert events(e, "auto_heal_retry")
    # the retry was free: the second session is still attempt 1
    attempts = [s["metadata"]["attempt"] for s in e["client"].primary_starts()]
    assert attempts[:2] == [1, 1]


def test_a_stop_that_keeps_failing_reaches_the_lead_after_the_retries(tmp_path):
    clock = Clock()
    e = make_game(tmp_path, lambda ctx: blocked(INFRA),
                  lead=lambda ctx: lead_reply(game_assignment()),
                  extra=lead_toml() + "\n[automation]\nauto_heal_retries = 2\n", clock=clock)
    e["c"].approve("game", TID, [], "victor")
    assert drive(e, lambda: bool(e["c"].lead.cases("game", TID)), clock), hist(e)
    assert [x["n"] for x in events(e, "auto_heal")] == [1, 2]
    assert [x["delay_s"] for x in events(e, "auto_heal")] == [60, 120]
    assert events(e, "auto_heal_exhausted")


def test_an_operator_unblock_drops_a_stale_auto_heal(tmp_path):
    clock = Clock()
    e = make_game(tmp_path, lambda ctx: blocked(INFRA), extra=lead_toml(), clock=clock)
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert drive(e, lambda: bool((task(e)["data"] or {}).get("auto_heal")), clock, step=0)
    c.store.update_task("game", TID, blocker="operator note: hold")
    autoheal.step(c, e["P"], task(e))
    assert "auto_heal" not in (task(e)["data"] or {})


# ------------------------------------------------------------------ lead session

def test_an_invalid_report_is_repaired_in_the_same_session(tmp_path):
    seen = []

    def lead(ctx):
        seen.append(ctx.get("prompt") or "")
        if "was not accepted" in seen[-1]:
            return lead_reply(game_assignment())
        return {"final": "I found it but forgot the block."}
    e = make_game(tmp_path, lambda ctx: blocked("which gallery layout is approved?"),
                  lead=lead, extra=lead_toml())
    c = e["c"]
    c.approve("game", TID, [], "victor")
    for _ in range(40):
        c.tick()
        k = case(e)
        if k and k["state"] in ("RESUMING", "NEEDS_VICTOR") or \
                (k and k["state"] == "RESOLVED"):
            break
    k = case(e) or e["c"].lead.cases("game", TID)[-1]
    assert len(e["client"].lead_starts()) == 1, hist(e)      # one session, not two
    assert len(seen) == 2 and "was not accepted" in seen[1]
    kinds = [a["kind"] for a in acts(e, k["case_id"])]
    assert "report_repair" in kinds and "diagnosis" in kinds
    call = next(a for a in acts(e, k["case_id"]) if a["kind"] == "lead_call")
    assert call["status"] == "done" and call["detail"]["report_repairs"] == 1
    assert k["counters"]["diagnoses"] == 1                   # the repair is not a new call


def test_the_hard_cap_is_absolute(tmp_path):
    e = make_game(tmp_path, lambda ctx: blocked("which gallery layout is approved?"),
                  lead=lambda ctx: {"final": "no block"},
                  # W1.5: the hard cap is never below max_lead_calls_per_task (derived)
                  extra=lead_toml(max_diagnoses=5, max_lead_calls_per_task=1)
                  + "\n[automation]\nlead_hard_max_calls = 1\nlead_report_repairs = 0\n")
    c = e["c"]
    c.approve("game", TID, [], "victor")
    for _ in range(60):
        c.tick()
        if (case(e) or {}).get("state") == "NEEDS_VICTOR":
            break
    k = case(e)
    assert k["state"] == "NEEDS_VICTOR", hist(e)
    assert len(e["client"].lead_starts()) == 1
    esc = [a for a in acts(e, k["case_id"]) if a["kind"] == "escalate"][-1]
    assert esc["detail"]["reason"] == "lead_hard_cap"
    assert c.lead.lifetime_calls("game", TID) == 1
    # W1.2: the escalation carries options and the PARK default with the timeout
    d = c.obligations.decision(k["decision_id"])
    assert d["decision_class"] == "decision" and d["timeout_minutes"] == 30
    assert json.loads(d["recommended"]).startswith("stop the task (park")


def test_the_hard_cap_derives_from_the_per_task_lead_budget(tmp_path):
    """W1.5: [automation] lead_hard_max_calls (8 live) was below [lead_manager]
    max_lead_calls_per_task (12 live), so the hard cap fired first and the per-task
    budget was unreachable. The hard cap is never lower than the per-task cap."""
    e = make_game(tmp_path, lambda ctx: blocked("x"), extra=lead_toml(max_lead_calls_per_task=12)
                  + "\n[automation]\nlead_hard_max_calls = 8\n")
    assert e["c"].lead.hard_cap(e["P"]) == 12
    e2 = make_game(tmp_path / "b", lambda ctx: blocked("x"),
                   extra=lead_toml(max_lead_calls_per_task=4) + "\n[automation]\n"
                                                               "lead_hard_max_calls = 9\n")
    assert e2["c"].lead.hard_cap(e2["P"]) == 9
    e3 = make_game(tmp_path / "c", lambda ctx: blocked("x"),
                   extra=lead_toml() + "\n[automation]\nlead_hard_max_calls = 0\n")
    assert e3["c"].lead.hard_cap(e3["P"]) == 0                   # 0 keeps meaning "off"


def test_a_deploy_moving_the_router_head_on_a_clean_tree_is_not_a_violation():
    before = {"path": "r", "head": "a" * 40, "status": "x", "clean": True}
    assert router_moved_by_deploy(before, {**before, "head": "b" * 40})
    # a dirty tree afterwards (an edit) still counts
    assert not router_moved_by_deploy(before, {**before, "head": "b" * 40, "clean": False,
                                               "status": "y"})
    # an older snapshot without the flag: no assumption
    assert not router_moved_by_deploy({"head": "a"}, {"head": "b", "clean": True})


def test_a_write_access_fix_is_reverified_even_when_the_lead_says_wait(tmp_path):
    wait = {"classification": "environment",
            "hypothesis": "A stale harness line blocked the gate; I fixed and committed it.",
            "evidence_refs": ["src/a.txt:1"], "action": {"kind": "wait", "seconds": 600}}
    client = _GameWriter(lambda ctx: blocked("which gallery layout is approved?"),
                         lambda ctx: lead_reply(wait))
    e = _game_with(tmp_path, client, write_access=True)
    c = e["c"]
    c.approve("game", TID, [], "victor")
    for _ in range(40):
        c.tick()
        k = case(e)
        if k and any(a["op_key"].endswith(":reverify_after_fix") for a in acts(e, k["case_id"])):
            break
    k = case(e) or c.lead.cases("game", TID)[-1]
    kinds = [a["kind"] for a in acts(e, k["case_id"])]
    assert "lead_fix" in kinds and "reverify" in kinds, hist(e)
    assert "defer" not in kinds                               # never waited on a fix
    pa = c.lead.case(k["case_id"])["proposed_action"]
    assert pa["kind"] == "reverify" and pa["after_fix"] is True
