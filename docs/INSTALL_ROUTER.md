# Installing the Foundry router: one command, then the setup wizard

Since v0.161.0. The router is the one place Foundry is configured: backends, personas, budgets,
games, client keys, machines and secrets. This guide installs it on a new machine, or moves an
existing router there with one backup file.

## What you need

- A Linux machine on your LAN that stays on: a Proxmox LXC or VM, a NAS app host, a mini PC.
  Debian 13 or Ubuntu 24.04 is easiest, because the installer adds Docker itself. Any other Linux
  works once Docker (with compose v2) and git are installed.
- 2 CPU cores, 2 GB of memory and 10 GB of disk are enough. The models run on their own servers,
  not on the router.
- A Proxmox LXC needs `features: nesting=1,keyctl=1` for Docker.

## 1. Run the installer

As root on the new machine:

    curl -fsSLO https://raw.githubusercontent.com/Vipast82/foundry-router/main/install/router.sh
    bash router.sh

It asks the following; Enter accepts the default shown:

| Question | Default | Notes |
|---|---|---|
| install folder | `/opt/foundry-router` | `app/` (the release), `config/`, `data/` |
| web UI port | `11435` | |
| HTTPS listener for secrets | no | port 11436, a self-signed certificate. Turn it on later on Access → Secrets → Transport if you like |
| a backup to restore | none | a `.fbak` file, when you are moving a router (section 4) |

It then:
1. installs Docker and git if they are missing;
2. downloads the newest Foundry release (`--version X.Y.Z` picks another);
3. builds the image and starts the router;
4. prints the address, for example `http://192.168.0.112:11435/ui`.

## 2. The setup wizard

Open the address. A new router opens the **setup wizard** (`/ui/setup`); the full UI is one
click away, and every step can be skipped and done later in the full UI.

| Step | What you do | Also in the full UI |
|---|---|---|
| Start | **Set up a new router**, or **Restore from a backup** | Logs → Data |
| Protect the web UI (optional) | make your admin key (shown once: save it), then turn protection on | Access → Clients & Keys |
| Secret store | choose its passphrase (12+ characters). Write it down: after every router restart the store is locked until you enter it | Access → Secrets |
| Backends | remove the example servers; add yours (Meridian, CLIProxyAPI, llama.cpp, Ollama, any OpenAI-compatible server) with their address and key; each is tested when added | Backends → Backend pool |
| Personas | install the default persona set, then **Validate**: a few real round trips per persona | Routing → Personas |
| Budgets | switch budgets and their rules on or off | Routing → Guardrails |
| Game | the game's id, name, work branch and implementer persona | Automation |
| Machines | add Studio slots (docs/INSTALL_SLOTS.md) and see the workstation steps | Access → Machines |
| Finish | lists what is left; **Finish setup** stops the wizard opening by itself | the **Setup** button reopens it |

**Provider keys.** Keys the router itself uses for its backends (Meridian, CLIProxyAPI,
OpenRouter, LiteLLM) are typed in the Backends step or on Access → Secrets → Provider keys.
They are kept in `config/provider-keys.env` (mode 600), never shown again, and config.yaml only
refers to them as `${NAME}`. A key set in the host's `app/.env` wins over that file and can only
be changed there.

## 3. Backups

**Logs → Data → Backup & restore → Make a backup** downloads one file
(`foundry-router-<host>-<date>.fbak`) holding:
- config.yaml and provider-keys.env;
- the main database (personas, clients and their key hashes, machines, automation settings,
  history and logs);
- the secret store, whose values stay encrypted under the secret store passphrase;
- the HTTPS certificate, so slots that pinned it keep trusting the router.

Tick "also media and training files" to include those too (they can be large).

The file is encrypted (AES-256-GCM) with the **backup passphrase** you type when you make it.
Foundry never stores that passphrase, so keep it with the file, in your password manager. A
backup made by a newer router than the one restoring it is refused; upgrade first.

From the router host's shell (the passphrase is asked for, never put on the command line):

    cd /opt/foundry-router/app
    docker compose exec foundry-router python -m foundry_router.backup create /data/backups/manual.fbak

## 4. Moving the router to another machine

1. On the old router: **Logs → Data → Make a backup**. Copy the `.fbak` file to the new machine.
2. On the new machine: run the installer and give the backup's path at the "backup to restore"
   question (or `bash router.sh --restore /root/foundry-router-old.fbak`), then its backup
   passphrase. Or install a new router and choose **Restore from a backup** in its wizard.
3. Open the new router: everything is back. Unlock the secret store on Access → Secrets with the
   **old router's secret store passphrase**.
4. Point the machines at the new address if it changed:
   - the Foundry Client: Settings → router address;
   - Studio slots: `FOUNDRY_URL` in `/srv/studio-slot/slot.env`, then
     `docker compose up -d` in `/srv/studio-slot`;
   - chat clients (AnythingLLM, Open WebUI, Cline): their base URL.

   The easiest way to avoid this step is to give the new machine the old router's IP.
5. Stop the old router (`docker compose down` in its folder) once the new one works.

A restore keeps the files it replaced in `data/restore/previous/`, until the next restore.

## 5. Upgrades

    bash /opt/foundry-router/app/install/router.sh --upgrade

It fetches the newest release (or `--version X.Y.Z`), rebuilds and restarts; config and data are
kept. Make a backup first.

## Troubleshooting

| Message | Fix |
|---|---|
| "Docker does not start" | in a Proxmox LXC, set `features: nesting=1,keyctl=1` and restart the LXC |
| "the router did not answer within 3 minutes" | `docker logs foundry-router` |
| "wrong passphrase for this backup" | the BACKUP passphrase (typed when the backup was made), not the secret store's |
| "this backup is from router vX, newer than this router" | `router.sh --upgrade --version X`, then restore |
| the wizard does not open | it opens only on a new router; the **Setup** button in the full UI opens it any time |
| a restore went wrong | the replaced files are in `data/restore/previous/`: stop the router, copy them back, start it |
