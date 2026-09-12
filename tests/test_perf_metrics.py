"""Backend-agnostic performance metrics: decode + prefill tok/s and cold-load,
computed identically from Ollama's ns timings and llama.cpp's `timings` object,
so the numbers are comparable no matter which backend answered (the user may
switch between Ollama and llama.cpp freely)."""

import httpx
import pytest

from foundry_router.db import Database
from foundry_router.pool.protocols import OpenAIProtocol, _llamacpp_timings
from foundry_router.registry.models_db import ModelRegistry


# -- llama.cpp timings normalize to the same ns fields Ollama uses -----------------

def test_llamacpp_timings_to_ns():
    tm = _llamacpp_timings({"prompt_n": 1000, "prompt_ms": 500.0,
                            "predicted_n": 200, "predicted_ms": 4000.0})
    assert tm["prompt_eval_duration_ns"] == 500_000_000     # 500ms -> ns
    assert tm["eval_duration_ns"] == 4_000_000_000          # 4000ms -> ns
    assert tm["prompt_n"] == 1000 and tm["predicted_n"] == 200


def test_llamacpp_timings_missing_is_zero():
    tm = _llamacpp_timings(None)
    assert tm == {"prompt_eval_duration_ns": 0, "eval_duration_ns": 0,
                  "prompt_n": 0, "predicted_n": 0}


async def test_openai_chat_captures_llamacpp_timings():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200},
            "timings": {"prompt_n": 1000, "prompt_ms": 500.0,
                        "predicted_n": 200, "predicted_ms": 4000.0}})
    proto = OpenAIProtocol("http://x", None,
                           httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                           flavor="llamacpp")
    res = await proto.chat("qwen", [{"role": "user", "content": "hi"}])
    # decode = 200 tok / 4.0s = 50 tok/s; prefill = 1000 / 0.5s = 2000 tok/s
    assert res.eval_duration_ns == 4_000_000_000
    assert res.prompt_eval_duration_ns == 500_000_000
    assert res.completion_tokens == 200 and res.prompt_tokens == 1000


async def test_openai_stream_captures_timings_on_tail():
    stream = (
        'data: {"choices":[{"delta":{"content":"ok"}}]}\n'
        'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":1000,"completion_tokens":200},'
        '"timings":{"prompt_n":1000,"prompt_ms":500.0,"predicted_n":200,"predicted_ms":4000.0}}\n'
        'data: [DONE]\n')
    proto = OpenAIProtocol("http://x", None,
                           httpx.AsyncClient(transport=httpx.MockTransport(
                               lambda req: httpx.Response(200, text=stream))),
                           flavor="llamacpp")
    chunks = [c async for c in proto.chat_stream("qwen", [{"role": "user", "content": "hi"}])]
    done = chunks[-1]
    assert done["eval_duration_ns"] == 4_000_000_000
    assert done["prompt_eval_duration_ns"] == 500_000_000
    assert done["prompt_tokens"] == 1000 and done["completion_tokens"] == 200


# -- registry records decode + prefill + last-call snapshot ------------------------

def test_note_inference_records_decode_and_prefill(tmp_path):
    db = Database(tmp_path / "m.sqlite")
    reg = ModelRegistry(db)
    # 200 out tokens in 4.0s -> 50 tok/s decode; 1000 prompt tokens in 0.5s -> 2000 prefill
    reg.note_inference("qwen", eval_count=200, eval_duration_ns=4_000_000_000,
                       load_duration_ns=6_000_000_000,
                       prompt_count=1000, prompt_eval_duration_ns=500_000_000)
    row = reg.get("qwen")
    assert round(row["eval_tps_avg"]) == 50
    assert round(row["prompt_tps_avg"]) == 2000
    assert round(row["cold_load_ms_avg"]) == 6000
    # last-call snapshot
    assert round(row["last_eval_tps"]) == 50
    assert round(row["last_prompt_tps"]) == 2000
    assert row["last_prompt_tokens"] == 1000 and row["last_eval_tokens"] == 200
    assert row["last_inference_at"]


def test_note_inference_prefill_is_independent_of_decode(tmp_path):
    # A backend that reports only decode timing (no prompt-eval duration) must
    # still record decode, and leave prefill untouched.
    db = Database(tmp_path / "m.sqlite")
    reg = ModelRegistry(db)
    reg.note_inference("m", eval_count=100, eval_duration_ns=2_000_000_000)
    row = reg.get("m")
    assert round(row["eval_tps_avg"]) == 50
    assert (row["prompt_tps_avg"] or 0) == 0        # nothing recorded for prefill


def test_note_inference_warm_call_keeps_prior_cold_load(tmp_path):
    db = Database(tmp_path / "m.sqlite")
    reg = ModelRegistry(db)
    reg.note_inference("m", 200, 4_000_000_000, load_duration_ns=6_000_000_000,
                       prompt_count=1000, prompt_eval_duration_ns=500_000_000)
    # a later WARM call (no load_duration) must not wipe the last cold-load figure
    reg.note_inference("m", 100, 2_000_000_000, prompt_count=500,
                       prompt_eval_duration_ns=250_000_000)
    row = reg.get("m")
    assert round(row["last_cold_load_ms"]) == 6000    # preserved via COALESCE
