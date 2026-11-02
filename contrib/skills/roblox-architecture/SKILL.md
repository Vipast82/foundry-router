---
name: roblox-architecture
description: Decide where Roblox code and assets live and who owns them - client/server split, services and containers, startup order, dependencies, player and character lifecycle, CollectionService components, shared state and instance streaming. Load when adding a feature, moving code, or reviewing structure.
---

# Roblox architecture

## Name the owner first
For each behaviour write down: the authoritative state and who may change it; its public operations; the instances, connections and tasks it owns; its network or persistence boundary; startup and teardown. One owner per mutation.

## Where things go
| Container | Runs / visible | Holds |
|---|---|---|
| `ServerScriptService` | server | server scripts and server-only modules |
| `ServerStorage` | server | templates, maps and assets clients must not see |
| `ReplicatedStorage` | both | shared pure modules, config safe for clients, remotes |
| `ReplicatedFirst` | client, first | loading screen only |
| `StarterPlayerScripts` | client | client entry point and controllers |
| `StarterGui` | client | ScreenGuis (`ResetOnSpawn = false` unless reset is wanted) |
| `Workspace` | both, streamed | the 3D world only, never code or templates |

Shared code holds no secrets and no authoritative mutable state.

## Dependencies and startup
- Direct requires are the default. Add a signal only for genuinely independent observers; no event bus, DI container or `Init/Start` ceremony to hide an ordinary call.
- One small bootstrap per side starts services in order and fails visibly. Module top level never yields.
- Bound every `WaitForChild` on replicated dependencies (a timeout and a clear error, not a silent hang).

## Authority
The client presents and predicts; the server validates and decides. Remotes are APIs: types, bounds, ownership, rate limits, failure replies (see `roblox-networking`, `roblox-security`).

## Lifecycle and components
- Handle players already present plus `PlayerAdded`; clean up on `PlayerRemoving`. Same for `CharacterAdded` (the character parents before its descendants: `WaitForChild("Humanoid")`).
- Tags (CollectionService) for discovery, attributes for configuration; one owner attaches and cleans up per tagged instance.

## Streaming (on for new places)
- Client code must not assume a far instance exists: bounded `WaitForChild`, nil checks, or do it on the server. Full-world queries and pathfinding run on the server.
- Ambient audio under `SoundService` or a persistent model, not a far part. Keep models compact; `Persistent` only when necessary. Details: `references/architecture-streaming.md`.

## Split only on evidence
A separate lifecycle or authority boundary, a distinct persistence contract, a testable pure core, or unrelated reasons to change. ECS only when many similar entities update every frame.

## References
- `references/architecture.md` - ownership, layout, startup, ECS notes (roblox-brain).
- `references/architecture-quick.md`, `architecture-streaming.md`, `architecture-ecosystem.md` - containers, Rojo layout, lifecycle code, streaming settings and patterns, ecosystem libraries (EL4CTEO).
