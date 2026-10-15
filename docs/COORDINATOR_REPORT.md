# Coordinator: compatibility and gap report, pilot results (2026-10-15)

**Status: built and pilot-tested here against real Kilo 7.8.8 and real Foundry,
with mock models. Not yet run on Victor's workstation against Strata, Meridian
(Claude Max) or CLIProxyAPI.** The design and operator guide are in
docs/COORDINATOR.md.

Legend: **verified** = shown by a test or a live run in this work;
**existing** = was already in Foundry or the project; **unverified** = needs
the workstation or real accounts.

## 1. What Kilo provides (checked against 7.8.8)

Sources: the `@kilocode/cli` 7.8.8 Linux binary (run here), `@kilocode/sdk`
7.8.8 generated API types, live requests captured from Kilo. kilo.ai's docs
pages were not reachable from this environment (network policy), so the SDK
types and observed behaviour are the reference.

| Capability | Finding | Status |
|---|---|---|
| Start an independent session with fresh context | `POST /session` (title, agent, model, metadata, permission rules) | verified (live) |
| Explicit model per session | `model: {providerID, id}` at creation, `{providerID, modelID}` per prompt | verified |
| Send a prompt, get status | `POST /session/{id}/prompt_async` (204); `GET /session/status` (busy/retry, idle = absent); assistant message `time.completed`, `finish`, `error`, tokens | verified |
| Questions / permission prompts | `GET /question`, `GET /permission`, reply endpoints | API verified; reply path tested with the scripted client only |
| Abort, child (helper) sessions | `POST /session/{id}/abort`, `GET /session/{id}/children` | API present; children binding tested with the scripted client only |
| Session identifiers on model calls | Kilo sends `x-session-id` and `x-session-affinity` = session id to custom providers | verified (captured) |
| `X-KILOCODE-TASKID` / `PARENT-TASKID` | present in the binary, not sent to a custom provider | verified (absent) |
| Agent Manager `agent_manager` tool | relayed to the VS Code host; not offered in headless `kilo serve` | verified (absent headless) |
| Session Goals | `goal` / `goal_report` tools; "Completion is your report, not independent verification" | verified (tool text) |
| Subagent model choice | `task` tool takes model/provider; falls back to the **Kilo Gateway** if the model isn't under the current provider | verified (tool text); mitigated with `enabled_providers` |
| Recursion limit | config `subagent_depth` | config key verified; behaviour unverified |
| Permissions for unattended work | session-level rules; a `deny` removes the tool from the model | verified (`bash` disappeared) |
| Restart / recovery | sessions persist in Kilo's store; a lost session is detected (`GET /session/{id}` 404) | detection verified with the scripted client; a real Kilo restart wasn't tested |
| A blocking wait endpoint | `/api/session/{id}/wait` returns 404 (v2 API not enabled) | verified absent; the coordinator polls |
| Telemetry | session tokens/cost, per-message tokens, events SSE | read for tokens only |

**Decision:** use the supported `kilo serve` API as the session layer. Agent
Manager and Goals don't give a durable, unattended, verifiable handoff, so
Hermes Kanban wasn't needed.

## 2. Foundry: existing, changed, unverified

| Requirement | Before | Now |
|---|---|---|
| Strict backend/model binding | existing (strict personas, backend pin, placeholder backends) | **changed:** turn-assist frontier helpers (advisor, auditor, escalation advice) are off for strict personas unless `assist.strict_frontier` names them. A strict local persona could otherwise consult a paid frontier model mid-turn. Tested. |
| Allowlist fall-through to the full pool | existing for *flexible* personas only (brain ranking, blind fallback); strict personas are dispatched direct before those paths | re-audited, tests exist (`test_strict_routing.py`) |
| Requested persona vs actual backend/model recorded | existing (`served_model`, `served_backend`, response headers) | unchanged; shown per session in the Work tab |
| Correlation: client, session, task, parent | existing (`x-foundry-*` headers only) | **changed:** Kilo's own `x-session-id` is used as the session; coordinator **work bindings** add project, sprint, run, role, parent session to request log, ledger and budgets |
| Budgets at account / backend / client / persona / session / helper / request | existing | **changed:** also project, sprint (`work_task`), attempt (`run`), role |
| Atomic admission | existing (reserve/settle) | unchanged |
| Provider-reported vs measured vs estimate | existing (budget monitor labels; unknown stays unknown) | unchanged; `/v1/foundry/work/usage` is labelled "measured" |
| Recognisable quota denials | codes existed, but a pause looked like `budget_denied` | **changed:** `category` / `error.foundry_category` (budget, pause, route, config, auth); a paused strict route answers `paused` |
| Coordinator visibility | none | **new:** `/v1/foundry/work/*` (orchestration scope) and Dashboard → **Work** |
| Kilo config from Foundry | existing (provider block) | **changed:** adds `enabled_providers: ["foundry"]`, `subagent_depth: 1` |
| Persona defaults (Local, Haiku 5.5, Sonnet 5.5, Opus 5.5, Luna, Sol, Astra, Vision, Vision-Deep, Images, Images-Draft) | existing (profile `animaluprising-kilo` v1, all strict, frontier disabled, user edits kept) | unchanged; no new version needed |
| Cline personas (`claude-cline-act`, `claude-cline-plan`) | existing | untouched |
| Meridian on Claude Max | existing quota client (`/v1/usage/quota`, which serves Max accounts) | **unverified** live; run `doctor` and check Usage Log → Budgets on the workstation |
| Haiku 5.5 route | in the profile | **unverified** against the live registry; validate the persona before enabling |
| Image workflows (async jobs, artifacts, dedupe, budgets, adapter) | existing (docs/MEDIA.md) | unchanged |
| Vision review packets with real image content | existing for chat (image parts are passed through) | the coordinator has no visual-review phase yet. Visual sprints stop at Victor's `approve --kind visual`. |
| Host / GPU pressure | existing where telemetry exists (Performance tab) | unchanged |

## 3. AnimalUprising

| Item | Status |
|---|---|
| Task source = `docs/progress/<ID>_PLAN.md` (scope and tier parsed by the project's own `done_check.py`) | verified on UI-01, AS-C16, GF-UI1b, HK-G2, AS-P1 |
| Dependencies from `**Depends on:**`; finished-before-the-coordinator via the STATE.md ledger | verified (AS-C16 / GF-UI1b DONE, UI-01 not) |
| Visual sprints (evidence images in scope, or acceptance ending at COMPLETE-PENDING-VISUAL-REVIEW) stop for Victor | verified on plans; approval flow tested on the fixture |
| `done_check.py` rows as verification (BUILD rows at verify, all rows + `DONE-CHECK PASS` at closeout) | verified: replay at the closing commits of AS-C16, HK-G2 and GF-UI1b gives PASS on every row; the in-progress UI-01 gives FAIL |
| Gate verdict bound to the committed code (`PASS: GATE HEAD <sha>` + only evidence files changed since) | verified on the same replays; **negative control:** a source change after the gate fails the binding |
| Lune suites + negative control re-run by the coordinator | implemented; **unverified** here (no Lune in this environment) |
| Studio gate steps | run by the coding session through Studio MCP (as today); the coordinator checks the verdict, never drives Studio |
| `kilo_checkpoint.py`: Kilo transcript → `checkpoint.py`'s own `audit()`/`render()` | verified: a loop transcript gives STOP-AND-REPLAN; real Kilo transcripts from the pilot gave CONTINUE |
| Rule amendment | **proposed** (`projects/animaluprising/RULE_AMENDMENT.md`); the coordinator refuses AU sprints until AGENT_RULES.md has the marker |
| Files in the game repo | none changed. `kilo.jsonc` and `.kilo/` are local (not in git), so I couldn't read them; `doctor` checks they exist |
| Rojo 34872, Studio ownership | modelled as exclusive resources `rojo:34872`, `studio:victor-workstation`, held by the writing session |

## 4. Live pilot (real Kilo + real Foundry, mock models)

`contrib/coordinator-pilot/run_pilot.py`: Kilo 7.8.8 `kilo serve`, Foundry from this branch,
the coordinator CLI, git with local bare remotes. `mock_backends.py` plays
Strata (a **scripted coder** that drives Kilo's real `write` / `read` / `bash`
tools) and Meridian (a **scripted reviewer** speaking the Anthropic API).
Two projects:

- **animaluprising**: a shared clone of the real repository with the
  amendment applied *in the sandbox only*, and two tier-C pilot plans
  (`PILOT-C1`, `PILOT-C2` depending on it). It runs the real AnimalUprising
  adapter and the real `done_check.py`.
- **fixture-game**: a generic profile with no adapter code; `FX-1`, then
  `FX-2` depending on it.

### Results (run 2)

| Task | Path | Attempts / sessions | Review |
|---|---|---|---|
| fixture-game/FX-1 | implement → verify → review CHANGES_REQUIRED → fresh repair session → verify → review APPROVED → close → COMPLETE | 2 / 2 | `864183f` changes, `4f9359b` approved |
| fixture-game/FX-2 | implement → verify FAIL (check: "does not contain OK") → fresh repair session → verify → review (one-helper rule: paused, auto-resumed) → APPROVED → COMPLETE | 2 / 2 | `1cf9d78` approved |
| animaluprising/PILOT-C1 | implement → verify (done_check BUILD rows + Kilo checkpoint) → review (paused twice by the one-helper rule, resumed) → APPROVED → CLOSE session (summary, STATE row, done_check, push) → closeout checks → COMPLETE | 1 / 2 | `658a4e6` approved; closed at `7e0fa29` |
| animaluprising/PILOT-C2 | started only after PILOT-C1 COMPLETE, in a new session → … → review **refused by the shared Claude budget (4/4 across both projects)** → PAUSED → operator raised the limit, `resume` → APPROVED → CLOSE → COMPLETE | 1 / 2 | `b2db4ef` approved; closed at `a0d988b` |

Shown live:

- **One sprint per session, fresh sessions:** 8 distinct Kilo sessions (4 implement, 2 repair, 2 close), each with one task's metadata.
- **Dependencies:** FX-2 and PILOT-C2 became READY only after their dependency was COMPLETE.
- **Restart:** the coordinator stopped after 4 ticks with FX-1 mid-session, restarted, and finished FX-1 with no duplicate session.
- **Shared exclusive resource:** PILOT-C1 waited for `studio:victor-workstation`, held by fixture-game/FX-1.
- **Strict routes:** all 33 Kilo calls went `Foundry-Kilo-Local → strata/qwen3.8-flash-next-iq3_s [strict]`. All 5 reviews went `Foundry-Kilo-Sonnet → meridian/claude-sonnet-5-5 [strict]`. The mock backends' own logs agree (33 + 5); no other backend was asked.
- **Correlation:** every request-log and ledger row carries project, sprint, run and role, e.g. `fixture-game / FX-1 / reviewer`.
- **Budgets:** `one-frontier-helper` refused 3 overlapping reviews (paused, resumed by themselves when the slot freed). `pilot-shared-claude` (one Claude account, 4 calls, both projects) refused the 5th review; the task paused instead of failing, and the operator resumed it.
- **Cross-project dependency refused:** `queue approve fixture-game FX-2 --deps animaluprising:PILOT-C1` → error.
- **Publishing:** fixture-game pushed by the coordinator (one push per commit); AnimalUprising pushed by its CLOSE session (`verify_only`). Both remotes equal HEAD.
- **Unrelated work preserved:** the untracked `kilo.jsonc` stayed untouched in both workspaces.

### Run 1: "the model said done" is not enough

The first run had no git identity inside Kilo's isolated HOME, so the
session's `git commit` failed. The scripted coder still reported
`{"status": "DONE"}`. The coordinator recorded the report, ran its checks, and
moved the task to CHANGES_REQUIRED (`coordinator.commits: no commit since the
task started`, `coordinator.clean: uncommitted changes in task files`). That
run also showed the engine blocking its own repair over the task's own
leftovers; it now hands them to the repair session (tested).

## 5. Tests

- `tests/coordinator/test_engine.py` (29): full lifecycle; "done" without a commit; failed check → repair; scope never widened → FAILED after max attempts; reviewer CHANGES_REQUIRED → repair → approval of the new commit; invalid reviews (wrong commit, wrong task) → BLOCKED; approval with blocking findings isn't approval; review budget denial → pause → resume after reset with no attempt used; pause switch waits for an operator; a denied coding session continues the same session; config refusal blocks; dependencies + fresh sessions; undeclared dependency blocks; one writer across projects; visual approval bound to revision and artifact hashes; before-close approval revoked by a code change; questions and answers; pause / resume / cancel; restart without duplicate sessions; lost session = free retry; one push per commit; uncommitted work never discarded; stray edits to unrelated work fail; a missing tool is UNKNOWN → BLOCKED; cross-project isolation; profile validation (literal secrets, unknown roles, force push); every transition requires its evidence; one coordinator per store.
- `tests/coordinator/test_animaluprising.py` (9 cases, against the real repository through throwaway clones): plan parsing, dependencies, amendment gate, replay of AS-C16 and HK-G2, gate-binding negative control, in-progress UI-01 not done, Kilo transcript audit, gate summary for the reviewer.
- `tests/test_orchestration.py` (7): Kilo `x-session-id` + bindings on every call, one project per session, orchestration scope, per-run / per-project rules with a shared account limit, refusal categories, the Work view, strict personas without frontier turn-assist.

## 6. Still to do on the workstation

1. Apply the rule amendment (HK sprint), commit the `.kilo` rule line.
2. Add the coordinator client key; validate and enable `Foundry-Kilo-Sonnet`; add `enabled_providers` / `subagent_depth` to `kilo.jsonc`.
3. `doctor animaluprising` must be clean (Lune on PATH, hooks set).
4. Pick a non-visual sprint with a plan from the current MASTER_ORDER (UI-01 is visual and mid-flight). Approve it alone first, watch it in Foundry's Work tab, then approve a two-sprint queue.
5. Check Meridian's Claude Max usage in Usage Log → Budgets (provider-reported) and set a project budget.
