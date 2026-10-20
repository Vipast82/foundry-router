# Client keys, scopes and correlation

Give every client its own Foundry key so requests, budgets and the logs know
**who** asked, and so each client only reaches what it should.

## Managing clients in the web UI (recommended)

**Access → Clients & keys** does all of this without editing files:

- **Add a client.** Pick a preset (Cline, Kilo, coordinator, Open WebUI,
  training pipeline, admin) or custom, then set its name, scopes, an optional
  persona restriction and a note. Foundry generates a key (`fnd_…`) and shows
  it **once**, with the exact steps to connect that program. Only a SHA-256
  hash is stored in config.yaml, so the key never appears in config history
  or export bundles.
- **Edit** scopes, personas, note or enabled; **New key** rotates (the old key
  stops working at once); **Delete**. The name is the client's identifier in
  logs, budget rules (`match: {client: …}`) and the Work view, so it isn't
  renamed.
- **Access** settings:
  - requests without a key: allowed or refused (`mode`);
  - keys that match no client: refuse or let through (`unknown_keys`);
  - protect the web UI (`protect_admin`, refused until this browser holds an
    admin key, so you can't lock yourself out).
- **Keys that matched no client.** Programs sending a key Foundry doesn't
  know, by fingerprint (never the key), with what it looks like
  (placeholder, OpenAI-style, old Foundry key…), path, address and user
  agent.

**Access → Helpers & limits** sets each backend's `helpers_allowed`,
`max_concurrent` and `queue_timeout_seconds` from a form, with the sessions
holding slots right now.

### "unknown Foundry client key"

In `optional` mode a program that sends **no** key still works (as
`anonymous`). A program that sends a key matching no client is refused once
any client exists. Cline's OpenAI-compatible provider always sends whatever
is in its API Key field, so after you add your first client (e.g. for Kilo),
Cline's placeholder key starts being refused. Fix it by making a client for
Cline and pasting its key into Cline. While you roll keys out, you can
instead set "keys that match no client" to *let through as anonymous*.

Keys written in config.yaml by hand (below) keep working alongside managed
ones; "New key" on such a client converts it to a managed key.

```yaml
clients:
  - name: kilo
    key: "${FOUNDRY_KEY_KILO}"
    scopes: [inference, media]        # chat/completions + image jobs
    personas: [Foundry-Kilo-Local, Foundry-Kilo-Review]   # optional limit (exact ids)
  - name: coordinator
    key: "${FOUNDRY_KEY_COORDINATOR}"
    scopes: [inference, orchestration]
    personas: [Foundry-Kilo-Review, Foundry-Kilo-Deep-Review]   # reviewer roles only
  - name: cline
    key: "${FOUNDRY_KEY_CLINE}"
  - name: openwebui
    key: "${FOUNDRY_KEY_OPENWEBUI}"
  - name: admin
    key: "${FOUNDRY_KEY_ADMIN}"
    scopes: [inference, media, admin]

client_auth:
  mode: optional        # optional | required
  protect_admin: false  # true: the web UI/API needs an admin-scoped key
```

| Scope | Reaches |
|---|---|
| `inference` | Ollama `/api/*` and OpenAI `/v1/*` chat, completions, responses, embeddings, model lists |
| `media` | `/v1/images/*`, `/media/*`, `/mcp/media` (image jobs, artifacts; see docs/MEDIA.md) |
| `admin` | `/admin/*` (the web UI's API; the `/ui` page shell itself is static) |
| `orchestration` | `/v1/foundry/work/*`: a development coordinator's session bindings, task state and events (docs/COORDINATOR.md) |

- **Sending the key:** `Authorization: Bearer <key>` (OpenAI clients: Kilo,
  Cline's OpenAI Compatible provider, OpenWebUI, AnythingLLM), `x-api-key`, or
  `x-foundry-key`.
- **`mode: optional`** (default): keyless requests still work as client
  `anonymous`, so existing clients keep running while you hand out keys. A key
  that matches no client (a typo, a disabled client) is always refused.
- **`mode: required`**: inference and media need a valid key. Switch once every
  client has one. The liveness endpoints (`/`, `/api/version`) stay open.
- **`personas:`** limits a client to those personas: `/v1/models` and
  `/api/tags` list only them, and any other persona or raw backend model name
  is refused with `model_not_allowed`.
- **`protect_admin: true`**: the web UI asks once for an admin key and keeps it
  in the browser.
- Hermes agent caller tokens keep working (client `agent:<name>`).

## Correlation ids

A client can tag requests so a coding session and its helper runs can be
followed end to end; Foundry records them with the client id in the request
log. They are never guessed from prompt text.

| Header | Meaning |
|---|---|
| `x-foundry-session` | the coding session (e.g. one Kilo task) |
| `x-foundry-task` | this helper run / sub-task |
| `x-foundry-parent-task` | the task that started it |

Without `x-foundry-session`, the client's own session header is used:
**Kilo and OpenCode send `x-session-id`** (their session id) on every model
call, so Kilo traffic is grouped per session with no client change.

### Work bindings (development coordinator)

A coordinator that starts coding sessions registers each one with
`POST /v1/foundry/work/sessions {session_id, project_id, task_id, run_id,
role, parent_session_id, persona}` (client scope **`orchestration`**). From
then on every call carrying that session id is recorded, budgeted and traced
with its project, coordinator task (sprint), run (attempt) and role, and its
parent session as `parent_task_id`. A session id stays bound to one project
(re-binding it to another is refused, 409). See docs/COORDINATOR.md.

### Refusal categories

Foundry's own refusals carry a machine-readable category, so an automated
client can decide without parsing the message: `category` in Ollama-style
bodies, `error.foundry_category` in OpenAI-style ones.

| Category | Codes | What to do |
|---|---|---|
| `budget` | `budget_denied` | wait for `reset_seconds` / `Retry-After` |
| `pause` | `paused` | wait for an operator to resume the pause switch |
| `route` | `route_unavailable`, `queue_timeout` | retry later, or report a blocker |
| `config` | `persona_disabled`, `image_persona`, `model_not_allowed` | needs a person |
| `auth` | `invalid_api_key`, `missing_api_key`, `insufficient_scope` | fix the key |

A strict persona whose only route is paused now answers `paused`, not
`budget_denied`.

## What every response tells the client

`x-request-id`, `x-foundry-client`, `x-foundry-persona`,
`x-foundry-served-model`, `x-foundry-served-backend`, `x-foundry-route-mode`,
`x-foundry-routing` (`strict` / `flexible`).

Refusals use standard error objects with a machine-readable `code`:
`invalid_api_key`, `missing_api_key`, `insufficient_scope`,
`admin_key_required`, `model_not_allowed`, `route_unavailable`,
`budget_denied` (HTTP 429).
