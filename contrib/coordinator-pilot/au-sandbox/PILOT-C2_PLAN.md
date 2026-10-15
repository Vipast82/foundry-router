# PILOT-C2 — Coordinator pilot follow-up (tier C, docs only) — PLAN

**SANDBOX ONLY.** Written for the coordinator pilot in a throwaway clone with a local remote; never in the real repository.

**Tier:** C. **Depends on:** PILOT-C1

## Goal
Write docs/progress/PILOT-C2_NOTES.md referring to PILOT-C1, append the evidence line, tick BUILD.

```pilot-actions:implement
[{"tool": "read", "args": {"filePath": "docs/progress/PILOT-C1_NOTES.md"}},
 {"tool": "write", "args": {"filePath": "docs/progress/PILOT-C2_NOTES.md", "content": "PILOT-C2: follows PILOT-C1 in a fresh session (scripted coder).\n"}},
 {"tool": "bash", "args": {"command": "printf 'PILOT-C2 BUILD evidence: notes written\\n' >> docs/progress/PILOT-C2_EVIDENCE_RAW.log && sed -i 's/^- \\[ \\] BUILD/- [x] BUILD/' docs/progress/PILOT-C2_PLAN.md"}},
 {"tool": "bash", "args": {"command": "git add docs/progress/PILOT-C2_NOTES.md docs/progress/PILOT-C2_EVIDENCE_RAW.log docs/progress/PILOT-C2_PLAN.md && git commit -q -m 'PILOT-C2: notes'"}}]
```

## Files this sprint may change
- `docs/progress/PILOT-C2_NOTES.md`

## Checklist
- [ ] BUILD notes written and committed
- [ ] CLOSE summary, STATE row, done_check, push

## Acceptance lines
Notes file committed · done_check PASS · pushed · status DONE.
