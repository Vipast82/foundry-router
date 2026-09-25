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

## MCP tools

Every MCP tool call goes through one place in Foundry, whatever triggered it,
so behaviour and metrics are identical on every route:

| Route | Who owns the loop | What the model sees |
|---|---|---|
| Agent mode, persona with MCP tools | the worker model (or the brain) | the persona's tools |
| **Direct mode** (Cline / OpenCode send their own tools) | the client, plus Foundry for persona tools | client tools **+** the persona's attached MCP tools (if `persona tools in direct mode` is on). Foundry runs its own tool calls and continues; client tool calls go back to the client. A persona with no tools attached is unchanged. |
| Foundry-MCP aggregator (`/mcp/`, `/mcp/p/<profile>/`, `/mcp/persona/<Persona>/`) | the external client (AnythingLLM, Cline, …) | exactly that endpoint's tools |

* **Results keep every content type** — text, images, audio, embedded
  resources; structured results are rendered as JSON. The aggregator returns
  them natively; worker / direct loops pass images to vision-capable models.
* **Tool annotations** (title, readOnly, destructive, idempotent, openWorld)
  are relayed to aggregator clients, so they can ask before a destructive call.
* **Progress** reported by a downstream server (e.g. "step 12/30") is relayed
  to aggregator clients; `progress_heartbeat_seconds` adds "still working…".
* **Persona endpoints** `/mcp/persona/<Persona>/` serve only that persona's
  tools (live — no restart when grants change): point AnythingLLM there to
  load 3 tools instead of 57.
* **Pooled sessions**: one MCP session per server is kept open and reused
  (auto-reconnect; `persistent_session: false` per server to disable).
* **Result size**: workers get `worker_tool_result_chars` (default 24000) of a
  tool result; `mcp_result_limit_chars` only limits what the small brain sees.
* **tool_choice / parallel_tool_calls** from OpenAI clients reach llama.cpp,
  vLLM and Claude (translated to Claude's form; a forced choice is relaxed to
  auto while extended thinking is on). Ollama has no equivalent.
* **Malformed tool arguments**: in Foundry-run loops the model gets an error
  asking it to resend valid JSON, instead of the tool running with `{}`.

**MCP Metrics tab** (Tools & MCP): calls, failure / timeout / 429 rates, p50 /
p95 / max latency, session-reuse rate and connect time, result size in tokens,
content types — per tool, per server, and per source (brain / worker / direct /
aggregator / research / gateway, with persona or endpoint and the external
client's User-Agent). The **context budget** shows how many tokens each tool
set's definitions add to every request, as a % of your context window.
