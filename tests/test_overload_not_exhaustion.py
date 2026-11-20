"""v0.181.214: a Claude "overloaded" error is not an exhausted usage window.

2026-11-20, 19:02-19:36 UTC: 59 HTTP 503 `overloaded_error` replies through
Meridian ("Session bookkeeping is unavailable; retry shortly." - Meridian's
own, after the router host's outage; they never reached Anthropic). "overloaded"
was an exhaustion marker, so each one backed Claude off for 30 minutes while
the quota endpoint showed the weekly window 5% used, and the strict routes
went claude-sonnet-5-5 denied -> gpt-6-astra denied (out of credits) -> local
qwen. A per-minute 429 was an exhaustion marker too.
"""
import pytest

from foundry_router.config import MeridianConfig
from foundry_router.db import Database
from foundry_router.pool.base import AllBackendsFailed
from foundry_router.usage import (MeridianUsage, backend_error_text,
                                  looks_like_window_exhaustion,
                                  note_subscription_failure, subscription_error_kind)

# the live reply, as the pool reported it
LIVE_503 = ("all backends failed for 'claude-sonnet-5-5': meridian: ProtocolError: "
            "anthropic-compat http://192.168.0.112:3456 HTTP 503: b'{\"type\":\"error\","
            "\"error\":{\"type\":\"overloaded_error\",\"message\":\"Session bookkeeping "
            "is unavailable; retry shortly.\",\"retry_after\":5}}'")
AUTH_EXPIRED = ('meridian: HTTP 401: {"error":{"type":"authentication_error",'
                '"message":"authentication expired"}}')
USAGE_LIMIT = "meridian: HTTP 429: Claude AI usage limit reached|1795300000"
BINFO = {"name": "meridian", "type": "anthropic-compatible", "url": "http://m"}


class NoSignalHTTP:
    """The quota endpoint answering with no usage figures - the condition in
    which an observed exhaustion governs."""
    async def get(self, url, headers=None, timeout=None):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"buckets": [{"type": "five_hour", "utilization": None,
                                     "resetsAt": None}]}
        return R()


def _usage(tmp_path):
    db = Database(tmp_path / "u.sqlite")
    return MeridianUsage(MeridianConfig(), NoSignalHTTP(), db), db


# -- the classifier -----------------------------------------------------------------

@pytest.mark.parametrize("text,kind", [
    (LIVE_503, "overloaded"),
    ("HTTP 529: overloaded", "overloaded"),
    ("meridian: HTTP 503 Service Unavailable", "overloaded"),
    (AUTH_EXPIRED, "exhausted"),
    (USAGE_LIMIT, "exhausted"),
    ('HTTP 429: {"error":{"type":"rate_limit_error","message":"usage_limit_exceeded"}}',
     "exhausted"),
    ("HTTP 429 rate_limit_error", "rate_limited"),
    ("connection refused", None),
])
def test_each_shape_is_named_for_what_it_is(text, kind):
    assert subscription_error_kind(text, "meridian") == kind


def test_an_overload_is_not_window_exhaustion():
    assert looks_like_window_exhaustion(LIVE_503) is False
    assert looks_like_window_exhaustion(AUTH_EXPIRED) is True
    assert looks_like_window_exhaustion(USAGE_LIMIT) is True


def test_another_backends_error_in_a_joined_failure_is_not_this_ones():
    """AllBackendsFailed joins every backend's error. A CLIProxyAPI 429 or
    auth error in the same message must not spend Meridian's window."""
    joined = ("all backends failed for 'shared': meridian: connection refused | "
              "cliproxyapi: HTTP 401 authentication_error")
    assert subscription_error_kind(joined, "meridian") is None
    assert backend_error_text(joined, "meridian").endswith("connection refused")
    assert subscription_error_kind(joined, "cliproxyapi") == "exhausted"
    # without a name the whole text is read, as before
    assert subscription_error_kind(joined) == "exhausted"


# -- what each one does to the usage window ------------------------------------------

async def test_an_overload_sets_no_backoff_and_says_what_it_was(tmp_path):
    usage, db = _usage(tmp_path)
    assert note_subscription_failure(usage, BINFO, LIVE_503) == "overloaded"
    assert usage._observed_exhausted == {}
    snap = await usage.snapshot("http://m")
    assert snap["available"] is True
    assert "OBSERVED" not in (snap.get("note") or "")
    ev = db.query("SELECT message FROM event_log WHERE source='usage'")
    assert any("overloaded" in e["message"] for e in ev)
    assert not any("window exhaustion OBSERVED" in e["message"] for e in ev)


async def test_fifty_nine_overloads_never_deny_claude(tmp_path):
    """The live count, one after another."""
    usage, db = _usage(tmp_path)
    for _ in range(59):
        note_subscription_failure(usage, BINFO, LIVE_503)
    assert (await usage.snapshot("http://m"))["available"] is True
    logged = [e for e in db.query("SELECT message FROM event_log WHERE source='usage'")
              if "overloaded" in e["message"]]
    assert len(logged) == 1, "one line for a run of them, not fifty-nine"


async def test_a_bare_rate_limit_sets_no_thirty_minute_backoff(tmp_path):
    usage, _db = _usage(tmp_path)
    assert note_subscription_failure(usage, BINFO, "meridian: HTTP 429 rate_limit_error") \
        == "rate_limited"
    assert (await usage.snapshot("http://m"))["available"] is True


async def test_authentication_expired_still_backs_off(tmp_path):
    usage, _db = _usage(tmp_path)
    assert note_subscription_failure(usage, BINFO, AUTH_EXPIRED) == "exhausted"
    snap = await usage.snapshot("http://m")
    assert snap["available"] is False and "OBSERVED" in snap["note"]


async def test_a_real_usage_limit_still_backs_off(tmp_path):
    usage, _db = _usage(tmp_path)
    assert note_subscription_failure(usage, BINFO, USAGE_LIMIT) == "exhausted"
    assert (await usage.snapshot("http://m"))["available"] is False


# -- the paths that call it --------------------------------------------------------

async def test_the_dispatch_path_does_not_spend_the_window_on_an_overload(tmp_path):
    """The brain's dispatch worker, end to end, with the live 503."""
    from foundry_router.brain.agent import AgentRunner
    from foundry_router.config import AgentBrainConfig, GuardrailsConfig
    from foundry_router.guardrails import GuardrailEngine
    from foundry_router.registry.models_db import ModelRegistry
    from foundry_router.tools.mcp_client import MCPManager
    from foundry_router.tools.sync import ToolRegistry

    class OverloadedPool:
        def available_models(self):
            return {"claude-sonnet-5-5": ["meridian"]}

        def backend_info(self, m):
            return {**BINFO, "api_key": "k"}

        async def chat(self, *a, **k):
            raise AllBackendsFailed(LIVE_503)

    class DummyBrain:
        cfg = AgentBrainConfig()

        async def complete(self, prompt):
            return ""

    db = Database(tmp_path / "d.sqlite")
    registry = ModelRegistry(db)
    usage = MeridianUsage(MeridianConfig(), NoSignalHTTP(), db)
    runner = AgentRunner(DummyBrain(), OverloadedPool(),
                         ToolRegistry(db, registry, MCPManager([], db)),
                         registry, GuardrailEngine(GuardrailsConfig(), db, usage), usage)
    with pytest.raises(AllBackendsFailed):
        await runner._dispatch_worker("claude-sonnet-5-5", "hello")
    assert (await usage.snapshot("http://m"))["available"] is True


def test_every_path_goes_through_the_one_classifier():
    """Three callers matched the text themselves; none may again."""
    import inspect

    from foundry_router.brain import agent
    from foundry_router.facade import ollama_api
    for mod in (agent, ollama_api):
        src = inspect.getsource(mod)
        assert "looks_like_window_exhaustion(" not in src, mod.__name__
        assert "note_subscription_failure" in src, mod.__name__
    assert "note_observed_exhaustion(" not in inspect.getsource(ollama_api)
    assert "note_observed_exhaustion(" not in inspect.getsource(agent)
