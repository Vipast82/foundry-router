"""0.147.0: verify / closeout checks run in worker threads; a slow verify of one
sprint never stops the coordinator from moving the others (GF-NAME1 sat READY
while GF-GATE1 verified for 12+ minutes, 2026-10-27)."""

from __future__ import annotations

import threading
import time

from foundry_coordinator import states as S
from foundry_coordinator.bg_checks import BackgroundChecks
from tests.coordinator.helpers import run_until
from tests.coordinator.test_tracks_manager import env as tracks_env


def test_a_quick_run_finishes_inline():
    bg = BackgroundChecks()
    assert bg.run(("p", "T", "verify", "h1"), lambda: ["ok"], wait_s=2) == ("done", ["ok"])
    assert bg.running() == []


def test_a_slow_run_goes_to_the_background_and_is_applied_once():
    bg, gate = BackgroundChecks(), threading.Event()
    key = ("p", "T", "verify", "h1")

    def slow():
        gate.wait(10)
        return ["late"]
    st, _ = bg.run(key, slow, wait_s=0.05)
    assert st == "running" and [j["label"] for j in bg.running()] == ["T verify"]
    assert bg.run(key, slow, wait_s=0)[0] == "running"           # not submitted twice
    gate.set()
    assert bg.run(key, slow, wait_s=5) == ("done", ["late"])
    st, _ = bg.run(key, lambda: ["again"], wait_s=2)             # a new run, not the old result
    assert st == "done"


def test_the_cap_queues_and_another_head_drops_the_old_run():
    bg, gate = BackgroundChecks(), threading.Event()
    slow = lambda: gate.wait(10) or ["x"]                        # noqa: E731
    assert bg.run(("p", "A", "verify", "h1"), slow, wait_s=0, cap=1)[0] == "running"
    assert bg.run(("p", "B", "verify", "h1"), slow, wait_s=0, cap=1) == ("queued", 1)
    # task A's head moved: its old run is forgotten, the new one waits for the cap
    assert bg.run(("p", "A", "verify", "h2"), slow, wait_s=0, cap=1)[0] in ("running", "queued")
    assert ("p", "A", "verify", "h1") not in [j["key"] for j in bg.running()]
    gate.set()
    bg.shutdown()


def test_a_run_that_raises_is_reported_and_drop_forgets_jobs():
    bg = BackgroundChecks()

    def boom():
        raise RuntimeError("lune crashed")
    st, val = bg.run(("p", "T", "verify", "h1"), boom, wait_s=2)
    assert st == "done" and isinstance(val, RuntimeError)
    gate = threading.Event()
    bg.run(("p", "T", "closeout", "h1"), lambda: gate.wait(10), wait_s=0)
    assert bg.drop(lambda k: False) == [("p", "T", "closeout", "h1")]
    gate.set()


def test_a_slow_verify_does_not_stop_a_track_sprint_from_starting(tmp_path):
    c, P, root = tracks_env(tmp_path, extra="\n[automation]\nmax_agents = 2\n")
    gate, entered = threading.Event(), threading.Event()
    real = P.adapter.verify

    def slow_verify(spec, phase, rev, base):
        if spec.task_id == "T1" and phase == "verify":
            entered.set()
            gate.wait(30)
        return real(spec, phase, rev, base)
    P.adapter.verify = slow_verify
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: entered.is_set(), limit=20)
    t0 = time.time()
    c.tracks.create(P, "blender", ["B1"])
    c.tracks.start(P, "blender", "victor")
    # the track sprint runs while T1's verify is still blocked in the background
    assert run_until(c, lambda: c.store.task("game", "B1")["state"] not in
                     (S.QUEUED, S.READY), limit=20)
    assert c.store.task("game", "T1")["state"] == S.VERIFYING
    assert "in the background" in (c.store.task("game", "T1").get("note") or
                                   c.store.task("game", "T1")["data"].get("note") or
                                   str(c.store.task("game", "T1")))
    assert time.time() - t0 < 25                     # no tick waited for the slow verify
    gate.set()
    assert run_until(c, lambda: c.store.task("game", "T1")["state"] == S.COMPLETE, limit=40)
    vs = c.store.q("SELECT COUNT(*) AS n FROM verifications WHERE task_id='T1' AND "
                   "phase='verify' AND check_name='project-check'")
    assert vs[0]["n"] >= 1


def test_a_task_changed_during_another_tasks_step_is_read_fresh(tmp_path):
    """An operator change made while an earlier task's step runs is what the
    later task's step sees (the tick used to act on a snapshot from its start)."""
    c, P, root = tracks_env(tmp_path)
    c.approve("game", "T1", [], "victor")
    c.approve("game", "A1", ["T1"], "victor")
    seen = {}
    real = c._step

    def step(P_, t):
        if t["task_id"] == "T1":
            c.store.update_task("game", "A1", next_action="changed by the operator")
        if t["task_id"] == "A1":
            seen["next_action"] = t.get("next_action")
        return real(P_, t)
    c._step = step
    c.tick()
    assert seen.get("next_action") == "changed by the operator"
