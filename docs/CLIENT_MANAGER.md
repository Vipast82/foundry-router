# Foundry Client: installer, tray and stack manager

Status: design and phase 1, 2026-10-21.

The client replaces the hand-written workstation scripts (`foundry-stack.ps1`,
`start_kilo_server.ps1`, `start_media_adapter.ps1`, `start_coordinator.ps1`,
`deploy_release.ps1`, `start_rojo.ps1` and the `FoundryStack` / `FoundryRojo` logon tasks).
They become one cross-platform app that anyone can install. Victor chose (2026-10-21):

| Choice | Decision |
|---|---|
| Packaging | Standalone packages: a Windows setup `.exe` and a Linux AppImage, with Python bundled |
| Settings screen | A local web page on 127.0.0.1, opened from the tray, in the Foundry GUI style |
| Managed components | The Kilo server, the coordinator with its upgrade supervisor, and Rojo. The media adapter is not managed: Kilo starts it itself (stdio MCP); the Doctor checks its entry in `kilo.jsonc` |

## Pieces (package `foundry_client/`)

| Module | Job |
|---|---|
| `config.py` | Client settings in `client.toml`: `%APPDATA%\FoundryClient` on Windows, `~/.config/foundry-client` on Linux. Holds the backend URL, state dir, game workspace and one table per component. No secrets: keys stay in environment variables or the OS keyring. |
| `components.py` | One definition per component: its command, working dir, health check, safe-point rule and restart policy. |
| `manager.py` | A supervisor thread per component. It starts, health-checks and restarts with backoff, writes rotating logs, and records state for the UI. Restarts of the Kilo server and the coordinator wait for a safe point (no live session in the coordinator DB) unless forced. |
| `api.py` | Local HTTP API on `127.0.0.1` with a per-install token. Endpoints: status, start/stop/restart per component, settings get/put, log tail, deploy a coordinator release. It also serves the settings page. |
| `static/client.html` | The settings and status page: one card per component (state, uptime, port, last error, controls), settings forms, a log viewer, and links to the Foundry control center and Automation page. |
| `tray.py` | A tray icon (pystray) coloured green, amber or red for the stack's health. The menu opens settings, shows each component's state, restarts the Kilo server at a safe point, restarts everything, opens the control center and logs, and quits. On Linux it uses AppIndicator, and falls back to headless when no tray is available. |
| `autostart.py` | Start at logon. Windows uses a Startup-folder shortcut; Linux uses an XDG autostart `.desktop` file. A `systemd --user` unit covers headless use. |
| `__main__.py` | Entry point `foundry-client`: `run` (manager, API and tray), `run --headless`, `status`, `restart <component>`, `install-autostart` / `remove-autostart`. The same executable also runs `coordinator ...` and `supervisor ...`, so the bundle needs no separate Python. |

## Components

| Component | Command | Health | Restart rule |
|---|---|---|---|
| Kilo server | `kilo serve --port 4096` (path detected or set), cwd = game workspace | `GET /config` on its port, with the Kilo password (tells a closed port, a wrong password and a wedged server apart) | safe point |
| Coordinator | `foundry-client coordinator run --control ...` (bundled) | heartbeat file age | safe point (tick boundary) |
| Upgrade supervisor | `foundry-client supervisor --loop ...` (bundled) | process alive | any time |
| Rojo | `rojo serve <project> --port 34872` (path detected or set) | `GET /api/rojo` (session id) | any time. Studio's plugin must be reconnected by hand after a restart; the client notices the new session id and says so. |

Kilo and Rojo are external programs. They are detected or configured, not
bundled.

## Watchdog, requests and checks (workstream W5)

**Settings.** `client.unhealthy_restart_seconds` (default 300, 0 = never): a component that
stays unhealthy this long is restarted (at a safe point for Kilo and the coordinator).
`client.safe_restart_max_wait_minutes` (default 60): a safe-point restart that has waited this
long raises a warning on the status page; it is never forced. `client.update_check`,
`client.update_repo` (default `Vipast82/foundry-router`), `client.relay` (take stack requests
from the router, `relay.py`), `workspace.kilo_config` and `workspace.project` for the role-rules
button. `PUT /api/settings` merges onto the current settings, so a partial document changes only
what it names.

**Processes started outside the client** are adopted. They can be stopped and restarted by the
pid that owns their port (Kilo, Rojo) or the pid in the coordinator heartbeat. A restart that
could not stop the old process reports `failed: ...` and is not counted.

**Media adapter.** Removed as a component: it is a stdio MCP server that Kilo spawns itself. An
old `[components.media]` table is dropped from `client.toml` on load (the previous file is kept
as `client.toml.before-migration`). The Doctor checks that `kilo.jsonc` has a `foundry-media`
entry whose script exists and that `FOUNDRY_KEY` reaches Kilo (entry environment, the Kilo
component's secrets, the user environment, or the adapter's `foundry-media.json`). When none of
those has it but the entry runs a launcher script that loads the key itself (for example
`config/workstation/start_media_adapter.ps1`, which reads the DPAPI file
`~/.foundry-coordinator/media-credential.xml`), the Doctor runs the launcher's own check mode
(`-Check` for a PowerShell `[switch]$Check`, `--check` otherwise) and trusts its exit code: pass
is ok, fail is red, a timeout or a self-loading launcher with no check mode is a warning. The
launcher's output is never shown and the key is never read. With no key anywhere the row is red.

**Kilo configuration.** "Apply role rules to kilo.jsonc" on the status page (or
`foundry-client apply-kilo-config`) runs `foundry-client coordinator kilo-config <project>
--apply`: the coordinator's generator backs the file up, rewrites only the agent permission
blocks, reads it back and restores the backup on any mismatch. The client also refuses a
`kilo.jsonc` that does not parse. Afterwards Kilo is reloaded with a safe-point restart.

**Requests from other programs.** Drop a JSON file in `<state_dir>/client-requests/`:

```json
{"action": "reload_kilo", "id": "abc", "force": false}
```

Actions: `reload_kilo` (safe-point restart of Kilo), `restart_rojo`, `restart_coordinator`
(safe point), `apply_kilo_config` (`project`, `dry_run`, `reload`). The client moves the file to
`processing/`, writes the answer to `results/<same name>` (`status`: `pending` with the reason
while it waits for a safe point, then `done` or `failed`, plus `message`), and archives the
request in `done/`.

**Router link.** The status page and `GET /api/observation` show whether `client.backend_url`
answers (`/health`, else `/api/version`), its version and whether it is still warming up.
`Manager.observation()` is the secret-free summary the relay sends upstream. The router opens its
port before MCP servers and probes are ready (`/health` reports `warming_up`).

**Updates.** The client looks at the GitHub releases of `client.update_repo` at start and every
6 hours and shows a banner with a link. It never downloads or installs anything.

**Linux.** `foundry-client install-service` writes `~/.config/systemd/user/foundry-client.service`
(`run --headless`) and enables it; run `loginctl enable-linger $USER` once to start it at boot.
Inside an AppImage, start-at-logon uses the AppImage file (`$APPIMAGE`), not the temporary
mount path.

## Packaging (phase 2)

- **Windows:** PyInstaller one-dir build, then an Inno Setup installer `FoundryClientSetup-<ver>.exe`.
  It adds a Start menu entry, an optional start-at-logon, and an uninstaller. On first run the
  wizard opens the settings page.
- **Linux:** PyInstaller one-dir build, then `appimagetool` to build `FoundryClient-<ver>-x86_64.AppImage`.
  It ships a `.desktop` file and an icon.
- **CI:** a GitHub Actions workflow builds both on a `v*` tag (windows-latest and ubuntu-22.04)
  and attaches them to the release.

## Using it

### Install

- **Windows:** run `FoundryClientSetup-<ver>.exe` from the GitHub release. It installs per user
  and needs no admin rights. "Start Foundry Client when I sign in" is an optional tick box.
- **Linux:** download `FoundryClient-<ver>-x86_64.AppImage`, make it executable with `chmod +x`,
  then run it.
- **From source:** `pip install -e ".[client]"`, then `foundry-client run`.

### First run

The first run starts nothing and opens the settings page.

1. Set the Foundry router URL, the game folder, the coordinator profiles folder and, optionally,
   the release checkout for the upgrade supervisor.
2. Choose which components to run.
3. On the Secrets tab, enter the Kilo server password and the coordinator key. On Windows you can
   instead click **Import from the old PowerShell launcher files**, which reads the DPAPI files
   the old scripts saved.
4. Save. The enabled components start in order: Kilo, then the coordinator, then the supervisor.

### Tray

- **Icon colour:** green when everything runs, amber while something starts or waits for a safe
  point, red when something failed, grey when nothing is enabled.
- **Menu:**
  - open settings;
  - each component's state;
  - restart the Kilo server or the coordinator at a safe point;
  - restart everything now;
  - open the Foundry control center or the Automation page;
  - open the logs folder;
  - start at logon;
  - quit, which stops the stack.

### Commands

`foundry-client status`, `foundry-client restart kilo [--force] [--wait]`,
`foundry-client install-autostart`, `foundry-client import-legacy-secrets`,
`foundry-client doctor`, `foundry-client apply-kilo-config [--dry-run]`,
`foundry-client install-service` / `remove-service` (Linux `systemd --user`).

### Migrating this workstation

1. Run `foundry-stack.ps1 stop -All` and `foundry-stack.ps1 remove-autostart`, and disable the
   `FoundryRojo` task.
2. Install the client and import the secrets.
3. Enable the components.

Components that are already running are adopted, not started twice.

## Router relay (`relay.py`, v0.134.0)

The client reports the stack to the router and carries out the stack actions the operator chat
files there, so "restart Kilo" works from the Automation page even when the coordinator is down.

- **Heartbeat** every 15 s: `POST /v1/foundry/telemetry/heartbeat` with source kind `client`,
  feature `stack_control` and each component's state. The router shows the stack in the chat's
  context and alerts when the client goes silent.
- **Claim** every 3 s: `POST /v1/foundry/control/claim {source_kind: "client"}` returns the
  pending `client_restart {component, force}` and `client_reload_kilo_config` actions. The
  client applies them through its own manager (safe-point restarts stay safe-point restarts)
  and reports `applied` / `rejected` / `failed` with the resulting state. The router never
  reaches into the workstation and never sees the client's local API token.

### Router key for the relay

1. In Foundry, Access → Clients & keys, make a key with the **operator** scope (the same
   preset the control-center sign-in uses). The relay only ever heart-beats as a client and
   claims `client_*` actions with it; the router refuses anything else from an operator key on
   these routes.
2. On the workstation, open the client's Settings page → Secrets and store it as
   **FOUNDRY_CLIENT_KEY** (the keyring name `client.router_key` points at; change that setting
   to use another name). Headless: `FOUNDRY_CLIENT_SECRET_FOUNDRY_CLIENT_KEY=<key>` in the
   environment.
3. Restart the client. Without a key the client logs "relay: no router key" and runs without
   the relay; with the coordinator's `orchestration` key present it falls back to that.
4. `client.relay_enabled = false` in `client.toml` turns the relay off.

## Troubleshooting

The install guide on the router's Client tab and the operator chat's install help render
this section (and Install / First run / Router key / Tray / Commands / Migrating above)
straight from this file — fix it here, not there.

- **The client does not appear on the router's Client tab (no heartbeat).** The relay
  needs a router key with the **operator** scope (Foundry → Access → Clients & keys) stored
  as **FOUNDRY_CLIENT_KEY** on the client's Settings → Secrets page (headless:
  `FOUNDRY_CLIENT_SECRET_FOUNDRY_CLIENT_KEY=<key>` in the environment). Without one the
  client logs `relay: no router key` and runs without the relay. Also check that
  `client.backend_url` points at the router (the status page shows whether it answers) and
  that `client.relay_enabled` is not `false` in `client.toml`. Restart the client after
  adding the key; it heart-beats every 15 s, and the router marks it silent when heartbeats
  stop for longer than the source expiry window.
- **A key that heart-beats but is refused.** The relay only ever heart-beats as source kind
  `client` and claims `client_*` actions; a key with the wrong scope is refused on these
  routes. Make the key from the operator preset, not inference.
- **A restart that never happens.** Kilo server and coordinator restarts wait for a safe
  point (no live session in the coordinator DB). After
  `client.safe_restart_max_wait_minutes` (default 60) the status page shows a warning; the
  restart is never forced on its own. Force one deliberately with
  `foundry-client restart kilo --force` or the tray's "restart everything now".
- **Where logs live.** Each component writes a rotating log to
  `<client.state_dir>/client-logs/<component>.log` (state_dir defaults to
  `~/.foundry-coordinator`); the tray menu's "open the logs folder" opens it. The client's
  own settings live in `client.toml`: `%APPDATA%\FoundryClient` on Windows,
  `~/.config/foundry-client` on Linux.
- **Rojo restarted but Studio stopped syncing.** Studio's Rojo plugin must be reconnected
  by hand after a Rojo restart; the client notices the new session id and says so on the
  status page.
- **Version mismatch.** The router's Client tab flags a client older or newer than the
  router. Update by installing the release that matches the router (the tab links it); the
  client also shows its own update banner when a newer release exists. It never
  self-updates.

## Services and MCP probing (Team Lead piece 5)

A sprint declares what it needs (`studio`, `blender`, `audio`, `images`, `gpu`, `backend`; a
`## Needs` block in its plan, or the track analysis). Before it starts, the coordinator checks the
services those needs map to and, when one is down, the sprint waits ("waiting for rojo", retried
with a 30 s .. 10 min backoff, no attempt used). The map lives in `foundry_automation/service_map.py`:

| Need | Services | Provided by |
|---|---|---|
| `studio` | `rojo`, `robloxstudio-mcp`, `kilo` | client components (Rojo, Kilo); the MCP is spawned by Kilo |
| `blender` | `blender-mcp` | external; the add-on socket (default 9876) must be connected in Blender |
| `audio` | `Media_Gen` (music, BGM, SFX, speech, dialogue; replaced acestep-music, stable-audio, kokoro-tts and chatterbox-tts on 2026-10-27) | external (TrueNAS `http://192.168.0.206:8190/mcp`) |
| `images` | `Media_Gen` or `foundry-media` (the ChatGPT image adapter; either one; the local comfy-mcp was replaced by Media_Gen) | external; a sprint that needs 2D assets first asks the operator (`[automation] media_gen_images`, default `ask`: they come from ChatGPT for now) |
| `gpu` | `local-model` (the router's backend `/health`) | backend |
| `backend` | `router` | backend |

**MCP probing.** Every heartbeat (cached 30 s) carries `mcp` (the servers in the workstation's
`kilo.jsonc` merged over the global `~/.config/kilo/kilo.jsonc`) and `services` (the registry as
this client sees it). The probe is cheap and is not an MCP handshake: a local command's binary or
script exists (looking through `cmd /c` and `powershell -File`), a remote URL's host and port
accept a connection, Blender's socket port is open. A stub like `{"enabled": false}` is resolved
against the global config; a server defined nowhere reports `unknown`, which never blocks a
sprint. `GET /api/mcp` on the client's local API shows the same data.

**Optional managed components**, off by default and with no program set: `blender`,
`acestep-music`, `stable-audio`, `kokoro-tts`, `chatterbox-tts` (`path`, `args`, `cwd`, `env`,
`port`). Use them only for programs that run on this workstation. The four audio components
are legacy (Media_Gen on TrueNAS replaced those servers and is probed, not managed).

**Media_Gen per sprint.** A coordinator session sees only the Media_Gen tools its sprint
needs: the audio family (`generate_music`, `generate_bgm`, `generate_sfx`,
`generate_speech`, `generate_dialogue`, `register_voice`, `list_voices`) and the job tools
for `audio`; the image family only after the operator chose Media_Gen for a sprint's 2D
assets; video tools and the personal-only `generate_song_personal` never. Reviewers get
none. Tracks switch the Media_Gen server on in their `kilo.jsonc` for `audio` or `images`. Blender has no health check (its socket opens only after
"Connect" in its side panel), so a started Blender is never restarted as unhealthy.

**Verbs.** `client_restart` accepts these component names; `client_service_enable {component,
enabled}` saves the setting and starts or stops the component (refused when enabling without a
program path); a coordinator file request `start_service` (`detail.component`) starts a stopped
enabled component or restarts an unhealthy one, and never switches a disabled one on.

## Phases

1. `foundry_client` running from source on this workstation, replacing `foundry-stack.ps1`.
2. Packages and CI.
3. Foundry GUI integration (v0.134.0): the client heart-beats to the router and claims stack
   actions; the operator chat's `restart_kilo`, `restart_coordinator`, `restart_rojo` and
   `reload_kilo_config` reach it directly, not through the coordinator.
