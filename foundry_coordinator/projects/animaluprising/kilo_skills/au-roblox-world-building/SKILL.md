---
name: au-roblox-world-building
description: Build or improve the AnimalUprising map's look in code - lighting, atmosphere and post-processing, terrain, materials, scenery density, ambient effects and streaming - within the Map v2 generator and an approved sprint scope. Use for a map look, environment, lighting, terrain or scenery task, not for gameplay rules or UI.
---

# Map look and world building (AnimalUprising)

Load `au-luau-development` and `au-roblox-development` first, and the general `roblox-world-building` skill for Roblox APIs and map-design rules (lighting, atmosphere, terrain, materials, props, verification); `roblox-architecture` (its streaming reference) before client code that reads map parts. This skill adds how to change what the map looks like without breaking the Map v2 generator, its seeds or its budgets; where they differ, this skill wins. Verify any API you have not used before with `rbx-docs-search` (Roblox_Studio `skill` tool).

## Where the map comes from

- The map is generated on the server by `src/server/MapBuild/` (`Build.lua` time-sliced jobs, `Ground.lua`, `Compounds.lua`, `Scenery.lua`, `Trails.lua`, `Pois/`). Design: `docs/design/13-map-procedural-generation.md`; guide and tested examples: `docs/design/13a-map-v2-implementation-guide.md`; playbook: `docs/playbooks/PB6_MAPV2.md`.
- The look follows the bibles: `docs/design/00-FULL-BIBLE.md` and `docs/design/ASSET_BIBLE.md` (biomes, palette, tone). Never invent a biome, palette or mood the bible does not give; when it is silent, propose options in the plan and leave the choice to the owner.
- Build on the existing seed: the same seed must give the same map. Use the generator's `Random`, never `math.random` or time.
- Keep every change inside `runSliced` jobs so one frame never builds too much (watch `maxSliceMs` in the build stats).

## Lighting and atmosphere

- No game code sets `Lighting` today (only a test reads it): the game runs Roblox defaults, which is a large part of why it looks flat. Lighting set by hand in the Studio place is not in git and is lost; put it in code (a config module applied once at server start, or the project file) so Rojo and the gate see it.
- Pick one `Lighting.LightingStyle` and keep it; add `Atmosphere`, `Sky`, and at most a few post effects (`ColorCorrectionEffect` or `ColorGradingEffect`, a light `BloomEffect`, `SunRaysEffect`). Tune per biome only through config values, not scattered literals.
- Time of day: a fixed `ClockTime` per mode is simpler to test than a running cycle. If a cycle is in scope, compute it on clients from `workspace:GetServerTimeNow()`.
- Few local lights cast shadows. Torches and lanterns use `PointLight` with `Shadows = false` unless the plan says otherwise.

## Ground, materials and scenery

- `Ground.lua` has variants `Strips`, `Cells` and `Terrain`. Changing the default variant is a design change: say so in the plan.
- Prefer real materials (`Grass`, `Ground`, `Mud`, `Rock`, `Slate`, `Wood`, `Cobblestone` ...) or `MaterialVariant` over `SmoothPlastic` for anything the player walks past. Keep colours from the bible.
- Scenery and props: decorative parts get `CanCollide`, `CanTouch` and `CanQuery = false`; tiny details also `CastShadow = false`. Reuse the same meshes (instancing is cheap; many unique unions are not). Static decoration models can use `LevelOfDetail = SLIM`.
- Never move or block lanes, tower pads, compounds or POI footprints: the Map v2 rows in `tests/RegressionIndex.lua` (`MV2_1` ... `MV2_12`, `PT_1`) and the `tests/lune/specs/MapV2*.spec.luau` specs must stay green.

## Streaming

- `Workspace.StreamingEnabled` is on (`default.project.json`). Client code must not assume a far map part exists: `WaitForChild` with a timeout, or do the work on the server. Ambient sound belongs under `SoundService` or a persistent model, not a far part.
- Keep decoration models spatially compact; do not mark large map models `Persistent`.

## Checking the look

- Judge lighting, colour and frame time only on the workstation's Studio. The Studio slots render in software (about 36 fps): never measure frame time or judge look there.
- Captures go through the gate's `CAPTURE` rows and the coordinator's vision approval; the owner approves the final look. A session never declares its own visual pass.
- Check a phone size as well as desktop (`rbx-device-simulator-lua`), and record the build stats (parts, `maxSliceMs`) before and after so the change has a cost on record.
- Finish with: what changed per biome, before/after captures, build-stat deltas, and anything left for the owner to choose.
