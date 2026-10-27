# Client compatibility: AnythingLLM, Open WebUI and friends

How chat clients other than Cline and Kilo see Foundry, and how to make them
show up by name and show which model answered (v0.141.0).

## 1. Give each client its own key

Access → Clients & Keys → Add a client (preset **openwebui** works for both),
then paste the key into the client's API-key field:

| Client | Where the key goes | Base URL |
|---|---|---|
| AnythingLLM | LLM provider **Generic OpenAI**: API Key | `http://<router>:11435/v1` |
| Open WebUI (OpenAI connection) | Admin → Connections → OpenAI API: Key | `http://<router>:11435/v1` |
| Open WebUI (Ollama connection) | Admin → Connections → Ollama: API key (bearer) | `http://<router>:11435` |

Without a key a client still works (while Access → "requests without a key" is
*allowed*), but it shows as **untracked · its IP** in Monitor → Active Use and
on the route map instead of by name.

## 2. See which model and backend answered

Cline and Kilo show Foundry's route line ("⚙️ local · qwen… — streaming…") in
their reasoning pane. AnythingLLM's agent view and Open WebUI often hide it, so
turn on the **route footer** for those keys: Clients & Keys → Edit → "show
which model and backend answered" (or per persona: Personas → "route footer").
Each answer then ends with:

    — via qwen3.8-flash-next-iq3_xxs @ strata (local) · routed by brain <model> · 3 model calls

and buffered (non-streamed) replies also carry the route line in their
reasoning (`reasoning_content` on the OpenAI dialect, `thinking` on Ollama).
The footer is never added to tool-call-only turns, structured (JSON) output,
compaction summaries or quiet data-generation personas.

## 3. Brain or not?

- A request that brings its **own tools** (AnythingLLM `@agent`, Open WebUI with
  tools enabled, Cline, Kilo) is served **directly**: the persona's routes pick
  one model and that model runs the whole turn. The routing brain is not used.
- A request **without tools** to a non-strict persona (AnythingLLM normal chat,
  Open WebUI plain chat) goes through the **brain**, which picks and delegates.
- Strict personas never use the brain.

Performance → Routing brain shows every brain call (speed, failures, requests
that fell back to the simple rule); Routing → Routes draws the brain on the
path and, with *active only*, what is running right now.

## 4. Long research turns

In direct mode Foundry runs the persona's own MCP tools (web search, crawl…)
for at most `agent_brain.worker_tool_max_steps` rounds (default 6). At the
limit the model is asked for its final answer from what it gathered (v0.139.2),
so a client always gets an answer. Raise the limit for research-heavy use
(operator chat: "raise the tool round limit to 12").

## 5. Long waits in @agent mode (v0.142.0)

Most of an agent step is the model thinking, which streams into the client's
reasoning panel ("Thought for 2m 35s" in AnythingLLM). Two things keep the
rest short and visible:

- **MCP tools with client tools**: when a client brings its own tools,
  Foundry still adds the persona's MCP tools (all, by default), so the model
  can do what the client can't (generate an image or a sound, search). Each
  tool definition is prompt the model reads cold on a new chat — Foundry-Chat's
  58 are ~13.9k tokens, ~11 s on Strata — so choose per client: Clients &
  keys → Edit → "MCP tools with client tools" (off / all / auto-dedupe / only
  these), "keyless clients" under Access for programs with no key, or the
  same field on the persona. Operator chat: "AnythingLLM gets Media_Gen and
  searxng" (set_client_tool_policy).
  Auto-dedupe drops persona tools the client already covers by name or
  purpose (AnythingLLM's web-browsing / web-scraping make searxng and
  crawl4ai redundant). Plain chat (no client tools) always gets them all.
- **Prompt-reading progress**: a long prompt read shows as `⏳ reading
  prompt… 45% (8.2k / 18.0k new tokens) · 7s` in the reasoning panel (from
  llama.cpp / Strata `prompt_progress`), and a call that is still queued shows
  "still working" from 10 s.

Time between steps that AnythingLLM spends running its own tool (Monitor →
Performance shows it as the client gap) has no request open, so Foundry can't
show anything there.

## 6. Voice, embeddings, Think toggle, pasted images

**Text to speech** (`/v1/audio/speech`, needs the `media` scope on the key).
Foundry calls the Kokoro or Chatterbox MCP server (run by the Foundry Client)
and returns the WAV it makes. Note (2026-10-27): those servers are being replaced by
Media_Gen, whose `generate_speech` is a queued job (job_id, then `media_check_job`); this
endpoint still maps to kokoro-tts / chatterbox-tts until it gets a Media_Gen adapter.

| Client | Setting |
|---|---|
| Open WebUI | Admin → Settings → Audio → TTS engine **OpenAI**, API base `http://<router>:11435/v1`, key, model `kokoro` (or `chatterbox`), voice `af_heart` (any Kokoro voice id; OpenAI names like `alloy` become `af_heart`) |
| AnythingLLM | Settings → Voice & Speech → TTS provider **OpenAI Compatible**, base URL `http://<router>:11435/v1`, key, model `kokoro`, voice `af_heart` |

**Speech to text is not available**: Foundry has no STT backend, and
`/v1/audio/transcriptions` answers 501 (`no_stt_backend`). Keep the client's
own speech-to-text (browser / local Whisper) for now.

**Embedding models** are listed in `/api/tags` and `/v1/models` under their
real backend names (Ollama capability `embedding`), so the AnythingLLM and
Open WebUI embedder pickers show them. Chat model pickers show them too, as
real Ollama does; turn the listing off with `server.list_embedding_models:
false`.

**Think toggle**: personas advertise `thinking` when a model they can reach
really reasons (Strata's qwen3.8, Claude, gpt-oss…), so Open WebUI shows its
Think switch. A persona with *force reasoning effort* set keeps its own level,
and the switch has no effect there. Flipping it mid-chat changes the prompt
once (one slower turn on a local model).

**Pasted images** on the direct path (AnythingLLM `@agent`, Open WebUI with
tools) go to a vision model on the persona's route when the chosen model can't
see images, or get a clear "no model on this route can read the image" answer.

## Tested request shapes

tests/test_brain_and_clients.py replays each shape, streamed and buffered, with
the route footer on: AnythingLLM chat (OpenAI, streamed, no tools), AnythingLLM
@agent (OpenAI, buffered, native tools), Open WebUI chat (Ollama `/api/chat`,
streamed), Open WebUI tools (OpenAI, streamed, tools), plus the brain-path
fallback.
