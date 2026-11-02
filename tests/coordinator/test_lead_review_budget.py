"""Replay of AS-P10 (2026-11-02 02:42-04:41): a sprint BLOCKED only by "review budget
used (N/N review calls, budget ledger)" opened seven Lead cases with one repeat key.
The Lead could raise attempts and bounds but not review calls, so every case ran
`extend_budget` -> reverify/resume -> the same review-budget block, one Opus call each;
it even raised max_fingerprint_repeats itself to get past the repeat guard. The daily
automatic review-extension cap (self_repair_max_reviews_per_day = 2) was already used
before the first case opened.

Now: the Lead has a bounded `extend_reviews` operation that shares that daily cap; a
review-budget case whose cap is used escalates once at open (no Lead call); a report
that cannot clear the review block (extend_budget/raise_bound only, a raise of the
repeat guard) is refused; an extend_reviews refused by the cap escalates instead of
going back to DIAGNOSING."""

from __future__ import annotations

import os
import subprocess

from foundry_coordinator import states as S
from foundry_coordinator.lead import validate_report
from foundry_coordinator.obligations import POLICY_ACTOR
from tests.coordinator.test_lead_e2e import lead_toml, make_router
from tests.coordinator.test_lifecycle_e2e import green_t1
from tests.coordinator.test_lifecycle_engine import _game

TID = "T1"
BLOCK = "review budget used ({u}/{m} review calls, budget ledger)"


def game(tmp_path):
    extra = lead_toml(full_authority=True, router_repo=str(make_router(tmp_path)))
    c, client, ff, root, prof = _game(tmp_path, lambda ctx: green_t1(ctx), extra=extra)
    c.approve("game", TID, [], "victor")
    # the sprint's committed, verified work: a review block is always past the base
    base = _git(root, "rev-parse", "HEAD")
    os.makedirs(os.path.join(str(root), "src"), exist_ok=True)
    with open(os.path.join(str(root), "src", "ui.txt"), "w", encoding="utf-8") as f:
        f.write("OK\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "T1: work")
    head = _git(root, "rev-parse", "HEAD")
    c.store.update_task("game", TID, base_sha=base, head_sha=head)
    return c, c.projects["game"]


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def task(c):
    return c.store.task("game", TID)


def policy_grants(c, n):
    """`n` automatic review extensions today (the engine's unreviewed-head policy)."""
    for i in range(n):
        c.obligations.authorize("game", TID, "review_calls", 1,
                                f"self-repair: +1 review for head{i}", POLICY_ACTOR,
                                f"policy:review_calls:head{i:040d}")


def spend_reviews(c, P):
    lim = c.obligations.effective_limits(P, task(c))["review_calls"]
    for i in range(lim["remaining"]):
        c.obligations.charge("game", TID, "review_calls", 1, f"review:spent:{i}", "engine")
    lim = c.obligations.effective_limits(P, task(c))["review_calls"]
    assert lim["remaining"] == 0
    return lim


def block_on_reviews(c, P):
    lim = spend_reviews(c, P)
    reason = BLOCK.format(u=lim["used"], m=lim["max"])
    c.store.update_task("game", TID, state=S.BLOCKED, blocker=reason, run_id="run-1")
    return {"state": "BLOCKED", "reason": reason, "class": "review_budget"}


def operate(ops, then="reverify"):
    return {"classification": "evidence",
            "hypothesis": "Every required check passes at HEAD; only the review-call budget "
                          "is used up, so the gated head was never reviewed.",
            "evidence_refs": ["ver-1"],
            "action": {"kind": "operate", "then": then, "operations": ops}}


EXTEND_ATTEMPTS = {"op": "extend_budget", "attempts": 1,
                   "reason": "one review call is needed to review the gated revision"}
EXTEND_REVIEWS = {"op": "extend_reviews", "calls": 1,
                  "reason": "the verified head needs one review call"}
RAISE_REPEATS = {"op": "raise_bound", "bound": "max_fingerprint_repeats", "by": 1,
                 "reason": "the repeat only came from the previous resume"}


# ------------------------------------------------------------------ the replay

def test_replay_review_budget_case_with_the_daily_cap_used_escalates_once(tmp_path):
    """AS-P10 02:42: the cap was 2/2 before the first case. The case escalates at open
    (one decision, no Lead call); the repeat opens no further case or decision."""
    c, P = game(tmp_path)
    policy_grants(c, 2)
    trig = block_on_reviews(c, P)
    case = c.lead.open_case(P, task(c), trig, {})
    assert case["state"] == "NEEDS_VICTOR", case["state"]
    esc = case["validation"]["escalation"]
    assert esc["reason"] == "review_extension_cap"
    assert not [a for a in c.lead.actions(case["case_id"]) if a["kind"] == "lead_call"]
    d = c.obligations.decision(case["decision_id"])
    assert d["kind"] == "lead_escalation" and "2/2" in d["question"]
    assert "self_repair_max_reviews_per_day" in d["question"]
    # the same block seen again: the open case is returned, no second question
    again = c.lead.open_case(P, task(c), trig, {})
    assert again["case_id"] == case["case_id"]
    assert len([x for x in c.obligations.all_decisions("game", TID)
                if x["kind"] == "lead_escalation"]) == 1


def test_review_budget_case_with_room_left_is_diagnosed(tmp_path):
    c, P = game(tmp_path)
    policy_grants(c, 1)
    case = c.lead.open_case(P, task(c), block_on_reviews(c, P), {})
    assert case["state"] == "DIAGNOSING"


# ------------------------------------------------------------------ the report contract

def test_report_that_cannot_clear_a_review_budget_block_is_refused():
    for ops, then in (([EXTEND_ATTEMPTS], "reverify"), ([EXTEND_ATTEMPTS], "resume"),
                      ([EXTEND_ATTEMPTS, RAISE_REPEATS], "reverify")):
        clean, errs = validate_report(operate(ops, then), full_authority=True,
                                      trigger_class="review_budget")
        assert clean is None and any("extend_reviews" in e for e in errs), errs
    # raising the repeat guard is refused for this blocker even with extend_reviews
    clean, errs = validate_report(operate([EXTEND_REVIEWS, RAISE_REPEATS]),
                                  full_authority=True, trigger_class="review_budget")
    assert clean is None and any("max_fingerprint_repeats" in e for e in errs), errs
    # a plain reverify proposes nothing that refills the budget either
    clean, errs = validate_report({**operate([]), "action": {"kind": "reverify"}},
                                  full_authority=True, trigger_class="review_budget")
    assert clean is None and any("extend_reviews" in e for e in errs), errs
    # extend_reviews is accepted and bounded
    clean, errs = validate_report(operate([EXTEND_REVIEWS]), full_authority=True,
                                  trigger_class="review_budget")
    assert errs == [] and clean["action"]["operations"][0] == {**EXTEND_REVIEWS}
    clean, errs = validate_report(operate([{**EXTEND_REVIEWS, "calls": 9}]),
                                  full_authority=True, trigger_class="review_budget")
    assert clean is None and any("calls must be 1..2" in e for e in errs), errs
    # other blockers keep the old contract
    assert validate_report(operate([EXTEND_ATTEMPTS]), full_authority=True,
                           trigger_class="check_failed")[1] == []


# ------------------------------------------------------------------ the operation

def _operate_case(c, P, trig):
    case = c.lead.open_case(P, task(c), trig, {})
    assert case["state"] == "DIAGNOSING"
    c.lead._save(case["case_id"], state="RESUMING", proposed_action={"kind": "operate"})
    return c.lead.case(case["case_id"])


def test_extend_reviews_grants_within_the_daily_cap_and_reverifies(tmp_path):
    c, P = game(tmp_path)
    policy_grants(c, 1)
    case = _operate_case(c, P, block_on_reviews(c, P))
    cid = case["case_id"]
    act = operate([EXTEND_REVIEWS])["action"]
    c._lead_operate(P, task(c), case, act)
    op = c.lead._action(f"{cid}:op:0")
    assert op["status"] == "done", op["result"]
    rows = c.store.q("SELECT decision_id, actor, delta FROM budget_ledger WHERE counter="
                     "'authorized:review_calls' AND decision_id LIKE ?", (f"%{cid}%",))
    assert [(r["decision_id"], r["actor"], r["delta"]) for r in rows] == [
        (f"policy:review_calls:lead-{cid}-1", POLICY_ACTOR, 1)]
    lim = c.obligations.effective_limits(P, task(c))["review_calls"]
    assert lim["remaining"] == 1
    # the head is unchanged, but the missing piece is the review: it reverifies
    assert task(c)["state"] == S.VERIFYING
    assert c.lead.case(cid)["state"] == "RESOLVED"
    # the cap is shared with the automatic extensions: it is now used (2/2)
    assert c.obligations.policy_extensions("game", TID, "review_calls") == 2


def test_extend_reviews_refused_by_the_cap_escalates_instead_of_rediagnosing(tmp_path):
    c, P = game(tmp_path)
    policy_grants(c, 1)
    case = _operate_case(c, P, block_on_reviews(c, P))
    cid = case["case_id"]
    act = operate([{**EXTEND_REVIEWS, "calls": 2}])["action"]
    c._lead_operate(P, task(c), case, act)
    assert c.lead._action(f"{cid}:op:0")["status"] == "failed"
    k = c.lead.case(cid)
    assert k["state"] == "NEEDS_VICTOR", k["state"]
    assert k["validation"]["escalation"]["reason"] == "review_extension_cap"
    # within the cap the one unit it could grant stays granted (audited), nothing more
    assert c.obligations.policy_extensions("game", TID, "review_calls") == 2


def test_only_an_open_review_budget_case_may_use_a_lead_review_reference(tmp_path):
    c, P = game(tmp_path)
    trig = block_on_reviews(c, P)
    case = c.lead.open_case(P, task(c), {**trig, "class": "check_failed"}, {})
    ok = c._policy_ref_ok("game", TID, f"policy:review_calls:lead-{case['case_id']}-1",
                          "review_calls")
    assert ok is False
    c.lead.resolve(case["case_id"], "done")
    case2 = c.lead.open_case(P, task(c), trig, {})
    ref = f"policy:review_calls:lead-{case2['case_id']}-1"
    assert c._policy_ref_ok("game", TID, ref, "review_calls") is True
    assert c._policy_ref_ok("game", "T9", ref, "review_calls") is False
