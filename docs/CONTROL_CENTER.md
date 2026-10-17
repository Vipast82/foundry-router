# Coordinator control center

Foundry v0.115.0. One page for a coordinator's work: live transcripts of the worker
and its helpers, published reasoning, tools, to-dos, MCP and routes; an action-required
inbox with visual and audio alerts; detailed replies to questions; permission and visual
approvals; operator instructions; and coordinator commands.

Open it from **Dashboard → Work**: the **Open control center** button, a task's
**live view ↗**, or a session's **open ↗**. You can also go to `/ui/control` directly.

Looking is always read-only. Every change is a typed **operator action**. The
coordinator process that owns the engine re-checks it and applies it with its own
methods. Foundry never edits coordinator state, never talks to Kilo, and the page
never runs model calls.

```text
 Windows workstation                                         Foundry (192.168.0.112:11435)
 ┌──────────────────────────────────────────────┐            ┌──────────────────────────────┐
 │ kilo serve :4096 ◄──GET + /event (read-only)─ telemetry   │ POST /v1/foundry/telemetry/* │
 │        ▲                                    bridge ──────►│  redact → store → alerts     │
 │        │ prompt / reply / abort             (bridge cmd)  │  watchdog (own timer)        │
 │ coordinator engine (run --control) ◄─claim/report──────── │ /v1/foundry/control/*        │
 │   SQLite store (single owner)                             │                              │
 └──────────────────────────────────────────────┘            │ /control/api/* + SSE ◄──── browser(s)
                                                              └──────────────────────────────┘
```

## Setup

1. **Foundry** (after this version is deployed): Setup → Clients & keys.
   - Make a person key with the **operator** preset (scope `operator`). Optional:
     restrict it to projects (`projects: [animaluprising]`).
   - Optional: make a **bridge** key (scope `telemetry`) for the bridge. If you skip it,
     the bridge uses the coordinator key (`orchestration` is accepted for telemetry).
     The coordinator key does **not** get admin, and its persona allowlist is untouched.
2. **Workstation bridge** (read-only; safe next to a running coordinator). In a terminal
   that has `KILO_SERVER_PASSWORD` and `FOUNDRY_COORDINATOR_KEY` loaded (see the runbook):

   ```powershell
   .\.venv\Scripts\python.exe -m foundry_coordinator --profiles .\foundry_coordinator\projects bridge
   ```

   `--once` makes one pass and exits. `--replay N` sets how many existing sessions to
   re-read from Kilo at start (default 40). `--no-events` polls only. To use a separate
   telemetry key, add `[telemetry] key_env = "FOUNDRY_TELEMETRY_KEY"` to the profile.
3. **Operator actions** need the engine owner to run the control channel. This means
   restarting the coordinator loop with `--control` (and optionally `--telemetry`,
   which runs the bridge in the same process):

   ```powershell
   .\.venv\Scripts\python.exe -m foundry_coordinator --profiles .\foundry_coordinator\projects run --poll 3 --control
   ```

   Until then, actions wait as **accepted** and expire unclaimed. Nothing is sent.
4. Sign in at `/ui/control` with the operator key. Foundry sets an HttpOnly,
   SameSite=Strict cookie (path `/control`, 12 h by default). The page doesn't keep the
   key.

Optional profile settings:

```toml
[telemetry]
context_checkpoints = [140000, 190000]   # markers on the context bar (operational, not limits)
key_env = "FOUNDRY_TELEMETRY_KEY"        # bridge key; default: [foundry] key_env
```

## What it shows, and where it comes from

| Area | Source | Notes |
|---|---|---|
| Task state, phase, attempt, active session, transitions, findings, checks, reviews, approval card | coordinator SQLite store, opened read-only by the bridge | The coordinator's verdict is shown as is. The page never marks anything done. |
| Sessions tree (worker, repairs, closeout, helpers) | coordinator `sessions` + Kilo `/session/{id}`, `/children` | Finished sessions stay in history. "missing" means Kilo returned 404. |
| Session status | Kilo `/session/status`; absent means idle | Each status shows where it came from. A Foundry binding alone shows as "bound (no Kilo data)". |
| Messages, text, published reasoning, tools, retries, compaction | Kilo `/session/{id}/message` plus `/event` (`message.part.updated`, `message.part.delta`, …) | Only the reasoning text Kilo publishes. Provider metadata, including encrypted reasoning, is dropped. If there's no reasoning part, the page says "Not provided". |
| To-do | Kilo `/session/{id}/todo` | If the endpoint is unsupported, this shows as unavailable. Nothing is inferred from prose. |
| MCP | Kilo `/mcp` (live) + `/config/effective` (configured) | Covers the whole workspace. Disabled servers are listed. History shows the snapshot in force at the time, or "unknown". |
| LSP / indexing / memory | `/lsp`, `/indexing/status`, `/memory/status` | Kept separate from MCP. Endpoints that are missing show as "unsupported". |
| Context limits | Kilo model metadata (config projection) and the Foundry persona declaration | Disagreements between the two are shown. The bar uses Kilo's value because Kilo compacts at it. |
| Calls, served route, fallback, speeds, in-flight calls, media jobs | Foundry `request_log`, `perf_samples`, `usage_events`, `media_jobs`, joined by `x-session-id` / `request_id` | Read at query time and never copied, so nothing is counted twice. |

### Capability map (verified against this code; Kilo server 7.8.8, extension SDK 7.8.12)

- **Real data**
  - Session tree, status, messages and parts, published reasoning, tool input, output,
    timing and status.
  - Pending questions and permissions. To-do. Workspace MCP status. Effective config
    (allowlisted).
  - Coordinator lifecycle, checks, reviews and approvals. Foundry's measured calls,
    timings and fallbacks.
- **Probed at runtime**
  - Every Kilo endpoint is checked as the bridge runs. A 404 is recorded as unsupported
    (shown in the bridge heartbeat's `capabilities`).
  - Event names come from the 7.8.12 SDK. Because the server is 7.8.8, the bridge also
    re-reads busy sessions in full every 30 s (every poll without `/event`) and once
    after they settle.
- **Not available**
  - Raw provider reasoning that Kilo doesn't publish, and per-call prefill/decode for
    backends that don't report them (shown as N/A).
  - Past MCP states before capture began, and content deleted from Kilo.
  - Exact current context when providers count cached tokens differently. The formula is
    shown, along with Foundry's measured prompt tokens.
- **Per-helper MCP permission**: shown only from the agent's boolean `tools` switches in
  Kilo config. Session permission rules can still deny a tool. The page says so.

### Honest metrics

- **Current context** = the latest assistant call's prompt (input + cache read + cache
  write, as Kilo reported). It is never a sum over the session.
- **Decode speed** = output tokens ÷ decode time (Foundry `perf_samples`, `timing_src`
  shown). **End-to-end** is labelled separately.
- **Session decode average** = Σ output ÷ Σ decode time. It is not a mean of rates.
- **Cache hit** always names its denominator.
- **Two ledgers**: Kilo's per-message tokens and Foundry's measured calls are shown
  separately ("do not add").
- **Cost**: $0 is labelled "reported $0 / unpriced". It doesn't prove the service is free.
- **Quiet streams**: a long stretch with no new output on a busy session is labelled as
  such. It is not reported as a crash.

## Alerts

Each alert has a stable dedupe key, so the same event never alerts twice.

| Alert | Raised when | Severity |
|---|---|---|
| question / permission | Kilo has a pending request on a task session or helper | critical |
| approval | task is AWAITING_HUMAN with a human approval kind | critical |
| blocker / failure | task BLOCKED / FAILED | high |
| coordinator error | engine `error` event | high |
| heartbeat lost | coordinator or bridge stops heart-beating (**Foundry watchdog**, every 15 s); Kilo unreachable from the bridge; engine's own store beat stale (seen by the bridge) | high |
| completion / queue exhausted / budget wait | COMPLETE, nothing left to run, paused until a reset | info |

Statuses are new → acknowledged → resolved / answered / expired. **Acknowledging marks
an alert as seen and nothing more.** It doesn't answer, approve or unblock anything.
Alerts resolve themselves when their cause goes away; a request answered here shows as
"answered".

### Delivery: what really happens

| Channel | Works when | Doesn't work when |
|---|---|---|
| Visual (toast, bell, inbox, tab title) | any control-center page is open | — alerts stay stored and appear when a page opens |
| Sound (Web Audio, distinct tone per class) | a page is open **and** you pressed Enable sound / Test sound (browser autoplay rule) | page closed, browser closed, workstation asleep, host offline. The page shows "Enable sound" if the browser blocked audio |
| Browser notification | a page is open and you allowed notifications | page closed. No background push service is installed |

- **One tab plays sound.** Web Locks (with a localStorage lease as fallback) picks the
  tab, and played alert ids are shared through BroadcastChannel.
- **Controls**: mute, volume, snooze, quiet hours (in your time zone, default
  America/Chicago), and repeat-until-acknowledged for critical alerts (bounded). Silent
  mode keeps visual alerts and counts.
- **History**: every delivery (shown / played / blocked / suppressed / notification) is
  logged per alert. The inbox shows it.

### The watchdog and its limits

Foundry checks source heartbeats on its own timer, so a crashed coordinator can't hide
its own crash. The page tells apart:

- a dropped browser stream ("Reconnecting" / "Foundry unreachable")
- a stale bridge
- Kilo unreachable (as reported by the bridge)
- a stopped engine loop (its store beat is stale)
- a missing control channel ("control: off")

**Not covered:**

- If Foundry itself is down, it can't alert. The page shows "Foundry unreachable". No
  separate supervision is configured for the Foundry service (see OPERATIONS.md for how
  it is deployed).
- Sleep and quiet execution can't be told apart from outside. A quiet busy session is
  labelled, not called a crash.

## Answering and acting

### Operator actions

Each action carries:

- a durable id and an idempotency key
- the authenticated actor and its target project / task / session / request
- the state it expects, and an expiry (default 1 h unclaimed)
- an audit log

The audit summary never contains the answer or instruction text.

| Status | Means |
|---|---|
| accepted | Foundry stored it. Nothing has been sent. Can still be cancelled. |
| claimed | the coordinator took it (the coordinator's own store keys it, so it's never applied twice) |
| queued | waiting for a defined safe point (instructions), or for the engine to record a question Kilo just asked (≤ 2 min) |
| delivered | Kilo accepted the reply or prompt |
| applied | the coordinator recorded the result (task state, approval id, the session that resumed) |
| rejected / failed / expired / cancelled | final; the reason is shown, and the draft is kept |

- **Re-checks**: Foundry checks an action when it accepts it, and the coordinator checks
  again when it applies it (state, session, request id, revision, artifact hashes).
- **One reply per request**: two tabs answering the same request get one reply. The
  second is refused (`duplicate_reply`); a retry with the same key returns the first.
- **Stale requests**: replying to a request that is gone is refused, with a link to the
  current request. Nothing is redirected to a new session.
- **Crashes**: if the coordinator stopped mid-apply, the action is reported as **failed:
  outcome uncertain**. Check the history; it is never repeated automatically.

### Action kinds

- **Answer a question**: per-question choices plus a roomy multi-line Markdown answer.
  Enter adds a line; Ctrl+Enter sends. Drafts are kept on the server, for you only. The
  answer goes through the engine's `answer()` (helper questions through the same Kilo
  reply path) and keeps the question-array structure.
- **Permission**: **Allow once** or **Reject** (with an optional message). A persistent
  "always" rule is never created from here.
- **Visual / human approval**: **Approve**, **Request changes** or **Reject**. It's bound
  to the exact revision and artifact sha256 shown on the card. If either changed, the
  approval is refused, both in Foundry and in the coordinator. Reject and Request changes
  both use the engine's single rejection path (back to repair with your findings).
  Cancel is separate.
- **Instruction** (a written message to the controlled agent, inside its scope):
  - *Queue for the next session prompt* (default): the coordinator stores it and appends
    it to the next prompt it sends for that task (repair, closeout or continue). A busy
    worker is never interrupted, and no second prompt is injected. You can withdraw it
    until then.
  - *Pause and steer now* (explicit, with a confirmation): the engine's own pause aborts
    the current step, including a running tool. Resume then continues **the same
    session** with the instruction.
  - *Only this session*: delivered only by steering to that exact session. If the session
    is gone, it is rejected, never moved.
  - Instructions can't widen scope, change paid routing or waive a gate. The page never
    executes text.
- **Commands**: pause, resume, unblock (optionally reset attempts), and cancel (you must
  type `cancel TASK`). Each shows its effect before you submit. "Inspect status" is
  read-only. All of them are the engine's own transitions.
- **Queue approval for new sprints** is not offered here. It stays a separate decision
  (`queue approve`).

## Settings

Display settings are stored per browser and per layout preset (localStorage
`fr.cc.prefs`, versioned and migrated), with **Reset display defaults**. They cover:

- theme (Dark+, Light, High contrast), font size and spacing, compact density, code
  wrapping, time zone and format
- thinking shown / collapsed / expanded, and whether thinking is included in copy/export
- tool arguments and results, assistant text, approvals, diagnostics, collapsing, and
  event filters
- sidebar section order, visibility and collapse, and disabled MCP servers
- token and timing details, to-do, capabilities and helpers
- follow coordinator, follow-scroll and history page size
- layout presets and panel sizes, graph orientation, route animation and reduced motion
- graph refresh (never below the server floor) and the stale-stream threshold
- notifications and every sound setting

Browser storage holds only preferences and played alert ids. It holds no keys,
transcripts, answers or drafts.

**Capture & retention** is a separate, authenticated section stored in Foundry and shared
by everyone:

- retention in days for observations, and for actions and alerts
- whether reasoning and tool output are stored, and their size caps
- watchdog expiries, action TTL, rate limit, sign-in lifetime and polling floor

Hiding something on screen never deletes it. Turning a capture switch off stops **new**
data of that kind being stored; existing data ages out with retention.

## Security

- **Credentials**: Kilo's password and the coordinator key stay in the workstation
  process. Browsers never receive them.
- **Operator sign-in**: an HttpOnly session made from an `operator`/`admin` key.
  Mutations need the session's CSRF token, a same-origin `Origin`, and
  `Sec-Fetch-Site: same-origin`. There's a per-actor rate limit.
- **Project isolation**: client `projects` lists restrict reads, ingest and actions.
- **No anonymous access**: transcripts are never served without a session, even when
  `protect_admin` is off.
- **Redaction** happens before storage, broadcast, search and the audit log, at both ends:
  - `foundry_training.redact` patterns plus every secret Foundry and the bridge know
  - headers, bearer tokens, API keys and passwords
  - Kilo config is an allowlisted projection (no commands, env, URLs, headers or provider
    options)
- **Size limits**: parts are capped (60k chars text, 20k tool), pages are paginated,
  search is bounded and LIKE-escaped, and batches are limited to 2000 observations.
  Unknown future observation types are counted and dropped.
- **Untrusted content**: model and tool output is escaped before any formatting. Links
  are http(s)/mailto only, and the page sends a strict CSP. Evidence pointers are shown
  as paths; the page never reads workstation files.
- **No SSRF surface**: the bridge talks only to the configured Kilo URL and Foundry.
  Foundry makes no outbound calls for this feature.

## Telemetry contract `foundry.cc.v1`

`POST /v1/foundry/telemetry/batch`:

```json
{"contract": "foundry.cc.v1", "source": {"id": "bridge:HOST", "kind": "bridge"},
 "sent_ts": 0, "observations": []}
```

Each observation has a `type` (types below), a `project_id`, stable ids, source
timestamps (`created_ts` / `time.start` / `time.end` / `observed_ts`) and a `source`
label. Foundry stamps `received_ts` and a feed `seq`.

| type | layer | key fields |
|---|---|---|
| `task`, `transition`, `coord_event` | coordinator facts | state, phase, attempt, session_id, shas, verification, reviews, approval {kinds, stage, revision, artifacts{kind:{path:sha256}}}, limits |
| `session`, `message`, `part`, `todo`, `pending`, `mcp`, `capabilities`, `config` | Kilo facts | parent_id, status {value, source, observed_ts}, tokens {input, output, reasoning, cache_read, cache_write}, part kind/text/tool {name, call_id, status, input, output, mcp_server, child_session} |

Snapshots (`mcp`, `todo`, `capabilities`, `config`) are stored only when their content
changes. Each keeps `source_ts` and `confirmed_ts`, so history is dated.

Other layers of the contract:

- **Foundry facts** are read live from Foundry's own tables (see above).
- **Presentation preferences** live in the browser and carry no authority.
- **Operator actions and alert delivery** go through the `cc_actions` / `cc_action_log`
  / `cc_alerts` / `cc_alert_log` tables, separate from observations.
- **Heartbeat**: `POST /v1/foundry/telemetry/heartbeat`
  `{source:{id, kind: bridge|coordinator}, kilo, coordinator_owner, event_stream, capabilities}`.
- **Control channel** (orchestration scope): `POST /v1/foundry/control/claim`
  `{source_id, projects}` and `POST /v1/foundry/control/actions/{id}/status`
  `{source_id, status, detail, result}`.
- **Browser stream**: `GET /control/api/stream?after=SEQ&sessions=…` (SSE). It replays
  from the cursor, coalesced to the latest state per entity, with a heartbeat every 15 s.
  The browser reconnects with bounded backoff using `Last-Event-ID`. One bridge feeds
  every viewer, so opening more panes adds no Kilo connections and no inference.
- **Clocks**: durations come from each source's own start/end pair. Times are displayed
  in the configured time zone.

## Development and tests

Run the full stack on localhost with no real Kilo, Studio or coordinator:

```powershell
.\.venv\Scripts\python.exe -m foundry_coordinator.devtools.control_fixture --port 18766
```

This starts Foundry, a fake `kilo serve`, the real engine with the control channel, the
real bridge, and a scripted UI-01-like scenario (question → answer → repair session →
permission request). The generated operator test key is written to `<dir>/operator-key.txt`.

Tests:

- `tests/test_control_center.py` (Foundry side)
- `tests/coordinator/test_control_bridge.py` (engine + bridge + channel against the fake
  Kilo)
- `tests/test_control_ui.py` (script parses, the Markdown renderer never emits live HTML,
  browser storage audit)

## Retention

| Data | Kept for (default) |
|---|---|
| Observations, parts, snapshots, feed | `retention_days` = 14 |
| Actions, alerts, transitions | `action_retention_days` = 90 |
| Operator sessions | removed when they expire |
| Drafts | `retention_days` |

The latest snapshot of each kind is always kept. Pruning runs hourly in Foundry's
background loop.

## Deployment steps remaining (not performed)

1. **Foundry**: deploy this version on 192.168.0.112 and restart the service. The new
   tables are created automatically and are additive.
2. **Keys**: make the operator key (and optionally a bridge key) in Setup → Clients &
   keys. The coordinator key's allowlist is not changed; the documented mismatch stays.
3. **Bridge**: start it on the workstation (read-only; it doesn't disturb a running
   UI-01 session).
4. **Coordinator**: for operator actions, restart the coordinator loop with `--control`
   at a safe point:
   - pause the task (if needed), stop the loop, run it again with `--control`, then
     resume
   - the restarted process also picks up the engine fix in this version: a question no
     longer logs a spurious engine error
5. **Optional**: add `[telemetry] context_checkpoints` to the AnimalUprising profile. It
   changes the profile hash, which the engine records as an event.
