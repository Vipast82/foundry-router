# Lead sprint manager: last automated recovery tier

Status: design for implementation. The automatic partial-BUILD reevaluation in
`engine.py` is implemented on the current development branch; the lead manager,
repository repair worker and deployment supervisor described below are **not**
running yet. Do not describe the current `retry_guidance` adviser as this tier.

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
4. **Upgrade supervisor** is outside the coordinator process. It can wait for
   the single writer to reach a safe point, back up SQLite and evidence blobs,
   verify tests and release identity, stop the old coordinator, install the
   candidate, restart it, run readiness and state-migration checks, then resume
   the original task. If health checks fail, it restores the prior build and
   state. The coordinator cannot reliably restart and validate itself.

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
