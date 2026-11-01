# Plan: a replay of every Blender build

Status: **plan, waiting for Victor** (2026-11-01; Victor: "Yes, plan gap 8"). Nothing is built.
Source: reports/ASSET_PIPELINE_REVIEW_2026-11.md gap 8. The video's recipe keeps a replay of
every build, so you can see how a model got where it is.

## Where things stand

- **foundry-blender** (docs/plans/SLOT_BLENDER_PLAN.md B1–B3, v0.163.0–v0.165.1) runs a sprint's
  Blender on a slot and copies the outputs back. It prints a summary line (slot, seconds, files
  back), but **nothing is kept** once the sprint's shell scrolls on.
- **No live sprint runs Blender on a slot yet:**
  - the live AU settings have no `[blender] slot`;
  - no track is bound to a slot for Blender;
  - `~/.foundry-blender` holds only two test drop-ins.
- **Today every AU Blender run is on this PC, started in several ways** (counted over the last
  400 Blender calls in Kilo's transcripts):
  - `& $b -b --factory-startup -P tools/blender/build_bs_owl.py -- …`
  - `py -3 tools\guard\run.py --name x -- "$b" …`
  - AU Python tools that start Blender themselves.
  So a journal only inside foundry-blender would record nothing until Blender moves to a slot
  (D-R1).
- **What's left after a build:** the latest renders in the worktree (each run overwrites them),
  the final builder script in git, and the sprint's transcript. The in-between versions, which
  show how the model got there, are lost.
- **What's ready to use:**
  - the engine already reads a busy session's transcript (`tracks.observe_session`, rate
    limited);
  - the telemetry bridge already ships small images to the router (`cc_artifacts`
    thumbnails);
  - the coordinator's Python has Pillow.

## What gets built

**R1. The build journal (core) and foundry-blender's entries.**
- **One entry per Blender run**, written by foundry-blender at the end of each run:
  - **where it ran:** the task and run (written into the drop-in's settings by `prepare()` when
    the sprint's prompt is made), the worktree, the slot and its host, the commit plus the count
    of uncommitted files sent;
  - **what ran:** the Blender arguments, plus the builder script (`-P`/`--python`) with its
    sha256, a stored copy (≤ 256 KB) and a diff from the previous run of the same script on
    this task (capped at 400 lines);
  - **timings:** waited for the slot's lock, library sync, Blender, copy-back;
  - **result:** the exit code (75 = busy), the files that came back (names and sizes);
  - **log tail:** the last 200 lines of Blender's output, plus the lines that matter, pulled out
    (`mesh_report` PASS/FAIL, `REF_IOU`, `ERROR`, `Saved:`);
  - **renders:** the PNGs that came back, at most 12 (render views first), shrunk to 256 px
    thumbnails. Without Pillow, only names and sizes are kept.
- **Storage:**
  - files under `state_dir/build_journal/<project>/<task>/<run id>/` (`journal.json`, `thumbs/`,
    `script.py`, `log_tail.txt`);
  - one row per run in a new `build_runs` table (project, task, session, run id, ts, source,
    summary json), idempotent on the run id.
  - **Never the big outputs:** .blend and .fbx files stay in the worktree and git as today.
- **Failure never breaks the run:** a journal that can't be written logs one line, and the exit
  code is still Blender's.

**R2. The task page view: "Build replay".**
- **Bridge → router:**
  - the telemetry bridge sends `build_run` observations with their thumbnails (each ≤ 40 KB;
    only new or changed rows);
  - the router stores them in `cc_build_runs`;
  - `/control/api/tasks/{p}/{t}/view` returns `build_runs`.
- **The view:** a section on the task page, one row per run, oldest to newest:
  - the time, where it ran, the script and its short hash, the exit code and the `mesh_report`
    line;
  - the render thumbnails as a strip.
- **Replay** (D-R4):
  - step through the runs with ← / →: the same view angle stays selected, so you watch the model
    change run by run;
  - "Show diff" opens the script diff from the previous run, and "Log" opens the log tail;
  - a run's full-size render opens from the worktree path while the file still exists.
- **On the batch review card:** a "Build replay (N runs)" link to the task page section.

**R3. Blender on this PC** (D-R1; needed as long as Blender runs here).
- **The engine watches the transcript:** while a sprint session is busy, every 20 s (and once
  when it ends), it finds new completed Blender calls:
  - a `bash` tool part whose command names `blender.exe` or the drop-in; foundry-blender calls are
    skipped, since R1 already logs them;
  - the AU Python tools that start Blender themselves (`selftest_all.py --blender`, …) are
    matched by a short list of AU tool names in the setting.
- **The same entry as R1, from what the transcript gives:**
  - the args, exit code, times and output lines;
  - the script's hash, copy and diff, read at that moment. The entry is marked "edited after the
    run" when the file changed after the call ended;
  - the renders: PNGs in the worktree written during the call (mtime inside start..end, at most
    12).
- **The risk:** a render overwritten within about 20 s of its run is missed. That's unlikely,
  since the model has to look at the renders before running again. The entry then says
  "renders not caught".

**R4 (later, optional). Re-run a journal entry on a slot.**
- "Re-run" on a run: the same commit, script and arguments, in a scratch checkout on a slot
  (`~/work/blender-replay`).
- The outputs go to a new journal entry only, never into the sprint's worktree.
- Uncommitted files other than the script are not kept, so a re-run is exact only for a committed
  state, and the entry says so.

**Settings** (Automation → Settings → Review, all with UI fields; router overlay like every
setting):

| Setting | Default | What it does |
|---|---|---|
| `blender.journal` | off | The switch for R1–R3 |
| `blender.journal_local` | on | R3: also journal Blender on this PC |
| `blender.journal_tools` | `["selftest_all.py", "render_views.py"]` | R3: AU tools that start Blender themselves |
| `blender.journal_renders` | 12 | Thumbnails kept per run |
| `blender.journal_thumb_px` | 256 | Thumbnail size |
| `blender.journal_keep_days` | 30 | Days a finished task's journal is kept (D-R3) |
| `blender.journal_max_runs` | 200 | Runs kept per task (oldest pruned first) |

## Decisions for Victor

- **D-R1. Blender on this PC:**
  - *Recommended:* R3, watch the transcript. It changes nothing about how sprints run Blender,
    and it fails soft (a missed run is only a gap in the replay).
  - The alternative: a "this PC" mode of foundry-blender, with the local `blender.exe` denied the
    way slot mode does it. Capture would be exact, but every AU sprint's Blender call would
    change, AU's tools that start Blender themselves would still go around it, and a drop-in
    fault would block Blender in a sprint.
- **D-R2. What an entry keeps:**
  - *Recommended:* the R1 list. That's the script copy and diff, the args, timings, exit, files
    back, a 200-line log tail and 12 thumbnails at 256 px: about 0.5 MB per run.
  - The alternatives:
    - smaller, without thumbnails: the replay then can't show the model changing;
    - bigger, with full-size renders: about 3 MB per run, and the renders are in the worktree
      anyway.
- **D-R3. How long it's kept:**
  - *Recommended:* 30 days after the task finishes, and at most 200 runs per task.
  - A running task's journal is never pruned.
  - Both limits are settings.
- **D-R4. What "replay" means:**
  - *Recommended:* step through the recorded runs (R2).
  - Re-running a run on a slot (R4) waits until slots run Blender for real, and only if you want
    it.
- **D-R5. Does the asset judge see the replay?**
  - *Recommended:* no. The judge stays blind (v0.167.0: its own checklist, shuffled letters), so
    the build history can't sway it.
  - The replay is for you, linked from the task page and the review card.
- **D-R6. Default:**
  - *Recommended:* `blender.journal` off at release, like the asset judge. You switch it on in
    Settings.
  - R3's 20-second transcript read adds one Kilo request per busy sprint session.

## Sizes, order, checks

- **Order:** R1 + R2 in one release (the journal is only useful with its view), then R3, R4
  later.
- **Effort:**
  - R1 small to medium (inside `slot_blender.run`, plus the store table and pruning);
  - R2 medium (bridge, router table, task page section);
  - R3 small to medium (the transcript parser reuses `evidence.tool_view`);
  - R4 medium.
- **Tests (targeted, in `tests/coordinator/test_build_journal.py` and the router's tests):**
  - **Command parsing:** the real invocation forms from the transcripts (direct `& $b`, the
    guard's `--name x -- "$b"`, the drop-in, `selftest_all.py --blender`). Python and git calls
    that only mention "blender" are not runs.
  - **Script:** sha256, a stored copy, a diff from the previous run of the same script; a new
    script has no diff; the size caps hold; "edited after the run" is marked.
  - **Renders:** only PNGs written in the run's time window are picked, render views first, at
    most N; thumbnails are made with Pillow; without Pillow, only names.
  - **foundry-blender:** a fake slot run writes one entry with the slot, timings, exit and files
    back. A busy exit (75) is an entry with no renders. A journal write that fails leaves
    Blender's exit code unchanged.
  - **Idempotent:** the same tool part or run id twice gives one row. A run seen by both R1 and R3
    is logged once.
  - **Settings:** with the switch off, nothing is written and the transcript isn't read; the caps
    and the keep days are honoured; pruning never touches a running task.
  - **Bridge → router:** a `build_run` observation, with thumbnails within the size cap, appears
    in `task_view`'s `build_runs`.
  - **UI:** the task page section renders, in the in-app browser, with the steps, diff and log.
- **Proof before switching it on:**
  - on slot-1, one real builder through foundry-blender (`build_bs_king.py`) gives one complete
    entry and shows on the task page;
  - R3: replaying one past asset sprint's transcript (read-only) gives a sensible list of runs.
- **Not changing:** the AU repo; how sprints run Blender (under D-R1 as recommended); the asset
  judge's inputs (D-R5); Studio gates; the secret store.
