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

## Tested request shapes

tests/test_brain_and_clients.py replays each shape, streamed and buffered, with
the route footer on: AnythingLLM chat (OpenAI, streamed, no tools), AnythingLLM
@agent (OpenAI, buffered, native tools), Open WebUI chat (Ollama `/api/chat`,
streamed), Open WebUI tools (OpenAI, streamed, tools), plus the brain-path
fallback.
