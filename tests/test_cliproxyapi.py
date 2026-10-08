"""CLIProxyAPI (router-for-me/CLIProxyAPI) as a Foundry backend.

The fake below answers the way CLIProxyAPI v8 does on its OpenAI dialect
(read from its source): /v1/models with only id/object/created/owned_by and a
Bearer key required; /v1/chat/completions streaming reasoning as
`reasoning_content` deltas (only when `reasoning_effort` asks for a summary),
tool calls, and Codex usage mapped to prompt/completion tokens with
prompt_tokens_details.cached_tokens.

Foundry must treat it as a paid SUBSCRIPTION cloud backend (not a free local
one), send the full Codex effort scale, and pass tools/images/streams through.
"""

import json

import httpx
import pytest

from foundry_router.config import BackendConfig, BackendPoolConfig
from foundry_router.pool.internal import InternalPool

KEY = "cpa-test-key"
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "read a file",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
SEEN: dict = {}
MODELS = ["gpt-5.5", "gpt-5.3-codex-spark", "claude-opus-4-8", "gemini-3-pro"]


def _sse(frames) -> str:
    return "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"


def _cpa(req: httpx.Request):
    if req.headers.get("authorization") != f"Bearer {KEY}":
        return httpx.Response(401, json={"error": {"message": "Missing API key"}})
    if req.url.path == "/v1/models":
        return httpx.Response(200, json={"object": "list", "data": [
            {"id": m, "object": "model", "created": 1, "owned_by": "openai"} for m in MODELS]})
    body = json.loads(req.content)
    SEEN.setdefault("bodies", []).append(body)
    summary = body.get("reasoning_effort") not in (None, "none")
    usage = {"prompt_tokens": 1200, "completion_tokens": 90, "total_tokens": 1290,
             "prompt_tokens_details": {"cached_tokens": 1024},
             "completion_tokens_details": {"reasoning_tokens": 40}}
    if body.get("stream"):
        frames = []
        if summary:
            frames.append({"choices": [{"index": 0, "delta": {"reasoning_content": "plan it"}}]})
        frames += [
            {"choices": [{"index": 0, "delta": {"content": "Reading."}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_C1",
              "type": "function", "function": {"name": "read_file", "arguments": "{\"path\":"}}]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {
              "arguments": " \"a.lua\"}"}}]}, "finish_reason": "tool_calls"}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], "usage": usage}]
        return httpx.Response(200, text=_sse(frames))
    return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": "Reading.",
                    "reasoning_content": "plan it" if summary else None,
                    "tool_calls": [{"id": "call_C1", "type": "function", "function": {
                        "name": "read_file", "arguments": "{\"path\": \"a.lua\"}"}}]}}],
        "usage": usage})


def _local(req: httpx.Request):
    if req.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "qwen-local"}]})
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": "local"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}})


def _router(req):
    return {"cpa-host": _cpa, "llm-host": _local}[req.url.host](req)


@pytest.fixture()
def cpa(app, client):
    svc = app.state.services
    SEEN.clear()
    http = httpx.AsyncClient(transport=httpx.MockTransport(_router))
    pool = InternalPool([
        BackendConfig(name="cliproxy", type="openai-compatible", url="http://cpa-host:8317/v1",
                      api_key=KEY, flavor="cliproxyapi"),
        BackendConfig(name="llama", type="openai-compatible", url="http://llm-host",
                      flavor="llamacpp")],
        BackendPoolConfig(), http, svc.db)
    pool.backends["cliproxy"].healthy = pool.backends["cliproxy"].ever_checked = True
    pool.backends["cliproxy"].models = list(MODELS)
    pool.backends["llama"].healthy = pool.backends["llama"].ever_checked = True
    pool.backends["llama"].models = ["qwen-local"]
    real, svc.pool = svc.pool, pool
    yield svc
    svc.pool = real


def test_flavor_marks_backend_as_cloud_subscription(cpa):
    info = cpa.pool.backend_info("gpt-5.5")
    assert info["cloud"] is True and info["flavor"] == "cliproxyapi"
    assert cpa.pool.backend_info("qwen-local")["cloud"] is False
    st = {b["name"]: b for b in cpa.pool.backend_status()}
    assert st["cliproxy"]["cloud"] is True and st["llama"]["cloud"] is False
    # explicit override for any other subscription proxy (e.g. ChatMock)
    assert BackendConfig(name="cm", type="openai-compatible", url="http://x",
                         cloud=True).is_cloud
    assert not BackendConfig(name="o", type="openai-compatible", url="http://x").is_cloud
    assert BackendConfig(name="m", type="anthropic-compatible", url="http://x").is_cloud


def test_model_discovery_with_bearer_key(cpa):
    import asyncio
    names = asyncio.run(cpa.pool.backends["cliproxy"].protocol.list_models())
    assert names == MODELS


def test_registry_gets_subscription_tier_and_context(cpa):
    cpa.register_discovered()
    gpt = cpa.registry.get("gpt-5.5")
    assert gpt["relative_cost_tier"] == "high" and gpt["context_length"] == 272_000
    assert (gpt.get("cost_per_1k_input") or 0) == 0
    assert cpa.registry.get("gpt-5.3-codex-spark")["relative_cost_tier"] == "medium"
    assert cpa.registry.get("claude-opus-4-8")["context_length"] == 200_000
    assert cpa.registry.get("gemini-3-pro")["context_length"] == 1_048_576


@pytest.mark.parametrize("effort,expected", [
    ("max", "xhigh"), ("xhigh", "xhigh"), ("high", "high"), ("medium", "medium"),
    ("low", "low"), (False, "none"), ("off", "none")])
def test_full_codex_effort_scale_is_sent(cpa, effort, expected):
    from foundry_router.pool.protocols import OpenAIProtocol
    proto = cpa.pool.backends["cliproxy"].protocol
    assert isinstance(proto, OpenAIProtocol)
    p = proto._payload("gpt-5.5", [{"role": "user", "content": "x"}], None,
                       {"top_k": 20, "min_p": 0.0, "temperature": 1.0}, 4096, effort, None)
    assert p["reasoning_effort"] == expected
    # strict cloud endpoint: no local-runner knobs or template kwargs
    assert "top_k" not in p and "min_p" not in p and "chat_template_kwargs" not in p
    assert p["temperature"] == 1.0


def test_plain_openai_flavor_keeps_standard_scale():
    from foundry_router import thinking
    assert thinking.openai_reasoning_effort("max") == "high"
    assert thinking.openai_reasoning_effort(False) is None
    assert "max" in thinking.supported_levels("gpt-5.5", backend_type="openai-compatible")
    assert "max" in thinking.supported_levels("gpt-6-astra", backend_type="openai-compatible")


def test_images_go_as_data_uri_parts(cpa):
    proto = cpa.pool.backends["cliproxy"].protocol
    p = proto._payload("gpt-5.5", [{"role": "user", "content": "see", "images": ["iVBORw0KGgo="]}],
                       None, {}, 1024, None, None)
    parts = p["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "see"}
    assert parts[1]["type"] == "image_url" and parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_paid_call_guardrail_counts_cliproxy(cpa):
    import asyncio
    from foundry_router.guardrails import RequestGuardState
    eff = {**cpa.guardrails.effective(None), "max_paid_calls_per_request": 0}
    v = asyncio.run(cpa.guardrails.check_paid_call(
        "gpt-5.5", cpa.pool.backend_info("gpt-5.5"), cpa.registry.get("gpt-5.5"),
        RequestGuardState(), eff))
    assert not v.allowed and "paid" in v.reason
    v = asyncio.run(cpa.guardrails.check_paid_call(
        "qwen-local", cpa.pool.backend_info("qwen-local"), cpa.registry.get("qwen-local"),
        RequestGuardState(), eff))
    assert v.allowed                                    # local stays free


def test_plan_persona_paid_cascade_uses_cliproxy_not_local_pins(cpa):
    from foundry_router.facade.ollama_api import _paid_pin_order
    cpa.personas.upsert("Plan", execution_mode="direct", local_bias_strength="prefer_paid",
                        model_allowlist=["gpt-5.5", "claude-opus-4-8", "qwen-local"],
                        pinned_models=["claude-opus-4-8", "qwen-local", "gpt-5.5"])
    assert _paid_pin_order(cpa, cpa.personas.get("Plan")) == ["claude-opus-4-8", "gpt-5.5"]


@pytest.mark.parametrize("stream", [True, False])
def test_end_to_end_tools_reasoning_usage_through_persona(cpa, client, stream):
    cpa.personas.upsert("GPT-Plan", execution_mode="direct", model_allowlist=["gpt-5.5"],
                        pinned_models=[])
    r = client.post("/api/chat", json={"model": "GPT-Plan", "stream": stream, "tools": TOOLS,
                                       "think": "xhigh",
                                       "messages": [{"role": "user", "content": "read a.lua"}]})
    assert r.status_code == 200, r.text
    objs = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    thinking = "".join(o["message"].get("thinking") or "" for o in objs)
    content = "".join(o["message"].get("content") or "" for o in objs)
    tcs = [tc for o in objs for tc in (o["message"].get("tool_calls") or [])]
    assert "plan it" in thinking and "Reading." in content
    assert [(t["function"]["name"], t["function"]["arguments"]) for t in tcs] == \
        [("read_file", {"path": "a.lua"})]
    assert objs[-1]["done"] is True and objs[-1]["prompt_eval_count"] >= 1200
    body = SEEN["bodies"][-1]
    assert body["model"] == "gpt-5.5" and body["reasoning_effort"] == "xhigh"
    assert body["tools"][0]["function"]["name"] == "read_file"


def test_compaction_summary_turns_reasoning_off(cpa, client):
    cpa.personas.upsert("GPT-Plan", execution_mode="direct", model_allowlist=["gpt-5.5"],
                        pinned_models=[])
    client.post("/api/chat", json={"model": "GPT-Plan", "stream": True, "messages": [
        {"role": "system", "content": "Summarize the provided coding session into a concise "
                                      "continuation note with detailed next steps."},
        {"role": "user", "content": "<conversation>…</conversation>"}]})
    assert SEEN["bodies"][-1]["reasoning_effort"] == "none"


def test_cost_report_lists_cliproxy_as_subscription(cpa, client):
    from foundry_router import telemetry
    from foundry_router.pool.protocols import ChatResult
    telemetry.record_call(cpa.db, cpa.registry, model="gpt-5.5", backend="cliproxy",
                          result=ChatResult(prompt_tokens=100, completion_tokens=10),
                          persona="", mode="direct", wall_ms=1000, max_tokens=0)
    d = client.get("/admin/api/cost-compare", params={"hours": 24}).json()
    rows = {r["model"]: r for r in d["by_model"]}
    assert rows["gpt-5.5"]["kind"] == "subscription"


def test_local_first_fallback_never_picks_cloud_as_local(cpa):
    from foundry_router.brain.fallback import fallback_candidates, pick_fallback_model
    cpa.personas.upsert("Act", execution_mode="direct", model_allowlist=[],
                        pinned_models=["gpt-5.5", "qwen-local"])
    persona = cpa.personas.get("Act")
    assert pick_fallback_model(cpa.pool, cpa.registry, persona, "fix the bug") == "qwen-local"
    cands = fallback_candidates(cpa.pool, cpa.registry, persona, "fix the bug", limit=6)
    assert cands[0] == "qwen-local"
    assert set(cands[1:]) <= set(MODELS)                # cloud only after local
