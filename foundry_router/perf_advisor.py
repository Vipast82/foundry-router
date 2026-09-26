"""Performance advisor — plain-English diagnosis of everything Foundry measures.

Reads the per-request history (perf_samples), live engine metrics (llama.cpp /
vLLM / Meridian), the event log, backend health, MCP tool metrics and the
config, and turns anything that points at a performance problem into a finding
a non-expert can act on:

    {id, severity: critical|warning|info, title, summary, why, fix: [steps],
     evidence: {label: value}, scope}

Rules are deliberately conservative (minimum sample counts, generous
thresholds) so a finding means something. Each rule is independent; one
failing never hides the others.
"""

from __future__ import annotations

import json
import logging
import statistics
from typing import Any, Optional

log = logging.getLogger(__name__)

_SEV_ORDER = {"critical": 0, "warning": 1, "info": 2}
MIN_N = 5


def _pct(vals: list, q: float) -> Optional[float]:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, max(0, int(round(q * (len(vals) - 1)))))]


def _med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def _s(ms) -> str:
    if ms is None:
        return "—"
    s = ms / 1000.0
    return f"{s:.1f}s" if s < 120 else f"{s / 60:.1f} min"


def _f(sev, fid, title, summary, why, fix, evidence=None, scope="") -> dict:
    return {"id": fid, "severity": sev, "title": title, "summary": summary, "why": why,
            "fix": fix, "evidence": evidence or {}, "scope": scope}


# --------------------------------------------------------------------------- #
# per-model history rules                                                     #
# --------------------------------------------------------------------------- #

def _model_rules(model: str, backend: str, rows: list[dict], ctx_len: Optional[int],
                 flavor: str) -> list[dict]:
    out: list[dict] = []
    n = len(rows)
    if n < MIN_N:
        return out
    scope = f"{model} on {backend}" if backend else model
    server = [r for r in rows if r.get("timing_src") == "server"]
    walls = [r.get("wall_ms") or 0 for r in rows]
    wall_total = sum(walls) or 1

    # 1. requests wait at the server before prompt processing
    waits = [max(0.0, r["ttft_ms"] - r["prefill_ms"]) for r in server
             if r.get("ttft_ms") is not None and r.get("prefill_ms") is not None]
    if len(waits) >= MIN_N:
        w50 = _med(waits)
        share = round(100.0 * sum(waits) / wall_total)
        if w50 >= 10000:
            out.append(_f(
                "critical" if share >= 30 else "warning", "server_wait",
                "Requests sit idle at the server before any work starts",
                f"Median {_s(w50)} per request passes before the prompt is even processed "
                f"— {share}% of all time spent on this model.",
                "This time is neither reading the prompt nor writing the answer: the "
                "server is busy with something else first. It is invisible in tok/s "
                "numbers but can be the single biggest cost.",
                ["llama.cpp: with one conversation per slot, try --cache-ram 0 (the host-RAM "
                 "prompt cache copies the whole slot state over PCIe on each request; the "
                 "in-GPU slot cache already gives the cache hits).",
                 "Check that no other client or abandoned request is using the same slot "
                 "(-np 1 means one request at a time).",
                 "llama-swap: make sure the model isn't being swapped out between requests.",
                 "Set a run label on the Performance tab before changing anything, so you "
                 "can compare before/after."],
                {"median wait": _s(w50), "share of time": f"{share}%", "requests": len(waits)},
                scope))

    # 2. prompt cache not reused on long conversations
    big = [r for r in rows if (r.get("prompt_tokens") or 0) >= 20000
           and r.get("cache_hit_pct") is not None]
    if len(big) >= MIN_N:
        c50 = _med([r["cache_hit_pct"] for r in big])
        if c50 is not None and c50 < 50:
            out.append(_f(
                "warning", "cache_miss", "The prompt cache is not being reused",
                f"Long prompts reuse only {c50:.0f}% (median) of the previous request's "
                f"cached tokens, so most of the conversation is re-read every turn.",
                "In a multi-turn chat (Cline) each request repeats the whole history; "
                "normally 90%+ comes from the cache and only the new part is processed. "
                "Low reuse means every turn pays for a full prompt read.",
                ["llama.cpp with -np > 1: requests can land on a different slot — use -np 1 "
                 "for a single user.",
                 "Something is changing the start of the prompt each turn (a timestamp in "
                 "a system prompt, a client that rewrites history, a chat template that "
                 "drops earlier reasoning).",
                 "If the context guard is trimming (see its finding), that's expected once "
                 "per block, not every turn."],
                {"median cache reuse": f"{c50:.0f}%", "long requests": len(big)}, scope))

    # 3. slow prompt reading on cache misses
    cold = [r for r in server if (r.get("prefill_tokens") or 0) >= 4000 and r.get("prefill_tps")]
    if len(cold) >= 3:
        p50 = _med([r["prefill_tps"] for r in cold])
        if p50 is not None and p50 < 150:
            out.append(_f(
                "warning", "slow_prefill", "Reading long prompts is slow",
                f"When a large new chunk must be read, the model processes {p50:.0f} tokens/s "
                f"(median) — a 50k-token read takes ~{50000 / max(p50, 1) / 60:.0f} min.",
                "Prompt reading (prefill) is compute-bound; it gets slower as context grows "
                "because attention covers every earlier token.",
                ["llama.cpp: try a larger micro-batch (-ub 1024 or 2048 with -b 2048) if VRAM "
                 "allows; keep flash attention on (-fa on).",
                 "Make sure every layer is on the GPU (-ngl 99) and nothing spills to CPU/RAM.",
                 "Multi-GPU over narrow PCIe links: tensor-split traffic limits prefill; "
                 "compare -sm tensor vs -sm layer.",
                 "Keep conversations shorter (new task / compaction) so less must be re-read."],
                {"median prefill": f"{p50:.0f} tok/s", "cold reads": len(cold)}, scope))

    # 4. generation speed
    dec = [r["decode_tps"] for r in rows if r.get("decode_tps")]
    if len(dec) >= MIN_N:
        d50, d90 = _med(dec), _pct(dec, 0.9)
        if d50 is not None and d50 < 8:
            out.append(_f(
                "warning", "slow_decode", "Answers are generated very slowly",
                f"Median {d50:.1f} tokens/s while writing.",
                "Under ~10 tok/s usually means part of the model runs on the CPU or in "
                "system RAM, or the GPU is shared with other work.",
                ["Check the model fits entirely in VRAM (llama.cpp -ngl 99; Ollama "
                 "'ollama ps' should say 100% GPU).",
                 "Use a smaller quantization or a smaller model if it doesn't fit.",
                 "Stop other GPU workloads on the same card."],
                {"median decode": f"{d50:.1f} tok/s"}, scope))
        elif len(dec) >= 10 and d90 and d50 < 0.6 * d90:
            out.append(_f(
                "info", "decode_variance", "Generation speed varies a lot",
                f"Typical {d50:.0f} tok/s, but the best 10% reach {d90:.0f} tok/s.",
                "The same model on the same hardware normally writes at a steady speed. "
                "Big swings point to sharing (another request or app on the GPU), "
                "thermal throttling, or very long contexts slowing each token.",
                ["Check GPU temperatures and power limits under load.",
                 "Look at the Live tab while it's slow: other models generating or queued "
                 "requests mean contention.",
                 "Long contexts decode slower — compare speed against context size on the "
                 "Performance charts."],
                {"median": f"{d50:.0f} tok/s", "best 10%": f"{d90:.0f} tok/s"}, scope))

    # 5. speculative decoding acceptance
    spec = [r["spec_accept_pct"] for r in rows if r.get("spec_accept_pct") is not None
            and (r.get("draft_n") or 0) > 0]
    if len(spec) >= MIN_N:
        s50 = _med(spec)
        if s50 is not None and s50 < 40:
            out.append(_f(
                "warning", "low_spec", "Speculative decoding is mostly guessing wrong",
                f"Only {s50:.0f}% of drafted tokens are accepted (median).",
                "The draft (MTP head or draft model) proposes tokens the main model then "
                "checks. Below ~40% acceptance the drafting costs more than it saves.",
                ["Lower the draft length (llama.cpp --spec-draft-n-max 2) or turn "
                 "speculation off and compare decode tok/s.",
                 "Use a draft model from the same family/tokenizer as the main model.",
                 "High temperature lowers acceptance; coding at a lower temperature helps."],
                {"median acceptance": f"{s50:.0f}%"}, scope))

    # 6. replies cut at the output limit
    trunc = sum(1 for r in rows if (r.get("finish_reason") or "") == "length")
    if trunc >= 3 or (n >= 10 and trunc / n > 0.05):
        out.append(_f(
            "warning", "truncation", "Replies are being cut off at the output limit",
            f"{trunc} of {n} replies stopped at the output-token limit before finishing.",
            "A cut-off reply is wasted work: a coding client (Cline) then asks the model "
            "to redo it more concisely ('output-token limit reached before a tool call').",
            ["Raise the persona's 'max output tokens' (Personas editor) or Global settings "
             "→ worker_max_tokens (32768 suits coding with reasoning).",
             "If Events (source 'truncation') says the BACKEND stopped it short of that "
             "limit, remove llama.cpp's -n / --n-predict (or llama-swap cmd), or raise "
             "Ollama num_predict.",
             "Reasoning counts toward the limit — a lower reasoning effort leaves more room."],
            {"cut off": f"{trunc}/{n}"}, scope))

    # 7. reasoning dominates the output
    rz = [(r["reasoning_tokens"], r["completion_tokens"]) for r in rows
          if r.get("reasoning_tokens") and r.get("completion_tokens")]
    if len(rz) >= MIN_N:
        share = _med([a / b for a, b in rz if b])
        if share is not None and share > 0.7:
            out.append(_f(
                "info", "reasoning_heavy", "Most output tokens are reasoning",
                f"About {share * 100:.0f}% of each reply is hidden reasoning.",
                "Reasoning improves hard answers but costs time and output budget on "
                "routine steps.",
                ["For routine work (e.g. a Cline Act persona) set reasoning_effort to low or "
                 "off (with 'force'); keep it high for planning personas."],
                {"reasoning share": f"{share * 100:.0f}%"}, scope))

    # 8. conversations near the context window
    if ctx_len:
        mx = max((r.get("prompt_tokens") or 0) for r in rows)
        if mx >= 0.9 * ctx_len:
            out.append(_f(
                "warning", "context_full", "Conversations are filling the context window",
                f"The largest prompt reached {mx:,} of {ctx_len:,} tokens.",
                "Near the limit every turn re-reads a huge prompt, and the context guard "
                "has to drop older messages (the model loses that history).",
                ["Start a new task or let the client compact (Cline: context window setting "
                 "= the persona's context_window).",
                 "Avoid pasting / reading very large files in one go."],
                {"largest prompt": f"{mx:,}", "window": f"{ctx_len:,}"}, scope))

    # 9. cold model loads
    loads = [r["load_ms"] for r in rows if (r.get("load_ms") or 0) > 3000]
    if len(loads) >= 3 and len(loads) / n >= 0.2:
        out.append(_f(
            "warning", "cold_loads", "The model is reloaded often",
            f"{len(loads)} of {n} requests waited for the model to load "
            f"(median {_s(_med(loads))}).",
            "Loading a model into VRAM takes seconds to minutes each time.",
            ["Ollama: raise keep_alive (Global settings → worker_keep_alive, e.g. 30m) and "
             "OLLAMA_MAX_LOADED_MODELS if several models must stay resident.",
             "Avoid alternating between models that don't fit in VRAM together.",
             "llama-swap: set a longer ttl or group models that can co-exist."],
            {"cold loads": f"{len(loads)}/{n}", "median load": _s(_med(loads))}, scope))

    # 10. spiky time-to-first-token
    tt = [r["ttft_ms"] for r in rows if r.get("ttft_ms")]
    if len(tt) >= 10:
        t50, t95 = _med(tt), _pct(tt, 0.95)
        if t95 and t50 and t95 > 20000 and t95 > 4 * t50:
            out.append(_f(
                "info", "ttft_spikes", "Occasional very long waits for the first token",
                f"Usually {_s(t50)}, but 1 in 20 requests waits {_s(t95)} or more.",
                "Occasional long waits come from cache misses (a big prompt read from "
                "scratch), model loads, or queueing behind another request.",
                ["Check the 'wait', 'cache' and 'load' columns for the slow requests on the "
                 "Performance tab.",
                 "Queued requests show on the Live tab (engine: running / queued)."],
                {"typical": _s(t50), "p95": _s(t95)}, scope))

    # 11. estimated timings
    est = sum(1 for r in rows if r.get("timing_src") == "estimated")
    if est / n > 0.5 and flavor not in ("", None):
        out.append(_f(
            "info", "estimated_timing", "Speed numbers for this model are estimates",
            "The server doesn't report its own timings, so Foundry estimates them from "
            "the stream (network and queue time included).",
            "Treat prefill and TTFT numbers as approximate for this backend.",
            ["For exact numbers use a server that reports timings (llama.cpp does; vLLM "
             "exposes them via /metrics, shown on the Live tab)."],
            {"estimated": f"{est}/{n}"}, scope))
    return out


# --------------------------------------------------------------------------- #
# live engine metrics                                                         #
# --------------------------------------------------------------------------- #

def _server_rules(servers: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in servers or []:
        b, flav = m.get("backend") or "?", m.get("flavor") or ""
        if m.get("metrics_ok") is False and m.get("metrics_error"):
            out.append(_f(
                "info", "no_metrics", "Engine metrics are not available",
                f"{b} doesn't expose /metrics ({str(m['metrics_error'])[:80]}).",
                "Without engine metrics Foundry can't see queueing, KV-cache fill or "
                "preemptions on this server.",
                ["llama.cpp: start with --metrics.", "vLLM exposes /metrics by default."],
                {}, b))
        waiting = m.get("requests_waiting")
        if waiting and waiting > 0:
            out.append(_f(
                "warning", "queued", "Requests are queued behind others right now",
                f"{int(waiting)} request(s) waiting on {b} while "
                f"{int(m.get('requests_running') or 0)} run.",
                "A waiting request makes no progress; to the user it looks like a slow "
                "model.",
                ["llama.cpp -np 1 serves one request at a time: stop other clients using it, "
                 "or raise -np (the context is split between slots).",
                 "Check for abandoned requests (a client that disconnected) on the Live tab."],
                {"waiting": int(waiting), "running": int(m.get("requests_running") or 0)}, b))
        kv = m.get("kv_cache_usage_pct")
        if kv is not None and kv >= 90:
            out.append(_f(
                "warning", "kv_full", "The KV cache is almost full",
                f"{b} KV cache is {kv:.0f}% used.",
                "When it fills, the server must evict or preempt work — requests slow down "
                "or get cut.",
                ["Reduce concurrent requests or context per request.",
                 "vLLM: raise gpu_memory_utilization or lower max_num_seqs; llama.cpp: a "
                 "larger -c (if VRAM allows) or fewer slots."],
                {"KV used": f"{kv:.0f}%"}, b))
        if flav == "vllm" and (m.get("preemptions_total") or 0) > 0:
            out.append(_f(
                "warning", "preemptions", "vLLM is preempting requests",
                f"{int(m['preemptions_total'])} preemption(s) since the server started.",
                "Preempted requests are paused and recomputed later — large slowdowns.",
                ["Lower max_num_seqs or max_model_len, or raise gpu_memory_utilization."],
                {"preemptions": int(m["preemptions_total"])}, b))
        if flav == "vllm" and m.get("prefix_cache_hit_pct") is not None \
                and (m.get("prompt_tokens_total") or 0) > 100000 and m["prefix_cache_hit_pct"] < 30:
            out.append(_f(
                "info", "vllm_prefix", "vLLM prefix cache hit rate is low",
                f"{m['prefix_cache_hit_pct']:.0f}% of prompt tokens come from the prefix cache.",
                "Multi-turn chats should mostly hit the cache.",
                ["Enable --enable-prefix-caching (default on in recent vLLM).",
                 "Keep the start of prompts stable (no per-request timestamps)."],
                {"prefix hits": f"{m['prefix_cache_hit_pct']:.0f}%"}, b))
        if flav == "meridian":
            q = m.get("mean_queue_ms")
            if q and q > 2000:
                out.append(_f(
                    "warning", "meridian_queue", "Claude requests queue inside Meridian",
                    f"Mean queue wait {_s(q)} before Meridian starts a request.",
                    "Meridian limits concurrent Claude sessions; extra requests wait.",
                    ["Avoid running several Claude-heavy clients at once.",
                     "Check Meridian's concurrency settings / profiles."],
                    {"mean queue": _s(q)}, b))
            tot, bad = m.get("requests_total") or 0, m.get("requests_failed") or 0
            if tot >= 20 and bad / tot > 0.1:
                out.append(_f(
                    "warning", "meridian_errors", "Many Claude requests fail",
                    f"{int(bad)} of {int(tot)} Meridian requests returned an error.",
                    "Failed requests are retried or failed over — slow and wasteful.",
                    ["Usually rate limits (429) or an expired login: check the Meridian card "
                     "on Backends → Pool and the Claude usage window."],
                    {"failed": f"{int(bad)}/{int(tot)}"}, b))
    return out


# --------------------------------------------------------------------------- #
# events, backends, config, MCP                                               #
# --------------------------------------------------------------------------- #

def _event_rules(db, hours: float) -> list[dict]:
    out: list[dict] = []
    win = (f"-{hours} hours",)

    def count(where: str, *params) -> int:
        row = db.query_one(
            f"SELECT COUNT(*) AS n FROM event_log WHERE datetime(ts) >= datetime('now', ?) "
            f"AND {where}", (*win, *params)) or {}
        return int(row.get("n") or 0)
    unhealthy = count("source='backend_pool' AND message LIKE '%marked unhealthy%'")
    if unhealthy:
        out.append(_f(
            "critical" if unhealthy >= 3 else "warning", "backend_flap",
            "A backend keeps going offline",
            f"Backends were marked unhealthy {unhealthy} time(s) in this window.",
            "Requests to an offline backend fail over or error; a flapping server "
            "usually crashed, ran out of memory, or was too busy to answer health checks.",
            ["Check the server's logs around those times (Events tab shows when).",
             "Out-of-memory crashes: lower context (-c), batch size, or use a smaller "
             "quantization."],
            {"times": unhealthy}))
    stalls = count("source='routing' AND message LIKE '%produced no output%'")
    if stalls:
        out.append(_f(
            "warning", "stalls", "Models went silent and were abandoned",
            f"{stalls} call(s) produced no output for the stall timeout and were cut.",
            "A silent model is either stuck, queued, or reading an enormous prompt.",
            ["If it was a very long prompt read, raise the stall timeout (Global settings)."
             " Otherwise check the server — a hung request or a full queue."],
            {"stalled calls": stalls}))
    import re
    cut_rows = db.query(
        "SELECT message FROM event_log WHERE datetime(ts) >= datetime('now', ?) "
        "AND source='truncation'", win)
    short = []
    for r in cut_rows:
        m = re.search(r"cut at (\d+) output tokens \(cap sent (\d+)\)", r["message"] or "")
        if m and int(m.group(1)) < 0.9 * int(m.group(2)):
            short.append((int(m.group(1)), int(m.group(2))))
    if short:
        got = statistics.median(a for a, _ in short)
        cap = max(b for _, b in short)
        out.append(_f(
            "critical", "backend_output_cap", "The server cuts replies shorter than Foundry allows",
            f"{len(short)} reply(ies) stopped at ~{int(got):,} tokens although Foundry allowed "
            f"{cap:,}.",
            "The backend has its own output limit, so raising Foundry's does nothing — "
            "and the cut replies (often unfinished tool calls) have to be redone.",
            ["llama.cpp: remove -n / --n-predict from the server command (or set -1), "
             "including llama-swap cmd lines.",
             "Ollama: raise num_predict in the model / Modelfile.",
             "Or the context filled up mid-reply: check the context window vs prompt size."],
            {"cut at": f"~{int(got):,}", "Foundry limit": f"{cap:,}", "replies": len(short)}))
    guard = count("source='context'")
    if guard >= 3:
        out.append(_f(
            "info", "context_guard", "Conversations are being trimmed to fit",
            f"The context guard trimmed {guard} request(s).",
            "Trimming keeps requests inside the window but the model loses older messages.",
            ["Start new tasks more often, or let the client compact earlier (set its "
             "context window to the persona's)."],
            {"trimmed requests": guard}))
    fo = db.query_one(
        "SELECT COUNT(*) AS n FROM request_log WHERE datetime(ts) >= datetime('now', ?) "
        "AND guardrail_events LIKE '%failover:%'", win) or {}
    if int(fo.get("n") or 0) >= 3:
        out.append(_f(
            "warning", "failovers", "Requests keep failing over to another model",
            f"{fo['n']} request(s) had to switch models after the first one failed.",
            "Each failover costs a failed attempt first — and the answer comes from a "
            "different model than intended.",
            ["The failover line in the client says why; the Events tab lists the backend "
             "errors. Fix the failing backend (crash, OOM, auth)."],
            {"failovers": int(fo["n"])}))
    return out


def _backend_rules(svc) -> list[dict]:
    out = []
    for b in getattr(svc.pool, "backend_status", lambda: [])() or []:
        if not b.get("healthy"):
            out.append(_f(
                "critical", "backend_down", f"Backend {b['name']} is offline",
                f"{b['name']} isn't answering health checks: "
                f"{(b.get('last_error') or 'no detail')[:120]}.",
                "Its models can't serve requests; personas fall back to other models.",
                ["Check the server is running and reachable from Foundry (URL / port / "
                 "firewall).", "Look at its logs for crashes or out-of-memory errors."],
                {"url": b.get("url")}, b["name"]))
    return out


def _config_rules(svc) -> list[dict]:
    out = []
    cfg = svc.config_store.config
    bc, pool = cfg.agent_brain, cfg.backend_pool
    stall = int(getattr(bc, "direct_stream_stall_seconds", 0) or 0)
    if stall and pool.request_timeout_seconds <= stall:
        out.append(_f(
            "warning", "timeouts_order", "Backend read timeout is shorter than the stall timeout",
            f"Read timeout {pool.request_timeout_seconds}s ≤ stall timeout {stall}s.",
            "The read timeout fires first and ends the request with an error, skipping "
            "the stall watchdog's clean failover.",
            ["Set Global settings → backend read timeout above the stall timeout "
             "(e.g. 1200 vs 900)."],
            {"read timeout": f"{pool.request_timeout_seconds}s", "stall": f"{stall}s"}))
    direct = [p for p in svc.personas.list(enabled_only=True)
              if (p.get("execution_mode") or "") == "direct"]
    if direct and not bc.direct_stream:
        out.append(_f(
            "info", "no_direct_stream", "Live streaming is off for coding clients",
            "direct_stream is disabled, so Cline / OpenCode get the whole answer at once.",
            "Without streaming there's no live output, no stall detection and no "
            "tool-call progress.",
            ["Global settings → direct_stream ✓."]))
    if (bc.heartbeat_seconds or 0) <= 0 or (bc.direct_stream and not bc.direct_stream_heartbeat_seconds):
        out.append(_f(
            "warning", "no_heartbeat", "Keep-alive is off",
            "A heartbeat interval is 0.",
            "Reverse proxies (Cloudflare ~100s, NPM) drop connections that send nothing "
            "while a model reads a long prompt.",
            ["Global settings → heartbeat / ds heartbeat = 10."]))
    if (bc.worker_max_tokens or 0) < 16384:
        out.append(_f(
            "info", "small_output_cap", "The global output limit is small",
            f"worker_max_tokens = {bc.worker_max_tokens}.",
            "Reasoning models plus large file edits often need more; replies get cut.",
            ["Raise Global settings → worker_max_tokens to 32768 (or per persona)."]))
    for p in svc.personas.list(enabled_only=True):
        try:
            cw = int(p.get("context_window") or 0)
            allow = json.loads(p.get("model_allowlist") or "[]")
        except (TypeError, ValueError):
            continue
        for mid in allow if isinstance(allow, list) else []:
            ml = (svc.registry.get(mid) or {}).get("context_length")
            if cw and ml and cw > int(ml):
                out.append(_f(
                    "warning", "persona_ctx_too_big",
                    f"{p['virtual_name']} claims more context than its model has",
                    f"context_window {cw:,} but {mid} serves {int(ml):,}.",
                    "Clients size conversations from the persona; the server will reject "
                    "or truncate what doesn't fit.",
                    [f"Set {p['virtual_name']}'s context_window to {int(ml):,}, or start the "
                     "server with a larger context."],
                    {"persona": f"{cw:,}", "server": f"{int(ml):,}"}, p["virtual_name"]))
    return out


def _mcp_rules(db, hours: float) -> list[dict]:
    out = []
    try:
        from . import mcp_metrics
        s = mcp_metrics.summary(db, hours)
    except Exception:
        return out
    for t in s.get("by_tool") or []:
        if t.get("calls", 0) < MIN_N:
            continue
        if (t.get("error_pct") or 0) >= 20:
            out.append(_f(
                "warning", "mcp_errors", f"Tool {t['tool']} fails often",
                f"{t['errors']} of {t['calls']} calls failed ({t['error_pct']}%).",
                "Failed tool calls waste a model turn and often trigger retries.",
                ["See MCP Metrics → recent calls for the error; fix or disable the tool."],
                {"errors": f"{t['error_pct']}%"}, f"{t['server']}/{t['tool']}"))
        elif (t.get("p95_ms") or 0) >= 30000:
            out.append(_f(
                "info", "mcp_slow", f"Tool {t['tool']} is slow",
                f"1 in 20 calls takes {_s(t['p95_ms'])} or more.",
                "The model waits for every tool result before continuing.",
                ["Check the tool's server; raise its timeout only if the work is legitimately "
                 "long (media generation)."],
                {"p95": _s(t["p95_ms"])}, f"{t['server']}/{t['tool']}"))
    return out


# --------------------------------------------------------------------------- #

async def advise(svc, hours: float = 24) -> dict:
    db = svc.db
    findings: list[dict] = []
    rows = db.query(
        "SELECT model, backend, prompt_tokens, prefill_tokens, completion_tokens, "
        "reasoning_tokens, cached_tokens, draft_n, decode_tps, prefill_tps, prefill_ms, "
        "ttft_ms, wall_ms, load_ms, cache_hit_pct, spec_accept_pct, finish_reason, timing_src "
        "FROM perf_samples WHERE datetime(ts) >= datetime('now', ?)", (f"-{hours} hours",))
    groups: dict = {}
    for r in rows:
        groups.setdefault((r["model"], r["backend"] or ""), []).append(r)
    flavors = {b.get("name"): (b.get("flavor") or b.get("type") or "")
               for b in (getattr(svc.pool, "backend_status", lambda: [])() or [])}
    for (model, backend), rs in groups.items():
        try:
            ctx = (svc.registry.get(model) or {}).get("context_length")
            findings += _model_rules(model, backend, rs, int(ctx) if ctx else None,
                                     flavors.get(backend, ""))
        except Exception:
            log.exception("perf advisor: model rules failed for %s", model)
    for fn in (lambda: _event_rules(db, hours), lambda: _backend_rules(svc),
               lambda: _config_rules(svc), lambda: _mcp_rules(db, hours)):
        try:
            findings += fn()
        except Exception:
            log.exception("perf advisor rule group failed")
    try:
        findings += _server_rules(await svc.pool.server_metrics())
    except Exception:
        log.exception("perf advisor: server rules failed")
    findings.sort(key=lambda f: (_SEV_ORDER.get(f["severity"], 9), f["title"]))
    return {"hours": hours, "requests_analysed": len(rows), "findings": findings,
            "counts": {s: sum(1 for f in findings if f["severity"] == s)
                       for s in ("critical", "warning", "info")}}
