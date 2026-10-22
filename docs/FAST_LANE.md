# Fast lane: one long session per sprint, verified by the studio gate

`[fast_lane]` (UI-T0) is a coordinator mode for named tasks. A fast-lane
task runs as **one long Kilo session**. Its verify step is the project's
studio gate runner (docs/STUDIO_GATE.md), not the adapter's checks. A red
gate or a vision finding goes back into the **same session**: no new session,
no recovery case, no attempt charged.

Why: UI-02 ran 37 hours, of which about 6 were coding. 16.2 hours went to 29
blocked periods; it used 31 sessions, 19 recovery cases and 28 Lead calls,
and the final gate made a model hand-carry 40–80 MCP calls. The fast lane
keeps the session and moves the proof into one command. Plan:
`docs/plans/AU_UI_REWORK_PLAN.md` sections 6.5 and 7.

It is off by default. A task not listed in `tasks` behaves exactly as
without it.

## Profile keys

In the project TOML (`project.example.toml` has the commented block):

```toml
[fast_lane]
enabled = true
tasks = ["UI-D1", "UI-02a", "UI-03a", "UI-04a", "UI-05a", "UI-06a", "UI-V1"]
gate_module = "foundry_coordinator.projects.animaluprising.studio_gate"
gate_timeout_minutes = 45
max_red_gates_before_handoff = 2
max_vision_rounds = 2
lanes = ["prework", "test_design", "failure_triage", "incremental_review"]
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch. |
| `tasks` | `[]` | Explicit, distinct task ids. Globs are refused. |
| `gate_module` | the AU `studio_gate` | Dotted module run with `python -m`, or a `.py` path (relative to the Foundry-Router checkout). |
| `gate_timeout_minutes` | `45` | A run still going after this is stopped and counts as a red gate (`fast_lane.timeout`). At least 1. |
| `max_red_gates_before_handoff` | `2` | On a local-primary task, this many red gates request the frontier primary. Twice this many stops the task BLOCKED. At least 1. |
| `max_vision_rounds` | `1` | Vision CHANGES rounds sent to the session before the next CHANGES is accepted with notes (revamp decision 1, 2026-10-21: one round). |
| `vision_batch_frames` | `20` | Frames per vision call; every batch must approve (v0.129.2). |
| `runner_outputs` | `["docs/progress/{task}_EVIDENCE_RAW.log"]` | Files the runner rewrites every run; left out of `coordinator.clean` and `coordinator.unrelated_preserved` for fast-lane tasks (v0.129.5). |
| `runner_captures` | `["docs/evidence/UI-v3/{task}/**"]` | Capture files: still committed after a PASS, but re-rendering one that was dirty at run start is not "touching a pre-existing change" (v0.129.5). |
| `lanes` | the four above | The only helper lanes that run for these tasks. Unknown lane names are refused. |

Unknown keys and wrong types fail profile loading with the exact key.

Related settings the fast lane uses:
- `[approval] visual_model = true` and `roles.visual_reviewer` for the vision
  approval (off in the example; when off, the profile's approval rules decide
  the look and `fast_lane.vision` is recorded as skipped);
- `[frontier_primary]` for the automatic hand-off (`enabled`, `persona`,
  `max_phases_per_task`, `cooldown_minutes`, `max_turns`, `max_minutes`);
- `[training] local_personas` to decide whether a task's implementer is local;
- `[role_overrides.ui]` / `[role_overrides.ui_frontier]` for each task's
  implementer and reviewer;
- `[locks] exclusive` (Studio, Rojo), held while the runner drives Studio.

## Task lifecycle

```
IMPLEMENTING ──DONE──► VERIFYING: generic git checks ─► studio gate (background)
                          │                                │
                          │ checks fail                    ├─ FAIL ─► findings into the same session ─► IMPLEMENTING
                          ▼                                ├─ ERROR / no verdict ─► BLOCKED
                  findings into the same session           └─ PASS ─► captures committed? ─► vision approval
                                                                          │
                                         CHANGES (≤ max_vision_rounds) ◄──┤
                                         findings into the same session   ├─ APPROVED / accepted with notes ─► REVIEWING ─► close
                                                                          └─ inconclusive twice ─► BLOCKED
```

**BUILD prompt.** The AU adapter gives a fast-lane task its own BUILD prompt:
load the `au-fast-lane` skill (plus `au-ui-v3` and `au-roblox-ui-development`
for UI), the rules card `docs/rules/FAST_LANE_RULES.md`, the plan
`docs/progress/<ID>_PLAN.md`, the gate file `docs/progress/<ID>_gate.json`,
helper drafts in `.plan/tmp/prework/<ID>/`, and the exact runner commands.
The session works in a loop (code → `stories` → fix), commits by explicit
path with `<ID>: ` messages, and reports DONE. It does not write the summary,
the STATE row or push; a short CLOSE session does that. There is no per-step
plan ticking and no partial-BUILD session hand-over.

**The runner is the verify step.** In VERIFYING the coordinator first runs
its generic checks (scope, commits, clean tree, history). If they pass, it
starts `<gate_module> --repo <workspace> --run-id <nonce> sprint --sprint <ID>
--files <changed files outside docs/>` (the nonce also in `FOUNDRY_GATE_RUN_ID`;
`<ID>-<head12>-<random>`) as a background subprocess (own process group, no
window) and records the run in the task data. Each tick only reads
`.plan/tmp/gate/<ID>/status.json` and the verdict file
`.plan/tmp/gate/<ID>/<head12>.sprint.json`, so a tick never waits for
Studio; `latest/`, `latest.json` and `stories`/`regression` verdicts are
never read as the result. The runner holds the project's exclusive locks
and `.plan/tmp/gate/runner.lock` while it runs; a second runner exits 2
(`GATE <ID> BUSY`), which the coordinator treats like any exit without a
verdict. The log is `fast_lane_gate.log` in the task's evidence directory.

The runner reads the gate file `docs/progress/<ID>_gate.json` and
`tests/RegressionIndex.lua` from HEAD, never the working tree, so the session
must commit the gate file before a sprint gate (an uncommitted one fails
`host.gate_plan`). Every regression suite runs in its own fresh solo playtest;
RegressionIndex entries can set `isolation = "shared"`, `setup`, `runLast`,
`rerunAfter` and `waits` (docs/STUDIO_GATE.md).

A verdict counts only when it is schema 1, `mode: "sprint"`, for this sprint
and revision, carries this run's `run_id`, was written after the coordinator
started that run, and says PASS with every required check PASS. If HEAD moves
during a run, the run is stopped and a new one starts, unless only evidence
and closeout files changed (the gate's own captures, the plan's
closeout-exempt paths). When the coordinator kills a runner it also starts
`studio_gate stop-playtest` in the background, so a playtest the killed
runner left running is stopped.

**Evidence.** The verdict becomes a required `fast_lane.gate` verification
row (plus one advisory `fast_lane.check.<id>` row per runner check) and, with
`[lifecycle]` evidence on, a `fast_lane.gate` ledger record bound to the
revision the gate ran at. The AU adapter's only BUILD obligation for a
fast-lane task is `build.gate` (that record, at the reviewed code); the plan's
suite, capture and gate obligations are dropped because the runner covers
them. A captures-only commit does not stale the record.

**Same-session red gates.** On FAIL the failed checks (id, result, detail,
artifacts, FAIL layout lines, at most 30) go into the same primary session as
one journaled prompt, and the task goes back to IMPLEMENTING. The red gate is
counted in the task data (`fast_lane.red_gates`). The coordinator falls back
to the ordinary CHANGES_REQUIRED path only when a same-session prompt is
impossible: the session is gone, the prompt failed, or the session came back
without a change after the same findings. If the generic checks fail before
the gate, those findings go into the session the same way.

**PASS.** If the gate's capture files are still uncommitted, the session is
asked to commit exactly those files (message `<ID>: gate captures`). The
verdict carries over that commit.

**Vision approval rounds.** After a green gate the frontier vision persona
(`roles.visual_reviewer`) sees every committed capture with its checker lines,
the sprint text and the rubric (plan 6.5: layout zones, text fit, tokens,
phone usability, controller focus, every state, production quality). The
route must be strict and vision-capable on every model, and the serving model
must be one of them; otherwise the approval waits.
- **APPROVED** records a revision-bound visual approval by
  `model:<persona>/<served model>`.
- **CHANGES** sends the numbered findings into the same session, up to
  `max_vision_rounds`. The next CHANGES after that is **accepted with notes**:
  the approval is recorded by `coordinator:fast_lane` and the findings are
  kept as follow-ups (`fast_lane.vision_notes`).
- **Inconclusive** is asked once more, then the task stops BLOCKED. A missing
  route, an unreadable frame or an uncommitted capture is never a pass.

**Automatic frontier hand-off.** At `max_red_gates_before_handoff` red gates
on a **local-primary** task, the coordinator calls `frontier.auto_request`.
This is the only automatic trigger; every other task keeps the manual-only
request. Same limits as a manual request: `max_phases_per_task`, cooldown,
the frontier persona must differ from the local implementer and every
reviewer. The phase is recorded active at once and the red-gate findings
prompt is its first turn, in the same session. If the hand-off is refused, a
`fast_lane_handoff_unavailable` event records why and the local primary
continues. Tasks whose implementer is already the frontier persona (UI-D1,
UI-02a) are never handed off.

**BLOCKED cases.** Twice `max_red_gates_before_handoff` red gates
(`fast_lane_red_gates`), a runner ERROR or an exit without a verdict
(`fast_lane_gate_error`), an unreadable capture, or a vision verdict
inconclusive twice. A blocked task goes to the lead manager if it is on, else
to the operator. Cancelling a task stops its runner.

**Lane trimming.** For a fast-lane task, every helper lane not in
`[fast_lane] lanes` is off (`lanes.settings(P, task_id)`, used by the helper
and proactive planners). Other tasks keep the profile's `[lanes]` settings.

**Events** (Work tab, `events`): `fast_lane_gate_started`, `fast_lane_gate`,
`fast_lane_red_gate`, `fast_lane_continuation`, `fast_lane_fallback`,
`fast_lane_gate_stopped`, `fast_lane_gate_error`,
`fast_lane_vision_accepted_with_notes`, `fast_lane_handoff_unavailable`,
`model_visual_review`, `frontier_requested` / `frontier_active` with
`auto: true`.

**Not done yet (TODO):**
- per-sprint lane caps sized to the one-page plan (only trimming exists);
- prework helpers fed from the plan's stories and components list;
- building the contact sheet (`review_sheet`) after a green gate;
- "keep the look pending without blocking the queue" beyond the existing
  `deps_accept_pending = ["visual"]`.

## Roles and provider failover

| Role | Who (AU UI batch) |
|---|---|
| Primary | UI-D1, UI-02a: `Foundry-Kilo-Sonnet` (`[role_overrides.ui_frontier]`). UI-03a…UI-06a, UI-V1: `Foundry-Kilo-Local` (local Strata/Qwen, `[role_overrides.ui]`). |
| Frontier helpers | prework, test design, triage, review lanes on `Foundry-Kilo-Helper-Claude` (Meridian) and `Foundry-Kilo-Helper-GPT` (CLIProxyAPI). On local-primary sprints they give guidance, interfaces, stories, tests and reviews, not implementation code. |
| Reviewer | `Foundry-Kilo-Deep-Review` for every UI task. |
| Vision approver | `Foundry-Kilo-Visual-Review`. |
| Frontier hand-off | `[frontier_primary] persona = "Foundry-Kilo-Sonnet"`. |
| Lead | only real blockers (lead manager, else the operator). |

Failover is the existing persona routing, unchanged by this release. Each
persona has an ordered route: a new call goes to the preferred model, else the
fallback; a call in progress is never switched; a frontier role never falls
back to local Strata. If both providers are out, required work waits and
optional helpers are skipped.

| Persona | Preferred → fallback |
|---|---|
| Foundry-Kilo-Planner | Opus 5.5 → GPT-6 Astra |
| Foundry-Kilo-Review | Sonnet 5.5 → GPT-6.1 Sol |
| Foundry-Kilo-Deep-Review | GPT-6 Astra → Opus 5.5 |
| Foundry-Kilo-Visual-Review | Sonnet 5.5 → GPT-6 Astra |
| Foundry-Kilo-Research | GPT-6 Luna → Claude Haiku 5.5 |
| Foundry-Kilo-Lead-Manager | Opus 5.5 → GPT-6 Astra |
| Foundry-Kilo-Sonnet | Sonnet 5.5 → GPT-6.1 Sol (**backend change, see the release notes**) |

Helpers are pinned one per backend (Meridian first, CLIProxyAPI for
high-value units), up to 3 per backend. Optional helpers are admitted below
`helper_policy.cutoff_pct` on every applicable window (docs/HELPER_POLICY.md).

## Queue and begin

Fast-lane tasks are queued like any other task (docs/COORDINATOR.md). For
the UI batch, only after Victor says "begin":

```
py -3 -m foundry_coordinator doctor animaluprising
py -m foundry_coordinator.projects.animaluprising.studio_gate --repo C:\Roblox\AnimalUprising doctor
py -3 -m foundry_coordinator queue approve animaluprising UI-D1  --by victor
py -3 -m foundry_coordinator queue approve animaluprising UI-02a --deps UI-D1  --by victor
py -3 -m foundry_coordinator queue approve animaluprising UI-03a --deps UI-02a --by victor
...                                                     UI-04a, UI-05a, UI-06a, UI-V1 in order
py -3 -m foundry_coordinator resume-project animaluprising
```

Before that:
1. `[fast_lane]` enabled with the task ids, `[approval] visual_model = true`,
   and the role overrides in the live `project.toml`;
2. each sprint's `docs/progress/<ID>_PLAN.md` and `<ID>_gate.json` committed
   in the AU repo (the runner reads the gate file from HEAD);
3. the studio gate `doctor` says `DOCTOR READY`;
4. the live failover check (plan 7.4): one real call each through
   Deep-Review and Research served by their Claude fallback while GPT is out.

Watch a running gate with `studio_gate status --sprint <ID>`, or in the Work
tab (the task's next action shows the gate phase and minutes used). If a
killed runner left Studio in Play, `studio_gate stop-playtest` stops it.

## Tests

`tests/coordinator/test_fast_lane.py`: profile validation, lane trimming,
PASS → vision → review → close, red gates in the same session, the hand-off
(and none for a frontier primary), the red-gate limit, runner timeout, exit
without a verdict, vision rounds then accept-with-notes, evidence records, the
AU fast-lane prompt and obligations, and an unlisted task left unchanged.
