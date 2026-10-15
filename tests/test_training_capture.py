"""Foundry's opt-in inference capture (capture.py): what is captured, what
isn't, redaction before anything is written, tool definitions and results,
the actual backend/model, and streaming reconstruction status."""

import asyncio
import json
import os

from foundry_router.config import ClientConfig, TrainingCaptureConfig
from tests.test_budgets import STATE, ledgered  # noqa: F401 (fixture)

COORD = {"Authorization": "Bearer coord-key"}
FAKE_KEY = "sk-ant-TESTONLY0000000000000000000000"          # synthetic, matches a pattern
FAKE_DB_PASSWORD = "hunter2-synthetic-pw"


def _setup(svc, **cfg):
    svc.config_store.config.clients.append(
        ClientConfig(name="coordinator", key="coord-key", scopes=["orchestration", "admin"]))
    svc.config_store.config.training_capture = TrainingCaptureConfig(enabled=True, **cfg)
    svc.pool.capture = svc.capture          # the fixture's pool (production sets it on build)


def _bind(client, sid, project):
    assert client.post("/v1/foundry/work/sessions", headers=COORD, json={
        "session_id": sid, "project_id": project, "task_id": "T1", "run_id": "T1-a1",
        "role": "implementer"}).status_code == 200


TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "read",
                                           "parameters": {"type": "object", "properties": {
                                               "path": {"type": "string"}}}}}]


def _chat(client, sid, extra_msgs=()):
    return client.post("/v1/chat/completions", headers={
        "Authorization": "Bearer kk", "x-session-id": sid, "Cookie": "session=abc123secret"},
        json={"model": "Foundry-Kilo-Local", "stream": False, "tools": TOOLS, "temperature": 0.2,
              "messages": [{"role": "system", "content": "You are a coder."},
                           {"role": "user", "content": f"key {FAKE_KEY} password={FAKE_DB_PASSWORD}"},
                           *extra_msgs]})


def test_capture_records_received_and_effective_backend_call(ledgered, client):
    _setup(ledgered, projects=["game"])
    _bind(client, "ses_1", "game")
    tool_turn = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_7", "type": "function", "function": {"name": "read_file",
                                                          "arguments": '{"path": "a.lua"}'}}]},
        {"role": "tool", "tool_call_id": "call_7", "content": "local x = 1"}]
    assert _chat(client, "ses_1", tool_turn).status_code == 200
    r = client.get("/v1/foundry/capture/records", headers=COORD).json()
    kinds = [x["kind"] for x in r["records"]]
    assert kinds == ["received", "backend_call"]
    rec, call = r["records"]
    assert rec["project_id"] == "game" and rec["session_id"] == "ses_1" and rec["run_id"] == "T1-a1"
    req = rec["body"]["request"]
    assert rec["body"]["dialect"] == "openai" and req["tools"][0]["function"]["name"] == "read_file"
    assert req["messages"][2]["tool_calls"][0]["id"] == "call_7"
    assert req["messages"][3]["tool_call_id"] == "call_7" and req["temperature"] == 0.2
    # the effective call: actual backend + model, Foundry-processed messages, tools, settings
    assert (call["backend"], call["model"], call["persona"]) == ("strata", "qwen-local",
                                                                 "Foundry-Kilo-Local")
    b = call["body"]
    assert b["request"]["tools"] and b["response"]["content"] == "ok"
    assert b["response"]["finish_reason"] == "stop" and b["response"]["usage"]["prompt_tokens"] == 120
    assert any(m.get("role") == "tool" for m in b["request"]["messages"])
    assert "not the provider's hidden prompt" in call["boundary"]
    # redacted BEFORE it was written: neither secret nor the cookie is in the file
    blob = open(ledgered.capture.store.path, "rb").read().decode("latin-1")
    for leak in (FAKE_KEY, FAKE_DB_PASSWORD, "abc123secret", "Bearer kk"):
        assert leak not in blob, leak
        assert leak not in json.dumps(r)
    assert rec["redaction"]["status"] == "redacted"
    assert {"anthropic-key", "assignment"} <= set(rec["redaction"]["kinds"])
    assert oct(os.stat(ledgered.capture.store.path).st_mode)[-3:] == "600"


def test_capture_is_opt_in_and_respects_project_scope(ledgered, client):
    _bind_cfg = ledgered.config_store.config
    _bind_cfg.clients.append(ClientConfig(name="coordinator", key="coord-key",
                                          scopes=["orchestration"]))
    ledgered.pool.capture = ledgered.capture
    _bind(client, "ses_a", "game")
    _chat(client, "ses_a")                                    # capture off: nothing
    assert ledgered.capture._store is None or ledgered.capture.records() == []
    ledgered.config_store.config.training_capture = TrainingCaptureConfig(
        enabled=True, exclude_projects=["secret-project"])
    _bind(client, "ses_b", "secret-project")
    _chat(client, "ses_b")                                    # excluded project
    _chat(client, "ses_unbound")                              # unbound, include_unbound off
    _chat(client, "ses_a")                                    # bound, allowed
    recs = ledgered.capture.records()
    assert {r["session_id"] for r in recs} == {"ses_a"}
    st = client.get("/admin/api/training").json()["capture"]
    assert st["enabled"] and st["counts"]


def test_failed_attempt_and_stream_reconstruction_status(ledgered, client):
    _setup(ledgered)
    _bind(client, "ses_s", "game")
    STATE["fail"] = ("strata",)
    _chat(client, "ses_s")
    call = [r for r in ledgered.capture.records() if r["kind"] == "backend_call"][-1]
    assert call["status"] == "error" and call["body"]["error"] and not call["body"]["response"]
    STATE.pop("fail")

    # streaming: a complete stream and one cut off by the client
    from foundry_router import client_auth, capture as C
    pool = ledgered.pool
    s = pool.backends["strata"]

    async def fake_stream(model, messages, **kw):
        yield {"content": "Hel"}
        yield {"tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "x"}}]}
        yield {"content": "lo", "done": True, "finish_reason": "tool_calls",
               "prompt_tokens": 9, "completion_tokens": 3}
    s.protocol.chat_stream = fake_stream

    async def run(cut):
        tok = client_auth._client.set({"client_id": "kilo", "session_id": "ses_s",
                                       "project_id": "game"})
        C.reset_request()
        try:
            agen = pool.chat_stream("qwen-local", [{"role": "user", "content": "hi"}])
            async for _ in agen:
                if cut:
                    break
            await agen.aclose()
        finally:
            client_auth._client.reset(tok)
    asyncio.run(run(False))
    asyncio.run(run(True))
    calls = [r for r in ledgered.capture.records() if r["kind"] == "backend_call" and r["stream"]]
    full, cut = calls[-2], calls[-1]
    assert full["reconstruction"] == "complete" and full["status"] == "ok"
    assert full["body"]["response"]["content"] == "Hello"
    assert full["body"]["response"]["tool_calls"][0]["id"] == "c1"
    assert cut["reconstruction"] == "incomplete" and cut["status"] == "cancelled"
