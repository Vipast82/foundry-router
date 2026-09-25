"""Prometheus /metrics parsing + normalization for local inference engines.

llama.cpp (`llama-server --metrics`) and vLLM both expose engine-level telemetry
in the Prometheus text format, but under different names and shapes (llama.cpp
mostly gauges + lifetime counters; vLLM counters + histograms, renamed across
releases). Per-request numbers (tok/s of one call) come from the response
itself — this is the SERVER-WIDE view the per-call data can't give: how full
the KV cache is, how many requests are queued behind the one you're watching,
how busy the slots are, preemptions, lifetime throughput.

`summarize()` maps both engines onto one small, stable dict so the Live view
renders a single "Inference servers" card for either. Every key is optional:
a metric a given build doesn't export is simply absent, never a fake zero.
"""

from __future__ import annotations

import re
from typing import Optional

_LINE_RE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)')
_LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse(text: str) -> dict[str, list[tuple[dict, float]]]:
    """Prometheus text exposition -> {metric_name: [(labels, value), ...]}.
    Comments/HELP/TYPE lines and unparseable samples are skipped."""
    out: dict[str, list[tuple[dict, float]]] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        try:
            val = float(m.group(3))
        except ValueError:
            continue
        if val != val:          # NaN (vLLM exports NaN gauges before first request)
            continue
        labels = dict(_LABEL_RE.findall(m.group(2) or ""))
        out.setdefault(m.group(1), []).append((labels, val))
    return out


def _sum(metrics: dict, *names: str, where: Optional[dict] = None) -> Optional[float]:
    """Sum of the first metric name present (across label sets, e.g. one per
    model/engine), optionally filtered by label values. None when absent."""
    for name in names:
        samples = metrics.get(name)
        if samples is None:
            continue
        vals = [v for labels, v in samples
                if not where or all(labels.get(k) == w for k, w in where.items())]
        if vals:
            return sum(vals)
    return None


def _hist_mean_ms(metrics: dict, *bases: str) -> Optional[float]:
    """Mean of a Prometheus histogram in ms (…_sum / …_count), first base found."""
    for base in bases:
        s, c = _sum(metrics, base + "_sum"), _sum(metrics, base + "_count")
        if s is not None and c:
            return round(1000.0 * s / c, 1)
    return None


def _pct(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or not den:
        return None
    return round(100.0 * num / den, 1)


def _r(v: Optional[float], nd: int = 1) -> Optional[float]:
    return None if v is None else round(v, nd)


def summarize(flavor: str, m: dict) -> dict:
    """Normalize one engine's parsed metrics. Keys (all optional):
      requests_running / requests_waiting   — in-flight / queued requests
      kv_cache_usage_pct / kv_cache_tokens  — KV cache fill
      avg_prompt_tps / avg_decode_tps       — engine-measured throughput
      prompt_tokens_total / generation_tokens_total
      mean_ttft_ms / mean_tpot_ms / mean_e2e_ms / mean_queue_ms /
      mean_prefill_ms / mean_decode_ms      — histogram means (vLLM)
      prefix_cache_hit_pct / spec_accept_pct
      preemptions_total / truncated_total / busy_slots_per_decode / n_ctx_max
    """
    out: dict = {}
    if flavor == "llamacpp":
        out["requests_running"] = _sum(m, "llamacpp:requests_processing")
        out["requests_waiting"] = _sum(m, "llamacpp:requests_deferred")
        ratio = _sum(m, "llamacpp:kv_cache_usage_ratio")
        out["kv_cache_usage_pct"] = _r(ratio * 100.0) if ratio is not None else None
        out["kv_cache_tokens"] = _sum(m, "llamacpp:kv_cache_tokens")
        pt, ps = _sum(m, "llamacpp:prompt_tokens_total"), _sum(m, "llamacpp:prompt_seconds_total")
        gt, gs = (_sum(m, "llamacpp:tokens_predicted_total"),
                  _sum(m, "llamacpp:tokens_predicted_seconds_total"))
        out["prompt_tokens_total"] = pt
        out["generation_tokens_total"] = gt
        # The engine's own gauges (average over its lifetime/last window);
        # fall back to lifetime totals ÷ seconds.
        out["avg_prompt_tps"] = _r(_sum(m, "llamacpp:prompt_tokens_seconds")
                                   or ((pt / ps) if pt and ps else None))
        out["avg_decode_tps"] = _r(_sum(m, "llamacpp:predicted_tokens_seconds")
                                   or ((gt / gs) if gt and gs else None))
        out["busy_slots_per_decode"] = _r(_sum(m, "llamacpp:n_busy_slots_per_decode"), 2)
        out["n_decode_total"] = _sum(m, "llamacpp:n_decode_total")
        out["n_ctx_max"] = _sum(m, "llamacpp:n_tokens_max", "llamacpp:n_past_max")
    elif flavor == "vllm":
        out["requests_running"] = _sum(m, "vllm:num_requests_running")
        out["requests_waiting"] = _sum(m, "vllm:num_requests_waiting")
        # 0..1 gauge; renamed gpu_cache_usage_perc -> kv_cache_usage_perc.
        frac = _sum(m, "vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc")
        out["kv_cache_usage_pct"] = _r(frac * 100.0) if frac is not None else None
        out["prompt_tokens_total"] = _sum(m, "vllm:prompt_tokens_total")
        out["generation_tokens_total"] = _sum(m, "vllm:generation_tokens_total")
        out["mean_ttft_ms"] = _hist_mean_ms(m, "vllm:time_to_first_token_seconds")
        out["mean_tpot_ms"] = _hist_mean_ms(m, "vllm:inter_token_latency_seconds",
                                            "vllm:time_per_output_token_seconds")
        out["mean_e2e_ms"] = _hist_mean_ms(m, "vllm:e2e_request_latency_seconds")
        out["mean_queue_ms"] = _hist_mean_ms(m, "vllm:request_queue_time_seconds")
        out["mean_prefill_ms"] = _hist_mean_ms(m, "vllm:request_prefill_time_seconds")
        out["mean_decode_ms"] = _hist_mean_ms(m, "vllm:request_decode_time_seconds")
        if out["mean_tpot_ms"]:
            out["avg_decode_tps"] = round(1000.0 / out["mean_tpot_ms"], 1)
        # Prefix cache: v1 counters (hits/queries, in tokens); v0 had a gauge.
        hits = _sum(m, "vllm:prefix_cache_hits_total", "vllm:prefix_cache_hits")
        queries = _sum(m, "vllm:prefix_cache_queries_total", "vllm:prefix_cache_queries")
        out["prefix_cache_hit_pct"] = _pct(hits, queries)
        if out["prefix_cache_hit_pct"] is None:
            g = _sum(m, "vllm:gpu_prefix_cache_hit_rate")
            out["prefix_cache_hit_pct"] = _r(g * 100.0) if g is not None else None
        acc = _sum(m, "vllm:spec_decode_num_accepted_tokens_total")
        drafted = _sum(m, "vllm:spec_decode_num_draft_tokens_total")
        out["spec_accept_pct"] = _pct(acc, drafted)
        if out["spec_accept_pct"] is None:
            g = _sum(m, "vllm:spec_decode_draft_acceptance_rate")
            out["spec_accept_pct"] = _r(g * 100.0) if g is not None else None
        out["preemptions_total"] = _sum(m, "vllm:num_preemptions_total",
                                        "vllm:num_preemptions")
        out["truncated_total"] = _sum(m, "vllm:request_success_total",
                                      where={"finished_reason": "length"})
        out["requests_finished_total"] = _sum(m, "vllm:request_success_total")
    return {k: v for k, v in out.items() if v is not None}


def summarize_slots(slots: list) -> dict:
    """llama.cpp /slots -> busy/total + per-slot fill. Handles both the current
    shape (is_processing, next_token.n_decoded) and the older `state` int."""
    rows = []
    for s in slots or []:
        if not isinstance(s, dict):
            continue
        busy = bool(s.get("is_processing")) if "is_processing" in s \
            else s.get("state") not in (None, 0)
        nt = s.get("next_token")
        nt = nt[0] if isinstance(nt, list) and nt else (nt if isinstance(nt, dict) else {})
        rows.append({"id": s.get("id"), "busy": busy,
                     "n_ctx": s.get("n_ctx") or 0,
                     "n_decoded": (nt or {}).get("n_decoded") or 0,
                     "speculative": bool(s.get("speculative"))})
    if not rows:
        return {}
    return {"slots_total": len(rows), "slots_busy": sum(1 for r in rows if r["busy"]),
            "slots": rows[:16]}
