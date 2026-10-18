# Helper utilization repair (v0.121.5)

The UI-01 audit found zero new helper starts after model access was repaired.
Old failures consumed a lane's lifetime allowance, failed work could coalesce
across runs, Studio evaluation errors were missed, and a source-reading command
was misclassified as a failed test.

## Behavior

- Coalescing is scoped to the current run and a stable coalescing key. Active
  work and valid structured results are reused; failed, cancelled, rejected or
  stale results are not. An additive `coalesce_key` column preserves old rows
  and the existing responsibility uniqueness constraint. No history is deleted.
- Replacements are bounded by `1 + lanes.max_retries` per assignment/run.
  Per-lane creation allowances are run-scoped (`max_per_run`, falling back to
  the legacy per-lane `max_per_task`). Global `max_lane_units_per_task` and
  `max_dispatches_per_task` still cover the task across runs. Coalescing does
  not refresh a helper's execution-timeout timestamp.
- A review checkpoint is recorded only after a structured result names its
  assignment and source snapshot. A queued/running review coalesces duplicate
  checkpoint events without being labelled successfully reviewed. Legacy
  scheduled-only markers cannot suppress a replacement review.
- Real Studio evaluation tool errors and structured `ok:false`/`isError:true`
  envelopes trigger triage. Reading/searching a test filename does not. Test
  executions piped to a filter still count; successful Studio negative-control
  output is not treated as a tool failure just because it prints FAIL.
- `helpers.backend_max_running` optionally caps coordinator-managed helper
  units per backend. `lanes.high_value_lanes` explicitly makes selected valuable
  lanes eligible for selective fallback; Meridian remains first. Model-written
  ordinary requests cannot elevate themselves. Foundry remains authoritative
  for all actual routes, account quotas and backend capacity, including direct
  visual calls and required agents.

## Workstation activation

AnimalUprising uses six helpers overall, at most three per named frontier
backend, and local remains one call with zero helpers. Failure triage, test
design, incremental review and code proposals are eligible for selective
CLIProxyAPI fallback. Per-lane running limits still apply. Task-wide lane
creation/dispatch ceilings are 48/32, not an instruction to fill them.

`Proactive.recover_current` is an explicit paused-task maintenance action. It
journals one recovery per run and rebuilds only current test design and code
review from current snapshots. Historical failure/API events are not replayed;
a transcript floor prevents newly added classifiers from backfilling obsolete
errors. Same-session continuation preserves primary context and run identity.

The existing Foundry server API is compatible; no live server restart is
required. Workstation source must be restarted to load these changes. The
image adapter path normalization is a separate Windows fix tested against
its existing tests. Persistent image settings are installed during maintenance,
with encrypted media-only credentials and unapproved-only staging.

## Validation and boundaries

The final focused core/queue/regression suite passed 49 tests and the surrounding
visual/advisory/lifecycle/control suite passed 50. These use fakes, not real
models. Production activation must separately record real helper startup,
loaded skills, useful output and delivery/acknowledgement. Connectivity alone
does not establish useful cooperation.

The 70%-used cutoff and freshness checks, read-only helpers, single writer,
independent final review, sprint scope, assertions and game gates remain in
force. This patch does not fix every previously documented v0.121.4 defect,
including lost helper HTTP responses and compaction continuation. It does not
grant automatic product decisions, art approval or a new sprint.
