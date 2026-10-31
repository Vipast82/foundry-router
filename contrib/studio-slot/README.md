# Studio slot

One container = one Roblox Studio (Vinegar + Wine) + Rojo + Blender + the AU toolchain + Foundry's
gate runner, on a headless Linux desktop you open in a browser. It can run a sprint's whole gate
(host half + scripted Studio calls + `-Finish`) on its own place, beside the workstation's Studio.
Target: one LXC per Proxmox mini PC (Intel iGPU, no passthrough).

**Status (2026-10-31):** everything below is built and tested on this PC (Docker Desktop). Since
Foundry v0.158.15 the coordinator sends work to a slot: a track started with
`tracks start <project> <name> <sprints> --slot slot-1` runs its scripted Studio gate inside the
slot (the slot's checkout is moved to the track's commit, outputs are copied back into the track's
worktree) and its sessions' Studio MCP tools reach the slot's Studio (`docker exec`). The slot is
configured on the router (Automation → Settings → Studio slots, `[slots.slot-1]`; a coordinator's
`project.toml` still works). First real sprint: AS-J05.

## Secrets and slot settings come from the router (since Foundry v0.159.0)

The slot pulls its secrets from the Foundry router's central store (Access → Secrets, see
`docs/SECRETS_STORE.md`) with **its own client key**. `slot-secrets` runs at container start and
from `slot-checkout` before every gate run. It writes each value to `/run/secrets/<file>`: a
**tmpfs** (memory only, mode 600, gone when the container stops), never the image, the environment
or a log.

- **Router away or store locked:** the last files stay.
- **Key disabled:** the files are deleted at the next pull, which is how you revoke a slot.
- **Settings:** the router's slot settings (place, universe, never-sync places) go to
  `~/.config/foundry-slot/slot.env` and win over the compose values.

Set up once:
1. On the router, Access → Clients & Keys:
   - preset **slot**;
   - only projects `animaluprising`, only slots `slot-1`;
   - copy the key it shows once into `contrib/studio-slot/secrets/slot-key.txt` (git-ignored; on a
     mini PC: `/srv/studio-slot/slot-key/slot-key.txt`, mode 600, owner 1000).
2. `FOUNDRY_URL` in `docker-compose.yml` points at the router (`http://192.168.0.112:11435`). With
   the router's HTTPS listener, use `https://...:11436` and set `FOUNDRY_TLS_FINGERPRINT` to the
   fingerprint the Secrets page shows.
3. `docker compose up -d --build`, then check:
   `docker exec studio-slot-poc slot-secrets --status` (names and dates only).

Roblox Open Cloud (Creator API): asset sprints upload through AU's `tools/assets/roblox_cloud.py`,
the only reader of the key. It reads `AU_ROBLOX_KEY_FILE=/run/secrets/roblox_open_cloud_key.txt`,
the file the router's `ROBLOX_OPEN_CLOUD_KEY` secret (group `animaluprising`) lands in. Check it
without touching the key:

    docker exec studio-slot-poc bash -lc 'cd ~/work/AnimalUprising && python3 tools/assets/roblox_cloud.py selftest && python3 tools/assets/roblox_cloud.py asset --id <an uploaded asset id>'

## Run on this PC (Docker Desktop)

    cd contrib/studio-slot
    docker compose up -d --build
    # desktop:  http://localhost:6080/vnc.html?autoconnect=1&resize=scale

The `studio-home` volume keeps the Wine prefix, the Studio install, your Studio sign-in and the
slot's AU checkout across restarts. Resources: 8 CPUs, 12 GB (a 2-player playtest runs 4 Studio
processes rendering in software; 8 GB ran out of memory).

## What is in the image

| Piece | Version / note |
|---|---|
| Desktop | TigerVNC Xvnc 2560x1440 + openbox + noVNC on 127.0.0.1:6080 |
| Studio | Vinegar 1.9.4 (built from its checksum-verified tarball), Debian Wine 10, `renderer = "D3D11"`, WebView2 `--no-sandbox` |
| Rojo | 7.7.1 CLI + the matching Studio plugin (GitHub release) |
| Studio MCP | Roblox's `StudioMCP.exe` under Wine (`studio-mcp-official`) and `@chrrxs/robloxstudio-mcp@3.4.2` on Node 22.21 (+ its `MCPPlugin.rbxmx`) |
| Gate runner | Python 3.13 + Pillow; Foundry mounted read-only at `/opt/foundry` (the deployed release) |
| AU toolchain | PowerShell 7.6.9 (`tools/gate.ps1`), rokit 1.2.0 → lune, selene, stylua, wally from AU's `rokit.toml` |
| Blender | 4.5.12 LTS (same build as the build PC, hash 84afd5f785f7), numpy, ffmpeg |
| Helpers | `slot-checkout`, `studio-reopen`, `xdotool`, ImageMagick |

All downloads are pinned to a version and checked against the publisher's SHA-256.

## Signing in to Roblox (once per slot)

Studio shows the Roblox login page (WebView2) in the browser desktop; sign in there. If the
browser sign-in hangs on "Redirecting", the `roblox-studio-auth` link handler
(`studio-auth-handler`) stops the running Studio and starts it with the code (the
Studio-to-Studio hand-off hangs under Wine). Fallback: turn off **Web Pages** in Vinegar's settings
and sign in through Firefox. Vinegar keeps the sign-in across restarts.

## Each slot needs its own place

Roblox allows one edit session per place per account: a slot that opens the workstation's TEST
place kicks the workstation's Studio out of it (this stopped three sprints on 2026-10-30). Every
slot opens its own private place (`STUDIO_OPEN` in docker-compose.yml):

| Slot | Place | placeId | universeId |
|---|---|---|---|
| 1 | Animal Uprising [SLOT-1] (private, no Team Create) | 116058790793121 | 10773901724 |

SLOT-1 holds a copy of the TEST place's content (map, terrain, non-Rojo scripts: 116
ServerScriptService children, as TEST), saved over it from a TEST snapshot on 2026-10-30. A new
slot's place: Studio start page → New Experience → File → Save to Roblox As → Create new experience,
then the same TEST copy. Keep "[TEST]" out of the name (the Foundry Client finds the
workstation's TEST window by that name).

## What starts with the container (`entrypoint.sh`)

1. The desktop (Xvnc, openbox, noVNC).
2. The slot's own AU checkout `~/work/AnimalUprising`, cloned once from the read-only mount of the
   workstation's checkout (`/work/AnimalUprising`, its `origin`) and fetched at every start.
3. AU's toolchain (`rokit install`) and wally packages (`Packages/` is not committed).
4. **Rojo 7.7.1**, supervised (restarted if it exits), serving the slot's checkout on 34872.
5. Studio plugin settings, written while Studio is closed: Rojo **Auto Reconnect** +
   **sync without asking**, Studio's MCP server on; LIVE (`ROJO_NEVER_PLACE_IDS`) loses any saved
   Rojo connection. The slot's own place gets a saved Rojo endpoint (`rojo-seed`: the `STUDIO_OPEN`
   place id + the project's name) — Auto Reconnect only reconnects a place that has one, and without
   it a reopened Studio stayed unsynced (2026-10-31). Auto-recovery is off (a slot's content comes
   from Rojo).
6. Studio opening SLOT-1 full screen (the editor window only; playtest windows stay small).
7. A **watchdog**: if Studio is gone for a minute, `studio-reopen` opens it again.

Rojo connects and syncs by itself on every open: no Connect/Accept clicks.

## Running a gate in the slot

```bash
# put the slot's checkout (and Studio) on the commit to test
docker exec studio-slot-poc slot-checkout <sha|branch>
# the coordinator's pipeline: gate.ps1 host half -> studio_gate calls -> gate.ps1 -Finish
docker exec -w /home/studio/work/AnimalUprising studio-slot-poc bash -lc \
  'export PATH=$HOME/.rokit/bin:$PATH PYTHONPATH=/opt/foundry;
   python3 -m foundry_coordinator.studio_calls pipeline --repo ~/work/AnimalUprising \
     --sprint GF-SEC3 --run-id slot-test --suites GF_SEC3 --regression none --tier A'
```

- `slot-checkout` fetches, checks out detached, reinstalls wally packages when `wally.lock`
  changes, and when the move adds files or touches `default.project.json` restarts Rojo and
  reopens Studio (Rojo 7.7 misses new files, rojo-rbx/rojo#1300). Measured: 20 new files → ready in
  53 s.
- The gate reaches the slot's MCP servers through `FOUNDRY_MCP_OFFICIAL` /
  `FOUNDRY_MCP_COMMUNITY` (set in docker-compose.yml); AU's `studio.lock` lives in `AU_LOCK_DIR`.
- `studio_gate doctor` (same environment, `... studio_gate --repo ~/work/AnimalUprising doctor`)
  must say `DOCTOR READY` (needs Foundry v0.158.4+: Roblox's MCP waits for Studio to connect).

### Verified 2026-10-31

| Check | Result |
|---|---|
| `studio_gate doctor` | READY: both MCP servers, Studio, show_dev_gui, http, story harness, GateRunner, devices (incl. AU_390x844 / AU_844x390), Rojo, Lune |
| Host half (`gate.ps1` under PowerShell 7 on Linux) | PASS: clean tree, crlf, hash expect (845 files), HEAD, index suites, Lune 1654/1654, negative control 319/319, NEXT |
| Scripted Studio half, GF-SEC3 suite | 14/15 at master and at 834b130a; the one red is timing (below) |
| Solo + 2-player playtests, server/client eval | PASS (multiplayer: 6.9 of 12 GB) |
| Screenshots | 2092x1224 viewport PNG; Device Simulator iPhone 13 ActualResolution = exactly 390x844 / 844x390 |
| Blender | `build_bs_king.py --selftest` in `blender -b` → `BUILD_BS_KING SELFTEST PASS` |

**Known difference:** `GF_SEC3_check` leg SEC3.0b expects the match still in Lobby/Briefing when
its first call runs. The slot renders in software and reaches that call later, by which time
autodrive is in Combat (`phase=Combat`); on the workstation it passes. The check should force
the Lobby phase itself rather than depend on speed (an AU test fix, not a slot fix).

## Blender

`blender -b --python tools/blender/<builder>.py -- <args>` from the slot's checkout, the same
commands the asset plans use on the workstation (with `blender` instead of the Windows path).

## On a Proxmox LXC (mini PCs): `proxmox/`

The workstation's Foundry drives a slot on another machine over **ssh** (`foundry_coordinator/slots.py`,
a slot with `host`): every container command is `ssh <host> docker exec ...`, the track's Kilo
Studio MCP servers are stdio over ssh, and nothing from the workstation is mounted. Before each
gate run Foundry pushes the track's commit to a bare AU mirror on the host (`slot/<task>`, the
slot's `origin`) and the deployed Foundry to a Foundry mirror whose checkout is the slot's
`/opt/foundry`.

0. **Workstation ssh key** (once): Foundry connects with key auth only. If
   `%USERPROFILE%\.ssh\id_ed25519.pub` does not exist, make one with
   `ssh-keygen -t ed25519 -f %USERPROFILE%\.ssh\id_ed25519 -C foundry-slots`. With no passphrase
   the unattended coordinator can use it; the key only opens the `studio` user on slot hosts.
1. **LXC** (Proxmox host): Debian 13 template, unprivileged, a static LAN IP, a disk of 80 GB or
   more, and the lines in `proxmox/lxc.conf.example` (nesting + keyctl, 16 GB, 6 cores; the Intel
   iGPU lines are optional; storage notes there).
2. **Inside the LXC** as root (Proxmox console): fetch `setup-host.sh` from the release
   (`curl -fsSLO https://raw.githubusercontent.com/Vipast82/foundry-router/<tag>/contrib/studio-slot/proxmox/setup-host.sh`),
   then `bash setup-host.sh "<workstation ssh public key>"`: Docker + compose,
   the `studio` user (uid 1000, the image's), `/srv/studio-slot/{AnimalUprising.git, foundry.git,
   foundry, slot-key}`. It prints the remaining steps:
3. the first ssh from the workstation accepts the LXC's host key
   (`ssh -o StrictHostKeyChecking=accept-new studio@<lxc> docker ps`; Foundry's own ssh uses
   BatchMode and refuses unknown hosts), then seed the two mirrors (`git push` over ssh) and check
   out Foundry on the host;
4. `proxmox/.env`:
   - the slot's **own place** (one edit session per place per account: never the workstation's
     TEST place or another slot's place);
   - `FOUNDRY_URL` and `FOUNDRY_SLOT`;
5. the slot's **key** (Access → Clients & Keys, preset slot): **you** copy it to
   `/srv/studio-slot/slot-key/slot-key.txt` (mode 600, owner 1000). The Open Cloud key stays on the
   router;
6. `docker compose up -d --build` in `proxmox/`, then sign Studio in once through an ssh tunnel
   (`ssh -L 6080:localhost:6080 studio@<lxc>`; noVNC never leaves the LXC's localhost);
7. the router: Automation → Settings → Studio slots, add `slot-2` and deploy (or in
   `project.toml`):

   ```toml
   [slots.slot-2]
   container = "studio-slot"
   host = "studio@<lxc>"
   au_push_url = "ssh://studio@<lxc>/srv/studio-slot/AnimalUprising.git"
   foundry_push_url = "ssh://studio@<lxc>/srv/studio-slot/foundry.git"
   foundry_host_dir = "/srv/studio-slot/foundry"
   enabled = true
   ```

   then `tracks start ... --slot slot-2`.

Memory: Studio + a solo playtest fits 12 GB in software rendering; 2-player rows need about 14+ GB
(`SLOT_MULTIPLAYER=1` in `.env` only then). Vinegar's Graphics Mode: DXVK (Vulkan) on Intel Gen9+
iGPUs once `/dev/dri` is mapped; WineD3D if DXVK misbehaves.

## Files

| File | What it does |
|---|---|
| `Dockerfile` | the image (all pinned + checksum-verified downloads) |
| `docker-compose.yml` | the slot on this PC: ports, mounts, place, MCP/lock env, limits |
| `entrypoint.sh` | start-up (above) |
| `slot-checkout.sh` | move the slot to a commit and get Studio onto it |
| `studio-reopen.sh` | close and reopen the slot's Studio cleanly |
| `studio-mcp-official.sh` | Roblox's StudioMCP.exe under Wine (newest Studio version) |
| `studio-plugin-settings.js` | Rojo auto-sync + Studio MCP plugin settings |
| `rojo-seed.sh` | the slot place's saved Rojo endpoint (`<placeId>=<project>`) for the settings |
| `slot-secrets.py` | pulls the slot's secrets + settings from the router into `/run/secrets` (tmpfs) |
| `proxmox/docker-compose.yml`, `proxmox/.env.example` | the slot on a Proxmox LXC (no Windows mounts) |
| `proxmox/setup-host.sh` | prepares the LXC (Docker, `studio` user, mirrors, slot-key dir) |
| `proxmox/lxc.conf.example` | the Proxmox container settings (nesting, keyctl, memory, iGPU) |
| `studio-auth-handler.sh`, `studio-auth.desktop` | the browser sign-in hand-back |
| `openbox-studio.xml` | window rules (playtest windows stay small) |
| `vinegar-config.toml` | Vinegar settings that work in a container |
| `mcp-smoke.js` | MCP smoke test (`STEPS`, `PLAYTEST=1`, `MCP_CMD`, images saved to `OUT_DIR`) |
