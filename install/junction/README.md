# The Foundry junction in a container (Distributed Foundry phase e)

The junction is the main workstation's role: the coordinator (queue, decisions, reviews, every
watch duty), the main checkout and its sprints, every merge and merge gate, and the only push to
`origin`. Today it is the Windows PC. This folder puts the same role in containers, so it can move
to a Linux machine or a Proxmox VM when you choose. Design: `docs/DISTRIBUTED_FOUNDRY.md` §2 and
§9. Nothing here changes the running system: a new junction starts on **standby**.

| Piece | What it is |
|---|---|
| `Dockerfile` | Debian 13 with `install/workstation.sh --tools-only`: git, Python, Node 22, Kilo 7.8.32, OpenCode 2.3.2, PowerShell 7.6.9, rokit, Blender 4.5.12, the docker CLI (for `studio-main`) |
| `junction-entrypoint.sh` | `install` (once), `run` (default: the Foundry Client headless), `doctor`, `shell` |
| `docker-compose.yml` | `junction`, plus `studio-main` (the slot image for main sprints, profile `studio`) |

## Install

```bash
cd install/junction
docker compose build                              # 20-40 min the first time
docker compose run --rm junction install          # the router address + a one-time code
docker compose up -d
docker compose exec junction /opt/foundry-install/junction-entrypoint.sh doctor
```

For `install`, make the one-time code first: router → Access → Machines → **Add a workstation**.
The keys go to a mode-600 file in the `junction-data` volume (`--secret-store file`, as decision
D-L5 chose for an LXC). The game and the Foundry release are cloned into the volume.

`install` ends with the Kilo role rules (the coordinator's `kilo-config` for each project here)
and `doctor --platform`. Run it again to repair or upgrade: an installed junction needs no code
(press Enter at the code question). Pass `--router http://<router>:11435` (no address is assumed).

**Studio for main sprints (optional):**
1. On Access → Machines, Add a Studio slot `studio-main` (this machine's IP, the TEST place): it
   shows a one-time code.
2. `docker compose run --rm junction studio-enrol --router http://<router>:11435` and give it the
   code: the slot's key, the router address (and an https router's fingerprint) go into the
   `junction-data` volume (`/srv/foundry/studio-main/`, mode 600, read by the Studio container);
   nothing is copied by hand.
3. Set `STUDIO_MAIN_OPEN` to the TEST place's `roblox-studio:` link.
4. Start it with `docker compose --profile studio up -d`, then sign Studio in once through noVNC
   (`ssh -L 6081:127.0.0.1:6081 ...`).
5. Turn main sprints over to it with `[studio] main_slot = "studio-main"` on the Automation page.

## Moving the work here

1. On the old workstation, at a safe point: `foundry-client workstation export --out work.fwx`
   (built in W4).
2. Here: `docker compose run --rm junction install --import /srv/foundry/work.fwx` (copy the file
   into the volume first).
3. On Access → Machines → Workstations, press **Make active** for this junction. The old one goes
   to standby within a minute.

Going back is the same move the other way. The Windows PC keeps working as a junction too:
`install/workstation.ps1`.
