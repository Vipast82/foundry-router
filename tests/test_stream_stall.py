"""Streaming robustness: an abandoned / stalled stream closes its upstream
(no orphaned llama.cpp or Claude requests), a silent backend is abandoned
after direct_stream_stall_seconds, a busy backend isn't marked down by a
probe timeout, and a switch to Claude says why."""

import asyncio

import httpx
import pytest

from foundry_router.facade.ollama_api import (StreamStalled, _local_down_notes,
                                              _stream_with_heartbeat)


def _upstream(closed: list, gap: float, n: int = 3, tail_hang: bool = False):
    async def gen():
        try:
            for i in range(n):
                await asyncio.sleep(gap)
                yield {"content": f"t{i}"}
            if tail_hang:
                while True:                      # keep-alive only, never output
                    await asyncio.sleep(0.05)
                    yield {"content": ""}
        finally:
            closed.append(True)
    return gen()


async def test_consumer_abandoning_stream_closes_upstream():
    closed: list = []
    agen = _stream_with_heartbeat(_upstream(closed, 0.3, n=10), hb=0.05, start=0)
    got = []
    async for kind, _p in agen:
        got.append(kind)
        if len(got) >= 3:
            break
    await agen.aclose()                      # what Starlette does on disconnect
    await asyncio.sleep(0.05)
    assert closed == [True]


async def test_cancelled_consumer_closes_upstream():
    closed: list = []

    async def consume():
        async for _ in _stream_with_heartbeat(_upstream(closed, 5.0), hb=0.05, start=0):
            pass
    t = asyncio.ensure_future(consume())
    await asyncio.sleep(0.2)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert closed == [True]


async def test_stall_raises_and_closes_upstream_but_keepalives_dont_count():
    closed: list = []
    kinds = []
    with pytest.raises(StreamStalled):
        async for kind, p in _stream_with_heartbeat(
                _upstream(closed, 0.01, n=1, tail_hang=True), hb=0.1, start=0, stall=0.5):
            kinds.append(kind)
    assert closed == [True]
    assert "chunk" in kinds


async def test_output_resets_stall_timer():
    closed: list = []
    out = [p async for k, p in _stream_with_heartbeat(
        _upstream(closed, 0.3, n=4), hb=0, start=0, stall=0.5) if k == "chunk"]
    assert [c["content"] for c in out] == ["t0", "t1", "t2", "t3"]   # 1.2s total, no stall


async def test_busy_backend_probe_timeout_not_counted(tmp_path):
    from foundry_router.config import BackendConfig, BackendPoolConfig
    from foundry_router.db import Database
    from foundry_router.pool.internal import InternalPool

    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pool = InternalPool([BackendConfig(name="llama", type="openai-compatible",
                                       url="http://llama:8080", flavor="llamacpp")],
                        BackendPoolConfig(failure_threshold=1), http,
                        Database(tmp_path / "p.sqlite"))
    s = pool.backends["llama"]
    s.healthy, s.ever_checked, s.models = True, True, ["qwen"]
    s.busy = 1                                   # mid-prefill on a big prompt
    await pool._check_backend(s)
    assert s.healthy and s.consecutive_failures == 0 and "busy" in s.last_error
    s.busy = 0                                   # idle and unresponsive -> counts
    await pool._check_backend(s)
    assert not s.healthy


def test_local_down_note_explains_switch_to_claude():
    class P:
        def backend_info(self, m):
            return {"type": "anthropic-compatible"} if m == "claude-sonnet-5" else {"type": "openai-compatible"}

        def backend_status(self):
            return [{"name": "llama-qwen38-mtp", "type": "openai-compatible", "healthy": False,
                     "last_error": "discovery failed: ConnectError"},
                    {"name": "meridian", "type": "anthropic-compatible", "healthy": True}]

    class S:
        pool = P()
    notes = _local_down_notes(S(), "claude-sonnet-5")
    assert notes and "llama-qwen38-mtp" in notes[0] and "claude-sonnet-5" in notes[0]
    assert _local_down_notes(S(), "qwen") == []


def test_direct_stream_stalled_model_fails_over(app, client):
    """A model that goes silent is abandoned after the stall window and the
    next allowed model answers the same turn; the hung upstream is closed."""
    import json as _json
    from foundry_router.pool.protocols import ChatResult
    svc = app.state.services
    closed: list = []

    class Pool:
        def backend_info(self, m):
            return {"name": "b-" + m, "type": "ollama", "url": "http://x"}

        def available_models(self):
            return {"hung": ["b-hung"], "good": ["b-good"]}

        def active_calls(self):
            return []

        def backend_status(self):
            return []

        async def chat_stream(self, model, messages, **kw):
            try:
                if model == "hung":
                    while True:
                        await asyncio.sleep(0.05)
                        yield {"content": ""}           # pings, never output
                yield {"content": "ok from good"}
                yield ChatResult(content="ok from good", completion_tokens=3).done_frame()
            finally:
                if model == "hung":
                    closed.append(True)

    real, svc.pool = svc.pool, Pool()
    cfg = svc.config_store.config.agent_brain
    old = (cfg.direct_stream, cfg.direct_stream_heartbeat_seconds, cfg.direct_stream_stall_seconds)
    cfg.direct_stream, cfg.direct_stream_heartbeat_seconds, cfg.direct_stream_stall_seconds = True, 0, 1
    svc.personas.upsert("Act", execution_mode="direct", model_allowlist=["hung", "good"],
                        pinned_models=[])
    import foundry_router.facade.ollama_api as oa
    orig_pick, orig_fo = oa.pick_fallback_model, oa._failover_list
    oa.pick_fallback_model = lambda *a, **k: "hung"

    async def fo(*a, **k):
        return ["hung", "good"]
    oa._failover_list = fo
    try:
        r = client.post("/api/chat", json={"model": "Act", "messages": [
            {"role": "user", "content": "go"}]})
    finally:
        svc.pool = real
        oa.pick_fallback_model, oa._failover_list = orig_pick, orig_fo
        cfg.direct_stream, cfg.direct_stream_heartbeat_seconds, cfg.direct_stream_stall_seconds = old
    lines = [_json.loads(x) for x in r.text.splitlines() if x.strip()]
    thinking = "".join(l["message"].get("thinking") or "" for l in lines)
    assert "no output for" in thinking and "failing over to good" in thinking
    assert "".join(l["message"]["content"] for l in lines) == "ok from good"
    assert closed == [True]
