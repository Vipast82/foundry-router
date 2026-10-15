# Operations: config history, export/import, diagnostics, queues, breakers

## Configuration history and rollback (Logs → Data)

Every save of `config.yaml` is recorded as a version: from the web UI, an
import, a restore, or an edit of the file itself (noticed at start-up and
recorded as "changed outside Foundry"). Each version lists the top-level
sections it changed.

- **diff**: what changed from that version to the current config, with
  secrets shown as `<redacted>`. `${VAR}` references are shown as written.
- **restore**: writes that version back. Changes to the backend pool, brain,
  guardrails and Meridian apply immediately. Budgets, media, health, clients,
  subscription usage, retention and profiles are read live anyway. Anything
  else (server, MCP servers, agents, the MCP aggregator, registry, semantic
  cache, logging) is listed as **restart to apply**. A restore is itself a
  version, so it can be undone.
- A save that would produce a config Foundry can't load is refused, and
  nothing is written.

### Versioned config format

`config.yaml` carries `config_version`. When Foundry adds a migration (a
renamed or reshaped setting), start-up upgrades the file, keeps the old one as
`config.yaml.v<N>.bak`, and notes the migration in the history. A file from a
newer Foundry is left untouched. The current format is v2 (the version stamp
itself).

## Snapshots

Personas, pause switches, and tool and model overrides live in the database,
not in `config.yaml`. They are snapshotted:
- before every import;
- before a default-profile apply that would change something;
- whenever you press **Take a snapshot now**.

**Restore** puts personas and pauses back exactly as they were, removing
personas created since then; it takes a snapshot of the current state first.

## Export and import

**Export configuration** downloads one JSON bundle: `config.yaml`, personas,
pause switches, and tool and model overrides. Secrets written literally in
`config.yaml` (API keys, tokens, client keys) are exported as `<redacted>`
unless you tick **include secrets**. `${VAR}` references are always kept.

**Import** takes a bundle with a choice of sections: config, personas
(merge = add and update, or only add new ones), and pauses and overrides.
- A `<redacted>` value never overwrites the real value configured here. List
  items such as backends and clients are matched by name.
- An older bundle's config is migrated to the current format.
- A snapshot and a config version are kept, so an import can be undone.

API: `GET /admin/api/export[?secrets=1]`,
`POST /admin/api/import {bundle, sections, persona_mode}`.

## Diagnostic bundle

**Download diagnostic bundle** (Logs → Data, or `GET /admin/api/diagnostics`)
gives a zip to attach to a bug report:
- the redacted config and its version list;
- backend status, functional health, queues and breakers;
- personas and profile status;
- the budget monitor and media summary;
- the last 500 events, plus metadata and timing for the last 300 requests;
- warnings and errors from the log.

Secrets are removed by key name (`api_key`, `token`, `secret`, `password`,
...). Every known secret **value** (config secrets, client keys, secret-looking
environment variables, bearer tokens, `sk-...` keys) is also scrubbed from
every file. Prompts, replies, request summaries, images and media artifacts
are not included. Read it before sharing anyway.

## Per-backend concurrency and fair queuing

```yaml
backend_pool:
  internal:
    backends:
      - name: strata
        type: openai-compatible
        url: http://192.168.0.206:34201/v1
        flavor: llamacpp
        max_concurrent: 1           # = llama.cpp's -np slots
        queue_timeout_seconds: 300
```

With `max_concurrent` set, at most that many calls run on the backend at
once. Further calls wait in Foundry's queue instead of inside llama.cpp,
where they can't be seen. A freed slot goes to the next **client** in turn
(round robin), first come first served within a client, so a burst from one
client (say, Cline re-sending a large context) can't starve another (Kilo).

- Time spent waiting is recorded on the request (`flags.queue_ms`) and shown
  in its trace.
- A call that waits longer than `queue_timeout_seconds` is a failed attempt
  that was never sent: it isn't counted against budgets, and the request
  moves on as its routing allows. A strict persona refuses.
- **Backends → Pool → Can it serve?** shows each backend's load: calls running
  / capacity, how many are waiting per client, the oldest wait, and timeouts.

Without `max_concurrent` nothing changes: the backend queues internally, as
before.

## Circuit breakers

The pool already stops sending to a backend after `failure_threshold`
consecutive failures (health checks or live calls). It skips the backend
for `cooldown_seconds`, then lets the next call through as a trial. The
**Can it serve?** card shows this as a breaker:

| State | Meaning |
|---|---|
| closed | in use |
| open | failing; skipped until the cooldown ends (seconds shown) |
| half-open | cooldown over; the next call is the trial |

**reset** closes it immediately, after you've fixed the backend (or use
`POST /admin/api/backends/{name}/reset-breaker`). Calls that time out while a
backend is busy with a long prompt are not counted as failures.

## Credentials

- API keys are masked in the UI and kept when a form is saved with the
  masked value.
- Versions and diffs are shown redacted, and exports are redacted unless
  asked otherwise.
- The diagnostic bundle is scrubbed by name and by value.
- Prefer `${VAR}` references in `config.yaml` so secrets live in the
  environment (`.env`), not in the file or its history.
