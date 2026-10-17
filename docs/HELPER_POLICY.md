# Optional coding helpers and quota reserves

Foundry v0.117.0. Frontier helpers can do bounded, optional coding work alongside the
coordinator's single primary writer. They only run while the subscription account that
would serve them has headroom. Required work keeps its own existing guardrails.

## Two kinds of work

| Kind | Purposes | Admission |
|---|---|---|
| **Required** | `implementer`, `planning`, `review`, `closeout`, `blocker_diagnosis` | Unchanged existing guardrails: Meridian tier conservation and window floor, the CLIProxyAPI usage limit (85%), call budgets, helper streams. |
| **Optional** | `code_proposal`, `test_design`, `debug_advice`, `exploration` | The helper gate below, in addition to every existing guardrail. |
| **Unmanaged** | traffic without a coordinator binding (manual Kilo, Cline, scripts) | Unchanged guardrails. Not attributable to a purpose and not treated as either kind. |

How each purpose is assigned:

- **Where it's set.** Only the coordinator's Foundry binding sets it (`POST
  /v1/foundry/work/sessions`, orchestration scope). No request header sets it, and a model
  can't choose it.
- **Spoofed session ids.** When the binding names the client that runs the session
  (`worker_client`, e.g. `kilo`), another client presenting that session id gets the
  purpose `unverified`, which is treated as optional.
- **Model-requested helpers.** Helper requests in a model's report (`helper_requests`)
  are always optional, whatever they call themselves.
- **Child sessions.** Helpers Kilo's `task` tool starts are classified by agent name from
  the session title `(@agent subagent)`: `au-planner` is planning, the reviewers are
  review, and any other agent is optional exploration.
- **Required allowance.** More than `required_helper_sessions_per_run` (default 2)
  planning or blocker-diagnosis sessions in one run count as optional.

## The gate

Optional work is admitted on an account only when **every** applicable window is below
`cutoff_pct` (70) **used**. "Applicable" means the session window, the weekly window, and
any model-scoped bucket that covers the model; 70% used is 30% remaining. Every reading
must also have been **observed** upstream within `max_observation_age_seconds` (30 min).

A window with any of the following counts as **UNKNOWN**, and UNKNOWN is never admitted:

- no session-window or no weekly-window reading
- no utilization figure
- an observation older than the limit
- an observation from before the window's reset
- an unreachable or malformed source
- a reading contradicted by a live call

Missing data is never treated as 0%. Fetch time and observation time are shown
separately. CLIProxyAPI re-serves the last headers each account saw, so refreshing
Foundry's cache doesn't make an old observation new. Only a real request through that
account does, and Foundry never sends a dummy generation to manufacture one.

**When the cutoff is reached**

- A call already running finishes. A primary writer, Studio test or required review is
  never interrupted.
- No new optional call or helper unit starts on that account.
- Admission resumes only once every window is below `cutoff - hysteresis_pct` (65) **and**
  `cooldown_seconds` (600) have passed. The state is stored per account and model, so a
  restart doesn't reset it, and a model-scoped bucket doesn't block the account's other
  models.

**Which account serves the call**

| Backend | Account judged |
|---|---|
| Meridian | The Claude account behind that backend (or its `meridian_profile`). Readings from the auth companion (`meridian.usage_profile`, live Anthropic `/api/oauth/usage`) are observed at fetch time. Meridian's own `/v1/usage/quota` doesn't say when it observed usage: UNKNOWN unless the backend is listed in `helper_policy.trust_fetch_time_for` (shown as an assumption). |
| CLIProxyAPI | It can't say which signed-in account will serve a request, so **every** enabled account of the model's provider must pass. A low-usage account never authorizes a call another account may serve. |
| Other subscription | No quota source: UNKNOWN. |
| Metered paid API | Never used for optional work. |
| Local | No subscription quota. Its slot limits still apply. Coordinator helpers don't use the local backend; the primary holds its only slot. |

**Where it's enforced**

- The ledger checks the gate before a call is reserved. It checks again while the call
  waits for a helper stream, and once more after the backend queue lets it start.
- This covers strict and direct personas and every fallback route. An optional call
  whose fallback account is over the cutoff is refused there, never served.
- Each decision is stored in `helper_admissions`, and every ledger row (`usage_events`)
  records its purpose.

## Coordinator helpers

Enable them with a profile section:

```toml
[helpers]
enabled = true
order = ["meridian", "cliproxyapi"]        # preference: Claude first
selective = ["cliproxyapi"]                # ChatGPT only for units marked value = "high"
agent = "au-helper"                        # a READ-ONLY Kilo agent (see below)
worker_client = "kilo"                     # the Foundry client name Kilo's key uses
max_running = 3                            # never more than the backend's helper streams
poll_seconds = 60                          # minimum gap between admission checks per unit
timeout_minutes = 30                       # a helper session's own limit
handoff_wait_minutes = 15                  # an idle primary waits this long for ADMITTED helpers
max_unit_requests = 2                      # a skipped/timed-out unit may be asked for again once
max_requests_per_report = 4

[helpers.personas]                         # backend name -> helper persona
meridian = "Foundry-Kilo-Helper-Claude"
cliproxyapi = "Foundry-Kilo-Helper-GPT"
```

**Where units come from:** a json task's `helpers: [{responsibility, kind, prompt, files,
value}]`, or the primary's report `helper_requests`. There is one unit per responsibility,
and files outside the task's scope are dropped.

**How the primary asks.** When `[helpers]` is enabled, every implementer prompt (any
adapter, AnimalUprising included) gets a short "Optional helpers" section. It tells the
primary to add this to its `coordinator-report` block and stop its turn:

```json
"helper_requests": [{"kind": "code_proposal | test_design | debug_advice | exploration",
                     "responsibility": "<one distinct part>", "prompt": "<what you need>",
                     "files": ["<paths inside the task scope>"]}]
```

The same section says: helpers only read; proposals arrive later as advisory text; a request
may be deferred or skipped; don't use the task tool for `au-helper`; don't ask helpers to
review. The Kilo agent and its skills are unchanged.

A request is always optional work, normalized as follows:

- An unknown kind (`review`, `planning`, …) becomes `code_proposal`.
- `value` is always `normal`, so the selective ChatGPT backend is never self-awarded.
- Files outside the task scope are dropped.
- Each report is consumed once (hashed in the task data), at most `max_requests_per_report`
  requests.

**What happens to each unit**

1. **Check.** Foundry's admission check (`GET /v1/foundry/helpers/admission`) reads quota
   only; it makes no inference call.
2. **Defer or start.** A deferred unit gets a recorded reason and is checked again after
   its next eligible time or the poll interval, so there's no retry storm. An admitted unit
   gets a fresh Kilo session, bound with its purpose, parent session, run and assignment
   **before** its first prompt, so every call it makes is gated.
3. **Read-only.** Session rules deny edit, write, patch, bash, task (delegation), todo and
   Studio MCP tools, and the prompt repeats the rules.
4. **Hand-off.** The final message is stored as a proposal. When the primary is idle (a
   safe point) the proposals go to it once, as advisory text. The primary integrates,
   tests and commits.
5. **Restarts.** A unit is never started twice (keyed operation journal).

**The safe point: the primary went idle (v0.117.1).** The engine decides in this order
before BUILD can move to VERIFYING:

1. **Consume.** The new report's `helper_requests` become units, and eligible ones are
   dispatched right away (admission check, binding, prompt). Before v0.117.1, report
   requests were read only on the next IMPLEMENTING tick, which never came, so they were
   lost.
2. **Deliver.** Returned proposals go once to this primary session, which continues
   (still IMPLEMENTING).
3. **Wait (bounded).** If admitted helpers are still running, the primary waits for them,
   for up to `handoff_wait_minutes` from the first idle tick (persisted, so a restart
   doesn't reset it). After that the helpers are aborted, recorded as `timed_out` and their
   bindings ended. A result arriving after that is never read or delivered, and doesn't
   count as authorship.
4. **Skip.** Units never admitted (DEFER, UNKNOWN, no persona, failed binding) don't hold
   the primary. They become `skipped`, with the last admission reason kept.
5. **Tell the primary.** If the report that asked was not `DONE` (the primary stopped to
   wait) and no proposal came, the primary gets one message listing each unit's outcome and
   continues alone. If the report was `DONE`, the task goes to verification.

Further rules:

- A proposal is only delivered while the task is IMPLEMENTING, to its current primary. It
  never reaches a revision that is being verified or reviewed.
- Units done but not delivered when the task leaves IMPLEMENTING (e.g. on BLOCKED) reach
  the next implementer session's first prompt and so become part of a new revision, which
  is reviewed again.
- Pause and cancel abort running helpers (`cancelled`).
- A skipped, timed-out, failed or cancelled unit can be asked for again by a later report,
  up to `max_unit_requests` in total. Delivered or running units cannot be.
- Every delivery is journaled before it's sent (`deliver:` operation). If the coordinator
  dies mid-send, it doesn't resend. It logs `helper_delivery_uncertain` and counts the unit
  as delivered, so review stays independent of a possible author.
- A helper session is journaled as `started → bound → done`, so a restart between creation
  and binding binds before the first prompt; it never runs unbound.
- Events: `helper_requests`, `helper_unit`, `helper_deferred`, `helper_started`,
  `helper_handoff_wait`, `helper_proposals_delivered`, `helper_handoff_timeout`,
  `helper_timed_out`, `helper_skipped`, `helper_outcome_delivered`, `helper_cancelled`.

**Review independence:** if a persona that proposed code integrated into the revision is
also the reviewer, the review moves to the other reviewer role. If no independent reviewer
is configured, the task is BLOCKED; review is never waived. The review packet lists code
authorship.

**Kilo side, which you configure:**

- Add an `au-helper` agent with read-only permissions: no edit, bash or task, no Studio
  MCP. Keep `subagent_depth: 1`, the read-only local Explore agent, and the denial of
  delegation to local Explore.
- Create the two helper personas: strict routing, frontier models only, and allowed on
  Kilo's client key. They aren't created automatically.

## Control center

Find these in the inspector section **Helper capacity & quota**:

- the effective policy and per-backend ceilings, shown separately from current activity
  (active sessions, calls in flight)
- each account's windows with used %, observation age, fetch age, reset and state
  (ADMIT / DEFER / UNKNOWN / BLOCKED), with a re-check button that reads quota only
- helper units: active, waiting (deferred), returned, delivered or failed, with persona,
  backend, reason and next eligible time

Account identities are redacted.

**Settings:** ⚙ → **Helper policy & capacity (Foundry, shared)** edits `helper_policy` and
each backend's `helpers_allowed` / `max_concurrent`. Changes are saved to `config.yaml`,
appear in config history and are logged in Events. An unreadable quota source raises one
persistent **warning** alert (no sound) and resolves itself when readings return.

## Workstation status (2026-10-17, read without inference)

**Backend limits (unchanged)**

| Backend | Helpers allowed | Concurrent calls | Notes |
|---|---:|---:|---|
| `meridian` | 3 | 4 | |
| `cliproxyapi` | 3 | 4 | usage limit 85% |
| `strata` | 0 | 1 | |
| `truenas-ollama`, `llama-qwen38-mtp` | — | — | disabled, untouched |

Meridian conservation is premium 1.0, strong 1.0 and Fable 0.8, with
`min_window_fraction` 0.05 and `usage_credits: never`. Usage comes from the auth companion
(`usage_profile: victor`), so readings are live.

**Readings at the time**

- **Meridian 1.82.0 (Claude Pro):** 5-hour 16%, weekly 22%, Fable weekly 33%.
- **CLIProxyAPI (one ChatGPT Plus account):** 5-hour 34%, weekly 41%, observed about 20
  minutes earlier.

The 6.7-hour-old reading seen earlier was correct behaviour by CLIProxyAPI: it refreshes
only when a request passes through that account. The gate now shows that age and treats
anything past the freshness limit as UNKNOWN.

**Upstream concurrency (not verified)**

| | Meridian | CLIProxyAPI |
|---|---|---|
| Concurrency setting | none in its `/health` output | none found in its documentation; its configuration needs the management key, which wasn't used |
| Model | passthrough mode runs the Claude CLI once per request | round-robin across signed-in accounts (the default) |
| Practical limit | the Pro account's own rate limits (`rateLimitTier: default_claude_ai`), which aren't visible | with one ChatGPT account, all four calls draw on that single account |

So Foundry admitting four calls doesn't prove four answers are generated at once on either
upstream. Confirming that would need a parallel test that spends quota, which wasn't done.
No setting was changed and no upstream was restarted.

## Rollout

1. **Deploy and restart Foundry.**
   - The migration is additive: new nullable columns (`work_sessions.purpose`,
     `assignment`, `worker_client`; `usage_events.purpose`) and new tables
     (`helper_quota_state`, `helper_admissions`).
   - `helper_policy` defaults to **off**. Until it's turned on, optional-purpose calls are
     refused and nothing else changes.
2. **Control center → Helper policy.**
   - Turn optional helpers on (cutoff 70, freshness 1800 s).
   - Set backend preference `meridian, cliproxyapi`, with `cliproxyapi` as high-value only.
   - Leave the backend ceilings as they are (already 3/4, 3/4, 0/1).
3. **Kilo:** add the read-only `au-helper` agent and the two helper personas (above).
   Validate each persona once, explicitly; that costs about five subscription calls each.
4. **Coordinator profile:** add `[helpers]` (above). Restart the coordinator loop at a safe
   point (see the runbook). It changes the profile hash, which the engine records.
5. **Watch:** check the Helper capacity section, then confirm one deferred and one
   admitted unit on a low-risk task before relying on it.

   v0.117.1 is required: v0.117.0 never dispatched report requests.
