# One fresh builder session per model (gap 9)

**Status:** built with the recommended answers (Victor 2026-11-01: "go ahead and plan everything
and then build it"). **Ships off** (`[builder] enabled = false`). The report's Phase D waits
for the pilot (Phase C), and D4 says a two-asset trial comes first.
**Source:** `reports/ASSET_PIPELINE_REVIEW_2026-11.md` gap 9, decisions D4 and D5.

## Why
An AS- sprint session builds the model, its faces and 15-17 clips, then runs the Studio checks,
uploads, and writes the ledger and evidence, in sessions of up to 240 min. By the time it builds
the second model, its context carries all of the first model's Studio and upload work, and every
cache bust costs minutes. The video's builder started fresh for each model, with only what it
needed: the views, a short note and the builder task.

## How (the smallest change that gives each model a fresh context)
Kilo already has fresh child sessions: a `task` subagent. Foundry adds one, **`au-builder`**,
and lets the AS- sprints named in the setting call it. The sprint session stays the one
session the coordinator runs and checks. The builder is a child of it, so nothing changes in
the engine's state machine, its limits or its reviews.

1. **The `au-builder` agent in kilo.jsonc** (written by Foundry, like the agent permission
   blocks; main file and track files) while `[builder] enabled`:
   - `mode: subagent`, a description, `steps` (`builder.steps`, 200);
   - `model` only when `builder.model` is set. Empty = the calling session's model, so a
     frontier AS- sprint gets a frontier builder (report gap 10: keep authoring on frontier);
   - a short system prompt: build ONE model, stop at the result lines, no export, upload,
     Studio, commit or push;
   - **permission:** the implementer's generated block (edits, shell, the hard denies), plus:
     - denied: `git commit*`, `git push*`, `git tag*`, nested `task`, every Studio and
       foundry-media tool, `C:/Roblox/_secrets/*`;
     - allowed: the `au-blender-asset` skill.
2. **Who may call it:** an implementer session of a task that matches `builder.tasks`
   (task-id globs; default `AS-*`). It gets a session rule `task: au-builder allow`, added after
   the role rules, so it wins.
3. **The BUILD prompt** of such a sprint gets a "BUILDER PER MODEL" paragraph:
   - per model, start one `au-builder` with the model's ID, the reference pack paths, the short
     note, the builder script path and absolute output folders;
   - several models may run at once (one `task` call each, in the same message), and the
     Blender pool spreads their runs;
   - each builder returns the paths and the result lines: `mesh_report` PASS, `side_check`,
     `REF_IOU`, renders;
   - the sprint session then runs its look gate and the asset judge, exports, uploads, does the
     Studio checks and writes the records;
   - it builds a model itself only after a builder failed twice on it.
4. **Commits stay with the sprint session.** The builder's files are in the same worktree, and
   the sprint's scope check covers them at commit and verify time.

## Decisions (recommended answers, used for the build)
- **D-B1. A Kilo subagent, not a new engine step.** *Recommended:* fresh context per model
  with no change to task states, limits, reviews or recovery. The alternative, a coordinator
  sub-step with its own sessions and hand-over, is the M-L engine change. It becomes worth it
  only if the trial shows the subagent's limits matter (one parent turn waits for its
  builders).
- **D-B2. Model:** inherit the sprint session's model unless `builder.model` is set.
  *Recommended.*
- **D-B3. Trial scope (D4):** `builder.tasks` takes globs. For the trial Victor names two asset
  tasks (e.g. the pilot's re-builds); `AS-*` once the trial shows a gain. *Recommended.*
- **D-B4. The builder never commits, pushes, uploads or touches Studio.** *Recommended:* one
  writer of history per sprint, as today.
- **D-B5. Second candidate (D5):** not built now. When the judge scores below its threshold,
  a second `au-builder` call with a different seed or model could run on the pool's spare
  capacity, judged under shuffled letters. It is planned after the trial, because it needs the
  judge's threshold from the pilot.

## Not in this release
- The second candidate (D-B5).
- Engine-side accounting of builder minutes. The build journal already records each Blender
  run with its task, and Kilo lists the child sessions.

## To switch it on (Victor), after the pilot
1. Rebuild the router (Settings shows `builder.*`).
2. Settings:
   - `builder.enabled` = on;
   - `builder.tasks` = the two trial tasks (e.g. `AS-J03R, AS-G02R`);
   - leave `builder.model` empty.
3. Review & deploy. The kilo.jsonc gets `au-builder` and Kilo reloads. The next session of a
   matching sprint uses it.
4. Recommended with it: `skills.install` includes `au-blender-asset` (gap 7).

## Tests
`tests/coordinator/test_builder_per_model.py`:
- off by default: no agent written, no session rule, no prompt paragraph;
- on:
  - the agent block has mode, steps, the prompt, the denies (commit, push, Studio, nested
    task) and the skill allow;
  - no `model` key unless set;
  - it is written into the main and track kilo.jsonc;
  - a matching implementer session may call `au-builder`, while a non-matching task and a
    reviewer may not;
  - the AS- prompt has the paragraph, and a non-matching task's prompt does not.
