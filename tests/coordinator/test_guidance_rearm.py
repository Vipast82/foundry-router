"""v0.121.7: a transient pre-inference skip of retry guidance (quota 429,
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


def test_transient_skip_is_rearmed_by_an_operator_reset_and_admission_is_asked_again(tmp_path):
    c, ff, *_ = _exhausted(tmp_path)
    ff.admission = {OPUS: REFUSED}
    assert run_until(c, lambda: _state(c) == S.FAILED)
    rg = c.store.task("game", "T1")["data"]["retry_guidance"]
    assert rg["status"] == "skipped" and "429" in rg["reason"] and ff.guidance_calls == []
    # without a reset the sticky record stays (no new budget was authorized)
    c.unblock("game", "T1", "try again")
    assert run_until(c, lambda: _state(c) == S.FAILED)
    assert ff.guidance_calls == []
    # an operator reset with quota recovered: guidance is asked again, once
    ff.admission = {OPUS: {"admit": True}}
    c.unblock("game", "T1", "quota recovered", reset_attempts=True)
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
