---
name: roblox-data
description: Save, load, migrate and audit Roblox data - player saves with session locking (ProfileStore), DataStore budgets and retries, schemas and migrations, OrderedDataStore leaderboards, MessagingService, MemoryStore, global state and right-to-be-forgotten. Load for any persistence or cross-server data change.
---

# Roblox data

## Player saves
- A template with a `version` field before anything is stored. Deep-copy defaults; migrate step by step, then fill missing fields from the template. Never delete unknown fields silently.
- Session ownership is mandatory (ProfileStore: `StartSessionAsync`, `OnSessionEnd`, `EndSession`; no `Steal` for normal loads). Two servers must never write one profile.
- Load once, mutate in memory, save on meaningful changes plus lifecycle boundaries. `PlayerRemoving` alone is not enough; `BindToClose` finishes pending saves.
- `UpdateAsync` for every read-modify-write; `SetAsync` only for blind overwrites you mean.
- Only primitives, arrays and string-keyed dictionaries. Reject NaN/inf, invalid UTF-8 (`utf8.len`), Instances, functions, cycles: one bad value fails the whole write.
- Scope store names by environment (e.g. `PlayerData_Studio`) or use ProfileStore's mock so Studio never writes production data.
- Keys contain the user id as a plain substring (`player_<UserId>`) so right-to-be-forgotten deletion templates match.
- Identity: new code may use `player.User` (`User.Id`, `DomainType`, `DomainId`); `UserId` stays valid. Never mix the two in one key scheme; key cross-experience data on `UserId`.

## Budgets and errors
- Retry transient errors with capped exponential backoff + jitter; never retry a non-idempotent grant blindly.
- Check `DataStoreService:GetRequestBudgetForRequestType` before bursts. Budgets scale with players; reads are cached ~4 s (`UseCache = false` to verify a write).

## Cross-server
- **OrderedDataStore**: leaderboards only (integer values, string keys); never player saves.
- **MessagingService**: no delivery or ordering guarantee. Make handlers idempotent; request/response needs a request id, ack and timeout; "no ack" does not mean "not executed".
- **MemoryStore** (queues, sorted maps): short-lived coordination (matchmaking, leases). Quotas are one shared pool per experience; remove a queue item only after idempotent processing; value <= 32 KB.
- **GlobalDataStore** for shared persistent state (seasons, counters) via `UpdateAsync`.

## Review checklist
Session lock, migration path for old saves, BindToClose, retry policy, budget check, validated values, environment-scoped names, RTBF-friendly keys, and a test that loads an old-version save.

## References
- `references/player-data.md` - saves, ProfileStore, migrations, budgets (roblox-brain).
- `references/server-data.md` - leaderboards, MessagingService, MemoryStore, global state (roblox-brain).
- `references/data-stores-quick.md`, `data-stores-raw-datastore.md`, `data-stores-memory-and-messaging.md` - raw DataStore rules and limits (EL4CTEO).
