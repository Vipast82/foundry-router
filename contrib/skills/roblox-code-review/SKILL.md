---
name: roblox-code-review
description: Review Roblox and Luau code for exploits, data loss, memory leaks, performance problems, lifecycle bugs, policy issues and deprecated or superseded APIs, with severity-ranked, actionable findings. Load for any code review or pre-merge check.
---

# Roblox code review

## Order of attention
1. **Exploits** - client trusted for damage, currency, inventory, positions or permissions; unvalidated remote arguments; prompts/click detectors not re-checked; secrets replicated (`roblox-security`).
2. **Data loss** - no session lock, `SetAsync` read-modify-write, no `BindToClose`, unvalidated values (NaN, bad UTF-8), missing migration (`roblox-data`).
3. **Purchases** - non-idempotent `ProcessReceipt`, granting before saving, trusting the client (`roblox-monetization`).
4. **Leaks and lifecycle** - connections, threads and instances without an owner; per-player state not cleared on leave; respawn/rejoin/reset paths.
5. **Performance** - per-frame searches, allocations in hot loops, remote spam (`roblox-performance`).
6. **Policy** - unfiltered player text, paid random items without policy checks (`roblox-text-chat`, `roblox-monetization`).
7. **Deprecated APIs** - see `references/code-review-deprecated-apis.md`; common ones: `wait/spawn/delay` -> `task.*`; `Humanoid:LoadAnimation` -> `Animator:LoadAnimation`; `SetPrimaryPartCFrame` -> `PivotTo`; `FindPartOnRay` -> `workspace:Raycast`; body movers -> constraint movers; `Instance.new(class, parent)` -> parent last; `CFrame.new(pos, lookAt)` -> `CFrame.lookAt`; `Lighting.Technology` -> `LightingStyle`.

## Severity
- **Critical**: exploitable for currency/items/progress, or data loss/corruption.
- **High**: likely crash, leak or broken feature in normal play.
- **Medium**: edge-case bugs, performance under load.
- **Low**: style, modernization, clarity.

## Finding format
For each: severity, file:line, what goes wrong in one sentence with a concrete scenario (inputs or state -> wrong result), and the fix. Only report what you can tie to the code; mark guesses as questions. Modernization suggestions go last and never block.

## References
- `references/code-review.md` - severity guide, checklist, output format (EL4CTEO).
- `references/code-review-deprecated-apis.md` - legacy -> current API table (EL4CTEO). Verify an entry against the Roblox API reference before flagging it as an error.
