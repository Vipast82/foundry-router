---
name: roblox-audio
description: Add, mix, generate or fix game audio - Sound/SoundGroup and the modular Audio API (AudioPlayer, AudioEmitter, AudioListener, Wire, effects), 2D vs 3D placement, roll-off, music and ducking, one-shot SFX, preloading, and quality checks for generated or sourced sound. Load for any sound, music or audio pipeline task.
---

# Roblox audio

## Systems
- Classic `Sound` / `SoundGroup` / `SoundEffect`: simple and fine for most games.
- Modular Audio API (`AudioPlayer -> Wire -> AudioDeviceOutput` for 2D, `-> AudioEmitter` on a part for 3D, `AudioListener`): preferred for new projects and needed for voice-chat processing. Do not mix both systems for one sound path.

## Placement and mix
- Child of a BasePart = volumetric; Attachment = point source; `SoundService` = global (music, UI).
- Set `RollOffMode` (InverseTapered) and `RollOffMin/MaxDistance` explicitly; the default max distance is huge.
- Mixer: `SoundGroup`s under a master (Music, SFX, UI, Voice) so players can set volumes; duck music with a `CompressorSoundEffect` side-chained to SFX.
- Volume above 1 clips. Rapid-fire sounds: limit voices (check `IsPlaying`, pool or cap per type).

## Playback
- Feedback sounds play on the client immediately (server sounds arrive late); the server sends the event.
- One-shots: create, play, `Destroy()` on `Ended`. Preload critical sounds with `ContentProvider:PreloadAsync`.
- Streaming: ambient loops under `SoundService` or a persistent model, not a far part that streams out.
- Assets must be owned by or shared with the experience owner, and pass moderation, before they play in the published game.

## Generated or sourced audio: quality gate
Check every take before it ships (see references/audio-production-notes.md):
- duration inside the sound's target window (too-short one-shots read as clicks);
- true peak at or below about -1.5 dBTP and consistent loudness across a family;
- loops: no audible seam (crossfade the loop point), no click at start/end;
- the sound matches its brief (a frog sounds like a frog; big animals sound big);
- a human listening pass for anything player-facing, with picks and notes recorded per sound.

## References
- `references/audio.md` - both systems, effects table, client patterns (roblox-brain).
- `references/audio-quick.md` - Audio API wiring, 2D vs 3D, SoundGroups (EL4CTEO).
- `references/audio-production-notes.md` - generation and listening-round lessons (Foundry).
