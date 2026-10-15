# Observability: what served each request, can each backend serve, and how clients compare

## Every request

Each `request_log` row (Logs → Usage Log, data export, the trace view) now
also records:

| Field | Meaning |
|---|---|
| `served_model`, `served_backend` | what actually answered (also sent as `x-foundry-served-model` / `-backend` headers) |
| `routing` | `strict` or `flexible` |
| `attempts` | model calls this request made (agent rounds and retries included), from the budget ledger |
| `retries` | failed attempts that were followed by another attempt |
| `outcome` | `ok`, `error`, `cancelled` (client went away), `timeout` (no reply / stalled), `denied` (budget or strict-route refusal) |
| `flags` | `client_tools` (tools the client sent), `compaction` (a client compaction summary), `tool_calls_out` (tool calls returned to the client) |
| `client_id`, `session_id`, `task_id`, `parent_task_id` | who asked, and the client's correlation ids (docs/CLIENTS.md) |

Each timing sample (`perf_samples`: queue/router time, time to first token,
prefill and decode time and speed, cache hits, finish reason, load time) now
carries the `request_id` and `client_id`, so a request joins its model calls.
Decode tokens/s is generation speed only; it is never mixed with end-to-end
request time.

## Can it serve? (Backends → Pool)

The pool's health check only proves a backend's HTTP server answers. The
**Can it serve?** card shows whether it can actually generate:

| Status | Meaning |
|---|---|
| serving | a generation succeeded in the last `health.fresh_minutes` (15), with no failure after it |
| failing | the latest generation attempt failed |
| reachable, not verified | the HTTP server answers but nothing has generated recently |
| down | the pool's HTTP health check is failing |

Evidence comes from real traffic first (every call is in the budget
ledger), so a busy backend is never probed. An idle backend with no success
for `health.probe_minutes` gets a tiny probe: "reply OK", 16 tokens, thinking
off, on exactly that backend. Local backends are probed automatically;
paid/subscription backends only when you press **probe** (or set
`health.probe_paid: true`), because each probe spends quota. Status changes
to failing/down, and recoveries, go to Events.

```yaml
health:
  probe_minutes: 15      # 0 = never probe automatically
  probe_paid: false
  probe_timeout_seconds: 120
  fresh_minutes: 15
```

The card also shows the routing brain's reachability and the image-job
backlog.

## Routes (Routing → Routes)

A graph generated from the **live configuration**, not a static drawing:
clients → personas (with their route mode and budget rules) → models →
backends.

- **Dotted grey**: what the config allows. A persona's allowlist in pinned
  order (#2, #3… are its explicit fallback chain), a client's persona
  restriction, the backend each model is served by (the pinned backend for
  strict personas; "no meridian backend" when a profile backend doesn't
  exist yet).
- **Blue**: requests in the window (1 h to 7 d); thicker means more.
- **Red**: failed requests. **Red dashed**: budget denials (the rule names
  are on the edge).
- **Purple dashed**: image jobs, drawn separately from text inference.
- **⛁N** on a client or persona: the budget rules that gate it (hover for
  names). The dot shows a persona's validation result or a backend's serving
  status. Disabled personas are greyed.

Click a coloured edge to see the requests behind it: time, client,
session/task, served model and backend, outcome, retries, duration, tokens,
error. Then open any request's **trace**:
- the budget-ledger row for each model call (account, tokens, call time,
  denial reason);
- the timing of each call (time to first token, prefill and decode with
  speeds, cached tokens, finish reason);
- route notes and guardrail events;
- Foundry MCP tool calls.

API: `GET /admin/api/routes?hours=24&all=0`,
`GET /admin/api/routes/edge?kind=&src=&dst=&hours=`,
`GET /admin/api/requests/{request_id}/trace`.

## Compare (Dashboard → Compare)

Kilo vs Cline (or persona vs persona) from Foundry's own records. Every
client goes through the same router, so the numbers are comparable:

- **Volume**: requests, sessions, requests per session.
- **Outcomes**: ok, errors + timeouts, cancelled, denied, retries, longest
  error streak in a session.
- **Speed**: end-to-end duration (median, p90), time to first token, decode
  tokens/s, kept separate.
- **Usage**: tokens, model calls, paid (frontier) calls and frontier calls
  per session, compactions, context per session.
- **Tool loop**: tool calls returned to the client, and immediately repeated
  tool calls.

Foundry can't see human interventions, failed file edits, acceptance-gate
results, time to a verified result or reopened issues. Those need client-side
telemetry, and the report lists them as not observable instead of guessing.
Sessions come from the `x-foundry-session` header; a client that doesn't send
it shows no sessions.
