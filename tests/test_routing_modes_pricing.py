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
    # v0.181.119: the schedule is on unless switched off, daily by default
    s = pricing_update.settings(db)
    assert s["auto"] and s["days"] == 1 and s["follow"] and s["next_update"] == ""
    assert pricing_update.due(db)                      # never updated: due now
    pricing_update.save_settings(db, auto=False)
    assert not pricing_update.due(db)
    pricing_update.save_settings(db, auto=True, days=7)
    assert pricing_update.due(db)
    db.kv_set(pricing_update.KV_LAST, "2999-01-01T00:00:00+00:00")
    assert not pricing_update.due(db)
    assert pricing_update.settings(db)["next_update"].startswith("2999-01-08")
    assert pricing_update.save_settings(db, follow=False)["follow"] is False


# -- following a model family to its newest version (v0.181.119) --------------------------

def _entry(mid, name, created, pin, pout, cache=None):
    p = {"prompt": str(pin / 1e6), "completion": str(pout / 1e6)}
    if cache is not None:
        p["input_cache_read"] = str(cache / 1e6)
    return {"id": mid, "name": name, "created": created, "pricing": p}


FAMILY_CATALOG = {"data": [
    _entry("anthropic/claude-opus-4.8", "Anthropic: Claude Opus 4.8", 100, 5, 25, 0.5),
    _entry("anthropic/claude-opus-5", "Anthropic: Claude Opus 5", 200, 5, 25, 0.5),
    _entry("anthropic/claude-opus-5.5", "Anthropic: Claude Opus 5.5", 300, 4, 20, 0.4),
    _entry("anthropic/claude-opus-5.5:batch", "Anthropic: Claude Opus 5.5 (batch)", 300, 2, 10),
    _entry("anthropic/claude-haiku-4.5", "Anthropic: Claude Haiku 4.5", 90, 1, 5, 0.1),
    _entry("anthropic/claude-haiku-5.5", "Anthropic: Claude Haiku 5.5", 310, 0.1, 0.5, 0.01),
    _entry("openai/gpt-5.6-sol", "OpenAI: GPT-5.6 Sol", 150, 4, 20, 0.4),
    _entry("openai/gpt-6-sol", "OpenAI: GPT-6 Sol", 250, 2, 10, 0.2),
    _entry("openai/gpt-6.1-sol", "OpenAI: GPT-6.1 Sol", 260, 2, 10, 0.2),
    _entry("openai/gpt-6.1-sol-pro", "OpenAI: GPT-6.1 Sol Pro", 270, 2, 10, 0.2),
    _entry("x-ai/grok-4.20", "SpaceXAI: Grok 4.20", 50, 1.25, 2.5),
    _entry("x-ai/grok-4.6", "SpaceXAI: Grok 4.6", 180, 2, 6, 0.5),
    _entry("x-ai/grok-4.7", "SpaceXAI: Grok 4.7", 280, 2, 6, 0.5),
    _entry("deepseek/deepseek-v4.1-flash", "DeepSeek: DeepSeek V4.1 Flash", 240, 0.05, 0.5, 0.006),
    _entry("deepseek/deepseek-v4.1-flash-1017", "DeepSeek: DeepSeek V4.1 Flash 1017", 290, 0.006, 0.5),
    _entry("google/gemini-3.1-pro-preview", "Google: Gemini 3.1 Pro Preview", 170, 2, 12, 0.2),
    _entry("google/gemini-4-carbon", "Google: Gemini 4 Carbon", 320, 2, 10, 0.2),
]}


def _family_client():
    return httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=FAMILY_CATALOG)))


def _rows(db, *rows):
    pricing.ensure_seed(db)
    db.execute("DELETE FROM service_pricing")
    for name, sid, pin, pout, locked in rows:
        db.execute("INSERT INTO service_pricing (name, input_per_1m, output_per_1m, "
                   "cached_input_per_1m, enabled, updated_at, locked, source, source_id) "
                   "VALUES (?,?,?,?,1,'2026-09-01T00:00:00+00:00',?,'openrouter',?)",
                   (name, pin, pout, 0.1, locked, sid))


def test_family_of_and_newest():
    f = pricing_update.family_of
    assert f("anthropic/claude-opus-4.8") == f("anthropic/claude-opus-5.5") == "anthropic/claude-opus"
    assert f("openai/gpt-5.6-sol") == f("openai/gpt-6.1-sol") == "openai/gpt-sol"
    assert f("openai/gpt-6.1-sol-pro") == "openai/gpt-sol-pro"            # its own family
    assert f("deepseek/deepseek-v4.1-flash") == "deepseek/deepseek-flash"
    assert f("x-ai/grok-4.20") == f("x-ai/grok-4.7") == "x-ai/grok"
    # variants of another id are not members of any family
    assert f("openai/gpt-6-sol:batch") is None
    assert f("deepseek/deepseek-v4.1-flash-1017") is None
    assert f("openai/gpt-4o-2024-11-20") is None and f("") is None and f("gpt-5") is None
    cat = [{"id": m["id"], "name": m["name"], "created": m["created"]}
           for m in FAMILY_CATALOG["data"]]
    newest = lambda mid: (pricing_update.newest_in_family(mid, cat) or {}).get("id")
    assert newest("anthropic/claude-opus-4.8") == "anthropic/claude-opus-5.5"
    assert newest("anthropic/claude-opus-5.5") is None                    # already the newest
    assert newest("openai/gpt-5.6-sol") == "openai/gpt-6.1-sol"
    # by release date, not by reading 4.20 as a bigger number than 4.7
    assert newest("x-ai/grok-4.6") == "x-ai/grok-4.7"
    assert newest("deepseek/deepseek-v4.1-flash") is None                 # a snapshot is no successor
    assert newest("google/gemini-3.1-pro-preview") is None                # renamed family: not followed
    assert newest("anthropic/claude-opus-3") == "anthropic/claude-opus-5.5"   # gone from the feed
    assert pricing_update.display_name({"name": "Anthropic: Claude Opus 5.5"}) == "Claude Opus 5.5"
    assert pricing_update.display_name({"id": "x/y"}) == "x/y"


async def test_a_row_follows_its_family_to_the_newest_version(tmp_path):
    db = _db(tmp_path)
    _rows(db, ("Claude Opus 4.8", "anthropic/claude-opus-4.8", 5.0, 25.0, 0),
          ("Claude Haiku 4.5", "anthropic/claude-haiku-4.5", 1.0, 5.0, 0),
          ("GPT-5.6", "openai/gpt-5.6-sol", 4.0, 20.0, 0),
          ("GPT-5.6 Sol", "openai/gpt-5.6-sol", 4.0, 20.0, 0),
          ("Grok 4.6", "x-ai/grok-4.6", 2.0, 6.0, 1),                     # locked
          ("Gemini 3.1 Pro", "google/gemini-3.1-pro-preview", 2.0, 12.0, 0))
    preview = await pricing_update.update_prices(db, _family_client(), dry_run=True)
    assert len(preview["replaced"]) == 3
    assert {r["name"] for r in pricing.list_services(db)} >= {"Claude Opus 4.8"}   # nothing saved
    rep = await pricing_update.update_prices(db, _family_client())
    moved = {x["from"]: x["to"] for x in rep["replaced"]}
    # two rows were matched to one model: the first takes the new name, the second stays
    assert moved == {"Claude Opus 4.8": "Claude Opus 5.5", "Claude Haiku 4.5": "Claude Haiku 5.5",
                     "GPT-5.6": "GPT-6.1 Sol"}
    rows = {r["name"]: r for r in pricing.list_services(db)}
    assert set(rows) == {"Claude Opus 5.5", "Claude Haiku 5.5", "GPT-6.1 Sol", "GPT-5.6 Sol",
                         "Grok 4.6", "Gemini 3.1 Pro"}
    assert rows["Claude Opus 5.5"]["source_id"] == "anthropic/claude-opus-5.5"
    assert (rows["Claude Opus 5.5"]["input_per_1m"], rows["Claude Opus 5.5"]["output_per_1m"]) == (4.0, 20.0)
    # a different model may honestly be 10x cheaper: not held back as suspicious
    assert rows["Claude Haiku 5.5"]["input_per_1m"] == 0.1 and not rep["suspicious"]
    assert rows["GPT-5.6 Sol"]["source_id"] == "openai/gpt-5.6-sol"       # kept its own model
    assert rows["Grok 4.6"]["source_id"] == "x-ai/grok-4.6" and "Grok 4.6" in rep["locked"]
    assert next(u for u in rep["updated"] if u["name"] == "Claude Opus 5.5")["was"] == "Claude Opus 4.8"
    assert pricing_update.settings(db)["last_report"]["replaced"] == 3
    ev = db.query("SELECT message, detail FROM event_log WHERE source='pricing' AND "
                  "message LIKE '%newer version%'")
    assert ev and "Claude Opus 4.8 → Claude Opus 5.5" in ev[0]["detail"]
    # a second run has nothing left to move
    rep2 = await pricing_update.update_prices(db, _family_client())
    assert rep2["replaced"] == [] and rep2["updated"] == []


async def test_newly_released_families_join_the_list(tmp_path):
    import time
    now = int(time.time())
    day = 86400
    cat = {"data": [
        _entry("anthropic/claude-opus-5.5", "Anthropic: Claude Opus 5.5", now - 200 * day, 4, 20),
        _entry("anthropic/claude-fable-5.1", "Anthropic: Claude Fable 5.1", now - 60 * day, 10, 50),
        _entry("anthropic/claude-fable-5.2", "Anthropic: Claude Fable 5.2", now - 2 * day, 8, 40),
        _entry("anthropic/claude-fable-5.2:batch", "Anthropic: Claude Fable 5.2 (batch)", now, 4, 20),
        _entry("openai/gpt-6.1-sol", "OpenAI: GPT-6.1 Sol", now - 40 * day, 2, 10),
        _entry("openai/gpt-6.1-sol-pro", "OpenAI: GPT-6.1 Sol Pro", now - 39 * day, 2, 10),
        _entry("openai/gpt-6-astra", "OpenAI: GPT-6 Astra", now - 70 * day, 10, 50),
        _entry("openai/gpt-6.1-terra", "OpenAI: GPT-6.1 Terra", now - 10 * day, 1, 5),
        _entry("openai/gpt-6-luna", "OpenAI: GPT-6 Luna", now - 50 * day, 0.1, 0.5),
        _entry("openai/gpt-5.4-image-2", "OpenAI: GPT-5.4 Image 2", now - 5 * day, 8, 15),
        _entry("openai/gpt-4.1", "OpenAI: GPT-4.1", now - 500 * day, 2, 8),       # too old
        _entry("google/gemini-4-carbon", "Google: Gemini 4 Carbon", now - day, 2, 10),  # no Google row
    ]}
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=cat)))
    db = _db(tmp_path)
    _rows(db, ("Claude Opus 5.5", "anthropic/claude-opus-5.5", 4.0, 20.0, 0),
          ("GPT-6.1 Sol", "openai/gpt-6.1-sol", 2.0, 10.0, 1))                 # locked still counts
    preview = await pricing_update.update_prices(db, client, dry_run=True)
    assert {a["name"] for a in preview["added"]} == {"Claude Fable 5.2", "GPT-6.1 Terra",
                                                    "GPT-6 Luna"}
    assert len(pricing.list_services(db)) == 2                                # nothing saved
    rep = await pricing_update.update_prices(db, client)
    # the two newest unlisted families per vendor; no Pro twin, no image model, nothing old,
    # no vendor that isn't in the table
    assert {a["name"] for a in rep["added"]} == {"Claude Fable 5.2", "GPT-6.1 Terra", "GPT-6 Luna"}
    rows = {r["name"]: r for r in pricing.list_services(db)}
    assert rows["Claude Fable 5.2"]["source_id"] == "anthropic/claude-fable-5.2"
    assert rows["Claude Fable 5.2"]["input_per_1m"] == 8.0
    assert "added automatically" in rows["GPT-6.1 Terra"]["notes"]
    # the next run adds the one that didn't fit the per-vendor limit, then nothing more
    rep2 = await pricing_update.update_prices(db, client)
    assert [a["name"] for a in rep2["added"]] == ["GPT-6 Astra"]
    assert (await pricing_update.update_prices(db, client))["added"] == []
    assert pricing_update.settings(db)["last_report"]["added"] == 0
    # a deleted row is remembered and stays gone
    pricing.delete_service(db, rows["Claude Fable 5.2"]["id"])
    assert "anthropic/claude-fable" in pricing_update.dismissed(db)
    assert (await pricing_update.update_prices(db, client))["added"] == []
    # switched off: no rows are added
    db2 = Database(tmp_path / "p2.sqlite")
    _rows(db2, ("Claude Opus 5.5", "anthropic/claude-opus-5.5", 4.0, 20.0, 0))
    assert pricing_update.save_settings(db2, add_new=False)["add_new"] is False
    assert (await pricing_update.update_prices(db2, client))["added"] == []


async def test_following_can_be_switched_off(tmp_path):
    db = _db(tmp_path)
    _rows(db, ("Claude Opus 4.8", "anthropic/claude-opus-4.8", 9.0, 9.0, 0))
    pricing_update.save_settings(db, follow=False)
    rep = await pricing_update.update_prices(db, _family_client())
    assert rep["replaced"] == []
    row = pricing.list_services(db)[0]
    assert row["name"] == "Claude Opus 4.8" and row["input_per_1m"] == 5.0   # its own price only


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
