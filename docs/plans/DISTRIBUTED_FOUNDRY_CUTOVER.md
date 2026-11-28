# Distributed Foundry, phase f: the cut-over runbook

Status: **written 2026-11-27, nothing run.** Every phase before this ships off by default in
v0.181.270. Each step below changes the live system, and each needs Victor's go-ahead when it
comes, at a quiet moment: no main sprint near its merge, and no Lead repair running (memory note
*deploy-after-lead-check*). Design: `docs/DISTRIBUTED_FOUNDRY.md`. Plans:
`docs/plans/DISTRIBUTED_FOUNDRY_PLAN.md`, `docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md`.

## Before any step: the pilot (b-4) has passed

On the spare slot-4 with `distributed-pilot`, following the runbook in the session notes
(`DISTRIBUTED_PILOT_RUNBOOK.md`):
- DP-1 completes on Kilo and DP-2 on OpenCode;
- a lost tunnel and a stopped agent each come back with no lost commit;
- a reboot of the slot shows "slot away" and moves nothing;
- the training store holds both sessions;
- an AU lane on slot-4 (one small tier-A sprint, TEST universe) completes with its Lune rows and
  Studio gate run on the slot, and merges through bring-home.

## Step 0. Deploy

1. Rebuild the router to v0.181.270. Its Lanes button and the slot fields `lanes`, `os` and
   `studio` need it; until then the router refuses a revision that sets them.
2. Deploy v0.181.270 to the coordinator: the client's Updates card, or a checkout of the tag plus
   a restart.
3. **Check:** `tracks status animaluprising` reads as before; every lane shows no host.

**Rollback:** the previous tag. Nothing is on yet.

## Step 1. slot-3 holds lanes (one week)

1. Access → Machines → slot-3 → **Lanes: on**. This makes `slot-3-kilo` / `slot-3-opencode` and
   needs the secret store unlocked.
2. Automation → Settings → Studio slots → slot-3 → **lanes** on.
3. `setup-host.sh` again on its host, when slot-3 holds no lane: no code needed (press Enter at
   the code question; it keeps slot-3's key and reads its settings again). It builds the image
   with Kilo, OpenCode and the lane agent, and makes `/srv/foundry-lanes/animaluprising.git`.
4. Automation → Settings → animaluprising → **remote_lanes** on.
5. The next track started with `--slot slot-3` is a lane on slot-3. Tracks already running stay as
   they are: only new tracks become remote.
6. **Watch for a week:**
   - `tracks status` (host and tunnel);
   - slot-3's disk on the board;
   - "slot away" or "branches differ" notices;
   - merges that bring lanes home.

**Rollback:**
1. Turn `remote_lanes` off; new tracks are local again.
2. Bring running remote lanes home with `tracks bring-home animaluprising <lane>`.
3. Optionally, set slot-3's `lanes` off and press Lanes: off (this deletes its two keys).

## Step 2. slot-2

The same as step 1 for slot-2, after step 1's week.

## Step 3. slot-1

slot-1's `project.toml` entry is the local `studio-slot-poc` container on this PC (no `host`). It
can hold lanes only as a remote slot: its Proxmox settings first (the README's "Moving a slot from
this PC to Proxmox"), then step 1 for it. Otherwise it stays a Studio-only slot.

## Step 4 (optional). A Windows slot

1. A Windows VM with a GPU, its own Roblox place and its own signed-in Studio.
2. Access → Machines → Add a Studio slot `slot-w` with **OS: Windows** and its ssh user (the
   router sets `os = windows`, `studio = native` and shows the PowerShell line), then
   `install\slot.ps1 -Router <url>` on the VM. The script makes Git for Windows' bash its OpenSSH
   shell, keeps the workstation keys in step (scheduled task "Foundry slot keys") and runs the
   lane agent on 127.0.0.1.
3. The slot's settings: `lanes = true` (Lanes: on).
4. **What it can take:** lanes without Studio needs (the native-Studio gate is not built). Its
   first use is measuring what the Linux slots cannot: frame timing, dressed boss models, window
   captures. That is a separate decision about the `workstation_only_sprints` rule.

**Rollback:** set its `lanes` off, or delete the machine.

## Step 5 (optional, later). The junction moves to its container

1. A Linux VM; `install/junction`: `docker compose build`, `run --rm junction install`, then
   `up -d`. It starts on **standby**.
2. `foundry-client workstation export` on this PC at a safe point, then `install --import` on the
   junction.
3. Access → Machines → Workstations → **Make active** for the junction. This PC goes to standby.
4. Keep this PC on standby for a week of sprints (LINUX_WORKSTATION_PLAN D-L6).

**Rollback:** Make active back on this PC.

## What stays true at every step

- **One writer per lane.**
- **Scope and commit checks:** run in the coordinator, on committed diffs.
- **Session rules:** carried with each session to the slot's Kilo / OpenCode.
- **No `--no-verify`.**
- **No force push to `origin`:** only the junction publishes, and lane branches come home
  fast-forward only.
- **No work discarded:**
  - every stop and bring-home saves uncommitted files here first;
  - a slot's worktree is removed only at a tip the junction holds, and only with exactly the files
    it saved.
- **TEST universe only:** every slot keeps its own private place.
