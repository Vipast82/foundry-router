# Release and migration notes

Newest first. Each version is tagged `vX.Y.Z` (docs/CONVENTIONS.md).

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
