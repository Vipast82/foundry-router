# OpenCode beside Kilo

The coordinator, the Foundry Client, the router and training capture can drive either
**Kilo Code** (`kilo serve`) or **OpenCode** (`opencode serve`). The choice is made per
project, and per role inside a project. Neither replaces the other. A project that names
only Kilo runs exactly as it did before v0.181.247.

- **Plan and decisions:** `docs/plans/OPENCODE_BACKEND_PLAN.md`.
- **What differs between the two products:** `foundry_automation/agent_backends.py`.

## Which OpenCode

OpenCode ships two lines, and they cannot be installed side by side because both are
the `opencode` command.

| Line | Version (2026-11-22) | npm package | Server API |
|---|---|---|---|
| 2.x, current | 2.3.2 | `@opencode/cli` | `/api/*`; translated by `adapters/oc2.py` |
| 1.x | 1.19.6 | `opencode-ai` | The same API Kilo has (`adapters/oc1.py`, shared with Kilo) |

`client.api = "auto"` asks the server which line it is: 2.x answers `GET /api/info`.
Never run `opencode upgrade --major` on the workstation, because it removes 1.x.

## Install (Victor)

```powershell
npm i -g @opencode/cli@2.3.2
```

Then:

1. **Router → Setup → Clients:** make a client key from the `opencode` preset. Grant it
   the personas the OpenCode roles use, for example `Foundry-Kilo-Local` and
   `Foundry-Kilo-Implementer`.
2. **Foundry Client → Secrets:** save that key as `FOUNDRY_KEY_OPENCODE`, and save a new
   password as `OPENCODE_SERVER_PASSWORD`.
3. **Foundry Client → Settings → OpenCode server:** turn it on. The default port is 4097,
   because Kilo uses 4096. The server runs with:
   - `OPENCODE_DISABLE_CLAUDE_CODE`, `_CLAUDE_CODE_SKILLS` and `_EXTERNAL_SKILLS`, so it
     never reads `.claude/skills`, `.agents/skills` or `CLAUDE.md`;
   - `OPENCODE_DISABLE_AUTOUPDATE`;
   - `OPENCODE_DISABLE_FREE_MODELS`.

## A project on OpenCode

Set this in the project profile, or under Automation → Coding client:

```toml
[client]
kind = "opencode"
url = "http://127.0.0.1:4097"
password_env = "OPENCODE_SERVER_PASSWORD"
agent = "build"            # OpenCode's primary agent (Kilo's is "code")
api = "auto"               # auto | oc1 | oc2

[helpers]
worker_client = "opencode" # the router client key its model calls arrive with
[lead_manager]
kilo_client = "opencode"
```

Then write the project's first `opencode.jsonc`:

```powershell
python -m foundry_coordinator --profiles <profiles> agent-config <project> --provider-from C:\Roblox\AnimalUprising\kilo.jsonc --apply
```

This copies `provider.foundry` from that Kilo file and points its key at
`{env:FOUNDRY_KEY_OPENCODE}`; a literal key is never copied. The file it writes always
holds:

- `share: "disabled"`;
- `autoupdate: false` (2.x also gets `update: "disable"`);
- `enabled_providers: ["foundry"]`;
- `snapshot: false`;
- a subagent depth of 1;
- a stub for every agent the role rules name.

The command never overwrites an existing file. From then on, everything the coordinator
does to `kilo.jsonc` it does to this file with the same code: role-rule permission
blocks, per-track copies and skill allows. Skills install to `.opencode/skills`.

The router prints the same provider block under Routing → Personas → Default personas.
Pick "OpenCode 2.x" in the format select next to **Provider config**, or call
`GET /admin/api/personas/kilo-config?format=opencode`. `format=kilo`, the default, is
unchanged.

## One role on OpenCode, the rest on Kilo

```toml
[clients.opencode]
kind = "opencode"
url = "http://127.0.0.1:4097"
password_env = "OPENCODE_SERVER_PASSWORD"
agent = "build"
worker_client = "opencode"

[client_roles]
helper = "opencode"        # roles: implementer, helper, lead
```

- **How it routes:** sessions start on the server their role names. Every later call on a
  session goes to the server that started it, even after a coordinator restart. Each
  server's config file gets the role rules.
- **Telemetry:** the control page shows sessions from both servers. It polls the OpenCode
  2.x server, because that server's event stream is not translated yet.

## Safety on every OpenCode session

On top of the coordinator's rules, every OpenCode session is denied:

- edits to `opencode.json(c)` and `.opencode/*`;
- shell commands that name its password or key.

The deny rules on Kilo's files stay as they were. The preflight check fails if `share`
is not `"disabled"`.

## What is not done yet

- **The live pilot.** It needs OpenCode installed (see the plan, section 6).
- **OpenCode 2.x's event stream.** The bridge polls instead.
- **A memory watchdog for the OpenCode server.** Kilo's watchdog is unchanged.
- **The control page's agent heartbeat.** It still shows the project's agent server
  under the "Kilo" label.
