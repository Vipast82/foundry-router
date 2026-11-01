# Installing a Studio slot (Proxmox): one code, one command

Since v0.160.0. A **Studio slot** is a container that runs Roblox Studio, Rojo and Foundry's gate
runner on another machine, so track sprints are gated there instead of on your PC. You add a slot
in the router, run one installer, and sign Studio in once. Nothing else is edited by hand.

## Once: let Foundry reach slots

Foundry drives slots over ssh from the PC that runs the coordinator.

1. On that PC, check for a key:

       type %USERPROFILE%\.ssh\id_ed25519.pub

   If there is none, make one. Press Enter at the passphrase prompts; the coordinator runs
   unattended, and the key only opens the `studio` user on slot machines:

       ssh-keygen -t ed25519 -f %USERPROFILE%\.ssh\id_ed25519 -C foundry-slots

2. Router → **Access → Machines → Workstation ssh keys**: paste the contents of the `.pub` file
   (the PUBLIC key) and save. Every slot installer authorizes the keys listed there.

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
| router address | `http://192.168.0.112:11435` |
| the enrollment code | typing is hidden |

It then:
- downloads the Debian 13 template if needed;
- creates the LXC (unprivileged, Docker-ready, starts at boot);
- runs the slot installer inside it.

*Or: a machine you made yourself* (a Debian 13 LXC with `nesting=1,keyctl=1`, or a VM). As root
inside it:

    apt-get update -qq && apt-get install -y -qq curl
    curl -fsSLO https://raw.githubusercontent.com/Vipast82/foundry-router/<release>/contrib/studio-slot/proxmox/setup-host.sh
    bash setup-host.sh

It asks for the router address and the code.

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
| change its VNC (on the LAN, port) | Access → Machines → **VNC**, then reinstall (New code) |
| reinstall or rebuild | **New code**, then run `setup-host.sh` again in the slot (keeps what exists, gives it a fresh key) |
| revoke a slot (stolen, retired) | **Revoke**: its key stops working now, and the slot deletes its secret files at its next pull |
| remove a slot | **Delete**: removes its key and its settings; the LXC itself is untouched (`pct destroy <id>` on Proxmox) |
| check from the slot | `docker exec studio-slot slot-secrets --status`, `docker logs studio-slot` |

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
| build fails | `/srv/studio-slot/build.log`; re-run the installer with a new code after fixing |
| "no workstation ssh key" | add it on Access → Machines, then reinstall with a new code (or append it to `/home/studio/.ssh/authorized_keys`) |
| gates never go to the slot | Access → Machines shows "not ready yet": switch it on under Automation → Settings → Studio slots; Foundry also needs `[config_pull]` on (the default) |
