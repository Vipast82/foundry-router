# PILOT-C1 — Coordinator pilot notes (tier C, docs only) — PLAN

**SANDBOX ONLY.** Written for the coordinator pilot in a throwaway clone with a local remote; never in the real repository.

**Tier:** C. **Depends on:** none.

## Goal
Write docs/progress/PILOT-C1_NOTES.md with one line saying the pilot ran, append the evidence line, tick BUILD.

```pilot-actions:implement
[{"tool": "write", "args": {"filePath": "docs/progress/PILOT-C1_NOTES.md", "content": "PILOT-C1: coordinator pilot note (scripted coder).\n"}},
 {"tool": "bash", "args": {"command": "printf 'PILOT-C1 BUILD evidence: notes written\\n' >> docs/progress/PILOT-C1_EVIDENCE_RAW.log && sed -i 's/^- \\[ \\] BUILD/- [x] BUILD/' docs/progress/PILOT-C1_PLAN.md"}},
 {"tool": "bash", "args": {"command": "git add docs/progress/PILOT-C1_NOTES.md docs/progress/PILOT-C1_EVIDENCE_RAW.log docs/progress/PILOT-C1_PLAN.md && git commit -q -m 'PILOT-C1: notes'"}}]
```

## Files this sprint may change
- `docs/progress/PILOT-C1_NOTES.md`

## Checklist
- [ ] BUILD notes written and committed
- [ ] CLOSE summary, STATE row, done_check, push

## Acceptance lines
Notes file committed · done_check PASS · pushed · status DONE.
