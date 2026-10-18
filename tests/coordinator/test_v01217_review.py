"""v0.121.7 review findings, as regressions (fakes only)."""
import pytest
from foundry_coordinator import states as S
from tests.coordinator.test_scenarios_lifecycle import (kenv, KiloEnv, _compaction_turn,  # noqa
                                                        REPORT_DONE, _state)


def test_refused_unblock_queues_no_note(kenv):
    sid = kenv.start()
    c = kenv.c
    assert _state(c) == S.IMPLEMENTING
    with pytest.raises(Exception):
        c.unblock("game", "T1", "use the other API")
    rows = c.store.q("SELECT * FROM operator_messages WHERE task_id='T1'")
    assert rows == []        


def test_failed_compaction_is_an_error_not_busy(kenv):
    sid = kenv.start()
    kenv.fake.add_message(sid, "assistant", [{"type": "text", "text": "x"}], agent="compaction",
                          error={"name": "APIError", "data": {"message": "context overflow"}})
    kenv.fake.sessions[sid]["messages"][-1]["info"].update(summary=True, mode="compaction")
    kenv.fake.set_status(sid, None)
    P = kenv.c.projects["game"]
    s = P.client.state(sid, directory=P.profile.workspace, since_ts=0)
    assert s.status == "error"


def test_compaction_loop_is_bounded(kenv):
    sid = kenv.start()
    c = kenv.c
    kenv.clock[0] += 3700          # coordinator clock now ahead of/at the fake's real time
    for i in range(10):
        _compaction_turn(kenv, sid)
        kenv.fake.sessions[sid]["messages"][-1]["info"]["time"]["created"] = kenv.clock[0] * 1000 + 5000
        c.tick()
        kenv.clock[0] += 31
        c.tick()
        kenv.clock[0] += 60 * 30
    t = c.store.task("game", "T1")
    evs = [e["kind"] for e in c.store.q("SELECT kind FROM events WHERE task_id='T1'")]
    assert not (t["state"] == S.IMPLEMENTING and t["session_id"] == sid), "never timed out"


def test_session_note_rides_on_the_nudge_to_its_session(kenv):
    sid = kenv.start()
    c = kenv.c
    c.store.add_operator_message("a-1", "game", "T1", sid, "session", "next_session", "op",
                                 "SESSION NOTE: also update the changelog")
    _compaction_turn(kenv, sid)
    c.tick(); kenv.clock[0] += 31; c.tick()
    texts = [p["text"] for p in kenv.prompts_to(sid)]
    kenv.commit("src/a.txt", "OK")
    kenv.fake.finish(sid, REPORT_DONE)
    for _ in range(3):
        c.tick()
    m = c.store.operator_message("a-1")
    assert any("SESSION NOTE" in x for x in texts)
    assert m["status"] == "delivered"


def test_unblock_note_is_idempotent_per_transition(kenv):
    c = kenv.c
    c.approve("game", "T1", [], "victor")
    c.tick()
    c.store.update_task("game", "T1", state=S.BLOCKED, blocker="x")
    c.unblock("game", "T1", "go")
    rows = c.store.q("SELECT action_id FROM operator_messages WHERE task_id='T1'")
    assert len(rows) == 1 and rows[0]["action_id"].startswith("internal:unblock:game:T1:")
