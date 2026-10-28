# Plan: capture every model call for fine-tuning the local model

Status: **waiting for Victor's approval** (nothing built yet). Target release: v0.153.0
(the number is settled with the session that releases from `_base` when this lands).

Victor, 2026-10-27: "track EVERYTHING so I can use for finetuning the local model".

## 1. What the live data shows (training store `~/.foundry-training`, read-only, 2026-10-27)

| Fact | Number |
|---|---|
| Episodes in the store | 851 (629 + 170 excluded, 28 duplicate, 24 eval, **0 selected**) |
| Top exclusion reasons | review_missing 815, non_local_author 761, unverified 711, broken_tool_sequence 208, truncated_tool_output 152 |
| Captured model calls | 13,200 (local Qwen 4,865; Claude 6,939; GPT 1,396) |
| Local calls the router rates "ok" (succeeded, no thumbs-down, no negative label) | 1,477 |
| Local calls with a positive coordinator label (gate PASS / review APPROVED) | **10, and none of them is also "ok"** |
| Frontier calls with a positive label and no negative | ~250 |
| Calls that carry reasoning text | 5,192 |
| Store size | raw 11.1 GB + normalized 10.8 GB for 13k calls (~400 KB per call on average) |

### Where the 152 "truncated tool outputs" come from
They are **not data loss**. Every one comes from Kilo's `read` (330 results) and `grep` (52)
tools, and their "truncated" flag means *the tool paged its output*: "Showing lines 1-120 of 304.
Use offset=121 to continue." I compared the 262 flagged results that appear in the captured
model context with Kilo's stored output, and **all 262 are byte-identical** to what the
model saw. The Kilo adapter (`foundry_training/adapters/kilo.py:281`) turns Kilo's
`metadata.truncated` into `capture_status = "truncated"`, and curation then excludes the episode.
**Source fix:** tool-side paging is recorded as `facts.tool_paged` and is not a capture defect.

### Where the 208 "broken tool sequences" come from
- **98:** caused only by the false "truncated" flag above (`complete_tool_sequence` is false
  whenever any result is flagged). The fix above clears them.
- **52:** the episode ends on an assistant turn with tool calls that never got an answer. The
  call after it was **cancelled** in 34 sessions (the coordinator stopped the session, or the
  stream was aborted). In 17 the session simply ended there, and 1 was a backend error. 42 of
  the 52 are helper sessions. **Fix:** the episode builder drops that unanswered final turn, ends
  on the last complete assistant turn, and records `flags.trimmed_open_tail`.
- **~58:** truly missing results (Kilo result still `pending`/`running`). They stay excluded,
  because a result must never be invented.

### A third cut-off source: 74 calls whose capture record was cut at `max_body_bytes`
71 of them are `visual_reviewer` calls. Their inline base64 screenshots push the body past
4 MB, and the record then keeps only its head. **Fix:** capture replaces inline images with
`{sha256, bytes, mime}` and writes the bytes once to `<data>/training/capture-media/`.

### The Training tab's "Capture gaps" list is mostly false `no-system-prompt` rows
Reported from the "Full thinking stream" session (Victor's screenshot, v0.152.1). Today all
175 gap rows in the store are `no-system-prompt`, one per Kilo session. `pipeline.ingest_kilo`
(~line 96) records that gap for every Kilo session it ingests and never checks the Foundry
capture.
- **144 of the 175 (82%) are false.** The session's Foundry capture (with the system prompt and
  tool definitions) is already in the store under the **same session id**. The join keys line
  up: Kilo's `ses_…` id is the session id Foundry records. Those episodes already use the
  captured context.
- **31 are real.** No Foundry capture exists at all in their time window (mostly 10-17, before
  capture was on for them). 1 session has a captured call nearby under another session id,
  probably a child session. That one is checked during the build.
- The tab shows the last 50 gaps, so the false rows hide the real ones.

### Smaller findings
- `frontier_assist` calls run inside a Kilo request, so today they are captured **under the
  same session** as the agent's turns. `_from_foundry` builds the episode from the session's
  last good call, and that can be the frontier helper's one-shot system+user call. That is
  wrong. The purpose tag fixes it: episodes use only `agent_turn` calls.
- Operator chat (`chat_engine._call_model`) runs with an anonymous client context. With
  `include_unbound: true` it would already be captured **implicitly**. It becomes an explicit
  switch instead.
- Unbound captures (Cline, Chat, AnythingLLM, the brain) are **skipped by the pipeline**:
  `Ingestor.run` drops rows with no project. Turning `include_unbound` on in the router alone
  would fill capture.sqlite and never reach a dataset.

## 2. What gets built

### A. Router capture: every call, tagged with a purpose
`foundry_router/capture.py`
- A new `purpose` column (added with `ALTER TABLE` on old stores) and a `purpose` context var.
  `with capture.purpose("frontier_assist"):` sets it, and `begin_call(..., purpose=)` overrides it.
- Purposes:

  | purpose | where | how |
  |---|---|---|
  | `agent_turn` | client turns through the pool (facade, brain agent workers) | default |
  | `brain_route` | `brain/client.py` → `protocol.chat` (sub: routing / assist / background) | new begin/finish around the direct call |
  | `frontier_assist` | `frontier_assist.call_frontier` | purpose context around `pool.chat` |
  | `agent_external` | `agents.py`: Hermes as a backend (`chat_stream`) and as a tool (`_tool_run`) | new `record_event`: input messages, final text, tool-progress lines, run id, status. Hermes' own model calls back into Foundry are already captured as `agent_turn` under the agent's client. |
  | `media` | `media.py` `_run`, at the job's end | `record_event`: op, prompt, model, settings, input image hashes, artifact ids/sha256/size, status, error. No bytes are copied: they stay in the media store. |
  | `operator_chat` | `chat_engine._call_model` | captured only with `operator_chat: true` |
- New config keys (`TrainingCaptureConfig`): `purposes: []` (empty = all),
  `exclude_purposes: []`, and `operator_chat: false`. Internal clients (health probes, persona
  validation) are still never captured.
- Inline images in the request are swapped for references (the 74-call fix above).
- The Training tab status shows today's counts by purpose next to the existing counts by
  backend and persona.

### B. Reasoning kept when `include_reasoning = true`
- `adapters/foundry.py` already stores `response.reasoning`. `episodes._from_foundry`
  attaches each call's reasoning to its own assistant turn (matched like authorship) in a
  separate `reasoning` key. It is never merged into `content`.
- `export.py`: with `[training.export] include_reasoning = true`, chatml-tools lines carry
  `reasoning_content` on the target assistant turns. That is the field the Qwen3 chat template
  renders as `<think>…</think>`. The manifest sets `reasoning_included: true`.
- `validate.py`: a reasoning field is an error only when the manifest says reasoning is off.
- Note: Qwen3's template drops `<think>` from turns before the last user message, so
  reasoning SFT is cleanest on the call-level dataset (D below), where each example has one target
  turn. The default stays off.

### C. Failed attempts become labelled negatives
- `foundry_coordinator/training_hook.finalize` accepts the outcomes `BLOCKED` / `FAILED` /
  `CANCELLED`, scoped to the attempt: the stopping run's sessions plus
  `"attempt_end": {state, class, blocker}`.
- `engine.py`: the existing best-effort exit hook (line ~345, today only FAILED and
  AWAITING→COMPLETE) also fires on BLOCKED and CANCELLED. A recovery-case BLOCKED (the lead
  is still working on it) waits until the case ends. There is one manifest per
  task + run + state, deduped through `task.data.training_manifests`, so a task that bounces
  BLOCKED→READY→BLOCKED doesn't spam the spool.
- `episodes.py`: `outcome.attempt_end` comes from the coordinator's own `task_transition`
  events for the episode's run. A later COMPLETE of the same task does not relabel the
  earlier attempt.
- `curate.py`: the new exclusion reasons are `attempt_blocked` / `attempt_failed` /
  `attempt_abandoned`. These episodes are never positive data. They are kept for evaluation
  sets and as `rejected` candidates in C/D.

### D. A looser call-level dataset, beside the strict episodes
- `foundry_training export --kind calls` (and in `[training.auto] kinds`) makes one example
  per captured call: the effective prompt plus the reply as the only target turn (earlier turns
  `weight: 0`).
  - Filters: `quality = ok | verified` (default `verified`, `ok` opt-in) and
    `purpose = agent_turn` (default).
  - **Local-authored replies only** while `allow_frontier_targets = false`.
  - The usual eval-leakage guard, redaction scan, chatml validation and token limit apply, and
    the result is marked `"label_strength": "router_ok" | "router_verified"` in the manifest
    and on every line.
- **Unbound traffic reaches the pipeline:** an optional `[training] unbound_project =
  "foundry-unbound"` maps rows with no project (Cline, Chat, AnythingLLM) to a pseudo-project
  with only the `foundry` source. They feed only the calls dataset; there are no episodes,
  because there is no task.
- **DPO pairs from reviews and gates** (`kind = preference`, basis recorded per pair):
  - P1 *same context*: two calls with an identical request context, one with a positive router
    label (review APPROVED, gate PASS, vision approved) and one with a negative label. This
    extends today's strict rule, which only looks at session verification.
  - P2 *same task prompt*: two attempts at one task whose system and first user turn are
    identical. Chosen = the attempt the frontier reviewer approved (or the gate passed).
    Rejected = an attempt with review CHANGES, gate FAIL or `attempt_end` BLOCKED/FAILED (from C).
    Conversational chosen/rejected lists in TRL's format.
  - Both sides must be local-authored while `allow_frontier_targets = false`.

### F. Capture gaps that are true
- `ingest_kilo` no longer writes `no-system-prompt` unconditionally. Foundry capture is often
  ingested *after* Kilo in the same run (`process_queue` runs Kilo before Foundry), so the gap is
  decided once both are in: at `curate()`, a Kilo session gets the gap only when no Foundry
  `model_call` / `model_request` row carries its session id.
- Stale rows are removed the same way. Each curate pass deletes the `no-system-prompt` gap of
  every session the join now covers. The 144 false rows go away on the next pipeline run, with
  no manual cleanup.
- `store.resolve_gaps(project, kind, sessions)` is a small new helper. Other gap kinds are left
  alone.
- If the join turns out to miss real matches (the one child-session case above), the build fixes
  the join through the `parent_child` correlation rather than hiding the gap.

### E. Release note with the router-host keys
Written into the release note exactly like this (Victor flips them in `/config/config.yaml` on
the router host, then restarts the router):
```yaml
training_capture:
  enabled: true
  projects: []              # every coordinator-bound project
  include_unbound: true     # Cline, Chat, Research, AnythingLLM, brain, media
  retention_days: 30
  max_body_bytes: 4000000   # images no longer count toward this (they become references)
  operator_chat: false      # true = also capture the Automation chat
```
And on the pipeline side (the profile's `[training]`), to get data out of it:
`unbound_project = "foundry-unbound"`, `[training.auto] kinds = ["sft", "correction",
"multimodal", "calls", "preference"]`, and optionally `[training.export] include_reasoning = true`.

### Tests (new or changed)
- capture: purpose tagging for each hook; the operator_chat switch; internal clients still
  skipped; image references; migration of an old store without the `purpose` column.
- brain client / frontier_assist / agents / media: one capture row each, with the right purpose,
  and nothing written when capture is off.
- kilo adapter: paging is not truncation. Episodes: a cancelled final call trims the open tail;
  only `agent_turn` calls build an episode; reasoning is attached per turn.
- export: `include_reasoning` on/off, validation follows the manifest; the calls dataset
  (quality filters, local-only, eval guard); preference P1/P2.
- coordinator: BLOCKED / CANCELLED manifests, dedupe, recovery-case wait, labels survive a later COMPLETE.
- capture gaps: a Kilo session with a Foundry capture row of the same session id gets no
  `no-system-prompt` gap, even when Foundry is ingested after Kilo. A session without one still
  does. An old false gap row is removed on the next curate.
- Full suite before the release note.

## 3. What this yields today (estimate on the current store)

| Dataset | Today | Why |
|---|---|---|
| Strict SFT episodes | still ~0 | 815 have no review; that's the coordinator's review coverage, not capture |
| truncated_tool_output | 152 → ~0 | paging no longer counts |
| broken_tool_sequence | 208 → ~58 | only results that really are missing stay |
| Calls, `quality=verified`, local | ~0–10 | the local model rarely reaches a gate PASS / APPROVED today |
| Calls, `quality=ok`, local | **~1,477** | weak label: succeeded, nobody rated it down, no negative label |
| DPO P1/P2, local both sides | a handful | only 5 tasks have local trajectories |
| Reasoning-bearing calls | 5,192 | usable once `include_reasoning = true` |
| `no-system-prompt` gaps | 175 → 31 | only sessions with no Foundry capture stay |

Data grows from here on: B and C start labelling every new local attempt, and E brings in
Cline, Chat and AnythingLLM traffic.

## 4. Decisions for Victor

- **D1. Frontier outputs as training targets: keep `allow_frontier_targets = false`?**
  (recommended) ~250 verified frontier calls would become SFT/DPO data if it were true, but the
  provider terms (Anthropic, OpenAI) restrict using outputs to build competing models. This
  plan doesn't change the switch. It's your call, per project.
- **D2. Operator chat: opt-in, off by default?** (recommended)
- **D3. Media: metadata + prompt + artifact hashes only, no image bytes copied?** (recommended)
- **D4. Storage.** Capturing everything will grow the stores past today's ~22 GB. Average
  context is ~400 KB per call, and each Kilo call re-sends its whole history. Keep
  `retention_days: 30` and watch disk. Storing each call as a delta of the previous call
  (sharing the prefix) would cut this by roughly 10× but is a bigger change. It's proposed as
  a follow-up, not part of this release.

## 5. Not in this change
- Raising review coverage (review_missing 815) is coordinator policy, not capture.
- The prefix-delta storage from D4.
- No change to the live config, no tag, no push. Those wait for you.
