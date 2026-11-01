# Plan: the main workstation on Linux (bare metal or a Proxmox LXC)

Status: **plan, nothing built** (2026-11-01). Victor: "If I want to have a LXC container to RUN a
FULL Coding Suite location to take the place of my PC as the main implementor, can we make that?
or have like a Script Installer for everything I need on a current Linux Install". This replaces
phase 3 of docs/plans/FRESH_INSTALL_PLAN.md (workstation enrollment) with a Linux-first version,
and answers its D6 (where the workstation pieces live).

## Goal

One command turns a Debian 13 / Ubuntu 24.04 machine into Foundry's **main workstation**: the
machine that runs the coordinator, the Kilo server, Rojo, the main game checkout, a Studio for
main sprints, and Blender for asset sprints. The same script works:
- on your PC after a move to Linux;
- inside a Proxmox LXC (made by hand, or by a small `create-workstation-lxc.sh`, like the slots).

Moving to it from this Windows PC carries the work over (Export / Import), and switching back is
the same move in the other direction.

## What the main workstation runs today (Windows) and what changes

| Piece | Today | On Linux |
|---|---|---|
| Foundry Client (tray: starts and watches everything) | Windows tray, Startup folder, Credential Manager | **Ready:** headless mode, `install-service` (systemd --user), Secret Service keyring (gnome-keyring or KeePassXC; an LXC uses a file-backed keyring, see D-L5) |
| Coordinator, upgrade supervisor | Python, `C:\Users\shock\foundry-deploy` checkout | Python as is. The deploy checkout path is hard-coded (`cli.py DEPLOY_CHECKOUT`): becomes a setting |
| Kilo server | `kilo.cmd` from npm | `kilo` from npm (the client already looks for it) |
| Rojo | `rojo.exe` (rokit) | `rojo` (rokit; the client already looks for it) |
| **Studio for main sprints** | Windows Studio: the client's `studio.py` reopens it, Kilo's Studio MCP servers start `npx.cmd` | **No native Studio on Linux.** The main checkout's Studio runs in the Studio slot image (Wine/Vinegar, noVNC on the LAN), on the same machine. See W2 |
| Blender (blender-mcp add-on, asset sprints) | Windows Blender with a window | Linux Blender. It needs a display: a virtual desktop with noVNC (D-L3) |
| Game checkout, Foundry dev checkout | `C:\Roblox\AU-integration`, OneDrive checkout | `/srv/foundry/game/<id>`, one release checkout (no OneDrive) |
| Models | router + Strata | unchanged (no GPU needed for models on the workstation) |

## Work items (each a release, in this order)

**W1. Portable paths and launches (small).**
- Replace the hard-coded Windows paths with settings (UI, as always):
  - the deploy checkout;
  - `mcp_stdio.NPX`;
  - the Blender search folders in `reference_render.py`.
- Check every `os.name == "nt"` branch in the coordinator and client (about 30 files) for a
  missing Linux side; most are only window flags.
- The coordinator's test suite gets a Linux CI run (Docker), so Linux stays working.

**W2. A main Studio in a container (the big one).**
- Today only a TRACK can be bound to a slot (`slots.slot_for` returns None for the main project).
  Add a project setting `studio.main_slot` (UI: Automation → Settings), so the main project's
  Studio gate, Studio MCP and Studio reopen go to a slot too.
- On the Linux workstation that slot is a **local** container (`host` empty, already supported),
  `studio-main`, built from the same image as the slots.
- **Its own place:** the main checkout's TEST place (77417134434265), not a slot place.
- **Rojo:** for the main project it runs on the host (the client's Rojo component), and the
  container's Studio connects to it. Track slots keep Rojo inside their container.
- **Not on Linux:** the client's Studio watchdog (`studio.py`, Windows process names) stays
  Windows-only. On Linux the container's own watchdog (entrypoint.sh) does that job.
- **Multiplayer playtests** need ~14 GB in software rendering: sized in D-L4.

**W3. `install/workstation.sh` (medium).** As root, one run:
1. Packages: Docker, git, Python 3.12 venv, Node 22, rokit (Rojo), Blender (D-L3), xvfb/noVNC
   for Blender's desktop.
2. A `foundry` user. Everything lives under `/srv/foundry`: `app/` (the Foundry release),
   `game/<id>/` (the game clone), `state/` (the coordinator's state).
3. **Enrollment** with a code from Access → Machines → **Add a workstation** (a new machine kind,
   like slots). It hands over:
   - the coordinator, Kilo and operator keys (stored in the keyring, never shown);
   - the router address and the game list;
   - each game's clone URL (moved to phase 3 from the router wizard). The repo key is a secret
     in the store; the installer pulls it and never shows it.
4. Clones the Foundry release matching the router, and each game. Writes `project.toml` from the
   router's settings plus the local paths, and `kilo.jsonc` (`apply-kilo-config`) with a random
   Kilo password.
5. Builds the Studio image (20-40 min the first time), starts `studio-main`, and prints the VNC
   address for the one-time Studio sign-in.
6. Installs the client as a systemd service (starts at boot, no sign-in needed), starts
   everything, runs `doctor` and prints its checklist.
7. The coordinator registers its ssh key (v0.162.0), so the slots allow it within 5 minutes.

`create-workstation-lxc.sh` makes the LXC first: Debian 13, nesting + keyctl, the sizes from
D-L4. Then it runs the same installer inside it.

**W4. Moving the work over: Export / Import workstation (medium).**
- **Export** on the old workstation (client tray, or `foundry_client export`), run at a safe point
  (no live session). It makes one encrypted file, like the router backup, holding:
  - the coordinator's state DB and config cache;
  - project memory;
  - the local `project.toml` values;
  - the client settings.

  It does **not** hold the game checkout or keys: games are re-cloned at the same commits, and
  keys come from enrollment.
- **Import** is an installer option (`workstation.sh --import FILE`), or a client action on a
  fresh install.
- Uncommitted work in the game checkout is listed and refused, so you commit or stash it first.
  Nothing is lost silently.
- The old workstation is then switched to **standby**: its coordinator stops claiming work. Two
  coordinators must never run the same project. W4 adds a router-side check: one active
  workstation per project, so a second one refuses to start sprints and says which PC holds them.

**W5. Docs (small).** `docs/INSTALL_WORKSTATION.md`: Linux PC, LXC, moving from Windows, and
back.

## Decisions for Victor

- **D-L1. Where the main Studio runs on Linux:** in the Studio slot image on the same machine
  (W2). *Recommended:* it is the image that already works on slot-1, and nothing else runs
  Studio on Linux. The alternative, a Windows VM just for Studio, keeps a Windows license and a
  second machine to look after.
- **D-L2. One installer for both** (a Linux PC and an LXC), with a thin `create-workstation-lxc.sh`
  for Proxmox. *Recommended.*
- **D-L3. Blender on the workstation:** a virtual desktop (Xvfb + noVNC, like the slot) with
  Blender software-rendering. *Recommended* for an LXC.
  - On a Linux PC with a GPU, Blender can use your real desktop instead; the installer asks.
  - Asset renders are CPU-only in an LXC unless you pass a GPU into it (possible on Proxmox; a
    later step).
- **D-L4. LXC size:** 8 cores, 32 GB memory (Studio + Wine is the heavy part; 2-player playtests
  need ~14 GB on their own), 120 GB disk. *Recommended.* With 16 GB, multiplayer rows stay MANUAL
  (as on slot-1 today).
- **D-L5. Keyring in an LXC (no desktop):** a file keyring encrypted with a passphrase you enter
  once per boot on the client's page. Or a plain file, mode 600, under the `foundry` user. The
  passphrase is like the router's secret store: safer, but the workstation waits for you after
  every reboot. *Recommended:* the mode-600 file. The machine is yours, on your LAN, and the keys
  it holds are revocable client keys, not your provider keys.
- **D-L6. The Windows PC afterwards:** kept as a standby workstation (its client installed but its
  coordinator switched off), or removed. *Recommended:* standby until the Linux one has run a week
  of sprints.

## Sizes and risks

- **Effort:** W1 small; W2 large (it touches the Studio gate path that every sprint uses, so it
  ships behind `studio.main_slot`, off by default, and is tested on a track first); W3 and W4
  medium; W5 small. Five releases.
- **Risk: Studio under Wine.** It works on slot-1, but Roblox updates can break Vinegar.
  - Mitigation: the image pins its Vinegar version.
  - The Windows PC stays as standby (D-L6) until the Linux workstation has proven itself.
- **Risk: two coordinators.** W4's standby switch and its router-side one-active-workstation
  check keep the old and new workstation from both running sprints.
- **Not changing:** the router, the slots and the models. The Lead's repair worktrees just move
  with the state.

## Order of work

W1 → W2 (tested first on a track, then for main) → W3 → W4 → W5. The rule from
docs/CONVENTIONS.md applies: each is a release, deployed only when no Lead repair runs.
