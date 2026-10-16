"""Step 2: versioned default personas, exact-backend strict routing, disabled
and image personas, /v1/models metadata, and end-to-end persona validation."""

import json

import httpx
import pytest

from foundry_router import profiles
from foundry_router.config import BackendConfig, BackendPoolConfig
from foundry_router.pool.internal import InternalPool

HITS: list = []
STATE: dict = {}
KILO = "animaluprising-kilo"
MODELS = {"strata": ["qwen3.8-flash-next-iq3_s"],
          "meridian": ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"],
          "cliproxy": ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra", "claude-sonnet-5-5",
                       "gpt-image-2.5-sunburst", "gpt-image-2.5-flare"]}
HOSTS = {"strata-host": "strata", "meridian-host": "meridian", "cpa-host": "cliproxy"}


def _reply(content=None, tool_calls=None, finish="stop"):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"finish_reason": finish, "message": msg}],
                                     "usage": {"prompt_tokens": 5, "completion_tokens": 2}})


def _text_of(m):
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if p.get("type") == "text"), \
            any(p.get("type") == "image_url" for p in c)
    return c or "", False


async def _router(req: httpx.Request):
    backend = HOSTS[req.url.host]
    if req.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": m} for m in MODELS[backend]]})
    if "/images/" in req.url.path:
        import base64
        from tests.test_media import png
        HITS.append((backend, "image"))
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png()).decode()}]})
    body = json.loads(req.content)
    HITS.append((backend, body["model"]))
    STATE["last_body"] = body
    if backend in STATE.get("fail", ()):
        return httpx.Response(500, text="boom")
    last = body["messages"][-1]
    text, has_image = _text_of(last)
    if last.get("role") == "tool":
        return _reply("Flag alpha is now on.")
    if body.get("tools") and last.get("role") == "user":
        val = "true" if STATE.get("bool_as_string") else True
        return _reply(None, [{"id": "call_1", "type": "function", "function": {
            "name": "set_flag", "arguments": json.dumps({"name": "alpha", "enabled": val})}}],
            finish="tool_calls")
    if has_image:
        return _reply("Blue" if STATE.get("blind") else "Red")
    if body.get("max_tokens") == 16 and STATE.get("ignore_max"):
        r = _reply(" ".join(map(str, range(1, 301))))
        r = httpx.Response(200, json={**r.json(), "usage": {"prompt_tokens": 5, "completion_tokens": 300}})
        return r
    if body.get("max_tokens") == 16:
        return _reply("1 2 3 4 5", finish="length")
    if "FOUNDRY_OK" in text:
        return _reply("FOUNDRY_OK")
    return _reply("hello there my good friend")


@pytest.fixture()
def fleet(app, client):
    svc = app.state.services
    HITS.clear()
    STATE.clear()
    http = httpx.AsyncClient(transport=httpx.MockTransport(_router))
    pool = InternalPool([
        BackendConfig(name="strata", type="openai-compatible", url="http://strata-host/v1",
                      flavor="llamacpp", priority=100),
        # Meridian is mocked with the openai dialect here; its type marks it as Meridian.
        BackendConfig(name="meridian", type="openai-compatible", url="http://meridian-host/v1",
                      subscription=True, priority=100),
        BackendConfig(name="cliproxy", type="openai-compatible", url="http://cpa-host/v1",
                      api_key="k", flavor="cliproxyapi", priority=50)],
        BackendPoolConfig(), http, svc.db)
    for name, models in MODELS.items():
        s = pool.backends[name]
        s.healthy = s.ever_checked = True
        s.models = list(models)
    real_pool, real_http = svc.pool, svc.http
    svc.pool, svc.http = pool, http
    svc.config_store.config.agent_brain.direct_stream = False
    svc.register_discovered()
    svc.registry.upsert_auto("qwen3.8-flash-next-iq3_s", source="discovery", context_length=131072)
    # the test pool marks Meridian by type in real deployments; map the profile's
    # "meridian" kind onto this backend for the test
    orig = profiles._resolve_backend
    profiles._resolve_backend = lambda svc_, kind: (
        "meridian" if kind == "meridian" else orig(svc_, kind))
    yield svc
    profiles._resolve_backend = orig
    svc.pool, svc.http = real_pool, real_http


def _rows(svc):
    return {p["virtual_name"]: p for p in svc.personas.list() if p["virtual_name"].startswith("Foundry-Kilo")}


# -- profile --------------------------------------------------------------------

def test_profile_creates_strict_personas_frontier_disabled(fleet, client):
    rep = client.post(f"/admin/api/profiles/{KILO}/apply").json()
    assert rep["counts"] == {"create": 11}
    rows = _rows(fleet)
    assert len(rows) == 11
    assert [n for n, r in rows.items() if r["enabled"]] == ["Foundry-Kilo-Local"]
    for r in rows.values():
        assert r["routing"] == "strict" and r["execution_mode"] == "direct"
        assert r["fallback_policy"] == "refuse" and r["profile"] == f"{KILO}@1"
    assert rows["Foundry-Kilo-Local"]["backend"] == "strata"
    assert rows["Foundry-Kilo-Sonnet"]["backend"] == "meridian"
    assert rows["Foundry-Kilo-Sol"]["backend"] == "cliproxy"
    assert rows["Foundry-Kilo-Images"]["kind"] == "media"
    again = client.post(f"/admin/api/profiles/{KILO}/apply").json()
    assert again["counts"] == {"unchanged": 11}


def test_profile_update_preserves_edits_enabled_state_and_foreign_personas(fleet, client, monkeypatch):
    fleet.personas.upsert("Foundry-Kilo-Sol", description="mine", routing="flexible")
    client.post(f"/admin/api/profiles/{KILO}/apply")
    fleet.personas.upsert("Foundry-Kilo-Opus", description="my own wording")   # an edit
    fleet.personas.upsert("Foundry-Kilo-Haiku", enabled=1)                    # just enabled
    # a UI save that re-sends unchanged values (numbers as strings) is not an edit
    fleet.personas.upsert("Foundry-Kilo-Luna", max_output_tokens="8192")
    v2 = json.loads(json.dumps(profiles.PROFILES[KILO]))
    v2["version"] = 2
    for d in v2["personas"]:
        d["max_output_tokens"] = 12000 if d["max_output_tokens"] else None
    monkeypatch.setitem(profiles.PROFILES, KILO, v2)
    status = {p["persona"]: p["action"] for p in
              client.get("/admin/api/profiles").json()["profiles"][0]["personas"]}
    assert status["Foundry-Kilo-Sol"] == "conflict" and status["Foundry-Kilo-Opus"] == "kept"
    assert status["Foundry-Kilo-Haiku"] == status["Foundry-Kilo-Luna"] == "update"
    client.post(f"/admin/api/profiles/{KILO}/apply")
    rows = _rows(fleet)
    assert rows["Foundry-Kilo-Sol"]["description"] == "mine"
    assert rows["Foundry-Kilo-Sol"]["routing"] == "flexible"
    assert rows["Foundry-Kilo-Opus"]["description"] == "my own wording"
    assert rows["Foundry-Kilo-Opus"]["profile"] == f"{KILO}@1"
    assert rows["Foundry-Kilo-Haiku"]["max_output_tokens"] == 12000
    assert rows["Foundry-Kilo-Haiku"]["enabled"] == 1                          # kept on
    assert rows["Foundry-Kilo-Haiku"]["profile"] == f"{KILO}@2"


# -- routing gates ----------------------------------------------------------------

def _chat(client, model, path="/v1/chat/completions"):
    return client.post(path, json={"model": model, "stream": False,
                                   "messages": [{"role": "user", "content": "hi"}]})


def test_disabled_and_image_personas_are_not_chat_models(fleet, client):
    client.post(f"/admin/api/profiles/{KILO}/apply")
    ids = {m["id"] for m in client.get("/v1/models").json()["data"]}
    assert "Foundry-Kilo-Local" in ids
    assert "Foundry-Kilo-Sol" not in ids and "Foundry-Kilo-Images" not in ids
    r = _chat(client, "Foundry-Kilo-Sol")
    assert r.status_code == 403 and r.json()["error"]["code"] == "persona_disabled"
    r = _chat(client, "Foundry-Kilo-Sol", "/api/chat")
    assert r.status_code == 403 and r.json()["code"] == "persona_disabled"
    assert client.get("/v1/models/Foundry-Kilo-Sol").status_code == 404
    fleet.personas.upsert("Foundry-Kilo-Images", enabled=1)
    r = _chat(client, "Foundry-Kilo-Images")
    assert r.status_code == 400 and r.json()["error"]["code"] == "image_persona"
    assert HITS == []


def test_strict_persona_never_switches_backend_for_same_model(fleet, client):
    STATE["fail"] = {"meridian"}
    # claude-sonnet-5-5 is offered by meridian AND by a Claude account in CLIProxyAPI
    fleet.personas.upsert("Pinned", execution_mode="direct", routing="strict",
                          backend="meridian", model_allowlist=["claude-sonnet-5-5"])
    assert _chat(client, "Pinned").status_code >= 500
    assert {b for b, _ in HITS} == {"meridian"}
    HITS.clear()
    fleet.personas.upsert("StrictNoBackend", execution_mode="direct", routing="strict",
                          model_allowlist=["claude-sonnet-5-5"])
    assert _chat(client, "StrictNoBackend").status_code >= 500
    assert {b for b, _ in HITS} == {"meridian"}            # the single best backend only
    HITS.clear()
    # pinned backend that doesn't serve the model -> refused before any call
    fleet.personas.upsert("WrongBackend", execution_mode="direct", routing="strict",
                          backend="strata", model_allowlist=["claude-sonnet-5-5"])
    r = _chat(client, "WrongBackend")
    assert r.status_code == 503 and r.json()["error"]["code"] == "route_unavailable"
    assert HITS == []
    # flexible personas keep their old cross-backend failover
    fleet.personas.upsert("Flexible", execution_mode="direct",
                          model_allowlist=["claude-sonnet-5-5"])
    assert _chat(client, "Flexible").status_code == 200
    assert ("cliproxy", "claude-sonnet-5-5") in HITS


# -- metadata ---------------------------------------------------------------------

def test_models_metadata_reports_effective_limits(fleet, client):
    client.post(f"/admin/api/profiles/{KILO}/apply")
    fleet.personas.upsert("Foundry-Kilo-Local", context_window=262144)   # above the model's
    fleet.personas.upsert("Foundry-Kilo-Vision", enabled=1)
    data = {m["id"]: m for m in client.get("/v1/models").json()["data"]}
    local = data["Foundry-Kilo-Local"]
    assert local["context_length"] == 131072                 # min(persona, model)
    assert local["max_output_tokens"] == 32768
    assert local["top_provider"] == {"context_length": 131072, "max_completion_tokens": 32768}
    assert local["architecture"]["input_modalities"] == ["text"]
    assert local["capabilities"] == {"tools": True, "vision": False, "reasoning": False}
    assert local["foundry"]["routing"] == "strict" and local["foundry"]["backend"] == "strata"
    assert local["foundry"]["served_model"] == "qwen3.8-flash-next-iq3_s"
    vis = data["Foundry-Kilo-Vision"]
    assert vis["capabilities"]["vision"] and "image" in vis["architecture"]["input_modalities"]
    show = client.post("/api/show", json={"model": "Foundry-Kilo-Vision"}).json()
    assert "vision" in show["capabilities"]
    assert client.get("/v1/models/Foundry-Kilo-Local").json()["context_length"] == 131072
    show = client.post("/api/show", json={"model": "Foundry-Kilo-Local"}).json()
    assert show["model_info"]["general.context_length"] == 131072


# -- validation -------------------------------------------------------------------

def _validate(client, name):
    r = client.post("/admin/api/personas/validate", json={"virtual_name": name})
    assert r.status_code == 200, r.text
    return r.json()


def test_validate_local_persona_passes_every_check(fleet, client):
    client.post(f"/admin/api/profiles/{KILO}/apply")
    res = _validate(client, "Foundry-Kilo-Local")
    checks = {c["name"]: c for c in res["checks"]}
    assert res["status"] == "passed", res
    assert [checks[n]["status"] for n in ("route", "text", "stream", "tools", "length")] == \
        ["ok"] * 5
    assert checks["image_input"]["status"] == "skip"
    assert res["served_models"] == ["qwen3.8-flash-next-iq3_s"] and res["backends"] == ["strata"]
    assert {b for b, _ in HITS} == {"strata"}
    stored = json.loads(fleet.personas.get("Foundry-Kilo-Local")["validation"])
    assert stored["status"] == "passed"
    entry = next(m for m in client.get("/v1/models").json()["data"]
                 if m["id"] == "Foundry-Kilo-Local")
    assert entry["foundry"]["validation"] == "passed"
    rows = fleet.db.query("SELECT client_id FROM request_log ORDER BY id DESC LIMIT 1")
    assert rows[0]["client_id"] == "foundry:validate"


def test_validate_disabled_vision_helper_and_failures(fleet, client):
    client.post(f"/admin/api/profiles/{KILO}/apply")
    res = _validate(client, "Foundry-Kilo-Vision")       # disabled, still validated
    checks = {c["name"]: c["status"] for c in res["checks"]}
    assert res["status"] == "passed" and checks["image_input"] == "ok"
    assert checks["length"] == "ok"
    assert fleet.personas.get("Foundry-Kilo-Vision")["enabled"] == 0
    assert _chat(client, "Foundry-Kilo-Vision").status_code == 403   # clients still can't
    STATE.update(blind=True, bool_as_string=True)
    res = _validate(client, "Foundry-Kilo-Vision")
    checks = {c["name"]: c for c in res["checks"]}
    assert res["status"] == "failed"
    assert "arrived as str" in checks["tools"]["detail"]
    assert checks["image_input"]["status"] == "fail"
    STATE.clear()
    STATE["fail"] = {"cliproxy"}
    res = _validate(client, "Foundry-Kilo-Sol")
    assert res["status"] == "failed"
    assert {c["name"]: c["status"] for c in res["checks"]}["text"] == "fail"
    assert {b for b, _ in HITS if b != "strata"} <= {"meridian", "cliproxy"}
    assert ("meridian", "gpt-6.1-sol") not in HITS


def test_validate_image_persona_runs_one_job(fleet, client):
    client.post(f"/admin/api/profiles/{KILO}/apply")
    res = _validate(client, "Foundry-Kilo-Images-Draft")
    checks = {c["name"]: c for c in res["checks"]}
    assert res["status"] == "passed", res
    assert "gpt-image-2.5-flare" in checks["image_job"]["detail"]
    assert HITS.count(("cliproxy", "image")) == 1
    assert res["served_models"] == ["gpt-image-2.5-flare"] and res["backends"] == ["cliproxy"]


def test_validate_length_warning_says_the_cap_was_not_applied(fleet, client):
    client.post(f"/admin/api/profiles/{KILO}/apply")
    STATE["ignore_max"] = True
    res = _validate(client, "Foundry-Kilo-Local")
    length = next(c for c in res["checks"] if c["name"] == "length")
    assert res["status"] == "warnings" and length["status"] == "warn"
    assert "300 tokens, so the backend didn't apply max_tokens 16" in length["detail"]


def test_unresolved_backend_fails_closed_until_it_appears(fleet, client, monkeypatch):
    """No Meridian found at install: the Claude personas must NOT be served by
    another backend that happens to list the model (CLIProxyAPI's Claude
    account here); once Meridian exists, re-applying fills the backend in."""
    monkeypatch.setattr(profiles, "_resolve_backend",
                        lambda svc_, kind: "cliproxy" if kind == "cliproxyapi" else None)
    client.post(f"/admin/api/profiles/{KILO}/apply")
    assert fleet.personas.get("Foundry-Kilo-Sonnet")["backend"] == "<meridian>"
    fleet.personas.upsert("Foundry-Kilo-Sonnet", enabled=1)
    r = _chat(client, "Foundry-Kilo-Sonnet")
    assert r.status_code == 503 and r.json()["error"]["code"] == "route_unavailable"
    assert HITS == []
    monkeypatch.setattr(profiles, "_resolve_backend",
                        lambda svc_, kind: {"cliproxyapi": "cliproxy", "meridian": "meridian"}.get(kind))
    rep = {p["persona"]: p for p in client.post(f"/admin/api/profiles/{KILO}/apply").json()["personas"]}
    assert rep["Foundry-Kilo-Sonnet"]["action"] == "fill"
    assert fleet.personas.get("Foundry-Kilo-Sonnet")["backend"] == "meridian"
    again = client.get("/admin/api/profiles").json()["profiles"]
    assert next(p for p in again if p["id"] == KILO)["counts"].get("fill") is None
    assert _chat(client, "Foundry-Kilo-Sonnet").status_code == 200
    assert HITS == [("meridian", "claude-sonnet-5-5")]


def test_kilo_provider_config(fleet, client):
    client.post(f"/admin/api/profiles/{KILO}/apply")
    cfg = client.get("/admin/api/personas/kilo-config?base_url=http://foundry:11435").json()
    prov = cfg["provider"]["foundry"]
    assert prov["options"]["baseURL"] == "http://foundry:11435/v1"
    assert "Foundry-Kilo-Images" not in prov["models"]
    local = prov["models"]["Foundry-Kilo-Local"]
    assert local["limit"] == {"context": 131072, "output": 32768} and local["tool_call"]
    assert prov["models"]["Foundry-Kilo-Vision"]["attachment"] is True
    assert "(disabled in Foundry)" in prov["models"]["Foundry-Kilo-Sol"]["name"]


def test_unflavored_cliproxy_is_named_and_filled_once_flavored(fleet, client):
    """A CLIProxyAPI backend saved without flavor: the profile says which
    backend to fix; after the fix an edited persona keeps its edits but gets
    the real backend instead of the placeholder."""
    cpa = fleet.pool.backends["cliproxy"].config
    cpa.flavor = None
    rep = {p["persona"]: p for p in client.post(f"/admin/api/profiles/{KILO}/apply").json()["personas"]}
    assert fleet.personas.get("Foundry-Kilo-Astra")["backend"] == "<cliproxyapi>"
    assert "backend 'cliproxy' looks like CLIProxyAPI" in rep["Foundry-Kilo-Astra"]["warning"]
    fleet.personas.upsert("Foundry-Kilo-Astra", description="my edit")
    cpa.flavor = "cliproxyapi"
    # the preview counts the fill, so the Update button isn't stuck at 0 changes
    prof = next(p for p in client.get("/admin/api/profiles").json()["profiles"] if p["id"] == KILO)
    assert prof["counts"]["fill"] == 6
    assert fleet.personas.get("Foundry-Kilo-Astra")["backend"] == "<cliproxyapi>"
    rep = {p["persona"]: p for p in client.post(f"/admin/api/profiles/{KILO}/apply").json()["personas"]}
    assert rep["Foundry-Kilo-Astra"]["action"] == "fill"
    row = fleet.personas.get("Foundry-Kilo-Astra")
    assert row["backend"] == "cliproxy" and row["description"] == "my edit"


def test_backend_save_fills_placeholders_of_a_ui_installed_profile(fleet, client):
    """Installed from the UI (not in config persona_profiles): saving the
    backend list must still fill the waiting personas, edits and all."""
    assert KILO not in (fleet.config_store.config.persona_profiles or [])
    cpa = fleet.pool.backends["cliproxy"].config
    cpa.flavor = None
    client.post(f"/admin/api/profiles/{KILO}/apply")
    fleet.personas.upsert("Foundry-Kilo-Luna", description="my edit")
    cpa.flavor = "cliproxyapi"
    filled = profiles.fill_placeholders(fleet)
    assert set(filled) == {"Foundry-Kilo-Luna", "Foundry-Kilo-Sol", "Foundry-Kilo-Astra",
                           "Foundry-Kilo-Vision-Deep", "Foundry-Kilo-Images",
                           "Foundry-Kilo-Images-Draft"}
    rows = _rows(fleet)
    assert all(rows[n]["backend"] == "cliproxy" for n in filled)
    assert rows["Foundry-Kilo-Luna"]["description"] == "my edit"
    rep = {p["persona"]: p for p in client.post(f"/admin/api/profiles/{KILO}/apply").json()["personas"]}
    assert rep["Foundry-Kilo-Luna"]["action"] == "kept"
    assert rep["Foundry-Kilo-Sol"]["action"] == "unchanged"
    assert profiles.fill_placeholders(fleet) == []


def test_length_check_is_skipped_on_meridian(fleet, client, monkeypatch):
    """Meridian (anthropic-compatible) never applies max_tokens, so the check
    is a skip with the reason, not a warning on every Claude persona."""
    import asyncio
    from foundry_router import persona_validate
    real = fleet.pool.backend_info
    monkeypatch.setattr(fleet.pool, "backend_info", lambda m: dict(
        real(m) or {}, type="anthropic-compatible") if m.startswith("claude") else real(m))
    client.post(f"/admin/api/profiles/{KILO}/apply")
    run = persona_validate._Run(fleet, client.app, fleet.personas.get("Foundry-Kilo-Haiku"))
    try:
        asyncio.run(persona_validate._length(run))
    finally:
        asyncio.run(run.client.aclose())
    assert run.checks == [{"name": "length", "status": "skip", "ms": None,
                           "detail": "Meridian ignores max_tokens (Claude Code SDK has no output cap)"}]
    assert ("meridian", "claude-haiku-5-5") not in HITS



def test_structured_output_request_skips_the_brain_loop(fleet, client, monkeypatch):
    """An agent-mode persona asked for a JSON schema (Mealie's provider test)
    is answered by one direct model call with the client's response_format,
    not by the brain loop, which would drop it."""
    monkeypatch.setattr(fleet.brain, "skip_reason", lambda: None)     # brain "healthy"
    fleet.personas.upsert("Chatty", execution_mode="agent", model_allowlist=["claude-haiku-5-5"])
    fleet.personas.upsert("Piped", execution_mode="pipeline", model_allowlist=["claude-haiku-5-5"])
    schema = {"type": "object", "properties": {"text": {"type": "string"}},
              "required": ["text"], "additionalProperties": False}
    r = client.post("/v1/chat/completions", json={
        "model": "Chatty",
        "messages": [{"role": "system", "content": "Respond in JSON."},
                     {"role": "user", "content": [{"type": "text", "text": "Hello, can I reach you?"}]}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "OpenAIText", "strict": True, "schema": schema}}})
    assert r.status_code == 200, r.text
    assert HITS == [("meridian", "claude-haiku-5-5")]
    sent = STATE["last_body"]["response_format"]
    assert sent["type"] == "json_schema" and sent["json_schema"]["schema"] == schema
    # a pipeline persona too: one direct call, JSON mode passed on
    HITS.clear()
    r = client.post("/api/chat", json={"model": "Piped", "stream": False, "format": "json",
                                       "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    assert HITS == [("meridian", "claude-haiku-5-5")]
    assert STATE["last_body"]["response_format"] == {"type": "json_object"}


def test_unfence_json_only_unwraps_one_valid_block():
    from foundry_router.pool.protocols import unfence_json
    assert unfence_json('```json\n{"text": "hi"}\n```') == '{"text": "hi"}'
    assert unfence_json('  ```\n[1, 2]\n```  ') == "[1, 2]"
    for keep in ('{"text": "hi"}', "Sure! ```json\n{}\n```", "```json\n{oops\n```",
                 '```json\n{"a": 1}\n```\n```json\n{"b": 2}\n```', "plain prose"):
        assert unfence_json(keep) == keep


def test_structured_output_keeps_the_schema_on_a_persona_with_mcp_tools(fleet, client, monkeypatch):
    """Direct mode drops the format when it attaches the persona's MCP tools
    (forced JSON would break tool calls); a JSON request from a client with no
    tools of its own gets the schema and no tools instead."""
    from foundry_router.facade import ollama_api

    class _Tool:
        name = "searxng_search"

        def spec(self):
            return {"type": "function", "function": {"name": self.name, "parameters": {}}}
    monkeypatch.setattr(ollama_api, "_direct_mcp_tools", lambda svc, p, ct: {"searxng_search": _Tool()})
    fleet.personas.upsert("Tooly", execution_mode="direct", model_allowlist=["qwen3.8-flash-next-iq3_s"])
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    r = client.post("/v1/chat/completions", json={
        "model": "Tooly", "messages": [{"role": "user", "content": "hi"}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "OpenAIText", "schema": schema}}})
    assert r.status_code == 200, r.text
    sent = STATE["last_body"]
    assert sent["response_format"]["json_schema"]["schema"] == schema and not sent.get("tools")
    # without a format the persona's tools are still offered
    client.post("/v1/chat/completions", json={"model": "Tooly", "messages": [{"role": "user", "content": "hi"}]})
    assert [t["function"]["name"] for t in STATE["last_body"]["tools"]] == ["searxng_search"]
    assert "response_format" not in STATE["last_body"]
