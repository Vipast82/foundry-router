"""Capture switches (W4): per-backend and per-persona include / exclude globs checked
in begin_call, calls outside a coordinator project captured when their persona is
switched on, the live-editable Training-tab card (POST /admin/api/training/capture),
counts for today, the captured-calls download, stream usage keeping cached_tokens,
and a `received` record for Ollama-dialect clients."""

import asyncio
import json

from foundry_router import configops
from foundry_router.config import TrainingCaptureConfig
from tests.test_budgets import STATE, ledgered  # noqa: F401 (fixture)
from tests.test_training_capture import COORD, _bind, _chat, _setup


def _calls(svc):
    return [r for r in svc.capture.records() if r["kind"] == "backend_call"]


def test_backend_include_and_exclude_globs(ledgered, client):
    _setup(ledgered, projects=["game"], exclude_backends=["strata"])
    _bind(client, "s1", "game")
    assert _chat(client, "s1").status_code == 200           # served by strata
    assert _calls(ledgered) == [] and ledgered.capture.records() == []   # no orphan `received`

    ledgered.config_store.config.training_capture = TrainingCaptureConfig(
        enabled=True, projects=["game"], backends=["cliproxy", "STRAT*"])
    assert _chat(client, "s1").status_code == 200
    calls = _calls(ledgered)
    assert [c["backend"] for c in calls] == ["strata"]       # globs are case-insensitive

    ledgered.config_store.config.training_capture = TrainingCaptureConfig(
        enabled=True, projects=["game"], backends=["cliproxy"])
    _chat(client, "s1")
    assert len(_calls(ledgered)) == 1                        # strata no longer listed


def test_persona_include_and_exclude_globs(ledgered, client):
    _setup(ledgered, projects=["game"], personas=["Foundry-Kilo-Sol"])
    _bind(client, "s1", "game")
    _chat(client, "s1")                                      # persona Foundry-Kilo-Local
    assert ledgered.capture.records() == []

    ledgered.config_store.config.training_capture = TrainingCaptureConfig(
        enabled=True, projects=["game"], exclude_personas=["foundry-kilo-*"])
    _chat(client, "s1")
    assert ledgered.capture.records() == []

    ledgered.config_store.config.training_capture = TrainingCaptureConfig(
        enabled=True, projects=["game"], personas=["Foundry-Kilo-Local"])
    _chat(client, "s1")
    kinds = [r["kind"] for r in ledgered.capture.records()]
    assert kinds == ["received", "backend_call"]
    assert {r["persona"] for r in ledgered.capture.records()} == {"Foundry-Kilo-Local"}


def test_unbound_calls_captured_when_persona_is_switched_on(ledgered, client):
    _setup(ledgered)                                         # include_unbound off
    _chat(client, "no-project")
    assert ledgered.capture.records() == []
    # a persona named in `personas` is captured with no coordinator project
    ledgered.config_store.config.training_capture = TrainingCaptureConfig(
        enabled=True, personas=["Foundry-Kilo-Local"])
    _chat(client, "no-project")
    recs = ledgered.capture.records()
    assert [r["kind"] for r in recs] == ["received", "backend_call"]
    assert recs[1]["project_id"] == "" and recs[1]["persona"] == "Foundry-Kilo-Local"
    # ...and only that persona
    ledgered.config_store.config.training_capture = TrainingCaptureConfig(
        enabled=True, personas=["Some-Other-Persona"])
    _chat(client, "no-project")
    assert len(ledgered.capture.records()) == 2


def test_training_capture_is_live_editable(ledgered, client):
    assert "training_capture" in configops._LIVE and "perf" in configops._LIVE
    ledgered.pool.capture = ledgered.capture
    _chat(client, "x")
    assert ledgered.capture._store is None or ledgered.capture.records() == []
    r = client.post("/admin/api/training/capture", json={
        "enabled": True, "include_unbound": True, "backends": ["strata"],
        "exclude_personas": ["Nothing"]})
    assert r.status_code == 200 and r.json()["training_capture"]["enabled"] is True
    cfg = ledgered.config_store.config.training_capture
    assert (cfg.include_unbound, cfg.backends, cfg.exclude_personas) == (True, ["strata"],
                                                                         ["Nothing"])
    _chat(client, "x")                                       # live: no restart
    assert [r["kind"] for r in ledgered.capture.records()] == ["received", "backend_call"]
    bad = client.post("/admin/api/training/capture", json={"backends": "strata"})
    assert bad.status_code == 400
    off = client.post("/admin/api/training/capture", json={"enabled": False})
    assert off.json()["training_capture"]["enabled"] is False
    _chat(client, "x")
    assert len(ledgered.capture.records()) == 2


def test_status_counts_today_and_options_for_the_card(ledgered, client):
    _setup(ledgered, projects=["game"])
    _bind(client, "s1", "game")
    _chat(client, "s1")
    _chat(client, "s1")
    v = client.get("/admin/api/training").json()
    today = v["capture"]["today"]
    assert today["backends"] == {"strata": 2} and today["personas"] == {"Foundry-Kilo-Local": 2}
    assert today["total"] == 2
    assert {"strata", "meridian", "cliproxy"} <= set(v["options"]["backends"])
    assert "Foundry-Kilo-Local" in v["options"]["personas"]
    assert v["capture"]["backends"] == [] and v["capture"]["exclude_personas"] == []


def test_download_captured_calls_jsonl(ledgered, client):
    _setup(ledgered, projects=["game"])
    _bind(client, "s1", "game")
    _chat(client, "s1")
    r = client.get("/admin/api/training/capture/download")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    lines = [json.loads(x) for x in r.text.splitlines()]
    assert len(lines) == 1
    ex = lines[0]
    assert ex["messages"][-1] == {"role": "assistant", "content": "ok"}
    assert ex["messages"][0]["role"] == "system" and ex["tools"][0]["function"]["name"] == "read_file"
    assert ex["meta"]["backend"] == "strata" and ex["meta"]["persona"] == "Foundry-Kilo-Local"
    assert "reasoning" not in json.dumps(ex)
    raw = [json.loads(x) for x in client.get(
        "/admin/api/training/capture/download?format=raw").text.splitlines()]
    assert raw[0]["kind"] == "backend_call" and raw[0]["body"]["response"]["content"] == "ok"
    assert client.get("/admin/api/training/capture/download?backend=meridian").text == ""
    assert client.get("/admin/api/training/capture/download?quality=bogus").status_code == 400


def test_stream_capture_keeps_cached_tokens(ledgered):
    from foundry_router import capture as C, client_auth
    _setup(ledgered)
    s = ledgered.pool.backends["strata"]

    async def fake_stream(model, messages, **kw):
        yield {"content": "Hi"}
        yield {"content": "", "done": True, "finish_reason": "stop", "prompt_tokens": 100,
               "completion_tokens": 4, "cached_tokens": 77}
    s.protocol.chat_stream = fake_stream

    async def run():
        tok = client_auth._client.set({"client_id": "kilo", "project_id": "game"})
        C.reset_request()
        try:
            async for _ in ledgered.pool.chat_stream("qwen-local",
                                                     [{"role": "user", "content": "hi"}]):
                pass
        finally:
            client_auth._client.reset(tok)
    asyncio.run(run())
    call = _calls(ledgered)[-1]
    assert call["body"]["response"]["usage"] == {"prompt_tokens": 100, "completion_tokens": 4,
                                                 "cached_tokens": 77}


def test_ollama_dialect_client_gets_a_received_record(ledgered, client):
    _setup(ledgered, projects=["game"])
    _bind(client, "s1", "game")
    r = client.post("/api/chat", headers={"Authorization": "Bearer kk", "x-session-id": "s1"},
                    json={"model": "Foundry-Kilo-Local", "stream": False,
                          "messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 200
    recs = ledgered.capture.records()
    assert [x["kind"] for x in recs] == ["received", "backend_call"]
    assert recs[0]["body"]["dialect"] == "ollama"
    assert recs[0]["body"]["request"]["messages"][0]["content"] == "hello"


def test_held_received_record_is_written_once_across_sibling_tasks(ledgered):
    from foundry_router import capture as C, client_auth
    _setup(ledgered, backends=["strata"])
    cap = ledgered.capture

    async def call(backend):
        tok = cap.begin_call(backend, "Foundry-Kilo-Local")
        if tok is not None:
            cap.finish_call(tok, model="m", backend=backend, messages=[], tools=None, settings={},
                            error="x", status="error")

    async def main():
        t = client_auth._client.set({"client_id": "kilo", "project_id": "game"})
        C.reset_request()
        try:
            cap.record_received({"model": "p", "messages": []}, "openai")
            assert cap.records() == []                         # held: no allowed call yet
            await asyncio.gather(asyncio.create_task(call("cliproxy")),      # filtered out
                                 asyncio.create_task(call("strata")),
                                 asyncio.create_task(call("strata")))
        finally:
            client_auth._client.reset(t)
    asyncio.run(main())
    kinds = [r["kind"] for r in cap.records()]
    assert kinds.count("received") == 1 and kinds.count("backend_call") == 2
