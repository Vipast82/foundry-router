"""Usage-cap headroom (Workstream A): the 95% cap must hold under lag and
concurrency, raw model names go through the same gate as personas, unreadable
usage can fail closed, per-backend cutoffs resolve in one place, and a budget
denial moves on to the next route instead of stopping.
"""

import asyncio
import json

import pytest

from tests.test_cliproxyapi import (KEY, MGMT, SEEN, USAGE, codex_account,  # noqa: F401
                                    cpa)
from tests import test_strict_routing as sr
from tests.test_strict_routing import strict  # noqa: F401 (fixture)


@pytest.fixture()
def su(cpa):
    """The live subscription_usage config, restored after the test."""
    cfg = cpa.config_store.config.subscription_usage
    saved = cfg.model_dump()
    yield cfg
    for k, v in saved.items():
        setattr(cfg, k, v)


def _status(svc, model="gpt-5.5", reserve=False, info=None):
    info = info or svc.pool.backend_info(model)
    return asyncio.run(svc.cliproxy_usage.model_status(model, info, reserve=reserve))


def _chat(client, model, stream=False, path="/api/chat"):
    return client.post(path, json={"model": model, "stream": stream, "messages": [
        {"role": "user", "content": "hi"}]})


# --------------------------------------------------------------------------- #
# the 97% reading past a 95% cap                                              #
# --------------------------------------------------------------------------- #

def test_97_percent_single_account_denied_at_95(cpa, su):
    su.limit_pct = 95
    USAGE["files"] = [codex_account(97, 93)]
    st = _status(cpa)
    assert not st["available"] and "5-hour at 97% (limit 95%)" in st["note"]


def test_multi_account_available_only_with_a_known_reading_below_cutoff(cpa, su):
    su.limit_pct = 95
    empty = {"provider": "codex", "label": "new", "quota": {"signals": {}}}
    USAGE["files"] = [codex_account(97, 93, label="a"), empty]
    assert not _status(cpa)["available"]                       # unknown is not capacity
    cpa.cliproxy_usage._cache.clear()
    USAGE["files"] = [codex_account(97, 93, label="a"), codex_account(10, 5, label="b")]
    assert _status(cpa)["available"]                           # a known account has room
    su.fail_closed = True
    cpa.cliproxy_usage._cache.clear()
    USAGE["files"] = [codex_account(97, 93, label="a"), empty]
    assert not _status(cpa)["available"]
    cpa.cliproxy_usage._cache.clear()
    USAGE["files"] = [codex_account(97, 93, label="a"), codex_account(10, 5, label="b"), empty]
    assert _status(cpa)["available"]


def test_unreadable_usage_is_open_by_default_and_closed_with_fail_closed(cpa, su):
    bad = {**cpa.pool.backend_info("gpt-5.5"), "management_key": "nope"}      # HTTP 401
    st = _status(cpa, info=bad)
    assert st["available"] and "usage unavailable" in st["note"]
    USAGE["files"] = []                                        # signed in: nobody
    assert _status(cpa)["available"]
    cpa.cliproxy_usage._cache.clear()
    USAGE["files"] = [{"provider": "codex", "label": "new", "quota": {"signals": {}}}]
    assert _status(cpa)["available"]                           # no reading yet
    su.fail_closed = True
    cpa.cliproxy_usage._cache.clear()
    st = _status(cpa, info=bad)
    assert not st["available"] and "fail_closed" in st["note"]
    assert not _status(cpa)["available"]                       # no reading yet -> refused
    cpa.cliproxy_usage._cache.clear()
    USAGE["files"] = []
    assert not _status(cpa)["available"]                       # no account at all
    cpa.cliproxy_usage._cache.clear()
    USAGE["files"] = [codex_account(10, 10)]
    assert _status(cpa)["available"]                           # readable and under -> fine


def test_near_the_cutoff_bypasses_the_cache(cpa, su):
    su.limit_pct = 95
    USAGE["files"] = [codex_account(40, 10)]
    assert _status(cpa)["available"]                           # cached: 40%
    USAGE["files"] = [codex_account(96, 10)]
    assert _status(cpa)["available"]                           # far from the cutoff: cache holds
    cpa.cliproxy_usage._cache.clear()
    USAGE["files"] = [codex_account(91, 10)]                   # within reserve_pct + 2 of 95
    assert _status(cpa)["available"]
    USAGE["files"] = [codex_account(96, 10)]
    assert not _status(cpa)["available"]                       # re-read, not the cached 91
    su.reserve_pct = 0.0                                       # window shrinks to 2 points
    cpa.cliproxy_usage._cache.clear()
    USAGE["files"] = [codex_account(91, 10)]
    assert _status(cpa)["available"]
    USAGE["files"] = [codex_account(96, 10)]
    assert _status(cpa)["available"]                           # 91 < 95 - 2: cache holds


def test_in_flight_calls_count_against_the_cutoff(cpa, su):
    su.limit_pct = 95
    USAGE["files"] = [codex_account(94.2, 10, age_s=60)]
    assert _status(cpa, reserve=True)["available"]             # 94.2
    assert _status(cpa, reserve=True)["available"]             # 94.2 + 0.5 in flight
    st = _status(cpa, reserve=True)                            # 94.2 + 2 * 0.5 >= 95
    assert not st["available"] and "2 call(s) in flight" in st["note"]
    assert st["worst"] == 94.2 and st["inflight"] == 2
    # a newer reading means a call finished: its reservation is retired
    USAGE["files"] = [codex_account(94.2, 10, age_s=5)]
    st = _status(cpa)
    assert st["available"] and st["inflight"] == 1
    # a probe (reserve=False) leaves no reservation
    cpa.cliproxy_usage._inflight.clear()
    for _ in range(5):
        assert _status(cpa)["available"]
    # inflight_pct 0 turns the count off
    su.inflight_pct = 0.0
    for _ in range(5):
        assert _status(cpa, reserve=True)["available"]


def test_reservations_expire(cpa, su):
    su.limit_pct = 95
    su.inflight_ttl_seconds = 0.0
    USAGE["files"] = [codex_account(94.2, 10)]
    for _ in range(4):
        assert _status(cpa, reserve=True)["available"]         # each reservation is already stale


def test_check_paid_call_reserves_and_names_the_window(cpa, su):
    from foundry_router.guardrails import RequestGuardState
    su.limit_pct = 95
    info = cpa.pool.backend_info("gpt-5.5")
    eff = cpa.guardrails.effective(None)

    def check(**kw):
        return asyncio.run(cpa.guardrails.check_paid_call(
            "gpt-5.5", info, cpa.registry.get("gpt-5.5"), RequestGuardState(), eff, **kw))
    USAGE["files"] = [codex_account(97, 93)]
    v = check()
    assert not v.allowed and "5-hour at 97% (limit 95%)" in v.reason
    cpa.cliproxy_usage._cache.clear()
    USAGE["files"] = [codex_account(94.4, 93)]
    assert check().allowed and check().allowed                 # 94.4, then 94.9
    assert not check().allowed                                 # 94.4 + 1.0 in flight
    cpa.cliproxy_usage._inflight.clear()
    assert all(check(reserve=False).allowed for _ in range(3))  # probes: nothing reserved


# --------------------------------------------------------------------------- #
# raw model names (passthrough)                                               #
# --------------------------------------------------------------------------- #

def test_passthrough_raw_gpt_name_is_gated_like_a_persona(cpa, client, su):
    su.limit_pct = 95
    USAGE["files"] = [codex_account(97, 93)]
    for stream in (False, True):
        r = _chat(client, "gpt-5.5", stream)
        assert r.status_code == 429, r.text
        body = r.json()
        assert body["code"] == "budget_denied" and body["category"]
        assert "5-hour at 97% (limit 95%)" in body["error"]
    r = _chat(client, "gpt-5.5", path="/v1/chat/completions")
    assert r.status_code == 429 and r.json()["error"]["code"] == "budget_denied"
    assert not SEEN.get("bodies")                              # the backend never saw a request
    # the local model is unaffected by the cloud window
    r = _chat(client, "qwen-local")
    assert r.status_code == 200 and r.json()["message"]["content"] == "local"
    # under the cutoff the raw name is served
    cpa.cliproxy_usage._cache.clear()
    USAGE["files"] = [codex_account(20, 10)]
    r = _chat(client, "gpt-5.5")
    assert r.status_code == 200 and SEEN["bodies"]


def test_passthrough_denial_never_reaches_the_pool(cpa, client, su):
    su.limit_pct = 95
    USAGE["files"] = [codex_account(97, 93)]
    calls = []

    async def boom(*a, **k):
        calls.append(a)
        raise AssertionError("pool.chat must not be called")
    real = cpa.pool.chat, cpa.pool.chat_stream
    cpa.pool.chat = cpa.pool.chat_stream = boom
    try:
        assert _chat(client, "gpt-5.5").status_code == 429
        assert _chat(client, "gpt-5.5", stream=True).status_code == 429
    finally:
        cpa.pool.chat, cpa.pool.chat_stream = real
    assert not calls


def test_passthrough_counts_in_flight_calls(cpa, client, su):
    su.limit_pct = 95
    USAGE["files"] = [codex_account(94.2, 10)]
    codes = [_chat(client, "gpt-5.5").status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and codes[-1] == 429        # 94.2 + n * 0.5 reaches 95


# --------------------------------------------------------------------------- #
# cutoffs: one resolver, Meridian included                                    #
# --------------------------------------------------------------------------- #

def _meridian_check(worst, fable=None):
    from foundry_router.config import GuardrailsConfig, MeridianConfig
    from foundry_router.guardrails import GuardrailEngine, RequestGuardState

    class Fake:
        cfg = MeridianConfig()

        async def snapshot(self, url, key=None):
            return {"available": True, "worst_used": worst, "fable_used": fable, "note": "fake"}
    eng = GuardrailEngine(GuardrailsConfig(), None, Fake())

    def check(model, limit=None):
        info = {"name": "claude", "type": "anthropic-compatible", "url": "http://m",
                "subscription": True, "usage_limit_pct": limit}
        return asyncio.run(eng.check_paid_call(model, info, None, RequestGuardState(),
                                               eng.effective(None)))
    return check


def test_meridian_cutoff_override_replaces_the_conserve_ladder():
    check = _meridian_check(0.8)
    assert not check("claude-opus-4-8").allowed                # legacy: 80% >= conserve_premium 70%
    assert check("claude-sonnet-5").allowed                    # legacy: below conserve_strong 85%
    assert check("claude-opus-4-8", limit=90).allowed          # one cutoff: 80% < 90%
    v = check("claude-haiku-5", limit=75)                      # every tier stops at the cutoff
    assert not v.allowed and "cutoff 75%" in v.reason
    assert not check("claude-opus-4-8", limit=80).allowed      # at the cutoff
    f = _meridian_check(0.1, fable=0.85)
    assert not f("claude-fable-5").allowed                     # legacy Fable bucket: 80%
    assert f("claude-fable-5", limit=90).allowed
    assert not f("claude-fable-5", limit=85).allowed


def test_effective_cutoff_resolver(app):
    from foundry_router.config import BackendConfig
    from foundry_router.guardrails import effective_cutoff
    svc = app.state.services
    cfg = svc.config_store.config.model_copy(deep=True)
    cfg.backend_pool.internal.backends = [
        BackendConfig(name="m", type="anthropic-compatible", url="http://m"),
        BackendConfig(name="m2", type="anthropic-compatible", url="http://m2", usage_limit_pct=92),
        BackendConfig(name="g", type="openai-compatible", url="http://g", flavor="cliproxyapi"),
        BackendConfig(name="g2", type="openai-compatible", url="http://g2", flavor="cliproxyapi",
                      usage_limit_pct=80)]
    cfg.subscription_usage.limit_pct = 95
    cfg.meridian.conserve_strong_at = 0.85
    assert effective_cutoff(cfg, "m") == 85                    # legacy Meridian ladder
    assert effective_cutoff(cfg, "m2") == 92
    assert effective_cutoff(cfg, "g") == 95                    # global
    assert effective_cutoff(cfg, "g2") == 80                   # per-backend override
    assert effective_cutoff(cfg, "unknown") == 95
    # the engine and the CLIProxyAPI gate agree with it
    assert svc.cliproxy_usage.limit_for({"usage_limit_pct": 80}) == 80


def test_cutoffs_admin_endpoints(app, client):
    svc = app.state.services
    r = client.post("/admin/api/config/backends", json=[
        {"name": "meridian", "type": "anthropic-compatible", "url": "http://mer-host:3456"},
        {"name": "cliproxy", "type": "openai-compatible", "url": "http://cpa-host:8317/v1",
         "api_key": KEY, "flavor": "cliproxyapi", "management_key": MGMT},
        {"name": "llama", "type": "openai-compatible", "url": "http://llm-host",
         "flavor": "llamacpp"}])
    assert r.status_code == 200
    sub0 = svc.config_store.config.subscription_usage.model_dump()
    try:
        d = client.get("/admin/api/config/cutoffs").json()
        rows = {b["name"]: b for b in d["backends"]}
        assert set(rows) == {"meridian", "cliproxy"}           # local backends have no cutoff
        assert rows["meridian"]["kind"] == "claude" and rows["meridian"]["override"] is None
        assert rows["cliproxy"]["cutoff_pct"] == d["gpt"] == sub0["limit_pct"]
        d = client.post("/admin/api/config/cutoffs", json={"claude": 90, "gpt": 95}).json()
        assert d["claude"] == 90 and d["gpt"] == 95
        rows = {b["name"]: b for b in d["backends"]}
        assert rows["meridian"]["override"] == 90 and rows["cliproxy"]["cutoff_pct"] == 95
        assert svc.pool.backends["meridian"].config.usage_limit_pct == 90      # live
        assert svc.cliproxy_usage.cfg.limit_pct == 95
        # one backend's own value wins over the global; null clears
        d = client.post("/admin/api/config/cutoffs", json={"backends": {"cliproxy": 80}}).json()
        assert {b["name"]: b for b in d["backends"]}["cliproxy"]["cutoff_pct"] == 80
        assert svc.pool.backends["cliproxy"].config.usage_limit_pct == 80
        d = client.post("/admin/api/config/cutoffs",
                        json={"claude": None, "backends": {"cliproxy": None}}).json()
        rows = {b["name"]: b for b in d["backends"]}
        assert rows["meridian"]["override"] is None and rows["cliproxy"]["cutoff_pct"] == 95
        assert client.post("/admin/api/config/cutoffs", json={"claude": 0}).status_code == 400
        assert client.post("/admin/api/config/cutoffs", json={"gpt": "x"}).status_code == 400
        assert client.post("/admin/api/config/cutoffs",
                           json={"backends": {"nope": 50}}).status_code == 404
        # the existing endpoint keeps working and carries the new settings
        sub = client.post("/admin/api/config/subscription_usage", json={
            "limit_pct": 85, "reserve_pct": 4, "fail_closed": True,
            "inflight_pct": 1}).json()["subscription_usage"]
        assert sub["reserve_pct"] == 4 and sub["fail_closed"] is True and sub["inflight_pct"] == 1
        assert client.get("/admin/api/subscription-usage").status_code == 200
    finally:
        client.post("/admin/api/config/subscription_usage", json={
            k: sub0[k] for k in ("limit_pct", "reserve_pct", "fail_closed", "inflight_pct")})
        client.post("/admin/api/config/backends", json=[])


# --------------------------------------------------------------------------- #
# a denial re-routes along the strict chain                                   #
# --------------------------------------------------------------------------- #

def _chain(strict_svc, name="Kilo-Chain", allow=("gpt-5.5", "qwen-local")):
    strict_svc.personas.upsert(name, execution_mode="direct", routing="strict",
                               model_allowlist=list(allow), pinned_models=list(allow))
    return name


def test_denial_at_check_time_moves_to_the_next_route(strict, client):
    sr.STATE["gpt_used"] = 96
    r = _chat(client, _chain(strict))
    assert r.status_code == 200 and r.json()["message"]["content"] == "local"
    assert sr.HITS == ["llm-host"]
    # a refreshed reading takes effect at once near the cutoff: back to GPT
    sr.STATE["gpt_used"] = 10
    strict.cliproxy_usage._cache.clear()
    r = _chat(client, "Kilo-Chain")
    assert r.json()["message"]["content"] == "gpt"


def test_denial_at_call_time_moves_to_the_next_route(strict, client, monkeypatch):
    """The gate passed (10% used) but the call itself is refused: the budget
    rule or a provider 429 is a denial too, and local is still in the chain."""
    from foundry_router.budget import BudgetDenied
    sr.STATE["gpt_used"] = 10
    name = _chain(strict)
    real = strict.pool.chat

    async def chat(model, *a, **k):
        if model == "gpt-5.5":
            raise BudgetDenied("5-hour call budget used", "call_budget_5h")
        return await real(model, *a, **k)
    monkeypatch.setattr(strict.pool, "chat", chat)
    r = _chat(client, name)
    assert r.status_code == 200 and r.json()["message"]["content"] == "local"
    row = strict.db.query("SELECT guardrail_events FROM request_log ORDER BY id DESC LIMIT 1")
    assert any("failover" in e and "qwen-local" in e for e in json.loads(row[0]["guardrail_events"]))


def test_provider_429_at_call_time_moves_on_and_streams(strict, client):
    sr.STATE["gpt_used"] = 10
    name = _chain(strict)
    sr.STATE["gpt_429"] = True
    for stream in (False, True):
        sr.HITS.clear()
        r = _chat(client, name, stream)
        assert r.status_code == 200, r.text
        assert "local" in r.text and sr.HITS == ["cpa-host", "llm-host"]


def test_budget_denial_never_stops_the_chain_while_a_local_route_remains(strict, client):
    sr.STATE["gpt_used"] = 99
    name = _chain(strict, "Kilo-LocalLast", ("gpt-5.5", "gpt-5.3-codex-spark", "qwen-local"))
    r = _chat(client, name)
    assert r.status_code == 200 and r.json()["message"]["content"] == "local"
    # only when nothing local remains does a strict chain refuse (429, with the reason)
    name = _chain(strict, "Kilo-GPTOnly", ("gpt-5.5",))
    r = _chat(client, name)
    assert r.status_code == 429 and r.json()["code"] == "budget_denied"
