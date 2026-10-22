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
| Managed components | The Kilo server, the coordinator with its upgrade supervisor, the media adapter, and Rojo |

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
| Kilo server | `kilo serve --port 4096` (path detected or set), cwd = game workspace | TCP 4096 and `GET /config` | safe point |
| Coordinator | `foundry-client coordinator run --control ...` (bundled) | heartbeat file age | safe point (tick boundary) |
| Upgrade supervisor | `foundry-client supervisor --loop ...` (bundled) | process alive | any time |
| Media adapter | the configured `foundry_media_mcp.py` command | process alive | any time |
| Rojo | `rojo serve <project> --port 34872` (path detected or set) | TCP 34872 | any time. Plugin reconnects are still manual in Studio. |

Kilo, Rojo and the media adapter are external programs. They are detected or configured, not
bundled.

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
`foundry-client install-autostart`, `foundry-client import-legacy-secrets`.

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

## Phases

1. `foundry_client` running from source on this workstation, replacing `foundry-stack.ps1`.
2. Packages and CI.
3. Foundry GUI integration (v0.134.0): the client heart-beats to the router and claims stack
   actions; the operator chat's `restart_kilo`, `restart_coordinator`, `restart_rojo` and
   `reload_kilo_config` reach it directly, not through the coordinator.
