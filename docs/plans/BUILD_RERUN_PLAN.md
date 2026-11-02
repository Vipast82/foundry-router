# Re-run a build-journal entry on a slot (build replay R4)

**Status:** built with the recommended answers (Victor 2026-11-01: "go ahead and plan everything
and then build it"). Ships off: `blender.journal_rerun = false`.
**Source:** `docs/plans/BLENDER_BUILD_REPLAY_PLAN.md` R4 (deferred there until slots run Blender
for real; they do since v0.170.0, the Blender pool).

## Why
Build replay (R1-R3) shows how a model got where it is. Sometimes the question is "does this run
still give the same model?": after a Blender or library change on a slot, after a pool change,
or to get the renders of an old run back once the worktree is gone. Re-running by hand means
finding the commit, the script and the arguments, and keeping the outputs away from a live
sprint.

## What changes
1. **"Re-run on a slot"** on a Build replay row (task page → Build replay). It shows on runs
   that recorded a commit and are not re-runs themselves.
2. **Router:** a new control action `build_rerun {run_id, slot?}`.
   - It is accepted only for a run the router holds for that task.
   - It is not bound to the task's state, so a finished task's runs can be re-run.
   - It is handed only to a coordinator that declares the `build_rerun` feature.
3. **Coordinator (`build_rerun.py`):**
   - **Checks first:**
     - the setting is on;
     - the entry exists and recorded a commit and arguments;
     - a slot is free to use: the pool's first slot, else `blender.slot`, or the one the action
       names.
   - **Runs in a background thread** (the action is answered at once), one re-run at a time:
     - pushes the recorded commit to the slot's mirror as `blender/replay` (one branch,
       overwritten each time);
     - syncs the asset library as a normal run does;
     - runs the same arguments with the pool's job locks;
     - sends the **journal's copy of the builder script**, so an uncommitted script is
       re-run exactly.
   - **The outputs** come back into the new entry's own `out/` folder under the journal, never
     into a sprint's worktree.
   - **The result is a new journal entry:**
     - source `replay`, `where.replay_of` = the original run;
     - a note that other files uncommitted at the original run are not included;
     - it shows in Build replay with its thumbnails like any run.
   - Events: `build_rerun_started`, `build_rerun_done`, `build_rerun_failed`.

## Decisions (recommended answers, used for the build)
- **D-RR1. Where the outputs go:** the new entry's folder under the journal. *Recommended:* a
  re-run never changes a sprint's files. The journal sweep removes them with the entry (30
  days after the task finishes).
- **D-RR2. Exactness:** commit + the journal's script copy + the same arguments. *Recommended.*
  Other uncommitted files of the original run aren't kept by the journal, so the entry says so
  instead of pretending.
- **D-RR3. One at a time:** a second request is refused while one runs. *Recommended:*
  re-runs are rare and shouldn't take pool jobs from sprints. They use the same job locks, so a
  busy slot makes the re-run wait or exit 75.
- **D-RR4. This PC is never used for a re-run.** *Recommended:* it runs the live stack, and a
  slot gives the clean checkout the re-run needs.

## To switch it on (Victor)
1. Rebuild the router: the button, the `build_rerun` action, and the Settings field
   `blender.journal_rerun`.
2. Settings → Fast lane, gate & vision → `blender.journal_rerun` = on, then Review & deploy.
3. Task page → Build replay → pick a run → **Re-run on a slot**. The new run appears in the list
   when it finishes.

## Tests
- `tests/coordinator/test_build_rerun.py`:
  - refused when off, with no entry, with no commit, or with no slot;
  - one at a time, with the events;
  - the run pushes the commit, sends the script copy, writes the outputs only into the
    journal and records a `replay` entry;
  - the control action is rejected when off.
- `tests/test_build_replay_router.py`:
  - the action is bound to a reported run of the task, with no task-state binding;
  - the button is in the section.
