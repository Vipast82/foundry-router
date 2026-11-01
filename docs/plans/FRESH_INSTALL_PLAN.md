# Foundry from scratch: the easiest install for every machine (plan, 2026-10-31)

**Status:**
- Victor answered on 2026-10-31: the recommended answer for every decision (D1–D6 in §7).
- **Phase 1 is built (v0.160.0):** Access → Machines, enrollment codes, `create-slot-lxc.sh` and the
  one-shot `setup-host.sh` (docs/INSTALL_SLOTS.md).
- **Phase 2 is built (v0.161.0):** install/router.sh, the first-run setup wizard (/ui/setup),
  provider keys in provider-keys.env, and encrypted Backup & restore (docs/INSTALL_ROUTER.md).
  The game step takes id, name, branch and implementer; the game's clone URL moves to phase 3,
  where the workstation clones it.
- Phases 3–4 are next. D6 (where the workstation pieces live after the move) is decided later.

Victor wants two things:
- a one-shot, user-friendly slot installer that asks for whatever it needs, so the container
  starts up connected and ready;
- the simplest step-by-step setup for the whole system (backend, front end, client configs,
  containers), so it can move off his PC onto other machines later.

## 1. What exists today (and why a rebuild is hard)

| Machine | Pieces | How it is set up today (manual steps) |
|---|---|---|
| **Router host** (Docker, 192.168.0.112) | the Foundry router (API + web UI), config.yaml, the SQLite data, the secret store | clone the repo, `.env` with provider keys, copy and edit `config.example.yaml`, `docker compose up`. Then in the UI by hand: backends, default personas (install + validate), budgets, client keys |
| **Model backends** | Meridian (Claude subscription), CLIProxyAPI (ChatGPT), llama.cpp / Strata (local Qwen), Docker MCP gateway, two companions | each has its own README and install. Its URL and key are typed into Foundry |
| **Workstation** (Windows) | Foundry Client (tray), the Kilo server, the coordinator, the upgrade supervisor, Rojo, Studio, Blender, the game checkout, the dev and deploy checkouts of Foundry, a Python venv | the client installer exists. After it, about 12 manual steps:<br>• make about 4 router keys (coordinator, kilo, media, operator relay) and copy each into the client's Secrets;<br>• invent a Kilo password;<br>• merge the provider block into `kilo.jsonc`;<br>• copy `project.example.toml` → `project.toml` and edit the paths and URLs;<br>• clone two Foundry checkouts and pin the deploy one to a tag;<br>• rule amendment, `doctor`, autostart |
| **Studio slots** (Proxmox LXC) | Docker + the slot container | create the LXC, run setup-host.sh, push the mirrors from the workstation, `.env`, copy the slot key, build, sign Studio in, add `[slots]` to project.toml |
| **Chat clients** | AnythingLLM, Open WebUI, Cline | make a key, paste the URL and the key |

The common pain is that every machine needs keys copied by hand from the router UI, plus
config files edited by hand. Each of those is a place to make a mistake.

## 2. The target: "enroll a machine" with one code

One idea runs through every machine. **The router is the only place you configure anything.**
Each new machine is joined with **one short enrollment code**.

1. In the router UI, **Access → Machines → Add a machine**, pick what it is:
   - Workstation (coordinator + Kilo + Studio);
   - Studio slot;
   - Chat client;
   - Backend helper.

   Fill in the few settings it needs (for a slot: id, IP, its place). The router creates that
   machine's client keys (right scopes, nothing to remember) and shows a code like
   `FNDR-7K4Q-M2XD`.
   - The code works **once** and expires after 30 minutes.
   - It is the only thing you carry to the machine; the keys themselves are never shown.
2. On the new machine, run its installer. It asks for:
   - the router address (pre-filled when it can find it);
   - the enrollment code;
   - only the things the router can't know, such as a local folder.
3. The installer trades the code for that machine's keys and settings (over HTTPS when it's on),
   installs everything, starts it, and prints a green checklist. The router's Machines page
   shows the machine as connected.

Revoking a machine is one button: it disables that machine's keys.

## 3. Step by step after a full wipe (the target)

**A. Router host** (any Linux box with Docker; one command)

    curl -fsSL https://raw.githubusercontent.com/Vipast82/foundry-router/<tag>/install/router.sh | sudo bash

1. The script installs Docker if it's missing and creates `/opt/foundry-router/{config,data}` with
   the right owner.
2. It asks for the port (11435) and whether to turn on HTTPS, then starts the router.
3. It prints the UI address.

**B. First-run wizard in the web UI** (opens by itself while the router has no configuration)

1. **Protect the UI:** create your admin key, or skip; this is the optional D3 choice from the
   secrets work.
2. **Secret store passphrase.**
3. **Backends:** add Meridian, CLIProxyAPI and local llama.cpp, each with a **Test** button. Each
   card links to that backend's own one-line installer.
4. **Personas:** install the default sets and validate them (one button, with a progress list).
5. **Budgets:** the suggested rules, with a toggle each.
6. **Game:** name, git URL, default branch. This creates the project and its settings.
7. **Machines:** add your workstation and slots (§2), showing the enrollment codes.

**C. Workstation** (Windows or Linux)

1. Run `FoundryClientSetup-<ver>.exe` (or the AppImage).
2. The first-run wizard asks for:
   - the router address;
   - the enrollment code;
   - where the game should live (a folder);
   - whether to start at sign-in.
3. Then it does the rest:
   - stores the keys in the OS keyring, and makes and stores a random Kilo password;
   - installs or updates Kilo and writes `kilo.jsonc` (the existing `apply-kilo-config`);
   - clones the game, and clones the Foundry release that matches the router's version (no
     second dev checkout);
   - writes `project.toml` from the router's project settings plus the few local answers
     (paths, URLs);
   - starts the Kilo server, the coordinator, the supervisor and Rojo;
   - runs `doctor` and shows the result as a checklist.

**D. Studio slots** (Proxmox; per slot, about 5 minutes of your time plus the build)

1. Router UI → Add a machine → Studio slot: id (`slot-3`), IP, place link. You get a code.
2. On the Proxmox host shell:

       bash create-slot-lxc.sh

   It asks for the CT id, IP, router address and enrollment code. Then it creates the LXC
   (Debian 13, unprivileged, nesting/keyctl, 16 GB, 6 cores, 80 GB) and runs the slot installer
   inside it, which:
   - installs Docker;
   - trades the code for the slot key, the slot settings and the workstation's ssh public key;
   - downloads Foundry at the router's version;
   - writes `.env`;
   - builds and starts the container.
3. The one step left: **sign Studio in once** through the noVNC link it prints. That's your Roblox
   login; it carries over restarts and rebuilds.
4. Foundry uses the slot at the next gate. The coordinator:
   - accepts the slot's ssh host key on first contact;
   - seeds the game mirror;
   - takes the slot's settings from the router (no `project.toml` edit).

If you prefer the Proxmox UI, create the LXC there and run the same slot installer with the
one-line `curl` inside it. The questions are the same.

**E. Chat clients:** Add a machine → Chat client gives a page with the exact URL, model and a
copy button for that client's key. These programs have no installer we control, so this stays a
paste.

**F. Backend helpers** (Meridian, CLIProxyAPI, Strata, the companions): each keeps its one-line
installer, which ends by printing a URL + token. The first-run wizard's backend cards ask for
them and test them.

## 4. Moving off your PC (backup, move, restore)

- **Router:** `data/` (SQLite + `secrets.sqlite` + the TLS certificate) and `config/`. A
  **Backup** button (Logs → Data) makes one archive, and the router installer has
  `--restore <archive>`. The secret store moves with it and still needs the passphrase. That is
  intended: the archive alone can't decrypt anything.
- **Workstation:** the coordinator's state (`~/.foundry-coordinator`) and the game checkout (git).
  The client gets **Export / Import workstation**, so a new box can take over without losing the
  queue, history or memory.
- **Where the workstation pieces live afterwards** is a topology choice (D6).

## 5. The slot installer in detail (phase 1)

For a slot, the router needs to know:
- the slot id;
- the host IP;
- the place and universe (or "use the existing slot-1 place");
- optionally CPUs and memory.

The installer itself only asks for:
- the router URL;
- the enrollment code;
- confirmation of the detected IP;
- the size of the LXC (create-slot-lxc.sh only, with the defaults shown).

Router side:
- **Access → Machines:** add a slot. It writes `slots.<id>` (container, host, place) into the
  project settings as a new deployed revision, marked "added by machine enrollment"; makes the
  slot's client key (`secrets` scope, that project and that slot only); and mints a one-time
  code.
- `POST /v1/foundry/enroll {code}`: no key needed; the code is the credential. It is single-use,
  expires in 30 minutes, is rate-limited and audited. It returns:
  - the key;
  - the slot settings;
  - the router version;
  - the workstation ssh public key(s);
  - the HTTPS fingerprint, when HTTPS is on.
- **Slot host ssh keys** (Access → Machines): this PC's public key, sent to slot installers.
- **Derived defaults:** `au_push_url`, `foundry_push_url` and `foundry_host_dir` come from `host`
  when empty.

Coordinator:
- **ssh:** `StrictHostKeyChecking=accept-new` (trust on first use, LAN).
- **Mirror:** pushes `master` to an empty game mirror before the first gate.

Container:
- `slot-checkout` does the first clone, rokit and wally when the checkout is missing.
- Rojo waits until the project exists.
- An image built before the mirror had commits then becomes ready at its first gate, with no
  restart.

Installer output, at the end:
- the noVNC tunnel command for the one Studio sign-in;
- "slot-3 is connected" once its first secrets pull succeeds; the router's Machines page shows it
  too.

## 6. Phases (each a release)

| Phase | What | Size |
|---|---|---|
| 1 | Enrollment codes + Machines page + one-shot slot installer + `create-slot-lxc.sh` + coordinator/container auto-ready (§5). **Moves slot-1 to Proxmox with it.** | medium |
| 2 | Router installer script + first-run wizard + Backup/Restore | medium |
| 3 | Workstation enrollment in the Foundry Client wizard (keys, kilo.jsonc, game clone, release checkout, generated `project.toml`, doctor checklist) + Export/Import workstation | large |
| 4 | A single "Install from scratch" guide (docs/INSTALL.md), tested by following it on a clean VM | small |
| 5 | (depends on D6) the coordinator + Kilo on Linux, with all Studio work in slots | large |

## 7. Decisions for Victor

- **D1. Enrollment codes** (one short single-use code per machine) instead of copying keys by hand.
  *Recommended.* It removes the error-prone step on every machine.
- **D2. A slot is defined in the router UI first, then installed with its code.** *Recommended.*
  The installer then never asks for a key or place id. The alternative is that the installer asks
  for everything and registers the slot itself.
- **D3. Trust a new slot's ssh host key automatically on first contact** (LAN). *Recommended.*
- **D4. Model-provider credentials** (Meridian, OpenRouter, LiteLLM keys; today in the router's
  `.env`):
  - (a) stay in `.env`, with the router installer and wizard writing them for you;
  - (b) move into the secret store. Then **every router restart stops all model calls until you
    unlock it** with the passphrase.

  *Recommended: (a).*
- **D5. Start with phase 1 now** (it makes the slot-1 move easy), then phases 2–4.
- **D6. After moving off your PC, where do the coordinator, Kilo and the "main" Studio live?**
  - (a) Another Windows PC: same as today, the least work.
  - (b) A Linux VM/LXC on Proxmox, with all Studio work in slots (the main checkout too). This
    takes everything off your desktop, but main sprints currently use the workstation's own
    Studio, so it needs phase 5.
  - (c) Decide later.

  *Recommended: (c), with phases 1–4 working for both (a) and (b).*
