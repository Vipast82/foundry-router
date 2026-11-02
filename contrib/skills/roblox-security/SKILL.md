---
name: roblox-security
description: Harden or audit a Roblox game against exploits - server authority, remote and prompt validation, rate limits, movement and combat checks, economy, trading and purchase safety, data integrity, confidentiality and enforcement. Load for a security review or any change touching rewards, currency, inventory, purchases or permissions.
---

# Roblox security

The client is always compromised. The server is the source of truth for damage, currency, inventory, permissions and positions.

## Critical (block a release)
- All authoritative state on the server; every remote argument validated (see `roblox-networking`); per-player rate limits.
- No client-side currency, inventory or reward mutation. Prices and rewards from server definitions.
- Purchases: `ProcessReceipt` is idempotent (record the purchase id before granting, return `PurchaseGranted` only after the grant is saved). Never grant from a client message.
- Player data session-locked (ProfileStore or an equivalent); trades atomic (both sides or neither).
- No secrets, API keys or webhook URLs in anything that replicates.

## Client-triggered instances are not safe
| Instance | What an exploiter can do | Server must re-check |
|---|---|---|
| `ProximityPrompt` | fire while disabled, change distance/LOS/hold locally | enabled, distance, alive, cooldown |
| `ClickDetector` | fire from any distance, even disabled | same |
| `DragDetector` | unchecked click events; drag only respects Enabled/permission | validate the drag result |
| `Touched` on client-owned parts | fake or suppress touches | server-side overlap/distance queries |

## Movement and combat
- Validate custom movement and action transitions with tolerant thresholds (speed, teleport distance) and history, not single-frame kicks.
- Hit reports: check range, line of sight, cooldown, target alive, and that the weapon is equipped; the server computes damage.
- Network ownership: a client owning a part controls its physics; take ownership back for anything authoritative.

## Medium
Server-side cooldowns, server-computed leaderboards, anti-AFK checks on rewards, `TextService` filtering for all user text (see `roblox-text-chat`), sandbox third-party scripts.

## Enforcement
Enforcement is a product decision. `Players:BanAsync` / `UnbanAsync` are server-only, need `Players.BanningEnabled`, and must be `pcall`ed. Log before you kick; never rely on client anti-cheat, `_G`, or obfuscation.

## Review output
For each finding: the exploit in one sentence, the file:line, the fix, and a forged-request test that proves it (a test that sends the bad request and expects a refusal).

## References
- `references/security.md` - authority models, audit checklist, enforcement details (roblox-brain).
- `references/security-quick.md`, `security-checklist.md` - validation toolkit, token bucket, combat and economy checks (EL4CTEO).
