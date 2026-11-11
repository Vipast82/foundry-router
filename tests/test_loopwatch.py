"""v0.181.124: the stall watchdog names what the event loop was doing when it froze."""

import asyncio
import time
import traceback

from foundry_router import loopwatch
from foundry_router.loopwatch import LoopWatch


def test_a_frozen_loop_is_reported_with_the_code_that_froze_it(monkeypatch):
    monkeypatch.setattr(loopwatch, "CHECK_S", 0.05)
    monkeypatch.setattr(loopwatch, "TICK_S", 0.02)
    seen = []

    def slow_handler():
        time.sleep(0.7)                      # synchronous work on the loop: the bug

    async def run():
        lw = LoopWatch(on_stall=seen.append, threshold=0.25)
        lw.start()
        await asyncio.sleep(0.1)
        assert lw.summary() == {"count": 0, "last": None}
        slow_handler()
        await asyncio.sleep(0.15)            # the loop moves again: the stall is written
        lw.stop()
        return lw
    lw = asyncio.run(run())
    assert len(lw.stalls) == 1 and seen == list(lw.stalls)
    row = lw.stalls[0]
    assert 0.5 <= row["seconds"] <= 2.0
    assert "slow_handler" in row["where"] and "test_loopwatch.py" in row["where"]
    assert "time.sleep(0.7)" in row["stack"]
    assert lw.summary()["count"] == 1 and "stack" not in lw.summary()["last"]


def test_short_pauses_are_not_stalls(monkeypatch):
    monkeypatch.setattr(loopwatch, "CHECK_S", 0.05)
    monkeypatch.setattr(loopwatch, "TICK_S", 0.02)

    async def run():
        lw = LoopWatch(threshold=0.6)
        lw.start()
        time.sleep(0.2)
        await asyncio.sleep(0.2)
        lw.stop()
        return lw
    assert len(asyncio.run(run()).stalls) == 0


def test_a_stall_inside_a_library_is_named_by_the_foundry_caller():
    F = traceback.FrameSummary
    stack = [F("/app/foundry_router/main.py", 10, "serve"),
             F("/app/foundry_router/routes_view.py", 88, "graph"),
             F("/usr/lib/python3.12/sqlite3/dbapi2.py", 5, "execute")]
    assert loopwatch._where(stack) == "graph (routes_view.py:88)"
    assert loopwatch._where([F("/usr/lib/python3.12/json/decoder.py", 337, "decode")]) == "decode (decoder.py:337)"
    assert loopwatch._where([]) == "unknown"


def test_health_reports_stalls_and_the_admin_api_lists_them(app, client):
    assert client.get("/health").json()["stalls"] == {"count": 0, "last": None}
    svc = app.state.services
    svc.loopwatch._report({"since": 0, "wall": 1794390000.0, "where": "graph (routes_view.py:88)",
                           "stack": "  File x, line 88, in graph\n"}, 12.4)
    h = client.get("/health").json()["stalls"]
    assert h["count"] == 1 and h["last"]["seconds"] == 12.4 and h["last"]["where"].startswith("graph")
    d = client.get("/admin/api/health/stalls").json()
    assert d["threshold_seconds"] == 2.0 and d["stalls"][0]["stack"].strip().endswith("in graph")
    ev = svc.db.query("SELECT message, detail FROM event_log WHERE source='stall'")
    assert ev and ev[0]["message"] == "router stalled for 12.4 s in graph (routes_view.py:88)"
    assert "line 88" in ev[0]["detail"]
