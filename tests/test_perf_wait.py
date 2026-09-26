"""Server-wait visibility (TTFT minus server prompt time) and lazy failover."""

import json

from foundry_router import perf_history


def _sample(db, ttft, prefill, wall, src="server"):
    db.execute(
        "INSERT INTO perf_samples (ts, model, backend, persona, mode, prompt_tokens, "
        "completion_tokens, decode_tps, prefill_tps, decode_ms, prefill_ms, ttft_ms, "
        "wall_ms, finish_reason, timing_src) VALUES "
        "(strftime('%Y-%m-%dT%H:%M:%fZ','now'),'qwen','llama','act','direct',100000,300,"
        "51,300,6000,?,?,?,'tool_calls',?)", (prefill, ttft, wall, src))


def test_constant_server_wait_is_diagnosed(app):
    db = app.state.services.db
    for _ in range(8):                       # the live pattern: ~53s idle, 2s prefill
        _sample(db, ttft=55000, prefill=2000, wall=61000)
    d = perf_history.query_history(db, hours=1)
    g = d["by_model"][0]
    assert 52000 <= g["p50_wait_ms"] <= 54000
    assert d["diagnoses"] and "--cache-ram" in d["diagnoses"][0]["message"]
    assert d["diagnoses"][0]["share_pct"] >= 80
    assert all(r["wait_ms"] == 53000 for r in d["series"])


def test_no_diagnosis_when_wait_is_small_or_estimated(app):
    db = app.state.services.db
    for _ in range(8):
        _sample(db, ttft=2500, prefill=2000, wall=9000)
    for _ in range(8):
        _sample(db, ttft=60000, prefill=0, wall=62000, src="estimated")
    d = perf_history.query_history(db, hours=1)
    assert not d["diagnoses"]


def test_failover_list_not_built_when_first_model_works(app, client):
    from foundry_router.pool.protocols import ChatResult
    import foundry_router.facade.ollama_api as oa
    svc = app.state.services
    calls = []

    async def spy(*a, **k):
        calls.append(1)
        return ["qwen"]
    orig, oa._failover_list = oa._failover_list, spy

    class P:
        def backend_info(self, m):
            return {"name": "b", "type": "openai-compatible", "url": "http://b"}

        def available_models(self):
            return {"qwen": ["b"]}

        def active_calls(self):
            return []

        def backend_status(self):
            return []

        async def chat(self, model, messages, **kw):
            return ChatResult(content="ok", completion_tokens=2), "b"
    real, svc.pool = svc.pool, P()
    svc.personas.upsert("Act", execution_mode="direct", model_allowlist=["qwen"], pinned_models=[])
    try:
        r = client.post("/api/chat", json={"model": "Act", "stream": False,
                                           "messages": [{"role": "user", "content": "x"}]})
    finally:
        svc.pool = real
        oa._failover_list = orig
    assert r.json()["message"]["content"] == "ok"
    assert calls == []                        # no pre-dispatch guardrail lookups
