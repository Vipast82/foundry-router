---
name: roblox-analytics-liveops
description: Instrument and run a live game - AnalyticsService custom, economy, funnel and onboarding events, economy health, segments, remote configuration (ConfigService), live events and experiments. Load when adding telemetry, reading game health, or planning live operations.
---

# Analytics and live ops

## Instrument from day one (history cannot be back-filled)
- Use the built-in `AnalyticsService`, server-side by default (client only for pure UI interaction).
- Economy events: every currency source and sink with an item/SKU, reporting the balance the committed change returned (never recompute from a replicated value).
- Funnel events for onboarding and key flows (steps may arrive out of order; later steps back-fill).
- Custom events for gameplay signals that matter to a decision; avoid noise.
- Rate limit ~120 + 20 x players calls/minute: batch and aggregate. Dashboards lag about a day; use the real-time event view to validate.

## Read the game's health
- Retention cohorts (D1, D7, D30), session length, onboarding drop-off by step, sink/source ratio and inflation per currency, spender concentration.
- When something breaks: confirm the events are right, find the narrowest broken signal, change one lever, re-measure.

## Live ops
- Tunable values (prices, drop rates, event schedules) in remote configuration (ConfigService experience configs) or server config, not hard-coded, so they change without a code release.
- Live events: scheduled from server time, idempotent start/stop, rewards granted once per player.
- Experiments: one change per test, a holdout group, a success metric chosen before launch.

## References
- `references/analytics.md` - AnalyticsService API, economy health, first-session traces (roblox-brain).
- `references/liveops-quick.md` - events, segments, ConfigService, live ops loop (EL4CTEO).
