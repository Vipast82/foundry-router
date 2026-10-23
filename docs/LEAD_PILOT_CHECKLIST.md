# Lead sprint manager: controlled live pilot checklist (v0.136.0)

One page for Victor. The full design and activation detail is
docs/LEAD_SPRINT_MANAGER.md; this is only what to do, watch and roll back for
the one backed-up pilot that review item A4 still requires.

## 0. Where the live config actually stands (found 2026-10-23, nothing changed)

The deployed checkout (`C:\Users\shock\foundry-deploy`, v0.135.0) already has
`[lead_manager] enabled = true` (since 2026-10-21) — the A4 review's "off by
default, never piloted" is stale about enablement. It also diverges from the
documented pilot preconditions:

| Live setting | Documented pilot precondition |
|---|---|
| `allow_local_route = true`, local Qwen third in `expected_models` | strict chain, Opus first + Astra only, **no local route** |
| `write_access = true` | lead session read-only in the game workspace |
| `max_lead_calls_per_task = 12`, `max_cases_per_task = 6` | defaults 6 / 3 |
| `full_authority` unset (= true, the v0.125.0 default) | — (your stated policy) |

**Decide before the pilot:** keep these loosened settings (they are your
2026-10-21 choices, per the comments) or tighten back to the documented
preconditions for the first observed case. Either way the pilot is only valid
once the coordinator actually runs a v0.136.0 build — v0.135.0 still resumes
assignments as READY and lacks the BLOCKED -> CHANGES_REQUIRED edge.

## 1. Enable (at a safe point; deploy is yours, never from a dev session)

1. Pause AnimalUprising in the control center, wait for "PAUSED — safe to
   upgrade"; back up `~/.foundry-coordinator/coordinator.sqlite` (+ wal/shm)
   and `evidence_blobs/`; note the pilot task's attempt and session counters.
2. Update the foundry-deploy checkout to the `v0.136.0` tag; restart the
   coordinator with the same profiles and state dir. Heartbeat must show
   v0.136.0 and the version gate "ok".
3. Readiness: `python -m foundry_coordinator doctor` — every lead check green
   (persona enabled + strict, route chain == `expected_models`, Kilo limits,
   `lead` agent). If `supervisor = "supervisor"`, start it as its own process
   (`python -m foundry_coordinator.supervisor --state ... --repo ... 
   --coordinator-cmd ... --loop`); otherwise upgrades wait for you.
4. Pick ONE paused task with a known, real, non-visual blocker; resume it.

## 2. Watch (the first case, end to end)

- **Case flow:** the task's blocker text shows `lead recovery: DIAGNOSING ...`;
  the Lead recovery section shows the case pills and journal.
- **Served model:** the lead session's served model is `claude-opus-5-5` (or a
  listed fallback); a model outside `expected_models` must fail closed
  (DEFERRED / NEEDS_VICTOR, report discarded) — never a silent substitution.
- **Game checkout safety:** no `guard_violation` in the journal; HEAD and
  status unchanged by the lead session itself (with `write_access = true`, any
  lead edits appear only as journaled `lead_commit` rows, in scope, re-verified).
- **Resume shape (new in v0.136.0):** a game assignment resumes the task as
  CHANGES_REQUIRED (a repair round whose transition evidence carries the
  assignment as findings), never as a bare READY.
- **Bounds:** counters stay within their maxima; a repeated blocker ends in one
  precise NEEDS_VICTOR question (or, under full authority, a journaled
  `delegated_round`), not a loop.
- **Gates still decide:** the assignment only re-enters normal verify/review;
  no check passes on the lead's say-so.

## 3. Roll back

- Set `enabled = false` in the live project.toml and restart the coordinator at
  a safe point; disable the `Foundry-Kilo-Lead-Manager` persona in Foundry.
- Stop the supervisor process if it runs.
- The `recovery_cases` / `recovery_actions` / `upgrade_requests` tables stay as
  the audit trail; restore the state-dir backup only if the DB itself is
  suspect.
