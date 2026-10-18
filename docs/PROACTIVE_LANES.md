# Proactive frontier lanes (v0.118.0)

Optional frontier helpers that contribute while the local primary works:
a visual observer for finished UI captures, a code proposer for its findings,
failure triage, incremental review, independent test design, documentation
research, evidence reconciliation, handoff preparation and preflight. Also an
optional, default-off frontier-primary phase.

**Everything new is off by default.** With `[lanes]` and `[frontier_primary]`
absent from a profile, the coordinator behaves exactly as v0.117.1. Existing
`[helpers]` behavior is unchanged.

Contents:

1. [Architecture and change summary](#1-architecture-and-change-summary)
2. [Checks run](#2-checks-run)
3. [Operator guide](#3-operator-guide)
4. [Deployment checklist](#4-deployment-checklist)
5. [Handoff for Victor](#5-handoff-for-victor)
6. [What is not built](#6-what-is-not-built-missing-or-partial)

---

## 1. Architecture and change summary

### One journal, three paths

Lane work reuses the v0.117 helper journal (`helper_units`), its admission,
binding, polling, bounded hand-off and delivery. There is no second engine,
no daemon and no file watcher that can spend money.

```
primary (Kilo, busy) --tool results--> ingest (GET transcript, rate-limited)
                                         | deterministic classifiers
                                         v
                                   lane_events  --plan-->  helper_units (lane=...)
                                         |                     | admission (quota, streams)
          capture -> artifacts (immutable)                     | bind (optional purpose) BEFORE inference
          commit  -> source snapshot (2 reads)                 v
                                                 Kilo read-only session  |  direct Foundry call (vision)
                                                               | result envelope, validated by code
                                                               v
                               primary idle (safe point) <-- batched action cards (once)
```

### New modules

| File | What it does |
|---|---|
| `foundry_coordinator/artifacts.py` | Checks that PNG/JPEG files are complete. Parses capture records. Enforces path, symlink, junction and deny boundaries. Keeps content-addressed immutable artifact snapshots with generations. Has an optional stable-size filesystem fallback, retention pruning and two-read source snapshots (HEAD plus a per-file sha256 manifest). |
| `foundry_coordinator/lanes.py` | Lane policy and defaults (all off). Deterministic event classifiers. The `helper-result` envelope and its parser. Unified-diff validation. |
| `foundry_coordinator/proactive.py` | Transcript ingestion and planning. The dependency-aware priority queue, coalescing, caps and budgets. Packets, result validation, delivery filters and action cards. The kill switch. |
| `foundry_coordinator/visual.py` | The direct vision call: real image bytes, per-route modality checks before and after the call, a worker pool, restart and timeout handling. The observer → proposer cascade (maximum depth 2). |
| `foundry_coordinator/advisory.py` | Preflight (reuses the planner), research (provenance check and cross-task cache), the deterministic evidence map, handoff notes. |
| `foundry_coordinator/frontier.py` | The optional frontier-primary phase: ownership record, safe-point transition, runtime verification, return conditions. |

### Changed modules

- **`helpers.py`**
  - Additive columns.
  - Lane-aware tick, dispatch, finish and delivery.
  - `record_feedback`, `expire_lane_results`, `supersede_artifact`, `holding`.
- **`engine.py`**
  - Records primary feedback.
  - Hands the frontier phase off at the idle point.
  - Only *holding* units hold the idle primary.
  - Lane results expire at VERIFYING.
  - Adds the handoff note to the next session.
  - Binds before the prompt in `_prompt_primary`.
  - Treats frontier authors as code authors for review.
- **`telemetry.py`** (bridge)
  - Exports lane units, artifacts (with a hash-checked thumbnail of 400 KB or less) and lane state.
  - Never polls Kilo for direct calls.
- **`control.py`**: new `lanes` action (pause or un-pause new lane work).
- **`cli.py`**: `frontier request|return|status`.
- **`devtools/fake_kilo.py`**: records the model used for each prompt (option `ignore_prompt_model`).
- **`devtools/control_fixture.py`**: `--lanes` scenario.
- **Foundry**
  - `orchestration.py`: new *optional* purposes.
  - `facade/work_api.py`: admission reports `vision` for each route.
  - `control_center.py` and `control_api.py`: lane tables, feed kinds, `/control/api/lanes`, `/control/api/artifacts/{id}/thumbnail`, the `lanes` action, lineage in the graph, helper-violation alert, pruning.
  - `ui/static/control.html`: "Proactive lanes" section, graph styles, settings toggle.

### Migrations (all additive, applied on start)

**Coordinator store:**

- `helper_units` gets 20 nullable columns. `lane IS NULL` means an ordinary unit, so old rows keep their meaning.
- New tables: `artifacts`, `source_snapshots`, `fs_watch`, `lane_events`, `lane_cache`, `primary_phases`.
- New meta keys: `lanes_paused:<project>` and `frontier_unsupported`.

**Foundry database:**

- New tables: `cc_lane_units`, `cc_artifacts`, `cc_lane_state`.
- No existing column changes.
- Purposes added to `OPTIONAL_PURPOSES`: `visual_observation`, `failure_triage`, `incremental_review`, `research`, `evidence_reconciliation`, `handoff_prep`, `preflight`, `frontier_primary`. They are never required.

**Source snapshots (v0.119.1):** identity and ownership are scoped.

- `snapshot_id` is a hash of project, task, run, workspace, HEAD and the file manifest. Two clones or fixtures with identical code therefore never share a row or a blob root.
- Additive columns: `source_snapshots.content_fp` (hash of HEAD and manifest, used for "has the code changed?") and `workspace`.
- Every lookup (packet excerpts, drift checks) passes the caller's project and task. A record owned by another project or task is never returned.
- **Legacy rows** written by v0.118.0 keep their content-only id, their recorded owner and `content_fp = NULL`. They stay readable by that owner only; nothing is rewritten.

**Files:** artifact copies go to `state_dir/evidence/<project>/<task>/artifacts/<sha24>.<ext>`. Snapshot blobs go to `state_dir/snapshots/<project>/blobs/<sha32>`. Both live outside the workspace.

**Rollback:**

- Remove `[lanes]` and `[frontier_primary]` from the profile (or set `enabled = false`), or check out v0.117.1. Old code ignores the new columns and tables.
- No data rewrite is needed.
- Historical runs are untouched.

### Version

`foundry_router` **0.118.0**. Both the server and the coordinator changed. The
lanes need both sides; see the deployment checklist.

---

## 2. Checks run

All checks were **mocked validation**: fake `kilo serve`, fake or in-process
Foundry, simulated quota and clock, and synthetic PNG/JPEG files and logs. **No
real-model validation was run.** There was no paid inference, no live sprint,
no restart and no deploy.

| Suite | Result |
|---|---|
| Baseline before the change (`tests/coordinator`, `test_control_center`, `test_control_ui`, `test_helper_policy`) | 140 passed, 9 skipped |
| Same suites plus the new ones (final) | **212 passed, 9 skipped** (the 9 skips are pre-existing) |
| `tests/coordinator/test_lane_infra.py` (stage 1) | 15 passed: retention prune keeps history; completeness, CRC, partial writes, capture records, path traversal, absolute paths, deny list, junction (real Windows junction), overwrite generations, duplicates, tamper detection, oversize, fileBytes and dimension mismatch, filesystem stability, dirty snapshots sharing HEAD, writer race, failure-signature normalization, gate/commit/API events, envelope and patch checks, ingestion from a busy primary (once, zero extra prompts), late or partial file retried, lanes off, HEAD move and context events |
| `test_lane_queue.py` (stage 2) | 13 passed: failure triage end to end (busy primary → admission → bound read-only session → validated patch → one idle delivery → acknowledgement → independent review), coalesced repeats, quota defer never holds the primary, older Foundry refusing the purpose, out-of-scope patch, below-threshold results, late result after verification, audit of helper tools and skills, kill switch, incremental review with supersession and no re-review, test design, queue priority and aging, restart between session start and binding |
| `test_lane_visual.py` (stage 3) | 16 passed: **capture → admission → actual image bytes in the mocked vision request (bytes compared) → findings → proposal → one delivery → acknowledgement → new generation**; invalid capture never sent; unverifiable, text-only or no route; wrong served model, no served model, or `image_seen` false all discarded; speculative finding goes to the primary; overwrite and duplicate before dispatch; stale image vs changed code (rebase flag); call-time refusal; restart mid-call not repeated; pause discards a late reply; no recursion; full helper capacity queues |
| `test_lane_control.py` (stage 4) | 4 passed: real bridge into the real ControlCenter; lineage edges; thumbnail with project isolation; deferral reason; "active" only while in flight; kill switch is authorized and project-scoped |
| `test_control_ui_behavior.py` (stage 4, node) | 7 passed: preference and layout migration and persistence; follow vs pinned history view; notification permission states (denied, default, granted, constructor throws) with honest delivery reports; lanes panel escapes hostile text, shows N/A when data is missing, flags "not inspected" |
| `test_lane_advisory.py` (stage 5) | 6 passed: preflight once vs reusing the planner, research provenance and cross-task cache, deterministic evidence contradictions, handoff into the next session only at an unchanged HEAD, every lane binding optional with the implementer binding unchanged and the aggregate cap held |
| `test_frontier_phase.py` (stage 6) | 9 passed: off by default; safe-point start, verification and hand-back on the same session; unsupported switch aborts and disables; running tool is not a safe point; DONE cancels; max phases and cooldown; restart keeps ownership; refusal returns ownership; new session ends the phase |
| `test_helper_policy.py` (+2) | 35 passed: lane purposes are optional and bindable; admission reports modality for each route |
| Full repository suite (`pytest tests`, v0.118.0) | 1321 passed, 34 failed, 10 skipped. **All 34 failures are pre-existing:** the same 34 test ids also fail on an unmodified export of v0.117.1 (1317318) in this environment. They are 25 in `tests/training/*`, plus test_client_passthrough, test_heartbeat_toolcalls, test_keepalive_audit, test_llamacpp_vllm, test_mcp_passthrough, test_media_adapter, test_passthrough_matrix, test_strict_routing and test_training_capture (one each). None are in files this change touches. |

**Browser check (local fixture only).** `python -m foundry_coordinator.devtools.control_fixture --lanes` was run on localhost. A synthetic capture and a failing Lune run in the fake primary became one visual unit and one failure-triage unit.

- Both were **deferred** with Foundry's real admission reason: the fixture has no helper or visual persona.
- The artifact thumbnail, sha256, generation and revision appeared in the new panel.
- Lineage appeared in the graph.

The run found two bugs, both fixed in stage 4 with tests:

- Windows MAX_PATH with deep state directories.
- One failing capture blocked other lanes.

---

## 3. Operator guide

### Enabling (profile `[lanes]`, per project)

```toml
[lanes]
enabled = true                 # master switch (default false)
transcript_poll_seconds = 20   # GET-only read of the busy primary's transcript
max_lane_units_per_task = 24
max_dispatches_per_task = 16   # optional budget per task (attempted dispatches)
max_cards_per_delivery = 6
max_deliveries_per_session = 3
min_delivery_severity = "medium"
aging_seconds = 900            # anti-starvation
max_retries = 1
skill_dirs = [".kilo/skills"]
snapshot_max_bytes = 400000    # local immutable source snapshot; maximum 8000000

[lanes.visual]                 # each lane: enabled = false by default
enabled = true
persona = "Foundry-Kilo-Visual-Review"
capture_globs = ["docs/evidence/{task}/*.png", "docs/evidence/{task}/*.jpg"]
reference_globs = ["docs/design/{task}/*.png"]   # explicitly approved references only
debounce_seconds = 20
max_image_bytes = 8000000
min_confidence = 0.75
min_severity = "medium"
propose = true                 # confident, in-scope findings -> proposer
approved_models = []           # optional extra restriction: EVERY reported route must be one of
                               # these models, or the unit waits (it never removes a route)
fs_fallback = false            # filesystem readiness fallback (stable size + complete image)
retention_days = 30

[lanes.proposer]
enabled = true
[lanes.failure_triage]
enabled = true
[lanes.incremental_review]
enabled = true
min_interval_seconds = 600
[lanes.test_design]
enabled = true
[lanes.research]
enabled = true
cache_hours = 168
max_lookups = 6
[lanes.evidence]
enabled = true
[lanes.handoff]
enabled = true
context_threshold = 140000
[lanes.preflight]
enabled = true
```

Lanes also need `[helpers] enabled = true`. They share its personas, order,
selective backends, `max_running` (the aggregate cap of three optional
helpers), timeouts and hand-off wait.

### Lane triggers and routes

| Lane | Trigger (deterministic) | Route | Purpose (optional) | Holds the idle primary? |
|---|---|---|---|---|
| visual | Capture tool output with `VALID=OK`, `CAPTURE … fileBytes=N` and `Saved: <path>`. The file on disk must match fileBytes and dimensions and be a complete image. | **Direct** Foundry chat to `[lanes.visual] persona`, carrying the image bytes | `visual_observation` | no |
| proposer | High-confidence, in-scope visual findings (depth 2, never spawns more) | Kilo `au-helper`, `[helpers]` personas (Claude first; GPT only for `value=high`) | `code_proposal` | yes (bounded) |
| failure_triage | A test-like command (`failure_commands`) failed. One unit per normalized signature. | Kilo `au-helper` | `failure_triage` | yes |
| incremental_review | A commit or HEAD move with a consistent snapshot that changed since the last review | Kilo | `incremental_review` | yes |
| test_design | New implementation run | Kilo | `test_design` | yes |
| research | A Roblox/Luau API error in tool output. One unit per symbol; cached across tasks. | Kilo (documentation lookups the agent allows) | `research` | no |
| evidence | `GATE <task> PASS\|FAIL` line | Kilo, plus the coordinator's deterministic evidence map | `evidence_reconciliation` | no |
| handoff | Primary context reaches `context_threshold` or more | Kilo | `handoff_prep` | no |
| preflight | New run of an approved task, **unless** the implement prompt already runs `au-planner` | Kilo | `preflight` | no |

**Image modality.** The visual unit runs only when **every** route Foundry
reports for the visual persona says `vision: true`. If modality is missing, the
route is treated as unverifiable and the unit waits. A text-only route is
refused. After the call, the served model must be one of those routes and the
model must confirm `image_seen`. Otherwise the result reads **"Not
inspected"**, and there are no findings and no proposal.

**Skills.**

- Visual: the workspace `.kilo/skills/au-ui-review/SKILL.md` is inlined. Its path and sha are recorded.
- Kilo lanes: the prompt tells the helper which skills to load (Luau/Roblox baseline, plus UI for visual proposals). "Skills loaded" means *observed* `skill` tool calls; claimed skills are shown separately.

### Limits and scheduling

- **Admission.** As in v0.117: below 70% used in fresh session, weekly and model windows, with hysteresis and cooldown. No paid fallback. CLIProxyAPI only for `value=high`. Required reserves and planner/reviewer admission are unchanged.
- **Capacity.**
  - At most `[helpers] max_running` (3) running units across all lanes for the active task.
  - Foundry's per-backend stream ceilings still apply.
  - Each lane has its own `max_running` and `max_per_task`.
- **Queue.**
  - Priority runs failure triage, then review, test design and proposals, then evidence and handoff, then visual and research, then preflight.
  - Units age upward over time.
  - Equal priorities keep arrival order. Ordinary units keep the v0.117 order.
- **Delivery.**
  - Only when the primary is **idle** (end of its turn). Kilo has no tool-boundary injection, so feedback waits until idle. It is never instant.
  - Cards are batched and capped.
  - Results that are below threshold, rejected, superseded, from another run, late or expired are shown but never delivered.
  - Nothing is delivered after VERIFYING.
- **Acknowledgement.** The primary adds `"helper_feedback": [{"unit", "status": "integrated|rejected|deferred", "note"}]` to its report. This is recorded as *its claim*, not as verification.

### Dashboard controls

- **Inspector → "Proactive lanes".**
  - For each unit: lane, status, feedback state, persona, backend, served model, parent (primary session or parent unit), timing, defer reason with next eligible time.
  - Artifact: thumbnail, path, generation, size, sha256, revision, validity.
  - Source snapshot, image bytes sent, vision routes, skills loaded vs claimed.
  - Findings (observed vs hypothesis), proposed diff (colored, collapsible, with a base-drift warning), checks proposed, sources, limitations, violations, recapture link.
- **Metrics.** Turnaround, queue wait, delivered, integrated (claimed), not actionable, duplicates coalesced, stale, tokens. Any missing value shows as N/A. No speedup is computed.
- **Graph.**
  - Artifact → observer → proposer → primary (delivery: delivered, integrated or rejected) → recapture.
  - An edge is "active" only while that unit's session has an in-flight ledger row or Kilo reports it busy. Otherwise it shows waiting, deferred or queued.
- **Pause proactive lanes** button. This is operational: it is the `lanes` action, claimed by the coordinator, and only stops new lane work. Turning lanes **on** stays a profile change.
- **Settings → Sidebar → "Proactive lanes panel".** Presentation only.
- **Alerts.**
  - New: `helper_violation` (high), raised when a helper tried a denied tool.
  - Quota-source alerts, completion, stalls, questions and audio/notification behavior are unchanged (see CONTROL_CENTER.md).

### Frontier-primary phase (optional, default off)

```toml
[frontier_primary]
enabled = false                  # default
persona = "Foundry-Kilo-Frontier"   # must differ from implementer and reviewers
max_turns = 3
max_minutes = 120
max_phases_per_task = 1
cooldown_minutes = 60
```

Commands:

- `python -m foundry_coordinator --profiles … frontier request PROJECT TASK --reason "…"`
- `… frontier return PROJECT TASK`
- `… frontier status PROJECT TASK`

How it works:

- **Manual only.** There is no automatic trigger.
- **Same session, one writer.** The switch happens only at an idle safe point with no running tool part and a stable tree.
- **Verified at runtime.** The first frontier turn is checked from Kilo's transcript (`modelID`). If Kilo did not switch models, the phase aborts, the local persona resumes and further requests are refused.
- **Quota-gated.** Every frontier call is bound with the optional purpose `frontier_primary`.
- **Return conditions.** The phase returns on DONE, BLOCKED or NEEDS_HUMAN, on hand-back (no `"frontier_continue": true` in the report), at `max_turns` or `max_minutes`, on a Foundry refusal, when a new session starts, or when an operator returns it.

### Privacy and retention

- Only the approved capture and reference globs inside the workspace are copied. Paths that resolve through symlinks or junctions are refused, as are credentials, `.git` and `_secrets` paths.
- Images go to Foundry only in the visual call, and only to a vision-validated route. The bridge sends thumbnails of 400 KB or less; larger images send hash and metadata only.
- Source snapshots contain only authorized scope files (60 files, 400 KB by default).
  `lanes.snapshot_max_bytes` can raise the local snapshot budget up to 8 MB for
  larger tasks. This does not expand task scope or increase the prompt excerpts.
  Raw evidence paths are excluded case-insensitively; images use the separate artifact store.
- Prompts carry excerpts, not the whole transcript.
- Coordinator copies: removed after `[lanes.visual] retention_days` (checked at most hourly while lanes run). Rows stay as history, marked `expired`.
- Control-center rows follow `retention_days`.

### Disable or roll back

| To | Do this |
|---|---|
| Stop new lane work immediately | Click **Pause proactive lanes**. Or set meta `lanes_paused:<project>` to `1`. |
| Turn lanes off | Set `[lanes] enabled = false` (or remove the section), then restart the coordinator at a safe point. The profile is read when the coordinator starts. |
| Turn off the frontier phase | Set `[frontier_primary] enabled = false`. |
| Full rollback | Deploy v0.117.1. The additive schema is ignored. |

---

## 4. Deployment checklist

Not performed. Victor authorizes and runs it separately, at a safe point after
UI-01's worker finishes or is paused.

### Server (Foundry host)

1. Deploy v0.118.0 and restart Foundry. This is a restart boundary and it interrupts in-flight calls, so do it while the primary is idle.
2. Confirm the admission endpoint reports `vision` for each route: `GET /v1/foundry/helpers/admission?persona=Foundry-Kilo-Visual-Review` with the coordinator key.
3. Check that **every** route of `Foundry-Kilo-Visual-Review` is vision-capable in the model registry.
   - If any route is text-only, fix the persona chain itself so that it contains only vision-capable routes. Until then the visual unit stays deferred.
   - `approved_models` cannot fix this. It only adds a restriction: every route Foundry reports must be on that list. It does not remove or skip a text-only fallback, so setting it cannot repair an incompatible route chain.
   - Do not relabel the text-only Helper-Claude as vision.
4. Persona allowlists:
   - The **coordinator** client key needs `Foundry-Kilo-Visual-Review`. The visual call uses the coordinator's key, like reviews.
   - The **Kilo inference** key needs the helper personas (unchanged), plus the frontier persona only if that phase is ever enabled.
   - Do not widen operator scopes. Operator sign-in does not grant persona access.
5. Check `helper_policy.optional_enabled` and the cutoff. Nothing changes them automatically.

### Workstation (coordinator, bridge, Kilo)

1. Update the checkout to v0.118.0. Keep the existing working-tree changes.
2. Back up the coordinator store (`~/.foundry-coordinator/...`). The migration is additive and runs on start.
3. Leave the profile unchanged for the first restart, so lanes stay off. Restart the coordinator and bridge (restart boundary: primary idle). Confirm v0.117.1 behavior.
4. Kilo `au-helper` agent: keep edit, bash, task and Studio denied and `subagent_depth=1`. For research, allow only the Roblox documentation lookup tools.
5. Pilot (separately authorized):
   1. Enable `[lanes] enabled = true` and one lane, starting with `failure_triage` or `visual`.
   2. Restart at an idle point.
   3. Watch the panel and graph.
   4. Use **Pause proactive lanes** to stop.
6. The frontier phase stays disabled until a separate pilot verifies a per-prompt model switch on the installed Kilo.

### Restoring prior settings

Remove `[lanes]` and `[frontier_primary]`. Quotas, credentials, persona
allowlists and other preferences were not touched by this change.

---

## 5. Handoff for Victor

```
Foundry-Router v0.118.0 — proactive frontier lanes (branch feature/helper-capacity, not merged/tagged/pushed)
Commits: c5a2cdd (stage 1 artifacts/snapshots/ingestion), c914a10 (2 triage/review/test design),
e098d91 (3 visual observer->proposer, Foundry purposes + modality), b176c86 (4 control center),
3e4cd96 (5 preflight/research/evidence/handoff), 469351d (6 frontier phase), + version/docs commit.
Tests: focused suites 212 passed / 9 skipped (baseline 140/9); +72 new lane/frontier/UI tests; full repo
1321 passed / 34 failed (the same 34 fail on v0.117.1 here: pre-existing); all mocked
(fake Kilo/Foundry, simulated quota/clock, synthetic images). No real-model validation, no paid inference.
Defaults: [lanes] and every lane OFF; [frontier_primary] OFF; [helpers] unchanged.
Dependencies: Foundry server v0.118.0 (new optional purposes; per-route vision in admission) — an older
server refuses the new purposes, so lane units just defer ("binding failed").
Config needed for a pilot: [lanes] enabled + chosen lanes; coordinator key allowlist += Foundry-Kilo-Visual-Review;
every Visual-Review route vision-capable. Deploy = Foundry restart + coordinator/bridge restart at an idle point.
Known limits: feedback reaches the primary only when it is idle (no Kilo tool-boundary injection); frontier
phase needs a live check that Kilo serves a per-prompt model; see docs/PROACTIVE_LANES.md §6.
Ready for a separately authorized pilot after review and merge to main (then tag v0.118.0, push main:cline).
```

---

## 6. What is not built (missing or partial)

### Staged retry-limit adviser (workstation follow-up)

`[retry_guidance]` is an opt-in bounded consultation before an exhausted ordinary
attempt budget becomes FAILED. It is disabled by default and is not yet enabled
in the live AnimalUprising profile. Example:

```toml
[retry_guidance]
enabled = true
persona = "Foundry-Kilo-Opus" # must be available to the coordinator key
extra_attempts = 1           # 0 = advice only; hard maximum 1
max_tokens = 2000
timeout_seconds = 60         # hard maximum 60
```

The adviser receives the task, scope, current revision, prior findings and failed
checks. Its direct, tool-free call is bound as optional `debug_advice`, obeys quota
admission and shared helper capacity, and is recorded in sessions/events/task data.
There is one consultation per task; manual unblocking does not erase that record.
A concrete `repair` response can authorize the explicitly configured extra attempt;
it never resets the ordinary attempt counter, session/time limits, checks or review.
Malformed, unavailable, stale or human-only guidance stops normally. A crash during
the call is recorded as unknown and never causes automatic replay. The adviser is
excluded from final review of a repair it guided. The synchronous consultation can
delay control action processing for up to its configured timeout.

Victor subsequently delegated core-aligned scope/test decisions to an Opus-class
model and final visual approval to an independent vision-capable frontier model.
Those broader delegation policies are **not implemented by this retry adviser**;
they need the separately specified decision/approval workflow. The live profile
continues to use its existing approval path until that workflow is built and tested.

These are reported as not done, not marked done.

- **Instant feedback at a tool boundary.** Not possible with the installed Kilo API. Feedback waits until the primary is idle. Documented; not simulated.
- **Live proof that a model saw the image.** The coordinator verifies route modality, the served model and the model's `image_seen` claim, and the tests prove the bytes are in the request. It cannot prove perception without a real model run. This is left for the pilot.
- **Frontier-primary switch on the real Kilo.** The adapter shape supports a model per prompt and the runtime check aborts safely, but no live validation was done. The option stays disabled.
- **Frontier phase from the control center.** CLI only. There is no control-center action or panel yet; the phase's events appear in the event log.
- **Approximate primary wait time metric.** `helper_handoff_wait` events are recorded, but the panel does not sum them into a wait-time figure yet.
- **Per-session MCP status.** Kilo exposes workspace MCP status only. The existing panel says so; unchanged.
- **Attachments on question/answer cards.** Not added, because there is no secure upload path. Existing durable answers, drafts and stale-submission refusal are unchanged.
- **Lane enable/disable from the UI.** By design, only *pause* is operational in the UI. Enabling is a profile change.
- **Eval-harness run.** Not applicable: no persona definition changed. A persona allowlist change at deployment is a config step and needs no eval.
