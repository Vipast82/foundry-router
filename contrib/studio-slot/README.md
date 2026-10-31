# Studio slot

One container = one Roblox Studio (Vinegar + Wine) + Rojo + Blender + the AU toolchain + Foundry's
gate runner, on a headless Linux desktop you open in a browser. It can run a sprint's whole gate
(host half + scripted Studio calls + `-Finish`) on its own place, beside the workstation's Studio.
Target: one LXC per Proxmox mini PC (Intel iGPU, no passthrough).

**Status (2026-10-31):** everything below is built and tested on this PC (Docker Desktop). The
coordinator does not yet send work to a slot (that is the next Foundry step: a second Studio
resource and a dispatch to the slot).

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
   Rojo connection. Auto-recovery is off (a slot's content comes from Rojo).
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

## On a Proxmox LXC (mini PCs)

- Unprivileged LXC with Docker inside (or the image's contents installed directly).
- Map the Intel iGPU: `lxc.cgroup2.devices.allow: c 226:* rwm` and
  `lxc.mount.entry: /dev/dri dev/dri none bind,optional,create=dir`, then in compose add
  `devices: ["/dev/dri:/dev/dri"]`.
- Replace the two Windows mounts: clone the AU repo (the slot's `origin`) and a Foundry release
  tag instead of mounting the workstation's folders.
- noVNC must get a password (or a VPN) before it leaves localhost.
- Vinegar's Graphics Mode: DXVK (Vulkan) on Intel Gen9+ iGPUs; WineD3D if DXVK misbehaves.

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
| `studio-auth-handler.sh`, `studio-auth.desktop` | the browser sign-in hand-back |
| `openbox-studio.xml` | window rules (playtest windows stay small) |
| `vinegar-config.toml` | Vinegar settings that work in a container |
| `mcp-smoke.js` | MCP smoke test (`STEPS`, `PLAYTEST=1`, `MCP_CMD`, images saved to `OUT_DIR`) |
