# Automation page (/ui/automation)

One page to run the coordinator: which persona each role uses, the order of its
model routes, backend cutoffs, every coordinator setting, per-role permissions and
the task queue. Open it from the dashboard's **Automation** tab or the control
center's **Automation** button. Sign-in is the control center's operator key.

## Sections

| Section | What you do there |
|---|---|
| Chat | The operator chat (`chat_engine.py`): say what you want in plain language. The model sees a compact context (sprints and plan progress, deployed settings, usage, stack, open decisions) and a typed tool catalog; the router validates every call. Reads, drafts and previews run at once. Deploys, cutoff changes, persona and backend changes, router settings, admin actions, sprint starts, decisions and restarts come back as a confirm card with one checkbox per call; **Settings…** switches a class to run without a card (deletes and clears always ask) and picks the chat's own model. Each turn shows the calls it made, what changed (with the action ids), and **Audit** lists every turn, call and confirmation. Threads are kept on the router per operator. Since v0.140.0 the chat can see and change the whole router: see "Operator chat: the whole router" below. |
| Routing | Pick a persona per role (saved as the role's settings field, e.g. `roles.implementer`, `lead_manager.persona`). Drag routes into order (or focus the handle and press ↑ ↓), turn routes and backends on or off, set each backend's cutoff %. Each route shows live 5-hour and weekly usage with the cutoff tick; a window with no reading is striped, never 0%. "Now:" says which route a call would use right now and why earlier ones were skipped. The overview map and the dry run (with optional what-if usage) read the draft. |
| Settings | Every coordinator setting from the shared schema (`foundry_automation`), grouped like the profile, with defaults, limits, a short description and when a change applies (live, next session, restart). Problems the config store finds show on the field. |
| Role rules | Allow / ask / deny per role and category (read, edit, create files, shell, git, subagents, web, Studio, outside the workspace). Click a cell for its path globs or command patterns; click a role for its Kilo agent settings. The tester ("may the implementer edit C:\Roblox\AnimalUprising\src\x.lua?") is the config store's own, so it decides exactly as the generated Kilo rules do. Hard denies (secrets, force push, tags, skipped hooks) are locked. |
| Queue | The task queue as a **board** (columns by state, tracks as swimlanes, CANCELLED and COMPLETE collapsed with a count) or a flat table — the toggle is remembered. Cards show sprint, attempt, blocker and the open decision's countdown. Dragging is only a shortcut for an existing typed action (reorder a QUEUED sprint = `queue_reorder`; BLOCKED/FAILED into READY = unblock, with its modal); every other move snaps back — the engine owns those transitions. Approve (`queue_approve`), dependency editing (`queue_deps`, cycle-checked again by the coordinator) and re-queue of a CANCELLED sprint (`queue_requeue`) use the same confirm modals; COMPLETE is never re-queued (passed work is never rerun). The data comes from `GET /control/api/queue` (position = the order the engine starts tasks). **Your decisions** on the same tab is the cross-project PM inbox (`GET /control/api/inbox`): recommended default, countdown, the Lead escalation's one-line "why you", and "resolved by the Lead this week". |

## Draft, deploy, rollback

- **Settings and role rules** are a versioned document per project in the config
  store (`/admin/api/automation/*`, see `foundry_router/automation_api.py`). Edits
  save to the draft as you go; **Review & deploy** shows every change grouped by
  table or role and deploys a new revision. The coordinator applies it at its next
  safe point. **History** rolls back (a rollback is a new revision).
- **Backend cutoffs** are the router-wide document (`_router`,
  `backends.<name>.cutoff_pct`); deploying it sets each backend's `usage_limit_pct`.
- **Route order and backend on/off** are live router state, not versioned. Deploy
  takes a state snapshot first, then writes each changed persona
  (`pinned_models` = the order, `model_allowlist` = the routes that are on) and
  each backend switch. History lists those snapshots and restores the personas.
  Until Deploy these edits live only in the page (it warns before you leave).
- Nothing deployed yet? **Import project.toml…** turns the coordinator's profile
  into the first draft (workstation-only keys stay on the workstation).

Writes to `/admin/api/*` carry `X-Foundry-Automation: 1` and the signed-in operator
as `X-Foundry-Actor`. When `client_auth.protect_admin` is on, the page asks once for
the admin key, like the dashboard.

## Read-only glue (`foundry_router/ui/automation_routes.py`)

All need the operator session; POSTs also need the CSRF header, like `/control/api/*`.

| Endpoint | Returns |
|---|---|
| `GET /ui/automation` | the page (CSP allows only the pinned SortableJS from cdn.jsdelivr.net) |
| `GET /control/api/automation/usage` | per backend: family, enabled, health, the cutoff in force (`guardrails.effective_cutoff`), 5-hour and weekly windows (`used_pct` null when unknown, `stale` when old), `fail_closed` |
| `GET /control/api/automation/seed` | live routing state: persona routes, backend on/off, cutoffs |
| `POST /control/api/automation/dry-run` | `{doc, role?, roles?, usage?}` -> per role: chosen route and every skipped hop with its reason |
| `POST /control/api/automation/check` | role-rule warnings (store validation plus shell-with-a-scope) |
| `POST /control/api/automation/diff` | changed paths between two views |

The page is one static file (`foundry_router/ui/static/automation.html`), vanilla JS,
no build step. Without internet the drag library doesn't load; the arrow keys and
the ▲ ▼ buttons still reorder.

## Operator chat: the whole router (v0.140.0, `chat_router_tools.py`)

The chat can see and change everything the dashboard can. Every write is a
confirm card, takes a state snapshot first (History restores personas;
config.yaml changes are also a version in Config history) and is audited.

| Ask | Tool(s) | Class |
|---|---|---|
| "make Foundry-Chat use only local and Claude" | `set_persona_routes`: `local` / `Claude` / `ChatGPT` (`GPT`) keep the persona's current routes of that family; also model ids, `model@backend`, a backend name. The card shows the concrete `model@backend`. A strict persona tied to a backend that can't serve a new route is refused; `backend=""` unties it. | persona |
| "128K context", "turn off its MCP tools", "high effort" | `set_persona_fields` (same rules as the Personas tab: `persona_rules.py`; MCP grants by `mcp_add` / `mcp_remove`) | persona |
| "make a research persona like Foundry-Chat" | `create_persona` (`copy_from` or template `strict-chat` / `flexible-chat`; starts disabled); the result names client keys whose persona allow-list must add it: `grant_persona_to_client`, one card each | persona |
| "run its evals" | `run_persona_evals` (every persona write reminds you) | persona |
| "turn off the GPT backend" | `set_backend_enabled`; the card lists every persona left with no usable route (strict: refused; flexible: falls back to the pool) | backend |
| "raise the tool round limit to 12" | `get_router_config` / `set_router_config` (`section.key`, named list items by name); validated before the card, which says what applies live and what needs a restart. Secrets refused. | router_config |
| anything else | `list_admin_endpoints` (generated from the app), `admin_get` (any GET, secrets scrubbed), `admin_call` (any other admin write). Not from the chat: client-key create/rotate, Meridian sign-in, MCP tokens, import, raw backend list, router restart. Deletes and clears always need the card, even on auto. | read / admin |
| "research the new models" | `research_status`, `research_models` | read / admin |
| "how is performance?" | `performance_report` (requests and errors per persona, speed per model, backend health, tool failures, round-cap hits, the perf advisor, brain metrics when recorded) | read |
| the chat's own model | `get_chat_backend` / `set_chat_backend`, or **Settings… → Chat model** | router_config |

**The chat's own model** (`operator_chat.routes` in config.yaml): an ordered list
of `{backend, model}`; each call is pinned to that backend and passes the budget
gate; the next route answers when one fails. It never goes through a persona
(the old `Foundry-Operator-Chat` persona is no longer used), so persona edits
can't break the chat. Empty = the built-in chain: Claude Sonnet, GPT-6.1 Sol,
the local Qwen, each on the first enabled backend serving it.
`GET / PUT /control/api/chat/backend` back the settings picker.
