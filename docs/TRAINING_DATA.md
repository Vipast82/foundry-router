# Training-data capture, curation and export

A reusable pipeline that turns verified coding work into training and
evaluation datasets. It records what the coding agent saw and did, what it
changed, and how that change was verified and reviewed. Only examples whose
outcome is proven by captured evidence become training candidates.

AnimalUprising is the first project. Any other project joins through its
`project.toml` profile; the engine does not change.

**This pipeline never starts training, and never sends data to an external
training provider.**

- Every command writes datasets to a local directory.
- The `auto` step, which runs after a sprint, can also upload. When
  `[training.auto] upload = true` (the default once `[training]` is enabled),
  it POSTs each valid, **redacted** dataset to your own Foundry router at
  `[foundry] url`.
- Foundry stores the datasets on its own disk and forwards them nowhere.
- An upload contains redacted transcripts: prompts, tool calls and outputs,
  and assistant turns. Secrets are removed, the content is not.
- Use HTTPS or a trusted LAN for that URL.
- Set `upload = false` to keep everything local. Read the restrictions in
[Using captured outputs](#using-captured-outputs-for-training) before
training on anything it exports.

The capability and gap report, with live-run results, is
[TRAINING_DATA_REPORT.md](TRAINING_DATA_REPORT.md).

## Architecture

```
 Foundry (opt-in capture) ──┐   GET /v1/foundry/capture/records
 Kilo (export / API / DB)  ─┤
 Cline session folders     ─┤   capture adapters        foundry_training/adapters/*
 coordinator store         ─┤   (read-only)
 studio_gate verdicts      ─┤
 git                       ─┘
            │  redact → raw record → normalize → events   ingest.py, redact.py
            ▼
   normalized event store  (versioned, append-oriented, idempotent; provenance,
            │               correlation, explicit capture gaps)        store.py
            ▼
   correlation → episodes → curation (policy, dedupe, splits)
            │     correlate.py   episodes.py   curate.py
            ▼
   exporters: canonical | chatml-tools | dpo  +  validation          export.py
                                                                     validate.py
```

Package: `foundry_training` (Python standard library only). Command:
`python -m foundry_training` or `foundry-training`.

### What each source contributes

| Source | How it is read | What it gives | What it cannot give |
|---|---|---|---|
| Foundry capture | `GET /v1/foundry/capture/records` (orchestration key), or its `capture.sqlite` read-only | The request as received (dialect, roles, system prompt, tool definitions, tool calls and results, settings). The effective request handed to the backend adapter. Actual backend and model, requested persona, output, tool calls, finish reason, usage, timing, errors, cancellation, retries (one row per attempt), and stream reconstruction status. | The provider's hidden prompt; the adapter's wire translation (e.g. OpenAI → Anthropic Messages). This boundary is recorded on every row. |
| Kilo | `kilo export <id>` files (preferred); `kilo serve` session API (follows `/children`); Kilo's DB as a private byte-copy | User prompts, assistant text, tool calls with inputs, tool results (output, error, exit code, `truncated` flag), file edits, step boundaries, coordinator metadata, parent session | The system prompt and tool definitions (Foundry supplies them). Image bytes unless a part embeds them. |
| Cline | Session folders (`<id>.json`, `<id>.messages.json`) | Messages, tool_use / tool_result blocks, cwd | System prompt, tool definitions, image bytes; recorded as gaps |
| Coordinator | Its SQLite store, read-only | Task transitions with evidence, sessions (run, role, phase, persona), verification results bound to a revision with evidence pointers, reviews bound to the reviewed commit, approvals bound to revision and artifact hashes, human corrections | — |
| git | The project workspace | Commits the records reference, base..head diffs (capped) | Uncommitted state |
| Studio gate | The runner's verdict files `<workspace>/.plan/tmp/gate/<SPRINT>/<head12>.json` (docs/STUDIO_GATE.md); `latest.json` and `status.json` are skipped | One `gate_verdict` label per verdict: result, every check, the failed required checks, captures (read only from `artifact_roots`), regression tally | Anything the runner did not write; a capture whose file is gone stays `missing` |

The coordinator source also gives the UI v3 inputs, from optional tables read
only when present: `task_memory` notes written by `helper:<unit>` (prework
patches, with the patch text read from the workspace while it exists) and by
`helper:precode-harvest`, `prework_ready` events, `model_visual_review` events
(vision verdicts) and `operator_critique` / `playtest_critique` events.

## Event schema (version 1)

Every normalized event has:

```
event_id         deterministic: hash(source_adapter, source_record_id, part)
schema_version   1
project_id       never empty; events of different projects never meet
repository_id    the workspace
task_id, session_id, parent_session_id, request_id, tool_call_id
source_adapter   kilo | foundry | cline | coordinator | git | gate
source_record_id the source's own id (export session, capture record uid, ...)
timestamp, event_type, actor_role, actor_model
requested_persona, actual_backend, actual_model
source_revision  the git SHA a verification / review / approval is bound to
artifact_references [{sha256, mime, width, height}]
capture_status   complete | partial | truncated | missing
redaction_status clean | redacted
verification_status PASS | FAIL | UNKNOWN | n/a
facts            what the source observed
inferred         anything derived; kept apart from facts
raw_ref          pointer to the redacted raw record
```

Event types include session_start, child_session, user_message,
assistant_message, reasoning, tool_call, tool_result, file_edit, file_ref,
compaction, model_request, model_call, task_transition, verification,
review, approval, human_correction, commit, label (gate verdict, vision
verdict, critique) and authorship (helper code recorded for a task).

**UNKNOWN stays UNKNOWN.** A missing verification is `unverified`, not
`PASS`. A missing tool result is `missing`; a result is never invented. An
image path is not an image: a `file_ref` is `present` only when the bytes
were read from an allowed root and hashed.

### Correlation

| Link | How | Confidence |
|---|---|---|
| parent_child | Kilo `parentID`; coordinator helper binding | 1.0, observed |
| session_task | Kilo session metadata from the coordinator; coordinator session table | 1.0, observed |
| session_task | `curate --map SESSION=TASK` | 1.0, explicit (not observed) |
| same_model_call | Kilo step and Foundry model_call in the same session with identical tool calls and text | 0.95 |
| same_model_call | several identical candidates (a retry) | 0.4, ambiguity recorded, never merged |

Every query is scoped to one project. A session that names a project
without a profile is skipped, not guessed.

## Episodes

A **trajectory** is one coding session: the conversation as the model saw
it, the tools it had, its base..head diff, and the outcome at that session's
head. A **correction** pairs a failed attempt with the feedback the repair
session received and the repair that was verified.

Context source, best first:

1. `foundry-capture`: the session's last complete model_call. This is the
   effective request (system prompt, tool definitions, every earlier turn)
   plus the response.
2. `kilo-reconstruction`: Kilo's own records, with the system prompt and
   tools marked unknown.
3. `cline`.

Outcomes are labels on the episode, never text inside it, so later
information cannot leak into an earlier prompt. Compaction (a Kilo summary,
or a Foundry context that shrank) is flagged; under the default SFT policy
it excludes the episode, as missing context does. Reasoning parts are
counted and left out.

Authorship comes from configuration only. An assistant turn is `local` when
its actual model is in `local_models` or its persona is in `local_personas`;
otherwise it is `non-local`. Reviewer feedback in a correction is
`frontier`, a human rejection is `human`, and a failed mechanical check is
`coordinator`.

### Labels, tags and per-step authorship (UI v3 fast lane)

`foundry_training/labels.py` adds three things to every episode. Plan:
`docs/plans/AU_UI_REWORK_PLAN.md` section 14.

**Labels** (`ep["labels"] = {gate, vision, critiques}`) are measurements at the
episode's revisions, never text inside the episode. A label belongs to an
episode when its task matches and its revision is one of the heads that
session handed to VERIFYING (or the episode's head).

| Label | From | Holds |
|---|---|---|
| `gate` | `ingest gate` (studio_gate verdict JSON) | `result` PASS / FAIL / UNKNOWN, `mode`, `summary_line`, every check, `failed_checks` (required checks not PASS: SKIP and UNKNOWN count), `captures` with story, size, geometry defects, layout lines, sha256 and status, `error` |
| `vision` | coordinator `model_visual_review` events | `verdict` APPROVED / CHANGES / UNCERTAIN, numbered `findings`, persona, served model, `frames` (path, recorded and actual sha256, status) |
| `critiques` | `operator_critique` / `playtest_critique` events and human rejections | who, numbered findings. A critique without a revision attaches to every later episode of the task. |

Captures of gate and vision labels become `flags.label_artifacts`, so
multimodal episodes get their images. A multimodal export skips an episode
whose label artifact is not `present` (`missing_artifact`).

**Tags** (`ep["tags"]`):

| Tag | When |
|---|---|
| `fast_lane` | the task is in the profile's `[fast_lane] tasks` (nothing when `[fast_lane] enabled = false`) |
| `ui_v3` | a label capture under `docs/evidence/UI-v3/`, or the sprint plan mentions UI-v3 / `au-ui-v3` |
| `template:T1`…`template:T8` | named in `docs/progress/<ID>_PLAN.md`, or implied by a template widget (T1 ModalFrame, T2 CatalogGrid, T3 Showcase, T4 TreeView, T5 ListRows, T6 RewardTrack, T7 ResultsSummary, T8 ConfirmDialog) |
| `widget:<Name>` | a known widget name in the plan (`labels.KNOWN_WIDGETS`, plus `[training.tags] widgets`) |
| `device:<W>x<H>` | a capture size |
| `helper_steps` | the episode has at least one helper step |

**Per-step authorship.** A step is one assistant turn. A local turn that
carried out frontier code is re-attributed as `{"kind": "helper", "model":
"helper:<unit>", "written_by": <local model>, "basis": ...}`:

- `helper:<unit>`: the turn applied a helper's prework patch
  (`.plan/tmp/prework/<task>/<unit>.patch` named in its tool call), or its
  written lines match the patch's added lines (at least 3 lines, 60 % overlap);
- `helper:precode-harvest`: the turn wrote or copied a model harvested from a
  frontier pre-code diff.

Helper steps stay in the conversation as context and are never training
targets. The local primary's own turns, including code written around a
harvested model, stay eligible. Helper guidance that is not code (plans,
interfaces, reviews, findings) is not authorship. `flags.helper_steps` lists
each re-attributed turn.

**Correction triples.** A correction pairs an attempt, the feedback the
session received and the verified fix. Two new feedback sources:

- `gate`: a red studio gate verdict at the failed attempt's head (author
  `coordinator` / `studio_gate`);
- `vision`: vision CHANGES findings (author `frontier`, the vision persona).
  A vision CHANGES on the earlier attempt also makes it a correction even if
  its code review approved.

In the fast lane the findings go into the **same** session, so one session
can hold the whole triple. `labels.inline_corrections` builds an inline
correction for each red label at an earlier head whose findings reappear in a
later user turn: the attempt and the findings are context (`context_until` =
the findings turn), and the turns after it are the target. After a frontier
hand-off the fix turns are non-local, so that triple is kept for the project
but excluded from training under the default policy.

## Curation

Each episode ends in exactly one status, with reasons:

- **selected**: meets the project's policy for its dataset kind.
- **eval**: its group is assigned to evaluation and never exported for training.
- **excluded**: one or more reasons below.
- **duplicate**: an exact or near duplicate (MinHash, Jaccard ≥ 0.9 on content only); it joins the canonical episode's group.
- **deleted**: removed by `forget`.

Exclusion reasons:

- Verification: `unverified`, `verification_failed`, `verification_unknown`.
- Review and approval: `review_missing`, `review_not_approved`, `unresolved_findings`, `approval_missing`.
- Tool sequence: `truncated_tool_output`, `missing_tool_result`, `broken_tool_sequence`.
- Content: `missing_context`, `non_local_author`, `no_local_target`, `loop`, `no_feedback`, `missing_artifact`, `secret_exposure`.
- Router outcome (only for a policy that lists the tokens below): `router_outcome_missing`, `router_not_ok`, `router_unverified`.

With `allow_frontier_targets = false` (the default) the authorship rule
applies to every dataset kind, whether or not a policy lists `local_author`.
A frontier or other non-local turn excludes the episode (`non_local_author`).
Helper steps do not: they are weighted out at export. An episode whose every
assistant turn is a helper step has nothing to train on (`no_local_target`).

Failed episodes are kept. They never become positive training data, but
remain available to evaluation sets and preference pairs.

**Splits** are by group (project:task, so all attempts, helpers and
duplicates of one task land on the same side). Assignment is
`hash(seed:group) < eval_fraction`, a held-out project list, or tasks first
seen on or after `time_cutoff`. An episode that was ever exported in an
evaluation set is blocked from training by group and by content hash, even
if the split policy changes later (tested).

**Preference pairs** are only built for the same decision: two model calls
with an identical request context (messages and tools), different answers,
and a justification. That is either opposite verified outcomes (PASS
chosen, FAIL rejected) or a recorded human judgment
(`prefer --chosen --rejected --by --note`). A human preference between
different contexts is refused.

## Export targets

| Target | Format | Notes |
|---|---|---|
| canonical | One JSON object per episode: messages with authorship, tools, revision, outcome, flags, provenance (event ids, sources), split, diff; corrections add original, feedback (with author) and accepted correction | The archival format |
| chatml-tools | OpenAI-style `{"messages", "tools"}` JSONL, as read by TRL, Unsloth and Axolotl for tool-calling chat models | Checked against Qwen-family ChatML with Hermes `<tool_call>` conventions: system first, results follow their calls, ends on an assistant turn without open calls. Length is estimated at chars/3.2, or counted exactly with `--tokenizer tokenizer.json` (needs the optional `tokenizers` package). Long examples are cut only at a complete assistant turn; if no prefix fits, the example is left out. |
| dpo | `{"prompt", "tools", "chosen", "rejected", "basis", "judge"}` | Preference pairs only |

**Per-turn targets.** When an example has context-only turns (helper steps,
or the turns up to the feedback in a correction), each assistant turn in
`chatml-tools` carries `"weight": 1` (target) or `"weight": 0` (context), the
per-message switch TRL and Axolotl chat datasets read, and `meta.context_only`
lists the weighted-out turns. Without such turns the line is unchanged. An
example left with no weight-1 turn is skipped (`no_target_turn`). `canonical`
lines add `labels`, `tags` and `targets` (the indexes of the target turns).
`validate` fails a line whose context-only turn is not weight 0, or whose
frontier-authored turns are targets while frontier targets are off.

**Tag filter.** `export --tag T` (repeatable) keeps only episodes carrying
every given tag; the rest count as `tag_filter` in the skip counts, and the
manifest records the tags. Example, the Roblox-UI subset:

```sh
python -m foundry_training ingest gate --project animaluprising
python -m foundry_training curate --project animaluprising
python -m foundry_training export --project animaluprising --kind sft --target chatml-tools --tag ui_v3
python -m foundry_training export --project animaluprising --kind correction --target canonical --tag fast_lane --tag ui_v3
```

`queue` and `auto` run `ingest gate` for each project themselves, so after a
sprint closes only the `export --tag` step is manual.

Every dataset gets `manifest.json`: projects, policy hashes, split policy,
episode ids, skip counts, validation result, token method,
`reasoning_included: false` and the restrictions below. A dataset that fails
validation is recorded as `invalid`.

## Storage, privacy and security

```
~/.foundry-training/            ($FOUNDRY_TRAINING_STORE; 0700)
  raw/raw.sqlite                redacted source records (0600)
  normalized.sqlite             events, correlations, gaps, checkpoints, runs, artifact refs
  curated.sqlite                episodes, datasets, members, preferences, deletions
  artifacts/<sha256>            images read from allowed roots (content-addressed)
  datasets/<id>/                training exports
  eval/<id>/                    evaluation exports (never mixed with datasets/)
Foundry: <data>/training/capture.sqlite   (0700 dir, 0600 file)
```

- **Opt-in.** Foundry capture is off unless `training_capture.enabled`. The
  pipeline ingests only for projects whose profile has
  `[training] enabled = true`, and only from the listed `sources`. Foundry
  has `projects` and `exclude_projects` lists; unbound traffic is captured
  only with `include_unbound`; `clients` can narrow it further.
- **Redaction before persistence.** Foundry redacts every record before
  writing it, using `foundry_training.redact` plus every secret value in
  its own configuration. The pipeline redacts again before the raw record,
  scans again at curation (`secret_exposure`) and validates again at export.
  Removed: authorization, cookie and API-key headers; bearer tokens; private
  keys; AWS, GitHub, Slack, Google, OpenAI and Anthropic keys; JWTs; Roblox
  cookies; `password=` / `token:` assignments; email addresses (except
  allowed domains); and any value supplied as a known secret (the CLI adds
  every environment variable whose name contains KEY, TOKEN, SECRET or
  PASSWORD). Redaction is a floor, not a guarantee; that is why export
  re-validates.
- **No credential files.** No adapter opens credential files. Kilo's
  `credential`, `account` and `session_share` tables are never queried.
- **Never committed.** The store refuses to live inside a git work tree
  unless that path is ignored (`--allow-in-repo` overrides for tests).
- **Access control.** Directories are 0700 and files 0600 (POSIX). **There is
  no application-level encryption at rest.** Keep the store on an encrypted
  volume (BitLocker on Windows, LUKS or FileVault elsewhere). On Windows,
  POSIX modes do not map to ACLs; put the store under your user profile.
- **Dashboard.** Foundry's Training tab (Logs → Training) shows counts,
  gaps, datasets and redacted episode cards (first 300 characters per turn).
  Cards that still look secret are refused at the endpoint. No raw payloads
  are served.

## Setting it up

1. **Foundry** (`config.yaml`). Enable capture for the projects you want:
   ```yaml
   training_capture:
     enabled: true
     projects: [animaluprising]     # empty = every coordinator-bound project
     retention_days: 30
   ```
2. **Profile** (`project.toml`). Add a `[training]` section. AnimalUprising's
   is in `foundry_coordinator/projects/animaluprising/project.toml`. The
   fixture-game example (`contrib/coordinator-pilot/fixture-game/project.toml.template`)
   is a second project with a different policy:
   ```toml
   [training]
   enabled = true
   sources = ["kilo", "foundry", "coordinator", "git"]
   artifact_roots = ["docs/evidence"]
   local_models = ["qwen3.8-flash-next-iq3_s"]
   local_personas = ["Foundry-Kilo-Local"]
   [training.policy.sft]
   require = ["verification_pass", "review_approved_at_revision", "complete_tool_sequence",
              "system_and_tools_known", "local_author", "no_loop", "approval_if_required"]
   max_tokens = 32768
   [training.split]
   eval_fraction = 0.15
   seed = "au-v1"
   [training.capture]
   on_failure = "warn"           # coordinator: ignore | warn | hold
   [training.tags]
   widgets = []                  # extra widget names to tag (labels.KNOWN_WIDGETS)
   ```
   The pipeline also reads `[fast_lane] tasks` from the same profile for the
   `fast_lane` tag.
3. **Coordinator.** Nothing to run. With `[training] enabled`, each task that
   closes (COMPLETE, closed awaiting approval, or FAILED) writes a pointer
   manifest to `<state>/training-spool/`. The manifest lists the task's
   sessions with roles and clients, the base, head and reviewed SHAs,
   verification sets, evidence paths, reviews, approvals, completeness and
   gaps. It holds no prompts or outputs. `on_failure` decides what happens
   if writing it fails:
   - `ignore`: carry on.
   - `warn` (default): carry on and record a `training_capture_failed` event.
   - `hold`: keep the task in CLOSING and retry each tick, without re-running
     the closeout checks. Dependents wait.

   Capture never fails a task.
4. **Pipeline** (asynchronous; run it when convenient, e.g. after a queue
   settles):
   ```sh
   export FOUNDRY_TRAINING_PROFILES=C:/foundry/profiles   # same as the coordinator's
   export FOUNDRY_TRAINING_KEY=...                        # an orchestration-scope client key
   python -m foundry_training queue --spool <state>/training-spool \
       --foundry-url http://192.168.0.112:11435 --kilo-url http://127.0.0.1:4096
   python -m foundry_training status
   python -m foundry_training export --project animaluprising --kind sft --target chatml-tools
   python -m foundry_training push --foundry-url http://192.168.0.112:11435
   ```

### Automatic after every sprint (v0.119.0)

You don't have to run the pipeline yourself. When a task closes, the
coordinator writes its manifest and then starts `python -m foundry_training
auto` in the background. That run never blocks or fails the task. It does four
things:

1. **Parse:** ingests and curates everything the manifest points at, the same as
   `queue`.
2. **Export:** writes validated fine-tuning datasets for each kind (`sft`,
   `correction`, `multimodal`) whose selected episodes changed since the last
   export. Each one is written in every configured format: `chatml-tools` (the
   messages + tools JSONL that TRL, Unsloth and Axolotl read) and `canonical`
   (full lineage).
3. **Store centrally:** copies each dataset into one folder:
   ```
   ~/.foundry-training/library/
     INDEX.json                          every dataset, newest first: project, kind, format,
                                         examples, validation, episodes, folder, upload status
     README.md
     <project>/<kind>/<format>/latest.jsonl        always the newest of that kind/format
     <project>/<kind>/<format>/<dataset_id>/{data.jsonl, manifest.json}
   ```
4. **Upload:** sends each new valid train dataset to Foundry
   (`POST /v1/foundry/training/datasets/{id}`).
   - Foundry re-checks every line for secrets, stores the files under
     `<data>/training/datasets/<project>/<id>/` and lists them in the
     **Training** tab, where you can download `data.jsonl` / `manifest.json`.
   - If Foundry is down, refuses the upload or no key is set, the dataset
     stays in the library. `INDEX.json` says why, and the next run retries.

No LLM is used in any step. Evaluation sets are never uploaded.

Settings (profile, all optional; defaults shown):

```toml
[training.auto]
enabled = true                 # on whenever [training] enabled = true
upload = true
foundry_url = ""               # default: [foundry] url
key_env = "FOUNDRY_TRAINING_KEY"   # falls back to [foundry] key_env (the coordinator key)
kilo_url = ""                  # default: [client] url
kinds = ["sft", "correction", "multimodal"]
targets = []                   # default: [training.export] targets
library = ""                   # default: <store>/library
store = ""                     # default: $FOUNDRY_TRAINING_STORE or ~/.foundry-training
```

How to run it or switch it off:

- **One process-wide off switch:** set the environment variable
  `FOUNDRY_TRAINING_AUTO=0` for the coordinator.
- **Output:** the run's log is `<state>/training-spool/auto.log`.
- **Overlap:** only one run happens at a time (`.auto.lock`). A manifest that
  arrives while a run is finishing is processed by the next sprint's run, or by
  running the command yourself:

  ```sh
  python -m foundry_training --profiles <profile> auto --spool <state>/training-spool
  ```

For complete episodes, keep Foundry capture on (`training_capture.enabled`).
Without it, Kilo-only sessions lack the system prompt and tool definitions and
the default SFT policy excludes them (`missing_context`). In that case the run
reports no datasets rather than producing incomplete ones.

### Commands

```
ingest kilo --export FILE...               kilo export files
ingest kilo --url URL --session ID...      a running kilo serve
ingest kilo --db PATH [--directory DIR]    Kilo's DB (byte-copied, never opened in place)
ingest foundry --url URL | --capture-db P  Foundry capture (incremental by record id)
ingest coordinator --db PATH               coordinator store, read-only
ingest cline --sessions DIR
ingest git --project P
ingest gate --project P                    studio_gate verdict JSON (labels + captures)
queue --spool DIR [--foundry-url U] [--kilo-url U]
curate --project P [--map SESSION=TASK...]
export --project P... --kind sft|correction|preference|multimodal
       --target canonical|chatml-tools|dpo [--purpose train|eval] [--tokenizer F]
       [--tag T ...]
validate DATASET_DIR --target T
status [--json] | inspect EPISODE | gaps [--project P]
prefer --project P --chosen EVENT --rejected EVENT --by NAME --note TEXT
forget EPISODE --reason TEXT [--sources]
retention | recover | push --foundry-url U [--id NAME]
auto --spool DIR [--foundry-url U] [--kilo-url U] [--library DIR] [--no-upload]
```

## Operating it

- **Idempotent and recoverable.** Event ids are deterministic and inserts
  ignore duplicates, so any ingest can be re-run. Each record is committed
  with its checkpoint. A killed or interrupted run is listed by `recover`
  until a later run of the same source completes; the fix is to run the
  same command again (tested).
- **Queue failures** are never discarded. A manifest whose processing fails
  stays in the spool with a `curation-failed` gap. Sessions the manifest
  lists but nobody captured are recorded as `session-not-captured`.
- **Retention.** Run `retention` regularly (Task Scheduler or cron). Raw
  records older than `raw_days` (default 30) are deleted. Normalized events
  older than `normalized_days` (default 180) are deleted unless a live
  episode still needs them for lineage. Foundry prunes its capture file
  after `retention_days` in its retention loop.
- **Backup.** Stop ingestion and copy the store directory. It is plain
  SQLite plus files. Treat backups as sensitive as the store (same
  encryption, same access). Foundry's `capture.sqlite` is not part of
  Foundry's config export bundle.
- **Deletion.**
  - `forget EPISODE --reason ...` marks the episode deleted, so it never
    appears in a future export. It lists every dataset already built from
    it and marks those datasets `contains-deleted`.
  - `--sources` also blanks the episode's normalized events and raw records.
  - Files already written under `datasets/` or `eval/` are **not rewritten**;
    delete or rebuild the affected datasets.
  - To remove a project entirely, delete its rows or the whole store, and
    Foundry's capture rows (`DELETE FROM capture_records WHERE project_id=?`).

## What to capture, and the router's quality signal

**Switches** (Training tab, Capture card; or `training_capture` in config.yaml). All
live: the next model call follows a saved change, no restart.

- `enabled`: the master switch.
- `backends` / `exclude_backends`, `personas` / `exclude_personas`: glob lists
  (case-insensitive; an empty include list means all). They are checked per backend
  call, so any backend works (local, Meridian, CLIProxyAPI) and any persona. The
  `received` record of a request is written together with its first allowed call.
- `include_unbound`: also capture calls no coordinator project is bound to. A
  persona named in `personas` is captured even outside a coordinator project, so
  Chat, Research or Cline calls can be switched on by name.
- The card shows calls captured per backend and persona today, and downloads the
  captured calls as JSONL: one `{messages, tools, meta}` line per successful
  call (`GET /admin/api/training/capture/download`, with `quality=ok|verified`).

**Quality signal.** Capture rows are joined, when read, to what the router knows
about the same request id (`GET /v1/foundry/capture/records` and the download add
a `quality` block): the request's `outcome` (ok, error, cancelled, timeout,
denied), thumbs up / down feedback, perf (TTFT, tokens/s and `timing_src`) and the
coordinator's verified labels. The coordinator bridge posts those labels to
`POST /v1/foundry/training/outcomes {label, request_ids | project_id+task_id
[+session_ids, run_id, until_ts], evidence}` after a gate PASS (`gate_pass`), a
review APPROVED (`review_approved`) or a vision approval (`vision_approved`); the
failures are `gate_fail`, `review_changes`, `vision_changes`. A label by work
covers the implementer's calls up to that event. The pipeline ingests the labels
as `router_outcome` events next to the capture records, so a label that arrives
after a call was ingested still reaches its episode.

Two curate policy tokens read that signal (`episode.outcome.router`):

- `router_ok`: the session's final call succeeded, nobody rated it down, and no
  negative label was recorded.
- `router_verified`: `router_ok` plus at least one positive label.

```toml
[training.policy.sft]
require = ["router_verified", "complete_tool_sequence", "system_and_tools_known"]
[training.export]
allow_frontier_targets = true       # provider terms: see below
```

The default policy is unchanged. Whose output may be a target is still decided only
by `allow_frontier_targets`; the tokens only say whether the router saw the work
succeed. The router never changes that flag.

## Using captured outputs for training

The export manifest repeats these:

- Captured content can include third-party material: game assets,
  documentation, dependency code, tool output. The export grants no right to
  redistribute it.
- Outputs of hosted models (Anthropic, OpenAI and others via Meridian or
  CLIProxyAPI) are governed by those providers' terms, which restrict using
  outputs to develop competing models. Frontier-authored turns are
  therefore excluded as training targets (`non_local_author`) unless a
  profile sets `allow_frontier_targets = true`. Steps that applied a
  frontier helper's code (`helper:<unit>`, `helper:precode-harvest`) are
  context only (weight 0), never targets. Reviewer findings appear only
  as feedback inside correction records, attributed to their author. Decide
  with your own reading of the current terms before training on them.
- Check the local base model's license before fine-tuning or distributing
  weights.

## Limitations (as of this release)

- The training stack is not identified. `chatml-tools` follows the common
  TRL / Unsloth / Axolotl conversational format and Qwen ChatML / Hermes
  conventions. The renderer used for length checks is a re-implementation;
  verify it against the actual tokenizer's chat template before training.
- Kilo exports and the Kilo API carry no system prompt or tool definitions.
  Without Foundry capture for the same session, an episode is
  `missing_context` under the default SFT policy.
- Foundry sees the request it handed to the backend adapter, not the
  provider's hidden prompt or the adapter's wire translation.
- Image capture needs the bytes in an allowed root or embedded in the
  record. Kilo `file` parts with only a path outside `artifact_roots` are
  `not-permitted`, and missing files are `missing`.
- There is no application-level encryption at rest (see Storage).
- The Kilo checkpoint check (`au.kilo_checkpoint`) still reads Kilo
  separately. Moving it onto this normalized adapter is future work.
- Helper-step detection matches patch paths and added lines. Helper code the
  local model retyped with heavy changes (under 60 % line overlap) counts as
  local. Prework patches live in a git-ignored tmp dir; once deleted, only
  path matching is left.
- Nothing in the coordinator writes `operator_critique` / `playtest_critique`
  events yet (TODO); until then critiques come only from human rejections.
- Cline ingestion is implemented against the folder layout Foundry's replay
  harness and AnimalUprising's checkpoint script read. It has not been run
  against a live Cline in this work.
