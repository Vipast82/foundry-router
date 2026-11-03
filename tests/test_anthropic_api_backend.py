"""Backend type "anthropic-api" (2026-11-03): Claude straight from Anthropic with an API
key. The owner moved Claude work off Meridian onto API credit; through CLIProxyAPI an API
key only ever got the 5-minute cache (its 1-hour upgrade is for Claude Code sign-ins), and
the router counted the calls as a free subscription, so no spend cap applied."""
import asyncio
import json

import httpx
import pytest

from foundry_router import budget, thinking
from foundry_router.config import BackendConfig
from foundry_router.pool.protocols import AnthropicAPIProtocol, make_protocol
from foundry_router.usage import anthropic_api_cost, estimate_cost_usd


def _proto(seen, ttl="1h", response=None):
    def handler(req):
        seen.append({"headers": dict(req.headers), "body": json.loads(req.content or b"{}")})
        return httpx.Response(200, json=response or {
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "cache_read_input_tokens": 150000,
                      "cache_creation_input_tokens": 1000, "output_tokens": 300}})
    p = make_protocol("anthropic-api", "https://api.anthropic.com", "sk-ant-test",
                      httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    p.cache_ttl = ttl
    return p


def test_the_request_is_what_the_current_api_accepts_with_router_placed_caching():
    seen = []
    p = _proto(seen)
    assert isinstance(p, AnthropicAPIProtocol)
    tools = [{"type": "function", "function": {"name": "edit", "description": "d",
                                               "parameters": {"type": "object"}}}]
    res = asyncio.run(p.chat("claude-sonnet-5-5",
                             [{"role": "system", "content": "rules"},
                              {"role": "user", "content": "hi"}], tools=tools,
                             options={"temperature": 0.2, "top_p": 0.9, "tool_choice": "required"},
                             think="high"))
    body, headers = seen[0]["body"], seen[0]["headers"]
    assert headers["x-api-key"] == "sk-ant-test" and "authorization" not in headers
    assert not any(k.startswith("x-meridian") for k in headers)
    assert body["system"] == [{"type": "text", "text": "rules",
                               "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    assert body["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert body["thinking"] == {"type": "adaptive"} and body["output_config"]["effort"] == "high"
    assert "temperature" not in body and "top_p" not in body
    assert body.get("tool_choice", {}).get("type") in (None, "auto")
    assert res.prompt_tokens == 151005 and res.cached_tokens == 150000
    # the 5-minute cache when the backend asks for it
    seen.clear()
    asyncio.run(_proto(seen, ttl="5m").chat("claude-haiku-5-5", [{"role": "user", "content": "x"}]))
    assert seen[0]["body"]["cache_control"]["ttl"] == "5m" and "system" not in seen[0]["body"]


def test_config_metering_and_menus_treat_it_as_paid_claude_not_a_subscription():
    b = BackendConfig(name="anthropic", type="anthropic-api", url="https://api.anthropic.com")
    assert b.cache_ttl == "1h" and b.is_subscription is False
    assert b.effective_flavor == "anthropic" and budget.is_paid(b)
    assert thinking.supported_levels("claude-sonnet-5-5", backend_type="anthropic-api")
    with pytest.raises(Exception):
        BackendConfig(name="x", type="anthropic-api", url="https://a", cache_ttl="2h")


def test_cost_uses_anthropic_rates_with_cached_tokens():
    # Sonnet 5.5: 156k context, 155k read from cache, 1k written (1h), 400 out
    c = anthropic_api_cost("claude-sonnet-5-5", 156000, 155000, 400, "1h")
    assert abs(c - (1000 * 4.0 + 155000 * 0.10 + 400 * 10.0) / 1e6) < 1e-9
    assert anthropic_api_cost("claude-sonnet-5-5", 1000, 0, 0, "5m") == pytest.approx(0.0025)
    # an unknown model is priced as Opus 5.5 (the conservative side)
    assert anthropic_api_cost("claude-next", 1000, 0, 0) == pytest.approx(1000 * 8.0 / 1e6)
    info = {"type": "anthropic-api", "cache_ttl": "1h"}
    assert estimate_cost_usd(None, 156000, 400, cached_tokens=155000, binfo=info,
                             model="claude-sonnet-5-5") == pytest.approx(c)
    # the registry row (shared with Meridian, free) never decides it
    assert estimate_cost_usd({"cost_per_1k_input": 0.0}, 1000, 0, binfo=info,
                             model="claude-sonnet-5-5") > 0


def test_the_spend_caps_apply_to_it(app):
    from foundry_router.guardrails import RequestGuardState
    svc = app.state.services
    info = {"name": "anthropic", "type": "anthropic-api", "url": "https://api.anthropic.com",
            "subscription": False, "cache_ttl": "1h"}
    eff = {**svc.guardrails.effective(None), "max_paid_calls_per_request": 9}
    v = asyncio.run(svc.guardrails.check_paid_call("claude-sonnet-5-5", info, {},
                                                   RequestGuardState(),
                                                   {**eff, "daily_spend_cap_usd": None}))
    assert v.allowed
    svc.db.execute("INSERT INTO request_log (ts, est_cost_usd) VALUES (datetime('now'), 3.5)")
    v = asyncio.run(svc.guardrails.check_paid_call("claude-sonnet-5-5", info, {},
                                                   RequestGuardState(),
                                                   {**eff, "daily_spend_cap_usd": 3.0}))
    assert not v.allowed and "daily spend cap" in v.reason
