# Automation page (/ui/automation)

One page to run the coordinator: which persona each role uses, the order of its
model routes, backend cutoffs, every coordinator setting, per-role permissions and
the task queue. Open it from the dashboard's **Automation** tab or the control
center's **Automation** button. Sign-in is the control center's operator key.

## Sections

| Section | What you do there |
|---|---|
| Chat | The operator chat (`chat_engine.py`): say what you want in plain language. The model sees a compact context (sprints and plan progress, deployed settings, usage, stack, open decisions) and a typed tool catalog; the router validates every call. Reads, drafts and previews run at once. Deploys, cutoff changes, persona changes, sprint starts, decisions and restarts come back as a confirm card with one checkbox per call; **Auto-run…** switches a class to run without a card. Each turn shows the calls it made, what changed (with the action ids), and **Audit** lists every turn, call and confirmation. Threads are kept on the router per operator. |
| Routing | Pick a persona per role (saved as the role's settings field, e.g. `roles.implementer`, `lead_manager.persona`). Drag routes into order (or focus the handle and press ↑ ↓), turn routes and backends on or off, set each backend's cutoff %. Each route shows live 5-hour and weekly usage with the cutoff tick; a window with no reading is striped, never 0%. "Now:" says which route a call would use right now and why earlier ones were skipped. The overview map and the dry run (with optional what-if usage) read the draft. |
| Settings | Every coordinator setting from the shared schema (`foundry_automation`), grouped like the profile, with defaults, limits, a short description and when a change applies (live, next session, restart). Problems the config store finds show on the field. |
| Role rules | Allow / ask / deny per role and category (read, edit, create files, shell, git, subagents, web, Studio, outside the workspace). Click a cell for its path globs or command patterns; click a role for its Kilo agent settings. The tester ("may the implementer edit C:\Roblox\AnimalUprising\src\x.lua?") is the config store's own, so it decides exactly as the generated Kilo rules do. Hard denies (secrets, force push, tags, skipped hooks) are locked. |
| Queue | Tasks with state and dependencies, decisions waiting on you with the recommended default and the time until it applies. Pause, resume, cancel, re-queue and answers use the control center's typed actions. Approve, reorder and dependencies need a queue API that doesn't exist yet; they stay hidden until it does. |

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
