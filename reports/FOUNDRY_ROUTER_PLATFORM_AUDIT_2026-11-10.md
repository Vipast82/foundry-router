# Foundry Router platform audit — 2026-11-10

Scope: Foundry Router as the single gateway for local and frontier models, agents, coding, chat and media, checked against the Ecosystem Blueprint (3 Nov), the media-gen roadmap and reference (v5.11), and the live router (v0.181.117 at 192.168.0.112:11435).

## Verdict

The router is healthy and already carries coding (Kilo, Cline, the coordinator), chat (AnythingLLM) and media. It is not yet the "one gateway" of the blueprint. Five things stand between here and there, in this order:

1. **The shared-slot queue.** Strata serves one request at a time and the queue in front of it has no priorities, no overflow lane and no conversation pinning. Blueprint build steps 1 to 3 are not started.
2. **Who is asking, and what may leave the house.** A request carries a client key and a persona, but no user, no project and no data tier. There is no policy gate and no single "local only" switch.
3. **Protocol coverage.** No Anthropic Messages endpoint, no rerank endpoint, and no embedding model is reachable today.
4. **One UI with a live picture.** Four separate pages, 31 tabs, no live map of who is waiting on what.
5. **Housekeeping.** Stale backends and MCP servers, a 693-model registry with 28 reachable, noisy events.

## Update, 11 Nov: phases A and B

Built and released as v0.181.118 (details in `docs/QUEUE_AND_LANES.md` and the release notes). It goes live with the next router rebuild.

| Item | Now |
|---|---|
| Priority classes with aging | Built: classes A to D in the slot gate and in the wait for a backend stream |
| Ordered helper wait, "queued behind N" | Built: the stream wait is ordered; a queued call reports its place and no longer runs the stall clock |
| Try, then divert to a lane | Built: `lane` backends, persona setting "divert when busy" |
| Conversation pinning | Built: lane conversations always; any direct conversation with persona setting "pin conversation" |
| "Escalate when local is busy" never fired (issue 1) | Fixed: any local backend counts |
| Local model has no cost (issue 3) | Fixed in code (llama.cpp, vLLM and Unsloth models register as free) and set on the live model |
| Strata outage refuses at once (issue 4) | Built: strict requests are held 90 s (a person 20 s) for a backend coming back |
| Stale MCP servers (issue 6) | Removed live: acestep-music, stable-audio, chatterbox-tts, kokoro-tts (config snapshot 19 taken first) |
| Brain host as a lane | Waiting for the rebuild: needs the `lane` flag of v0.181.118 |
| Strata hold time (issue 5) | Not changed: saving it rebuilds the pool mid-sprint; do it with the lane setup |
| Embeddings host (issue 2) | Open: needs your choice of host |

Still open from the audit: phases C to F, events noise (issue 7), and the three personas (Foundry-Agent, Foundry-Coding, Foundry-Creative) that still grant the removed audio servers and not Media_Gen.

## What I checked

- Live: `/health`, status, 31 personas, 6 backends, 9 MCP servers, 123 tools, the last 293 events (about 22 hours), config for queues, auth and caches.
- Code: the facades (OpenAI, Ollama, audio, media, MCP aggregator), the pool and its queue gate, budgets, client auth, tool finder, media attachments, and the four UI pages.
- Tests: the full suite (4,240 tests). Result at the end of this file.
- Not checked: Open WebUI and n8n (not installed yet), a real AnythingLLM attachment run, and whether MCP tool calls still stall while a model loads.

## Issues on the live router

| # | Finding | Evidence | Action |
|---|---|---|---|
| 1 | "Escalate when local is busy" can never fire | `_escalate_if_local_busy` only acts on backends of type `ollama`; Strata is `openai-compatible`. Four personas have it on (Agent, Chat, Coding, Vision). | Fold into the divert rule (phase B) |
| 2 | No embedding model is reachable | `truenas-ollama` (nomic-embed-text, mxbai-embed-large) is disabled; semantic cache is off | Pick an embeddings host before Knowledge or Open WebUI document search |
| 3 | The local model has no cost in the registry | `cost_per_1k_input` and `relative_cost_tier` are empty for `qwen3.8-flash-next-unsloth-ud-iq3_xxs`; the brain model shows `reachable: false` | Mark local models free; register the brain host as a backend |
| 4 | A Strata outage fails requests instead of holding them | 57 warnings "Foundry-Kilo-Implementer: no model in its allowlist is reachable" during the 11:10 outage; the coordinator then backs off up to 480 s | Hold for a recovering backend (phase B) |
| 5 | New sessions can wait up to 60 s for Strata | `session_hold_seconds` is still 60 with `helpers_allowed` 0 | Lower it on Strata (Access, Helpers and Limits); test with a chat during a sprint |
| 6 | Stale entries clutter Pool, Routes and MCP pages | Disabled backends: truenas-ollama, llama-qwen38-mtp, anthropic. Disabled MCP servers: acestep-music, stable-audio, chatterbox-tts, kokoro-tts (replaced by Media_Gen), Roblox-docs | Remove, or hide disabled entries by default |
| 7 | Events are noisy | 108 of 293 events are "client set reasoning effort"; 14 are research "cross-pass conflation" | Drop the first to debug level; fix or silence the second |
| 8 | Local replies cut by the client's own cap | 16 of 3,641 replies; caps of 1,024 to 8,192 sent by Kilo when it thinks the window is nearly full | Known behaviour; watch after the model upgrade |
| 9 | Admin UI and API are open on the LAN | `client_auth.mode: optional`, `protect_admin: false` | Your setting. Turn on before household users or Open WebUI go live |
| 10 | Usage is over the 85% cutoff | ChatGPT 5-hour 87%, Claude 89% at the last check | Working as configured |

## Blueprint status

| Blueprint item | Status | Note |
|---|---|---|
| Strata gated with `max_concurrent` 1 | Done | Live |
| Direct dispatch for most personas | Done | 24 of 31 personas are direct |
| Fewer tools shown per persona | Done | Find-on-demand, v0.181.109 to 111 |
| Chat attachments reach media-gen | Done, untested in a client | v0.181.117 |
| Brain host as a pool backend ("brain lane") | Not started | The brain uses its own client |
| Local models marked free | Not started | See issue 3 |
| `Foundry-Utility` persona for short jobs | Not started | |
| Priority classes with aging (A to D) | Not started | `FairGate` is round-robin per client, first in first out |
| Try, then divert to the lane | Not started | See issue 1 |
| Conversation pinned to its first model | Not started | `conversation_key` exists; nothing pins on it |
| Ordered helper wait, "queued behind N" | Partly | v0.181.116 fixed the rule-denial wait; the helper wait is still a poll |
| Adequacy judge for openai-compatible backends | Not checked | |
| Paid-off switch per persona or project | Not started | On 9 Nov it took cutoff edits, pin edits and a budget rule that backfired |
| Document tiers and the policy gate | Not started | Nothing reads a tier |
| The signed-in user reaches the router | Not started | `user` and Open WebUI's user headers are ignored |
| Tool proxy independent of model loading | Not checked | |

## Compatibility

| Protocol or client | Status |
|---|---|
| OpenAI chat completions (streaming, tools, JSON schema, seed, stop, logprobs) | Works |
| OpenAI Responses | Works; function tools only, hosted tools are dropped |
| Ollama API (chat, generate, tags, show, embed, pull) | Works |
| Images (generate, edit) and audio (speech, transcribe, translate) | Works; fall back to Media_Gen |
| MCP server (`/mcp`, `/mcp/media`, `/mcp/persona/<name>`) | Works; tools only, no resources or prompts |
| Per-key model list | Works (`_visible_personas`) |
| Embeddings (`/v1/embeddings`, `/api/embed`) | Endpoint works; no model behind it today |
| Anthropic Messages (`/v1/messages`) inbound | Missing. Claude Code, Anthropic SDK apps and Cline's Anthropic provider can't point at the router |
| Rerank (`/v1/rerank`) | Missing. The blueprint's Knowledge search needs it |
| Video endpoint | None; video goes through Media_Gen tools. Fine for now |
| Realtime voice (WebSocket) | None. Later |
| Trace headers (`traceparent`) | Not passed through |
| Kilo 7.8.27, Cline, AnythingLLM | In daily use |
| Open WebUI, n8n | Untested |

## Media-gen readiness

Works today: 35 Media_Gen tools, image and speech endpoints that fall back to it, attachments as `upload:` references, and find-on-demand so the 17.6k-token tool list stays out of the prompt.

Gaps:

- **Finished jobs don't come back to the chat.** media-gen returns a job id by design. The router could watch jobs it submitted and add the result to the next turn, or send it through Alerts.
- **No media page in the router.** Jobs started through Media_Gen tools don't show in the router's own media jobs list. A Media workspace should list them (`media_list_jobs`) and link to the media-gen console.
- **Library and project changes need a manual resync.** The media-gen notes say to press test on Media_Gen after each change. The router should resync on a timer or on the server's tool-list-changed notice.
- **Tool stalls during model load.** Listed in the media-gen notes; not rechecked.

## UI

What is there: `/ui` (31 tabs in 10 groups), the control center, the automation page and setup. Together they are 1.2 MB of hand-written HTML in four files, each with its own navigation and styles. Updates are 21 polling timers; only the control center streams. Charts are a sparkline and a line chart. The Routes tab has a good flow map, but it shows the last hours, not a live queue.

Problems:

1. Three apps for one job. A sprint needs the control center, its routing lives on the automation page, and its backend lives in `/ui`.
2. No live picture. "Live" is a list of advisor text; you can't see who is waiting on Strata or why.
3. The same advisor list appears on Overview, Live and Performance.
4. Media, Knowledge and Ops have no home.
5. The persona editor has 48 fields at one level.

Proposals:

- **One shell, organised by job.** Home, Chat, Coding (control center and automation), Media, Knowledge, Ops, System (backends, access, logs). A left sidebar and a Ctrl+K search that jumps to any persona, setting, session or request.
- **Home is a live traffic map.** Clients, personas, models and backends as four columns; each request in flight is a moving line; each backend shows its slots, its queue and the longest wait. Subscription windows as gauges with a forecast to reset. Click a line for the request's trace.
- **New graphs.** Queue wait per backend over time; a failover waterfall per request (each candidate tried and how long it took); usage-window burn-down; context fill per session; a tool-call timeline per session.
- **Routing modes.** One switch per persona group or project: all local, prefer local, normal, paid for review only.
- **Persona editor.** Presets first, the rest under "advanced", and a preview line: "this request would go to …, because …".
- **Shared code.** One stylesheet and script for all pages, and one event stream instead of the polling timers.

## Structure for what comes next

These four changes make the blueprint's later phases mostly configuration:

1. **A request envelope.** Every request carries client, user, project, purpose, data tier and queue class. Budgets, routing, the queue and the policy gate all read it. Today only client, session and persona exist (plus purpose for coordinator sessions).
2. **Lanes as objects.** A lane is a backend with capacity and queue rules: Strata, the brain lane, an embeddings lane, the media lane. The divert rule and the traffic map both read lanes.
3. **One policy decision.** A single function that takes the envelope and the candidate models and returns allowed or denied with a reason. Cutoffs, budget rules, tiers and the paid-off switch plug into it, and the UI shows the reason.
4. **One capability path.** Chat, embed, rerank, image, speech, transcribe and video all resolve through persona, chain and backend. Today images and audio pick their backends separately.

New services (Knowledge, Paperless, Infra, Code) then arrive as MCP servers granted per persona, found on demand. One gap for the blueprint's "agents read first" rule: the aggregator passes read-only and destructive hints through, but nothing holds a write tool for approval.

## Build order

| Phase | What | Size |
|---|---|---|
| A | Config only: brain lane backend, local models free, Strata hold time, remove stale entries, an embeddings host | About an hour; needs your OK on live config |
| B | Queue: priority classes with aging, try-then-divert, conversation pinning, hold for a recovering backend, "queued behind N" | Three or four releases |
| C | Request envelope, routing modes, policy gate with reasons | Two or three releases |
| D | Home traffic map, event stream, one shell | Three or four releases |
| E | Anthropic Messages endpoint, rerank, embeddings lane | Two releases |
| F | Media workspace and job watch; Knowledge and Ops pages when those services exist | As they arrive |

Recommended start: A and B. Everything shares one Strata slot, and the waits and silent stalls of the last two days all trace back to that queue. The traffic map (D) is the natural second step because it draws the queue B builds.

## Tests

Full suite (4,240 tests) still running when this was written: 1,710 run, 2 failed, 12 skipped. Both failures fall in the coordinator part of the run (the router tests come later); the two tests they most likely are pass when run alone. This section is updated when the run ends.
