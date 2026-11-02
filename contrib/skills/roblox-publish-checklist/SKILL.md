---
name: roblox-publish-checklist
description: Decide whether a Roblox release is ready - define the release, block on persistence, authority, purchase and cloud failures, exercise the shipped surface on every device class, and require evidence for every gate. Load before publishing or updating a live experience.
---

# Publish checklist

## 1. Define the release
List the changed places, scripts, assets, configuration, data schemas, products, supported devices and the rollback path. Mark each gate required, conditional or not applicable before testing.

## 2. Blocking gates (any failure = NOT READY)
- Persistence changed: load, save, migration of old saves, duplicate session, disconnect and shutdown (`roblox-data`).
- Remotes or authority changed: types, bounds, state, ownership, replay and abusive frequency (`roblox-networking`, `roblox-security`).
- Purchases changed: durable, idempotent receipt grants and retries (`roblox-monetization`).
- Cloud auth or webhooks changed: secrets, scopes, signatures, retries, de-duplication (`roblox-open-cloud`).
A failed blocking gate is never averaged into a score. Owner approval can accept a risk; it does not turn a failed or unverified gate into a pass.

## 3. Exercise what ships
- One normal flow plus failure, retry, leave/rejoin and rapid-input cases for each change.
- Every supported input mode and representative screen sizes and device tiers (phone included).
- Errors, memory, frame and network cost, streaming; accessibility basics (touch targets, contrast, non-colour cues, text overflow in other languages).
- Dashboard metadata, permissions, age/content questionnaire and policy; after publishing, confirm the version players actually get.

## 4. Evidence
Every PASS cites a test, log, profiler capture, readback, screenshot or dashboard check; otherwise it is UNVERIFIED.

## Output
`READY`, `NOT READY` or `READY WITH ACCEPTED RISK`; blockers first, then warnings, unverified gates, evidence, the rollback plan, and skipped gates with reasons.

## References
- `references/publish-checklist.md` - release gates and audit detail (roblox-brain).
