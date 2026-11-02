---
name: roblox-world-building
description: Build and dress Roblox worlds - map layout and readability, parts, models and pivots, materials and MaterialVariants, Terrain and water, lighting, atmosphere and post-processing, props and Creator Store assets, procedural generation, CSG and destruction, and verifying a build. Load for maps, levels, environments, lighting or any "the world looks flat" task.
---

# World building

## Build with evidence
- Inspect before building: the root folder, origin, dimensions and the assets already in the project. Name the origin and sizes; offset pieces from anchor CFrames, never guessed world coordinates.
- Build in phases and read each phase back (instance tree, bounds, a capture, a walk-through). No "it is built" without readback.
- Snap to 1 / 0.5 / 0.25 / 0.125 studs. Set anchoring, collision, shadows, colour and material explicitly. Create, configure, parent last; move models with `PivotTo`.

## Layout and readability (design rules; see references/map-design-notes.md)
- Player is ~5 studs tall: doors ~4x7, ceilings 10-14, paths 6+ studs wide with a wide entry.
- Every zone gets its own palette and material set: colour is the cheapest zone separator.
- Landmarks need a vertical element and work best at corners and turns, where players slow down.
- Points of interest within ~40 s of walking of each other at most; secondary content where their areas overlap.
- Break the grid on decoration: jitter yaw and scale on repeated props, bend paths. 90-degree everything reads as a greybox.
- Dress the map edges; a bare edge is the clearest sign of an unfinished map.

## Lighting (all script-writable except the legacy `Technology`)
- `Lighting.LightingStyle` (`Soft` = stylized, `Realistic` = naturalistic) and `PrioritizeLightingQuality` can be set from scripts; `Lighting.Technology` is deprecated and cannot. Put lighting in code or the project file so it is versioned, not only in the place file.
- Start: `ClockTime` 9-10 or 15-17 for long warm shadows; `Atmosphere` Density ~0.1-0.3 (higher turns a small map into fog), low Haze; `ColorCorrection` tint near white (it multiplies the whole frame); a mild Bloom; sparing SunRays.
- Fully flat cartoon look is a different recipe (no shadows, white ambient); pick one look and keep it.
- Few local lights cast shadows. A day/night cycle is computed on clients from `workspace:GetServerTimeNow()`.

## Materials and props
- Real materials (Grass, Ground, Mud, Rock, Slate, Wood, Cobblestone...) or `MaterialVariant` beat `SmoothPlastic` for anything players walk past.
- Decoration: `CanCollide/CanTouch/CanQuery = false`, `CastShadow = false` on tiny details. Reuse the same meshes (instanced); many unique unions are expensive. Static decoration models may use `LevelOfDetail = SLIM`.
- Creator Store assets: check creator, price, rights and embedded scripts before inserting; remove unneeded code. Restricted assets render blank until the experience is granted access.

## Terrain and water
- Terrain uses 4-stud voxels: fills next to each other fight rather than blend; end a basin where the water begins.
- Use Terrain water, not a transparent part (waves, reflection, swimming). Water under a solid part is hidden: leave a real hole. Bulk generation uses `ReadVoxels/WriteVoxels` in chunks with yields.

## Procedural maps
Deterministic: a seeded `Random`, never `math.random` or time. Build in time-sliced jobs. Keep gameplay footprints (paths, pads, spawns) fixed and test them; decoration is free to vary.

## Verify
Captures from fixed views at desktop and phone sizes, part and triangle counts before/after, a walk of every path, and no players able to leave the map.

## References
- `references/building.md` - MCP build mode, assets, acceptance, map review (roblox-brain).
- `references/lighting.md` - Lighting, Atmosphere, post effects (roblox-brain; its note that LightingStyle is not script-writable is outdated - it is).
- `references/world-building-quick.md` - parts, Terrain, CSG/GeometryService, ProceduralModel, EditableMesh, effects (EL4CTEO).
- `references/map-design-notes.md` - layout, landmarks, water and building rules of thumb (Foundry, after SyphoDev's research).
