# Release and migration notes

Newest first. Each version is tagged `vX.Y.Z` (docs/CONVENTIONS.md).

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
