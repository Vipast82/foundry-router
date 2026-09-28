# Long runs: keep context small and completion verifiable

## State lives in files, not in the chat
- Keep `docs/STATE.md` current: the sprint/goal, a checklist of deliverables
  (done / in progress / blocked), and the next step. Update it after every
  completed item and before any long command.
- After each verified item, append the evidence to `docs/evidence/<sprint>.md`:
  item, exact command or tool call, PASS/FAIL, and the raw key output lines
  (not whole logs).
- When starting or resuming, read `docs/STATE.md` first. Do not redo items
  marked done unless their evidence is missing or failing.

## Keep tool output small
- Never dump whole files or logs. Use targeted reads and filters:
  `Select-String -Pattern ...`, `Select-Object -First 40`, `git diff --stat`,
  `(Get-Content f)[a..b]` for line ranges.
- Prefer one focused command over several exploratory ones. Summarize long
  output in one or two lines before continuing.
- For broad read-only research across many files, use subagents.

## Context limits
- If a message says earlier tool calls were collapsed into a TOOL LEDGER,
  treat those steps as done. Re-run one only if you need its full output
  again, and check the ledger's result excerpt first.
- One sprint per task: when a sprint is complete and recorded in
  `docs/STATE.md` and `docs/evidence/`, stop and report completion. Do not
  start the next sprint in the same task.
