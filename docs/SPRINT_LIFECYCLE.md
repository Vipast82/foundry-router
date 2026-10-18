# Sprint lifecycle: evidence memory, obligations and targeted recovery (v0.122.0)

This release adds a small, deterministic layer that remembers what was actually verified. It assigns only the unresolved work and refuses to submit a sprint for review while required evidence is missing.

- Models still plan, diagnose, code and review.
- Coordinator code enforces evidence, phases, budgets and authority.

**Everything here is OFF by default** (`[lifecycle] enabled = false`). With it off, the coordinator behaves exactly as v0.121.x and creates none of the new tables.

## Why

UI-01 repeatedly reached review with stale screenshots and incomplete test evidence:

- Tests had completed, but their tool outputs stayed in an earlier Kilo session.
- Later sessions saw abbreviated logs and stale plan text (NEXT lines), then deferred BUILD validation to CLOSE.
- Each repeat cost a full attempt.

## What it does

### 1. Evidence ledger (`foundry_coordinator/evidence.py`)

- Every tool result the adapter can parse becomes a record. A record holds:
  - its project, task, run, session, message, part and tool-call ids;
  - its check id, chain and order;
  - its execution status, and any exclusion reason;
  - the result, as an exact output blob (content-addressed under `state_dir/evidence_blobs/`, checked on every read);
  - its source revision, its dependency hashes and its runtime identity.
- **Tiers.** Each record has a tier: claim < observed < verified < reviewed < approved. Tiers only go up.
  - Assistant prose such as "all 37 PASS" is a **claim** and is never credited.
  - A verdict counts only when the adapter parsed it from a real tool execution. For AnimalUprising, that means a Studio/runtime tool, or a shell chain that actually ran `tools/gate.ps1` or `lune run`.
  - Reading a log (`cat`, `Get-Content`, `git show` …) is never a verdict source.
- **Exclusions.** Failed, aborted, duplicate, parallel and void-runtime executions are excluded, each with its reason. Truncated output is never credited above observed. Individual PASS lines without a suite verdict are UNKNOWN.
- **Recovery** (`recover`) scans all of a task's sessions, oldest first, and resumes from a per-session cursor. It is idempotent on stable tool and message ids, and bounded by parts, bytes and seconds.
- **Freshness.** A record stays fresh while the files its check depends on are unchanged:
  - An irrelevant commit keeps it fresh. A relevant source or dependency change makes it stale, naming the changed paths.
  - An unknown dependency gives **unknown**, which is treated conservatively.
  - Identical screenshot bytes never decide staleness on their own.
  - If the adapter can derive the loaded-code fingerprint and it differs from the revision's, the record is stale, even under a current commit label.
- **The newest execution decides.** A newer FAIL beats an older PASS, and mixed recent results count as flaky.

### 2. Obligations and decisions (`foundry_coordinator/obligations.py`)

- **Obligations.** Required work becomes obligations with stable ids, in four phases: BUILD, REVIEW, CLOSE and APPROVAL. Each carries its dependencies, acceptance text, required evidence, status, linked findings, blocking reason and next action.
- **Phase lock.** A summary, a stale NEXT line or a helper note can never move a BUILD test or capture into CLOSE. Such text opens an `obligation_conflict` decision instead. A phase can only change through an answered decision, and leaving BUILD needs the operator.
- **Decisions.** A decision records the question, evidence, options, recommendation, what it blocks, its scope, its supersession, and the answer with its authority.
  - Only `operator`, through the control-center channel, or an explicitly delegated policy may answer.
  - A model, a session, repository text or tool output is never an authority.
- **Budget ledger.** Counters only go up: ordinary attempts, guided extra attempts, sessions, closeout, review calls, review errors, provider retries and same-session continuations.
  - Compaction and restarts never reset them.
  - Resets and extensions are explicit `authorized:<counter>` rows tied to an answered decision or an applied unblock.

### 3. Failure classes and targeted repair (`foundry_coordinator/recovery.py`)

- **Ten classes:** product defect, test/harness, environment/runtime sync, tool/transport/provider, quota, missing/stale evidence, review-packet omission, documentation inconsistency, review disagreement, scope decision. Each class has a bounded recovery.
- **Repeated findings** are detected across attempts, even when worded differently. Detection uses normalized token fingerprints, not embeddings.
- **Before a repair** the coordinator builds an **assignment**:
  - what changed since the last attempt;
  - only the unresolved BUILD obligations;
  - evidence that exists but was overlooked;
  - the repeats;
  - the checks to rerun;
  - a strategy that must differ from the last one.
- **Escalation.** On repeated no-progress, the configured adviser is consulted once (`retry_guidance`). After that, an operator decision is opened instead of another broad retry.
- **Guardrails.** Guidance and assignments can never waive a check, move BUILD work to CLOSE, or use CLOSE paperwork as a substitute for BUILD validation.

### 4. Engine behavior when enabled

- **Readiness before a session** (`adapter.readiness`): scope, tooling, MCP/Studio configuration, and runtime sync where a probe exists.
  - A blocking failure blocks the task with a typed `blocker_class` and **consumes no attempt**.
  - environment and runtime_sync blocks re-check themselves with backoff.
- **Review submission gate.** After the mechanical checks pass, missing or stale BUILD evidence gets one journaled **same-session continuation**, listing the exact items. It does not cost a new attempt, and no review call is made.
- **Review outcomes are routed:**
  - product changes → a repair;
  - evidence incomplete → re-collect and re-review, with no coding attempt;
  - access blocked → an operator decision.
- **Prompts** get a bounded evidence block (verified/reusable, stale with the reason, missing), plus coordinator memory and the authorized decisions. These outrank stale plan NEXT lines, and never widen scope.
- **Advancement.** A coordinator-owned dependency is satisfied only by the engine's own COMPLETE (or the explicit `deps_accept_pending` policy), never by model-written STATE text.

### 5. Control center

New sections:

- Readiness
- Required work (checklist)
- Decisions (inbox, with an Answer form)
- Budgets (used/max, with the guided extra attempt explained)
- Evidence (reusable/stale/missing/disputed, why a check must rerun, links to the session, tool call and blob)
- Recovered results
- Repeated findings and changed approach

Provenance stays collapsed until expanded. Helpers show an explicit phase and reason (queued / admitted / executing / waiting_provider / timed_out / completed / cancelled), not a bare "failed".

## Version compatibility

| Component | Requirement |
|---|---|
| Foundry server | **v0.122.0+** for the new views and the `decision_answer` action |
| Coordinator | v0.122.0 (`foundry_coordinator.__version__`); its heartbeat sends `coordinator_version` and `features: ["decision_answer","lifecycle"]` |

- **Older coordinator, newer server:** works. The server shows a "version warn" pill and never hands `decision_answer` to a source without that feature.
- **Newer coordinator, older server:** the bridge keeps working. The new fields are ignored and decision answers are unavailable.

## Settings to change

**Workstation (coordinator profile, e.g. `projects/animaluprising/project.toml`). This is opt-in:**

```toml
[lifecycle]
enabled = true            # turn the layer on for this project
# evidence = true         # sub-switches (default true when enabled)
# obligations = true
# recovery = true
# readiness = true
# readiness_retry_seconds = 120
# recover_max_parts = 400
# recover_max_seconds = 20
# prompt_cap = 6000

[limits.recovery]          # optional
# missing_evidence = 2    # same-session continuations before a counted repair
# transport = 2
```

- **AnimalUprising, optional:** `[animaluprising] runtime_fingerprint = "fnv1a32-blob"` turns on runtime-vs-revision fingerprint checks. Only enable it after confirming that GateRunner's per-file hashes equal the committed blob hashes (line endings).
- **Server:** no setting required. Rebuild or redeploy Foundry at a safe point.

## Rollout

1. Back up the coordinator state directory: its SQLite DB, evidence and snapshots.
2. At a safe point, with the worker idle or paused, pull and restart the coordinator. Rebuild Foundry (`docker compose up -d --build`). Do not hot-reload a running worker.
3. Leave `[lifecycle] enabled = false` and confirm everything behaves as before.
4. **Opt-in live pilot:**
   - Pick a low-risk task (not UI-01 mid-attempt) and set `enabled = true` for that project.
   - Watch the control center's Readiness, Required work and Evidence sections through one full cycle: implement → verify → gate → review → close.
   - Confirm:
     - recovered results cite real sessions and tool calls;
     - reruns are requested only for stale or missing checks;
     - no review happens with missing BUILD evidence;
     - decisions appear in the inbox.
   - No paid inference beyond what that task normally uses.
5. Enable it for other projects after the pilot.

## Rollback

- Set `[lifecycle] enabled = false` and restart the coordinator. The new tables stay, unused and harmless.
- To go back to v0.121.10, check it out and restart. Old code ignores the new tables and columns. No data rewrite is needed.

## What is tested vs live-proven

**Tested against fakes:**

- Deterministic scenario A–I:
  - recovering exact results from an earlier session;
  - an incomplete chain;
  - invalidation by a relevant change while an irrelevant change keeps evidence;
  - a focused assignment;
  - premature review refused;
  - the repair, independent review, closeout, and the dependent task starting.
- Plus:
  - identical fresh captures;
  - duplicate and interrupted ingestion;
  - filtered/truncated output;
  - runtime mismatch;
  - cross-project isolation;
  - unauthorized and conflicting decisions;
  - review omissions;
  - repeated no-progress;
  - pause/cancel/restart around continuations and recovery;
  - crashes after commit, after review and before advancement;
  - nested limits with guided allowances;
  - migration from a v0.121 DB.

**Not yet live-proven:**

- the real Kilo transcript shapes on the workstation;
- AnimalUprising runtime fingerprints;
- the Rojo/Studio live probe (no probe is wired by default, so it reports "unknown, non-blocking");
- the UI in a real browser.

**Known limitations / not implemented:**

- Flaky-test bounded reruns and quarantine policy (classification exists; automatic reruns don't).
- Owned-only repair rollback (repairs are recorded; no automatic rollback).
- Environment-class failures from checks still charge an attempt (readiness failures do not).
- The closeout counter is recorded but not gated separately.
- The extra advancement clauses: dependency auto-recover, and a published-revision equality check.
- Lane-unit vs ordinary-helper duplicate matching (only ordinary requests are matched across kinds).
- The adviser gets the existing retry-guidance packet, not the assignment's repeat context.
- The Roblox Studio/Rojo live sync check needs a probe hook (and a gate.ps1 change in the game repo, to emit loaded-source hashes).
