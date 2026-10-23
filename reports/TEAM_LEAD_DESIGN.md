# Team Lead: parallel tracks, shared Studio, project memory, service management

Draft 2026-10-22 (decisions by Victor the same day). Builds on v0.134.x: the chat engine,
typed control actions, the config store, role rules, the Foundry Client relay.

## What it does

The coordinator runs a project like a development team with a lead:

1. **It reads the whole sprint plan set** (`docs/progress/*_PLAN.md`), works out what each sprint
   needs (Studio, Blender, audio MCPs, backend only), what it depends on, and which sprints can run
   side by side.
2. **The chat proposes tracks** ("while UI-03a uses Studio, Blender sprints 20–25 and backend
   sprints 31–33 can run in parallel; 2 extra agents fit your limit"). You confirm, and it starts
   them. Later, "add an audio track" or "stop the Blender track" works the same way.
3. **Each track runs in its own git worktree and branch**, with its own Kilo session, services and
   memory slice. The main sprint keeps the main checkout.
4. **Studio is shared, main first, at merge time.** Studio is bound to one Rojo serve of one
   folder and the plugin reconnect is manual, so a track never drives Studio from its worktree. A
   track does its Studio-free work there; its Studio verification (stories, gate, asset import)
   runs in the main checkout when the track merges, in the main sprint's idle windows. The main
   sprint never waits more than one bounded run.
5. **Finished tracks merge back.** The coordinator rebases the track onto master, runs the gate
   on the merge result (only checks the track's files affect), lands it, and records the merge.
   A conflict it can't resolve becomes a Lead repair, then a decision with a default.
6. **Memory lives in the repo** under `docs/memory/`, indexed by the coordinator, and every
   session, the chat and you read the same notes.
7. **Services are managed**, not assumed: every sprint declares what it needs, the coordinator
   checks that it is up before starting, and the Foundry Client starts or restarts what it can.

## Decisions (2026-10-22)

| Question | Decision |
|---|---|
| Isolation | A git worktree and branch per parallel track; the coordinator merges finished tracks into master and gates the merge. |
| Studio sharing | Main sprint first. A track's Studio work runs at merge time in the main checkout, in the main sprint's idle windows, one bounded run at a time; the main sprint gets Studio back as soon as it asks. (Refined after the audit: Studio can't be re-pointed to a worktree without a manual plugin reconnect.) |
| Memory | `docs/memory/` in the game repo, git-tracked, plus an indexed copy in the coordinator that sends each session only the relevant slices. |
| Concurrency | An operator setting, `max_agents`, in the GUI and the chat; default 1 (today's behaviour). |
| Starting tracks | The chat proposes; nothing starts without a confirm card, unless the class is set to auto. |

## Pieces

### 1. Sprint analysis (`foundry_coordinator/tracks/analysis.py`)

- Parse every plan: files list, affected suites, stories, "Depends on", and a new optional
  `## Needs` block (`studio`, `blender`, `audio`, `images`, `backend`, `none`). When the block is
  missing, infer: UI stories or a gate file → `studio`; `assets/**` with `.blend`/`.fbx` → `blender`;
  `audio/**` → `audio`; otherwise `backend`.
- Build the dependency graph from explicit deps plus file overlap (two sprints that edit the same
  file are serial).
- Output: ordered tracks, each a chain of sprints with its needs, plus the sprints that must stay
  serial and why. Deterministic; no model call. The chat uses this as a read tool.

### 2. Track manager (`foundry_coordinator/tracks/manager.py`)

- `tracks` table: id, project, name, kind (main | parallel), worktree path, branch, base commit,
  state (planned | running | merging | merged | stopped | failed), current sprint, needs, created
  by.
- Creating a track: `git worktree add <workspace>/.tracks/<name> -b track/<name> master`, copy
  the generated `kilo.jsonc` with the track's MCP allow-list, register the worktree as that
  track's workspace.
- A sprint queued "on track X" runs with that track's workspace, locks and memory slice. The
  engine's workspace, session rules and gate all take the track's paths. The main track is the
  main checkout, unchanged.
- `max_agents` caps running sprints across tracks. The main sprint always counts first.
- Merging happens only at a quiescent point of the main sprint (no live main session; between
  its sessions or after it closes), never mid-session. Steps: `git rebase master` in the
  worktree; fast-forward master; run the gate in the main checkout for the merged files (same
  incremental carry) when Studio is idle; record `track_merged`; remove the worktree. The
  coordinator's merge commits are exempt from the main sprint's scope checks (B1 hygiene). On a
  conflict: a Lead repair in the worktree (write access), then a decision with the default
  "stop this track".
- **Shared files conflict by construction** (`STATE.md`, `MASTER_ORDER.md`, `ASSET_LEDGER.md`,
  `RegressionIndex.lua`, `Strings.lua`, `Ui.lua`, `default.project.json`). The analysis marks
  two sprints that edit the same shared file as serial, except files on an `append_safe` list
  (ledgers and registries moved to per-sprint memory files, see 4; `default.project.json`
  mappings and `RegressionIndex` entries merged by the coordinator from per-sprint fragments).

### 3. Resources and the Studio scheduler (`foundry_coordinator/tracks/resources.py`)

- Resources with capacity: `studio` (1, the main checkout's Rojo serve), `gpu` (1: audio, Comfy
  and local Qwen share one card; a sprint declares `gpu` in its needs), `blender` (1), plus the
  existing writer slots. Each is a row in the `locks` table with an owner, a lease and a journal.
- **Studio "in use"** is the gate runner lock plus the main session's last Studio tool call (from
  the transcript ingest), not the session's existence: a 4-hour implementer session no longer
  pins Studio while it is only editing files.
- The main sprint holds priority. Its stories and gate take Studio at once.
- A track's merge-gate takes Studio only when the main sprint has not used it for
  `studio_idle_minutes` (default 10), no main request is pending, and no main session is live in
  VERIFYING. The lease is bounded by `studio_track_max_minutes` (default 45, one gate run) and is
  never interrupted mid-playtest; a main request waits for the run to finish.
- Everything is journaled: who held what, for what, and how long. The GUI shows the timeline.

### 4. Project memory (`foundry_coordinator/memory/`)

- Repo layout, git-tracked: `docs/memory/DECISIONS.md`, `CONVENTIONS.md`, `ASSETS.md` (asset
  registry: name, path, source sprint, status), `KNOWN_ISSUES.md`, `tracks/<name>.md` (per-track
  notes), `sprints/<ID>.md` (per-sprint notes a session may append).
- The coordinator indexes these files (sqlite FTS by heading) on every commit and tick.
- Each session gets a **slice**: the entries whose headings or tags match the sprint's files,
  needs and track, plus the last N decisions, capped at a token budget. Unchanged slices are not
  re-sent (hash per entry, as the memory delta already does).
- Sessions write memory through a structured report block (`MEMORY_NOTE: file, heading, text`)
  that the coordinator validates and commits, so notes are reviewed facts rather than free prose.
- The chat reads and edits memory with tools (`memory_search`, `memory_add`, `memory_edit`).

### 5. Service and MCP registry (`foundry_coordinator/services.py` + client)

- `services` table: name, kind (mcp | process | backend), how to check (port, http, mcp
  handshake), who provides it (client component, backend, external), needed by (track kinds).
- The Foundry Client reports its components and the MCP servers it can see (Blender, audio) in
  its heartbeat; the router stores them; the coordinator reads availability before starting a
  sprint and marks a track `waiting for <service>` instead of failing.
- The chat can start, stop or restart a service through `client_*` actions, and shows a
  services panel.
- Per-track `kilo.jsonc`: the MCP allow-list is the track's needs (a Blender track never gets
  Studio tools; a UI track never gets Blender), generated from the role rules plus the needs into
  each worktree (today's `kilo.jsonc` is untracked, so a fresh worktree has none).
- `max_agents` replaces the CLI-only `--max-writers`: a live profile/config-store setting the
  chat and GUI can change.

### 6. Chat tools (router `chat_engine.py`)

- `analyze_tracks(project)`: the analysis above.
- `propose_tracks(project, max_agents)`: a plan card with the tracks, their sprints and needs,
  and the agent count. Confirm starts the tracks.
- `track_start`, `track_stop`, `track_status`, `track_merge`.
- `set_max_agents`.
- `memory_search`, `memory_add`, `memory_edit`.
- `services_status`, `service_restart`.

### 7. GUI (Automation page)

- A **Tracks** tab: each track as a column (sprint, state, worktree, needs, Studio lease), the
  Studio timeline, the agent count, and start, stop and merge buttons.
- A **Memory** tab: the memory files with search, and the slice each running session got.
- A **Services** tab: the registry with health and restart buttons.

## Audit notes (2026-10-22)

Reused as-is: the generic `locks` table, writer slots, the second-project-on-a-worktree pattern,
`infra.py` preflight, the client `Spec`/health model, the assistant's propose-validate-confirm
pipeline, `repair.py`'s worktree creation, the `kilo_config` JSONC editor, `task_memory` and
`memory_delta`. Built new: the needs model, per-task resource sets, live `max_agents`, the track
and merge managers, project memory, the service registry, and first-class asset tasks (helpers
are advisory only). Known limits: the coordinator tick is single-threaded with blocking git and
HTTP calls, so track counts above 3–4 will slow ticks; asset sprints still need a Studio import
step, which the merge-gate covers.

## Token rules

- Analysis, scheduling, merging and memory slicing are deterministic. The only model calls are
  the sprint sessions themselves and the chat turns.
- A track session gets the same incremental gate, vision and review as the main sprint.
- Memory slices are hashed per entry; nothing unchanged is re-sent.

## Phases

1. **Core:** analysis, track manager with worktrees, `max_agents`, Studio scheduler, merge back,
   chat tools and the Tracks tab.
2. **Memory:** repo layout, index, slices, session notes, chat tools, Memory tab.
3. **Services:** registry, client reporting, per-track `kilo.jsonc`, Services tab.
