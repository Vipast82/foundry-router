"""Routing modes (auto / brain / passthrough), brain-down failover, and the
brain-assisted automatic price update for the cost calculator."""

import json

import httpx
import pytest

from foundry_router import pricing, pricing_update
from foundry_router.brain.client import BrainClient, BrainUnreachable
from foundry_router.config import AgentBrainConfig
from foundry_router.db import Database
from foundry_router.pool.base import AllBackendsFailed
from foundry_router.pool.protocols import ChatResult

CATALOG = {"data": [
    {"id": "anthropic/claude-opus-4.8", "name": "Anthropic: Claude Opus 4.8",
     "pricing": {"prompt": "0.000005", "completion": "0.000025", "input_cache_read": "0.0000005"}},
    {"id": "anthropic/claude-opus-4.8:thinking", "name": "Anthropic: Claude Opus 4.8 (thinking)",
     "pricing": {"prompt": "0.000005", "completion": "0.000025"}},
    {"id": "anthropic/claude-sonnet-5", "name": "Anthropic: Claude Sonnet 5",
     "pricing": {"prompt": "0.0000025", "completion": "0.0000125", "input_cache_read": "0.00000025"}},
    {"id": "openai/gpt-5.6", "name": "OpenAI: GPT-5.6",
     "pricing": {"prompt": "0.000002", "completion": "0.000012"}},
    {"id": "google/gemini-3.1-pro", "name": "Google: Gemini 3.1 Pro",
     "pricing": {"prompt": "0.0002", "completion": "0.0012"}},          # 100x jump
    {"id": "meta/llama-free:free", "name": "free", "pricing": {"prompt": "0", "completion": "0"}},
]}


def _client():
    return httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=CATALOG)))


def _db(tmp_path):
    return Database(tmp_path / "p.sqlite")


# -- matching --------------------------------------------------------------------------

async def test_heuristic_match_prefers_plain_variant():
    cat = await pricing_update.fetch_catalog(_client())
    assert all(not c["id"].endswith(":free") for c in cat)
    assert pricing_update.heuristic_match("Claude Opus 4.8", cat) == "anthropic/claude-opus-4.8"
    assert pricing_update.heuristic_match("Claude Sonnet 5", cat) == "anthropic/claude-sonnet-5"
    assert pricing_update.heuristic_match("GPT-5.6", cat) == "openai/gpt-5.6"
    assert pricing_update.heuristic_match("Grok 4.6", cat) is None


class _Brain:
    def __init__(self, answer=None, fail=False):
        self.answer, self.fail, self.prompts = answer, fail, []

    async def complete(self, prompt, aux=False):
        self.prompts.append((prompt, aux))
        if self.fail:
            raise BrainUnreachable("down")
        return json.dumps(self.answer)


async def test_brain_match_only_accepts_real_candidates():
    cat = await pricing_update.fetch_catalog(_client())
    brain = _Brain({"Claude Opus 4.8": "anthropic/claude-opus-4.8",
                    "GPT-5.6": "made-up/model"})
    got = await pricing_update.brain_match(brain, ["Claude Opus 4.8", "GPT-5.6"], cat)
    assert got == {"Claude Opus 4.8": "anthropic/claude-opus-4.8", "GPT-5.6": None}
    assert brain.prompts[0][1] is True                      # background (aux) job


# -- update ----------------------------------------------------------------------------

async def test_update_prices_applies_locks_and_sanity(tmp_path):
    db = _db(tmp_path)
    pricing.ensure_seed(db)
    gpt = next(r for r in pricing.list_services(db) if r["name"] == "GPT-5.6")
    pricing.upsert_service(db, id=gpt["id"], name="GPT-5.6", input_per_1m=9.0,
                           output_per_1m=9.0, locked=True)
    rep = await pricing_update.update_prices(db, _client(), brain=_Brain(fail=True))
    assert rep["matcher"] == "heuristic"                    # brain down -> fallback
    names = {u["name"]: u for u in rep["updated"]}
    assert names["Claude Sonnet 5"]["new"]["input_per_1m"] == 2.5
    assert names["Claude Sonnet 5"]["new"]["cached_input_per_1m"] == 0.25
    assert "GPT-5.6" in rep["locked"]
    assert any(s["name"] == "Gemini 3.1 Pro" for s in rep["suspicious"])   # range or 10x rule
    rows = {r["name"]: r for r in pricing.list_services(db)}
    assert rows["Claude Sonnet 5"]["input_per_1m"] == 2.5
    assert rows["Claude Sonnet 5"]["source"] == "openrouter"
    assert rows["Claude Sonnet 5"]["source_id"] == "anthropic/claude-sonnet-5"
    assert rows["GPT-5.6"]["input_per_1m"] == 9.0           # locked, untouched
    assert rows["Gemini 3.1 Pro"]["input_per_1m"] == 2.0    # suspicious, not applied
    assert "Grok 4.6" in rep["unmatched"]
    # second run: remembered matches, nothing left for the brain
    rep2 = await pricing_update.update_prices(db, _client(), brain=_Brain(fail=True))
    assert any(u["name"] == "Claude Sonnet 5" for u in rep2["unchanged"])
    assert pricing_update.settings(db)["last_report"]["updated"] == 0


async def test_update_prices_dry_run_saves_nothing(tmp_path):
    db = _db(tmp_path)
    rep = await pricing_update.update_prices(db, _client(), dry_run=True)
    assert rep["updated"]
    rows = {r["name"]: r for r in pricing.list_services(db)}
    assert rows["Claude Sonnet 5"]["input_per_1m"] == 3.0
    assert not pricing_update.settings(db)["last_update"]


def test_schedule_due(tmp_path):
    db = _db(tmp_path)
    assert not pricing_update.due(db)
    pricing_update.save_settings(db, auto=True, days=7)
    assert pricing_update.due(db)
    db.kv_set(pricing_update.KV_LAST, "2999-01-01T00:00:00+00:00")
    assert not pricing_update.due(db)


def test_manual_edit_marks_source(tmp_path):
    db = _db(tmp_path)
    pricing.upsert_service(db, name="My vendor", input_per_1m=1, output_per_1m=2, locked=True)
    r = next(r for r in pricing.list_services(db) if r["name"] == "My vendor")
    assert r["source"] == "manual" and r["locked"] == 1


# -- routing-mode gate -----------------------------------------------------------------

def _brain(**kw):
    return BrainClient(AgentBrainConfig(**{"endpoint": "http://127.0.0.1:9",
                                           "model": "b", **kw}), httpx.AsyncClient())


def test_skip_reason_modes():
    assert _brain().skip_reason() is None
    assert "passthrough" in _brain(routing_mode="passthrough").skip_reason()
    assert _brain(routing_mode="passthrough").skip_reason(aux=True) is None
    assert "no brain configured" in _brain(model="").skip_reason()
    b = _brain()
    b.health_down = "connection refused"
    assert "unhealthy" in b.skip_reason()
    b2 = _brain(routing_mode="brain")
    b2.health_down = "connection refused"
    assert b2.skip_reason() is None          # brain mode always tries


async def test_skipped_brain_raises_instantly():
    b = _brain(routing_mode="passthrough")
    with pytest.raises(BrainUnreachable, match="skipped"):
        await b.chat([{"role": "user", "content": "hi"}])


async def test_failed_call_opens_breaker():
    b = _brain()                              # nothing listens on :9
    with pytest.raises(BrainUnreachable):
        await b.chat([{"role": "user", "content": "hi"}])
    assert b.skip_reason() == "brain failed moments ago"


# -- facade behaviour ------------------------------------------------------------------

class TwoModelPool:
    """local-a fails, local-b answers."""
    def __init__(self):
        self.calls = []

    def available_models(self):
        return {"local-a": ["o1"], "local-b": ["o1"]}

    def backend_info(self, m):
        return ({"name": "o1", "type": "ollama", "url": "http://o"}
                if m in ("local-a", "local-b") else None)

    async def loaded_models(self):
        return set()

    def active_calls(self):
        return []

    async def chat(self, model, messages, **kw):
        self.calls.append(model)
        if model == "local-a":
            raise AllBackendsFailed("local-a down")
        return ChatResult(content=f"answer from {model}", prompt_tokens=3,
                          completion_tokens=2, finish_reason="stop"), "o1"

    async def chat_stream(self, model, messages, **kw):
        self.calls.append(model)
        if model == "local-a":
            raise AllBackendsFailed("local-a down")
        yield {"content": f"answer from {model}", "done": False}
        yield ChatResult(prompt_tokens=3, completion_tokens=2, finish_reason="stop").done_frame()


def _persona(svc):
    return next(p for p in svc.personas.list(enabled_only=True)
                if (p.get("execution_mode") or "agent") == "agent"
                and not json.loads(p.get("preferred_mcp_tools") or "[]"))


def test_passthrough_mode_skips_brain_and_serves_directly(app, client):
    svc = app.state.services
    svc.config_store.config.agent_brain.routing_mode = "passthrough"
    svc.brain.cfg.routing_mode = "passthrough"
    pool = TwoModelPool()
    real, svc.pool = svc.pool, pool
    try:
        p = _persona(svc)
        svc.registry.upsert_auto("local-b", source="discovery")
        r = client.post("/api/chat", json={"model": p["virtual_name"], "stream": False,
                                           "messages": [{"role": "user", "content": "hi"}]}).json()
    finally:
        svc.pool = real
    assert r["message"]["content"].startswith("answer from")
    row = svc.db.query("SELECT mode FROM request_log ORDER BY id DESC LIMIT 1")[0]
    assert row["mode"] == "passthrough"


def test_brain_mode_fallback_fails_over_between_models(app, client):
    svc = app.state.services
    svc.brain.cfg.routing_mode = "brain"            # try the (dead) brain, then fall back
    pool = TwoModelPool()
    real, svc.pool = svc.pool, pool
    try:
        p = _persona(svc)
        r = client.post("/api/chat", json={"model": p["virtual_name"],
                                           "messages": [{"role": "user", "content": "hi"}]})
    finally:
        svc.pool = real
    text = "".join(json.loads(l)["message"]["content"] for l in r.text.splitlines() if l.strip())
    thinking = "".join(json.loads(l)["message"].get("thinking", "") for l in r.text.splitlines() if l.strip())
    assert "answer from local-b" in text
    assert pool.calls[-1] == "local-b"
    if pool.calls[0] == "local-a":
        assert "failing over to local-b" in thinking


def test_status_reports_routing_mode(client):
    st = client.get("/admin/api/status").json()["brain"]
    assert st["routing_mode"] == "auto" and "active" in st
    r = client.post("/admin/api/config/brain", json={"routing_mode": "bogus"})
    assert r.status_code == 400


def _run_persona(app, client, *, stream, mode="passthrough", health_down=None, direct_stream=False):
    svc = app.state.services
    svc.brain.cfg.routing_mode = mode
    svc.brain.health_down = health_down
    svc.config_store.config.agent_brain.direct_stream = direct_stream
    pool = TwoModelPool()
    real, svc.pool = svc.pool, pool
    try:
        p = _persona(svc)
        r = client.post("/api/chat", json={"model": p["virtual_name"], "stream": stream,
                                           "messages": [{"role": "user", "content": "hi"}]})
    finally:
        svc.pool = real
    return svc, pool, r


def test_passthrough_fails_over_to_next_model(app, client):
    svc, pool, r = _run_persona(app, client, stream=False)
    assert r.json()["message"]["content"] == "answer from local-b"
    if pool.calls[0] == "local-a":
        ev = svc.db.query("SELECT guardrail_events FROM request_log ORDER BY id DESC LIMIT 1")[0]
        assert "failover" in ev["guardrail_events"]


def test_auto_mode_with_unhealthy_brain_goes_passthrough(app, client):
    svc, pool, r = _run_persona(app, client, stream=False, mode="auto",
                                health_down="connection refused")
    assert r.json()["message"]["content"] == "answer from local-b"
    assert svc.db.query("SELECT mode FROM request_log ORDER BY id DESC LIMIT 1")[0]["mode"] == "passthrough"


def test_live_stream_fails_over_before_output(app, client):
    svc, pool, r = _run_persona(app, client, stream=True, direct_stream=True)
    lines = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    text = "".join(l["message"]["content"] for l in lines)
    assert text == "answer from local-b" and lines[-1]["done"]
    thinking = "".join(l["message"].get("thinking", "") for l in lines)
    assert "passthrough" in thinking
    if pool.calls[0] == "local-a":
        assert "failing over to local-b" in thinking


def test_failover_path_is_actually_exercised(app, client):
    _, pool, _ = _run_persona(app, client, stream=False)
    assert pool.calls == ["local-a", "local-b"]
