"""Semantic cache (quality spec Phase 3): configurable embedding source,
sqlite-vec storage with a pure-Python fallback, narrow eligibility (single-turn,
non-agent personas only), per-category TTL policy, visible ⚡ hit badge, and
fail-to-no-cache on embedding outages."""

import json

import pytest

from foundry_router.brain.agent import RequestContext
from foundry_router.config import SemanticCacheConfig
from foundry_router.db import Database
from foundry_router.guardrails import RequestGuardState
from foundry_router.semcache import SemanticCache, cache_badge
from foundry_router.usage import RequestLogger

# Deterministic "embeddings": close paraphrases share a direction, unrelated
# questions are orthogonal — similarity is then exactly controllable.
VECS = {
    "what is a monad": [1.0, 0.0, 0.0],
    "explain what a monad is": [0.999, 0.045, 0.0],   # ~0.999 cosine
    "what's the weather like": [0.0, 1.0, 0.0],
}


class FakeEmbedHTTP:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        if self.fail:
            raise ConnectionError("embed host down")

        class R:
            status_code = 200
            def __init__(self, vec):
                self._vec = vec
            def raise_for_status(self):
                pass
            def json(self):
                return {"embeddings": [self._vec]}
        return R(VECS.get(json["input"], [0.0, 0.0, 1.0]))


def _cache(tmp_path, http=None, **cfg_kwargs):
    cfg = SemanticCacheConfig(enabled=True, embed_url="http://embed:11434",
                              embed_model="nomic-embed-text", **cfg_kwargs)
    db = Database(tmp_path / "c.sqlite")
    return SemanticCache(cfg, http or FakeEmbedHTTP(), db), db


PERSONA = {"virtual_name": "Foundry-Chat", "benchmark_category": "general_chat",
           "preferred_mcp_tools": "[]"}


# -- eligibility ------------------------------------------------------------------

def test_eligibility_rules(tmp_path):
    cache, _ = _cache(tmp_path)
    one_turn = [{"role": "user", "content": "what is a monad"}]
    assert cache.eligibility(PERSONA, one_turn) == (True, "")
    # agent/tool workloads always bypass
    agent_persona = {**PERSONA, "benchmark_category": "agentic"}
    ok, why = cache.eligibility(agent_persona, one_turn)
    assert not ok and "agentic" in why
    tooled = {**PERSONA, "preferred_mcp_tools": json.dumps(["searxng"])}
    ok, why = cache.eligibility(tooled, one_turn)
    assert not ok and "MCP tools" in why
    # multi-turn context changes meaning — bypass
    multi = one_turn + [{"role": "assistant", "content": "…"},
                        {"role": "user", "content": "and in haskell?"}]
    assert not cache.eligibility(PERSONA, multi)[0]
    # images bypass
    img = [{"role": "user", "content": "what is this", "images": ["b64"]}]
    assert not cache.eligibility(PERSONA, img)[0]
    # disabled kills everything
    cache.cfg.enabled = False
    assert not cache.eligibility(PERSONA, one_turn)[0]


# -- store / lookup ---------------------------------------------------------------

async def test_similar_question_hits_and_bumps_counter(tmp_path):
    cache, db = _cache(tmp_path)
    assert await cache.store(PERSONA, "what is a monad", "A monad is a monoid…")
    hit = await cache.lookup(PERSONA, "explain what a monad is")
    assert hit and hit["answer"].startswith("A monad")
    assert hit["similarity"] >= 0.99
    assert db.query_one("SELECT hits FROM semantic_cache")["hits"] == 1


async def test_unrelated_question_misses(tmp_path):
    cache, _ = _cache(tmp_path)
    await cache.store(PERSONA, "what is a monad", "A monad is a monoid…")
    assert await cache.lookup(PERSONA, "what's the weather like") is None


async def test_hits_are_persona_scoped(tmp_path):
    cache, _ = _cache(tmp_path)
    await cache.store(PERSONA, "what is a monad", "A monad is a monoid…")
    other = {**PERSONA, "virtual_name": "Foundry-Coding"}
    assert await cache.lookup(other, "what is a monad") is None


async def test_python_fallback_agrees_with_vec_path(tmp_path):
    cache, _ = _cache(tmp_path)
    await cache.store(PERSONA, "what is a monad", "A monad is a monoid…")
    cache._vec_loaded = False        # force the pure-Python scan
    hit = await cache.lookup(PERSONA, "explain what a monad is")
    assert hit and hit["similarity"] >= 0.99


async def test_expired_entry_is_not_served_and_purges(tmp_path):
    cache, db = _cache(tmp_path)
    await cache.store(PERSONA, "what is a monad", "A monad is a monoid…")
    db.execute("UPDATE semantic_cache SET ts=datetime('now', '-10 days')")
    assert await cache.lookup(PERSONA, "what is a monad") is None
    cache.purge()
    assert db.query("SELECT * FROM semantic_cache") == []


async def test_category_ttl_zero_never_stores(tmp_path):
    cache, db = _cache(tmp_path)
    agentic = {**PERSONA, "benchmark_category": "agentic"}
    assert not await cache.store(agentic, "search for x", "answer")
    assert db.query("SELECT * FROM semantic_cache") == []


async def test_embed_outage_degrades_to_no_cache(tmp_path):
    http = FakeEmbedHTTP(fail=True)
    cache, db = _cache(tmp_path, http=http)
    assert not await cache.store(PERSONA, "what is a monad", "answer")
    assert await cache.lookup(PERSONA, "what is a monad") is None
    # edge-triggered outage alert, once
    events = db.query("SELECT * FROM event_log WHERE source='semcache'")
    assert len(events) == 1 and "unavailable" in events[0]["message"]


async def test_max_entries_evicts_oldest(tmp_path):
    cache, db = _cache(tmp_path, max_entries=2)
    await cache.store(PERSONA, "what is a monad", "a1")
    await cache.store(PERSONA, "what's the weather like", "a2")
    await cache.store(PERSONA, "explain what a monad is", "a3")
    assert db.query_one("SELECT COUNT(*) AS n FROM semantic_cache")["n"] == 2


def test_badge_is_visible_and_readable():
    b = cache_badge(0.97, 7200)
    assert "⚡" in b and "97%" in b and "2h ago" in b


# -- embedding health check / test probe ------------------------------------------

async def test_test_embed_reports_success_and_dimension(tmp_path):
    cache, _ = _cache(tmp_path)
    out = await cache.test_embed()
    assert out["ok"] is True
    assert out["dimension"] == 3            # VECS default vector length
    assert "sqlite_vec" in out and "latency_ms" in out


async def test_test_embed_surfaces_the_real_error(tmp_path):
    # unlike embed(), which swallows failures to degrade to no-cache, the probe
    # must report what actually went wrong
    cache, _ = _cache(tmp_path, http=FakeEmbedHTTP(fail=True))
    out = await cache.test_embed()
    assert out["ok"] is False and out["error"]


async def test_test_embed_requires_both_fields(tmp_path):
    cache, _ = _cache(tmp_path)
    out = await cache.test_embed(url="", model="")
    assert out["ok"] is False and "required" in out["error"]


async def test_test_embed_uses_form_values_before_save(tmp_path):
    # passing overrides tests what's in the form, not the saved (empty) config
    cfg = SemanticCacheConfig(enabled=True, embed_url="", embed_model="")
    cache = SemanticCache(cfg, FakeEmbedHTTP(), Database(tmp_path / "f.sqlite"))
    out = await cache.test_embed(url="http://typed-in-form:11434",
                                model="nomic-embed-text")
    assert out["ok"] is True


def test_test_embed_endpoint(client, app):
    svc = app.state.services
    svc.semcache.http = FakeEmbedHTTP()
    r = client.post("/admin/api/semcache/test",
                    json={"embed_url": "http://e:11434", "embed_model": "nomic"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["dimension"] == 3


# -- facade integration: a hit serves WITHOUT routing ------------------------------

@pytest.mark.anyio
async def test_cache_hit_bypasses_routing_entirely(app):
    """The conftest brain is unreachable, so any real routing degrades to the
    fallback path — a clean cached answer therefore proves no routing ran."""
    from foundry_router.facade.ollama_api import _agent_events_to_chat_chunks
    svc = app.state.services
    svc.semcache = SemanticCache(
        SemanticCacheConfig(enabled=True, embed_url="http://e",
                            embed_model="nomic-embed-text"),
        FakeEmbedHTTP(), svc.db)
    persona = svc.personas.get("Foundry-Chat")
    await svc.semcache.store(persona, "what is a monad", "A monad is a monoid…")

    ctx = RequestContext(
        persona=persona,
        messages=[{"role": "user", "content": "explain what a monad is"}],
        guard=RequestGuardState(),
        logger=RequestLogger(svc.db, "Foundry-Chat", "Foundry-Chat", "agent", "q"))
    chunks = [json.loads(c) async for c in
              _agent_events_to_chat_chunks(svc, ctx, "Foundry-Chat")]
    content = "".join(c["message"]["content"] for c in chunks)
    thinking = "".join(c["message"].get("thinking") or "" for c in chunks)
    assert content.startswith("A monad")
    assert "⚡" in content                       # visible badge
    assert "cache hit" in thinking.lower()      # narrated, not silent
    # logged as its own mode, and no model/backend was ever touched
    row = svc.db.query_one("SELECT * FROM request_log ORDER BY id DESC LIMIT 1")
    assert row["mode"] == "cache" and row["status"] == "ok"
    assert json.loads(row["models_used"]) == []
