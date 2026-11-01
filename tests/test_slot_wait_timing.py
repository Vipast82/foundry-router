"""2026-10-31: sent_at was stamped before Foundry's wait for a backend stream
slot, so the perf CSV booked a 54 s router wait as server wait. The wait is now
measured by the pool, moved out of ttft / sent_at and recorded as queue_ms."""

import asyncio
import time

from foundry_router import request_context, walltime
from foundry_router.facade.ollama_api import _slot_shift
from foundry_router.perf_history import SERIES_COLUMNS, record_sample


def test_slot_shift_moves_sent_past_the_wait():
    sent = walltime.Stamp()
    wall0, mono0 = sent.wall, sent.mono
    assert _slot_shift([0.4], sent) == 0.0 and sent.wall == wall0    # under 1 ms: noise
    assert _slot_shift([54000.0], sent) == 54000.0
    assert walltime.ms_between(wall0, sent.wall) == 54000.0
    assert abs(sent.mono - mono0 - 54.0) < 1e-6
    assert _slot_shift(None, walltime.Stamp()) == 0.0


def test_the_pool_notes_its_slot_wait_for_this_request_only():
    async def one(ms):
        box = request_context.slot_wait_begin()
        await asyncio.sleep(0)
        request_context.slot_wait_note(ms)
        await asyncio.sleep(0)
        return box[0], request_context.slot_wait_ms()

    async def both():
        return await asyncio.gather(one(10.0), one(20.0))
    assert asyncio.run(both()) == [(10.0, 10.0), (20.0, 20.0)]
    request_context.slot_wait_note(5.0)          # no box: ignored, never raises


def test_queue_ms_is_a_perf_column(app):
    db = app.state.services.db
    record_sample(db, model="m", backend="strata", persona="p", mode="direct",
                  prompt_tokens=10, completion_tokens=1, ttft_ms=100.0, wall_ms=200.0,
                  queue_ms=54000.0)
    row = db.query("SELECT queue_ms FROM perf_samples ORDER BY rowid DESC LIMIT 1")[0]
    assert row["queue_ms"] == 54000.0
    assert "queue_ms" in SERIES_COLUMNS
