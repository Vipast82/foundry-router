# Installing a main workstation on Linux

The **main workstation** runs the coordinator, the Kilo server, the game checkout, Blender for asset
sprints, and the Foundry Client that starts and watches them. Today that is a Windows PC. This
guide puts it on Linux: a Linux PC, or a Proxmox LXC. It also covers moving the work over from
the old workstation, and back. The design and the decisions are in
[plans/LINUX_WORKSTATION_PLAN.md](plans/LINUX_WORKSTATION_PLAN.md).

Only one workstation runs a game's sprints at a time. A new one starts on **standby** while
another one holds the game, so installing never takes the work away from your current PC.
You move it with **Make active** (below) when you are ready.

## Before you start

1. **The router is v0.176.0 or newer.** Workstation enrollment and the one-active check came in
   that release.
2. **The game's clone settings** are set on Automation → Settings → Workspace:
   - `workspace.clone_url`: where the game is cloned from, e.g.
     `git@github.com:you/AnimalUprising.git` or an https URL.
   - `workspace.repo_key_secret` (private repos): the name of a secret in the game's group on
     Access → Secrets. It holds either an ssh **deploy key** (the private half; add the public half
     to the repo as a read/write deploy key) or an https **token**. The workstation gets it once,
     at enrollment, and stores it only for its own `foundry` user. The secret store must be
     unlocked.
   - `workspace.branch`: the work branch, as today.
3. **Add the workstation** on Access → Machines → **Add a workstation**:
   - a name, e.g. `ws-linux`;
   - the game;
   - the machine's static IP;
   - **"a Studio for main sprints here"**: on for a machine that will run main sprints. Linux has
     no native Studio, so it runs in the Studio slot container on the same machine, as a slot
     named `<name>-studio`. Give it the main checkout's TEST place, never LIVE.

   You get a one-time **enrollment code** and the exact commands to run.

## Install

### A Proxmox LXC, in one go

In the Proxmox **host** shell:

```bash
curl -fsSLO https://raw.githubusercontent.com/Vipast82/foundry-router/v<router version>/install/create-workstation-lxc.sh
bash create-workstation-lxc.sh
```

It asks for the container id, IP, sizes, the router address and the code, then makes the LXC and
runs the workstation installer inside it.
- **Sizes (D-L4):** 8 cores, 32 GB, 120 GB. With 16 GB, multiplayer playtests stay MANUAL.
- **The iGPU:** it can pass the iGPU in. That helps Studio's view and Blender's viewport; Cycles
  stays on the CPU.

### A Linux PC, or an LXC you made yourself

Debian 13 or Ubuntu 24.04. An LXC needs `nesting=1,keyctl=1` for the Studio container. As root:

```bash
apt-get update -qq && apt-get install -y -qq curl
curl -fsSLO https://raw.githubusercontent.com/Vipast82/foundry-router/v<router version>/install/workstation.sh
bash workstation.sh --router http://<router>:11435
```

The router's own copy of the installer runs if this one differs from the router's release (it
fetches `install/workstation.sh` and `install/pins.env` at the router's tag first).

### What the installer does

1. **Packages:** git, Python, Node 22, Kilo, OpenCode, PowerShell 7, rokit, Blender 4.5 LTS (the
   slots' build), at the versions in `install/pins.env`; npm packages are checked against the
   registry's integrity. A `foundry` user. Everything lives under `/srv/foundry`:
   - `app/`: the Foundry release, also the deploy checkout;
   - `venv/`: Python for the client and coordinator;
   - `game/<id>/`: the game checkout (one per project);
   - `state/`: the coordinator's state;
   - `config/`: the media adapter's settings.
2. **Enrollment:** it trades the code for four keys of its own:
   - `<name>` (coordinator);
   - `<name>-kilo` (Kilo and the media adapter);
   - `<name>-relay` (the operator relay);
   - `<name>-opencode` (OpenCode, for a project whose coding client is OpenCode).

   They are stored in a mode-600 file of the `foundry` user (D-L5, `client.secret_store = file`),
   never printed. Revoking the workstation on Machines turns all of them off.
3. **Settings:** it clones the game and writes `client.toml`, the coordinator's `project.toml`
   (from the release's example with this machine's paths; a project with no example of its own
   starts from the generic template, `foundry_coordinator/projects/_template`), and a first
   `kilo.jsonc`. That first file holds the router's provider block, the implementer model and the
   media adapter. When the project's `[client] kind` is `opencode` (Automation → Settings →
   Client), it also writes `opencode.jsonc` (the coordinator's `agent-config`) and turns the
   OpenCode server on (`components.opencode`). Values the router holds still come from the router.
4. **Import:** with `--import FILE`, it brings over the work from the old workstation (below).
5. **A Studio here:** it runs the Studio slot installer (`setup-host.sh`) for `<name>-studio`. The
   build takes 20-40 minutes the first time. It then prints the VNC address for the one-time
   Studio sign-in.
6. **The service:** it installs the Foundry Client as a systemd service, which starts at boot with
   no sign-in. It then writes Kilo's role rules into `kilo.jsonc` and runs `doctor`.

Useful afterwards (as root):

```bash
runuser -l foundry -c '/srv/foundry/venv/bin/python -m foundry_client status'
```

```bash
runuser -l foundry -c '/srv/foundry/venv/bin/python -m foundry_client doctor'
```

The client's settings page listens on 127.0.0.1:47811 of the workstation. Reach it with
`ssh -L 47811:127.0.0.1:47811 foundry@<ip>`, then open http://localhost:47811/.

**Blender:** asset sprints run Blender headless (`blender -b`). That needs no desktop. With
`blender.pool`, a `local` worker on this machine uses `/opt/blender/blender`, or
`blender.local_exe` if set. The asset library is not copied by the installer. Copy your
`C:\Roblox\AssetLibrary` to e.g. `/srv/foundry/AssetLibrary` (owned by `foundry`), then set
`blender.library` to it when this workstation becomes active.

## Running it again: repair and upgrade (no code)

An enrolled workstation needs **no code**. Run the installer again and press Enter at the code
question (or pass `--repair`): it reads its settings from the router again with its own
coordinator key (`GET /v1/foundry/control/workstation-enrollment`) and

- keeps its keys, the Kilo and OpenCode server passwords, `project.toml`, `kilo.jsonc` and every
  `client.toml` value you changed (only unset or default values are filled in);
- upgrades Foundry to the router's release. While the client runs, the checkout is moved the
  client's own way (the same job as Client page → **Update**: at a gap in the work, the
  coordinator and the client restarted, rolled back on a failure), never by a checkout under a
  running coordinator. With the client stopped, the installer checks the tag out itself.

A **code** is only for a first install or a deliberate re-enrolment: **New code** on Access →
Machines (or `--reenrol` to be asked for one) makes fresh keys, and the installer then restarts
the running client (`systemctl --user restart foundry-client`) so nothing keeps a revoked key.

## Several projects on one workstation

1. On Access → Machines → **Add a workstation**, give the enrolled workstation's **name** and the
   other project. The machine row then lists both projects, and its code **adds** the project:
   the workstation's keys stay as they are and now cover both projects (the first project keeps
   working). **New code** stays the full re-enrolment, with fresh keys for every project.
2. Run the installer on that workstation with the code. It clones the second game into
   `/srv/foundry/game/<id>`, writes its `project.toml` and `kilo.jsonc` (and `opencode.jsonc` when
   that project uses OpenCode), and adds it to `client.toml`:

   ```toml
   [workspace]                       # the default project: as before
   project = "animaluprising"
   game = "/srv/foundry/game/animaluprising"

   [workspace.projects.animaluprising]
   game = "/srv/foundry/game/animaluprising"
   kilo_config = "/srv/foundry/game/animaluprising/kilo.jsonc"

   [workspace.projects.othergame]
   game = "/srv/foundry/game/othergame"
   kilo_config = "/srv/foundry/game/othergame/kilo.jsonc"
   ```

   An older `client.toml` without `[workspace.projects]` reads exactly as before.
3. Each project still has one active workstation (Make active on Machines, per project).

## A Windows workstation

As an administrator in PowerShell (Access → Machines shows this line with the router's address;
choose OS: Windows when adding the workstation):

```powershell
irm https://raw.githubusercontent.com/Vipast82/foundry-router/v<router version>/install/workstation.ps1 -OutFile workstation.ps1
powershell -ExecutionPolicy Bypass -File workstation.ps1 -Router http://<router>:11435
```

- An https router: `-Router https://<router>:11436 -Fingerprint <SHA-256 from Access → Secrets → Transport>`.
- It installs Git, Node 22, Python 3.12 and PowerShell 7 with winget (at `install\pins.env`'s
  versions where winget takes one), Kilo and OpenCode (npm, integrity checked), then the same
  Python steps as on Linux (`workstation apply` / `profile`) under `C:\Foundry` (`-Root`). Keys go
  to Windows Credential Manager (`-SecretStore`).
- **rokit** is not downloaded (no Windows build is pinned yet): with a rokit on PATH it runs
  `rokit install` in the game; without one it says where to get it.
- `-Import FILE` brings over an export; `-AtBoot` starts the client at boot (Task Scheduler; Windows
  asks for the account's password itself) instead of at sign-in.
- It ends with `apply-kilo-config` (once the client answers) and `doctor`. Run again without a
  code to repair / upgrade, as on Linux (`-Repair`, `-Reenrol`, `-Project`).

## Moving the work over (Export / Import)

On the **old** workstation:
1. Wait until no sprint session is running.
2. Stop the coordinator: Foundry Client → Status → Coordinator → Stop.
3. Commit or stash any change in the game checkout and in each track worktree. The export refuses
   uncommitted changes to tracked files and lists them.
4. Export (Windows):
   ```powershell
   py -3 -m foundry_client workstation export --out C:\Users\<you>\move.fwx
   ```
   It asks for a passphrase of 12+ characters. The file holds:
   - the coordinator's state (its database, settings cache, build journal, evidence, prep patches);
   - the local `project.toml`;
   - every `kilo.jsonc`;
   - a git bundle of the commits that are only on this PC, with every local branch's tip, so track
     branches travel even if never pushed.

   It holds no keys and no logs.
5. **The old PC steps down.** The export switches its coordinator off in `client.toml` and hands
   the game over on the router. Pass `--keep-active` to do neither (for a test run).

On the **new** workstation, after the install:

```bash
pct push <ctid> move.fwx /root/move.fwx      # from the Proxmox host, for an LXC
bash /root/workstation.sh --import-only /root/move.fwx
```

That stops the client, imports, and starts it again. Import:
- restores the state;
- rewrites the old paths to `/srv/foundry/...`, including inside the state database and JSON
  files;
- recreates the branches and the track worktrees;
- turns off the MCP servers in `kilo.jsonc` that only run on Windows, and lists them.

The Studio MCP servers come back once main sprints use the slot: set
Automation → Settings → `studio.main_slot` = `<name>-studio`.

`--import FILE` on a fresh install does the same in one run.

Then on Access → Machines → **Which workstation runs each game**:
- If the export handed the game over, the new workstation took it at its first check-in.
- Otherwise press **Make active** for it. The old one goes on standby within a minute.

## Moving back

The same in the other direction:
1. Export on the Linux workstation:
   ```bash
   runuser -l foundry -c '/srv/foundry/venv/bin/python -m foundry_client workstation export --out /tmp/move.fwx'
   ```
2. On the Windows PC: `py -3 -m foundry_client workstation import move.fwx --root C:/Roblox/ws`.
3. Point the client's Settings back at its folders.
4. Switch the coordinator on, and press Make active.

Keep the Windows PC as a standby workstation until the Linux one has run a week of sprints (D-L6).

## When something is off

- **"standby" on Machines for the workstation you want:** press Make active for it. The check is
  `workspace.single_workstation`, on by default. It fails open: an old router or one that can't be
  reached never stops sprints.
- **Enrollment says "this code is for a Studio slot":** the code came from "Add a Studio slot". Use
  "Add a workstation" instead.
- **The clone fails:** check `workspace.clone_url`. For a private repo, set
  `workspace.repo_key_secret`, make sure the secret store is unlocked, then run the installer again
  (no code needed: a repair with the game folder missing asks the router for the repo key once
  more).
- **`apply-kilo-config` failed:** run it as `foundry` once the client is up:
  ```bash
  /srv/foundry/venv/bin/python -m foundry_client apply-kilo-config
  ```
