"""Strata serves /metrics as JSON unless the client asks for Prometheus text, and
uses vLLM-style metric names although it is configured as a llama.cpp flavor."""

import asyncio
import json

import httpx

from foundry_router.pool.protocols import OpenAIProtocol

PROM = """# HELP vllm:num_requests_running Requests running
vllm:num_requests_running{model_name="qwen3.8-flash-next-iq3_s"} 1
vllm:num_requests_waiting{model_name="qwen3.8-flash-next-iq3_s"} 0
vllm:kv_cache_usage_perc{model_name="qwen3.8-flash-next-iq3_s"} 0.42
vllm:prefix_cache_hits_total{model_name="qwen3.8-flash-next-iq3_s"} 900
vllm:prefix_cache_queries_total{model_name="qwen3.8-flash-next-iq3_s"} 1000
vllm:spec_decode_num_accepted_tokens_total 70
vllm:spec_decode_num_draft_tokens_total 100
strata:last_hit_rate 0.83
strata:live_tok_s 31.5
strata:ram_used_bytes 50
strata:ram_total_bytes 100
"""


def _strata(accept_prometheus: bool = True):
    def h(req: httpx.Request):
        if req.url.path == "/metrics":
            if accept_prometheus and "text/plain" in req.headers.get("accept", ""):
                return httpx.Response(200, text=PROM, headers={"content-type": "text/plain"})
            return httpx.Response(200, json={"live": {"state": "idle"}})
        return httpx.Response(404, text="not found")
    return httpx.AsyncClient(transport=httpx.MockTransport(h))


def test_strata_metrics_are_read_as_prometheus_with_vllm_names():
    proto = OpenAIProtocol("http://strata:34201/v1", None, _strata(), flavor="llamacpp")
    m = asyncio.run(proto.server_metrics())
    assert m["metrics_ok"] and m["metrics_dialect"] == "vllm"
    assert m["requests_running"] == 1 and m["kv_cache_usage_pct"] == 42.0
    assert m["prefix_cache_hit_pct"] == 90.0 and m["spec_accept_pct"] == 70.0
    assert m["engine"] == "strata" and m["expert_cache_hit_pct"] == 83.0
    assert m["avg_decode_tps"] == 31.5 and m["ram_used_pct"] == 50.0


def test_json_only_metrics_say_why_they_were_rejected():
    proto = OpenAIProtocol("http://strata:34201/v1", None, _strata(accept_prometheus=False),
                           flavor="llamacpp")
    m = asyncio.run(proto.server_metrics())
    assert not m["metrics_ok"]
    assert "not Prometheus text" in m["metrics_error"] and "application/json" in m["metrics_error"]
