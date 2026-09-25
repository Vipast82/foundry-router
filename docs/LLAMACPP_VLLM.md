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
