---
name: roblox-testing
description: Test Roblox games - testable pure modules, Lune and Jest Lua unit tests, negative controls, live Studio checks in the right VM, multi-client and device tests, evidence tied to the exact revision, and QA before shipping. Load when writing tests, building a test gate, or proving a feature works.
---

# Testing Roblox games

## Make code testable
- Put rules in pure modules (no Instances, no services) and inject dependencies (clock, random, data store) so tests can replace them.
- Test the pure rules outside Studio with Lune (fast, CI-friendly) or Jest Lua; keep engine-dependent checks small and run them in Studio.

## Tests that can fail
- Every rule gets at least one negative test that must fail on wrong code (a deliberately broken input or implementation). Run the negatives too: a suite whose negatives pass is not testing anything.
- Assert exact values, not "it ran". Include forged/invalid requests for every remote (expect refusal).

## Live checks in Studio
- Verify Studio runs the code you think it does: compare a hash or version marker of the key modules in the live session against the repo revision before running checks (stale sync silently tests old code).
- Run each check in the right VM (Edit, Server, Client) of a fresh play session; boot the game's services once per session, not per check.
- Multi-player behaviour needs a multi-client test (Studio Clients and Servers, or the MCP multiplayer playtest); some things (friends, parties, purchases, real latency) only a live published test can prove - mark those as manual owner steps.
- Screen and UI checks at a fixed size list (desktop and phone, both orientations) with the viewport read back; see `roblox-ui`.
- Performance and timing only on real-speed rendering (`roblox-performance`).

## Evidence
- Bind evidence to the exact commit: raw output lines saved with the revision, never a summary typed by hand.
- Re-run only what failed or changed; carry passes forward by content hash so a gate does not redo hours of passing work.
- A regression index (one row per suite: file, VM, what it covers) lets a change run the rows it touches plus a full sweep before release.

## QA before shipping a feature
Normal flow, failure and retry, leave/rejoin, rapid input, every input type, phone and desktop sizes, server shutdown during the feature, and data saved and restored.

## References
- `references/testing.md`, `testing-cloud-test-runner.md` - testable modules, Jest Lua, where tests run, Studio playtesting, cloud test runner (EL4CTEO).
