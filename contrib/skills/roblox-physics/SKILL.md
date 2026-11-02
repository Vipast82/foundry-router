---
name: roblox-physics
description: Build or fix Roblox physics and spatial queries - constraints and attachments, movers (LinearVelocity, AlignPosition/Orientation), vehicles, ragdolls, projectiles, raycasts and overlap queries, collision groups, network ownership and Server Authority. Load for anything that moves by physics or needs a spatial query.
---

# Roblox physics

## Choose the authority model first
- Classic: an unanchored assembly near a player may be simulated by that player. `SetNetworkOwner(nil)` keeps gameplay-critical parts server-owned; treat player-owned physics as untrusted and validate outcomes.
- Server Authority (`Workspace.AuthorityMode = Server` + its settings bundle): core simulation stays server-owned with client prediction; `SetNetworkOwner` is not the security boundary.

## Constraints
- Mechanical: Hinge, Prismatic, Cylindrical, BallSocket, Universal, Weld/Rigid. Motion: `AlignPosition`, `AlignOrientation`, `LinearVelocity`, `AngularVelocity`, `VectorForce`, `Torque`. Spring, Rope, Rod.
- Each needs `Attachment0` and `Attachment1` (a missing one fails silently); constraints do nothing on anchored parts.
- Over-constraining jitters; tune with `CustomPhysicalProperties` (density, friction).
- Legacy body movers (`BodyVelocity`, `BodyPosition`, `BodyGyro`) are superseded: use the constraint movers.

## Spatial queries
- `workspace:Raycast(origin, direction, params)`, `Blockcast`, `Spherecast`, `Shapecast`; overlap with `GetPartBoundsInBox/Radius`, `GetPartsInPart` and `OverlapParams`.
- Reuse `RaycastParams`; filter with collision groups or include/exclude lists.
- Hitscan weapons use raycasts; `Touched` only for slow physics projectiles, and it is unreliable on client-owned parts.

## Recipes
- Vehicles: motorized `HingeConstraint` wheels, `SpringConstraint` suspension, servo steering.
- Ragdolls: record every joint and property you change and restore only what you own.
- Projectiles: always a lifetime and cleanup; validate hits on the server.

## Collision groups
`PhysicsService:RegisterCollisionGroup` and `CollisionGroupSetCollidable` once at startup; set `BasePart.CollisionGroup` by name.

## References
- `references/physics.md` - constraints, vehicles, ragdolls, ownership, Server Authority (roblox-brain).
- `references/physics-quick.md`, `physics-recipes.md`, `physics-server-authority.md` - queries, collision groups, Touched pitfalls, recipes (EL4CTEO).
