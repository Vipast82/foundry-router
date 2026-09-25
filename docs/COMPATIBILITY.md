# Backend ↔ client passthrough

What each backend reports, and how it reaches each kind of client. Foundry
normalizes everything into one internal shape (content, thinking, tool calls,
usage, timings, finish reason) and re-emits it in the client's own protocol.

## Clients

| Client | Protocol | Gets |
|---|---|---|
| Cline, Kilo | Ollama `/api/chat` | live content + `message.thinking`, tool calls (original ids), real `eval_duration` / `prompt_eval_duration` / `load_duration`, `done_reason: "length"` on truncation |
| Open WebUI (Ollama connection) | Ollama `/api/chat`, `/api/generate`, `/api/ps`, `/api/show`, `/api/embed` | same as above; its tok/s badge uses the backend's real decode time; running models from `/api/ps` |
| AnythingLLM | Ollama API or generic OpenAI | thinking, streaming, embeddings via `/api/embed` |
| OpenCode, Open WebUI (OpenAI connection), SDKs | OpenAI `/v1/chat/completions` | tools → `tool_calls` (original ids), reasoning as `delta.reasoning_content`, `finish_reason` `tool_calls` / `length` / `stop`, `usage` with `prompt_tokens_details.cached_tokens` and `completion_tokens_details.reasoning_tokens`, llama.cpp-style `timings`, `served_by` |

Both APIs share one dispatcher, so persona routing, direct mode (client tools),
telemetry and stats are identical whichever protocol a client speaks.

Extra per-reply detail is attached as `foundry` on Ollama final chunks
(ignored by clients that don't know it): `served_by` (which real model answered
a persona), `backend`, `cached_tokens`, `reasoning_tokens`, `timing_source`
(`server` or `estimated`) and the backend's raw `finish_reason`
(e.g. `tool_use`, `refusal`).

Client inputs forwarded: `tools`, `options` / sampling fields, `think` /
`reasoning_effort` / `reasoning.effort`, `format` / `response_format`,
`keep_alive` (raw-model requests), images (Ollama `images` or OpenAI
data-URI parts), prior-turn reasoning, session headers (see Meridian).

## Backends

| | Ollama | llama.cpp | vLLM | Meridian (Claude) |
|---|---|---|---|---|
| Thinking | `message.thinking` | `reasoning_content` | `reasoning_content` / `reasoning` | `thinking` blocks (redacted blocks shown as a note) |
| Decode / prefill timing | server | server (`timings`) | estimated from stream | estimated from stream |
| Cache hits | — | `cache_n` / cached_tokens | cached_tokens | `cache_read_input_tokens` |
| Speculative acceptance | — | per call | engine-wide | — |
| Engine metrics (Live) | — | `/metrics`, `/slots` | `/metrics` | `/health` + `/metrics` |
| Errors | HTTP | HTTP | HTTP (+ context-overflow retry) | HTTP **and in-stream `event: error`** |

## Meridian notes

Foundry sends, on every call: `x-meridian-source: foundry-router`,
`x-request-id` (so requests are attributable in Meridian's own log),
`x-meridian-profile` (backend `meridian_profile`), `x-meridian-agent`
(backend `meridian_agent`, optional) and any client session header
(`x-opencode-session`, `x-session-affinity`, `x-session-id`, …). With
`meridian_session_affinity: true` Foundry derives a stable
`x-session-affinity` per conversation when the client sends none, so Meridian
resumes the same Claude session — warm prompt cache, and the model sees its own
previous turns. Effort goes out as `output_config.effort` alongside the
`thinking` block.

Settings that live on the Meridian side (its Settings page, per adapter):

* Foundry doesn't send a recognised User-Agent, so Meridian uses its default
  adapter — `MERIDIAN_DEFAULT_AGENT`, else **opencode**, which forwards thinking
  and returns tool calls to the caller. If you point that default at an adapter
  without thinking support (e.g. `passthrough`), set `meridian_agent: opencode`
  on the Foundry backend or enable *thinking passthrough* for that adapter.
* The OpenCode adapter defaults to layering the ~28 KB Claude Code system
  prompt onto every request. Foundry sends its own prompts, so turning off
  *Claude Code system prompt* for the adapter Foundry uses saves quota and
  latency.
* Meridian's tool mode should be passthrough (the default for these adapters):
  tool calls come back to Foundry / your client instead of running inside
  Meridian.
