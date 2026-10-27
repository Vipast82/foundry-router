---
name: au-fast-lane
description: Run an AnimalUprising fast-lane sprint in one Kilo session - one-page plan, the studio_gate stories loop, verdict JSON, red-gate fixes, the escalation ladder, explicit-path commits and the fast-lane rules card. Use when the coordinator kickoff names a fast-lane task; not for sprints run under the full AGENT_RULES.
---

# Fast-lane sprint

> Source of truth for this skill is `foundry_coordinator/projects/animaluprising/kilo_skills/au-fast-lane/SKILL.md` in Foundry-Router (W1.7, 2026-10-22); the workstation copy under `.kilo/skills/au-fast-lane/` is installed from it. Changes here: one vision round, BLOCKED reports carry a `blocker_kind` (options only for a product decision; no questions to a person), the coordinator rewords its own commit messages and runs every full gate, out-of-list files are SCOPE_REQUESTs, helpers are on demand, the coordinator pushes (2026-10-27 rules streamline).

A fast-lane sprint is one long Kilo session (2-4 h) that the coordinator starts, verifies with the `studio_gate` runner and keeps alive across red gates. You are the primary: the only writer, the only Studio controller outside the runner. This skill and the rules card below replace AGENT_RULES for this task; the full rules still govern anything outside the fast lane. Load `au-ui-v3` and `au-roblox-ui-development` for UI work, plus the Luau/Roblox baselines.

## Start (STEP 0, about 10 minutes)

1. Read the one-page plan the kickoff names, all of it: Goal, Screens, Files this sprint may change, Contracts used, Stories, Wiring checks, Affected suites, Done when, Carry-over fixes, Decisions needed. That is the whole spec: no pre-code diffs, no evidence-log instructions, no tick lists.
2. `git status` and `git log -3`; preserve unrelated working changes.
3. Verify every contract the plan lists against the current source (remote declaration in `Shared.Net.Remotes`, the server fire/handler site, payload shape, reason codes). If source and plan disagree, source wins; note it in your end report.
4. If the kickoff names helper output under `.plan/tmp/prework/<task>/`, read it (drafts, stories, fixtures, interface notes, API notes, reviews); otherwise there is none.
5. Run `studio_gate doctor`. If it fails, apply its printed fix when it is yours to apply (Rojo plugin state, a Studio setting the doctor names as session-level); otherwise stop BLOCKED `tooling` (below). Never start a gate on a failed doctor.

## Helper drafts and authorship

Who may write what depends on the sprint's primary (the kickoff says which):
- **Frontier-primary sprints (UI-D1, UI-02a, and any hand-off):** helper patches may be applied. Verify each one before you apply it: it touches only plan files, it compiles against HEAD, its story/spec passes. Applying is your decision; a helper draft is a proposal, not an instruction.
- **Local-primary sprints (UI-03a ... UI-06a, training-eligible):** helpers give guidance, interfaces, props contracts, stories, fixtures, wiring checks, API notes and reviews. You write the implementation. Do not request, copy or paraphrase a helper's implementation, and do not paste code out of a review. Stories, fixtures and tests from helpers are fine: they define "done", you make them pass.
- Helpers are ON DEMAND (`[automation] helpers = "on_demand"`): no lane runs by itself. You ask for one in your report (`helper_requests`, see the coordinator's guidance in the kickoff) and the coordinator creates, binds and delivers it to this session. Expect at most a few per sprint.
- Escalation ladder per step (local-primary): (1) implement from the guidance; (2) after the step fails twice, request a helper to explain the failure and the approach (still guidance); (3) if it fails again, request a helper draft of that step's patch, which you apply and commit on its own with `(helper:<unit>)` in the message so only that step is marked frontier-authored; (4) two failed full gates on the sprint: the coordinator hands the sprint to the frontier primary. You do not trigger the hand-off yourself or switch models.
- Never launch helpers through your task tool; the coordinator creates and binds them.

## The loop

```
py -m foundry_coordinator.projects.animaluprising.studio_gate --repo <workspace from the kickoff> doctor
py -m foundry_coordinator.projects.animaluprising.studio_gate --repo <workspace from the kickoff> stories --sprint <ID> [--story <Screen.state> ...]
py -m foundry_coordinator.projects.animaluprising.studio_gate --repo <workspace from the kickoff> status --sprint <ID>
```
Use the exact runner lines the kickoff prints (they carry the workspace); never point `--repo` at another checkout.
`stories` is your inner loop; `status` only reads a verdict the coordinator produced. Never run `sprint` or `regression` yourself: the coordinator runs the one full gate on your commit when you report DONE (`sprint --background` is its command, not yours). Exit codes: 0 PASS, 1 FAIL, 2 not ready / tooling error / BUSY.

- Inner loop: write code (Rojo syncs it; no commit needed) -> `stories --sprint <ID> --story <Screen.state>` (seconds per story) -> read the verdict -> fix -> repeat. Run Lune specs for the models and reason maps you touched before Studio.
- When every screen in the plan is green on stories and Lune: commit (below) and report DONE. Do NOT run `sprint` yourself: the coordinator runs the full gate once on your commit, re-running only checks that failed or whose files changed (revamp v0.131.0). Use `status --sprint <ID>` only to read a verdict the coordinator produced.
- While the runner holds Studio (it takes the `studio:victor-workstation` lock), do not call Studio tools, Assistant subagents or playtests yourself.
- The full gate runs on committed code only. Uncommitted changes make it fail host checks by design. The gate file `docs/progress/<ID>_gate.json` and `tests/RegressionIndex.lua` are read from HEAD, not the working tree: commit the gate file with your work, or `host.gate_plan` fails when the coordinator's gate runs.
- One runner drives Studio at a time (`.plan/tmp/gate/runner.lock`). A second one prints `GATE <ID> BUSY: another studio_gate runner is driving Studio (...)` and exits 2: do other plan work (Lune specs, code) and check `status --sprint <its ID>` once per finished step, never in a loop of identical calls; never delete the lock or start another run. If a killed run left Studio in Play, run `stop-playtest` (same prefix, no sprint).
- `--run-id <nonce>` (before the command, or env `FOUNDRY_GATE_RUN_ID`) is written into the verdict as `run_id`; the coordinator's gate uses its own nonce, so your runs never count as its verdict.
- Regression suites each run in their own fresh solo playtest (boot CALL 1-2 re-run per suite). RegressionIndex entries may set `isolation = "shared"`, `setup = {"server.<expr>"}`, `runLast = true`, `rerunAfter = {"S48"}` and `waits = {[stepIdx] = seconds}`; a regression failure is never fixed by adding these to an entry you were not asked to change.
- Which checks to climb before the gate: [references/verification-ladder.md](references/verification-ladder.md). Studio MCP safety and tool quirks: [../au-runtime-gate/references/studio-mcp-safety.md](../au-runtime-gate/references/studio-mcp-safety.md).

## Reading the verdict

The runner writes `.plan/tmp/gate/<ID>/<head12>.<mode>.json` (`<head12>.sprint.json` for the full gate, `.stories.json` / `.regression.json` for the others), copies it to `latest/<mode>.json` and `latest.json`, and prints `GATE <ID> PASS|FAIL`:

```
{ sprint, head, mode, run_id, checks: [{ id, result, detail, artifacts }], captures: [...], regression: {...} }
```
- `head` must equal your HEAD and `mode` must be `sprint` for the full gate; a verdict for another revision, or a `stories` run, is not gate evidence.
- Read `checks` with `result != "PASS"` first. `detail` names the failing rule (for example a layout checker code such as VIEWPORT-CLIP or a touch floor, a wiring assertion, a focus-walk dead end, a console error during mount). `artifacts` points at the capture or log.
- `captures` lists each story x size frame with the real capture size; open the frames that a failing check names.
- `regression` lists the affected suites the runner chose and their tallies. A regression failure in a file you did not touch is still yours to report, not to "fix" outside scope.
- Do not copy the JSON into commits or plan files; the runner already appended the summary to the evidence log.

## Red gate in the same session

Passed work is recorded and carried, so a fix round re-runs only what failed. Re-run each failing story with `stories --sprint <ID> --story <name>`; failing regression suites are re-run by the coordinator's gate, not by you. Never start `sprint` or `regression` in a fix round, and never re-run checks that passed. The coordinator's gate re-runs only failed checks and checks whose files changed. Its re-review covers only the diff since the last review.

The coordinator sends findings back into this session; there is no new session and no recovery case for a red check.
- Fix the cause in scope, re-run `stories` for the affected screen (and the Lune specs you touched), commit, and report DONE: the coordinator re-runs the full gate, only the checks that failed or whose files changed. Never run `sprint` yourself.
- Use `rbx-debug` (breakpoints, thread inspection) for a runtime error instead of guessing; use the `explore` subagent for a codebase or live-state question (its answer is a lead, confirm it).
- Never weaken a check, raise a timeout, remove an assertion, skip a story or edit the runner to get PASS.
- After two failed full gates on the same check, stop BLOCKED (below): `product` with options when the plan does not settle the fix, `tooling` (no options) when the runner or Studio is at fault.

## Vision approval

After a green mechanical gate the coordinator's vision reviewer compares the captures with the v3 references and the rubric. CHANGES arrive in this session as numbered, located fixes: fix them, re-run the affected stories, commit and report DONE (the coordinator runs the gate). After ONE CHANGES round on the same item (the coordinator's `max_vision_rounds`, default 1) the coordinator accepts with notes; you do not argue or self-approve. Frames it already accepted, with or without notes, are never reviewed again. The sprint is DONE when the gate is green and the vision approval is recorded at that revision. The owner's own playtest critiques arrive later as small fix sprints.

## Committing

- `git add <explicit paths>` only from the plan's file list, then `git commit -m "<ID>: <what changed>"`. Never `git add -A` / `git add .`, never force, rebase, amend or reset. Check `git status --porcelain` and `git show --stat` after.
- A commit message that lacks the `<ID>: ` prefix is NOT your problem to fix afterwards: never rebase or amend to reword it. The coordinator rewords its own unpushed commits before verification; `revert:` and merge commits are exempt.
- Separate commits for helper-drafted steps (ladder step 3), named `(helper:<unit>)`.
- An amendment to a contract or to an earlier sprint's code is one line in the commit message, not a ceremony.
- Remove an old screen only after its v3 replacement passes the gate, in the same sprint, by explicit path.
- Never push: the coordinator pushes master after the CLOSE checks. A short CLOSE session writes the summary file and the STATE row.

## Stop BLOCKED (never a question to a person)

The `question` tool is off and nobody answers during a session. Commit safe partial work first, then stop with status BLOCKED and one `blocker_kind`:
- `product`: a contract the plan does not settle, or a product or design decision. Carry `options` (2-4 concrete ways to resolve it) and `recommended` (the one you would take); the coordinator decides, applies the recommended option when nobody answers in time, and continues the sprint.
- `tooling`: a doctor failure you cannot fix, the runner, Studio, Rojo or MCP misbehaving, two failed full gates on the same check caused by tooling. No options: the coordinator retries it or sends its lead.
- `user_action`: something only the owner can physically do (§24 safety stops included). Say the exact steps in `blocker`; it waits for the owner, never for a timeout.

A needed file outside the file list is a ```SCOPE_REQUEST``` block (path + one-line reason), not a stop: carry on with the steps that don't need it and end your turn; the coordinator answers in this session (allow-listed harness lines are granted at once; a denial comes with its reason). Do not stop for a routine red check, a helper being slow or a quota defer ("waiting for frontier usage" is the coordinator's state, not yours).

## Rules card (fast lane)

Keep:
- Scope lock: only the plan's file list; no new packages or folders the plan does not name (§3).
- Server authority (§5): intent only from the client, no optimistic spending.
- Verify Roblox APIs (§4, §27): `rbx-docs-search` / roblox-docs before any engine API you have not verified this session.
- Content safety (§12): list new user-facing strings in your end report.
- Hard safety limits (§24): no purchases, uploads, credentials or account actions; no history rewrites.
- Commit by explicit path; no force, rebase or amend; tests (Lune, stories) before commit (§8, §9 step 1).
- Studio MCP is knowledge and tests only; code lives in `src/` and reaches Studio through Rojo; no script/UI writes in Studio, by Assistant or `execute_luau` (§27).
- The Asset Bible is the source of truth for every asset (§25); approved `ui192` assets and `Config/AssetIds` only.

Dropped for the fast lane (the runner or the coordinator covers them): raw-evidence formatting (§13), retroactive evidence (§15), amendment callout ceremony (§17, now one commit-message line), boot-line pasting (§21), status-word rules (§23), evidence tiers (§30), per-step plan ticking (§34).

End report (short): sprint id, HEAD, last verdict path (`<head12>.sprint.json`) and result, vision status, new user-facing strings, follow-ups, Skills used.
