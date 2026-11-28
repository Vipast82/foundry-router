# Installing a Studio slot (Proxmox): one code, one command

Since v0.160.0. A **Studio slot** is a container that runs Roblox Studio, Rojo and Foundry's gate
runner on another machine, so track sprints are gated there instead of on your PC. You add a slot
in the router, run one installer, and sign Studio in once. Nothing else is edited by hand.

## How Foundry reaches slots (automatic since v0.162.0)

Foundry drives slots over ssh from the PC that runs the coordinator, your **main PC**. Nothing
needs pasting.

- **The main PC registers itself.** When its coordinator starts (and every 10 minutes), it:
  - makes Foundry's own key, `~/.foundry-coordinator/ssh/id_ed25519`, if there is none;
  - sends the PUBLIC half to the router. Access → Machines → **Workstations** lists it.

  A new main PC only has to start its coordinator.
- **Slots allow every registered PC.** The slot installer, then a timer on the slot host
  (`foundry-slot-keys.timer`, every 5 minutes), writes the registered keys into a Foundry block
  of `/home/studio/.ssh/authorized_keys`. **Remove** on Workstations takes a PC out within 5
  minutes. Keys you add by hand outside that block stay.
- **Slots report their ssh host key.** The coordinator gets it back and checks each slot strictly
  against Foundry's own known_hosts (`~/.foundry-coordinator/ssh/known_hosts`). A reinstalled slot
  or a reused IP updates by itself; anything else is refused.
  - A slot installed before v0.162.0 keeps trusting on first use until it is reinstalled.
  - Machines shows it as "host key not reported".
- **Check it:** `python -m foundry_client status` has a "Slot ssh" line, and Machines shows each
  PC's key and each slot's host key fingerprint.
- **Extra keys (optional):** Access → Machines → Extra ssh keys, e.g. your own key for manual
  checks.

## Each slot

**1. Add it in the router: Access → Machines → Add a Studio slot**

| Field | What to enter |
|---|---|
| game | the project, e.g. `animaluprising` |
| slot id | e.g. `slot-2`. Tracks bind to it: `tracks start ... --slot slot-2` |
| IP | the static IP the LXC will get, e.g. `192.168.0.52` |
| its place | the slot's **own** place (a private experience, e.g. "Animal Uprising [SLOT-2]"): its Studio link, its games URL or the place id. Never your workstation's test place or another slot's place: one place can only be open in one Studio |
| never-sync places | your LIVE places, which Rojo must never sync into |

You get an **enrollment code** (`FNDR-XXXX-XXXX-XXXX`). It works once and expires after 30 minutes;
**New code** makes another. The slot starts switched **off**, and its installer switches it on
when its container is ready, so no gate goes to a half-built slot.

**2. Run the installer** (the Add dialog shows these commands for your release)

*Easiest: the Proxmox host makes the LXC too.* In Proxmox: Datacenter → your node → **Shell**:

    curl -fsSLO https://raw.githubusercontent.com/Vipast82/foundry-router/<release>/contrib/studio-slot/proxmox/create-slot-lxc.sh
    bash create-slot-lxc.sh

It asks the following; Enter accepts the default shown:

| Question | Default |
|---|---|
| container id | the next free one |
| name | `foundry-slot-<id>` |
| IP/prefix (the one you entered above) | `192.168.0.50/24` |
| gateway | read from the host |
| bridge | `vmbr0` |
| storage | the first one that holds containers |
| disk | 80 GB |
| memory | 16 GB |
| cores | 6 |
| Intel iGPU passthrough | no |
| router address | none: type it, e.g. `http://192.168.0.112:11435` |
| the enrollment code | typing is hidden |

It then:
- downloads the Debian 13 template if needed;
- creates the LXC (unprivileged, Docker-ready, starts at boot);
- runs the slot installer inside it.

*Or: a machine you made yourself* (a Debian 13 LXC with `nesting=1,keyctl=1`, or a VM). As root
inside it:

    apt-get update -qq && apt-get install -y -qq curl
    curl -fsSLO https://raw.githubusercontent.com/Vipast82/foundry-router/<release>/contrib/studio-slot/proxmox/setup-host.sh
    bash setup-host.sh --router http://<router>:11435

It asks for the code (and the router address, without `--router`).

**What the installer does**, printing ✓ per step:
1. Installs Docker, git and ssh, and creates the `studio` user.
2. Trades the code for this slot's key (saved to `/srv/studio-slot/slot-key/slot-key.txt`, mode
   600, never shown) and its settings.
3. Authorizes your workstation's ssh key.
4. Downloads Foundry at the router's version and makes the mirrors Foundry pushes to.
5. Writes `/srv/studio-slot/slot.env` (place, router, CPUs, memory), then builds and starts the
   container. This takes **20-40 min the first time**: `tail -f /srv/studio-slot/build.log`.
6. Waits until the container has pulled its secrets, then switches the slot on in the router.

**3. Sign Studio in once.** Open the slot's desktop from any PC on your LAN:

    http://<slot IP>:6080/

It goes straight to the Studio desktop, connected and scaled to the window. Sign in to Roblox in
the Studio window. It stays signed in across restarts and rebuilds.

## The slot's desktop (VNC)

Since v0.161.1, the desktop (noVNC) is on the LAN by default, at `http://<slot IP>:6080/`.

- **A VNC password is optional** (none by default). Without one, anyone on your LAN can use the
  Studio, which is signed in to your Roblox account. To add one:
  - on Access → Secrets, add `SLOT_VNC_PASSWORD` to the game's group (e.g. `animaluprising`),
    keeping its default file name `slot_vnc_password.txt`;
  - restart the slot's container (`docker restart studio-slot` in the slot); noVNC then asks for
    it.

  VNC uses only the first 8 characters. Access → Machines shows "password on" or "no password".
- **Change it:** Access → Machines → **VNC**, which sets "VNC on the LAN" and the port. Off means
  only an ssh tunnel reaches it:

      ssh -L 6080:localhost:6080 studio@<slot IP>    then http://localhost:6080/

  The slot host applies a change when its installer runs again: **New code**, then
  `bash setup-host.sh` in the slot.
- **Behind Nginx Proxy Manager:** add a proxy host to `http://<slot IP>:6080`.
  - Turn **Websockets Support** on.
  - For LAN only, add an Access List that allows your LAN and denies all others.
  - The root page already opens `vnc.html?autoconnect=1&resize=scale`, so it needs no custom
    location.

The slot is ready. On the first gate, Foundry:
- accepts the slot's ssh host key;
- pushes the track's commit and its Foundry;
- has the slot clone the game, install its toolchain and start Rojo.

## Looking after slots

| To | Do |
|---|---|
| see a slot | Access → Machines: state (waiting for its installer, not ready yet, connected), last secrets pull, events |
| change its place, never-sync places, or switch it off | Automation → Settings → Studio slots, then deploy |
| change its VNC (on the LAN, port) | Access → Machines → **Settings**, then reinstall (New code) |
| let Studio draw on the host's GPU | Proxmox → the LXC → Resources → Add → **Device Passthrough** `/dev/dri/renderD128`, mode `0666`, then restart the LXC and reinstall (New code). Machines shows "GPU in use". slot-1 on an Intel UHD 630: ~145% CPU instead of ~1300%. **Settings → Use the GPU** off = software drawing |
| limit its CPU | Access → Machines → **Settings** → CPU limit. With no GPU, Studio redraws in software on every core it gets, even when idle. The default (0) is automatic: **all cores but 2** while the GPU draws Studio (Studio needs ~1.5 cores then, and Blender on the slot gets the rest), otherwise a third of the host's cores; at least 4. It applies within 5 minutes; the renderer's threads follow at the next container start |
| a smaller desktop (less drawing) | Access → Machines → **Settings** → desktop size, e.g. `1920x1080`, then reinstall. Gate screenshots get that size |
| repair, upgrade or apply changed settings | run `setup-host.sh` again in the slot and press Enter at the code question (or `--repair`): **no code needed**. It reads the slot's settings again with its own key (`GET /v1/foundry/secrets/enrollment`), keeps the key, and leaves the Foundry checkout alone while the slot runs (the coordinator moves it before each gate) |
| reinstall with a fresh key | **New code**, then run `setup-host.sh` again with it (`--reenrol` asks for one on an enrolled slot); a running slot container is restarted with the new key |
| revoke a slot (stolen, retired) | **Revoke**: its key stops working now, and the slot deletes its secret files at its next pull |
| remove a slot | **Delete**: removes its key and its settings; the LXC itself is untouched (`pct destroy <id>` on Proxmox) |
| check from the slot | `docker exec studio-slot slot-secrets --status`, `docker logs studio-slot` |

## Lanes on a slot (Distributed Foundry, being built: phase b)

A slot can also hold **lanes**: the lane's worktree, its Kilo / OpenCode sessions and its
checks run on the slot, and the coordinator fetches the commits back
(`docs/DISTRIBUTED_FOUNDRY.md`). Everything is off until you turn on all of the following, in
this order. Only on a spare slot until the pilot (release b-4) has passed:

1. Access → Machines → the slot → **Lanes: on**. This makes the slot's two model keys
   (`<slot>-kilo`, `<slot>-opencode`) and needs the secret store unlocked.
2. Automation → Settings → Studio slots → the slot → **lanes** on.
3. **New code**, then run `setup-host.sh` again on the slot host. With lanes on, it makes
   `/srv/foundry-lanes/<project>.git` and adds `docker-compose.lanes.yml`: the lane agent,
   Kilo and OpenCode, on the slot host's 127.0.0.1 only.
4. Automation → Settings → the project → **remote_lanes** on.
5. `tracks start <project> <name> <sprints> --slot <slot>` makes the lane on the slot.

To check:
- `tracks status` shows `on <slot> (remote)` and `lane tunnel <slot>: up`;
- on the slot, `docker exec studio-slot tail ~/lane-agent.log`.

What a lane on a slot does (v0.181.270):
- **Runs on the slot:** its sessions (the slot's own Kilo / OpenCode), its Lune rows, its Studio
  gate and Studio MCP.
- **Here:** the coordinator reads it through a sparse read mirror, `<game>/.lanes/<slot>/<lane>`.
- **At merge time** it is brought home (`tracks bring-home` does the same by hand) and merges as a
  local track.
- **Not yet:** Blender on a lane, and Studio on a Windows slot.

Turn any switch off to go back: new lanes are then made on this machine as before.
Cut-over steps: `docs/plans/DISTRIBUTED_FOUNDRY_CUTOVER.md`.

## Several slots on one host

Add a second slot with the same IP on Access → Machines. The router gives it its own layout, so
the first slot changes nothing:

| | the host's first slot | each further slot |
|---|---|---|
| folder | `/srv/studio-slot` | `/srv/studio-slot/<slot>` (`foundry_host_dir` = `.../<slot>/foundry`) |
| container | `studio-slot` | `studio-<slot>` (its own compose project and studio-home volume) |
| noVNC port | 6080 | 6080 + n |
| lane ports (agent, Kilo, OpenCode) | 47811, 4096, 4097 | 47811 + n, 4096 + 2n, 4097 + 2n |
| lane folder | `/srv/foundry-lanes` | `/srv/foundry-lanes/<slot>` |
| key timer | `foundry-slot-keys.timer` | `foundry-slot-keys-<slot>.timer` |

`setup-host.sh` reads the folder from the slot's settings. With more than one slot on the host,
`--slot <id>` picks the one to repair. Every value can still be set by hand on Automation →
Settings → Studio slots.

## Another game on a slot

A slot of a project other than AnimalUprising keeps that game's mirror as
`/srv/studio-slot/<project>.git` (the container's checkout: `~/work/<project>`). AnimalUprising
keeps `AnimalUprising.git`, so the current slots change nothing. The AU LIVE place stays on Rojo's
never-sync list by default (the compose file's `ROJO_NEVER_PLACE_IDS` default); another game sets
its own never-sync places on Access → Machines.

## A Windows slot (draft)

`install\slot.ps1` is the Windows twin of `setup-host.sh` (decision Db5: run it only after the
Linux lane pilot has passed; it has not run on a real slot yet).

1. Access → Machines → **Add a Studio slot**, **OS: Windows**, and the **ssh user**: the Windows
   administrator account the installer runs as (the coordinator logs in as it). The router sets
   the slot's `os = windows` and `studio = native` (no container) and shows the PowerShell line.
2. As that administrator on the Windows machine:

   ```powershell
   irm https://raw.githubusercontent.com/Vipast82/foundry-router/v<release>/install/slot.ps1 -OutFile slot.ps1
   powershell -ExecutionPolicy Bypass -File slot.ps1 -Router http://<router>:11435
   ```

   An https router: `-Router https://<router>:11436 -Fingerprint <SHA-256>`.
3. It installs Git, Node 22 and Python 3.12 (winget, at `install\pins.env`'s versions where winget
   takes one), Kilo and OpenCode (npm, integrity checked); the OpenSSH server with Git for
   Windows' bash as its shell; the workstation keys in `administrators_authorized_keys` and the
   scheduled task **Foundry slot keys** that keeps them in step every 5 minutes (as SYSTEM:
   `install\slot-keys-win.py`, lines it manages end in `foundry-managed`, lines you added stay);
   the game copy and the lane store under `C:\Foundry`; the Foundry Client with `[lane]` on at
   sign-in; then `doctor --platform`.
4. Run it again without a code to repair / upgrade (`-Repair`); the Kilo and OpenCode server
   passwords and your `client.toml` edits are kept. `-Reenrol` asks for a code.
5. Install Roblox Studio and sign in once yourself. Studio on a Windows lane is not built yet.

## Moving a slot from this PC to Proxmox (slot-1)

1. Access → Machines → Add a Studio slot: `slot-1`, the LXC's IP, the **same** place
   (`116058790793121`). The local `[slots.slot-1]` in `project.toml` is overridden by the router's
   settings.
2. Stop the PC's container **before** the installer finishes: one place opens in one Studio only.
   No track may be running a gate in it.

       docker stop studio-slot-poc

3. Run `create-slot-lxc.sh` on the Proxmox host, then sign Studio in.
4. When Access → Machines shows slot-1 **connected**, remove the old container and its volume:
   `docker rm studio-slot-poc` and `docker volume rm studio-slot_studio-home`.

## Troubleshooting

| Message | Fix |
|---|---|
| "the router refused the code: …expired / already used" | Access → Machines → **New code** |
| "cannot reach the router" | from the slot, try `curl http://<router>:11435/health`; check the IP, gateway and DNS |
| "Docker does not start" | the LXC needs `features: nesting=1,keyctl=1` (create-slot-lxc.sh sets it) |
| build fails | `/srv/studio-slot/build.log`; re-run the installer after fixing (no code needed: press Enter) |
| "the router refused this slot's key" on a repair | the slot was revoked or deleted: **New code**, then run the installer with it |
| "no workstation ssh key" / "Permission denied (publickey)" | start the coordinator on your main PC (it registers its key), then wait up to 5 minutes or run `foundry-slot-keys` on the slot host |
| "Host key verification failed" | the slot's host key changed without it reporting the new one: run `foundry-slot-keys` on the slot host (or reinstall with a new code) |
| gates never go to the slot | Access → Machines shows "not ready yet": switch it on under Automation → Settings → Studio slots; Foundry also needs `[config_pull]` on (the default) |
