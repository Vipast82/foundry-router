# Full-stack gap list and build plan (2026-10-22)

Sources: four audits in the shared notes folder `audit2/` (01 training and metrics,
02 permissions and escalation, 03 chat engine and client, 04 efficiency and failures).
Principle: least tokens, highest quality, no loops, and no human in the path unless it's a
product decision.

## Headline findings

1. **Several paths still end with a human.** Implementer questions, lead escalations, "no
   progress", obligation conflicts and the after-closeout visual check can all stop a sprint.
   Decision timeouts never fire because the recommendation argument is ignored.
2. **Permissions aren't what the GUI says.** No role-rules revision has ever been deployed, the
   coordinator's own subagent rule overrides the GUI's, `kilo.jsonc` is still hand-written, an
   empty edit scope means "edit anything", and the lead's write mode drops every deny rule,
   including the push and history-rewrite denies.
3. **The commit-message rule can only be met by rewriting history**, which is forbidden. That one
   contradiction cost UI-02b a 6.7-hour lead session.
4. **The chat engine is one-shot** and only knows eight queue actions.
5. **Training capture has no per-backend or per-persona switch**, and no quality signal, so all
   229 captured episodes are excluded.
6. **Metrics mix estimates with real numbers**, keep no Strata history and have no sprint
   scorecard.
7. **The client has real bugs**, and the router can take minutes to open its port.

## Workstream W1: no human in the loop (coordinator)

| # | Gap | Fix |
|---|---|---|
| 1 | The implementer's `question` tool goes to AWAITING_HUMAN with no default | Deny `question` for the implementer. Tell it to report BLOCKED with options, which the Lead or a decision default then resolves. Set `snapshot: false`. |
| 2 | `decision_policy.meta` ignores `recommendation=`; defect-class decisions never time out; lead bound, hard-cap, `no_progress`, `recovery_escalation` and `obligation_conflict` carry no default | Require options and a recommendation in every decision. The default for bound and hard-cap escalations is "stop the task and queue the next"; for `no_progress` it is "continue once, then stop". Timeouts apply to every decision kind. |
| 3 | The after-closeout visual check re-asks for frames already accepted with notes, then dead-ends on "uncertain" | Carry a prior approval when the frame hashes match. Never re-ask frames accepted with notes. Retry "uncertain" once, then accept with notes. |
| 4 | `coordinator.commit_messages` can only be satisfied by a history rewrite | The coordinator rewords its own unpushed commits itself, or marks the check advisory for `revert:` and merge commits. The session is never asked to rebase. |
| 5 | `lead_hard_max_calls` (8) is lower than `max_lead_calls_per_task` (12), so a defect escalation fires first | Make the hard cap derive from the profile. |
| 6 | Uncommitted Lead edits fall through to a cold implementer session | After a write-access Lead fix, commit it as the Lead's and reverify in place. |
| 7 | The memory delta hashes the whole block, so one new entry resends everything; an invalid review is re-sent unchanged | Hash per entry. Resend an invalid review with the validation error appended, once. |
| 8 | Live profile drift: `max_vision_rounds = 2`, no `preflight_endpoints` for Rojo or the local model, no AU `infra_checks`, SKILL.md says two rounds | Set them, and add a profile-vs-decision check to the doctor. |
| 9 | Auto-heal only retries | Add real repairs: restart Rojo through the client, clear a stale runner lock, kill a dead gate pid (`fast_lane` dead-pid check), and re-sync sources when Rojo is stale. |
| 10 | The safe point misses Lead sessions and gate runners; a client coordinator restart kills the detached runner | Count Lead sessions and runners. The client's coordinator restart spares the runner's process tree. |

## Workstream W2: permissions that match the GUI

| # | Gap | Fix |
|---|---|---|
| 1 | `studio_mcp: allow` and the lead's `* allow` override the Studio never-denies (monetization, uploads, asset insertion, script mutators) | Put those in HARD_DENIES. Split `studio_mcp` into read and write. |
| 2 | Empty `edit_paths` means unrestricted; `read_paths` grants write through `external_directory` | Inject the sprint's scope into the session's edit paths. Deny config files (`kilo.jsonc`, hooks, `.env`). Read never implies write. |
| 3 | `lead_write_permissions` drops every deny rule | Keep role-rule denies and the hard denies: no push, no history rewrite, no secrets, no self-approval of test changes, scope widening only from the allow-list. |
| 4 | `subagent_rules` overrides role-rule `task_agents`; `kilo.jsonc` is never regenerated; config pull has never applied a revision | Derive subagent rules per role. Regenerate `kilo.jsonc` on a role-rules deploy and ask the client to reload Kilo. The overlay keeps local edits, and restart-needed keys trigger the restart flow. |
| 5 | Duplicate, contradicting defaults (`automation.py` vs `schema.py`; `max_review_calls` 3 vs 4) | One source, plus a parity test. |
| 6 | No deployed revision | Deploy the imported draft (operator action in the GUI). |

## Workstream W3: the chat engine (router)

- `foundry_router/chat_engine.py`: persisted threads and turns. The model answers with
  `{say, ask, calls[]}` from a typed tool catalog.
- **Tool catalog:** settings draft, validate and deploy; persona routes; cutoffs; role rules;
  queue, sprint start, pause, resume and reopen; decisions; stack control (restart Kilo,
  reload `kilo.jsonc`, restart Rojo); status reads; and the sprint-plan index.
- **Confirmation classes:** reads, drafts and previews run at once. Deploys, cutoffs, persona
  changes, sprint starts, decisions and restarts show a confirm card. The operator can set any
  class to "auto" in the GUI.
- **Cost:** compact context with hashed blocks (only changed blocks are re-sent), at most four
  model calls and eight tool calls per turn, and an `operator-chat` persona on the Claude, then
  GPT, then local chain.
- **Stack reach:** the Foundry Client registers with the router (heartbeat kind `client`) and
  claims stack actions, so the chat can restart Kilo without the coordinator.
- **GUI:** the Ask tab becomes a chat with confirm cards, a full audit trail, and the list of
  what changed.

## Workstream W4: training capture and metrics (router)

- **Capture switches:** `training_capture.backends` and `.personas` include/exclude globs, a
  Capture card on the Training tab (on/off per backend and persona, live without restart), and a
  download of captured calls.
- **Quality signal:** join outcome, feedback and perf data by `request_id`; add a `router_ok`
  curate policy; attach the coordinator's verified outcome (gate PASS, review APPROVED) to the
  episode so verified frontier outputs can train local models when the policy allows.
- **Fixes:** stream capture keeps `cached_tokens`; Ollama-dialect clients get a `received`
  record; capture settings are live.
- **Server metrics history:** a `server_samples` table written every 30–60 s from
  `pool.server_metrics()` (Strata tokens/s, prompt speed, slots, KV cache; Meridian metrics), an
  endpoint and a Performance chart.
- **Honest speeds:** aggregate by `timing_src`, so estimates never average with measured
  numbers; `run_label` applies to local backends only.
- **Sprint scorecard:** `perf_samples` gains project, task, run and role; gate and helper events
  get durations; `/admin/api/work/scorecard` and a Work-tab table show per-sprint time, tokens,
  gates, reviews, helpers and outcome.
- **Settings:** perf retention days; subscription cost per call from a per-plan rate.

## Workstream W5: client and router startup

- **Router startup:** open the port first. MCP connections, probes and context sync run as a
  warm-up task with a per-server timeout of 20 s.
- **Client bugs:** stale detail on a disabled component; adopted processes can't be stopped or
  restarted; a partial settings save resets paths; hung components are never restarted; a
  pending safe-point restart can wait forever; Kilo health is TCP-only (use `/config`).
- **Client features:** drop the media component and add a doctor check for Kilo's media
  config; claim stack actions from the router; apply and reload `kilo.jsonc`; update check
  against GitHub releases; Rojo session-id change detection; a `systemd --user` unit; fix the
  AppImage autostart path; the router shows "no client attached".

## Decisions (Victor, 2026-10-22)

1. **Chat confirmation:** reads, drafts and previews run at once. Deploys, cutoff changes,
   persona changes, sprint starts, decisions and restarts show a confirm card. Each class can be
   switched to auto in the GUI.
2. **Tooling-limit default:** when a sprint hits a Lead budget or a no-progress stop and nobody
   answers in time, the sprint is parked as BLOCKED with a summary and the queue moves on to the
   next sprint.

## Sessions

| Session | Workstream | Main files |
|---|---|---|
| S1 | W1 | `engine.py`, `decision_policy.py`, `lead.py`, `visual_approval.py`, `fast_lane.py`, `autoheal`, the AU profile and skill |
| S2 | W2 | `foundry_automation/role_rules.py`, `adapters/project.py`, `lead.py` permission builders, `config_pull.py`, `document.py`, `automation.py` |
| S3 | W3 | `foundry_router/chat_engine.py`, `control_center.py`, `automation.html`, `foundry_client` claim loop |
| S4 | W4 | `foundry_training/*`, `perf_history.py`, `db.py`, `orchestration.py`, the Training, Performance and Work tabs |
| S5 | W5 | `foundry_router/main.py` startup, `foundry_client/*` |

S1 and S2 both touch `lead.py`: S1 owns its flow, S2 owns `lead_permissions` and
`lead_write_permissions`. S3 and S5 both touch the client: S5 owns the manager and API, S3 adds
the claim loop in a new module.
