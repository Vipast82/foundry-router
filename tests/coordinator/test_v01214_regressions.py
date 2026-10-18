"""v0.121.4 regressions: crash-safe first start, honest helper stops, mid-run
refusals and silent startup failures. Fakes only (fake Kilo, fake Foundry,
scripted client); nothing here starts a real service or inference."""

from __future__ import annotations

import pytest

from foundry_coordinator import states as S
from tests.coordinator.helpers import coordinator, run_until, write_ok
from tests.coordinator.test_scenarios_helpers import (CLAUDE, POLL, S45, TIMEOUT_MIN,  # noqa: F401
                                                      adm, env, events, start_both, units)
from tests.coordinator.test_scenarios_lifecycle import Gated, _state, game  # noqa: F401


# ------------------------------------------------------------------ primary start

def test_refused_first_prompt_is_retried_on_the_same_session_not_a_new_one(game):
    """A normal prompt failure (Kilo refused it) leaves nothing sent: the next
    tick prompts the SAME journaled session instead of starting another."""
    tmp, _, _, prof = game
    client = Gated(write_ok("src/a.txt"))
    client.fail_before = lambda text: True
    c = coordinator(tmp, [prof], {"game": client})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    assert len(client.starts) == 1 and len(client.prompted()) == 1
    t = c.store.task("game", "T1")
    assert t["attempt"] == 1 and "pending_start" not in t["data"]


def test_restart_after_an_uncertain_prompt_is_recorded_not_repeated(game):
    tmp, _, _, prof = game
    from tests.coordinator.test_scenarios_lifecycle import Crash
    client = Gated(write_ok("src/a.txt"))
    client.crash_after = lambda text: True
    c1 = coordinator(tmp, [prof], {"game": client})
    c1.approve("game", "T1", [], "victor")
    c1.tick()
    with pytest.raises(Crash):
        c1.tick()
    del c1
    c2 = coordinator(tmp, [prof], {"game": client})
    assert run_until(c2, lambda: _state(c2) == S.COMPLETE)
    kinds = [r["kind"] for r in c2.store.q(
        "SELECT kind FROM events WHERE project_id='game' AND task_id='T1'")]
    assert "session_prompt_uncertain" in kinds


# ------------------------------------------------------------------ helpers

def test_unconfirmed_abort_keeps_binding_and_unit_until_kilo_confirms(env):
    fake, c, ff, clock = env["fake"], env["c"], env["ff"], env["clock"]
    u = start_both(env)
    sid = u[S45]["session_id"]
    fake.post_fault = lambda p, b: "before" if p == f"/session/{sid}/abort" else None
    clock[0] += (TIMEOUT_MIN + 1) * 60
    c.tick()
    s45 = units(c)[S45]
    assert s45["status"] == "running" and "abort not confirmed" in s45["reason"]
    assert (sid, "failed") not in ff.ended                  # still bound, still gated
    assert len(events(c, "helper_abort_unconfirmed")) == 1
    c.tick()                                                  # still failing: no event spam
    assert len(events(c, "helper_abort_unconfirmed")) == 1
    fake.post_fault = None
    c.tick()
    s45 = units(c)[S45]
    assert s45["status"] == "failed" and "timed out" in s45["reason"]
    assert (sid, "failed") in ff.ended and sid in fake.aborts


def test_mid_run_refusal_defers_until_retry_after_then_starts_a_new_session(env):
    fake, c, ff, clock = env["fake"], env["c"], env["ff"], env["clock"]
    u = start_both(env)
    sid = u[S45]["session_id"]
    requests = u[S45]["requests"]
    fake.retry[sid] = ('{"error": {"message": "optional helper work not admitted", '
                       '"code": "optional_deferred", "foundry_category": "budget", '
                       '"reset_seconds": 900}}')
    clock[0] += POLL + 1
    c.tick()
    s45 = units(c)[S45]
    assert s45["status"] == "deferred" and "refused the helper mid-run" in s45["reason"]
    assert s45["next_eligible"] == pytest.approx(clock[0] + 900)
    assert sid in fake.aborts and (sid, "deferred") in ff.ended
    fake.retry.pop(sid)
    clock[0] += 300
    c.tick()
    assert units(c)[S45]["status"] == "deferred"            # not before retry_after
    clock[0] += 601
    c.tick()
    s45 = units(c)[S45]
    assert s45["status"] == "running" and s45["session_id"] != sid
    assert s45["requests"] == requests                      # no re-request used up


def test_silent_startup_failure_waits_the_grace_then_fails(env):
    fake, c, clock = env["fake"], env["c"], env["clock"]
    u = start_both(env)
    sid = u[S45]["session_id"]
    fake.set_status(sid, None)                               # idle, only the user prompt
    clock[0] += 60
    c.tick()
    assert units(c)[S45]["status"] == "running"              # inside the grace
    clock[0] += 150
    c.tick()
    s45 = units(c)[S45]
    assert s45["status"] == "failed" and "startup failed" in s45["reason"]
    assert sid in fake.aborts


def test_optional_deferred_is_a_budget_refusal_on_both_sides():
    from foundry_coordinator.adapters.kilo import foundry_denial
    from foundry_router.errors import denial_category
    assert denial_category("optional_deferred") == "budget"
    d = foundry_denial('APIError 429 {"error": {"code": "optional_deferred", "reset_s": 60}}')
    assert d and d["category"] == "budget"
