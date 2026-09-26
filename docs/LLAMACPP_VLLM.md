# llama.cpp and vLLM backends

Both are `type: openai-compatible` backends; the `flavor` field tells Foundry
which server software is behind the URL so it can use that server's extras.

```yaml
backend_pool:
  internal:
    backends:
      - {name: llamacpp-1, type: openai-compatible, url: http://gpu-box:8080, flavor: llamacpp}
      - {name: vllm-1,     type: openai-compatible, url: http://gpu-box:8000, flavor: vllm}
```

## What Foundry reads from each server

| | llama.cpp (`llama-server`) | vLLM |
|---|---|---|
| Per-call decode / prefill tok/s | `timings` in every response (server-measured) | not reported — Foundry **times streamed calls itself** (marked ≈ in the UI) |
| Prompt tokens actually prefilled | `timings.prompt_n` (cache hits excluded, so prefill tok/s isn't inflated) | prompt − cached |
| KV prefix-cache hit per call | `usage.prompt_tokens_details.cached_tokens`, else `timings.cache_n` | `usage.prompt_tokens_details.cached_tokens` (needs `--enable-prompt-tokens-details`) |
| Speculative acceptance per call | `timings.draft_n` / `draft_n_accepted` | engine-wide only (`/metrics`) |
| Reasoning tokens | — | `usage.completion_tokens_details.reasoning_tokens` |
| Context window (auto-filled in the registry) | `/props` per-slot `n_ctx` (or `/v1/models` `meta.n_ctx_train`) | `/v1/models` `max_model_len` |
| Capabilities | `/props` modalities (vision/audio) + chat-template tool support | — |
| Engine metrics (Live → Inference servers) | `/metrics` (needs `--metrics`) + `/slots` | `/metrics` |
| Version | `/props` `build_info` | `/version` |
| Model switching | `llama-swap` auto-detected via `GET /running` | one model per server |

## Request parameters forwarded

Persona `sampling_options`, global sampling defaults and client options are
forwarded per flavor (a strict OpenAI endpoint gets only the standard set):

* **llama.cpp:** top_k, min_p, typical_p, repeat_penalty, repeat_last_n,
  DRY (`dry_multiplier`, `dry_base`, `dry_allowed_length`, `dry_penalty_last_n`,
  `dry_sequence_breakers`), XTC (`xtc_probability`, `xtc_threshold`),
  top_n_sigma, dynatemp, mirostat, samplers, n_keep, n_probs, cache_prompt,
  id_slot, grammar / json_schema, reasoning_format, t_max_predict_ms, lora …
* **vLLM:** top_k, min_p, repetition_penalty, length_penalty, min_tokens,
  stop_token_ids, ignore_eos, bad_words, priority, truncate_prompt_tokens,
  guided_json / guided_regex / guided_choice / guided_grammar, structured_outputs …
* Ollama spellings are translated (`repeat_penalty` ↔ `repetition_penalty`,
  `num_keep` → `n_keep`), `num_predict` → `max_tokens`.
* Thinking off → `chat_template_kwargs.enable_thinking=false` (merged with any
  `chat_template_kwargs` you set); a level → `reasoning_effort`.
* Structured output (persona `output_format`) → `response_format` json_schema
  with the schema wrapped as `{name, schema}` — the shape both servers read.

**vLLM context overflow:** vLLM rejects prompt + `max_tokens` > `max_model_len`
instead of shortening the reply. Foundry parses the error and retries once
with the output budget that fits.

## Suggested launch flags

llama.cpp: `--jinja --metrics -np 2 -fa on` (+ `-ctk q8_0 -ctv q8_0` for a
smaller KV cache, `-md draft.gguf --draft-max 16` for speculative decoding).

vLLM (2 GPUs): `vllm serve <model> --tensor-parallel-size 2
--enable-auto-tool-choice --tool-call-parser <parser> --reasoning-parser <parser>
--enable-prefix-caching --enable-prompt-tokens-details --served-model-name <id>`.
Turing cards (RTX 20xx, e.g. 2080 Ti) have no bf16 — use `--dtype half` and an
AWQ/GPTQ quant; FlashAttention-2 needs Ampere+, so vLLM falls back to another
attention backend there.

## Comparing hardware / clearing data

Performance tab → **Run label**: set it (e.g. `2x4060ti-16gb`) before a change
and again after (`2x2080ti-22gb`); the per-model breakdown then shows one row
per model × backend × run. **Clear data…** (Performance) / **Clear stats…**
(Live) wipe history samples, Live averages, truncation counters, observed
latency scores and optionally the Usage Log — for all models or one.


## Finding hidden server time: the "wait" column

Performance → per-model table has a **wait** column: the median time to first
token *minus* the server's own prompt-processing time. It is time the backend
held the request before (or around) working on it, and it should be near 0.
When it is consistently above 10 s, the Performance tab shows a warning card
with the share of wall time it costs.

Seen live on 2× RTX 2080 Ti (PCIe Gen3 x4 OcuLink), Qwen3.8-27B, `-c 262144`,
`-np 1`, `--cache-ram 32768`: a near-constant **~53 s wait on 469 of 643 Cline
turns — 61% of all wall time** — independent of context size, while prefill
was only ~2 s thanks to 99% slot-cache hits. That signature matches the
host-RAM prompt cache saving/restoring the slot state over the narrow PCIe
links on each request. With one conversation per slot the in-GPU slot cache
already provides the hits, so try `--cache-ram 0` and compare the wait column
(set a new run label first so before/after are separate).

Other causes of a high wait: another client (or an abandoned request) holding
the only slot on `-np 1`, and model swaps under llama-swap.


## Performance advisor

**Dashboard → Live** (top card, refreshed every 30 s) and **Performance** (for
the selected window) show the **🩺 Performance advisor**: every sign of a
performance problem Foundry can see, in plain language. Click a finding for
*why it matters*, *what to do* (numbered steps) and the *evidence* numbers.
Severity: 🔴 critical (costing a lot now / something is down), 🟠 warning,
🔵 tip.

What it checks:

| Area | Findings |
|---|---|
| Request history | server wait before prefill · prompt cache not reused · slow prompt reading · very slow or highly variable generation · low speculative acceptance · replies cut at the output limit · reasoning-heavy output · conversations near the context window · frequent cold model loads · spiky first-token times · estimated (not measured) timings |
| Live engine metrics | queued requests · KV cache almost full · vLLM preemptions · low vLLM prefix-cache hits · Meridian queueing and failing requests · engine metrics unavailable |
| Events | backends going offline (and flapping) · stalled calls · requests failing over · the server cutting replies shorter than Foundry allows · context trimming |
| Backends | backend offline right now |
| Settings | read timeout ≤ stall timeout · streaming off for coding clients · keep-alive off · small output limit · a persona claiming more context than its model serves |
| MCP tools | tools that fail often or are slow |

Rules need a minimum number of samples and use generous thresholds, so a
finding is worth acting on. API: `GET /admin/api/perf/advisor?hours=24`.
