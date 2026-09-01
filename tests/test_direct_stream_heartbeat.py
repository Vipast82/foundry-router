"""Direct-stream keep-alive heartbeat + Ollama num_predict cap.

The heartbeat injects a 'still working' marker during a silent backend gap so a
coding client (Cline) shows progress instead of a frozen 'Thinking…'; num_predict
caps a runaway generation (previously uncapped on the streaming path)."""

import time

from foundry_router.facade.ollama_api import _stream_with_heartbeat
from foundry_router.pool.protocols import OllamaProtocol


async def _src(gap: float):
    yield {"content": "a", "done": False}
    import asyncio
    await asyncio.sleep(gap)
    yield {"content": "b", "done": True}


# -- heartbeat -------------------------------------------------------------------

async def test_heartbeat_beats_during_silent_gap():
    kinds = []
    async for kind, _payload in _stream_with_heartbeat(_src(0.16), 0.05, time.monotonic()):
        kinds.append(kind)
    assert kinds[0] == "chunk"          # first real chunk
    assert "beat" in kinds              # at least one keep-alive during the gap
    assert kinds[-1] == "chunk"         # final chunk still delivered


async def test_heartbeat_off_is_passthrough():
    kinds = [k async for k, _ in _stream_with_heartbeat(_src(0.16), 0, time.monotonic())]
    assert kinds == ["chunk", "chunk"]  # hb<=0 → no beats, pure passthrough


async def test_heartbeat_no_beat_when_fast():
    kinds = [k async for k, _ in _stream_with_heartbeat(_src(0.0), 0.5, time.monotonic())]
    assert kinds == ["chunk", "chunk"]  # chunks arrive within the window → no beat


# -- num_predict cap -------------------------------------------------------------

def _payload(options=None, max_tokens=None):
    p = OllamaProtocol("http://x", None, None)
    return p._payload("m", [{"role": "user", "content": "hi"}], None, options, None,
                      stream=False, max_tokens=max_tokens)


def test_num_predict_from_max_tokens():
    assert _payload(max_tokens=32768)["options"]["num_predict"] == 32768


def test_client_num_predict_wins():
    pl = _payload(options={"num_predict": 100}, max_tokens=32768)
    assert pl["options"]["num_predict"] == 100        # explicit client value preserved


def test_no_num_predict_without_max_tokens():
    pl = _payload()
    assert "options" not in pl                        # nothing to send → no options block
