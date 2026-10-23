# Playbook — cross-project knowledge base (A1) — design

2026-10-23, approved by Victor and built the same day (see the as-built notes at
the end for the small deviations). Implements item A1 of
`reports/UI_AUTOMATION_REVIEW_2026-10-23.md`: the org-wide wiki — templates,
how-tos, verified fixes, scripts, formats — that every agent in every project
pulls from, so recurring knowledge stops being rediscovered per sprint.

Principle throughout: **reuse the project-memory machinery** (`foundry_coordinator/memory/`),
never duplicate it. Per-project memory stays exactly what it is (project facts);
the playbook is the cross-project layer beside it.

---

## 1. The playbook repo

- A **separate git repository** (suggested: a new local repo `foundry-playbook`
  next to the game repos; the coordinator can `git init` it on first use).
  Its path is configured **per install**: `--playbook PATH` on the coordinator
  command line, or `$FOUNDRY_PLAYBOOK` (flag wins). `doctor` reports it.
  **No path configured → every playbook feature is inert** (same pattern as
  memory without the layout).
- Layout (created idempotently by `playbook init`, like `memory init`):

  ```
  README.md
  templates/<name>.md     sprint plans, closeout, evidence packet shapes
  howto/<name>.md         recurring procedures
  fixes/<name>.md         error signature → verified fix
  scripts/<name>.md       reusable scripts as fenced code blocks + usage
  formats/<name>.md       formatting standards
  ```

  Everything is markdown in the **same heading-entry format** as memory, so the
  one indexer handles it all. A script lives as a fenced block inside its
  `scripts/<name>.md` (the fence stays inside the entry — the parser already
  does this). A `fixes/` file groups entries as `## <signature>: <title>`, body:
  what the error looks like, cause, the verified fix, where it was seen.
- New module `foundry_coordinator/playbook/` — `layout.py` (dirs, `file_ok`,
  templates, init) and `manager.py` (`PlaybookManager`, built in `Coordinator.__init__`
  beside `MemoryManager`). It **reuses** `memory/index.py` (`MemoryIndex`),
  `memory/notes.py` (parsing, validation, section editing) and `memory/slices.py`
  (scoring, render, hashing) as libraries.

## 2. Index and slices

- **Index:** the existing `memory_files` / `memory_entries` / `memory_fts`
  tables keyed by the reserved id **`@playbook`** (they're keyed by
  `project_id`, so this needs no schema change). Same cheap mtime/size
  reindex on tick, same tags (paths, needs words, heading words), plus
  per-kind tags (`fixes`, `howto`, `templates`, ...).
- **Slices:** `PlaybookManager.slice(P, spec, role, needs, signature="")`
  uses `slices.select` with base weights `fixes 1.0, howto 0.5, templates/
  formats/scripts 0.3`. When the prompt follows a stop, the stop's failure
  signature (autoheal's `infra_signature` name or the normalized class) is
  added as a high-weight query tag, so the matching `fixes/` entry surfaces
  exactly when it's needed.
- **Shared budget:** per A1, the playbook joins the memory slice **under the
  same token budget** (`memory_slice_tokens`, reviewer/lead halving included):
  the engine builds the memory slice first, the playbook fills what's left
  (floor of ~400 tokens so a fat memory never starves an exact `fixes/` match;
  the fixes match is placed first). Rendered with the same
  `### file > heading` paragraphs and **appended to the same block**, so the
  existing `engine._memory_delta` per-entry hashing applies unchanged —
  an entry a session already received is never re-sent.
- **All lanes get it:** the block is built in the one place prompts already
  pick up memory (`engine._with_operator_notes` / `_memory_block`) and in the
  lead's and helpers'/lanes' prompt builders via the same shared call.
- Each slice is recorded in a new **`playbook_slices`** table (same shape as
  `memory_slices`; separate table because that one's primary key is
  `session_id`) — this is what the UI shows as "which sessions received
  which entries".

## 3. kb_search and writes

- **Operator chat:** `kb_search`, `kb_add`, `kb_edit` tools mirroring
  `memory_search/add/edit` (same confirm cards, same validation); the chat
  context block gets a one-line playbook summary. Control-channel kinds
  `playbook_add` / `playbook_edit` beside the existing memory kinds.
- **Sessions (read):** a minimal read-only stdio MCP server
  (`python -m foundry_coordinator.playbook.kb_mcp`) exposing one `kb_search`
  tool over the index DB (SQLite opened `mode=ro`). Wired into a project's
  `kilo.jsonc` like the other MCP servers; per-project opt-in. No coordinator
  process involvement, no writes, no model.
- **Sessions (write):** `PLAYBOOK_NOTE: file=fixes/rojo.md heading="..." text="..."`
  lines in the final report — the same parser/validator as `MEMORY_NOTE`
  (keyword parametrized), validated against the playbook layout (known dir,
  name rules, size cap, secret scan), then **committed by the coordinator by
  explicit path to the playbook repo** (`Workspace(playbook_path).commit_paths`,
  message prefix `PLAYBOOK:` + the contributing project/task in the body).
  Sessions are otherwise read-only on the playbook.

## 4. Promotion pipeline (deterministic; **default off**)

New table `playbook_candidates(key, kind, count, projects, first_ts, last_ts,
evidence, status)`. Two triggers, both counted cross-project, no model call:

1. **Repeated repaired failure.** When an `auto_heal` retry heals a stop, or a
   lead case ends `repair_done`, count key `fail:<stop class>|<signature>`
   (autoheal's signature names are already normalized). At **2**, the
   coordinator drafts a `fixes/` entry from a template filled with the
   evidence (signature, stop class, the repair that worked, projects, an
   example blocker text) and opens a **low-priority decision**
   (`kind="playbook_promotion"`, options `add / edit first / skip`,
   recommendation **add**, `blocks=[]` so nothing waits on it, no timeout —
   it sits until Victor answers).
2. **Repeated operator instruction.** Operator messages normalized (lowercase,
   whitespace collapsed, task/sprint ids and dates stripped) and hashed; key
   `op:<hash>`. At **3**, same flow with a `howto/` draft.

On **add**, the coordinator commits the draft by explicit path and marks the
candidate promoted; on **skip**, it's dismissed and only re-opens after two
further repeats (no nagging). The draft text is template-rendered in this
build; a model-polished draft (Lead) can be added later behind the same flag.

## 5. UI — Memory tab scope switch

- The coordinator publishes a playbook snapshot exactly like the memory
  snapshot (`type: "memory"`, project `@playbook`, `scope: "playbook"`,
  fingerprint-deduped) through the same `publish`.
- The Automation page's Memory tab gets a **project ↔ playbook** switch:
  browse files/entries, search, add/edit (through `playbook_add/edit`), the
  per-session slice list, and the pending promotion candidates with their
  open decisions.

## 6. Settings (in `foundry_automation/schema.py`, parity test updated)

| key | default | meaning |
|---|---|---|
| `playbook` | `true` | include playbook slices in this project's prompts (inert without a configured path) |
| `playbook_promote` | **`false`** | the promotion pipeline (Victor enables it) |

No new budget knob — the budget is shared with `memory_slice_tokens`.

## 7. Storage / migrations (all additive)

- Reused as-is: `memory_files`, `memory_entries`, `memory_fts` (under `@playbook`).
- New tables: `playbook_slices`, `playbook_notes`, `playbook_candidates`.

## 8. Tests (fake fixtures, like `test_memory.py`)

`tests/coordinator/test_playbook.py` with a **fake playbook repo** via the
existing `make_repo` helpers and two fake projects: layout init + commit;
indexing under `@playbook`; slice joining under the shared budget + per-entry
delta; signature-matched `fixes/` selection after a stop; `PLAYBOOK_NOTE`
validation + commit to the playbook repo (never the game repo); promotion
counting across both projects → decision → add commits the entry; defaults
off; chat tools through the control fixture; `test_automation_parity` extended.

## 9. Release

Version `0.136.0` in both `__init__.py`, release notes, tag `v0.136.0` on
main, cline branch brought to parity. No deploy or restart from this session
(the live stack picks it up on the next checkout per the runbook). Nothing
touches the Animal Uprising repo.

## Open questions for Victor (answered 2026-10-23)

1. **Playbook repo path/name** — approved: create `foundry-playbook` beside the
   game repos (`python -m foundry_coordinator playbook init` after starting the
   coordinator with `--playbook` / `$FOUNDRY_PLAYBOOK`).
2. **Session-side `kb_search` MCP** — include now, per-project opt-in in
   kilo.jsonc (`foundry_coordinator/playbook/kb_mcp.py`, read-only).
3. **Promotion decisions** — operator-only (`playbook_promotion` is in
   `OPERATOR_ONLY_KINDS`: recommendation shown, never timeout-applied, only the
   operator may answer).

## As built (deviations from the sections above)

- Promotion decision options are **add / skip** (no separate "edit first": the
  operator can edit the committed entry from the Memory tab or `kb_edit`
  afterwards). A skip re-proposes only after **two** further repeats.
- Counting always runs while a playbook is configured; `playbook_promote` only
  gates whether a decision opens — so enabling the pipeline later starts from
  the real history.
- The playbook's budget floor is 400 tokens (`playbook/manager.py MIN_BUDGET`);
  the failure-signature boost is +6 on a matching `fixes/` entry, with the
  signature taken from the task's recorded auto-heal state or its blocker text.
- The snapshot travels as a `memory` observation under the reserved project id
  `@playbook` (scope `playbook`); the router's ingest allow-list admits that id
  for memory observations from any memory-allowed key, and the Memory tab's
  scope switch reads `/control/api/memory?project=@playbook`.
- Helpers get the full shared budget (they carry no docs/memory slice).
- Verified end to end against the control-fixture stack: indexed entries,
  playbook scope in the Memory tab, and a UI `playbook_add` that the
  coordinator committed to the demo playbook repo
  (`PLAYBOOK: add to fixes/rojo.md > kilo_auth: Kilo 401`).
