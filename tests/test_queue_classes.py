"""v0.181.118: queue classes. When a backend is full, waiters are served by class
(A interactive, B required, C optional, D background) with aging, in both places a
call waits: the pool's slot gate and the ledger's wait for a backend stream. A queued
streaming call reports its place in the queue as progress."""

import asyncio
import os
import tempfile
import time
from types import SimpleNamespace

import httpx
import pytest

from foundry_router import client_auth, keepalive, queue_class as QC
from foundry_router.budget import BudgetDenied, CallCtx, Ledger
from foundry_router.config import (BackendConfig, BackendPoolConfig, BudgetsConfig,
                                   QueueClassConfig, QueueConfig)
from foundry_router.db import Database
from foundry_router.pool import internal as pool_internal
from foundry_router.pool.internal import FairGate, InternalPool


@pytest.fixture(autouse=True)
def _default_queue_settings():
    QC.configure(None)
    yield
    QC.configure(None)


# -- which class a request is ---------------------------------------------------------

def test_class_from_purpose_persona_client_and_default():
    assert QC.classify({}) == "A"                                   # unknown traffic: a person
    assert QC.classify({"purpose": "implementer"}) == "B"
    assert QC.classify({"purpose": "prework"}) == "C"
    # an unverified purpose proves nothing
    assert QC.classify({"purpose": "unverified", "queue_class": "D"}) == "D"
    # purpose wins over the persona, the persona over the client, the client over the default
    assert QC.classify({"purpose": "review", "queue_class": "D"}, {"queue_class": "D"}) == "B"
    assert QC.classify({"queue_class": "B"}, {"queue_class": "D"}) == "D"
    assert QC.classify({"queue_class": "C"}, {"queue_class": None}) == "C"
    assert QC.classify({}, None, QueueConfig(default_class="C")) == "C"
    assert QC.normalize("background") == "D" and QC.normalize("b") == "B"
    assert QC.normalize("urgent") is None


def test_the_priority_header_only_lowers():
    assert QC.classify({"priority": "background"}) == "D"
    assert QC.classify({"purpose": "prework", "priority": "A"}) == "C"      # can't jump the line
    assert QC.classify({"queue_class": "D", "priority": "interactive"}) == "D"
    assert client_auth.correlation({"x-foundry-priority": "D"})["priority"] == "D"


def test_aging_and_wait_limits():
    assert QC.effective("C", 599) == "C" and QC.effective("C", 600) == "B"
    assert QC.effective("C", 99999) == "B"                          # never above B
    assert [QC.effective("D", s) for s in (0, 900, 1499, 1500, 9000)] == ["D", "C", "C", "B", "B"]
    assert QC.effective("A", 0) == "A" and QC.effective("B", 99999) == "B"
    assert [QC.wait_seconds(c, 300) for c in "ABCD"] == [300, 600, 900, 1800]
    cfg = QueueConfig(classes={"D": QueueClassConfig(wait_factor=4, promote_after_seconds=0)})
    assert QC.wait_seconds("D", 60, cfg) == 240 and QC.effective("D", 99999, cfg) == "D"
    # off: everyone waits the backend's own timeout
    assert QC.wait_seconds("D", 300, QueueConfig(enabled=False)) == 300


# -- the slot gate ----------------------------------------------------------------------

def test_the_gate_serves_by_class_then_takes_turns():
    async def run():
        g = FairGate(1)
        await g.acquire("hog", 5)                                   # in flight
        order = []

        async def waiter(client, cls, tag):
            await g.acquire(client, 5, cls=cls, info={"persona": tag})
            order.append(tag)
        tasks = []
        for client, cls, tag in (("n8n", "D", "d1"), ("coord", "C", "c1"), ("coord", "B", "b1"),
                                 ("kilo", "B", "b2"), ("coord", "B", "b3"), ("chat", "A", "a1")):
            tasks.append(asyncio.create_task(waiter(client, cls, tag)))
            await asyncio.sleep(0.01)
        st = g.status()
        assert st["by_class"] == {"A": 1, "B": 3, "C": 1, "D": 1} and st["waiting_total"] == 6
        # the person first; inside B the clients take turns (coord, kilo, coord)
        assert [w["persona"] for w in st["waiters"]] == ["a1", "b1", "b2", "b3", "c1", "d1"]
        assert g.ahead_of(g.waiters[0].fut) == 5                    # d1 arrived first, goes last
        for _ in range(6):
            g.release()
            await asyncio.sleep(0.01)
        await asyncio.gather(*tasks)
        assert order == ["a1", "b1", "b2", "b3", "c1", "d1"]
        g.release()
        assert g.active == 0 and g.waiting_count() == 0
    asyncio.run(run())


def test_a_long_wait_moves_a_background_call_up():
    async def run():
        g = FairGate(1)
        await g.acquire("x", 5)
        order = []

        async def waiter(client, cls, tag):
            await g.acquire(client, 5, cls=cls)
            order.append(tag)
        t1 = asyncio.create_task(waiter("n8n", "D", "old-background"))
        await asyncio.sleep(0.01)
        t2 = asyncio.create_task(waiter("coord", "C", "fresh-optional"))
        await asyncio.sleep(0.01)
        g.waiters[0].since -= 1500                  # the background call has waited 25 minutes
        assert g.status()["waiters"][0]["counts_as"] == "B"
        g.release()
        await asyncio.sleep(0.01)
        g.release()
        await asyncio.gather(t1, t2)
        assert order == ["old-background", "fresh-optional"]
    asyncio.run(run())


# -- the ledger's wait for a backend stream ------------------------------------------------

def _ledger(timeout=5.0, hold=1.0):
    db = Database(os.path.join(tempfile.mkdtemp(), "q.sqlite"))
    backends = {"strata": SimpleNamespace(helpers_allowed=0, queue_timeout_seconds=timeout,
                                          session_hold_seconds=hold)}
    cfg = BudgetsConfig()
    return Ledger(lambda: cfg, db, get_backends=lambda: backends)


def test_the_stream_wait_is_ordered_by_class():
    """One stream (helpers_allowed 0). A background call started waiting first, a person
    second; when the stream comes free the person takes it."""
    led = _ledger()

    async def run():
        holder = led.reserve(CallCtx(model="qwen", backend="strata", session_id="sprint"))
        order = []

        async def waiter(cls, session):
            client_auth._client.set({"client_id": session, "queue_class": cls})
            eid = await led.reserve_wait(CallCtx(model="qwen", backend="strata",
                                                 client_id=session, session_id=session))
            order.append(cls)
            await asyncio.sleep(0.05)
            led.settle(eid, "settled", 10, 10, final=True)
        t_d = asyncio.create_task(waiter("D", "pulse"))
        await asyncio.sleep(0.7)
        t_a = asyncio.create_task(waiter("A", "chat"))
        await asyncio.sleep(0.7)
        rows = led.stream_waiters()["strata"]
        assert [(r["position"], r["class"], r["client"]) for r in rows] == [
            (1, "A", "chat"), (2, "D", "pulse")]
        led.settle(holder, "settled", 10, 10, final=True)           # the sprint's turn ends
        await asyncio.gather(t_d, t_a)
        assert order == ["A", "D"]
        assert led.stream_waiters() == {}
    asyncio.run(run())


def test_a_newcomer_does_not_jump_a_waiter_and_a_holder_is_never_queued():
    led = _ledger()

    async def run():
        holder_ctx = CallCtx(model="qwen", backend="strata", session_id="sprint")
        holder = led.reserve(holder_ctx)

        async def waiter():
            client_auth._client.set({"client_id": "coord", "purpose": "review"})
            return await led.reserve_wait(CallCtx(model="qwen", backend="strata",
                                                  client_id="coord", session_id="review"))
        t = asyncio.create_task(waiter())
        await asyncio.sleep(0.7)
        # the session holding the stream makes its next call: no queue for it
        again = await led.reserve_wait(holder_ctx)
        led.settle(again, "settled", 10, 10, final=True)
        led.settle(holder, "settled", 10, 10, final=True)
        # the stream is free; a background newcomer must go behind the waiting review
        client_auth._client.set({"client_id": "n8n", "queue_class": "D"})
        newcomer = CallCtx(model="qwen", backend="strata", client_id="n8n", session_id="pulse")
        assert led._stream_ahead(newcomer, "D", None) == 1
        # ... and a person goes ahead of it
        assert led._stream_ahead(newcomer, "A", None) == 0
        eid = await t
        led.settle(eid, "settled", 10, 10, final=True)
    asyncio.run(run())


def test_the_wait_limit_follows_the_class():
    led = _ledger(timeout=0.6, hold=0.5)
    led.reserve(CallCtx(model="qwen", backend="strata", session_id="sprint"))

    async def wait_as(cls):
        client_auth._client.set({"client_id": "x", "queue_class": cls})
        t = time.monotonic()
        with pytest.raises(BudgetDenied) as e:
            await led.reserve_wait(CallCtx(model="qwen", backend="strata", client_id="x",
                                           session_id="s-" + cls))
        assert e.value.rule == "backend-helpers"
        return time.monotonic() - t
    a = asyncio.run(wait_as("A"))
    c = asyncio.run(wait_as("C"))
    assert a < 1.4 and 1.5 < c < 3.2                 # 0.6 s x1 against 0.6 s x3


# -- a queued streaming call reports its place ------------------------------------------------

class _FakeProtocol:
    async def chat_stream(self, model, messages, **kw):
        yield {"content": "hi", "done": False}
        yield {"content": "", "done": True, "prompt_tokens": 5, "completion_tokens": 1}


def test_a_queued_stream_reports_its_place_and_frees_the_slot_on_cancel(monkeypatch):
    monkeypatch.setattr(pool_internal, "QUEUE_REPORT_SECONDS", 0.05)

    async def run():
        db = Database(os.path.join(tempfile.mkdtemp(), "p.sqlite"))
        pool = InternalPool([BackendConfig(name="strata", type="openai-compatible",
                                           url="http://strata/v1", flavor="llamacpp",
                                           max_concurrent=1, queue_timeout_seconds=5)],
                            BackendPoolConfig(), httpx.AsyncClient(), db)
        st = pool.backends["strata"]
        st.healthy, st.ever_checked, st.models, st.protocol = True, True, ["qwen"], _FakeProtocol()
        gate = pool._gate(st)
        await gate.acquire("sprint", 5)                             # the slot is taken
        client_auth._client.set({"client_id": "chat", "session_id": "c1"})
        chunks = []

        async def consume():
            async for ch in pool.chat_stream("qwen", [{"role": "user", "content": "hi"}]):
                chunks.append(ch)
        t = asyncio.create_task(consume())
        await asyncio.sleep(0.2)
        assert chunks and all(c["progress"]["queued"]["for"] == "slot" for c in chunks)
        q = chunks[0]["progress"]["queued"]
        assert q["backend"] == "strata" and q["class"] == "A" and q["ahead"] == 0
        assert keepalive.progress_detail({"queued": q}) == \
            "next in line for a slot on strata (class A)"
        assert keepalive.progress_detail({"queued": {**q, "ahead": 2, "for": "stream"}}) == \
            "queued behind 2 for its stream on strata (class A)"
        assert keepalive._is_progress_only(chunks[0])                # proof of life, not output
        assert pool.queue_status()[0]["waiters"][0]["client"] == "chat"
        gate.release()
        await t
        tail = chunks[-3:]
        assert tail[0]["progress"] == {"queued": None}              # status cleared on admission
        assert keepalive.progress_detail(tail[0]["progress"]) == ""
        assert tail[1]["content"] == "hi" and tail[2]["done"]
        assert gate.active == 0 and gate.waiting_count() == 0

        # a caller that goes away while queued leaves nothing behind
        await gate.acquire("sprint", 5)
        t = asyncio.create_task(consume())
        await asyncio.sleep(0.12)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert gate.waiting_count() == 0
        gate.release()
        assert gate.active == 0
    asyncio.run(run())


# -- settings ---------------------------------------------------------------------------------

def test_queue_settings_api_and_persona_field(app, client):
    svc = app.state.services
    d = client.get("/admin/api/setup/queue").json()
    assert d["settings"]["enabled"] is True and d["settings"]["default_class"] == "A"
    assert [c["class"] for c in d["settings"]["classes"]] == ["A", "B", "C", "D"]
    assert isinstance(d["queues"], list)
    r = client.post("/admin/api/setup/queue", json={
        "default_class": "background", "classes": {"D": {"wait_factor": 4},
                                                   "C": {"promote_after_seconds": 120}}})
    s = r.json()["settings"]
    assert r.status_code == 200 and s["default_class"] == "D"
    rows = {c["class"]: c for c in s["classes"]}
    assert rows["D"]["wait_factor"] == 4 and rows["D"]["promote_after_seconds"] == 900
    assert rows["C"]["promote_after_seconds"] == 120 and rows["C"]["wait_factor"] == 3
    # live: the module reads the saved config
    QC.configure(lambda: svc.config_store.config.queue)
    assert QC.classify({}) == "D" and QC.wait_seconds("D", 100) == 400
    assert client.post("/admin/api/setup/queue", json={"default_class": "Z"}).status_code == 400
    assert client.post("/admin/api/setup/queue",
                       json={"classes": {"A": {"wait_factor": 0}}}).status_code == 400
    # the persona's own class
    svc.personas.upsert("Queued", queue_class="D")
    assert svc.personas.get("Queued")["queue_class"] == "D"
    assert QC.classify({"queue_class": "A"}, svc.personas.get("Queued")) == "D"
    from foundry_router import persona_rules
    assert "queue_class" in persona_rules.ENUMS
