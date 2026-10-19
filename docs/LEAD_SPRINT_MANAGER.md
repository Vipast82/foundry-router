# Lead sprint manager: last automated recovery tier

Status (v0.123 development branch): implemented and wired, **off by default**
(`[lead_manager] enabled = false`). With it off the engine behaves exactly as
before (FAILED / BLOCKED as today, no lead tables created, `engine.lead` is
None). It has only been exercised against fakes (tests/coordinator/
test_lead_e2e.py); the controlled live pilot below has not happened yet. Do not
describe the `retry_guidance` adviser as this tier.

What is implemented:

- `foundry_coordinator/lead.py` (`LeadManager`): readiness (persona enabled and
  strict, route chain == `expected_models`, no local route, Kilo limits/filters,
  lead agent), cases idempotent per fingerprint, the bounded redacted dossier,
  the case state machine (DIAGNOSING, REPAIRING, UPGRADING, RESUMING, DEFERRED,
  NEEDS_VICTOR), read-only lead permissions in the game workspace, served-model
  verification after every call (mismatch aborts, never a fallback), journaled
  `recovery_actions`, restart reconciliation (an uncertain prompt is never
  resent), operator pause / takeover / instructions.
- `foundry_coordinator/repair.py` (`RepairWorkspace`): isolated Foundry-Router
  worktrees on `lead/<case>`, dirty-scope and test-expectation refusal, a
  reproducing test (fails at base, passes at the candidate), focused/broader
  pytest runs, an independent Foundry review and the manifest.
- `foundry_coordinator/supervisor.py`: the out-of-process upgrade step machine
  (waiting_safe_point -> verifying -> stopping -> backing_up -> installing ->
  starting -> checking -> resuming, with ALL projects paused) with rollback of
  the build and the DB. There is no `git reset --hard`: a rollback uses
  `git reset --keep`, and only when HEAD is still the candidate.
- Engine wiring (`engine.py`): `engine.lead` is built lazily, only when a
  project enables it. Every final stop (`_block`; the FAILED budget stops in
  `_start_implementation` / `_lc_budget`; review budget, invalid reviews and
  review errors; a reviewer BLOCKED verdict; required checks that could not
  run; a session's explicit BLOCKED / NEEDS_HUMAN report; a non-automatic
  readiness block) goes through `Coordinator._stop`: deterministic recovery
  first (evidence recover, the lead's route/quota readiness refresh), then
  `lead.open_case` and BLOCKED with `data.recovery_case` and blocker text
  `lead recovery: <state> ...` (no new task states). A no_progress /
  recovery_escalation decision wait first runs the automatic partial-BUILD
  reevaluation; only a question that survives it becomes a case (an operator
  answer to that original question still wins and cancels the case). BLOCKED
  tasks with an active case get one bounded `lead.step` per tick, honour a
  project pause, an operator pause/takeover and an unblock, and resume from
  `lead.resume_action`: a game assignment becomes the primary's repair findings
  (marked as the lead's untrusted advice) with up to `extra_attempts` granted
  under the delegated `[lead_manager]` policy (budget-ledger
  `authorized:attempt_ordinary` / `authorized:sessions` rows referencing
  `unblock:lead:<case>`, journaled on the case), reverify and an installed
  upgrade re-verify the same head, a cleared wait retries, and Victor's answer
  to a `lead_escalation` decision becomes his instruction (or stops the task).
  Each resume transition carries `{recovery_case, action}` and resolves the case.
- Control center: the bridge's `lead` task field, case controls
  (`lead_case_pause` / `lead_case_takeover` / `lead_case_instruct`) and the
  `lead_manager` heartbeat feature, gated on `engine.lead` and a project
  enabling it.

Known gaps (see the integrator report): the lead queues upgrade requests with a
list journal the supervisor cannot read (the engine normalizes a `requested`
row until lead.py queues through `supervisor.request_upgrade`); the supervisor
moves the case to RESUMING / DIAGNOSING itself, bypassing the lead's
`upgrade_result` journal row and its rollback-once rule (bounded instead by
`max_upgrades` / `max_candidates`); lead session rows are never ended; and
states.py has no BLOCKED -> CHANGES_REQUIRED edge, so a game assignment resumes
as READY with the assignment as the repair findings.

## Goal and authority

Before an approved sprint asks Victor to resolve a blocker, a frontier lead
manager investigates the actual coordinator state, workspace, tests, logs,
Foundry route/quota state and prior attempts. It can assign bounded repairs and,
when necessary, repair Foundry-Router/coordinator code in a separate checkout.
It may edit, delete and commit files needed for an approved repair, run tests,
stage a safe upgrade and resume the same sprint. Victor receives a question only
for genuinely external or ambiguous input, a change that conflicts with the
approved core-game contract, or a recovery that exhausted its explicit limits.
Victor can pause or redirect it at any time.

The lead's authority is a project policy capability, not a model impersonating
`operator`. Each action must identify its source decision, input revision,
allowed repository and files, and verification result. A model conclusion alone
cannot mark a test PASS, answer an undelegated decision or suppress a gate.

## Full delegated recovery authority (v0.125.0, default when enabled)

Victor's policy: an enabled lead manager is the final autonomous decision
maker for routine recovery (`[lead_manager] full_authority = true`, the
default). The lead's Kilo session is still read-only, so it never writes
itself. It directs an `operate` action that the coordinator executes as the
single writer of the game checkout: the task is BLOCKED, no primary session
runs, and the workspace lock is held for the whole step.

Operations:
- `clean_untracked`: generated scratch files only. A file must be untracked,
  outside the task scope, absent when the task began, not an acceptance test
  and not a secret. Each one is backed up under
  `<state>/lead_backups/<case>/`, verified, then removed.
- `extend_budget`: authorized ledger rows referencing `unblock:lead:<case>`.
- `resolve_decision`: routine decisions, under the `lead_policy` authority as
  `lead_manager:<case>`. Scope, approval, expectation, quarantine, policy
  decisions and the lead's own escalations stay with Victor.
- `raise_bound`.
- `approve_test_change`: a recorded `lead_test_change` decision. The change
  must still be made and its tests re-run.

Every operation is one journaled `lead_op` row with actor, case, source
revision, paths, result and validation. It is idempotent by op key, so a
crash resumes where it stopped. Any refused or failed operation sends the case
back to DIAGNOSING, where a repeat of the same action is rejected. Only when
every operation has verified does the follow-up run:
- `assign`: the primary gets an assignment;
- `reverify`: the same head is verified again;
- `resume`: the task goes back to READY.

Bounds no longer end in a question for Victor:
- **Exhausted counters.** An exhausted counter (diagnoses, candidates,
  quota calls, case time, cases, lead calls or extra attempts per task) becomes
  a journaled `delegated_round`. The next lead action must be materially
  different from every earlier action of the task (`action_fp`).
- **Runaway guard.** `max_delegated_rounds` (12) is the only stop. When it
  stops a case, Victor gets the blocker and the actions tried, not a counter.
- **New concrete failures.** A failure such as a new untracked scratch file is
  part of the fingerprint. A blocker the task has not had before opens its
  own case regardless of earlier, exhausted cases.
- **Legacy escalations.** A NEEDS_VICTOR case that a counter alone escalated
  before this release is taken back by the lead: its question is withdrawn,
  with a journal entry, and a delegated round starts.

`ask_victor` is accepted only for input Victor must give personally
(`user_action`), or for a scope_core/external change to the core game goal.

Unchanged:
- No model can mark a check PASS, create evidence, approve visuals or
  suppress a gate. The engine's checks and reviews still decide.
- Served-model, permission and guard failures still fail closed.

`full_authority = false` restores the strict behaviour of v0.123/v0.124.

## Required topology

1. **Engine** owns task state and remains the sole AnimalUprising writer. On a
   proposed `BLOCKED` or `FAILED` transition, it persists a `recovery_case`
   first and gives the case to an independent supervisor. It does not repeatedly
   create the same case on every tick.
2. **Lead manager** runs as a separate, tool-capable frontier session. Its
   initial dossier has the exact task/run/revision, decision and transition
   evidence, failed checks with provenance, BUILD obligations and plan state,
   worktree status, commit range, related source and tests, model/route quota,
   previous strategies and outcomes. It can request additional bounded reads;
   the current `retry_guidance` packet and no-tool call are insufficient.
3. **Repair worker** gets an isolated Foundry-Router worktree for a diagnosed
   coordinator defect; a game defect stays with the approved game primary. The
   lead may request coding help and independent review, but helpers cannot
   inherit its authority or write the active game checkout.
4. **Upgrade supervisor** is outside the coordinator process. With ALL
   projects paused it waits for a safe point, verifies tests and release
   identity, stops the old coordinator, backs up SQLite and evidence blobs,
   installs the candidate, restarts it, runs readiness and state-migration
   checks, then resumes the original task. If health checks fail, it restores
   the prior build (`git reset --keep`, only while HEAD is the candidate; never
   `reset --hard`) and state. The coordinator cannot reliably restart and
   validate itself.

   Review A hardening (v0.123): a blocker's repeat key ignores HEAD, so repeat
   cases count resolved and cancelled ones and per-task caps
   (`max_cases_per_task` 3, `max_lead_calls_per_task` 6,
   `max_extra_attempts_per_task` 2) bound a game-repair loop; repair sessions
   may run only the venv `python -m pytest` and read-only git; every lead /
   repair session holds the workspace lock and is checked against before /
   after snapshots of the game, the router checkout and the task's
   coordinator rows; UPGRADING (`max_upgrade_minutes` 120) and RESUMING
   (`max_resuming_minutes` 60) time out to Victor; operator pause time is off
   the case clock (`lead_case_resume` unpauses).

## Decision sequence

1. Run deterministic checks first: refresh quota/readiness, recover evidence,
   compare current HEAD and open BUILD steps, and reevaluate stale decisions.
   A proven partial BUILD continuation resumes automatically without spending
   an attempt. This includes the UI-02 false `recovery_escalation` case.
2. If still blocked, create a case fingerprint from task, failure class,
   revision, obligation and check ids, and exact input hashes. Gather a fresh
   dossier. The lead classifies `product`, `test/harness`, `evidence`,
   `environment`, `provider/quota`, `coordinator`, `scope/core` or `external`.
3. For an in-scope game repair, hand a concrete assignment to the sole primary,
   with an additional bounded budget only under the approved policy. Preserve
   the normal gate, review and acceptance checks.
4. For a coordinator defect, work in the isolated checkout; require a
   reproducing regression test, focused and relevant broader tests, independent
   review, and a manifest naming modified files and migrations. Supervisor
   deploys only at a safe point and verifies the original blocker disappears.
5. If the same fingerprint recurs after a deployed fix, return the new evidence
   to the lead once. Cap diagnosis calls, code candidates, upgrades and total
   elapsed time independently. The lead must identify a materially different
   hypothesis; no generic retry or budget reset loop.
6. Only then open a Victor decision with the dossier, attempted repairs,
   remaining uncertainty and a precise question. Mark the case `needs_human`
   only when a specific user-only action is named.

## Durable case record and control center

Add `recovery_cases` and `recovery_actions` tables (or equivalent journaled
records) with case id, task/run, trigger, source revision, fingerprint, dossier
hash, lead session/model, classification, proposed action, validation, upgrade
identity, rollback result and outcome. Store source excerpts by hash and redact
secrets before inference or display. Show a distinct `LEAD_DIAGNOSING`,
`REPAIRING`, `UPGRADING`, `RESUMING` or `NEEDS_VICTOR` state in the control
center, with the active lead session and its tools, helpers, tests and alerts.
Offer pause/takeover and a way for Victor to add instructions to the case.

## Verification before rollout

Use fake Kilo, Foundry, Git and supervisor processes for end-to-end tests:
false UI-02 stall auto-resumes; true stall invokes lead; code defect gets an
isolated patch, review, safe upgrade, health check and continuation; failed
tests or dirty scope reject a patch; restart/crash does not repeat a paid call or
half-apply a migration; rollback restores the prior build; quota defer waits;
lead timeout or repeated failed candidates reaches Victor with a useful
question. Then run a controlled live pilot on a paused, backed-up sprint.

The current running worker must not load development-branch code mid-session.

## Deployment, activation and the live pilot (v0.123.0)

### Deployment order (each at a safe point, nothing hot-reloaded)

1. **Foundry server.** It currently reports v0.122.0, so this also brings it
   up to date with v0.122.1–v0.122.6: pinned fallback routes, pause support,
   quota fixes, the new `/served` and `/personas/{name}/routes` endpoints and
   the `lead_manager` purpose.

   `git pull origin main && docker compose up -d --build`

   Check: the control center shows Foundry v0.123.0.
2. **Coordinator (workstation).**
   1. Pause AnimalUprising in the control center (at a safe point) and wait
      for "PAUSED — safe to upgrade".
   2. Back up `~/.foundry-coordinator/coordinator.sqlite` (+ `-wal`/`-shm`)
      and `evidence_blobs/`.
   3. Stop the coordinator, pull, and start it with the same profiles and
      state directory.
   4. Leave `[lead_manager] enabled = false`.

   Check: the heartbeat shows coordinator v0.123.0 and the version gate reads
   "ok".
3. **Resume** the project.

Behavior is unchanged until you enable the lead.

### Activation checklist (all must pass before `enabled = true`)

- **Foundry persona `Foundry-Kilo-Lead-Manager`:**
  - enabled;
  - routing strict;
  - model chain exactly Opus 5.5 first, then the Astra model (no local route);
  - granted to the Kilo worker client key.
  - Verify with `GET /v1/foundry/personas/Foundry-Kilo-Lead-Manager/routes?client=kilo`
    (orchestration key): it must return `enabled: true`, `strict: true`,
    `client_allowed: true`, the two routes in order, and no `source: local`.
- **Kilo (`kilo.jsonc`):**
  - `provider.foundry.models["Foundry-Kilo-Lead-Manager"]` with
    `limit.context` / `limit.output`;
  - the persona is not excluded by whitelist or blacklist;
  - an agent named `lead` (read-only tools) exists.
- **Profile (local `project.toml`):**

  ```toml
  [lead_manager]
  enabled = true
  persona = "Foundry-Kilo-Lead-Manager"
  expected_models = ["claude-opus-5-5", "<Astra model id exactly as /routes reports it>"]
  kilo_client = "kilo"
  supervisor = "supervisor"     # or "manual"
  ```

  Keep `role_overrides.ui` (Sonnet stays the UI implementer).
- **Readiness:** `python -m foundry_coordinator doctor` (and the heartbeat's
  `lead_ready`) shows every lead check passing.
- **Supervisor (only if `supervisor = "supervisor"`)** runs as its own
  process:

  `python -m foundry_coordinator.supervisor --state ~/.foundry-coordinator --repo <Foundry-Router checkout> --coordinator-cmd "<your coordinator start command>" --loop`

### One backed-up live pilot

1. Choose a paused task at a safe point, ideally one with a known, real,
   non-visual blocker. Back up the state directory and note the task's
   attempt and session counters.
2. Enable the lead for the project and resume. The first stop should show
   the lead recovery section going to DIAGNOSING.
3. Verify a real Opus-first recovery:
   - The lead session's served model (Lead recovery section → served model,
     or `GET /v1/foundry/work/sessions/<lead session>/served`) is
     `claude-opus-5-5` on the expected backend.
   - The game checkout's HEAD and status are unchanged by the lead session
     (journal: no `guard_violation`).
   - The lead's action is journaled with revision, scope and result.
   - Gates still decide: a lead assignment only re-enters normal verify and
     review.
4. Verify the Astra fallback without touching game state:
   - With the task paused, make Opus unavailable to Foundry for a moment,
     e.g. cool down or disable that route in Foundry.
   - Readiness should report the chain as an ordered subset (a transient
     DEFERRED, not a configuration escalation).
   - When a call is made on the fallback, `/served` shows the Astra model and
     the check passes because it is in `expected_models`.
   - A model NOT in `expected_models` must fail closed: the case is DEFERRED
     or NEEDS_VICTOR and no report is used.
   - Restore the Opus route afterwards.
5. Confirm bounds: the case counters stay within their maxima, and a repeated
   blocker reaches one precise NEEDS_VICTOR question instead of looping.
6. Roll back if needed:
   - set `enabled = false` and restart the coordinator at a safe point;
   - disable the persona in Foundry;
   - the recovery tables stay for the audit trail.

### Remaining risks (not proven live)

- **Kilo enforcement.** The read-only and repair shells rely on Kilo
  permission enforcement plus the mechanical before/after snapshots. A
  command shape Kilo does not match could still run, but any change it makes
  is detected after the session.
- **Code under test.** Lead-written test code runs in-process during repair
  validation, with a scrubbed environment but no OS sandbox.
- **Supervisor process control.** It is tested with fake processes only.
  Graceful stop needs a v0.123.0 coordinator writing the new pid and stop
  files; an older coordinator is force-stopped only if the supervisor can
  verify its identity.
- **Astra model id.** It must be configured exactly as Foundry reports it.
  Until then, readiness refuses activation.
