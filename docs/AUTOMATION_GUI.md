# Automation page (/ui/automation)

One page to run the coordinator: which persona each role uses, the order of its
model routes, backend cutoffs, every coordinator setting, per-role permissions and
the task queue. Open it from the dashboard's **Automation** tab or the control
center's **Automation** button. It uses the control center's operator sign-in.

## Sections

| Section | What you do there |
|---|---|
| Routing | Pick a persona per role, drag routes into order (or focus the handle and press ↑ ↓), turn routes and backends on or off, set each backend's cutoff %. Each route shows live 5-hour and weekly usage with the cutoff tick; a window with no reading is striped, never 0%. "Now:" says which route a call would use right now and why earlier ones were skipped. The overview map and the dry run (with optional what-if usage) read the draft. |
| Settings | Every coordinator setting from the shared schema, grouped like the profile, with defaults, limits, a short description and when a change applies (live, next session, restart). |
| Role rules | Allow / ask / deny per role and category (read, edit, create files, shell, git, subagents, web, Studio, outside the workspace). Click a cell for its path globs or command patterns; click a role for its Kilo agent settings. The tester answers "may the implementer edit C:\Roblox\AnimalUprising\src\x.lua?". Hard denies (secrets, force push, tags, skipped hooks) are locked. |
| Queue | Tasks with state and dependencies, decisions waiting on you with the recommended default and the time until it applies. Pause, resume, cancel and re-queue use the control center's typed actions; approve, reorder and dependencies use the config store's queue API. |

## Draft, deploy, rollback

Edits on Routing, Settings and Role rules go into a draft. **Review & deploy** shows
every changed path (before and after) and deploys a new revision; **History** lists
revisions and rolls back to one. The coordinator applies a deployed revision at its
next safe point.

Until the config store (C1) is on the router, the page runs in **preview**: it reads
today's personas and backends, keeps the draft in this browser, and Deploy and
Rollback are off. The tester then uses a close approximation of the rules engine
and says so.

## Endpoints (read-only glue, `foundry_router/ui/automation_routes.py`)

All need the operator session; POSTs also need the CSRF header, like `/control/api/*`.

| Endpoint | Returns |
|---|---|
| `GET /ui/automation` | the page (CSP allows only the pinned SortableJS from cdn.jsdelivr.net) |
| `GET /control/api/automation/usage` | per backend: family, enabled, health, cutoff, 5-hour and weekly windows (`used_pct` null when unknown, `stale` when old) |
| `GET /control/api/automation/seed` | preview only: today's settings as one document |
| `POST /control/api/automation/dry-run` | `{doc, role?, roles?, usage?}` -> per role: chosen route and every skipped hop with its reason |
| `POST /control/api/automation/path-test` | `{doc, role, op: read|edit|write|bash, path, workspace?}` -> decision, deciding rule, locked |
| `POST /control/api/automation/check` | rule warnings (C1 validation plus shell-with-a-scope) |
| `POST /control/api/automation/diff` | changed paths between two documents |

The page is one static file (`foundry_router/ui/static/automation.html`), vanilla JS,
no build step. Without internet the drag library doesn't load; the arrow keys and
the ▲ ▼ buttons still reorder.
