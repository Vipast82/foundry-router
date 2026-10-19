# Lead manager: full delegated recovery authority by default

Victor's policy: when the lead manager is enabled for an approved sprint, it is
the final autonomous decision maker for recovery. It should be able to perform
every operational action an operator could perform to finish that sprint:
inspect files and logs, edit or remove files, run tests and Studio workflows,
change coordinator or Foundry-Router configuration and code, commit, restart at
a safe point, reset or extend budgets, resolve routine decisions, and resume
work. Do not send routine recovery decisions to Victor merely because a fixed
case, diagnosis, attempt, or session counter was reached.

This is a code change, not a Foundry key allowlist change. The current lead
session is hard-coded read-only in `foundry_coordinator/lead.py`, its report
schema rejects approval/answer/budget actions, and its case caps can escalate
before Opus is called. The UI-02 case `rc-db6eef8a7a7e7f2f` demonstrated this:
`max_cases_per_task = 6` stopped diagnosis at seven cases, while the immediate
verification failure was a new untracked scratch file,
`.plan/eFullVerify_call.lua`, outside the sprint scope. The checkout was at
v0.124.1 while the running coordinator heartbeat still reported v0.124.0.

Implement the following end to end:

1. Give the enabled lead full delegated operational authority by default, with
   explicit, journaled actions exposed through the coordinator/supervisor. A
   profile-only or Kilo-only permission change does not meet this requirement.
   The lead must be able to clean generated scratch files, change approved
   configuration, reset budgets, authorize further bounded recovery, repair
   coordinator code in an isolated checkout, deploy at a safe point, and resume
   the sprint without an operator round trip.
2. Let the lead resolve routine task decisions and override exhausted recovery
   counters with a recorded reason and fresh strategy. Escalate to Victor only
   for input that requires him personally or for a genuine change to the core
   game goal that the lead cannot reconcile. Never present a counter limit alone
   as a user-only decision.
3. Preserve one writer for the active game checkout. Give the lead exclusive
   ownership during direct edits, or have it direct a coordinator-executed
   action. Each action records actor, case, source revision, paths, result, and
   validation. Resume the primary only after the action is complete.
4. Keep verification factual. Full authority allows running and repairing
   tests, changing test expectations with an explicit recorded decision, and
   approving visuals, but never fabricating a PASS, image inspection, or gate.
   Re-run affected checks after a change.
5. Reclassify a new concrete failure such as an untracked generated scratch file
   independently of repeated prior findings. A new failure must receive a
   repair opportunity even if previous lead cases were exhausted. Avoid an
   unbounded retry loop by requiring a materially different action and checking
   its result, not by reflexively asking Victor when a number is reached.
6. Add regression tests for the UI-02 case, complete action journaling, safe
   restart and recovery after interruption, budget extension, decision handling,
   single-writer ownership, and truthful post-repair verification. Run focused
   and coordinator suites, then a controlled live pilot with a state backup.

Do not describe this as deployed merely because the profile or model persona
was changed. Report the exact version/commit loaded by the live coordinator and
show a real lead action that resolves a block without Victor's intervention.
