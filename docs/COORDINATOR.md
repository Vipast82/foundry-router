# Development coordinator: sprint after sprint, each verified before the next

`foundry_coordinator` (in this repository, stdlib Python 3.11+) runs an
**approved queue** of development tasks. For every task:

1. it waits until the task's dependencies are complete;
2. starts a **fresh Kilo session** with the local coding persona;
3. runs the project's own **mechanical checks** on the committed code;
4. asks one **frontier reviewer** (through Foundry) to review the exact commit;
5. on failure starts a fresh **repair session** with the findings, within the
   attempt and budget limits;
6. runs **closeout** (summary, state files, push) and checks it again;
7. stops for **human approval** where the project requires it;
8. moves on to the next eligible task.

The model's own "done" is recorded but never used to advance. Results of the
first live pilot are in docs/COORDINATOR_REPORT.md.

## Architecture

```
approved queue ─► coordinator engine ──► Kilo (`kilo serve` HTTP API) ──► Foundry-Kilo-Local ─► Strata
     (operator)    states · store ·        fresh session per attempt           (strict persona)
                   locks · recovery        session-level permission rules
                        │
                        ├─► project adapter: task text, scope, checks, prompts, approvals
                        │      (generic profile, or a narrow adapter e.g. AnimalUprising)
                        ├─► project checks: done_check.py, gate verdict, Lune, verify.py ...
                        └─► Foundry: review persona (Foundry-Kilo-Review / -Deep-Review), work bindings,
                               budgets, usage, request log, Work tab
```

Three layers, kept separate:

| Layer | Where | Knows about |
|---|---|---|
| Engine | `foundry_coordinator/{states,store,engine,review}.py` | queues, dependencies, sessions, review lifecycle, retries, locks, approvals, recovery. No project names, paths, formats or commands. |
| Project profile + adapter | `projects/<id>/project.toml` (+ optional `adapter.py`) | repository, task source, rules, verification commands, prompts, approval and publishing policy, limits, exclusive resources |
| Foundry | `foundry_router` | personas, strict routing, budgets, usage, observability. Foundry never decides whether a task is done. |

A second game needs only a profile: `contrib/coordinator-pilot/fixture-game/`
is a complete example with no adapter code.

### Why not Kilo's Agent Manager or Goals as the coordinator

Checked against Kilo 7.8.8 (CLI + `@kilocode/sdk` 7.8.8 types, live calls):

- **`agent_manager`** is a tool the *model* calls; requests are relayed to the
  VS Code Agent Manager host (failure codes include `disconnected`,
  `host_error`). It isn't offered at all in a headless `kilo serve`. Useful for
  a person in VS Code; not a durable, unattended queue.
- **Goals** (`goal`, `goal_report` tools) keep one session working on one
  objective. Their own description: "Completion is your report, not
  independent verification." So: an execution aid, never a gate.
- **`task`** (subagents) can choose a model per helper, but when the model
  isn't found under the current provider it falls back to the **Kilo Gateway
  (paid credits)**. Set `enabled_providers: ["foundry"]` and
  `subagent_depth: 1` (Foundry's generated Kilo config now includes both).
- The supported **`kilo serve` HTTP API** does everything the coordinator
  needs: create a session with an explicit model, agent, metadata and
  permission rules; prompt it asynchronously; read its status, messages,
  pending questions and permission requests, child sessions; abort it. A
  session-level `deny` rule removes that tool from the model entirely. No GUI
  automation and no reading of Kilo's private storage.

Hermes Kanban was not needed.

## Lifecycle

```
QUEUED ─► READY ─► IMPLEMENTING ─► VERIFYING ─► REVIEWING ─► APPROVED ─► CLOSING ─► COMPLETE
            ▲          │   ▲            │            │           │          │
            │          │   └── CHANGES_REQUIRED ◄────┴───────────┼──────────┤
            │          └─► AWAITING_HUMAN (question / permission) │          └─► AWAITING_HUMAN (approval)
  any active state ─► PAUSED (operator, Foundry budget or pause) ─► back where it was
  any active state ─► CANCELLED            BLOCKED / FAILED ─► READY (operator)
```

Every transition is checked against `states.TRANSITIONS` and stored with its
evidence (`history PROJECT TASK` shows it):

| Transition | Evidence required |
|---|---|
| QUEUED → READY | every approved dependency COMPLETE (or pending only an approval the profile accepts) |
| READY / CHANGES_REQUIRED → IMPLEMENTING | session id, base commit, run id; workspace + resource + writer locks held |
| IMPLEMENTING → VERIFYING | the session is idle; head commit |
| VERIFYING → REVIEWING | a verification set at that head, every required check PASS |
| REVIEWING → APPROVED | a validated review naming exactly that head commit |
| APPROVED → CLOSING | required before-close approvals present for this revision |
| CLOSING → COMPLETE | closeout checks PASS, the review still valid, published (or policy none) |
| AWAITING_HUMAN → COMPLETE / CLOSING | an approval bound to the revision and artifact hashes |

**What blocks advancing:** any required check that is FAIL or UNKNOWN (a
missing tool is UNKNOWN, never PASS); a review that isn't APPROVED; an
APPROVED review that still lists blocker/major findings or missing evidence;
code changed after the review (only the profile's closeout-exempt files may
change); a missing required human approval; a failed or unverified push.

### Built-in checks (every project)

| Check | Fails when |
|---|---|
| `coordinator.history` | the task's base commit is no longer an ancestor of HEAD (history rewritten) |
| `coordinator.commits` | nothing was committed (evidence from an uncommitted copy doesn't count) |
| `coordinator.repair_changed` | a repair session committed nothing |
| `coordinator.commit_messages` | a commit lacks the task prefix (profile `commit.prefix`) |
| `coordinator.scope` | a file outside the task's scope changed. Scope is never widened. |
| `coordinator.clean` | task files have uncommitted changes |
| `coordinator.unrelated_preserved` | uncommitted work that existed before the task was changed, or new stray files appeared |
| `coordinator.published` | the remote branch doesn't equal HEAD after closeout |

Uncommitted work is never discarded. Work in the task's own files that existed
before the task blocks it before any session starts. A task's own leftovers
(a session that forgot to commit) are handed to the repair session.

### Sessions and recovery

- One fresh Kilo session per attempt and per closeout; one task per session.
- Each session is created with metadata `{project, task, run, phase, attempt,
  role}` and denies `agent_manager`, `external_directory`, `webfetch`,
  `websearch` and `doom_loop` (plus the profile's rules).
- Kilo sends `x-session-id` on every model call. The coordinator binds that id
  in Foundry before prompting, so every call carries project / task / run /
  role. Helper subagents are bound too, with their parent session.
- Operations that must not repeat (start a session, send its prompt, request
  a review, push) are keyed in the store. After a crash the coordinator
  resumes the session it already started instead of starting another (pilot:
  stopped after 4 ticks mid-implementation, restarted, one session).
- A session that disappeared (Kilo restarted) is retried in a fresh session
  without using up an attempt.

### Reviews

AnimalUprising profiles may also map `roles.planner` to a frontier persona and
configure a read-only Kilo subagent named `au-planner` using that persona. Each
BUILD/repair kickoff asks the Code worker to consult it once, then integrate
the advice and continue without a manual mode switch. The planner interprets
the existing approved plan; it cannot widen scope, waive gates, or approve
new queue entries. This is a model-executed delegation instruction, not a new
engine planning phase or a guarantee that a worker invoked the helper. Helper
sessions remain observable through Kilo and Foundry; mechanical checks and
exact-commit review still control advancement. Profiles without a planner
mapping keep their existing behavior.

One reviewer per attempt (`roles.reviewer`, or `roles.deep_reviewer` when a
task tag matches `review.deep_when`). No duplicate reviews from several
models. The packet holds the task text and scope, the commits, the diff
(capped), the verification results with evidence pointers and the project's
extra evidence. The result must be JSON with `sprint_id`,
`reviewed_commit_sha`, `verdict` (APPROVED | CHANGES_REQUIRED | BLOCKED),
`findings` (severity, file, line, message), `evidence_inspected`,
`missing_evidence`, `required_repairs` and `residual_risks`. It is rejected
unless it names this task and **this exact commit**. Paths outside the
repository are dropped. Two invalid results block the task. Reviewer text
goes to the repair session as advice, never as permission to change scope.

A review is reused for the same commit: a repair that changes nothing doesn't
buy a second paid review.

### Budgets and refusals

The coordinator never counts a Foundry refusal as a failed attempt:

| Foundry category | Coordinator |
|---|---|
| `budget` | PAUSED until the reset (`reset_seconds`), then resumes on its own (`limits.auto_resume_after_budget_reset`) |
| `pause` | PAUSED until an operator runs `resume` |
| `route` | PAUSED, retried after 5 minutes |
| `config`, `auth` | BLOCKED: a person fixes the setup |

Budgets are enforced in Foundry, atomically, for every call, at account,
backend, client, persona, project, sprint (`work_task`), attempt (`run`), role
and session level (docs/BUDGETS.md). The coordinator's own limits come on
top: `max_attempts`, `max_sessions_per_task`, `max_review_calls`,
`max_task_minutes`, `session_timeout_minutes`.

### Locks, isolation, fairness

Existing work blocks a task by default. To resume an explicitly approved handoff,
repeat `queue approve ... --adopt-existing RELATIVE_PATH` for each dirty file the
operator authorizes that task to finish. Only dirty files already in the task's
declared scope qualify. The coordinator stores original bytes and SHA256 hashes
under its state directory, outside the coding workspace; queue approval and the
handoff record are saved atomically. Before the first session starts, the revision,
scope, original files and backup bytes must still match. Changes block execution.
Unselected task files still block; unrelated work remains hash-protected. Adoption
does not accept old screenshots, waive a gate or widen task scope.

Shared closeout exemptions permit commits without claiming pre-existing unrelated
work. The engine checks unchanged baseline files separately, so an existing asset
ledger cannot force another sprint to commit it; modifying that baseline still
fails preservation. New or task-owned uncommitted closeout files still fail CLEAN.

- A task holds `workspace:<path>` from start to completion, plus the
  profile's exclusive resources (e.g. `studio:victor-workstation`,
  `rojo:34872`) and one global writer slot while a session writes.
  The writer cap is the live `[automation] max_agents` setting (default 1:
  one implementation writer across all projects); `--max-writers N` overrides
  it for the process.
- **Parallel tracks (Team Lead, 2026-10-22; `foundry_coordinator/tracks/`).**
  `tracks analyze PROJECT` reads every sprint plan and prints which sprints can
  run side by side (needs: studio, blender, audio, images, gpu, backend; explicit
  dependencies and shared files make sprints serial, except the append-safe
  ledgers in `[automation] track_append_safe`). `tracks propose`, `tracks start
  NAME SPRINT...` / `--plan ID`, `tracks stop`, `tracks merge`, `tracks status`
  and `tracks set-max-agents N` drive the same manager the chat and the
  Automation page's Tracks tab use. A track is a git worktree at
  `<workspace>/.tracks/<name>` on branch `track/<name>` (cut from the profile's
  branch); its sprints are queued on it and run there with no Studio/Rojo names
  in their exclusive resources (the `blender` / `gpu` rows from their needs
  instead) and the fast lane off. The main sprint counts first against
  `max_agents` (a track takes writer slot 1 and up). A finished track merges at
  a quiescent point of the main sprint only (no live main session): rebase onto
  the branch, fast-forward, the project's checks on the merged files in the
  main checkout (the studio_gate runner under a Studio lease when the track
  needs Studio: admitted after `studio_idle_minutes` without a main Studio tool
  call, no main request pending, bounded by `studio_track_max_minutes` and never
  interrupted), `track_merged`, worktree removed (branch kept). The merge
  commits count as the coordinator's for the main sprint's scope checks. A
  rebase conflict opens a decision whose default (after
  `decision_timeout_minutes`) is "stop this track". Studio holds and leases are
  journaled (`resource_journal`); the Tracks tab shows the timeline.
- Resource names are global. Two projects that name the same Studio contend
  for it (in the pilot, AnimalUprising waited for the fixture game's Studio
  lock).
- Projects take turns (round robin). Every row carries its project id.
  Evidence and reviews are looked up by project, task and revision together,
  so one project's review never counts for another (tested with two
  byte-identical repositories that share a commit id). Cross-project
  dependencies are refused. Two profiles on one workspace are refused. A
  session id can't be re-bound to another project in Foundry.
- Unrelated approved work continues while a task waits for a person, as
  long as the dependencies allow it and the waiting task has closed (after-close
  approvals release the workspace).

### Automation policy (`[automation]`, revamp B5-B13)

One validated table (`foundry_coordinator/automation.py`) with every revamp knob;
the Foundry-Router GUI edits it. All keys are optional; the defaults are what
Victor decided on 2026-10-21.

| Key | Default | What it does |
|---|---|---|
| `helpers` | `"on_demand"` | No proactive lane runs; a helper starts only from the primary's `helper_requests` (consumer recorded). `"proactive"` restores `[lanes]`. |
| `helper_git_measure` | `true` | A delivered result is "used" only when a later commit touches a path of its proposal (`helper_units.git_used`). |
| `helper_lane_misses` | `2` | Proactive mode: unused results in a row that turn a lane off for the task. |
| `subagents_allowed` | `["explore"]` | Kilo `task` subagents a session may spawn; all others are denied (appended last). |
| `scope_auto_approve` | GateRunner `EXPECTED_BOOT`/`MOUNTED_TESTS` rows, `docs/progress/{task}_gate.json`, `default.project.json` mappings for plan files | Harness lines the coordinator adds to a sprint's file list itself (`SCOPE_REQUEST` channel, `scope.py`). |
| `scope_unlisted_default` | `"deny"` | Recommended answer of a `scope_request` decision for any other path. |
| `decision_timeout_minutes` | `30` | A decision with a recommended option applies it after this (authority `timeout_default`, journaled, `operator_notice`). Defects never time out. |
| `no_progress_sessions` | `2` | Sessions in a row with no commit, verified delta or plan step before a no-progress stop (infra sessions don't count; an operator resolution resets). |
| `auto_heal_retries` | `2` | Deterministic retries of a tooling / infra stop (per head and class) before a lead case. |
| `lead_hard_max_calls` | `8` | Absolute lead calls per task; delegation never raises it. |
| `lead_report_repairs` | `1` | Invalid lead reports sent back into the same session. |
| `infra_retry_max` | `5` | Free infra retries in a row; then an ordinary attempt (and one operator notice for a failing preflight). |
| `infra_backoff_seconds` | `60` | First infra retry delay; doubles, capped at 15 min. |
| `preflight` | `true` | Kilo / endpoints / adapter checks before a session starts; a failure costs nothing. |
| `preflight_endpoints` | `[]` | `"name=tcp://host:port"` or `"name=http(s)://..."` (Rojo, the local model...). |
| `context_ceiling_tokens` | `160000` | A session above this that stopped before DONE hands off to a fresh one (free). |
| `memory_delta` | `true` | Memory blocks reach a session once, then only changed paragraphs. |
| `transcript_poll_seconds` | `120` | Lane transcript ingest interval (an explicit `[lanes]` value wins). |
| `generic_checks_cache` | `true` | Reuse the per-tick coordinator checks while HEAD / tree / inputs are unchanged. |

### Needs and services (`services.py`, Team Lead piece 5)

A sprint may declare `needs` (`studio`, `blender`, `audio`, `images`, `gpu`, `backend`, `none`): a
`## Needs` block in its plan (the AU adapter reads it into the task spec), `needs:x` tags, or
`data["needs"]` set by the track analysis. Nothing is guessed from the old free-text
`**NEEDS (MCP):**` line, and a sprint without needs behaves exactly as before. Before the
implementer session starts (after the infra backoff, before any lock), the engine asks
`services.ready(needs)`; the services the needs map to must be up (a cheap probe stored in the
`services` table, trusted 30 s). A missing one sets the task's note to `waiting for <service>`,
retries with a 30 s .. 10 min backoff, uses no attempt and never fails the task; when a Foundry Client
component runs that service the coordinator leaves a `start_service` client request (at most one
per service every 10 minutes). The task observation carries `needs` and `service_wait` for the
Queue tab. A profile may override a probe with `[services.<name>] port | host | url | enabled`.
`kilo_config.track_kilo_config(track)` writes a track worktree's `kilo.jsonc` with the MCP servers
its needs allow (see `docs/CLIENT_MANAGER.md` for the map and the Kilo assumptions).

## Setting it up (Victor's workstation)

1. **Foundry:** add a coordinator key. Access → Clients & keys → preset
   **coordinator** fills a role-based persona list: `reviewer` →
   `Foundry-Kilo-Review` (required for normal direct review) and
   `deep_reviewer` → `Foundry-Kilo-Deep-Review`. The key needs no admin scope and
   no `Foundry-Kilo-Local`: the worker and helpers run on Kilo's own key.
   ```yaml
   clients:
     - name: coordinator
       key: "${FOUNDRY_KEY_COORDINATOR}"
       scopes: [inference, orchestration]
       personas: [Foundry-Kilo-Review, Foundry-Kilo-Deep-Review]
   ```
   Allowlists match persona ids exactly: a legacy alias such as
   `Foundry-Kilo-Sonnet` does **not** authorize `Foundry-Kilo-Review`, even
   though both can reach `claude-sonnet-5-5`. Foundry ships
   `Foundry-Kilo-Review` as a default persona (enabled; strict:
   `claude-sonnet-5-5` then `gpt-6.1-sol`; refuses otherwise) but doesn't
   validate it for you: Routing → Personas → validate it once (about five
   subscription calls). `doctor` checks reviewers with the coordinator key and
   the worker/helpers with Kilo's key, and says which allowlist to fix. Suggested
   budget rules: `review-per-attempt` (role reviewer, per run, 2 calls) and a
   per-project daily cap (docs/BUDGETS.md).
2. **Kilo config:** Routing → Personas → Kilo config (or
   `GET /admin/api/personas/kilo-config`) gives the provider block plus
   `enabled_providers: ["foundry"]` and `subagent_depth: 1`. Merge them into
   `C:\Roblox\AnimalUprising\kilo.jsonc`. For unattended runs also allow
   `bash`, `edit` and `read` in its `permission` block. The coordinator adds
   its denials per session.
3. **Start Kilo headless** (separate from the VS Code extension's own server):
   ```powershell
   $env:KILO_SERVER_PASSWORD = "<a password>"
   kilo serve --port 4096 --hostname 127.0.0.1
   ```
4. **Environment for the coordinator:**
   ```powershell
   $env:FOUNDRY_COORD_PROFILES = "C:\Users\shock\OneDrive\Documents\ClaudeCode\Foundry-Router\foundry_coordinator\projects"
   $env:FOUNDRY_COORDINATOR_KEY = "<the coordinator key>"
   $env:KILO_SERVER_PASSWORD = "<same password>"
   cd C:\Users\shock\OneDrive\Documents\ClaudeCode\Foundry-Router
   py -3 -m foundry_coordinator doctor animaluprising
   ```
   `doctor` is read-only. It checks the profile, the task source, git, Kilo
   (reachable, provider, `enabled_providers`, `subagent_depth`), Foundry
   (every role's persona offered, routing, validation) and the project (rule
   amendment present, `done_check.py`, hooks, Lune).
5. **AnimalUprising only:** apply `projects/animaluprising/RULE_AMENDMENT.md`
   through an HK sprint. Until `AGENT_RULES.md` contains
   `COORDINATOR-ADVANCEMENT`, the coordinator refuses to start any sprint.
6. **Approve a pilot queue** (non-visual first), then run:
   ```powershell
   py -3 -m foundry_coordinator queue approve animaluprising <ID> --by victor --note "pilot 1"
   py -3 -m foundry_coordinator queue approve animaluprising <ID2> --deps <ID> --by victor
   py -3 -m foundry_coordinator run --until-settled     # or `run` to keep going
   ```

## Operating it

| Command | |
|---|---|
| `status` / `status --json` | every task, its state, attempt, commit, what it waits on; held locks |
| `history P T` | transitions with their evidence |
| `reviews P T` | review results (and rejected ones with the reason) |
| `events [P]` | recent lifecycle events |
| `pause P T --reason ..` | abort the session, keep the state; `resume P T` continues the same session |
| `cancel P T --reason ..` | stop for good, release locks |
| `pause-project P` / `resume-project P` | nothing new starts for that project |
| `unblock P T --resolution .. [--reset-attempts]` | after fixing a blocker |
| `answer P T <text>` | answer the session's question; it continues |
| `approve P T --kind visual --by victor` | approval bound to the current revision and artifact hashes |
| `reject P T --by victor --finding ".."` | back to a repair session with those findings |
| `bridge` | read-only telemetry bridge for the control center (Kilo + this store → Foundry) |
| `run --control [--telemetry]` | also apply control-center operator actions in this engine loop (and run the bridge) |

The **control center** (Foundry `/ui/control`, reached from Work) shows live transcripts,
helpers, alerts and replies, and sends typed operator actions that this engine applies
itself: [CONTROL_CENTER.md](CONTROL_CENTER.md).

Foundry → Monitor → **Coordinator work** shows tasks, bound sessions (persona asked for,
backend/model that answered, calls, tokens, denials) and events.
Foundry → Routing → **Routes** and **Usage Log → Budgets** show the same calls
by route and by rule.

State lives in `%USERPROFILE%\.foundry-coordinator\coordinator.sqlite`
(or `--state`). Raw check output is under `evidence\<project>\<task>\<commit>\`.
Only one coordinator may run on a store (`run --force` takes over a stale one).

### Training-data capture at closeout

With `[training] enabled = true` in a profile, each task that closes
(COMPLETE, closed awaiting approval, or FAILED) gets a pointer manifest in
`<state>/training-spool/`. The manifest lists the task's sessions, SHAs,
verification sets, evidence, reviews and approvals; it holds no prompts or
outputs. `python -m foundry_training queue` processes the spool
asynchronously. `[training.capture] on_failure` (ignore | warn | hold)
decides whether a failed manifest write is ignored, recorded as an event
(the default), or holds the task in CLOSING until it succeeds. See
[TRAINING_DATA.md](TRAINING_DATA.md).

## Registering another game

Project profiles are machine-specific (workspace paths, Foundry URL, model
personas, which optional features run), so `projects/*/project.toml` is
git-ignored and each project commits a `project.example.toml` instead. For
AnimalUprising, copy `foundry_coordinator/projects/animaluprising/project.example.toml`
to `project.toml` in the same folder and change the lines marked `EDIT:`;
sections marked `OPTIONAL` say what they cost or change.

For a new game, copy `contrib/coordinator-pilot/fixture-game/project.toml.template` to
`foundry_coordinator/projects/<id>/project.toml` (or any folder in
`FOUNDRY_COORD_PROFILES`) and fill in:

- `id`, `name`, `version`;
- `[workspace]`: `path`, `allowed_dirs`, `branch`, `remote`;
- `[tasks]`: `source = "json"` with a `file` listing
  `{id, title, spec | spec_file, scope, deps, tags}`, or an `[adapter] module`;
- `[client]`: Kilo URL, `password_env`, provider, agent, permissions;
- `[roles]`: implementer, reviewer, deep_reviewer, visual_reviewer,
  image_generator, researcher → Foundry personas;
- `[foundry]`: `url`, `key_env`;
- `[[verify.checks]]`: name, command, `pass_regex` / `fail_regex`, `phase`
  (verify | closeout), `required`, `evidence`;
- `[review]`, `[approval]` rules (`match` tag, `kind`, `stage`, `artifacts`),
  `[publish]` (`none` | `push`; the coordinator pushes after closeout verification and no
  session ever pushes, so the old `verify_only` reads as `push`; force and tags are refused),
  `[limits]`, `[resources] exclusive`, `[closeout]` (`mode`, `exempt_paths`),
  `[commit]`.

Secrets are never in a profile: give the **name** of an environment variable
(`key_env`, `password_env`). A literal secret-looking value is refused when
the profile loads. A project with its own document formats or tools adds a
small adapter subclassing `GenericAdapter`
(`projects/animaluprising/adapter.py` is the example). The engine doesn't
change.

## Rollback

- **Stop:** Ctrl+C in `run` (state is saved), or `pause-project`. Running
  sessions can be paused (`pause P T`) or cancelled.
- **Turn it off entirely:** stop running `foundry_coordinator`. Nothing in
  Kilo, Cline, the game repository or Foundry depends on it. Foundry's work
  bindings just stop arriving; existing personas and routes are unchanged.
- **Undo a task's commits:** the coordinator never rewrites history. Revert
  by hand (`git revert`). Its pushes are plain fast-forward pushes and are
  recorded in `ops`.
- **Foundry changes in this release** are additive: new tables
  (`work_sessions`, `work_tasks`, `work_events`), new nullable columns on
  `request_log` / `usage_events`, a new client scope. To roll back Foundry,
  check out the previous version; the extra tables and columns are ignored.
- **AnimalUprising:** the amendment is an ordinary flagged rules change.
  Revert its HK commit to return to manual kickoffs.

## Limitations (as of this release)

- **Studio-side gate steps are run by the coding session**, through Studio
  MCP. The coordinator verifies the gate's verdict and that it ran at the
  committed code (R-C). It doesn't drive Studio itself and can't check that
  the pasted Studio lines are genuine beyond their mechanical format. The
  project's raw-evidence rules (§13) remain the control.
- **Not run against your real backends yet.** The live pilot used mock models
  (docs/COORDINATOR_REPORT.md). The real local model's ability to follow the
  kickoff, Meridian on Claude Max, and the Windows paths are unverified until
  run on the workstation.
- **The model's report is parsed from a fenced block** in its final message.
  Kilo's structured output (`format: json_schema`) exists but isn't used, to
  keep it model-agnostic. The report is advisory anyway.
- **Agent Manager visibility:** sessions created through `kilo serve` appear
  in Kilo's session history, but the VS Code Agent Manager panel shows its own
  server's sessions. Use `status` and Foundry's Work tab.
- **Local edits and Studio operations aren't visible to Foundry.** Only model
  calls are. The Work tab says so.
- **Image generation** is reachable through the existing media tools inside a
  session (`generate_image` / `edit_image`, docs/MEDIA.md). The coordinator
  has no image-specific phase; visual approval is the `approve --kind visual`
  checkpoint.
