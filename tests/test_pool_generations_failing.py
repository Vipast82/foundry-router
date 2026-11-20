"""v0.181.213: a backend that lists its models and fails its generations.

2026-11-20, after the router host's outage: Meridian came back answering
/v1/models in 2 ms and failing every generation with a 503 ("Session
bookkeeping is unavailable") - 58 of 58 for 32 minutes - while the Backend
Pool showed it healthy and its breaker closed. The pool's liveness check is a
model listing every 15 s, and every passing listing reset the failure count
and marked the backend healthy, so live failures arriving every ~20 s never
reached the threshold of 3: the cheap evidence kept overruling the dear one,
and the sprints' Claude calls kept going to it instead of failing over.

Generations failing are now counted where a listing cannot reset them; only a
generation that succeeds, or the operator, puts the backend back.
"""
import pytest

from foundry_router.config import BackendConfig, BackendPoolConfig
from foundry_router.db import Database
from foundry_router.pool.base import AllBackendsFailed
from foundry_router.pool.internal import InternalPool
from foundry_router.pool.protocols import ChatResult, ProtocolError


class Lists:
    """Answers its model list; generations fail while `broken`."""

    def __init__(self, name, models, broken=False):
        self.name, self.models, self.broken = name, models, broken
        self.calls = 0

    async def list_models(self):
        return self.models

    async def chat(self, model, messages, **kw):
        self.calls += 1
        if self.broken:
            raise ProtocolError(
                "anthropic-compat HTTP 503: overloaded_error: Session bookkeeping "
                "is unavailable; retry shortly.")
        return ChatResult(content=f"hi from {self.name}", prompt_tokens=1,
                          completion_tokens=2)


@pytest.fixture()
def pool(tmp_path):
    db = Database(tmp_path / "pool.sqlite")
    cfg = BackendPoolConfig(failure_threshold=3, cooldown_seconds=60)
    p = InternalPool([
        BackendConfig(name="meridian", type="ollama", url="http://m", priority=90),
        BackendConfig(name="cliproxyapi", type="ollama", url="http://c", priority=80),
    ], cfg, client=None, db=db)
    p.backends["meridian"].protocol = Lists("meridian", ["claude-sonnet-5-5", "shared"])
    p.backends["cliproxyapi"].protocol = Lists("cliproxyapi", ["gpt-6", "shared"])
    return p


MSG = [{"role": "user", "content": "x"}]


async def _fail_between_listings(pool, n):
    """The live pattern: a failed generation, then a passing listing."""
    for _ in range(n):
        with pytest.raises(AllBackendsFailed):
            await pool.chat("claude-sonnet-5-5", MSG)
        await pool.check_all()


async def test_a_passing_listing_no_longer_resets_failing_generations(pool):
    await pool.check_all()
    pool.backends["meridian"].protocol.broken = True
    await _fail_between_listings(pool, 3)
    s = pool.backends["meridian"]
    assert s.failing_live and not s.healthy, (s.live_failures, s.consecutive_failures)
    assert s.listing_ok is True, "it does still answer its list"


async def test_while_out_its_calls_fail_fast_instead_of_hitting_it(pool):
    await pool.check_all()
    m = pool.backends["meridian"].protocol
    m.broken = True
    await _fail_between_listings(pool, 3)
    before = m.calls
    with pytest.raises(AllBackendsFailed):
        await pool.chat("claude-sonnet-5-5", MSG)
    assert m.calls == before, "inside the cooldown it is not called at all"


async def test_a_model_another_backend_serves_fails_over(pool):
    await pool.check_all()
    pool.backends["meridian"].protocol.broken = True
    await _fail_between_listings(pool, 3)
    _r, backend = await pool.chat("shared", MSG)
    assert backend == "cliproxyapi"


async def test_a_listing_does_not_put_it_back_but_a_generation_does(pool):
    await pool.check_all()
    s = pool.backends["meridian"]
    s.protocol.broken = True
    await _fail_between_listings(pool, 3)
    await pool.check_all()
    assert not s.healthy, "a passing listing is not evidence it can generate"
    s.protocol.broken = False
    s.cooldown_until = 0.0                    # the cooldown ran out
    _r, backend = await pool.chat("claude-sonnet-5-5", MSG)
    assert backend == "meridian"
    assert s.healthy and not s.failing_live and s.live_failures == 0


async def test_a_failed_trial_after_the_cooldown_takes_it_out_again(pool):
    import time
    await pool.check_all()
    s = pool.backends["meridian"]
    s.protocol.broken = True
    await _fail_between_listings(pool, 3)
    s.cooldown_until = 0.0
    with pytest.raises(AllBackendsFailed):
        await pool.chat("claude-sonnet-5-5", MSG)
    assert s.failing_live and s.cooldown_until > time.monotonic()


async def test_one_success_between_failures_keeps_it_in(pool):
    """Intermittent failures are not this: only failures IN A ROW count."""
    await pool.check_all()
    s = pool.backends["meridian"]
    for broken in (True, True, False, True, True):
        s.protocol.broken = broken
        try:
            await pool.chat("claude-sonnet-5-5", MSG)
        except AllBackendsFailed:
            pass
        await pool.check_all()
    assert s.healthy and not s.failing_live


async def test_the_operator_reset_puts_it_back(pool):
    await pool.check_all()
    s = pool.backends["meridian"]
    s.protocol.broken = True
    await _fail_between_listings(pool, 3)
    assert pool.reset_breaker("meridian")
    assert s.healthy and not s.failing_live and s.live_failures == 0


async def test_the_breaker_says_why(pool):
    await pool.check_all()
    pool.backends["meridian"].protocol.broken = True
    await _fail_between_listings(pool, 3)
    b = {x["backend"]: x for x in pool.breaker_status()}["meridian"]
    assert b["state"] == "open"
    assert b["generations_failing"] is True and b["failed_generations"] == 3


async def test_a_backend_whose_listing_fails_still_trips_the_old_way(pool):
    """The listing path is unchanged: a backend that is really gone."""
    class Gone(Lists):
        async def list_models(self):
            raise ProtocolError("connection refused")
    await pool.check_all()
    pool.backends["meridian"].protocol = Gone("meridian", ["claude-sonnet-5-5"])
    for _ in range(3):
        await pool.check_all()
    s = pool.backends["meridian"]
    assert not s.healthy and not s.failing_live and s.listing_ok is False


def test_the_serve_table_shows_failing_not_down(tmp_path):
    """Out of rotation for its generations, but it IS reachable."""
    from foundry_router.health import HealthMonitor
    p = InternalPool([BackendConfig(name="meridian", type="ollama", url="http://m")],
                     BackendPoolConfig(), client=None, db=Database(tmp_path / "h.sqlite"))
    s = p.backends["meridian"]
    s.ever_checked, s.healthy, s.failing_live, s.listing_ok = True, False, True, True
    health_cfg = type("C", (), {"fresh_minutes": 15, "probe_minutes": 15, "probe_paid": False})()
    svc = type("S", (), {"pool": p, "db": p.db, "ledger": None,
                         "config_store": type("CS", (), {"config": type(
                             "Cfg", (), {"health": health_cfg})()})()})()
    hm = HealthMonitor(svc)
    hm._ledger_evidence = lambda since: {}
    hm._paid = lambda c: True
    row = next(r for r in hm.status() if r["backend"] == "meridian")
    assert row["status"] == "failing"
    assert row["reachable"] is True and row["out_of_rotation"] is True
