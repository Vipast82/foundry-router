---
name: roblox-networking
description: Design, change or audit client-server communication - RemoteEvent, UnreliableRemoteEvent, RemoteFunction, payload and rate budgets, serialization rules, server validation, Server Authority mode, latency and time sync. Load for anything sent between client and server.
---

# Roblox networking

## Pick the channel
| Channel | Guarantee | Use for |
|---|---|---|
| `RemoteEvent` | reliable, ordered per remote | requests, game events, state the client must not miss (the default) |
| `UnreliableRemoteEvent` | may drop or reorder; payload over 1000 bytes is dropped | replaceable continuous data: aim, cosmetic VFX, non-physics positions |
| `RemoteFunction` | request/response, caller yields | short client-to-server queries; never server-to-client (a client can hang it) |

Remotes are not ordered against property/attribute replication: use one channel or version the state.

## Write the contract before the handler
Who may call; argument types and max sizes; the state that must be true; how often; what success and failure send back.

## Validate at the server boundary (every argument is attacker-controlled)
- Type and shape first (`typeof`), then NaN/inf (`x ~= x`, `math.abs(x) == math.huge`), string length and `utf8.len`, then ownership, state, distance and cooldown against server data.
- Prices, damage, rewards and permissions come from server-owned definitions, never from the payload.
- Instances in a payload can be spoofed or nil: check `IsA`, ancestry and ownership.
- What survives the wire: functions arrive nil, metatables are stripped, mixed or non-string keys are mangled, a nil truncates an array, every table is a copy. Validate field by field.
- Rate-limit per player (token bucket). A rate limit protects the server; validation still rejects bad requests. Log suspicion with thresholds; never punish one malformed packet.

## Budget
- Measure rate x bytes x recipients under load. Send a snapshot on join, then diffs. ~500 requests/s per client per remote type is a ceiling, not a target.
- Typed remote schemas or generators (e.g. Blink) do not replace validation; pin the generator version.

## Server Authority (opt-in)
`Workspace.AuthorityMode = Server` brings a settings bundle (next-gen replication, InputAction system, deferred signals, fixed simulation, streaming): verify all of them. Simulation input goes through `InputAction` + `RunService:BindToSimulation()`, not remotes. Misprediction and rollback are normal. Custom actions (attacks, purchases, teleports) still need server validation.

## Time
Use `workspace:GetServerTimeNow()` for shared clocks. Client timestamps are context, never proof: bound freshness and check server history.

## References
- `references/networking.md` - contracts, validation, throttling, Server Authority, edit-mode mocks (roblox-brain).
- `references/networking-quick.md`, `networking-patterns.md` - typed remote module, handler template, bandwidth and latency patterns (EL4CTEO).
