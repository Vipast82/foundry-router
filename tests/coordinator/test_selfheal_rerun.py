"""Self-repair rules 3b and 3c: a sprint stopped by tooling is run once more when a new
release starts, and a Lead case opened on an infrastructure fault goes back to the
auto-heal. The blockers are the ones the operator unblocked by hand in the week of
2026-11-14 ("Foundry <commit> is live: run it again"; "the block was not this sprint's
work")."""

from __future__ import annotations

import json
import time

import pytest

from foundry_coordinator import autoheal, automation, selfheal
from foundry_coordinator import states as S
from tests.coordinator.test_lead_manager import env as lead_env  # noqa: F401  (fixture)
from tests.coordinator.test_lead_manager import task
from tests.coordinator.test_selfheal import Clock, events, game

# FIN-4a 2026-11-21 00:20, POL-7 / LOB-1b 2026-11-17 (unblocked after 4d194d9 and 0286000)
REVIEW_REFUSED = ("insufficiently inspected: source/test diff 683104 bytes exceeds the review "
                  "packet budget 535646; split the review or raise the budget")
# FIN-4a 2026-11-20 18:25 (trigger class studio_calls_error)
PUSH_TIMEOUT = ("scripted Studio gate: the pipeline exited with code 1 without a result; log "
                "x\\studio_calls.log: subprocess.TimeoutExpired: Command '['git', '-C', "
                "'C:\\\\Roblox\\\\AnimalUprising\\\\.tracks\\\\fin4a', 'push', '-q', '--force', "
                "'ssh://studio@192.168.0.117/srv/studio-slot/AnimalUprising.git', "
                "'7e845dfef5332a47ee3dfe704319eab37dd3d267:refs/heads/slot/FIN-4a']' timed out "
                "after 300 seconds)")


def block(c, text, cls=""):
    ev = {"blocker": text}
    if cls:
        ev["blocker_class"] = cls
    c.store.transition("game", "T1", S.BLOCKED, ev, blocker=text)
    return c.store.history("game", "T1")[-1]["ts"]


def upgraded(c, block_ts, old="0.181.223"):
    """`old` ran when the task blocked; the running release was first seen 50 s later."""
    c.store.set_meta(selfheal.VERSIONS_META, json.dumps(
        {old: block_ts - 1000, selfheal._running_version(): block_ts + 50}))


# ------------------------------------------------------------------ 3b after an upgrade

def test_replay_a_refused_review_is_run_again_once_the_new_release_runs(tmp_path):
    """POL-7, LOB-1b and FIN-5a1 sat blocked for 2.5 to 7 hours on 11-17, each on a review
    that could not fit its diff, until the operator unblocked them with "Foundry 4d194d9 is
    live". The fix was deployed; nothing told the sprints."""
    c, P, clk, *_ = game(tmp_path)
    ts = block(c, REVIEW_REFUSED)
    assert selfheal.upgrade_reruns(c, P) == []            # one release only: no upgrade yet
    upgraded(c, ts)
    [act] = selfheal.sweep(c, P, force=True)
    assert act["rule"] == "upgrade_rerun" and act["evidence"]["running"] == \
        selfheal._running_version()
    assert act["evidence"]["reason"].startswith("insufficiently inspected")
    t = c.store.task("game", "T1")
    assert t["state"] == S.READY and t["data"]["free_retry"] is True
    assert "one run on the new code" in t["next_action"]
    h = c.store.history("game", "T1")[-1]
    assert h["actor"] == "self_heal" and h["evidence"]["self_heal"].startswith("upgrade:")
    # it stops the same way on the new release: not this rule's any more
    block(c, REVIEW_REFUSED)
    assert selfheal.upgrade_reruns(c, P) == []
    assert len(events(c)) == 1


@pytest.mark.parametrize("text,cls,rerun", [
    (REVIEW_REFUSED, "", True),
    ("scripted Studio gate: timed out after 2400s", "studio_calls_error", True),
    ("session reported BLOCKED: build.suite.AS_P7 is still seeded as a required ORI-2 "
     "obligation although the operator retired it", "session_blocked", True),      # ORI-2 11-17
    ("readiness: the plan is not committed at HEAD", "readiness_scope", True),
    ("29 sessions used (max_sessions_per_task 29, budget ledger)", "sessions_exhausted", False),
    ("over max_task_minutes (1440 active minutes)", "task_minutes", False),
    ("the same open BUILD steps three sessions in a row", "build_stalled", False),
    ("S46 c6: expected 12 rows, got 11", "", False),
    ("waiting for operator decision dec-1: no progress across sessions",
     "decision_no_progress", False),
])
def test_only_a_tooling_stop_is_run_again(tmp_path, text, cls, rerun):
    c, P, clk, *_ = game(tmp_path)
    upgraded(c, block(c, text, cls))
    assert bool(selfheal.upgrade_reruns(c, P)) is rerun
    assert (c.store.task("game", "T1")["state"] == S.READY) is rerun


def test_a_stop_on_the_running_release_or_an_owner_decision_is_left_alone(tmp_path):
    c, P, clk, *_ = game(tmp_path)
    ts = block(c, REVIEW_REFUSED)
    # the release was already running when it stopped
    c.store.set_meta(selfheal.VERSIONS_META, json.dumps(
        {"0.181.223": ts - 1000, selfheal._running_version(): ts - 500}))
    assert selfheal.upgrade_reruns(c, P) == []
    upgraded(c, ts)
    # a decision of the owner's is open: the re-run never overtakes it
    did = c.obligations.open_decision("game", "T1", "scope_request", "May it edit X?",
                                      {"path": "src/x.lua"}, ["approve", "deny"])
    assert selfheal._owner_decision_open(c, P, "T1") == (did if isinstance(did, str)
                                                         else did["decision_id"])
    assert selfheal.upgrade_reruns(c, P) == []
    c.store.x("UPDATE decisions SET status='withdrawn'")
    # a hold, a scheduled auto-heal or the switch off keep it too
    automation.set_live(P, "self_heal_rerun_after_upgrade", False)
    assert selfheal.upgrade_reruns(c, P) == []
    automation.set_live(P, "self_heal_rerun_after_upgrade", True)
    c.store.merge_data("game", "T1", auto_heal={"blocker": "x"})
    assert selfheal.upgrade_reruns(c, P) == []
    c.store.merge_data("game", "T1", auto_heal=None)
    assert len(selfheal.upgrade_reruns(c, P)) == 1


def test_one_run_per_release_and_not_more_often_than_every_six_hours(tmp_path, monkeypatch):
    c, P, clk, *_ = game(tmp_path)
    ts = block(c, REVIEW_REFUSED)
    upgraded(c, ts)
    first = selfheal._running_version()
    assert len(selfheal.upgrade_reruns(c, P)) == 1
    ts2 = block(c, REVIEW_REFUSED)
    # another release an hour later: too soon after the last run
    monkeypatch.setattr(selfheal, "_running_version", lambda: "9.0.1")
    reg = json.loads(c.store.meta(selfheal.VERSIONS_META))
    c.store.set_meta(selfheal.VERSIONS_META, json.dumps({**reg, "9.0.1": ts2 + 60}))
    assert selfheal.upgrade_reruns(c, P) == []
    # the same release seven hours later: it runs
    real = time.time
    monkeypatch.setattr(selfheal.time, "time", lambda: real() + 7 * 3600)
    [act] = selfheal.upgrade_reruns(c, P)
    assert act["evidence"]["running"] == "9.0.1"
    assert set(c.store.task("game", "T1")["data"]["self_heal_upgrade"]) == {first, "9.0.1"}


def test_the_setting_is_typed_in_the_schema():
    from foundry_automation import schema
    f = schema.find_field("automation.self_heal_rerun_after_upgrade")
    assert f is not None and schema._AUTO_DESC["self_heal_rerun_after_upgrade"]
    assert schema.AUTOMATION_DEFAULTS["self_heal_rerun_after_upgrade"] is True


# ------------------------------------------------------------------ 3c infrastructure cases

def _case_on(e, reason, cls):
    """What engine._lead_open leaves: an open case and the task BLOCKED on it."""
    c, P, lm = e["c"], e["P"], e["lm"]
    case = lm.open_case(P, task(e), {"state": "BLOCKED", "reason": reason, "class": cls}, {})
    blocker = f"lead recovery: DIAGNOSING (case {case['case_id']}; would have been BLOCKED: {reason})"
    data = {**(task(e)["data"] or {}), "recovery_case": case["case_id"],
            "lead_trigger": {"state": "BLOCKED", "reason": reason[:500], "class": cls}}
    if task(e)["state"] == S.BLOCKED:
        c.store.update_task("game", "T1", blocker=blocker[:500], data=data)
    else:
        c.store.transition("game", "T1", S.BLOCKED,
                           {"blocker": blocker, "recovery_case": case["case_id"]},
                           blocker=blocker[:500], data=data)
    return case


def test_replay_fin4a_an_infrastructure_case_goes_back_to_the_auto_heal(lead_env):
    """FIN-4a, 2026-11-20 18:25 to 22:21: the push of its lane to slot-3 timed out, the stop
    opened a Lead case and the sprint sat in DIAGNOSING and NEEDS_VICTOR until the operator
    wrote "the 18:25 gate never reached Studio" and unblocked it."""
    e = lead_env
    c, P, lm = e["c"], e["P"], e["lm"]
    clk = Clock()
    c.clock = clk
    case = _case_on(e, PUSH_TIMEOUT, "studio_calls_error")
    lm._save(case["case_id"], state="NEEDS_VICTOR", state_ts=clk.t)
    assert selfheal.infra_cases(c, P) == []               # the case just changed
    clk.advance(310)
    [act] = selfheal.infra_cases(c, P)
    assert act["rule"] == "infra_case" and act["evidence"]["signature"] == "slot_push"
    assert act["evidence"]["case_state"] == "NEEDS_VICTOR"
    assert lm.case(case["case_id"])["state"] == "CANCELLED"
    t = task(e)
    ah = t["data"]["auto_heal"]
    assert t["state"] == S.BLOCKED and "recovery_case" not in t["data"]
    assert ah["signature"] == "slot_push" and ah["action"] == "restart"
    assert t["blocker"] == ah["blocker"] and "slot_push" in t["blocker"]
    # the auto-heal step releases it: a free retry, no attempt
    assert autoheal.step(c, P, t) is True
    t = task(e)
    assert t["state"] == S.READY and t["data"]["free_retry"] is True
    assert "auto_heal" not in t["data"]
    assert selfheal.infra_cases(c, P) == []


def test_a_case_with_a_call_in_flight_or_a_real_finding_stays_with_the_lead(lead_env):
    e = lead_env
    c, P, lm = e["c"], e["P"], e["lm"]
    clk = Clock()
    c.clock = clk
    case = _case_on(e, PUSH_TIMEOUT, "studio_calls_error")
    case = lm.step(P, task(e), case)                      # the diagnosis call starts
    assert lm._open_call(case["case_id"], "lead_call")
    clk.advance(3600)
    assert selfheal.infra_cases(c, P) == []
    assert lm.case(case["case_id"])["state"] != "CANCELLED"


def test_a_case_on_the_sprints_own_work_is_never_taken_from_the_lead(lead_env):
    e = lead_env
    c, P, lm = e["c"], e["P"], e["lm"]
    clk = Clock()
    c.clock = clk
    case = _case_on(e, "scripted Studio gate: S46 c6 expected 12 rows, got 11",
                    "studio_calls_error")
    lm._save(case["case_id"], state="NEEDS_VICTOR", state_ts=clk.t)
    clk.advance(3600)
    assert selfheal.infra_cases(c, P) == []
    assert lm.case(case["case_id"])["state"] == "NEEDS_VICTOR"


def test_an_infrastructure_case_is_handed_back_a_bounded_number_of_times(lead_env):
    e = lead_env
    c, P, lm = e["c"], e["P"], e["lm"]
    clk = Clock()
    c.clock = clk
    n = 0
    for _ in range(10):
        case = _case_on(e, PUSH_TIMEOUT, "studio_calls_error")
        lm._save(case["case_id"], state="NEEDS_VICTOR", state_ts=clk.t)
        clk.advance(310)
        got = selfheal.infra_cases(c, P)
        if not got:
            break
        n += 1
        assert autoheal.step(c, P, task(e)) is True       # released: READY
        c.store.merge_data("game", "T1", free_retry=None)
    # 3 x auto_heal_retries (2) in six hours; then the Lead keeps the case
    assert n == min(6, int(automation.get(P, "self_heal_max_actions_per_task_per_day")))
    assert lm.case(case["case_id"])["state"] == "NEEDS_VICTOR"
