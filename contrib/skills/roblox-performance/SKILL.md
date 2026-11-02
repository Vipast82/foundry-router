---
name: roblox-performance
description: Measure and fix Roblox performance - MicroProfiler, Script Profiler, Developer Console, memory categories and leaks, hot Luau paths, rendering and asset cost, physics, streaming, Parallel Luau and mobile budgets. Load for a measured FPS, lag, memory or network problem, or a performance budget check.
---

# Roblox performance

Optimize measured hot paths, not folklore. Every claim needs a before/after measurement on the same workload.

## Measure first
- MicroProfiler (Ctrl+F6) for frame breakdown; Script Profiler for per-script CPU and heap; Developer Console (F9) for memory, network, server stats.
- Label regions with `debug.profilebegin/profileend` and memory with `debug.setmemorycategory` (behind a flag).
- Record device, build, workload, warm-up and duration. One FPS reading is not a benchmark.
- **Measure only on real hardware.** A software-rendered Studio (a VM, a container or Wine, often ~30-40 fps) gives meaningless frame and timing numbers; use it for behaviour checks only.

## Starting targets
| Metric | Target | Investigate at |
|---|---|---|
| Server heartbeat | < 16 ms | > 33 ms |
| Desktop FPS | 60 | < 30 |
| Mobile FPS | 45 | < 30 |
| Memory | device-specific | sustained growth |

## Usual causes -> fixes
- Leaked connections, threads and instances -> owners that disconnect, cancel and destroy (repeat spawn/despawn and check memory returns to baseline).
- Per-frame loops doing full-world searches -> events, spatial indexes, staggered updates.
- Many unique meshes/unions, huge part counts, oversized textures -> reuse meshes (instanced), merge static decoration, smaller textures, `LevelOfDetail` on static models.
- Too many shadow-casting lights, particles, transparent overlap -> budget them per scene.
- Unanchored decoration, needless collisions -> anchor, `CanCollide/CanTouch/CanQuery = false`.
- Remote spam -> snapshot + diffs, batching, unreliable channel for replaceable data.

## Luau hot paths
Cache services and repeated lookups outside loops; avoid allocating tables per frame; let the compiler optimize (no manual unrolling folklore); profile GC cost too.

## Parallel Luau
Only after profiling finds isolatable CPU work. Actors compute in parallel, `task.synchronize()` before DataModel writes; no shared module state across Actors (`SendMessage` copies; `SharedTable` for shared state).

## Streaming
On by default; streamed-out instances are parented to nil, not destroyed. Bounded `WaitForChild`, server-side full-world queries. Tune radii from data.

## References
- `references/performance.md` - tools, targets, pooling, streaming, mobile (roblox-brain).
- `references/performance-quick.md`, `performance-profiling.md` - common problems table, rendering and physics notes, profiling workflow (EL4CTEO).
