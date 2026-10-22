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
  - webfetch, Studio MCP
  - external_directory
- **Path scopes:** read and edit path globs. Absolute Windows paths are fine, e.g. `C:\Roblox\**`.
- **Shell:** bash allow and deny patterns.
- **Tools and agents:** a subagent allow-list and a deny list for tools.

From the deployed rules, the coordinator generates:

- **Per-session `permission[]` rules**, appended after the adapter's own rules.
- **`agent.<name>.permission` blocks in kilo.jsonc**, written by `kilo-config --apply`. It backs up the file, replaces only those values (comments survive), re-parses the file, and restores the backup on any mismatch.

**Hard denies are always emitted last, and no setting removes them.** They cover:

- reading or editing secrets and `.env` files
- force push
- pushing or deleting tags
- `--no-verify`

## Turning it off

Set `[config_pull] enabled = false` in `project.toml`. The coordinator then behaves as before: the TOML alone.
