# Plan: a Blender pool (slot-1, slot-2 and this PC)

Status: **plan, waiting for Victor** (2026-11-01). Victor: "I want to run multiple blenders in
multiple containers / slots and even the main pc to increase speed of asset creation". Nothing
is built. It builds on docs/plans/SLOT_BLENDER_PLAN.md (foundry-blender, B1–B3) and
docs/plans/BLENDER_BUILD_REPLAY_PLAN.md (the journal).

## Where things stand

- **Machines:**

  | Machine | Address | CPU | Blender |
  |---|---|---|---|
  | slot-1 | studio@192.168.0.115 | 16 cores | 4.5.12, all 50 AU tools pass (B2b) |
  | slot-2 | studio@192.168.0.116 | 16 cores (same mini PC) | in the image; not proven yet, no asset library copy |
  | this PC | — | Ryzen 7 7700X, 8 cores / 16 threads, 31 GB | 4.5.12 (same build), but it also runs Kilo, Studio, Rojo and the local models |

- **Only one asset sprint with Blender at a time:** the coordinator's `blender` resource has a
  capacity of 1 (`tracks/resources.py` `CAPACITY`). A second track sprint that needs Blender
  waits. In the last 7 days it was taken 128 times with no recorded wait, so it hasn't been the
  bottleneck yet. It will be as soon as more asset tracks run side by side.
- **One machine per project:** foundry-blender runs one job per slot (a `flock`) in one checkout
  (`~/work/blender`). A project sends all its Blender to one place: `blender.slot`, or a track's
  own slot.
- **Where the time goes:** the build replay of AS-D11 (implement #4) found 34 Blender runs in
  about 27 minutes. Most took 0.3–1.5 s, six took about 30 s, so about 3–4 minutes in all. The
  rest is the model writing and fixing scripts and looking at renders. **So more Blender
  machines alone won't make an asset much faster.** The speed comes from more asset work running
  at the same time (P1 + P3). The pool makes sure Blender never becomes the queue when that
  happens, and it takes the heavy jobs (renders, selftest suites, bakes) off this PC.

## What gets built

**P1. The pool: every Blender run goes to a free worker (the core).**
- **A worker list:** a new setting `blender.pool`, e.g.

  | Worker | Jobs at once | Used |
  |---|---|---|
  | slot-1 | 2 | first |
  | slot-2 | 2 | first |
  | this PC | 1 | last |

  It replaces the single `blender.slot`. An empty pool means this PC only, as today.
- **foundry-blender picks the worker for each run:**
  1. the worker this sprint used last, when it's free. Its checkout and the library are warm,
     and its outputs come from the same machine;
  2. otherwise any free worker, slots before this PC;
  3. otherwise it waits for the first one to free up (`--wait`, 10 min), then exits 75 as
     today.
- **Several jobs per slot:** each job gets its own lock and its own checkout
  (`~/work/blender-1`, `-2`), and Blender gets the slot's CPU divided by its jobs (`-t`).
  Studio keeps its share (B3).
- **This PC as a worker:** the drop-in runs the local `blender.exe` itself, through the same
  lock, timings and journal.
- **Every sprint goes through the pool** (D-P3): the prompt gives the drop-in, and Kilo denies
  the plain `blender.exe`, as slot mode already does. This also makes the build replay exact
  for every run, instead of read from the transcript.
- **Coordinator:** the `blender` capacity becomes the pool's total jobs (5 with the table
  above), so up to 5 Blender sprints can run at once. `max_agents` (5) and model quota still
  cap it.

**P2. slot-2 ready for Blender.**
- The asset library copied to slot-2 (4.2 GB the first time, then only changes, as B2b).
- The B2 proof on slot-2: `selftest_all.py --blender <drop-in>` for all 50 tools, plus one
  real builder. Its output files are compared with slot-1's (same Blender build: they should
  match).
- slot-2's WebView2 fix (v0.166.2) is a Studio matter; Blender doesn't need it.

**P3. More than one Blender run inside one sprint** (where an asset actually gets faster).
- **Sprint level, small:** the AU prompt (Foundry's adapter text, not the AU repo) tells an
  asset sprint with several models to start their builds together (`tools\guard\run.py --bg`
  through the drop-in, one per model), then `--wait` each. The pool spreads them over the
  workers.
- **Engine level, bigger (gap 9 of the asset review):** one fresh builder session per model,
  each with its own scratch folder and worker, running at the same time, then the sprint session
  judges, exports and records. Start as the D4 trial on 2 assets. It needs its own plan.

**P4. Seeing it.**
- **Access → Machines:** each worker's Blender jobs (running, waiting, last run, CPU).
- **Build replay:** each entry already names its machine.
- **Settings (UI fields):**
  - `blender.pool` (a table: worker, jobs, order);
  - `blender.pool_wait_seconds`;
  - per-slot `blender_jobs`.

## Decisions for Victor

- **D-P1. Which workers:**
  - *Recommended:* slot-1 and slot-2, plus this PC as the last choice with 1 job. This PC also
    runs Kilo, Studio and the local models, so it takes Blender only when both slots are busy.
  - The alternative, slots only, keeps this PC free, but a run waits when both slots are full.
- **D-P2. Jobs per mini PC:**
  - *Recommended:* 2 each. AU's builders are mostly single-threaded Python, so two jobs use the
    cores better than one, and Studio keeps its share.
  - 3 is possible and is a setting.
- **D-P3. All Blender through the pool:**
  - *Recommended:* yes. Every run is scheduled, journaled exactly and uses the same Blender
    build (4.5.12 everywhere, checked).
  - The plain `blender.exe` is denied in sprints, as slot mode does today.
  - The alternative, the pool only for runs the agent sends there, leaves most runs on this PC,
    unscheduled.
- **D-P4. Which worker a run gets:**
  - *Recommended:* the sprint's last worker when free, else any free (slots first).
  - The alternative, always any free, spreads load better but moves a sprint's outputs between
    machines more often.
- **D-P5. How many Blender sprints at once:**
  - *Recommended:* the pool's total jobs (5), still capped by `max_agents` and quota.
  - The alternative is a separate number in Settings.
- **D-P6. Parallel builds inside a sprint:**
  - *Recommended:* the prompt pattern now (P3, small).
  - The builder session per model (gap 9) gets its own plan after the 2-asset trial.
- **D-P7. Your pending `blender.slot = slot-1` draft:**
  - *Recommended:* don't deploy it. The pool replaces it, and turning it on alone would send
    everything to one slot with one job.
  - Deploy `blender.journal` on its own now.

## Sizes, order, checks

- **Order:**
  1. P2 (slot-2 proof; small, mostly running things);
  2. P1 (medium: worker choice, per-job locks and checkouts, local worker, capacity);
  3. P3 sprint pattern (small);
  4. P4 view (small to medium);
  5. gap 9 later, with its own plan.
- **Tests:**
  - worker choice (sticky, free, slots first, this PC last, wait then exit 75);
  - per-job lock and checkout names in the remote script;
  - thread split;
  - the local worker's run, journaled like a slot run;
  - capacity = total jobs;
  - Kilo deny rules with a pool;
  - settings round trip.
- **Proof before switching it on:**
  - on the slots: two builders started together on slot-1 run as 2 jobs; a third goes to slot-2;
    a sixth waits;
  - the outputs of the same builder on slot-1, slot-2 and this PC compared;
  - one real asset sprint on the pool, with its build replay.
- **Not changing:** the AU repo (AU's tools stay as they are; the drop-in takes Blender's
  arguments); Studio gates; how slots run Studio.
