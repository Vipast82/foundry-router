# OpenCode beside Kilo: one agent-backend interface, two adapters

**Status:** plan only, nothing built. It waits for Victor's answers to the decisions at the end.
**Asked for (2026-11-22):** everything in Foundry-Router that talks to Kilo Code should also
support the latest OpenCode, and Kilo should keep working as it does now and in future versions.
Neither one replaces the other. Either agent CLI can be picked per project or per role.
**Branch:** `feat/opencode-backend` (worktree `C:\Users\shock\foundry-revamp\opencode`).

## 1. What "the latest OpenCode" is (checked 2026-11-22)

OpenCode now ships two lines that cannot be installed side by side. Both use the `opencode`
command, and `opencode upgrade --major` removes V1.

| | **V1** | **V2** |
|---|---|---|
| Latest | **1.19.6** (2026-11-17), npm `opencode-ai` | **2.3.2** (2026-11-18), npm `@opencode/cli` |
| Server API | the same API Kilo has. Every V1 path is in Kilo 7.8.32's OpenAPI; Kilo only adds paths | a rewrite under `/api/*`, with different bodies, events, permissions and question flow |
| Config | `opencode.json(c)`; `provider`, `agent`, `permission` (tool map), `mcp`, `subagent_depth` | the same file; `providers`, `agents`, `permissions: [{action, resource, effect}]` (`bash` becomes `shell`, `task` becomes `subagent`), `experimental.subagent_depth`; the V1 forms are still read |
| Auth | Basic, `OPENCODE_SERVER_PASSWORD`, user `opencode` | always on, `OPENCODE_PASSWORD` (or the old name), user `opencode` |
| Docs | removed from opencode.ai; the reference is the OpenAPI at tag v1.19.6 | opencode.ai/docs |

Kilo is an opencode fork that is still on the V1 API (Kilo 7.8.27 is installed here). If Kilo
later rebases onto V2, the same V2 code will serve it. That is the main reason to build the
interface around **API dialects** and not around product names.

Copies of the specs from this check are in the side-session notes folder, `opencode/specs/`
(`v1-sdk-openapi.json`, `v2-openapi.json`, `kilo-openapi.json`, the config schema and V2 docs).

### Endpoint map (what the coordinator uses today)

| Use | Kilo 7.8 (today) | OpenCode V1 1.19.6 | OpenCode V2 2.3.2 |
|---|---|---|---|
| create session | `POST /session?directory=` {title, agent, model{providerID,id}, metadata, permission[]} | same | `POST /api/session` {title, agent, model, location{directory}, metadata, permissions[{action,resource,effect}]} → `{data}` |
| send prompt | `POST /session/{id}/prompt_async` {agent, model{providerID,modelID}, parts} → 204 | same | `POST /api/session/{id}/prompt` {text, files[], delivery}; agent and model are set first with `POST …/agent` and `POST …/model` |
| status | `GET /session/status` (idle sessions absent; busy / retry) | same shape, and it can also list `{type: idle}` | `GET /api/session/active` ({type: running}) |
| transcript | `GET /session/{id}/message?limit=` [{info, parts}] | same | `GET /api/session/{id}/message?limit&cursor` typed messages (assistant, compaction, …) |
| exists / children / abort | `/session/{id}`, `/children`, `POST /abort` | same | `/api/session/{id}`, `?parentID=`, `POST …/interrupt` |
| questions | `GET /question`, `POST /question/{id}/reply` | same (the tool is on under `serve`) | forms: `GET /api/form`, `POST /api/session/{sid}/form/{fid}/reply` |
| permissions | `GET /permission`, `POST /permission/{id}/reply` {reply, message} | same | `GET /api/permission/request`, `POST /api/session/{sid}/permission/{rid}/reply` {decision, message} |
| merged config | `GET /config/effective` (**Kilo only**) | `GET /config` + `GET /config/providers` | `GET /api/config` (a list of documents) |
| reload a folder | `POST /instance/dispose` | same | `POST /api/location/reload` |
| health / version | `GET /global/health` | same | `GET /api/info` |
| events | `GET /event` SSE | same | `GET /api/event` (different event names) |
| compaction mark | `summary: true`, mode or agent `compaction`, a `compaction` part | same | message `type: "compaction"` |
| session header on model calls | `x-session-id` = the session itself | `X-Session-Id` + `x-opencode-session-id` = the session itself | `x-opencode-session-id` = the session; `X-Session-Id` = the **parent** for a child session |

## 2. Inventory: where Kilo is assumed today

The full lists, with file:line references, are in the side-session notes folder (`opencode/OPENCODE_INVENTORY.md`).
In summary:

**Coordinator (`foundry_coordinator/`, 119 files)**
- **One client interface already exists.** `adapters/base.py` defines `CodingClient` and
  `SessionState`, and `adapters/__init__.build_client` picks one by `[client].kind`. The engine,
  lead and helpers only see `SessionState`. They also call optional methods through `getattr`:
  `dispose`, `effective_config`, `model_config_problem`, `preflight`, `has_prompt`,
  `find_session`, `recent`, `exists`, `inference_models`, `children` and `.provider`.
- **A second Kilo HTTP client.** `telemetry.KiloObserver` reads health, status, messages,
  children, todo, question, permission, mcp, `/config/effective` and lsp, and streams `/event`
  SSE. `projects/animaluprising/kilo_checkpoint.py` calls `client._req` directly.
- **Config file surface.**
  - `kilo_config.py` handles agent permission blocks, `instructions`, `snapshot: false`, per-track
    `kilo.jsonc` and MCP toggles.
  - Other writers: `slots.py` (MCP overrides), `builder.py` (the `au-builder` agent object),
    `context_window.py` (model limits), `lead.py:975` (repair-worktree copy),
    `tracks/manager.py` (the `kilo_reload` flag) and `engine.py:1101` (`dispose`).
  - Protected paths: `scope.py:107`, `recovery.py:57` and `engine.py:2641`.
- **Skills and rules.** `skills_install.py` installs into `.kilo/skills` (also used by
  `skills_preload.py` and the `lanes.skill_dirs` default). AU's rule file is
  `.kilo/rules/animaluprising-adapter.md`.
- **Session semantics.**
  - Permission rule lists use last-match-wins, and a tool denied in full disappears from the tool
    list. Kilo-only tool names are denied (`role_rules.UNUSED_SESSION_TOOLS`).
  - The default agent is `code`.
  - Questions and permissions are answered with Kilo's reply shapes.
  - Compaction, retry, "not admitted" and stall detection all work this way, and `autoheal`
    matches Kilo's error text.
  - Child sessions are recognised by the title format `(@agent subagent)`.
- **Router binding.** Sessions are bound before the prompt (`engine._bind`, the lead, helpers).
  This relies on the agent stamping its session id on every model call.
- **Selection.** `[client].kind` (kilo | fake | scripted) is per project only; `profile.py:356`
  rejects anything else. There is no per-role client. Reviewers already run as direct router
  calls, not as Kilo sessions.

**Foundry Client (`foundry_client/`)**
- There is one `kilo` component: the `kilo` / `kilo.cmd` binary,
  `kilo serve --hostname 127.0.0.1 --port 4096`, `KILO_SERVER_PASSWORD`,
  `KILO_DISABLE_EXTERNAL_SKILLS`, and a Basic-auth user hard-coded to `kilo` (`probe.py:34`).
- Other Kilo-specific pieces: the health check `GET /config`, reload via `/instance/dispose` or a
  restart, the memory watchdog (`kilo_restart_gb`), `apply-kilo-config`, the workstation
  installer pinned to `@kilocode/cli@7.8.8`, the first `kilo.jsonc` (`workstation.kilo_jsonc`),
  the doctor checks, `move.py`, and the tray item.

**Router (`foundry_router/`)**
- **No user-agent branching.** The session id comes from `x-foundry-session`, then
  `x-session-id`, `x-opencode-session` and `x-session-affinity` (`client_auth.py:57`). OpenCode is
  half covered already.
- **How a persona is tied to Kilo.** Only by name: the `Foundry-Kilo-` prefix (`kilo_block`, the
  UI group, a budget-rule migration) and the client key `kilo` / `<ws>-kilo` with
  `FOUNDRY_KEY_KILO`.
- **Config export.** `GET /admin/api/personas/kilo-config` emits the `provider.foundry` block.
- **Stack actions.** `client_restart{component: kilo}`, `client_reload_kilo_config`, the chat
  tools `restart_kilo` / `reload_kilo_config`, and the heartbeat field `kilo`.

**Automation schema (`foundry_automation/`)**
- The `client.*` group: `kind` enum, `url`, `password_env`, `provider`, `agent`.
- Also `lead_manager.kilo_client`, `helpers.worker_client`, `skills.target = .kilo/skills`, and
  `kilo_restart_drain_minutes`.
- `role_rules` holds the Kilo permission dialect and the hard denies on `kilo.jsonc` and
  `.kilo/*`.
- `service_map` reads the global `~/.config/kilo/kilo.jsonc` and names Kilo MCP tools
  `<server>_<tool>`.

**Training (`foundry_training/`)**
- `adapters/kilo.py` reads `kilo export` files, the server API, or Kilo's SQLite store. OpenCode
  V1 has the same tables in `~/.local/share/opencode/opencode.db`.
- Kilo is the only adapter: `pipeline.py:283` ingests `client == "kilo"` only, `correlate` and
  `episodes` filter on `source_adapter == "kilo"`, and the default sources include `kilo`.

**Legacy:** `contrib/kilo-animaluprising` is the 2026-10-14 trial overlay. Nothing runs it.
The plan leaves it alone.

## 3. Design

### 3.1 Two layers: backend and dialect

```
Backend (product)   kilo | opencode         names, paths, process, files, router key, training source
Dialect (HTTP API)  oc1  | oc2              how sessions, prompts, status, questions, config, events work
                    kilo = oc1 + Kilo extras (/config/effective, x-kilocode-mode, Kilo-only tools)
                    opencode 1.19 = oc1      opencode 2.3 = oc2      a future Kilo on V2 = oc2 + extras
```

**`foundry_automation/agent_backends.py` (new, no imports from the coordinator).** It holds one
`BackendSpec` per backend, and the client, coordinator, automation schema and training all read
it. Each spec carries:
- binary names and npm fallback paths;
- `serve` arguments, the default port (Kilo 4096, OpenCode **4097** so both can run), and the
  password and username environment variables with the default username;
- the environment variables that turn off outside skills. For OpenCode this matters:
  `OPENCODE_DISABLE_EXTERNAL_SKILLS`, `OPENCODE_DISABLE_CLAUDE_CODE` (so it does not read
  `.claude/skills` or `CLAUDE.md`), and `OPENCODE_DISABLE_AUTOUPDATE`;
- the project config file name and global config folder, the schema URL, the skills folder
  (`.kilo/skills` or `.opencode/skills`), and the rules folder;
- the protected paths (`kilo.jsonc`, `.kilo/*` / `opencode.json(c)`, `.opencode/*`);
- the default agent (`code` / `build`), the built-in subagent for helpers (`explore` in both), and
  the tools only this backend has (to deny);
- the router client-key name (`kilo` / `opencode`), its environment variable
  (`FOUNDRY_KEY_KILO` / `FOUNDRY_KEY_OPENCODE`), and the training source name.

The Kilo spec holds exactly today's values, so every `"kilo"` literal that moves into it reads
the same thing it did before.

### 3.2 The coordinator's client interface

Keep `CodingClient` / `SessionState` as the one interface. The engine, lead and helpers do not
change. Make the optional methods explicit with safe defaults in `base.py`:

| Change | Detail |
|---|---|
| New base methods | `reload(directory)` (dispose or location reload), `config_view(directory)` (Kilo's `/config/effective`, or OpenCode's `/config` + `/config/providers`, normalised to one shape), `model_config_problem`, `find_session`, `has_prompt`, `exists`, `health` |
| `adapters/oc1.py` (new) | `Oc1Client`, the HTTP code moved out of `KiloClient` unchanged |
| `KiloClient` | becomes `Oc1Client` with the Kilo spec plus its extras. Same class name, same module, same behaviour, same tests |
| `adapters/opencode.py` (new) | `OpenCodeClient`: `Oc1Client` with the OpenCode spec for 1.19, or `Oc2Client` for 2.x |
| `Oc2Client`: version | picks `oc2` automatically: a server that answers `GET /api/info` with version 2.x |
| `Oc2Client`: requests | creates a session with `location.directory` and translated permissions; sets agent and model before each prompt; reads status from `/api/session/active` |
| `Oc2Client`: answers and transcript | maps forms to `question` and permission requests to `permission`; returns the transcript translated into today's `{info, parts}` shape (`oc2_shape.py`) |
| Why translate the transcript | `context_window`, `evidence.tool_view`, `frontier`, `telemetry` and the checkpoint keep reading one shape |
| Error text | `foundry_denial` moves to `adapters/denial.py` (scripted.py imports it from there); the autoheal patterns gain the V2 error words beside the Kilo ones |
| `telemetry.KiloObserver` | becomes the `oc1` observer, unchanged for Kilo |
| `oc2` observer | polls first. The `/api/event` stream comes later, and it is not needed for sprints to work |

### 3.3 Config: one intent, three renderers

The coordinator decides what a session needs:
- a permission rule list;
- MCP servers on or off;
- `instructions`;
- whole agent objects (lead, helper, `au-builder`);
- `snapshot: false` and `subagent_depth`;
- provider models with limits and modalities.

Today `kilo_config.py` writes that straight into `kilo.jsonc` text, keeping comments. The plan:
- **Kilo:** the code path stays as it is (`kilo_config.py` is not rewritten). Only its file name
  and paths come from the spec.
- **OpenCode** gets `agent_config.py`, which renders the same intent into `opencode.jsonc` with the
  same comment-keeping JSONC edits (`foundry_automation.jsonc`). It writes V1 keys for 1.19, or
  V2 keys for 2.x:
  - permissions become `{action, resource, effect}`;
  - `bash` becomes `shell` and `task` becomes `subagent`;
  - a catch-all rule goes first, because V2 asks when no rule matches;
  - image input is set with `modalities` only, because V2 ignores `attachment`.
- **Always written for OpenCode:** `"share": "disabled"` (sessions are never uploaded),
  `"autoupdate": false`, and `enabled_providers: ["foundry"]`.
- **Permission translation** lives in `role_rules` as one function per dialect. The Kilo output
  stays byte-for-byte the same, and a test pins it.
- **Router export:** `GET /admin/api/personas/agent-config?format=kilo|opencode1|opencode2`. The
  old `/kilo-config` stays and returns exactly what it does today.
- **First config:** the Foundry Client's first-config writer and `apply-kilo-config` get an
  `apply-agent-config --backend opencode` twin.

### 3.4 Picking Kilo or OpenCode per project or per role

```toml
[client]                 # unchanged: the project's default agent server
kind = "kilo"            # kilo | opencode (| fake | scripted)
url = "http://127.0.0.1:4096"

[clients.opencode]       # new, optional: more agent servers this project may use
kind = "opencode"
url = "http://127.0.0.1:4097"
password_env = "OPENCODE_SERVER_PASSWORD"
agent = "build"
api = "auto"             # auto | oc1 | oc2

[client_roles]           # new, optional: role -> which client runs it (default: [client])
helper = "opencode"      # roles: implementer, lead, repair, helper, closeout, builder
```

**`P.client` becomes a small `ClientSet` facade.** It implements `CodingClient`:
- `start_session` picks the client from the session's `metadata.role`, and the task's
  `role_overrides` can name one;
- every later call (`prompt`, `state`, `answer`, `abort`, …) goes to the client that owns that
  session id. The owner is written to the session row the store already keeps (`client` column)
  and is cached in memory, so polling never asks the wrong server.

With no `[clients]` table the facade has one client, and today's projects behave exactly as they
do now. A track's sessions use the same choice in their worktree. For OpenCode the per-track
config file is `opencode.jsonc`, written by the same track hook. Personas are not tied to a
backend: OpenCode sends `foundry/Foundry-Kilo-Implementer` like Kilo does, through a router
client key named `opencode` that is granted the same personas. **No live persona, route or chain
changes.**

### 3.5 Foundry Client

| Area | Change |
|---|---|
| Component | a new `opencode` component, **off by default**: binary `opencode` / `opencode.cmd` / `opencode.exe`, `opencode serve --hostname 127.0.0.1 --port 4097`, the password from the Secrets store (`OPENCODE_SERVER_PASSWORD`), and the disable-outside-skills / auto-update / Claude-Code environment variables from the spec |
| Generalised | the health probe (with the spec's username), doctor checks, the memory watchdog (per agent component, with `opencode_restart_gb` beside the Kilo keys), reload at a safe point (dispose or location reload, else restart), and the tray item |
| Actions | `client_reload_agent_config {component}` beside the existing `client_reload_kilo_config`, which stays as an alias; `reload_opencode` beside `reload_kilo` |
| Heartbeat | carries `agents: {kilo: …, opencode: …}` and keeps the `kilo` field as it is, so the control page keeps working |
| Installer | `install/workstation.sh` gets an optional `OPENCODE_VERSION`. **Installing is Victor's step**, see 6 |

### 3.6 Router

| Area | Change |
|---|---|
| Session header | `x-opencode-session-id` is read **before** `x-session-id`. For Kilo and OpenCode 1.x the two are the same id; for OpenCode 2.x child sessions only the first is the child's own id. A test pins Kilo's headers to the same binding as today |
| Client key | a client-key preset `opencode` (scopes inference + media) and a workstation key `<name>-opencode`. **Making the key is a step Victor approves**: it is a new access key |
| Labels | "Kilo" labels become "Kilo / OpenCode" where they are about either. The persona tab still groups by the `Foundry-Kilo-` prefix |
| Control page | reads the new `agents` heartbeat map. It shows an OpenCode pill only when that component is on |

### 3.7 Training capture

| Area | Change |
|---|---|
| OpenCode 1.x | `adapters/opencode.py` reuses Kilo's normaliser (the same `{info, parts}` and the same SQLite tables), with `ADAPTER = "opencode"` and the spec's username and storage path |
| OpenCode 2.x | read through the API, translated by `oc2_shape` first |
| Pipeline | `pipeline.py` ingests every agent client, not just `kilo`; `correlate` and `episodes` accept `opencode` |
| Sources | the default sources gain `opencode`. Kilo capture is unchanged |
| Rule | as agreed for training data (keep everything), OpenCode sessions are kept like Kilo's |

### 3.8 Settings and UI (every setting has a control and passes the allow-list)

| Setting | Where it is set | UI control |
|---|---|---|
| `client.kind` += `opencode`; `client.api` (auto/oc1/oc2) | automation schema (strict table) | Automation → Coding client |
| `clients.<name>.{kind,url,password_env,agent,provider,api}` | schema; `url` / `password_env` added to `LOCAL_KEYS` like today's | Automation → Coding client → "More agent servers" table |
| `client_roles.<role>` | schema | Automation → Coding client → "Run this role on" select per role |
| `skills.target` per backend (`.kilo/skills` / `.opencode/skills`) | schema | Automation → Skills |
| `automation.agent_restart_drain_minutes` (the Kilo key stays an alias) | schema | Automation → Automation |
| `components.opencode.{enabled,path,port,args}`, `resources.opencode_restart_gb`, `workspace.opencode_config` | client.toml | Foundry Client page (`client.html`): settings rows and an "OpenCode configuration" preview/apply card |
| agent-config export format | GET parameter | Routing → Personas → Default personas: a format select beside "Kilo provider config" |

The router gets no new `/admin/api/config/*` key in this plan. If one comes up while building, it
goes into that handler's `allowed` set in the same commit, and a test reads the handler for it.
The Foundry Client's `PUT /api/settings` has no allow-list; its
`validate()` gets the new keys.

## 4. What stays exactly as it is
- `KiloClient`'s requests, bodies and parsing; `kilo_config.py` output; the `/kilo-config`
  export; `reload_kilo`; the `kilo` component; the Kilo heartbeat field; `adapters/kilo.py` in
  training.
- Every existing Kilo test runs unchanged. A moved function keeps a re-export at its old path, so
  no test import changes.
- Live Kilo personas, routes, model order, cutoffs, sprint settings and `kilo.jsonc` are not
  touched. AnimalUprising keeps `[client] kind = "kilo"` and gets no `[clients]` table unless
  Victor asks.

## 5. Build order (each step is its own release; I claim the version number with the coordinator session before each gate)

1. **Spec + interface, no behaviour change.**
   - Add `agent_backends.py`.
   - Move `KiloClient`'s HTTP code into `Oc1Client`, and move `foundry_denial`.
   - Make the base-class methods explicit.
   - Route the client component, probe and doctor literals through the Kilo spec.
   - Gate: the full suite green, plus a new test that pins Kilo's request bodies, headers and
     config output.
2. **OpenCode 1.x and 2.x clients.**
   - Add `Oc2Client` with `oc2_shape`, and `fake_opencode.py` (both dialects, like `fake_kilo`).
   - Add the config renderer and permission translation, and the profile and schema keys.
   - Add `ClientSet` with session ownership.
   - Tests run the engine, lead and helpers against `fake_opencode` in both dialects.
3. **Foundry Client + router + UI.**
   - The `opencode` component, actions, heartbeat map, header order and key preset.
   - The export formats and all UI controls.
4. **Training.** The OpenCode adapter, the pipeline and the sources.
5. **Pilot (live check).** See 6, then fix what it finds.

Steps 1–4 can ship while sprints run: Kilo behaviour does not change, and OpenCode is off until it
is turned on.

## 6. The live pilot (Victor's steps marked ★)

**Pilot project.** A new small project, `opencode-pilot`, not AnimalUprising:
- a scratch git repo on the workstation (`C:\Foundry\opencode-pilot`), `tasks.source = "toml"`;
- two tasks: a small Python module with tests, and a doc edit;
- verification by `pytest`.

It runs through the coordinator like any project: session, prompt, poll, verify, review, commit.
It also runs one helper and one forced question/permission so those paths are exercised.

**Steps:**
1. ★ Install OpenCode on the workstation, one of:
   - V2 (latest): `npm i -g @opencode/cli@2.3.2`
   - V1: `npm i -g opencode-ai@1.19.6`

   They cannot both be installed. Never run `opencode upgrade --major`.
2. ★ Approve the router client key `opencode`, granted the personas the pilot uses (I suggest
   `Foundry-Kilo-Local` and `Foundry-Kilo-Implementer`). Then save its key as
   `FOUNDRY_KEY_OPENCODE`, and a new `OPENCODE_SERVER_PASSWORD`, in the Foundry Client's Secrets.
3. Turn on the `opencode` component (port 4097) and apply the generated `opencode.jsonc` to the
   pilot folder.
4. ★ Deploy the release to the live coordinator (checkout tag + coordinator restart). Never while a lead case is being checked, and never
   during the bench profile run.
5. Run the pilot's tasks. Check that the router binds each OpenCode call to its session (the
   control page shows the work), that verify and review pass, and that training capture records
   the sessions.
6. Optional, only if Victor asks: give one AnimalUprising role (for example `helper`) to OpenCode
   through `[client_roles]`.

**Done means:**
- the pilot's tasks complete on OpenCode, checked live;
- the full Kilo suite is green, with no Kilo test edited;
- every new setting has its UI control and, for router config, its allow-list entry.

## 7. Risks and how the plan handles them

| Risk | Handling |
|---|---|
| OpenCode 2.x child sessions send the parent id in `X-Session-Id` | read `x-opencode-session-id` first (3.6) |
| 2.x asks when no permission rule matches; a session blocks on an ask | catch-all rule first (3.3); the existing auto-reject path handles a stray ask |
| OpenCode reads `.claude/skills`, `.agents/skills` and `CLAUDE.md`. The AU repo and the workstation have `.claude/` folders | `OPENCODE_DISABLE_CLAUDE_CODE` + `OPENCODE_DISABLE_EXTERNAL_SKILLS` on the component |
| Session sharing uploads transcripts | `"share": "disabled"` always written; doctor fails if it is not |
| Auto-update moves the version under us | `"autoupdate": false` + `OPENCODE_DISABLE_AUTOUPDATE`; version pinned in the installer |
| 2.x has no per-prompt model in the prompt body (frontier switch) | two calls (set model, then prompt) |
| Kilo moves to the V2 API later | the Kilo spec switches dialect to `oc2`; detected from `/api/info` |
| Kilo-only tool names in deny lists | per-backend list in the spec |
| Not yet verified: does 1.19 list idle sessions in `/session/status`; can V1 and V2 share `opencode.db` | the clients treat `{type: idle}` as idle (a no-op for Kilo); training reads only through the API for 2.x |

## 8. Decisions for Victor

1. **Which OpenCode to target first.**
   - **Recommended: 2.3.2 (V2, the latest).** The pilot runs on it, and 1.19 is supported through
     the shared Kilo dialect with fake-server tests only.
   - Alternatives: target 1.19.6, which is less work and the same API as Kilo, but OpenCode's
     older line; or both live.
2. **Pilot.** Recommended: a new scratch project, `opencode-pilot`. The alternative is one
   AnimalUprising role, `helper`.
3. **Selection.** Recommended: a per-project default plus a per-role override (3.4). The
   alternative is per project only.
4. **Personas.** Recommended: reuse the `Foundry-Kilo-*` personas through a new `opencode` client
   key, with no renames and no new personas. The alternative is an `Foundry-OpenCode-*` set.
5. **Port.** Recommended: 4097 for OpenCode, so both run at once.
