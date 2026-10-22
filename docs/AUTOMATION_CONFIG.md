# Automation config store (router-owned coordinator settings)

The router keeps the coordinator's settings, roles and role rules as **versioned documents**, one per project, plus `_router` for per-backend cutoffs. You edit a draft, validate it, and deploy it as a new revision. Rollback appends a copy of an older revision. The coordinator pulls the deployed revision and applies it when no session is live.

This page covers:

- [Pieces](#pieces): the modules involved.
- [What stays local](#what-stays-local): settings the router never stores.
- [Flow](#flow): import, edit, deploy, pull.
- [Role rules](#role-rules): permissions, scopes and hard denies.
- [Turning it off](#turning-it-off).

## Pieces

| Piece | Where |
|---|---|
| Shared schema: fields, defaults, validators | `foundry_automation/schema.py`, used by the router and `foundry_coordinator/profile.py` |
| Role rules and Kilo rule generation | `foundry_automation/role_rules.py` |
| Document: import, overlay, validate, diff | `foundry_automation/document.py` |
| Router store and API | `foundry_router/automation_store.py` and `automation_api.py` (`/admin/api/automation/*`, `/v1/foundry/control/config`) |
| Coordinator pull and apply | `foundry_coordinator/config_pull.py`, registered in `run` |
| kilo.jsonc regeneration | `foundry_coordinator/kilo_config.py`, run with `foundry-coordinator kilo-config PROJECT [--apply]` |

## What stays local

Secrets and machine bootstrap values never leave `project.toml`. The router refuses them in a draft. They are:

- `workspace.path`
- `adapter.module`
- `client.url` and `client.password_env`
- `foundry.url` and `foundry.key_env`
- `telemetry.key_env`
- `lead_manager.repair_root` and `lead_manager.router_repo`
- `animaluprising.python` and `animaluprising.lune`
- `[config_pull]`
- every `*_env` key

## Flow

1. **Import the current profile into a draft.** Nothing is lost: overlaying the imported document back onto the TOML gives the TOML again.
   ```bash
   curl -X POST http://ROUTER:11435/admin/api/automation/projects/animaluprising/import \
     -H "X-Foundry-Automation: 1" -H "content-type: application/json" \
     --data "{\"toml\": $(python -c 'import json,sys;print(json.dumps(open(sys.argv[1]).read()))' project.toml)}"
   ```
2. **Edit the draft in the GUI.** Each save sends the draft's `etag`, so concurrent edits are detected. Then validate and deploy.
3. **The coordinator pulls each tick**, at most every `[config_pull] interval_seconds` (default 60). It overlays the revision onto `project.toml`, validates it with the profile loader, and updates the live profile in place when no session is live. It then reports back:
   - `pending`: waiting for a safe point.
   - `applied`: the new settings are live.
   - `rejected`: the revision failed validation.

   The applied revision is cached in `<state_dir>/config/<project>.json`. On a restart, or while the router can't be reached, the coordinator keeps using that cached revision.
4. **Force a pull** with `foundry-coordinator config-pull [PROJECT]`.

## Role rules

Each role (implementer, lead, reviewer, deep_reviewer, visual_reviewer, planner, researcher, helper, coordinator) gets:

- **Permissions:** a matrix of allow / ask / deny for:
  - edit, write, bash, git
  - task (subagents)
  - webfetch
  - Studio read (list, get, search, screenshot, console) and Studio write (set, create, delete, insert, mass, run, execute)
  - external_directory
- **Path scopes:** read and edit path globs. Absolute Windows paths are fine, e.g. `C:\Roblox\**`.
- **Shell:** bash allow and deny patterns.
- **Tools and agents:** a subagent allow-list (`task_agents`, default `explore`) and a deny list for tools.

An older document that still says `studio_mcp` is accepted: its value applies to both Studio categories until the draft is saved again.

### What a session may edit

- **An empty edit scope means the sprint's files only.** The coordinator injects the sprint's own file list (the plan's "Files this sprint may change"), the closeout-exempt paths, the fast-lane runner outputs and the paths the `[automation] scope_auto_approve` allow-list grants by itself. A sprint with no files may edit nothing.
- **`edit_paths.allow` adds to that** (paths every sprint of the role may touch); **`edit_paths.deny` always wins.**
- **Reading never grants writing.** A folder opened by a read glob is denied to the edit and write tools, even though `external_directory` opens it.
- **Config files are never editable** by any role but the coordinator: `kilo.jsonc`, `.kilo/**`, `.git/**`, `tools/hooks/**`, `.githooks/**`, `.husky/**`.
- Without a deployed revision, a session still gets this floor: the sprint scope, the config-file denies and the hard denies. Everything else then comes from `project.toml` as before.

From the deployed rules, the coordinator generates:

- **Per-session `permission[]` rules**, appended after the adapter's own rules. The subagent rule comes before them, so a GUI edit of `task` or `task_agents` wins.
- **`agent.<name>.permission` blocks in kilo.jsonc**, rewritten automatically when a deploy changes role rules (`[config_pull] kilo_config_apply = false` turns that off), or by hand with `kilo-config --apply`. The file is backed up, only those values are replaced (comments survive), the file is re-parsed and the backup restored on any mismatch. Kilo reads the file at start, so the coordinator writes a `reload_kilo` request for the Foundry Client (see below).

**Hard denies are always emitted last, and no setting removes them**, not a role's allow, not an `extra` rule, not the lead's write mode. They cover:

- reading or editing secrets, `.env*` and credential files, through the tools and through the shell (`cat .env`, `printenv`, `$env:`, `~/.foundry-coordinator`)
- force pushes, `+ref` pushes, ref and tag deletion, `--mirror`, `--prune`
- history rewrites: `reset --hard`, `rebase`, `commit --amend`, `update-ref`, `filter-branch`
- hook bypasses: `--no-verify`, `commit -n`, `core.hooksPath`, `git -c`
- the Studio tools no role may call: `manage_monetization`, `upload_image`, `insert_asset` and the live script mutators (`set_script_source`, `*_script_lines`, `multi_edit`, `find_and_replace_in_scripts`). Scripts change through git.

### The lead's write mode

`[lead_manager] write_access` gives the lead session every tool, then the lead role's rules verbatim (denies included) with the sprint's edit scope, then its own denies: no `git push` of any kind, no history rewrite, no hook or git-config change, no `git clean`, no `question`. The lead widens a sprint's scope only by adding a path the `scope_auto_approve` allow-list covers to the plan's file list; anything else is a scope decision. A test the lead changed itself cannot be approved by the lead (`approve_test_change` is refused for it; the operator decides).

## Operator checklist: the first deployment

Nothing is deployed until you deploy. Until then every session runs on `project.toml` plus the floor above.

1. **Import the live profile into the draft** (GUI: Automation, Import, or the `curl` in step 1 of Flow). Check the import notes: role rules are seeded from `client.permissions.external_directory`, `helpers.deny_tools` and `lead_manager.write_access`.
2. **Read the role matrix.** For each role, check the four things that matter: edit scope (empty = the sprint's files), Studio write, subagents (`explore` only), and the lead's `write_access`. The path tester shows what a role may read or edit; it does not know a sprint, so an empty edit scope shows as "workspace" there.
3. **Validate.** Warnings named `hard_deny_overrides` mean a rule you wrote is overridden by a hard deny; `legacy_key` means an old `studio_mcp` value that applies to both Studio categories; `ask_unattended` means a session would wait for you.
4. **Deploy with a note.** The deploy response says which changed keys need a coordinator restart (`restart_needed`).
5. **Watch the coordinator pick it up** (within `[config_pull] interval_seconds`, default 60 s, at a safe point: no live session). The GUI's applied list shows `pending`, then `applied` or `rejected` with the reason. Events: `config_applied`, `config_rejected`, `config_local_drift`, `kilo_config_applied` or `kilo_config_skipped`, `client_request`.
6. **Let the Foundry Client restart Kilo** when it shows the `reload_kilo` request (the new kilo.jsonc agent blocks apply to sessions created after that). A `restart_coordinator` request appears when the revision changed restart-scoped keys; the client does both at a safe point.
7. **Start a sprint and check its session rules** (control center, session detail): the edit allows should be the plan's file list plus `docs/progress/<task>_*`; `kilo.jsonc` and `.env` denied; `task` denied except `explore`.

### What changes after the first deploy

- Sessions get the role rules from the router, not the legacy `client.permissions` alone (those still apply first; the role rules come after and win where they overlap).
- The implementer's edit scope is the sprint's files. A file outside it needs a `SCOPE_REQUEST` (allow-listed paths are granted by the coordinator itself).
- The lead's write mode follows the lead row in the matrix, including its deny lists.
- Subagents follow each role's `task_agents`; `[automation] subagents_allowed` only applies while nothing is deployed.
- kilo.jsonc agent blocks are rewritten on every role-rules change; interactive Kilo sessions see them after the client restarts Kilo. Hand-written blocks of agents the role rules do not name (`plan`, `debug`, `orchestrator`) are left alone.
- A hand edit of `project.toml` survives the next pull as long as the router did not change the same key since the last apply (`config_local_drift` lists what was kept). If both changed, the router wins and the conflict is logged.
- Restart-scoped keys are never applied in place: the live value stays until the client restarts the coordinator, which then applies the cached revision whole.

## Turning it off

Set `[config_pull] enabled = false` in `project.toml`. The coordinator then behaves as before: the TOML alone.
