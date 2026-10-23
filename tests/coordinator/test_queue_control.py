"""Queue control actions applied by the coordinator (review A2): queue_approve /
queue_reorder / queue_deps / queue_requeue through ControlChannel.handle() and
the engine's own approve / move / set_deps / reopen. Real git repositories, the
generic adapter and a fake Foundry control endpoint; nothing here dispatches
work — order is priority, states move only through the existing paths."""

import pytest

from foundry_coordinator import states as S
from foundry_coordinator.control import ControlChannel
from tests.coordinator.helpers import coordinator, make_repo, write_profile

T1 = {"id": "T1", "title": "Make the thing", "spec": "Write src/a.txt containing OK.",
      "scope": ["src/a.txt"]}
T2 = {"id": "T2", "title": "Other thing", "spec": "Write src/b.txt containing OK.",
      "scope": ["src/b.txt"]}
T3 = {"id": "T3", "title": "Third thing", "spec": "Write src/c.txt containing OK.",
      "scope": ["src/c.txt"]}


class Ctl:
    def __init__(self):
        self.queue, self.reports, self.beats = [], [], []

    def heartbeat(self, p):
        self.beats.append(p)

    def claim_actions(self, source_id, projects, limit=20):
        q, self.queue = self.queue, []
        return q

    def action_status(self, action_id, source_id, status, detail, result=None):
        self.reports.append({"action_id": action_id, "status": status, "detail": detail,
                             "result": result or {}})


_n = [0]


def send(ch, kind, task="", actor="victor", expected=None, aid="", **payload):
    _n[0] += 1
    aid = aid or f"act-{_n[0]}"
    ch.foundry.queue.append({"action_id": aid, "kind": kind, "project_id": "game",
                             "task_id": task, "actor": actor, "payload": payload,
                             "expected": expected or {}})
    ch.poll()
    return [r for r in ch.foundry.reports if r["action_id"] == aid][-1]


def order(c):
    return [e["task_id"] for e in c.store.queue("game")]


@pytest.fixture()
def game(tmp_path):
    root, _ = make_repo(tmp_path, "game", [T1, T2, T3])
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": {}})     # no session ever starts here
    return c, ControlChannel(c, foundry=Ctl(), source_id="coordinator:test",
                             heartbeat_every_s=0)


def test_beat_advertises_the_queue_kinds_and_feature(game):
    c, ch = game
    ch.poll()
    beat = ch.foundry.beats[-1]
    acts = beat["source"]["capabilities"]["actions"]
    for k in ("queue_approve", "queue_reorder", "queue_deps", "queue_requeue"):
        assert k in acts, k
    assert "queue" in beat["features"]


def test_queue_actions_apply_real_store_effects(game):
    c, ch = game
    # approve a task the store has never seen (a planned sprint)
    r = send(ch, "queue_approve", task="T1", deps=[], note="first")
    assert r["status"] == "applied", r
    assert c.store.task("game", "T1")["state"] == S.QUEUED
    r = send(ch, "queue_approve", task="T2", deps=["T1"], note="")
    assert r["status"] == "applied" and r["result"]["deps"] == ["T1"]
    send(ch, "queue_approve", task="T3", deps=[])
    assert order(c) == ["T1", "T2", "T3"]
    # reorder: positions renumber 1..N, journaled, no state change
    r = send(ch, "queue_reorder", task="T3", position=1,
             expected={"task_state": "QUEUED"})
    assert r["status"] == "applied" and order(c) == ["T3", "T1", "T2"]
    assert [e["position"] for e in c.store.queue("game")] == [1, 2, 3]
    assert c.store.task("game", "T3")["state"] == S.QUEUED
    # deps edit while QUEUED
    r = send(ch, "queue_deps", task="T3", deps=["T1"])
    assert r["status"] == "applied" and r["result"]["deps"] == ["T1"]
    assert c.store.queue_entry("game", "T3")["deps"] == ["T1"]
    # a cycle is refused server-side (the page's check is advisory only)
    r = send(ch, "queue_deps", task="T1", deps=["T3"])
    assert r["status"] == "rejected" and "loop" in r["detail"]
    # requeue: only CANCELLED comes back, with its history kept
    send(ch, "cancel", task="T1", reason="stop",
         expected={"task_state": "QUEUED"})
    assert c.store.task("game", "T1")["state"] == S.CANCELLED
    r = send(ch, "queue_requeue", task="T1", reason="try again",
             expected={"task_state": "CANCELLED"})
    assert r["status"] == "applied"
    assert c.store.task("game", "T1")["state"] == S.QUEUED


def test_queue_rejections(game):
    c, ch = game
    send(ch, "queue_approve", task="T1", deps=[])
    # unknown task for everything but approve
    r = send(ch, "queue_reorder", task="T9", position=1)
    assert r["status"] == "rejected" and "not on this coordinator's queue" in r["detail"]
    # stale expected state
    r = send(ch, "queue_reorder", task="T1", position=1,
             expected={"task_state": "IMPLEMENTING"})
    assert r["status"] == "rejected" and "now QUEUED" in r["detail"]
    # position must be 1+
    assert send(ch, "queue_reorder", task="T1", position=0)["status"] == "rejected"
    # deps must exist on the queue, and never cross projects
    r = send(ch, "queue_deps", task="T1", deps=["T9"])
    assert r["status"] == "rejected" and "not on game's approved queue" in r["detail"]
    r = send(ch, "queue_deps", task="T1", deps=["other:T1"])
    assert r["status"] == "rejected" and "another project" in r["detail"]
    # requeue of a live task is refused (reopen is CANCELLED -> QUEUED only)
    r = send(ch, "queue_requeue", task="T1", reason="again")
    assert r["status"] == "rejected" and "not CANCELLED" in r["detail"]
    # deps of a CANCELLED task can't change
    send(ch, "cancel", task="T1", reason="stop", expected={"task_state": "QUEUED"})
    r = send(ch, "queue_deps", task="T1", deps=[])
    assert r["status"] == "rejected" and "QUEUED" in r["detail"]


def test_an_applied_queue_action_is_never_applied_twice(game):
    c, ch = game
    send(ch, "queue_approve", task="T1", deps=[])
    send(ch, "queue_approve", task="T2", deps=[])
    r1 = send(ch, "queue_reorder", task="T2", position=1, aid="act-again")
    assert r1["status"] == "applied" and order(c) == ["T2", "T1"]
    # Foundry re-offers the same action (a lost report): the stored outcome is
    # replayed, the queue is not shuffled again
    send(ch, "queue_reorder", task="T1", position=1)       # someone moved it back
    assert order(c) == ["T1", "T2"]
    r2 = send(ch, "queue_reorder", task="T2", position=1, aid="act-again")
    assert r2["status"] == "applied"
    assert order(c) == ["T1", "T2"]                        # unchanged: replay, not redo
