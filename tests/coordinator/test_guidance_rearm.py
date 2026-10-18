"""v0.121.7/v0.122.1: a transient pre-inference skip of retry guidance (quota 429,
capacity full) no longer suppresses guidance forever. Only an operator reset
re-arms it, bounded, with the old record kept; uncertain or completed
consultations are never replayed. Fakes only, no inference."""

from __future__ import annotations

from foundry_coordinator import states as S
from foundry_coordinator.retry_guidance import RetryGuidance
from tests.coordinator.helpers import run_until
from tests.coordinator.test_scenarios_lifecycle import OPUS, _exhausted, _state

REFUSED = {"admit": False, "state": "defer", "reason": "usage HTTP 429 (temporary)"}


def _exhaust_again(c):
    c.store.update_task("game", "T1", state=S.CHANGES_REQUIRED, attempt=3, sessions=3,
                        data={**c.store.task("game", "T1")["data"],
                              "findings": ["check project-check: FAIL"]})


def test_transient_quota_defers_guidance_and_asks_again_when_quota_recovers(tmp_path):
    """v0.122.1: a transient admission refusal (429/defer) defers guidance with a
    bounded retry timer instead of failing the task; no adviser inference happens
    while deferred, and the adviser is asked once when quota recovers."""
    c, ff, *_ = _exhausted(tmp_path)
    t0 = [c.clock()]
    c.clock = lambda: t0[0]
    ff.admission = {OPUS: REFUSED}
    for _ in range(6):
        c.tick()
    t = c.store.task("game", "T1")
    rg = t["data"]["retry_guidance"]
    assert t["state"] != S.FAILED and rg["status"] == "deferred" and "429" in rg["reason"]
    assert ff.guidance_calls == []                  # nothing paid while deferred
    t0[0] += 30                                     # before retry_at: still waiting
    c.tick()
    assert ff.guidance_calls == [] and _state(c) != S.FAILED
    ff.admission = {OPUS: {"admit": True}}          # quota recovered
    t0[0] += 61
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    assert ff.guidance_calls == [OPUS]              # asked exactly once


def test_hard_skip_still_fails_and_an_operator_reset_rearms_it(tmp_path):
    """A non-transient skip (e.g. capacity full / adviser unavailable) still fails
    the task; an operator reset re-arms guidance once, keeping the old record."""
    c, ff, *_ = _exhausted(tmp_path)
    ff.admission = {OPUS: {"admit": False, "state": "blocked", "reason": "persona disabled"}}
    assert run_until(c, lambda: _state(c) == S.FAILED)
    rg = c.store.task("game", "T1")["data"]["retry_guidance"]
    assert rg["status"] == "skipped" and ff.guidance_calls == []
    ff.admission = {OPUS: {"admit": True}}
    c.unblock("game", "T1", "adviser re-enabled", reset_attempts=True)
    data = c.store.task("game", "T1")["data"]
    assert "retry_guidance" not in data
    assert data["retry_guidance_history"][0]["reason"] == rg["reason"]    # kept
    _exhaust_again(c)
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    assert ff.guidance_calls == [OPUS]


def test_rearming_is_bounded():
    data = {}
    for _ in range(5):
        data = {**data, "retry_guidance": {"status": "skipped", "reason": "429"}}
        data = RetryGuidance.on_reset(data)
    assert len(data["retry_guidance_history"]) == RetryGuidance.MAX_REARMS
    assert data["retry_guidance"]["status"] == "skipped"                 # sticky after the bound


def test_uncertain_or_completed_consultations_are_never_rearmed():
    for status in ("calling", "unknown", "needs_human", "stop", "reset"):
        data = RetryGuidance.on_reset({"retry_guidance": {"status": status, "persona": OPUS}})
        assert data["retry_guidance"]["status"] == status, status
        assert "retry_guidance_history" not in data
    # a granted repair keeps its adviser excluded from review after a reset
    data = RetryGuidance.on_reset({"retry_guidance": {"status": "repair", "persona": OPUS,
                                                      "extra_attempts": 1}})
    assert data["retry_guidance"]["status"] == "reset"
    t = {"data": data}
    assert RetryGuidance.allowance(t) == 0 and RetryGuidance.authors(t) == {OPUS}
