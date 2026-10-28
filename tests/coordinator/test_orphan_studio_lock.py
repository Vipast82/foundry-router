"""2026-10-28: GF-GATE1's session took AU's studio.lock at 19:37 and crashed at 21:45
(context overflow) without releasing it; the next sprint's session would have waited on it
forever. The coordinator releases a studio.lock whose task has no session that could hold
Studio, and leaves every other holder alone."""

import os
import time
from types import SimpleNamespace

import pytest

from foundry_coordinator import states as S
from foundry_coordinator.engine import Coordinator
from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter

LINE = "kilo-gfgate1-a9 2026-10-28T00:37:19Z GF-GATE1 attempt-9 step-6 full regression triage"


@pytest.fixture
def lockdir(tmp_path, monkeypatch):
    monkeypatch.setenv("AU_LOCK_DIR", str(tmp_path))
    return tmp_path


def _lock(d, line=LINE, age=600):
    p = d / "studio.lock"
    p.write_text(line + "\n", encoding="utf-8")
    t = time.time() - age
    os.utime(p, (t, t))
    return p


class _Store:
    def __init__(self, states):
        self.states, self.events = states, []

    def task(self, pid, tid):
        st = self.states.get(tid)
        return {"task_id": tid, "state": st} if st else None

    def event(self, pid, tid, kind, **kw):
        self.events.append((tid, kind, kw.get("detail")))


def _engine(states, active=False):
    a = AnimalUprisingAdapter.__new__(AnimalUprisingAdapter)
    P = SimpleNamespace(id="au", adapter=a)
    eng = SimpleNamespace(store=_Store(states), clock=lambda: 1000.0, log=lambda m: None,
                          ORPHAN_LOCK_EVERY_S=60.0,
                          studio_calls=SimpleNamespace(active=lambda P, tid: active))
    return eng, P


@pytest.mark.parametrize("state", [S.BLOCKED, S.COMPLETE, S.CANCELLED, S.VERIFYING, S.QUEUED])
def test_a_lock_left_by_a_task_with_no_session_is_released(lockdir, state):
    p = _lock(lockdir)
    eng, P = _engine({"GF-GATE1": state})
    Coordinator._orphan_studio_lock(eng, P)
    assert not p.exists()
    [(tid, kind, detail)] = eng.store.events
    assert (tid, kind) == ("GF-GATE1", "studio_lock_released") and detail["state"] == state


@pytest.mark.parametrize("state", [S.IMPLEMENTING, S.CLOSING, S.AWAITING_HUMAN])
def test_a_lock_a_live_session_may_hold_is_kept(lockdir, state):
    p = _lock(lockdir)
    eng, P = _engine({"GF-GATE1": state})
    Coordinator._orphan_studio_lock(eng, P)
    assert p.exists() and not eng.store.events


def test_other_holders_fresh_locks_and_scripted_runs_are_left_alone(lockdir):
    p = _lock(lockdir, "victor 2026-10-28T00:00:00Z MANUAL hands-on testing")
    eng, P = _engine({"GF-GATE1": S.BLOCKED})
    Coordinator._orphan_studio_lock(eng, P)                    # not a task of this project
    assert p.exists()
    p = _lock(lockdir, age=30)                                 # just taken: grace period
    eng, P = _engine({"GF-GATE1": S.BLOCKED})
    Coordinator._orphan_studio_lock(eng, P)
    assert p.exists()
    p = _lock(lockdir, "studio_gate 2026-10-28T00:00:00Z GF-GATE1 calls")   # the runner's own
    eng, P = _engine({"GF-GATE1": S.BLOCKED})
    Coordinator._orphan_studio_lock(eng, P)
    assert p.exists()
    p = _lock(lockdir)
    eng, P = _engine({"GF-GATE1": S.BLOCKED}, active=True)     # a scripted run is starting
    Coordinator._orphan_studio_lock(eng, P)
    assert p.exists()


def test_a_lock_that_changed_meanwhile_is_not_removed(lockdir):
    a = AnimalUprisingAdapter.__new__(AnimalUprisingAdapter)
    _lock(lockdir)
    h = a.studio_lock_holder()
    _lock(lockdir, "kilo-hk 2026-10-28T01:00:00Z HK-CLOSE1 boot gate")
    assert a.release_studio_lock(h) is False and (lockdir / "studio.lock").exists()
