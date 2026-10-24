"""Operator chat: personas, backends and the whole router (v0.140.0).

persona_rules (one rule set for the Personas tab and the chat), route_resolve
("local" / "Claude" / "ChatGPT" -> concrete models, strict-backend trap, orphaned
personas), every new chat tool through its confirm card (snapshot taken,
audited, refusals, eval reminder), the router-wide tools (config.yaml, admin
reads and writes with the deny list and irreversible cards), research and
performance tools, and the chat's own model chain (never a persona)."""
import copy
import json

import pytest

from foundry_router import persona_rules, request_context, route_resolve
from foundry_router.pool.protocols import ChatResult
from tests.test_chat_engine import _confirm, call, chat, reply  # noqa: F401
from tests.test_control_center import cc  # noqa: F401

MODELS = {"meridian": ["claude-sonnet-5-5", "claude-opus-5-5"],
          "cliproxyapi": ["gpt-6.1-sol", "gpt-6-astra"],
          "strata": ["qwen3.8-flash-next-iq3_s"]}
BACKENDS = [{"name": "meridian", "type": "anthropic-compatible", "url": "http://m"},
            {"name": "cliproxyapi", "type": "openai-compatible", "url": "http://c"},
            {"name": "strata", "type": "openai-compatible", "url": "http://s"}]


@pytest.fixture()
def router(app, client, cc, monkeypatch):  # noqa: F811
    """Three configured backends (Claude, ChatGPT, local) with fake live model
    lists, the operator clients written to config.yaml (so a save keeps them),
    and two personas: a flexible Foundry-Chat and a strict Claude-tied helper."""
    svc = app.state.services
    clients = [c.model_dump(exclude_none=True) for c in svc.config_store.config.clients]

    def mutate(raw):
        raw["backend_pool"]["internal"]["backends"] = copy.deepcopy(BACKENDS)
        raw["clients"] = clients
    svc.config_store.save(mutate)

    def status(_svc):
        out = []
        for b in svc.config_store.config.backend_pool.internal.backends:
            out.append({"name": b.name, "type": b.type, "flavor": "", "healthy": True,
                        "enabled": b.enabled, "models": list(MODELS.get(b.name, []))})
        return out
    monkeypatch.setattr(route_resolve, "status_with_models", status)

    async def no_rebuild():
        return None
    monkeypatch.setattr(svc, "rebuild_pool", no_rebuild)
    svc.personas.upsert("Foundry-Chat", description="chat", enabled=1,
                        pinned_models=["claude-sonnet-5-5", "gpt-6.1-sol", "qwen3.8-flash-next-iq3_s"],
                        model_allowlist=["claude-sonnet-5-5", "gpt-6.1-sol", "qwen3.8-flash-next-iq3_s"])
    svc.personas.upsert("Kilo-Sonnet", description="helper", enabled=1, routing="strict",
                        backend="meridian", pinned_models=["claude-sonnet-5-5"],
                        model_allowlist=["claude-sonnet-5-5"], execution_mode="direct")
    return svc


def _snapshots(svc):
    return svc.db.query_one("SELECT COUNT(*) AS n FROM state_snapshots")["n"]


def _persona(svc, name):
    p = svc.personas.get(name)
    return {**p, "pinned": json.loads(p["pinned_models"] or "[]"),
            "allow": json.loads(p["model_allowlist"] or "[]")}


# ---------------------------------------------------------------------------- persona_rules

def test_rules_normalize_aliases_and_refuse_bad_values(router):
    clean, errors, warnings = persona_rules.validate(router, {
        "reasoning_effort": "xhigh", "routing": "flexible", "fallback_policy": "local",
        "execution_mode": "agent", "kind": "image", "enabled": True, "context_window": "131072",
        "max_output_tokens": 0, "capabilities": ["text", "tools"], "pinned_models": "{}",
        "backend": "meridian"})
    assert not errors
    assert clean == {"reasoning_effort": "max", "routing": None, "fallback_policy": None,
                     "execution_mode": None, "kind": "media", "enabled": 1,
                     "context_window": 131072, "max_output_tokens": None,
                     "capabilities": ["text", "tools"], "pinned_models": [], "backend": "meridian"}
    _, errors, _ = persona_rules.validate(router, {
        "reasoning_effort": "extreme", "context_window": -1, "max_output_tokens": 10,
        "capabilities": ["telepathy"], "backend": "nope", "enabled": "yes", "bogus": 1})
    joined = " | ".join(errors)
    for bit in ("reasoning_effort must be one of", "context_window must be between",
                "max_output_tokens must be between", "capabilities: unknown telepathy",
                "no backend named 'nope'", "enabled must be true or false",
                "unknown persona field 'bogus'"):
        assert bit in joined, bit
    _, errors, _ = persona_rules.validate(router, {"routing": "strict", "model_allowlist": []})
    assert "needs at least one model" in errors[0]


def test_rules_mcp_grants_shape_and_unknown_server_warning(router):
    clean, errors, warnings = persona_rules.validate(router, {"preferred_mcp_tools": [
        "searxng", {"server": "crawl4ai", "tools": ["md"]}]})
    assert not errors and clean["preferred_mcp_tools"][1] == {"server": "crawl4ai", "tools": ["md"]}
    _, errors, _ = persona_rules.validate(router, {"preferred_mcp_tools": [{"tools": ["x"]}]})
    assert errors and "bad MCP grant" in errors[0]


def test_admin_persona_save_uses_the_same_rules(router, client):
    r = client.post("/admin/api/personas", json={"virtual_name": "Foundry-Chat",
                                                 "reasoning_effort": "ultra"})
    assert r.status_code == 400 and "reasoning_effort" in r.json()["error"]
    # the editor's full payload for an existing persona still saves
    body = {"virtual_name": "Foundry-Chat", "description": "x", "benchmark_category": "coding",
            "local_bias_strength": "moderate", "enabled": 1, "escalation_triggers": [],
            "context_window": None, "preferred_mcp_tools": [], "guardrail_overrides": {},
            "pinned_models": ["gpt-6.1-sol"], "model_allowlist": ["gpt-6.1-sol"],
            "execution_mode": None, "brain_handles_tools": 0, "pipeline_check_enabled": 1,
            "outcome_judge": None, "required_tags": [], "prefer_permissive": 0, "prefer_loaded": 0,
            "escalate_when_local_busy": 0, "expose_tool_trail": 0, "mcp_tools_in_direct": 1,
            "agent_backend": None, "max_output_tokens": None, "selection_weights": {},
            "review_enabled": 0, "review_model": None, "review_prefilter": 1,
            "client_compat": {}, "output_style": None, "reasoning_effort": None,
            "sampling_options": {}, "output_format": None, "force_reasoning_effort": 0,
            "fallback_policy": None, "routing": None, "backend": None, "kind": None,
            "capabilities": ["text"], "quiet_narration": 0, "assist": {}}
    r = client.post("/admin/api/personas", json=body)
    assert r.status_code == 200, r.text
    assert _persona(router, "Foundry-Chat")["allow"] == ["gpt-6.1-sol"]


# ---------------------------------------------------------------------------- resolve

def test_family_words_keep_that_familys_current_routes(router):
    cur = route_resolve.current_routes(router, router.personas.get("Foundry-Chat"))
    routes, notes = route_resolve.resolve(router, ["local", "Claude"], cur)
    assert [r["model"] for r in routes] == ["qwen3.8-flash-next-iq3_s", "claude-sonnet-5-5"]
    assert [r["backend"] for r in routes] == ["strata", "meridian"]
    with pytest.raises(route_resolve.ResolveError) as e:
        route_resolve.resolve(router, ["ChatGPT"], [])
    assert "gpt-6.1-sol@cliproxyapi" in str(e.value)          # names the concrete choices
    routes, _ = route_resolve.resolve(router, ["claude-opus-5-5@meridian", "mystery-model"], [])
    assert routes[0]["backend"] == "meridian" and routes[0]["family"] == "claude"
    warn = route_resolve.reachability(router, routes, None, None)
    assert any("mystery-model is not served" in w for w in warn)


# ---------------------------------------------------------------------------- persona tools

def test_routes_local_and_claude_only_through_the_card(chat, router, client):
    before = _snapshots(router)
    t = chat("make Foundry-Chat use only local and Claude",
             reply("", [call("set_persona_routes", name="Foundry-Chat", routes=["local", "Claude"])]),
             reply("Confirm the card."))
    card = t["confirm"]
    assert card and card["calls"][0]["class"] == "persona"
    prev = card["calls"][0]["preview"]
    assert prev["routes"] == ["qwen3.8-flash-next-iq3_s@strata (local)",
                              "claude-sonnet-5-5@meridian (Claude)"]
    assert any("gpt-6.1-sol" in x for x in prev["before"])
    res = _confirm(client, chat, t)
    assert res["ok"], res
    p = _persona(router, "Foundry-Chat")
    assert p["pinned"] == ["qwen3.8-flash-next-iq3_s", "claude-sonnet-5-5"] == p["allow"]
    assert _snapshots(router) == before + 1
    assert "run_persona_evals" in res["result"]["evals"]
    audit = chat.eng.audit(t["_thread"])
    assert any(a["status"] == "applied" and "set_persona_routes" in a["detail"] for a in audit)


def test_strict_backend_trap_is_refused_and_untie_works(chat, router, client):
    t = chat("add local to Kilo-Sonnet",
             reply("", [call("set_persona_routes", name="Kilo-Sonnet",
                             routes=["claude-sonnet-5-5", "qwen3.8-flash-next-iq3_s"])]),
             reply("refused"))
    assert t["confirm"] is None
    assert "tied to backend meridian" in t["refused"][0]["reason"]
    t = chat("untie it", reply("", [call("set_persona_routes", name="Kilo-Sonnet", backend="",
                                         routes=["claude-sonnet-5-5", "qwen3.8-flash-next-iq3_s"])]),
             reply("ok"))
    assert t["confirm"]["calls"][0]["preview"]["backend"] == "meridian -> (any)"
    assert _confirm(client, chat, t)["ok"]
    p = router.personas.get("Kilo-Sonnet")
    assert not p["backend"]


def test_route_refusals(chat, router):
    t = chat("x", reply("", [
        call("set_persona_routes", "c1", name="Nope", routes=["local"]),
        call("set_persona_routes", "c2", name="Foundry-Chat",
             routes=[{"model": "claude-sonnet-5-5", "enabled": False}]),
        call("set_persona_routes", "c3", name="Kilo-Sonnet", routes=["ChatGPT"])]), reply("no"))
    why = {r["id"]: r["reason"] for r in t["refused"]}
    assert "no persona 'Nope'" in why["c1"]
    assert "at least one route turned on" in why["c2"]
    assert "no ChatGPT route yet" in why["c3"] and "gpt-6.1-sol@cliproxyapi" in why["c3"]


def test_set_persona_fields_validates_and_edits_mcp(chat, router, client):
    router.personas.upsert("Foundry-Chat", preferred_mcp_tools=["searxng", "crawl4ai"])
    t = chat("128K context, no crawl4ai, medium effort", reply("", [
        call("set_persona_fields", name="Foundry-Chat",
             fields={"context_window": 131072, "reasoning_effort": "medium"},
             mcp_remove=["crawl4ai"], mcp_add=["stable-audio"])]), reply("card"))
    rows = {r["field"]: r for r in t["confirm"]["calls"][0]["preview"]["fields"]}
    assert rows["context_window"]["after"] == 131072
    assert rows["preferred_mcp_tools"]["after"] == ["searxng", "stable-audio"]
    assert _confirm(client, chat, t)["ok"]
    p = router.personas.get("Foundry-Chat")
    assert p["context_window"] == 131072 and p["reasoning_effort"] == "medium"
    assert json.loads(p["preferred_mcp_tools"]) == ["searxng", "stable-audio"]
    t = chat("bad", reply("", [
        call("set_persona_fields", "c1", name="Foundry-Chat", fields={"reasoning_effort": "huge"}),
        call("set_persona_fields", "c2", name="Foundry-Chat", fields={"pinned_models": ["x"]}),
        call("set_persona_fields", "c3", name="Kilo-Sonnet", fields={"backend": "strata"})]),
        reply("no"))
    why = {r["id"]: r["reason"] for r in t["refused"]}
    assert "reasoning_effort must be one of" in why["c1"]
    assert "set_persona_routes" in why["c2"]
    assert "does not serve its routes" in why["c3"]


def test_create_persona_copy_and_template_with_client_grant(chat, router, client):
    from foundry_router.config import ClientConfig

    def mutate(raw):
        raw["clients"].append(ClientConfig(name="anythingllm", key="allm-key-1234567890",
                                           scopes=["inference"], personas=["Foundry-Chat"])
                              .model_dump(exclude_none=True))
    router.config_store.save(mutate)
    t = chat("new research persona", reply("", [
        call("create_persona", "c1", name="Foundry-Deep-Dive", template="strict-chat",
             routes=["claude-opus-5-5@meridian", "qwen3.8-flash-next-iq3_s"], backend="",
             fields={"context_window": 200000}),
        call("create_persona", "c2", name="Foundry-Chat-2", copy_from="Foundry-Chat")]),
        reply("cards"))
    assert not t["refused"], t["refused"]
    calls = {c["id"]: c for c in t["confirm"]["calls"]}
    assert calls["c1"]["preview"]["clients_to_allow"] == ["anythingllm"]
    assert calls["c1"]["preview"]["note"] == "starts disabled"
    r = client.post(f"/control/api/chat/confirms/{t['confirm']['confirm_id']}", headers=chat.h,
                    json={"decision": "confirm", "picked": ["c1", "c2"]}).json()
    res = {x["id"]: x for x in r["results"]}
    assert res["c1"]["ok"] and res["c2"]["ok"], r
    assert res["c1"]["result"]["client_keys_to_allow"] == ["anythingllm"]
    p = router.personas.get("Foundry-Deep-Dive")
    assert p["enabled"] == 0 and p["routing"] == "strict" and p["context_window"] == 200000
    assert json.loads(p["model_allowlist"]) == ["claude-opus-5-5", "qwen3.8-flash-next-iq3_s"]
    assert router.personas.get("Foundry-Chat-2")["enabled"] == 0
    # name taken / neither source given
    t = chat("again", reply("", [call("create_persona", "c1", name="Foundry-Chat", template="strict-chat",
                                      routes=["local"]),
                                 call("create_persona", "c2", name="X")]), reply("no"))
    why = {x["id"]: x["reason"] for x in t["refused"]}
    assert "already exists" in why["c1"] and "copy_from" in why["c2"]
    # grant on the client key: a separate card
    t = chat("allow it", reply("", [call("grant_persona_to_client", persona="Foundry-Deep-Dive",
                                         client="anythingllm")]), reply("card"))
    assert _confirm(client, chat, t)["ok"]
    allm = next(c for c in router.config_store.config.clients if c.name == "anythingllm")
    assert "Foundry-Deep-Dive" in allm.personas
    t = chat("again", reply("", [call("grant_persona_to_client", persona="Foundry-Deep-Dive",
                                      client="anythingllm")]), reply("no"))
    assert "already allows" in t["refused"][0]["reason"]


def test_run_persona_evals_starts_a_run(chat, router, client, monkeypatch):
    started = []

    async def fake_run(persona, judge="", categories=None):
        started.append(persona)
        return 1
    monkeypatch.setattr(router.evals, "run", fake_run)
    t = chat("run its evals", reply("", [call("run_persona_evals", name="Foundry-Chat")]),
             reply("card"))
    assert _confirm(client, chat, t)["ok"]
    import time
    for _ in range(50):
        if started:
            break
        time.sleep(0.02)
    assert started == ["Foundry-Chat"]


# ---------------------------------------------------------------------------- backends

def test_backend_off_lists_orphaned_personas(chat, router, client):
    t = chat("turn off meridian", reply("", [call("set_backend_enabled", name="meridian",
                                                  enabled=False)]), reply("card"))
    c = t["confirm"]["calls"][0]
    assert c["class"] == "backend"
    orphans = c["preview"]["orphans"]
    assert any(o.startswith("Kilo-Sonnet: tied to backend meridian") and "refused" in o
               for o in orphans)
    assert not any(o.startswith("Foundry-Chat") for o in orphans)   # still has GPT and local
    before = _snapshots(router)
    res = _confirm(client, chat, t)
    assert res["ok"] and res["result"]["personas_without_a_route"]
    assert not next(b for b in router.config_store.config.backend_pool.internal.backends
                    if b.name == "meridian").enabled
    assert _snapshots(router) == before + 1
    t = chat("x", reply("", [call("set_backend_enabled", name="nope", enabled=True)]), reply("no"))
    assert "no backend named 'nope'" in t["refused"][0]["reason"]


def test_admin_backends_enabled_endpoint_uses_the_same_write(router, client):
    r = client.post("/admin/api/backends/enabled", json={"name": "strata", "enabled": False})
    assert r.status_code == 200
    assert not next(b for b in router.config_store.config.backend_pool.internal.backends
                    if b.name == "strata").enabled
    assert client.post("/admin/api/backends/enabled",
                       json={"name": "zzz", "enabled": True}).status_code == 404


# ---------------------------------------------------------------------------- whole router

def test_set_router_config_validates_versions_and_forecasts_restarts(chat, router, client):
    t = chat("raise the tool round limit to 12", reply("", [
        call("set_router_config", "c1", changes={"agent_brain.worker_tool_max_steps": 12,
                                                 "backend_pool.internal.backends.strata.priority": 5}),
        call("set_router_config", "c2", changes={"backend_pool.internal.backends.meridian.api_key": "x"}),
        call("set_router_config", "c3", changes={"agent_brain.worker_tool_max_steps": "lots"}),
        call("set_router_config", "c4", changes={"server.port": 9999})]), reply("cards"))
    why = {x["id"]: x["reason"] for x in t["refused"]}
    assert "secret" in why["c2"]
    assert "would not validate" in why["c3"]
    calls = {c["id"]: c for c in t["confirm"]["calls"]}
    assert calls["c4"]["preview"]["restart_needed"] == ["server"]
    assert set(calls["c1"]["preview"]["applies_live"]) == {"agent_brain", "backend_pool"}
    versions = router.db.query_one("SELECT COUNT(*) AS n FROM config_versions")["n"]
    res = _confirm(client, chat, t)
    assert res["ok"], res
    cfg = router.config_store.config
    assert cfg.agent_brain.worker_tool_max_steps == 12
    assert next(b for b in cfg.backend_pool.internal.backends if b.name == "strata").priority == 5
    assert router.db.query_one("SELECT COUNT(*) AS n FROM config_versions")["n"] == versions + 1
    assert cfg.server.port == 11435                                   # c4 was not picked


def test_admin_reads_and_writes_with_deny_list_and_irreversible_cards(chat, router, client):
    t = chat("read", reply("", [
        call("admin_get", "c1", path="/admin/api/personas"),
        call("admin_get", "c2", path="/admin/api/export"),
        call("get_router_config", "c3", section="agent_brain"),
        call("list_admin_endpoints", "c4", filter="personas")]), reply("done"))
    res = {r["id"]: r for r in t["results"]}
    assert res["c1"]["ok"] and any(p["virtual_name"] == "Foundry-Chat"
                                   for p in res["c1"]["result"]["personas"])
    assert res["c3"]["result"]["agent_brain"]["model"] == "test-brain"
    eps = res["c4"]["result"]["endpoints"]
    assert any(e.startswith("POST /admin/api/personas/delete") for e in eps)
    assert any("[not from chat]" in e for e in eps)              # POST /admin/api/personas
    assert "can carry secrets" in t["refused"][0]["reason"]
    # admin class on auto: a harmless write runs at once, an irreversible one still asks
    client.put("/control/api/chat/settings", headers=chat.h, json={"auto_classes": {"admin": True}})
    t = chat("do it", reply("", [
        call("admin_call", "c1", method="POST", path="/admin/api/config/data_retention",
             body={"prune_interval_minutes": 45}),
        call("admin_call", "c2", method="POST", path="/admin/api/events/clear", body={}),
        call("admin_call", "c3", method="POST", path="/admin/api/setup/clients/victor/rotate"),
        call("admin_call", "c4", method="POST", path="/admin/api/personas",
             body={"virtual_name": "x"})]), reply("ok"))
    assert t["results"][0]["ok"], t["results"]
    assert router.config_store.config.data_retention.prune_interval_minutes == 45
    card = t["confirm"]["calls"]
    assert [c["id"] for c in card] == ["c2"] and card[0]["preview"]["irreversible"]
    why = {x["id"]: x["reason"] for x in t["refused"]}
    assert "shows the new key" in why["c3"] and "set_persona_fields" in why["c4"]


def test_admin_get_scrubs_secret_values(chat, router, client, monkeypatch):
    from foundry_router import configops
    monkeypatch.setattr(configops, "secret_values", lambda svc: {"sekrit-value-123456"})
    router.personas.upsert("Foundry-Chat", description="token sekrit-value-123456 here")
    t = chat("read", reply("", [call("admin_get", path="/admin/api/personas")]), reply("ok"))
    assert "sekrit-value-123456" not in json.dumps(t["results"][0]["result"])


def test_research_tools(chat, router, client, monkeypatch):
    router.db.execute("INSERT INTO models (id, provider, research_status, enabled, source) "
                      "VALUES ('new-model', 'strata', NULL, 1, 'discovery')")
    t = chat("what needs research", reply("", [call("research_status")]), reply("ok"))
    out = t["results"][0]["result"]
    assert any(m["model"] == "new-model" for m in out["models"])
    monkeypatch.setattr(router.research, "prerequisites", lambda: (False, "no search tool"))
    t = chat("research it", reply("", [call("research_models", models=["new-model"])]), reply("no"))
    assert "no search tool" in t["refused"][0]["reason"]
    queued = []
    monkeypatch.setattr(router.research, "prerequisites", lambda: (True, ""))
    monkeypatch.setattr(router.research, "enqueue", lambda m: queued.append(m) or True)
    t = chat("research it", reply("", [call("research_models", models=["new-model"])]), reply("card"))
    assert _confirm(client, chat, t)["ok"] and queued == ["new-model"]


def test_performance_report(chat, router):
    router.db.execute("INSERT INTO request_log (ts, persona, status, outcome, duration_ms) "
                      "VALUES (datetime('now'), 'Foundry-Chat', 'ok', 'ok', 1200)")
    t = chat("how is performance", reply("", [call("performance_report", hours=24)]), reply("ok"))
    rep = t["results"][0]["result"]
    assert {"requests", "models", "backends", "brain"} <= set(rep)


# ---------------------------------------------------------------------------- chat backend

def test_chat_uses_its_own_chain_never_a_persona(router, client, cc, app, monkeypatch):  # noqa: F811
    from tests.test_control_center import login
    eng = router.chat
    eng._model_fn = None
    assert eng.chat_routes() == [("meridian", "claude-sonnet-5-5"), ("cliproxyapi", "gpt-6.1-sol"),
                                 ("strata", "qwen3.8-flash-next-iq3_s")]
    seen = []

    async def fake_chat(model, messages, **kw):
        seen.append((model, request_context.backend_pin(), kw.get("fmt") is not None))
        if model == "claude-sonnet-5-5":
            raise RuntimeError("over the cutoff")
        return ChatResult(content=json.dumps({"say": "hi", "ask": None, "calls": []}),
                          prompt_tokens=3, completion_tokens=2), "cliproxyapi"
    monkeypatch.setattr(router.pool, "chat", fake_chat)
    h = login(client)
    th = client.post("/control/api/chat/threads", headers=h, json={}).json()["thread_id"]
    r = client.post(f"/control/api/chat/threads/{th}/turns", headers=h, json={"text": "hello"}).json()
    assert r["say"] == "hi" and r["served_model"] == "gpt-6.1-sol"
    assert seen == [("claude-sonnet-5-5", "meridian", True), ("gpt-6.1-sol", "cliproxyapi", True)]
    assert router.personas.get("Foundry-Operator-Chat") is None       # no persona involved
    # pick the chain in Chat settings
    r = client.put("/control/api/chat/backend", headers=h,
                   json={"routes": [{"backend": "strata", "model": "qwen3.8-flash-next-iq3_s"}]})
    assert r.status_code == 200, r.text
    assert r.json()["chain"] == ["qwen3.8-flash-next-iq3_s@strata"]
    assert eng.chat_routes() == [("strata", "qwen3.8-flash-next-iq3_s")]
    bad = client.put("/control/api/chat/backend", headers=h,
                     json={"routes": [{"backend": "strata", "model": "claude-opus-5-5"}]})
    assert bad.status_code == 400 and "does not serve" in bad.json()["error"]
    assert client.get("/control/api/chat/backend").json()["source"] == "operator_chat.routes"
