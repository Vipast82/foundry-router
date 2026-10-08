# Escalation: big-model plan -> local fix

- Escalate only after the local model has failed the same problem twice.
  Start a NEW Plan task; give it the failing check output, the error lines
  and the file paths. Do not escalate inside a long Act task.
- In Plan mode (big model): read and search the code, find the root cause,
  and write `docs/fix-plans/<topic>.md`. Do not write code. It must contain:
  1. Root cause: one paragraph, with file:line references.
  2. Exact changes, in order: file, function, what to change and why.
     Write each change as a precise instruction or a short before/after
     snippet (at most ~15 lines each).
  3. A verification command or check for each change, with the expected PASS
     output.
  4. Risks: what else could break, and which existing tests cover it.
- In Act mode (local model): read the fix plan first. Apply changes one at a
  time with `replace_in_file`, run each change's verification, and record
  PASS/FAIL in the evidence file. If a step doesn't match the code (the file
  changed, a line is missing), stop and report instead of improvising.
