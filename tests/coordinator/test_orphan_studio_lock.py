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


def _engine(states, active=False, in_play=None):
    a = AnimalUprisingAdapter.__new__(AnimalUprisingAdapter)
    # 2026-11-13: the play probe never reaches a real Studio from a test (None = no answer)
    a.studio_in_play = in_play if callable(in_play) else (lambda: in_play)
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


# ---- 2026-11-13 BOT-1 / TT-1: a bot match plays on after its session's turn ended ----------
def _sweep(eng, P, now, times=1):
    """One sweep a minute (ORPHAN_LOCK_EVERY_S)."""
    for _ in range(times):
        Coordinator._orphan_studio_lock(eng, P)
        now[0] += 60


def _timed(states, in_play, **kw):
    eng, P = _engine(states, in_play=in_play, **kw)
    now = [1000.0]
    eng.clock = lambda: now[0]
    return eng, P, now


def test_a_lock_is_kept_while_studio_is_in_play_and_noted_every_ten_minutes(lockdir):
    p = _lock(lockdir)
    playing, asked = [True], []
    eng, P, now = _timed({"GF-GATE1": S.VERIFYING}, lambda: asked.append(1) or playing[0])
    _sweep(eng, P, now, 10)                                    # minutes 0-9
    assert p.exists() and len(asked) == 10
    [(tid, kind, detail)] = eng.store.events                   # written once, not every sweep
    assert (tid, kind, detail["why"]) == ("GF-GATE1", "studio_lock_kept", "playtest in flight")
    _sweep(eng, P, now)                                        # minute 10: noted again
    assert p.exists() and [k for _, k, _ in eng.store.events] == ["studio_lock_kept"] * 2
    playing[0] = False                                         # the match is over
    _sweep(eng, P, now)
    assert not p.exists() and eng.store.events[-1][1] == "studio_lock_released"


def test_a_lock_a_live_session_may_hold_is_not_asked_about(lockdir):
    p = _lock(lockdir)
    asked = []
    eng, P, now = _timed({"GF-GATE1": S.IMPLEMENTING}, lambda: asked.append(1) or True)
    _sweep(eng, P, now, 3)
    assert p.exists() and not asked and not eng.store.events


def _boom():
    raise RuntimeError("mcp: no answer")


@pytest.mark.parametrize("in_play", [False, None, _boom])
def test_not_in_play_or_no_answer_releases_as_before(lockdir, in_play):
    p = _lock(lockdir)
    eng, P, now = _timed({"GF-GATE1": S.BLOCKED}, in_play)
    _sweep(eng, P, now)
    assert not p.exists() and [k for _, k, _ in eng.store.events] == ["studio_lock_released"]


def test_the_keep_ends_at_the_cap_with_its_own_event(lockdir):
    p = _lock(lockdir)
    eng, P, now = _timed({"GF-GATE1": S.BLOCKED}, True)        # a Play session that never ends
    _sweep(eng, P, now, 120)                                   # default: 120 minutes
    assert p.exists() and {k for _, k, _ in eng.store.events} == {"studio_lock_kept"}
    _sweep(eng, P, now)
    assert not p.exists()
    assert [k for _, k, _ in eng.store.events[-2:]] == ["studio_lock_keep_cap",
                                                        "studio_lock_released"]
    assert eng.store.events[-2][2]["kept_minutes"] == 120


def test_an_adapter_without_the_probe_and_setting_0_release_as_before(lockdir):
    p = _lock(lockdir)
    eng, P, now = _timed({"GF-GATE1": S.BLOCKED}, True)
    P.adapter = SimpleNamespace(studio_lock_holder=P.adapter.studio_lock_holder,
                                release_studio_lock=P.adapter.release_studio_lock)
    _sweep(eng, P, now)
    assert not p.exists() and [k for _, k, _ in eng.store.events] == ["studio_lock_released"]
    p = _lock(lockdir)
    asked = []
    eng, P, now = _timed({"GF-GATE1": S.BLOCKED}, lambda: asked.append(1) or True)
    P.profile = SimpleNamespace(raw={"automation": {"studio_lock_playtest_keep_minutes": 0}})
    _sweep(eng, P, now)
    assert not p.exists() and not asked
    assert [k for _, k, _ in eng.store.events] == ["studio_lock_released"]
