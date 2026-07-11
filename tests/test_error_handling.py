"""Regression tests for the two live bugs:

Bug 1b (error swallowing): "all backends failed for 'qwen3.6:27b':
truenas-ollama: " — str(e) on httpx transport errors is often empty, and
ExceptionGroups stringify uselessly. describe_exception must never come back
empty, and the pool must carry the real text.

Bug 2 (blind guardrail): Meridian's quota sources can report nothing even at
100% used, and Anthropic uses the same "authentication expired" text for
exhaustion as for credential failure — an observed failure of that shape on a
subscription backend becomes the guardrail's signal, cleared by the next
successful call."""

import pytest

from foundry_router.config import BackendConfig, BackendPoolConfig, MeridianConfig
from foundry_router.db import Database
from foundry_router.errors import describe_exception
from foundry_router.pool.base import AllBackendsFailed
from foundry_router.pool.internal import InternalPool
from foundry_router.usage import MeridianUsage, looks_like_window_exhaustion


class EmptyMessageError(OSError):
    """Mimics httpx.ReadError's frequently-empty str()."""
    def __str__(self):
        return ""


# -- describe_exception ---------------------------------------------------------

def test_empty_message_gets_class_name():
    assert describe_exception(EmptyMessageError()) == "EmptyMessageError (no message)"


def test_exception_group_unwrapped():
    eg = ExceptionGroup("unhandled errors in a TaskGroup",
                        [EmptyMessageError(), ValueError("stream reset")])
    text = describe_exception(eg)
    assert "EmptyMessageError (no message)" in text
    assert "ValueError: stream reset" in text


def test_cause_chain_included():
    try:
        try:
            raise EmptyMessageError()
        except EmptyMessageError as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as e:
        text = describe_exception(e)
    assert "RuntimeError: wrapper" in text
    assert "EmptyMessageError" in text


# -- pool preserves real error text ------------------------------------------------

class EmptyFailProtocol:
    async def list_models(self):
        return ["m"]

    async def chat(self, model, messages, tools=None, options=None,
                   keep_alive=None, max_tokens=4096):
        raise EmptyMessageError()


async def test_pool_failure_message_never_empty(tmp_path):
    db = Database(tmp_path / "p.sqlite")
    pool = InternalPool([BackendConfig(name="truenas-ollama", type="ollama",
                                       url="http://x", priority=100)],
                        BackendPoolConfig(), client=None, db=db)
    pool.backends["truenas-ollama"].protocol = EmptyFailProtocol()
    await pool.check_all()
    with pytest.raises(AllBackendsFailed) as exc:
        await pool.chat("m", [{"role": "user", "content": "hi"}])
    msg = str(exc.value)
    # the live bug: "truenas-ollama: " with nothing after the colon
    assert "truenas-ollama: EmptyMessageError (no message)" in msg


# -- observed-exhaustion fallback -----------------------------------------------------

def test_exhaustion_error_shapes_recognized():
    # confirmed live: usage exhaustion arrives as an auth error
    assert looks_like_window_exhaustion(
        'HTTP 401: {"error":{"type":"authentication_error",'
        '"message":"authentication expired"}}')
    assert looks_like_window_exhaustion("HTTP 429 rate_limit_error")
    assert not looks_like_window_exhaustion("connection refused")


class NoSignalHTTP:
    """Quota endpoint that answers but carries no usage signal — the exact
    live condition (oauth null, sdk entryCount 0)."""
    async def get(self, url, headers=None, timeout=None):
        class R:
            def raise_for_status(self):
                pass
            def json(self):
                return {"buckets": [{"type": "five_hour", "utilization": None,
                                     "resetsAt": None}]}
        return R()


async def test_observed_exhaustion_overrides_blind_quota(tmp_path):
    db = Database(tmp_path / "o.sqlite")
    usage = MeridianUsage(MeridianConfig(), NoSignalHTTP(), db)

    snap = await usage.snapshot("http://m")
    assert snap["available"] is True          # blind endpoint: fail-open, as before

    usage.note_observed_exhaustion("http://m")
    snap = await usage.snapshot("http://m")
    assert snap["available"] is False         # observed signal now governs
    assert "OBSERVED" in snap["note"]

    usage.note_successful_call("http://m")    # a working call clears the backoff
    snap = await usage.snapshot("http://m")
    assert snap["available"] is True


async def test_real_quota_data_beats_observed_inference(tmp_path):
    class RealSignalHTTP:
        async def get(self, url, headers=None, timeout=None):
            class R:
                def raise_for_status(self):
                    pass
                def json(self):
                    return {"buckets": [{"type": "five_hour", "utilization": 0.2,
                                         "resetsAt": None}]}
            return R()
    db = Database(tmp_path / "o2.sqlite")
    usage = MeridianUsage(MeridianConfig(), RealSignalHTTP(), db)
    usage.note_observed_exhaustion("http://m")
    snap = await usage.snapshot("http://m")
    assert snap["available"] is True          # 20% used from real data wins
    assert snap["worst_used"] == 0.2


async def test_dispatch_worker_records_exhaustion(tmp_path):
    """The canonical dispatch path is the sensor: an exhaustion-shaped
    AllBackendsFailed on a subscription backend marks the window."""
    from foundry_router.brain.agent import AgentRunner
    from foundry_router.config import AgentBrainConfig, GuardrailsConfig
    from foundry_router.guardrails import GuardrailEngine
    from foundry_router.registry.models_db import ModelRegistry
    from foundry_router.tools.mcp_client import MCPManager
    from foundry_router.tools.sync import ToolRegistry

    class ExhaustedPool:
        def available_models(self):
            return {"claude-haiku-4-5": ["meridian"]}
        def backend_info(self, m):
            return {"name": "meridian", "type": "anthropic-compatible",
                    "url": "http://m", "api_key": "k"}
        async def chat(self, model, messages, tools=None, options=None,
                       max_tokens=4096):
            raise AllBackendsFailed(
                "all backends failed for 'claude-haiku-4-5': meridian: "
                "ProtocolError: HTTP 401 authentication expired")

    class DummyBrain:
        cfg = AgentBrainConfig()
        async def complete(self, prompt):
            return ""

    db = Database(tmp_path / "d.sqlite")
    registry = ModelRegistry(db)
    usage = MeridianUsage(MeridianConfig(), NoSignalHTTP(), db)
    runner = AgentRunner(DummyBrain(), ExhaustedPool(),
                         ToolRegistry(db, registry, MCPManager([], db)),
                         registry, GuardrailEngine(GuardrailsConfig(), db, usage),
                         usage)
    with pytest.raises(AllBackendsFailed):
        await runner._dispatch_worker("claude-haiku-4-5", "hello")
    snap = await usage.snapshot("http://m")
    assert snap["available"] is False and "OBSERVED" in snap["note"]
