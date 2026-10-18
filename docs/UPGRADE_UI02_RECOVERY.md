# Deploying v0.122.5 and recovering UI-02

This applies when AnimalUprising is paused pending the update. Use only supported controls. Never edit the coordinator database by hand.

## Follow-up: UI-02's next partial-BUILD block

After the v0.122.6 restart, UI-02 committed additional code at `6ae11a3` but
left its in-scope `UI-02_BASELINE.md` edit and `hud_desktop.png` capture
uncommitted. The partial-BUILD clean check allowed only task gate JSONs, so it
sent the open checklist to final verification and opened a repeat-strategy
decision. The workstation follow-up permits task-owned uncommitted files in a
*committed partial-BUILD continuation* and names them in the next worker prompt.
The carry-forward prompt does not classify those files as a failed repair or
reopen historical repeat-strategy findings.
Final verification and closeout still require a clean tree; pre-existing or
out-of-scope dirty files still block. `decision reevaluate` also accepts a
`recovery_escalation` decision **only** when that exact partial-BUILD
continuation passes its safety checks. Dry-run first, then apply; no attempt or
session counters are reset.

## 0. Before you start

- AnimalUprising should already be paused (control center, **Pause**). Wait for the header to read **"PAUSED — safe to upgrade"**, meaning no active writer turn, helper, review or retry-guidance call.
  - A task's workspace reservation does not need to clear.
  - If you paused through the CLI or an older build, check the control center: no session should be working.
- Back up the coordinator state, with the coordinator still running and paused:
  - `~/.foundry-coordinator/coordinator.sqlite` (plus its `-wal` and `-shm`);
  - `~/.foundry-coordinator/evidence_blobs/`.

  For example, copy them to `config/workstation/UI02_BEFORE_V01225_<epoch>.sqlite`.

## 1. Deploy (in this order)

1. **Foundry server:**

   ```
   git pull origin main
   docker compose up -d --build
   ```

   This restarts the router and interrupts in-flight calls. That's safe now, because nothing is running while paused.
2. **Coordinator (workstation).** Stop the coordinator process, `git pull` (or check out the release tag), and start it again with the same profile and state directory.
   - Do not hot-reload a running worker.
   - Your local `project.toml` (git-ignored since v0.122.6) stays as it is.
3. **Check the restart:**
   - The control center's source pill shows coordinator **0.122.5** and the version gate reads **ok**.
   - The project still shows **PAUSED**: a pause survives a restart.
   - If you paused with "Resume automatically when the coordinator restarts on a newer version", the coordinator resumes the project-paused tasks by itself on its first poll, and the header shows "resumed after upgrade to v0.122.5". Otherwise it stays paused until step 3.

## 2. Re-evaluate the UI-02 decision (before resuming)

The blocking decision `dec-8cdd05214b919060` ("no progress across sessions") was a false positive:

- `[~]` steps 4–6 were invisible to the checklist parser.
- Three untracked task-owned gate JSON files failed the partial-BUILD clean check.

Re-evaluate it with the fixed logic. Do not delete it.

Run these with the same profiles and state directory as the running coordinator: the same `--profiles` / `--state` flags, or `FOUNDRY_COORD_PROFILES` / `FOUNDRY_COORD_STATE`. It is safe while the paused coordinator is running.

1. **Dry run.** This changes nothing and prints the recomputed progress:

   ```
   python -m foundry_coordinator decision reevaluate dec-8cdd05214b919060 --by victor --reason "reevaluate: false no-progress (checklist [~] + intermediate gate outputs), fixed in v0.122.5" --dry-run
   ```

   Expect `withdrawn: true` and progress recognized: commits plus checklist steps advancing.
2. **If the dry run confirms progress, run it again without `--dry-run`.** That:
   - marks the decision **withdrawn** (it is kept);
   - records a `decision_reevaluated` event with actor, reason, the original evidence and the new result;
   - switches the stale final-verification findings to a BUILD continuation;
   - leaves attempts, sessions and the budget ledger untouched.
3. **If the dry run reports no progress**, the decision stays open and nothing changes. Answer it in the control center's Decisions inbox (for example "continue BUILD: steps 4–6 partly done, step 8 open") instead. That answer is recorded with operator authority.

The three untracked gate files (`docs/progress/UI-02_gate_expect.json`, `UI-02_gate_expect_full.json`, `UI-02_gate_verdicts.json`) are now reported as task-owned intermediate output. Partial BUILD continues past them. UI-02 must commit (or regenerate and commit) them before final verification, where `clean` still fails without them.

## 3. Resume

- In the control center, press **Resume** on AnimalUprising, unless it already auto-resumed. Only tasks the project pause stopped are resumed; a task you paused individually stays paused.
- UI-02 should show **"BUILD continuing — N/M checklist steps open"** (neutral, no attempt used). The next session continues from the committed plan (b6a98fa): steps 4–6 `[~]`, then 8.

## 4. What to watch in the first cycle

- No new "no progress" decision while commits and checklist steps advance.
- Helpers admitted on CLIProxyAPI while Meridian's usage endpoint returns 429. The unit's reason reads "fallback … admitted (pinned)", and its calls go only to that route.
- Budgets show the authorized room, e.g. "4 / 6, 3 base + 3 authorized".
- When steps 4–8 are complete, final verification still requires:
  - a clean tree, with the gate JSONs committed;
  - `GATE UI-02 PASS` at HEAD;
  - the required captures;
  - independent review;
  - done-check at closeout.

## Rollback

- Pause again (safe point), then check out v0.122.4 (or v0.121.10) for both the coordinator and Foundry, and restart.
- The new columns and tables are additive and ignored by older code.
- A withdrawn decision stays withdrawn. Its audit event explains why.
