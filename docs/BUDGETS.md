# Budgets: one shared ledger for every client

Every model call Foundry sends goes through the budget ledger: Kilo, Cline,
OpenWebUI, AnythingLLM, scripts, the agent loop's rounds, retries,
reviews and judges, persona validation. Each call is

1. **reserved** before it is sent: the matching rules are checked and the
   call is recorded in one step, so two parallel helpers can't both take the
   last slot;
2. **settled** afterwards with the input/output tokens the backend reported.
   An error still counts (the backend may have done the work and used
   quota); a connection that never reached the backend is released.

A refusal is HTTP **429** with `code: budget_denied` (or a `paused` reason),
the rule's name, what was used against the limit, and a `Retry-After` /
`reset_seconds` when the reset time is known. Foundry never answers a denial
by trying another provider on its own. A strict persona refuses outright; a
flexible persona follows only the fallback you configured for it (e.g. Cline
Plan's pinned cascade).

## Rules

`budgets.rules` in config.yaml, or Usage Log → Budgets & usage → Edit rules.

```yaml
budgets:
  enabled: true
  retention_days: 90
  rules:
    # default: one frontier helper per subscription at a time (Kilo helper personas only)
    - name: one-frontier-helper
      match: {persona: "Foundry-Kilo-*", paid: true}
      per: account               # one for Meridian's Claude, one for CLIProxyAPI's ChatGPT, ...
      max_concurrent: 1          # calls in flight
      max_active: 1              # distinct helpers active
      active_by: task            # a helper = x-foundry-task, else session, else client+persona
      idle_seconds: 60           # a helper stays active this long after its last call
    # a parent coding session may make 400 calls per 5 hours
    - name: kilo-session
      match: {client: kilo}
      per: session
      max_calls: 400
      window_minutes: 300
    # each helper run (task) gets 30 frontier calls in total
    - name: helper-run
      match: {persona: "Foundry-Kilo-*", paid: true}
      per: task
      max_calls: 30
      window_minutes: 0          # 0 = no window: the whole run
    # the ChatGPT account, shared by every client: 2M tokens a day
    - name: chatgpt-daily
      match: {account: "cliproxy:codex"}
      max_tokens: 2000000
      window_minutes: 1440
    # coordinator sessions (docs/COORDINATOR.md): at most 2 review calls per
    # sprint attempt, and a per-project allowance on top of the shared ones
    - name: review-per-attempt
      match: {role: reviewer}
      per: run
      max_calls: 2
      window_minutes: 0
    - name: animaluprising-day
      match: {project: animaluprising, paid: true}
      max_calls: 60
      window_minutes: 1440
    # try a limit without enforcing it
    - name: cline-plan-watch
      mode: observe
      match: {persona: claude-cline-plan}
      max_calls: 60
```

| Field | Meaning |
|---|---|
| `match` | which calls the rule covers: `backend`, `persona`, `client`, `account`, `model`, `session`, `task`, and for coordinator-bound sessions `project`, `work_task`, `run`, `role` (glob or list of globs), `paid: true/false`. Empty = every call. `persona: ""` matches raw model names (no persona). |
| `per` | one allowance per `client`, `persona`, `session`, `task` (a helper run), `request`, `backend`, `account`, `project`, `work_task` (a sprint), `run` (one sprint attempt) or `role`; omitted = shared by all matching calls |
| `window_minutes` | rolling window for `max_calls` / `max_tokens`; `0` = no window |
| `max_calls` | model rounds, retries included |
| `max_tokens` | input + output tokens. The admission check uses the request's estimated input; the actual tokens are settled after the call. |
| `max_concurrent` | calls in flight at once |
| `max_active`, `active_by`, `idle_seconds` | distinct helpers active at once ("one frontier helper per deliverable") |
| `wait_seconds` | wait this long for a concurrency / active slot before refusing |
| `mode` | `enforce` (default) or `observe`: record "would deny", deny nothing |

**Levels covered:**
- account / subscription: `match.account`, e.g. `cliproxy:codex`,
  `cliproxy:claude`, `meridian:claude`;
- backend: `match.backend`;
- persona: `match.persona` / `per: persona`;
- client: `match.client` / `per: client`, the client key's name;
- parent coding session: `per: session`;
- helper run: `per: task`;
- individual request: `per: request`;
- project (a game registered with the coordinator): `match.project` / `per: project`;
- sprint and sprint attempt: `per: work_task`, `per: run`; agent role: `match.role`.

Project, sprint, run and role come from the coordinator's work bindings
(docs/COORDINATOR.md): Kilo sends its session id, the coordinator registered
that session. Account and backend rules still apply to every project at once,
so two games share one Claude subscription limit.

The existing per-request paid-call cap (`guardrails.max_paid_calls_per_request`),
the backend `call_budget_5h` / `call_budget_daily`, Meridian's conservation
ladder, the CLIProxyAPI 85% usage limit and the image-job budget
(docs/MEDIA.md) all still apply. The ledger adds to them.

### Session and task ids

Per-session, per-task and active-helper limits need the client to say which
session/task a call belongs to. It does this with the `x-foundry-session`,
`x-foundry-task` and `x-foundry-parent-task` headers (docs/CLIENTS.md).
Foundry never guesses them from prompt text. Without them, a helper falls
back to "same client + same persona", which still keeps two different helper
personas from running at once. If Kilo can't send per-helper headers, give
each helper persona its own client key, or rely on that fallback.

## Pause switches

Usage Log → Budgets & usage: **pause** on any account, backend, client or
persona row (or `POST /admin/api/budgets/pause {scope, value, paused, note}`).
A paused scope refuses immediately with a clear message and is never routed
elsewhere. Pausing the account `cliproxy` pauses every account behind that
backend; `cliproxy:codex` pauses only ChatGPT.

## The monitor

Usage Log → **Budgets & usage** (and `GET /admin/api/budgets`):

- **Accounts**: every backend and provider account, showing:
  - **plan usage, provider-reported**: ChatGPT / Claude windows from
    CLIProxyAPI's management API or Meridian's quota, with the reading's age;
    `unknown` when nobody reports it, never 0;
  - **measured**: calls and input/output tokens Foundry itself sent, from
    every client, over 5 hours and 7 days, plus quota errors seen;
  - image jobs, and Foundry's own call caps.
- **In flight now**: calls currently reserved, with persona, backend and age.
- **Rules** and their current use per group, including the helpers active.
- **By client** and **by persona** for the last 24 h: calls, paid calls,
  tokens, errors.
- **Denials**, and "would deny" from observe rules, with the reason.
- Totals: calls, tokens, retries (extra attempts at the same model in one
  request), errors, quota errors, denials.

API-price estimates (the Usage log's `cost` column) are a list-price
comparison only. They are never shown as subscription consumption or as
charges.

## Not covered

- The **routing brain's** own calls go to its endpoint directly, not through
  the pool. They aren't in the ledger; keep the brain local (the default).
- **Hermes agent** runs are counted for the model calls Hermes makes through
  Foundry, not for what it does elsewhere.
