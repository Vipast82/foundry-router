# Client keys, scopes and correlation

Give every client its own Foundry key so requests, budgets and the logs know
**who** asked, and so each client only reaches what it should.

```yaml
clients:
  - name: kilo
    key: "${FOUNDRY_KEY_KILO}"
    scopes: [inference, media]        # chat/completions + image jobs
    personas: [Foundry-Kilo-Local, Foundry-Kilo-Sonnet]   # optional limit
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
| `media` | `/v1/images/*`, `/media/*` (image jobs, artifacts) |
| `admin` | `/admin/*` (the web UI's API; the `/ui` page shell itself is static) |

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

## What every response tells the client

`x-request-id`, `x-foundry-client`, `x-foundry-persona`,
`x-foundry-served-model`, `x-foundry-served-backend`, `x-foundry-route-mode`,
`x-foundry-routing` (`strict` / `flexible`).

Refusals use standard error objects with a machine-readable `code`:
`invalid_api_key`, `missing_api_key`, `insufficient_scope`,
`admin_key_required`, `model_not_allowed`, `route_unavailable`,
`budget_denied` (HTTP 429).
