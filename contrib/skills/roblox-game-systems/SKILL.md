---
name: roblox-game-systems
description: Server-authoritative recipes for common game systems - currency service and leaderstats, XP and levels, round loops with intermission and teams, combat and damage, inventory and shops, progression and unlocks. Load when building or reviewing a standard gameplay system.
---

# Game system recipes

## Four rules every system follows
1. State lives in a server service; persistent parts go through the data layer (`roblox-data`).
2. Clients send intents through validated, rate-limited remotes (`roblox-networking`, `roblox-security`); the server decides and replicates results.
3. Config is data: tunables in a config module or live config, never scattered literals.
4. Lifecycle-safe: late joiners, respawns, leaving mid-action, server shutdown and match reset all handled.

## Patterns
- **Currency**: one service owns balances (`Add`, `TrySpend` returning success and the new balance); every change logged as an economy event (`roblox-analytics-liveops`); leaderstats only display.
- **XP and levels**: a pure level curve function (tested), server-side grants with a reason, level-up as an event the UI listens to.
- **Rounds**: an explicit state machine (`Intermission -> Starting -> InRound -> Ending`) driven by server time, with a reset that returns every service to a clean state (list each service's reset in one place and test it).
- **Combat**: the server computes damage from its own stats; hit reports are checked for range, line of sight, cooldown and target state; deaths and rewards granted once.
- **Inventory and shop**: item definitions on the server, ownership in the profile, purchases through one validated path (Robux purchases via `roblox-monetization`); equip/unequip validated and replicated.
- **Progression and unlocks**: unlock rules as pure, tested data; the server checks them on every request, the client only displays.

## Design checklist for a new system
Owner and state; remotes and their validation; persistence and migration; reset/cleanup; analytics events; UI states (loading, empty, error); tests for the pure rules plus one forged-request test.

## References
- `references/game-systems.md`, `game-systems-combat.md`, `game-systems-inventory-shop.md`, `game-systems-progression.md`, `game-systems-round-system.md` - recipes with code (EL4CTEO).
