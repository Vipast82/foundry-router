# Plan: Distributed Foundry

Status: **D1-D7 and phase a approved (Victor 2026-11-27: "Approve, start a"). Phase a built in
v0.181.263** (the slot image change is not yet built on a slot: it waits for the spare slot).
Phases b-f each need their own go-ahead. Design: `docs/DISTRIBUTED_FOUNDRY.md` (decisions D1-D7
in its §13). Nothing in this plan touches the live coordinator, router or slots until a phase's
cut-over step is approved on its own.

## Rules for every phase

- **One release per phase step**, version bump + `vX.Y.Z` tag, main = cline (docs/CONVENTIONS.md).
- **Off by default.** Every new behaviour sits behind a switch that ships off. With every switch
  off, the release behaves exactly like the one before; a test asserts that.
- **Built on a branch** in a worktree off `C:\Users\shock\foundry-revamp\_base` (the OneDrive
  checkout cannot make worktrees). **Tested on a spare machine** (D7): a fourth Linux slot and, for
  the Windows parts, a Windows VM. Never on slot-1/2/3 or the live coordinator.
- **The gate runs in parts** under the client's 60-minute pytest limit: `tests` without
  coordinator, then `tests/coordinator` in two halves, then `test_animaluprising.py`, each with
  its own short `--basetemp` (memory notes *gate-split-40-min-cap*, *parallel-pytest-basetemp*).
- **Deploy** only after the Lead check, never during a Lead repair, through the client's Updates
  card (memory notes *deploy-after-lead-check*, *client-self-update*).
- **Rollback** of any phase = turn its switch off, or check out the previous tag. No phase
  migrates data that the previous release cannot read.

Estimates are working days of build and test on my side. Victor's own steps (making a VM,
signing Studio in, approving keys) are listed separately and are usually under an hour each.

## Phase a: platform layer and both clients in the images

**What**
1. `foundry_platform/` (stdlib only): paths, job shell (`bash -lc` / `pwsh`), process start and
   kill tree, no-console flags, file locks, service install, UTF-8 file helpers. Move the 8 copies
   of `_NO_WINDOW` and the ~20 files' `os.name` branches onto it, with no behaviour change.
2. The slot image gains Kilo and OpenCode (npm, pinned versions, the image's Node 22). No
   container starts them yet.
3. A GitHub Actions matrix (`ubuntu-latest`, `windows-latest`) for `tests/platform/` and the fast
   unit tests; a `foundry-client doctor --platform` smoke command.

**Switch:** none needed. It is a refactor plus image contents; the tests pin the old behaviour.
**Ship when:** the full gate passes on Windows, the platform tests pass on both CI jobs, the image
builds on the spare slot, and `kilo --version` / `opencode --version` run inside it.
**Rollback:** previous tag; the image without the two packages (one Dockerfile line each).
**Estimate:** 3-4 days (the refactor touches many files; each move is mechanical but needs its
own test).
**Victor:** make the spare Linux slot (`create-slot-lxc.sh` or a VM + `setup-host.sh`) with its
own private place (see `contrib/studio-slot/README.md`, "Each slot needs its own place"); sign
Studio in once.

## Phase b: the lane agent, the tunnel and remote sessions (spare slot; Linux, then Windows)

**What**
1. Client `slot` profile: runs headless in the slot container, supervises `kilo serve` and
   `opencode serve` on 127.0.0.1, and serves `/lane/health`, `/lane/git`, `/lane/file`,
   `/lane/stat`, `/lane/run` (design §3) with the agent token.
2. The coordinator's tunnel manager: one `ssh -N -L ...` per slot with `lanes = true`, restarted
   with backoff, state on the board.
3. `RemoteWorkspace(Workspace)`: `run()` goes to `/lane/git`; same return values and errors as
   `Workspace.run` (`foundry_coordinator/adapters/git.py:39-54`).
4. `ClientSet` members per slot server (URL = tunnel port); the engine picks the member from the
   lane's host.
5. Router: the slot agent keys (D5): enrollment adds `<slot>-kilo` / `<slot>-opencode` (inference
   scope, `kilo` / `opencode` persona allow-list), delivered through `slot-secrets`.
6. Windows: the same agent on a Windows VM (native Studio, OpenSSH server, scheduled task);
   `install\slot.ps1` up to "agent healthy".

**Switch:** `[slots.<id>] lanes = true` (UI: Access → Machines and Automation → Settings → Studio
slots; also in the config POST allow-list, memory note *config-post-allowlist*). Off by default.
**Test:** on the spare slot, a scratch project (like `opencode-pilot`) with a second
coordinator profile that registers no ssh key of its own (memory note *opencode-backend-plan*:
a second coordinator `run` replaced the live key once). One sprint on Kilo, one on OpenCode, both
remote. Then pull the network cable / stop the tunnel mid-session and check the recovery in
design §4.3.
**Ship when:** both scratch sprints reach COMPLETE remotely; the tunnel-loss test recovers with no
lost commit; the Windows agent passes `doctor --lane`.
**Rollback:** `lanes = false` (the default): no tunnel, no agent use, nothing else changes.
**Estimate:** 6-8 days (Linux 4-5, Windows 2-3).
**Victor:** approve the two new router client keys for the spare slot; make the Windows VM (D6)
and sign its Studio in.

## Phase c: remote Lune and Studio gates for a remote lane (spare slot)

**What**
1. The 79 direct worktree reads move to `ws.read` / `ws.stat` / `ws.exists`, one module at a time
   (engine, studio_calls, visual, visual_approval, asset_judge, plancheck, kilo_config, the AU
   adapter, ...). Local `Workspace` implements them with plain file calls, so local lanes do not
   change.
2. The gate for a remote lane: `/lane/run gate`. `slot-checkout` fetches from the local bare
   copy (no network push). Outputs are copied from the Studio checkout to the lane worktree with
   the same `OUTPUT_GLOBS` (`slots.py:59`) and committed through `/lane/git`.
3. Lune: `lune_cache.input_key` runs through the agent; PASS results stay in the junction's store
   (`lune_cache.py:203-204`), so a PASS on a slot carries to the merge gate.
4. Studio MCP for remote lanes: plain local commands in the slot's agent config.
5. Windows slot: the gate's host half under `pwsh` natively, Studio through the client's
   `studio.py`; the frame checks named in design §7.3 (one UX-1 AlwaysOnTop probe, a dressed boss
   model, `PreloadAsync`) are run once and the results written into the design.

**Switch:** the same `lanes = true`, plus `[studio_calls] remote_gate` (default on when
`lanes = true`, so it can be turned off alone if the gate path misbehaves).
**Test:** a TEST-universe AU copy on the spare slot's own place; a sprint whose gate has Lune,
scripted Studio rows and frames; compare its gate result with the same commit gated by today's
path (worktree on the junction, Studio on the slot). They must match row for row, except rows
already marked timing-dependent.
**Ship when:** the row-for-row comparison matches; a second run of the same commit reuses the
Lune PASS (no rerun); the read-API refactor passes the full gate with every switch off.
**Rollback:** `remote_gate = false` (gates go back to today's push-and-copy path) or
`lanes = false`.
**Estimate:** 6-8 days (the read-API refactor is most of it).
**Victor:** none beyond keeping the spare slot's place on TEST.

## Phase d: lane worktrees on slots and the hand-back to the junction

**What**
1. `tracks start ... --slot <id>` on a `lanes = true` slot creates the worktree on the slot
   (design §4.1), records `host` in the track row, and fetches every new commit to the junction
   (design §5).
2. Stop and fail: fetch first, `save-dirty`, then remove the slot worktree only when both sides
   agree on the tip (design §4.4).
3. Merge (D4): the junction builds a sparse merge worktree from the fetched branch; `merge.py`
   runs on it unchanged otherwise; the slot worktree is removed after `track_merged`.
4. Board / control center / CLI: a host column for tracks and sessions; `tracks list` shows
   `slot-2 (remote)`; the slot's disk and load next to its lanes.
5. Failure handling from design §8: slot away, diverged branch, disk full, version differs.
6. The watch duties that read tracks (unmerged-work check, memory note *stranded-track-work*)
   read the fetched refs.

**Switch:** `[automation] remote_lanes = true` (project level) **and** the slot's `lanes = true`.
Both off by default.
**Test:** on the spare slot: a two-sprint lane from start to `track_merged` on a scratch branch of
the TEST copy (not AU master); a conflict that sends a Lead repair; a stop mid-sprint; a slot
power-off mid-sprint (no commit lost, the decision appears); a disk-full test with a filled
scratch volume.
**Ship when:** all five tests pass; with the switches off, the full gate matches the previous
release.
**Rollback:** switches off. Running remote lanes are first fetched and merged or stopped (a
command, `tracks bring-home <lane>`, does fetch + `save-dirty` + a junction worktree from the
fetched tip, so the lane continues locally). New lanes are local.
**Estimate:** 7-10 days.
**Cut-over step d-live (separate approval):** one live AU lane on one live slot, chosen by Victor,
at a quiet moment (no main sprint near its merge). Watch it to `track_merged`.
**Victor:** approve d-live and pick the slot and lane.

## Phase e: the main-workstation (junction) container

**What**
1. `install/junction/`: a Docker image built from what `install/workstation.sh` already installs
   (coordinator, client headless, git, Python, Node 22, Kilo, OpenCode, rokit/Lune, Blender), plus
   a Compose file with `studio-main` from the slot image (`[studio] main_slot`, built in W2).
2. `install\workstation.ps1` for the Windows junction: today's setup as one idempotent script
   ending in `doctor`.
3. Export / Import (built in W4) carries the state, including remote-lane rows and their fetched
   refs.
4. The junction container runs as a **standby** workstation first (W4's one-active rule), so it
   can be checked against the live one without running sprints.

**Switch:** none on the live system: the container starts as standby.
**Test:** build and start it on a spare VM; Import a fresh Export from a scratch coordinator;
`doctor` green; Make active on a scratch project only; one scratch sprint end to end with one
remote lane on the spare slot.
**Ship when:** that sprint completes and merges, and Make active back to the scratch Windows
profile works.
**Rollback:** the container stays on standby or is deleted; the Windows PC stays active.
**Estimate:** 4-5 days.
**Victor:** make the VM; decide later whether the junction moves (that is a separate approval and
not part of this plan).

## Phase f: cut-over

One slot at a time, each step its own approval, each at a quiet moment:
1. slot-3 (`lanes = true`), one week of lanes;
2. slot-2;
3. slot-1 (its `project.toml` entry is today's local `studio-slot-poc` container on this PC; it
   needs its remote `host` settings first, or it stays a Studio-only slot);
4. optional: the Windows slot as a fourth lane host, and the frame-timing rule change from design
   §7.3 (a separate decision);
5. optional, much later: the junction moves to the container (phase e) with the Windows PC on
   standby for a week (as LINUX_WORKSTATION_PLAN D-L6).

**Rollback** per slot: `lanes = false`, after its running lane is merged or brought home.
**Estimate:** about 1 day of my time per slot, spread over the watch weeks.

## Totals

| Phase | Build + test | Live change |
|---|---|---|
| a | 3-4 days | none (a release that changes no behaviour) |
| b | 6-8 days | none |
| c | 6-8 days | none |
| d | 7-10 days | one approved lane (d-live) |
| e | 4-5 days | none (standby) |
| f | ~1 day per slot | one slot per approval |
| **Total** | **about 27-36 working days**, plus the watch weeks of f | |

Phases a → b → c → d are in order (each needs the one before). Phase e needs only a and b and can
run beside c/d.

## What I need from Victor to start

1. Approve the design decisions D1-D7 (design §13), or change them.
2. Approve phase a only. Phases b-f each come back for their own go-ahead.
3. For phase b: a spare Linux slot machine (VM or LXC) and, later, a Windows VM.
