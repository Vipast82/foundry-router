"""2026-10-29: four Lead cases classified "coordinator" (gate -Files, review packet,
unmounted check) sat DEFERRED for an hour after the coordinator was upgraded with their
fixes. A coordinator case opened before the running process started, with no session in
flight, is cancelled and its BLOCKED task retried once on the new code."""

from types import SimpleNamespace

from foundry_coordinator.lead import LeadManager


def _lead(actions=()):
    lm = LeadManager.__new__(LeadManager)
    seen = {"cancel": [], "unblock": [], "events": [], "acts": []}
    lm.actions = lambda cid, limit=0: list(actions)
    lm._act = lambda case, kind, **kw: seen["acts"].append(kind)
    lm.cancel = lambda cid, why, actor="", P=None: seen["cancel"].append(cid)
    lm.c = SimpleNamespace(unblock=lambda pid, tid, why, reset_attempts=False, actor="":
                           seen["unblock"].append((tid, reset_attempts)))
    lm.store = SimpleNamespace(event=lambda pid, tid, kind, detail=None:
                               seen["events"].append(kind))
    return lm, seen


P = SimpleNamespace(id="au")
BLOCKED = {"task_id": "T1", "state": "BLOCKED"}


def case(**kw):
    c = {"case_id": "rc1", "task_id": "T1", "classification": "coordinator",
         "state": "DEFERRED", "created_ts": 100.0}
    c.update(kw)
    return c


def test_a_coordinator_case_from_before_the_upgrade_retries_the_task():
    lm, seen = _lead()
    assert lm.retry_after_upgrade(P, BLOCKED, case(), started=200.0) is True
    assert seen["cancel"] == ["rc1"] and seen["unblock"] == [("T1", False)]
    assert "lead_retry_after_upgrade" in seen["events"]


def test_it_never_retries_a_newer_case_another_class_or_a_running_session():
    for c, t, acts in ((case(created_ts=300.0), BLOCKED, ()),            # opened after start
                       (case(classification="game"), BLOCKED, ()),
                       (case(state="NEEDS_VICTOR"), BLOCKED, ()),
                       (case(), {"task_id": "T1", "state": "READY"}, ()),
                       (case(state="REPAIRING"), BLOCKED, ({"status": "started"},))):
        lm, seen = _lead(acts)
        assert lm.retry_after_upgrade(P, t, c, started=200.0) is False
        assert not seen["cancel"] and not seen["unblock"]


def test_an_attempts_exhausted_case_retries_with_a_fresh_budget():
    """AS-P5 06:18: the retry without attempts failed again at once."""
    lm, seen = _lead()
    c = case(trigger={"class": "attempts_exhausted",
                      "reason": "8 implementation attempts used (max_attempts 8)"})
    assert lm.retry_after_upgrade(P, BLOCKED, c, started=200.0) is True
    assert seen["unblock"] == [("T1", True)]
