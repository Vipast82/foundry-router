# Training-data pipeline: capability and gap report (2026-10-15)

**Status: built and tested here.** That covers unit and acceptance tests, plus
a live run against real Kilo 7.8.8, real Foundry and the real coordinator,
with mock models. It has **not yet run on Victor's workstation** against
Strata, Meridian or CLIProxyAPI traffic, and **no dataset has been used for
training**. The operator guide is [TRAINING_DATA.md](TRAINING_DATA.md).

Legend: **verified** = shown by a test or the live run in this work;
**inspected** = read from the source or records, not exercised end to end;
**future** = not built.

## 1. What was inspected before building

| Source | Finding | Status |
|---|---|---|
| Foundry inference path | Both facades (OpenAI and Ollama) converge on `_chat_dispatch`. The OpenAI endpoint converts first. The pool's `chat` / `chat_stream` see the *effective* request: Foundry's canonical messages, with tool ids paired by `pair_tool_call_ids`, tools, options, max tokens and think. Request context carries client, session, project, task, run, role and persona. | inspected, then verified by capture tests |
| Coordinator identifiers | Sessions table (session, project, task, run, role, phase, attempt, client, persona, parent). Transitions carry `session_id`, `base_sha`, `head_sha`, `reviewed_commit_sha`, `findings`. Verification sets are bound to a revision with evidence paths. Reviews are bound to `reviewed_sha`; approvals to revision and artifact hashes. | verified |
| Kilo 7.8.8 export | `kilo export <id>` gives `{info, messages[{info, parts}]}`. Parts: text, reasoning, tool (state.input, output, status, metadata.exit, metadata.truncated), step-start / step-finish (tokens), patch, file. Session `metadata` carries the coordinator's labels; `parentID` links helpers. | verified (committed fixture) |
| Kilo export: absent | No system prompt, no tool definitions. Child sessions are separate sessions. | verified |
| Kilo `--sanitize` | Replaces every text, tool input/output, title and metadata with `[redacted:…]` placeholders. Safe to share, useless for training. Kept only as provenance and flagged as a gap. | verified |
| Kilo `serve` API | `GET /session/{id}`, `/session/{id}/message` (same `{info, parts}` shape as the export) and `/session/{id}/children`, with the `directory` query. | verified (live) |
| Kilo database | `~/.local/share/kilo/kilo.db`, WAL mode. `session` / `message` / `part` (JSON in `data`), plus `credential`, `account` and `session_share` (holds a `secret`). | inspected (schema read from a copy) |
| Kilo writes its own DB | Running the Kilo CLI (`kilo export`) changed `kilo.db`'s hash. That is Kilo's own write, but it means "run `kilo export`" is not a read-only operation on Kilo's state. | verified |
| SQLite and WAL | Opening a WAL database even with `mode=ro` creates or touches its `-wal` and `-shm` files. The first snapshot implementation did exactly that; it was caught by a test and replaced with a byte copy. | verified |
| Cline | Session folders `<id>/<id>.json` (metadata, cwd) and `<id>.messages.json` (Anthropic blocks). No system prompt, no tool definitions. | inspected (layout used by Foundry's replay harness) |
| Privacy controls | Foundry already masks secrets in config, logs and diagnostics (`configops.secret_values`). Client keys are scoped. Request logs hold no bodies. | inspected; capture reuses `secret_values` |

## 2. Capture boundaries

- **Foundry** records what it received from the client and what it handed
  to the backend adapter. It does not see the provider's hidden prompt or
  the adapter's wire translation (for example OpenAI → Anthropic Messages).
  Reasoning goes in its own field and is never exported. Each pool attempt
  is its own row: status ok, error or cancelled, and for streams whether the
  answer was reconstructed `complete` or `incomplete`. The boundary text is
  stored on every row and shown in the UI.
- **Kilo** records what the agent did, but not the prompt framing it ran
  under. An episode built only from Kilo is marked `kilo-reconstruction`,
  `system_and_tools_known: false`, and is excluded (`missing_context`) under
  the default SFT policy.
- **Cline** has the same gap as Kilo.
- **Images**: a path is not an image. A `file_ref` is `present` only when
  the bytes were read from an allowed root and stored by hash. Otherwise it
  is `missing` or `not-permitted`.

## 3. Acceptance items

All in `tests/training/`. Every item is **verified**.

| # | Item | Test(s) |
|---|---|---|
| 1 | Real Kilo session imported without modifying its source | `test_real_kilo_export_imported_without_touching_the_source`. Also `test_kilo_database_is_read_through_a_readonly_snapshot`: a database with Kilo 7.8.8's layout in WAL mode keeps the same main and WAL hashes and mtimes, and its credential and share secrets never reach the store. `test_kilo_database_with_an_unexpected_schema_is_refused`, `test_sanitized_export_is_provenance_only`. |
| 2 | Foundry/client duplicates reconciled | `test_foundry_and_kilo_records_of_the_same_calls_are_reconciled`: 3 links at 0.95 and one episode with Foundry's context. `test_identical_retries_are_recorded_as_ambiguous_not_merged`. |
| 3 | Parent and helper linked | `test_parent_and_helper_sessions_are_linked`: observed link; same group, so the same split. |
| 4 | Complete tool round trip exported | `test_complete_tool_round_trip_exports_as_valid_chatml_tools`: system → user → write → result → bash → result → final, with authorship `local`. |
| 5 | Truncated sequence rejected | `test_truncated_tool_output_is_rejected`; `test_cut_off_stream_and_unfinished_tool_are_rejected` (partial call stored as partial, result never invented) |
| 6 | Verified correction with the correct source revision | `test_verified_correction_carries_true_revisions_and_authorship`: original base→head1, correction head1→head2, reviewer feedback attributed `frontier` |
| 7 | Synthetic secret redaction | `test_synthetic_secrets_are_redacted_before_storage_and_at_export`: GitHub token, bearer JWT, PEM key, AWS key, Roblox cookie and a known value. None appear in any SQLite file or in the export. Plus Foundry-side `tests/test_training_capture.py`. |
| 8 | Missing screenshot marked missing | `test_missing_screenshot_is_marked_missing_never_assumed`: present (hashed, 3×2 PNG), missing, and not-permitted; multimodal policy gives `missing_artifact` |
| 9 | Cross-project isolation | `test_projects_are_never_merged`: an unconfigured project is skipped; B's Foundry rows reusing A's session id are not attached to A; no cross-project duplicates; A's export holds only A |
| 10 | Idempotent re-ingestion | `test_reingestion_is_idempotent` (Kilo, Foundry checkpoint, coordinator, curation) |
| 11 | Train/eval leakage prevention | `test_train_eval_leakage_is_prevented`: an episode once exported for eval stays out of training after a split change. A near-duplicate under another task joins the canonical group. |
| 12 | One target format validated | chatml-tools in test 4: structure, pairing, JSON arguments, no reasoning keys. A broken file and a reasoning field are rejected. |
| 13 | Recovery after interrupted ingestion | `test_interrupted_ingestion_recovers_without_loss_or_duplicates`: the checkpoint is kept, the same command resumes, and the result equals a fresh ingest. `test_ingestion_error_leaves_a_record_and_raises`. |
| 14 | Second example project profile | `test_second_project_profile_changes_policy_not_code`: the fixture-game and AnimalUprising profiles reach different decisions on the same episode with no code change |

Also verified:

- **Engine integration**, in `test_integration.py`:
  - a manifest is written on COMPLETE;
  - nothing is written when training is disabled;
  - `warn` completes and records the failure;
  - `hold` stays in CLOSING without re-running the closeout checks, then completes once the manifest is written.
- **Preference pairs**: only for the same decision, justified by PASS vs FAIL; a human preference across different contexts is refused; the dpo export carries the tool calls.
- **Foundry endpoints**: orchestration scope required; a secret-looking card is refused; the admin view serves summaries only.
- **Regressions from the live run** (section 5): reviewer calls, shared boilerplate, closeout measurement.

Full suite: 1140 passed.

## 4. Live run (contrib/coordinator-pilot, `--training`)

Real `kilo serve` 7.8.8, real Foundry (with `training_capture: {enabled: true}`)
and the real coordinator CLI, over two projects. The models were mocks.

- **Coordinator**:
  - 4 tasks COMPLETE (fixture-game FX-1 and FX-2 needed a repair; AnimalUprising PILOT-C1 and C2 passed first time).
  - 4 closeout manifests written to the spool.
- **Foundry capture**: 80 records (42 received, 38 backend_call) across both projects.
- **Queue processing**:
  - Ingestion: Kilo via the server API, Foundry via the capture API, the coordinator store and git.
  - Correlation: 33 Kilo-step ↔ Foundry-call links, all at 0.95 (19 AnimalUprising, 14 fixture-game), none ambiguous. Plus 8 observed session→task links.
  - Every episode has `foundry-capture` context, with Kilo's real system prompt.
  - No session listed in a manifest was left uncaptured.
- **Kilo DB**: the copy-only snapshot of the running Kilo's database left `kilo.db` and `kilo.db-wal` byte-identical. Re-ingesting the same 8 sessions from the snapshot produced 0 new and 156 existing events: the API and DB paths give the same event ids.
- **Curation**:

  | Project | Selected | Excluded | Eval |
  |---|---|---|---|
  | AnimalUprising | 2 implementation trajectories | 2 closeouts (`review_missing`: no review covers the closeout diff) | — |
  | fixture-game | 1 trajectory + 1 correction | 1 (FX-2 attempt 1, `verification_failed`, `review_missing`) | 3: FX-1's group, assigned by the 0.5 example split, including its CHANGES_REQUIRED attempt and its correction |

- **Exports**, all validated:
  - AnimalUprising: sft chatml-tools 2, sft canonical 2, correction 0.
  - fixture-game: 1 each for sft chatml-tools, sft canonical and correction canonical.
- **Redaction**: 0 findings; the pilot's transcripts carried no secrets, as expected. The Kilo key lives in `kilo.jsonc`, which is never read.
- **Report**: pushed to Foundry. The Training tab showed it with no page errors at 1440 px and 390 px.

## 5. Problems the live run found (fixed and covered by tests)

1. **The coordinator's review calls became trajectories.** Review sessions
   exist only in Foundry (role `reviewer`), not in the coordinator's session
   table. Roles are now read from Foundry's bindings too, and review calls
   stay outcome labels.
2. **Closeout sessions were `unverified`.** Their head is now the revision
   the closeout checks ran on, and their base is the approved review's
   commit. They are measured by the closeout checks. The pre-closeout review
   is not relabelled as covering the closeout's changes.
3. **Different AnimalUprising sprints were marked near-duplicates.** Their
   prompts share about 17 KB of rules, so whole-conversation MinHash was
   0.97. Near-duplicates now need both the prompt and the actions
   (assistant + tool turns) at ≥ 0.9. Exact duplicates still use the full
   content hash.

Found by tests while building: the WAL side-file behaviour (section 1),
near-duplicates hidden by per-session tool-call ids, and `recover` missing
interrupted runs.

## 6. Restrictions on using captured outputs

These are repeated in every export manifest:

- Third-party content (assets, docs, dependencies, tool output) may appear;
  the export grants no right to redistribute it.
- Hosted-model outputs (Anthropic or OpenAI via Meridian or CLIProxyAPI) are
  governed by provider terms that restrict using outputs to develop
  competing models. Frontier-authored turns are excluded as targets by
  default (`allow_frontier_targets = false`). Reviewer findings appear only
  as attributed feedback inside correction records. Whether even that use
  is acceptable is for Victor to decide with the current terms in hand.
- Check the local base model's license (the Qwen-family model behind
  `Foundry-Kilo-Local`) before fine-tuning or distributing weights.

## 7. Gaps and future work

| Gap | Effect | Plan |
|---|---|---|
| Training stack not identified | `chatml-tools` follows TRL / Unsloth / Axolotl conventions and a re-implemented Qwen ChatML / Hermes renderer; token counts are estimates unless `--tokenizer` is given | future: add the actual stack's adapter and check against its chat template |
| No encryption at rest | Store protected by 0700/0600 modes only | operator: encrypted volume (BitLocker); future: optional SQLCipher |
| Windows ACLs | POSIX modes don't map to ACLs | operator: keep the store under the user profile |
| Kilo without Foundry | No system prompt or tools → `missing_context` | keep Foundry capture on for coordinated work |
| Closeout episodes | Excluded by default (no review at the closeout head) | policy choice; a profile may drop `review_approved_at_revision` for closeouts in future |
| Cline | Adapter built against the known folder layout; not run against a live Cline here | future: live check |
| Kilo checkpoint audit | `au.kilo_checkpoint` still reads Kilo itself | future: reuse the normalized adapter |
| Datasets already written | `forget` marks them `contains-deleted` but doesn't rewrite files | operator: rebuild or delete |
| Workstation run | Not run against Strata, Meridian or CLIProxyAPI traffic | next step on the workstation |
