# Plan: Blender work on the Studio slots (the mini PCs)

Status: **approved 2026-11-01** (Victor: "Yes, recommended": D-B1 to D-B5 as recommended
below). **B1 done (v0.163.0), B2 done (v0.164.0).** B2b (the asset library on the slots) is
next: until it lands, keep "Blender runs on" at this PC for env and NPC builders. Victor: "I thought we were going to add blender in
the container so it would use the CPU on the mini PCS to do blender actions / creations"; "do the
container plan first so I can implement this, then when its complete, work on the workstation".
It comes before docs/plans/LINUX_WORKSTATION_PLAN.md W2.

## Where things stand

- **Installed but unused:** the slot image already has Blender 4.5.12 LTS, the same build as the
  PC, and it runs on slot-1 (`blender -b --version`). Nothing sends Blender work there yet.
- **How Blender is called:** asset sprints run AU's builder scripts **headless**, from the
  agent's shell on the PC:
  `blender -b [file.blend] --factory-startup -P tools/blender/<tool>.py -- <args>`. The call goes
  through `tools/guard/run.py --shell none` with the Windows path (AU docs/ENVIRONMENT.md).
- **blender-mcp** (the interactive add-on) is switched off in AU's kilo.jsonc. The headless
  builders are where the CPU goes.
- **Slots today:** a slot only takes a track's **Studio** work: the gate run and the Studio MCP
  servers. The track's Kilo and its shell stay on the PC.
- **The CPU is there to spare:** with the iGPU (v0.162.4), slot-1's Studio needs about 1.5 of
  its 16 cores. The rest can do Blender.

## What gets built

**B1. `foundry-blender`: one command that runs Blender on a slot (the core).**
`python -m foundry_coordinator.slots blender [--slot slot-1] -- <blender args>`, run from a
sprint's worktree. It:
1. **Picks the slot:** `--slot`, else the project's Blender slot (D-B1).
2. **Takes a Blender lock** on that slot (one Blender job per slot at a time). A second job waits
   and shows "waiting for slot-1's Blender".
3. **Brings the slot up to date:**
   - pushes the worktree's commit to the slot's game mirror (the gate's existing push);
   - streams the uncommitted changes and new files (tar over ssh), so the agent never has to
     commit first.
4. **Runs Blender in its own checkout in the container** (`~/work/blender`), so it never disturbs
   the checkout Studio and Rojo use. The output streams back live and the exit code passes
   through.
5. **Copies back** every file the run created or changed (`git status` in the slot checkout),
   into the same paths in the worktree. There is a size cap, and never `.git`. It prints the list.
6. **Logs** each run on the task (slot, seconds, files back) and on Access → Machines (the
   slot's last Blender job).

Blender's thread count follows the slot's CPU (D-B3). Paths in the arguments are repo-relative,
as AU's tools already use them.

**B2. Sprints use it without being told the details.**
- **Settings:** each project gets a "Blender runs on" setting (Automation → Settings): `this PC`
  (today) or a slot. A track bound to a slot uses that slot by default.
- **The sprint prompt** for a sprint with the `blender` need says how to run Blender: the
  `foundry-blender` line, plus a reminder that outputs come back by themselves.
- **Kilo's permission rules:** while Blender is on a slot, the local `blender.exe` is denied, so
  a run can't drift back to the PC unnoticed.
- **AU's selftest** (`selftest_all.py --blender`) gets a slot form, so asset sprints can prove
  the slot's Blender before using it.

**B2b. AU's asset library on the slots** (found by B2's proof, 2026-11-01).
- **The gap:** 22 of AU's Blender tools read model kits from `C:\Roblox\AssetLibrary` (4.2 GB,
  outside the repo). Only 6 honour `AU_ASSET_LIBRARY`; 16 hard-code the path. On slot-1,
  `selftest_all.py` through foundry-blender passed 44 of 50 Blender tools. The 6 failures were
  all "no such file … AssetLibrary/…".
- **Copy:** the library goes to each slot host (`/srv/studio-slot/assetlib`). The first copy is
  4.2 GB; after that foundry-blender sends only the changed files (by size and time), before each
  run.
- **Mount:** read-only into the container (`/opt/assetlib`), with `AU_ASSET_LIBRARY` set.
- **Shim:** for the 16 hard-coded tools, `C:/Roblox/AssetLibrary` and `C:\Roblox\AssetLibrary`
  in the slot's checkout point at it. On Linux those paths are relative to the checkout, where the
  links live.
- **The setting:** the library's folder on the main PC (default `C:\Roblox\AssetLibrary`).
- **The proper fix,** for AU (a game sprint, not Foundry): every tool reads
  `AU_ASSET_LIBRARY`.

**B3. The slot's CPU with Blender.**
- Studio on the GPU needs little, so a slot with a GPU gets a higher default limit: all cores but
  2. Without a GPU it stays at a third (v0.162.1).
- Blender's own threads: `-t` from the limit.
- Access → Machines shows a running Blender job and its CPU.

**B4 (later, optional). blender-mcp on the slot.**
- Blender with the add-on running on the slot's desktop, which you can watch over VNC.
- A track's `blender-mcp` server routed there with `docker exec`, like Studio's.
- Only if the interactive tools are wanted again (they are off in AU today).

## Decisions for Victor

- **D-B1. Which sprints send Blender to a slot:** a per-project "Blender runs on" setting. It
  covers main and track sprints alike, so asset sprints on main use the mini PC too. A track bound
  to a slot uses its own slot. *Recommended.* The alternative, only slot-bound tracks, would
  leave main asset sprints on the PC.
- **D-B2. The working copy sent to the slot:** the commit plus the uncommitted changes, streamed
  (B1.3). *Recommended:* agents run Blender mid-sprint, before committing. The alternative,
  committed state only, would make every Blender run need a commit first.
- **D-B3. CPU for a slot with a GPU:** all cores but 2 by default, still adjustable in Settings,
  and Blender uses that many threads. *Recommended.*
- **D-B4. If the slot is down or busy:**
  - *Recommended:* wait up to 10 minutes for a busy slot, then fail with the reason, so the
    sprint reports it like any blocker.
  - A slot that can't be reached fails at once with the reason. It never falls back to the PC
    silently, because outputs from two places would differ.
  - Switching "Blender runs on" back to `this PC` is one setting.
- **D-B5. blender-mcp (B4):** later, only if you want the interactive add-on tools back.
  *Recommended:* skip for now.

## Sizes, order, checks

- **Order:** B1 → B2 → B3 (three releases); B4 later.
- **Effort:**
  - B1 medium: the sync and copy-back reuse the gate's push and tar code.
  - B2 small to medium: one setting, a prompt line, a permission rule.
  - B3 small.
- **Proof before B2 turns it on for sprints:** on slot-1, run AU's `selftest_all.py` Blender
  tools and one real builder (`build_bs_king.py --selftest`, then a real build) through
  `foundry-blender`. Compare the output files with the same run on the PC, which should give the
  same files (same Blender build). Then time both.
- **Not changing:** Studio gates, the router, the secret store. A slot's Studio and Blender share
  the machine but not the checkout.
