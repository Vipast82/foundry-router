"""v0.181.196: the brain lane was invisible, in two different ways.

Asked whether brain-lane performance is monitored like the rest, the answer was
no, and it was not one gap but three:

  * `brain-lane` (the brain model's own backend, enabled and healthy) had **0**
    engine samples ever. The probe asks for /metrics and /slots and that host
    404s both; its telemetry is on /health, under a `tiers` block holding the
    expert-cache hit rate and the fault count - the numbers someone asking "why
    is this lane slow" actually wants.
  * `brain-lane` had **0** per-call samples in 24 hours while a 60-case
    benchmark was running against it, because the benchmark calls
    `svc.pool.chat` directly and never records telemetry. `telemetry.record_call`
    exists precisely because dispatch paths kept drifting apart, and its
    docstring lists the paths it unified - the benchmark is not among them, so no
    bench run on ANY lane has ever reached the Performance tab.
  * and one case of the run died instantly on a cross-loop asyncio Event,
    scoring 0: the bench runs its own loop in a thread, and a pooled MCP session
    belongs to the loop that opened it.
"""
import asyncio

import pytest

from foundry_router.pool import prom

# The real payload from http://192.168.0.111:8081/health, 2026-11-19.
REAL_HEALTH = {
    "status": "ok",
    "tiers": {"experts": 10240, "vram_slots": 2007, "vram_held": 2007,
              "ram_copies": 0, "mapped": 8233, "mean_blob": 1084825,
              "tokens": 6073, "vram_hits": 2494090, "lookups": 2999441,
              "ram_reads": 0, "file_reads": 0, "file_mb": 0.0,
              "warm_hints": 0, "major_faults": 69, "max_context": 65536},
    "tiers_stale": True,
}


# -- /health as a metrics surface --------------------------------------------------

def test_the_real_health_payload_yields_the_metrics_that_explain_the_lane():
    out = prom.summarize_health(REAL_HEALTH)
    # 2,494,090 of 2,999,441 expert lookups were already in VRAM
    assert out["expert_cache_hit_pct"] == 83.2
    # and the VRAM slots are entirely full, with 10240 experts to serve
    assert out["expert_vram_fill_pct"] == 100.0
    assert out["experts_total"] == 10240
    assert out["expert_vram_held"] == 2007
    assert out["major_faults"] == 69
    assert out["max_context"] == 65536


def test_the_hit_rate_uses_the_name_strata_already_reports():
    """The point of normalising: one chart holding both lanes. A second name for
    the same measurement is how two lanes stop being comparable."""
    import inspect
    src = inspect.getsource(prom)
    assert src.count("expert_cache_hit_pct") >= 2      # the strata path and this one


def test_a_stale_flag_is_a_flag_and_not_a_series():
    """server_history skips booleans, so this reaches the live view and not the
    charts - which is right: "stale" is a state, not a quantity."""
    out = prom.summarize_health(REAL_HEALTH)
    assert out["tiers_stale"] is True
    from foundry_router import server_history
    assert "tiers_stale" not in dict(server_history.flatten(out))


@pytest.mark.parametrize("payload", [None, {}, {"status": "ok"}, {"tiers": None},
                                     {"tiers": "not a dict"}, []])
def test_it_returns_nothing_rather_than_raising_on_anything_else(payload):
    """It runs inside a health probe; raising there would mark a healthy backend
    unhealthy."""
    assert prom.summarize_health(payload) == {}


def test_a_zero_lookup_count_does_not_divide_by_zero():
    out = prom.summarize_health({"tiers": {"vram_hits": 0, "lookups": 0,
                                           "vram_held": 0, "vram_slots": 0}})
    assert "expert_cache_hit_pct" not in out
    assert "expert_vram_fill_pct" not in out


def test_the_probe_falls_back_to_health_only_when_metrics_gave_nothing():
    import inspect

    from foundry_router.pool import protocols
    src = inspect.getsource(protocols)
    i = src.index("summarize_health")
    before = src[max(0, i - 700):i]
    assert 'if not out.get("metrics_ok")' in before, \
        "the fallback must not override a working /metrics"
    after = src[i:i + 400]
    assert 'metrics_source' in after        # the operator can see where it came from


# -- the benchmark's own calls ------------------------------------------------------

def test_the_bench_records_telemetry_like_every_other_path():
    """`telemetry.record_call` was written because dispatch paths drifted; the
    bench was a path that never joined. 600 cases of the lessons A/B wrote
    nothing to perf_samples."""
    import inspect

    from foundry_router import bench
    src = inspect.getsource(bench.ask)
    assert "telemetry.record_call" in src
    assert 'mode="bench"' in src
    # best-effort: telemetry must never break a case
    assert "except Exception" in src.split("telemetry.record_call")[1][:400]


def test_the_bench_telemetry_names_the_backend_it_actually_used():
    """Attributing a bench call to the wrong backend would be worse than not
    recording it - the whole question is which lane was slow."""
    import inspect

    from foundry_router import bench
    src = inspect.getsource(bench.ask)
    head = src.split("telemetry.record_call")[1][:300]
    assert "backend=backend" in head          # the one pool.chat returned
    assert "model=model" in head


# -- a pooled MCP session belongs to one loop --------------------------------------

def _mgr():
    from foundry_router.tools.mcp_client import MCPManager
    m = object.__new__(MCPManager)
    m._holders = {}
    m.IDLE_CLOSE = 10_000
    m._reap_idle = lambda: None
    return m


def test_two_loops_get_two_holders():
    """The bench runs asyncio.run() in a thread. One case of run 40 died in
    0.01 s with "Event ... is bound to a different event loop" and scored 0."""
    from foundry_router.tools.mcp_client import MCPManager
    m = _mgr()

    async def grab():
        return id(MCPManager._holder(m, "searxng"))

    a = asyncio.run(grab())
    b = asyncio.run(grab())
    assert a != b, "both loops shared one holder, which is the bug"
    assert len(m._holders) == 2


def test_the_same_loop_keeps_one_holder():
    """Or every tool call would pay the handshake again."""
    from foundry_router.tools.mcp_client import MCPManager
    m = _mgr()

    async def twice():
        return (id(MCPManager._holder(m, "searxng")),
                id(MCPManager._holder(m, "searxng")))

    first, second = asyncio.run(twice())
    assert first == second
    assert len(m._holders) == 1


def test_different_servers_stay_apart_on_one_loop():
    from foundry_router.tools.mcp_client import MCPManager
    m = _mgr()

    async def two():
        return (id(MCPManager._holder(m, "a")), id(MCPManager._holder(m, "b")))

    a, b = asyncio.run(two())
    assert a != b and len(m._holders) == 2


def test_the_status_panel_is_still_keyed_by_server_name():
    """The keys became tuples; a status dict keyed by them would have shown the
    same server once per loop."""
    from foundry_router.tools.mcp_client import MCPManager
    m = _mgr()
    h1, h2 = object.__new__(_H), object.__new__(_H)
    h1.session, h1.last_used, h1.connects = object(), 0.0, 2
    h2.session, h2.last_used, h2.connects = None, 0.0, 1
    m._holders = {("searxng", 111): h1, ("searxng", 222): h2}
    out = MCPManager.session_status(m)
    assert list(out) == ["searxng"]
    assert out["searxng"]["open"] is True        # one of them has a session
    assert out["searxng"]["connects"] == 3
    assert out["searxng"]["loops"] == 2


def test_a_closed_loops_holder_is_dropped_and_never_reset():
    """Closing a foreign session is the very fault this keying avoids. A
    finished bench run leaves a closed loop behind, and holding its key would
    keep the loop object alive too."""
    from foundry_router.tools.mcp_client import MCPManager
    m = object.__new__(MCPManager)
    m.IDLE_CLOSE = 0
    dead = asyncio.new_event_loop()
    dead.close()
    gone = object.__new__(_H)
    gone.session, gone.last_used, gone.connects = object(), 1.0, 1
    gone.reset = _never

    async def reap():
        m._holders = {("searxng", dead): gone}
        MCPManager._reap_idle(m)
        return dict(m._holders)

    assert asyncio.run(reap()) == {}, "a closed loop's holder should be dropped"


def test_a_live_other_loops_session_is_left_alone():
    """Not dropped either: that loop is still using it and reaps its own."""
    from foundry_router.tools.mcp_client import MCPManager
    m = object.__new__(MCPManager)
    m.IDLE_CLOSE = 0
    other = asyncio.new_event_loop()
    try:
        busy = object.__new__(_H)
        busy.session, busy.last_used, busy.connects = object(), 1.0, 1
        busy.reset = _never

        async def reap():
            m._holders = {("searxng", other): busy}
            MCPManager._reap_idle(m)
            return dict(m._holders)

        assert len(asyncio.run(reap())) == 1
    finally:
        other.close()


def test_an_id_is_not_a_loop_identity():
    """Why the key holds the loop and not id(loop): two sequential asyncio.run()
    loops can land on the same address, and keying on the id handed the second
    one the first one's session. Caught by this suite before it shipped."""
    seen = []

    async def note():
        seen.append(id(asyncio.get_running_loop()))

    asyncio.run(note())
    asyncio.run(note())
    # it MAY reuse the address; the fix must not care either way
    import inspect

    from foundry_router.tools import mcp_client
    src = inspect.getsource(mcp_client.MCPManager._holder)
    assert "id(asyncio.get_running_loop())" not in src
    assert "asyncio.get_running_loop()" in src


class _H:
    """Stands in for _SessionHolder: the real one opens a connection."""


async def _never():
    raise AssertionError("reset() must not be called across loops")
