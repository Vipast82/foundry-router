# Workstation gate-review boundary fix

During v0.121.8 deployment, a dry review packet for UI-01 at a416f61 exposed a
real log shape absent from the synthetic fixtures. The final GATE FINISH block
contains an embedded Studio-session transcript before its final GATE UI-01 PASS.
Treating every SESSION heading as a new run cut off that verdict and incorrectly
marked the packet evidence unverified.

The workstation v0.121.9 patch permits one explicitly named inline Studio
transcript only when its task and HEAD match the current gate's binding. A new
run, different task/head, second session, another GATE block or unrelated final
verdict remains a boundary. Old completed finishes cannot lend their binding to
a later incomplete finish. Ordinary gate step markers do not discard the current
HEAD binding. Missing/failed/stale evidence is not promoted to PASS.

This changes review extraction only. It does not change game code, the gate,
acceptance criteria, scope, visual approvals or actual test evidence. It is a
workstation-only follow-up to the published v0.121.8 release; the live router
can remain on v0.121.8 because no API contract changes. The fix must be included
in the next coordinated router release. No remote publication is implied by a
local repair commit.

Regression coverage is in tests/coordinator/test_au_review_evidence.py. The real
immutable UI-01 packet must include the final verdict with its raw line/commit
provenance, complete source/test coverage and the BUILD/CLOSE phase contract.
