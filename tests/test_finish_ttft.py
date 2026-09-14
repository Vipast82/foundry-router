"""Truncation + time-to-first-token telemetry. A "length" finish_reason means the
reply was cut at the max-token cap — the direct cause of a client (Cline) asking to
"continue" — so it's captured across every backend and counted. TTFT (streaming) is
the prefill-dominated latency the operator feels."""

import httpx
import pytest

from foundry_router.db import Database
from foundry_router.pool.protocols import (AnthropicProtocol, OllamaProtocol,
                                           OpenAIProtocol)
from foundry_router.registry.models_db import ModelRegistry


# -- finish_reason normalized across backends --------------------------------------

async def test_ollama_finish_reason_length():
    def h(req):
        return httpx.Response(200, json={
            "message": {"content": "..."}, "done": True, "done_reason": "length",
            "eval_count": 32768, "prompt_eval_count": 100})
    proto = OllamaProtocol("http://x", None,
                           httpx.AsyncClient(transport=httpx.MockTransport(h)))
    res = await proto.chat("m", [{"role": "user", "content": "hi"}])
    assert res.finish_reason == "length"


async def test_openai_finish_reason_from_choice():
    def h(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "x"}, "finish_reason": "length"}],
            "usage": {"completion_tokens": 32768}})
    proto = OpenAIProtocol("http://x", None,
                           httpx.AsyncClient(transport=httpx.MockTransport(h)),
                           flavor="llamacpp")
    res = await proto.chat("m", [{"role": "user", "content": "hi"}])
    assert res.finish_reason == "length"


async def test_openai_stream_finish_reason():
    stream = (
        'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}]}\n'
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n'
        'data: [DONE]\n')
    proto = OpenAIProtocol("http://x", None,
                           httpx.AsyncClient(transport=httpx.MockTransport(
                               lambda r: httpx.Response(200, text=stream))),
                           flavor="llamacpp")
    chunks = [c async for c in proto.chat_stream("m", [{"role": "user", "content": "hi"}])]
    assert chunks[-1]["finish_reason"] == "length"


async def test_anthropic_max_tokens_maps_to_length():
    def h(req):
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "x"}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 1, "output_tokens": 4096}})
    proto = AnthropicProtocol("http://m", "k",
                              httpx.AsyncClient(transport=httpx.MockTransport(h)))
    res = await proto.chat("claude-opus-5", [{"role": "user", "content": "hi"}])
    assert res.finish_reason == "length"        # Anthropic "max_tokens" normalized


# -- registry: truncation counter + last finish -----------------------------------

def test_note_finish_counts_truncations(tmp_path):
    db = Database(tmp_path / "m.sqlite")
    reg = ModelRegistry(db)
    reg.note_finish("m", "length")
    reg.note_finish("m", "length")
    reg.note_finish("m", "stop")       # not a truncation
    row = reg.get("m")
    assert row["truncations"] == 2
    assert row["last_finish_reason"] == "stop"


def test_note_finish_ignores_empty(tmp_path):
    db = Database(tmp_path / "m.sqlite")
    reg = ModelRegistry(db)
    reg.note_finish("m", "")
    assert reg.get("m") is None          # nothing recorded, no row created


# -- registry: TTFT rolling mean + last -------------------------------------------

def test_note_ttft_rolling_mean(tmp_path):
    db = Database(tmp_path / "m.sqlite")
    reg = ModelRegistry(db)
    reg.note_ttft("m", 1000.0)
    reg.note_ttft("m", 3000.0)
    row = reg.get("m")
    assert round(row["ttft_ms_avg"]) == 2000     # mean of 1000 and 3000
    assert round(row["last_ttft_ms"]) == 3000
    assert row["ttft_samples"] == 2


def test_note_ttft_ignores_nonpositive(tmp_path):
    db = Database(tmp_path / "m.sqlite")
    reg = ModelRegistry(db)
    reg.note_ttft("m", 0)
    reg.note_ttft("m", -5)
    assert reg.get("m") is None
