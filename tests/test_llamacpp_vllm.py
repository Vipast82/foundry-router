"""llama.cpp + vLLM backend depth: accurate prefill/cache accounting, the full
per-flavor sampling surface, structured output, vLLM context-overflow retry,
router-estimated stream timings, engine /metrics normalization, llama-swap /
vLLM model discovery, and the Prometheus parser."""

import json

import httpx

from foundry_router import perf_history as ph
from foundry_router.db import Database
from foundry_router.pool import prom
from foundry_router.pool.protocols import ChatResult, OpenAIProtocol, _fit_max_tokens
from foundry_router.registry.models_db import ModelRegistry

_SEEN: list = []


def _client(handler):
    def _h(request):
        try:
            _SEEN.append(json.loads(request.content)) if request.content else None
        except Exception:
            pass
        return handler(request)
    return httpx.AsyncClient(transport=httpx.MockTransport(_h))


def _ok(request):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2}})


async def _drain(agen):
    return [c async for c in agen]


# -- llama.cpp accounting ------------------------------------------------------------

async def test_prefill_uses_processed_tokens_and_cache_n_fallback():
    # 100-token prompt, 90 reused from cache, only 10 actually prefilled. The
    # prefill rate must divide 10 (timings.prompt_n) — not 100 — by prompt_ms.
    def h(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 4},
            "timings": {"prompt_n": 10, "prompt_ms": 20.0, "cache_n": 90,
                        "predicted_n": 4, "predicted_ms": 40.0}})
    proto = OpenAIProtocol("http://x", None, _client(h), flavor="llamacpp")
    res = await proto.chat("m", [{"role": "user", "content": "hi"}])
    assert res.prompt_tokens == 100
    assert res.prefill_tokens == 10
    assert res.cached_tokens == 90                 # from timings.cache_n
    assert res.timing_source == "server"


def test_record_sample_prefill_rate_uses_prefill_tokens(tmp_path):
    db = Database(tmp_path / "p.sqlite")
    ph.record_sample(db, model="m", prompt_tokens=100, prefill_tokens=10,
                     completion_tokens=4, prompt_eval_duration_ns=20_000_000,
                     eval_duration_ns=40_000_000, cached_tokens=90)
    r = db.query("SELECT * FROM perf_samples")[0]
    assert round(r["prefill_tps"]) == 500          # 10 / 0.02s, not 100 / 0.02s
    assert r["cache_hit_pct"] == 90.0
    assert r["prefill_tokens"] == 10 and r["timing_src"] == "server"


def test_note_inference_prefill_count(tmp_path):
    reg = ModelRegistry(Database(tmp_path / "r.sqlite"))
    reg.note_inference("m", 4, 40_000_000, prompt_count=100,
                       prompt_eval_duration_ns=20_000_000, prefill_count=10)
    assert round(reg.get("m")["prompt_tps_avg"]) == 500


def test_done_frame_roundtrip():
    r = ChatResult(prompt_tokens=100, completion_tokens=4, eval_duration_ns=5,
                   prompt_eval_duration_ns=6, draft_n=3, draft_n_accepted=2,
                   cached_tokens=90, prefill_tokens=10, reasoning_tokens=1,
                   timing_source="server", finish_reason="length",
                   tool_calls=[{"id": "a", "name": "t", "arguments": {}}])
    back = ChatResult.from_done_frame(r.done_frame())
    for k in ("prompt_tokens", "completion_tokens", "eval_duration_ns",
              "prompt_eval_duration_ns", "draft_n", "draft_n_accepted",
              "cached_tokens", "prefill_tokens", "reasoning_tokens",
              "timing_source", "finish_reason"):
        assert getattr(back, k) == getattr(r, k), k
    assert back.tool_calls[0]["name"] == "t"


# -- per-flavor sampling ---------------------------------------------------------------

async def test_llamacpp_gets_full_sampler_set_and_aliases():
    _SEEN.clear()
    proto = OpenAIProtocol("http://x", None, _client(_ok), flavor="llamacpp")
    opts = {"dry_multiplier": 0.8, "xtc_probability": 0.5, "top_n_sigma": 1.0,
            "repetition_penalty": 1.07, "num_keep": 32, "cache_prompt": True,
            "min_tokens": 5}
    await proto.chat("m", [{"role": "user", "content": "hi"}], options=opts)
    p = _SEEN[-1]
    assert p["dry_multiplier"] == 0.8 and p["xtc_probability"] == 0.5
    assert p["top_n_sigma"] == 1.0 and p["cache_prompt"] is True
    assert p["repeat_penalty"] == 1.07             # alias -> llama.cpp spelling
    assert p["n_keep"] == 32
    assert "repetition_penalty" not in p and "min_tokens" not in p   # vLLM-only


async def test_vllm_gets_its_params_not_llamacpp_ones():
    _SEEN.clear()
    proto = OpenAIProtocol("http://x", None, _client(_ok), flavor="vllm")
    opts = {"repeat_penalty": 1.1, "min_tokens": 8, "top_k": 20, "min_p": 0.05,
            "dry_multiplier": 0.8, "mirostat": 2, "guided_regex": "a+"}
    await proto.chat("m", [{"role": "user", "content": "hi"}], options=opts)
    p = _SEEN[-1]
    assert p["repetition_penalty"] == 1.1          # alias -> vLLM spelling
    assert p["min_tokens"] == 8 and p["top_k"] == 20 and p["min_p"] == 0.05
    assert p["guided_regex"] == "a+"
    assert "dry_multiplier" not in p and "mirostat" not in p and "repeat_penalty" not in p


async def test_chat_template_kwargs_merge_with_thinking_off():
    _SEEN.clear()
    proto = OpenAIProtocol("http://x", None, _client(_ok), flavor="vllm")
    await proto.chat("m", [{"role": "user", "content": "hi"}], think=False,
                     options={"chat_template_kwargs": {"custom": 1}})
    assert _SEEN[-1]["chat_template_kwargs"] == {"custom": 1, "enable_thinking": False}


async def test_bare_json_schema_is_wrapped():
    # llama.cpp and vLLM read json_schema.schema — a bare schema there
    # constrained nothing.
    _SEEN.clear()
    proto = OpenAIProtocol("http://x", None, _client(_ok), flavor="llamacpp")
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    await proto.chat("m", [{"role": "user", "content": "hi"}], fmt=schema)
    rf = _SEEN[-1]["response_format"]
    assert rf == {"type": "json_schema",
                  "json_schema": {"name": "response", "schema": schema}}


# -- vLLM context-overflow retry -------------------------------------------------------

def test_fit_max_tokens_parses_vllm_errors():
    old = ("This model's maximum context length is 32768 tokens. However, you "
           "requested 40000 tokens (31808 in the messages, 8192 in the completion).")
    assert _fit_max_tokens(old, 8192) == 32768 - 31808 - 16
    new = ("'max_tokens' or 'max_completion_tokens' is too large: 8192. This model's "
           "maximum context length is 32768 tokens and your request has 30000 input "
           "tokens (8192 > 32768 - 30000).")
    assert _fit_max_tokens(new, 8192) == 32768 - 30000 - 16
    # prompt alone overflows -> nothing to fit
    assert _fit_max_tokens("maximum context length is 1000 tokens. (1200 in the "
                           "messages, 10 in the completion)", 10) is None
    assert _fit_max_tokens("some other 400", 8192) is None


async def test_vllm_overflow_retries_with_fitted_budget():
    _SEEN.clear()
    calls = []

    def h(request):
        body = json.loads(request.content)
        calls.append(body["max_tokens"])
        if len(calls) == 1:
            return httpx.Response(400, json={"message": (
                "This model's maximum context length is 32768 tokens. However, you "
                "requested 40000 tokens (31808 in the messages, 8192 in the completion).")})
        return _ok(request)
    proto = OpenAIProtocol("http://x", None, _client(h), flavor="vllm")
    res = await proto.chat("m", [{"role": "user", "content": "hi"}], max_tokens=8192)
    assert res.content == "hi"
    assert calls == [8192, 32768 - 31808 - 16]


# -- estimated stream timings + reasoning tokens ---------------------------------------

async def test_vllm_stream_gets_estimated_timings_and_reasoning_tokens():
    sse = ('data: {"choices":[{"delta":{"reasoning_content":"hmm"}}]}\n'
           'data: {"choices":[{"delta":{"content":"a"}}]}\n'
           'data: {"choices":[{"delta":{"content":"b"},"finish_reason":"stop"}]}\n'
           'data: {"choices":[],"usage":{"prompt_tokens":50,"completion_tokens":3,'
           '"prompt_tokens_details":{"cached_tokens":20},'
           '"completion_tokens_details":{"reasoning_tokens":1}}}\n'
           'data: [DONE]\n')
    proto = OpenAIProtocol("http://x", None,
                           _client(lambda r: httpx.Response(200, text=sse)), flavor="vllm")
    chunks = await _drain(proto.chat_stream("m", [{"role": "user", "content": "hi"}]))
    done = chunks[-1]
    assert done["timing_source"] == "estimated"
    assert done["eval_duration_ns"] > 0 and done["prompt_eval_duration_ns"] > 0
    assert done["prefill_tokens"] == 30             # prompt minus cached
    assert done["reasoning_tokens"] == 1
    assert done["cached_tokens"] == 20


# -- model discovery -------------------------------------------------------------------

async def test_vllm_loaded_detail_and_context_from_max_model_len():
    def h(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "Qwen/Qwen3-32B-AWQ", "max_model_len": 32768}]})
        return httpx.Response(404)
    proto = OpenAIProtocol("http://x", None, _client(h), flavor="vllm")
    assert await proto.loaded_models_detail() == [
        {"model": "Qwen/Qwen3-32B-AWQ", "size_vram": 0, "size": 0, "context": 32768}]
    assert await proto.show_context_length("Qwen/Qwen3-32B-AWQ") == 32768


async def test_llama_swap_running_models_are_the_loaded_set():
    def h(request):
        p = request.url.path
        if p == "/running":
            return httpx.Response(200, json={"running": [
                {"model": "qwen-coder", "state": "ready"}]})
        if p.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "qwen-coder", "meta": {"size": 17 * 2**30, "n_ctx_train": 131072}},
                {"id": "gemma"}]})
        raise AssertionError(f"unexpected probe {p}")   # never /props on swap
    proto = OpenAIProtocol("http://x", None, _client(h), flavor="llamacpp")
    detail = await proto.loaded_models_detail()
    assert detail == [{"model": "qwen-coder", "size_vram": 0, "size": 17 * 2**30,
                       "context": 0, "state": "ready"}]
    assert await proto.show_context_length("qwen-coder") == 131072


async def test_llamacpp_single_model_context_and_capabilities_from_props():
    def h(request):
        p = request.url.path
        if p.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m.gguf"}]})
        if p == "/props":
            return httpx.Response(200, json={
                "total_slots": 2, "build_info": "b6500-abc",
                "modalities": {"vision": True},
                "chat_template_caps": {"supports_tools": True},
                "default_generation_settings": {"n_ctx": 65536}})
        return httpx.Response(404)
    proto = OpenAIProtocol("http://x", None, _client(h), flavor="llamacpp")
    assert await proto.show_context_length("m.gguf") == 65536
    assert await proto.show_capabilities("m.gguf") == ["completion", "vision", "tools"]
    assert await proto.server_version() == "b6500-abc"
    detail = await proto.loaded_models_detail()
    assert detail[0]["context"] == 65536 and detail[0]["slots"] == 2


# -- engine metrics --------------------------------------------------------------------

LLAMA_METRICS = """# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed.
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 12000
llamacpp:prompt_seconds_total 6
llamacpp:tokens_predicted_total 3000
llamacpp:tokens_predicted_seconds_total 60
llamacpp:kv_cache_usage_ratio 0.42
llamacpp:kv_cache_tokens 27000
llamacpp:requests_processing 1
llamacpp:requests_deferred 2
llamacpp:n_busy_slots_per_decode 1.5
"""

VLLM_METRICS = """# HELP vllm:num_requests_running x
vllm:num_requests_running{model_name="q"} 3.0
vllm:num_requests_waiting{model_name="q"} 0.0
vllm:kv_cache_usage_perc{model_name="q"} 0.8
vllm:prefix_cache_queries_total{model_name="q"} 1000.0
vllm:prefix_cache_hits_total{model_name="q"} 600.0
vllm:num_preemptions_total{model_name="q"} 4.0
vllm:time_to_first_token_seconds_sum{model_name="q"} 10.0
vllm:time_to_first_token_seconds_count{model_name="q"} 20.0
vllm:inter_token_latency_seconds_sum{model_name="q"} 2.0
vllm:inter_token_latency_seconds_count{model_name="q"} 100.0
vllm:request_success_total{finished_reason="stop",model_name="q"} 18.0
vllm:request_success_total{finished_reason="length",model_name="q"} 2.0
vllm:spec_decode_num_draft_tokens_total{model_name="q"} 200.0
vllm:spec_decode_num_accepted_tokens_total{model_name="q"} 150.0
vllm:gpu_prefix_cache_hit_rate{model_name="q"} NaN
"""


def test_prom_summarize_llamacpp():
    s = prom.summarize("llamacpp", prom.parse(LLAMA_METRICS))
    assert s["kv_cache_usage_pct"] == 42.0
    assert s["requests_running"] == 1 and s["requests_waiting"] == 2
    assert s["avg_prompt_tps"] == 2000.0 and s["avg_decode_tps"] == 50.0
    assert s["busy_slots_per_decode"] == 1.5


def test_prom_summarize_vllm():
    s = prom.summarize("vllm", prom.parse(VLLM_METRICS))
    assert s["requests_running"] == 3 and s["requests_waiting"] == 0
    assert s["kv_cache_usage_pct"] == 80.0
    assert s["prefix_cache_hit_pct"] == 60.0
    assert s["preemptions_total"] == 4
    assert s["mean_ttft_ms"] == 500.0 and s["mean_tpot_ms"] == 20.0
    assert s["avg_decode_tps"] == 50.0
    assert s["truncated_total"] == 2 and s["requests_finished_total"] == 20
    assert s["spec_accept_pct"] == 75.0


def test_summarize_slots_both_shapes():
    s = prom.summarize_slots([{"id": 0, "is_processing": True, "n_ctx": 8192,
                               "next_token": [{"n_decoded": 12}]},
                              {"id": 1, "is_processing": False, "n_ctx": 8192}])
    assert s["slots_total"] == 2 and s["slots_busy"] == 1
    assert s["slots"][0]["n_decoded"] == 12
    old = prom.summarize_slots([{"id": 0, "state": 1}, {"id": 1, "state": 0}])
    assert old["slots_busy"] == 1


async def test_server_metrics_llamacpp_combines_metrics_and_slots():
    def h(request):
        p = request.url.path
        if p == "/metrics":
            return httpx.Response(200, text=LLAMA_METRICS)
        if p == "/slots":
            return httpx.Response(200, json=[{"id": 0, "is_processing": True}])
        return httpx.Response(404)
    proto = OpenAIProtocol("http://x", None, _client(h), flavor="llamacpp")
    m = await proto.server_metrics()
    assert m["metrics_ok"] and m["kv_cache_usage_pct"] == 42.0
    assert m["slots_total"] == 1 and m["slots_busy"] == 1


async def test_server_metrics_llamacpp_without_metrics_flag():
    def h(request):
        if request.url.path == "/slots":
            return httpx.Response(200, json=[{"id": 0, "is_processing": False}])
        return httpx.Response(501, text="metrics disabled")
    proto = OpenAIProtocol("http://x", None, _client(h), flavor="llamacpp")
    m = await proto.server_metrics()
    assert m["metrics_ok"] is False and "501" in m["metrics_error"]
    assert m["slots_total"] == 1


async def test_server_metrics_none_for_strict_openai():
    proto = OpenAIProtocol("http://x", None, _client(lambda r: httpx.Response(404)),
                           flavor="openai")
    assert await proto.server_metrics() is None
