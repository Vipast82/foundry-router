# Release and migration notes

Newest first. Each version is tagged `vX.Y.Z` (docs/CONVENTIONS.md).

## v0.181.124 — redesign, step 3: Personas and Pool; a watchdog for router stalls

**Personas.** The tab was 4,260 px tall with the editor at the very bottom. Now:

- A toolbar above the list: a find box (name, model, tool, description) and group buttons with counts (All, Chat and agent, Kilo, Cline, Media). The choice is kept while the page is open.
- **New persona** and each row's **edit** open the editor as a panel on the right, over the list. It has five sections (Basics, Routing, Tools, Models, Advanced) and Save stays in view at the bottom. Close, Esc or a click outside closes it without saving.
- Resizing the window never redraws the page while the editor is open.

**Pool.** The tab was 4,230 px of unrelated cards. It now shows one section at a time: Backends, Add or edit, Routing and brain, Global settings, MCP endpoint (`TAB_SECTIONS` and `sectionTabs()`, reusable for other long tabs).

**Stall watchdog** (`loopwatch.py`). On 2026-11-11 the live router went silent for 10 to 30 seconds three times, with nothing in Events to say why. Everything runs on one event loop, so any code that works for seconds without yielding freezes every stream. A task on the loop now stamps the time four times a second and a separate thread watches the stamp; when the loop is stuck for more than 2 s the thread copies the loop's stack. When the loop moves again one warning is written: "router stalled for 12.4 s in graph (routes_view.py:88)", with the stack in its detail. `/health` gains `stalls` (count and the latest), and `GET /admin/api/health/stalls` lists the last 20 with stacks.

Tests: `tests/test_ui_kit.py`, `tests/test_loopwatch.py`.

## v0.181.123 — redesign, step 2: Live and Performance

**Live.** The tab was twelve cards in one column, 2,470 px tall, opening with the advisor. It now opens with **Running now**: one chip per backend (slots filled, who waits, lane or not) and a table of the requests in flight (who, persona, model, backend, what it is doing, for how long, request id). A call reading its prompt shows a progress bar and the token counts. Under it, two columns: recent performance, recently finished and loaded models on the left; inference servers, routing brain, tools (running and recent, one card) and backends on the right. On a window under 1,250 px the columns stack. The advisor is folded at the bottom with its counts on the fold line. A model's usual speed is shown only while it generates, and stream estimates in the thousands are not shown at all (a waiting Claude call read "~9938.2 tok/s").

**Performance.** The charts come right after the summary tiles; then the per-model breakdown, the advisor, the routing brain and the inference servers. Run label and performance settings sit together under a new Settings heading, before the details.

**Charts.**
- A few extreme points flattened every chart (a stream-estimated speed of 1.4 million tok/s, one 97 s wait). When the maximum is more than three times the 98th percentile, the scale ends just above that percentile, the spikes are drawn at the top edge, and the legend says how many. Hovering still shows the real values.
- Each series' latest value is marked on the line and written at the right edge in the series' colour.
- Wide charts carry five time labels (three before).

Tests: `tests/test_ui_kit.py`.

## v0.181.122 — an embeddings-only backend is healthy, and untagged embedding names resolve

Found on the live router right after the CPU embeddings service went in (2026-11-11): `nomic-embed-text` answered every request (768 dimensions, about half a second), but the backend showed **failing**.

- **Health probe.** The functional probe sends a tiny chat. A backend that serves only embedding models has no chat model, so the probe reported "no chat model listed by this backend" and the backend read as failing (red on the route map, a warning in Events). Such a backend is now probed with one tiny embedding (`HealthMonitor._probe_embedding`); a failed or empty answer is still a failure, with its reason.
- **Model names.** `/v1/embeddings` and `/api/embed` returned 404 for `nomic-embed-text` because the served id is `nomic-embed-text:latest`. An embedding model asked for without a tag now resolves to `<name>:latest`, or to its only served tag, as Ollama itself does. An exact name always wins, a name with a tag is never changed, and nothing is guessed when two tags are served.

Tests: `tests/test_embeddings_backend.py`.

## v0.181.121 — redesign, step 1: side menu, quick search, live traffic on the home page; CPU embeddings

**Side menu.** On a window 1,000 px or wider the sections are a menu down the left side; the open group lists its pages. "Use top tabs" at the bottom of the menu (or in quick search) brings the tab bar back, and the choice is remembered in the browser. Narrower windows always get the top tabs.

**Quick search.** Ctrl+K (or `/` when not typing) opens a search over every page, persona, backend and MCP server, plus the control center, automation, setup, the theme switch and the menu choice. Arrow keys move, Enter opens, Esc closes.

**Home page: traffic.** A new card at the top of Overview shows what is moving through the router: one chip per backend with its slots (filled = running), how many are waiting and their queue classes, and whether it is a lane; under it the route map of the last 30 minutes (observed traffic only), with the same hover path highlight as the Routes tab. The chips and the map refresh every 10 s, the numbers below every 30 s as before.

**Fix.** In live mode the route map labelled every client "no key", because live data carries no key details. A client known by name has a key and now says so; only an untracked IP reads "no key — make one".

**Embeddings on CPU.** `docker-compose.yml` gains an `embeddings` service: a small Ollama with no GPU requested, for `/v1/embeddings` and `/api/embed`. After `docker compose up -d`: add the backend `{"name": "embeddings", "type": "ollama", "url": "http://embeddings:11434", "priority": 50}` under Backends, Pool, then pull `nomic-embed-text` (about 275 MB) on it under Backends, Host Admin. To move it to a GPU later, change the backend's URL. Whatever is indexed with one embedding model has to be re-indexed if the model changes.

Tests: `tests/test_ui_kit.py` (shell, quick search, traffic card, the compose service).

## v0.181.120 — UI pass: resizing, tables, dropdowns, the route map and the control center graph

Victor, 2026-11-11: "when I change window sizes, some things don't scale up and down very well", plus a request for better-looking charts, route and activity maps, dropdowns and adjustable tables. Checked in a local preview against the live router's data at 1500, 1000, 780, 700 and 600 px wide.

**A shared UI kit** (`ui/static/kit.css`, `ui/static/kit.js`, served at `/ui/kit.css` and `/ui/kit.js`) loaded by `/ui`, `/ui/control` and `/ui/automation`:

- **Tables.** Click a column header to sort (numbers, "4.6k", "$2.33" and plain text are compared properly; a page that redraws keeps the order). Drag a header edge to resize a column, double-click the edge to reset; widths are remembered per table in the browser. A table wider than its place scrolls inside its own box. On the automation page tables are resizable but not sortable: their rows are in a deliberate order.
- **Dropdowns, fields, scrollbars.** One look across the three pages, in each page's own palette.
- **Navigation wraps** onto more rows in a narrower window. It used to turn into a sideways scroller that hid the last groups.
- **`FK.flow`**, a flow-map renderer: boxes with a state stripe and a count, lines whose thickness follows the traffic, a legend of only the line kinds on screen, and hover: point at a box or a line and its whole path stays lit while the rest fades.

**Admin page (`/ui`).**

- The open tab redraws when the window's width changes (not while you are typing or a dialog is open). Charts and the route map were drawn once for the width at load and then stretched.
- **Routes** is drawn by `FK.flow` at the page's width. It was a fixed 1,180 px drawing with thin lines. Configuration and fallback lines stay quiet until you point at their path.
- **The route graph no longer blocks the router.** `GET /admin/api/routes` scans the request log (5 to 29 s for a one-day window on the live router) and ran on the event loop, so every stream stalled while the Routes tab was open. It now runs in a worker thread and a result is reused for 12 s (4 s in live mode).
- The performance advisor shows its first four findings and folds the rest ("Show all N findings"), so the charts below stay in reach.

**Control center (`/ui/control`).**

- In a short or narrow window the three work panes were crushed into a strip (213 px tall at 1000 x 800) because the sprint summary and the composer kept their full height. The panes now keep a usable height and the page scrolls.
- The inspector and graph widths are pixels saved on whatever window they were last dragged in; on a smaller window they added up to more than the page and the graph was pushed off the right edge. They are now fitted to the window: the graph shrinks, then the inspector, and a graph squeezed under 420 px gets its own row under the panes. A changed window width re-fits the panes.
- A pane that had been a full-width row no longer keeps spanning every column when it moves back beside the others.
- **Route / activity graph:** a neutral box with a stripe in the kind's colour, rounded lines whose thickness follows the event count, and the same hover path highlight as the route map.

**Automation (`/ui/automation`).** The Memory and Repo changes tables ran off the right edge at 780 px and cut off the Actions column; they now scroll inside their own box.

Tests: `tests/test_ui_kit.py` (the kit is served and loaded by every page before its own script, the admin page redraws on resize and draws routes with the kit, the route graph cache).

## v0.181.119 — the cost calculator keeps itself current

The Cost tab compared your traffic against a fixed list of paid models. Prices could be refreshed from OpenRouter, but only on request or on an opt-in weekly schedule, and the list itself never changed: on 2026-11-11 it still showed Claude Opus 4.8, Claude Sonnet 5, Claude Haiku 4.5 and GPT-5.6, last refreshed on 30 October.

- **The schedule is on unless you switch it off, and daily by default** (it was opt-in, every 7 days). The first check runs 90 seconds after the router starts, then hourly; an update runs when one is due. The panel shows the last update and the next one.
- **Rows follow their model family to the newest version** (`pricing_update.family_of`, `newest_in_family`). A row matched to `anthropic/claude-opus-4.8` belongs to the family `anthropic/claude-opus`; when OpenRouter lists a newer member (by release date, so Grok 4.20 isn't read as newer than 4.7), the row takes its name, id and prices. Batch ids and dated snapshots are not family members. With today's feed: Claude Opus 4.8 → 5.5, Claude Sonnet 5 → 5.5, Claude Haiku 4.5 → 5.5, GPT-5.6 Sol → GPT-6.1 Sol, GPT-5.6 Luna → GPT-6 Luna, Gemini 2.5 Flash → 3.8 Flash, Grok 4.6 → 4.7.
  - A locked row never moves. "Follow each model to its newest version" in the panel turns it off for all rows.
  - When two rows are matched to one model, the first takes the new name and the second stays on the model it names (service names are unique).
  - A price difference of more than 10x is still held back as suspicious for the SAME model; a row that moved to a different model is not held back (Haiku 5.5 is 10x cheaper than 4.5).
  - A family that is renamed outright (Gemini 3.1 Pro → Gemini 4 Carbon) can't be followed; add that row by hand.
- **Newly released models join the list** (`new_families`). For each vendor already in the table, a model family that isn't listed and had a release in the last 120 days gets a row: with today's feed Claude Fable 5.2, GPT-6 Astra, GPT-6.1 Terra, Gemini 4 Carbon, Gemini 4 Argon, Gemini Omni 1.2 Flash and Gemini 3.5 Flash Lite, over two runs (at most two per vendor per run, 24 rows in all). Image, audio, embedding, preview and experimental models are left out, and so is a "Pro" variant of a listed family. A row you delete is remembered and not added again; nothing is removed automatically. "Add newly released models" in the panel turns it off.
- **Head to head with your local model** (`pricing.head_to_head`, a new card under the visual comparison). The window's busiest local model next to each paid service: benchmark score in a category you pick (coding, agentic work, reasoning, general chat, tool calling), points above or below the local model, what the window's tokens would have cost there, the extra over self-hosting, and dollars per point gained. The headline is the like-for-like saving: the cheapest paid model rated at least as high as the local one. Scores are the model registry's ratings (estimates, research, seeds, observed); each shows its source, and a score under 0.3 confidence is marked "rough", never hidden. A model with no score is listed as not rated.
- **The page refreshes itself.** "Cost for this window", the visual comparison and the rates table are re-read every minute while the tab is open (the rates table is left alone while you type in it).
- Each move is written to Events ("3 service(s) moved to a newer version") and shown in the update report as "(was …)".

**Brain-routed chats escalate when local is busy.** v0.181.118 made "escalate when local is busy" work for llama.cpp backends, but only on the direct path. A persona the brain routes with tools (Foundry-Chat) still queued behind whatever held Strata. `AgentRunner._busy_local_last` now tries the other models on the route first while the local one is busy (guardrail-checked as before), and falls back to the local queue when none is allowed.

Also in this release: `tests/coordinator/test_revamp_lead.py::test_the_hard_cap_is_absolute` expected the old PARK default for a Lead hard-cap stop. Since the 7 Nov decision-policy change the default is the capped self-repair (Victor, 2026-11-11: keep it); the test now says so. The cap itself is still checked: one lead session.

Tests: `tests/test_routing_modes_pricing.py` (family and newest-version rules, a full update with a locked row and a duplicate match, new families joining and a deleted row staying gone, both switches, the new schedule defaults) and `tests/test_pricing.py` (head to head).

## v0.181.118 — the shared-slot queue: classes, lanes, divert, pinning, recovery hold

Strata serves one stream. Chats, sprints, helper lanes and background jobs all wait for it, and until now the wait was first come, first served with no way around a busy backend. This is phase B of the 2026-11-10 platform audit (`reports/FOUNDRY_ROUTER_PLATFORM_AUDIT_2026-11-10.md`), blueprint build steps 1 to 4. Details: `docs/QUEUE_AND_LANES.md`.

**Queue classes** (`queue_class.py`). Every request has a class: **A** interactive, **B** coordinator required, **C** coordinator optional, **D** background. It comes from the coordinator purpose bound to the session (required purposes are B, optional ones C), else the persona's `queue_class`, else the client's `queue_class`, else `queue.default_class` (A). A request can lower its own class with `x-foundry-priority`, never raise it. Waiters are served by class, then clients take turns inside a class. A class waits the backend's `queue_timeout_seconds` times its `wait_factor` (1 / 2 / 3 / 6, so 300 / 600 / 900 / 1800 s on a 300 s backend) and counts as the next class up after `promote_after_seconds` (C 600 s, D 900 s; never above B).

- `FairGate` (the per-backend slot gate) serves by class, then round robin between clients. Its status lists each waiter with position, class and time waited.
- The ledger's wait for a backend **stream** (`helpers_allowed`) was an unordered 0.5 s poll: whoever looked first after a stream came free took it. It is now ordered by the same classes, and a newcomer goes behind those already waiting in its class or a better one. A session that already holds a stream is never queued behind anyone.
- A queued streaming call reports its place every 5 s as a progress chunk. The client's status line reads "queued behind 2 for its stream on strata (class B)" instead of "still working", and time spent queued no longer runs the stall clock.
- A caller cancelled between admission and the call no longer leaks the slot.

**Lanes** (`lanes.py`). A backend with `lane: true` is a side backend for short jobs (the brain host's llama.cpp is the first candidate). Its models are left out of automatic routing (`available_models()`), so nothing drifts onto a 32k-context model by ranking; a persona reaches it by naming the model, or through divert. A request must fit: at most `lane_max_prompt_tokens` (3,000), no tools unless `lane_allow_tools`, and no earlier assistant turn unless the conversation is pinned to the lane. On a lane the reply is capped at `lane_max_output_tokens` (400) and thinking is off unless `lane_thinking`. Failover never puts a request on a lane it doesn't fit.

**Try, then divert** (persona `divert_when_busy`). When the local model a request would use has no free stream or slot, and another local model on the persona's route (or any lane, for a flexible persona) is free and takes the request, it goes there instead of waiting. Paid models keep their place in a strict chain. Nothing free: it waits in the queue.

**Pinning.** A conversation served by a lane stays on it while it still fits. With persona `pin_conversation`, every direct-mode conversation stays on the model that answered its first turn while that model is on the route and allowed.

**Escalate when local is busy** now works for llama.cpp backends. It only looked at Ollama backends, so it never fired for Strata, although Foundry-Agent, Foundry-Chat, Foundry-Coding and Foundry-Vision have it on. "Busy" also means the backend's stream or slots are taken, not only a call in flight. The usage guardrail still gates the paid model (85% cutoffs).

**Hold for a backend coming back.** A strict persona whose only models sit on a backend that just went down was refused at once (57 refusals during the 2026-11-10 Strata outage, then the coordinator's backoff of up to 480 s). It is now held `queue.recovery_hold_seconds` (90 s; a person 20 s) and routed normally if the backend returns.

**Local llama.cpp models are free.** Discovery marked only Ollama models as free; a model on a local llama.cpp, vLLM or Unsloth backend (Strata) had no cost and the brain ranked it as unknown cost. They are now registered with cost 0 and tier `free`.

**Test fix.** `tests/test_helper_route_fallback.py` still named the pre-upgrade local model id (two tests failed since v0.181.115).

**Settings.** Access → Helpers and Limits: queue classes (on/off, default class, wait factor and aging per class), the recovery hold, a "waiting now" table, lane settings per backend and a lanes table. Clients and Keys: a client's queue class. Persona editor: queue class, divert when busy, pin conversation. API: `GET/POST /admin/api/setup/queue`; lane fields on `POST /admin/api/setup/limits`.

**Unchanged unless configured:** no backend is a lane and no persona diverts or pins until you set it. Queue classes are on: unbound traffic is class A as before; coordinator sessions now wait up to 2x (required) or 3x (optional) the backend's queue timeout instead of 1x.

Tests: `tests/test_queue_classes.py` (10) and `tests/test_lanes.py` (12, including a chat through `/v1/chat/completions` that lands on the lane while a sprint holds Strata).

## v0.181.117 — chat attachments reach media-gen as references

A picture attached in a chat (AnythingLLM, Open WebUI, anything on the OpenAI or Ollama facade) arrives as base64 inside the message, and a model can't copy megabytes of base64 into a tool argument, so "use this photo as the singer" or "make this picture talk" failed. media-gen v5.11 takes media as references and has a JSON upload endpoint (`POST <media-gen>/upload {"files": [{name, type, data}]}` → `{"uploads": [{ref, kind, file, sha256}], "errors": [...]}`).

New `media_attachments.py`, called once per chat request in `_chat_dispatch` (after the persona is resolved, before the strict / direct / agent / pipeline paths split), so it works for every client and backend:

- **When:** the persona's new `media_attachments` setting is on (default on; persona editor, next to the tool settings), this request's tool set includes a Media_Gen tool after tool policy and dedupe (direct mode: what `_direct_mcp_split` offers, or the finder pair that reaches it; agent mode: the persona's grants), and a user message carries media.
- **What:** `data:` images (Ollama `images` too) are uploaded; a URL on Foundry's own file store (`/media/a/`) or a host in `media.attachments_fetch_hosts` is fetched here and uploaded; any other public http(s) URL is passed as its own reference (media-gen fetches URLs). `input_audio` parts and file parts with an image, audio or video type are handled the same way. Every user message is processed, not just the last, so "the photo I sent earlier" works. The OpenAI facade now keeps http(s) image URLs and audio / video / file parts on the side (`_media_urls`, `_media_parts`); `_canonical_messages` drops those keys, so models never see them.
- **Injection:** in the same user message, after its attachments, one line: `[Attached for media tools: image 1 = upload:...; image 2 = ...]`, numbered per kind across the conversation. The pictures stay in place for vision models. The system prompt is unchanged; one sentence telling the model to pass the references to Media_Gen tools goes after the last line.
- **Cache:** sha256 → reference, an in-memory LRU of 400, so the same picture is uploaded once (media-gen also dedupes by sha256).
- **Failures never block the chat:** 15 s upload timeout; an unreachable media-gen, a rejected file, an attachment over 50 MB or unreadable data becomes `[image 1 couldn't be passed to media tools: <reason>]` plus a warning event (persona and reason, never the data). Uploads are batched under media-gen's limits (20 files, 64 MB).
- **Config:** `MEDIA_GEN_UPLOAD_URL` (env) wins; else `media.attachments_upload_url`; else the Media_Gen MCP server's URL with `/mcp` (or `/sse`) replaced by `/upload`. Both media settings are in Settings → Images (*attachment upload URL*, *fetch hosts*). One INFO event per request that injected references (count, kinds, size, time).
- **Unchanged:** requests without attachments (the same message list goes through), streaming, tool policy and dedupe, the Media_Gen server and tool names, and the speech facade.

Tests: `tests/test_media_attachments.py` (extraction from string / list content, several images, earlier turns, http URLs, audio, non-media parts; injection and numbering; the LRU; failure notes; own-store vs public URLs; the upload address; a real local fake `/upload` server; and an end-to-end chat through `/v1/chat/completions` with cache reuse, no-attachment and setting-off cases).

## v0.181.116 — a budget rule no longer waits the backend's queue timeout

On 2026-11-09 a budget rule (`implementer-local-only`: paid calls of Foundry-Kilo-Implementer, 1 token per minute) kept the Implementer on local Qwen. `Ledger.reserve_wait` treated every denial with no reset time as a slot worth waiting for, up to the backend's `queue_timeout_seconds` (300 s). So each paid candidate in the Implementer's strict chain (Claude, then ChatGPT) waited five minutes before failing over, and every Implementer request sat about 10 minutes before reaching Strata (NW-1b: 682 s for a 73k-token prompt the model reads in about a minute; Strata showed no activity).

Now `queue_timeout_seconds` applies only to a stream-slot denial (`backend-helpers`: the backend serves the main stream plus `helpers_allowed`). Any other rule denial waits only the matching rules' own `wait_seconds` (default 0), so a "deny" rule fails over at once.

Test: `tests/test_budgets.py::test_a_rule_denial_does_not_wait_the_backends_queue_timeout`.

Live change the same night: the rule was removed and Foundry-Kilo-Implementer's chain set to the local model only (it was claude-sonnet-5-5 → gpt-6.1-sol → local).

## v0.181.115 — issue sweep: cleared pins stay cleared, unconfirmed aborts are retried, chat limits are settings

Victor paused everything for a local model upgrade on 2026-11-10 and asked for an issue sweep. Fixes:

- **Re-enabling a backend re-pinned eight personas** (router). `profiles.fill_placeholders` treated an empty `backend` as an install placeholder. On 2026-11-09 re-enabling meridian and cliproxyapi pinned Haiku, Opus, Sonnet and Vision to meridian and Astra, Luna, Sol and Vision-Deep to cliproxyapi, which made them strict to one backend and cut off their local fallback (Victor's rule since 2026-11-06). An emptied backend on an edited persona is now the operator's choice and is never refilled. Test: `tests/test_persona_profiles.py::test_a_cleared_backend_is_not_refilled_when_backends_change`.
- **A pause or cancel the CLI could not confirm left the session running** (coordinator). The CLI has no Kilo password, so its abort failed silently: VQ-13's session kept calling models for 45 minutes after its pause. `engine._abort_or_queue` records an unconfirmed abort (`abort_pending:<session>` + event `abort_pending`), and the running coordinator's self-heal sweep retries it (`selfheal.pending_aborts`) until Kilo confirms or no longer has the session (20 tries). Test: `tests/coordinator/test_selfheal.py::test_an_abort_kilo_did_not_confirm_is_retried_by_the_sweep`.
- **12,108 Kilo errors a day from judge "sessions"** (coordinator). Evidence recovery asked Kilo for every stored session of a sprint, including the visual / asset judges' and helpers' router calls (client `foundry-direct`, ids like `visual-approval-…`). Kilo has no such sessions; each lookup was an error line, and Kilo's log reached 1.1 GB. Those rows are skipped. Test: `tests/coordinator/test_evidence_ledger.py::test_recover_never_asks_kilo_for_a_judge_session`.
- **The same skills-install refusal was logged 115 times a day** (coordinator). It is logged once per reason now. Test: `tests/coordinator/test_skills_install.py::test_a_tracked_target_is_refused`.
- **The Automation chat stopped at 8 tool calls / 4 model rounds** (router). Both were constants; a model-swap check needed more reads. They are now `operator_chat.max_tool_calls` (default 30) and `max_model_calls` (default 10), set in Automation → Chat → Settings → *Limits for one turn*.
- **The new local model** `qwen3.8-flash-next-unsloth-ud-iq3_xxs` (Strata, 2026-11-10). The chat's built-in chain no longer names a Qwen build: its local step is the first model an enabled local backend serves. The AU profile's `training.local_models` and `lead_manager.expected_models`, and the router's Kilo profile default for Foundry-Kilo-Local, name the new id. (Every live persona's chain was switched to it through the admin API.)

The router half needs a router rebuild; the coordinator half applies on the coordinator deploy.

## v0.181.114 — a session that outgrows the window is handed off, and failover gets the whole history

NW-1a ran three hours on local Qwen without a commit (2026-11-09). Its Kilo history reached about 203k tokens. The Implementer's chain is Claude, then ChatGPT, then local Qwen; both paid steps were budget-denied, so Qwen (262k window) served every turn. Two faults made it crawl:

- **The router trimmed for the wrong model.** The direct path's context guard fits the conversation to the first candidate's window (Claude's 200k) before dispatch. When failover moved to Qwen, Qwen still got that trimmed history. Each turn's trim rewrote the start of the prompt, so Qwen re-read 75-145k tokens every request (5-11 minutes a turn). Now, when failover moves to another model, the client's untrimmed history is fitted to that model's window (`_refit_for`, buffered and streaming paths). A model with room gets the whole history and a stable, cached prefix.
- **The coordinator never saw the real size.** Kilo recorded only the trimmed prompt size (125-145k), under the 167.8k handoff line, so the session was never wrapped up and handed off. The router writes "context guard: ~203k tokens would overflow …" into the reply's reasoning. `context_window.context_of` now takes that real size when it is larger than the reported counts, so the wrap-up and handoff fire. The wrap-up turn's hard stop still reads the reported counts only, so the note cannot stop the turn that commits the work.

Tests: `tests/test_thinking_context.py::test_failover_refits_the_history_for_the_model_that_serves`, `tests/coordinator/test_context_guard.py::test_a_router_trimmed_session_is_handed_off_by_the_guard_note`.

The router half needs a router rebuild; the coordinator half applies on the coordinator deploy.

## v0.181.113 — a CLI-set agent cap reaches the running coordinator

On 2026-11-09 Victor asked for one sprint at a time. `tracks set-max-agents animaluprising 1` from the CLI printed "max_agents 1" and wrote the store, but the running coordinator kept its in-memory cap of 4 (the CLI changes only its own copy of the profile), and VQ-13 started on the vq track 80 minutes later. The cap took effect only after a coordinator restart. `TrackManager.max_agents()` now reads the stored cap on every call, so the running coordinator follows a CLI change at once. A router revision or a profile-file change still clears the stored value, as before.

Test: `tests/coordinator/test_tracks_manager.py::test_a_cap_set_from_another_process_reaches_the_running_coordinator`.

## v0.181.112 — many more tool rounds per request, set globally and per persona

The worker tool loop and direct mode stopped after 6 tool rounds (`agent_brain.worker_tool_max_steps`), and the brain after 12 steps. Those limits date from when local workers looped on tool calls. Since then the poll guard, the paid-call cap, the context guard and the wrap-up round at the cap stop a runaway loop, and Victor's intricate tasks use many tools in one request.

- **Higher defaults:** 25 tool rounds and 30 brain steps. Config migration v5 raises a file that still has the shipped 6 or 12; a value you edited stays.
- **Per persona:** the new `max_tool_steps` (persona editor: *tool steps*, 1-200; empty = global) sets that persona's tool rounds in the worker loop and direct mode, and its brain step limit (+2 for routing and the final answer). A `max_steps_per_request` in the persona's guardrail_overrides still wins for the brain.
- **In the UI:** *tool steps per request* in Agent brain settings (it had no control before) and *tool steps* in the persona editor. The chat's `set_persona_fields` takes `max_tool_steps`.

Search-only turns with find on demand still do not count (v0.181.111).

Tests: `tests/test_worker_tools.py::test_a_persona_tool_step_limit_overrides_the_global_one`, `::test_tool_step_defaults_and_the_migration`.

## v0.181.111 — looking a tool up no longer uses up the tool-step limit

With find on demand (v0.181.109) every tool costs two model turns, a search and a call. The worker tool loop stops at `worker_tool_max_steps` (6) and hands the request to the brain, so a find-on-demand persona could run only about three tools per request. Three fixes:

- **Search turns are free, up to `tool_finder.FREE_FINDS` (6) per request.** In the worker tool loop and the brain's loop, a turn whose only calls are `foundry_find_tools` gives its step back. Direct mode adds the same 6 rounds to its limit whenever the finder tools are offered. Real tool calls still count as before.
- **Search results arrive whole.** The brain saw at most 2,000 characters of an MCP result (`mcp_result_limit_chars`), which cut the returned argument schemas mid-JSON. A search result now gets up to 12,000 characters, and it no longer becomes the "last tool result" that `return_to_user(use_last_result)` would send to the user.
- **A repeated search is not a stuck poll.** The worker loop's poll guard stopped the loop on a second identical call, which ended a request that looked the same tool up twice. Searches are exempt; tool calls are still guarded.

Test: `tests/test_worker_tools.py::test_find_turns_do_not_use_up_the_worker_step_cap`.

## v0.181.110 — the finder accepts the call shapes small models get wrong

Local models (Qwen on Strata) handle a two-step "find, then call" well when the schema comes back in the result, but they often get the shape of the second call slightly wrong. Each of these used to cost a turn on an error; `foundry_call_tool` now accepts them:

- `tool` / `tool_name` for `name`; `args` / `parameters` / `input` for `arguments`; `q` / `search` for `foundry_find_tools`' `query`;
- the arguments as a JSON string, also inside a code fence;
- the tool's own arguments placed next to `name` instead of inside `arguments`;
- a name that differs only in case or separators (`Crawl4ai/Crawl`, `searxng.searxng_web_search`).

A name that still matches nothing gets the three closest granted tools back, with their schemas, so the next call can be right. The persona's grants still decide what runs.

A find-on-demand persona whose model calls a granted tool by its own name, skipping `foundry_call_tool`, no longer fails: the brain loop and the worker tool loop send that call through the finder, so it is checked against the grants and runs (the worker loop used to give up and hand the request to the brain).

Tests: `tests/test_tool_finder.py::test_loose_call_shapes_from_small_models_are_accepted`, `::test_the_brain_routes_a_direct_tool_call_through_the_finder_gate`.

## v0.181.109 — personas can find MCP tools on demand

Every granted MCP tool definition rides in the prompt on every turn: Foundry-Chat's 58 were about 13.4k tokens, read cold on a new chat (about 11 s on Strata). Victor asked for a find/call option, gated per persona, so a chat persona can reach any tool without giving each client its own MCP servers.

- **MCP tool loading** (persona editor; `mcp_tool_mode`). *All up front* is the default and today's behaviour. *Find on demand* gives the model two Foundry-owned tools instead, about 400 tokens: `foundry_find_tools(query, limit?)` returns the best matches with server, description and argument schema, and `foundry_call_tool(name, arguments)` runs one. Matching weighs words in the tool name, then the server name, then the description; a query that matches nothing lists the servers it can use.
- **The grants stay the gate.** Search sees only the persona's granted tools, and a call to anything else is refused with an error the model can recover from. A new *every server* box grants `"*"`, servers added later included. With it on, a server or tool left unticked is stored as `"!server"` / `"!tool"` and excluded, so a persona can reach everything except what you hold back. Exclusions also narrow a plain grant. Gateway admin tools are never granted, as before.
- **Every route**: the brain's loop, the worker tool loop, direct mode (when tools ride along with the client's own; *MCP tools with client tools* = off still sends none) and the aggregator's `/mcp/persona/<Persona>/` endpoint. A real call made through `foundry_call_tool` goes the usual way: media hand-back, tool jobs, poll guard, MCP metrics.
- **Prompt cache**: the two tools never change between turns, so the cached prefix holds. Found schemas arrive as tool results, appended to the conversation like any other result.
- The chat's `set_persona_fields` takes `mcp_tool_mode` and the `"*"` / `"!name"` grants.

Test: `tests/test_tool_finder.py`.

## v0.181.108 — main-checkout sessions get the track tool cuts

v0.181.76–79 cut tools a session never uses, but only in a track's own kilo.jsonc. The main checkout's file is also the source every track's file is built from, so it was never trimmed in place: main sprints (UI-V2, POL-FIX1, the NW chain) still sent the official `Roblox_Studio` server's 36 tools (about 20.8k tokens), the unused Kilo built-ins (about 7-8k) and the full skill list on every turn. A main session's first turn was about 105k tokens, against about 80k on a slot track.

Kilo leaves a tool out of the list it sends when the session's own permission rules deny it outright. Checked against Kilo 7.8.8 with a fake model that records each request: a session `bash` deny took the list from 96 tools to 95, and a `re*` deny removed `read`. So every implementer session, on the main checkout and on tracks, now starts with the cuts as its own deny rules, worked out per sprint:

- `role_rules.UNUSED_SESSION_TOOLS` (the built-ins no session called in a 1,000-request sample);
- `Roblox_Studio_*`, unless the sprint's plan or a skill page it preloads names the official server or one of its own tools (`[automation] studio_official_mcp_on_demand`);
- `roblox-docs_*`, for a Blender sprint whose plan and preloaded skills do not name it.

No file is rewritten and Kilo is never reloaded. The set is fixed for a session's life, so it does not bust the prompt cache. The skill list is unchanged on the main checkout. The new `[automation] session_trim_unused_tools` (default on) turns it off. Sessions that started before the deploy keep their tools.

Tests: `tests/coordinator/test_kilo_config_track.py::test_every_implementer_session_starts_with_the_track_tool_cuts`, `tests/coordinator/test_media_gen_sessions.py::test_an_implementer_session_starts_without_unused_builtins_or_the_official_studio`.

## v0.181.107 — a slot sprint runs its own in-Studio checks

VQ-10 rebuilt EN-BADGER, EN-BAT and EN-BEAR, passed the scripted gate, and then left the in-Studio ASSET_CHECK "owed to the coordinator" twice, so its findings repeated and it parked BLOCKED. Nothing else runs that check: VQ-14 ran the same REGISTER/ASSET_CHECK itself on slot-2. A slot sprint's prompt now says that an in-Studio check a plan step names (ASSET_CHECK, REGISTER, a capture, a playtest row) is the sprint's own, run with its Studio MCP tools on the slot and logged as `PASS:` lines; the scripted gate runs only the test suites.

Test: `tests/coordinator/test_rules_streamline.py::test_a_slot_sprint_runs_its_own_in_studio_checks`.

## v0.181.106 — the visual judge ignores capture-environment chrome

UI-SQHP1's HUD frames were sent back round after round for Studio's viewport navigation widget and the Roblox core topbar drawn over the capture. No sprint can remove either. On 2026-11-09 the sprint stopped as an owner step, asking Victor to change Studio settings. The review prompt now carries `visual_approval.ENV_CHROME`: the Roblox core topbar, Studio's navigation widget, view cube and axis gizmo, the device simulator's touch thumbstick and jump button, and Studio window chrome are never defects, and neither is anything Studio draws over the game. The game's own UI placed under the core topbar is still a defect, since the topbar is in the live client too.

Test: `tests/coordinator/test_visual_env_chrome.py`.

## v0.181.105 — a Lead question whose user action is "none" takes the Lead's recommendation

A Lead escalation with any `user_action` text counted as an owner step, and an owner step may only time out to PARK. v0.181.80 exempted texts that ask for a decision ("Decide whether ..."). On 2026-11-09 VQ-14's Lead wrote "none; a scope decision only. Grant or deny adding assets/roblox/Enemies/EN-OWL.rbxm ...". The question timed out to "stop the task", and VQ-14 was parked instead of getting the recommended Grant. `_decision_only` now also treats a user action that starts "none", "n/a" or "no action", or that offers "grant or deny" (allow / refuse / accept / reject), as a decision. A named human act ("Victor uploads ...") stays the owner's.

Test: `tests/coordinator/test_audit_d_decisions_commits.py::test_a_lead_question_whose_user_action_is_none_takes_the_leads_recommendation`.

## v0.181.104 — a digit-less sprint id owns its commits (VR-AS-SYNC, VQ-SYNC)

The scope check leaves out commits another sprint made (`bookkeeping.sprint_owner`). An id prefix counted as a sprint only when it contained a digit, so the goal of keeping out housekeeping prefixes (`plans:`, `docs:`) also dropped `VR-AS-SYNC:` and `VQ-SYNC:`. On 2026-11-09, VR-AS-SYNC's committed evidence, summary and gate files fell inside VR-MAP2's range. VR-MAP2's scope check failed three times, a "findings repeat" decision opened, and the Lead had to add another sprint's files to VR-MAP2's plan. A prefix in capitals with at least one hyphen (`[A-Z]{2,}(-[A-Z0-9]{2,})+`) now counts as a sprint id too. `plans:`, `docs:`, `Merge:` and `WIP:` stay housekeeping.

Test: `tests/coordinator/test_revamp_gate.py::test_a_digit_less_sprint_id_owns_its_commits`.

## v0.181.103 — the gate pipeline keeps a run whose HEAD moved by docs-only commits; a tooling stop during VERIFYING carries its verification id

1. On 2026-11-09 03:05, an operator `plans:` commit (docs/progress only) landed between the host half and the Studio half of VR-MAP2's gate. The Studio half passed at the new HEAD and wrote `<new>.calls.json`. The pipeline looked only for `<start>.calls.json`, found none, and recorded "ERROR ... CALLS VR-MAP2 PASS". When the verdict is missing at the start HEAD, `run_pipeline` now reads HEAD again. If every change since the start is under `docs/progress/` or `docs/evidence/` (`docs_only_since`) and the Studio half's verdict at the new HEAD belongs to this run, the pipeline uses it and runs `-Finish` at that HEAD (`head_moved_docs_only` in the record).
2. A scripted-gate tooling ERROR while the task was VERIFYING failed with "VERIFYING -> BLOCKED needs evidence: verification_id", so the task was never stopped. `_tooling` now passes the task's verification id, or the run id when there is none.

Test: `tests/coordinator/test_studio_calls.py::test_pipeline_keeps_a_run_whose_head_moved_by_docs_only_commits`.

## v0.181.102 — a dependency waits for the model judge's visual verdict

`[approval] deps_accept_pending = ["visual"]` lets a sprint start while its dependency waits for its visual approval, so a chain does not stop on a human. With `visual_model = true`, the model judge gives that approval within minutes, and it can reject. On 2026-11-09, VQ-14 started at 02:32 on VQ-0's pending approval, and at 02:33 the judge sent VQ-0 back for changes. VQ-14 had started from a method pick that was not final. `_dep_satisfied` now accepts a pending approval only when a human gives it (`situation.model_approves` is false for that kind). Track queue order, without a declared dependency, is unchanged.

Test: `tests/coordinator/test_engine.py::test_a_dependency_waits_for_the_model_judges_visual_verdict`.

## v0.181.101 — the gate's end-of-run HEAD check allows docs-only commits; the viewport pin falls back to a listed device

1. v0.181.99 let a docs-only commit keep the host-half verdicts at the start of the Studio half. The end-of-run check still failed: on 2026-11-09 an operator `plans:` commit (docs/progress only) landed during VR-MAP2's 75-minute gate, and the run ended "tooling: HEAD moved during the run (2b5ba63740ec -> 948d2991b635)" and opened a Lead case. When every change between the start and the current HEAD is under `docs/progress/` or `docs/evidence/` (`docs_only_since`), the gate now writes a NOTE line and keeps the run. Any other move is still a tooling error.
2. A Studio update on slot-1 dropped the `average_laptop` built-in device ("not an available built-in device. Available: ..., average_windows, hd_1080, ..."). FIX-1's client CALLs then ran at 1920x1060, and the sprint stopped. `set_play_viewport` now tries `PLAY_DEVICES` in order (`average_laptop`, `average_windows`, `hd_1080`); the resolution override sets 1920x1080 on any of them. The VIEWPORT line names the device used.

Tests: `tests/coordinator/test_studio_gate_calls.py::test_a_docs_only_commit_during_the_run_keeps_the_lines`, `::test_a_studio_without_average_laptop_uses_the_next_device`.

## v0.181.100 — slot sprints can bring files Studio saves back into their worktree

On a slot, the Studio MCP servers run inside the slot container, so a tool that saves a file (`export_rbxm`'s `output_path`) writes it there. On 2026-11-09, ART-POI1 (slot-2) passed a workstation path, the file landed at `/home/studio/C:\Roblox\...` in the container, and the sprint reported BLOCKED: "not synced back". Foundry now writes a `foundry-slot-fetch` drop-in for each slot worktree (beside foundry-blender's), and the AU prompt for a sprint whose Studio is on a slot says to save into the slot checkout (`/home/studio/work/AnimalUprising/<repo-relative path>`) and run it to copy the file into the same path in the worktree. `python -m foundry_coordinator.slots fetch --config <json> PATH...` copies only the named repo paths (never `.git` or anything outside the repo). Exit codes: 0 copied, 2 bad path, 3 slot error, 4 missing on the slot.

Tests: `tests/coordinator/test_slot_fetch.py`, `tests/coordinator/test_rules_streamline.py::test_a_slot_sprint_is_told_how_to_bring_studio_files_back`.

## v0.181.99 — a docs-only commit keeps the gate's host-half verdicts valid

The Studio half of the scripted gate (`calls_preconditions`) required the host half's `<ID>_gate_verdicts.json` to be stamped with the exact current HEAD. On 2026-11-09, two operator plan commits (`plans: ...`, files under `docs/progress/` only) moved HEAD while VR-MAP2's gate ran. Both times the Studio half was refused "stamped <old>, HEAD is <new>: the host half must pass at the current HEAD", and a Lead case opened. A commit cannot change the host half's result when every file it changes, between the stamp and HEAD, is under `docs/progress/` or `docs/evidence/` (plans, summaries, evidence logs and captures). Such commits now keep the verdicts valid (`studio_gate.docs_only_since`: the stamp must be an ancestor of HEAD). Any other change still needs a fresh host half.

Test: `tests/coordinator/test_studio_gate.py::test_a_docs_only_commit_keeps_the_host_half_verdicts`.

## v0.181.98 — a sprint resumed past the queue check still waits for a cancelled dependency

The first-start check (`_unmet_start_deps`) skipped a FAILED or CANCELLED dependency and left it to the QUEUED check, which blocks the sprint. A Lead resume moves a sprint from BLOCKED to READY, past the QUEUED check. On 2026-11-08, VR-AS-SYNC was cancelled at 20:1x so it could rerun after the vr-e merge. VR-MAP2, depending on it, went BLOCKED; its Lead case's decision timed out at 20:21 and resumed it, and it started at once, ahead of VR-AS-SYNC. A queue dependency that is cancelled or failed now counts as unmet at start ("waiting for VR-AS-SYNC (CANCELLED)"). Dependencies named only in the task source keep the old behaviour.

Test: `tests/coordinator/test_engine.py::test_a_sprint_resumed_past_the_queue_check_waits_for_a_cancelled_dependency`.

## v0.181.97 — a main sprint waits until its dependency's track has merged

A COMPLETE dependency satisfied every dependent, even while its work was still on an unmerged track branch. On 2026-11-08, VR-AS14 completed on vr-e at 19:59 and VR-AS-SYNC (main checkout, depending on all 43 VR sprints) started at once. vr-e still waited to merge, both on the busy main checkout and on a Lead repair of an ASSET_LEDGER.md conflict, so VR-AS-SYNC regenerated the asset ids without VR-AS11..14's models. The watch cancelled it before it completed, and it reruns after the merge.

A COMPLETE dependency now satisfies only sprints on its own track while that track is live (planned, running or merging). Every other sprint waits, with the note "VR-AS14 (COMPLETE, track vr-e not merged yet)". The QUEUED check and the first-start check (`_unmet_start_deps`) both apply it.

Test: `tests/coordinator/test_engine.py::test_a_main_sprint_waits_until_its_dependencys_track_has_merged`.

## v0.181.96 — a Lead case waiting for its worktree is not handed back

An automation decision whose Lead case never made its call (DEFERRED on readiness, quota or admission, or not started) is handed back to the capped self-repair when its timeout comes due (2026-10-28). A case that waited only for its task's worktree was treated the same way. On 2026-11-08, VR-AS14's case opened at 17:01 and waited for vr-e's worktree while VR-AS13 held it. At 17:04 the 3-minute decision timeout handed it back "without a lead call", the self-repair counted the Lead repair as used, and the sprint was parked. While another task holds the task's worktree (`selfheal._ws_lock_held_by_other`, track-aware since v0.181.84), the decision now defers 5 minutes at a time, as it does for a working case.

Known: `tests/coordinator/test_revamp_lead.py::test_the_hard_cap_is_absolute` already failed before this release. A Lead hard-cap escalation defaults to self-repair, not park, since v0.181.82.

Test: `tests/coordinator/test_lead_handback.py::test_a_case_waiting_only_for_the_worktree_is_not_handed_back`.

## v0.181.95 — a visual round brings two attempts

Each visual fix round added one implementation attempt. A round's fix is an attempt, and a code review that sends the fix back is another, so VR-AS13 used 11 attempts by round 5 of 8. It opened four `attempts_exhausted` Lead cases between 15:27 and 16:30 on 2026-11-08, each a paid Lead call and a wait for the track's worktree. The last one also met a router outage and asked Victor. A visual round now adds two attempts (`obligations.VISUAL_ROUND_PER_KEY`), next to two review calls and three sessions.

Test: `tests/coordinator/test_obligations.py::test_visual_fix_rounds_bring_their_own_attempt_and_review_room`.

## v0.181.94 — a coordinator hold says what it waits for

v0.181.93 labelled a hold "Held by the coordinator (task:animaluprising/PT-4)". AS-VR1 and MV2-13 now read "Held until PT-4 finishes". The other hold owners read the same way: until its session ends, until Lead case X closes, until its process exits, or until a prep landing finishes.

Test: `tests/coordinator/test_situation.py::test_pauses_say_who_paused_and_ignore_a_stale_session_note`.

## v0.181.93 — the GUI says what a task is actually doing and who has to act

Victor 2026-11-08: "the GUI is read and managed by a human ... if its awaiting human, then that means a HUMAN must intervene, if its waiting for Visual Inspection then it should reflect that ... look for other gaps like this". The engine's states are its own bookkeeping, and the Control Center showed them raw:

- AWAITING_HUMAN also covered the model judge's visual review. VR-AS10 read "Waiting for your answer", and every visual round raised a critical "human visual approval needed" alert.
- BLOCKED also covered a Lead repair in progress, with the prompt "Clear the blocker, then unblock" and a high "is BLOCKED" alert.
- A decision with a default looked the same as one waiting for Victor.
- PAUSED sprints kept the note of the session they paused (MV2-13: "implement session working").

New `foundry_coordinator/situation.py` reads the state together with the approval policy, open decisions, the Lead case, background work and the wait note. It returns `{code, label, who, detail}`, where `who` is `human` only when a person must act; otherwise `model`, `lead`, `agent` or `coordinator`. Codes:

| Code | Meaning |
|---|---|
| NEEDS_YOU | A person must act. |
| AWAITING_HUMAN | A person approves. |
| VISUAL_REVIEW | The model judge is reviewing. |
| LEAD_RECOVERY | The Lead is repairing. |
| DECISION_PENDING | A decision whose default applies at a stated time. |
| WAITING_TURN | Waiting for a shared resource. |
| STUDIO_GATE | Studio checks are running. |
| CODING | A coding session is working. |
| CODE_REVIEW | The reviewer model is reviewing. |
| FIX_ROUND | Changes from review or the judge are next. |
| PAUSED / HELD | Paused by you, or held by the coordinator. |

Task telemetry carries it as `situation`. The Control Center shows its label on the task badge, the task picker, the composer target, the sprint "Coordinator" line, and the Automation queue and board, with the engine state in the tooltip. The page lists "needs you" items only for `who: human`. `control_center._in_task` raises approval and blocked alerts only for `who: human`, and clears open ones when the situation says no one has to act. A coordinator without `situation` keeps the old alerts.

The router serves the Control Center, so the page and alert changes take effect when the router is updated to this release.

Tests: `tests/coordinator/test_situation.py`.

## v0.181.92 — a track worktree goes to the sprint first in queue order

Only the gpu and claude units went in queue order (`_gpu_first_in_line`, 2026-11-02 AS-A02). A track's worktree lock went to whichever sprint the tick stepped next after the release. On 2026-11-08 at 11:50, VR-AS18 released vr-d's worktree and VR-AS19 (stepped later) took it, while VR-AS16 (queue position 278, at its last visual round) had been waiting for it since 11:45. On a track, a sprint that wants the worktree now steps aside for an earlier sprint in queue order that is already waiting for it. Only READY and CHANGES_REQUIRED waiters count, so a sprint mid-gate with an old note never holds the line.

Test: `tests/coordinator/test_engine.py::test_a_track_worktree_goes_to_the_waiter_first_in_queue_order`.

## v0.181.91 — a reset sprint that ran before no longer waits behind fix rounds

On a track, a new sprint waits while another sprint's fix round is pending (v0.181.61, "fixes first"). "New" meant attempt 0 and no `started_ts`, and an operator unblock with `--reset-attempts` clears both. On 2026-11-08, VR-AS16 (15 attempts, visual round 8 of 8) was reset at 06:54 and then waited on vr-d for 5 hours while VR-AS17, VR-AS18 and VR-AS19 took turns with their fix rounds. Only a sprint that never ran (no `run_id`) now waits for another's fix round; the dependency check for a fresh start is unchanged.

Test: `tests/coordinator/test_engine.py::test_a_reset_sprint_that_ran_before_does_not_wait_for_fix_rounds`.

## v0.181.90 — a visual round brings three sessions

Each visual fix round added one session to the sprint's budget (base `max_sessions_per_task` 18). A round's fix often spans a context handoff plus its closeout session, so VR sprints used about three sessions per round above the base: VR-AS09 had 39 at round 7, VR-AS11 37 and VR-AS40 40 at round 8. Each extra session past the budget opened a `sessions_exhausted` Lead case that granted one more. VR-AS09 opened three of them between 09:15 and 10:01 on 2026-11-08, each a paid Lead call and a wait for the track's worktree. A visual round now adds three sessions (`obligations.VISUAL_ROUND_PER_KEY`), still bounded by `[approval] visual_rounds_max`.

Test: `tests/coordinator/test_obligations.py::test_visual_fix_rounds_bring_their_own_attempt_and_review_room`.

## v0.181.89 — a visual round brings two review calls

Each visual fix round added one review call to the sprint's budget (v0.181.57). A round's fix is reviewed, and reviewed again when the reviewer sends it back, so VR sprints used about two per round. VR-AS12 parked at round 5 of 8 on 10 of 10 review calls with the 2/day automatic cap used. VR-AS16 had 13 at round 8, and VR-AS09 12 at round 6. A visual round now adds two review calls (`obligations.VISUAL_ROUND_PER_KEY`); attempts, sessions and closeouts still add one each. Rounds stay bounded by `[approval] visual_rounds_max`.

Test: `tests/coordinator/test_obligations.py::test_visual_fix_rounds_bring_their_own_attempt_and_review_room`.

## v0.181.88 — a sprint at its last visual round gets the review call that ends it

A sprint's automatic review-call extensions are capped per day (`[automation] self_repair_max_reviews_per_day`). On 2026-11-08, VR-AS16 reached visual round 8 of 8 with 13 of 13 review calls used and today's 2 extensions taken, so its Lead case asked Victor whether to stop it, one review short of the end. The last visual round always ends the sprint: it is approved, or accepted with notes at `visual_rounds_max`. A task whose `visual_changes_rounds` has reached `[approval] visual_rounds_max` now gets one review extension above the daily cap. That covers both the engine's ledger check (`engine._policy_cap(project, counter, task)`) and the Lead's `review_extension_room`.

Test: `tests/coordinator/test_lead_review_budget.py::test_a_task_at_its_last_visual_round_gets_one_more_review_call`.

## v0.181.87 — the visual judge sees the frames of the findings it rules on

The visual judge took the first `BATCH_FRAMES` (10) named frames in alphabetical order, while the findings kept their own order. When findings named more than 10 frames, the earliest findings could lose their frame. On 2026-11-08 at 04:32, VR-AS15's first three findings named EN-SQUIRREL_q34_front, which sorted last and was cut, so the judge answered "This frame was not attached". In the past 24 hours, 11 judge replies said a frame was missing. `visual_approval._judge_batch` now takes frames in the order the findings first name them, and sends a finding only while its frame fits. Findings that don't fit stay real, as an unruled finding always has. A finding that names no frame is judged on the frames left over.

Test: `tests/coordinator/test_visual_model_approval.py::test_the_judge_sees_the_frames_of_the_findings_in_their_order`.

## v0.181.86 — an edit-VM gate step loads a changed check module fresh

Studio's edit VM keeps a required ModuleScript's result for the whole Studio session. A gate step that requires a check module after its source changed ran the old code. On 2026-11-08, VR-AS16's gate failed twice on the old vulture target (4.373 x 6.675) after `tests/checks/VR_AS16_check.lua` was updated to 4.490 x 8.014; a renamed copy of the check passed. Edit-VM steps that call `require` now run with a prelude (`studio_gate.fresh_require`): a ModuleScript whose `Source` changed since it was last loaded is required from a fresh, non-archivable clone beside it; an unchanged one keeps its loaded result, so state shared between steps still works. Modules required by those modules use the normal `require`.

Test: `tests/coordinator/test_studio_gate.py::test_edit_vm_steps_load_a_changed_check_module_fresh` (the reload rule was also run in Lune against a stand-in ModuleScript).

## v0.181.85 — a judge ruling on a finding that names no frame sees all the frames

The visual judge attached only the frames whose path starts a finding. The asset judge's "N of M checklist items could not be judged" and "checklist C4 ..." findings (v0.181.83) name no frame, so on 2026-11-07 at 23:09 VR-AS40's judge was sent no images and ruled on the reviewer's descriptions alone ("No frames were attached to this judging request"). When any finding names no frame, `visual_approval._judge` now also attaches the other frames, up to `BATCH_FRAMES`.

Test: `tests/coordinator/test_visual_model_approval.py::test_a_finding_that_names_no_frame_is_judged_on_all_the_frames`.

## v0.181.84 — a track Lead case waiting for its worktree is not stuck

The self-heal `stuck_case` rule skips a Lead case whose call waits for a workspace lock held by another task, but it only checked the main checkout's lock. A track task's Lead waits for its track worktree's lock. On 2026-11-08, VR-AS09's case (track vr-a) waited 30 minutes for the vr-a lock held by VR-AS08, was cancelled as stuck, and left VR-AS09 BLOCKED with no case. `selfheal._ws_lock_held_by_other` now also checks the lock of the task's own track worktree (`tracks.project_for`).

Test: `tests/coordinator/test_selfheal.py::test_a_track_case_waiting_for_its_worktree_lock_is_not_stuck`.

## v0.181.83 — a judge that cannot see the checklist items asks for better frames

The asset judge scores each checklist item as pass, fail or unclear. When more than half were unclear, `asset_judge.valid_score` returned `uncertain`, and in review-until-clean mode an uncertain answer is deferred: re-asked 30 minutes later, up to 6 times, then accepted with notes. That deferral is meant for a judge route that returned no valid verdict. On 2026-11-07, VR-AS41's judge (Sonnet, valid answers) said "14 of 22 checklist items could not be judged from the images" at 21:01, 21:32, 22:02 and 22:33. These were the same frames at the same revision, so each paid call could only repeat the answer, and the sprint would end up accepted with 14 items never checked. A valid review whose items are mostly unclear is now `changes_required`. It names each unclear item ("checklist <id> <part>: <check> -> not visible in any frame"), so the implementer adds or reframes renders that show them. An invalid or malformed answer is still `uncertain` and keeps the deferral.

Test: `tests/coordinator/test_asset_judge.py::test_valid_score_maps_letters_back_to_files` (the all-unclear case).

## v0.181.82 — a lead question that only asks for a decision is not an owner step

Every lead escalation carries a `user_action` text: what the owner would do. Any non-empty `user_action` made the decision category `user_action`, and since the GF-RT3 rule (2026-11-01) a `user_action` decision may only time out to PARK. So whatever the lead recommended, its 3-minute timeout applied "stop the task". This happened to PT-5 (lead cap), VR-AS04, and on 2026-11-07 VR-AS06. The lead asked whether VR-AS06 may keep the generated outputs of its own builders and recommended "Yes: keep the scope widening"; the sprint was parked instead. `decision_policy.category` now looks at a lead escalation's `user_action`. When it only asks for a decision (it starts with decide, choose, pick, answer, confirm whether, say whether, approve or, raise, lower, increase or set) and names no physical act (`needs_human_act`), the escalation is categorised by its reason: a lead's own question (`lead_asks`, `scope_core`, `external`) is product, so its matched recommendation applies at the timeout, and a cap or limit is automation, so it gets the capped self-repair. A physical act ("click Connect in Studio") or a `user_action` blocker is still the owner's, as before.

Test: `tests/coordinator/test_audit_d_decisions_commits.py::test_a_lead_question_that_only_asks_for_a_decision_is_not_an_owner_step`.

## v0.181.81 — a slot Blender job has a time limit

On 2026-11-07 two of VR-AS29's Blender jobs on slot-1, a `render_views.py` and a `mesh_report.py` for COS-TORTOISE-SHELL-SHIELD, hung for 7 h 42 min at 0% CPU. Together they held both of the slot's pool locks. From about 09:30 every other track's Blender run waited `pool_wait_seconds` (600 s) and exited 75 ("busy"). VR-AS31 and VR-AS37 parked on it, and VR-AS29 itself completed at 11:37 with other runs. Nothing ended a slot Blender job: when the coordinator side gave up, the remote `bash` and Blender kept running and kept the lock. The slot script now runs Blender under `timeout --kill-after=30 <max_job_seconds>`. A job past the limit is stopped, its lock is freed, and the log says so (exit 124, or 137 after the kill). The new setting `[blender] max_job_seconds` (default 3600, 0 = no limit) is in the Automation UI beside `pool_wait_seconds`. It reaches the drop-in's settings for both a single slot and the pool, and the build-replay rerun path uses it too.

Test: `tests/coordinator/test_build_journal.py::test_a_slot_blender_job_has_a_time_limit_that_frees_its_lock`.

## v0.181.80 — a router with a full database never costs a sprint an attempt

On 2026-11-07 at about 05:45 the router's own database filled. Every model call answered `database or disk is full` (code `backend_error`), and every session of every AU sprint ended at its first call. None of the infra signatures matched the error, so each failure was an ordinary failed attempt. In eight minutes nine sprints (PT-5, VR-AS06, 07, 21, 29, 31, 37, 38, 40) used their whole attempt budgets and opened lead cases. The router's `/health` kept answering ok throughout. A new `router_storage` signature (`database or disk is full`, `No space left on device`, `database is locked`, `disk I/O error`) joins `local_busy` in `infra.NEVER_EXHAUSTS`: its failures wait for the infra backoff and never cost an attempt, however many come in a row.

Test: `tests/coordinator/test_infra_outages.py::test_a_router_with_a_full_database_never_uses_an_attempt`.

## v0.181.79 — the unused Kilo built-ins are hidden through the track config

v0.181.78 hid `UNUSED_SESSION_TOOLS` through the role-rule defaults. AU's live role rules list their own `deny_tools`, so the defaults do not reach them, and a router role_rules revision waits for a safe point with no live session. AU always has a live session: router revision 29, which added the list, sat pending and also held back revision 30, a live-only persona override (the Haiku trial). Revision 31 put role_rules back as they were. A track's kilo.jsonc now adds the list for its implementer and lead agents when `[automation] track_trim_unused_tools` is on. That file is rewritten and reloaded at the track's own safe point, the next session start. The main checkout keeps its built-ins until a main-checkout safe point is designed (together with the official-Studio trim there).

Test: `tests/coordinator/test_kilo_config_track.py::test_unused_builtins_are_hidden_even_when_role_rules_list_their_own_denies`.

## v0.181.78 — sessions stop carrying tools, docs and skills they never use

Measured on 2026-11-07 from captured request bodies: a slot-track implementer turn sends 103 tool definitions (about 31.5k tokens) and a 14.3k-token system prompt before any sprint text. A session's first turn is about 80k tokens on a slot track and about 105k on vr-c and the main checkout, and that fixed start is 77% of all implementer tokens.

- **Unused Kilo built-ins (about 7-8k tokens a turn).** `role_rules.UNUSED_SESSION_TOOLS` lists `background_process`, `schedule_wakeup`, `cancel_wakeup`, `cron_*`, `link_pr`, `board_post`, `board_read`, `goal`, `goal_report`, `suggest`, `agent_manager_models`, `plan_exit` and the three MCP-resource tools. It is now in the default `deny_tools` of the implementer and lead roles. None of these tools was called in a 1,000-request sample. Kilo leaves a tool denied outright out of the list it sends; the capture confirms this, since denied `webfetch` and `searxng_*` are absent. A project with its own `deny_tools` adds the list in the router's Role rules editor ("Tools always denied").
- **roblox-docs on asset tracks (about 3.9k tokens a turn).** Its 27 tools repeat a lookup `robloxstudio-mcp` already has (`get_roblox_docs`), and VR sessions never called it. A track with the `blender` need turns it off unless one of its plans names `roblox-docs`. Code tracks keep it.
- **Skill list (about 120 tokens a skill).** The skill tool lists every allowed skill in the system prompt; AU allows 43. For the implementer and lead agents, a track now hides the generic `roblox-*` skills none of its sprints uses: not named in a plan's `## Skills`, not in `[skills] preload_by_task` and not in `preload_always`. That is exactly the set the coordinator preloads. The project's own `au-*` skills always stay listed, and reviewer agents keep every skill. An unreadable plan hides nothing.

Both track cuts follow the new `[automation] track_trim_unused_tools` (default on). They reach a track when its kilo.jsonc is rewritten (`tracks set-needs`, set-slot, start).

Test: `tests/coordinator/test_kilo_config_track.py::test_a_blender_track_drops_docs_and_skills_its_plans_do_not_name`.

## v0.181.77 — the official Studio server follows every job only it does

Victor (2026-11-07): the two Studio servers each do different jobs. The official `Roblox_Studio` server generates (mesh, scene, avatar, material, texture, animation), captures the screen, searches and stores Creator Store assets and images, drives navigation and user input, and runs Studio skills and Open Cloud. The community `robloxstudio-mcp` server runs playtests, runtime eval, logs, screenshots, the device simulator, profilers and rbxm import and export. v0.181.76 turned the official server on for a plan that named it or one of 15 of its tools. It now follows all 31 tools only the official server has (its `tools/list` minus the community one's), plus the phrase "official (Studio) MCP". Generic words a plan uses for other things (`skill`, `subagent`, and `multi_edit`, which is also a Kilo tool) do not count. 74 of the 395 AU plans name an official-only job; the current VR-AS plans name none.

Test: `tests/coordinator/test_kilo_config_track.py::test_the_official_studio_server_is_on_only_when_a_sprint_plan_names_it` (each official-only job turns it on; community-only jobs keep it off).

## v0.181.76 — a track gets the official Studio MCP server only when a plan needs it

Victor asked where Claude tokens go. In a 1,000-request sample (90 minutes, 2026-11-07), implementer turns were 91% of Claude prompt tokens, at about 131k tokens a turn with a 93% cache hit rate. Every turn resends the session's tool definitions. A track with the `studio` need turned on both Studio MCP servers. The official `Roblox_Studio` server lists 36 tools, about 20.8k tokens of definitions (measured with `tools/list`). VR asset sessions called it once in 90 minutes; their Studio calls go through `robloxstudio-mcp`.

The new `[automation] studio_official_mcp_on_demand` (default on) gives a track the official server only when one of its sprint plans (`docs/progress/<id>_PLAN.md` in the worktree) names `Roblox_Studio` or one of its own tools: `generate_mesh`, `generate_scene`, `screen_capture` and the rest. A missing or unreadable plan counts as needing it. Off restores the old behaviour. `robloxstudio-mcp`, the coordinator's own Studio gate (which starts its own MCP clients) and the main checkout's kilo.jsonc are unchanged.

Duplicate check (Victor's request): the router adds no MCP tools to Kilo sessions, since no Kilo persona has `preferred_mcp_tools`. Kilo's global config uses the same server names as the project file, so the project's settings win: git, crawl4ai and general-mcp-gateway, which are on in the global file, stay off. Skills reach a session as prompt text, not as tools.

Test: `tests/coordinator/test_kilo_config_track.py::test_the_official_studio_server_is_on_only_when_a_sprint_plan_names_it`.

## v0.181.75 — an unblock with a reset starts the self-repair ladder over

The self-repair ladder for a timed-out automation decision is lead repair, then a different approach, then one frontier session, then a park. It counts the steps from the task's self-repair log over a rolling 24 hours. An operator unblock with `--reset-attempts` gave a fresh attempt budget but kept that log. The first findings repeat after the unblock then went straight to "exhausted" and parked the sprint again. On 2026-11-06 Victor chose "more rounds" for VR-AS04 and VR-AS21. Each parked again within about an hour with "lead repair and different approach used; no frontier persona available", while 2 of their 8 visual rounds were still unused. An unblock with a reset now clears `self_repair_log`, so the ladder starts over with the new budget. The old steps stay in the `self_repair` events. A plain unblock without a reset keeps the log.

Test: `tests/coordinator/test_selfheal.py::test_an_unblock_with_reset_starts_the_self_repair_ladder_over`.

## v0.181.74 — a track with no Studio slot takes the workstation Studio lock

A track bound to a Studio slot runs its Studio work in the slot. A track that needs Studio but has no slot (vr-c, started 2026-11-05 as a fourth agent) reaches the workstation Studio through its Studio MCP servers, but its sessions took none of the main Studio's exclusive rows. vr-c's asset checks started and stopped play sessions in the workstation Studio while BAL-1 held it, and BAL-1's bot matches ended about 2 minutes in ("play session terminates by itself"). Victor chose "vr-c waits for Studio". A slot-less track whose needs include `studio` now adds the main profile's `studio:<host>` and `rojo:<port>` resources to its session locks. Its sessions wait while a main sprint holds the workstation Studio, the same as another main sprint would. Slot-bound tracks and tracks without the `studio` need are unchanged.

Test: `tests/coordinator/test_tracks_manager.py::test_a_slotless_studio_track_takes_the_workstation_studio_rows`.

## v0.181.73 — a budget case resumed without an extension gets the Lead's delegated one

A Lead case opened because a sprint used up its sessions or attempts (`sessions_exhausted`, `attempts_exhausted`) could be resolved by a write-access fix plus `reverify`, or by `wait` / `resume`, without adding budget. The sprint then stopped again at once on the same limit and opened a new case. On 2026-11-06 VR-AS04's case `rc-1f33fe50b6ba85ef` was resumed at 11:47, and the same minute "21 sessions used" opened `rc-b1519a8aed0445b8`. `_lead_resume` now grants the Lead's own delegated extension (`[lead_manager] extra_attempts`, attempts and sessions, at most once per case; event field `budget_grant`) when it resumes such a case with reverify, wait, resume or coordinator_repair. Cases of other classes are unchanged.

Test: `tests/coordinator/test_lead_manager.py::test_a_budget_case_resumed_by_reverify_gets_the_delegated_extension`.

## v0.181.72 — a sprint holding the workspace yields it to a waiting Lead case at its next attempt

v0.181.65 made a sprint wait when a Lead case on the same checkout waits for the workspace, but only a sprint that did not hold the workspace yet. On a track, a sprint in fix rounds keeps the worktree lock from VERIFYING back into CHANGES_REQUIRED and the next attempt, so it never let go. On 2026-11-06 VR-AS04's Lead case (`rc-1f33fe50b6ba85ef`) waited 45 minutes for vr-a while VR-AS02 went verify -> new attempt -> verify. Now, when a sprint that holds the workspace starts a new attempt (state READY or CHANGES_REQUIRED) and such a Lead case is waiting, the sprint releases the workspace (`workspace_yielded_to_lead`) and waits. A sprint mid-attempt (IMPLEMENTING, CLOSING) keeps it.

Test: `tests/coordinator/test_engine.py::test_a_sprint_holding_the_workspace_yields_it_to_a_waiting_lead_at_a_new_attempt`.

## v0.181.71 — the reviewer knows docs/memory is not the session's to fix

`docs/memory/` is the coordinator's project memory. Sessions may not edit it: they record facts as MEMORY_NOTE lines, which the coordinator commits, and a scope grant for a memory file only keeps done_check's SCOPE row green. The code reviewer was not told this. On 2026-11-06 it sent VR-AS34 back twice with required repairs such as "dedupe the docs/memory/ASSETS.md rows" and "merge the KNOWN_ISSUES row". Each time the session reported "the edit tool is denied for every path" (the memory file matched only the `edit *` deny) and went to a Lead case. The reviewer's system prompt now says docs/memory files are the coordinator's. It must never put a change to them in required_repairs or missing_evidence, never rate such a finding above minor, and mention it in residual_risks if it matters.

Test: `tests/coordinator/test_review.py::test_the_reviewer_is_told_docs_memory_is_not_the_sessions_to_fix`.

## v0.181.70 — review and judge schemas go out in OpenAI's strict form

The coordinator's one-shot calls (code review, visual judge, retry guidance, the launch assistant) send a JSON schema as `response_format`. OpenAI's structured outputs, which serve the ChatGPT models through CLIProxyAPI, require every object in that schema to set `additionalProperties: false`. The review schema did not, so gpt-6.1-sol refused every review with HTTP 400. On 2026-11-06 Claude's Sonnet window was used up. VR-AS21's review then fell from Claude past ChatGPT to the busy local model, and paused and retried every few minutes. `FoundryClient.chat` now sends `strict_schema(schema)`: a copy in which every object has `additionalProperties: false` and all of its properties in `required`. Claude and llama.cpp accept the same form. The schemas' field shapes are unchanged, so an optional field can still be empty or null.

Tests: `tests/coordinator/test_strict_schema.py`.

## v0.181.69 — the answer to a sprint's BLOCKED report is its next strategy

When a session reports BLOCKED with options, the coordinator opens an `implementer_blocked` decision. Victor answers it, or its timeout applies the recommended option. The next repair assignment did not use that answer as its strategy. If findings had repeated, it asked "findings repeat with no materially different strategy" at once and opened a Lead case, even though the answer was the new strategy and had not run yet. BAL-1 did this twice on 2026-11-05: at 19:43 after "reduce pad density" and at 22:36 after "weaken the castle defence". Now, when no other strategy is set, the latest answered `implementer_blocked` decision becomes the assignment's strategy for one attempt, trusted like a Lead strategy (`blocked_answers_used`, event `blocked_answer_strategy`). A repeat after that attempt escalates as before.

Test: `tests/coordinator/test_engine.py::test_a_fresh_blocked_report_answer_is_the_next_strategy_once`.

## v0.181.68 — an asset-judge call cut off by a coordinator restart is made once more

Asset-judge calls (checklist, per-candidate scoring) are journaled ops. One whose status was still `calling` when the coordinator restarted was never repeated ("outcome uncertain after restart; paid call not repeated"), and the sprint waited for a human. On 2026-11-05 the v0.181.67 deploy restarted the coordinator during VR-AS33's checklist call, and VR-AS33 sat in AWAITING_HUMAN. A judge call has no side effect besides its cost. So an op in `calling` that began before this coordinator process started (`lead.PROCESS_START`) is now made once more (`restart_retry`, event `asset_judge_restart_retry`). A call cut off a second time, or one this process is making, still waits as before.

Test: `tests/coordinator/test_asset_judge.py::test_a_judge_call_cut_off_by_a_coordinator_restart_is_made_once_more`.

## v0.181.67 — the scripted Studio gate boots once when only Edit-DM rows ran fresh

In `calls` mode the AU Studio gate runs boot CALLs 1-2 inside each playtest a suite group starts. When every row is carried or manual, it runs one boot-only playtest instead. A fresh run whose rows all execute in the Edit DM never started a playtest, so the boot gate never ran. `tools/gate.ps1 -Finish` (tier B needs two `PASS: BOOT counts` lines) then failed regardless of the sprint's work. On 2026-11-05 this blocked VR-AS21 twice (Lead cases `rc-2876e6b462617f50` and `rc-f37d487bf4c26613`) and hit VR-AS22's fresh tier-B runs. VR-AS21's one earlier PASS came only because every row was carried. Now, when no suite group ran the boot gate (`Gate.boot_tried`), the gate runs the same boot-only playtest after the calls lane. `_boot_only` also fills the ledger's boot evidence (`evidence.boot`). A run whose group starts a playtest still boots once per playtest, with no extra playtest.

Tests: `tests/coordinator/test_studio_gate_calls.py::test_edit_only_fresh_row_still_records_both_boot_pass_lines`, `::test_a_row_that_plays_still_boots_once_per_playtest_only`.

## v0.181.66 — a local-fallback hold ends when the sprint's implementer persona changes

When a frontier sprint's model fell back to the busy local model (`local_busy`), the sprint is held for the GPU for up to an hour (`local_fallback_until`). The hold ends early when another sprint with the same persona is served by the frontier, or when the v0.181.63 probe gets a non-local answer. On 2026-11-05 at 19:55, VR-AS22's last session on the Claude persona (Foundry-Kilo-Implementer) hit the busy local model just after router rev 25 moved vr-b back to ChatGPT (Foundry-Kilo-Sol). The probe then called Sol, which the router does not allow the `coordinator` client to use (`model_not_allowed`), so the hold stood and VR-AS22's closeout waited for vr-a's GPU. A hold now records the persona it was set under (`local_fallback_persona`, also in the `local_fallback_gpu` event). When the sprint's implementer persona is different, the hold ends (`local_fallback_cleared`, "the implementer persona changed").

Test: `tests/coordinator/test_engine.py::test_a_local_fallback_hold_ends_when_the_implementer_persona_changes`.

## v0.181.65 — a Lead case waiting for the workspace goes before a new sprint start

A Lead repair session locks the sprint's workspace (the main checkout, or a track's worktree) for its lifetime. Sprints on the same checkout queued for that lock in turn, but a waiting Lead case had no place in line. On 2026-11-05 VR-AS21's Lead case (`rc-2876e6b462617f50`) waited over an hour for the vr-b worktree while the VR-AS20 and VR-AS22 fix rounds took it in turn. Now `engine._acquire` checks before it takes a workspace lock the sprint does not hold yet. If an open Lead case of another sprint on the same checkout (same track, or both on main) is waiting for the workspace lock (`validation.lead_waiting`, cleared when the Lead session starts), the sprint waits instead ("Lead case … goes first"). A sprint already holding the workspace keeps going, and a case on another track or one not waiting for the workspace changes nothing.

Test: `tests/coordinator/test_engine.py::test_a_lead_case_waiting_for_the_workspace_goes_before_a_new_sprint_start`.

## v0.181.64 — persona and role-override changes apply without a coordinator restart

The coordinator pulls the router's deployed automation settings and, until now, applied a revision in place only when no session of the project was live, unless every change was an `apply=live` field. Persona settings (`roles.*`, `role_overrides.*`) and other `apply=next_session` fields therefore waited for a moment with no live session. A busy project like AU never has one. On 2026-11-05, rev 24 moved the vr-b track (VR-AS20..43) from ChatGPT, which was out of usage, to Claude. The revision stayed `pending` and took effect only after a coordinator restart. A revision whose every change is `apply=live` or `apply=next_session` now applies at once. Existing sessions pick up a new persona on their next prompt: every prompt path binds the persona before sending. Changes that reload Kilo (`role_rules.*`) or move Studio (`studio.*`, `slots.*`), and restart-scoped keys, still wait for a safe point as before.

Test: `tests/coordinator/test_config_pull.py::test_a_persona_override_applies_while_sessions_run`.

## v0.181.63 — a local-fallback hold ends when a probe finds the frontier answering

When a frontier sprint's model fell to the busy local model (`local_busy`), the sprint was held for the GPU for an hour (`local_fallback_until`, `LOCAL_FALLBACK_S`). The hold cleared early only when another sprint with the same persona was visibly served by the frontier. On 2026-11-05 Claude stopped answering while the router host's disk was full. BAL-1 got the hold and, the only Claude sprint running, waited behind the local model after Claude worked again. During a hold, the coordinator now sends one tiny call ("Reply with OK.", 16 tokens) through the sprint's implementer persona, at most every `[limits] local_fallback_probe_seconds` (default 600; 0 = off). When a non-local route answers (the backend's route source is not `local`), the hold is cleared and the sprint starts there. A sprint already holding a GPU unit is never probed. Events: `local_fallback_probe`, `local_fallback_cleared`.

Test: `tests/coordinator/test_engine.py::test_a_local_fallback_hold_clears_when_a_probe_finds_the_frontier_back`.

## v0.181.62 — open plan steps after a mid-sprint gate run are a free BUILD continuation

v0.181.52 added `au.plan_steps`, which fails verification while any Steps row except CLOSE is unticked. Plans ask for a scripted Studio gate run in the middle of BUILD ("end the turn with the STUDIO_GATE block"). After that run, the coordinator verifies, and the only failing check was `au.plan_steps`, because the later steps were still open by design. Each such round went to CHANGES_REQUIRED and the next session cost an implementation attempt. On 2026-11-05 BAL-1 went from attempt 3 to attempt 6 in 15 minutes. When `au.plan_steps` (any `*.plan_steps` check) is the only failing required check, verification now gives the free partial-BUILD continuation, the one an idle session with open steps already gets (`build_progress_verify`). It has the same stall guard: the same open steps, or no new commit when the plan has no checklist, `BUILD_STALL_LIMIT` times in a row goes to the normal repair path (`build_progress_stalled`).

Test: `tests/coordinator/test_engine.py::test_only_open_plan_steps_after_a_gate_run_is_a_free_build_continuation`.

## v0.181.61 — on a track, a sprint sent back by review goes before a new sprint

A track works in one folder, one sprint at a time. With `deps_accept_pending`, the next sprint starts while the last one waits for its visual or asset approval. When review then sent that one back (CHANGES_REQUIRED), its fix round waited until the whole new sprint finished. On 2026-11-05 VR-AS02 and VR-AS03 waited hours behind VR-AS03 and VR-AS04, and no VR-AS sprint completed for 10 hours. A sprint starting for the first time on a track now waits while another sprint of that track has a fix round pending. The setting is `[automation] track_fixes_first`, on by default and shown on the Automation settings page. The main checkout is unaffected (`main_one_sprint_at_a_time` already applies there).

Test: `tests/coordinator/test_engine.py::test_a_track_sprint_sent_back_by_review_goes_before_a_new_sprint`.

## v0.181.60 — a GPU unit held past its local-fallback window is released for a waiter

When a frontier sprint's model falls back to the busy local model (`local_busy`), the sprint queues for `gpu` for `LOCAL_FALLBACK_S` (`local_fallback_until`). A session that starts inside that window takes `gpu` and kept it for the whole session, even after the window ended and the frontier served the sprint again. On 2026-11-05 BAL-1 started a session at 07:32 while the Claude API was out of credit. It still held `gpu` at 08:50, served by Claude through Meridian, and VR-AS02's local closeout on the vr-a track waited 75 minutes behind it. When a sprint now waits for `gpu` held by a frontier sprint, and that sprint is not local-primary and its fallback window has ended, the unit is released (`local_fallback_gpu_released`) and the waiter takes it. A live window or a local-primary holder keeps the unit.

Test: `tests/coordinator/test_engine.py::test_a_gpu_unit_held_past_the_local_fallback_window_is_released_for_a_waiter`.

## v0.181.59 — an inconclusive visual review waits and tries again instead of passing

A screenshot or asset review that returned no valid verdict got one immediate structured retry. If that was also inconclusive, the frames were accepted with notes. On 2026-11-05 the Claude API route was down and the visual reviewer fell back to the local model. That model twice returned no valid verdict for VR-AS01's three heroes, so the sprint was approved with no real review.

In review-until-clean mode (`[approval] visual_rounds_max` set), an inconclusive answer after the structured retry now waits `[approval] visual_inconclusive_wait_s` (default 1800). It then reviews again, up to `visual_inconclusive_retries` times (default 6). Each wait is logged as `visual_approval_deferred`, with the model that answered. Only after the last wait is the review accepted with notes, together with a `visual_owner_items` event. Without `visual_rounds_max`, the old rule applies.

Test: `tests/coordinator/test_visual_model_approval.py::test_an_inconclusive_review_waits_and_retries_instead_of_accepting`.

## v0.181.58 — a usage-budget refusal pauses until the reset it names

When every model of a persona's route is denied by the router's usage budget, the refusal says when the budget resets ("... resets in 2h 25m"). The coordinator ignored that in two places:
- **A refusal returned to the coordinator:** it paused for the default 15 minutes whenever no Retry-After came with the refusal.
- **A refusal Kilo kept retrying:** it was handled as a router outage. The session was aborted after about 16 minutes and a new one started 60 seconds later.

On 2026-11-05 VR-AS20 and VR-AS21 hit the ChatGPT 5-hour window and restarted roughly every 17 minutes for about 2.5 hours. `infra.budget_reset_seconds()` now reads the reset time, plus a minute of slack. Both paths pause the task until then: `_on_denial` when the refusal carries no Retry-After, and `kilo_retry`, which now calls `_on_denial` with category budget. A closeout session keeps the old infra wait.

Tests: `tests/coordinator/test_infra_outages.py::test_budget_reset_time_is_read_from_the_refusal_text`, `::test_a_budget_refusal_in_kilo_retry_pauses_until_the_reset`.

## v0.181.57 — visual fix rounds bring their own attempt and review budget

Each visual fix round, sent back after closeout by the screenshot review, costs one attempt, one session, one closeout and one code review. AU allows 8 rounds (`[approval] visual_rounds_max`). The ordinary budget (`max_attempts`, `max_review_calls`) ran out long before that. On 2026-11-05 VR-MAP1 was parked twice on `review_budget`. The Lead escalated to Victor twice, and the timeout default stopped the task, including after the decision to close it had already been accepted. `effective_limits` now adds one attempt, session, closeout and review call per visual round the task has used, capped at `visual_rounds_max`. With no `visual_rounds_max` set, nothing changes. The ledger itself is unchanged: usage is still counted, and the extra room comes from the round count, which is bounded.

Test: `tests/coordinator/test_obligations.py::test_visual_fix_rounds_bring_their_own_attempt_and_review_room`.

## v0.181.56 — a refused judge persona falls back to the visual reviewer

After v0.181.55 the judge reached the router, but the router refused its persona for the coordinator's client key: "client 'coordinator' may not use model 'Foundry-Kilo-Vision-Deep'". The persona is not in that key's allowed list. No judge ruled, and VR-MAP1 kept looping on findings that need new art. The judge now tries `[roles] visual_judge` first. When the router refuses it, or it lacks a vision route on every model, the `visual_reviewer` persona judges instead. Each refusal is still logged as `visual_judge_unavailable`, with the persona named. The live AU profile now names `Foundry-Kilo-Vision`, which that key allows.

Test: `tests/coordinator/test_visual_model_approval.py::test_a_refused_judge_persona_falls_back_to_the_visual_reviewer`.

## v0.181.55 — the visual judge opens its router session under a role the router accepts

The judge bound its router work session as role `visual_judge`. The router's work binding only knows implementer, researcher, reviewer, deep_reviewer, visual_reviewer, image_generator, coordinator and helper, so every call failed with HTTP 400 `invalid_work_binding` (`visual_judge_unavailable` events). On 2026-11-05 VR-MAP1's frames came back three times with findings that need new art (proxy towers, box trees). No judge ever ruled them `owner`, so the sprint kept looping until its review budget ran out and the Lead asked Victor. The judge now binds as `visual_reviewer`. `[roles] visual_judge` still picks its persona.

Test: `tests/coordinator/test_visual_model_approval.py::test_the_judge_binds_a_role_the_router_accepts`.

## v0.181.54 — `visual_judge` is a known profile role

v0.181.53 added the `visual_judge` role (the vision model that rules on findings a fix round did not clear), but `PROFILE_ROLES` did not list it. On 2026-11-04 at 23:29 the AU profile named it, the profile failed to load ("unknown roles ['visual_judge']"), and the coordinator exited with code 2 on restart until the line was commented out. The role is now known, and the Automation settings page shows its field. It is a direct model call, so it has no role rules.

Test: `tests/coordinator/test_visual_model_approval.py::test_visual_judge_is_a_known_profile_role`.

## v0.181.53 — every screenshot set gets reviewed, in batches, with fix rounds and a judge; skills by sprint type

On 2026-11-04 the AU polish phase showed that the coordinator's visual review almost never ran. POL-1 committed 207 captures and read about 5; POL-3 and POL-4 made visual fixes with no captures; 74 completed sprints hold 1,478 captures no model ever looked at. Causes and fixes:

- **A whole evidence folder counts.** A sprint was "visual" only when its plan named image files. The polish plans listed `docs/evidence/<id>/**`. A plan that lists its whole evidence folder is now visual once images are in it, and the review requires every image there (AU adapter `_evidence_images`; `visual_approval._required`).
- **Batches.** The review sent every frame in one request, which ~200 captures would push past the model context and the API request size. Frames now go in batches of `[approval] visual_batch_frames` (10) and `visual_batch_mb` (18 MB). A clean batch joins the task's clean-frame memory, and the set's approval is recorded once every frame is covered. Each batch has its own journal key.
- **Fix rounds and a judge** (Victor: "as many reviews and fixes as needed ... and it should have a judge system"). With `[approval] visual_rounds_max` set, review findings go back as fix rounds until the frames are clean. From `visual_rounds_before_judge` on, a second vision model (role `visual_judge`, default Foundry-Kilo-Vision-Deep) rules on each finding: real (back with its instruction), not_a_defect, or owner. When no real finding is left the set is accepted with notes; `owner` items raise `visual_owner_items`. At the max the rest is accepted with notes and flagged `visual_rounds_exhausted`. Without `visual_rounds_max` the old one-round policy holds.
- **Skills by sprint type.** `[skills] preload_by_task` maps sprint id globs to skills that every implement/repair prompt carries with the plan's own list (Victor: "make sure ... sprints are using the best corresponding skills"; no AS- sprint ever had `au-blender-asset`).

Tests: `tests/coordinator/test_visual_model_approval.py` (batches, whole-folder frames, judge loop) and `tests/coordinator/test_skills_preload.py::test_skills_by_sprint_id_pattern_join_the_plans_own`. Takes effect after the coordinator restart, plus the AU profile keys (`approval.visual_rounds_max`, `visual_rounds_before_judge`, `roles.visual_judge`, the recursive approval rule, `skills.preload_by_task`).

## v0.181.52 — a BUILD with unticked plan steps never reaches review

On 2026-11-04, POL-2, POL-3 and POL-6 were approved with BUILD steps still unticked and High findings still `open` in the findings ledger (POL-F011 bot matches stalling, POL-F003 the HUD cropped at 1280x720). Their templated closeout failed done_check's PLAN row, and the fallback CLOSE session ticked the BUILD steps itself ("F002-F004 left open, see summary"). Nothing checked the plan's Steps list before review. The AU adapter now runs `au.plan_steps` in verify (main checkout and tracks): it fails while any `## Steps` row except the last (the CLOSE row) is unticked, and partial marks (`[~]`, `[/]`) count as open. The session gets the open steps back and is told a step it cannot finish is a BLOCKED or SCOPE_REQUEST line, never a tick. A plan missing at the revision is left to done_check's PLAN row.

Tests: `tests/coordinator/test_revamp_gate.py::test_a_build_with_unticked_steps_never_reaches_review`; `tests/coordinator/test_lune_speed.py` (check order updated). Takes effect after the coordinator restart.

## v0.181.51 — the Studio gate re-checks a sync in progress before calling it stale

On 2026-11-04, POL-3 added a check file and a mount. The auto-heal restarted Rojo and reopened the TEST place, and the gate verified 30 s later, while Rojo's first sync of the big place was still running (`matched=0/4`). It called that "stale sync". The next auto-heal restarted Rojo again, which dropped the sync in progress, and the reopen rate limit (2 per hour) then left Studio unsynced. `studio_gate calls` now takes `--sync-wait-s N`: a failed PRE-PLAY verify is re-run every 15 s until it passes or N seconds are spent. The coordinator's pipeline passes 90 s by default (`studio_calls pipeline --sync-wait-s`).

Test: `tests/coordinator/test_studio_gate_calls.py::test_a_verify_still_syncing_is_rerun_before_it_is_called_stale`. Takes effect after the coordinator restart.

## v0.181.50 — a TEST place closed mid-gate reopens Studio instead of waiting

On 2026-11-04 at 03:03 UTC, Roblox HTTP errors closed POL-2's TEST place in Studio partway through its scripted gate. The gate ended with `GateRunner.run tool error: The requested \`studio_id\` is not connected - that Roblox Studio instance may have been closed or its place unloaded`. No autoheal pattern matched: in `studio_id`, the underscore defeats `studio`. So the Studio auto-reopen (`[studio] auto_reopen`, on) never ran, and the Lead deferred the case as "environment" and only waited. The `studio_reload` signature now also matches that MCP wording (`studio_id is not connected`, `Studio instance may have been closed`, `its place unloaded`), so the stop gets the same place reopen as "Place is not open".

Test: `tests/coordinator/test_revamp_lead.py::test_a_place_closed_mid_gate_reopens_studio`. Takes effect after the coordinator restart.

## v0.181.49 — a sprint's own Studio suites that already passed carry over while no code changed

On 2026-11-04, POL-1's audit grew to seven suites of its own (POL_1..POL_1g). The scripted gate (`studio_gate calls`) never carried a sprint's own suites, so every rerun ran all seven again. Together they ran longer than the 30-minute run budget, so the last suite was cut as UNKNOWN on every run, and the gate could never go green. Under `--rerun failed`, an own suite that passed at an earlier run on HEAD's history now carries, as long as no `src/` or `tests/` file (and no harness file) has changed since. A real code or test change still reruns every own suite.

Tests: `tests/coordinator/test_studio_gate_calls.py::test_rerun_failed_carries_only_passed_untouched_regression_rows` (updated: S28 now carries after a docs-only fix) and `::test_the_sprints_own_suite_reruns_after_a_test_or_src_change`. Takes effect after the coordinator restart.

## v0.181.48 — a track merge with no Studio run owed no longer fails on the Studio-only rows

On 2026-11-04, GF-OPS1 (tier A, Open Cloud only) finished on track gpt-7, and its merge gate went red: `au.done_check.GATE: FAIL no 'GATE GF-OPS1 PASS|FAIL' line`. A track sprint leaves GATE, BOOT and EVIDENCE to the merge gate. The gate only excuses them when it plans a Studio batch. GF-OPS1 had no Studio suite of its own, and no regression row mapped to its merged files (`track_studio_batch_no_rows`), so no batch was planned and nothing could ever write that line. When the batch is on and finds no Studio run to make for the covered sprints, the merge gate now waives those Studio-only rows and records them in `gate.waived_no_studio_run`. With the batch switched off, the gate keeps its old verdict.

Test: `tests/coordinator/test_tracks_studio_batch.py::test_a_track_with_no_studio_run_owed_merges_without_a_gate_line`. Takes effect after the coordinator restart; then retry the gpt-7 merge.

## v0.181.47 — a stray non-UTF-8 byte in an evidence log no longer fails the templated closeout

On 2026-11-04, HK-REG1's templated closeout failed with `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa7 in position 470864`. Its evidence log held a cp1252 section sign that a tool had printed. `closeout_template.finish` read the log as strict UTF-8 to append the done-check block, so the closeout fell back to a 10-minute CLOSE session on the local model. The AU closeout template now reads its files with `errors="surrogateescape"`, and the engine's `_write_and_commit` writes them back the same way. A stray byte passes through unchanged, and valid text is written exactly as before.

Test: `tests/coordinator/test_revamp_gate.py::test_au_template_finish_keeps_a_non_utf8_byte_in_the_evidence_log`. Takes effect after the coordinator restart.

## v0.181.46 — a sprint back from a real stop rejoins behind the one that took the main checkout

One started sprint at a time holds the main checkout, and the earliest started goes first. On 2026-11-03, AS-F24 (started 19:49) blocked on a Studio sync with its Lead case deferred. MAP-5 took the checkout at 20:08 and committed work in progress. When AS-F24 came back, it was still the earliest started, so it took the checkout ahead of MAP-5. Its Lune leg then ran red on MAP-5's half-done MapV2 specs, and the two sprints blocked each other: AS-F24 had to be granted MAP-5's spec files. Now, when a sprint takes the main checkout, every other started main sprint that is out on a real stop (not an automatic one: a scheduled auto-heal, a decision that answers itself in minutes, a working Lead case, model visual approval) gets `lane_line_ts`, a place just behind it (event `lane_place_given`). It rejoins behind the sprint whose work is on master, which finishes first.

Test: `tests/coordinator/test_engine.py::test_a_sprint_back_from_a_real_stop_rejoins_behind_the_one_that_took_the_checkout`. Takes effect after the coordinator restart.

## v0.181.45 — a settings revision reaches running tracks

Each track runs with a copy of the project profile taken when the track starts (its own worktree, branch, locks and slot). `config_pull` applies a router revision to the main profile in place, but the track copies kept the old values. On 2026-11-03, revision 20 raised `context_ceiling_pct` to 85: S57b on main handed off at 163k, while HK-TEST2 on track test2 still handed off at the old 63% (144.5k). After a config apply, every cached track profile now takes the main profile's values again (`TracksManager.refresh_track_profiles`), keeping its track-specific fields, including a slot set later.

Test: `tests/coordinator/test_track_profile_refresh.py`. Takes effect after the coordinator restart.

## v0.181.44 — a settings revision that only changes live fields applies while sessions run

The coordinator applied every router revision only at a "safe point" with no live session. With a main sprint and a parallel track running, that point rarely comes. On 2026-11-03, revision 20 (`automation.context_ceiling_pct` 63 → 85) stayed pending for 20 minutes, and the old 121k ceiling went on handing S56 off. A revision whose every change is a field the schema marks `apply = live` (read on each use) now applies at once. A change to a `next_session` or `restart` field (role rules, client URL, ...) still waits for the safe point or restart, as before.

Test: `tests/coordinator/test_config_pull.py::test_a_live_only_revision_applies_while_sessions_run`. Takes effect after the coordinator restart.

## v0.181.43 — a context ceiling below a fresh session's startup context stops handing off

An operator set `[automation] context_ceiling_pct` to 63 (router revision 19). On Claude's 191,808-token usable window that is a 120.8k handoff ceiling. On 2026-11-03 a fresh S55 session reached it within about 30 s of starting, just from reloading its plan and files. It was handed off, and the next session started over the ceiling again: six handoffs and 6 of 8 sessions in 8 minutes. A session over the ceiling within `CEILING_FAST_S` (180 s) of its start counts toward a streak. After `CEILING_FAST_MAX` (2) such handoffs in a row, the ceiling is skipped for that task's later sessions (event `context_ceiling_skipped`, with a note to raise the setting). The wrap-up stop near the window still applies. The AU value is now 85 (router revision 20, 163k).

Test: `tests/coordinator/test_revamp_infra.py::test_a_ceiling_below_the_startup_context_stops_handing_off`. Takes effect after the coordinator restart.

## v0.181.42 — a merge repair or an operator reopen really gets fresh attempts

`repair_reopen` (a red merge Studio batch puts a COMPLETE sprint back on main) and `reopen` (an operator re-queues a CANCELLED sprint) promised "fresh attempts", but they only zeroed the task row. The budget ledger, which enforces the limits, still summed every attempt and session since the task began. On 2026-11-03 the local2 merge reopened HK-ASSET1. Its first repair session made "7 implementation attempts used (max 7)", so it went straight to a Lead case (twice: 15:57 and 18:12). Effective limits now count only ledger rows from the task's latest repair or reopen (`budget_epoch`: `data.repair.ts`, `data.reopened.ts`, which `reopen` now records). Earlier rows stay as history, and authorizations before the epoch no longer count.

Tests: `tests/coordinator/test_repair_fresh_budget.py`. Takes effect after the coordinator restart, including for tasks reopened by a merge repair before it.

## v0.181.41 — a Studio gate with no Luau file under test checks the default pair too

The scripted Studio gate tests a sprint's own changed files, and PRE-PLAY proves that Studio loaded HEAD by hashing the Luau files among them. On 2026-11-03 PT-3's only in-scope changes were four concept PNGs, which a Lead repair had committed. PRE-PLAY had nothing to hash ("no hash map for the files under test"), and the run stopped as stale Rojo, which the auto-heal restart cannot fix. When none of the files is Luau, `tests/RegressionIndex.lua` and `tests/GateRunner.lua` now go first, the same pair a sprint with no changed files already gets.

Test: `tests/coordinator/test_studio_calls.py::test_gate_files_without_a_luau_file_add_the_default_pair`. Takes effect after the coordinator restart.

## v0.181.40 — owner inbox folders: deliveries mid-sprint are never a session's stray files

New `[automation] owner_inbox` setting (globs; default empty; shown with the other automation settings). These are folders where the owner drops deliveries into the main checkout at any time. AU uses it for the ChatGPT concept-art inbox `assets/source/incoming/**`. A new uncommitted file there no longer fails a running sprint's `coordinator.unrelated_preserved` check. On 2026-11-03 the concept-art chat saved map images there while PT-3 verified, and PT-3 went back to CHANGES_REQUIRED for files it never wrote. Stray files anywhere else still fail.

Tests: `tests/coordinator/test_engine.py::test_owner_inbox_drops_mid_sprint_are_not_the_sessions`, `::test_a_new_file_outside_the_inbox_still_fails_unrelated_preserved`. Takes effect after the coordinator restart, once the project sets the folder.

## v0.181.39 — scope never blames a sprint for an operator chat's commits

The owner's concept-art chat (Claude Code) committed map-concept docs and the MAP-5 plan straight to master while PT-3 ran (`docs: map concept order ...`, `plans: MAP-5 ...`). PT-3's scope check counted those files as PT-3's. It failed twice and went to a recovery decision. A commit whose message carries a `Co-Authored-By: Claude ...` trailer comes from an operator chat, because sprint sessions never write one. Unless its subject carries this sprint's own prefix, its files are now left out of the sprint's scope, review and memory checks, the same as another sprint's commits or a prep landing. New: `Workspace.operator_commits(base, head)`.

Tests: `tests/coordinator/test_scope_operator_commits.py`. Takes effect after the coordinator restart.

## v0.181.38 — the Lead accepts the spend-cap failover to Meridian

At the API's daily spend cap the router denies the paid `anthropic` route and serves the same Claude model on Meridian (v0.181.34). The Lead's after-call served-model check compared each served (backend, model) against the persona's route chain, where Meridian is not listed, and discarded the session as a mismatch. HK-ASSET1's recovery case was deferred that way on 2026-11-03. A served route on another backend now passes when its model is one the chain serves from a non-local source and the router recorded a denial in that session (its own failover). A backend with no denial behind it, or a local model off the chain, is still a mismatch.

Tests: `tests/coordinator/test_lead_follow_persona.py` (failover accepted, no-denial mismatch, local model refused). Takes effect after the coordinator restart.

## v0.181.37 — control center: show or hide each task state in the task picker

The single "Show cancelled" box beside the Task picker is now a **States ▾** dropdown. It has a tick box for every state the project's tasks are in, each with its count, active states listed first, plus **All**, **Active only** and **None** buttons. The summary reads `States (shown/total)` while something is hidden. The task being viewed always stays in the list. The choice is saved with the page's display settings (`hiddenStates`, default: only CANCELLED hidden, as before) and now survives a reload. The old `showCancelled` flag was dropped on every load, because saved preferences keep only keys the defaults know. The panel flips to the right edge in a narrow window.

Owner request 2026-11-03: "can we have another filter option to not show completed too, have a tick box for each of the types to show / not show". Test: `tests/test_control_ui_behavior.py::test_task_picker_state_filter_persists_and_counts`. Takes effect after the router rebuild.

## v0.181.36 — a busy local model never costs a sprint an attempt

A session that ends with the router's `local_busy` refusal ("backend 'strata' serves the main stream only and N session(s) are active") is always a free retry after the usual backoff, however many times in a row. Other infrastructure failures still become charged attempts after `infra_retry_max`.

Why: on 2026-11-03 the Claude API reached its daily spend cap, and sessions of Claude personas and the Lead fell to the one local stream. HK-ASSET1, a local-model sprint, was refused ten times in a row, used up its free retries, and then spent real attempts (5 of 6) on contention, not on its work. Test: `tests/coordinator/test_infra_outages.py::test_a_busy_local_model_never_uses_an_attempt`.

## v0.181.35 — STATE and decision-log conflicts merge without a Lead session

A track rebase whose conflicts touch only `docs/memory/**`, `docs/progress/STATE.md`, `docs/progress/state/DECISIONS.md` or `docs/progress/state/KNOWN_ISSUES.md` now resolves itself and keeps both sides. In the three logs, a table row that both sides changed (same first cell) appears once, as the track's version; rows only one side added are all kept. Conflicts in any other file still abort and open the usual decision and Lead repair. `docs/design/ASSET_LEDGER.md` is not covered, because a duplicated asset row would break `ledger.py check`.

Why: on 2026-11-03 the copy1 and rt2 merges conflicted only in STATE.md (each side added its own sprint row), and each cost a Lead repair session, run on the local model while the Claude API was at its daily cap. Test: `tests/coordinator/test_tracks_merge.py::test_state_table_conflicts_merge_rows_without_duplicates`.

## v0.181.34 — a capped paid API hands a strict persona's model to the subscription backend that serves it

A strict persona checked the spend caps only against the best-ranked backend of each model in its chain. When the paid `anthropic` API reached its daily spend cap, the persona moved to its next model even though Meridian (priority 90, Claude Max subscription) serves the same Claude model.

Now, unless the persona or its helper route pins a backend explicitly, each backend serving the model is budget-checked in priority order. The first allowed one is pinned for that request, still with no failover beyond it. Under the cap the higher-priority paid API serves as before. The request log shows `<model> via <backend> (<first backend>: <why denied>)`. New pool method: `backend_infos(model)`.

Why: on 2026-11-03 the $25 daily cap was reached around 04:00 local with ChatGPT at 96% of its weekly window, so every Claude role (Lead, implementers, reviewers) fell to the single local Qwen stream while Meridian sat unused. Test: `tests/test_strict_routing.py::test_a_capped_paid_api_hands_the_same_model_to_the_subscription_backend`. Takes effect after the router rebuild.

## v0.181.33 — a session never starts on a persona the Kilo key may not use

Before an implementer session starts, the infra preflight asks the router whether the Kilo client (`[lead_manager] kilo_client`, default `kilo`) may use the session's persona (`/v1/foundry/personas/<persona>/routes?client=`). If it may not, or the persona does not exist, the sprint waits in preflight with the reason (no session, no attempt used, no Lead case) and starts as soon as the role is routed to a granted persona or the grant is added. An unreachable router is left to the router health probe.

Why: on 2026-11-03 HK-COPY1 and HK-DATA1 were routed to Foundry-Kilo-Haiku, which the `kilo` key may not use. Each session died on its first call (403, config) and opened a Lead case. The Lead's resume then started the same refused session again, so the Lead calls looped. Test: `tests/coordinator/test_infra_outages.py::test_preflight_holds_a_session_on_a_persona_the_kilo_key_may_not_use`.

## v0.181.32 — a never-started sprint can move from one track to another

`tracks start <project> <new track> <sprint>` now accepts a sprint that is queued on another running (or planned) track, as long as it never started: state QUEUED or READY, attempt 0, no session, and not that track's current sprint. The sprint leaves its old track and that track's chain is relinked: the sprint that followed it now follows the one before it. Its own plan dependencies stay; only the old chain link is dropped. Events: `moved_off_track` and `moved_to_track`. A started or current sprint still refuses to move.

Why: on 2026-11-03 HK-COPY1 and HK-DATA1 waited hours behind HK-ASSET1 on one local-model track, and AS-F26 waited behind AS-E11b, while Claude agents and four of the five Blender pool jobs sat idle. No command could split them. Tests: `tests/coordinator/test_tracks_move.py`.

## v0.181.31 — a merge-conflict timeout never strands a finished track; stopped tracks with finished work can merge

**The bug.** A track whose rebase onto the main branch conflicted opened a decision whose default was "stop this track". When nobody answered within the decision timeout, the coordinator stopped the track. `tracks merge` refused a stopped track ("already stopped"), so its work could never reach the main branch, while every one of its sprints read COMPLETE. On Animal Uprising this stranded five tracks between 2026-10-27 and 2026-11-02: name1 (GF-NAME1), backend-1 (S57a), birds (AS-D08, AS-D09, AS-D16: four flying enemies and three elites), poi (AS-F19, AS-F20, AS-F23, AS-F32: POI, merchant stop, landmarks and den quarters kits) and rt2 (GF-RT5, GF-RT6, GF-RT7, GF-LIVE2). Each stop raised a `track_work_unmerged` alert, but nothing could act on it.

**Now:**
- When every sprint of the track is COMPLETE, the timeout retries the merge (`TIMEOUT_RETRIES` = 2; each conflict may start a fresh Lead repair).
- After those retries it keeps the branch as a **failed** track, which `tracks merge` can re-request, and raises the same alert. It never stops a track with finished work.
- An operator still stops a track explicitly; a track with unfinished sprints still stops on the timeout as before.
- `tracks merge` accepts a **stopped** track whose sprints are all COMPLETE and reopens its merge. A stopped track with unfinished sprints is still refused.

Tests: `test_tracks_merge.py` (the timeout test now expects retries then a failed, re-requestable track; two new tests for stopped tracks) and `test_tracks_lead_repair.py` (the Lead-unavailable timeout keeps the branch).

## v0.181.30 — the Lead can follow its persona's route chain (`expected_models = []`)

`[lead_manager] expected_models` may now be empty. When it is, the Lead's readiness check and
its after-call served-model check use the lead persona's strict route chain as the router
reports it, instead of a second list kept in the profile. Persona strictness is still required,
and a call served outside that chain is still a mismatch. A named list pins the lead as before.

Why: on 2026-11-03 the lead persona moved from Opus to Sonnet in the router to save API spend.
The profile still listed Opus, so readiness failed and every Lead call on MAP-3 was discarded as a
served-model mismatch. The validation test that rejected an empty list now checks that duplicates
are rejected and that an empty list loads.

## v0.181.29 — Sonnet 5.5 cache reads priced at $0.10 per million

`usage.ANTHROPIC_API_PRICES`: Claude Sonnet 5.5 cache reads are 0.05x input ($0.10/MTok, Anthropic
prompt-caching page), not $0.20. Router spend for Sonnet on the `anthropic-api` backend was
over-counted by about half of its cached share, so the spend caps tripped early.

## v0.181.28 — backend type `anthropic-api`: Claude straight from Anthropic, 1-hour cache, metered

2026-11-03. The owner moved Claude work onto API credit. Through CLIProxyAPI an API key only gets
the 5-minute prompt cache: CLIProxyAPI's 1-hour upgrade is for Claude Code sign-ins only. Every
Studio check longer than 5 minutes therefore re-wrote a 150k-token context. The router also counted
those calls as a free subscription (`est_cost_usd` 0), so no spend cap could apply.

- New backend `type: anthropic-api` (`url: https://api.anthropic.com`, `api_key: ${ANTHROPIC_API_KEY}`).
  `pool/protocols.AnthropicAPIProtocol` speaks the Messages API directly:
  - auth is `x-api-key` only, with no Meridian headers and no forwarded client headers;
  - prompt caching is placed by the router: a breakpoint on the last system block plus top-level
    automatic caching, both with the backend's `cache_ttl` (`"1h"` by default, or `"5m"`);
  - thinking is adaptive (the 5.5 models reject `budget_tokens`), with depth set by
    `output_config.effort`;
  - no temperature, top_p or top_k, and forced tool_choice is degraded to auto;
  - models are discovered from `/v1/models`.
- Metered, not a subscription. `guardrails` counts it as metered, so `guardrails.daily_spend_cap_usd`
  and `weekly_spend_cap_usd` apply. `usage.anthropic_api_cost` prices each call from Anthropic's
  rates (`ANTHROPIC_API_PRICES`), with cache-read and cache-write (1.25x / 2x) tokens. The model
  registry row is not used for this, because it is shared with Meridian, which is free.
  `budget.is_paid`, the thinking menu and registry tiers know the type.
- Setup → Backends has an "Anthropic API (Claude, pay per token)" template. The key is typed there
  and stored as the provider key `ANTHROPIC_API_KEY`, never in config.yaml.
- Tests: `tests/test_anthropic_api_backend.py`.

To use it: rebuild the router, add the backend from Setup (paste the key there), give it a
higher `priority` than cliproxyapi and meridian, set the spend caps, then remove `api-keys.claude`
from CLIProxyAPI's config.

## v0.181.27 — Claude through CLIProxyAPI (an API key) is judged by Claude's windows, not ChatGPT's

2026-11-02 23:xx. The owner added a Claude API key to CLIProxyAPI (`api-keys.claude`) and asked
for the sprint roles to move off Meridian onto it. The Automation page's Routing tab could not
show or deploy that correctly:

- A persona stores model names only (`pinned_models`). The pool serves a model from every backend
  that lists it, highest `priority` first, with failover. The page put each model on the first
  backend in list order, so "claude-sonnet-5-5 on cliproxyapi" and "on meridian" collapsed into
  one route on Meridian.
- The dry run ("Now: ...") and the route rows judged every model on cliproxyapi by its ChatGPT
  windows. Every Claude route there showed "skipped (weekly 96% ≥ cutoff 95%)", although the
  router's own gate (`cliproxy_usage.model_status`) checks each model by its provider's windows.

Changes:
- `automation_view.normalize_usage`: CLIProxyAPI rows carry `provider_windows` (codex / claude)
  and the backend `priority`.
- `automation_view.dry_run`: tries the backends that serve a model in the pool's order
  (priority, failover). Each is judged by the model's own provider windows. A Claude API key (no
  signed-in Claude account) is never refused by `fail_closed`.
- `seed_document`: shows a model on the backend the pool calls first (enabled, then priority).
- Automation page: route rows and the overview use the model's provider for "over cutoff" and
  the family label.
- `cliproxy_usage.model_status`: `fail_closed` no longer refuses a Claude model when no Claude
  account is signed in (an API key: billed per token, no window). A ChatGPT model with no
  account is still refused.
- Tests: `test_claude_through_cliproxy_is_gated_by_claude_windows_and_the_pool_order`,
  `test_a_claude_api_key_is_never_refused_by_fail_closed`.

To make Claude calls use the API key: give `cliproxyapi` a higher `priority` than `meridian`
(Setup → backend list, e.g. meridian 90). Meridian then stays as failover. The router needs a
rebuild for the page changes. Set a spend limit in the Anthropic Console: the router has no
dollar cap for a per-token key behind CLIProxyAPI (it counts it as a subscription).

## v0.181.26 — a dropped dependency stays dropped at the start check; the Claude cap survives a restart

2026-11-02 23:xx, from the owner's "check to make sure all sprints and slots are correct".

- **S55 deadlock.** AS-VR1 (the owner's review) waits for POL-7. A person removed it from S55's
  deps, and v0.181.19 kept the QUEUED check from adding it back. But `_unmet_start_deps`, the
  check a READY sprint passes before its first session, still added the plan's own "Depends on"
  sprints. So S55 sat READY "waiting for AS-VR1 (PAUSED)". S56, S57b and the whole polish phase
  wait for S55, and POL-7 is what AS-VR1 waits for. `_unmet_start_deps` now skips
  `data.deps_dropped`.
  Test: `test_a_dropped_dependency_does_not_hold_back_a_ready_sprint_at_its_start`.
- **Claude agent cap after a restart.** The last usage reading lived only in memory. Three restarts
  tonight erased it while every read failed (router HTTP 429), so the cap went back to the full
  `claude_agents` (5) at 77% usage instead of `claude_agents_over` (2). Each good reading is now
  saved (meta `claude_usage:<project>`) and used after a restart while it is under 6 hours old.
  The `test_claude_agents_are_capped_...` test covers the restart and the age limit.
- Operator queue fix (no code): MAP-2, MAP-3 and MAP-4 were moved right after MAP-1, ahead of PT-3,
  S55, S56 and S57b, so the soak, art and performance passes run on the overhauled map.

## v0.181.25 — a visual sprint with no capture is told what to commit

2026-11-02 22:5x. GF-LIVE2 (the soft-shutdown "updating / reconnect" overlay) is a visual sprint, and
its plan names no frame file. Three attempts running, the review packet's capture item read
"missing: 0 files committed, 0 required by the plan's file list / acceptance lines, 0 missing". The
session took that as nothing missing. The repeat check then opened a recovery decision and called
the Lead.

- AU adapter `_capture_evidence`: when a visual sprint has no capture at all and the plan names
  none, the note starts with "VISUAL SPRINT WITH NO CAPTURE". It says to commit at least one
  screenshot of the screen(s) the acceptance lines describe under `docs/evidence/<id>/` (the
  `CAPTURE_READY` line), with any name accepted.
- Test: `test_a_visual_sprint_without_any_capture_is_told_what_to_commit`.

## v0.181.24 — a sprint that becomes READY this tick keeps its place in line

2026-11-02 22:12. AS-V12 completed. MAP-1 is next in the queue (position 197): the owner's map fixes,
which the operator had moved ahead of S53. In the same tick, MAP-1's step only took it from QUEUED to
READY. S53 (position 199, already READY) stepped right after, found the main checkout free and
started. v0.181.13 made a never-started sprint wait for an earlier sprint *told to wait* this tick.
A sprint that only became READY this tick had not been told anything, so it did not count.

- `engine.tick`: a main (non-track) sprint that becomes READY in its step this tick, and has never
  started, is recorded like one told to wait. A later never-started main sprint then waits for it
  ("waiting for MAP-1 (earlier in the queue) ..."), and the queue order decides on the next tick.
- No hold was kept. The operator held S53 behind MAP-1, but a paused sprint keeps the main
  checkout lock, so MAP-1 waited on S53's lock (a deadlock). S53 had also already committed to
  master. The hold was ended; S54 (started before) took the checkout meanwhile. Order now: S54,
  S53, MAP-1. Do not hold a started main sprint behind a never-started one.
- Test: `test_a_sprint_that_becomes_ready_this_tick_keeps_its_place_in_line` (without the fix, the
  later sprint starts).

## v0.181.23 — skill pages go into every implement and repair prompt

2026-11-02 21:xx. Owner: "make sure the skills are being used". The Kilo log said they mostly were
not. In the last 30 hours, 81 of 552 AU implement/repair sessions called the skill tool at all, and
only a sprint's first session ever did. Repair, hand-off and local-model sessions loaded none.
None of the 27 `roblox-*` skills installed that afternoon was ever opened (GF-LOOK1 loaded
`au-roblox-world-building` once, in its first session). Cause: plans list their skills in a
`## Skills` section, but AU's condensed session brief does not carry that section, so an agent saw
the list only if it opened the plan file and read that far.

- New `skills_preload`. Every implement and repair prompt (first prompt, restarts, repairs, context
  hand-offs; not closeout) gets the SKILL.md pages of `[skills] preload_always`, followed by the
  skills the plan names in backticks in its `## Skills` section, up to `[skills] preload_max_kb`
  (default 24). A skill past the budget is named for the session to load with the skill tool.
  Skills are found in the game's skill folder first (the main checkout's, for tracks), then in
  Foundry's copy.
- The AU adapter passes the plan's skill names as task data (`data.skills`).
- New events: `skills_preloaded` (loaded, over_budget, missing, bytes) and `skills_preload_failed`.
  A failure never blocks a session.
- New settings with control-center controls: `skills.preload` (on), `skills.preload_always`,
  `skills.preload_max_kb`.
- Tests: `tests/coordinator/test_skills_preload.py`.

## v0.181.22 — the reviewer never demands a self-referential gate pointer

2026-11-02 19:3x. GF-MUT1's code was done: GATE PASS, Lune 2078/2078, negative control 398/398,
suite 48/48 and every code finding resolved. Each of its last four reviews still raised a major
finding: its evidence summary's "Gate head: <sha>" line no longer named the gate bound to the
reviewed commit. Every commit that corrected the line ran a newer gate, so the line was stale again
at the next review. The review budget ran out, the Lead's extension cap (2 a day) was spent, and the
sprint was parked by a timeout default.

- `review.SYSTEM`: a document's pointer to a check run, gate or commit that is out of date only
  because later commits re-ran the check is at most minor, when the packet's mechanical checks bind
  a passing run to the reviewed commit. A document is never required to name the final commit or
  run of the review reading it.
- GF-MUT1 was unblocked with a reset, and its summary now points to `au.gate_bound_to_head`
  instead of a sha.
- Test: `test_the_reviewer_never_demands_a_self_referential_gate_pointer`.

## v0.181.21 — owner Studio reservation (studio-reserve / studio-release)

2026-11-02. For Victor's GF-RT4 two-account test, the operator reserved the workstation's Studio
by hand: an AU `studio.lock` line plus holds on the main sprints. The line named no process, so
AU's lock tool called it stale after 30 minutes and a session broke it. A track merge's Studio
batch then ran on the owner's Studio while he was in Play (PRE-PLAY verify hit Play mode, then
"no TEST window"). It went red and reopened two finished sprints (S54, AS-AI1) as repairs.

- `foundry_coordinator/studio_reserve.py`: the reservation lives in the coordinator store
  (`meta studio_reserved:<project>`: by, reason, since, until), so no lock tool can judge it
  stale. While active:
  - `studio_calls` starts no Studio run on the workstation; slot-bound tracks are not affected;
  - `tracks/resources.admit_studio` gives no merge the Studio lease;
  - `engine._start_implementation` starts no main-checkout session;
  - main tasks that were writing are held with owner `studio_reserve:<project>`. The self-repair
    sweep resumes them once the reservation is released or expires (a registered hold owner).
- CLI: `studio-reserve PROJECT [--minutes N] [--reason] [--by]` (default 120 minutes, at most 24
  hours) and `studio-release PROJECT`. Both write events (`studio_reserved` / `studio_released`).
- Tests: `tests/coordinator/test_studio_reserve.py`.

## v0.181.20 — LightingStyle cannot be set by game scripts (skill correction)

2026-11-02 18:5x. During Victor's GF-RT4 live test, the published TEST server logged
`[MapService] map look not applied: The current thread cannot write 'LightingStyle' (lacking
capability PluginOrOpenCloud)` at boot. That came from GF-LOOK1's work in progress, which followed
the v0.181.16 skill. The API reference lists `Lighting.LightingStyle` as read-write with no
security, so the v0.181.16 entry page said it can be set from scripts and marked roblox-brain's
note ("set it in Studio") as outdated. The live server shows roblox-brain was right.

- `roblox-world-building`:
  - `LightingStyle` and `PrioritizeLightingQuality` are set in Studio or the Rojo project file
    (`Lighting` `$properties`), never by game scripts.
  - Other lighting is runtime-writable, applied one `pcall` per value.
  - The roblox-brain reference note is marked correct.
- `au-roblox-world-building`: `LightingStyle` goes in `default.project.json`.
- `ROBLOX_SKILLS.md`: the example of a contested claim is corrected.
- The test checks the corrected wording.
- GF-LOOK1's plan got an operator note: set those two in `default.project.json`, and apply the
  rest one value at a time.

## v0.181.19 — a dependency a person removes stays removed

2026-11-03 00:4x. The owner moved S55 (art pass) and S57b (exploit audit) ahead of his visual
review (AS-VR1) and the closed beta (GF-BETA1). Those reviews now wait for the polish phase's last
sprint (POL-7), and POL-1 waits for S55 and S57b. The operator removed AS-VR1 from S55's queue
deps, but the readiness check added it straight back from the plan's "Depends on" line (the
2026-10-29 AS-R7 rule: a dependency named in the task source and already on the queue becomes a
queue dependency). The loop S55 -> AS-VR1 -> POL-7 -> POL-1 -> S55 would never have started.

- `engine.set_deps` by anyone other than the coordinator records the deps it removes in the
  task's `data.deps_dropped`; adding one back clears it.
- The readiness check skips `deps_dropped` when it turns task-source deps into queue deps.
  The coordinator's own additions are unchanged.
- Test: `test_a_dependency_a_person_removes_is_not_put_back_from_the_task_source`.

## v0.181.18 — an installed skill is allowed in Kilo at once

2026-11-02 23:3x. The 27 `roblox-*` skills and `au-roblox-world-building` were added to the live
profile's `[skills] install` list and installed into `.kilo/skills/`. kilo.jsonc still denied them
(`"skill": {"*": "deny", ...}`), so no session could load them. The kilo.jsonc skill allows
(`skills_install.allow_rules`) are only written when the agent blocks are regenerated, and only a
router-side settings change (config_pull) did that. A list edited in the profile file never
reached kilo.jsonc.

- `skills_install.refresh_kilo()`: when a sync installs or removes a skill, it does three things:
  - rewrites the main workspace's kilo.jsonc agent blocks (`kilo_config.regenerate`, using the
    cached router role rules, else the profile's own);
  - files a `reload_kilo` client request;
  - rewrites every planned, running or merging track's kilo.jsonc through
    `tracks.set_needs` with its current needs, which flags that track's Kilo reload.

  A `skills_kilo_refreshed` event records it.
- Test: `test_an_installed_skill_refreshes_kilo_and_live_tracks`.

## v0.181.17 — operator backups in the deploy checkout no longer trip the Lead guard

2026-11-02 22:4x. While AS-D18's Lead diagnosed a review block, the operator saved
`project.toml.bak-2026-11-02-roblox-library` in the live deploy checkout. The Lead guard compares
the router checkout's `git status` before and after a Lead call. It saw the new untracked file,
aborted the Lead (`guard_violation`) and opened an escalation decision, which only a timeout
default answered. The exemption for router changes during a Lead call only covers read-only Leads,
and the live Lead has write access.

- `lead.operator_status()`: the guard's router snapshot (`LeadManager._git_state`) leaves out
  untracked `*.bak` / `*.bak-*` files, the operator's backups of the git-ignored live profile.
  `clean` is computed the same way, so a deploy onto a release tag over such backups is
  recognised as a deploy. Every other untracked file and every tracked change still counts.
- Test: `test_operator_backup_files_in_the_deploy_checkout_never_trip_the_guard`. The
  release-tag snapshot test now uses an ordinary untracked file for its dirty checkout.

## v0.181.16 — a general Roblox skill library (27 skills) for every Foundry game

2026-11-02. Victor asked for "the absolute best skills for all of the work we are doing, and future
roblox games", comparing three public libraries with ours: EL4CTEO/roblox-skills (MIT, 23 compact
skills), TabooHarmony/roblox-brain (MIT, 29 skills with sourced, doc-checked deep references) and
SyphoDev-yt/roblox-ai-skills (free licence with credit, no selling; pipeline skills built on its own
tooling).

- `contrib/skills/roblox-*` holds 27 game-agnostic skills: luau, architecture, networking, security,
  data, performance, world-building, physics, npc-ai, camera, input, ui, animation-vfx, audio,
  localization, text-chat, teleport-matchmaking, game-systems, game-design, monetization,
  analytics-liveops, testing, code-review, tooling, studio-mcp, open-cloud and publish-checklist.
- Each `SKILL.md` is Foundry's own short entry page (4.2 KB at most) that merges the sources' rules
  with Foundry lessons, among them:
  - Device-simulator orientation is kept apart from the device.
  - Software-rendered Studios give no timing or frame-rate evidence.
  - XML saved as `.rbxm` breaks Rojo.
  - Check that the live code matches HEAD before testing; carry passes forward by content hash.
  - Audio quality gates and listening rounds.
  - Agents never handle API keys.
  - Ethical limits on game design for young players.
- Its `references/` carry the upstream deep references with a provenance line, plus per-folder
  `THIRD_PARTY_NOTICES.md`. SyphoDev's map-design research is summarised in Foundry's own words with
  credit (`roblox-world-building/references/map-design-notes.md`); no SyphoDev file is copied.
- Contested claims follow the Roblox API reference, checked this session:
  - `Lighting.LightingStyle` and `PrioritizeLightingQuality` are script-writable. roblox-brain says
    otherwise; only the deprecated `Technology` is not.
  - `Player.User` is the identity for new code.
  - `ConfigService` and `MarketplaceService:BindReceiptHandler` exist.
- `contrib/skills/ROBLOX_SKILLS.md` is the index and routing table.
  `contrib/skills/_build/build_roblox_skills.py` refreshes `references/` from new upstream commits
  and never touches `SKILL.md`.
- `au-roblox-world-building` now builds on `roblox-world-building` (its duplicate references are
  removed).
- Tests: `tests/coordinator/test_roblox_skill_library.py` checks frontmatter, entry-page size,
  listed references, notices, provenance lines, installer lookup and the contested-claim wording.

## v0.181.15 — AU skill: au-roblox-world-building

2026-11-02. Victor: "the map looks like it needs real work". No AU skill covered lighting,
atmosphere, terrain or materials, and no game code sets `Lighting`, so the map runs Roblox
defaults on mostly `SmoothPlastic` parts. Victor approved adding a world-building skill and
planning a map look sprint.

- New shipped Kilo skill
  `foundry_coordinator/projects/animaluprising/kilo_skills/au-roblox-world-building/`:
  - `SKILL.md` (AnimalUprising's own text) covers the Map v2 generator (`src/server/MapBuild/`),
    keeping seeds and time-sliced builds intact, and putting lighting in code rather than the
    place file. It also covers materials and scenery cost, streaming, and the Map v2 regression
    rows that must stay green.
  - It sets how the look is judged: on the workstation only (never on the software-rendered
    slots), through gate captures and the coordinator's vision approval, with the owner
    approving the final look.
  - `references/world-building.md` and `references/streaming.md` are adapted from
    EL4CTEO/roblox-skills (MIT; `references/THIRD_PARTY_NOTICES.md`).
- It is installed through `[skills] install` (skills_install.py) into the game's untracked
  `.kilo/skills/`.

## v0.181.14 — a track sprint's plan needs join its track before the session

2026-11-02 18:2x. The audio-local track (AS-AI1, then S54) started with needs `[studio]`, before
S54 wrote its plan. The plan says `## Needs: studio, audio`, but the track's kilo.jsonc still
denied Media_Gen's audio tools. S54's session stopped BLOCKED ("Audio generation needs
Media_Gen, which is off for this session"), and the Lead's re-check changed nothing. The
assets-local track hit the same problem on 2026-10-28, and an operator fixed it with
`tracks set-needs`.

- `services.widen_track_needs()` runs in `services.blocks()`, before every implementer
  session. Needs a track sprint's plan declares and its track lacks join the track through
  `tracks.set_needs`. That rewrites the kilo.jsonc and flags the reload, which the session start
  applies. A `track_needs_widened` event is recorded.
- `studio` never joins a track without a Studio slot, so its sessions never reach the
  workstation's Studio. The main checkout is never touched.
- Test: `test_a_track_sprints_plan_needs_join_the_track_before_its_session`.

## v0.181.13 — a main checkout freed mid-tick goes to the sprint first in the queue

2026-11-02 16:3x. GF-COS1 completed partway through a tick. GF-QUEEN1 and AS-G07b come earlier
in the queue, but they had already been stepped in that tick and told to wait for GF-COS1.
MV2-13 comes much later: it is an owner-step sprint the owner asked to keep near the end, and
its three full test matches take hours. It stepped later in the same tick, found the main
checkout free and took it. The tick steps sprints in queue order, so whichever later sprint
came first after a completion won the checkout.

- `engine._lane_yielded[pid]`: the main sprints told in this tick to wait for the main
  checkout. It is reset at the start of each project's task loop.
- A never-started main sprint does not take a checkout freed during the tick while an earlier
  sprint was told to wait: "waiting for X (earlier in the queue) to take the main checkout
  first". On the next tick the queue order decides. A started sprint keeps its own place in
  line (the earliest started first, as before).
- Test: `test_a_checkout_freed_mid_tick_goes_to_the_earlier_waiting_sprint`.

## v0.181.12 — Studio's device simulator stays in landscape

2026-11-02 15:0x. Victor's Studio showed the AU_1920x1080 device rotated to portrait, and had
for a while. Studio keeps the simulated orientation apart from the device. A phone-portrait
capture left it on Portrait, and the gate's edit-time device switches (`SET_DESKTOP_DEVICE` for
the regression lane, `SWITCH_AND_MOUNT` for story checks) set the device but never the
orientation. Measured live on the workstation: in Portrait every AU device is swapped (1920x1080
shows as 1080x1920, 390x844 as 844x390). In LandscapeRight each shows its configured size,
including 390x844.

The measured sizes were not affected. The play-client captures read their viewport back (all
five sizes and the 1920x1080 restore matched), and the story checks compare screen and capture
size with the size they asked for. The rotated state was what the owner saw in Studio's edit
view.

- `DEVICE_HELPER.pinLandscape()`: every edit-time device switch pins
  `ScreenOrientation.LandscapeRight`. The AU devices' Width/Height already are the intended
  viewport.
- `SET_DESKTOP_DEVICE` reports `orientation=... resolution=...` in its raw line.
- Every `calls` run ends by setting AU_1920x1080 landscape again (best effort), so the owner's
  Studio is never left rotated.
- Test: `test_every_device_switch_pins_landscape_and_the_run_ends_on_it`.

## v0.181.11 — automatic Studio runs never reuse tier C

2026-11-02 14:2x. All five VFX sprints (AS-V08..V12) finished on the vfx-2 track. Its merge
Studio batch then failed in 11 seconds, before any Studio call ran: `FAIL: GATE no code -
src/tests changed since the previous commit (tier C)`. AS-V08's last host half had been tier C
("nothing under src/ or tests/ changed"), and `studio_calls.default_request` copied that tier
into the batch. A merge always brings in master's code (here GF-SINK1's), so tier C fails by
definition. The Lead reopened AS-V08 and AS-V12 on main as repairs for nothing.

- `studio_calls.code_tier()` turns tier C into A for every automatic request: the re-gate on
  new code and the track merge's Studio batch, including its shared regression run. Tiers A
  and B are kept. A session that asks for tier C itself is unchanged.
- Test: `test_an_automatic_request_never_copies_tier_c`.

## v0.181.10 — a regression suite's own frame no longer fails another sprint's gate

2026-11-02 12:4x. The audio-int track's merge Studio batch was red for AS-AI1, although every
suite passed: AS_P5 12/12, AS_V00, and AS_AI1 12/12. AS_P5's check, re-run as regression,
staged its own frame at `docs/evidence/AS-P5/proxy_telegraph_...png`. The runner refused it
because it was not under `docs/evidence/AS-AI1/`, and the `FAIL: CAPTURE` line turned the gate
red. The Lead reopened AS-AI1 as a repair for nothing. Every sprint that re-runs AS_P5 (AS-D18,
S55, ...) would hit the same thing.

- **`Gate.capture_ready(text, suite)`:** the CALL's regression-index suite is passed in. A
  frame under `docs/evidence/<that suite's sprint>/` (suite `AS_P5` → `AS-P5/`), staged by a
  suite that isn't the sprint's own, becomes `NOTE: CAPTURE ... skipped: another sprint's
  frame` and nothing is written. A path outside `docs/evidence/`, a suite writing into a third
  sprint's folder, the sprint's own suite in the wrong folder, or no known suite is still
  refused with `FAIL` (the 2026-10-29 rule; `test_capture_ready_outside_the_sprints_evidence_is_refused`
  unchanged).
- Test: `tests/coordinator/test_studio_gate_calls.py`
  (`test_a_regression_suites_own_frame_is_skipped_not_failed`; fails on v0.181.9).

## v0.181.9 — timing work stays off the Studio slots

2026-11-02 12:xx. Victor: "The Slots are very slow systems as far as gaming goes, why is that the
test?" The VFX chain AS-V08..V12 ran on a track bound to slot-1. There Studio renders in
software under Wine at about 36 fps, and AS-V08's one-frame telegraph-timing check failed.
Victor then approved the test fix (it reads the runtime's own resolve frame) and asked that
timing work never run on a slot.

- **`tracks set-slot <track> none`:** unbinds a running track from its Studio slot. It is refused
  while a sprint holds the slot or has a scripted gate pending, like a move. `studio` leaves the
  track's needs, its kilo.jsonc is rewritten without the Studio MCP servers, so its sessions
  never reach the workstation's Studio, and Kilo reloads that folder before the next session.
  Its sprints' Studio checks then run on the workstation in the merge's Studio batch. A
  `track_slot_changed` event records the change with `to: ""`.
- **`[automation] workstation_only_sprints`** (sprint-id globs, in the Automation UI): sprints
  whose Studio checks measure frames or performance are refused on a slot-bound track, by
  `tracks start --slot` and by `set-slot <slot>`.
- **`TrackManager.studio_less`:** reads the track's slot from the store. A CLI `set-slot` runs
  in another process, so the live engine's cached track profile still named the old slot.
  `set-slot` also drops that cached profile in its own process.
- Tests: `tests/coordinator/test_tracks_set_slot.py`
  (`test_a_track_unbinds_from_its_slot_and_its_studio_work_moves_to_the_merge`,
  `test_workstation_only_sprints_never_go_on_a_slot`; both fail on v0.181.8).

## v0.181.8 — a repair's broader suite runs the tests its change can reach

2026-11-02. A coordinator repair's broader suite defaults to the whole `tests/coordinator`
folder (125 files, about 1,770 tests). The test conftest caps xdist workers by free memory,
and this workstation often has about 1.4 GB free, so that folder runs serially for about 35
minutes, more than twice the 900 s pytest limit. GF-LOBBY1's candidate timed out this way.
v0.181.7 moved validation off the tick; this release makes the broader run fit.

- **Affected tests (`lead_manager.repair_broader_select`, default `affected`):** a folder in
  the broader suite, whether the default or named by the Lead, runs only its test files that
  import a changed module (`foundry_coordinator.x.y`, or `from foundry_coordinator.x import
  y`), test files named after it (`test_y*.py`), and the changed test files. The run is still
  judged as the folder: at least one test passes and none fail, so a selected file that is
  all skips is fine. The folder runs whole when the selection can't be followed: a changed
  non-Python file, a changed helper or conftest under tests, or no match. For GF-LOBBY1's
  change (scope.py plus the AU adapter) that is 26 files instead of 125; a bg_checks change
  is 2. `all` restores the old behavior. Explicitly named files and node ids run as named.
- **Broader time limit (`lead_manager.repair_broader_timeout_minutes`, default 45):** the
  broader pytest run has its own limit. Reproduce, existing-test and focused runs keep 900 s.
  Since v0.181.7 a long run no longer stalls the coordinator, only the case clock
  (`max_case_minutes`, 240).
- Both settings have UI controls (Lead section) and are validated: `affected` or `all`, and
  at least 1 minute.
- The Lead's report contract now says a folder in broader_tests runs only its test files
  that import a changed module. A timed-out run's rejection reason asks for fewer, more
  specific tests.
- Tests: `tests/coordinator/test_repair_workspace.py`
  (`test_affected_tests_picks_the_folder_files_that_import_a_changed_module`,
  `test_run_tests_affected_runs_only_the_folder_tests_the_change_reaches`,
  `test_the_broader_run_has_its_own_time_limit`) and `tests/coordinator/test_lead_manager.py`
  (`test_the_lead_passes_broader_selection_and_its_time_limit_to_the_gate`).

## v0.181.7 — Lead candidate validation runs off the tick

2026-11-02. Between 05:57 and 06:17 the coordinator wrote no events, logged nothing and
started no sessions. GF-LOBBY1's recovery case (rc-616ceb9a980df824) was validating its
repair candidate, and `candidate_validation` ran pytest inside the engine tick. The broader
suite was the whole `tests/coordinator` folder (1,952 tests). The test conftest caps xdist
workers by free memory, and with about 1.4 GB free that suite runs serially for about 35
minutes. It hit the 900 s per-run limit, the candidate was rejected as "timed out", and every
other sprint waited 16 minutes.

- **Lead `_repairing`, validate phase:** the candidate gate (regression reproduce, existing
  tests, focused and broader pytest, the independent review) runs as an `engine.bg_checks`
  job keyed `(project, task, "lead_validate", "<attempt>:<candidate sha>")`. A tick waits at
  most `[automation] inline_check_wait_s` for it, then moves on. The case stays REPAIRING in
  the validate phase and its blocker says the validation runs in the background (queued when
  the check pool is full; a validation gets one slot beyond `max_parallel_checks`). The next
  tick after the job finishes applies the result: accept, or reject as before. The timeout
  (`repair.DEFAULT_TIMEOUT`, 900 s per pytest run), the journaled review (never sent twice)
  and the result format are unchanged. Without a job runner the gate still runs inline.
- **Engine background-job sweep:** keeps a validation job while its case is still
  validating that attempt and candidate, and drops it once the case moves on (cancelled,
  escalated, a new attempt).
- **Landing idle check (`prep_queue`):** a Lead validation runs in its own repair worktree,
  so it no longer counts as a background check in the project's main checkout.
- **`BackgroundChecks.run`:** a crash-type exception from a job (KeyboardInterrupt,
  SystemExit) is still raised on the tick, but the job is forgotten first, so a restarted
  caller runs it again instead of getting the same exception.
- **Timeouts are named:** the validation action stores `test_runs` (each pytest run's ids,
  seconds, timed out or not, rc, problems, log tail). A rejection adds one reason per run
  that timed out, for example "the broader pytest run of tests/coordinator timed out after
  900 s: name smaller broader tests". That reason goes back to the Lead. The Lead's report
  contract also says broader_tests names the test files of the changed modules, never the
  whole tests/coordinator folder.
- Wrong test paths (`tests/test_engine.py` for `tests/coordinator/test_engine.py`) were
  fixed in v0.181.6.
- Tests: `tests/coordinator/test_lead_manager.py`:
  `test_candidate_validation_runs_off_the_tick_and_the_case_picks_up_the_result`,
  `test_engine_tick_returns_while_a_candidate_validates_and_keeps_the_job`,
  `test_background_validation_job_is_dropped_once_its_case_leaves_validation`,
  `test_a_timed_out_candidate_run_is_named_in_the_rejection_and_the_action`.
- Deploy only after a read-first Lead-case check (no DIAGNOSING or REPAIRING cases, no open
  lead sessions).

## v0.181.6 — the Lead can grant review calls, and stops looping on a used review budget

2026-11-02. Between 02:42 and 04:41, AS-P10 opened seven Lead cases on "review budget used
(N/N review calls, budget ledger)", one Opus call each, until the operator reset the
budget. The Lead's operations raise attempts and bounds, never review calls, so every
case ran `extend_budget` → reverify/resume → the same block. All seven cases had one
repeat key. The repeat guard escalated, but `max_fingerprint_repeats` is a delegable
bound, so the Lead took the question back and raised the bound itself. The daily
automatic review-extension cap (`self_repair_max_reviews_per_day` = 2) was already used
at 02:36, before the first case opened. In the same incident, two `coordinator_repair`
candidates were rejected: c1 because its new test file was outside the declared files,
c2 because the Lead named `tests/test_engine.py` (the suite is
`tests/coordinator/test_engine.py`) and validation reported "not collected".

- **New Lead operation `extend_reviews`** (`{"op": "extend_reviews", "calls": 1|2}`):
  writes `policy:review_calls:lead-<case>-<n>` ledger rows as the policy actor. They
  count against the same daily cap as the engine's automatic review extensions. A
  `lead-…` reference is valid only for an open case of the same task whose trigger is
  `review_budget`, with full authority on. Before this, any key naming an unreviewed
  head passed the check. With `then: reverify` the head is verified and reviewed again.
- **One escalation instead of a loop:** a `review_budget` case whose daily cap is used
  (or whose project has no budget ledger) goes straight to NEEDS_VICTOR with reason
  `review_extension_cap`, with no Lead call. An `extend_reviews` refused by the cap does
  the same, instead of returning to DIAGNOSING. That reason is not a delegable bound, so
  the Lead never takes the question back. Unanswered, the decision's capped self-repair
  parks the task.
- **Report contract:** for a `review_budget` trigger, a report whose action leads back
  to review without `extend_reviews` is refused, and so is raising
  `max_fingerprint_repeats`. The contract text names `extend_reviews` and says that
  attempts and bounds never add review calls.
- **Coordinator repairs and tests:** new files and existing `test_*.py` modules under
  `tests/` are always in a repair's scope. Changed expectations still need the
  declaration plus operator approval. Existing helpers, fixtures, `conftest.py` and
  `__init__.py` under `tests/` still need declaring. `RepairWorkspace.validate` maps a
  test id whose file is missing to the only file of that name under `tests/` (reported
  as `test_paths`, used in the manifest). The repair prompt and the report contract say
  where the suites live.
- Tests: `tests/coordinator/test_lead_review_budget.py` (6 tests; 5 fail on v0.181.5)
  and `tests/coordinator/test_repair_workspace.py`
  (`test_replay_repair_may_add_and_extend_tests_outside_its_declared_files`,
  `test_replay_bare_test_paths_resolve_to_tests_coordinator`; both fail on v0.181.5).

## v0.181.5 — scope grants survive a non-UTF-8 byte in the plan

2026-11-02 06:2x. GF-LOBBY1's plan Notes held one cp1252 byte, 0x97 (an em dash pasted from
Windows). The AU adapter's `grant_scope` read the plan as strict UTF-8, so all five approved
scope grants (Net.lua, RateLimit.spec, EntryAdapter, EntryRoot, ReadyCheckNegative) failed
and never reached the file list, and the session's edits stayed denied. The Lead found the
cause, but its repair candidate was rejected when validation timed out. That validation
also stalled the coordinator for about 16 minutes (see the follow-up).

- **`AnimalUprisingAdapter.grant_scope`:** reads and writes the plan with
  `errors="surrogateescape"`, so bytes that aren't UTF-8 pass through unchanged and the
  grant line is still added.
- Test: `tests/coordinator/test_revamp_scope.py`
  (`test_the_au_grant_works_on_a_plan_with_a_stray_cp1252_byte`; fails on v0.181.4 with
  UnicodeDecodeError).

## v0.181.4 — approval frames include the plan's own sub-folder globs

2026-11-02 05:0x. After v0.181.3, AS-P10's visual approval answered "no frame matches
docs/evidence/AS-P10/cand_A/*.png", and the sprint waited for a person. Its 16 frames are
committed, but `approval_artifacts` listed only the rule's own pattern
(`docs/evidence/{task}/*.png`), which does not reach sub-folders.

- **`GenericAdapter.approval_artifacts`:** the frames an approval rule covers are its own
  patterns plus the plan's glob scope entries that fall under them (for example
  `docs/evidence/AS-P10/cand_A/*.png`). All callers share this one list: the visual
  approval, the engine's approval binding, the asset judge, control and telemetry.
  Without such entries the list is unchanged.
- Test: `tests/coordinator/test_visual_model_approval.py`
  (`test_approval_artifacts_list_the_frames_of_the_plans_own_subfolder_globs`; fails on
  v0.181.3).

## v0.181.3 — visual approval expands a glob in the plan's scope

2026-11-02 04:3x. AS-P10's plan lists `docs/evidence/AS-P10/*.png` in its scope.
`visual_approval._required` took the glob as a literal frame path and reported "required frame
docs/evidence/AS-P10/*.png is missing". The automatic visual approval sent the sprint back
four times after code review had approved it. Each round cost a review, so the 5-review
budget ran out, and the Lead looped on "review budget used".

- **`visual_approval._required`:** a scope entry with `*`, `?` or `[` requires every evidence
  frame (PNG/JPEG) it matches. A glob that matches no frame reports "no frame matches <glob>".
  Literal entries are unchanged.
- Test: `tests/coordinator/test_visual_model_approval.py`
  (`test_a_glob_scope_entry_requires_the_frames_it_matches`; fails on v0.181.2).

## v0.181.2 — move a never-started main sprint onto a track without cancelling it

2026-11-02 02:4x. All asset tracks had merged, so only one Claude sprint was running (main)
while the cap allows 5. The VFX chain AS-V08..V12 shares files only with later sprints, so it
went to a track, `vfx-2` on slot-1. `tracks start` takes only sprints that are not yet on the
queue, so the five were cancelled on main first. In that window AS-D18 and S53 saw a
CANCELLED dependency, stopped BLOCKED and opened two Lead cases. I unblocked both.

- **`tracks start` / `TrackManager._movable`:** a main-queue sprint that never started
  (QUEUED or READY, attempt 0, no session) moves onto the track as it is. Its own deps stay,
  and the track chain adds the sprint before it. A READY one returns to QUEUED
  (new transition READY → QUEUED, which requires `moved_to_track`), so its deps are checked
  again on the track. Each move records a `moved_to_track` event. A sprint that has started
  is still refused ("only a never-started sprint moves").
- Tests: `tests/coordinator/test_tracks_manager.py`
  (`test_a_never_started_main_sprint_moves_onto_a_track_without_cancelling`; fails on
  v0.181.1). `test_create_refuses_...` now checks the started case.

## v0.181.1 — say why Claude usage was not read

After v0.181.0 went live, no Claude usage reading arrived and nothing said why. Now a failed
or empty read records a `claude_usage_unread` event and a log line, once per distinct
reason. An empty read names the routes the router answered with: backend, state and account
count. The last good reading still stands. The AU profile turns the cap on:
`[resources] claude_agents = 5`, `claude_agents_over = 2`, `claude_usage_pct = 75`.

## v0.181.0 — Claude agent cap that drops at a usage threshold

2026-11-02 01:5x. Victor: "keep 4 or 5 right now ... after 75% usage on Claude, then it's only
1 local LLM and 2 Claude agents".

- **`resources.claude_agents`** (default 0 = no cap): the number of sprint sessions served by
  Claude that may run at once. A sprint whose implementer persona is in
  `resources.claude_personas` (empty = the implementer role's persona) takes one unit of a
  new `claude` resource for its session. Further sprints wait ("waiting for claude ..."),
  first in queue order first, as for `gpu` (v0.180.7).
- **`resources.claude_agents_over`** (default 2) at **`resources.claude_usage_pct`**
  (default 75): once Claude usage reaches the threshold, the cap drops to this number.
  Usage is the highest 5-hour or weekly window the router reports for the
  `resources.claude_backend` (default `meridian`). The coordinator reads it every 2 minutes
  from the helper-admission answer for a Claude persona. A failed read keeps the last
  reading. Running sessions finish and only new ones wait. Each crossing records a
  `claude_agent_cap` event and a log line.
- The local model is unchanged: `resources.local_streams` (1) units of `gpu`.
- Schema (Automation UI): the five fields under Workspace → resources.
- Test: `tests/coordinator/test_engine.py`
  (`test_claude_agents_are_capped_and_the_cap_drops_at_the_usage_threshold`).

## v0.180.7 — Strata goes to the sprint first in line

2026-11-02 01:2x. AS-A02 (queue position 119) waited 30 minutes for the single Strata unit
while AS-A22 and then AS-A21 (positions 134, 137) started fresh sessions on it. The sprint
giving up Strata releases it during its own engine step. The sprint stepped right after it
in the same tick took the unit before AS-A02, which steps first, looked again. So an early
sprint lost every hand-off.

- **`engine._acquire` / `_gpu_first_in_line`:** a sprint about to take a `gpu` unit steps
  aside when a sprint earlier in queue order is already "waiting for gpu", between sessions
  or mid-sprint (READY, CHANGES_REQUIRED, IMPLEMENTING or CLOSING), and holds no unit. Its
  note says "waiting for gpu (<task> is first in line)". A stale note on a stopped task
  does not hold the line. The existing "main sprint goes first" rule is unchanged.
- Test: `tests/coordinator/test_engine.py`
  (`test_the_gpu_unit_goes_to_the_waiter_first_in_queue_order`; fails on v0.180.6).

## v0.180.6 — self-heal ends sessions their task no longer follows

2026-11-02 00:4x. Twelve main sprints sat behind the landing drain for an hour. Four prep
entries waited on "session ses_e659b746 (implementer) of AS-F30 is open". That session was
an implement turn cut off by last night's forced Kilo restart. Kilo still reported it busy,
2.2 hours later. Since then AS-F30 had been completed and reopened by a Lead repair, and was
READY with no session of its own. Nothing ever ended such a session, and
`prep_queue.safe_point` asks Kilo, so the landing waited for it. The same restart left three
more leftovers (AS-A08, F29, J06; those tasks are COMPLETE).

- **`selfheal.dead_sessions`, rule `orphan_session`:** an implementer session that is not
  the task's `session_id` or its `closeout_session` is aborted in Kilo (best effort) and
  ended with outcome `self_heal_orphan` once nothing has touched it for 30 minutes (or for
  the dead-session grace, if longer). The task itself is not touched. The task's own
  session stays under the `dead_session` rule.
- Test: `tests/coordinator/test_selfheal.py`
  (`test_an_implementer_session_the_task_no_longer_follows_is_ended`; fails on v0.180.5).

## v0.180.5 — the gate names a commit Rojo cannot serve and hands it to the sprint

2026-11-02 00:3x. AS-H07 wrote 38 animation clips as rbx_xml text but named them `.rbxm`,
under the Rojo-mounted `assets/roblox/Animations/`. Rojo reads `.rbxm` as the binary format
and refused the whole project ("Malformed rbxm file ... Invalid file header"), so slot-2's
Studio kept the pre-sprint sources. Every gate failed PRE-PLAY verify as "stale sync". The
Lead could not reach the slot to see Rojo's error, so it parked the sprint on a question for
Victor.

- **`studio_gate.rojo_unservable(repo, rev)`:** lists the files Rojo mounts at HEAD that are
  `.rbxm` but start with `<roblox xmlns` (rbx_xml text). Binary `.rbxm` files, `.rbxmx` files
  and unmounted files are fine.
- **PRE-PLAY verify:** when it fails and such files exist, the ERROR now says
  `PROJECT: Rojo cannot serve HEAD ...`. It names the files and the fix: rename them to
  `.rbxmx`, or move upload-only clips out of Rojo's tree.
- **`studio_calls`:** this error goes back to the sprint's session, the same way the unmapped
  case (`PROJECT_MARKERS`) does, instead of a tooling stop or a Lead case.
- Test: `tests/coordinator/test_studio_gate.py`
  (`test_rbx_xml_text_named_rbxm_is_the_sprints_to_fix_not_stale_sync`).

## v0.180.4 — a sprint keeps Strata while it waits on a short decision

2026-11-01 23:4x. Victor: "AS-A02 doesn't look like it's moving". AS-A02 stopped for 3 minutes on a
decision with a timeout default, gave up the single local-model unit, and AS-A10 took it for a
whole turn. AS-A02, with only its uploads and closeout left, waited 10 to 30 minutes.

- **`engine._lc_wait`:** when one of the decisions answers itself within
  `LEAD_SHORT_DEFER_SECONDS` (15 min), the sprint keeps its `gpu` unit(s) and releases the
  rest. `store.transition` frees everything on BLOCKED, so the kept unit is taken back in the
  same engine step. The resumed sprint picks its own unit and starts at once. A decision with
  no deadline, or a far one, releases everything as before. The `decision_wait` event lists
  what was kept.
- **`selfheal.stale_locks`:** it leaves those `gpu` units alone while the decision is short.
  This is the same `_main_pause_is_automatic` rule that keeps the main checkout.
- Test: `tests/coordinator/test_engine.py`
  (`test_a_short_decision_wait_keeps_the_sprints_gpu_unit`; fails on v0.180.3).

## v0.180.3 — a frontier sprint is not pinned to Strata while the frontier serves

2026-11-01 23:3x. Victor asked whether every sprint was moving. AS-F28 (a Blender sprint, frontier
persona) got one "backend 'strata' serves the main stream only" right after a connection reset.
`infra.session_failure` then held it for `gpu` for an hour (`LOCAL_FALLBACK_S`), behind the serial
audio sprints. Meanwhile four sprints with the same persona were being served by Claude and
ChatGPT.

- **`engine._session_locks` + `_frontier_serving`:** a `local_fallback_until` hold is dropped,
  with a `local_fallback_cleared` event, while another sprint with the same implementer persona
  has a session working and holds no `gpu` unit. The local model serves one stream, and that
  stream holds `gpu`. With nobody served (the 2026-10-30 case: frontier quotas out) the hold
  stands as before.
- Test: `tests/coordinator/test_engine.py`
  (`test_a_local_fallback_hold_clears_while_the_frontier_serves_the_same_persona`; fails on
  v0.180.2).

## v0.180.2 — the Blender skill says how to save a slot Studio screenshot

2026-11-01 23:1x. AS-F04 and AS-F21 stopped BLOCKED. On a slot track, `capture_screenshot`
shows the model the image but writes no file, and `tools/capture-window.ps1` captures the
workstation's Studio, so the capture evidence could not be saved. The Lead solved AS-F04 with a
one-off script.

- **AU** (prep pq-078a965432): `tools/assets/slot_capture.py` runs that script as a self-tested
  tool. It starts the track's own robloxstudio-mcp (already pointed at the slot) and saves the
  PNG. It refuses to run outside a slot track, so the workstation's TEST place is never
  captured.
- **`au-blender-asset` skill (Traps):** sessions frame the view with `execute_luau`, then call
  `slot_capture.py` once per view. The skill reaches the game's `.kilo/skills` at the next
  install check (`skills.check_seconds`).

## v0.180.1 — audio closeouts write themselves again; a templated closeout never waits for Strata

2026-11-01 22:5x. Victor: "AS-A08 says closing but nothing seems to be running".

- **`closeout_template.master_order`:** a sprint MASTER_ORDER has no status line for is
  "nothing to update", not a failure. Two or more lines is still refused.
  - **What happened:** sessions write "MASTER_ORDER" into their plans, because the CLOSE
    duties name it. MASTER_ORDER lists the audio batch in a paragraph, with no line per sprint.
  - **Effect:** every audio sprint's templated closeout failed (AS-A01, A07, A13, A19, E10,
    P9) and fell back to a CLOSE session on Strata, one at a time.
- **`engine` templated closeout:** it takes no `gpu` unit. The template is coordinator work with
  no model call. AS-A08 sat CLOSING behind AS-A19's Strata turn only to write its summary.
- Tests: `test_revamp_gate.py`
  (`test_au_template_skips_master_order_when_it_has_no_line_for_the_sprint`) and
  `test_engine.py` (the templated closeout drops `gpu`).

## v0.180.0 — move a running track to another Studio slot; a track's Kilo folder reloads itself

2026-11-01 22:4x. Victor: "split them". All 8 Blender tracks were started on slot-1, so their
Studio checks queued there while slot-2 sat idle. A track's slot was fixed at its start.

- **`tracks set-slot <project> <track> <slot>`** (`TrackManager.set_slot`):
  - It moves a running track to another enabled slot.
  - It is refused while one of the track's sprints holds a slot lock or has a scripted Studio
    gate running or pending.
  - The track gets the `studio` need. Its kilo.jsonc is rewritten with the Studio MCP pointed
    at the new slot. Its Studio gates follow at once, because the slot is read per run.
- **The track's Kilo folder reloads itself** (`kilo_reload` in the track data, set by
  set-slot and set-needs):
  - Before the track's next session starts, the engine drops Kilo's instance for that
    worktree only (`KiloClient.dispose`, POST /instance/dispose). The session reads the new
    kilo.jsonc.
  - Other tracks' sessions are untouched. Before this, a needs change only took effect after a
    full Kilo restart, which cut 4 running turns.
  - A failed reload keeps the flag and never blocks the start.
- Tests: `tests/coordinator/test_tracks_set_slot.py` (4).

## v0.179.7 — a sprint waiting for a landing commits its own leftover plan history first

2026-11-01 22:2x. AS-V04's session ran `plan_new.py --set-next` after its last commit, which
wrote `docs/progress/AS-V04_PLAN_HISTORY.md`. Its gate path then went to CHANGES_REQUIRED
without passing VERIFYING, the one place the coordinator commits such tool-written files
(`[automation] auto_commit_task_files`). The main checkout stayed dirty, so the prep landing
pq-17d5c583ba never found a safe point. AS-V04, GF-RT3 and the VFX chain waited behind it.

- **`engine` landing drain:** before a main sprint between sessions gives way to a waiting
  landing, the coordinator commits that sprint's own `auto_commit_task_files`.
- Test: `tests/coordinator/test_engine.py`
  (`test_a_drained_sprint_commits_its_left_over_plan_history_first`; fails on v0.179.6).

## v0.179.6 — a session whose turn Kilo stopped running ends after 5 minutes, not 4 hours

2026-11-01 22:1x. A forced Kilo restart (to load the slot tracks' Studio MCP) cut four turns
mid-reply: AS-A08, AS-F29, AS-F30 and AS-J06. Kilo no longer ran them, but each unfinished reply
read as "busy". The sessions kept the Strata unit and three Blender units, so AS-F04 and every
audio sprint waited behind them, until the 4-hour session timeout.

- **`engine._stalled_dead`:** the Kilo adapter already marks such a turn `stalled` (an
  unfinished reply that is not in Kilo's busy/retry list). The engine now records when that
  started; continuously stalled for `[limits] infra_retry_abort_seconds` (300) means the
  session is aborted and the sprint retries for free from its recorded state, as the Lead
  already did for its own sessions. Any live poll clears the mark.
- Test: `tests/coordinator/test_engine.py`
  (`test_a_stalled_turn_ends_the_session_after_the_infra_window`; fails on v0.179.5).

## v0.179.5 — version strings

v0.179.4 shipped its fix but left both `__version__` strings at 0.179.3, so the coordinator
heartbeat reported 0.179.3. This release only sets them (no code change).

## v0.179.4 — a track bound to a Studio slot always gets the slot's Studio MCP

2026-11-01 21:5x. AS-F04 and AS-J06 stopped BLOCKED: "Studio MCP not routed to slot-1" / "No
Roblox_Studio MCP tools available in this session". Their tracks were started with `--slot
slot-1`, but the Blender plans read as needs `blender` alone. A track's kilo.jsonc enables only
the Studio MCP servers its needs turn on, and the slot routing then points only enabled servers
at the slot, so the sessions had no Studio tools at all.

- **`tracks.manager.create`:** a track created with a slot always needs `studio`, so its
  kilo.jsonc enables Roblox_Studio and robloxstudio-mcp and they run on the slot over ssh.
- The 8 live slot tracks were fixed with `tracks set-needs <track> blender studio`, which
  rewrites kilo.jsonc and re-points the servers.
- Test: `tests/coordinator/test_tracks_manager.py` (`test_a_track_bound_to_a_slot_always_needs_studio`).

## v0.179.3 — Settings: list settings (approval.rules, verify.checks) take a JSON list

2026-11-02. The pilot's `approval.rules` (the existing visual rule plus the AS-P10 asset rule)
could not be saved: the Settings page checked every table field as a JSON object and showed "A JSON
object" for the list. The router itself always accepted a list (`schema.check`).

- `automation.html` `parseField`: a table field whose default is a list takes a JSON list of
  objects ("A JSON list of objects" otherwise); the others still take a JSON object.
- Reaches the page with a router rebuild. The coordinator is unchanged (no deploy needed).
- Tests: `tests/test_automation_ui.py` (runs the page's parser in node: a list passes for a list
  field, an object and a list of non-objects are refused, object fields unchanged).

## v0.179.2 — the templated closeout takes no Blender unit either

2026-11-01 21:2x. After v0.179.1 AS-D08 still waited in CLOSING for a Blender unit while holding
slot-1: the templated closeout (the no-model one that runs before any CLOSE session) asked for
`_session_locks` with a Blender unit, a path v0.179.1 missed.

- **`engine._close_locks`:** one helper for both closeout paths. It drops every Blender unit and
  first gives back any Blender unit or idle Studio-slot lock the task still holds.
- Test: `tests/coordinator/test_engine.py`
  (`test_close_locks_drop_blender_and_give_back_held_units_and_idle_slots`).

## v0.179.1 — Studio gates and CLOSE take no Blender unit; a session end gives back an idle Studio slot

2026-11-01 20:5x. Two Blender tracks blocked each other. AS-D08 was CLOSING (docs only) and
waited for a Blender unit; AS-F30 held a unit and waited for slot-1, which AS-D08 still held
from its gate run. Only another sprint finishing its Blender work could break the loop.

- **`engine` CLOSE start:** the CLOSE session's locks drop every `blender` unit. Before it
  starts, the task lets go of any Blender unit it still holds, and of an idle Studio-slot lock.
- **`engine._release_session`:** a session end also gives back a `slot:<id>` lock when no
  scripted Studio run of the task is active (`_idle_slot_lock`). Before this, a slot stayed held
  from the gate until the sprint completed, through review and CLOSE.
- **`studio_calls._run_locks`:** a Studio gate run takes no Blender unit. Built with no task, it
  named the pool's first unit, so every slot track's gate waited behind the one sprint holding
  `blender` (AS-E10, F04, F17, F19 behind AS-F30).
- Tests: `tests/coordinator/test_slots.py` (`test_a_gate_run_never_waits_for_a_blender_unit`) and
  `tests/coordinator/test_engine.py`
  (`test_a_session_end_gives_back_an_idle_studio_slot_lock`,
  `test_a_close_takes_no_blender_unit_and_lets_go_of_a_held_slot`; both fail on v0.177.0).

## v0.179.0 — an AU plan's `ref_views` reaches the engine (D3 fix)

2026-11-01. Found while pre-coding the Phase C pilot. v0.171.0 looked for the word `ref_views`
in the task spec's `## Needs` block. The AU adapter condenses each plan into a spec without
that block, so an AU sprint's `ref_views` was never seen, and D3's standing OK
(`ref_views_images = allow`) could not apply to any AU sprint.

- **AU adapter (`_parse_plan`):** a plan whose `## Needs` lists `images` and the word
  `ref_views` gets task data `ref_views: true`. The engine already reads that data.
- **`services.ref_views_in_text`:** the Needs-block scan, shared by the adapter and
  `ref_views_declared`.
- Test: `tests/coordinator/test_ref_views.py`
  (`test_the_au_adapter_carries_ref_views_as_task_data`). The AU adapter suites (459) pass.

## v0.178.0 — the asset judge scores candidates blind (Phase C pilot)

2026-11-01. Plan: `docs/plans/ASSET_PILOT_PLAN.md` (Victor: "Yes, start the pilot"). The pilot
rebuilds AS-J03's lever the new way and compares it with the shipped one. The v0.167.0 judge
could only score one set of images per task.

- **Approval rules: `candidates`** (asset rubric only). They are artifact globs with `{task}`,
  one per candidate (e.g. `docs/evidence/{task}/cand_A/*.png`, `.../cand_B/*.png`).
  - One checklist is written once from the design. Each candidate is then scored in its own
    blind call against it: only that candidate's images, letters shuffled per candidate, no
    file names, and a line saying the images show ONE candidate.
  - Event `asset_judge_candidates`: per candidate, its score, verdict, findings and files.
    It is recorded once per revision.
  - The best candidate decides the task's verdict: approved first, then the most items
    passed, then the fewest findings. A CHANGES verdict sends its findings back, prefixed with
    the candidate.
  - Fewer than two candidates with images scores as before. Rules without `candidates` are
    unchanged, and the single-model prompt is byte-identical (refactored into
    `score_parts`).
- The pilot rule and the settings Victor turns on are listed in the plan. The pilot sprint is
  AU `AS-P10`, pre-coded and queued through the prep queue.
- Tests:
  - `tests/coordinator/test_asset_judge.py` (+4);
  - the judge, visual-approval, profile, engine and automation suites: 140 pass.

## v0.177.0 — slots: Studio's reopen puts missing plugins back

2026-11-02. Slot-2's Studio had no MCP plugin and no Rojo plugin, so Foundry could not reach it,
and it showed as a lost sign-in ("Connecting..."). The cause: `entrypoint.sh` copies the plugins
into the Wine prefix only when the container starts, and slot-2's prefix had been rebuilt while
the container ran (the half-built prefix moved aside, v0.166.2), so the new prefix never got
them. Fixed by hand on 2026-11-02: the plugins were copied in and Studio reopened; the MCP check
then passed.

- `studio-reopen.sh` copies the plugins (`cp -u`, only new or newer files) into
  `%LOCALAPPDATA%\Roblox\Plugins` while Studio is closed, before it opens again. Every reopen,
  by Foundry or by hand, now puts them back.
- Reaches a slot with its image: a reinstall (Access → Machines → New code, then
  `setup-host.sh`). Slot-1 and slot-2 got the new script copied into their running containers
  on 2026-11-02.
- Tests: `tests/test_machines.py` (a new test: the copy sits between closing Studio and opening it).

## v0.176.0 — Linux main workstation: installer, enrollment, Export/Import, one active workstation (W3-W5); slot place field

2026-11-01. Plan: `docs/plans/LINUX_WORKSTATION_PLAN.md` W3-W5 (approved; built with the
recommended answers; the build notes in the plan fill in the details). Guide:
`docs/INSTALL_WORKSTATION.md`. Victor: "GO ahead and plan everything and then build it".

- **Access → Machines → Add a workstation** (machines.py): a workstation is a new machine kind.
  - Its one-time code is traded once at `/v1/foundry/enroll` for three keys of its own, stored
    hashed on the router:
    - `<name>`: inference + orchestration;
    - `<name>-kilo`: inference + media;
    - `<name>-relay`: operator.
  - The same answer also carries the router's Kilo provider block (`routes.kilo_block`, now
    shared), the implementer, and the game's clone settings.
  - "A Studio here" adds a Studio slot `<name>-studio` on the same IP and hands its code to the
    installer.
  - Revoke and Delete cover all three keys.
- **New project settings (Workspace):**
  - `workspace.clone_url`;
  - `workspace.repo_key_secret`: a secret in the game's group, an ssh deploy key or an https
    token, handed over once at enrollment;
  - `workspace.single_workstation`, on.
- **`install/workstation.sh`** (Debian 13 / Ubuntu 24.04):
  - the packages (Node 22, Kilo 7.8.8, rokit, Blender 4.5.12 with checksums);
  - a `foundry` user (uid ≥ 1001, so the Studio image keeps 1000), everything under
    `/srv/foundry`;
  - enrollment, the release at the router's version, a venv;
  - `foundry_client workstation apply` (keys into the secret store, client.toml, the repo key);
  - the game clone, then `workstation profile` (project.toml from the release example with this
    machine's paths, a first kilo.jsonc);
  - optionally `--import`, and the Studio slot installer for `<name>-studio`;
  - the systemd service (linger), apply-kilo-config, doctor.
  - `--import-only FILE` imports onto an installed workstation.
- **`install/create-workstation-lxc.sh`:** makes the Proxmox LXC (8 cores, 32 GB, 120 GB,
  nesting + keyctl, optional iGPU) and runs the installer inside it.
- **The client's secret store** (D-L5): `client.secret_store` = auto | keyring | file (Settings
  → Watchdog). `file` is a mode-600 JSON file; `auto` uses the OS keyring when one works, else the
  file. The Secrets tab says which.
- **Export / Import** (`foundry_client workstation export|import`, foundry_client/move.py), in
  the router backup's encrypted format:
  - **What travels:** the coordinator's state (SQLite copied consistently; no logs, backups,
    repair worktrees, ssh key or Windows credential files), project.toml, every kilo.jsonc, and
    a git bundle of the commits not on the remote plus every local branch tip.
  - **What it refuses:** to run while the coordinator runs, or with uncommitted changes to
    tracked files (it lists them).
  - **The old workstation steps down:** the coordinator is switched off in client.toml and the
    router hold is released, unless `--keep-active`.
  - **Import:** rewrites the old paths in any separator style, also inside the state DB (FTS
    tables skipped); recreates branches and track worktrees; turns Windows-only MCP servers off
    in kilo.jsonc and lists them.
- **One active workstation per game (W4):**
  - `POST /v1/foundry/control/workstation-active`: the first coordinator to check in holds the
    game, and another one is recorded on standby (with a warning alert).
  - `foundry_coordinator/workstation_gate.py` checks before the first tick and every minute.
    The engine skips a game on standby (that engine hunk already shipped in v0.172.0/v0.173.0,
    inert until now).
  - **It fails open:** no router, an older router, an error, or `single_workstation = false`
    all keep the game running.
  - Machines has a **Which workstation runs each game** card with **Make active** and Forget.
- **Blender on Linux:** the pool's `local` worker defaults to `blender` on the PATH or
  `/opt/blender/blender`, and the Kilo deny rules have a Linux set. The drop-in was already a
  shell script there.
- **Slot settings dialog:** a place id or link and a universe id. They reach the slot at its next
  secrets pull (Studio reopen or container start) and the coordinator at its next config pull.
- **Tests:**
  - `tests/test_workstations.py` (5);
  - `tests/test_workstation_client.py` (9, including an export → import round trip with a
    never-pushed track branch);
  - `tests/coordinator/test_workstation_gate.py` (4);
  - a test in `tests/test_machines.py`.
  - Related suites: 360 pass.
- **Dry run:** `install/workstation.sh --skip-tools --no-studio --no-start` in a fresh
  `debian:trixie` container against a local test router, with the release from a scratch clone.
  - **Passed:** the `foundry` user (uid 1001), enrollment, the release and venv, all five secrets
    in a mode-600 file, client.toml, the game clone, project.toml and kilo.jsonc with
    `/srv/foundry` paths, and a reused code refused.
  - **It found and fixed:** a code file without a final newline ended the script, and
    `foundry_client` was missing from pyproject's packages, so a pip install could not import
    it.
  - **Not run here (they need a real LXC):** the Node/Kilo/rokit/Blender downloads, the Studio
    slot step and the systemd service.
- **To use it:**
  - rebuild the router;
  - set `workspace.clone_url` (and `workspace.repo_key_secret` for a private repo);
  - add a workstation on Machines, then follow docs/INSTALL_WORKSTATION.md.
  - Your Windows PC keeps the game: it checks in first, so a new workstation starts on standby.

## v0.175.0 — build replay R4: re-run a journal entry on a slot (ships off)

2026-11-01. Plan: `docs/plans/BUILD_RERUN_PLAN.md` (built with the recommended answers). Build
replay showed every run but couldn't repeat one. Ships off: `blender.journal_rerun`.

- **Task page → Build replay:** a "Re-run on a slot" button on runs that recorded a commit.
- **Router:** a new control action `build_rerun {run_id, slot?}`.
  - It is accepted only for a run the router holds for that task, and is not bound to the
    task's state.
  - It is handed to a coordinator that declares the `build_rerun` feature.
- **Coordinator (`build_rerun.py`):**
  - **checks:** the setting is on, and the entry has a commit and arguments; it picks a slot
    (the pool's first, else `blender.slot`);
  - **runs in the background, one at a time:** pushes the commit to the slot mirror as
    `blender/replay`, syncs the library, and runs the same arguments under the pool's job
    locks with the journal's copy of the builder script;
  - **the outputs** go into the new entry's own folder under the journal, never into a
    worktree;
  - **records** a new `replay` entry (`where.replay_of`), with a note that other files
    uncommitted at the original run aren't included;
  - events `build_rerun_started` / `_done` / `_failed`.
- **To switch on:**
  - rebuild the router;
  - set `blender.journal_rerun` on.
- Tests:
  - `tests/coordinator/test_build_rerun.py` (4);
  - `tests/test_build_replay_router.py` (+2);
  - the control and automation suites: 256 + 118 pass.

## v0.174.0 — builder per model: a fresh au-builder subagent per 3D model (gap 9, ships off)

2026-11-01. Plan: `docs/plans/BUILDER_PER_MODEL_PLAN.md` (built with the recommended answers).
An AS- sprint built every model in one long session, so by the second model its context carried
all of the first model's Studio and upload work. Ships off: the report's Phase D waits for the
pilot, and D4 asks for a two-asset trial first.

- **`builder.py` + kilo.jsonc:** while `builder.enabled`, Foundry writes the Kilo subagent
  `au-builder` (main and track files):
  - `mode: subagent`, `steps` 200, a short "build ONE model, stop at the result lines" prompt;
  - no `model` unless `builder.model` is set, so it runs on the sprint session's model;
  - its permission is the implementer's block, with commit, push, tag, nested `task`, Studio,
    foundry-media, Media_Gen and the secrets folder denied, and `au-blender-asset` allowed.
- **Who may call it:** an implementer session of a task matching `builder.tasks` (globs;
  default `AS-*`). It gets `task: au-builder allow` after the role rules. A reviewer or helper
  never does.
- **The BUILD prompt** of a matching sprint:
  - one `au-builder` per model, several at once, given the pack, the short note, the script
    and absolute output folders;
  - the sprint keeps the look gate, the judge, export, upload, Studio, records and every
    commit.
- A `builder.*` change rewrites kilo.jsonc and reloads Kilo.
- The second candidate (D5) is not built yet; it needs the judge's threshold from the pilot.
- **To switch on (after the pilot):**
  - rebuild the router;
  - `builder.enabled` on, `builder.tasks` = the two trial tasks;
  - best with `skills.install` listing `au-blender-asset`.
- Tests:
  - `tests/coordinator/test_builder_per_model.py` (4);
  - the AU adapter suites: 447 pass.

## v0.173.0 — Foundry-shipped skills reach AU's Kilo; new `au-blender-asset` skill (gap 7)

2026-11-01. Plan: `docs/plans/FOUNDRY_SKILLS_DELIVERY_PLAN.md` (built with the recommended
answers). AU's Kilo skills were hand copies in its untracked `.kilo/skills/` (`foundry-images`
was already stale), and a new skill also needed an allow in kilo.jsonc. Ships off.

- **`skills_install.py`:** installs the skills `[skills] install` lists into
  `<workspace>/.kilo/skills/` (`skills.target`). Main and track sessions both read that folder.
  - Sources: `projects/<project>/kilo_skills/`, `contrib/media-adapter/skills/`,
    `contrib/skills/`.
  - **Refuses** when anything under the target is tracked in git (`skills_install_refused`).
    The AU repo is never edited.
  - Foundry's folders carry `.foundry-skill.json` and are updated when the source changes.
  - A hand copy is backed up to `<state_dir>/skills_backup/` first, then replaced.
  - A skill taken off the list is removed only when Foundry installed it.
  - It runs every `skills.check_seconds` (300), and at once on a list change.
- **kilo.jsonc:** each listed skill is allowed in the agent blocks (main and track files).
  Generated denies still win (`foundry-images` without `images`). A `skills.*` change
  regenerates kilo.jsonc and reloads Kilo.
- **New skill `au-blender-asset`:**
  - what it covers: the 3D conventions (+Y front, sides by axis), how Blender runs (pool,
    `--bg`, exit 75), the A-E one-asset loop with the AS-P9 reference pack and the short-note
    rule, and the traps;
  - an AS- prompt says to load it once it is installed.
- **To switch on:**
  - rebuild the router;
  - set `skills.install` = `au-blender-asset, foundry-images`.
- Tests: `tests/coordinator/test_skills_install.py` (7).

## v0.172.0 — local-model sessions run in parallel; audio sprints no longer take the GPU lock

2026-11-01. Victor: "Run the Audio Sprints in parallel with local AI (Strata) as the implementor,
and run them non stop, run as many parallel sprints as possible". Two things made that
impossible: every audio (and image) sprint held the one `gpu` lock for its whole track, and the
`gpu` lock had a single unit, so only one local-model session could run at a time.

- **Audio/images no longer imply `gpu`** (`tracks/analysis.py`, `tracks/resources.py`). Since
  2026-10-27 they run on Media_Gen (TrueNAS), which queues its own jobs one at a time. The `gpu`
  lock is now only the local model's sessions.
- **New setting `resources.local_streams`** (1-8, default 1; Automation → Settings → workspace).
  `gpu` gets that many units (`gpu`, `gpu#2`, ...). A local-implementer session takes a free
  unit and keeps it; when all are taken it waits as before. The main-sprint-first rule and the
  frontier CLOSE fallback count units.
- **Router side (Victor's setting):** the router must let that many sessions in. Backends → Pool
  → the local backend's `helpers_allowed` = local_streams - 1. While it is 0 the router serves
  one session ("serves the main stream only"), so keep local_streams at 1 until it is raised.
- Tests: `test_tracks_analysis.py`, `test_tracks_resources.py`
  (`test_gpu_units_follow_resources_local_streams`), `test_engine.py`
  (`test_local_streams_let_that_many_local_sprints_run_at_once`).
## v0.171.0 — turnaround recipe: draw an asset's missing views from its approved sheet (gap 5)

2026-11-01. Plan: `docs/plans/TURNAROUND_RECIPE_PLAN.md` (built with the recommended answers).
AU concept sheets show one side; the other side, the top and the underside were guessed. Ships
off.

- **`foundry-images` skill:** a "Turnaround views" section.
  - The order: a three-quarter view first from the approved sheet, then each missing view with
    the sheet + the three-quarter view as references.
  - Views are named by axis (`tq_+X`, `side_+X`, `side_-X`, `back`, `top`, `under`, `sheet`;
    the asset faces +Y, +X is screen-left in FRONT).
  - Fixed settings and prompt; it saves to `ref/drawn/` and never touches an approved
    `_ref_vN.png`.
  - At most six images per asset, one redraw per view; `ref_align.py --check-only` when AS-P9
    has landed.
- **D3 standing OK, as a setting:** `[automation] ref_views_images` = `ask` (default) |
  `allow`.
  - With `allow`, a sprint whose plan lists `ref_views` beside `images` starts without the 2D
    decision (event `ref_views_standing_ok`).
  - Its BUILD prompt limits the OK to reference views: icons, UI art, textures and decals still
    ask, and `media_gen_images = "deny"` still wins.
- **foundry-media adapter 1.1.0:** `purpose` and `view` arguments.
  - An edit's sidecar records its input images (project-relative path + SHA-256) and the view.
  - It is metadata only: the request to Foundry is unchanged.
- **To switch on:**
  - rebuild the router (the Settings control);
  - set `ref_views_images = allow`;
  - pull the OneDrive checkout (the adapter runs from it).
  - AU's copy of the skill updates through gap 7's installer.
- Tests:
  - `tests/coordinator/test_ref_views.py` (5);
  - `tests/test_media_adapter.py` (`test_edit_sidecar_records_inputs_purpose_and_view`);
  - the media, automation and rules suites: 94 pass.

## v0.170.2 — a decision never times out to "the owner did it"

2026-11-01 17:44. Studio on the workstation stopped syncing and only Victor's Connect click
could fix it. GF-RT3's Lead asked a user_action question whose recommended option read "I
clicked Connect in Studio; re-run the gate". The 3-minute timeout applied it as if Victor had
clicked, and GF-RT3 spent another attempt on the same stale sync.

- **`decision_policy.needs_human_act`:** also recognises an option written in the owner's voice
  ("I clicked / opened / signed in / connected / installed / did ..."). A timeout never applies
  such an option; the decision parks instead.
- **`decision_policy.meta`:** a `user_action` decision (only the owner can do it) may time out
  to PARK at most (its own stop option or PARK), never to any other option.
- Test: `tests/coordinator/test_audit_d_decisions_commits.py`
  (`test_an_owner_voice_option_and_any_user_action_option_only_ever_time_out_to_park`). 419
  decision, Lead, lifecycle and engine tests pass.

## v0.170.1 — `blender.pool` typed on one line still works; Settings refuse a bad pool

2026-11-01. The pool went live as one entry, `slot-1:2,Slot-2:22,local:1,`. The coordinator read
it as slot-1 with 1 job, which is slower than no pool.

- **Coordinator (`slot_blender.pool`):** each entry is split on commas and spaces, and slot ids
  match the configured slots without case (`Slot-2` is slot-2).
- **Settings (`schema.check_value`):** `blender.pool` must be `worker:jobs` with 1–8 jobs and no
  worker twice; otherwise Review & deploy shows the problem.
- Test: `tests/test_blender_pool.py`
  (`test_a_pool_typed_on_one_line_still_reads_and_settings_refuse_bad_jobs`). The pool, slot and
  automation tests pass (76).
- **Fix the deployed value:** one worker per line, `slot-1:2`, `slot-2:2`, `local:1`. The check
  takes effect after a router rebuild; the parsing after a coordinator restart.

## v0.170.0 — the Blender pool: slot-1, slot-2 and this PC, several jobs each

2026-11-01. docs/plans/BLENDER_POOL_PLAN.md P1–P4 (Victor: D-P1 to D-P7 as recommended):
"run multiple blenders in multiple containers / slots and even the main pc".

- **The pool (`blender.pool`, e.g. `slot-1:2`, `slot-2:2`, `local:1`):** every Blender run of a
  sprint goes through the drop-in to a free worker.
  - **Which worker (D-P4):** the one this worktree used last when it's free, else the pool's
    order (slots first, this PC always last), else it waits (`blender.pool_wait_seconds`, 600)
    and exits 75.
  - **Reservation:** a job is reserved on this PC the moment it is chosen
    (`~/.foundry-blender/locks/<worker>-<n>.lock`, held for the run), so runs started together
    spread instead of piling onto one slot.
  - **On a slot:** each job has its own lock, checkout (`~/work/blender-N`; job 1 keeps
    `~/work/blender`) and marker, and Blender gets the slot's CPU divided by its jobs. A slot
    takes the first free job lock itself, too. Jobs hold the library lock shared; the library
    copy holds it exclusive. Pushes of the same branch from runs started together retry.
  - **This PC (`local`):** the drop-in runs `blender.local_exe` in the worktree, with the same
    reservation, timings and build-replay entry.
- **Coordinator (D-P5):** the `blender` resource has one unit per pool job (`blender`,
  `blender#2`, …); a task keeps its unit and otherwise takes the first free one. The track
  planner proposes as many Blender tracks as there are jobs. With no pool, it's 1 as before.
- **Sprints (D-P3, P3):** with a pool, Kilo denies the plain `blender.exe` (the drop-in runs it).
  The prompt names the pool and tells a sprint with several models to start one guard `--bg`
  run per model, then `--wait` each.
- **Access → Machines (P4):** a "Blender pool" card: each worker's jobs, and its runs, Blender
  time and last run over 24 h, from the build replay.
- **Settings (UI fields):**
  - `blender.pool`
  - `blender.pool_wait_seconds`
  - `blender.local_exe`

  `blender.slot` still works when the pool is empty (D-P7: leave it empty).
- **P2, slot-2 ready:** the asset library was copied (30,904 files, 4,177 MB, about 8 min) and
  `build_env_cave --selftest` passed. `selftest_all --blender` through slot-2: every Blender
  tool passes except `build_bs_king_turret`, which fails the same way on this PC (AU's current
  KingCoonService has no turret hitbox: a game issue). `export_clip_json` failed once inside the
  full run and passed alone.
- **Proof on the machines** (a scratch AU clone, the worktree's code):
  - 6 Blender jobs of 20 s started together went 2 to slot-1, 2 to slot-2 and 1 to this PC; the
    sixth waited and took the first free worker. All done in 45 s, against 116 s before the
    reservation fix and 120 s one after another.
  - 5 real `build_bs_king.py` builds at once on all three machines: all PASS, 2,416 tris, the
    same dimensions and the same file size.
  - Every run had its build-replay entry with its worker and job.
- **Tests:** `tests/test_blender_pool.py` (14) plus the updated `test_slot_blender`.
- **Router rebuild needed** for the settings fields and the Machines card; the coordinator side
  takes effect on a coordinator restart.
## v0.169.2 — a Lead's own decision to wait gives the main checkout away

2026-11-01. MV2-CL1's work was green apart from AS-P8's unsettled test. Its Lead decided to wait
for AS-P8, which recorded a deferral ("lead: environment wait (...)"). From v0.160.2 a deferral
due within 15 minutes keeps the main checkout, so MV2-CL1 held it. When AS-P8's session handed
off at 15:21, AS-P8 queued behind MV2-CL1, and each sprint waited on the other.

- **`Coordinator._main_pause_is_automatic`:** a DEFERRED Lead case whose `deferred_reason` is the
  Lead's own wait (`lead: <class> wait (...)`) no longer keeps the main checkout, however soon
  it rechecks. An infrastructure backoff (Kilo admission, context wrap-up, quota) still keeps it
  as before.
- Test: `tests/coordinator/test_engine.py`
  (`test_a_sprint_with_a_working_lead_case_keeps_the_main_checkout`, extended). 272 engine and
  Lead tests pass.

## v0.169.1 — Settings: a saved draft no longer reports a page error as a deploy problem

2026-11-01. Victor switched on `blender.journal` and the deploy dialog said "Fix before
deploying: Cannot set properties of null (setting 'hidden')". It has been there since v0.159.0:
the slots table's settings row had an error slot only while it had an error. After each draft
save, the page paints the store's verdict on every row; on that row it threw, and the error
became the draft's "problem". The draft itself was saved, and reloading the page cleared the
false problem until the next edit.

- `automation.html`: the slots row always has its (hidden) error slot, and the painter skips a
  row without one.
- Test: `tests/test_automation_ui.py`
  (`test_every_settings_row_has_an_error_slot_so_a_saved_draft_never_reports_a_page_error`).
  The automation UI, view and prep-queue tests pass (30).
- **Router only:** it takes effect after a router rebuild. Until then, reload the Settings page
  before Review & deploy.

## v0.169.0 — build replay for Blender on this PC

2026-11-01. R3 of docs/plans/BLENDER_BUILD_REPLAY_PLAN.md. Sprints run Blender on this PC today,
not on a slot, so v0.168.0 alone recorded nothing for them. Still off until `blender.journal`
is on; with it on, `blender.journal_local` (on by default) adds these runs.

- **The transcript watcher (`build_journal.observe`):**
  - **When:** while a sprint session is busy, every `blender.journal_watch_seconds` (20), the
    engine reads its transcript, and once more when the session settles.
  - **Which calls:** each completed `bash` call that starts Blender on this PC becomes one journal
    entry (`local-<part id>`, once per call):
    - `& $b -b … -P …`, the guard's `-- "$b" -b …`, `& "…lender.exe" …`, `blender -b …`;
    - or a tool in `blender.journal_tools` (`selftest_all.py`).
  - **Skipped:** foundry-blender calls (they journal themselves), and commands that only mention
    Blender (git, plain Python, PowerShell searches: `-Path` is not `-P`).
- **The entry:**
  - the args after the executable, the script made relative to the worktree, with hash, copy
    and diff;
  - the exit, times and output lines;
  - the PNGs written during the call, found in the folders the arguments name and in
    `docs/evidence/<task>`.
- **AU's guard runner:** when the output names `LOG tmp/guard/cmd/….log`, the output, exit, start
  and end come from that log. A background run (`--bg`) is read once its log has `EXIT=`. A run
  read more than a minute late says so on the entry.
- **Settings (UI fields):**
  - `blender.journal_local` (on)
  - `blender.journal_watch_seconds` (20)
  - `blender.journal_tools` (`["selftest_all.py"]`)
- **Proof:** a read-only replay of a past session's transcript (AS-D11 implement #4, 730 parts)
  found its 34 Blender runs in 0.6 s: export_fbx 12 times, the sprint's own probe scripts, with
  exits and times. That track's guard logs had been cleaned up after the merge, and the entries
  say so.
- **Tests:** `tests/coordinator/test_build_journal.py` (37 tests: invocation forms, look-alikes, the
  transcript, renders by time, the guard log, the switch, the rate limit, the run on settling). 456 engine, AU, tracks,
  evidence, bridge, control-center, settings and slot tests pass.

## v0.168.0 — build replay: a journal of every Blender run, on the task page

2026-11-01. Gap 8 of reports/ASSET_PIPELINE_REVIEW_2026-11.md: R1 + R2 of
docs/plans/BLENDER_BUILD_REPLAY_PLAN.md (Victor: D-R1 to D-R6 as recommended). **Ships off:**
switch on `blender.journal` in Automation → Settings → Fast lane, gate & vision.

- **The journal (`foundry_coordinator/build_journal.py`):** one entry per Blender run:
  - where it ran: task, slot, host, commit, uncommitted files;
  - the arguments, and the builder script (`-P`) with its sha256, a stored copy and a diff
    from the previous run of the same script on the task;
  - timings (push, library, prepare, Blender, copy back), the exit code, the files back;
  - a 200-line log tail plus the lines that matter (`mesh_report`, `REF_IOU`, PASS/FAIL,
    ERROR, Saved);
  - up to 12 render thumbnails at 256 px, render views first (Pillow).
  - Files go under `state_dir/build_journal/<project>/<task>/<run>/`, plus a new `build_runs`
    table. The big outputs are never copied. A journal that can't be written is one log line;
    Blender's exit code is never changed.
- **foundry-blender (`slot_blender.py`):** with the journal on, the run's output is copied as
  it streams; the remote script marks Blender's end (`foundry-blender: blender exit N`). A busy
  slot (75) is an entry with a note and no renders. `prepare()` writes the task, run and journal
  settings into the drop-in's settings when the sprint's prompt is made.
- **Pruning (D-R3):** hourly, FINISHED tasks only. A task finished more than `journal_keep_days`
  (30) ago loses its journal; other finished tasks keep their newest `journal_max_runs` (200).
- **Bridge → router:** the bridge sends a `build_run` observation per run, with its thumbnails on
  the first send (at most 40 such runs per step, so a restarted bridge catches up in steps). The
  router stores them in `cc_build_runs` (router retention applies).
  - `GET /control/api/tasks/{p}/{t}/builds`
  - `GET /control/api/builds/{run}/thumb/{name}`
- **Task page: "Build replay"** (Inspector, after Lead recovery): step through the runs with ← / →
  (arrow keys work too). The chosen view angle stays selected, so you watch the model change.
  Each run shows its exit, where it ran, timings, the script and its hash (changed / first run /
  edited after the run), the result lines, the thumbnails with a big preview, and boxes for the
  script diff, the log tail and the files back.
- **Settings (UI fields):**
  - `blender.journal` (off)
  - `blender.journal_renders` (12)
  - `blender.journal_thumb_px` (256)
  - `blender.journal_keep_days` (30)
  - `blender.journal_max_runs` (200)
- **Scope:** only runs through foundry-blender (Blender on a slot) are journaled in this
  release. Blender on this PC comes with R3. The asset judge does not see the replay (D-R5).
- **Proof:** on slot-1, from a scratch AU clone, three runs went through foundry-blender:
  1. `build_bs_king.py --pose rest`: BUILD_BS_KING PASS, 2,416 tris;
  2. `render_views.py`: 8 views, 8 thumbnails;
  3. the same builder with a one-line edit: the diff against run 1.

  They showed on the task page of the control-center fixture in the in-app browser (stepping,
  angle selection, thumbnails, diff).
- **Tests:** `tests/coordinator/test_build_journal.py` (20) and
  `tests/test_build_replay_router.py` (10, including the bridge → router round trip).
- **Router rebuild needed:** the router side (table, endpoints, task page) takes effect when the
  router is rebuilt and restarted. The coordinator side takes effect on a coordinator restart.

## v0.167.2 — a sprint's scripted gate tests only its own files

2026-11-01. AS-P8 worked in the main checkout between MV2-CL1's sessions. MV2-CL1's gate takes
its `-Files` from every path changed in base..HEAD, so it picked up AS-P8's 150
`assets/exports` paths. MV2-CL1 stopped because it was asked to vouch for another sprint's
files. The Lead diagnosed this and proposed the fix.

- **`studio_calls._files`:** the changed paths are cut down to the task's own scope: its plan's
  file list, scope grants included (`adapter.describe(task).scope`), matched as exact path, glob
  or folder prefix. If none of the task's own files changed, the default pair is used as before.
  An adapter that cannot describe the task keeps the old behaviour (every changed file).
- Test: `tests/coordinator/test_studio_calls.py`
  (`test_gate_files_keep_only_the_tasks_own_scope_when_another_sprint_committed_in_between`).
  438 studio_calls, studio_gate, engine, lifecycle, tracks, AU and slot tests pass.

## v0.167.1 — designed-fail legs are not a "session shape" gap

2026-11-01. MV2-CL1 pinned S03's two legs that need a play session as designed FAILs (expectPass 5,
designedFails 2). GateRunner's tally then said `SUITE S03 ... VERDICT PASS`, but the runner's
check for each call still marked those two FAIL lines `UNKNOWN` ("needs a session shape").
The sprint saw the contradiction and stopped (lead case).

- **`studio_gate` regression calls:** when a bridge reply's FAIL lines all say the leg needs a
  session shape, and their count is within the suite's own `designedFails`, they are designed,
  not a gap. No `UNKNOWN` check is added. A reply with more such lines than designed is still
  `UNKNOWN`, and a real FAIL among them still keeps the row FAIL.
- Test: `tests/coordinator/test_studio_gate_calls.py`
  (`test_session_shape_fail_lines_the_suite_pins_as_designed_fails_are_not_a_gap`); it fails on
  v0.167.0. 121 Studio-gate and studio_calls tests pass.

## v0.167.0 — Asset judge: an independent, blind vision check for 3D model sprints

2026-11-01. Phase B of `reports/ASSET_PIPELINE_REVIEW_2026-11.md` (gap 4; Victor: "Yes,
recommended", then "Phase B (asset judge)").

**Why:**
- AU's 3D sprints were judged only by the builder itself (the "look gate").
- The coordinator's vision approval is a UI-screen check that never saw the concept. It also
  never ran on asset sprints: their plans list `docs/evidence/<ID>/**`, not named PNGs.
- An outside test of AI Blender builds found that a fresh judge works best. It writes its own
  checklist from the design, then scores the models under shuffled letters.

**What it does (`foundry_coordinator/asset_judge.py`):**
- **It runs** when `[approval] asset_judge` is on and an approval rule with `rubric = "asset"`
  applies to the task. A rule applies when:
  - its tag matches;
  - its `scope_any` globs name a file in the plan (a model sprint lists its `.blend` / `.fbx`);
  - a reference image is found for an asset id that `asset_from` takes from the plan scope.
- **When it applies,** it replaces the screenshot rule for that task. A rule with no reference
  does not apply, and the task's other rules decide, as before.
- **Call 1, checklist:** a fresh vision session gets only the reference images (per asset, the
  first `references` glob with a hit; aligned views before the concept sheet), the asset's
  `notes` (e.g. its `checklist.md`) and the sprint criteria. It writes its own 6-30 item
  checklist: parts, position, side (+X / −X under the house axes), colours, proportions,
  details. It is keyed by the references' bytes, so a rework pass reuses it (paid once).
- **Call 2, score:** another fresh session gets the checklist, the references and up to
  `max_frames` (default 12) model images. The images come in view order and are labelled with
  letters shuffled by a hash of the revision; file names are hidden. The judge scores every item
  pass / fail / unclear and gives a verdict for every image.
- **The coordinator computes the verdict itself:**
  - a failed item or a defect → CHANGES, with file paths mapped back from the letters;
  - too many unclear items → uncertain;
  - else approve. The approval note carries the score (e.g. `score 11/12`).
- **Same safety as the screenshot approval:**
  - it is bound to the revision and the image bytes, and a paid call is never repeated;
  - the serving model must be one of the persona's vision routes;
  - one structured retry of an inconclusive answer, then accepted with notes (W1.3);
  - CHANGES rounds follow `[fast_lane] max_vision_rounds`;
  - the event `asset_judge_review` records the letters, score, references and findings.
- **Tracks:** an asset rule also runs on a track sprint, because it judges committed renders.
- **One rule per task everywhere:** the engine, the control center's approval card, telemetry
  and the obligations now pick the rule that applies to the task (`asset_judge.rule_for`), not
  "the first rule of that kind".

**Settings (UI):**
- **Automation → Settings → Review:** `approval.asset_judge` (on/off, default off).
- **The rule** goes in `approval.rules`; its new fields are `rubric`, `references`, `notes`,
  `scope_any`, `asset_from` and `max_frames`, and the profile loader validates them.
- **AU's rule** is in `projects/animaluprising/project.example.toml`.

**Checked against the live AU repo** (read only):
- **Judged:** AS-E08 (BS-KING), AS-D19 (Baroness final-boss sheet), AS-H06, AS-E10, AS-J03,
  AS-G02 and AS-T06 apply, each with its asset ids and reference sheets.
- **Skipped:** AS-P8, AS-C23, AS-E11 and AS-BDG4 (no model file in the plan).
- **AS-J08:** its turret has no reference sheet, so the old rule decides.

**Not in this release:** grouping a second candidate (gap 9) and the AS-P9 overlays as judge
input. The judge reads the AS-P9 aligned views (`<ID>_views_v*.png`) automatically once they
exist.

Tests: test_asset_judge (new, 13); the visual approval, engine, control, obligations and
automation-schema suites pass unchanged.

## v0.166.2 — A slot rebuilds a half-set-up Wine prefix by itself

2026-11-01. Victor: "slot2 seems to be having issues", then "Yes, add the check".
- **What happened on slot-2:** its Wine prefix's first-time setup was cut off (the LXC restarted
  9 minutes before), leaving a 31 KB `system.reg` where a whole one is ~2.3 MB. Then:
  - no `C:\Program Files (x86)`, so the WebView2 install failed ("Failed to get a valid install
    target path. Exit code: 10") and Studio showed "Something went wrong";
  - no RpcSs service ("Failed to open RpcSs service"), so every Studio HTTP request failed within
    2 ms (`HttpError: Unknown/-1`) and Studio quit ("Trouble launching Studio").

  The Linux side, the packages (all 511 identical to slot-1's) and the clock were fine.
- **Fixed by hand:** the prefix was moved aside (`prefixes/studio.broken-20261101`), Vinegar built
  a whole one (2.35 MB), and Studio reached its sign-in window.
- **entrypoint.sh** now does the same at the container's start, before Vinegar runs: a prefix
  whose `system.reg` is under 512 KB is moved to `prefixes/studio.incomplete` (the newest one
  kept), and Vinegar builds it anew. A whole prefix, or none, is left alone; this was tried in the
  slot container on stub/whole/none copies.

  It takes effect with the slot image from the next installer run.
- Tests: test_slot_ssh (+1).

## v0.166.1 — a bracketed note in a plan's "Depends on" line is not a dependency

2026-11-01. AS-E10's plan reads "**Depends on:** AS-P6 (not landed: live items deferred to
AS-P6 / AS-E11, lane-B default) + AS-P4b". The id inside the brackets made AS-E11 a
dependency, and AS-E11 depends on AS-E10, so the approved sprint went straight to a paid Lead
case. GF-REPO1's "(GF-FX4 retired → UI-02)" had the same problem.

- **`animaluprising.adapter.deps_in_field`:** bracketed text is commentary, like the "; ..."
  clause already was. Across all AU plans this drops only notes and non-sprint labels (R8,
  LOOP1a/b, UseV2, D8/D9) plus three soft mentions of finished sprints.
- Test: `tests/coordinator/test_tracks_analysis.py`. 180 tracks and AU-adapter tests pass.

## v0.166.0 — The main sprints' Studio can live on a slot (Linux workstation plan W2)

2026-11-01. docs/plans/LINUX_WORKSTATION_PLAN.md W2 (D-L1: the main Studio in the slot image).
**Off by default: nothing changes until `studio.main_slot` is set.**

- **Setting** `studio.main_slot` (Automation → Settings): empty = this PC's Studio; a slot id =
  that slot is the MAIN project's Studio, the way a slot-bound track already uses one.
- **One routing point:** `slots.slot_for()` now returns the main slot for the main project. The
  scripted Studio gate (pipeline in the slot; the slot's lock instead of `studio:` and
  studio.lock), the Studio auto-heal repairs (`slots.resync`) and the Studio reopen all follow,
  unchanged. A track never takes the main slot: tracks share main's settings, and an unbound
  track has no Studio.
- **Main's Kilo config:** its Studio MCP servers follow the setting (`slots.sync_main_kilo`,
  `config_pull._sync_main_studio`).
  - When the setting is set, they point at the slot (`docker exec` over ssh), after their
    original blocks are saved in `<state>/kilo/<project>-studio-mcp.json`.
  - When it is cleared, those blocks are put back as they were.
  - Each change sends a `reload_kilo` request. It runs when `studio.*` or `slots.*` change and at
    start. With nothing set and nothing saved it does nothing, so today's setup is untouched.
- **Fast lane:** it steps aside while the main Studio is a slot's (`fast_lane.is_task`). It
  drives this PC's Studio; those tasks take the scripted gate in the slot instead. Tracks keep
  their own fast-lane setting.
- **Prompt:** main sprints get "MAIN SPRINT, STUDIO ON SLOT `<id>`". Studio MCP reaches the
  slot, the slot's Rojo serves committed code (commit before asking for the gate), and
  studio.lock and this PC's Studio are not used.
- **Limits:**
  - `[studio_calls]` must cover the tasks (AU's covers tiers A, B and C). The agent-run
    `gate.ps1` path would still use this PC's Studio.
  - The Foundry Client's Studio watchdog stays Windows-only; the slot container's own watchdog
    reopens Studio there.
- **Not run live yet:** setting it moves the live main sprints' Studio to slot-1. The first trial
  is Victor's call, at a quiet moment.
- Tests: test_slots (+4), test_rules_streamline (+1).

## v0.165.2 — game code with "secret" in its name is readable; secret files stay denied

2026-11-01 (Victor: "You can narrow the secret"). GF-SM1 (the Back-Fence Bazaar) stopped at once.
The hard Kilo deny `*secret*` hid its own `src/server/Services/SecretMarketService.lua` and
`src/shared/Config/SecretMarket.lua`.

- **`foundry_automation.role_rules.SECRET_NAME_GLOBS`:** the bare `*secret*` is replaced by the
  forms secret files take: `*secret*` with a data or key extension (`.txt .json .key .pem .yml
  .yaml .toml .ini .cfg .conf .env .kdbx`), a bare `*secret` name, `*.secret`, `*.secrets`, and
  any `secrets/` folder. `*_secrets*` (the key store `C:\Roblox\_secrets`), `*credential*` and
  the `.env` forms are unchanged. Source files such as `.lua`, `.luau` and `.py` are readable.
- The same list now feeds every place that had its own `*secret*`: the hard denies (read, edit,
  write, grep, glob, external_directory), the Lead's read and write denies (`lead.py`), the scope
  grants that are never allowed (`scope.NEVER_GRANT`), and the artifact snapshot deny list
  (`artifacts.DENY`).
- After deploying, regenerate the projects' `kilo.jsonc` (`kilo-config --apply`) so sessions get
  the new rules.
- Test: `tests/test_role_rules_w2.py`
  (`test_game_code_named_secret_is_readable_while_secret_files_stay_denied`). 370 role-rule,
  automation, Lead, scope, artifact and Kilo tests pass.

## v0.165.1 — A GPU slot gives Blender its cores (slot Blender plan B3)

2026-11-01. docs/plans/SLOT_BLENDER_PLAN.md B3 (D-B3: "all cores but 2 on a GPU slot").
- **The automatic CPU limit** (`cpu_limit` 0) now depends on the GPU:
  - while the GPU draws Studio, all cores but 2 (at least 4). Studio needs ~1.5 cores then
    (slot-1 on its UHD 630), so Blender through foundry-blender gets the rest. Its `-t` threads
    follow the container's limit;
  - in software drawing, a third of the cores as before (v0.162.1).

  A limit set in Settings is used as set.
- **Where it applies:**
  - `setup-host.sh` applies the rule after it finds the GPU;
  - `foundry-slot-keys` (every 5 minutes) passes whether the container uses the GPU into
    `effective_cpus`, so a slot that gains or loses its GPU follows by itself.
- The help texts on Access → Machines, the schema and INSTALL_SLOTS describe both cases.
- **slot-1:** the new rule arrives with its next installer run (New code), which installs the
  new `foundry-slot-keys`. With its 16 cores and the GPU it then gets 14.
- Tests: test_slot_ssh (B3 rules).

## v0.165.0 — The game's asset library on the slots: every AU Blender tool runs on a mini PC (B2b)

2026-11-01. docs/plans/SLOT_BLENDER_PLAN.md B2b (Victor: "Yes, start B2b").
- **Setting** `blender.library` (default `C:/Roblox/AssetLibrary`; empty = none): the library's
  folder on the main PC. `prepare()` writes it into each worktree's drop-in settings;
  `--library` / `--no-library` override it.
- **Copy:** `foundry-blender` keeps a copy of the library in the slot container's persistent home
  volume (`~/assetlib`), so no new mount and no reinstall are needed.
  - Before each run it compares a size and whole-second mtime manifest. When nothing changed it
    reads one hash file from the slot.
  - Otherwise, under the slot's Blender lock, it sends only new or changed files (a tar on
    stdin) and removes files gone here. The manifest is written last, so an interrupted copy is
    redone next time.
  - A failed copy fails the run (exit 3), never Blender on a stale library.
- **In the run:**
  - `AU_ASSET_LIBRARY` points at the copy (6 tools read it);
  - `C:/Roblox/AssetLibrary` and `C:\Roblox\AssetLibrary` are linked to it inside the checkout,
    for the 16 tools that hard-code the path. On Linux those are paths relative to the checkout.
    The links are made after `git clean` and before the copy-back marker, so they are never sent
    back.
- **Proof on slot-1** (a scratch clone, through the drop-in):
  - the first copy sent the whole library, 30,904 files and 4,177 MB, and `build_env_cave.py
    --selftest` passed (397 s, nearly all of it the copy);
  - then `selftest_all.py --blender <drop-in>` passed **50 of 50 Blender tools** (it was 44 of
    50 without the library), in 357 s, about 7 s a tool.

  The one failing entry is `roblox_cloud.py`, which runs on this PC and needs the Open Cloud key.
- **Still for AU** (a game sprint): every tool should read `AU_ASSET_LIBRARY` instead of the
  hard-coded path. The links cover it until then.
- Tests: test_slot_blender (+4).

## v0.164.0 — Sprints run Blender on a slot by themselves (slot Blender plan B2)

2026-11-01. docs/plans/SLOT_BLENDER_PLAN.md B2.
- **Setting** `blender.slot` (Automation → Settings): empty = this PC (the default, so nothing
  changes until it is set); a slot id = that slot. A track bound to a slot uses its own slot
  (D-B1).
- **Drop-in for `blender.exe`:** `slot_blender.prepare()` writes, per worktree,
  `~/.foundry-blender/<project>-<hash>.cmd` (`.sh` on Linux) and its settings.
  - It takes Blender's own arguments and runs `foundry_coordinator.slots blender --config …
    --repo <worktree> -- …`. Absolute paths inside the worktree become repo-relative; a path
    outside it is refused.
  - It is not under `.foundry-coordinator`: Kilo's bash rules deny any command naming that
    folder, and the role-rule tester showed the first draft blocked there.
- **Sprint prompt** (AU adapter `_blender_notice`): when Blender runs on a slot, the BUILD and
  repair prompts say to use the drop-in wherever docs/ENVIRONMENT.md says blender.exe. They give
  the guard-runner form and `selftest_all.py --blender "<drop-in>"`, the copy-back, and what
  exit 75 means. A slot that is missing or local gives a BLOCKED instruction instead.
- **Kilo rules:** while Blender runs on a slot, every role gets bash denies for `*blender.exe*`
  and `*Blender Foundation*`.
  - This covers the main kilo.jsonc (`kilo_config.regenerate`) and track worktrees
    (`track_kilo_config`).
  - The config pull now also rewrites kilo.jsonc when `blender.*` changes, not only
    `role_rules.*`.
- **Proof on slot-1:** AU's `selftest_all.py --blender <drop-in>` (a scratch clone) passed **44
  of 50** Blender tools there. All 6 failures read AU's asset library `C:\Roblox\AssetLibrary`,
  which the slot does not have yet: B2b, next. Until then, keep `blender.slot` empty for env and
  NPC builders.
- Tests: test_slot_blender (+5).

## v0.163.3 — a BUILD that keeps leaving the same plan steps open stops instead of looping

2026-11-01. GF-AFK1's plan left one checklist line for "the coordinator", a Studio negative
control that the coordinator never runs. Each 1-minute session re-ran the gate, and the
coordinator's evidence commit moved HEAD, so each session counted as partial-BUILD progress
and got a free next session. Seven sessions ran between 07:47 and 07:53.

- **Partial-BUILD continuation (`Coordinator._implementing`):** the open plan steps are compared
  with the previous continuation's. When the same steps come back `BUILD_STALL_LIMIT` (2) times
  in a row, the sprint stops BLOCKED (`build_stalled`, a lead case with the lead manager on)
  with an event `build_progress_stalled`, instead of taking another free session. A change in
  the open steps resets the count.
- Test: `tests/coordinator/test_lifecycle_engine.py`
  (`test_the_same_open_build_steps_three_sessions_in_a_row_stop_as_a_stall`). 477 lifecycle,
  engine, Lead, recovery, control and scenario tests pass.

## v0.163.2 — a sprint waiting on a soon-answered decision keeps the main checkout

2026-11-01. At 05:28 MV2-V1 blocked on a decision with a 3-minute default. GF-AFK1 took the main
checkout in those 3 minutes. When the default resumed MV2-V1, which was earlier in line,
GF-AFK1 sat mid-review behind MV2-V1's 195-minute regression.

- **`Coordinator._main_pause_is_automatic`:** a BLOCKED sprint whose `decision_wait` names an open
  decision with a deadline within `LEAD_SHORT_DEFER_SECONDS` (15 min) keeps its place, as a
  short Lead backoff already does (v0.160.2). A decision with no deadline or a far one still
  waits for a person, and the checkout goes to the next sprint.
- Test: `tests/coordinator/test_engine.py`
  (`test_a_sprint_waiting_on_a_decision_that_answers_itself_soon_keeps_the_main_checkout`). 323
  engine, Lead and decision tests pass.

## v0.163.1 — a deploy no longer aborts a running lead call

2026-11-01. The v0.163.0 deploy moved `foundry-deploy` while GF-RT3's lead diagnosis ran. The
guard counted it as the lead touching the router checkout, discarded its report and asked a
3-minute question whose default parked the sprint.

- **Deploy check (`lead.router_moved_by_deploy`):** a deploy was recognised only on a clean
  checkout, and the live checkout is never clean (untracked `project.toml` backups). A HEAD move
  now also counts as a deploy when the new HEAD carries a release tag (`vX.Y.Z`) and the
  checkout's dirty set is unchanged. An edit (dirty set changed) or an untagged commit is still
  a violation.
- The router snapshot (`LeadManager._git_state`) records `release_tag`, the `vX.Y.Z` tag at
  HEAD, if any.
- Tests: `tests/coordinator/test_revamp_lead.py` (deploy onto a tag with untracked backups; the
  snapshot names the tag on a real git repo). 241 lead + engine tests pass.

## v0.163.0 — foundry-blender: run Blender on a Studio slot (slot Blender plan B1)

2026-11-01. docs/plans/SLOT_BLENDER_PLAN.md (Victor: "do the container plan first"; D-B1 to
D-B5 approved). B1, the command; sprints do not use it yet (B2).

- **`python -m foundry_coordinator.slots blender [--host H --container C | --config
  .foundry/blender.json] -- <blender args>`** (`foundry_coordinator/slot_blender.py`), run from a
  sprint's worktree:
  1. It pushes the worktree's commit to the slot's game mirror as `blender/<worktree>`. The
     branch is kept, so later pushes send only what is new.
  2. One `docker exec` holds the slot's Blender lock (`flock`; a busy slot is waited for
     `--wait` s, default 600, then exit **75** with the reason). Inside it:
     - it checks the commit out in the slot's own `~/work/blender` (never Studio's checkout);
     - it unpacks the uncommitted and new files sent on stdin (deletions included);
     - it runs `blender -t <the slot's CPU limit> <args>`, with the output streamed live;
     - it sends back every file newer than a marker made at Blender's start (no `.git` or
       `__pycache__`), as a tar on stdout.
  3. The files land in the same paths in the worktree: repo-relative only, nothing outside it,
     capped at 4 GB back and 512 MB sent.

  The exit code is Blender's. Nothing ever falls back to a local Blender.
- **Proven on slot-1** (a scratch sparse clone of AU at 3dff8c63; the live checkout untouched):
  - `build_bs_king.py --selftest`: `BUILD_BS_KING SELFTEST PASS`;
  - `build_bs_king.py --phase P1 --save …` gave the same report as this PC
    (`2872 tris, dims [6.947, 6.62, 9.765]`) and a `.blend` of the same size (673,263 bytes);
  - an uncommitted new script ran there, and its two output files came back (2 s);
  - a second job with `--wait 2` exited 75 while the first held the slot;
  - threads followed the slot's 5-core limit.
- **Timing:** after the first push of the 2.5 GB history, a small build takes ~3 s through the
  slot (1 s locally). The overhead is the transfer, so heavy builds and renders gain.
- **Fixed during the proof:**
  - deleting the run's branch after each run left the mirror branchless, so every push re-sent
    the 2.5 GB history (52 s instead of 3 s);
  - not reading tar's trailing padding left ssh waiting forever.
- **Note:** AU's `--preview*` modes are for plain Python (Pillow), not Blender. They fail inside
  any Blender, this PC's included.
- Tests: test_slot_blender (6).

## v0.162.4 — Studio slots draw on the host's GPU

2026-11-01. Victor: "proxmox has an onboard intel IGPU, can that pass through to the container…
would that help?", then "Yes, build and trial".

- **Trial on slot-1** (Proxmox → the LXC → Resources → Device Passthrough
  `/dev/dri/renderD128`, mode 0666; then /dev/dri into the container):
  - Vulkan sees an Intel UHD Graphics 630 (CML GT2), and Studio holds 5 handles to the GPU;
  - the container went from ~1300% CPU (software, ~500% at its 5-core cap) to **~145%**;
  - a screenshot showed Studio's viewport drawn correctly.
- **Slot setting** `gpu` (default on): Access → Machines → Settings → **Use the GPU**, which also
  shows what the host reports. The Machines list shows "GPU in use", "GPU present, not used" or
  "software drawing".
- **Installer:**
  - When the LXC has `/dev/dri/renderD128` that uid 1000 can open, and the setting is on, it
    adds `docker-compose.gpu.yml` (devices /dev/dri) through `COMPOSE_FILE` in slot.env.
  - A device without mode 0666 gets a warning that says what to set.
  - It removes the trial's `docker-compose.override.yml` (marked `# TRIAL`).
- **foundry-slot-keys** reports the GPU every 5 minutes (present, used by the container, and the
  name from `vulkaninfo`); the router keeps it in `machines.host_info`.
- Docs: INSTALL_SLOTS (a GPU row), LINUX_WORKSTATION_PLAN D-L3 (Blender: the viewport and Eevee
  gain; Cycles does not, on a UHD 630).
- Tests: test_slot_ssh (+2).
- **slot-1:** it runs the trial setup now. Its next installer run (New code) moves it to the
  proper GPU file.

## v0.162.3 — A slot's desktop starts again after its host reboots

2026-11-01. Victor: "I restarted but VNC didn't come on". On slot-1, the LXC restart stopped the
container hard. Docker restarted the same container, whose /tmp still held display :1's lock,
so Xvnc refused to start ("remove /tmp/.X1-lock"): no desktop, so no Studio, while noVNC's page
still loaded.
- entrypoint.sh removes `/tmp/.X1-lock` and `/tmp/.X11-unix/X1` before starting Xvnc. It is the
  container's start, so no Xvnc runs yet.
- **Fixed by hand on slot-1** (the lock removed, the container restarted). The fix is in the
  image from its next installer run.
- Tests: test_slot_ssh (+1).

## v0.162.2 — Linux workstation W1 (portable paths), and the slot installer always matches the router

2026-11-01. W1 of docs/plans/LINUX_WORKSTATION_PLAN.md (approved: "Yes, recommended").
- **The audit found less Windows-only code than planned:**
  - process trees are killed with taskkill on Windows and process groups elsewhere;
  - the client already has Linux config, keyring and systemd paths;
  - the browser search already falls back to Linux names;
  - the game's PowerShell gate scripts run under PowerShell 7 for Linux, which the slot image
    already installs.
- **Fixed:**
  - `mcp_stdio`: the community Studio MCP server runs `npx` from PATH. Windows still wraps
    `npx.cmd` in `cmd.exe`, now found on PATH before `C:\Program Files\nodejs`.
  - `mcp_stdio`: the official server, Studio's own `mcp.bat`, exists only on Windows. Elsewhere
    it now says so ("no Roblox Studio on this machine") instead of starting `cmd.exe`; W2 gives
    Linux its Studio container.
  - The CLI's version guard no longer names the hard-coded `C:\Users\shock\foundry-deploy`. The
    coordinator's heartbeat records its own code folder (`root`), and the guard says "run it
    from <that folder>".
- **CI:** `.github/workflows/tests-linux.yml` runs the suite on Ubuntu 24.04, weekly and on
  demand (Actions → Tests on Linux). The first run is still to be seen.
- **Slot installer** (setup-host.sh): before asking for the code, it fetches the router's own
  release of itself and runs that when it differs.
  - On 2026-11-01 slot-1's reinstall ran an older copy left on the LXC. It wrote an old slot.env,
    so VNC stayed on localhost (NPM 502), with no key timer and 16 CPUs.
- Tests: test_mcp_stdio (+1, the Windows asserts only on Windows), test_selfheal (the guard
  names the heartbeat's folder), test_slot_ssh (+1).

## v0.162.1 — A slot's CPU limit: an idle Studio no longer takes every core

2026-11-01. Victor: "why is the container using 80% CPU when nothing is going on?"
- **Cause:** on slot-1 (16 cores, no GPU), Studio itself used ~1300%. Its 3D view is drawn by
  Mesa's software renderers (`libgallium` llvmpipe and lavapipe, seen in the process), which
  redraw continuously on every core, even when idle. The installer had given the container all
  16 cores.
- **Slot settings** `cpu_limit` (0 = a third of the host's cores, at least 4) and `desktop_size`
  (empty = 2560x1440), in the schema and validated.
  - The per-slot **Settings** button on Access → Machines (was **VNC**) holds VNC, CPU limit and
    desktop size: `POST /admin/api/machines/{name}/host`, with `/vnc` kept.
- **Slot host:**
  - setup-host.sh writes `SLOT_CPUS`, `SLOT_RENDER_THREADS` (same number) and `SLOT_GEOMETRY`;
  - the compose file passes `LP_NUM_THREADS` and `GEOMETRY` from them;
  - foundry-slot-keys (every 5 minutes) gets the limit with its keys
    (`/v1/foundry/secrets/authorized-keys` → `host`, nothing secret). It applies a change live
    with `docker update --cpus` and records it in slot.env, so the renderer's threads follow at
    the next start.
- **Right now, by hand** (until slot-1 runs this installer):
  `docker update --cpus 4 studio-slot` in the LXC.
- Tests: test_slot_ssh (+2).

## v0.162.0 — Slot ssh without pasting keys: the main PC registers itself, slots report their host key

2026-11-01. Victor: "can there be a place in Foundry-Router to add the SSH key, so it does the
setup for the client side on the MAIN PC, whichever the main implementation PC is going to be";
approved with the recommended choices (A: the coordinator does it at start; B: Foundry's own
key; C: host keys reported and checked strictly, replacing plan D3's trust on first use; D:
existing slots pick it up at a reinstall). It also fixes what broke on 2026-11-01: a slot reusing
192.168.0.115 failed on a stale host key in ~/.ssh/known_hosts.

- **router** (`machines.py`):
  - `POST /v1/foundry/control/workstation-ssh` {name, public_key} (orchestration key)
    registers or refreshes the workstation's key. A new or changed key is an event plus an info
    alert. It answers with every slot's address and reported host keys.
  - `GET /v1/foundry/secrets/authorized-keys` and `POST /v1/foundry/secrets/host-keys` are for
    the enrolled slot's own key only.
  - `DELETE /admin/api/machines/workstations/{name}`.
  - Enrollment hands out the registered keys plus the extra keys.
  - New table `workstation_keys`, and a `machines.host_keys` column.
- **coordinator** (`slot_ssh.py`): at start and every 10 minutes it
  - makes `<state_dir>/ssh/id_ed25519` if missing;
  - registers it as this PC's hostname;
  - writes the slots' host keys into `<state_dir>/ssh/known_hosts`;
  - records `state.json` (no secrets).

  `slots.ssh_cmd` (docker exec, Kilo's Studio MCP, the pipeline runner) and the mirror pushes
  (now `GIT_SSH_COMMAND`, the same ssh) use Foundry's key first, then the personal
  `~/.ssh/id_*`, because `-i` turns ssh's defaults off. A slot whose host key the router knows
  is checked strictly against Foundry's known_hosts; others keep the old trust on first use.
  A custom slot `ssh` setting is used as it is.
- **slot host:** `contrib/studio-slot/proxmox/slot-keys.py`, installed as
  `/usr/local/sbin/foundry-slot-keys` plus `foundry-slot-keys.timer` (every 5 minutes).
  - It reports `/etc/ssh/ssh_host_*_key.pub` and rewrites a Foundry block in
    `/home/studio/.ssh/authorized_keys`; lines outside the block stay.
  - setup-host.sh now downloads Foundry (3/6) before the ssh step (4/6).
- **UI** (Access → Machines):
  - a **Workstations** card (PC, key fingerprint, registered and last seen, Remove);
  - each slot's host key fingerprint, or "host key not reported";
  - "Workstation ssh keys" became "Extra ssh keys (optional)".

  The wizard's Machines step counts registered PCs. `foundry_client status` has a "Slot ssh"
  line.
- **Checked live** against slot-1 (read-only):
  - Foundry's key plus the strict check against slot-1's verified host key ran ssh,
    `docker exec` and git `ls-remote` OK through the personal-key fallback;
  - a wrong host key was refused.
- Tests: test_slot_ssh (11).
- **To apply:**
  - rebuild the router, then deploy the coordinator, which registers this PC at its next start;
  - for slot-1, a reinstall (New code) installs the timer and reports its host key. Until then it
    keeps working with the personal key and the known_hosts entry from 2026-11-01.

## v0.161.2 — A slot hides Studio's leftover splash window

2026-11-01. On slot-1, Studio's splash (a 600x300 window titled "RobloxStudio") stayed on top of the
loaded editor under Wine. Victor: "logged in, but the studio screen still sits on top".
- entrypoint.sh's 5-second window loop (the one that makes the editor full-screen) now hides any
  visible "RobloxStudio" window once the slot's editor window exists.
  - It is unmapped, not closed: closing a window of Studio's process could close Studio.
  - This covers a start, a watchdog reopen and `studio-reopen`.
- Applies with the slot image, through the installer again (New code). Until then:
  `docker exec studio-slot bash -c 'DISPLAY=:1 xdotool windowunmap <window id>'`.
- Tests: test_machines slot files check.

## v0.161.1 — A Studio slot's desktop opens from the LAN, with an optional VNC password

2026-11-01. Victor: "the install should open VNC port so it can be accessed from the LAN, not just
local host"; "I want NPM to automatically goto /vnc.html?autoconnect=1&resize=scale".

- **Slot settings:** `vnc_lan` (default on) and `vnc_port` (default 6080), in the settings schema
  (`foundry_automation/schema.py` SLOT_FIELDS; the port is checked to be 1-65535).
  - They are on Automation → Settings → Studio slots, the Machines **Add a Studio slot** form,
    and a new **VNC** button per slot (`POST /admin/api/machines/{name}/vnc`).
  - The Machines list has a VNC column with a link to the desktop, plus "password on" or "no
    password".
- **Installer:** setup-host.sh writes `SLOT_NOVNC_BIND` (0.0.0.0, or 127.0.0.1 when off) and the
  port into slot.env, and prints `http://<slot IP>:<port>/` instead of the ssh tunnel. The
  enrollment answer now has `vnc` {lan, port, password, url}.
  - proxmox/docker-compose.yml publishes `${SLOT_NOVNC_BIND:-127.0.0.1}`, so a slot.env from
    before stays on localhost until its installer runs again.
- **VNC password, optional (none by default; Victor: "I want it optional"):** the
  `SLOT_VNC_PASSWORD` secret (default file `slot_vnc_password.txt`, in the game's group) is
  pulled like any secret.
  - entrypoint.sh turns it into TigerVNC's password file on the tmpfs (`vncpasswd -f` from a
    file, never a command line or a log) and starts Xvnc with VncAuth. Without it, Xvnc stays
    `-SecurityTypes None`.
  - It applies when the container restarts. VNC uses its first 8 characters.
- **Start page:** the image's `/usr/share/novnc/index.html` goes to
  `vnc.html?autoconnect=1&resize=scale`, so the slot address, or a proxy such as Nginx Proxy
  Manager (Websockets on), opens the desktop with nothing else configured.
- Docs: docs/INSTALL_SLOTS.md "The slot's desktop (VNC)".
- Tests: test_machines (+5: the default on the LAN, the add form and VNC button, the password only
  when readable, the schema port check, the slot files).
- **To apply:**
  - rebuild the router and deploy the coordinator, which knows the new slot fields;
  - then for slot-1, **New code** and `bash setup-host.sh` in the LXC. The image is rebuilt from
    its last layers only.

## v0.161.0 — Router installer, first-run setup wizard, backup and restore

2026-11-01. Phase 2 of the fresh-install plan (docs/plans/FRESH_INSTALL_PLAN.md; Victor: "Yes,
start phase 2"). Guide: docs/INSTALL_ROUTER.md.

- **install/router.sh** (new): one command on a Linux machine.
  - It asks for the folder (`/opt/foundry-router`), the port, the HTTPS listener and a backup to
    restore.
  - It installs Docker and git if missing, downloads the newest release (or `--version`), builds,
    starts and prints the address.
  - `--upgrade` moves an installed router to a newer release.
  - Config and data live outside the checkout. docker-compose.yml takes `FOUNDRY_CONFIG_DIR` /
    `FOUNDRY_DATA_DIR_HOST`, and the defaults are unchanged, so existing installs are untouched.
- **router — first-run setup wizard** (`/ui/setup`, `ui/setup_wizard.py`): restore or start fresh,
  protect the web UI, secret store passphrase, backends (example servers removed in one click,
  templates for Meridian / CLIProxyAPI / llama.cpp / Ollama / OpenAI-compatible, tested when
  added), personas (install + validate with progress), budgets, game, machines, finish.
  - Every step calls the same admin API as its tab.
  - A config.yaml made from the example gets `setup.completed: false`, and the full UI opens the
    wizard while it is false. Existing configs have no `setup` section and count as set up.
  - The header has a **Setup** button.
- **router — provider keys** (`provider_keys.py`): `${NAME}` in config.yaml falls back to
  `config/provider-keys.env` (mode 600) when the environment has no value.
  - The wizard and Access → Secrets → Provider keys write it. Values are never returned, are
    scrubbed from diagnostics, and backends pick up a change at once.
  - A value in the host `.env` still wins, so the live router behaves exactly as before (fresh-install
    plan D4a: model keys stay out of the secret store, so a restart never stops model calls).
- **router — backup and restore** (`backup.py`, Logs → Data → Backup & restore).
  - **What a backup holds:** one `.fbak` file with consistent online copies of every SQLite DB
    (main DB, secret store), `tls/`, config.yaml and provider-keys.env. Media and training files
    are included on request.
  - **Encryption:** AES-256-GCM in 1 MiB chunks, with a key derived by scrypt from a backup
    passphrase typed at backup time and never stored. The header, the chunk order and the last
    chunk are all authenticated. A wrong passphrase is told apart from a damaged file.
  - **Download:** a one-time link, valid 10 min.
  - **Restore (API):** upload, check every file against the manifest's sha256 plus a SQLite
    quick_check, stage under `data/restore/pending`, then apply at the next start before anything
    opens. It is all or nothing: a failure puts the current files back and marks the restore
    FAILED. The replaced files are kept in `data/restore/previous`.
  - **Version check:** a backup from a newer router is refused.
  - **CLI:** `python -m foundry_router.backup info|create|restore FILE` (passphrase on stdin),
    which `router.sh --restore` uses.
- Tests: test_backup (8), test_provider_keys (4), router suite 1973 passed.
- Installer dry runs:
  - Debian 13: the apt step installs Docker, compose v2 and git.
  - Docker-in-Docker: a fresh install opens the wizard, and `--upgrade` works.
  - `--restore`: a wrong passphrase is refused; the right one brings router 2 back with router 1's
    state.
  - Fixed on the way: a real terminal check (`curl | bash` and no-terminal runs), the
    `--restore` passphrase from stdin when there is no terminal, `hostname -I` missing, and a
    re-fetched tag on upgrade.
- Router rebuild needed. Nothing changes on the live router until you use the new pages.

## v0.160.2 — a lead case's short retry wait keeps the main checkout; the lead's wait is shown

2026-10-31 21:05-21:45: MV2-V1's lead session died on infrastructure, so its case was DEFERRED for the
5-minute backoff. In that window MV2-V1 no longer counted as the main sprint in flight, GF-RT3 started
a session and took the workspace lock, and when the case was DIAGNOSING again (21:24) its lead call
waited on GF-RT3's lock with nothing saying so (the case would have escalated at its deadline).

- **engine `_main_pause_is_automatic`:** a BLOCKED sprint whose lead case is DEFERRED with its retry
  due within 15 minutes (`LEAD_SHORT_DEFER_SECONDS`) keeps its place in the main checkout, like
  DIAGNOSING / REPAIRING. A long deferral (a quota reset, up to an hour) or a paused case still lets
  the next sprint in.
- **lead `_note_wait`:** a lead or repair call that cannot start (`pending`, e.g. "workspace lock held
  by game/GF-RT3") is recorded once per reason on the case (`validation.lead_waiting`) with one
  `lead_waiting` event, shown in the task's blocker (`[lead waits: ...]`), and cleared once it starts.
- Tests: test_engine (short vs long deferral, the blocker text), test_infra_outages (a lead waiting
  on the lock says so once and starts when it frees); engine + lead suites 227 passed.

## v0.160.1 — New Kilo tasks no longer wait 60 s for Strata

2026-10-31. Victor's report: every new Kilo task waited about 60 s before Strata started
reading it (33 requests in 14 h, about 26 min). Follow-up turns were not affected.

- **Cause:** Strata has `helpers_allowed: 0` (one session at a time). A session counted as
  active until 60 s after its last call, even when that call was a final answer. A new task is a
  new session, so it waited in `reserve_wait` until the finished task had been quiet for 60 s.
  The gap was a flat 60.1-60.5 s whatever the prompt size.
- **router — a final answer frees the stream at once:** each ledger row records whether the call
  ended in a final answer (`usage_events.final`). A session whose latest call was a final answer,
  with no call in flight, holds no stream. A reply with no tool calls, or only
  `attempt_completion`, counts as final. A session whose last call was a tool call (coming back
  with the result, or waiting on its own sub-agent) keeps the hold as before.
- **router — `session_hold_seconds` per backend** (default 60): how long that hold lasts. It is
  in Setup → Helpers & limits next to helpers allowed, and in the backend JSON; 0 turns the hold
  off.
- **router — timing:** `sent_at` was stamped before Foundry's wait for a stream or queue slot, so
  the perf CSV booked that wait as server wait (`wait_ms` = ttft - prefill). The pool now
  measures the slot wait. `sent_at`, ttft and wall start at the real send, the wait is added to
  `router_ms`, and it is recorded as a new `queue_ms` column (perf samples and the CSV).
- Router rebuild needed (all changes are router-side).

## v0.160.0 — Machines and enrollment codes: a Studio slot in one code and one command

2026-10-31 (Victor: "easy to use, one shot, ask for any config it needs"; the fresh-install plan
docs/plans/FRESH_INSTALL_PLAN.md, phase 1 with the recommended answers). Guide:
docs/INSTALL_SLOTS.md.

- **router — Access → Machines:** add a Studio slot (game, slot id, IP, its place link). It:
  - writes `slots.<id>` into the project's settings as a new deployed revision, and into an open
    draft, so a later deploy keeps it (`AutomationStore.put_slot`);
  - makes a one-time **enrollment code** (`FNDR-XXXX-XXXX-XXXX`, 60 bits; only its hash is stored;
    single use; 30 min, `machines.code_minutes`);
  - shows the exact install commands.

  The slot starts **off**. Revoke, New code and Delete are on the page, and so is the
  **Workstation ssh keys** list (`machines.slot_ssh_keys`, public keys only), with an event log.
- **router — `POST /v1/foundry/enroll {code}`** (no key; the code is the credential): returns the
  slot's client key, made at that moment and stored hashed (`secrets` scope, that project and slot
  only). It also returns the slot settings, the router version, the workstation ssh keys and the
  HTTPS fingerprint.
  - Wrong codes are rate-limited per address (10 in 10 min).
  - Every enrollment is audited and raises an alert.
  - `secrets.allow_http` applies.
- **router — `POST /v1/foundry/secrets/ready`** (the slot's own key): switches the slot on once
  its container runs.
- **router — bundle:** a store that never had a passphrase now returns the slot settings with no
  secrets (a locked store still answers 503).
- **coordinator:** a remote slot's `au_push_url`, `foundry_push_url` and `foundry_host_dir` default
  to the installer's layout under `/srv/studio-slot` when empty. The default slot ssh accepts a new
  host key on first contact (`StrictHostKeyChecking=accept-new`, Victor D3) and gives up after 15 s
  (`ConnectTimeout`).
- **contrib/studio-slot:**
  - `proxmox/setup-host.sh` is now the **one-shot installer**. It asks for the router address and
    the code, then installs Docker, enrolls, saves the key (mode 600, never shown), authorizes the
    workstation, clones Foundry at the router's version, writes the mirrors and `slot.env`
    (`.env`), builds and starts the container, waits for its first secrets pull and reports ready.
    `--no-build` and `--code-stdin` cover tests and scripting. The enrollment answer is removed
    whatever happens.
  - New `proxmox/create-slot-lxc.sh` (Proxmox host) asks for id, IP, sizes, the router and the
    code. It downloads the Debian 13 template, creates the LXC (unprivileged, nesting/keyctl,
    onboot, optional iGPU) and runs the installer inside it.
  - `slot-checkout` clones the game itself on a new host's first gate and installs its toolchain.
    Rojo waits for the project file.
  - The Proxmox compose file no longer requires `STUDIO_OPEN` and passes `ROJO_NEVER_PLACE_IDS`.
- **Verified:**
  - `tests/test_machines.py` 6/6 and the derived-defaults test in `test_slots.py`;
  - 194 related tests green;
  - a **dry run of the real installer** in a clean `debian:trixie` container against a throwaway
    router: enrolled, key file 600/studio, workstation key authorized, Foundry v0.159.1 cloned and
    its mirror set, `slot.env` with the place, nothing left behind. It caught two bugs, now fixed:
    a shallow clone can't be pushed to the mirror, and a failure left the enrollment answer on
    disk;
  - `slot-checkout`'s first clone, run in the built image against a mirror holding only a track
    branch.

  Not run: a real Proxmox host (`create-slot-lxc.sh`) and a real build in an LXC. That is the
  slot-1 move.
- **Rollout:** rebuild the router on .112; restart the coordinator (the slot defaults). Then on
  Access → Machines, paste this PC's public ssh key and add slot-1 (see INSTALL_SLOTS.md, "Moving
  a slot").

## v0.159.1 — Proxmox slot readiness: setup gaps closed before the first LXC

2026-10-31 (Victor: move slot-1 to Proxmox; "make sure EVERYTHING for the container build is
ready"). Docs and setup text only; no code change.

- **Workstation ssh key:** this PC had none, and Foundry's ssh to a slot host is key-only. The
  README now has a step 0: `ssh-keygen -t ed25519`, no passphrase so the unattended coordinator can
  use it.
- **First connection:** Foundry's ssh uses BatchMode, which refuses an unknown host key. The first
  `ssh` now uses `StrictHostKeyChecking=accept-new` (setup-host.sh next steps, README).
- **Getting setup-host.sh onto a new LXC:** fetched from the release tag on GitHub
  (raw.githubusercontent.com), in the script header and the README.
- **lxc.conf.example:**
  - disk 80 GB or more;
  - Docker overlay2 storage: LVM-thin or a directory, or ZFS with Proxmox VE 8.1+;
  - a static LAN IP that can reach the router.
- **Checked:**
  - every pinned download in the Dockerfile answers 200;
  - the v0.159.0 image builds, `slot-secrets` runs as `studio`;
  - the Proxmox compose file validates with a sample `.env`;
  - the tmpfs `/run/secrets` mount is 700, owned by `studio`, and detected as memory-only;
  - all slot scripts pass `bash -n`.

## v0.159.0 — central secret store: slots pull their keys and settings from the router

2026-10-31 (Victor: design agreed; passphrase master key, HTTP and HTTPS both your choice, the admin
key optional, new containers pull by default, no automatic migration). docs/SECRETS_STORE.md.

- **router — Access → Secrets:** a write-only store of keys and passwords per game (project) plus
  `shared`.
  - **At rest:** AES-256-GCM in its own file `<data>/secrets.sqlite`, never `config.yaml`, its
    history or export bundles.
  - **Master key:** derived from a passphrase (scrypt) and never stored. After every router restart
    the store is locked until you unlock it on the page. While it is locked, pulls answer 503 and
    raise a "Secret store is locked" alert (user-action class).
  - **On the page:** set or rotate (typed or from a file), clear, delete, change the passphrase.
    It shows set or not set, last set, last pulled (who and from where), and which keys can read
    each secret.
  - **Pull log:** every pull is logged, values never.
  - **Scrubbing:** stored values are removed from diagnostic text while the store is unlocked.
- **router — `secrets` client scope:** preset **slot** on Access → Clients & Keys. A key reads
  `shared` plus its `projects` (none = `shared` only), narrowed by the new `slots` and
  `secret_names` fields. Projects, slots and names are now editable on the Add and Edit forms.
  - Creating a key with this scope, or giving a key the scope, raises an alert, and so does a
    key's first pull (`secrets.alert_on_new_access`).
- **router — pull API:** `GET /v1/foundry/secrets/bundle?project=&slot=` returns the values plus
  the slot's settings from the deployed revision. ETag/304 makes the per-gate re-pull cheap, and
  every answer sends `no-store`.
- **router — transport (Access → Secrets → Transport):**
  - an optional HTTPS listener (`secrets.https_enabled`, port 11436) with a self-signed certificate
    whose fingerprint clients pin;
  - `secrets.allow_http` off = secret values travel only over HTTPS.
  - The router's compose file now publishes 11436. Changing either setting needs a router restart.
- **settings — Studio slots:** `[slots.<id>]` is part of the project settings schema, with its own
  editor on Automation → Settings and validation:
  - a container is required;
  - two slots can't share a container;
  - a remote slot needs `au_push_url`.

  New fields: `place_id`, `universe_id`, `rojo_never_place_ids`, `secrets`. The coordinator's
  existing config pull delivers deployed slots. A `[slots]` table in `project.toml` keeps working.
- **contrib/studio-slot (image rebuild):** new `slot-secrets` helper.
  - **When:** at container start, and from `slot-checkout` before every gate run.
  - **Where:** the slot's bundle goes into `/run/secrets` (a tmpfs, mode 600 files; it refuses a
    disk folder). The slot settings go into `~/.config/foundry-slot/slot.env`, which `entrypoint.sh`
    and `studio-reopen` read over the compose values.
  - **Failures:** router away or store locked keeps the last files; a revoked key deletes them; a
    secret no longer served is removed.
  - **Compose:** both files drop the Open Cloud key mount for `FOUNDRY_URL`/`FOUNDRY_SLOT` plus the
    slot's own key file (`./secrets/slot-key.txt`, git-ignored; on Proxmox
    `/srv/studio-slot/slot-key/`).
  - AU is unchanged (`AU_ROBLOX_KEY_FILE`).
- **New dependency:** `cryptography==49.0.0` (router image rebuild).
- **To take it into use** (docs/SECRETS_STORE.md "Moving the Open Cloud key"):
  1. Rebuild the router on .112.
  2. Set the passphrase on Access → Secrets and load the key with the file picker.
  3. Make the `slot-1` key and put it in `contrib/studio-slot/secrets/slot-key.txt`.
  4. Recreate the slot.

  Until then the running slot keeps its current mount.
- **Verified:**
  - `tests/test_secret_store.py` 15/15 and `tests/test_slot_secrets.py` 6/6;
  - 185 related router and coordinator tests green;
  - a live run inside `studio-slot-poc` against a throwaway router with a test value:
    - HTTP pull, then 304;
    - wrong HTTPS fingerprint refused before the request;
    - pinned HTTPS works;
    - HTTPS-only router refuses HTTP;
    - locked store keeps the files;
    - a rotated value arrives (hash matched);
    - a disabled key deletes the files;
    - no test value in the router log.

## v0.158.20 — Studio slots on another machine (Proxmox LXC) over ssh

2026-10-31 (Victor: "start it"): a slot no longer has to be a container on the workstation.

- **slots:** a `[slots.<id>]` with `host = "user@machine"` runs every container command as
  `ssh <host> docker exec ...` (`ssh` setting, default `ssh -o BatchMode=yes`; the remote side gets
  one shell-quoted command). That covers the gate run, copy-back, resync, stop, the slot
  stop-playtest, and the track's Kilo Studio MCP servers (stdio over ssh).
- **sources:** before a remote gate run, `au_push_url` gets the track's commit as
  `refs/heads/slot/<task>` (the slot's `origin` is that bare mirror, so `slot-checkout` fetches it),
  and `foundry_push_url` gets the deployed Foundry's HEAD as `deploy`, which `foundry_host_dir`
  (mounted at `/opt/foundry` in the slot) then checks out. A failed push stops the run before
  checkout (exit 3). A local slot (no `host`) is unchanged.
- contrib/studio-slot `proxmox/` (compose with no Windows mounts, `setup-host.sh`, LXC settings) and
  `entrypoint.sh` cloning from a bare mirror.
- Verified on this PC: a real AS_P7 gate in remote mode through an ssh stand-in (push + host Foundry
  checkout, slot-checkout, pipeline, 21 files copied back); `tests/coordinator/test_slots.py` 13/13.

## v0.158.19 — "need exactly one Studio, found 0" reopens Studio (in the slot for a slot track)

2026-10-31 11:50: the slot's edit Studio crashed during AS-J05's 2-player playtest; the gate said
"McpError: need exactly one Studio, found 0 (open: none)" (OfficialStudio.resolve), which no autoheal
pattern matched, so it became a Lead case.

- **autoheal `studio_reload`:** also matches "exactly one Studio..., found 0" and "(open: none)". For
  a slot-bound track the repair runs in the slot (v0.158.18).
- contrib/studio-slot (image rebuild): the watchdog reopens Studio when the EDIT Studio is gone, even
  while a crashed playtest's server/client processes and the WebView2 helpers are still alive.

## v0.158.18 — auto-heal repairs a slot track's Studio in its slot, never the workstation's

2026-10-31 11:00 / 11:04: AS-J05 (track slot1-props on slot-1) hit a stale Studio <-> Rojo sync
in the container (after the container was recreated). Auto-heal treated it like a main stop and
reopened the WORKSTATION's TEST Studio twice: the main sprint's Studio closed, the slot stayed stale.

- **autoheal `repair`:** for a slot-bound track, every Studio/Rojo signature (and verify) runs
  `slots.resync`: the slot's `rojo` binary is restarted by its supervisor and `studio-reopen --wait`
  reopens the slot's place (the plugin auto-reconnects). The workstation path never runs.
- **autoheal `request_studio_reopen`:** refuses a slot-bound track's task outright.
- **slots `resync`:** `pkill -x rojo` (the binary only; a `-f 'rojo serve'` match also hit the shell).
- Live check on slot-1: resync -> "studio-reopen: Studio is back", new Rojo pid, `studio_gate doctor`
  DOCTOR READY.

## v0.158.17 — a lead on a slot-bound track leaves the workstation's studio.lock alone

2026-10-31 10:19: the lead of AS-J05 (track slot1-props on slot-1) followed the project's lock
protocol and took the workstation's studio.lock (tools/coord/lock.py) for Studio work that went to
the container; MV2-V1's gate on the main Studio stopped "studio.lock is held".

- **lead `_lead_prompt`:** for a task on a slot-bound track, the lead is told its Studio tools reach
  the slot and to never take, wait for or release the workstation's studio.lock.

## v0.158.16 — a lead abort a CLI process could not confirm is retried by the coordinator

2026-10-31 10:19: an operator `unblock AS-J05` (CLI) cancelled lead case rc-4da8ec15; its abort ran
in the CLI process, which has no Kilo server credentials (HTTP 401), failed silently, and the lead
session kept working in the slot track's worktree. It took the workstation's studio.lock and
MV2-V1's scripted gate stopped "studio.lock is held".

- **lead `_abort`:** an abort Kilo does not confirm (False or an error) is queued in meta
  `lead_abort_pending` with an event; **`retry_aborts`** retries it from the running coordinator's
  own client every tick (`engine.tick`), for up to 24 h.

## v0.158.15 — Studio slots: a track can run its Studio work in a containerised Studio

2026-10-31 (Victor: "build dispatch, real sprint" — first run AS-J05 on contrib/studio-slot).
A track used to have no Studio (one Rojo-served main checkout): its Studio checks waited for the
merge. A track bound to a slot now runs them in the slot.

- **New `foundry_coordinator/slots.py` + `[slots.<id>]` in project.toml** (container, repo, foundry,
  path_prefix, official_mcp, community_mcp, enabled). `python -m foundry_coordinator.slots run`
  moves the slot to the commit (`slot-checkout`: fetch, detached checkout, Rojo restart and Studio
  reopen when files were added), runs the same `studio_calls pipeline` inside the slot, and copies
  only that task's outputs (docs/progress/<ID>_*, docs/evidence/<ID>/, .plan/tmp/gate/<ID>/) back
  into the track's worktree for the coordinator to commit.
- **`tracks start <project> <name> <sprints> --slot <id>`** binds the track; its kilo.jsonc's
  enabled Studio MCP servers run `docker exec -i <container> <mcp>` (also after `set-needs`).
- **studio_calls:** a slot-bound track gets the scripted gate; its run takes `slot:<id>` instead of
  `studio:*` (never the workstation's Studio), skips the workstation studio.lock, stops a
  playtest/pipeline inside the slot when killed.
- **Track rules:** `TrackManager.studio_less(P)` (a track with no slot) replaces "is a track" for the
  Studio-only decisions: full verify incl. the gate binding and the GATE/BOOT/EVIDENCE rows, the
  review keeps Studio evidence, visual approval runs in-track, the BUILD/CLOSE prompts give the
  slot notice instead of "no Studio here".
- Live check: a GF-SEC3 run through `slots run` on the container: checkout + Rojo restart + Studio
  reopen, host half PASS, scripted half 14/15 (the known SEC3.0b timing red), 18 outputs copied back.

## v0.158.14 — a red run on code the sprint's base never had is no "pre-existing" baseline

2026-10-31: MV2-V1's gate skipped S15, UI_02a..05a and UI_POLISH1 as PRE-EXISTING: they were red in
MV2-11's UseV2-on run, but MV2-11 reverted the flip and MV2-V1 flipped it again, so those reds were
MV2-V1's own change (Victor: the v2 map has to work; nothing gets excused).

- **AU studio_gate `preexisting_plan`:** a stored run is a baseline only if every `src/` file this
  sprint's own commits changed has the same content at that run as at the sprint's base (the
  parent of its first own commit). Otherwise the run is skipped and an older matching run, if any,
  decides.

## v0.158.13 — a task base left on a failed track falls back to the main base

2026-10-31 04:16: GF-SEC3's gate passed but verification failed `coordinator.history`
("324b16cb is no longer an ancestor ... history rewritten"): its task_base was a commit of the
failed pt track; the sprint had restarted on main at base_sha. 6 attempts were used up.

- **engine `_generic_checks_uncached`:** when task_base is not an ancestor of HEAD but base_sha
  is, the history / scope / commit checks are measured from base_sha.

## v0.158.12 — plan suite labels that are not suites are never required

2026-10-31 03:58: AS-D19 (the Baroness) stopped: its plan line "Owed at merge: suites S1-S4"
(labels of merge-owed Studio checks) had seeded required suites S1..S4; the session ran the
network suites S01-S04 instead and blocked on S03's play-session legs.

- **AU adapter `_plan_requirements`:** a suite the plan requires must be a
  `tests/RegressionIndex.lua` row or a check file the sprint has or adds (`tests/<id>_check.*`).
- **obligations `seed`:** an open plan-seeded `build.suite.<id>` obligation whose id the adapter
  says is not a suite is marked superseded ("a plan label; no evidence owed").

## v0.158.11 — the Studio gate takes phone-sized frames, and never a wrong-size one

2026-10-31 03:35: GF-RT1 ran out of attempts again (23). Its visual review needs phone frames
(lunchbox_390x844.png, lunchbox_844x390.png), but CAPTURE_READY always took the play viewport
(1920x1080) and wrote whatever came back under the requested name. Any UI fix staled the old
frames, and no valid new one could ever be taken (diagnosis by Lead case rc-defcb91f).

- **AU studio_gate `capture_ready`:** a frame names its size with `viewport=WxH` on the
  CAPTURE_READY line or a `_WxH` file-name suffix (320..3840). The runner sizes the play client
  to it (set_device_simulator, ActualResolution, read back), takes the frame, restores the play
  viewport, and writes the file only when the PNG is that size (else `FAIL: CAPTURE ... got AxB,
  want WxH (no file written)`). Frames that name no size behave as before.

## v0.158.10 — the chain order check accepts any earlier prerequisite PASS

2026-10-31 03:01: GF-RT1 used all 21 implementation attempts while every scripted gate passed.
Its submission was refused "suite:GF_RT1 order_violation (ran before prerequisite boot:server,
boot:client)": the suite's deciding record is its highest-tier PASS (a 01:06 "reviewed" run),
the boots' is their newest PASS (03:00), and comparing the two said the suite ran first.

- **evidence `_evaluate`:** a prerequisite is in order when any of its creditable fresh PASSes
  ran at or before the dependant's deciding record (each check still decides on its own).

## v0.158.9 — bare evidence names are tracked; a closed lead case no longer delays decisions for hours

2026-10-31 02:14: GF-RT1's fix round blocked "the plan requires GF-RT1_EVIDENCE_RAW.log but no
obligation tracks it". The plan line mentioned CAPTURES and named the evidence log without its
folder; the log is tracked as a log_marker ref (`docs/progress/GF-RT1_EVIDENCE_RAW.log::GATE ...`).
The 3-minute decision then waited until 06:10: a lead case had been DIAGNOSING at its first
deadline, so the deadline moved to the case's 4-hour bound and stayed there when the case closed.

- **obligations `reconcile`:** plan-named files are compared against each ref's path (the part
  before `::`), and a bare file name is tracked when a ref ends with `/<name>`.
- **decision_policy `sweep`:** while a lead case works, a due decision is re-checked every 5 min
  instead of being moved to the case's own deadline.

## v0.158.8 — "No Roblox Studio instances are connected" reopens Studio

2026-10-31 02:04: the workstation Studio's TEST place closed during MV2-11's scripted gate
(Studio stayed up on its start page). The official MCP said "No Roblox Studio instances are
connected", which no autoheal pattern matched, so it became a Lead case instead of a reopen.

- **autoheal `studio_reload`:** also matches "no (roblox) studio instance(s) (are/is) connected".

## v0.158.7 — the post-review check uses the same unchanged-frames rule

- **visual_approval `step`:** after the model answers, the "became stale" check accepts a HEAD
  that moved past the closeout revision when no named frame changed (v0.158.6's rule). Before,
  the verdict was applied only on the next tick, from its record, with a misleading "stale" note.

## v0.158.6 — visual approval runs after a later commit that leaves the frames alone

2026-10-31 01:25: GF-RT1 passed deep review and closed at 1dc4bef, then a docs-only prep commit
landed on master. Its visual approval only ran while HEAD was the exact closeout revision, so it
sat in AWAITING_HUMAN ("waits for the exact clean closeout revision") with no way forward.

- **visual_approval `step`:** when HEAD has moved on, the review still runs if the closeout
  revision is an ancestor of HEAD and no commit since touched a frame the rule names (a named
  scope frame or a path matching the rule's artifact patterns). Otherwise it waits, as before.

## v0.158.5 — automatic gates run every required suite; plan labels are never suites

2026-10-31 01:00: two sprints stopped on the automatic Studio gate's suite list.
- MV2-11 parked "no fresh verified record" for S46: its last host half had re-run S17 only, and
  every automatic re-gate copied that list, so S46 (required, staled by newer code) never ran.
- The as-d19 track merge went red "-Suites S1 has no index row": the plan labelled its
  merge-owed Studio checks S1-S4 and they were read as suite ids.

- **studio_calls `default_request`:** the sprint's required suites (BUILD `suite:` checks) always
  join the last run's suites; and only ids present in `tests/RegressionIndex.lua` are kept (a
  sprint left with none shares the merge's full regression, as Blender sprints do).

## v0.158.4 — Roblox's own Studio MCP waits for Studio to connect (Studio slot containers)

2026-10-31: in the studio-slot container, studio_gate's doctor failed "need exactly one Studio
matching '[SLOT-', found 0 (open: none)" while the community MCP saw Studio. There is no Studio-side
MCP host running before the gate starts StudioMCP.exe, so it becomes the host and Studio connects a
few seconds later; the gate listed Studios at once.

- **mcp_stdio `OfficialStudio.resolve`:** when no Studio with a place is listed yet, list again every
  2 s for up to `CONNECT_WAIT_S` (as the community server already does). With the fix the
  container's `studio_gate doctor` is READY.

## v0.158.3 — the 2-player lane's suites get evidence records

2026-10-31: MV2-11's gate ran S17 in the multiplayer lane and passed 14/14 (v0.157.99), yet the
submission kept refusing "check suite:S17: no fresh verified record". The calls verdict's evidence
block carried only the solo lane's GateRunner.tally, so S17's `SUITE` line never became a
`suite:S17` record.

- **studio_gate calls:** each multiplayer lane's tally is appended to the evidence tally, so its
  rows are recorded like the solo rows.

## v0.158.2 — Lead repair validation finds Lune (studio_gate candidates were all rejected)

2026-10-30: GF-RT1's Lead produced five candidates for the per-frame capture viewport; every one
was rejected "tests/coordinator/test_studio_gate.py: 1 failed (test_bridge_wrapper_runs_every_
suite_shape_under_luau)". The same test passes at each candidate outside the sandbox. Repair
validation runs pytest with HOME/USERPROFILE moved into a sandbox, so the test could not find Lune
in `~/.rokit/tool-storage`, fell back to the rokit shim on PATH, and the shim only runs inside a
folder with a rokit.toml. Every Lead repair touching studio_gate.py failed validation this way.

- **repair `sandbox_env`:** the user's rokit tool storage (binaries, no credentials) stays
  reachable as `ROKIT_TOOL_STORAGE`.
- **test_studio_gate `_lune`:** looks in `ROKIT_TOOL_STORAGE` first and checks the candidate
  actually runs (`lune --version`); a Lune that cannot run skips the test instead of failing it.

## v0.158.1 — an interrupted prep batch that already pushed is kept

2026-10-30 23:43: a forced coordinator restart (a deploy) hit after a prep batch had committed and
pushed its two entries but before the in-flight marker was cleared. On the next start the
recovery reset master to the batch start, so origin/master held commits the main checkout no
longer had (fixed by hand with a fast-forward).

- **prep_queue `_recover`:** a batch whose HEAD is already on the upstream branch, or whose entries
  are all marked landed, is kept (the marker is cleared and the event says "kept"); only a really
  half-landed batch is rolled back to its start.

## v0.158.0 — prep entries land with a 3-way apply when master moved nearby

2026-10-30 23:09: two prep entries (the GF-NAME1 merge and the owner-decisions docs) failed
"git apply --check failed: patch failed: docs/progress/SPRINT_STATUS.md:236": master had edited
lines near their hunks after the branches were cut, and the plain patch apply needs the context to
match exactly.

- **prep_queue:** when the plain `git apply --check` fails (and the patch is not already in the
  tree), a `git apply --3way` check follows; non-overlapping edits then land 3-way. An overlapping
  edit is still refused (reported as a failed check), and the batch resets the checkout as before.

## v0.157.99 — the Studio gate runs 2-player suites in a multiplayer playtest

2026-10-30: MV2-11 parked on suite S17 (`players = 2`). Since 2026-10-27 the scripted gate listed
such rows as MANUAL ("the session runs it with multiplayer_playtest") and never ran them, while the
evidence obligations still required a fresh S17 record, so the sprint could not close either way.
Victor: "build it".

- **studio_gate calls:** rows with `players > 1` now run after the solo rows, one lane per player
  count, in a `multiplayer_playtest` with that many clients (start waits for every player; the lane
  ends the playtest). Each lane gets its own Studio CALL plan and `GateRunner.tally`, merged into the
  run's suites; the rows score like any other row (fresh PASS/FAIL), never MANUAL. Unscoped
  `eval_server_runtime` / `eval_client_runtime` reach the group's server and client-1
  (verified live on robloxstudio-mcp 3.4.2 in the studio-slot container).
- `FOUNDRY_GATE_MULTIPLAYER=0` keeps the old MANUAL behaviour.

## v0.157.98 — Lead repair sessions run (they never had since 10-20)

2026-10-30 21:47: GF-RT1's Lead diagnosed a coordinator fix (per-frame viewport on
CAPTURE_READY) and its repair session died "the prompt was never admitted", as at 18:49 and
19:12. Kilo's database shows all 60 repair sessions since 10-20 with only the prompt and no
reply. A repair worktree is a Foundry-Router checkout with no kilo.jsonc; the global Kilo
config's `foundry` provider has no models, so the persona's model
(`foundry/Foundry-Kilo-Lead-Manager`) did not resolve there and Kilo never ran the turn. Lead
calls worked only because they run in the game workspace, which has the file.

- **lead:** before each repair session, the repair worktree gets the project's kilo.jsonc
  (as track worktrees do), listed in the repo's `info/exclude` so it is never part of the
  repair's change.

## v0.157.97 — a sprint in a working Lead case keeps the main checkout

2026-10-30 20:55-21:12: MV2-11 and then GF-RT1 blocked into Lead cases, and in the same tick
the next main sprint (GF-RT1, then GF-SEC3) started and took the main checkout. The Lead has
write access, so it waits for the workspace lock; no Lead call ran from 19:08 to 21:20. The
3-minute decision timeout read "DIAGNOSING without a lead call" as "lead unavailable", handed
the case back to self-repair, and MV2-11 was parked.

- **engine:** `_main_pause_is_automatic` also covers a BLOCKED sprint whose Lead case is
  DIAGNOSING or REPAIRING (not DEFERRED, not paused, not waiting on a person): it keeps its
  place in the main checkout, so the Lead runs at once instead of after the next sprint.

## v0.157.96 — the Studio gate's file list keeps the files Studio loads

2026-10-30 19:08 MV2-11: the scripted gate passed `-Files` from the whole base..HEAD diff, sorted
and capped at 200. After other tracks merged, that diff held 1381 paths, mostly
`assets/exports/*.fbx`; the first 200 were all exports, so PRE-PLAY had no Studio-loaded file
to verify ("no hash map for the files under test"), auto-heal read it as a stale Rojo sync, and
the sprint looped until a no-progress decision. Found by the Lead (case rc-6e86136d8fdf85dc).

- **studio_calls:** the gate's file list puts Luau sources first, then the rest of `src/`,
  `tests/` and `default.project.json`, then everything else, before the 200-file cap.

## v0.157.95 — Rojo reconnects and syncs by itself after a Studio reopen

Victor 2026-10-30: "make Rojo automatically connect and sync so it doesn't ask; if needed the
AI can close and reopen Studio and it auto-connects and syncs". Rojo's plugin had Auto
Reconnect on, but its confirmation setting was "Initial": the first sync of every Studio
session waited for an Accept click.

- **Foundry Client, Studio auto-reopen:** new setting `studio.rojo_auto_sync` (default on).
  Between closing Studio and opening the TEST place, the client writes Rojo's plugin settings
  for every Studio user (`%LOCALAPPDATA%/Roblox/<userId>/InstalledPlugins/0/settings.json`):
  `Rojo_autoReconnect = true` and `Rojo_confirmationBehavior = "Never"` (Rojo 7.7 values). It
  writes only while Studio is closed, because Studio keeps these in memory and writes its copy
  back.
- **LIVE stays manual:** any saved Rojo connection for a `never_place_ids` place is removed,
  so Rojo never auto-connects and auto-syncs into LIVE.
- The client must be restarted once to load this (tray → restart, or the next sign-in).
- Studio slot container (`contrib/studio-slot`): the same settings are applied before Studio
  starts, and auto-recovery prompts are turned off there; verified: start → SLOT-1 opens →
  Rojo connects and syncs → playtest, no clicks.

## v0.157.94 — the Studio gate picks the TEST Studio when more than one is open

2026-10-30 18:49: a Studio left on its start page (no place open) ran next to the TEST Studio.
Every scripted gate (MV2-11, GF-RT1) failed with "need exactly one Studio, found 2 (open: None,
Animal Uprising [TEST])": the gate ran with no place filter, so any second Studio broke it.

- **mcp_stdio:** a Studio with no place open (official: no name; community: no placeName and
  no placeId) is never counted as a gate target.
- **studio_calls:** the calls stage passes `--place` from the Foundry Client's
  `studio.title_match` (e.g. "[TEST]", already set in the client UI), so a second Studio on
  another place (a Studio slot, a sandbox) cannot be picked either. No client: unchanged.

## v0.157.93 — Studio with no place open gets the place reopened

2026-10-30 18:19: Studio was running with no place open. GF-RT1's session reported "Place is
not open" / "lists no instances" / "the TEST place needs reopening", and MV2-11's scripted gate
failed with "need exactly one Studio instance, found 0 (connected: none)". Neither wording
matched the `studio_reload` signature, so both went to Lead cases and the main track stalled.

- **autoheal:** `studio_reload` also matches "place is not open", "no place open",
  "Studio instance(s), found 0", "connected: none", "lists no instances" and "place needs
  reopening", so the coordinator reopens the TEST place and retries.

## v0.157.92 — a session that asks for a fresh Studio gets one

2026-10-30 16:57: MV2-11's session reported BLOCKED: its gate rows failed from a degraded Studio
session (stale MatchService errors, no profile, timeouts) and it "needs a fresh Studio or place
reload by the coordinator". That went to a Lead case instead of the deterministic reopen.

- **autoheal:** new signature `studio_reload` (a fresh / restarted / reloaded / reopened Studio
  or place); a session stop with it asks the client to reopen the TEST place and retries the
  session for free, like the other Studio-sync signatures.

## v0.157.91 — a scope grant is judged on the sprint's own commits

2026-10-30 14:34/14:36: AS-P7's Studio evidence was finally fresh (v0.157.90), but its
`coordinator.scope_grant` check failed: the pt and mv2-2 track merges had added their check
mounts to default.project.json after AS-P7's base, and the rojo_mapping grant blamed AS-P7 for
"tests/PT_1_check.lua is not a file the plan lists". Two sessions later AS-P7 hit its session
budget (21) and went to a Lead case.

- **scope.grant_checks:** when another sprint (by commit subject prefix) also changed the
  granted file in base..head, the grant is checked on this sprint's own commits only, each
  as commit^..commit. Without foreign commits nothing changes.

## v0.157.90 — a gate with nothing Studio-loaded under test still records what Studio loaded

2026-10-30: AS-P7 (clip exporter; its files under test were clip JSON and models) passed its
scripted Studio gate at 13:49 and 14:08, but PRE-PLAY verify was a SKIP ("no Studio-loaded file
under test"), so the run's runtime had no loaded fingerprint and the ledger read each PASS as
"unknown". The sprint cycled to 19 sessions (sessions_exhausted, Lead case).

- **Gate runner (AU):** when no Studio-loaded file is under test (only models, or only unmounted
  data), PRE-PLAY verify checks the harness baseline instead: tests/GateRunner.lua,
  tests/RegressionIndex.lua and the running suites' check files, hashed from the run's full HEAD
  map (`<ID>_gate_expect_full.json`). The run then carries a loaded fingerprint.

## v0.157.89 — the Lead repairs a red merge on the main checkout

2026-10-30 13:21-13:30: track rt's merge Studio batch was red for GF-RT1..3 (their checks never
switched the retention flags on in the test, as MASTER_ORDER R4 says), mv2-2's was red for
MV2-7..10, and pt stopped twice on Studio missing its new PT_1_check.lua. The merge had already
fast-forwarded master, so the code was there; the tracks just failed and nothing fixed them.

- **Lead repair (`merge_red_repair`, default on, Automation UI):** a red merge Studio batch
  reopens each red sprint on the main checkout (`engine.repair_reopen`: COMPLETE -> QUEUED with
  `repair_of` evidence, fresh attempts, deps on earlier repaired sprints it depends on). The
  failing rows ride as a Lead assignment; the sprint then runs the normal BUILD -> Studio gate
  -> review -> close. Event `track_studio_repair`.
- **Owed elsewhere:** a sprint whose plan says it is "verified inside **X**" (AU adapter
  `verification_owner`; MV2-7..10 -> MV2-V1) is not reopened; the event lists it as owed.
- **States:** (COMPLETE, QUEUED) is allowed only with `repair_of` evidence.
- **Merge tooling retry:** a Studio-missing-files fault (`UNRESOLVED ... live=missing`)
  restarts Rojo and reopens the TEST place before the retry (waits 120 s).

## v0.157.88 — merges get Studio; an automatic gate ERROR never loops

2026-10-30 11:41-12:50: five finished tracks (mv2-2, rt, shop1, ux, pt) waited to merge on
"main AS-P7 is READY (about to use Studio)" while AS-P7 itself waited for the checkout, then on
each next main sprint. 12:23-12:29: MV2-11's automatic gate run hit a tooling ERROR (its new
check file was missing in Studio) 11 times: each ERROR committed its outputs, the new HEAD looked
like new code, and auto-heal (keyed on HEAD) never ran out.

- **Studio for merges:** a main sprint whose next action is "waiting for ..." (checkout, gpu,
  writer slot, dependency) is not a pending Studio request; a merge waiting longer than
  `merge_studio_priority_minutes` (default 20, Automation UI) goes before pending main sprints
  (journal event `priority`).
- **Auto-verify ERROR:** like a FAIL (v0.157.x AS-P5), an automatic run's ERROR is never re-run
  when only its own outputs changed since; the sprint stops BLOCKED once, so auto-heal (Rojo
  restart + Studio reopen) and the Lead take it.

## v0.157.87 — the track planner proposes ready work; an automatic pause keeps the checkout

2026-10-30: `tracks propose` offered only S51B (finished as S51B-R) and MV2-4c (no file list),
listed the cancelled UI-02..06 on the main chain, and put ~40 open sprints in one chain, because
a few files nearly every sprint touches linked them all; the rt, shop1, ux and pt tracks were
picked by hand. Separately, GF-LOOP1 lost the main checkout for 1.5 h while it waited seconds on
the model visual approval, and AS-P7's stale-sync block let AS-J08 in.

- **Planner:** CANCELLED sprints count as done; a plan with no file list is never on a track
  (`unplanned`, with a warning). The default `track_append_safe` adds default.project.json, the
  regression index and its spec, SPRINT_STATUS.md, Strings.lua, Remotes.lua, Config/Ui.lua,
  MatchResetContract.lua (+ MatchReset/RateLimit specs), the generated asset-id tables and the
  localization tables (merge conflicts there go to the Lead repair).
- **Ready tracks:** when grouping leaves fewer tracks than max_agents allows, `propose` adds
  `ready_tracks`: sprints whose dependencies are all done, sharing no file with a running or
  queued sprint, needing no blender/gpu, each with its direct follow-ons.
- **Main checkout:** a sprint AWAITING_HUMAN only on the model visual approval, or BLOCKED on a
  scheduled auto-heal retry, keeps its place; other main sprints wait for it.

## v0.157.86 — a scripted gate's stale-sync error is auto-healed

2026-10-30 12:12/12:17: after the mv2-2 merge added check files and mounts, AS-P7's and AS-J08's
scripted Studio gates failed PRE-PLAY verify (live=missing). The stop class studio_calls_error
was not one autoheal handles, so the v0.157.74 Rojo restart + Studio reopen never ran and both
sprints went to lead cases; the three finished tracks' merge batches waited behind them.

- **autoheal.plan:** studio_calls_error with a Studio-sync signature (rojo_stale, rojo,
  rojo_connect, studio) is a free session restart after the deterministic repairs.
- **autoheal.try_heal:** the repairs read the stopped task (its blocker names the missing files),
  not the pre-stop copy, so the live=missing Rojo restart can match.

## v0.157.85 — a kept local profile edit stays kept

2026-10-30 03:2x: roles.reviewer = Foundry-Kilo-Deep-Review (Victor's choice) was set in
project.toml. The first router apply kept it (router unchanged), then recorded the TOML as the
new local base; at every later apply or restart the edit no longer read as an edit and the
router's older Foundry-Kilo-Review won, so AS-E09's review stalled on capped models.

- **Config apply:** for each kept_local path the recorded local base keeps its old value
  (foundry_automation.document.carry_local_base), so the edit stays kept until the router
  changes that key. A router change to a kept key is now logged as a conflict (router wins).

## v0.157.84 — a review cut at max_tokens retries with more room

2026-10-30 09:00/09:14: with Claude and ChatGPT capped, AS-P7's and AS-J08's reviews ran on the
local fallback; its verdict was cut at max_tokens 4000 ("structured output was incomplete
(finish_reason=length)"), three times each, and both sprints were BLOCKED.

- **Review call:** on finish_reason=length / structured_output_failed the call retries at
  twice the output budget, up to 16,000 tokens (REVIEW_MAX_TOKENS_CAP), before it counts as a
  review error.

## v0.157.83 — an attempt reset keeps the sprint's place in the checkout line

2026-10-30 04:2x-06:2x: GF-LOOP1 (closed and published, only its last checks left) was unblocked
with --reset-attempts. The reset cleared started_ts, so the one-started-main-sprint rule treated
it as unstarted and it waited while AS-P7 and AS-J08 traded the main checkout at every context
handoff.

- **unblock --reset-attempts** stores the old started_ts as data.queue_ts; the main-checkout
  line orders by the earlier of queue_ts and started_ts. The time budget still restarts.

## v0.157.82 — a leftover playtest at PRE-PLAY verify is stopped, not "stale sync"

2026-10-30 05:29: AS-P7's gate failed PRE-PLAY verify with "Edit datamodel is not available in
Play mode" (a playtest the session left running; get_studio_state did not report play mode)
and the sprint was BLOCKED as Rojo/plugin sync stale.

- **Gate runner (AU):** when PRE-PLAY verify fails on Play mode, the runner stops the
  playtest, waits 3 s and verifies once more (NOTE line in the lines file).

## v0.157.81 — a carried Studio pass is not an order violation

2026-10-30 03:57: GF-LOOP1's six carried regression suites now had fresh verified records
(v0.157.76), but the ledger's chain order check refused them: "ran before prerequisite
boot:server, boot:client". A carried record has this run's timestamps and the run's boot records
are written after it. The sprint was parked when self-repair ran out.

- **Evidence chains:** a record with provenance section "carried" is exempt from the
  prerequisite-order check (it ran after its own boot in the earlier run it came from).

## v0.157.80 — visual approval reads .jpg frames

2026-10-30 02:15: GF-LOOP1 closed and published (gate, review, closeout all passed), then the
visual approval found "the plan names no required visual frames" and sent it back to
CHANGES_REQUIRED: its frame is results_nextmatch_desktop.jpg and the AU visual rule listed
docs/evidence/{task}/*.png only.

- **Approval frames:** an approval rule's `*.png` artifact glob also matches `*.jpg` and
  `*.jpeg` (profile load). The AU example profile lists all three.

## v0.157.79 — a SUPERSEDED/CANCELLED ledger row counts as done

2026-10-30 01:28: track analysis proposed S51B and it was started; it had been rescoped and
finished as S51B-R on 2026-10-10. Its STATE.md row says SUPERSEDED/CANCELLED, which
dependency_done did not accept, so the sprint read as NOT-STARTED.

- **AU dependency_done:** a ledger row whose status column is SUPERSEDED, CANCELLED or
  SUPERSEDED/CANCELLED counts as done (never proposed or queued again).

## v0.157.78 — a frontier sprint that fell to the busy local model waits for the GPU

2026-10-30 00:51-01:16: Claude's weekly window hit 100% and ChatGPT sat at its 95% cap, so the
Implementer persona fell through to the local model. AS-D14 (assets-2) held it; GF-LOOP1 and
MV2-9 started a session every few minutes and each one failed with "serves the main stream only".

- **Local fallback:** a session failure with the local_busy signature sets the task's
  local_fallback_until (1 hour, renewed by each such failure; event local_fallback_gpu). While it
  is set, the task's session locks include `gpu`, so it queues behind the local sprint (main
  track first) instead of starting sessions that fail.

## v0.157.77 — a suite's freshness ignores other sprints' check files

2026-10-30 00:42-00:48: after v0.157.76, GF-LOOP1 ran all 7 rows green at 9bb5ae07, then edited
its own tests/GF_LOOP1_check.lua; the ledger staled S06/S07/S27/S28/S32/S50 again because every
suite depended on tests/**.

- **Suite dependencies (AU):** suite:<SID> / regression:<SID> depend on src/**,
  default.project.json, tests/GateRunner.lua, tests/RegressionIndex.lua, tests/lib/**,
  tests/<SID>_* and tests/checks/<SID>_*. Other sprints' check files no longer stale them.
  Boot, call and runtime records keep the full tests/** set.

## v0.157.76 — carried Studio passes reach the ledger; gate captures may be .jpg

2026-10-29 23:5x-00:12: GF-LOOP1 used all 6 attempts. Every Studio gate passed (its own suite
fresh, 6 regression suites carried because no file they cover changed), but the runner wrote
ledger records only for the suites it ran. The ledger staled the older S06/S07/S27/S28/S32/S50
records (any tests/** change) and the submission refused them every attempt. The plan's
results_nextmatch_desktop.jpg was re-taken with a window grab each time, which has no runtime
identity and never counts; CAPTURE_READY accepted only .png.

- **Carried suites:** a regression suite the runner marks CARRIED gets a verified record at the
  run's HEAD and runtime (provenance section "carried").
- **CAPTURE_READY .jpg/.jpeg:** the runner converts the screenshot (Pillow); the run's commit,
  the gate binding and the evidence check treat docs/evidence/<ID>/*.jpg like *.png.
- **Missing capture reason:** the refusal says only a gate-taken frame counts and names the
  CAPTURE_READY line to print.

## v0.157.75 — a retried merge re-checks the files it brought

2026-10-29 23:11-23:18: track mv2-1's merge Studio batch was red (MV2_1..6 rows, S49, S50). Its
commits were already on master (fast-forwarded at 22:16); the merge was re-requested, the second
fast-forward found 0 commits, and the gate passed "no code files" and marked the track merged
without re-running anything.

- **Merge retry:** when a retry fast-forwards nothing, the merge keeps the first merge's files,
  commits and base, so the gate and the Studio batch run on them again.

## v0.157.74 — files missing in Studio restart Rojo, then reopen the place

2026-10-29 22:16: the mv2-1 track merge added eight check files and their mounts to
`default.project.json`. The running Rojo never re-reads its project file, so Studio had none of
them (`live=missing`): GF-LOOP1's scripted gate and the merge's own Studio batch both failed the
pre-play verify. The auto-heal kept the serving Rojo and only reopened the place, which cannot
help; Rojo was restarted by hand at 22:23.

- **Auto-heal:** a stale-sync stop whose verify lists UNRESOLVED files with `live=missing`
  restarts Rojo through the Foundry Client, then reopens the TEST place so the plugin reconnects
  to the new session. Other stale-sync stops keep the serving Rojo (reopen only), as before.
- **Self-heal caps** count events on the coordinator's clock (they compared with the wall clock).
- Test fix: two self-heal tests advanced the clock exactly one second past the 2-minute grace and
  failed on a loaded machine; the margin is now 10 s.

## v0.157.73 — the track review note is the coordinator's recorded decision

2026-10-29 21:22-21:49: MV2-5 used 11/11 review calls. With the Studio gate excused, the reviewer
kept asking for "an explicit coordinator or human decision" deferring it, for the D7 spike's
measured numbers (a Studio measurement) and for unticking the steps whose Studio half is owed.

- **TRACK_REVIEW_NOTE** now states it is the coordinator's recorded, binding decision; that ticks
  for a step's code half are correct; and that Studio measurements (spikes, timings) are owed too.
- **Owed-evidence pattern** also matches findings that only argue about that deferral (spike /
  measurement, merge gate, explicit/coordinator/human decision, waive, defer).

## v0.157.72 — a main-checkout CLOSE never waits for the local GPU

2026-10-29 20:52-21:20: AS-R5 (asset sprint, local model) was APPROVED; its CLOSE session (docs:
summary, STATE row) waited 28 min for `gpu`, held by the assets-2 track, while the main checkout
and its Claude/ChatGPT sprints (AS-REG1, GF-LOOP1) sat idle.

- **CLOSE persona:** on the main checkout, when a local-model sprint's CLOSE would wait for a
  `gpu` someone else holds, the CLOSE session runs on the project's default implementer persona
  (frontier first) without taking `gpu` (`closeout_frontier` event). CLOSE is no Blender work.
  Track sprints and frontier-default projects are unchanged.

## v0.157.71 — a track review that only asks for Studio evidence is an approval

2026-10-29 20:36-20:49: after v0.157.69 MV2-5's reviewer still cited the plan's BUILD contract
("GATE MV2-5 PASS in the evidence log") on every round, and S52's submission still listed its
capture PNGs as missing evidence items; both looped into decisions.

- **Track review:** a CHANGES_REQUIRED/BLOCKED review of a track sprint whose every finding names
  only evidence the merge gate owns (gate line, suite tallies, boot, captures, visual frames) is
  treated as APPROVED (`track_review_owed_only` event). Any other finding keeps the review.
- **Submission gate:** the files of a deferred capture obligation (its PNGs) are deferred too.

## v0.157.70 — a track sprint's visual approval is owed at the merge

2026-10-29 19:52: S52 (track frontier-4) reached CLOSING and went to visual approval, which found
no frames ("the plan names no required visual frames") and sent it back. A track has no Studio;
its frames come from the merge's Studio batch, and the visual sign-off is the owner's batch
approval after it.

- **Approvals:** a `visual` approval rule is skipped for a track sprint; the task records
  `visual_owed_at_merge` and a `visual_approval_deferred` event. Main-checkout sprints are
  unchanged.

## v0.157.69 — track sprints are never asked for Studio evidence

2026-10-29 20:0x-20:16: two track sprints looped into "findings repeat" decisions on evidence only
the merge gate can produce. S52 was asked for its Studio captures (each capture obligation pairs
`capture:<png>` with the PNG path, and only the check counted as the merge gate's). MV2-5's
reviewer saw `gate:MV2-5` / `suite:MV2_5` listed MISSING (items with no path) and held that the
plan's BUILD contract outranked the track note.

- **Submission gate:** an obligation that needs a check the merge gate owns is deferred as a
  whole; missing evidence items named by such a check are not asked for.
- **Review packet:** missing/stale items owned by the merge gate are left out by file or by check
  name (`TrackManager.merge_gate_owns_item`).
- **Track review note:** says those plan lines (GATE PASS, suite tallies, boot counts, captures)
  are owed to the merge gate.

## v0.157.68 — the main track gets the GPU first

2026-10-29 19:48: AS-R5 (main track, local model, between sessions) waited for `gpu` held by the
assets-2 track's AS-D11. As a started main sprint it kept the main checkout, so AS-REG1 and
GF-LOOP1 (Claude/ChatGPT) could not start either: the main track sat idle.

- **GPU priority:** a track sprint does not take `gpu` while a main-checkout sprint is waiting for
  it; the track sprint waits ("main sprint X goes first") and takes it after.

## v0.157.67 — a RETIRED plan never runs

2026-10-29 19:30: GF-FX4 was proposed by track analysis and started on track frontier-3, but its
plan opens with "RETIRED 2026-10-10 — superseded by UI-02 ... Do not run GF-FX4."; the session
refused it at once and a lead case opened.

- **Track analysis:** a plan whose first non-empty line is a RETIRED banner counts as done (never
  proposed, never "open").
- **AU preflight:** a sprint with a RETIRED plan is refused before any session or attempt.

## v0.157.66 — a CLOSE session waiting for a resource gives the checkout to a landing

2026-10-29 19:0x: AS-R8 (local model) was APPROVED and its CLOSE session waited for `gpu` (the
assets-2 track held it) while keeping the main checkout; prep entry pq-8755b41e44 could not land
and AS-REG1 waited behind it.

- **CLOSE start:** under the landing drain, a sprint whose CLOSE session has not started gives
  its checkout and session locks back (as an implement start already did) and takes them again
  when the session starts.
- **Prep safe point:** a CLOSING sprint with no CLOSE session and no checkout lock is between
  sessions, not mid-sprint.
- **Review validity:** a prep-queue landing between the review and CLOSE never voids the review
  (prep commits are operator work, as in the scope check since v0.157.53).

## v0.157.65 — one local-model session at a time

2026-10-29 18:29-18:45: AS-R8 (main track, local Qwen) and AS-D11 (assets-2 track, local, holding
`gpu`) started local sessions together. The local server serves one stream and refused AS-D11
three times ("serves the main stream only and 1 session(s) are active"); each refusal cost it an
attempt.

- **Session locks:** a sprint whose implementer is a local persona (`[training] local_personas`)
  also takes the `gpu` lock, so a main sprint and a track sprint on the local model wait for each
  other instead of colliding.
- **Infra signature `local_busy`:** that refusal is a free, backed-off retry, never an attempt.

## v0.157.64 — a COMPLETE ledger row counts as done

2026-10-29 look-ahead scan: `dependency_done` accepted only `DONE` in a STATE.md row, but older
rows say `COMPLETE` (S14, S33, S34, S45, S51). Plans depending on them would block at QUEUED
("needs S51, which is not approved/complete"), and `tracks propose` offered tracks that re-run
S33/S34/S14.

- **AU adapter dependency_done:** a row whose status column is exactly `COMPLETE` is done.
  `COMPLETE-PENDING-VISUAL-REVIEW` keeps its own rule (`deps_accept_pending`), `NOT DONE` and
  `IN PROGRESS` rows are not done.

## v0.157.63 — model files are not counted by Studio's source check

2026-10-29 17:35: AS-REG1's scripted run stopped at the pre-play verify: matched 4/35,
mismatched 0, unresolved 31. The 31 were `.rbxm` models, which have no Source for
`GateRunner.verifyLive` to hash, so they always read `live=missing`.

- **studio_gate live_verify:** `.rbxm`/`.rbxmx` files are left out of the source-hash map and
  listed in a NOTE ("not source-verifiable"); a run with only model files under test is SKIP
  (not required). Scripts are checked as before.

## v0.157.62 — the lead's calls may step down to the allowed local model

2026-10-29 17:35: Claude reached its usage cutoff and AS-REG1's lead calls stepped down to the
local Qwen listed in `[lead_manager] expected_models` (`allow_local_route = true`). The
after-call served-model check still called the local route a mismatch, discarded three lead
reports and escalated the case to the owner.

- **Lead served-model check:** with `allow_local_route`, a local route serving a model in
  `expected_models` is accepted (as the readiness check already did). An unknown backend or a
  local model outside the list is still a mismatch.

## v0.157.61 — the coordinator commits a sprint's left-over plan history

2026-10-29 16:18: AS-R7's verification failed `coordinator.clean` on an uncommitted
`docs/progress/AS-R7_PLAN_HISTORY.md` (written by `plan_new.py --set-next`). The same happened to
AS-D13 and AS-V03; each cost a verify round and a new session.

- **`[automation] auto_commit_task_files`** (default `["docs/progress/{task}_PLAN_HISTORY.md"]`):
  when a sprint reaches VERIFYING, the coordinator commits those of its own files a session left
  dirty, then verifies the new HEAD. Another sprint's files are never touched.

## v0.157.60 — an upgrade retry also refreshes a used review or session budget

2026-10-29 15:54: the Lead retried HK-STORE1 on the new code (retry_after_upgrade) after the
SPRINT_STATUS loop was fixed, but kept the review budget the loop had used (5/5); the task
re-blocked 30 s later and parked when self-repair ran out.

- **Lead retry_after_upgrade:** a case opened for a spent budget (attempts, review calls or
  sessions) retries with a fresh one (`unblock --reset-attempts`); any other trigger keeps
  its budget.

## v0.157.59 — SPRINT_STATUS.md never voids a review

2026-10-29 15:15-15:37: HK-STORE1 (track gpt-6) was APPROVED five times; each CLOSE session
edited `docs/progress/SPRINT_STATUS.md`, which the profile's closeout exempt list did not name,
so the review was voided and run again until the review budget (5/5) was used and it BLOCKED.

- **AU adapter:** `exempt_paths` always adds `docs/progress/SPRINT_STATUS.md` (a file every AU
  CLOSE writes), whatever the profile lists. The example profile lists it too.

## v0.157.58 — the always-approved scope classes are granted at once

2026-10-29 permissions review (Victor: "if there is constant permission issues then we need to
adjust our permissions"): 80 scope decisions in 3 days, 79 approved, 75 of them only after the
decision timeout. Each one stalled the sprint, and a grant often restarted its session. Kilo
tool denials in the last 12 h were few (read-only helper/lead sessions, as designed).

- **Default allow-list** (`[automation] scope_auto_approve`) gains the classes approved every
  time: the sprint's own `docs/progress/<ID>_*`, its check file `tests/checks/<ID_>_check.lua`
  / `tests/<ID_>_check.lua`, `tests/RegressionIndex.lua`, the generated
  `docs/localization/LocalizationTable.*`, asset `assets/source/*/*/checklist.md`, and hand-off
  lines in another sprint's `docs/progress/*_PLAN.md`. These are in the session's edit scope
  from the start. `src/**`, other sprints' tests, Lune specs and tools still ask.
- **`{task_}`** placeholder (the id with `_` for `-`) for allow-list paths (`scope.rule_pattern`).
- The router's Automation settings show the new defaults after a backend rebuild (optional; the
  coordinator applies them now).

## v0.157.57 — a first start waits for the plan's dependencies

2026-10-29 15:09: AS-R7 was unblocked to READY (an unblock skips the QUEUED dependency check)
and started on the main checkout while AS-P5, which its plan needs, was still open.

- **First start:** a sprint about to start for the first time waits ("waiting for …") while a
  queue dep or a task-source dep that is on the queue is not satisfied. A resumed sprint
  (attempt > 0) is unaffected.
- Test update: the lifecycle e2e test now expects v0.157.55's wait (not a block) for a
  source dep already on the queue.

## v0.157.56 — one non-code list for both gate checks

2026-10-29 15:07: AS-P5's verification passed (`au.gate_bound_to_head` included, v0.157.52), but
the submission evidence check kept its own copy of the exempt paths without the sprint's frame.
It called the gate stale and sent AS-P5 back; the session then BLOCKED on a "tooling" fault.

- **AU adapter:** `gate_noncode(spec)` is the one list of paths that never unbind a gate
  (closeout-exempt, `docs/progress/<ID>_*`, `docs/evidence/<ID>/*.png`,
  `docs/evidence/UI-v3/<ID>/**`, memory, plan). `_gate_binding` and `_gate_staleness` both
  use it. The binding cache key changed so older results are not reused.

## v0.157.55 — a dependency named in the plan that is already queued is waited for

2026-10-29 15:01: AS-R7's plan names AS-P5, which was approved and running, but its queue
entry listed only AS-R4. When AS-R4 completed, AS-R7 BLOCKED ("needs AS-P5, which is not
approved/complete") and opened a lead case.

- **QUEUED check:** a task-source dependency already on the queue (not FAILED/CANCELLED) is
  added to the sprint's queue deps (`deps_added_from_source` event) and the sprint waits. Only
  a dependency missing from the queue still blocks.

## v0.157.54 — a cancelled sprint moved onto a track is reopened there

2026-10-29 14:43: HK-STORE1 was approved (READY on main), cancelled to move it onto the
ChatGPT track gpt-5, and `tracks start` accepted it, but the sprint stayed CANCELLED and the
track failed 2 s later.

- **tracks start:** a CANCELLED sprint is reopened onto the track (fresh attempt counters,
  chained after the sprint before it). `reopen` takes a `track`.

## v0.157.53 — scope never blames a sprint for a prep-queue landing

2026-10-29 14:16: the operator's TIER-C prep (`tools/guard/done_check.py`) was cherry-picked
onto master inside MV2-0's range. `coordinator.scope` counted it as MV2-0's change, MV2-0 used
its last attempt and parked.

- **Scope check:** a commit whose subject starts with a prep-queue entry's message is the
  operator's landing (or a cherry-pick of its branch), like another sprint's commit. Review and
  gate freshness checks still count its files.

## v0.157.52 — the gate binding check accepts the sprint's own committed frame

2026-10-29 14:09: once v0.157.51 ended the AS-P5 re-run loop, verification failed
`au.gate_bound_to_head`: the gate ran at 856a204 and the only change since was the run's own
frame `docs/evidence/AS-P5/*.png`, which that check (separate from the scripted-run freshness
check) still counted as code.

- **au.gate_bound_to_head:** PNG frames under `docs/evidence/<ID>/` are evidence, not code.
  Another sprint's evidence or any code change still unbinds the gate. The check's cache key
  changed so earlier FAIL results at the same revision are not reused.

## v0.157.51 — a committed frame keeps the scripted gate fresh

2026-10-29 13:27-14:02: after v0.157.50, each AS-P5 PASS run committed its frame under
`docs/evidence/AS-P5/`. The freshness check exempted only `docs/progress/<ID>_*` and
`docs/memory/**`, so the outputs commit made the gate look stale, auto_verify ran again at the
new HEAD, and ~60 PASS runs looped every ~40 s, holding the main checkout and the landing drain.

- **Gate freshness / same-code check:** PNG frames under `docs/evidence/<ID>/` count as the
  run's own outputs; any other change (src, tests, another sprint's evidence) still makes the
  gate stale.

## v0.157.50 — the frame the runner takes is committed with the run's outputs

2026-10-29 12:42: AS-P5's scripted run took its CAPTURE_READY frame (v0.157.47), but the outputs
commit covered only docs/progress/<ID>_*, so the PNG stayed uncommitted and the clean-tree checks
failed; the sprint parked on repeated findings.

- **Scripted run commit:** the captures the run logged as `PASS: CAPTURE <path>` (under
  `docs/evidence/<ID>/` only) are committed with its lines, evidence and gate files.

## v0.157.49 — an idle leftover session never blocks a landing

2026-10-29 12:2x: right after v0.157.48, MV2-0's helper session from its earlier track run (MV2-0
READY, not running) held the same prep landing.

- **Prep safe point:** a session of a sprint that is not mid-sprint (READY / BLOCKED /
  CHANGES_REQUIRED / QUEUED / PAUSED) blocks only while Kilo reports it busy.

## v0.157.48 — a finished sprint's session never blocks a landing; Studio checks wait for the batch

2026-10-29 11:35-12:20: every main sprint waited behind prep entry pq-644516b79d, whose safe
point was blocked by GF-FX5's helper session (GF-FX5 COMPLETE, its track merged, the session never
marked ended). Track gpt-3 (GF-SEC1, tier A on a backend track) failed its merge gate on the gate
verdict, boot lines and evidence log that only the merge's Studio batch produces.

- **Prep safe point:** sessions of COMPLETE / CANCELLED sprints are ignored.
- **Track merge gate:** with a Studio batch planned, the project's Studio-owned checks (gate
  verdict, boot, evidence log, gate bound to head) are owed to the batch, not a red gate.

## v0.157.47 — the scripted Studio runner takes the frame at CAPTURE_READY

2026-10-29 11:59: AS-P5's suite staged its telegraph frame and printed `CAPTURE_READY <path>`,
expecting the host to take it before the next CALL restored the scene; the runner never did, so
the evidence frame was never taken and the sprint blocked.

- **studio_gate:** a CALL result with `CAPTURE_READY <path> ...` makes the runner take the game
  viewport (`capture_screenshot`, no Studio chrome) to <path> before the next CALL — only under
  `docs/evidence/<sprint>/` — and log `CAPTURE <path> ...` + `PASS:`/`FAIL: CAPTURE ...` in the
  lines file and the run's record (`raw_results.captures`), where the coordinator records it as
  the capture (v0.157.39). The suite's own text, and so its tally, is unchanged.

## v0.157.46 — a sprint cancelled before it starts leaves its track

2026-10-29 11:24: MV2-1 (queued by mistake behind MV2-0 on track gpt-2) was cancelled; the track
failed on it and MV2-0 moved to the main checkout mid-sprint, away from its staged work.

- **Tracks:** a sprint cancelled before it ever started is dropped from its track
  (`track_sprints_dropped`); the track carries on. A started sprint that fails or is cancelled
  still fails the track.

## v0.157.45 — a re-sent coordinator event never reopens its alert (router)

2026-10-29: "Coordinator error on GF-ENT1" (four errors from 2026-10-28 18:00, long fixed, the
sprint COMPLETE) kept popping up: the coordinator re-sends its recent events on every restart, and
the router reopened the auto-cleared alert for each.

- **Control center:** a coordinator event raises its alert only the first time the router stores
  it. A new event still alerts. **Needs a router rebuild.**

## v0.157.44 — the Foundry Client watches memory and stops runaway dev / test jobs

2026-10-29 (Victor): a stuck training run held 32 GB for 30 hours and nothing noticed.

- **Resource watch** (`foundry_client/resources.py`, every 60 s): free RAM and the development /
  testing jobs the stack runs (training runs, scripted Studio checks, gate.ps1, Lune, headless
  Blender builds, `rojo build`, pytest, the game repo's tools/ scripts), each with its whole
  process tree.
- A job over its **memory limit** (two checks in a row) or **time limit** is stopped with its tree
  when auto-stop is on; its work stays queued. Every stop is an event and an alert.
- **Anything else is never touched**: Studio, Blender's window, the components and your apps only
  appear in a low-RAM alert that names the biggest users.
- A **Memory** card on the status page; every limit, the low-RAM level, the interval and
  auto-stop are on the Settings page (Resource watch).

## v0.157.43 — the automatic training run reads shared sources once and has a watchdog

2026-10-28 04:29 → 2026-10-29 10:5x: one automatic training run (`foundry_training auto`) held
32 GB committed for 30 hours and finished nothing after 20:36; every later sprint's run found
its lock busy, and the machine ran out of RAM. For every queued manifest it re-read the whole
coordinator DB (167 MB), Foundry, git and gate files and re-curated the project.

- **process_queue** ingests each shared source once per pass and each manifest's own Kilo
  sessions, then curates each project once and settles the manifests.
- **`auto --max-minutes`** (default 90): a watchdog ends the run and removes its lock; the next
  run resumes the spool (ingestion is idempotent, unfinished manifests stay queued).

## v0.157.42 — a bridge suite's file never needs a Studio mount

2026-10-29 09:3x: GF-SAVE1 ran S05 (loader = bridge) and PRE-PLAY verify stopped on
"tests/S05_check.lua not mounted anywhere". The runner's bridge serves a bridge suite's source
from HEAD, so Studio's loaded copy never matters.

- **PRE-PLAY / POST-PLAY verify** notes a bridge suite's unmounted check file instead of
  stopping on it.

## v0.157.41 — a suite's designed FAILs do not make its PASS unknown

2026-10-29 06:02-08:54: HK-TEST1's suite printed `pass=10 fail=1 expect=10 designedFails=1
VERDICT PASS` (its negative control FAILs by design) on six scripted runs; each was recorded
UNKNOWN ("verdict PASS with fail=1"), the plan's suite obligation never closed and the sprint
was parked after its self-repair ran out.

- **AU evidence** accepts a suite PASS whose fail count is within its designedFails.

## v0.157.40 — rows already red on master never fail a sprint

2026-10-29: legacy regression rows (S39 'no active profile', S48, S00/S03/S06/S14, UI_0xa,
UI_V1) were red on master and failed every sprint whose selection held them: the assets-local
merge batch, HK-CLEAN1 (three times), and others.

- **Scripted Studio runs** skip a regression row as PRE-EXISTING (a `SKIP: GATE suite` line, a
  non-required check, verdict `PRE_EXISTING`, never a failure) when the newest scripted run of
  it by ANOTHER sprint on HEAD's history found it red, nothing that maps to it directly changed
  since, its own RegressionIndex entry is unchanged, and none of this sprint's commits touched a
  file that maps to it or the harness. A sprint's own suites always run; `--rerun all` runs
  everything.

## v0.157.39 — a check's own playtest capture counts as the plan's capture

2026-10-28/29: AS-P5's A10 leg took its proxy-telegraph frame in the scripted playtest and
printed `CAPTURE <path> runtime=playtest ...` + `PASS: A10` at every run, but only
capture-window.ps1's record format made a capture record, and the scripted run's call output
was never read for captures. The plan's capture obligation never had a fresh record, and the
sprint cycled for a day on "no fresh verified record".

- **AU evidence** reads a GateRunner `CAPTURE <path> runtime=...` line as a capture record; the
  next PASS:/FAIL: line is its verdict.
- **The scripted Studio run** records captures from its call output, bound to the run's
  runtime and gate HEAD like its suites.

## v0.157.38 — a sprint that yields the checkout gives back its lock

2026-10-29 07:00-07:20: AS-P5 (restarted by an attempt reset, so later than HK-CLEAN1) yielded
to HK-CLEAN1 under the one-started-sprint rule but kept the checkout lock; HK-CLEAN1 waited for
the lock and nothing ran.

- **A main sprint waiting for an earlier started one** releases its checkout and session locks,
  as a drained sprint does; its next session start takes them again.

## v0.157.37 — scope grants work on plans with mixed line endings

2026-10-29 06:44: GF-SAVE1's plan is LF with three CRLF lines. The AU scope grant split it on
CRLF, never found 'Files this sprint may change', and the approved grant of the regenerated
localization exports failed ("the plan's file list could not be edited").

- **The AU scope grant** splits the plan on LF (CRLF lines keep their ending) and writes the
  new line with the file's majority ending.

## v0.157.36 — a retry after an upgrade gets attempts when they ran out

2026-10-29 06:18: v0.157.34 retried AS-P5 after the upgrade, but its case had opened because
its 8 attempts were spent, so the engine failed it again within seconds.

- **The Lead's retry after an upgrade** resets the attempt budget when the case was opened
  because the attempts ran out.

## v0.157.35 — a never-admitted Lead session retries after a minute

2026-10-29 05:39-05:45: two Lead repair sessions whose prompts Kilo never admitted ended after
300 s and the cases were deferred 10, then 40 minutes while every sprint waited.

- **The Lead** retries a case whose session was never admitted after 60 s, twice per case, then
  falls back to the normal backoff.

## v0.157.34 — a coordinator bug case retries its sprint after an upgrade

2026-10-29 04:50-05:50: four Lead cases classified "coordinator" (gate file list, review packet,
unmounted check) sat DEFERRED for an hour after v0.157.29-33 had shipped their fixes; the Lead's
own repair sessions kept ending on infrastructure, and no sprint ran.

- **The Lead** cancels a coordinator-class case opened before the running coordinator started
  (no session in flight) and retries its BLOCKED sprint once on the new code. If the problem is
  still there, a new case takes the normal route, so it never loops.

## v0.157.33 — the review packet leaves out other sprints' changes

2026-10-29 04:50: GF-FIX1's review range ran from an old task base and held 25 commits of
UI-SKIN2, HK-CLEAN1 and HK-TEST1; the source diff came to 271 KB, over the 250 KB packet budget,
and the review was refused.

- **The review packet** sends only files the sprint's own (or the coordinator's) commits changed,
  and the commit list without other sprints' commits; the packet notes the files it left out.

## v0.157.32 — a track's merge batch runs the rows its files touch, not the full index

2026-10-29 04:27: the assets-local merge batch ran the full regression (80 rows) and failed on 20
legacy rows (Studio bridge and player-profile harness faults, balance rows moved by S57a) that its
VFX files never touch, which failed the track.

- **The merge Studio batch** runs, for sprints with no suites of their own, the regression rows
  the merged files map to directly (the runner's `merge_regression`), falling back to the
  affected selection; no run when nothing maps. Runners without it still run the full index.

## v0.157.31 — gate.ps1 is not handed files deleted at HEAD

2026-10-29 04:03: UI-SKIN2's 16th attempt failed its host stage because HK-CLEAN1 had deleted
six Kit files that were still in its files under test. v0.157.30 covered the Python hash map;
the host stage runs the game's own tools/gate.ps1.

- **The scripted run's host stage** passes `-Files` without files that do not exist at HEAD.

## v0.157.30 — files a sprint deleted are not hashed by the Studio gate

2026-10-29 03:39: HK-CLEAN1 deleted six Kit files; they stayed in its files under test and
hash_fnv.ps1 failed the whole hash map on them.

- **The gate's hash map** leaves out files that do not exist at HEAD and logs them.

## v0.157.29 — an unmounted check file of a suite the run skips does not block it

2026-10-29 03:20: track backend-1 landed tests/S57a_check.lua without a Studio mount, and the
PRE-PLAY verify of GF-FIX1, HK-TEST1 and UI-SKIN2 stopped with "not mounted anywhere in Studio"
although none of them runs the S57a suite.

- **Scripted Studio runs** note, rather than stop on, an unmounted `tests/*_check.lua` whose suite
  the run does not execute. A run that executes that suite still stops as before.

## v0.157.28 — a track's Studio batch is not starved by held main sprints

2026-10-29 02:05-03:10: the assets-local merge reached its Studio batch and waited on "main X is
READY (about to use Studio)" for sprints the coordinator itself was holding (landing drain,
one-started-sprint rule).

- **Studio admission** skips a main sprint the coordinator is holding back.
- **The landing drain** also covers a track waiting in its `studio_batch` stage.

## v0.157.27 — no more history rewrites; branch landings survive one

2026-10-28/29: commit rewording rewrote the main checkout's history four times in a night (22:20,
22:47, 01:00, 01:57). Every branch cut from master before a rewrite lost its base: the plans
landing failed twice ("patch does not apply" on hours of unrelated work), S57a's track merge
had to be rebuilt.

- **[automation] `reword_commits`** (default off, in the UI): the coordinator no longer rewrites
  commits; an unprefixed commit is an advisory note on `coordinator.commit_messages`.
- **Prep branch landings apply the branch's own commits** (`git cherry`, by patch content), not
  a merge-base diff, so a rewritten base no longer replays unrelated work.

## v0.157.26 — a Lead prompt Kilo never admits is a dead session

AS-P5, 2026-10-29 02:19-02:50: the Lead's repair session got its prompt and no assistant turn
ever followed (Kilo: idle, unanswered); the Lead kept waiting while both prep landings and the
whole main queue waited on that open session.

- **`LeadManager._stall`** treats `unanswered` like a stalled turn: after [limits]
  infra_retry_abort_seconds the session is ended and the call deferred as an infra wait (never
  a counted call), the same as the implementer path's admission timeout.

## v0.157.25 — a screenshot does not go stale when the sprint edits its own check

AS-P5, 2026-10-29 00:33-01:59: every fresh runtime capture (PASS, with runtime identity) was
stale at once, "dependency changed since ...: tests/AS_P5_check.lua": the session ticks the
capture row in its check right after capturing. Eight attempts went on it.

- **AU capture dependencies** are the sprint's UI/src files and default.project.json; its own
  tests/*_check.lua is no longer one (the gate re-runs the check at the reviewed head).

## v0.157.24 — a drained sprint gives the checkout back

2026-10-29 01:28: the landing itself takes the checkout lock, and UI-SKIN2 (between sessions,
held by the drain) still held it: "main checkout locked by animaluprising/UI-SKIN2".

- **While the drain holds a sprint back, it releases its checkout and session locks**; its next
  session start takes them again.

## v0.157.23 — the landing drain cannot deadlock on a between-sessions lock

2026-10-29 01:21: with the drain on (v0.157.22), UI-SKIN2 sat in CHANGES_REQUIRED still holding
the checkout lock; the prep landing waited for that lock and the drain kept UI-SKIN2 from its
next session.

- **Prep safe point:** a checkout lock blocks a landing only when its holder is in a mid-sprint
  state (`block_states`); a holder between sessions does not.

## v0.157.22 — merges and prep landings get the main checkout

2026-10-28 21:50 -> 2026-10-29 01:2x: the assets-local track (5 COMPLETE sprints) and two prep
landings (six enemy plans, S57a's merge) waited the whole night: main sprints ran back to back
and a quiet main checkout never came.

- **Landing drain ([automation] `landing_drain_minutes`, 30, in the UI):** a track merge or ready
  prep entry waiting longer than that stops new main-sprint sessions until it lands; the running
  step finishes first.
- **Prep safe point:** CHANGES_REQUIRED (no live session) no longer counts as mid-sprint, the same
  rule as a track merge; the clean-checkout check still applies.

## v0.157.21 — one started main-checkout sprint at a time

2026-10-28/29: AS-P5 and UI-SKIN2 took turns in the main checkout between sessions. Each one's
commits staled the other's captures and gate, broke its scope check (v0.157.16, .20) and got
reworded (v0.157.19); both cycled for hours.

- **[automation] `main_one_sprint_at_a_time`** (default on, in the UI): once a main-checkout sprint
  has started, other main-checkout sprints wait until it is COMPLETE, BLOCKED, PAUSED or waiting
  on a person; the earliest started goes first. Parallel tracks are not affected.

## v0.157.20 — a double-prefixed commit belongs to its inner sprint

2026-10-29 01:05: the commits reworded to "UI-SKIN2: AS-P5: ..." before v0.157.19 stay in history;
UI-SKIN2's scope check counted them as its own and it asked 10 scope questions about AS-D13,
AS-V0x and AS-P5 files, while AS-P5 lost its own re-shot capture.

- **`bookkeeping.sprint_owner`:** the inner id of a double prefix owns the commit; scope and
  rewording use it.

## v0.157.19 — a sprint's verification never rewords another sprint's commits

2026-10-29 00:3x-00:5x: UI-SKIN2 and AS-P5 shared the main checkout; UI-SKIN2's verification
reworded every unprefixed-looking commit in its range, including AS-P5's, to "UI-SKIN2: AS-P5:
..."; AS-P5 then lost them (v0.157.16 skips other sprints' commits) and its capture never counted.

- **`commits.unprefixed`** treats a commit already prefixed with another sprint's id as exempt
  (never reworded); an unprefixed commit is still the sprint's to reword.

## v0.157.18 — a sprint's own evidence folder is always in scope

AS-P5, 2026-10-29 00:06: its own capture docs/evidence/AS-P5/proxy_telegraph_*.png failed
coordinator.scope; the scope grant then restarted the session.

- **Default scope allow-list:** `docs/evidence/{task}/**` (`sprint_own_evidence`), granted silently
  at verification.

## v0.157.17 — the Lead repairs a track's merge conflict

backend-1 (S57a), 2026-10-28: `tracks/merge.py` asked the Lead through `open_track_repair`,
which the Lead never implemented, so a conflict went straight to a decision whose timeout
default stopped the track and finished work never reached master.

- **`LeadManager.open_track_repair`:** a Lead case (trigger `track_conflict`) for the track's last
  sprint; the coordinator merges master into the track worktree (docs/memory conflicts resolved
  automatically), one bounded Lead session resolves the rest and commits in the worktree only
  (no branch moves, nothing outside it), then the coordinator verifies (merge concluded, no
  markers, clean tree, both heads in HEAD) and the merge retries by itself. Caps, readiness and
  the served-model check apply; a Lead that cannot run leaves the decision.
- **Timeout never drops finished work silently:** while a repair runs the default is "retry the
  merge after the Lead repair"; only with no Lead does "stop this track" apply, and then an
  operator notice names the COMPLETE sprints whose work did not reach master.

## v0.157.16 — scope never blames a sprint for another sprint's commits

AS-P5, 2026-10-28 23:10-23:58: master moved under the long sprint (UI-SKIN2's captures,
coordinator commits for AS-V0x / AS-D13 docs). AS-P5's scope check counted them as AS-P5's,
the session asked for scope grants on other sprints' files, and every grant restarted it
(23:38, 23:47, 23:58).

- **`bookkeeping.agent_files`** leaves out commits whose subject starts with ANOTHER sprint's
  id prefix (`UI-SKIN2: ...`, `S57a: ...`); the project's commit hook only lets a sprint commit
  under its own prefix.

## v0.157.15 — track merges keep both sides of a memory-notes conflict

backend-1 (S57a), 2026-10-28 23:47: S57a was COMPLETE, but the track's rebase onto master
conflicted in docs/memory/CONVENTIONS.md (each side appended a section). `_conflict` asks the
Lead through `open_track_repair`, which the Lead does not implement, so it opened a decision
whose timeout default stopped the track; the work never reached master.

- **Memory-only conflicts are merged:** when every conflicting file is under docs/memory/**, each
  hunk keeps both sides (ours, then theirs) and the rebase/merge continues. Any other
  conflicting file still aborts to the decision.

## v0.157.14 — a stale client Settings page cannot overwrite newer settings

2026-10-28 23:27: a Settings page opened before Blender was switched on was saved (to turn the
relay on) and sent its whole stale copy back: Blender went off with no path and the client
stopped it.

- **The page sends only the fields on its form**, merged onto the current settings.
- **Revision check:** `GET /api/settings` returns `rev`; a save naming an older `rev` gets 409
  "the settings changed since this page was opened; reload".

## v0.157.13 — client restart helper: Windows-only flags only on Windows

- `selfrestart` helper: the detach flags were applied on every platform by operator
  precedence; they are Windows-only.

## v0.157.12 — Restart client button

- **Status page: Restart client.** Starts a fresh Foundry Client (new code and settings) that waits
  for the old one's settings port to free, then the old client exits without stopping its
  components; the new one adopts Kilo, the coordinator, Rojo and Blender, so no session is
  interrupted (`POST /api/client/restart`, `foundry_client/selfrestart.py`).

## v0.157.11 — "Depends on: none; ..." is no dependency

S57a, 2026-10-28 23:06: its plan says `**Depends on:** none; must land before S52's Studio gate`;
the AU parser took S52 as a dependency and the sprint stopped at once on its new track.

- **`deps_in_field`:** "none" means none, and a `; ...` clause is commentary (it also drops false
  dependencies like AS-C20's "...; consumed by UI-SKIN1").

## v0.157.10 — cancelled sprints hidden from the control center's task picker

- **Task picker:** CANCELLED sprints are left out unless "Show cancelled" is ticked (a
  per-browser preference next to the picker; the selected task always stays listed). The
  records are kept: history, reviews and training captures (Victor 2026-10-28 asked to delete
  them; hiding keeps the data). Router rebuild needed (UI only).

## v0.157.9 — a scripted Studio run holds the main checkout

UI-SKIN2, 2026-10-28 22:1x: AS-P5's scripted gate wrote docs/progress/AS-P5_* into the shared main
checkout while UI-SKIN2 was verifying there; UI-SKIN2's `au.done_check.CLEAN` and
`coordinator.clean` failed on another sprint's half-written files, five times, and its repeat
guard handed it to the Lead.

- **The run takes the checkout lock** with Studio/Rojo (and releases them together), so a second
  main sprint waits for the run instead of interleaving with it.

## v0.157.8 — a failed auto-verify Studio run goes to a session, never reruns

AS-P5, 2026-10-28 22:07-22:23: `auto_verify` ran the scripted Studio gate at VERIFYING; on FAIL it
moved the task to CHANGES_REQUIRED without the `verification_id` that transition needs. The
refused transition was swallowed, the task stayed VERIFYING, the run's own output commit moved
HEAD so the gate looked stale, and it ran again: 9 FAIL runs (S24, S26) in 16 minutes.

- **The transition carries `verification_id`** (`studio_calls:<run_id>`), so the failing rows reach
  a session as findings.
- **Same code is never re-run:** when the last automatic run FAILED and only its own outputs
  changed since, verification sends the rows to a session instead of starting another run.

## v0.157.7 — a track's merge Studio-tests its sprints in one batch

2026-10-28: the Blender track (AS-D13, AS-V00..V03, local model, no Studio) would have merged
with no Studio test: the merge gate used Studio only for fast-lane tracks. Victor: build the
assets first, then one batch of Studio tests.

- **New merge stage `studio_batch`.** After the merge gate's verify passes, the coordinator takes
  ONE Studio lease (it waits like the fast-lane merge gate while the main sprint uses Studio)
  and runs the scripted Studio gate on the main checkout: each sprint with its own suites runs
  them, then ONE full regression covers every suite-less sprint (asset sprints). All green ->
  `track_merged`; red -> the track fails with the sprint(s) and rows named and the findings on
  the sprint; a tooling ERROR retries that run only.
- **Settings ([automation], in the UI):** `merge_studio_batch` (on), `merge_studio_batch_max_minutes`
  (0 = `studio_track_max_minutes` x runs). `admit_studio` takes a lease length.

## v0.157.6 — a scope grant gets a fresh session with the granted files

AS-P5, 2026-10-28 19:56: three scope requests (tests/S24, S25, S26_check.lua) were granted and the
same session was told to continue; its edit allow-list (`edit * deny` plus the sprint's files,
fixed when the session starts) still denied them, and it reported BLOCKED.

- **Fresh session after a grant.** When a grant adds files and the project's session rules deny
  edits by default, the session ends and a free continuation starts with the granted files in
  its edit permissions (no attempt charged). Projects without an edit allow-list keep the same
  session.

## v0.157.5 — an unmapped new file goes to the session, not to "stale sync"

UI-SKIN2, 2026-10-28 19:4x: the sprint added tests/UI_SKIN2_check.lua with no
default.project.json mount, so Rojo never synced it; PRE-PLAY verify reported "Studio's loaded
sources are not HEAD's ... Rojo / plugin sync is stale", a tooling ERROR, and the task stopped.

- **`unmapped_unresolved`:** when every UNRESOLVED file has no Rojo mount at HEAD, the run ends
  with a `PROJECT: not mapped in default.project.json` message naming the files and the fix
  (move a check into tests/checks/ or add its mounts); studio_calls sends it to the live
  session instead of blocking. A mapped unresolved file is still real staleness (tooling).

## v0.157.4 — the coordinator's own kilo.jsonc rewrites never fail a session

AS-D13, 2026-10-28 18:5x: `tracks set-needs` rewrote the track worktree's untracked
kilo.jsonc and left `kilo.jsonc.bak-*` beside it; `coordinator.unrelated_preserved` blamed the
session ("pre-existing uncommitted changes were modified") and the repeat guard stopped it.

- **kilo.jsonc and its backups are coordinator-managed** in that check (sessions are denied
  edits to them anyway).

## v0.157.3 — Blender in the client's Settings, and a running Blender is adopted

- **Settings page: Blender section** (run it, blender.exe path, add-on port). It had no UI;
  the path could only be set in client.toml.
- **Adoption without a health check.** A component with no health probe that already listens
  on its port (Blender's add-on, 9876) is adopted as started outside the client instead of
  launching a second copy (v0.157.2 set the owner pid but adoption only ran for health-checked
  components).

## v0.157.2 — client adopts a running Blender, hides retired media; tracks set-needs

- **Blender adopted, never started twice.** The client's Blender component adopts a Blender
  already listening on its add-on port (owner pid by port, like Rojo). Blender has no health
  check: its socket opens only after Connect in the add-on.
- **Retired media hidden.** ACE-Step, Stable Audio, Kokoro and Chatterbox (replaced by Media_Gen
  on 2026-10-27) no longer show as cards on the client's Status page unless switched back on.
- **`tracks set-needs <project> <track> <needs...>`.** Changes a running track's needs and
  rewrites its kilo.jsonc, so its MCP servers follow (assets-local was started with `blender`
  only, so Media_Gen stayed off).

## v0.157.1 — the Lead hands a stop it cannot work to self-repair; tracks never wait on Studio

- **Why the Lead deferred (GF-GATE1, HK-CLOSE1, 2026-10-28).** Its readiness check failed on
  `route_chain` before any call: the last-resort local Qwen route was unloaded, so
  `routes != expected_models`, classed transient, deferred at 5, 10, 20, 40 minutes, and the
  decision sweep treated a DEFERRED case as working and pushed the timeout hours out.
- **Readiness:** with `allow_local_route` on, only a down TAIL of `expected_models` passes
  `route_chain`; a primary route down is still transient.
- **`LeadManager.hand_back`:** a decision-wait case for an open automation decision that cannot
  make its call (readiness, quota/admission, start refusal, infra, already DEFERRED) is cancelled
  and the decision is due now: the capped self-repair applies (different approach, frontier
  implementer, quiet park when the caps are spent). Never for product decisions, a paid session in
  flight, or `self_repair = false`. The strategy text now carries the latest refused submission
  items, the repeated findings and the strategy that failed.
- **Tracks never wait on Studio.** A track worktree's kilo.jsonc turns the Studio MCP off by
  design; readiness reports it for a track session and never blocks (AS-D13).

## v0.157.0 — fewer stops: alerts dismiss and auto-clear, mechanical asks answered, non-sprint deps ignored

From the 2026-10-28 AU run (GF-GATE1, HK-CLOSE1, AS-D13), where the operator was asked things the
coordinator already knew the answer to.

- **Alerts: Dismiss and auto-clear.** Each alert has Dismiss (and "Dismiss all info"); dismissed
  alerts leave the bell and the open list and stay in history (filter "dismissed"). The router
  watchdog resolves alerts whose condition is gone: a forgotten-pause alert once the task is not
  PAUSED, a coordinator error after `alert_error_quiet_hours` (24) without a repeat or when the task
  completes, completion / queue-exhausted info after acknowledgement or `alert_info_hours` (24). Both
  windows and `alert_autoclear` are control-center settings.
- **Regression index count spec auto-granted.** `tests/lune/specs/RegressionIndex.spec.luau` is on
  the default scope allow-list: a new index row always bumps the pinned count (HK-CLOSE1 16:02).
- **A refused phase move never waits on the operator.** "Move BUILD work to CLOSE" from a model
  report stays refused; its obligation_conflict record is answered with the default at once
  (`refusal_default`). Moving an obligation is still possible with an obligation_phase decision
  (GF-GATE1 15:21: three such cards blocked review).
- **Non-sprint ids in a spec's deps are ignored.** AS-D13's deps named "Q1" (a CC0 asset pack);
  the AU adapter's `is_known_task` checks SPRINT_STATUS / MASTER_ORDER / STATE rows, docs/SPRINTS*
  headings and plan files; an unknown id is logged and never blocks.

## v0.156.9 — only live repeats stop a sprint

GF-GATE1, 2026-10-28 15:11 and 15:36: scope and gate-binding findings fixed attempts earlier
(both PASS at the head) still counted as repeats, so a review with two new doc points stopped
the sprint as "findings repeat with no materially different strategy".

- **`Recovery.live_repeats`.** The repeat guard counts a repeat only when it was seen again in
  the latest round of findings and its check does not pass at the head. The finding history is
  kept; a fixed finding that comes back counts again.

## v0.156.8 — the scripted Studio gate's PASS is never "flaky"

GF-GATE1, 2026-10-28 15:21: the coordinator's scripted Studio gate passed (2/2), but the
session's earlier `gate.ps1 -Finish` FAIL (the Studio half it no longer runs itself) on the
same dependencies made the verdict "flaky", and the submission was refused.

- **Same rule as the fast-lane gate (B1).** The newest PASS from the coordinator's own
  revision-bound scripted Studio run (`tool=studio_gate`, `source=coordinator`) is the verdict;
  older FAILs no longer make it flaky. Session runs keep the flaky rule.

## v0.156.7 — landed prep commits never count against a sprint's scope

GF-GATE1, 2026-10-28 15:11: its Studio gate passed, but verification failed scope on
`.clinerules`, `AGENT_RULES.md`, `docs/...`: files the 11 AU prep commits changed when the
prep queue landed them mid-sprint. Scope trusts only the coordinator's own commit list, which
the prep commits were not on, so the repeat guard stopped the sprint.

- **Credited as coordinator commits.** Every queue tick adds each landed prep commit to the
  `coord_commits` list of every open main-checkout task (idempotent), so scope and memory
  hygiene checks never blame a session for them.

## v0.156.6 — no repeat question on a handoff; README.md no longer fails Studio verify

GF-GATE1, 2026-10-28 14:13: after a context-ceiling handoff, the repeat guard opened "Findings
repeat across attempts" and the engine withdrew it in the same second (v0.156.2). The notice
had already reached Victor.

- **No question in the first place.** `Recovery.assignment` takes `free` and skips the repeat
  guard for a free continuation (context handoff, infra retry, operator reset). The engine's
  withdraw step stays as a safety net.
- **Studio verify skips files Rojo never syncs.** GF-GATE1 14:18: PRE-PLAY verify matched
  15/16 code files with none different, but `tests/checks/README.md` (new with the prep
  commits, in a Rojo-mounted folder) was "missing" in Studio, so the run stopped as "stale
  sync". The verify map now leaves out types Rojo does not sync (README.md, .gitkeep,
  *.meta.json): GateRunner never looks for them and `rojo_resolve` gives them no Studio path.
  An AU audit found 8 such files in mounted folders (2 README.md, 6 .gitkeep).

## v0.156.5 — the prep queue's safe point ignores an idle paused session

After v0.156.4 freed paused GF-GATE1's lock, the prep queue still refused: "session
ses_e7c5... (implementer) of GF-GATE1 is open". The store keeps a paused task's session
open so resume can continue it.

- **Same rule as the lock fix.** The safe point now skips the open session of a PAUSED task
  whose session is not busy in Kilo. A busy session, or Kilo unreachable, still blocks.

## v0.156.4 — a paused task with an idle session gives up its locks

GF-GATE1, 2026-10-28 13:19: the task was paused and its session finished its turn, but the
task kept the main-checkout lock. The prep queue waited on "main checkout in use by
animaluprising/GF-GATE1" with 11 AU commits ready to land.

- **Self-heal releases it.** The stale-lock rule now also frees the coordinator locks of a
  PAUSED task whose session is not busy in Kilo (after `self_heal_stale_lock_minutes`).
  Resume takes the locks back before it continues the session, as before. A busy session,
  or Kilo being unreachable, keeps the locks.

## v0.156.3 — an operator fresh budget also refills the Lead's call cap

GF-GATE1, 2026-10-28 12:57: the Lead's hard cap (`lead_hard_max_calls` = 12) counted every
Lead call of the task's whole life. Once GF-GATE1 had used 12, every later escalation went
straight to Victor ("raise lead_hard_max_calls or decide the sprint"), even after his
fresh-budget unblocks.

- **The cap counts from the last fresh budget.** It now counts Lead calls since the last
  `unblock --reset-attempts`, the operator's fresh budget, like attempts, sessions and reviews
  (v0.156.0). A delegated round still never refills it.

## v0.156.2 — a free continuation is not a repeat

GF-GATE1, 2026-10-28:
1. At 11:52 the context guard stopped its local session at 206k tokens (by design), and the
   session had committed its work.
2. At 11:53 the repeat guard (`_lc_before_session`) still asked "findings repeat with no
   materially different strategy".
3. The Lead's calls were used up, so it escalated to Victor, and the sprint stopped.

- **No repeat question on a free continuation.** A free start (a context handoff, an infra
  retry, an operator reset with BUILD progress) no longer opens that question: the
  continuation is the same work carrying on. The question is withdrawn and recorded as
  `repeat_guard_skipped`, and the next session gets the normal continuation prompt.
- **Unchanged:** a paid repeat still asks, and a Lead assignment still crosses the guard as
  before.

## v0.156.1 — the scripted Studio gate runs bridge rows by their runbooks and sets the viewport

GF-GATE1's first live full triage (68 rows): 28 pass, 35 fail, 4 unknown, mostly runner bugs.

- **Bridge rows.**
  - **How they run:** the suite source runs as a function body with `print` captured, so
    print-only scripts (S00–S03) and returned functions (S44) work. A crash keeps the lines
    printed so far and its traceback.
  - **Per-suite exceptions:** S20 runs without the ServiceLoader re-boot (it needs the generated
    map). S33 gets `_G.S33_ABILITY_SOURCE` from HEAD.
  - **Honest UNKNOWN:** suites the runner can't serve become UNKNOWN with a reason, never FAIL.
    That covers `io.*` file access, host runners on a port that isn't listening (S05 :8431,
    S13.6 :8436), and lines that only say the session shape is missing (2 clients, a play
    session).
- **Viewport:** every playtest with client CALLs applies AU's device recipe
  (`set_device_simulator` 1920x1080, ActualResolution), waits 1.2 s and reads it back. A
  mismatch makes that group's client CALLs UNKNOWN, never `FAIL: SIZE`.
- **Luau errors:** a Luau error raised inside a live eval (`ERROR: <script>:<line>:`) is a FAIL
  of that CALL (no retry), not a tool error. The 4 UNKNOWN UI rows were really `ForcePhase` at
  wave 0, which is an AU step-order issue.

## v0.156.0 — self-driving: no watch, no watcher

Victor 2026-10-28: "I shouldn't have to have a watcher on for the automation process to work."
Plan: `reports/SELF_DRIVING_PLAN.md`.

- **Only product decisions can stop a sprint (part 1).**
  - Every decision has a `category`: product, user_action or automation. It is stored on the
    decision, in notices, and in the router's decision rows.
  - An automation decision never times out to PARK. Its default is a capped, audited
    self-repair:
    - **budget:** a `policy` ledger extension, e.g. +1 review for a head that was never reviewed
      after a passed verification. That is today's 06:01 GF-GATE1 case.
    - **repeated findings:** a Lead round with the finding text, then a fresh "different
      approach" session, then one frontier session.
    - **tooling:** a backoff retry.
  - When the daily caps run out, it parks quietly, with a digest entry and the alert class
    `self_repair_exhausted`.
  - Settings: `[automation] self_repair*`.
- **The self-repair sweep inside the coordinator (part 2):** `selfheal.py`, every minute, capped
  per task per day, every action a `self_heal` event. It handles:
  - dead implementer and Lead sessions (seen on two sweeps);
  - Lead cases with no live call;
  - tasks BLOCKED by a bug a newer release fixed (`FIXED_SIGNATURES`), which get a re-verify;
  - stale runner, studio and coordinator locks;
  - expired or ownerless holds;
  - a one-time `pause_forgotten` notice for a pause with no owner;
  - and `unblock --reset-attempts` resets every budget counter.
- **Holds (part 2).** `Coordinator.hold(project, task, owner, reason, minutes)` /
  `release_hold`, and `pause --owner/--minutes`.
- **CLI version guard.** Writing commands refuse when the CLI's code is another version than
  the running coordinator ("run it from C:\Users\shock\foundry-deploy", or `--force`).
- **Repo changes queue (part 3):** `prep_queue.py`, the `prep add|list|remove|move|run-now|status`
  CLI, and Automation → Repo changes in the UI.
  - Patches, branches and scripts land on the main checkout at a safe point.
  - A batch is all or nothing through the project's hooks, followed by one push and the
    after-actions (kilo-config apply, Kilo reload, copy, resume…).
  - Entries can hold tasks until they land.
  - `tools/migrate_au_watcher.py` queues the 11 AU prep commits the bash watcher carried.
  - A hold is placed only on a task the coordinator knows, and released only by its owner.
- **Alerts and digest (parts 4-5):**
  - **Down alert:** the router raises `stack_down` / `stack_recovered` when the coordinator's
    heartbeat is stale for over 10 min.
  - **Webhook:** pushes go to a webhook (ntfy or JSON) only for product decisions, user_action,
    image asks, stack down, self-repair exhausted, prep failed and pause forgotten. Each is a
    toggle.
  - **Daily digest:** at `digest_hour`, also "Build digest now".
  - **Settings:** Monitor → Alerts & digest.
  - **Start at boot:** `foundry_client install-autostart --at-boot` adds a Task Scheduler task at
    startup; Windows asks for the password itself.

## v0.155.2 — the scripted Studio gate fetches long plans in pages

GF-GATE1's scripted full regression triage, 2026-10-28:
- The smoke run of the sprint's own two suites gave GATE PASS live.
- The full run (68 rows, 330 CALLs) refused every row with "Studio's CALL plan does not match
  RegressionIndex at HEAD". Studio's MCP reply is cut at about 100,000 characters, so
  `GateRunner.run` came back with 254 of its 332 CALLs and ended in "... (truncated)".

- **Plans are fetched in pages.** `Gate.plan` requests rows in pages of at most 40 steps and
  renumbers them into the plan `GateRunner.run` / `tally` use for the whole selection: boot 1-2,
  then every step in order. The timing and DYNARG flags stay with their CALLs.
- **A cut-off reply is reported.** A page that comes back without the plan's closing NOTE is
  reported as "output was cut off", never matched as a short plan.

## v0.155.1 — an unblock with a fresh budget also resets the review budget

GF-GATE1, 2026-10-28:
1. It was unblocked with `--reset-attempts` at 04:15, worked on the local model, and passed
   verification at 06:01.
2. It was then refused at its very first review: "review budget used (3/3 review calls, budget
   ledger)". The earlier attempts' reviews still counted.
3. The Lead escalated, and the 30-min decision timeout parked the sprint ("stop the task").

- **`unblock --reset-attempts` now also resets the `review_calls` budget**, not only attempts and
  sessions. It adds an authorized ledger row; the raw count is kept for the audit.

## v0.155.0 — automation audit fixes: fewer loops, free outages, no stops for tooling

Victor 2026-10-28: "do a deep check for automation gaps, time sinks, inefficiencies, blockers,
incompatibilities". Audit: `reports/AUTOMATION_GAP_AUDIT_2026-10-28.md`. GF-GATE1 used 404M
tokens and looped 9 times on one evidence finding.

- **Gate bound to content (A).**
  - `au.gate_bound_to_head` stays PASS when only `docs/progress/**`, `docs/memory/**` or
    `.plan/**` changed since the gate.
  - Runtime evidence isn't staled by those paths.
  - AU's `gate.ps1 -Finish` gets the same rule; it lands as an AU prep commit.
- **Decisions never wait forever on tooling (D):**
  - a `review_access` fault rebuilds and retries the packet, then the alternate reviewer, then
    a Lead case;
  - `review_access` and `scope_request` go to the Lead first;
  - a "human act" wording keeps the timeout unless the blocker is `user_action`;
  - an unmatched recommendation parks with the timeout.
- **Hook-safe coordinator commits (D).**
  - Bookkeeping, Lead, scope-grant and closeout commits pass the adapter's commit env (AU:
    `AU_ALLOW_SCOPE`).
  - stylua runs before a Lead commit.
  - A refused commit unstages its paths.
- **Outages are free (C):**
  - Kilo "retry" for 5 min becomes an infra wait, with no attempt charged.
  - Lead infra failures defer without using the Lead's caps.
  - A dead Lead session is detected.
  - Lead sessions get the context guard.
  - The Lead timeout is 90 min with write access.
  - A served-model mismatch defers.
  - A failed push retries with backoff.
  - The infra signatures cover 429, rate limits, ETIMEDOUT, fetch failures and llama.cpp's
    context error; "500 ms" no longer reads as a 5xx error.
  - Preflight also probes the router.
  - The session timeout wraps up and hands off for free when the session made progress.
  - A compaction loop ends free.
  - `max_task_minutes` counts active states only.
- **Throughput (E):**
  - Review and the track merge gate run off the coordinator tick.
  - Review starts beside Lune once the other checks pass.
  - Check subprocesses run in a kill-on-close job, each run gets its own log, and done_check
    and binding results survive a restart. The heartbeat reports `checks_running`.
  - Kilo polling reads the status first, then only the newest messages.
  - A merge-gate tooling fault retries.
  - A write-access Lead keeps its studio.lock.
  - The profile's `max_agents` wins (2 for Studio-free tracks).
- **Memory notes:** `memory_note_max_chars` defaults to 400 (was 1500): CONVENTIONS and KNOWN_ISSUES had 6 KB lines that every resume re-read.
- **Live tracker:** it also reads the gate's full Lune logs (`tmp/guard/lune/*.log`), since the
  gate now runs Lune `--quiet`.

AU side (prep commits through the safe-point watcher):
- **Gate:** `-Finish` across evidence/memory/plan-only commits; Lune `--quiet` with `--log`; the
  gate runs the suite and the negative control in parallel and puts only a summary in the
  evidence log; gate.ps1 is ASCII-only and reads UTF-8 explicitly.
- **Lune cache:** the key leaves out the `tests/` mounts; `AU_LUNE_CACHE_DIR` shares the cache.
- **New checks:** a `tests/checks/` folder mount, so a new check needs no Rojo restart.
- **Tools:** run.py runs commands directly, streams to the log, and has `--bg` / `--wait`;
  hash_fnv uses one git batch call.
- **Rules:** the contradictions are fixed and evidence is cited by `path:lines` instead of
  pasted.
- **Plans and checks:** `plan_new --set-next` (with a plan history file);
  `done_check --phase build`; outline `--width`; checkpoint counts bytes; lock.py reports stale
  locks; plans drop "stop and ask"; STEP 0 runs in the first session only.

## v0.154.0 — the Studio half of the gate can run as a script (off by default)

Victor 2026-10-27: "plan moving the Studio half of the gate (the CALLs the LLM runs one by one)
to the scripted studio_gate runner, so the LLM only reads the results". Plan:
`reports/STUDIO_GATE_SCRIPTED_PLAN.md`. S17 and other 2-player rows are MANUAL for now.

- **`studio_gate calls`** runs a gate's Studio CALLs with no model:
  - selection: gate.ps1's D3 selection, `subset:` and `-Suites`;
  - checks: PRE/POST-play verifyLive, and HEAD unchanged;
  - DYNARG values read from JSON as well;
  - budgets: 10 min per suite group, 30 / 150 min per run;
  - locks: the runner lock and AU's `studio.lock`.
  - **Outputs:**
    - `docs/progress/<ID>_studio_lines.txt`, written per CALL in the format `-Finish` and the
      live tracker read;
    - `calls.json`;
    - a failures-only summary.
  - `--rerun failed` reuses rows that passed and whose files didn't change. `--dry-run` touches
    nothing; for GF-GATE1 full it gives 69 rows / 331 CALLs, S17 manual.
- **`foundry_coordinator/studio_calls.py`:** a session ends its turn with a `STUDIO_GATE` block,
  or the coordinator starts the run itself (`auto_verify`, or `studio-calls run <project>
  <task>`). It then:
  1. runs host half → CALLs → `gate.ps1 -Finish` as one background pipeline;
  2. writes verified evidence records (runtime identity, run id, lines sha256);
  3. commits the `docs/progress/<ID>_*` outputs;
  4. sends the session one prompt listing only the failing rows.
  While a run is active the session isn't prompted and memory-note commits wait.
- **A Studio lock left by a dead session is released.**
  - GF-GATE1's session took AU's `studio.lock` at 19:37, crashed at 21:45 (context overflow)
    and never released it, so the next sprint's session would have waited forever.
  - Every minute the coordinator releases a `studio.lock` (held over 2 min) when all of these
    hold:
    - its task is one of the project's;
    - the task isn't IMPLEMENTING, CLOSING or AWAITING_HUMAN;
    - no scripted run of that task is active.
  - It only removes the exact line it read, and logs a `studio_lock_released` event.
  - Operator locks, other projects and the `studio_gate` runner's own lock are left alone.
- **Turn it on per task:** `[studio_calls] enabled = true, tasks = ["GF-GATE1"]`. Off by default;
  `enabled = false` restores today's flow. It still needs a live Studio smoke test before the
  pilot.

## v0.153.1 — every training setting in the UI; the live tracker reads AU's per-spec Lune cache

Victor 2026-10-28: "Why is capture on option not in the UI, so I can turn on / off, everything
should be configurable in UI".

- **Router, Logs → Training → Foundry capture.** The card already had the master switch,
  "calls outside a coordinator project", operator chat, days kept and the backend / persona /
  purpose boxes. It now also edits:
  - the body limit (MB);
  - the projects to capture, and the projects never captured;
  - the clients to capture;
  - the e-mail domains left unredacted.
  All of it is live, with no restart and no config.yaml edit.
- **Automation → Settings → Telemetry & training.** The project's `[training]` section was one
  JSON block. Each setting is now its own control:
  - switches: enabled, include reasoning, allow frontier targets, auto run / upload;
  - text: the unbound project (Cline / Chat / AnythingLLM / brain → the call-level dataset);
  - choices: the call-level quality (verified / ok);
  - lists: dataset kinds, export targets, call purposes, sources;
  - numbers: retention days and the eval split.
  The curation `policy` rules stay a raw block. The upload key's env var is
  workstation-only.

- **Reused tests are counted.** AU's Lune runner, landing as an AU prep commit, reuses a spec
  file's PASS when nothing it loaded changed. Those lines end with ` (cached)`, and the summary
  line ends with `(C cached, R run)`. The live tracker counts the reused tests, and the control
  center shows "· N cached" beside the pass/fail counts.
- **`au.lune` / `au.lune_negative` details keep the whole summary line**, e.g.
  `LUNE 1618/1618 PASS (4353 ms) (1588 cached, 30 run)`.
- Includes v0.153.0 (capture every model call for fine-tuning).

## v0.153.0 — capture every model call for fine-tuning the local model

Victor 2026-10-27: "track EVERYTHING so I can use for finetuning the local model". Plan:
`reports/TRAINING_CAPTURE_EVERYTHING_PLAN.md` (approved with the recommended defaults:
`allow_frontier_targets` stays false, operator chat opt-in, media metadata only, 30-day retention).

- **Every call has a purpose** (`capture_records.purpose`; older stores are migrated, and their
  rows count as `agent_turn`):
  - `agent_turn`: a client's turn through the pool (the default).
  - `brain_route`: the brain's own routing / assist / background calls. These were never
    captured before; backend `brain:<provider>`.
  - `frontier_assist`: a frontier helper call inside a Kilo turn. It used to be recorded as one
    of the session's turns and could become the "last call" an episode was built from.
  - `research`: Research's extraction call.
  - `agent_external`: one `agent_run` record per Hermes task (as a backend or as a tool):
    input, final text, tool-progress lines and run id. Hermes' own model calls back into Foundry
    are still captured as that client's agent turns.
  - `media`: one `media_job` record per finished image / tool-media job: prompt, settings and
    artifact ids / sha256 / size. Bytes are never copied.
  - `operator_chat`: the Automation chat, captured only with `operator_chat: true`, whatever
    `include_unbound` says. Internal clients (health probes, persona validation) are still
    never captured.
- **New switches** (Training tab card, live): `purposes` / `exclude_purposes` (globs) and
  `operator_chat`. The card shows today's counts per purpose. `GET /v1/foundry/capture/records`
  takes `purpose=`.
- **Inline images become references.** They are replaced by
  `capture-media:sha256:<hex>:<mime>:<bytes>`, and the bytes are written once to
  `<data>/training/capture-media/`. 74 visual-reviewer calls had been cut at 4 MB by their
  screenshots.
- **Reasoning survives** with `[training.export] include_reasoning = true`.
  - Each episode turn keeps the reasoning its call returned, in its own `reasoning` field.
  - chatml-tools target turns carry `reasoning_content` (Qwen3's `<think>` field), and the
    manifest says `reasoning_included: true`.
  - Off (the default), nothing carries reasoning. Validation follows the manifest.
- **Source fixes for the excluded episodes** (live store: 152 truncated_tool_output, 208
  broken_tool_sequence):
  - Kilo's `metadata.truncated` is the `read` / `grep` tool paging its output ("Showing lines
    1-120 of 304"). All 262 flagged results in captured context were byte-identical to what the
    model saw. It is now `tool_paged`, not a truncation.
  - A session that stopped on a tool call nobody answered (the next call was cancelled, or the
    session ended) is trimmed to the turn before it (`flags.trimmed_open_tail`). Export cuts it
    to its longest valid prefix.
  - Results that are really missing still exclude the episode.
- **Failed attempts are labelled negatives.**
  - The coordinator writes a training manifest when an attempt stops BLOCKED, FAILED or
    CANCELLED. A BLOCKED attempt waits until the lead's recovery case ends. There is one
    manifest per task + run + state (`task.data.training_manifests`), and tasks already
    BLOCKED get one on the first tick.
  - Episodes get `outcome.attempt_end`: the last BLOCKED / FAILED / CANCELLED / COMPLETE /
    AWAITING_HUMAN before the task's next attempt started. A later success does not relabel an
    earlier attempt.
  - Curation excludes these attempts with `attempt_blocked` / `attempt_failed` /
    `attempt_abandoned`. They are kept for eval sets and as rejected sides.
- **Call-level dataset** (`foundry_training export --kind calls`, `[training.auto] kinds`):
  - One example per captured call: the effective prompt, plus the reply as the only target
    turn. The reply may be a tool call.
  - `[training.export] calls_quality = "verified"` (default: a positive coordinator label) or
    `"ok"` (succeeded, not rated down, no negative label). `calls_purposes` defaults to
    `["agent_turn"]`.
  - Local-authored replies only, while `allow_frontier_targets = false`.
  - Every line and the manifest carry `label_strength`.
- **Preference pairs from reviews and gates:**
  - Same request context, preferred by the router's labels on the two calls
    (`judge = "router_label"`).
  - Same task prompt: the approved / gate-passed attempt vs one with review CHANGES, gate FAIL,
    failed verification, or a BLOCKED / FAILED end (`judge = "review"`, stored as `ep:<id>`).
  - Both sides must be local work.
  - Derived pairs are rebuilt on each curate. Human pairs are kept.
- **Unbound traffic reaches the pipeline.** `[training] unbound_project = "foundry-unbound"`
  maps capture rows with no project (Cline, Chat, AnythingLLM, brain) to a pseudo-project that
  feeds only the calls kind. Without the setting, these rows are still skipped.
- **Capture gaps are true.** `no-system-prompt` is recorded only for Kilo sessions that no
  Foundry capture (with the same session id) covers. The check runs after Kilo ingestion and at
  every curate, so a gap is removed once Foundry's rows arrive. On the live store, 144 of 175
  gaps were false; 31 remain.

**To switch it on: router host, `/config/config.yaml`**, then restart the router:
```yaml
training_capture:
  enabled: true
  projects: []              # every coordinator-bound project
  include_unbound: true     # Cline, Chat, Research, AnythingLLM, brain, media
  retention_days: 30
  max_body_bytes: 4000000   # inline images no longer count toward this
  operator_chat: false      # true = also capture the Automation chat
```
**Pipeline side** (the project profile, e.g. AU's `project.toml`):
```toml
[training]
unbound_project = "foundry-unbound"
[training.auto]
kinds = ["sft", "correction", "multimodal", "calls", "preference"]
[training.export]
include_reasoning = true     # optional
calls_quality = "verified"   # or "ok" for the weaker label
```
Storage: today's 13k captured calls take ~22 GB (raw + normalized, ~400 KB per call, because
each Kilo call re-sends its history). Capturing everything grows that, so keep an eye on disk.
Storing each call as a delta of the previous one is a proposed follow-up.

## v0.152.2 — a task's own leftover file is not "someone else's work"

GF-GATE1, 2026-10-27 22:33 and 22:50: the session that died at 21:45 (context overflow)
left its own untracked `docs/progress/GF_GATE1_hashes.json`. The next session started with
that file in the "pre-existing dirty" baseline and regenerated it. Final verification then
failed `coordinator.unrelated_preserved` ("pre-existing uncommitted changes were
modified"). That opened a decision and a Lead case, twice: the Lead's reverify hit the same
check.

- **`unrelated_preserved`:** a baseline file that was not dirty when the task began
  (`task_baseline_dirty`), and that matches the task's scope or exempt paths, is the task's
  own leftover. It is never counted as "modified unrelated work". Work that was dirty before
  the task began is still protected byte for byte.
- **`{task}` patterns** in `[closeout] exempt_paths` also match the id with `_` for `-`.
  AU names test and evidence files `GF_GATE1_*` for sprint GF-GATE1.

## v0.152.1 — a running session is stopped before its context overflows

GF-GATE1, 2026-10-27 21:44: the local-model session went from 207k to 230k tokens in a single
Studio call (a 22k-token result). That passed the 229k usable window, and Kilo's compaction
gave up ("Compaction exhausted"). About two hours of uncommitted Studio triage were lost. The
95% handoff (218k) only applied to an IDLE session, and one tool result can jump past it.

- **Running-session stop line:** min(Red 90%, usable − 24k), so 205k for the local model.
  - The coordinator measures a BUSY implement session's context from its transcript every
    `[limits] context_guard_seconds` (30).
  - Past the line it stops the turn (Kilo abort) and sends ONE prompt: write what it found into
    the plan/evidence files, make a work-in-progress commit, and report NOT DONE with a `next`
    line.
  - When that turn ends, a fresh session continues from the commit. No attempt is charged.
    A DONE or BLOCKED report is handled as usual.
  - A session still running near the window after the prompt (usable − 6k) is stopped outright.
  - The margin is `[limits] context_wrapup_margin_tokens` (24000).
  - Events: `context_wrapup`, then `context_handoff`.
- **The coordinator's own abort is not an error.** The aborted turn's MessageAbortedError no
  longer fails the session.
- **`.plan/tmp/context_window.json` is written only where git ignores it** (AU ignores
  `tmp/`). In any other project it would have been an untracked file and failed the
  clean / unrelated_preserved checks.

## v0.152.0 — live test tracker: "8 of 30 done, 1 failed" while tests run

Victor 2026-10-27: "When it does the sessions for Lua and gate running for the testing, is there
a way to have a status bar, with 8 out of 30 tests completed, pass / fail values, live tracker?"

- **Control center, task header:** a new "Test runs" block. Each run in progress (and each one
  that finished in the last 15 minutes) shows:
  - a progress bar with done / total;
  - ✓ pass and ✗ fail counts, the elapsed time and the state.
  The folded header keeps a one-line count of the running suite.
- **What is counted** (`foundry_coordinator/live_tests.py`, from the runs' own output lines;
  only the bytes added since the last read are read):
  - **The coordinator's Lune runs (VERIFY/CLOSE):** Lune now writes straight to the evidence
    log and a watcher reports the counts every second. While a run is going, its total is
    the last full run's count, shown as `~1609`.
  - **The session's runs through AU's `tools/guard/run.py`** (Lune, gate host steps):
    `tmp/guard/cmd/*.log`.
  - **The Studio half of a gate:** `docs/progress/<ID>_studio_lines.txt`. The bar is CALL
    n of the plan's `-> N CALLs`. AU's SESSION_CORE now says to append each CALL's lines as
    it returns; this AU rule lands with the AU prep commits.
  - A negative control's expected FAIL lines are not counted as failures.
- **Where it shows:**
  - The task view (`task_view.tests`) and the bridge (`live_tests`) carry the rows only while
    the task is IMPLEMENTING, VERIFYING or CLOSING.
  - The heartbeat's background checks carry the counts too, and the activity graph labels
    them: `Lune suite running 812/1609 (2 fail)`.
  - The task note reads, for example, `Lune suite running 812/~1609, 2 fail 5m02s`.
- **Adapters:** `live_test_sources(task_id)` names the files to watch. The generic adapter has
  none; AU sets them, and `[animaluprising] live_test_sources` overrides them.
- Display only: nothing decides on these numbers. Engine writes happen only when a count
  changes, at most every 3 s.
- **Training tab loads again** (Victor: "training is not working?"):
  - **Cause:** episode cards over 60,000 characters were stored as sliced JSON
    (`json.dumps(e)[:60000]`), and `GET /admin/api/training` answered a plain-text 500.
    The UI then showed a blank "failed to load:".
  - **Fix:** `foundry_router/jsonfit.py` (`dumps_fit` shrinks the object until it fits and
    never slices it; `loads_or` never raises), used for episodes, pipeline status and
    dataset manifests.
  - Rows already broken show as stubs ("re-push from the workstation"); no migration needed.
  - An `/admin/api/*` crash now answers JSON naming the exception.
  - The UI always shows a reason and the HTTP status.

## v0.151.0 — rules streamline: one source of truth, no hand-offs to the owner

Victor 2026-10-27: "go through all the rules, make sure we have continuity on all the rules, look
for rules that are deprecated, and look for rules that would cause issues, also try to streamline
everything". Plan: reports/RULES_STREAMLINE_PLAN.md. Audits: 59 AU findings and 53 Foundry
findings. Decision: "Coordinator pushes, always; sessions never push."

- **Push:**
  - The coordinator pushes master after closeout verifies. `publish.mode = "verify_only"` is
    read as `push`, so the live profile works unchanged.
  - Every `git push` is a hard deny for every role and phase. A CLOSE session has no push
    duty, and a track never pushes.
- **One stop protocol:** BLOCKED with `blocker_kind` = `product` (options, a decision),
  `tooling` (no options; auto-heal or the Lead, never a decision for the owner) or
  `user_action` (only what the owner can physically do: a notice, no Lead case, no timeout).
  - A legacy NEEDS_HUMAN report is read as `user_action`.
  - A decision timeout never applies an option that needs a human act (when the decision
    opens and in the sweep).
- **Prompts:**
  - A repair reruns only the affected checks (`-Files` the changed files), never the whole gate.
  - Track sprints get a track-aware prompt: no Studio steps, `done_check --skip-rows
    GATE,BOOT,EVIDENCE`, no push, and their workspace named.
  - The kickoff points to `docs/rules/SESSION_CORE.md`; AGENT_RULES is reference only, no
    longer "applies in full".
  - Same-session continuations send a short pointer instead of the whole report block.
  - Dead text is gone: UI-GT1, UI-0x, PREWORK, the vision-helper requirement,
    `.clinerules`, `-ExecutionPolicy Bypass`.
- **Lead:** one authority text (write mode means it fixes the blocker), and a diagnosis
  that still holds may repeat with a new action.
- **Scope:**
  - `docs/memory/**` is auto-approved (the coordinator's own memory).
  - Every SCOPE_REQUEST answer (granted, denied by rule, by the owner or by timeout) is
    told to the session with its reason.
  - kilo.jsonc, hooks and secrets are denied by rule.
- **Edit scope:** `.plan/tmp/**` is editable (prompts send scratch there); `docs/memory` is
  never editable.
- **Kilo permissions:** robloxstudio-mcp `upload_asset`, `manage_experience`, `import_rbxm`
  and the Studio `publish*` tools are hard denies for every role. The Lead gets the
  implementer's skill map.
- **2D:**
  - The images decision offers three options: "Generate them: ChatGPT first (foundry-media),
    Media_Gen fallback" (recommended), "I will provide the 2D assets myself", and "Hold".
  - `foundry-media` tools and the `foundry-images` skill are gated like Media_Gen's image
    tools.
- **Kilo `instructions`:** `kilo-config` drops KILO_MCP_PROFILES.md and loads SESSION_CORE
  when the workspace has it.
- **Report schema:** gains `next`; the helper prompt names `helper_feedback`;
  COORDINATOR_FORMATS documents the report, MEMORY_NOTE and SCOPE_REQUEST formats.
- **Live AU profile cleaned** (workstation, not in the repo):
  - removed the dead `[lanes]` and UI role overrides, the closed fast-lane tasks and the
    fixed context checkpoints;
  - one vision round; helpers 3/8;
  - `[automation]` preflight endpoints (Rojo, local model), `lead_hard_max_calls = 12` and
    `decision_timeout_minutes = 30`.

The AU side lands as two "prep:" commits (rules phases 1 and 2): SESSION_CORE.md,
done_check `--skip-rows` / memory / clean-tree fixes, checkpoint.py under Kilo, and the
trimmed rules.

Coordinator restart needed; run `kilo-config animaluprising --apply` after the AU phase 2
commit.

## v0.150.3 — a gate run through AU's command runner counts as a gate run

AU sessions run the gate through `py -3 tools/guard/run.py --name ... -- pwsh -File
tools/gate.ps1 ...`, AU's command runner, which logs the output and prints its tail. The verdict
classifier unwrapped pwsh, `cmd /c` and `bash -c` but not this runner. Every GF-GATE1 gate run
was therefore excluded as `read_only_command`, the `build.gate` obligation could never be
verified, and the submission gate sent the sprint back again and again (18:33, attempt 3). The
runner now counts as a wrapper: the command after `--` is what ran. Reading the runner's log
(`--last`, Get-Content of the log) is still a read.

Coordinator restart needed.

## v0.150.2 — the control center shows the coordinator's background checks

Victor 2026-10-27: "how do I know from the control center what is happening right now? I see
it's on verifying, but I don't see any actions". Since v0.147.0 verify and closeout checks run
on worker threads, outside any session, so the activity view said "nothing running" while the
Lune suite ran for minutes.

- **Each check records its progress** (`check_progress.py`): done_check, gate binding, Lune
  suite, Lune negative control; a reused Lune pass shows as "PASS (reused)". It records the
  start, the end and the result.
- **The task note** (the coordinator line in the control center) lists them, for example
  "verify checks running in the background (324 s) at 7111ae8da98a: done_check PASS 41 s ·
  Lune suite running 5m02s".
- **The activity graph:** the coordinator heartbeat carries the running checks (`checks`),
  and the graph shows the Coordinator, then "<task> verify checks", then one node per check.
  Each node is active while running, completed or failed when done.

Coordinator restart (the note) and a router backend rebuild (the graph) needed.

## v0.150.1 — a track's merge no longer waits on untracked files

GF-NAME1 completed and its track was ready to merge, but the merge stage waited for a "clean"
main checkout and counted AU's permanent untracked files (`.kilo/TRIAL.md`, `.kilo/plans/*`,
local Kilo rules) as uncommitted changes. It would have waited forever.

Only tracked files with uncommitted changes block it now. A fast-forward that would overwrite
an untracked file still fails in git and waits, as before.

Coordinator restart needed.

## v0.150.0 — context thresholds follow each model's window; images ChatGPT first, Media_Gen fallback

Victor 2026-10-27: "we are allowing 262k context and output reserve is currently at 32768" and,
for images, "ChatGPT first, Media_Gen fallback".

- **Context thresholds are shares of the session model's usable window.**
  - The usable window is Kilo's `limit.context` minus `limit.output`, read per persona from
    the project's (then the global) `kilo.jsonc` (`context_window.py`).
  - **The coordinator hands off at `[automation] context_ceiling_pct` = 95%:**
    - local Qwen (262,144 / 32,768): about 217.9k, where the old fixed ceiling was 160k
      (GF-GATE1 was handed off three times at 165k–212k);
    - a 200k Claude persona: about 182k.
  - The handoff lane fires at `[lanes.handoff] context_pct` = 90%.
  - The fixed `context_ceiling_tokens` / `context_threshold` apply only when the window is
    unknown.
  - At each implementer session start the coordinator writes
    `<workspace>/.plan/tmp/context_window.json` (usable, amber 75%, red 90%, handoff) for
    AU's `checkpoint.py`. Its AU side lands as a prep commit.
- **Router images: gpt-image first, Media_Gen fallback.**
  - When gpt-image can't make the image (backend down or removed, timeout, 5xx, its quota or
    the image budget used up), the job runs on `media.fallback_server` (`Media_Gen`):
    - `generate_image` (aspect ratio from the size, variations from `n`, a transparent
      background);
    - or `edit_image` (the inputs are uploaded to its `/upload`);
    - then `media_check_job` and the download.
  - A 4xx from gpt-image (a content refusal) is never sent to another model.
  - Library: `media.fallback_library` (personal), or per client in
    `media.fallback_libraries`.
  - The fallback needs the server to be one of the router's MCP servers.

Coordinator restart and a router backend rebuild needed.

## v0.149.0 — speech, sound and 2D on Media_Gen; local ComfyUI retired

Victor 2026-10-27: "Any reference to the old ComfyUI (local) should now go to Media_Gen, any
old TTS, or anything that covers MCP for sound / 2D assets should go to the new Media_Gen
MCP. Blender is still local for right now."

- **`/v1/audio/speech` runs on Media_Gen.**
  - Every model name the endpoint accepted (media-gen, kokoro, chatterbox, tts-1,
    tts-1-hd, gpt-4o-mini-tts) maps to `Media_Gen`.
  - Foundry submits `generate_speech` (`library: personal`, a designed voice), polls
    `media_check_job` until the job is done (`audio.job_timeout_seconds`, 180), and
    downloads the file from the server's own host.
  - The file follows `response_format`: mp3 (default), ogg for opus/ogg, flac for
    wav/flac.
  - `audio.voices` maps a client voice to a registered Media_Gen voice or a
    description. Speed becomes a delivery style.
  - A failed or timed-out job is a 502 in the OpenAI error shape.
  - Servers that answer with a URL (Kokoro, Chatterbox) still work when configured
    (`audio.job_servers` lists the job-based ones).
  - **Router backend:** if its `config.yaml` sets `audio.speech_servers` explicitly,
    remove it (or point it at `Media_Gen`) so these defaults apply.
- **Local ComfyUI retired.** `comfy-mcp` left the service catalog. It is a legacy server
  that tracks always switch off. `images` needs Media_Gen or `foundry-media`, either one.
  `foundry-media` is kept because it is the ChatGPT image path (gpt-image through the
  router). The 2D ask-first hold (v0.148.0) is unchanged.
- **Docs:** CLIENT_COMPAT §6 (speech settings for Open WebUI and AnythingLLM: model
  `media-gen`), COMPATIBILITY, CLIENT_MANAGER (needs table), MEDIA and the config example
  (a Media_Gen server entry and the new audio keys).

Coordinator restart (catalog) and a router backend rebuild (speech) needed.

## v0.148.0 — Media_Gen per sprint; 2D assets ask the operator first

Victor 2026-10-27: "make sure all documents reference the new Media_Gen ... and turn it
on for that sprint ... if there is anything that needs 2D assets, let me know first,
because we are trying to use ChatGPT for that right now."

- **A session sees only the Media_Gen tools its sprint needs.** Every coordinator session
  carries deny rules for the Media_Gen tools it doesn't need:
  - an `audio` sprint keeps `generate_music`, `generate_bgm`, `generate_sfx`,
    `generate_speech`, `generate_dialogue`, `register_voice`, `list_voices` and the job
    tools;
  - a sprint without `audio` (or approved images) gets none;
  - reviewers get none;
  - nobody gets the video tools or the personal-only `generate_song_personal`.

  `service_map.media_gen_denied()`.
- **2D assets ask first.** A sprint that needs `images` waits on an operator decision
  (kind `approval`, never auto-answered) before it starts:
  - "I will provide the 2D assets (ChatGPT)": it runs without Media_Gen's image tools;
  - "Use Media_Gen's image tools": it gets them;
  - "Hold this sprint": it keeps waiting.

  An `operator_notice` announces it. `[automation] media_gen_images`: `ask` (default),
  `allow` or `deny`. The answer is read from the decision itself, so a transition
  can't lose it.
- **Docs:** the AnythingLLM MCP guide, client compatibility, the Foundry Client guide (the
  needs table and a "Media_Gen per sprint" note), MEDIA.md and the config example name
  Media_Gen. The speech endpoint note says `/v1/audio/speech` still maps to kokoro-tts
  until it gets a Media_Gen adapter.
- **Workstation config (not in the repo):** AU's untracked `kilo.jsonc` has
  `"Media_Gen": {"enabled": true}` (backup `C:\Roblox\_scratch\kilo.jsonc.bak-2026-10-27`).
  Kilo loads it on its next restart.

Coordinator restart needed.

## v0.147.1 — Media_Gen replaces the four audio MCP servers

Victor 2026-10-27: one Media-Gen MCP server (TrueNAS `media-mcp`, streamable HTTP at
`http://192.168.0.206:8190/mcp`, 20 tools: music, BGM, SFX, speech, dialogue, voices,
images, video, jobs) replaces acestep-music, stable-audio, kokoro-tts and
chatterbox-tts. The router's aggregator already has it as `Media_Gen`.

- **Service registry:** a sprint that needs `audio` now waits on `Media_Gen` (one server
  instead of all four). `images` is satisfied by Media_Gen, comfy-mcp or the
  foundry-media adapter. "media-gen" and "media_gen" count as audio words in plans.
- **Track Kilo configs:** a track that needs audio or images gets Media_Gen switched on.
  The four replaced servers are always off in tracks. Media_Gen's tools are denied per
  family: an images-only track keeps `generate_image` / `edit_image` /
  `remove_background` / `upscale_image` and is denied the audio tools, and the reverse
  for an audio-only track.
- **Global Kilo config** (workstation, not in the repo): a `Media_Gen` remote entry,
  disabled by default, so sessions that don't need it don't carry its 20 tool
  definitions. The backup is `kilo.jsonc.bak-2026-10-27`.
- The Foundry Client components for the four old servers stay for now (they are off);
  the router's OpenAI speech endpoint still maps to kokoro-tts (see the follow-up).

Coordinator restart needed (services and track configs).

## v0.147.0 — Studio evidence counts; checks never freeze the coordinator

Plan: reports/V0147_STUDIO_EVIDENCE_AND_BACKGROUND_VERIFY_PLAN.md (Victor approved
2026-10-27).

- **Studio boot and suite records now count.** A Studio check counts only when the
  ledger knows what code the runtime had loaded (verifyLive's fingerprint). One Studio
  session prints its boot, suites and verifyLive in separate tool calls, and each call
  named its own runtime, so only the verifyLive runtime had a fingerprint. Boot and
  suite records were "unknown" forever, the submission gate refused review, and AU
  Studio sprints looped into "findings repeat" and the Lead. Only two tasks ever got
  past this gate.

  Now a record without a fingerprint borrows the one from the nearest verifyLive PASS,
  when all of these hold:
  - it is in the same session;
  - it ran against byte-identical dependencies;
  - it is within `[lifecycle] runtime_bind_window_s` (default 1800; 0 turns it off);
  - there is no failed or truncated verifyLive in between;
  - it is on the same place, when both runtimes know it.

  The freshness detail says so ("runtime bound to ..."). A verdict still comes only from
  real tool output, never from a file read.
- **docs/memory is exempt everywhere** (`GenericAdapter.exempt_paths`). Before, a
  coordinator memory-notes commit:
  - still staled the gate block in the review packet;
  - could invalidate a review.

  It already no longer unbound the gate (v0.146.0).
- **Verify and closeout checks run in the background.** done_check, Lune and the
  project suites used to hold the tick for up to ~15 min, so nothing else could start,
  decide or recover (GF-NAME1 sat READY while GF-GATE1 verified).
  - They now run on worker threads. `[automation] max_parallel_checks` (default 2)
    caps them, because Lune is memory-heavy.
  - A tick waits `inline_check_wait_s` (default 2) for a quick result, then moves on;
    the task's note shows "checks running in the background (N s)".
  - A run at an older head, or for a task that left the state, is dropped.
  - A run that raised is one required UNKNOWN row.
  - The heartbeat lists running checks, and the client status shows them
    ("busy: checks: GF-GATE1 verify 312 s").
  - After a restart the checks run again. Lune passes are reused by input content.

Coordinator restart needed.

## v0.146.2 — a track that merged master can land; tests need no network

- **Track merge keeps merge commits.** The merge stage ran `git rebase <master>` in the
  track's worktree. A track that had already merged master (GF-NAME1 merged it to pick
  up GF-GATE1's work) lost its merge commits and its edits were replayed over master,
  conflicting by construction; the Lead diagnosed it (case rc-bb1526ee). Now: master
  already in the track -> nothing to integrate, straight to the fast-forward; the track
  has merge commits -> master is merged in; otherwise the rebase as before. The
  `track_rebased` event says which (`how`).
- **Tests need no network.** A lookup of a name that isn't local fails at once (router
  tests upserted MCP servers like `http://x/mcp`, about 2.7 s each on Windows), and the
  "nothing listens here" endpoint `127.0.0.1:9` fails at once instead of after 2 s of
  SYN retries. `FOUNDRY_TEST_NETWORK=1` turns the guard off.

Coordinator restart needed.

## v0.146.1 — track plan analysis 11x faster

`tracks analyze` / `tracks propose` compared every pair of open sprints' "may change"
entries, normalizing and regex-checking each path on every comparison and scanning the
whole serial list for every pair. On AU's 178 plans that took 21 s (35–60 s under load).
Each entry is now normalized once and the pair check is a set lookup: 1.9 s, with a
byte-identical result on all 178 plans. Docs: why the tests use `--dist worksteal`.

Coordinator restart needed (rides with v0.146.0's pending restart).

## v0.146.0 — faster verify and tests; three more stops that weren't the sprint's

Victor 2026-10-27: "make EVERYTHING faster ... use all the cores ... keep quality high",
and find the automation gaps.

**Speed**

- **AU verify runs its checks side by side.** done_check, the gate binding, the Lune
  suite and its negative control now run concurrently instead of one after another; rows
  keep their order and a timeout is still UNKNOWN.
- **Lune passes are carried by content.** A Lune PASS is reused at a revision whose Lune
  inputs are identical: src, tests, the project file and every `$path` in it (Packages by
  disk content), wally/rokit files, every path a Lune script names, the Lune version and
  the mode. Only a PASS is stored (`<state_dir>/cache/lune/`), only for a clean HEAD.
  `[animaluprising] lune_reuse` (default true) and `lune_workers` (AU_LUNE_WORKERS for
  the parallel run.luau; the suite gets n-1, the negative control 1).
- **pytest uses all cores by default.** `-n auto` (pytest-xdist, now a dev dependency):
  one worker per physical core, capped by free memory; one named test still runs
  serially, `-n 0` turns it off. The full suite (3259 tests) runs in about 13 min on
  the workstation while sprints run (the coordinator tests alone: about 42 min serial,
  about 9 min parallel). Tests made parallel-safe: no test reads the machine's global Kilo
  config or probes live ports (track tests passed only while Blender was open), the
  chat-engine fixture stopped sleeping 20 s per client action, and shared temp roots
  became per-test. See docs/CONVENTIONS.md, "Tests run in parallel".

**Stops fixed (AU, 2026-10-27)**

- **The coordinator's own memory commit unbound a passed gate.** GF-GATE1's gate passed
  at the session's commit; the coordinator then committed its memory notes and verified
  that commit, and `au.gate_bound_to_head` failed because "files changed since the gate".
  `docs/memory/` (project memory, not code) may now change after the gate.
- **A track sprint was asked for Studio evidence.** GF-NAME1 (a track, no Studio) passed
  every check and was still refused review for missing `suite:`, `gate:` and `boot:`
  evidence and the gate log, until "findings repeat" parked it. The submission gate of a
  track sprint leaves the Studio evidence to the merge gate (which runs it on the merge
  result in the main checkout), and the reviewer is told the same.
- **Reword noise on tracks.** A track that merged master listed master's commits (other
  sprints') as its own unprefixed commits on every verify (`commits_not_reworded`). Only
  the track's own commits are checked now.

Coordinator restart needed. The parallel AU Lune runner (tests/lune/run.luau, about
904 s serial to about 340 s on 12 workers, identical results) is an AU-side change and
lands as a separate "prep:" commit in the AU repo.

## v0.145.2 — no more parks mid-repair; a repair with nothing left to fix passes

Two stops from the AU test sprint (2026-10-27) that needed Victor:

- **A timeout default fired while the lead was still working.** "Findings repeat"
  escalations carry a 30-minute timeout whose default is "park the sprint". The lead case
  opened for that same stop was still repairing when the timeout fired, so GF-NAME1
  (09:51) and GF-GATE1 (10:20) were parked mid-repair and the case cancelled. The sweep now
  moves the deadline to the lead case's own bound while the case is diagnosing, repairing,
  upgrading, resuming or deferred (`decision_timeout_deferred`); once the lead gives up
  (NEEDS_VICTOR) or the case closes, the default applies as before.
- **Stale findings after an unpark.** An unparked sprint kept its old findings, so its next
  session was a "repair"; with nothing left to fix it committed nothing and failed
  `coordinator.repair_changed` alone, which went straight to "findings repeat". When every
  other required check passes at the head, that check passes ("nothing left to repair").
  Findings from a code review still need a change (they are not in the check results).

Coordinator restart needed.

## v0.145.1 — verify stops failing on project memory, merged tracks and slow Lune

Three verify failures that parked or re-looped the AU sprints on 2026-10-27, none of
them the sprint's code:

- **docs/memory is the coordinator's.** `coordinator.scope` and AU's done_check SCOPE
  still failed GF-GATE1 on `docs/memory` files, because its session had reverted the
  coordinator's memory commits to get past the older SCOPE bug. Project memory is now
  exempt from both scope checks; a session's own edit there is reported as the advisory
  `coordinator.memory_direct_edit` (notes belong in the report's memory_notes).
- **A track that merged the main branch is measured from that merge.** GF-NAME1 merged
  master into its track to pick up GF-GATE1's work, and its scope and scope-grant checks
  then blamed it for ~30 files master had changed. A track sprint's base is now the
  newest main-branch commit it contains (`TrackManager.effective_base`), so only the
  track's own changes count.
- **Lune timeout.** One `lune run tests/lune/run.luau` (1588 specs) takes ~12 min on the
  workstation with sessions running beside it; the 600 s limit made the required `au.lune`
  check UNKNOWN on every verify. Now 30 min per run (`[animaluprising] lune_timeout_s`),
  and the verify heartbeat cover is 90 min.

Coordinator restart needed.

## v0.145.0 — Studio auto-reopen (TEST place, opt-in); track sprints never need Studio

**Studio auto-reopen.** The Studio Rojo plugin reconnects only when a place is opened,
so after Rojo or Studio restarted a sprint waited for the owner to click Plugins → Rojo →
Connect. Now, when a session or gate stops on a lost Rojo connection (new auto-heal
signature `rojo_connect`: "Rojo → Connect", "owner click", "plugin not connected") and
Rojo itself is serving, auto-heal asks the Foundry Client to close the TEST place's
Studio window and open the TEST place again; Rojo's Auto Reconnect does the rest. The
retry waits for the client job (`studio_reopened` / `studio_reopen_failed` events).

Off by default (`[studio] auto_reopen` in client.toml) and narrow:
- only the window whose title contains `title_match` is touched (any LIVE or other
  Studio window is left alone); the place opened is always `place_id` / `universe_id`,
  never one in `never_place_ids`
- a polite close (WM_CLOSE), never a kill: if Studio asks to save or publish, the
  question is cancelled, Studio stays open, and the reopen fails with the dialog's title
- never while another sprint holds the coordinator's Studio lease, a gate runner is live,
  or the project says Studio is busy (AU: `studio.lock` held by another sprint)
- waits for 2 idle minutes (at most 10, then goes ahead with an alert); 2 per hour
- `POST /api/studio/reopen {"dry_run": true}` reports what it would do; Doctor shows it

**Track sprints never need Studio.** The AU adapter's track verify still asked done_check
for GATE / BOOT / EVIDENCE, which only a Studio gate can satisfy, so a track sprint failed
them, its session tried to run the gate, hit the main sprint's Studio lock and went
BLOCKED (GF-NAME1, 2026-10-27). Track verify and closeout now leave those rows to the
merge gate (which already runs Studio under a lease), every track implementer prompt
says so, and a track session's BLOCKED report about Studio, Rojo or studio.lock goes to
the Studio-free verify instead of a decision.

Coordinator restart and full client restart needed. To turn the reopen on, set
`[studio]` in client.toml (see docs) and do the supervised first run.

## v0.144.3 — automation runs unattended through long verifies, stale questions and Rojo

Seven places where the AU test sprint (2026-10-27) stopped and waited for Victor:

- **Long verify, restart loop.** A verify runs the project's suites inside one tick (AU:
  Lune twice, minutes each) and wrote no heartbeat meanwhile, so the Foundry Client took
  the busy coordinator for hung and restarted it every ~6.5 min, killing the verify each
  time. Verify and closeout checks now run under `keepalive.busy` (ceiling 1 h): a
  background thread refreshes the heartbeat and names the step ("busy: verify …" in the
  client). A tick that hangs outside a declared step still goes stale and is restarted.
- **First heartbeat at start.** A new coordinator beats before its first tick, so the
  client never sees only a dead pid's heartbeat and starts a second coordinator.
- **Closed lead case left its questions open.** Closing or cancelling a case withdrew
  only its escalation; its `lead_test_change` approvals (anything blocking on
  `lead_case:<id>`) stayed open with nothing behind them. All go with the case now.
- **Separate questions were "contradictions".** Answering a newer question of the same
  kind on a task raised DecisionConflict, so neither the lead nor the timeout default
  could answer it. `lead_test_change` approvals now accumulate and a newer
  `implementer_blocked` answer supersedes the older one; state-like kinds (budgets,
  escalations) keep the guard.
- **Coordinator memory commits failed AU SCOPE.** The coordinator's own `docs/memory`
  note commits carry the sprint prefix, and done_check's SCOPE counted them. When those
  are the only out-of-scope files, SCOPE (and the closeout verdict, if it was the only
  FAIL) passes.
- **max_agents fell back to 1.** Re-applying the same router revision replaced the
  profile tables and dropped a chat/CLI-set `max_agents`, so a track sprint waited for
  a writer slot forever. The stored value is restored after every apply unless the
  revision itself changes `max_agents`. The writer-slot note now shows the live cap and
  says when a track needs `max_agents` 2 or more.
- **Auto-heal restarted a running Rojo.** A new Rojo session needs a Connect click in
  Studio (the plugin reconnects only on place open). A serving Rojo is now left alone;
  only a Rojo that is down is restarted.

Foundry Client: `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n` are no
longer flagged as secrets (every settings save failed with them set).

Coordinator restart needed; full client restart for the client part.

## v0.144.2 — coordinator: phase labels are not sprint dependencies; no flashing windows

A plan whose `Depends on:` line named a MASTER_ORDER phase ("the A0 entry gate",
"after A2", "D1") made the coordinator wait for a sprint called A0 and BLOCK the task;
GF-GATE1 hit it on 2026-10-27. The AU adapter and the track analysis now drop
phase-shaped ids (A0–F99, optional letter) unless a sprint plan by that name exists
(`tracks.analysis.drop_phase_labels`). Affected plans: GF-GATE1, AS-REG1, GF-SAVE1, MV2-V1,
AS-G04, AS-G05. Coordinator restart needed.

Foundry Client (source install on Windows): components were started from `pythonw.exe`,
so every `git` the coordinator ran on each tick opened its own console window that
flashed and closed. Children now start from `python.exe` beside it with
CREATE_NO_WINDOW: one hidden console that their own children reuse
(`components.console_python`). The tray app itself still starts windowless at logon.
Full client restart needed (tray Quit, then start).

## v0.144.1 — coordinator: robloxstudio-mcp pinned to 3.4.2

The community Studio MCP server must match its Studio plugin exactly (HTTP 426
`plugin_version_mismatch` otherwise). The plugin has been 3.4.2 since 2026-10-25; the
coordinator still launched 3.2.1, so its Studio gate could not connect
(`foundry_coordinator/mcp_stdio.py`, docs/STUDIO_GATE.md). Coordinator restart needed.

## v0.144.0 — Client compatibility: mixed tool turns, Think toggle, images, embedders, speech

The smaller findings of the 2026-10-24 client compatibility audit
(reports/CLIENT_COMPATIBILITY_AUDIT_2026-10-24.md: A4, C2, C5, C4, B4).

- **Mixed tool turns keep the client's calls (A4).** When the model called a
  persona MCP tool and a client tool in the same turn, the client's call was
  dropped for that round. Now Foundry runs its own call, hands the client's
  back in the same reply (no extra model round), and saves its own call and
  result (`kv` table, `mixed_round:*`, 7 days). On later requests that
  history turn is rebuilt with every call in the model's order and each one
  paired with its result (OpenAI by `tool_call_id`, Ollama by order and tool
  name). The rebuilt prompt is the same on every later turn, so the prompt
  cache holds. With nothing saved (expired), the history passes through as
  before. At the round cap the persona call is answered "NOT RUN"; pre-judge
  and the loop-breaker still check the client calls of a mixed turn.
  It only happens where persona MCP tools ride along with client tools (the
  v0.142.0 "MCP tools with client tools" setting). A slow persona tool keeps
  the stream alive the same way as a plain tool round. Buffered and streamed,
  both dialects (tests/test_mixed_tool_turns.py).
- **Think toggle (C2).** Personas now advertise `thinking` on `/api/show`, and
  `/v1/models` `capabilities.reasoning`, when a model they can reach really
  reasons. It is the same check that decides whether Foundry sends a think
  setting, so Strata's qwen3.8 (llama.cpp, which declares no `thinking`) and
  Claude routes count. Open WebUI now shows its Think switch on
  Foundry-Chat / Agent / Coding / Kilo-Local. A persona with forced reasoning
  effort keeps its own level.
- **Pasted images on the direct path (C5).** When a request carries an image
  (any turn, so a chat doesn't bounce between models) and the chosen model is
  known not to see images (probed capabilities without vision), Foundry
  switches to the first vision model on the persona's own route: a strict
  persona's chain, else its ranked candidates within its allowlist, with paid
  models still gated by the guardrail. If there is none, the client gets a
  clear "can't read the attached image" answer instead of a blind call.
  Failover for that request keeps to models that can see images. Models
  whose capabilities were never probed (vLLM, CLIProxyAPI) are trusted as
  before; Claude routes count as vision.
- **Embedding models discoverable (C4).** `/api/tags` and `/v1/models` also
  list reachable, enabled embedding models under their real names (Ollama
  capability `embedding`), and `/api/show` on a llama.cpp / vLLM embedder says
  `embedding`. AnythingLLM and Open WebUI embedder pickers now show them.
  Switch: `server.list_embedding_models` (default on).
- **Speech API (B4).** New `POST /v1/audio/speech` in the OpenAI shape, the one
  Open WebUI ("OpenAI" TTS engine) and AnythingLLM ("OpenAI compatible") use.
  `model` kokoro / chatterbox (tts-1, tts-1-hd and gpt-4o-mini-tts mean
  kokoro) runs that MCP server's `generate_speech` tool. Foundry downloads
  the audio URL it returns, only from the MCP server's own host or
  `audio.fetch_hosts`, and returns the WAV. Kokoro voice ids pass through;
  OpenAI voice names become `af_heart`. It needs the `media` scope.
  **No speech to text**: `/v1/audio/transcriptions` and `/translations`
  answer 501 `no_stt_backend`, because no STT backend exists yet. Setup:
  docs/CLIENT_COMPAT.md §6.

Config: new `audio` section, new `server.list_embedding_models`. No schema
change (mixed rounds use the existing `kv` table). API: `/v1/audio/*` (new,
`media` scope), embedding models in the model lists.

## v0.143.0 — Images and audio from persona MCP tools reach every chat client

Victor's goal: ask any chat (AnythingLLM, Open WebUI, Kilo, Cline) for an
image or a sound and get it back without opening ComfyUI. Before this, a
persona tool's image/audio became a text note only the model saw (audit
2026-10-24 §B). Full detail: docs/MEDIA.md, "Images and audio from persona MCP
tools".

- **Stored.** Image/audio blocks, image/audio resource blobs, and media URLs in
  a tool's text reply (the live Kokoro, Chatterbox, Stable Audio and ACE-Step
  servers return `http://<their host>/file.wav`) are saved in the media store
  (`media_jobs.op = tool`) with a SHA-256. URLs are fetched only from the MCP
  server's own host (plus `media.tool_fetch_hosts`), with redirects off. Per-file
  and total size caps. Tool files never count against the image budget.
- **Signed links** `GET /media/a/<token>`: an HMAC-signed, expiring
  (`media.public_link_ttl_hours`, 7 days) link to one file. A browser can load it
  without a key. This is the one deliberate unauthenticated read
  (`client_auth.PUBLIC_MEDIA_PREFIX`). Failures are a bare 404, there's no listing,
  and responses have the right content type, `nosniff` and Range support.
- **In the reply.** After the model's text: `![<tool> image](link)` for images
  and `🔊 [<tool> audio (wav, 1.2 MB)](link)` for audio (optional
  `<audio controls>`, `media.audio_html`). This covers the brain path, the worker
  tool loop and the direct path, Ollama and OpenAI shapes, buffered and
  streaming. Tool calls handed to the client are untouched. JSON-format requests
  and compaction summaries get no links. The model sees the text note, a line
  saying the files are already shown, and the image itself if it has vision.
  Answers with links skip the semantic cache. Foundry-MCP (`/mcp`) keeps the real
  image/audio content and adds a text block with the link.
- **Long-running tools.** New per-server `long_running` setting (config, and a
  checkbox in the MCP server editor). A chat call runs as a background tool job
  (`op = tool_job`). The request waits up to `media.tool_inline_wait_seconds`
  (90), streaming progress, then hands the model a job id. The Foundry-owned
  `tool_job_status` tool (offered only to personas with such a server) long-polls
  and never resubmits. The same call from the same client returns the running
  job. Restarts mark running jobs interrupted.
- **Direct streaming keeps flowing while persona tools run.** Progress lines and
  keep-alive frames are sent during the tool call, instead of silence until it
  returns.

Config: new `media.tool_outputs`, `tool_output_max_mb` (100), `tool_output_max_total_gb` (5),
`tool_fetch_urls`, `tool_fetch_hosts`, `public_link_ttl_hours` (168), `public_base_url`,
`audio_html`, `tool_inline_wait_seconds` (90); `mcp_servers[].long_running`.
Schema: `media_jobs.result_text` (additive). No deploy-time steps. To use it,
mark Stable Audio (and ComfyUI once connected) `long_running`.
## v0.142.0 — Shorter, visible waits in AnythingLLM agent mode

Victor's ask: AnythingLLM's @agent mode on Foundry-Chat sat silent for long
stretches between tool steps. Measured on the live router (2026-10-24,
Strata, last ~25 Foundry-Chat calls): most of each step is the model
thinking (9k–30k tokens, 90–320 s, streamed into AnythingLLM's "Thought for…"
panel). The silent part was the prompt read: ~28 s cold on the first step of
a new chat, mostly the 58 persona MCP tool definitions merged next to
AnythingLLM's own tools, and ~18 s whenever a large file the model wrote came
back as prompt. Nothing was shown during either.

- **MCP tools with client tools**: when a request brings its own tools
  (AnythingLLM @agent, Cline, Kilo, Open WebUI native tools), which persona
  MCP tools ride along is now a setting, so a client without the right tools
  still gets Foundry's (image / sound generation, search) and one with its
  own toolbox isn't slowed by duplicates:
  - `all` (default, as before) · `off` · `dedupe` (all except tools that
    duplicate a client tool by name or by purpose: web search, page fetch /
    crawl, files, shell, SQL, image / speech / music generation) · a list of
    MCP server/tool names (narrows the persona's grants, never widens them).
  - Set on the **client key** (Clients & keys → Edit, or the operator chat's
    `set_client_tool_policy`), for **keyless clients** (Clients & keys →
    Access; an AnythingLLM with no API key is keyless), or on the **persona**
    (Personas tab, `set_persona_fields`). The client's setting wins; then the
    persona's; then `all`.
  - Fixed per client and persona, never chosen per message, so the tool list
    and the cached prompt prefix stay the same from step to step.
  - Requests without tools of their own (plain chat) still get every
    attached tool; `mcp_tools_in_direct` stays the master switch.
  - The thinking panel says what was left out and why ("🔧 2 persona MCP
    tool(s) available alongside 9 client tool(s) (56 left out — client
    anythingllm: only acestep-music, …)") and the request log records it.
  - Suggested: Cline / Kilo `off` (no persona of theirs has MCP tools today
    anyway), AnythingLLM `acestep-music, stable-audio, kokoro-tts,
    chatterbox-tts, searxng` (~3.3k tokens instead of ~13.9k; add ComfyUI's
    server when it's connected), Open WebUI `all`.
- **Prompt-reading progress**: before a model call's first token, the
  llama.cpp / Strata `prompt_progress` Foundry already collected is now shown:
  `⏳ reading prompt… 45% (8.2k / 18.0k new tokens) · 7s` — the first report,
  then each 25 % step, at most 4 lines a call; prompts with under 4k tokens
  to read show none. Before, it only appeared on the 30 s / 60 s keep-alive
  lines. `agent_brain.prefill_progress_lines` (default on) turns it off.
- **Earlier first status line**: a call that has produced nothing and has no
  progress report (queued behind Strata's single slot, or a backend that
  can't report progress) shows its first "still working" line at 10 s
  instead of 30 s (`agent_brain.heartbeat_first_visible_seconds`, 0 = 30 s).
- Status lines only: the prompt, think level, context guard and tool list of
  a Cline turn are untouched, and the new lines start with ⏳ / 🔧 so
  `scrub_router_lines` strips them from echoed history exactly as before
  (tested against v0.141's scrub result, so the next step's prompt cache
  still matches). Quiet (data-generation) personas show none of them.

Measured 2026-10-24 on Strata (qwen3.8-flash-next IQ3_XXS), the same cold
AnythingLLM-shaped step (`/api/chat`, Foundry-Chat, stream, think on, one
client tool), twice each:

| | prompt tokens | first model token |
|---|---|---|
| v0.141.1 (58 MCP tools merged) | 13,745 / 13,747 | 13.4 s / 13.3 s |
| v0.142.0, set to `off` | 370 / 369 | 2.4 s / 2.4 s |

A 24k-token cold read on v0.142.0 showed `⏳ reading prompt… 34% (8.2k /
24.1k new tokens)` at 7 s and `68%` at 11 s, first token at 17.8 s; before,
nothing appeared until the 30 s keep-alive line.

Migration: new nullable column `personas.mcp_with_client_tools` (added on
start) and two optional config keys, `clients[].mcp_with_client_tools` and
`client_auth.keyless_mcp_with_client_tools`. Unset everywhere means `all`:
nothing changes until a setting is made. Changing the setting for a client
that is mid-chat costs that chat one prompt re-read on its next step.

Not in this release (from reports/CLIENT_COMPATIBILITY_AUDIT_2026-10-24.md):
images / audio returned by a persona MCP tool still reach the model as a
text placeholder, not the user (B1/B2); that hand-back is the next step for
ComfyUI / sound in chat.

## v0.141.1 — Coordinator version label, loud migrations, no dead-coordinator adoption

Three small defects found while operating v0.141.0 (2026-10-23/24):

- **Coordinator version label**: `foundry_coordinator.__version__` had stayed
  at 0.138.0 since v0.138.1 bumped only the router (the bridge heartbeat's
  `coordinator_version` and `foundry_coordinator status` reported it). It now
  matches the release, and `tests/test_version_parity.py` fails when the
  router, coordinator and client versions disagree, or when the newest
  release-notes entry is not the current version.
- **Swallowed migration errors**: the additive `ALTER TABLE ... ADD COLUMN`
  migrations for `helper_units` (helpers.py) and `source_snapshots`
  (artifacts.py) ignored every exception. They now share
  `store.add_column`, which ignores only "duplicate column name" and raises
  anything else (a locked database, a bad definition), so a half-migrated
  table fails at startup instead of later as "no such column: phase" (the
  one-off failure in test_lead_full_authority seen during the v0.138.1
  triage). The other coordinator migrations (store.py, evidence.py,
  obligations.py) already checked `PRAGMA table_info` first and were left
  as they are.
- **Client adopted a dead coordinator**: coordinator health was heartbeat
  age < 90 s alone, so after a force-stop the restarted client took the dead
  coordinator's last heartbeat for one "started outside the client" and did
  not launch a coordinator for up to 90 s. Health now also needs the
  heartbeat's pid to be alive; a heartbeat whose process has ended counts as
  not running and the client starts a fresh coordinator at once. A heartbeat
  without a pid is still judged by age alone.

No schema, API or config change.

## v0.141.0 — Brain metrics, who uses the router, which model answered

Victor's ask: track the brain's performance, see live which clients use the
router (named by key, else "untracked" with the IP), the path each request
takes including the brain, and see in AnythingLLM / Open WebUI which backend
answered, like Cline shows.

- **Brain metrics**: every call to the routing brain is a `brain_calls` row
  (brain/client.py, its one choke point): purpose (routing / assist /
  background), latency, tokens, tok/s, outcome (ok / retried / failed /
  skipped + reason; failed and skipped = the request fell back to the simple
  rule), request id, persona, client and IP. `GET /admin/api/brain/metrics`;
  Performance → Routing brain panel (stats, calls and latency charts, by
  purpose, top failure reasons). A log table (Data tab: prune / clear).
- **Caller identity**: the auth middleware keeps the caller's IP
  (x-forwarded-for / x-real-ip / socket) and user agent on the request;
  `request_log.client_addr` / `user_agent` (perf samples and ledger rows join by
  request id). A request without a key shows as **untracked · <IP>**, one entry
  per IP, everywhere clients are listed.
- **Active use** (Monitor → Active Use; `GET /admin/api/clients/activity`,
  `GET /admin/api/live`): per client key or untracked IP — requests, errors,
  tokens, median time, a traffic sparkline, last seen, IPs and app, personas
  and models — plus what is running right now (backend and brain calls with
  who asked), refreshed every 10 s; "Make a key" for untracked programs.
- **Route map** (Routing → Routes): untracked callers split by IP, the routing
  brain drawn on the path (amber) for personas whose requests used it, calls
  running now animated (green), 10 min / 30 min / 2 h windows and an
  **active only (live)** mode with no configuration, refreshed every 5 s.
  Edge drill-down works for untracked labels and brain edges; request traces
  include the brain calls. In-flight pool calls now carry client, IP and
  persona.
- **Which model answered** (opt-in): a `route_footer` switch per persona
  (Personas tab) and per client key (Clients & Keys → Edit) adds
  "— via <model> @ <backend> (local/Claude/ChatGPT) · routed by brain <model> ·
  N model calls" under answers and the route line to buffered replies'
  reasoning. Never on tool-call-only turns, JSON output, compaction summaries or
  quiet personas. All direct reply paths and the brain path (incl. its static
  fallback). docs/CLIENT_COMPAT.md: keys, footer, brain-or-not, long research.
- Tests: tests/test_brain_and_clients.py (16: labels, IP + agent capture and
  per-IP grouping, graph untracked/brain/live/active-only and drill-downs,
  brain rows for every outcome and the metrics endpoint, in-flight brain calls,
  footer rules per persona / per client / brain path, the Setup switch, and the
  AnythingLLM / Open WebUI request-shape matrix streamed and buffered).

## v0.140.0 — Operator chat runs the whole router; one rule set for personas

Victor's ask: tell the chat "make Foundry-Chat use only local and Claude", "128K
context", "turn off its MCP tools", "disable the GPT backend", "research the new
models", "how is performance?", and have the chat's own model not depend on any
persona. Plan approved 2026-10-24 (decisions A-I).

- **Personas from the chat** (`chat_router_tools.py`, confirm class `persona`):
  `set_persona_routes` (rewritten) takes model ids, `model@backend`, a backend
  name, or the family words `local` / `Claude` / `ChatGPT` (`GPT`), which keep
  the persona's current routes of that family; the card shows the concrete
  `model@backend` and the routes before. A strict persona tied to a backend that
  can't serve a new route is refused (it would never be used); `backend=""`
  unties it. `set_persona_fields` (context window, max output, reasoning
  effort, local bias, routing, backend, description, MCP grants with
  `mcp_add` / `mcp_remove`, fallback policy, ...), `create_persona` (copy or
  `strict-chat` / `flexible-chat` template, starts disabled, names the client
  keys whose allow-list must add it), `grant_persona_to_client`,
  `run_persona_evals`. Every persona write reminds you to run its evals.
- **One rule set** (`persona_rules.py`): the Personas tab's save
  (`POST /admin/api/personas`) and the chat use the same validation; a bad value
  is refused with 400 (`errors`, `warnings` in the body). Old spellings are
  normalized (`xhigh` -> `max`, `flexible` -> unset, `{}` lists -> `[]`). Checked
  against every persona on the live router before shipping.
- **Backends**: `set_backend_enabled` (class `backend`) — the same write as the
  Pool / Routing switch (`router_ops.py`, also behind
  `POST /admin/api/backends/enabled`); the card lists every persona left with no
  usable route.
- **The whole router**: `list_admin_endpoints` (generated from the app),
  `admin_get` (any admin GET; secret values scrubbed), `get_router_config` /
  `set_router_config` (class `router_config`: config.yaml through
  `configops.apply_raw` — validated before the card, a Config history version,
  applied live where possible, restart-needed shown; secrets refused),
  `admin_call` (class `admin`: any other admin write; client-key create/rotate,
  Meridian sign-in, MCP tokens, import, the raw backend list and router restart
  are refused; deletes and clears always show a red "can't be undone" card even
  with the class on auto), `research_status` / `research_models`,
  `performance_report`. The chat calls the admin API in-process under its own
  internal admin identity (`client_auth.INTERNAL_ADMIN_TOKEN`, per process).
- **The chat's own model** (`operator_chat.routes` in config.yaml; Chat ->
  Settings... -> Chat model; `GET/PUT /control/api/chat/backend`;
  `get_chat_backend` / `set_chat_backend`): an ordered `{backend, model}` list,
  each call pinned to its backend through the pool (budget gate applies), next
  route on failure. It never goes through a persona: the `Foundry-Operator-Chat`
  persona is no longer created or used. Empty = Claude Sonnet, GPT-6.1 Sol,
  local Qwen on the first enabled backend serving each.
- Shared helpers: `route_resolve.py` (family resolution, reachability, orphaned
  personas; the Automation page's seed uses its `status_with_models`).
- Tests: tests/test_chat_router_tools.py (rules, resolution, every tool's card,
  snapshot, refusals, unknown-model and orphan warnings, deny list,
  irreversible-on-auto, scrubbing, chat chain pinning and failover, the
  settings endpoint).

## v0.139.2 — Persona tool loop: always an answer at the round cap

AnythingLLM's @agent got "Agent session complete" with no answer on a long
research question (Foundry-Chat, local Qwen on Strata, 2026-10-24 requests
68841/68842). In direct mode Foundry runs a persona's own MCP tools (searxng,
crawl4ai…) for at most `agent_brain.worker_tool_max_steps` rounds (default 6).
When the model still asked for those tools at the cap, Foundry dropped the
calls and ended the turn with only the model's interim text ("let me search
more"), so the client got no answer and no tool call. The cap's original
"hand to the brain to finish" step (agent mode) never existed on this path.

- At the cap with only Foundry-owned calls pending, those calls get a "NOT RUN:
  tool-round limit reached" result (calls stay paired) and the model gets one
  more round with "write your final answer now from what you have gathered;
  say what you could not check". The tool list is unchanged on that round, so
  a local model's prompt cache still hits. Streaming and buffered paths both.
- A model that still asks for tools on that round: its calls are dropped and a
  visible note says Foundry stopped after N rounds (ask it to continue, or raise
  `worker_tool_max_steps`). Guardrail events say which case happened.
- Own + client tool calls together at the cap: unchanged (the client's call is
  handed out, the client's loop continues).
- Tests: tests/test_tool_round_cap.py (buffered and streaming, answer after the
  wrap-up, still no answer, mixed calls).

## v0.139.1 — Doctor: media adapter key loaded by its launcher is not a red

The client's Doctor showed "foundry-media: FOUNDRY_KEY is not available to Kilo"
and "Something blocks the stack" on a working setup: the kilo.jsonc entry runs
`config/workstation/start_media_adapter.ps1`, which loads the key itself from
its DPAPI credential file, and the Doctor only looked at the entry environment,
Kilo secrets, the user environment and `foundry-media.json`.

- When none of those has the key, the Doctor now runs the entry's launcher in
  its own check mode (`-Check` for a PowerShell `[switch]$Check`, `--check`
  otherwise; 30 s limit) and trusts the exit code: pass → ok, fail → red, timed
  out or self-loading with no check mode → warning. The launcher's output is
  never shown and the key is never read. No key anywhere stays red.
- The user-environment (registry) lookup moved into `_user_env_has_key()`.
- Tests: tests/client/test_doctor_media_key.py (env, secret, launcher check
  passes / fails / times out / has no check mode, nothing provided, a real
  PowerShell `-Check`).

## v0.139.0 — Client tab: download, install guide, chat install help

Victor's ask: somewhere in the web GUI to get the Foundry Client and read how
to install it, and the operator chat able to assist an install.

- **Client tab** (dashboard, its own nav group): offers the installers for the
  running router version. Downloads **link** to the public GitHub release that
  each `v*` tag builds (client-release.yml) — the router proxies nothing and
  serves no binaries. The states are honest: available · release exists but no
  installers attached yet · untagged dev build (the latest release is offered
  as a labeled fallback that "will not match this router's version") · GitHub
  unreachable (the deterministic asset links are shown, marked unverified).
  Availability answers are cached an hour, errors ten minutes, with a
  re-check button. A connected-clients table lists every heart-beating
  Foundry Client (source, host, version, freshness, stack state) with a
  version badge against the router: matches / older / newer.
- **Install guide** on the same tab, rendered server-side from
  docs/CLIENT_MANAGER.md (new client_guide.py: section extraction plus an
  escaping markdown-subset renderer) — one source of truth, nothing
  hand-copied that can drift. CLIENT_MANAGER.md gains a Troubleshooting
  section (no heartbeat → relay key scope and keyring name; restarts that
  wait for a safe point; where logs live; Rojo/Studio reconnect; version
  mismatch).
- **Endpoints** `GET /admin/api/client/install` and `/admin/api/client/guide`
  sit with the rest of the dashboard: open on the internal network by
  default, covered by `client_auth.protect_admin` when that is on. No auth
  widening — the payloads hold public release links, a tracked doc and the
  same heartbeat metadata the dashboard already shows; never a secret.
- **Operator chat**: two read tools, no new mutations —
  `client_install_status` (router version, download availability, connected
  clients with version match) and `read_install_guide` (guide sections on
  demand), plus a system-prompt line to walk an operator through an install
  one step at a time and confirm the client registered afterwards.
- Tests: guide extraction and safe rendering, the four release states and
  caching, the endpoints, and the chat tools (tests/test_client_guide.py and
  tests/test_chat_engine.py).

## v0.138.1 — Windows timing fixes; environment-dependent tests repaired

Triage of the 7 test failures that reproduced on a clean checkout (plus one
timing flake). None was a regression from recent releases; all were
Windows-environment issues, two of them real product bugs on the platform the
live stack runs on:

- **Stream timing used the coarse Windows clock** (`time.monotonic_ns` ticks
  every ~15.6ms on Python < 3.13). In `pool/protocols.py` the OpenAI and
  Anthropic stream adapters now measure with `time.perf_counter_ns`:
  previously `headers_ms` could read 0, and fast responses got no
  `timing_source="estimated"` decode/prefill estimate at all because the
  first and last delta read the same clock value.
- **`client_gap.py` under-measured client tool time** the same way (a 300ms
  tool run could record as 297ms); it now uses `time.perf_counter`.
- Test-only repairs: two Windows default-encoding bugs (a config written as
  cp1252 but read as UTF-8, a source file read as cp1252), the capture-store
  `chmod 600` assertion gated to POSIX (Windows has no POSIX modes; the file
  is ACL-protected), and the heartbeat-narration test's tick/visible
  intervals spread apart so Windows' ~31ms effective tick cadence still
  produces invisible keep-alives.

No behavior change on Linux/macOS beyond marginally finer timing resolution.

## v0.138.0 — Playbook: the cross-project knowledge base

Review item A1 (reports/UI_AUTOMATION_REVIEW_2026-10-23.md); design and
as-built notes in reports/PLAYBOOK_DESIGN.md. The org-wide wiki Victor asked
for: templates, how-tos, verified fixes (error signature → fix), scripts and
formats in one git repo every project's sessions read — reusing the project
memory machinery, not duplicating it. Everything is inert until the
coordinator is started with `--playbook PATH` (or `$FOUNDRY_PLAYBOOK`); then
`python -m foundry_coordinator playbook init` creates and commits the layout.

- **Repo + index** (`foundry_coordinator/playbook/`): `templates/ howto/
  fixes/ scripts/ formats/`, same heading-entry markdown as docs/memory,
  indexed by the same indexer under the reserved id `@playbook` (no schema
  change there; new additive tables `playbook_slices`, `playbook_notes`,
  `playbook_candidates`).
- **Slices in every prompt** share the `memory_slice_tokens` budget: the
  memory slice first, the playbook fills the remainder (400-token floor), a
  `fixes/` entry matching the task's recorded failure signature is boosted to
  the front, and the joined block rides the existing per-entry delta hashing.
  Primaries, the lead and helpers all get it ([automation] `playbook`, default
  on, gates it per project).
- **Writes**: sessions contribute `PLAYBOOK_NOTE` lines in their final report
  (same grammar/validator as MEMORY_NOTE, secret scan, per-session dedupe);
  the coordinator commits by explicit path to the playbook repo (`PLAYBOOK:`
  prefix). Chat `kb_search` / `kb_add` / `kb_edit`; control kinds
  `playbook_add` / `playbook_edit` (feature `playbook`); `playbook
  init|status|reindex|search|add` on the CLI; a doctor line.
- **Promotion pipeline** ([automation] `playbook_promote`, **default off**):
  deterministic counters — the same repaired failure signature twice (first
  auto-heal retry per key, or a resolved lead case) or the same normalized
  operator instruction three times — render a template draft and open a
  low-priority `playbook_promotion` decision (operator-only, recommends "add",
  blocks nothing, never timeout-applied). "add" commits the draft; "skip"
  re-asks only after two further repeats. Counting runs while a playbook is
  configured, so enabling the pipeline later starts from real history.
- **Sessions' kb_search**: `foundry_coordinator/playbook/kb_mcp.py`, a
  read-only stdio MCP server over the index (SQLite `mode=ro`), wired into a
  project's kilo.jsonc per project.
- **UI**: the Memory tab gets a Project memory ↔ Playbook scope switch —
  browse/search, add/edit (typed actions), which sessions received which
  entries, and the pending promotion candidates. The snapshot travels as a
  `memory` observation under `@playbook` (scope `playbook`), readable by any
  operator.

## v0.137.0 — Queue API, kanban board, PM decisions inbox

Review items A2, A3 and V9 (reports/UI_AUTOMATION_REVIEW_2026-10-23.md). The
Automation page's Queue tab had its approve/reorder/dependency controls built
but switched off ("no queue API on the router yet"); the API now exists and
the tab grew a board. Presentation and typed actions only — no dispatch change.

- **Queue actions (A2).** New task-scoped kinds `queue_approve`,
  `queue_reorder`, `queue_deps`, `queue_requeue` (feature-gated on `queue`),
  applied by the coordinator through the engine's own paths: `approve`, new
  `move` (positions renumber 1..N; order is what `tick()` starts), new
  `set_deps` (QUEUED only, same-project, server-side cycle check) and `reopen`
  (CANCELLED only — COMPLETE is never re-queued: passed work is never rerun).
  Journaled (`queue_moved`, `deps_changed`), idempotent through the ops table,
  one queue action per task in flight.
- **Queue view.** The approved queue (position, deps, approved_by, note,
  track) rides the task observation; `GET /control/api/queue?project=` serves
  it in the shape the page already probed for, so approve, drag-reorder and
  the dependency editor light up on their own against a coordinator that
  declares the feature.
- **Board (V9).** The Queue tab renders a kanban board (toggle to the flat
  table is remembered): columns QUEUED → BLOCKED with folded states keeping
  their true pill, tracks as swimlanes, cards with attempt, blocker and the
  open decision's countdown. COMPLETE and CANCELLED collapse to counted groups
  in both views, so live rows are no longer buried. Dragging is only a
  shortcut for an existing typed action (reorder within QUEUED; BLOCKED/FAILED
  into READY = the unblock modal); every other move snaps back.
- **"Your decisions" (A3).** `GET /control/api/inbox`: every open decision
  across projects with its recommended default, deadline and in-flight answer,
  open operator questions, and `resolved_by_lead_7d`. Lead escalations carry a
  one-line "why you" (`escalation_reason`, now an explicit field in
  `decisions_view` — the capped evidence string was losing it), said as a
  sentence ("permission only you hold", "recovery limits exhausted", …).
  Surfaced on the Queue tab (cross-project, with the weekly Lead counter) and
  in the control center's bell drawer.

## v0.136.0 — Lead sprint manager: known gaps closed, pilot checklist

Closes the four "Known gaps" of docs/LEAD_SPRINT_MANAGER.md and prepares the
controlled live pilot (review item A4, reports/UI_AUTOMATION_REVIEW_2026-10-23.md).
Gaps 1–3 (upgrade queueing through `supervisor.request_upgrade`, the supervisor
never moving `recovery_cases`, lead/repair session rows ended on every outcome)
were already fixed in code; the doc now says so.

- **BLOCKED -> CHANGES_REQUIRED (gap 4).** states.py gains the edge (evidence:
  `resolution` + `findings`). A lead game assignment — and Victor's strategy
  answer to a case's question — resumes the task as a proper repair round whose
  transition evidence carries the assignment as findings, not as a fresh READY
  start. `operate` with `then: assign` follows the same path; `resume`, `wait`
  and reverify targets are unchanged.
- **Delegated-authority audit.** `resolve_decision`, `approve_test_change` and
  the extra-attempts grant match the doc (operator-only kinds and the lead's
  own escalations reserved; self-changed acceptance tests become an operator
  `expectation_change` decision; grants capped, once per case, journaled).
  Hardening: obligations itself now refuses a `lead_escalation` under
  `lead_policy` authority — before, only the engine's `resolve_decision` did.
- **Tests.** New restart-boundary coverage: a crash after the lead saved
  RESUMING but before the engine applied the resume applies the assignment
  exactly once (no second paid call, no duplicate grant); a crash mid repair
  session reuses the journaled session and worktree and queues exactly one
  upgrade request. The defect e2e pins that the upgrade row is queued in the
  supervisor's own format (`get_request`, candidate branch ref).
- **Pilot.** docs/LEAD_PILOT_CHECKLIST.md: enable / watch / rollback for the
  one backed-up live pilot, including where the deployed config actually
  stands. `[lead_manager] enabled = false` stays the repo default; nothing
  live was changed.

## v0.135.0 — Team Lead: parallel tracks, project memory, service registry

Design: `reports/TEAM_LEAD_DESIGN.md`. The coordinator now runs a project like a team lead.
Victor's decisions (2026-10-22): a git worktree per parallel track; Studio stays with the main
checkout and a track's Studio work runs at merge time in the main sprint's idle windows; memory
lives in the repo under `docs/memory/`; `max_agents` is a live setting; the chat proposes and
nothing starts without a confirm.

- **Parallel tracks (`foundry_coordinator/tracks/`).**
  - `analysis.py` reads every sprint plan, infers or parses its needs (studio, blender, audio,
    images, gpu, backend), builds the dependency graph from explicit deps and file overlap, and
    returns the tracks that can run side by side and the sprints that must stay serial. No model
    call. CLI: `tracks analyze <project>`.
  - `manager.py`: a `tracks` table; a track is a git worktree under `<workspace>/.tracks/<name>`
    on branch `track/<name>` with its own `kilo.jsonc`; a sprint queued on a track runs with the
    track's workspace, locks, scope and memory slice. `max_agents` caps running sprints, main
    first.
  - `resources.py`: `studio`, `gpu` and `blender` as capacity resources in the `locks` table.
    Studio "in use" is the gate runner lock plus the main session's last Studio tool call, so a
    long editing session no longer pins Studio. A track's merge-gate takes Studio only after
    `studio_idle_minutes` (10) with no main request pending, for at most
    `studio_track_max_minutes` (45), never interrupted mid-run.
  - Merge manager: at a quiescent point of the main sprint, rebase the track onto master,
    fast-forward, run the gate in the main checkout for the merged files, record `track_merged`,
    remove the worktree. A conflict goes to a Lead repair, then a decision with the default "stop
    this track". Shared files conflict by construction and are marked serial unless on the
    `append_safe` list.
  - Chat tools: `analyze_tracks`, `propose_tracks`, `track_start/stop/status/merge`,
    `set_max_agents`. A **Tracks** tab shows each track, the Studio timeline and the agent count.
- **Project memory (`foundry_coordinator/memory/`).**
  - `docs/memory/` in the game repo: `DECISIONS.md`, `CONVENTIONS.md`, `ASSETS.md`,
    `KNOWN_ISSUES.md`, `tracks/<name>.md`, `sprints/<ID>.md`. `memory init` creates them;
    `memory import-state` folds the old `STATE.md` ledger and operator notes in.
  - An FTS index by heading, re-indexed when the files change. Each session gets a slice matched
    to its files, needs and track within a token budget, hashed per entry so unchanged slices are
    not re-sent. The implementer, Lead and review prompts use it in place of the operator-notes
    block.
  - Sessions write memory through `MEMORY_NOTE` blocks that the coordinator validates, commits
    and re-indexes. Chat tools: `memory_search`, `memory_summary`, `memory_add`, `memory_edit`.
    A **Memory** tab shows the files, search, add and edit, and the slice each session received.
- **Service and MCP registry (`foundry_coordinator/services.py`).**
  - A `services` table with a needs-to-services map. `services.ready(needs)` runs before a
    sprint starts: a missing service makes the sprint wait, never fail, and the client is asked to
    start it when it can.
  - The Foundry Client probes the MCP servers in `kilo.jsonc` and reports them in its heartbeat;
    optional managed components for Blender and the audio MCPs (off by default).
  - `track_kilo_config(track)` writes each track's `kilo.jsonc` with the MCP allow-list filtered
    to the track's needs and agent blocks from role rules.
  - Chat tools: `services_status`, `service_restart`, `service_enable`. A **Services** tab shows
    health, provider and needed-by.

## v0.134.2 — coordinator: a dead process's owner lock is taken over

A coordinator killed without releasing its owner row blocked the next one ("another
coordinator is running on this store") until the beat went stale. The lock of a process on this
host that no longer exists is now taken over at once.

## v0.134.1 — Foundry Client: Rojo health reads msgpack

Rojo 7 answers `/api/rojo` with msgpack, not JSON, so the client's Rojo check never passed and
would have restarted Rojo every five minutes. A 200 now counts as healthy; the session id is
decoded with `msgpack` when installed (added to the `client` extra).

## v0.134.0 — Operator chat (Chat tab), Foundry Client relay

The Automation page's Ask tab becomes a **Chat** (`foundry_router/chat_engine.py`,
`/control/api/chat/*`; docs/AUTOMATION_GUI.md). The operator does everything the page does
in plain language, over as many turns as needed:

- **How it works.** Each turn sends the operator's text plus a compact context (project
  index from the bridge data, deployed settings, usage, stack status, open decisions; only
  blocks whose hash changed are sent again) to the persona `Foundry-Operator-Chat`
  (Claude Sonnet, then GPT-6.1 Sol, then the local Qwen; strict routing, JSON output; created
  on first use). The model answers `{say, ask, calls[]}` against a typed tool catalog. The
  router validates every call deterministically (schema, project allow-list, local and locked
  settings keys refused, size caps), runs reads, drafts and previews at once, and turns deploys,
  cutoff changes, persona changes, sprint starts, decisions and restarts into one **confirm
  card** with per-call checkboxes. At most 4 model calls and 8 tool calls per turn.
- **Auto-run.** Each confirm class (deploy, cutoffs, persona, sprint, decision, restart) can be
  switched to auto in the Chat tab's Auto-run drawer (`chat_auto_classes`).
- **Audit.** Every turn, model call, tool call, refusal, card and decision is a row in
  `chat_audit_log`; the Audit button shows them. Actions sent to the coordinator or the
  Foundry Client carry `{chat: {thread, turn, call}}` in their payload.
- **Stack reach.** The Foundry Client now has a router relay (`foundry_client/relay.py`): it
  heart-beats as source kind `client` (feature `stack_control`) and claims the new
  `client_restart` / `client_reload_kilo_config` actions, applying them through its own manager.
  It needs a router key with the **operator** scope stored as `FOUNDRY_CLIENT_KEY` (the
  coordinator key is the fallback); `client.relay_enabled` turns it off. See
  docs/CLIENT_MANAGER.md.
- **Kept.** `assistant_plan` / `assistant_run` still work; the chat uses them for queue steps
  (`plan_sprint_steps`, `run_sprint_steps`). `/admin/api/config/cutoffs` now shares its code
  with the chat (`foundry_router/cutoffs.py`).

## v0.134.0 (continued) — full-stack gap closure: no human in the loop, permissions, metrics, client

Built from the four audits in `reports/FULL_STACK_GAP_PLAN.md`. Victor's decisions: the chat
confirms risky actions; a sprint that hits a tooling limit with no answer is parked and the queue
moves on.

- **No human in the loop (W1).**
  - The implementer's `question` tool is denied; a session reports BLOCKED with options
    instead, which becomes a decision with a recommended default. Permission asks are
    auto-rejected with guidance, never left waiting.
  - Every decision kind carries options, a recommended default and a timeout. On a tooling limit
    the task is parked BLOCKED with a summary and the queue continues. `no_progress` continues
    once, then parks.
  - The after-closeout visual check carries prior approvals by frame hash, never re-asks
    accepted-with-notes frames, and retries "uncertain" once.
  - The coordinator rewords its own unpushed commits for the message prefix; sessions are never
    asked to rebase.
  - The Lead's hard call cap derives from the profile. Uncommitted Lead write-mode edits are
    committed as the Lead's and reverified in place. The Lead's self-approval of test changes it
    made opens an operator decision instead.
  - Memory is sent as a per-entry delta. An invalid review is re-sent once with the error.
  - Auto-heal now repairs: stale runner lock, dead gate pid, Rojo restart through the client,
    source re-sync. A profile-drift doctor check flags values that contradict the decisions.
  - The client's safe point counts Lead sessions and gate runners, and a coordinator restart no
    longer kills the detached runner.
- **Permissions match the GUI (W2).**
  - Hard denies cover the Studio never-tools, git history rewrites and shell secret reads. No
    role allow and no Lead `*` allow can override them. `studio_mcp` splits into read and write.
  - An empty edit scope means the sprint's scope only. Reads never grant writes. Config files
    (`kilo.jsonc`, `.kilo/**`, hooks, `.env*`) are not editable by sessions.
  - The write-mode Lead keeps every deny: no push, no history rewrite, no secrets, scope
    widening only through the allow-list.
  - Subagent rules come from role rules, so GUI edits win. A role-rules deploy regenerates
    `kilo.jsonc` and asks the client to reload Kilo. Config pull keeps local edits (three-way
    overlay), applies restart-scoped keys at startup, and asks the client for a safe-point
    restart.
  - One source of defaults, with a parity test; `max_review_calls` is 4 everywhere.
- **Training capture and metrics (W4).**
  - Capture is switchable per backend and per persona from a Capture card on the Training tab,
    live, with counts and a JSONL download.
  - Outcome and quality join by request id; coordinator outcomes (gate PASS, review APPROVED,
    vision approved) are posted to the router, so verified outputs can become training examples
    under the existing policy.
  - Server metrics history (`server_samples`): Strata tokens/s, prompt speed, slots, KV cache;
    Meridian metrics; a Performance chart; `perf.retention_days`.
  - Speeds aggregate by `timing_src`, so estimates never mix with measured numbers.
  - A per-sprint scorecard: `/admin/api/work/scorecard` and a Work-tab table.
- **Client and router startup (W5).**
  - The router opens its port first; MCP connections and probes warm up in the background with
    a 20 s per-server timeout. `/health` reports warm-up.
  - The media component is gone (Kilo starts the adapter itself); a doctor check covers it.
  - Bugs fixed: stale detail on disabled components, adopted processes can be stopped and
    restarted, settings saves merge onto current values, hung components restart, pending
    safe-point restarts warn after a limit, Kilo and Rojo health use their HTTP endpoints.
  - New: client-request consumer (reload Kilo, restart Rojo or the coordinator), "Apply role
    rules to kilo.jsonc", update check against GitHub releases, Rojo session-change detection
    with a reconnect prompt, a `systemd --user` unit, AppImage autostart fix, a router-link card
    and a Doctor tab.

## v0.133.2 — Foundry Client: windowless start works

- **Windowless start.** Started without a console (`pythonw`, the Startup entry, or the packaged
  tray app), the client had no stdout or stderr, so uvicorn's logging crashed it at once. Its
  output now goes to `<config dir>/client.log`.
- **Startup entry.** The Windows startup `.cmd` is written without doubled line breaks.

## v0.133.1 — Foundry Client: start at logon from a source checkout

When the client runs from source, the start-at-logon entry now starts in the checkout's folder. On
Windows the `.cmd` does `cd /d`; on Linux the `.desktop` file sets `Path=`. Without this,
`-m foundry_client` was not found at logon. Packaged installs are unchanged.

## v0.133.0 — Foundry Client: installer, tray and stack manager

A new package, `foundry_client`, replaces the hand-written workstation scripts
(`foundry-stack.ps1` and the start/deploy scripts) with one cross-platform app. See
`docs/CLIENT_MANAGER.md`.

- **What it manages.** It starts, health-checks and restarts the Kilo server, the coordinator,
  the upgrade supervisor, the media adapter and Rojo.
  - Dependency order: Kilo, then the coordinator, then the supervisor.
  - Crash restarts back off: 2 s, 5 s, 15 s, up to 2 min.
  - Restarts of the Kilo server and the coordinator wait for a safe point. That means no session
    working on an active task; the idle session of a paused task no longer blocks.
  - Components already running are adopted, not started twice.
  - Each component gets its own log under `<state>/client-logs/`.
- **Secrets** live in the OS keyring (Windows Credential Manager or the Linux Secret Service) and
  are passed only to the components that need them. They are write-only in the UI. A one-click
  import reads the old PowerShell DPAPI credential files.
- **Local settings page** on 127.0.0.1, guarded by a per-install token in the URL fragment and a
  Host-header check. It has status cards with start, stop and restart, settings forms, secrets,
  start-at-logon and a log viewer.
- **Tray icon** (pystray) coloured by health. Its menu opens settings, restarts components at a
  safe point, opens the Foundry control center and Automation page, opens the logs folder, sets
  start at logon, and quits.
- **Commands:** `foundry-client run [--headless]`, `status`, `restart <component>`,
  `install-autostart`, `import-legacy-secrets`. The same program runs the bundled coordinator
  (`coordinator ...`) and supervisor (`supervisor ...`).
- **Packages.** A PyInstaller bundle (`packaging/foundry-client.spec`) holds two programs: a
  windowless tray app and a console twin. On top of it:
  - Windows: an Inno Setup per-user installer (`packaging/windows/FoundryClient.iss`).
  - Linux: an AppImage (`packaging/linux/build_appimage.sh`).
  - The GitHub Actions workflow `client-release.yml` builds both on each `v*` tag and attaches
    them to the release.
- **Install from source:** `pip install -e ".[client]"`.

## v0.132.0 — launch assistant: run the automation in plain language

The Automation page has a new **Ask** tab. You describe what you want, for example "start the
project from the next sprint that isn't completed in its documentation".

- **Proposing.** The coordinator builds a compact snapshot: each sprint plan's checklist progress
  and status line, its coordinator state, the queue, and the project pause. It makes **one** call
  to the planner persona (Claude, then GPT, then local) for an ordered list of steps.
- **Allowed steps:** queue, re-queue a cancelled sprint, resume, pause, unblock or cancel a sprint,
  and resume or pause the project.
- **Checking.** Every step is checked against the live queue, simulating earlier steps. A step
  that doesn't fit is dropped and shown with the reason.
- **Running.** Nothing runs until you press **Run**, and you can uncheck steps first. The steps
  are checked again against the live queue before they run, and a plan runs at most once.
- **Plumbing.** Two new typed control actions, `assistant_plan` `{text}` and `assistant_run`
  `{plan_id, steps}`, are project-scoped and need an operator session. Plans are stored under the
  coordinator's state dir, `assistant/<plan_id>.json`, with `assistant_plan` and `assistant_run`
  events.
- **Reopen.** The new `engine.reopen()` and the `CANCELLED -> QUEUED` transition put a cancelled
  sprint back on the queue. The attempt counters are fresh, and the history is kept. This was
  needed to re-queue the sprints cancelled on 2026-10-21.
- **Persona.** `[assistant] persona` in the profile (default: the planner role).

## v0.131.0 — automation revamp: tokens and speed first, everything editable in the GUI

The plan is `reports/AUTOMATION_REVAMP_PLAN.md`, built from five audits. Over 38 hours no sprint
closed on its own, roughly 7% of helper work was used, and most retry rounds were tooling or
bookkeeping rather than product defects.

- **Router budgets (A).**
  - Raw model names now pass the same paid-call cap as personas.
  - Near the cutoff the router reads usage fresh and counts in-flight calls (`reserve_pct`,
    in-flight accounting), so concurrent calls can't overshoot.
  - `fail_closed` (default off) treats unreadable usage as no capacity.
  - Per-backend cutoffs: `effective_cutoff()`, plus `GET`/`POST /admin/api/config/cutoffs`.
  - A budget denial re-routes to the next route instead of pausing.
- **Gate, vision, review, closeout (B1–B4, B7).**
  - The coordinator commits runner outputs itself, and checks only fail on files the agent wrote.
  - One gate per change: the implementer runs stories and Lune; the gate re-runs only failed or
    changed checks; a baseline is taken automatically at the sprint base.
  - Vision batches run in parallel (`vision_parallel`, default 4), only changed frames are sent,
    and the clean-frame memory is saved after each batch. There is one fix round, then the sprint
    is accepted with notes.
  - Code review runs alongside vision, and both send one merged fix round into the same session.
  - The coordinator ticks the checklist and builds the evidence packet itself.
  - Closeout is templated.
- **Helpers, Lead, scope, decisions, infra (B5, B6, B8–B13).**
  - Helpers start on demand only, each with a declared consumer, and use is measured from git.
  - Sessions can't start their own subagents, except `explore`.
  - Retry guidance is skipped when the Lead is on.
  - Deterministic auto-heal runs before a Lead case opens.
  - Typed `SCOPE_REQUEST`s are approved automatically from an editable allow-list of harness lines.
  - Decisions carry a recommended default and a timeout (default 30 min).
  - An infrastructure preflight runs first; infrastructure failures cost no attempt.
  - Long sessions hand off at a context ceiling.
  - Operator notes are sent as a delta, polling is reduced, and unknown actions are not retried.
- **Config store (C1).**
  - The shared schema lives in `foundry_automation/schema.py`.
  - The router stores versioned automation config: draft, validate, deploy, rollback, diff and
    audit, under `/admin/api/automation/...`.
  - Role rules cover a permission matrix per role, path-glob scopes and hard denies. They generate
    both Kilo session rules and `kilo.jsonc` agent blocks.
  - The coordinator pulls the deployed revision at safe points through
    `/v1/foundry/control/config`.
- **GUI (C2–C4).** A new Automation page covers:
  - roles, mapped to a persona and an ordered route list with drag reorder, live usage bars and
    cutoffs;
  - a backend on/off switch;
  - an overview graph and a dry-run;
  - settings forms generated from the schema;
  - the role-rules matrix with a path tester;
  - a queue editor;
  - draft, deploy and rollback.
- **Version.** The router's `__version__` had stayed at 0.130.16, which is why the stack showed a
  stale release label. Both packages are now 0.131.0.

Known: about 6 router streaming/timing tests fail on the base too, and
`test_heartbeat_narration_during_slow_worker` is flaky on the base.

## v0.130.20 — retry guidance can fall back to local

- **`[retry_guidance] local_fallback`** (default false): when Foundry's quota admission refuses
  the guidance persona's paid routes, the coordinator still asks. The persona's strict chain ends
  in a local model, which answers. The record carries a `route_note`. A quota deferral recorded
  earlier no longer waits for the paid reset.
- **Animal Uprising:** on for retry guidance. Helpers stay paid-only (Victor 2026-10-21); their
  personas are back to their single paid route.

## v0.130.19 — lead write access and a local last-resort route

- **`[lead_manager] write_access`** (default false): the lead session gets full Kilo access
  (every tool, shell, git, Studio MCP, subagents, outside directories) through
  `lead_write_permissions`. Reading or editing secrets and `.env` files stays denied, and the lead
  never asks a question. It still holds the game workspace lock, so no primary writes at the same
  time. A change to the game workspace is recorded as a `lead_fix` action and event instead of a
  guard violation; the router checkout and the coordinator DB stay guarded. In this mode the
  prompt tells the lead to fix blockers itself: harness or tooling faults, plan file-list gaps for
  legitimate harness lines, and stale state. It commits by explicit path with the task prefix,
  then reports `reverify`.
- **`[lead_manager] allow_local_route`** (default false): a local model may be the lead chain's
  last route. The `no_local_route` readiness check passes and names it. List the local model in
  `expected_models` too.
- **Animal Uprising profile:** both settings are on; local Qwen is added to the lead's
  `expected_models`. The implementer role maps to the generic `Foundry-Kilo-Implementer` persona
  (Sonnet, then GPT-6.1 Sol, then local Qwen).

## v0.130.18 — re-reviews and fix rounds re-run only what failed

- **Incremental re-review:** after a CHANGES_REQUIRED review at an ancestor commit, the next
  review packet holds only the diff since that review, plus its findings to verify. Files reviewed
  there and unchanged since are listed but not re-sent. The setting is `[review] incremental`
  (default true).
- **Fix-round prompt:** the session re-runs only the failing stories (`stories --story`) and
  failing suites (`regression --selection`). It never starts its own full `sprint` gate. The
  coordinator's gate already carries passed work (v0.130.16).
- **Tests:** the batch-retry vision test's session-id assertion is corrected (v0.130.17 shipped
  with it failing; the code was right).

## v0.130.17 — fast lane: vision never re-sends a frame it already passed

A vision CHANGES round re-sent every frame, including frames the reviewer had already passed.
The fast lane now remembers, per task, the sha256 of every frame judged clean (`vision_clean`
in the task's fast-lane state). Later rounds send only frames that changed or had findings.

- **A frame counts as clean** when its batch approved, or when its batch asked for changes, every
  finding names a frame, and none names this one. A finding that cannot be placed never lets a
  frame skip review.
- **When every frame is remembered clean,** the approval is recorded as
  `coordinator:vision-memory` and no model call is made.
- **Inconclusive retries** re-ask only the batch that was unsure, because the approved batches'
  frames are already remembered.

## v0.130.16 — studio_gate: incremental sprint gates

Every sprint gate re-ran every story at five sizes and every selected regression suite in its own
playtest, even what had passed one commit earlier and depends on nothing that changed (7–10
minutes of Studio per gate, many gates per sprint). Sprint mode now starts from the most recent
sprint verdict whose head is an ancestor of HEAD:

- **Regression suites:** a suite that passed there carries when no file it covers changed. This
  uses the runner's file → suite map (`affected_selection`); only the rest run.
- **Stories and focus checks:** they carry, with their committed captures, when no `src/` file
  changed and every one of them passed.
- **Harness changes carry nothing:** a change to `tests/RegressionIndex.lua`,
  `tests/GateRunner.lua`, `tests/lib/**`, `default.project.json` or the sprint's gate file means a
  full run. Failed suites are never carried.
- **The verdict stays complete:** carried checks are marked `CARRIED from <commit>`, with the
  raw log listing what carried. The boot check and console check still run whenever a playtest
  runs.
- **Tests:** `test_carry_keeps_passed_suites_whose_files_did_not_change`,
  `test_carry_never_carries_a_failed_suite`. Runner-side only.

## v0.130.15 — vision: "uncertain" over clean, verified frames is an approval

UI-02a stopped as "vision approval inconclusive 3 times". gpt-6-astra reviewed batch 1 (20
hero-select frames, then each 5-frame chunk on retry) and every frame row was verified: image
seen, sha256 matched, subject present, validity ok, an observation and no finding. Yet the
top-level verdict was "uncertain" every time. A verdict of "uncertain" whose frames all pass
those checks with no finding now counts as an approval. Any unseen, mismatched or invalid
frame, or any finding, still keeps it uncertain or CHANGES. Test:
`test_uncertain_word_over_clean_verified_frames_is_an_approval`; vision and fast-lane tests pass.
Coordinator change; it needs a restart.

## v0.130.14 — fast lane: a red gate never carries to a newer commit

UI-02a's own sprint gate passed with the new baseline (v0.130.13) at 746db62. Only evidence files
had changed since the coordinator's last gate at eb14163, which was red and recorded before the
baseline existed. The carry rule, which lets a gate describe a later commit when only
evidence or closeout files changed, re-used that red verdict. The coordinator reported "same
findings" and opened a no-progress decision. Now only a green gate carries: a red or errored
verdict always makes the coordinator run the gate again on the new commit. Test:
`test_red_gate_never_carries_to_a_newer_commit`; fast-lane tests pass. Coordinator change; it
needs a restart.

## v0.130.13 — studio_gate: baseline-relative regression

UI-02a's sprint gate could never pass: six of its 14 regression suites (S45, S49, S50, UI_01,
GF_UI1, GF_UI1b) and 15 calls (the dynarg calls 50–71, three UI_01 tool errors) were already red
on its base commit. A baseline run at a1b298b showed identical counts. The gate required every
selected suite to pass.

- **Baseline file:** when `.plan/tmp/gate/<sprint>/baseline.json` holds a regression verdict
  recorded at the sprint's base commit, sprint mode compares each red regression check with it.
- **Suite rule:** a suite that was red at base and is not worse (passes not fewer, failures
  not more) becomes non-required, with its detail prefixed `PRE-EXISTING at <base>`.
- **Call rule:** a call failure whose text, with the call number removed, appears at base becomes
  non-required. Sprints renumber calls when they retire suites.
- **Verdict:** new or worse failures stay required, so the gate stays red. The verdict records
  `baseline: {head, pre_existing, new_or_worse}`. Without a baseline file nothing changes.
- **Making a baseline:** check out the base commit, run `studio_gate regression --selection ...
  --sprint BASE<id>`, then copy the verdict to the sprint's `baseline.json`.
- **Tests:** `test_baseline_marks_pre_existing_regression_failures_non_required`,
  `test_baseline_keeps_worse_and_new_failures_blocking`. Runner-side only.

## v0.130.12 — studio_gate: regression runs at 1920x1080

UI-02a's UI_01 legs c21a–c25a failed with "the viewport is 1920x1080 — got 2531x1128 (device
simulator)". The stories mode stops Studio's device simulator when it finishes, and Studio then
uses its window size, which is 2531x1128 on a large monitor, while those regression legs expect
1920x1080. The regression lane now starts by selecting the runner's `AU_1920x1080` device at
actual resolution (created if missing). A failure to set it is logged in the raw log and does
not stop the run. Runner tests pass (52). Runner-side only.

## v0.130.11 — studio_gate: playtest-start retry, Lune files aren't "unmounted"

UI-02a's first full Studio gate had two runner-made failures:

- **Playtest start:** Studio answered "Wait for Studio to finish its current playtest
  transition" (a playtest still stopping), and the runner gave up at once. The suites that never
  started then left their dynamic arguments unresolved for later calls. The runner now retries
  the start every 3 s for about 30 s while Studio reports a transition.
- **Live hash:** it was UNKNOWN because `tests/lune/**` files were "not mounted anywhere in
  Studio". Those run headless in Lune and are never mounted, so they no longer count; every
  mountable file still has to match.

Runner tests pass (52). Runner-side only.

## v0.130.10 — closeout files are never an "outside the task scope" decision

Every sprint opened three operator decisions ("required evidence docs/progress/<ID>_PLAN.md /
<ID>_SUMMARY.md / STATE.md is outside the task scope"). The close session writes those files
for every task, and they are already the profile's `[closeout] exempt_paths`. The coordinator's
scope check honored that list; the obligation reconciliation didn't. It now does, so these
decisions no longer open, and real out-of-scope evidence still does. Test:
`test_closeout_files_are_never_an_outside_scope_conflict`; obligation, lifecycle and AU
evidence tests pass. Coordinator change; it needs a restart. Decisions already open stay open
until answered.

## v0.130.9 — retry guidance works on its GPT fallback

With Claude at its weekly cutoff, retry guidance (`Foundry-Kilo-Opus`, now falling back to
`gpt-6-astra`) failed with HTTP 400. OpenAI strict `json_schema` requires
`additionalProperties: false` on every object, and the guidance schema didn't set it. The
schema now sets it, and all three properties were already required. Verified live: the old
schema fails on the GPT route with the same 400, and the new one returns valid JSON. Retry
guidance tests pass. Coordinator change; it needs a restart.

Workstation (local, untracked `config/workstation/foundry-stack.ps1`): `start` now waits up to
5 minutes for the Foundry backend (`/api/version` and a non-empty `/api/tags`) before starting
Kilo. A Kilo server started at logon before the network was up aborted every turn
(`MessageAbortedError`) and burned two UI-02a attempts.

## v0.130.8 — studio_gate waits for the community Studio plugin

After a Studio restart (or a reboot), `studio_gate doctor` and the gate preflight failed with
"community: need exactly one Studio instance, found 0". They checked the moment the community
MCP server started, but the Studio plugin connects to a fresh server only a few seconds later
(8 s, live; longer once its retry has backed off). `CommunityStudio.resolve` now polls
`get_connected_instances` for up to `CONNECT_WAIT_S` (30 s) while no instance is connected.
Test: `test_community_resolve_waits_for_the_plugin_to_connect`. Runner-side only: it applies as
soon as the deploy checkout is on the tag.

## v0.130.7 — doctor: every persona's fallback chain

At Claude's weekly 95% cutoff (2026-10-20), two personas the coordinator depends on had a
Claude-only route: `Foundry-Kilo-Sonnet` (frontier handoff) and `Foundry-Kilo-Opus` (retry
guidance). Implementer calls were refused with nothing to fall back to. Both now pin a GPT
fallback in the backend (`gpt-6.1-sol` and `gpt-6-astra`). `doctor` and the `run --control`
startup check list every persona the profile uses: roles, role overrides, `frontier_primary`,
`retry_guidance`, `lead_manager`, `lanes.visual` and the helper personas. Each line shows its
full route chain, and a WARN marks any route with a single frontier provider. Doctor tests pass.
Coordinator change; it needs a restart.

## v0.130.6 — evidence ledger: a fast-lane timeout is not a flaky FAIL

With v0.130.5 everything for UI-D1 passed: coordinator checks, the gate, and vision (carried,
no model calls). The submission was still refused: the ledger called `fast_lane.gate` flaky
because a PASS came after a FAIL on unchanged code. That FAIL was the false 11:00 timeout
fixed in v0.130.1. For `fast_lane.gate`, a FAIL whose verdict says `(timeout)` no longer makes
a later PASS flaky; real FAILs still do. Ledger, fast-lane, AU evidence and lifecycle tests
pass (139). Coordinator change; it needs a restart.

## v0.130.5 — fast lane: no repair_changed check

UI-D1's repair attempts 5 and 6 had nothing to change: the finding (`build.gate`) was a
coordinator-side evidence bug, fixed in v0.130.4. `coordinator.repair_changed` ("the repair
session committed nothing") still failed, and the fallback started a new attempt each time.
Fast-lane tasks skip that check: the studio gate re-verifies every revision itself. Coordinator
change; it needs a restart.

## v0.130.4 — fast lane: build.gate resolves from the studio_gate verdict

UI-D1 passed its gate and had vision accepted, then the [lifecycle] submission check refused
review. The `build.gate` obligation's `fast_lane.gate` record was never fresh: the ledger
requires a runtime identity for it ("runtime check without a runtime identity"), and fast-lane
records never carry one (a mock sprint runs no Studio at all). Every repair assignment today
listed `build.gate` as unresolved.

- **Ledger:** `fast_lane.gate` no longer needs a ledger runtime identity. The runner verifies
  the live Studio fingerprint itself before it can pass. Freshness still requires its code
  dependencies (`src/**`, `tests/**`, `default.project.json`) to be unchanged.
- **Gate binding:** the evidence-log binding exempts the runner's capture folder
  (`docs/evidence/UI-v3/<ID>/**`), which the post-PASS capture commit always changes.
- **Tests:** fast-lane, evidence-ledger, AU adapter, lifecycle and obligation tests all pass
  (218). Coordinator change; it needs a restart.

## v0.130.3 — fast lane: after an approval, vision reviews only changed frames

Cost: every small UI-D1 commit re-sent all ~500 frames (26 image calls) to vision, even after
the look was accepted. Once a task has a visual approval (approved, or accepted with notes),
a later revision sends only the frames whose bytes differ from that approval. If none differ,
it carries the approval with no model call. Test:
`test_vision_after_an_approval_reviews_only_changed_frames`. Coordinator change; it needs a
restart.

## v0.130.2 — fast lane: an inconclusive vision batch is re-asked in chunks

UI-D1 stopped twice on "vision approval inconclusive twice". At 20 frames, the vision model
sometimes leaves a frame out of its reply or mis-copies a sha256 (2-4 of 26 batches a round).
When the same batch failed on both tries, the whole task stopped, although every other batch
was done. Retries already reused approved and CHANGES batches; now:

- **Chunked retry:** a retry re-asks an inconclusive batch in chunks of `vision_batch_frames
  // 4` (5 frames by default).
- **Per-chunk reuse:** a chunk that answered (approve or CHANGES) is kept, and only a failing
  chunk is asked again on the next try.
- **Three tries:** the task stops only after a third inconclusive try (it was two). An
  inconclusive answer is still never a pass.
- **Tests:** `test_vision_reviews_in_batches_and_retries_only_inconclusive` (updated) and
  `test_inconclusive_batch_gets_three_tries_in_chunks`. Coordinator change; it needs a restart.

## v0.130.1 — fast lane: a consumed gate verdict is never lost

UI-D1's gate passed at 10:13 and vision was accepted with notes. The session then made a small
plan-scope commit, and the carried gate run was polled again. Its workspace verdict
(`<head>.sprint.json`) had since been rewritten by the session's own runner call, which carries
another run id, and the consumed run no longer had a process handle. The coordinator timed it
out 45 minutes after it started and sent the session a false "find what hangs" red gate. When
the coordinator consumes a verdict it now keeps a copy next to the run log
(`verdict-<run id>.json`), and the verdict lookup falls back to that copy. Test:
`test_consumed_verdict_survives_the_session_rewriting_the_workspace_file`. Coordinator change;
it needs a restart.

## v0.130.0 — the control center shows background work: the gate, vision batches, helpers

During UI-D1 the control center looked idle while the coordinator ran the studio gate and the
vision review. Neither is a Kilo session, and the activity and session views only show
sessions. The only sign was one grey line under the task state, e.g. "studio gate running at
8408b696f3f4: phase mock 84/110, 5/45 min".

- **Background work** block in the sprint progress header, while the task is VERIFYING:
  - **Studio gate**: phase, stories done of total with a bar, a playtest marker, elapsed time
    of the limit, and the log path (on the workstation) with a *Copy path* button.
  - **Vision review**: batches done of total with a bar, the batch now running and its frame
    count, how many batches asked for CHANGES, and the findings so far.
  - **Helpers** still in progress (queued, starting, executing, waiting on the provider), with
    the last time each reported. Older than 10 minutes is marked.
  - The Coordinator line says what runs ("Running the studio gate (a script, not a Kilo
    session)"), and a record older than 10 minutes says "no update".
- **Elapsed time**: the page counts it from the run's start time, so it moves every few
  seconds. The old line floored to whole minutes. UI-D1's gate ran 7.4 minutes in total, so
  77/110 and 84/110 both fell in minute 5. It was never stuck, but it read that way. The
  coordinator's line now reads `5:12 of 45 min`.
- **Data**: the fast lane keeps a structured record (`fast_lane.live`: gate phase, progress,
  start, limit, log; or vision batch, done, changes, findings so far). It is saved only when
  something changes. The bridge sends it as `background` while the task is VERIFYING, and
  Foundry bounds it into `task_view.background`, parsing "84/110" into done/total. Display
  only; nothing reads it back.

**Deploy**: the coordinator (fast lane + bridge) for the record, and Foundry (router host) for
`task_view.background` and the page. Either alone is safe: without the other, the block simply
doesn't appear.

**Tests**: `test_running_gate_publishes_live_progress_for_the_control_center` and
`test_vision_batches_publish_done_of_total_and_findings_so_far` in
`tests/coordinator/test_fast_lane.py`; three new cases in `tests/test_control_progress_header.py`.
## v0.129.8 — vision approval: exact frame field values

UI-D1's second vision round returned CHANGES on 25 of 26 batches, with 813 findings. Many read
"invalid, occluded or wrong UI subject" followed by an observation that the frame was fine.
Both vision prompts (fast lane and model visual approval) named the per-frame fields but not
their values. The model answered `validity: "valid"` for good frames, and the parser counts
anything except exactly `"ok"` as an invalid frame. One batch was also "inconclusive" twice
because its verdict word was off, and that blocked the task.

- **Prompts:** both now carry `visual_approval.FRAME_FIELDS`, which gives the exact values
  (`validity` is one of `ok | invalid | occluded | missing_subject`, `ok` even for a frame with
  defects).
- **Parser:** accepts `valid/good/pass/true` (any case) as `ok`, and `approved`, `changes
  requested` and the like for the verdict. `occluded`, `invalid` and `missing_subject` are
  still findings.
- **Test:** `test_frame_validity_and_verdict_synonyms_are_understood`. Coordinator change; it
  needs a restart.

## v0.129.7 — reference renderer: viewport calibration diagnostic

UI-D1's vision review flagged an empty band (96px at the bottom, 18px at the right) on every
frame. It came from neither the UI nor the v0.129.3 flags: in the Kilo sessions' shell, headless
Chrome lays the page out in a viewport smaller than the screenshot (headed-window insets), and
the mock sized its stage from `clientWidth/clientHeight`. The session fixed the mock (UI-D1
b8fd0e6). The coordinator's own gate runs and a hidden PowerShell window render correctly. The
renderer now calibrates once per (browser, size) per process with a full-viewport test page,
and the mock lane records `renderer.viewport@<size>` as a **non-required** check when that
shell's viewport is short. This is a diagnostic, never a refusal. Tests:
`test_png_pixels_reads_filtered_rows`, `test_calibration_catches_the_short_viewport`,
`test_short_viewport_is_noted_on_each_frame`. Runner-side only.

## v0.129.6 — fast lane: the capture commit covers the whole capture folder

On v0.129.5, UI-D1's session ran its own stories render, which re-rendered 463 frames
besides the gate's 40 captures. The post-PASS prompt said to commit "exactly the gate's
capture files ... nothing else", so the session left the 463 frames dirty, and
`coordinator.clean` failed until the fallback started a new attempt. When other frames under
`[fast_lane] runner_captures` are uncommitted, the prompt now asks for one commit of the
capture folder (`git add -A -- docs/evidence/UI-v3/<ID>`) and says how many extra frames it
holds. With only the gate's captures dirty, the prompt is unchanged. Test:
`test_capture_commit_covers_other_renders_in_the_capture_folder`. Coordinator engine change:
it needs a restart at a safe point.

## v0.129.5 — fast lane: the runner's own outputs don't trip the clean checks

UI-D1 used up its attempts with the gate green. The studio_gate runner appends every run to
`docs/progress/<ID>_EVIDENCE_RAW.log`. That file was already uncommitted when the attempt
started, so every gate run (and every commit of the log) failed
`coordinator.unrelated_preserved` ("pre-existing uncommitted changes were modified"). The
same-session continuation could not clear it, and each fallback started a new attempt.

- **New `[fast_lane]` keys** (`{task}` = the task id):
  - `runner_outputs` (default `["docs/progress/{task}_EVIDENCE_RAW.log"]`): files the runner
    rewrites on every run. For fast-lane tasks they are left out of `coordinator.clean` and
    `coordinator.unrelated_preserved` entirely.
  - `runner_captures` (default `["docs/evidence/UI-v3/{task}/**"]`): capture files must still be
    committed (the post-PASS capture commit). Re-rendering or committing one that was dirty at
    run start no longer counts as touching a pre-existing change.
- **Unchanged:** non-fast-lane tasks, and the start check that refuses to begin with
  uncommitted files in the task's own scope.
- **Test:** `test_runner_outputs_dirty_at_run_start_never_trip_the_clean_checks` (fails on
  v0.129.4).
- **Deploy:** coordinator engine change; needs a coordinator restart (`deploy_release.ps1`
  waits for a safe point).

## v0.129.4 — studio_gate refuses a bad `--repo`

A UI-D1 session ran the runner with `--repo C:\Roblox\AnimalUprising` through a POSIX shell,
which turned the path into the drive-relative `C:RobloxAnimalUprising`. The runner then wrote its
evidence log under a stray `RobloxAnimalUprising/` folder inside the game repo, and the stray
file tripped `coordinator.unrelated_preserved`. The runner now refuses (exit 2) any `--repo`
that is not an absolute path to a folder holding `.git` and `default.project.json`, and the
message tells the session to use forward slashes (`--repo C:/Roblox/AnimalUprising`). This is
runner-side only: it takes effect once the deploy checkout is on the tag, with no restart.
Test: `test_repo_must_be_the_absolute_game_repo_root`.

## v0.129.3 — deterministic mock renders

Three headless renders of the same mock frame produced three different PNGs, so every gate run
re-dirtied UI-D1's committed captures and cost a commit turn per cycle. The reference renderer
now passes Chrome's determinism flags (`--virtual-time-budget`,
`--run-all-compositor-stages-before-draw`, no LCD text or font hinting, `--deterministic-mode`).
In a live check on UI-D1's mock, three renders were byte-identical. This change is runner-side
only: it takes effect as soon as the deploy checkout is on the tag, with no coordinator restart.

## v0.129.2 — fast lane: vision approval in batches

UI-D1's gate passed with about 560 mock frames. The fast lane then asked the vision model to
review all of them in one call and return a verdict row per frame. It came back "uncertain"
twice ("did not account for every required frame"), and the task stopped BLOCKED.

- **Batches:** frames go to the vision reviewer in batches of `[fast_lane] vision_batch_frames`
  (default 20). There is one idempotent call per batch and at most one call per tick. Every
  batch must approve; CHANGES findings from all batches merge, each prefixed with its batch. A
  retry re-asks only the inconclusive batches, and approved or CHANGES batches are reused.
- **Tests:** `test_vision_reviews_in_batches_and_retries_only_inconclusive`,
  `test_vision_batch_changes_merge_findings`; fast-lane, engine and vision-approval tests 61
  passed.

## v0.129.1 — studio gate: mock lane for design sprints; runner importable with `py`

UI-D1's first session stopped BLOCKED on two gaps: its gate is an HTML mock, not Studio
stories, and `py -m foundry_coordinator...` was not importable from the game repo.

- **Mock lane:** a gate file with `"lane": "mock"` renders each story through the reference
  renderer (`docs/design/ui-overhaul/v3/references.json` maps story -> page) at its sizes into
  `docs/evidence/UI-v3/<ID>/`. Each frame is a `story.<name>@<WxH>` check and a capture for the
  vision approval. Studio, playtests and regression are skipped. Works in `sprint` and
  `stories` modes.
- **Workstation:** a per-user `foundry_deploy.pth` puts the deploy checkout on the default
  Python's path, so `py -m foundry_coordinator.projects.animaluprising.studio_gate` works from
  any folder (stdlib only).

**Tests:** `test_mock_lane_renders_frames_as_checks_and_captures`,
`test_mock_lane_with_no_stories_fails`; runner, fast-lane and renderer tests 73 passed.

## v0.129.0 — UI-T0: the studio gate runner and the coordinator fast lane

UI-02 showed that building UI was not the slow part; proving it was. It ran
about 37 hours, of which about 6 were coding. 16.2 hours went to 29 blocked
periods, plus 31 sessions, 19 recovery cases, 28 Lead calls and 35
decisions. Its final gate made the model hand-carry 40–80 printed MCP calls,
honour timing windows by hand and paste raw output between tools; one bad
paste invalidated a run. 57–63 % of UI-01/UI-02 commits were documentation
and evidence only. This release automates the proof and keeps one session
per sprint. Plan: `docs/plans/AU_UI_REWORK_PLAN.md`.

**Studio gate runner** (docs/STUDIO_GATE.md)
- New `foundry_coordinator/mcp_stdio.py`: a stdlib MCP stdio client for the
  official Studio MCP server and the community `robloxstudio-mcp@3.2.1`
  server. Fails closed: a timeout or dead server is an error, never an empty
  success.
- New `py -m foundry_coordinator.projects.animaluprising.studio_gate` with
  `doctor`, `stories`, `sprint`, `regression` and `status`. It runs the host
  checks ported from `gate.ps1`, Edit-mode story captures at the five `AU_*`
  sizes with `UiLayoutCheck`, focus walks, the sprint's wiring and touch
  checks in one solo playtest, the affected regression (executes
  `GateRunner.run`'s CALL plan itself: bridge suites, DYNARG, tight pairs),
  `GateRunner.tally` and a console-error check. It writes one verdict JSON
  (schema 1) per revision and one evidence block, and prints `GATE <ID>
  PASS|FAIL`. Exit 0 PASS, 1 FAIL, 2 not ready / tooling error.
- `--background` plus `status --wait` (at most 9 minutes per call), so a full
  gate fits Kilo's command limits.
- File → suite map: shared config files, comment-only `Remotes.lua` edits
  and unmapped UI files map to the `ui` subset, not to every suite.
- New `reference_render` (headless Edge/Chrome renders the v3 HTML mocks at
  the five sizes) and `review_sheet` (per-sprint HTML contact sheet: capture
  next to reference, checker lines, vision findings). The runner builds the
  sheet after every run with captures; it can never change a verdict.

**Coordinator fast lane** (docs/FAST_LANE.md)
- New `[fast_lane]` profile table, off by default: `enabled`, `tasks`,
  `gate_module`, `gate_timeout_minutes` (45), `max_red_gates_before_handoff`
  (2), `max_vision_rounds` (2), `lanes`. Unknown keys and bad values fail
  profile loading. Unlisted tasks are unchanged.
- The runner is the verify step: a background subprocess polled per tick
  through `status.json`; a tick never waits for Studio. The runner holds the
  project's exclusive locks.
- A red gate sends the failed checks into the same session (no new session,
  no recovery case, no attempt charged) and counts the red gate. The verdict
  is a `fast_lane.gate` verification row and evidence record bound to the
  revision; the AU adapter's only BUILD obligation for these tasks is that
  record.
- After a green gate: captures committed, then the revision-bound vision
  approval with the plan's rubric. CHANGES findings go into the same
  session; after `max_vision_rounds` the next CHANGES is accepted with its
  notes recorded as follow-ups.
- Automatic frontier hand-off: `max_red_gates_before_handoff` red gates on a
  local-primary task request `[frontier_primary]` in the same session
  (`frontier.auto_request`, same limits as a manual request). Twice that many
  stops the task BLOCKED.
- Lane trimming: fast-lane tasks run only the lanes in `[fast_lane] lanes`.
- AU adapter: a fast-lane BUILD prompt (rules card, `au-fast-lane` skill, the
  one-page plan, the gate file, the runner commands, no per-step ticking).
- `project.example.toml`: `[role_overrides.ui]` (UI-03a…UI-06a, UI-V1, local
  implementer) and `[role_overrides.ui_frontier]` (UI-D1, UI-02a,
  `Foundry-Kilo-Sonnet`), Deep-Review reviewer for all; the commented
  `[fast_lane]` block; `[frontier_primary]` notes the automatic trigger.
- New AU Kilo skills `au-fast-lane` and `au-ui-v3` in
  `projects/animaluprising/kilo_skills/`.

**Training data** (docs/TRAINING_DATA.md)
- New source `ingest gate --project P` (also run by `queue` / `auto`):
  verdict JSON as `gate_verdict` labels with captures as artifacts.
- Vision verdicts (APPROVED / CHANGES with numbered findings) and critiques
  as labels; tags `fast_lane`, `ui_v3`, `template:T1…T8`, `widget:<Name>`,
  `device:WxH`; `export --tag T`.
- Per-step authorship: a local turn that applied a helper's prework patch
  (`helper:<unit>`) or a harvested pre-code model (`helper:precode-harvest`)
  is context only (`"weight": 0`), never a target. Local turns stay eligible.
  `allow_frontier_targets = false` now applies to every dataset kind, and
  `validate` checks the weights.
- Corrections from red gates and vision CHANGES, including inline triples
  inside one fast-lane session.

**Not done yet:** the `leak.*` check (scene analysis), per-sprint lane caps
fed from the plan's stories, critique events from the control center, and
the Scene Analysis beta check in `doctor`.

**Tests**: `tests/coordinator/test_mcp_stdio.py`,
`tests/coordinator/test_studio_gate.py` (fake MCP server in
`tests/coordinator/fixtures/`), `tests/coordinator/test_reference_render.py`,
`tests/coordinator/test_review_sheet.py`, `tests/coordinator/test_fast_lane.py`,
`tests/test_training_ui_labels.py`. Live: `doctor`, a sample story at five
sizes and the `ui` regression at HEAD on the workstation (lead session).

**Deploy**
- **Coordinator** (workstation, `C:\Users\shock\foundry-deploy`, tag with
  `deploy_release.ps1`), then restart it. The live `project.toml` needs
  `[fast_lane]` enabled with the task ids, `[approval] visual_model = true`,
  and the new role overrides. Nothing changes for other tasks.
- **Kilo restart** after the `kilo.jsonc` changes: `"snapshot": false` (stops
  the "snapshot system is slow" pop-up that stalled UI-02 for about 40
  minutes), the pre-allowed runner, Lune, git and Studio MCP commands, and
  the `au-ui-v3` / `au-fast-lane` skill allowlists per agent. Kilo reads its
  config only at start.
- **AU repo** (lead session): `tests/lib/StoryHarness.lua`,
  `tests/lib/FocusWalk.lua`, the rules card, the skills and each sprint's
  `<ID>_PLAN.md` / `<ID>_gate.json`, Rojo-synced. Run `studio_gate doctor`
  until `DOCTOR READY`.
- **Backend items for Victor** (router host, not in this release's code):
  1. Raise the optional-helper cutoff to **95 %** (`helper_policy.cutoff_pct`;
     hysteresis stays 5, so helpers resume below 90 %). Control center
     helper-policy settings (operator session) or the backend `config.yaml`.
     CLIProxyAPI's separate 85 % essential-work limit is unchanged.
  2. Give `Foundry-Kilo-Sonnet` an ordered fallback: Sonnet 5.5 → **GPT-6.1
     Sol**, so the frontier primary (UI-D1, UI-02a, the hand-off) is not
     Claude-only.
  3. Turn on the **Scene Analysis beta** in Studio (File → Beta Features) for
     the leak check. `doctor` does not check it yet.
- Before "begin": the live failover check with GPT out (one real call each
  through Deep-Review and Research served by the Claude fallback; a
  high-value helper unit still dispatches on Meridian).

## v0.128.0 — easier to read: an Overview page, better charts, a sprint progress header

The web UI was hard to follow: no landing page, metrics spread over long
pages, and in the control center the only progress signals were the
obligations count (open until the final gate) and Kilo's to-do list.

**Dashboard (`/ui`)**
- **Overview** is the new landing page: one plain sentence on health
  ("All 4 backends are up. 312 requests in the last 24 h, typically 5.9 s
  each."), a *needs a look* list (backend down, brain down, a subscription
  window at 85%+, advisor warnings, failed calls), five headline numbers with
  24-hour trend lines (requests, response time with the slowest 5%, first
  token, generation speed, tokens), backend health, subscription windows,
  what is generating now and the busiest models. Refreshes every 30 s.
- **Navigation**: Overview, Monitor (was Dashboard), Backends, Routing,
  Tools & MCP, Labs, Logs, Access (was Setup). Every tab is still there;
  each group and tab has a one-line tooltip; a few tabs got plainer names
  (Coordinator work, Backend pool, Request log). The nav sticks to the top
  and scrolls sideways on a phone instead of wrapping. The open tab is in the
  address (`/ui#Performance`), so reload, bookmarks and Back work.
- **Header**: a *Control center* link, and the status line (backends up,
  brain, routing) now loads on every tab, not only on the Backend pool tab.
- **Performance charts**: two per row on wide screens, drawn at their real
  size so axis text stays readable, a soft area under single series, and
  hover readouts (time, every series' value, model and backend). A *jump to*
  line skips to the charts or the per-call detail.

**Control center (`/ui/control`)**
- **Sprint progress header** above the panes: the phase line PLAN → BUILD →
  VERIFY → REVIEW → CLOSE, BUILD steps ticked of the plan's total (bar and a
  filling BUILD chevron), the next step, Kilo's to-do count as a sub-line,
  the Required-work count with "closes together at the final gate", what
  the coordinator is doing, the blocker, and a *Needs you* list whose buttons
  open the answer composer, the inbox or the command tab. Foldable; folded
  by default on a phone; can be hidden in Settings. docs/CONTROL_CENTER.md.
- Inspector sections have plain-language tooltips; section titles are
  sentence case; the Required work section says why its open count is high
  during BUILD.
- New theme option *Match the system*.

**Both**: one shared palette (dark "cast iron", cool light, the logo's
orange as accent, steel blue for information) and type (Bahnschrift for
numbers and headings, Segoe UI for text; no web fonts, works offline).

**Data**: the bridge now passes the plan's BUILD row count
(`build_continuing.total_steps`, from the engine's own transition record)
and Foundry bounds it into `task_view.build.total_steps` (int or null). No
API was removed or renamed.

**Deploy**: Foundry (router host) for the pages and `total_steps` in the
view; the coordinator (workstation, deploy checkout) for the bridge to send
`total_steps`. Either alone is safe: an older bridge just leaves the total
out, and the header shows the open count only.

**Tests**: `tests/test_control_progress_header.py`, `tests/test_ui_overview.py`,
`total_steps` cases in `tests/coordinator/test_control_bridge.py`.

## v0.127.0 — live Kilo checkpoint for the implementer; withdrawn questions resume

The UI-02 primary tried to run `kilo_checkpoint` against `127.0.0.1:4096`
and got a 401, because the Kilo server password is the coordinator's
credential and is not in the session's shell. It recorded a manual audit
instead (AU 822e81c). Giving sessions that password would let them drive the
server (other sessions, aborts, permission replies), so they don't get it.

- **Live checkpoint:** while an implementer session is busy, the coordinator
  fetches its transcript, runs the same `kilo_checkpoint` audit it already
  runs at verification, and writes `.plan/tmp/kilo_checkpoint.txt`
  (git-ignored, read-only, header with session, UTC time and HEAD). It
  refreshes at most every `[limits] live_checkpoint_seconds` (default 120;
  0 turns it off). This is advisory: a failed refresh is logged as a
  `live_checkpoint_failed` event and never changes the task.
- **Prompt and rule:** the BUILD prompt tells the session to read that file
  and never call port 4096. `RULE_AMENDMENT.md` is updated to match. The
  matching AU `.kilo/rules/animaluprising-adapter.md` edit is a separate
  patch (AU_kilo_rule_live_checkpoint.patch), applied after UI-02 closes.
- **Withdrawn session questions:** Kilo withdrew UI-02's snapshot question
  ("Disable for this project?") on its own while the session went on
  working. The coordinator stayed AWAITING_HUMAN with nothing to answer, and
  replying gave a 404 (`QuestionNotFoundError`). Now, when Kilo has no longer
  held the request for `[limits] question_withdrawn_grace_seconds` (default
  60; 0 = never), the task returns to the state it came from and the
  coordinator polls the session again, recording a `question_withdrawn`
  event. An operator answer to a withdrawn request resumes the task instead
  of failing. A failed reply to a request that is still pending still raises.

**Tests:** `tests/coordinator/test_live_checkpoint.py` (rate limit, off
switch, failure is advisory), `tests/coordinator/test_question_withdrawn.py`
(grace period, off switch, answer after withdrawal, pending reply failure)
and `test_live_checkpoint_file_is_read_only_and_ignored` (AU clone).

## v0.126.8 — the Lead's guard baseline is the prompt, not the session creation

The v0.126.7 deploy checked out a new tag in the deploy checkout while Lead
call #5 of `rc-5b39e448db36eb6b` was created but not yet prompted. The project
pause had held it. After the restart the call was prompted, and the guard
compared against its creation-time snapshot. It reported "router checkout
head changed" and escalated to Victor (`dec-f09c25239da1a8cd`).

- **Fix:** the guard re-takes its snapshot when the prompt is sent, the
  first moment the model can act, so a deploy or any operator change before
  that is not attributed to the Lead.
- **Deploy script:** `deploy_release.ps1`'s safe-point probe now counts only
  Lead and repair calls whose session was actually prompted. A call the
  project pause held before sending no longer deadlocks a deploy.

**Test:** `test_a_deploy_between_create_and_prompt_is_not_a_guard_violation`
fails on v0.126.7 and passes now.

## v0.126.7 — "Edit nothing in tests/" is a prohibition

The Lead's report for `rc-5b39e448db36eb6b` was rejected for "Edit nothing in
src/ or tests/; do not touch tests/S46_check.lua", because the negation came
after the verb.
- A test-edit match whose object is `nothing`, `none` or `no` is a
  prohibition.
- The clause negation words now also include `nothing`, `none`, `neither`
  and `nor`.
- Real edits are still flagged, including after a negated clause ("Change no
  assertions; update the tests/S46_check.lua setup").

## v0.126.6 — an operator unblock's resolution is the next strategy

UI-02, 2026-10-19: an operator unblock with a reset and a written resolution
(Victor's S46 scope amendment) was re-asked by the repeat guard within
seconds (`dec-a687daddaae7e248`), which opened another Lead case. The
resolution reached the primary only as a note, not as the strategy the
guard looks for.

**Fix:** with `[lead_manager]` and lifecycle on, `unblock` stores the
resolution as the next start's strategy. Like a Lead assignment, it is used
for exactly one session and crosses the repeat guard once.

**Test:** `test_operator_unblock_resolution_crosses_the_repeat_guard_once`.

## v0.126.5 — every feature on in the Animal Uprising template; deploy checkout

- **`[frontier_primary]` is enabled** in `project.example.toml`, with persona
  `Foundry-Kilo-Sonnet`, 3 turns, 120 minutes, one phase per task. It stays
  manual only (`frontier request`) and never triggers itself, so the local
  primary remains the default game writer. Every other feature was already
  enabled.
- **The live coordinator runs from a separate deploy checkout**
  (`C:\Users\shock\foundry-deploy`), pinned to a release tag. Deploys use
  `config\workstation\deploy_release.ps1 -Tag vX.Y.Z`, which:
  - checks out the tag and copies the live profile;
  - waits for a safe point and backs up;
  - stops the coordinator through its stop file, starts it hidden and checks
    the heartbeat.

  Dev edits and commits no longer touch the live code or the Lead's guard.

## v0.126.4 — prework survives the session ending

UI-02's primary session died on an ECONNRESET. That cancelled its in-flight
prework, a failure triage and other lane units ("the session ended").
Prework is bound to a commit, not to a session, so it is now exempt from
the session-end lane abort, from the "primary is ready" skip of queued units
and from the end-of-run expiry. Its PREWORK note serves the next session.
Reactive lanes keep their old behaviour.

Coordinator only.

## v0.126.3 — prohibitions are not violations (Lead report validation)

The Lead's reports for `rc-6e288461f726c329` were rejected twice for text
like "Do not edit any src/ or tests/ file, tests/UI_02_check.lua, closeout
docs or the scope":
- **Test-edit check.** Any acceptance path named in the line counted as an
  edit, even inside a prohibition. Clause detection also split at the dot in
  `tests/x.lua`.
- **BUILD-to-CLOSE check.** `moves_build_to_close` tested the whole line, so
  "closeout" in one clause plus a deferral word anywhere else was flagged.

Fixes:
- A new `obligations.clause_negated` finds clauses that end at `. ; : ! ?`
  followed by a space, or at a parenthesis, never at a dot inside a path. A
  prohibition earlier in the same clause covers every item of its list.
- Named acceptance paths and test-edit verbs count only when not prohibited.
- `moves_build_to_close` now looks per clause and only at an unprohibited
  deferral.
- Real instructions are still caught, including after a prohibition clause,
  as are waivers. "known" no longer counts as "no".

Coordinator only.

## v0.126.2 — helpers: per-run reactive budget, NEXT-first prework, CLOSE drafts

These are findings from UI-02's first v0.126 run.
- **Per-run reactive budget.** Eight useful reactive units were refused by
  the task's lifetime cap: 4 failure triages, 2 incremental reviews, a
  research lookup and a handoff. That cap had been used by earlier runs.
  `max_lane_units_per_task` and `max_dispatches_per_task` now count per RUN.
  Older runs' rows stay for the audit. The new `max_lane_units_lifetime`
  (default 4× the run cap) still spans every run as a runaway guard.
- **NEXT-first prework.** Prework had prepared partly done steps 4 and 5
  while the plan said `NEXT: CLOSE`. Steps are now ranked by the plan's NEXT
  line: an explicit step number first, then shared words, with fully open
  steps before partly done ones.
- **CLOSE drafts.** When NEXT is CLOSE, one prework unit drafts the CLOSE
  duties (summary, ledger row). The draft may touch only the documents those
  duties name. It is stored as `close-<unit>.patch` with a "CLOSE session
  only" memory note and is never delivered to the BUILD primary.
- **Delivery.** Prework cards of an older HEAD are superseded, never
  delivered. Current prework reaches the idle primary between turns through
  the existing helper delivery.

Coordinator only.

## v0.126.1 — the Lead stops burning calls on false rejections

In live UI-02 case `rc-fa9340ea8134a007`, three of four Opus calls were
wasted:
- **Negation in long clauses.** A report saying "do not change src/, tests,
  RegressionIndex pins or update any acceptance expectation" was rejected as
  changing tests. Negation was only checked in the 24 characters before each
  verb. A prohibition now covers its whole clause.
- **Valid actions thrown away at a limit.** A validated `assign_game_repair`
  that reached `max_extra_attempts_per_task` while being routed was thrown
  away for another diagnosis. Under full authority the bound is now raised in
  a journaled delegated round and the same action proceeds. The same applies
  to `max_candidates`, for assignments, coordinator repairs and `operate`.

Coordinator only, no server change.

## v0.126.0 — helper prework ahead of the implementer; shared task memory

UI-02's helpers had been idle for hours. The task had used all 96 lane units
and 64 dispatches, 38 of them reactive failure triage. Only 6 of 96 results
were integrated: the rest expired with their run or fell below the delivery
threshold. No helper wrote to the task memory.

- **New `prework` lane** (server: new optional purpose `prework`). At run
  start and after every commit, helpers prepare the next `steps_ahead` (2)
  open plan steps at the current HEAD. Each helper:
  - writes ONE minimal unified diff for its step;
  - lists the exact check commands and the gotchas.

  The coordinator stores an in-scope, well-formed patch under the git-ignored
  `.plan/tmp/prework/<task>/<unit>.patch`. It then writes a `PREWORK` note to
  the task memory that every primary prompt carries (the revision-bound
  "data, not instructions" section). The local primary applies, checks and
  commits instead of re-deriving the change. Helpers stay read-only, and the
  primary remains the only game writer.
- **Own budget.** Prework never uses the task-wide reactive caps
  (`max_lane_units_per_task`, `max_dispatches_per_task`), so triage cannot
  starve it. Its cap is `[lanes.prework] max_per_task` (60).
- **Stale prework.** A new HEAD supersedes queued prework of older heads and
  retires their memory notes. A result that finishes after HEAD moved is
  discarded.
- **Carried results.** Structured prework, review, test-design, research,
  preflight and triage results from an earlier run are delivered in the next
  run while every file of their snapshot is unchanged. Previously they always
  expired.
- **No-op reverify.** An `operate` with `then = reverify` and no new commit
  resumes the primary instead of re-running identical checks.
- **Scratch files.** The Animal Uprising BUILD prompt tells the primary to use
  PREWORK notes first and to keep verification scratch under `.plan/tmp/`,
  which is git-ignored, so it never fails CLEAN.

Tests:
- New: tests/coordinator/test_lane_prework.py (5 tests).
- New in tests/coordinator/test_lead_full_authority.py: the no-op reverify
  test.

## v0.125.1 — operate ending in READY no longer re-asks the repeat question

In the v0.125.0 live pilot, `rc-db6eef8a7a7e7f2f` resolved by `operate` and
then `reverify`, which landed in READY because HEAD had not moved. The repeat
guard then reopened `dec-96cec292c0994aee` before the implementer started. The
automatic re-check withdrew it a tick later.

**Fix.** When an `operate` ends in resume or reverify, its operations and
diagnosis now ride with the task as the lead's strategy:
- they cross the repeat guard once, as a lead assignment does;
- they are the implementer's first finding;
- they are consumed by exactly one session start.

**Test:** `test_operate_resume_after_repeats_reaches_one_session_without_a_re_ask`.

## v0.125.0 — Lead manager: full delegated recovery authority (default when enabled)

Victor's policy (docs/prompts/LEAD_MANAGER_FULL_AUTHORITY.md). It was
demonstrated by UI-02 case `rc-db6eef8a7a7e7f2f`: the case stopped at
`max_cases_per_task` before Opus was called, while the real blocker was a new
untracked scratch file, `.plan/eFullVerify_call.lua`.

- **`operate` action.** With `full_authority` (default true when the lead is
  enabled), the read-only lead directs operations and the coordinator
  executes, journals and verifies each one as the single writer:
  `clean_untracked`, `extend_budget`, `resolve_decision`, `raise_bound` and
  `approve_test_change`. The step ends with assign, reverify or resume.
- **New authority `lead_policy`.** Decisions answered as `lead_manager:<case>`
  are checked by the engine against an open case of the same task.
  Operator-only kinds stay with Victor.
- **Counter limits** become journaled delegated rounds. Each needs an action
  materially different from every earlier one for the task. The runaway
  guard `max_delegated_rounds` asks Victor with facts, not a counter.
- **Legacy escalations.** Counter-only NEEDS_VICTOR escalations opened
  before this release are taken back by the lead.
- **New concrete failures** open their own case, because untracked
  out-of-scope files are now part of the fingerprint. The dossier names the
  stray files.
- **`ask_victor`** is only for input Victor must give personally, or a
  change to the core game goal.
- **Scratch-file safety.** A journaled scratch removal updates the session
  baseline. Files that were dirty when the task began are refused and stay
  protected.
- **Unchanged:** no model can mark PASS, create evidence or approve visuals.
  Served-model, permission and guard failures still fail closed.
  `full_authority = false` restores strict mode.

Tests:
- New: tests/coordinator/test_lead_full_authority.py (10 tests).
- The existing lead tests pin strict mode.

## v0.124.1 — UI sprint recovery and visual-policy follow-ups

Workstation commits `4a7c8a6`..`6903403`, plus one prompt fix:
- **Visual obligations** route to the model visual approval when
  `[approval] visual_model` is on. Lead recovery is kept aligned with the
  current visual policy.
- **Lead recovery:**
  - unfinished builds continue;
  - the recovery epoch resets after an operator reset;
  - explicit "no test edit", "no change" and "no waiver" instructions are
    honored as written.
- **UI agents** are taught the named menu actions. Physical input tests move
  to the game-test sprint, UI-GT1. The local primary is restored for the
  remaining UI sprints.
- **Prompt fix:** the Animal Uprising BUILD prompt says final screenshot
  sign-off is delegated to the coordinator's vision reviewer only when
  `visual_model` is on. With it off, the operator's visual approval still
  closes the sprint.

Tests:
- test_au_evidence_contract
- test_engine
- test_lead_manager
- test_lifecycle_engine
- test_obligations
- test_planner_handoff

## v0.124.0 — opt-in model visual sign-off; fresh-session recovery after a reset

**Model visual sign-off.** This is opt-in with `[approval] visual_model = true`,
off by default; the profile accepts only true or false.

- With it on, an `after_close` visual approval can be given by an
  independent vision reviewer (`visual_reviewer` persona, called directly
  through Foundry) instead of waiting for the operator. Every test, gate and
  code review still applies.
- Before the call, the coordinator checks:
  - the closeout revision is exact and clean;
  - every required frame named by the plan is committed, PNG/JPEG, 8 MB or
    less, and still matches its hash;
  - the persona is strict, allowed to the client, and every route model is
    vision-capable.
- The reply must account for every frame by path and SHA256, with
  `image_seen`, a valid subject and an observation. The served model must be
  one of the verified routes.
- Any other reply, a stale revision, or a changed frame counts as
  inconclusive and leaves the approval gate unsatisfied.
- `changes_required` rejects the closeout with the findings. `approve`
  records a revision- and artifact-bound approval by
  `model:<persona>/<served model>`.
- The call is journaled as a one-time op. A restart never pays for it
  twice, and a deferred one retries after its backoff.

**Fresh-session recovery.**
- An operator unblock with `reset_attempts` now withdraws the task's open
  `no_progress` / `recovery_escalation` questions, with an event. The next
  READY tick no longer revives them before a new worker starts.
- A task with partial BUILD progress gets a free retry, so the previous
  session's unfinished files don't count as repeat failures.

**Animal Uprising adapter.** The BUILD prompt sends required screenshot
inspection to the read-only `au-visual-review` subagent and the Studio MCP
tools before any NEEDS_HUMAN. It also tells the implementer to submit
committed frames for the coordinator's visual sign-off.

Tests:
- tests/coordinator/test_visual_model_approval.py
- tests/coordinator/test_lifecycle_engine.py

## v0.123.2 — idle Kilo session after a permission reply; unadmitted prompts

UI-02 stalled after a tool-permission rejection. Two defects combined.

**The permission reply reset the session timer.**
- Cause: answering a session's question reset its `prompt_ts`, though no new
  prompt was sent. Kilo's completed assistant turn then looked older than
  the prompt, so an idle session was mistaken for one still waiting for
  prompt admission.
- Fix: a reply now keeps the original `prompt_ts`.
- The Kilo adapter now compares the last assistant turn's completion time,
  not only its creation time.
- After a permission reply, the implementer gets one in-scope "continue"
  prompt in the same session (`permission_reply_continued`). It is not
  verified as an unfinished BUILD.

**Unadmitted prompts waited out the full timeout.** A prompt Kilo never
admitted waited out the 240-minute session execution timeout.
- Fix: a session that reports idle with no assistant turn for its prompt is
  now aborted after `[client] prompt_admission_timeout_seconds` (default 180).
- The session ends as `prompt_not_admitted`, and a free retry resumes it from
  recorded state.
- A slow model call reports busy and is not affected.

Tests:
- tests/coordinator/test_engine.py
- tests/coordinator/test_kilo_idle_after_reply.py

## v0.123.1 — UI-02 lead recovery loop: one fresh lead strategy, reconciled escalations

UI-02 hit two defects in the path from the Lead Manager back to the implementer.

- **The repeat guard swallowed the Lead's strategy.** After Lead case
  `rc-fef1688cc851dd9c` resolved, `Recovery.assignment` saw findings that
  had repeated three or more times. It reopened the repeated-strategy
  escalation (`dec-d046f1082ccff01d`) before any implementer tried the Lead's
  strategy, which opened another Lead case.
  - Fix: a validated, never-before-used Lead strategy now reaches exactly one
    implementer session.
  - The repeat set's still-open question is withdrawn and kept for the audit.
  - The assignment records `lead_strategy`.
  - A repeated strategy, a waiving one, or an ordinary untrusted one still
    escalates. The same Lead text gets no second pass.
- **Recovery decisions contradicted each other.** A new repeat set opens a new
  `recovery_escalation` with a new question. That escalation never named the
  older set's still-answered escalation (`dec-27e25b94f297203a`) as the one it
  replaces, so the operator's answer tripped the contradiction guard.
  - Fix: a new set's question now records `supersedes` = the task's newest
    answered escalation from another set. Answering it marks that one
    `superseded`.
  - Rows opened before this fix are reconciled the same way when they are
    answered (`decision_linked` event). The link is set only for an open,
    unlinked `recovery_escalation` of the same task that was opened after the
    answered one, and only once.
  - The guard itself is unchanged. No hand-editing of the database is needed.

- **A second Lead case was opened before the first case's strategy ran.**
  The old loop opened case `rc-826e3d9287aeb9c5` on the re-asked repeat
  question while rc-fef1's strategy was still pending in the task.
  - Fix: the engine now cancels such a case, journaled, with its sessions
    ended. The conditions are strict: the case is still only DIAGNOSING; its
    waits are open `recovery_escalation`s; the earlier case is resolved; the
    pending strategy is new to the task and waives nothing.
  - It also withdraws the question, kept for the audit. The task then resumes
    READY, and the pending strategy reaches exactly one implementer session
    with no extra Lead call.

Tests:
- tests/coordinator/test_recovery.py: new repeat set, legacy reconcile and
  the guard, fresh Lead strategy, waiving/repeated Lead strategy.
- tests/coordinator/test_lead_e2e.py:
  - `test_lead_strategy_after_three_repeats_reaches_exactly_one_primary_session`
  - `test_case_opened_on_an_unused_lead_strategy_is_reconciled_after_the_upgrade`

Each new test fails on v0.123.0 (or on `9495063`, where that commit had not
covered the case) and passes on v0.123.1.

## v0.123.0 — Lead Sprint Manager (last automated recovery tier, off by default)

Before an approved sprint asks Victor to resolve a blocker, the coordinator
first runs deterministic recovery. It then escalates to a tool-capable frontier
lead. Only after a bounded, journaled recovery does it ask Victor. Everything is
behind `[lead_manager] enabled = false` and the persona
`Foundry-Kilo-Lead-Manager`, which stays disabled until the safe-point
deployment. Design and runbook: docs/LEAD_SPRINT_MANAGER.md.

**Recovery cases (`lead.py`)**
- Every final BLOCKED/FAILED stop runs deterministic recovery first: evidence
  recovery, readiness, the stale-decision re-check (8809871), and re-verify
  when it resolves the stop. Only then is a durable `recovery_cases` row
  opened, deduplicated by a fingerprint without the revision.
- The lead Kilo session gets a fresh dossier, bounded at 40k and redacted, with
  model-written text marked as untrusted. It runs READ-ONLY in the game
  workspace: an allowlisted shell, with no `--output`/`--pre`/redirects and
  no `_secrets`. Snapshots before and after the session abort it if the game,
  the router checkout or the coordinator DB changed.
- It returns a validated `lead-report` classified as product, test_harness,
  evidence, environment, provider_quota, coordinator, scope_core or external.
  The report may never mark a check PASS, approve, answer a decision, waive a
  check, move BUILD work to CLOSE or change undeclared acceptance tests.
- The actions are: a game assignment to the sole primary (with bounded extra
  attempts under delegated policy), an isolated coordinator repair, wait,
  reverify, or ask Victor with one precise question that supersedes the
  stall decision.
- Bounds are enforced independently: diagnoses, candidates, upgrades,
  fingerprint repeats, case/resume/upgrade minutes (paused time excluded),
  quota calls, and per-task caps on cases, lead calls and extra attempts.
- Journaled op keys mean a crash never repeats a possibly-made paid call
  (uncertain is never resent), never double-grants and never half-applies.
- Fail-closed routing: readiness requires the persona to be enabled and
  strict, the Kilo worker key to be granted, the route chain to equal
  `expected_models` in order with nothing local, and the Kilo model limits to
  be present. Each call's served backend+model is checked through
  `/served`; a mismatch aborts, and nothing ever falls back.
- Operator controls: pause, resume, takeover and instruct on a case; cancel
  and unblock cancel the case.

**Isolated repairs and safe upgrades (`repair.py`, `supervisor.py`)**
- Coordinator defects are fixed in a git worktree on `lead/<case>` outside
  every protected checkout. Each candidate needs:
  - a reproducing `path::test` that FAILS at base and PASSES at the candidate,
    proven through junit;
  - focused and broader suites passing, with test ids validated and no pytest
    options;
  - unchanged test expectations: conftest and ini edits, removed asserts,
    early returns and duplicate tests are refused, and changes to existing
    tests need an operator decision;
  - an independent reviewer (never the lead persona) and a manifest.
  - Tests run with a scrubbed environment.
- `python -m foundry_coordinator.supervisor` runs outside the coordinator:
  - it pauses ALL projects at a safe point, verifies the candidate (review
    approved, manifest equals the diff, fast-forward, tests), and stops only
    a verified coordinator process;
  - it backs up the quiescent DB and evidence, installs with
    `merge --ff-only`, starts the new build and checks its health (the same
    pid, release and lead readiness);
  - on failure it restores the prior build with `reset --keep`, only when
    HEAD is the candidate, restores the DB only if it was migrated, and
    restarts the old build;
  - each step is journaled and resumable; `cancel_request` cancels an
    unstarted upgrade.
- The coordinator writes a pid file (pid + creation time) and a per-tick
  heartbeat (release, lead readiness, errors). It stops at a tick boundary
  only for a genuine supervisor stop request.

**Foundry server**
- `GET /v1/foundry/work/sessions/{id}/served`: served models, backends and
  routes per session, from the ledger, including errors and pending calls.
- `GET /v1/foundry/personas/{name}/routes[?client=]`: enabled, strict, the
  route chain with sources, and the client grant. Readiness uses it; no
  secrets are returned.
- New required purpose `lead_manager`, with a per-run cap (default 20).

**Control center**
- A Lead recovery section: DIAGNOSING / REPAIRING / UPGRADING / RESUMING /
  NEEDS_VICTOR / DEFERRED pills, the lead session, served model, current
  action, journal, counters vs bounds and upgrade status.
- Pause / Resume / Take over / Add instruction controls, and a high
  `lead_needs_victor` alert.

**Also:** commits 55ff628, bc0ddb6 and 8809871 (partial-BUILD carryover and
the auto re-check of stale BUILD blockers) ship in this release.

**Migration:** additive tables (`recovery_cases`, `recovery_actions`,
`upgrade_requests`) created only when enabled. Foundry must be deployed before
the coordinator. Keep `enabled = false` and the persona disabled until the
pilot.

## v0.122.6 — AnimalUprising profile becomes an example; local profiles are git-ignored

- New `foundry_coordinator/projects/animaluprising/project.example.toml`: a
  complete, commented copy of a real working profile.
  - Machine-specific values are marked `EDIT:` (workspace, Kilo and Foundry
    URLs, source root, machine name, personas, backends).
  - Paid or optional features are marked `OPTIONAL` with what they cost:
    helpers, lanes, retry guidance, lifecycle and training auto-upload.
- `foundry_coordinator/projects/*/project.toml` is now git-ignored and no
  longer tracked, because each machine keeps its own copy.
- Tests that read the AnimalUprising profile now validate the example.

**Migration (important if you pull into another checkout):**
- **This workstation:** your existing `project.toml` stays on disk and keeps
  working; git simply stops tracking it.
- **Any other checkout with an unmodified tracked `project.toml`:** pulling
  this release deletes it there. Copy it aside first, or recreate it from
  `project.example.toml`.
- **A checkout where the file was locally modified:** git refuses the pull
  until you move the file aside. Restore it after pulling.

## v0.122.5 — UI-02 false "no progress" fixed; pinned helper fallback route; BUILD continuing; upgrade resume

**UI-02 false no-progress block (coordinator).** Four bugs in one path:
- `[~]` checklist rows were invisible to the AnimalUprising parser. `[~]` is
  now an OPEN step everywhere the plan is parsed; `[x]` is done and `[ ]`
  is open.
- The three untracked gate files gate.ps1 writes for the task tripped
  partial-BUILD's clean check:
  - the files: `docs/progress/<task>_gate_expect.json`,
    `<task>_gate_expect_full.json`, `<task>_gate_verdicts.json`;
  - now only these exact task-owned paths are tolerated during partial BUILD,
    reported as "intermediate gate output";
  - final verification and closeout still require them committed;
  - other sprints' gate files and unrelated dirty files still block.
- No-progress detection counted committing sessions with repeated findings
  as stalls. Committed in-scope work, and plan steps advancing (`[ ]`→`[~]`→`[x]`
  or new `[~]` notes), now count as progress. A true stall (no commits, no
  checklist change, same findings) still escalates.
- Later commits in a session were lost because progress marks were written
  only at the first idle. They are now updated on later idles.
- New `python -m foundry_coordinator decision reevaluate <id> --by <operator>
  --reason "..." [--dry-run]`. It re-runs the fixed check on an open
  no_progress decision. If progress is now recognized it withdraws the
  decision, keeping it with an audit event, and never touches attempts or
  budgets.

**Helper admission: pinned fallback route (Foundry plus coordinator).**
- When a strict persona's first route is UNKNOWN or DEFER (e.g. a Meridian
  usage 429), a later route may admit the helper if it passes all of these:
  - ADMIT under the configured cutoff;
  - not local;
  - has image input when the helper needs it;
  - within approved_models;
  - a free helper stream.
- The session is bound with `pinned_route`, and Foundry serves its calls
  ONLY from that route.
- The call-time check re-evaluates the pinned route. If it is no longer
  admitted the call is refused (optional_deferred), never re-routed.
- Required work is never pinned.

**Control center.**
- **BUILD continuing.** Committed partial-BUILD progress shows as "BUILD
  continuing — N/M checklist steps open". It is neutral, not a finding, and
  uses no attempt. It is derived only from the engine's transition evidence.
- **Safe upgrade.** "Safe to upgrade" also waits for in-flight
  retry-guidance calls. It does not wait for a task's workspace or writer
  reservation held between sessions.
- **Resume after upgrade.** An opt-in pause option, "Resume automatically
  when the coordinator restarts on a newer version". Only tasks the project
  pause stopped are resumed, recorded as authorized by the original
  operator.

**Migration:** additive schema (progress marks v2). Deploy Foundry and the
coordinator together. See docs/UPGRADE_UI02_RECOVERY.md for the UI-02 steps.

## v0.122.4 — project Pause / Resume in the control center (for upgrades)

- **Header control.** A **Pause** / **Resume** button for the selected
  project sits in the control-center header.
  - **At a safe point** (default): nothing new starts. That covers task
    starts, sessions, prompts, reviews, closeout and helper dispatch. A model
    turn already running finishes. The pill reads "PAUSING — N running",
    then "PAUSED — safe to upgrade" once no session, helper or review is
    active.
  - **Now:** also aborts running sessions and helpers through the existing
    task pause. State is kept, and Resume continues the same sessions.
  - **Resume** resumes only the tasks this pause stopped. Tasks paused
    individually stay paused.
- **Persistence.** The pause survives a coordinator restart; nothing starts
  until Resume. While paused, an individual resume and a pause-and-steer
  instruction are refused.
- **Coordinator support.** The coordinator advertises the `project_pause`
  feature and reports `project_pause` status in its heartbeat and through
  the bridge. The server hands pause/resume actions only to coordinators
  that declare the feature.

**Migration:** none. Deploy Foundry and the coordinator together. An older
coordinator shows the button disabled ("update it").

## v0.122.3 — Meridian usage 429s no longer pause helpers; budgets show authorized room

- **Quota gate (Foundry).**
  - **Before:** Meridian's usage endpoint sometimes rate-limits itself
    (HTTP 429) while inference works. The gate then threw away all readings
    and deferred optional helpers as UNKNOWN until a read succeeded.
  - **Now:** a temporary fetch error (429, 5xx or timeout) falls back to the
    last good reading, which keeps its original observation time.
  - **Safety:** the normal freshness limit still decides, so an old reading
    gives UNKNOWN and helpers are never admitted on stale data. An auth error
    (401/403) never falls back.
  - **Alert:** a persistent failure still raises the debounced
    quota-source alert.
- **Budgets panel.** It showed "4/3 used up" after an operator unblock had
  authorized +3. The effective max now includes `authorized:<counter>` ledger
  rows, shown as "3 base + 3 authorized by operator decisions". The engine
  already enforced the right limit; only the display was wrong.

**Migration:** none. Rebuild Foundry for the gate fix; restart the coordinator
(bridge) for the budget view.

## v0.122.2 — retry guidance waits out a temporary quota refusal (tests aligned)

- **Behavior, from commit a408be0 (tagged with v0.122.1):** a temporary
  admission refusal, such as a usage-endpoint HTTP 429 or a defer, no longer
  fails the task. Retry guidance is "deferred" with a bounded retry timer
  (about 60 s, or the refusal's next_eligible). It makes no adviser call and
  spends no attempt while deferred, and asks the adviser once when quota
  recovers.
- **Tests:** the guidance tests now assert this, and an older
  `recovery_escalation` opened by such a 429 is withdrawn automatically.
- **Unchanged:** a hard skip (capacity full, persona disabled, adviser
  unavailable) still fails the task; an operator reset still re-arms it once.

**Migration:** none.

## v0.122.1 — the bell badge clears once alerts are acknowledged

The 🔔 badge counted acknowledged alerts whenever there were no new ones, so it
kept showing "1" after you acknowledged an alert. It now counts only what still
needs you: unacknowledged alerts and open questions or permission requests.
Acknowledged alerts stay in the inbox, marked "acknowledged", until they are
resolved, and the bell's tooltip still lists them.

**Migration:** none; reload the control-center page.

## v0.122.0 — sprint lifecycle: evidence memory, obligations, targeted recovery (opt-in)

An opt-in layer, `[lifecycle] enabled = false` by default, that stops the
evidence-loss loop seen on UI-01. Full operator documentation, settings,
rollout, rollback and the live-pilot procedure are in docs/SPRINT_LIFECYCLE.md.

- **Evidence ledger** (`evidence.py`):
  - exact tool results are recovered idempotently from every session of a
    task and stored as content-addressed blobs;
  - each result moves up the tiers claim < observed < verified < reviewed <
    approved;
  - freshness comes from dependency hashes and runtime identity, never from
    screenshot bytes alone;
  - the newest execution decides.
- **Obligations and decisions** (`obligations.py`):
  - BUILD, REVIEW, CLOSE and APPROVAL obligations, under a phase lock: a NEXT
    line or a summary can't move BUILD work to CLOSE;
  - operator decisions answered only through the control-center channel;
  - a budget ledger that only grows and records every authorization.
- **Failure classes and targeted repair** (`recovery.py`): ten classes,
  repeated-finding fingerprints, bounded assignments covering only the
  unresolved work, and escalation to one adviser consult, then an operator
  decision.
- **Engine** (when enabled):
  - readiness before a session: a block costs no attempt;
  - a review-submission gate with a journaled same-session continuation;
  - review outcomes routed as product changes, evidence incomplete or access
    blocked;
  - bounded evidence, memory and decision blocks in prompts;
  - advancement only on the engine's own completion.
- **AnimalUprising adapter:**
  - verdicts parsed only from real gate or runtime executions, never from
    reading a log;
  - stateful chains and order;
  - dependencies, runtime identity, obligations and readiness;
  - `_gate_binding` now reads the evidence log at the verified revision.
- **Helpers:** an explicit phase and reason vocabulary instead of a bare
  "failed", a disposition for every unit, and cross-kind duplicate detection.
- **Control center:**
  - new sections: Readiness, Required work, Decisions (with Answer), Budgets,
    Evidence, Recovered results, Repeated findings;
  - batch-id dedupe for the bridge;
  - a coordinator version and feature gate;
  - project-isolation fixes;
  - decision answers bound to the rendered question.
- **Compatibility:** server v0.122.0 or later for the new views and decision
  answers. Coordinator v0.122.0. Mixed versions degrade gracefully.

**Migration:** additive tables only, created on first use when enabled. To
roll back, set `enabled = false` or check out v0.121.10.

## v0.121.10 — quota-source alert no longer flaps

Meridian's usage endpoint sometimes returns HTTP 429 while inference keeps
working. Each 429 raised "Quota reading for meridian unavailable", and the next
good reading resolved it. The alert re-opened as new every time, which also
wiped the operator's acknowledgement.

- **Debounce:** the alert is raised only after the usage source has failed
  continuously for `helper_policy.source_alert_after_seconds` (default 600).
  It resolves only after `source_resolve_after_seconds` (default 300) of good
  readings. Admission itself is unchanged: an unreadable source still means
  UNKNOWN, so helpers wait.
- **Acknowledged alerts stay acknowledged:** an alert that was acknowledged,
  cleared and comes back within 6 hours at the same or lower severity reopens
  as acknowledged. It shows "reopened" in its history, with no new badge or
  sound. A worse severity, or a return after 6 hours, alerts as new.

**Migration:** none.

## v0.121.8 — BUILD review packets: phase contract, revision-bound evidence, code-first diffs

UI-01's BUILD review (rev-90fe3549c3eb) asked for CLOSE work before approving
the step that starts CLOSE. It never saw the source changes, because the
whole-task diff was cut in path order and the 545 KB evidence log came first.
It also read evidence from the working tree. Review packet v2 fixes all
three. Everything below is tested against fakes; it is not yet proven live.

**Review phase contract**

- Each adapter states what a review covers (`review_contract`).
  - Projects with a separate CLOSE session (`closeout.mode = "session"`) get a
    BUILD contract. The CLOSE duties are listed as deferred and verified later:
    for AnimalUprising, SUMMARY, the STATE row and state lines, MASTER_ORDER,
    the final plan step, done_check and the push.
  - Reviewers may not demand those duties as BUILD blockers or treat them as
    done. They go in `residual_risks`.
  - Missing BUILD evidence still blocks, and an APPROVED verdict that lists
    missing evidence is still refused.
  - One-phase projects keep the whole-task contract.
- AnimalUprising builds the deferred list and the CLOSE prompt from the same
  structured source.

**Evidence from the reviewed commit**

- The task spec, scope and plan are read from the reviewed commit
  (`describe_at`), not the working tree. The packet cites plan path,
  revision and blob.
- Evidence items (`review_evidence`) are read with `git show` at the revision:
  - the last gate block, quoted raw with line numbers;
  - suite verdict lines quoted raw, with nothing summarized as PASS;
  - the BUILD checklist state;
  - required captures, with their blobs.
- Each item cites path@revision, blob and lines, and is marked ok, missing,
  stale or unverified. A gate run on another baseline is marked stale, using
  the same rule as gate binding.
- Adapters without the new method fall back to their old evidence, marked
  unverified.

**Code-first diffs and an honest budget**

- Diffs are assembled per file in priority order: source, tests,
  plan/config, docs, then raw logs and evidence.
- A `## Coverage` section lists every changed file as included, partial or
  omitted, with its size, and says whether all source/test changes are
  included.
- The packet budget comes from the reviewer persona's advertised context and
  output limits, as Kilo's effective config reports them. If those aren't
  available it uses `[review] max_packet_chars` (default 200,000 characters).
- If the source/test diff or essential evidence cannot fit, the reviewer is
  not called. The task blocks as "insufficiently inspected" instead of getting
  a partial review.
- Findings from earlier reviews of the task are listed, so the repaired head
  is checked against them. Historical reviews are untouched.

**Journaling across the format change**

- The review op key carries the packet version and a per-head review count.
  An old v1 "done" key can't cause a skip loop.
- An old v1 "started" op is closed as uncertain and never replayed.
- An old-format approval at the same head is not reused (`review_not_reused`).
  It stays in the audit trail and still counts toward `max_review_calls`.
- Independent reviewer selection and verdict validation are unchanged.

**Migration:** none. Deploy at a safe boundary; see the UI-01 recovery handoff.

## v0.121.7 — live-audit fixes: compaction, lost responses, stale images, guidance, operator notes, diagnostics

Fixes for defects found in the live UI-01 audit, including the items still open
after v0.121.4. All are covered by tests against fakes, not real models.

**Compaction (stage 1)**

- A turn that ends on Kilo's compaction summary now reads as busy, not
  finished. Detected by the summary flag, a compaction mode or agent, or a
  compaction part. Before, the summary looked like a finished turn and could be
  sent to verification.
- That turn is never verified and costs no attempt. The coordinator logs
  `session_compacted` and waits.
- If Kilo doesn't continue on its own within `compaction_grace_seconds`, the
  same session gets one journaled continue prompt (once per compaction message,
  so a restart doesn't resend it).

**Lost helper responses (stage 2)**

- Helper session create and prompt are journaled before they are sent.
- Helper session titles now start with a `[unit#request]` marker. After a lost
  create response the coordinator finds the session by that marker and adopts
  it, so there is no orphan session and no twin.
- After a lost prompt response, Kilo's own messages decide whether the prompt
  arrived. A model call is never replayed blind. Both cases log
  `helper_reconciled`.

**Stale screenshots and visual base ids (stage 3)**

- A capture is bound to the source snapshot it was planned against. If the code
  changed before observation finished, observer and proposer results are marked
  stale, and the card says to recapture or reassess.
- The visual base check now uses separate `artifact_id` and
  `source_snapshot_id` fields, with a compatibility path for older results.
  This fixes `base_mismatch` on every visual result.

**Retry guidance (stage 4)**

- A guidance record skipped before any inference (quota 429, capacity full,
  adviser unavailable) no longer turns guidance off for good.
- `unblock --reset-attempts` moves it to `retry_guidance_history`. The next
  exhaustion asks quota admission again, at most twice per task.
- Consultations that are calling, unknown or completed are never re-armed or
  replayed. The unblock transition now records `reset_attempts`.

**Operator messages (stage 5)**

- A session-scoped instruction whose session can no longer take a prompt (a new
  session took over, or the task stopped) is rejected with that reason. Before,
  it stayed queued forever.
- The unblock resolution is queued as a task-scoped note. It is delivered once
  with the next prompt, journaled like any note, and kept out of control-center
  action reporting. Before, it never reached the session.

**Diagnostics (stage 6)**

- `doctor` accepts `pass`, `passed`, `ok` and `valid` as validation results.
- `doctor` checks media personas through Foundry's media catalog. That check
  never FAILs and never generates anything.
- The Verification panel labels checks and reviews from a previous run, warns
  when they are for an older revision, and shows an authorized operator
  recovery (`reset_attempts`, read from the unblock transition) instead of a
  bare 3/3.

**Trade-offs**

- **Kilo stays the only compaction manager.** The coordinator never summarizes
  or trims context; it only waits and nudges once. If Kilo stalls after that
  nudge, the session's normal timeout still applies.
- **A lost helper prompt with no way to check is treated as sent, never
  replayed.** If the client can't report whether Kilo has the prompt, the unit
  may wait out its timeout with no reply. That costs time, not a duplicate
  model call.
- **Guidance re-arm happens only on an operator reset, at most twice.** A
  transient skip still turns guidance off until an operator acts; nothing
  retries it automatically.

**Not in this release**

- Delegated visual sign-off. That is a separate policy change.
- Primary image input. That is persona and Kilo configuration.

**Settings:** `[client] compaction_grace_seconds` is optional (default 30). It
sits in the profile's `[client]` table, next to `kind`.

**Also fixed:** a v0.121.5 regression. Every new run re-paid an incremental
review of code that an earlier run had already reviewed. A finished,
non-rejected review in any run of the task now counts. A review that never
ran, or whose result expired, still lets the new run review the same code.

**Review fixes (before release)**

- A refused unblock no longer leaves a resolution note behind. The note and the
  transition are written together, once per unblock.
- A compaction turn that ended in an error (overflow, budget refusal) is
  handled as an error, not as "compacting".
- Compaction nudges are capped per session (`[client] max_compaction_nudges`,
  default 2). A session that keeps compacting without finishing is ended like a
  stall, and a fresh session continues. Before, each nudge restarted the
  session timeout.
- Queued operator notes now go with every prompt the coordinator sends to their
  session: the compaction nudge, helper hand-off, proposal delivery and frontier
  turns. A refused prompt puts its notes back in the queue.
- Each helper create attempt gets a unique reconcile marker, so a re-admitted
  unit can never adopt a session that was aborted earlier.
- New indexes for the operator-note and recovery-label lookups. A malformed
  task no longer breaks the control-center overview.

**Migration:** none; deploy at a safe boundary (no hot reload of a running worker).

## v0.121.6 — Sonnet primary for scoped UI tasks

- A profile can now pick a different writer for named tasks with
  `[role_overrides.<name>]` (`tasks`, `implementer`, `reviewer`). The
  AnimalUprising workstation uses it to put `Foundry-Kilo-Sonnet` on UI-01 to
  UI-06, with `Foundry-Kilo-Deep-Review` as reviewer.
- The default `[roles] implementer` stays `Foundry-Kilo-Local`. Every other
  task, including the security sprints in between, is unchanged.
- The override covers implementation, repair, closeout, same-session resume and
  helper feedback delivery. Resume registers the selected persona with Foundry
  before inference.
- Sessions keep earlier writer personas, so a writer can't become the
  independent final reviewer after a model change.
- Overrides never approve queue entries, widen scope, waive checks or change
  publishing rules. Details and the hand-off procedure:
  docs/UI_SPRINT_MODEL_POLICY.md.

**Migration:** none. The override is optional and read at coordinator startup.

## v0.121.5 — recover bounded helper work and use frontier capacity

The UI-01 audit found no new helper starts after model access was repaired.
Details: docs/HELPER_UTILIZATION_FIXES.md.

- Coalescing is scoped to the current run and a stable `coalesce_key`. Failed,
  cancelled, rejected or stale results are no longer reused. The new column is
  additive; no history is deleted.
- Replacements are bounded by `1 + lanes.max_retries` per assignment and run.
  Lane allowances are per run (`max_per_run`, falling back to `max_per_task`);
  the task-wide ceilings still apply.
- A review checkpoint is recorded only after a structured result names its
  assignment and source snapshot.
- Real Studio evaluation errors trigger triage. Reading or searching a test
  file no longer counts as a failed test.
- New optional `helpers.backend_max_running` (per-backend cap) and
  `lanes.high_value_lanes` (lanes eligible for selective fallback; Meridian
  stays first). Foundry stays authoritative for routes, quota and capacity.
- `Proactive.recover_current` rebuilds current test design and code review for
  a paused task, once per run, without replaying old events.
- Windows path fix in the image adapter.

**Migration:** none on the Foundry server. Restart the workstation coordinator
to load the changes.

## v0.121.4 — crash-safe first start, honest helper stops

Fixes for the P1 defect and the top P2 defects from the v0.121.3 scenario
review. All are covered by tests against fakes.

**Coordinator**

- **P1 fix: no second writer session after a crash.** If the coordinator dies
  after Kilo accepts the first prompt but before the task is recorded as
  IMPLEMENTING, a restart now resumes the same session. Before, a restart
  started and prompted a second writer session in the same workspace.
  - The run id is kept in `pending_start` until the transition.
  - The prompt is journaled as "prompting" before it is sent.
- A prompt that may have been sent before the crash is never sent again. The
  coordinator logs `session_prompt_uncertain`. A session that never answers
  still ends by `session_timeout`.
- A prompt Kilo refuses is retried on the same session. It no longer starts a
  new one.
- The continue prompt after an auto-resume is journaled once per pause, so a
  crash no longer sends it twice.

**Helpers**

- **Silent startup failure:** a helper session that Kilo reports idle, with no
  reply to its prompt, fails after `startup_grace_seconds` (default 180).
  Before, it waited the full 30-minute timeout.
- **Mid-run refusal:** when Foundry refuses a running helper (for example at the
  quota cutoff) and Kilo goes into retry, the helper is aborted and deferred
  until the refusal's retry time. It then starts in a new session without
  using up one of its re-requests.
- Foundry's `optional_deferred` code now maps to the `budget` category, on both
  the server and the coordinator.
- **Unconfirmed abort:** when Kilo does not confirm an abort, the unit stays
  running with its Foundry binding, so its calls are still gated. The abort is
  retried each poll and logged once as `helper_abort_unconfirmed`. Before,
  the unit was marked failed and unbound while the session might still be
  running.
- **Admission outage:** a failed admission check is asked once per persona per
  tick. Before, it was asked for every unit and backend, at 20 s each. Any
  error now defers the unit with a reason; before, some errors left it
  re-asked on every tick.
- Foundry's client turns a non-JSON reply, a cut-off body or a reset into
  `FoundryError`.
- One helper session whose status can't be read no longer stops the rest of
  the helper queue. Its timeout still runs.

**Still open** (marked as expected-failure tests):

- A lost response can cause a duplicate helper prompt or an orphan session.
- No Kilo compaction recovery.
- The unblock resolution never reaches the session.
- A session-scoped note is left queued after its session ends.
- Visual: the stale-image warning, and the `base` check.

**Migration:** none. `[helpers] startup_grace_seconds` is optional.

## v0.121.3 — review fixes: lanes, delivery journal, training, quota probe, control center

**Coordinator and lanes**

- Lane units no longer use up `max_units_per_task`; hitting the cap now logs a
  `helper_limit` event instead of dropping the unit silently.
- When a session fails, its running lane units are cancelled and its undelivered
  lane results expire. A new session only receives proposals for its own run, and
  events from an earlier run expire.
- A failed bind or prompt to the primary no longer leaves a journal entry that
  blocks the delivery forever ("uncertain, not repeated"). A frontier start
  prompt that fails reverts the phase to `requested`.
- Failure triage coalesces per failure signature and code state: the same
  failure after a code change is triaged again.
- Pause now holds queued lane work (it resumes on unpause) instead of dropping
  it. Visual retention prunes per project.
- Filesystem-fallback artifacts are observed again (they were never planned).
- A frame the vision model calls invalid, occluded or missing its subject
  delivers no findings, only "recapture".
- Artifacts: going back to earlier bytes (A→B→A) is the current generation
  again. Drift checks never open paths outside the workspace.
- The Kilo provider-filter preflight reads Kilo's config once per dispatch,
  not once per candidate.
- `unblock --reset-attempts` no longer carries over a guided extra retry. The
  adviser stays excluded from reviewing the work it guided.

**Quota refresh probe (Foundry)**

- Helper admission no longer waits on the refresh probe. When a reading has
  only gone stale while idle, admission answers UNKNOWN at once ("refresh probe
  started"), the probe runs in the background, and the coordinator's next poll
  sees the result. Before this, a slow probe could outlast the coordinator's
  20-second admission timeout.
- The "verified serving" rule is unchanged: no probe while a real call or probe
  has succeeded within `health.fresh_minutes`.
  - **Trade-off:** on a CLIProxyAPI pool, "serving" is per backend, not per
    account. One account used recently keeps the whole backend "serving", so a
    different account's stale reading is not probed. Admission stays UNKNOWN
    (deferred, never admitted blind) until that window passes, the account is
    used, or **re-check** is pressed. That is at most `health.fresh_minutes`,
    15 minutes by default.

**Training auto pipeline**

- Duplicate detection uses the newest dataset for each project, kind and
  target.
- Duplicate or empty exports are removed.
- One failed upload no longer stops the others.
- Uploads Foundry refuses permanently are recorded as `refused` and not
  retried: HTTP 409/413/422, or a dataset over 190 MB.
- A manifest for a project this run doesn't know stays in the spool for the
  run that does, instead of being marked done unread.
- A lock left by a dead process is taken over.
- The upload endpoint checks `Content-Length` before reading the body.
- An unreadable or malformed manifest is reported and left in the spool. It
  no longer stops every manifest after it.
- A run with upload off no longer erases `uploaded` or `refused` statuses, so
  the next upload-on run does not re-send those datasets.
- A newly created lock whose owner has not yet written its pid is respected.
  This closes a POSIX race.
- **Docs corrected:** `auto` does upload. When upload is on, it POSTs the
  redacted datasets to your own Foundry router (`[foundry] url`), never to an
  external training provider. Set `[training.auto] upload = false` to keep
  everything local. The comment at `project.toml` line 119 still says
  "Nothing is uploaded"; that file is left for you to edit.

**Control center**

- Lanes **Pause** is project-wide. It no longer fails with "stale state" and
  works without a selected task.
- The lanes panel follows the selected project and task, and open diffs stay
  open.
- Pop-outs are tracked per project and per window, with a 75-second
  heartbeat.
- The narrow layout respects popped panes, and session links no longer jump the
  page.
- A malformed thumbnail returns 404 instead of 500.
- A graph redraw keeps focus and never interrupts a drag.
- **Active only** keeps queued, waiting and deferred lane work.

**Migration:** none.

## v0.121.2 — helpers no longer hang 30 minutes on a model Kilo filters out

**What changes**

- **Fix:** before admitting a Kilo-run helper, the coordinator checks Kilo's
  effective config for the helper's persona. If the `foundry` provider's
  `whitelist` leaves the persona out, or its `blacklist` includes it, the unit
  is deferred with that reason and no session is created.
  - Before this, Kilo accepted the prompt and then silently refused the model.
    The helper session held only the user message, looked busy, and was stopped
    as "failed" after the 30-minute helper timeout ("model ?" in the control
    center).
- **Recovery:** after the config is fixed, the unit dispatches on its next
  scheduled poll with no lost attempt.
- **Scope:** direct (vision) lanes don't go through Kilo and are unaffected.

**Migration:** none. If helpers were timing out, add the helper personas
(`Foundry-Kilo-Helper-Claude`, `Foundry-Kilo-Helper-GPT`) to Kilo's provider
whitelist, or remove them from the blacklist.

## v0.121.1 — popped-out transcript panes leave the main page too

**What changes:** a transcript pane popped out with ⧉ is now hidden from the main
page while its window is open, like sections and the graph. The other panels take
its space; if nothing else is shown, a placeholder says where the transcript is.
It returns when the pop-out window closes. "Worker transcript ↩" shows it in the
main page as well. If the main pane follows the coordinator to a new session, it
shows again (the pop-out stays pinned to the old one).

## v0.121.0 — readable route graph, Active only, pop-outs and floating cards

**What changes** (control center, presentation only):

- **Readable Route / activity graph:** it is laid out for its box. Wide strips
  spread out instead of shrinking, labels stay readable, anything that doesn't
  fit scrolls, and it re-fits on resize. Zoom with −, Fit, + and Ctrl+wheel.
- **Active only:** a switch that shows just the agents working now and the
  path they use. The history view is still available.
- **Placement and pop-out:**
  - The graph can sit at the bottom, at the side or floating, saved per layout
    preset.
  - The graph can be popped out into its own window.
  - A popped-out section or graph is hidden from the main page until its window
    closes.
- **Floating cards:** any Inspector section (and the graph) can float as a card
  you drag and resize. It has keyboard move/resize, positions saved per preset,
  and is hidden on narrow screens.
- **Fixes:** one shared graph panel (switching placement no longer duplicates
  it), and no layout pass before the page has booted.

**Migration:** display preferences only. Existing presets gain `graphPos`,
`graphW` and `floats`; older saved preferences keep their sizes and order.

## v0.120.0 — refresh a stale quota reading with a probe before deferring a helper

**What changes**

- **Fix for helpers deferred as UNKNOWN after idle periods.** When a coordinator
  asks whether a helper may run and the backend's state is unknown, Foundry sends
  one tiny "Can it serve?" probe to refresh the usage reading, then decides.
  Unknown means no successful call or probe within `health.fresh_minutes`, and a
  usage reading that went stale while idle.
- **Never probes** when the backend was verified recently, the reading is fresh,
  the last reading was at or over the cutoff, the source is broken, or the
  backend is busy. At most once per 10 minutes per backend.
- **Settings:** `helper_policy.refresh_probe` (default on) and
  `refresh_probe_min_interval_seconds`, in the control center's helper policy
  settings.

**Migration:** none (new config keys with defaults).

## v0.119.1 — scoped source-snapshot ownership (v0.118.0 review fix)

**What changes**

- **Fix:** `source_snapshot()` ids were HEAD plus manifest only, and the table's
  INSERT was skipped when the id already existed. Two projects with identical code
  therefore shared one row, owned by whoever snapshotted first, and
  `snapshot_file()` read from that project's blob root.
  - Identity is now scoped to project, task, run and workspace.
  - `content_fp` keeps the "unchanged code" comparison, so incremental review still
    never repeats unchanged code, including across runs.
  - `snapshot()`, `snapshot_file()` and `drift()` require the caller's project (and
    task) and refuse foreign records.
  - A collision with another owner's row raises instead of adopting it.
- **Docs:** `approved_models` only adds a restriction. It never removes a text-only
  fallback, so the visual persona chain itself must contain only vision-capable
  routes.

**Migration:**

- Additive: `source_snapshots.content_fp` and `workspace`, plus an owner index.
- Legacy v0.118.0 rows are untouched and readable only by their recorded owner.

## v0.119.0 — automatic training data after every sprint

**What changes**

- **Automatic run after a task closes:** the coordinator starts
  `foundry_training auto` in the background after writing the closeout manifest.
  It parses and curates the sprint, exports validated fine-tuning datasets
  (`chatml-tools`, `canonical`) only when the selected episodes changed, and
  keeps them in one library: `~/.foundry-training/library/` with `INDEX.json`,
  `README.md` and `<project>/<kind>/<format>/latest.jsonl`.
- **Upload to Foundry:** each new valid train dataset is uploaded. If the upload
  fails, the dataset stays in the library and the next run retries. No LLM is
  used.
- **Foundry:**
  - `POST /v1/foundry/training/datasets/{id}` (orchestration scope): secrets are
    re-checked on arrival; eval sets, sha mismatches and conflicting ids are
    refused.
  - `GET /admin/api/training/datasets` and a download route.
  - A "Fine-tuning datasets in Foundry" card in the Training tab.
- **Tests:** fixed three Windows-only fixture bugs in `tests/training` (a TOML
  path, a UTF-8 read, `/tmp`). That suite now passes on Windows.

**Migration:**

- Additive: Foundry table `training_datasets`.
- Automatic runs start for every project whose profile has
  `[training] enabled = true`. Turn them off with `[training.auto] enabled = false`
  or `FOUNDRY_TRAINING_AUTO=0`.

## v0.118.0 — proactive frontier lanes (all default off)

**What changes**

- **Proactive helper lanes** on the existing helper journal. Full guide:
  docs/PROACTIVE_LANES.md.
  - Visual observer: a direct vision call with the actual capture bytes, and image
    modality verified for each route.
  - Code proposer.
  - Failure triage.
  - Incremental review.
  - Independent test design.
  - Documentation research.
  - Evidence reconciliation.
  - Handoff and preflight.
- **Triggers come from the busy primary's completed tool results.** They are read
  with GET only and classified deterministically. A naked file watcher never
  spends money.
- **Immutable lineage:**
  - Content-addressed artifact snapshots with generations and supersession.
  - Two-read source snapshots (HEAD plus a file manifest).
- **Queue:**
  - Dependency-aware, with priority and aging, coalescing, and per-lane and
    per-task caps.
  - Shares the aggregate cap of three and Foundry's per-backend ceilings.
- **Delivery:** batched action cards, only at the primary's idle safe point. The
  primary acknowledges via `helper_feedback`.
- **Control center:**
  - "Proactive lanes" panel.
  - Artifact → observer → proposer → primary → recapture lineage in the graph,
    animated only while the activity is observed.
  - Operational `lanes` pause action.
  - `helper_violation` alert.
- **Optional frontier-primary phase:** manual, same session, verified at runtime,
  default off.
- **Foundry:**
  - New optional purposes.
  - `/v1/foundry/helpers/admission` reports `vision` for each route.
  - Lane tables and endpoints in the control center.

**Migration:**

- Additive only. New columns on `helper_units`.
- New coordinator tables: `artifacts`, `source_snapshots`, `fs_watch`,
  `lane_events`, `lane_cache`, `primary_phases`.
- New Foundry tables: `cc_lane_units`, `cc_artifacts`, `cc_lane_state`.
- Nothing is enabled unless `[lanes]` or `[frontier_primary]` is set in a profile.
- Roll back by removing those sections or by deploying v0.117.1.

## v0.117.1 — helper request lifecycle fix

**What changes**

- **Fix:** `helper_requests` in a primary's coordinator-report are now consumed at the safe
  point where the primary went idle, before BUILD can move to VERIFYING, and dispatched
  right away.
  - In v0.117.0 the task left IMPLEMENTING first, so 0 units were created.
- **Bounded hand-off:**
  - Proposals are delivered once to the current primary.
  - Admitted helpers that are still running get up to `[helpers] handoff_wait_minutes`
    (default 15). After that they're aborted (`timed_out`), and a late result is never
    delivered.
  - Deferred/UNKNOWN units are `skipped` and never hold the primary.
  - A primary that stopped to ask is told the outcome once.
- **Robustness:**
  - Pause and cancel abort running helpers.
  - A helper session is bound before its prompt even across a restart.
  - Proposal and outcome deliveries are journaled and never resent.
  - A skipped or timed-out unit can be requested again up to `max_unit_requests`.
- **Prompts:** implementer prompts (all adapters) explain how to request helpers when
  `[helpers]` is enabled.
- **Version:** `foundry_router` 0.117.1. The Foundry service itself is unchanged; this is
  coordinator code.

**Migration:**

- Additive: `helper_units.requests` (added on start).
- New optional `[helpers]` keys: `handoff_wait_minutes`, `max_unit_requests`,
  `max_requests_per_report`.
- Helpers stay off until `[helpers] enabled = true`.

## v0.117.0 — backend-aware optional coding helpers

**What changes**

- **Helper gate:** optional coding work (a purpose the coordinator's binding sets)
  is admitted only while every applicable window of the serving account is below
  `helper_policy.cutoff_pct` (70% used) and freshly observed. Unknown, stale or
  unreadable quota is never admitted. Required work and unbound traffic keep the
  existing guardrails. See docs/HELPER_POLICY.md.
- **Coordinator helpers:** the profile's `[helpers]` section dispatches read-only frontier
  helper units, each bound in Foundry before it runs. Units are deferred and resumed with
  persisted reasons and handed to the idle primary. Review stays independent of helper
  authors.
- **Control center:** a Helper capacity & quota inspector section, Helper policy settings,
  and a persistent, silent alert when a quota source fails.
- **Engine fix:** sessions the engine binds now carry an explicit required purpose
  (`implementer` / `closeout` / `review`).

**Migration:** additive only.

- New nullable columns: `work_sessions.purpose`, `assignment`, `worker_client`;
  `usage_events.purpose`.
- New tables: `helper_quota_state`, `helper_admissions`.
- `helper_policy` defaults to off, so optional-purpose calls are refused until you enable
  it. Nothing else changes.
- Personas, client allowlists, endpoints, budgets, backend limits and credentials are not
  touched. No inference or validation runs at startup.

**Manual steps:** see docs/HELPER_POLICY.md → Rollout (Foundry policy, Kilo `au-helper`
agent, two helper personas, coordinator `[helpers]` section).

## v0.116.0 — canonical default review persona

**What changes**

- **New default persona `Foundry-Kilo-Review`** (seed v11, `foundry_router/db.py`): the
  coordinator's normal reviewer. Its stored policy:

  | Field | Value |
  |---|---|
  | kind / mode | chat, `execution_mode: direct` |
  | routing | `routing: strict`, `backend: null` (automatic: whichever backend serves the pin) |
  | `pinned_models` (ordered) | `claude-sonnet-5-5`, then `gpt-6.1-sol` |
  | `model_allowlist` | exactly those two models |
  | `fallback_policy` | `refuse` (no silent local fallback) |
  | `reasoning_effort` | medium |
  | `context_window` / `max_output_tokens` | 200,000 / 4,096 |
  | `capabilities` | `["text", "tools"]` (no image input until a real image test) |
  | `enabled` | 1 |
  | `validation` | none: creating a persona isn't validating it |
  | `client_compat` | notes for the coordinator and Kilo |
  | `profile` | `foundry-default:kilo-review@1` (records that the seed made it) |

  Startup makes no inference call and writes no "passed" status. Validate the persona
  yourself (Routing → Personas → validate; about five subscription calls).
- **Generated Kilo config** (`/admin/api/personas/kilo-config`):
  - Model names show the role (`Foundry-Kilo-Review (normal reviewer)`) or mark legacy
    aliases (`(legacy alias, not a coordinator role)`).
  - For strict personas, the context limit is now the minimum across the whole ordered
    chain (declaration and every known model window), not just the first reachable pin.
    `?explain=1` (shown as comments in the UI) names the source of each limit, unknown
    windows and unreachable pins.
  - `/v1/models` and `/api/show` use the same rule.
- **Setup → Clients & keys**:
  - The **coordinator** preset pre-fills a role-based allowlist (`Foundry-Kilo-Review`,
    `Foundry-Kilo-Deep-Review`) and explains it.
  - Existing clients with `orchestration` scope whose allowlist lacks
    `Foundry-Kilo-Review` show a "missing Foundry-Kilo-Review (reviewer)" warning,
    which also names any legacy alias they rely on.
- **`python -m foundry_coordinator doctor`**: when the coordinator key can't see a
  reviewer that Kilo's key can, the failure now says to add that exact id to the
  coordinator client's allowed personas. Worker and helper models are still checked
  with Kilo's key, so a frontier-only coordinator key passes.

**Migration behaviour** (runs once, flag `persona_seed_v11_kilo_review`)

| Database | Result |
|---|---|
| Fresh | the complete persona above |
| Upgrade without a `Foundry-Kilo-Review` | it is added, complete |
| Upgrade with one (customized, disabled, other pins/backend/limits) | left exactly as is; the event log says "already exists; left unchanged" |
| Repeated start | nothing happens |
| You delete it later | it is not re-created |

Legacy personas (`Foundry-Kilo-Sonnet`, `-Opus`, …) are not renamed, deleted or
changed. **No client allowlist is changed.**

**Manual reconciliation for existing installs**

1. Setup → Clients & keys: on a coordinator client showing *missing
   Foundry-Kilo-Review*, click Edit and add `Foundry-Kilo-Review`. Keep the other
   entries. A legacy alias in the list does not authorize it.
2. If you already had your own `Foundry-Kilo-Review`, compare it with the table above
   (it was not changed).
3. Regenerate the Kilo provider config and merge it into `kilo.jsonc` if you want the
   new labels or limits. A saved file is not updated automatically.
4. Validate `Foundry-Kilo-Review` once.

The AnimalUprising workstation already did steps 1 and 2 on 2026-10-17
(docs/COORDINATOR_WORKSTATION_RUNBOOK.md); upgrading leaves that configuration
alone.

## v0.115.0 — coordinator control center

See docs/CONTROL_CENTER.md, which includes the deployment steps.
