# Release and migration notes

Newest first. Each version is tagged `vX.Y.Z` (docs/CONVENTIONS.md).

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
