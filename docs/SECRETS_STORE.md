# Central secret store (Access → Secrets)

Since v0.159.0. The router keeps the keys and passwords that every game's Studio slot containers
(and other Foundry machines) need. Each machine pulls its own bundle with its own client key. The
same pull also brings the slot's non-secret settings (place, universe, never-sync places), which
are edited on the Automation settings page.

Design and decisions: Victor, 2026-10-31.
- D1: the master key comes from a passphrase.
- D2: HTTP and HTTPS are both available, and you pick in the UI.
- D3: the store always works, and the admin key stays optional.
- D4: new containers pull by default; the workstation keeps its key file for now.
- D5: no automatic migration.

## What is stored, and where

| What | Where |
|---|---|
| secret values | `<data>/secrets.sqlite` on the router (its own file, not the main DB): AES-256-GCM, one row per `group/name`; associated data `group|name|key_id`, so a value copied to another row does not decrypt |
| the master key | **nowhere**: derived from your passphrase with scrypt at unlock. Only a salt and a check value are stored |
| who pulled what | the same file, table `secret_pulls`: time, client, address, user agent, HTTP/HTTPS, project, slot, names, result. Never a value. Kept `secrets.pull_log_days` (180) |
| slot settings | the project's automation settings, `[slots.<id>]` (Automation → Settings → Studio slots), deployed as a revision |
| transport settings | `config.yaml` → `secrets:` (no values) |

Values never go in `config.yaml`, its history, export bundles, logs, the image or an environment
variable. Diagnostic text is scrubbed of every stored value while the store is unlocked
(`configops.secret_values`).

## Groups

- `shared` is readable by every key with the secrets scope.
- Each game (project id, e.g. `animaluprising`) has its own group. A key reads it only when the
  project is in the key's `projects`.

A key with the secrets scope and **no** projects reads `shared` only. This is stricter than the
other scopes, where no projects means every project.

## The passphrase (lock and unlock)

- **First use:** Access → Secrets → set a passphrase (12+ characters).
- **After every router restart** the store is **locked**:
  - slots keep the files they already pulled;
  - new pulls answer 503;
  - a control-center alert ("Secret store is locked") is raised and pushed as a user-action alert.

  Unlock it on the same page.
- **Lock now** drops the key from memory.
- **Change passphrase** re-encrypts every value in one transaction.
- **A lost passphrase** means every value has to be set again: delete `secrets.sqlite` and start over.
- **Wrong passphrases:** after 5 in a row, each further try waits 30 s.

## Values (write-only)

- **Add** a secret: group, name (UPPER_SNAKE_CASE), the file name it gets in the slot (default
  `<name lower case>.txt`) and a description.
- **Set / rotate:** type or paste the value, or pick a file. The browser sends the file's text
  straight to the router. A trailing newline is dropped. 16 KB at most.
- **Clear** removes the value, and slots delete the file at their next pull. **Delete** removes the
  entry.
- No page or API route ever returns a value. The admin API lists metadata only: set or not, set
  when and by whom, last pulled when, by whom and from where, and which keys can read it.

## Keys (Access → Clients & Keys)

Give each container or machine its own client key with the **secrets** scope (preset **slot**).
Revoking a machine means disabling or deleting its key.

| Key field | Meaning for the secrets scope |
|---|---|
| `projects` | the games whose group it reads, besides `shared` |
| `slots` | only these slot ids may be asked for (empty = any) |
| `secret_names` | only these names (empty = every secret in its groups) |

A slot's own settings can narrow its bundle further with `slots.<id>.secrets`, a list of the names
that slot needs.

**Admin key.** With `client_auth.protect_admin` off (the default), anyone who can reach the router
can create a secrets key and pull everything. The Secrets page warns about this, and these raise
control-center alerts (`secrets.alert_on_new_access`, on by default):
- a key being created with, or given, the secrets scope;
- a key's first pull.

Turn on "protect the web UI" to require your admin key.

## Pull API

    GET /v1/foundry/secrets/bundle?project=<id>&slot=<id>
    Authorization: Bearer <slot key>

The response:

    {"project", "slot", "transport",
     "secrets": [{"name", "file", "value", "set_at"}],
     "config": {<slots.<slot> from the deployed revision>},
     "etag"}

- `Cache-Control: no-store` on every answer.
- `If-None-Match: <etag>` answers 304 when nothing changed, so a slot can pull before every gate run
  for almost nothing.
- 401: no key, or an unknown or disabled key.
- 403: wrong scope, a project or slot outside the key, or plain HTTP while the router is HTTPS-only.
- 503: the store is locked.

Every answer, refusals included, is in the pull log.

## Transport: HTTP or HTTPS (your choice, Access → Secrets → Transport)

- **allow plain HTTP for secrets** (default on): pulls, setting a value and the passphrase also work
  on the normal port (11435).
- **HTTPS listener** (default off): a second port (`https_port`, default 11436) that serves the same
  app over TLS.
  - The router makes a self-signed certificate once (`<data>/tls/`, key mode 600), and the page
    shows its SHA-256 fingerprint.
  - Clients pin that fingerprint (`FOUNDRY_TLS_FINGERPRINT`); there is no CA.
  - **Restart the router** after turning it on or changing the port.
  - The router's `docker-compose.yml` publishes the port (`FOUNDRY_TLS_PORT`, default 11436).
- With HTTP off, every secret value travels only over HTTPS. Open the Secrets page itself on
  `https://<router>:11436/ui` to set values or unlock. Plain HTTP can't be turned off until the
  HTTPS listener is on.

## Slots (contrib/studio-slot)

`slot-secrets` (in the image) runs at container start and from `slot-checkout` before every gate
run:

| Env (not secrets) | |
|---|---|
| `FOUNDRY_URL` | `http://<router>:11435` or `https://<router>:11436` |
| `FOUNDRY_TLS_FINGERPRINT` | required with https; a mismatch stops before the request is sent |
| `FOUNDRY_PROJECT`, `FOUNDRY_SLOT` | what to ask for |
| `FOUNDRY_SLOT_KEY_FILE` | the slot's key, a read-only mounted file (`/run/slot-key/slot-key.txt`) |
| `FOUNDRY_SECRETS_DIR` | `/run/secrets`, a **tmpfs**. It refuses to write to a disk-backed folder |

- **Files:** each value goes to `/run/secrets/<file>` (mode 600, written to a temp file then
  renamed). The settings go to `~/.config/foundry-slot/slot.env` (`STUDIO_OPEN`,
  `ROJO_NEVER_PLACE_IDS`), which `entrypoint.sh` and `studio-reopen` read over the compose values.
- **What a pull does:**

  | Situation | Result |
  |---|---|
  | router unreachable, or the store locked | the last files stay |
  | 401 or 403 (key revoked, or HTTPS-only) | the files are deleted |
  | a secret no longer served | its file is removed |

- **Never:** a value on a command line, in the environment or on stdout; the key travels only in
  the Authorization header.

AU needs no change: `AU_ROBLOX_KEY_FILE=/run/secrets/roblox_open_cloud_key.txt` is the file the
`ROBLOX_OPEN_CLOUD_KEY` secret (group `animaluprising`) lands in.

## Moving the Open Cloud key (D5: by hand)

1. Access → Secrets: set the passphrase.
2. Add `ROBLOX_OPEN_CLOUD_KEY` to `animaluprising` (file `roblox_open_cloud_key.txt`), then use
   Set / rotate → pick `C:\Roblox\_secrets\roblox_open_cloud_key.txt`.
3. Access → Clients & Keys:
   - preset **slot**, name e.g. `slot-1`;
   - only projects `animaluprising`, only slots `slot-1`;
   - copy the key it shows once into `contrib/studio-slot/secrets/slot-key.txt` on this PC
     (git-ignored), or `/srv/studio-slot/slot-key/slot-key.txt` on a Proxmox slot.
4. Recreate the container (`docker compose up -d --build`), then check the result:
   `docker exec studio-slot-poc slot-secrets --status`.
5. On a Proxmox slot, delete the old `/srv/studio-slot/secrets/roblox_open_cloud_key.txt`.

The workstation keeps `C:\Roblox\_secrets\roblox_open_cloud_key.txt` for its own tools for now (D4).
