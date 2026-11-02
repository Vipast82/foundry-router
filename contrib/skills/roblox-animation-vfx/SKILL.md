---
name: roblox-animation-vfx
description: Add or fix character animation and visual effects - Humanoid and AnimationController, Animator tracks, priorities, markers, characters and HumanoidDescription, particles, beams, trails, highlights, tweens, camera shake and effect timing. Load for animation playback, character setup or any VFX.
---

# Animation and VFX

## Animation
- Load tracks through an `Animator` (under a `Humanoid` or `AnimationController`): `animator:LoadAnimation(anim)`; `Humanoid:LoadAnimation` is legacy. Cache loaded tracks per character and stop them on cleanup.
- Set `AnimationTrack.Priority` deliberately (Core < Idle < Movement < Action...); same-priority tracks blend by weight.
- Gameplay or presentation cues use `GetMarkerReachedSignal(name)`; disconnect when the track ends. `Ended` fires after the fade; `Stopped` includes explicit stops.
- Animations must be owned by (or shared with) the experience's owner or they fail to load in the published game.
- Characters: spawn with `LoadCharacterAsync`, appearance with `HumanoidDescription`; wait for descendants (`WaitForChild("Humanoid")`).

## Effects
- Shape every effect in readable phases: warning (telegraph), contact, brief peak, fade. Get the silhouette of the impact right before adding particles, light, sound or shake.
- Burst emitters: `Rate = 0` and `:Emit(n)` with a bounded lifetime; pool frequent effects. Use flipbooks for animated particle textures.
- Beams and trails need two attachments with a stable relationship and a cleanup owner. `Highlight` count is limited.
- Tween a small set of properties with `TweenService:Create`; cancel or destroy temporary effects with their owner (`Debris:AddItem` for simple lifetimes).
- Cosmetic effects run on clients (server sends the event); the server never waits on a visual.
- Visual-only explosions: `BlastPressure = 0`, `DestroyJointRadiusPercent = 0`.
- Server Authority: keep synchronized animation logic in `BindToSimulation`, query current tracks rather than caching across rollback, and make predicted effects reversible.

## Timing and budgets
- Effect timing checks (frames per phase, telegraph lengths) need real-rate rendering: never measure them in a software-rendered Studio.
- Budget particle counts, lights and post effects per scene and per device class; profile on a phone.

## References
- `references/animation-vfx.md` - tracks, markers, effect recipes, Server Authority notes (roblox-brain).
- `references/characters-quick.md`, `characters-character-controller-library.md` - Humanoid states, spawning, damage, appearance, controllers (EL4CTEO).
