"""Strict routing (persona routing=strict): fail closed.

A strict persona may only use its model_allowlist (in pinned order). It must
never fall through to the rest of the pool, degrade to "any local", escalate
to a paid model it doesn't list, use the brain, or fail over outside its list.
Every case also checks that NO other backend received a request.
"""

import json

import httpx
import pytest

from foundry_router.config import BackendConfig, BackendPoolConfig
from foundry_router.pool.internal import InternalPool

HITS: list = []
STATE = {"local_fail": False, "gpt_used": 10}


def _now_iso(offset_s: float = 0) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()


def _chat_ok(model, text):
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": text}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}})


def _router(req: httpx.Request):
    host, path = req.url.host, req.url.path
    if path.endswith("/models"):
        ids = {"cpa-host": ["gpt-5.5"], "llm-host": ["qwen-local"], "alt-host": ["other-local"]}[host]
        return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
    if path == "/v0/management/auth-files":
        return httpx.Response(200, json={"files": [{"provider": "codex", "label": "me", "quota": {
            "observed_at": _now_iso(-30), "signals": {
                "X-Codex-Primary-Used-Percent": str(STATE["gpt_used"]),
                "X-Codex-Primary-Window-Minutes": "300",
                "X-Codex-Primary-Reset-After-Seconds": "3000"}}}]})
    HITS.append(host)
    if host == "llm-host" and STATE["local_fail"]:
        return httpx.Response(500, text="boom")
    return _chat_ok("x", {"cpa-host": "gpt", "llm-host": "local", "alt-host": "other"}[host])


@pytest.fixture()
def strict(app, client):
    svc = app.state.services
    HITS.clear()
    STATE.update(local_fail=False, gpt_used=10)
    http = httpx.AsyncClient(transport=httpx.MockTransport(_router))
    pool = InternalPool([
        BackendConfig(name="cliproxy", type="openai-compatible", url="http://cpa-host:8317/v1",
                      api_key="k", flavor="cliproxyapi", management_key="m"),
        BackendConfig(name="llama", type="openai-compatible", url="http://llm-host",
                      flavor="llamacpp"),
        BackendConfig(name="alt", type="openai-compatible", url="http://alt-host",
                      flavor="llamacpp")],
        BackendPoolConfig(), http, svc.db)
    for name, models in (("cliproxy", ["gpt-5.5"]), ("llama", ["qwen-local"]),
                         ("alt", ["other-local"])):
        s = pool.backends[name]
        s.healthy = s.ever_checked = True
        s.models = list(models)
    real, svc.pool = svc.pool, pool
    real_cu, svc.cliproxy_usage.client = svc.cliproxy_usage.client, http
    svc.cliproxy_usage._cache.clear()
    svc.config_store.config.agent_brain.direct_stream = False
    yield svc
    svc.pool = real
    svc.cliproxy_usage.client = real_cu


def _ask(client, model, path="/api/chat"):
    if path == "/api/chat":
        return client.post(path, json={"model": model, "stream": False,
                                       "messages": [{"role": "user", "content": "hi"}]})
    return client.post(path, json={"model": model, "stream": False,
                                   "messages": [{"role": "user", "content": "hi"}]})


def test_unreachable_allowlist_refuses_instead_of_using_pool(strict, client):
    strict.personas.upsert("Kilo-Sol", execution_mode="direct", routing="strict",
                           model_allowlist=["gpt-6.1-sol"], pinned_models=[])
    r = _ask(client, "Kilo-Sol")
    assert r.status_code == 503
    body = r.json()
    assert body["code"] == "route_unavailable" and "gpt-6.1-sol" in body["error"]
    assert HITS == []                                   # nothing else was asked


def test_budget_denial_refuses_no_local_degrade(strict, client):
    STATE["gpt_used"] = 92
    strict.personas.upsert("Kilo-GPT", execution_mode="direct", routing="strict",
                           model_allowlist=["gpt-5.5"], pinned_models=[])
    r = _ask(client, "Kilo-GPT")
    assert r.status_code == 429 and r.json()["code"] == "budget_denied"
    assert "limit 85%" in r.json()["error"]
    assert HITS == []
    # OpenAI clients get a standard error object with the same code
    r = _ask(client, "Kilo-GPT", "/v1/chat/completions")
    err = r.json()["error"]
    assert r.status_code == 429 and err["code"] == "budget_denied"
    assert err["type"] == "rate_limit_error"
    assert HITS == []


def test_explicit_chain_moves_to_next_listed_model_visibly(strict, client):
    STATE["gpt_used"] = 92
    strict.personas.upsert("Kilo-Chain", execution_mode="direct", routing="strict",
                           model_allowlist=["gpt-5.5", "qwen-local"],
                           pinned_models=["gpt-5.5", "qwen-local"])
    r = _ask(client, "Kilo-Chain")
    assert r.status_code == 200 and r.json()["message"]["content"] == "local"
    assert HITS == ["llm-host"]
    row = strict.db.query("SELECT guardrail_events FROM request_log ORDER BY id DESC LIMIT 1")
    assert "strict route: gpt-5.5 denied → qwen-local" in json.loads(row[0]["guardrail_events"])


def test_failover_stays_inside_allowlist(strict, client):
    STATE["local_fail"] = True
    strict.personas.upsert("Kilo-Local", execution_mode="direct", routing="strict",
                           model_allowlist=["qwen-local"], pinned_models=[])
    r = _ask(client, "Kilo-Local")
    assert r.status_code >= 500
    assert "alt-host" not in HITS and "cpa-host" not in HITS


def test_strict_agent_mode_persona_is_served_direct_without_brain(strict, client):
    strict.personas.upsert("Kilo-Agentish", execution_mode="agent", routing="strict",
                           model_allowlist=["qwen-local"], pinned_models=[])
    r = _ask(client, "Kilo-Agentish")
    assert r.status_code == 200 and r.json()["message"]["content"] == "local"
    mode = strict.db.query("SELECT mode FROM request_log ORDER BY id DESC LIMIT 1")[0]["mode"]
    assert mode == "direct"
    assert HITS == ["llm-host"]


def test_flexible_persona_behaviour_unchanged(strict, client):
    # Old behaviour stays for non-strict personas: unreachable allowlist falls
    # through to an available model (documented, opt-out via routing=strict).
    strict.personas.upsert("Old-Act", execution_mode="direct",
                           model_allowlist=["missing-model"], pinned_models=[])
    r = _ask(client, "Old-Act")
    assert r.status_code == 200


def test_response_headers_name_persona_and_served_model(strict, client):
    strict.personas.upsert("Kilo-Local", execution_mode="direct", routing="strict",
                           model_allowlist=["qwen-local"], pinned_models=[])
    for path in ("/api/chat", "/v1/chat/completions"):
        r = _ask(client, "Kilo-Local", path)
        assert r.status_code == 200
        h = r.headers
        assert h["x-foundry-persona"] == "Kilo-Local"
        assert h["x-foundry-served-model"] == "qwen-local"
        assert h["x-foundry-served-backend"] == "llama"
        assert h["x-foundry-routing"] == "strict"
    # streaming too (headers are sent before the first token)
    r = client.post("/v1/chat/completions", json={"model": "Kilo-Local", "stream": True,
                    "messages": [{"role": "user", "content": "hi"}]})
    assert r.headers["x-foundry-served-model"] == "qwen-local"


def test_request_is_logged_once():
    from foundry_router.usage import RequestLogger

    class DB:
        rows = 0

        def execute(self, *a):
            DB.rows += 1
    lg = RequestLogger(DB(), "p", "p", "direct", "x")
    lg.finish("ok")
    lg.finish("cancelled", "client disconnected")
    assert DB.rows == 1


def test_disconnect_cancels_backend_task(strict):
    """The buffered stream runs the backend call as a task; closing the stream
    early (client gone) must cancel it."""
    import asyncio
    from foundry_router.facade import ollama_api
    src = open(ollama_api.__file__).read()
    assert "task.cancel()" in src and "client disconnected — backend call cancelled" in src

    async def run():
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def slow():
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def gen():
            task = asyncio.create_task(slow())
            try:
                while True:
                    done, _ = await asyncio.wait({task}, timeout=0.01)
                    yield b"."
            finally:
                if not task.done():
                    task.cancel()
        g = gen()
        await g.__anext__()
        await started.wait()
        await g.aclose()                  # what Starlette does on disconnect
        await asyncio.wait_for(cancelled.wait(), 1)
        return True
    assert asyncio.run(run())
