---
name: roblox-npc-ai
description: Create or fix NPCs, enemies and bots - PathfindingService (costs, modifiers, links, blocked paths), state machines, perception (distance, field of view, line of sight), spawning, update loops and scaling to many NPCs. Load for any AI-driven character.
---

# NPC and enemy AI

## Pathfinding
- `PathfindingService:CreatePath({AgentRadius, AgentHeight, AgentCanJump, Costs = {...}})`, `path:ComputeAsync(from, to)`, check `path.Status`, walk `GetWaypoints()` (jump on `Jump` actions), wait on `MoveToFinished`.
- Recompute on `Blocked` with a bounded retry; cancel stale paths when the target changes.
- `PathfindingModifier` labels map regions to costs; `math.huge` = impassable; `PassThrough` for doors; `PathfindingLink` for gaps and ladders.
- Long or frequent `ComputeAsync` calls are expensive; moving collidable geometry rebuilds the navmesh.
- For a fixed lane or route (tower defence, rails), precomputed waypoints beat runtime pathfinding.

## Behaviour
- An explicit state machine (`idle -> patrol -> chase -> attack -> flee -> dead`); each transition owns its movement change, target cleanup and cancellation.
- Perception cheapest first: distance (`(a-b).Magnitude`), then field of view (dot product against a configured cone), then line of sight (`Raycast`) last.

## Authority and cost
- Decisions and movement on the server; clients only present (animation, VFX, sound).
- Classic: `SetNetworkOwner(nil)` for physics-sensitive NPCs (not a full security boundary).
- Stagger and throttle updates by NPC count and measured cost, not a fixed universal tick. Pool and reuse NPC models; clean up connections on death.
- Many NPCs: one manager loop over a table of NPCs beats one script per NPC; consider client-side presentation of server positions.

## References
- `references/npc-ai.md` - pathfinding, state machines, spawners, budgets (roblox-brain).
- `references/npc-ai-quick.md`, `npc-ai-scaling-npcs.md` - chase loops, perception, scaling many NPCs (EL4CTEO).
