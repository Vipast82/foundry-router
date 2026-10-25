# Strata (Qwen3.8-Flash-Next) behind Foundry

[Strata](https://github.com/Niko1221/Strata) is a local inference engine for
Qwen3.8-Flash-Next (a 512-expert MoE) on a consumer GPU plus system RAM: the
GPU holds the hot experts, RAM holds all of them, and an MTP draft layer does
speculative decoding. It speaks the OpenAI API (`/v1/chat/completions`,
`/v1/models`), Anthropic `/v1/messages` and the Responses API, so Foundry treats
it as an **openai-compatible backend with the `llamacpp` flavor** (it is built
on llama.cpp/ggml and takes the same reasoning and sampling fields).

## 1. Start Strata for Foundry

| Setting | Value | Why |
|---|---|---|
| `--host 0.0.0.0 --api-key <secret>` | on the Strata box | Foundry connects over the LAN; Strata's docs say never expose it without a key |
| `--context` | 131072 if RAM allows (65536 otherwise) | Cline needs room; the number goes into the persona and Cline below |
| `fit_max_tokens: true` in `strata-<model>.json` (or `--fit-max-tokens`) | **required** | Strata refuses a request whose `max_tokens` doesn't fit the remaining context. Foundry also fits the cap itself (v0.100.1), this is the backstop |
| `--prompt-cache` | leave on (default) | the conversation cache; one conversation is cached at a time |
| `--prompt-cache-tail` | on, **single-GPU serve only** (not with `layer_split` across two cards) | one extra checkpoint near the end of each prompt: a change late in the prompt re-reads less |
| `--conversation-cache-mib` | e.g. 8192 | RAM cache for conversations, so another request doesn't cost Cline's whole context |
| `reasoning_budget_tokens` | e.g. 24576 | caps thinking; the persona's `max_output_tokens` must be larger (thinking + the tool call) |
| `--parallel` / `"parallel"` | 1 | Cline's loop is sequential; a second slot only splits memory |
| Quant | `IQ3_XXS` (47 GB RAM+VRAM) or `IQ3_S` (55 GB, "matches the full model") | the "Q3" sizes Strata ships; `IQ2_XS` (39 GB) if RAM is short |

**One conversation is cached at a time.** Any other request through the same
Strata (another chat, a Foundry-FineTune generator, an eval) evicts Cline's
cache, and the next Cline turn re-reads the whole context. Keep this Strata for
the Act persona during a sprint.

## 2. Add the backend

Backends → Pool → Edit backend list:

```json
{"name": "strata", "type": "openai-compatible", "flavor": "llamacpp",
 "url": "http://<strata-host>:8080", "api_key": "${STRATA_API_KEY}", "priority": 100}
```

Put the key in `.env` as `STRATA_API_KEY`. After **test**, the Model Registry
shows Strata's model id (Strata accepts any model name, but Foundry routes by
the id it discovers). If Model Registry doesn't show a context length for it,
set the persona's `context_window` (below) — the guard and the output-cap fit
need it.

## 3. What Foundry sends, and what Strata does with it

| Foundry | Strata |
|---|---|
| `reasoning_effort`: `low` / `medium` / `high` (for high, max, xhigh) | accepted; **default `high`** when nothing is sent |
| thinking off → `chat_template_kwargs.enable_thinking=false` | accepted |
| prior turns' reasoning → `reasoning_content` on assistant messages | needed: Strata's template keeps older reasoning in the prompt; sending it back is what lets the cache match |
| `top_k`, `min_p`, `repeat_penalty` … (persona `sampling_options`) | honored per request (`top_k` above 64 = all 64) |
| `max_tokens` (persona `max_output_tokens`) | refused if it can't fit — Foundry now shrinks it to the room left (v0.100.1) |
| `return_progress: true` | Strata streams `prompt_progress` (total / cache / processed / time_ms) every 8k-token chunk, shown as `⏳ reading prompt… 45% (…)` lines before the first token (v0.142.0: first report + each 25 % step) |
| cache numbers | if Strata doesn't report `cached_tokens` / `timings`, the Scoreboard shows `not_reported` instead of counting false misses |

Host Admin's llama.cpp panel calls `/props`, `/slots` and `/metrics`; Strata has
`/props` and `/health`, the others may show as unavailable. Routing is unaffected.

## 4. Persona and Cline settings

Same principles as the 27B: **never change earlier prompt content mid-session.**
Strata re-reads from its last checkpoint (every ~16K tokens) before any change,
and with experts in system RAM a long re-read is slower than on a dense GPU model.

`claude-cline-act`:

| Field | Value |
|---|---|
| `model_allowlist` | `["<Strata's model id>"]` — one model only |
| `context_window` | Strata's `--context` (e.g. `131072`) |
| `reasoning_effort` | `medium`, **force on** — constant every turn (Strata's own default is `high`) |
| `max_output_tokens` | `16384`, or **`32768` when Strata's `reasoning_budget_tokens` is above ~12K** (thinking counts toward it; a cap under the budget cuts the reply before its tool call) |
| `sampling_options` | `{"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0}` |
| turn assist | see below |

```json
{"think_policy": "sticky", "think_escalate": "high",
 "dedupe_reads": true, "roblox_cleanup": true, "image_keep": 3,
 "prejudge": "observe", "supervisor": "observe", "auditor": "observe",
 "advisor": true, "frontier_models": ["claude-opus-5-5"], "frontier_max_calls": 3}
```

Cline (OpenAI Compatible model settings): context window ≈ **0.9 × Strata's
context** (e.g. 118000 for 131072) so Cline compacts before Foundry's guard would
have to cut; request timeout 900 s or blank. `cline_mcp_settings.json`:
`Roblox_Studio` `"timeout": 300`.

### Thinking levels to test

* **Fixed high** (`think_policy: "persona"`, `reasoning_effort: "high"`, force on): the
  level never changes, so it never costs a re-read. Slowest turns, most thinking.
* **Adaptive later** (`think_policy: "adaptive"`): use Strata's levels only —
  `"think_levels": {"routine": "low", "after_edit": "medium", "after_failure": "high"}`
  (no `off`). Every level change rewrites part of the prompt; the Scoreboard's
  cache card counts those as `think_change`, so you can see what adaptive costs
  on your PCIe link before keeping it.

### Sampling

Strata's own `sampling` block is used only when the request carries none. Foundry
sends the persona's `sampling_options`, and anything the CLIENT sends wins over
both. Events → `client set reasoning effort … sampling={…}` shows what Cline
actually sends (v0.100.2); if it sends a temperature, that is what runs.

## 5. Check after the first session

* Thinking line every turn: `think: medium by persona (forced)` — `high by sticky (stuck: …)`
  only after the supervisor flags a real problem, until your next message.
* Scoreboard → Prompt-cache misses: a few at conversation start and compaction,
  almost nothing else. `not_reported` everywhere = Strata isn't returning cache
  numbers (then judge by the "reading the prompt" times in the thinking lines).
* No `HTTP 400` in Events about `max_tokens`.
