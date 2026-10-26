# Studio gate: one command proves an AnimalUprising sprint

`foundry_coordinator.projects.animaluprising.studio_gate` (UI-T0) runs every
check a UI sprint needs against Roblox Studio and writes one JSON verdict. It
replaces the printed CALL plan that a model used to execute by hand
(`tools/gate.ps1` + `GateRunner.run`, 40–80 MCP calls per gate with pasted
output). No model copies tool output any more; the model reads one verdict.

It is a plain CLI. A Kilo session runs it in its inner loop, and the
coordinator runs it as the verify step of a fast-lane task
(docs/FAST_LANE.md). Plan: `docs/plans/AU_UI_REWORK_PLAN.md` section 6.

## What it does

`sprint` mode, in order:

1. **Host checks** (ported from `tools/gate.ps1`): HEAD, files under test
   committed, no CRLF-only churn, the FNV hash maps (`tools/hash_fnv.ps1`),
   Lune specs and the Lune negative control. Tier `C` also requires no
   `src/`/`tests/` change since the previous commit.
2. **Live hash check:** `GateRunner.verifyLive` in Edit mode confirms Studio
   runs the committed files (Rojo synced).
3. **Stories in Edit mode:** for each story in the sprint's gate file and each
   of the five sizes, switch the device simulator to the `AU_*` device, mount
   the story with `StoryHarness`, run `UiLayoutCheck` on the mounted tree,
   capture a PNG with the community `capture_screenshot`, unmount. The screen
   size and the PNG size must both equal the device size.
4. **Focus walks** on stories (Edit mode, `FocusWalk.walk`).
5. **Playtest lane** (only when the gate file has wiring, touch or panel focus
   checks, or a regression selection applies): one solo playtest for the
   wiring and touch snippets and the in-play focus walks; then the CALL plan
   built with `GateRunner.run` (Edit) runs **one fresh solo playtest per
   suite** (below), then `GateRunner.tally` over the original CALL numbers.
6. **Console check:** any console error line that was not there at the start
   fails the gate.
7. **Verdict:** writes the verdict JSON, appends one evidence block to
   `docs/progress/<ID>_EVIDENCE_RAW.log`, prints `GATE <ID> PASS|FAIL`.

The CALL plan is executed exactly as `GateRunner` prints it:

| CALL line | How the runner executes it |
|---|---|
| `vm=server` | community `eval_server_runtime` |
| `vm=client` | community `eval_client_runtime` |
| `vm=edit` | official `execute_luau`, datamodel Edit |
| `MANUAL (bridge)` in the server VM | serves the AU repo on a free `127.0.0.1` port, loads the suite through `HttpService:GetAsync` into a temporary ModuleScript, `require`s it through community `execute_luau target="server"`. Re-boots `ServiceLoader` first if `HeartbeatConn == nil`, except for `S11_5_boot_check` (never manufacture a boot). |
| `MANUAL (bridge)` in the client VM | not executable: recorded `UNKNOWN` |
| `SKIP offstudio` | recorded as SKIP, never as PASS |
| other `MANUAL` | not executed; no evidence (the tally sees it missing) |
| `DYNARG` | each `<token>` is filled from the most recent earlier output matching `token=value` or `token: value`; an unresolved token is a FAIL line for that CALL |
| `tightBefore` pairs | issued back to back by the script (milliseconds apart) |

**Per-suite fresh playtests.** Every regression suite runs in its own solo
playtest: start, boot CALL 1-2 re-run (both must PASS, else that suite's line
stops), the suite's `setup` steps, its play steps, stop. Bridge suites always
run alone (their `SL:Boot()` re-boot would hijack later suites'
RemoteFunctions). A suite whose run hit only tool errors or timeouts (no
assertion `FAIL:`) is retried once in a fresh playtest; both runs are in the
raw log and a non-required `regression.<suite>.retry` SKIP check records it.
When the CALL plan does not match `tests/RegressionIndex.lua` at HEAD, the
suites run in one shared playtest and `regression.mapping` is UNKNOWN.

`tests/RegressionIndex.lua` entry fields the runner reads (all optional):

| Field | Meaning |
|---|---|
| `isolation = "shared"` | opt out of the fresh playtest; consecutive shared suites share one (ignored for bridge suites) |
| `setup = {"server.<expr>", ...}` | steps run after the boot gate, before the suite; each is a `regression.<suite>.setup<k>` check, and a setup FAIL counts as an assertion failure |
| `runLast = true` | the suite runs after every other suite (`S46` always does: it has no teardown and quiesces the match) |
| `rerunAfter = {"S48"}` | the suite runs again, fresh, after `S48` when it first ran before it |
| `waits = {[stepIdx] = seconds}` | sleep before that step (a plain list counts from 1) |

**Fail closed everywhere.** A tool error, timeout or unparsable output is
`UNKNOWN`, and `UNKNOWN` or `SKIP` on a required check keeps the gate red.

## Setup on the workstation

Run once, then `doctor` before every sprint.

**Python.** Run the module from a Foundry-Router checkout (the live
coordinator uses `C:\Users\shock\foundry-deploy`). Standard library only.

**Both MCP servers**, connected to the same Studio at once:

| Server | Command the runner starts | Needs |
|---|---|---|
| Official Studio MCP | `cmd.exe /d /c %LOCALAPPDATA%\Roblox\mcp.bat` | Studio open with its MCP server enabled. Every tool takes `studio_id`; the runner resolves it with `list_roblox_studios`. |
| Community `robloxstudio-mcp` | `cmd.exe /d /c "C:\Program Files\nodejs\npx.cmd" -y @chrrxs/robloxstudio-mcp@3.4.2` | Node.js, the package fetched once with `npx`, and its Studio plugin enabled. |

`FOUNDRY_MCP_OFFICIAL` / `FOUNDRY_MCP_COMMUNITY` override a command with a
JSON argv list (tests, unusual installs).

**Studio:**
- exactly one Studio open on the AU place, or pass `--place NAME` (a
  substring of the Studio name; the place file may be named after a restore);
- Edit mode (no playtest running) and not minimised;
- `StarterGui.ShowDevelopmentGui = true`;
- Game Settings → Security → Allow HTTP Requests on (the bridge needs it);
- the five custom devices `AU_1920x1080`, `AU_1366x768`, `AU_1024x768`,
  `AU_844x390`, `AU_390x844` (PixelDensity 96). `doctor` creates missing ones.
- Scene Analysis beta on (File → Beta Features) for the planned leak check.
  `doctor` does not check this yet.

**AU repo (Rojo-mounted):**
- `tests/lib/StoryHarness.lua`, `tests/lib/FocusWalk.lua` and the existing
  `tests/lib/UiLayoutCheck.lua` at `ReplicatedStorage.TestLib`;
- `GateRunner` and `RegressionIndex` in `ServerScriptService`;
- one `rojo serve` on the `servePort` in `default.project.json` (default
  34872), with the Studio plugin connected;
- Lune on PATH (`rokit install` in the AU repo).

**Captures and display scaling.** The community capture returns the PNG at
exactly the simulated device size, also at 125 % Windows scaling (measured
2026-10-20), so no scaling change is needed. The verdict records the real
capture size and fails a mismatch.

## Commands

```
py -m foundry_coordinator.projects.animaluprising.studio_gate [--repo DIR] [--place NAME] [--timeout S] <cmd>
  doctor                                    preflight; prints FIX lines
  stories  --sprint ID [--story NAME ...] [--background]
  sprint   --sprint ID [--files a,b] [--selection SEL] [--tier A|B|C] [--base REV] [--background]
  regression --selection ui|full|S45,S46 [--sprint ID] [--background]
  status   --sprint ID [--wait [MINUTES]]
  stop-playtest                             stop a solo playtest a killed runner left running
```

- `--repo` is the AU checkout (default: the current directory).
- `--timeout` is seconds per MCP tool call (default 120; `doctor` caps it at 60).
- `--run-id ID` (or env `FOUNDRY_GATE_RUN_ID`) is the caller's run nonce,
  written into the verdict and `status.json` as `run_id`, so a caller can
  tell its own run from an older one at the same head.
- Sprint ids are letters, digits, `.`, `_` and `-` only.

Examples:

```
set G=py -m foundry_coordinator.projects.animaluprising.studio_gate --repo C:\Roblox\AnimalUprising
%G% doctor
%G% stories --sprint UI-02a --story Hud.normal          # seconds: the inner loop
%G% sprint --sprint UI-02a --files src/client/UI/v3/Hud.lua --background
%G% status --sprint UI-02a --wait                       # blocks up to 8 min (max 9)
%G% regression --selection ui --background              # sprint id regression-ui
```

**`doctor`** checks the official and community servers, the Studio window,
Edit mode, ShowDevelopmentGui, HTTP, the story harness, GateRunner and
RegressionIndex, the `AU_*` devices, Rojo and Lune. It prints one line per
check, one `FIX <check>: <what to do>` line per problem, then
`DOCTOR READY` or `DOCTOR NOT READY`.

**`stories`** runs the gate file's stories (all, or the `--story` names) and
their Edit-mode focus walks, then the console check. No host checks, no
playtest, no evidence block. Use it while writing code.

**`sprint`** is the full gate described above. `--files` is merged with the
gate file's `files`. The regression selection is `--selection`, else the gate
file's `selection`, else computed from the changed files (below). `--base`
(default `HEAD~1`) is the base for the comment-only `Remotes.lua` test.

**`regression`** runs only the CALL plan for a selection (one fresh playtest
per suite), plus the console check. Without `--sprint` the id is
`regression-<selection>`. Use `--selection full` unattended at the end of a
batch.

**`--background`** starts the same command detached, prints
`GATE <ID> STARTED pid=...` and the exact `status` command, and exits 0. Use
it whenever a gate may outlast a shell call's timeout (Kilo). Output goes to
`.plan/tmp/gate/<ID>/run.log`.

**`stop-playtest`** takes the runner lock and calls the community
`solo_playtest` stop: cleanup after a runner was killed mid-playtest. Exit 0
stopped, 1 stop failed, 2 BUSY (another runner holds the lock) or no MCP.

**Runner lock.** `doctor`, `stories`, `sprint`, `regression` and
`stop-playtest` take `.plan/tmp/gate/runner.lock` (pid, sprint, mode) while
they drive Studio. A second runner prints `GATE <ID> BUSY: another
studio_gate runner is driving Studio (...)` (`doctor` / `stop-playtest`:
`BUSY: ...`) and exits 2 without touching the other run's `status.json`;
`--background` checks the lock before it starts. A lock whose pid is dead is
stale and taken over.

**`status`** reads `status.json` and `latest/<mode>.json` for the running
mode (falling back to `latest.json`). Without `--wait` it
answers at once; with `--wait` it polls (every 5 s, `FOUNDRY_GATE_POLL_SECONDS`)
for up to 8 minutes by default, 9 at most, so it fits one Kilo command.
While running it prints `PHASE <phase> <progress>` lines and ends with
`GATE <ID> RUNNING ... re-run: <command>`. When done it prints the non-PASS
checks, `RESULT ...` and the summary line. A runner whose pid is gone before
it wrote `done` is reported as `GATE <ID> ERROR`.

### Affected suites (file → suite map)

Without an explicit selection, `sprint` maps the changed files onto
`tests/RegressionIndex.lua`. A suite is hit when a changed file is its
`tests/<file>.lua`, is listed in its `files`, is a mount leaf of the same
name, or (for `src/` files) its module name appears in the suite's source.
These map to the `ui` subset instead of every suite that reads them:
`src/shared/Config/Strings.lua`, `src/shared/Config/Ui.lua`, a comment-only
`src/shared/Net/Remotes.lua` edit, and any unmapped `src/client/UI/` file.
No hit means no regression (`regression.selection` SKIP, not required).

## Gate file: `docs/progress/<ID>_gate.json`

Written with the sprint's one-page plan. **Read from HEAD** (`git show
HEAD:docs/progress/<ID>_gate.json`), never the working tree: commit the
gate file before running a sprint gate. `sprint` fails `host.gate_plan` when
it is not committed at HEAD (also when it exists only in the working tree);
`stories` stops with an error. `tests/RegressionIndex.lua` is also read
from HEAD. Every key is optional.

```json
{"schema": 1,
 "stories":  [{"name": "Hud.normal", "module": "ReplicatedStorage.TestLib.SampleStory",
               "fixture": {"hp": 80}, "sizes": ["1920x1080", "390x844"]}],
 "wiring":   [{"id": "hud.snapshot", "vm": "server", "code": "return ...",
               "expect": "PASS: W1", "tightBefore": false}],
 "touch":    [{"id": "hit.sizes", "vm": "client", "code": "return ...", "expect": "PASS:"}],
 "focus":    ["HudV3", {"id": "hud", "story": "Hud.normal"}, {"id": "menu", "panel": "MenuV3"}],
 "files":    ["src/client/UI/v3/Hud.lua"],
 "selection": "ui"}
```

| Key | Meaning |
|---|---|
| `stories[]` | `name` (screen.state), `module` (dotted datamodel path to the story ModuleScript), `fixture` (any JSON, passed to the story), `sizes` (default all five) |
| `wiring[]`, `touch[]` | Luau snippets run in the playtest. `vm` is `server`, `client` (default) or `edit`. PASS when a line starts with `expect` (default `PASS:`) and no line starts with `FAIL:`. |
| `focus[]` | a panel name or `{"panel"}`: `FocusWalk.run` in the client VM during the playtest; `{"story"}`: the story mounted in Edit mode and walked. PASS needs a `PASS: FOCUS` line. |
| `files[]` | files under test, merged with `--files` |
| `selection` | regression selection; overrides the file → suite map |

## Verdict JSON (schema 1)

Written to `<AU repo>/.plan/tmp/gate/<ID>/<head12>.<mode>.json` (for
example `0123456789ab.sprint.json`), copied to `latest/<mode>.json` and
`latest.json`. A `stories` or `regression` run can never overwrite or stand
in for the sprint verdict. Read by the coordinator (docs/FAST_LANE.md) and by
training (`ingest gate`, docs/TRAINING_DATA.md), which reads only
`<head12>.<mode>.json` and legacy `<head12>.json` files (never `latest/`,
`latest.json`, `status.json`, `gate_expect*.json`, `*.raw.log` or
`*.vision.json`) and labels only `mode: "sprint"` verdicts (a legacy file
without `mode` counts as sprint).

```json
{"schema": 1, "sprint": "UI-02a", "head": "<40-hex>", "mode": "sprint", "run_id": "<nonce>",
 "started": 1792500000.0, "finished": 1792500600.0, "result": "PASS",
 "checks": [{"id": "host.clean_tree", "result": "PASS", "required": true,
             "detail": "files under test committed", "artifacts": []}],
 "captures": [{"story": "Hud.normal", "size": "1920x1080",
               "path": "docs/evidence/UI-v3/UI-02a/Hud.normal@1920x1080.png",
               "capture_size": "1920x1080", "geometry_defects": 0,
               "layout_lines": ["PASS: UI-LAYOUT ..."], "summary": "..."}],
 "regression": {"selection": "ui", "suites": {"S45": "PASS"}, "tally": "<raw tally text>"},
 "summary_line": "GATE UI-02a PASS", "error": null}
```

- `mode`: `sprint`, `stories` or `regression`; also in the file name.
- `run_id`: the `--run-id` / `FOUNDRY_GATE_RUN_ID` value, `""` when none.
- `result`: `PASS` only when there is at least one required check and every
  required check is `PASS`; `ERROR` when the runner itself failed (`error`
  holds the exception); otherwise `FAIL`.
- `checks[].result`: `PASS`, `FAIL`, `SKIP` or `UNKNOWN`. `detail` is at most
  500 characters.
- `head` is all zeros when `git rev-parse HEAD` failed.
- `regression` is `null` when no CALL plan ran.

Check ids:

| Prefix | Checks |
|---|---|
| `host.*` | `head`, `gate_plan`, `clean_tree`, `crlf`, `hash_expect`, `no_code` (tier C), `live_hash`, `playtest` |
| `lune.*` | `specs`, `negative_control` |
| `story.<name>@<WxH>` | one per story and size; `story.none` when nothing was selected |
| `wiring.<id>`, `touch.<id>`, `focus.<id>` | the gate file's checks |
| `regression.<suite>` | one per suite in the tally (a SKIP suite is not required); `regression.verdict`, `regression.plan`, `regression.selection` |
| `console.errors` | new console errors since the run started |

`leak.*` (20× open/close with scene analysis) is reserved in the spec.
**TODO:** not implemented yet.

`status.json` (while running, removed at a background start, `phase: "done"`
at the end):

```json
{"sprint": "UI-02a", "pid": 1234, "started": 1792500000.0, "phase": "stories",
 "progress": "3/10", "updated": 1792500100.0, "mode": "sprint", "run_id": "<nonce>",
 "playing": false}
```

Phases: `starting`, `host`, `stories`, `playtest`, `wiring`, `touch`,
`regression`, `tally`, `done`.

## Evidence outputs

All under the AU repo:

| Path | What | In git |
|---|---|---|
| `.plan/tmp/gate/<ID>/<head12>.<mode>.json` | the verdict (older runs: `<head12>.json`) | no (`tmp/`) |
| `.plan/tmp/gate/<ID>/<head12>.<mode>.raw.log` | every payload and raw output, appended per run | no |
| `.plan/tmp/gate/<ID>/latest/<mode>.json`, `latest.json` | copies of the latest verdict per mode / overall | no |
| `.plan/tmp/gate/<ID>/status.json` | progress | no |
| `.plan/tmp/gate/runner.lock` | held while a runner drives Studio | no |
| `.plan/tmp/gate/<ID>/run.log` | stdout of a `--background` run | no |
| `.plan/tmp/gate/<ID>/gate_expect.json`, `gate_expect_full.json` | FNV hash maps | no |
| `docs/evidence/UI-v3/<ID>/<story>@<WxH>.png` | captures | yes, committed by the session |
| `docs/progress/<ID>_EVIDENCE_RAW.log` | one `=== GATE studio_gate (sprint) ===` block per `sprint` run, one line per check, `VERDICT JSON <path>`, ending `GATE <ID> PASS\|FAIL` | yes |

**Reference renders and contact sheet** (separate commands, stdlib only):

```
py -m foundry_coordinator.projects.animaluprising.reference_render --workspace C:\Roblox\AnimalUprising [--story S ...] [--size 390x844 ...] [--browser PATH] [--out DIR]
py -m foundry_coordinator.projects.animaluprising.review_sheet --workspace C:\Roblox\AnimalUprising --sprint UI-02a [--verdict PATH] [--references DIR] [--out DIR]
```

- `reference_render` renders the approved HTML mocks listed in
  `docs/design/ui-overhaul/v3/references.json` (`{"Hud.normal":
  "prototype/hud.html#normal"}`) with headless Edge or Chrome, to
  `docs/design/ui-overhaul/v3/references/<story>@<WxH>.png`. Exit 0 all frames
  at the exact size, 1 some failed, 2 setup error (no browser, no mapping,
  unknown story).
- `review_sheet` builds `.plan/tmp/review/<ID>/index.html`: rows are stories,
  columns are sizes, each cell shows the capture next to the reference, the
  defect count, the FAIL layout lines and, when `<head12>.vision.json` sits
  next to the verdict, that cell's vision findings. Nothing reads it back.

**TODO:** neither the runner nor the coordinator builds the contact sheet
yet, and the coordinator does not write `<head12>.vision.json`; run
`review_sheet` by hand. Vision verdicts are stored in the coordinator
(`model_visual_review` events and the approval record).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | PASS (`doctor`: ready; `--background`: started) |
| 1 | FAIL |
| 2 | not ready (`doctor`), runner/tooling error (`result: ERROR`), BUSY (another runner holds `runner.lock`), or `status` with no result yet, no run, or a dead runner |

The last line is always the summary: `GATE <ID> PASS|FAIL`, `DOCTOR READY|NOT
READY`, or a `GATE <ID> STARTED|RUNNING|NONE|ERROR|BUSY ...` line.

## Known limits

- **Edit-mode inset (about 20 px).** In Edit mode the default ScreenGui
  insets differ from Play. The harness mounts stories in
  `StarterGui.__StoryHarness` with `ScreenInsets = None` and checks against
  the ScreenGui's own rect, so geometry equals the device size. Edit-vs-Play
  insets for real screens are still verified inside UI-02a.
- **Edit `require` cache.** The Edit VM caches `require` per ModuleScript
  instance, so a Rojo edit to `TestLib` would never be seen (KNOWN_ISSUES 42).
  The runner keeps a fresh clone of the whole `TestLib` folder
  (`ServerStorage.__AU_TestLib_fresh`) and rebuilds it when any source
  changes; the harness requires a fresh clone of the story module on every
  mount.
- **Play-VM require cache.** Official `execute_luau` (Server) has a separate
  require cache, so play-VM checks go through the community eval tools
  (HK-G2), as before.
- **Bridge suites in the client VM** have no HttpService path and are
  recorded `UNKNOWN`, which keeps a gate red if the suite is required.
- **No touch or gamepad injection.** VirtualInput has neither, and the runner
  uses no mouse clicks either. Checks are functional only: named
  `ButtonActions` activations, the focus-graph walk and hit-size rules.
  Physical click, touch and pad testing happens in Victor's playtests.
- **Studio lock.** When the coordinator runs the gate it holds the project's
  exclusive locks (Studio, Rojo). Every runner, manual or not, also takes
  `runner.lock`, so two runners never drive Studio at once; that lock does
  not stop a session or helper calling Studio tools directly, so do not run
  a gate while one does.
- **`doctor` does not check** the Scene Analysis beta, display scaling or the
  viewport size.
- The Roblox Assistant `playtest` subagent is advisory and never part of the
  verdict.

## Troubleshooting (doctor messages)

| Check | Message / symptom | Fix |
|---|---|---|
| `doctor.official_mcp` | cannot start, or no `execute_luau` tool | Open Roblox Studio with its MCP server enabled; `%LOCALAPPDATA%\Roblox\mcp.bat` must exist. |
| `doctor.community_mcp` | cannot start, or no `capture_screenshot` | Install Node.js, run `npx -y @chrrxs/robloxstudio-mcp@3.4.2` once, enable its Studio plugin. |
| `doctor.studio` | `need exactly one Studio ..., found N (open: ...)` | Close the extra Studio windows, or pass `--place NAME`. |
| `doctor.edit_mode` | `mode=play` | Stop the playtest. `mode=unknown` is UNKNOWN: check `get_studio_state`. |
| `doctor.datamodel` | `execute_luau (Edit) failed` | Check the Studio output window; usually a plugin or permission dialog. `doctor` writes no raw log. |
| `doctor.show_dev_gui` | `False` | Set `StarterGui.ShowDevelopmentGui = true`. |
| `doctor.http_enabled` | `False` | Game Settings → Security → Allow HTTP Requests. |
| `doctor.story_harness` | `False` | Commit `tests/lib/StoryHarness.lua` and let Rojo sync it to `ReplicatedStorage.TestLib`. |
| `doctor.gate_runner` | `GateRunner=False` or `RegressionIndex=False` | Rojo sync `ServerScriptService.GateRunner` / `RegressionIndex`. |
| `doctor.devices` | `missing AU_...` | Create the device in the Device Emulator (Name, Width, Height, PixelDensity 96). Normally `doctor` creates it. |
| `doctor.rojo` | `port 34872: ...` | Start the one `rojo serve` on that port (AU `docs/ENVIRONMENT.md`) and connect the plugin. |
| `doctor.lune` | `not on PATH` | `rokit install` in the AU repo. |

During a run:

- `story.<name>@<size>` UNKNOWN `mount output unparsable`: the harness threw
  before returning JSON; the raw log has the Studio text.
- `capture WxH != WxH`: Studio was minimised or the viewport is smaller than
  the device; restore Studio and re-run.
- `host.playtest` UNKNOWN `no player in world after start`: a modal dialog or
  a failing server boot; check the console, then `regression --selection ui`.
- `status` says `ERROR runner pid ... is not running`: read
  `.plan/tmp/gate/<ID>/run.log`; if Studio is still in Play, run
  `stop-playtest`.
- `GATE <ID> BUSY`: another runner holds `runner.lock`; wait for it
  (`status --sprint <its ID> --wait`), do not delete the lock by hand.
- `host.gate_plan` FAIL `... is not committed at HEAD`: commit
  `docs/progress/<ID>_gate.json` and re-run.

## Tests

`tests/coordinator/test_mcp_stdio.py` and `tests/coordinator/test_studio_gate.py`
(fake MCP server in `tests/coordinator/fixtures/fake_mcp_server.py`; no live
Studio), `tests/coordinator/test_reference_render.py`,
`tests/coordinator/test_review_sheet.py`.
