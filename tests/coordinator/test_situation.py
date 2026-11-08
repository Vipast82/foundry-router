"""foundry_coordinator.situation: what a task is doing and who has to act (Victor
2026-11-08: "if its awaiting human, then that means a HUMAN must intervene, if its
waiting for Visual Inspection then it should reflect that")."""
from __future__ import annotations

from foundry_coordinator.situation import situation

RAW = {"approval": {"visual_model": True}}


def task(state, **kw):
    data = kw.pop("data", {})
    return {"state": state, "data": data, **kw}


def test_a_model_visual_review_is_visual_inspection_not_awaiting_human():
    """VR-AS10 2026-11-08 14:19: AWAITING_HUMAN while the model judge reviewed it."""
    t = task("AWAITING_HUMAN", awaiting="approval: visual",
             data={"awaiting_kinds": ["visual"], "visual_changes_rounds": 7})
    s = situation(t, RAW)
    assert s["code"] == "VISUAL_REVIEW" and s["who"] == "model"
    assert "Visual inspection" in s["label"] and "7" in s["label"]
    # without [approval] visual_model a person approves: that is awaiting human
    s = situation(t, {"approval": {"visual_model": False}})
    assert s["code"] == "AWAITING_HUMAN" and s["who"] == "human"


def test_a_lead_repair_is_not_a_stop_for_a_person():
    t = task("BLOCKED", blocker="lead recovery: DIAGNOSING (case rc-1; would have been FAILED)")
    s = situation(t, RAW, [], {"state": "DIAGNOSING", "case_id": "rc-1"})
    assert s["code"] == "LEAD_RECOVERY" and s["who"] == "lead"
    s = situation(t, RAW, [], {"state": "NEEDS_VICTOR", "case_id": "rc-1"})
    assert s["code"] == "NEEDS_YOU" and s["who"] == "human"


def test_decisions_with_and_without_a_default():
    t = task("BLOCKED", blocker="waiting for operator decision dec-1")
    timed = {"status": "open", "question": "scope?", "deadline_ts": 1794170000, "timeout_minutes": 3}
    s = situation(t, RAW, [timed])
    assert s["code"] == "DECISION_PENDING" and s["who"] == "coordinator" and "applies at" in s["label"]
    s = situation(t, RAW, [{"status": "open", "question": "which art direction?"}])
    assert s["code"] == "NEEDS_YOU" and s["who"] == "human"


def test_pauses_say_who_paused_and_ignore_a_stale_session_note():
    """MV2-13 kept next_action 'implement session working' while paused."""
    t = task("PAUSED", next_action="implement session working", pause_reason="owner playtest",
             data={"hold": {"owner": "victor", "reason": "owner playtest"}})
    s = situation(t, RAW)
    assert s["label"] == "Paused by you" and s["who"] == "human" and "session" not in s["detail"]
    t = task("PAUSED", data={"hold": {"owner": "prep:landing", "actor": "engine", "reason": "landing"}})
    assert situation(t, RAW)["code"] == "HELD"
    # AS-VR1 / MV2-13 2026-11-08: held for PT-4, worded for a person
    t = task("PAUSED", data={"hold": {"owner": "task:animaluprising/PT-4", "actor": "operator"}})
    assert situation(t, RAW)["label"] == "Held until PT-4 finishes"


def test_waits_gates_and_sessions_are_named():
    s = situation(task("CHANGES_REQUIRED", next_action="waiting for workspace:c:\\x (held by VR-AS14)"), RAW)
    assert s["code"] == "WAITING_TURN" and s["who"] == "coordinator"
    s = situation(task("IMPLEMENTING", next_action="scripted Studio gate at 254f: starting, 0:44 of 75 min"), RAW)
    assert s["code"] == "STUDIO_GATE"
    assert situation(task("IMPLEMENTING", next_action="implement session working"), RAW)["who"] == "agent"
    assert situation(task("REVIEWING"), RAW)["who"] == "model"
    assert situation(task("CHANGES_REQUIRED"), RAW)["code"] == "FIX_ROUND"


def test_control_center_raises_needs_you_alerts_only_when_a_person_must_act(tmp_path):
    """Every model visual round raised a critical "human visual approval needed" alert and
    every Lead repair a high "is BLOCKED" alert. With the situation they raise none (and an
    open one clears); without a situation (older coordinator) the old alerts stay."""
    from foundry_router.control_center import CONTRACT, ControlCenter
    from foundry_router.db import Database
    c = ControlCenter(Database(tmp_path / "a.sqlite"), clock=lambda: 1_800_000_000.0)

    def ingest(**o):
        c.ingest({"contract": CONTRACT, "source": {"id": "bridge:t", "kind": "bridge"},
                  "observations": [{"type": "task", "project_id": "au", "task_id": "VR-AS10", **o}]},
                 {})

    def kinds():
        return sorted(a["kind"] for a in c.alerts([], "open"))
    appr = {"kinds": ["visual"], "stage": "after_close", "revision": "abc"}
    ingest(state="AWAITING_HUMAN", approval=appr)                    # no situation: old alert
    assert kinds() == ["approval"]
    ingest(state="AWAITING_HUMAN", approval=appr,
           situation={"code": "VISUAL_REVIEW", "label": "Visual inspection by the model judge",
                      "who": "model", "detail": ""})
    assert kinds() == []                                             # cleared, none raised
    ingest(state="BLOCKED", blocker="lead recovery: DIAGNOSING",
           situation={"code": "LEAD_RECOVERY", "label": "The Lead is repairing it", "who": "lead"})
    assert kinds() == []
    ingest(state="BLOCKED", blocker="rojo is down",
           situation={"code": "BLOCKED", "label": "Stopped", "who": "human"})
    assert kinds() == ["blocker"]
