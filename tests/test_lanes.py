"""v0.181.118: lanes (a side backend for short jobs), try-then-divert, conversation
pinning, the busy-escalation fix for llama.cpp backends, and the hold for a backend
that is coming back."""

import asyncio
import json
import os
import tempfile
from types import SimpleNamespace

import httpx
import pytest

from foundry_router import client_auth, lanes as L, queue_class as QC
from foundry_router.budget import CallCtx, Ledger
from foundry_router.config import BackendConfig, BackendPoolConfig, BudgetsConfig, QueueConfig
from foundry_router.db import Database
from foundry_router.pool.internal import InternalPool
from foundry_router.pool.protocols import ChatResult

BIG, SMALL = "qwen-strata", "qwen-lane"


class _Proto:
    def __init__(self, name, calls):
        self.name, self.calls = name, calls

    async def chat(self, model, messages, **kw):
        self.calls.append({"backend": self.name, "model": model, **kw})
        return ChatResult(content=f"from {self.name}", prompt_tokens=10, completion_tokens=2,
                          finish_reason="stop")

    async def chat_stream(self, model, messages, **kw):
        self.calls.append({"backend": self.name, "model": model, **kw})
        yield {"content": f"from {self.name}", "done": False}
        yield {"content": "", "done": True, "prompt_tokens": 10, "completion_tokens": 2}


def _pool(db, calls, lane=True, **lane_kw):
    pool = InternalPool([
        BackendConfig(name="strata", type="openai-compatible", url="http://strata/v1",
                      flavor="llamacpp", max_concurrent=1, helpers_allowed=0,
                      queue_timeout_seconds=5, session_hold_seconds=30),
        BackendConfig(name="brain-lane", type="openai-compatible", url="http://brain/v1",
                      flavor="llamacpp", max_concurrent=1, priority=90, lane=lane, **lane_kw),
    ], BackendPoolConfig(), httpx.AsyncClient(), db)
    for name, model in (("strata", BIG), ("brain-lane", SMALL)):
        st = pool.backends[name]
        st.healthy, st.ever_checked, st.models = True, True, [model]
        st.protocol = _Proto(name, calls)
    return pool


@pytest.fixture()
def rig():
    """A real pool (Strata + a lane) and ledger behind a small fake Services."""
    QC.configure(None)
    L._PINS.clear()
    db = Database(os.path.join(tempfile.mkdtemp(), "l.sqlite"))
    calls: list = []
    pool = _pool(db, calls)
    cfg = BudgetsConfig()
    pool.ledger = Ledger(lambda: cfg, db,
                         get_backends=lambda: {n: s.config for n, s in pool.backends.items()})
    svc = SimpleNamespace(pool=pool, db=db, ledger=pool.ledger,
                          registry=SimpleNamespace(get=lambda m: {}))
    yield svc, calls
    L._PINS.clear()
    QC.configure(None)


def _hold_strata(svc):
    """A sprint session takes Strata's one stream."""
    return svc.ledger.reserve(CallCtx(model=BIG, backend="strata", session_id="sprint"))


def _facts(tokens=200, tools=False, turns=0, key="P:c1"):
    return L.Facts(prompt_tokens=tokens, has_tools=tools, assistant_turns=turns, conv_key=key)


# -- what a lane is ----------------------------------------------------------------------

def test_lane_models_are_left_out_of_automatic_routing(rig):
    svc, _ = rig
    assert list(svc.pool.available_models()) == [BIG]             # ranking never sees the lane
    assert set(L.all_models(svc.pool)) == {BIG, SMALL}            # a persona may still name it
    assert L.lane_cfg(svc, SMALL).name == "brain-lane" and L.lane_cfg(svc, BIG) is None
    assert svc.pool.backend_info(SMALL)["lane"] is True
    # other pool modes have no `lanes` argument
    assert L.all_models(SimpleNamespace(available_models=lambda: {"m": ["b"]})) == {"m": ["b"]}


def test_what_fits_a_lane(rig):
    svc, _ = rig
    cfg = L.lane_cfg(svc, SMALL)
    assert L.eligible(cfg, _facts()) == (True, "")
    ok, why = L.eligible(cfg, _facts(tokens=9000))
    assert not ok and "9,000 tokens" in why and "3,000" in why
    assert not L.eligible(cfg, _facts(tools=True))[0]
    assert not L.eligible(cfg, _facts(turns=2))[0]
    assert L.eligible(cfg, _facts(turns=2), pinned=True)[0]        # its own conversation
    # on the lane: short replies, no thinking
    assert L.output_cap(svc, SMALL, 8192) == 400 and L.output_cap(svc, BIG, 8192) == 8192
    assert L.thinking_off(svc, SMALL) and not L.thinking_off(svc, BIG)


def test_free_and_busy(rig):
    svc, _ = rig
    assert L.model_free(svc, BIG) and L.model_free(svc, SMALL)
    eid = _hold_strata(svc)
    client_auth._client.set({"client_id": "chat", "session_id": "c1"})
    assert not L.model_free(svc, BIG)                              # the sprint holds the stream
    client_auth._client.set({"client_id": "kilo", "session_id": "sprint"})
    assert L.model_free(svc, BIG)                                  # ... but not against itself
    svc.ledger.settle(eid, "settled", 10, 10, final=True)
    client_auth._client.set({"client_id": "chat", "session_id": "c1"})
    assert L.model_free(svc, BIG)
    lane_gate = svc.pool._gate(svc.pool.backends["brain-lane"])
    asyncio.run(lane_gate.acquire("x", 1))
    assert not L.model_free(svc, SMALL)                            # its one slot is taken
    assert [d["free"] for d in L.describe(svc)] == [False]


# -- strict chains ---------------------------------------------------------------------------

def test_a_strict_chain_diverts_to_the_free_lane_and_pins_the_conversation(rig):
    svc, _ = rig
    persona = {"virtual_name": "P", "divert_when_busy": 1}
    client_auth._client.set({"client_id": "chat", "session_id": "c1"})
    # nothing busy: the chain as written
    assert L.order_chain(svc, persona, [BIG, SMALL], _facts()) == ([BIG, SMALL], [])
    _hold_strata(svc)
    out, notes = L.order_chain(svc, persona, [BIG, SMALL], _facts())
    assert out == [SMALL, BIG] and "is busy" in notes[0] and "divert when busy" in notes[0]
    # without the setting: wait in the queue, as before
    assert L.order_chain(svc, {"virtual_name": "P"}, [BIG, SMALL], _facts())[0] == [BIG, SMALL]
    # a big prompt or a tool request never goes to the lane
    out, notes = L.order_chain(svc, persona, [BIG, SMALL], _facts(tokens=50_000))
    assert out == [BIG] and "short jobs only" in notes[0]
    assert L.order_chain(svc, persona, [SMALL, BIG], _facts(tools=True))[0] == [BIG]
    # a chain of only the lane is never emptied (the lane gets to try)
    assert L.order_chain(svc, persona, [SMALL], _facts(tokens=50_000)) == ([SMALL], [])
    # served by the lane: the conversation is pinned there, so turn two (which now has
    # an assistant reply) stays on it
    L.remember(svc, persona, _facts(), SMALL)
    out, notes = L.order_chain(svc, {"virtual_name": "P"}, [BIG, SMALL], _facts(turns=1))
    assert out == [SMALL, BIG] and "pinned" in notes[0]
    # a different conversation with replies from elsewhere is not eligible
    assert L.order_chain(svc, persona, [BIG, SMALL], _facts(turns=1, key="P:other"))[0] == [BIG]
    # once the pinned conversation outgrows the lane it moves on and the pin ends
    assert L.order_chain(svc, persona, [BIG, SMALL], _facts(tokens=9000, turns=3))[0] == [BIG]
    L.remember(svc, persona, _facts(tokens=9000, turns=3), BIG)
    assert L.pin_get("P:c1") is None


def test_paid_models_keep_their_place(rig, monkeypatch):
    svc, _ = rig
    monkeypatch.setattr(L, "is_local", lambda s, m: m in (BIG, SMALL))
    _hold_strata(svc)
    client_auth._client.set({"client_id": "chat", "session_id": "c1"})
    out, _ = L.order_chain(svc, {"virtual_name": "P", "divert_when_busy": 1},
                           ["claude", "gpt", BIG, SMALL], _facts())
    assert out == ["claude", "gpt", SMALL, BIG]


def test_pin_conversation_keeps_the_first_model(rig):
    svc, _ = rig
    persona = {"virtual_name": "P", "pin_conversation": 1}
    L.remember(svc, persona, _facts(), BIG)
    assert L.pin_get("P:c1") == BIG
    out, notes = L.order_chain(svc, persona, ["claude", BIG], _facts(turns=1))
    assert out == [BIG, "claude"] and "pinned" in notes[0]
    # not pinned without the setting (and not for a non-lane model)
    L._PINS.clear()
    L.remember(svc, {"virtual_name": "P"}, _facts(), BIG)
    assert L.pin_get("P:c1") is None


# -- flexible personas -------------------------------------------------------------------------

def test_a_flexible_pick_diverts_to_a_free_lane(rig):
    svc, _ = rig
    persona = {"virtual_name": "P", "divert_when_busy": 1}
    client_auth._client.set({"client_id": "chat", "session_id": "c1"})
    assert L.divert(svc, persona, BIG, _facts()) == (BIG, "")       # not busy
    _hold_strata(svc)
    m, note = L.divert(svc, persona, BIG, _facts())
    assert m == SMALL and "is busy" in note
    assert L.divert(svc, {"virtual_name": "P"}, BIG, _facts()) == (BIG, "")      # not opted in
    assert L.divert(svc, persona, BIG, _facts(tokens=20_000)) == (BIG, "")       # doesn't fit
    asyncio.run(svc.pool._gate(svc.pool.backends["brain-lane"]).acquire("x", 1))
    assert L.divert(svc, persona, BIG, _facts()) == (BIG, "")       # the lane is busy too: wait


def test_escalate_when_busy_now_sees_a_llamacpp_backend(rig, monkeypatch):
    """It only looked at Ollama backends, so it never fired for Strata."""
    from foundry_router.facade import ollama_api as O
    svc, _ = rig
    svc.registry.ranked_for_category = lambda cat, models, limit=1: [{"id": models[0]}]
    monkeypatch.setattr(O, "_available_paid", lambda s, p: ["claude-sonnet-5-5"])
    persona = {"virtual_name": "P", "escalate_when_local_busy": 1}
    client_auth._client.set({"client_id": "chat", "session_id": "c1"})
    assert O._escalate_if_local_busy(svc, persona, BIG, "hi") == BIG           # idle
    _hold_strata(svc)
    assert O._escalate_if_local_busy(svc, persona, BIG, "hi") == "claude-sonnet-5-5"
    assert O._escalate_if_local_busy(svc, {"virtual_name": "P"}, BIG, "hi") == BIG


def test_a_brain_routed_chat_tries_paid_models_first_while_local_is_busy(rig):
    """v0.181.119: Foundry-Chat (brain-routed, with tools) always queued behind whatever
    held Strata; escalate-when-busy only worked on the direct path."""
    from foundry_router.brain.agent import AgentRunner
    svc, _ = rig
    notes = []
    ctx = SimpleNamespace(logger=SimpleNamespace(record_guardrail=notes.append))
    runner = SimpleNamespace(pool=svc.pool)
    ranked = [{"id": BIG}, {"id": "claude-haiku-5-5"}, {"id": "claude-sonnet-5-5"}]
    client_auth._client.set({"client_id": "chat", "session_id": "c1"})
    assert AgentRunner._busy_local_last(runner, ranked, ctx) == ranked      # local is free
    _hold_strata(svc)
    out = AgentRunner._busy_local_last(runner, ranked, ctx)
    assert [r["id"] for r in out] == ["claude-haiku-5-5", "claude-sonnet-5-5", BIG]
    assert "is busy" in notes[0]
    # only local models on the route: nothing to try first, it waits in the queue
    assert AgentRunner._busy_local_last(runner, [{"id": BIG}], ctx) == [{"id": BIG}]


# -- a backend that is coming back ----------------------------------------------------------------

def test_a_strict_request_is_held_for_a_backend_coming_back(rig):
    from foundry_router.facade import ollama_api as O
    svc, _ = rig
    persona = {"virtual_name": "Impl", "routing": "strict",
               "model_allowlist": json.dumps([BIG])}
    st = svc.pool.backends["strata"]
    st.healthy = False                                             # Strata just went down
    assert O._recovering_backends(svc, persona) == ["strata"]
    assert O.strict_candidates(svc, persona) == []
    QC.configure(lambda: QueueConfig(recovery_hold_seconds=3, recovery_hold_interactive_seconds=0.3))

    async def back_after(delay):
        await asyncio.sleep(delay)
        st.healthy = True

    async def run():
        # coordinator work (class B) waits and gets through
        client_auth._client.set({"client_id": "coordinator", "purpose": "implementer"})
        t = asyncio.create_task(back_after(0.2))
        cands = await O._await_recovery(svc, persona)
        await t
        assert cands == [BIG]
        ev = svc.db.query("SELECT message FROM event_log WHERE source='routing' "
                          "ORDER BY id DESC LIMIT 1")
        assert "for strata to come back" in ev[0]["message"]
        # still down after the hold: the usual refusal follows
        st.healthy = False
        client_auth._client.set({"client_id": "chat"})              # a person: the short hold
        t0 = asyncio.get_event_loop().time()
        assert await O._await_recovery(svc, persona) == []
        assert asyncio.get_event_loop().time() - t0 < 1.5
        # nothing is "coming back" when no backend ever served the model
        assert await O._await_recovery(
            svc, {"virtual_name": "X", "model_allowlist": json.dumps(["nope"])}) == []
    asyncio.run(run())


# -- through the chat API ----------------------------------------------------------------------------

def test_a_short_chat_goes_to_the_lane_while_a_sprint_holds_strata(app, client):
    svc = app.state.services
    QC.configure(lambda: svc.config_store.config.queue)
    L._PINS.clear()
    calls: list = []
    real = svc.pool
    pool = _pool(svc.db, calls)
    pool.ledger = svc.ledger
    svc.pool = pool
    svc.personas.upsert("Utility", execution_mode="direct", routing="strict",
                        model_allowlist=[BIG, SMALL], pinned_models=[BIG, SMALL],
                        divert_when_busy=1, max_output_tokens=4096)
    hi = [{"role": "user", "content": "give this note a five word title"}]
    try:
        # Strata idle: the chain's first model answers, with the persona's own cap
        r = client.post("/v1/chat/completions", json={"model": "Utility", "messages": hi})
        assert r.status_code == 200 and calls[-1]["backend"] == "strata"
        assert calls[-1]["max_tokens"] > 400
        # the turn ended in a final answer, so the stream is free again; now a sprint takes it
        svc.ledger.reserve(CallCtx(model=BIG, backend="strata", session_id="sprint",
                                   client_id="kilo"))
        r = client.post("/v1/chat/completions", json={"model": "Utility", "messages": hi},
                        headers={"x-foundry-session": "note-1"})
        assert r.status_code == 200 and calls[-1]["backend"] == "brain-lane"
        assert r.json()["choices"][0]["message"]["content"].startswith("from brain-lane")
        assert calls[-1]["max_tokens"] == 400 and calls[-1]["think"] in (False, None)
        # turn two of that conversation stays on the lane
        r = client.post("/v1/chat/completions", headers={"x-foundry-session": "note-1"}, json={
            "model": "Utility", "messages": hi + [{"role": "assistant", "content": "A title"},
                                                 {"role": "user", "content": "shorter"}]})
        assert calls[-1]["backend"] == "brain-lane"
        # a long prompt doesn't fit the lane: it is not sent there
        n = len(calls)
        long = [{"role": "user", "content": "word " * 20000}]
        r = client.post("/v1/chat/completions", json={"model": "Utility", "messages": long},
                        headers={"x-foundry-session": "long-1"})
        assert all(c["backend"] != "brain-lane" for c in calls[n:])
    finally:
        svc.pool = real
        QC.configure(None)
        L._PINS.clear()


def test_a_local_llamacpp_model_is_registered_as_free(app):
    """Only Ollama backends were marked free at discovery; Strata's model had no cost."""
    svc = app.state.services
    real = svc.pool
    svc.pool = _pool(svc.db, [])
    try:
        svc.register_discovered()
        for m in (BIG, SMALL):
            row = svc.registry.get(m)
            assert row["relative_cost_tier"] == "free" and row["cost_per_1k_input"] == 0.0
    finally:
        svc.pool = real


def test_lane_settings_are_editable(app, client):
    svc = app.state.services
    name = "brain-lane"

    def add_backend(raw):
        raw.setdefault("backend_pool", {}).setdefault("internal", {})["backends"] = [
            {"name": name, "type": "openai-compatible", "url": "http://127.0.0.1:9/v1",
             "flavor": "llamacpp"}]
    svc.config_store.save(add_backend)
    r = client.post("/admin/api/setup/limits", json={
        "name": name, "lane": True, "lane_max_prompt_tokens": 2000,
        "lane_max_output_tokens": 300, "lane_allow_tools": False, "lane_thinking": True})
    assert r.status_code == 200
    row = next(b for b in client.get("/admin/api/setup/limits").json()["backends"]
               if b["name"] == name)
    assert row["lane"] is True and row["lane_max_prompt_tokens"] == 2000
    assert row["lane_max_output_tokens"] == 300 and row["lane_thinking"] is True
    assert [l["backend"] for l in client.get("/admin/api/setup/queue").json()["lanes"]] == [name]
    assert client.post("/admin/api/setup/limits", json={
        "name": name, "lane_max_prompt_tokens": -5}).status_code == 400
    svc.personas.upsert("Div", divert_when_busy=1, pin_conversation=1)
    p = svc.personas.get("Div")
    assert p["divert_when_busy"] == 1 and p["pin_conversation"] == 1
