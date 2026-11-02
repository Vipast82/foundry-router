---
name: roblox-teleport-matchmaking
description: Multi-place and multi-server games - universes and places, TeleportService.TeleportAsync with reserved and specific servers, teleport data, retries, lobby-to-match flows, parties, detecting server type, matchmaking and server lifecycle. Load for lobbies, matches, private servers or moving players between places.
---

# Teleports and matchmaking

## Teleporting
- Server-side `TeleportService:TeleportAsync(placeId, players, teleportOptions)`; `TeleportOptions` for a reserved server (`ShouldReserveServer`, or a stored `ReservedServerAccessCode`), a specific server (`ServerInstanceId`), and teleport data.
- Teleports fail: `pcall`, listen to `TeleportInitFailed`, retry with backoff for transient errors and tell the player.
- Teleport data is client-visible and spoofable: send ids, look the real state up on the destination server (a DataStore or MemoryStore entry keyed by an id you generated). Never trust it for rewards or permissions.
- Save player data before teleporting out and load it fresh on arrival (session locking handles the hand-off; see `roblox-data`).

## Lobby -> match
- A lobby queues players (MemoryStore queue or sorted map for cross-server queues), forms a group, reserves a server, stores the match setup under a match id, then teleports the whole group together.
- The match server reads its setup by id on start, waits for the expected players with a timeout, and handles late or missing players.
- Parties: `player.PartyId` and `SocialService` expose Roblox party membership; keep a party together when forming matches.

## Server type and lifecycle
- Reserved vs public vs private server: check `game.PrivateServerId` / `PrivateServerOwnerId` (reserved servers have a `PrivateServerId` and no owner).
- `game:BindToClose` saves and returns players gracefully; a match server shuts down when its last player leaves.
- Places in one universe share DataStores; configure place access (same-universe only for match places) in the Creator Dashboard.

## References
- `references/teleport.md` - structure, teleport options, data hand-off, lobby flow, lifecycle (EL4CTEO).
