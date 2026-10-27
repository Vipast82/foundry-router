# Proposed amendment — coordinator advancement (for an HK sprint, Victor's approval)

**Status: PROPOSED, not applied.** The coordinator refuses to start any AnimalUprising
sprint until `AGENT_RULES.md` contains the marker `COORDINATOR-ADVANCEMENT` (profile
`[animaluprising] require_rule_amendment = true`). Apply it the normal way: an HK sprint
whose plan lists `AGENT_RULES.md`, `.clinerules`, `docs/rules/CLINERULES_CORE.md` and
`.kilo/rules/animaluprising-adapter.md`, flagged per §17.

Nothing below lets an agent do more in one session. It lets the **coordinator** start the
next approved sprint in a **new** session after the previous one is verified, reviewed and
closed — the step Victor (or the Sprint Manager chat) does by hand today.

---

## §1a Coordinator advancement (COORDINATOR-ADVANCEMENT) — amended YYYY-MM-DD, flagged §17

The one-sprint rule (§1) is unchanged for every session: a session executes exactly one
sprint, then stops. In addition:

1. **Who may advance.** Only the Foundry development coordinator, running an approved
   queue that Victor wrote (`queue approve ... --by victor`), may start the next sprint. It
   does so in a **fresh** Kilo session with no memory of the previous one. An agent never
   starts, chains or "gets a head start on" another sprint, in any session.
2. **When.** Only after the previous sprint's closeout passed: `GATE <ID> PASS` bound to the
   committed code (R-C), `done_check.py <ID>` PASS, a frontier review approving the exact
   commit, the summary/STATE rows written, and `origin/master` equal to HEAD (the
   coordinator pushes it). Visual sprints still end COMPLETE-PENDING-VISUAL-REVIEW and wait
   for the configured visual approver (§19, §20, R-A); the
   coordinator may start the next sprint only where MASTER_ORDER already allows it
   ("DONE, or COMPLETE-PENDING-VISUAL-REVIEW where only Victor's look is missing").
3. **Kickoff = the plan.** The coordinator's kickoff points at `docs/progress/<ID>_PLAN.md`
   (written by the Sprint Manager or `plan_new.py`); it never writes or widens a plan's
   "Files this sprint may change" list. A sprint without a plan is not started.
4. **BUILD and CLOSE.** A BUILD session implements and runs the gate; it never writes the
   summary or the STATE row. After review, a separate CLOSE session does §9 steps 2–3. No
   session pushes, in any phase: the coordinator pushes master after closeout verification
   (2026-10-27), and a track never pushes (its merge publishes). The coordinator's own checks
   replace nothing: §8, §13–§18, §21, §30–§34 apply to both.
5. **Stops.** A session stops with the coordinator's report block: BLOCKED with
   `blocker_kind` `product` (options + recommended; the coordinator decides), `tooling` (no
   options; auto-heal or the lead) or `user_action` (only the owner can do it; no timeout).
   A failed check after the attempt limit, a Foundry budget or pause refusal, or a failed
   push stops that sprint and everything that depends on it. The coordinator does not retry
   a push, a paid review or an image job automatically beyond its configured limits.
6. **Tags** remain the Sprint Manager's (§33). The coordinator never creates or pushes tags.
7. **Audit.** A Cline audit is never evidence about a Kilo session. For a coordinator
   session the coordinator runs the Kilo audit itself and refreshes
   `.plan/tmp/kilo_checkpoint.txt` (read-only, git-ignored) while the session works; the
   session reads it and never runs checkpoint.py / kilo_checkpoint or calls the
   authenticated Kilo server.

## .clinerules / CLINERULES_CORE.md one-liner

> §1a: only the coordinator advances to the next approved sprint, in a fresh session, after
> verified closeout (COORDINATOR-ADVANCEMENT); you still run exactly one sprint and stop.

## .kilo/rules/animaluprising-adapter.md addition

> When the kickoff comes from the coordinator (it names a coordinator run id), end your
> final message with the `coordinator-report` block it asks for. The report is a record, not
> a verdict; the coordinator decides completion from the gate, done_check and the review.
> For §34 checkpoints in a coordinator session read `.plan/tmp/kilo_checkpoint.txt` (the
> coordinator's own audit of this session, refreshed while you work; not checkpoint.py, and
> never a call to the Kilo server on port 4096).
