# Animal Uprising — UI rework and fast sprint process (plan for review)

Status: **DRAFT for Victor's review. Nothing here has been started.** The project is paused
(UI-02 held at a safe point). Written 2026-10-19.

## Summary

- **Diagnosis:** building UI isn't the slow part; proving it is. UI-02 spent about 6 of 37
  hours coding. The rest was blocked time, recovery loops, and a final gate where the model
  hand-carries dozens of Studio calls and pastes their output. 57–63 % of UI-01/UI-02 commits
  were documentation and evidence only.
- **New process:** one command (`studio_gate`) proves a sprint and writes a JSON verdict. Screens
  render in Edit mode from fixture "stories" and are captured at 5 sizes in one call, then checked
  by the existing layout checker. Menus are proven by button calls and a controller focus walk,
  with no click emulation. One short scripted playtest covers wiring. **Frontier vision approves
  the captures** against the approved references and a rubric. You refine later from your own
  playtests. Studio is used to full capacity: the Roblox skills, the Assistant `explore` and
  `playtest` subagents, scene analysis and profiling, and our own AU skills shared by Kilo and
  Assistant. Each sprint is one long **Kilo
  Code** session run by the coordinator's new fast-lane mode. Frontier helpers draft components and
  stories ahead, and the Kilo primary verifies, applies and finishes them. A one-page rules card
  covers fast-lane work.
- **New UI (v3):** keeps the approved look, art and game rules, and borrows proven patterns from
  similar games. A Kingdom Rush-style build ring at each plot handles towers, walls and gates on
  every input. Phones get a Wild Rift / Brawl Stars right-thumb ability arc with taps only. Menus
  use a Roblox-standard left rail, a portrait tab bar and one Menu button. Controllers navigate
  with focus, shoulder-button tabs and no cursor. Eight templates cover every screen. Built with
  React-luau.
- **Plan:** 8 sprints (UI-T0 tooling, coordinator fast lane, skills and training capture → UI-D1
  vision-approved mock → UI-02a … UI-06a → UI-V1). About 36–40 hours of machine time with the
  decided setup: local Strata/Qwen implements the screen sprints with frontier help, and the existing
  Claude/GPT persona routes fail over automatically. It runs unattended in the coordinator after UI-T0. About 10 minutes of
  yours, then your own playtesting.
- **Start:** UI-02 and the old UI-03…UI-GT1 queue are cancelled as superseded at the start of
  UI-T0. The new sprints are queued only when you say "begin".
- **Training data:** episodes are labelled with gate verdicts, vision verdicts, fixes and later
  your critiques. Your policy only trains on locally authored steps (provider terms), so D11
  decides how much of the batch local Qwen writes.
  UI-02 is closed as superseded, with its useful pieces carried forward, and all 38 known UI issues
  are assigned to a sprint.
- **Decisions:** section 11 (D11–D13 decided 2026-10-20).

---

## 1. Why the current process is slow (measured)

UI-02 has been running since 2026-10-18 10:05, about 37 hours so far:

| Where the time went | Hours | Count |
|---|---:|---:|
| BLOCKED (waiting on a decision, a recovery case or an operator) | 16.2 | 29 times |
| IMPLEMENTING (a coding session open) | 15.0 | 31 sessions |
| PAUSED / AWAITING_HUMAN / FAILED | 5.1 | 9 |
| Verifying, re-queuing, changes-required | 0.6 | 62 transitions |

- **Real coding time is small.** The 24 Sonnet sessions averaged 8 minutes and the 7 local-Qwen
  sessions 23 minutes, so roughly 6 hours of actual work in 37. The code was already pre-written
  headless before the sprint started (`UI-02_PRECODE/UI-02.diff`).
- **Verification is the bottleneck.** The final gate makes the model act as a human
  courier: `gate.ps1` prints a numbered list of MCP calls (`GateRunner.run` emits one `CALL n` per
  suite step across 9 suites), and the model runs them one by one, copies raw output between tools,
  honours timing windows by hand ("issue CALL 37 back-to-back with CALL 36 in ONE tool response"),
  pastes everything into `GateRunner.tally`, and then into `_studio_lines.txt`. One wrong paste or a
  slow tool response invalidates the run (S46 "STUCK 3", timing-invalid re-runs).
- **Bookkeeping dominates the commits.** UI-01 took 134 commits and UI-02 87, for about
  15,000 lines of hand-built `Instance.new` UI. Most commit messages are evidence logs, plan
  re-ticks, re-pinned counts and DRIFT notes rather than code.
- **The coordinator added its own loops.** UI-02 used 28 Lead calls (10 rejected reports, 4 guard
  trips), 19 recovery cases and 35 decisions. Many were infrastructure bugs I have since fixed, but
  the structure (many short sessions, each re-reading the rules and the plan) multiplies them.
- **Rules load.** Every session reads `AGENT_RULES.md` (482 lines, 34 numbered sections plus
  appendices), the UI track rules, the design package and the plan before writing a line.

**Conclusion:** building UI is not what is slow. Proving it is. The plan below automates the
proof, renders UI without playtests, batches visual approval, and runs each sprint as one long
session.

---

## 2. Principles of the new process

1. **One command proves a sprint.** A script runs every check (Lune, Studio suites, captures,
   layout checks, affected regressions) and writes a JSON verdict. No model ever copies tool output.
2. **Render UI without playing.** Every screen has "stories": the screen mounted in Edit mode with
   fixture data for each state (loading, empty, normal, pending, error, downed, phone). Visual
   checks run on stories, which takes seconds and needs no playtest, player or server.
3. **Playtests only for wiring.** One short scripted playtest per sprint proves the screen is fed
   by the real remotes and that its actions reach the server. The full regression runs unattended
   at the end of a batch, not inside every sprint.
4. **Frontier vision approves, not Victor.** Every sprint's captures (5 sizes × each state) go to
   the coordinator's existing revision-bound vision reviewer (`[approval] visual_model`), with the
   approved v3 references and a written rubric. It approves or returns concrete fixes into the
   same session. The bar is "close to production quality and usable"; Victor refines later from
   real playtests, and his critiques feed the next pass.
4b. **Functional, not physical, input tests.** Menus are proven by calling their buttons through
   `ButtonActions` (named activations, the method UI-01/UI-02 already used) and checking the
   result, plus a scripted controller focus-graph walk. No mouse-click or touch emulation; Victor
   does the hands-on click testing during game testing.
5. **One long Kilo session per sprint, frontier helpers drafting ahead.** A sprint is sized for
   one 2–4 hour Kilo Code session (frontier primary) with Studio connected, run by the
   coordinator. If the gate fails, the same session fixes it. No new session per
   failure, no recovery case for a red check.
6. **Short rules.** A one-page "fast lane" rule card replaces the 482-line rules file for UI work.
   Server authority, scope lock and safety stay; the evidence ceremony goes.
7. **Templates over screens.** Most screens become a few reusable templates fed with data, so a
   new shop or tab is configuration plus a story, not a new 900-line file.

---

## 3. Tools: what to adopt, what to skip

Researched 2026-10-19 (sources at the end).

| Tool | What it gives us | Verdict |
|---|---|---|
| **Official Studio MCP server** (built into Studio, `mcp.bat`) | `execute_luau` (Edit/Server/Client), `start_stop_play`, `screen_capture`, `user_mouse_input`/`user_keyboard_input` (VirtualInput), `character_navigation`, `get_console_output`, `subagent` (playtest/explore), asset generation. Verified working from a plain Python script. | **Adopt** as the runner's main channel (its input tools are not used; see VirtualInput). |
| **Community `robloxstudio-mcp` 3.2.1** (already installed) | `eval_server_runtime`/`eval_client_runtime` (play-VM require cache), `solo_playtest`/`multiplayer_playtest`, `set_device_simulator`, **`capture_device_matrix` (up to 6 sizes in one call)**, profilers, scene analysis. | **Adopt** for play-VM evals and multi-size captures. |
| **Roblox Assistant** (Planning Mode, playtest and explore subagents, Skills) | Verified 2026-10-19 through the MCP server: Studio serves 7 Roblox skills through its `skill` tool (`rbx-device-simulator-lua`, `rbx-unit-test`, `rbx-debug`, `rbx-perf-profiling`, `rbx-scene-analysis`, `rbx-docs-search`, `rbx-create-skill`) and two Assistant subagents through `subagent` (`explore`, `playtest`). Creator-authored Skills are served the same way. Roblox staff warn the playtest agent's "Pass" can miss issues. | **Adopt fully, through MCP** (section 13): Kilo loads the Roblox skills; we author AU skills (v3 UI, fast lane) that both Kilo and Assistant use; the `explore` subagent answers codebase / game-state questions; the `playtest` subagent runs an advisory smoke pass per screen. Assistant is also your tool for quick tweaks during playtesting. Never the gate by itself. |
| **Community agent skills** (`Roblox/roblox-studio-agent-plugin`, `EL4CTEO/roblox-skills` (23 skills), `MSayib/roblox-dev-skill`, `m4bwav/roblox-agent`, `zilibobi/roblox-skills` (docs lookup)) | Luau, engine API, security and Studio-MCP practice written for coding agents in the open Agent Skills format. | **Review, then adopt the useful parts** into `.kilo/skills` (UI-T0): read every file before installing; it's third-party text. |
| **StudioDeviceSimulatorService** (May 2026) | Scriptable device presets, resolution, DPI, orientation. | **Adopt** through the community matrix tool or a plugin call; to be confirmed in the tooling spike. |
| **StudioTestService** (May 2026) | Programmatic multiplayer tests (up to 8 clients), test args, end from server. | **Adopt** for late-join / 2-client wiring checks (replaces manual 2-client runs). |
| **VirtualInput** | Simulated mouse/keyboard/pointer processed like hardware (no touch, no gamepad). | **Not used.** Functional tests call buttons through `ButtonActions` instead; click testing happens in your playtests. |
| **SceneAnalysisService / profilers** (via `rbx-scene-analysis`, `rbx-perf-profiling`, community `capture_script_profiler`) | Leaked unparented instances, memory, script time. | **Adopt** in the runner: the 20× open/close leak check and the HUD update budget (S45's ≤ 250 µs) use them. |
| **Open Cloud Luau Execution** | Headless server-side Luau against a published place version, 5 min, 10 concurrent. | **Later**: nightly server-only regressions without Studio (needs an API key and a test place). Not needed for the UI batch. |
| **Lune** (already used) | Headless pure-module tests in seconds. | **Keep and expand**: every screen's view-model is a pure module tested here. |
| **React-luau + a storybook (UI Labs / Flipbook)** | Declarative screens, components previewed in Edit mode with fixture data. Roblox's own app UI is built on react-luau. | **Recommended** for UI v3 (decision D2, section 5). |
| **Figma importers** (UILint, Framewisp, RoImport) and **AI UI generators** (VizzBees, Bloxsmith) | Turn a Figma frame or a reference screenshot into a ScreenGui. | **Skip for now.** Our mockups are HTML, not Figma, and generated hierarchies would need rewriting into components anyway. Revisit if we move design into Figma. |
| **RbxSync** | Two-way Studio↔filesystem sync with its own MCP and `run_test`. | **Skip.** Rojo already works; its test tools need `loadstring` in plugins. |
| **Studio CLI `--task RunScript`** | Run a script in a place and quit. | **Spike only**; useful for headless Edit-mode story captures if the MCP path is too slow. |
| **AI asset tools** (Studio `generate_mesh`/`generate_procedural_model`, Meshy, Tripo, Hunyuan3D + Blender cleanup) | 3D props and characters. | **Separate track** (asset sprints); not part of this UI batch. |


---

## 4. UI v3 design (reference-driven, phone and controller first-class)

Full research: `UI_REFERENCE_TEARDOWN.md` (shared notes folder), covering Tower Defense
Simulator, Anime Vanguards / Toilet Tower Defense, Bloons TD 6, Kingdom Rush, Dungeon Defenders
(+ Awakened), Orcs Must Die 3, Brawl Stars, Wild Rift and Roblox hit-game conventions.

### 4.1 What we keep from the approved 2026-10-09 package

- **Visual language and tokens** (DESIGN_SYSTEM §1): charcoal panels, enamel blue, orange primary
  action with dark text, brass accents, rarity stripes, paper slips for tutorials, contrast ≥ 4.5.
- **Approved art**: the 189 approved `ui192` assets, `Config/AssetIds`, logo, nameplate 9-slices,
  result banners.
- **Game rules on screen**: server authority (no optimistic spending), plain labels with flavour
  subtitles, Squad Candy vs your Gold always distinct, no invented mechanics, the deferred systems
  stay hidden (SCREEN_SPECIFICATIONS §H), the component contracts (item tile, detail, currency
  action, confirmation, empty, error, loading, success).
- **Compact HUD intent**: small persistent footprint, all information one action away (Intel).

### 4.2 What changes, and which reference it comes from

| # | Change | From | Why |
|---|---|---|---|
| 1 | **Build ring at the plot** replaces "Tower Shed catalog → ghost placement → inspector". Tap a plot → ring of tower options with Candy cost (greyed with reason if not possible) → tap = ghost + range preview → tap again = build. A built tower opens the same ring, matching the server as it is (verified at HEAD 2a2534b): Upgrade at the top, the three mod categories (Element / Targeting / Utility, spec D05) on the sides, Sell at the bottom (tap-again confirm), and "i" for the inspect card. There is no branch remote, and target mode is fixed per tower (`Config/Towers.lua:13-74`), so the inspect card shows it read-only. | Kingdom Rush, BTD6 inspect card, TDS | Plots are fixed, so free placement is wasted work. One component (`RadialRing`) serves mouse, touch and controller, and replaces most of UI-05's hardest flows (no click-through spending, no drag). |
| 2 | **Same ring for walls, gates and the castle** (repair, reinforce, oil, klaxon with confirm). | Kingdom Rush | One interaction for every placed-object verb. |
| 3 | **Phone combat: right-thumb arc** of ability buttons anchored at the bottom-right. Verified at HEAD 2a2534b: there is **no basic-attack action** in `src/`, and the ability contract has **2 fixed slots** (`AbilityInputController.lua:47`; UI-02 D1 "show only the real ability buttons" still applies). So the arc shows the real slots today: slot 1 is the anchor (largest, 76–84 pt), slot 2 is on the arc at 64 pt. It grows to 4 + capstone when the contract does, with no new layout work. Net appears when in range; the client can't see enemy HP, so the server's reason ("Weaken it more first") is shown if the throw is refused. Taps only; drag-to-aim optional. | Wild Rift, Brawl Stars | Proven two-thumb ergonomics. The current dock (48 px buttons in a row) is the hardest thing to hit on a phone. |
| 4 | **Menus: left icon rail** (PC and landscape) with badges, a **bottom tab bar** in portrait, **one Menu button** in combat. Every screen opens in the **same modal frame**, max 2 levels deep, one back stack (Esc / B / Android back). | Blox Fruits, Pet Simulator 99, Adopt Me, Brawl Stars | Roblox players already know it, and one frame + one router covers every screen. |
| 5 | **Top-centre status pill**: Wave 4/10 · phase · timer · Gate HP. | TDS, Dungeon Defenders | One glance for match state. |
| 6 | **Tap the minimap → full tactical map** (downtime-only expansion per current policy). | Dungeon Defenders Awakened, Wild Rift | Already designed; now one gesture on every device. |
| 7 | **Two-tier notices**: small stacked toasts (max 2) and rare full-width banners (boss, wave cleared, capstone ready); critical alerts suppress celebrations. | Brawl Stars, Roblox convention | Matches the approved alert hierarchy with a simpler implementation. |
| 8 | **Templates instead of screens** (4.4). | All references | Most screens become data + a story. |

Patterns from the teardown **not adopted** because they would invent mechanics the specs forbid:
early "call wave" vote and skip (KR/TDS), pre-match gear loadout (OMD3; gear is bought in-match
with Candy), auto-upgrade toggle. They are listed as future options in section 11.

### 4.3 HUD layouts (logical points, inside `CoreUISafeInsets`)

**Desktop 1920×1080 (mouse + keyboard)**

```
[Roblox topbar]      [ Wave 4/10 · Combat 0:42 · Gate ███████ ]        [🍬 Squad 1,240][🪙 You 380]
[Rail]                                                                   [ Minimap 200 ]
 Hero                                                                    [ Squad (8) ▾ ]
 Bag
 Build                       (world — build ring opens at the plot)
 Map
 Menu        [toasts, left-mid, max 2]
[♥ HP / resource 320×28 + level ring]      [A1][A2]  (up to 4 + CAP)    [Net][Intel]
                                            64-72 pt slots, current bindings
```

**Landscape phone 844×390 (touch)**

```
[topbar] [W4/10 · 0:42 · Gate ███]                   [🍬1,240] [Map 104]
[≡]  (rail collapses to Menu + Bag in combat)
                                                  (future: A3, A4, CAP on the arc)
   (Roblox thumbstick zone — kept clear)              [A2 64]
                                                  [Net 56*]  [A1 80]
[♥ HP 200×16]                                            [Jump 60]
```
\* Net shows only in range. Jump stays available (decision D5) but sits in the arc so no
button covers Roblox's core jump button. Today's contract has 2 ability slots and no basic
attack; the arc reserves positions for 4 + capstone.

**Portrait phone 390×844 (touch)**

```
[topbar]             [🍬][🪙]
[W4/10 · 0:42 · Gate ███]
[Map 88]          [Squad ▾]

          (world)
     build ring 170 Ø

[toasts, max 2]
[♥ HP full width]
        [A1 72][A2 64]      (row grows to 4 + CAP when the contract does)
(thumbstick)        [Net*][Jump]
[Bag][Build][Map][Squad][≡]   tab bar, hidden in combat
```
Nothing interactive in the top 25 % except map, currency and Menu.

**Controller (Xbox naming; PlayStation glyphs auto-swap)**

| Input | Combat | Build ring open | Menus |
|---|---|---|---|
| Left stick | Move | Point at a ring slot (fixed positions) | Move focus |
| Right stick | Camera | — | Scroll |
| RT / LT | Ability 1 / Ability 2 (today's 2 slots; existing `AbilityInputController` pad bindings take precedence where they exist) | — | Sub-tabs |
| LB / RB | Abilities 3 / 4 when the contract grows | Previous / next ring page | Switch tabs |
| LB+RB | Capstone when it exists (deliberate) | — | — |
| A | Context: build at the highlighted nearest plot, revive, interact | Confirm (1st press preview, 2nd build) | Select |
| B | Cancel | Close ring | Back (always) |
| X | Net | Sell (hold 0.6 s with fill ring) | Secondary action |
| Y | Intel drawer | Inspect card | Details |
| View | Tactical map | — | — |
| Menu | In-match menu | — | Close all |

No cursor is ever needed: the nearest plot in the camera's forward cone is highlighted with an
"Ⓐ Build" prompt; every menu uses Roblox's built-in gamepad selection (`SelectionGroup`,
`GuiService:Select`) with explicit neighbour links, a 3 pt focus ring, focus memory, and a
button-prompt footer. Console scale ×1.15 and minimum text 20 pt. Holds only for destructive or
costly actions, with a "double-press instead of hold" setting.

### 4.4 Templates and widgets

| Template | Covers |
|---|---|
| **T1 ModalFrame** — title, tabs (LB/RB), content, footer actions, close/back | every screen |
| **T2 CatalogGrid + DetailPane** — filters, rarity cards, detail right (wide) or bottom sheet (portrait), one primary action | Inventory, Marketplace, Roving Randy, Bazaar, Store, Critter roster, Wardrobe |
| **T3 Showcase** — big preview, stat bars, tabs, primary action | Hero select, Profile, Critter detail |
| **T4 TreeView** — columns × tiers, node states with reasons, point counter, capstone confirm | Skills (phone: branch tabs) |
| **T5 ListRows** — icon, title, progress, reward, action | Civic Duties, Squad, Settings, Help |
| **T6 RewardTrack** — claimed / today / locked strip | Lunchbox Locker (daily) |
| **T7 ResultsSummary** — banner, Kept / Reset sections, per-player rows | Victory / defeat |
| **T8 ConfirmDialog** — object, cost/refund, consequence, Cancel default, optional hold | buy, sell, capstone, leave, Klaxon |

HUD widgets: `StatusPill`, `CurrencyChip`, `VitalsBar`, `AbilityButton` (cooldown sweep + numerals,
lock reasons), `AbilityArc` / `AbilityRow` (per device), `Minimap` host (existing renderer),
`MapOverlay`, `RadialRing`, `InspectCard`, `Toast` (2 tiers), `AlertStrip` (downed > castle > boss
> routine), `TutorialSlip`, `DownedStrip`, `Rail` / `TabBar`, `PromptFooter`.

### 4.5 One action model for all inputs

Every verb (Attack, Ability 1–4, Capstone, Build, Upgrade, Sell, Inspect, Target mode, Net, Map,
Menu, Back) is defined once in an action registry and bound per input (ContextActionService for
keys and pad, our touch buttons on phone). The same components serve every input; only the
prompt layer (key labels, pad glyphs, none on touch) and hit sizes change, switching live on
`UserInputService.LastInputType`. **A screen is done only when its functional checks pass for
every input mode**: button activations by name, the controller focus walk, and touch hit sizes at
both phone orientations (section 6.6). It needs no drag and no hover-only information.

Touch rules: hit areas 1.3× the drawn icon, nothing tappable under 44 pt (abilities 64, the
anchor ability 76–84), 12 pt between destructive and primary buttons, tap-again confirm on touch (no holds,
they fight camera drag), text ≥ 14 pt body and ≥ 18 pt for HUD numbers.

### 4.6 Screen map (old spec → template → sprint)

| Screen (spec id) | v3 template / widget | Sprint |
|---|---|---|
| Loading / arrival (A01) | T1 + StatusPill | UI-06a |
| Hero select (A02) | T3 (grid on phone) | UI-02a |
| Lobby / briefing (A03) | T3 + T5 | UI-02a |
| Combat / downtime HUD (B01) | HUD widgets | UI-02a |
| Overlays: boss, buffs, prompts (B02) | AlertStrip, Toast, InspectCard | UI-02a |
| Minimap / full map (B03) | Minimap host + MapOverlay | UI-02a |
| Tutorial (B04) | TutorialSlip | UI-02a |
| Downed / respawn / spectate (B05) | DownedStrip + T8 | UI-02a |
| Inventory (C01), loot toast (C02) | T2, Toast | UI-03a |
| Skills (C03) | T4 | UI-03a |
| Profile / wardrobe (C04) | T3 + T2 | UI-03a |
| Marketplace (D01), Randy / Bazaar (D02/D03) | T2 + T8 | UI-04a |
| Tower Shed + placement + inspector (D04/D05) | RadialRing + InspectCard (+ T2 info list) | UI-05a |
| Walls & Gates (D06) | RadialRing + InspectCard | UI-05a |
| Critter Corrections (D07) | T2 + T3 | UI-05a |
| Store (E01) | T2 + T8 | UI-04a (screen; products from S52 later) |
| Duties / Lunchbox (E02/E03) | T5 / T6 | later (GF-RT sprints) — templates ready |
| Squad (E04), Settings / Help (E05) | T5 | UI-06a (shell), GF-UX1 fills settings |
| Practice (F01/F02) | T5 + T1 | later (GF-RT7) — templates ready |
| Results / photo (F03) | T7 | UI-06a |
| AFK / update / reconnect (G01/G02) | T1 blocking overlay | UI-06a |


---

## 5. Framework: React-luau, stories, and how v3 sits next to the old UI

Research: `UI_TOOLING_FEASIBILITY.md` (shared notes folder).

- **React-luau** from Roblox's official Wally packages `roblox/react` and `roblox/react-roblox`
  (17.3.13). Roblox's own app and in-experience UI are built on it; models know React well, so
  AI-written screens have fewer API mistakes than Fusion 0.3 (whose new scope API is often mixed
  up with 0.2) or Vide. Per-frame HUD values (HP, cooldown sweeps, timers) use Bindings, not state.
- **Stories** in the UI Labs table format (`*.story.luau`: `react`, `reactRoblox`, `controls`,
  `story`) with fixtures in `src/client/UI/v3/stories/fixtures`. The UI Labs plugin is for your own
  preview in Studio; the runner mounts stories with our own ~20-line harness (`createLegacyRoot`
  for synchronous, deterministic mounts) into a dev-only `StarterGui.__StoryHarness` ScreenGui.
- **Pure view-models** (the HudModel/HudGeometry pattern UI-02 already uses) turn server snapshots
  into display data and are tested in Lune in seconds.
- **Services by context provider**: screens never `require` game services directly; adapters
  provide data and actions. That is what lets a story render with fixtures and no running game.
- **Coexistence**: v3 lives in `src/client/UI/v3/`. UiFramework keeps owning layers, the single
  UIScale, safe areas and input capture; React roots portal into its layers. Each sprint deletes the
  old screen once its v3 replacement passes the gate (same rule as today: keep the old path working
  until the new one is green).
- **Fallback (decision D2):** if you'd rather not add a framework, the same plan works with the
  current imperative Kit plus the story harness; screens are simply longer to write.

**Tooling check results (UI-T0, run 2026-10-20 on this PC):**
- A ScreenGui under StarterGui renders in the Edit viewport and in captures, with exact geometry.
  `AbsoluteSize` equals the device size (1920×1080 … 390×844), and anchored and scaled children
  land at the expected pixels.
- `execute_luau` (official, Edit) drives `StudioDeviceSimulatorService` (CreateDeviceAsync,
  SetDeviceAsync, SetScalingModeAsync ActualResolution) without a plugin. The custom devices
  `AU_1920x1080`, `AU_1366x768`, `AU_1024x768`, `AU_844x390` and `AU_390x844` now exist in
  Studio.
- Captures: community `capture_screenshot` returns PNG at exactly the device size (1366×768,
  390×844) **even at 125 % Windows scaling**. Official `screen_capture` (needs a `capture_id`)
  returns JPEG of the same view. The runner uses the community PNG.
- Timing: device switch + geometry about 1.6 s and capture about 0.5 s, so about 11 s per story
  at 5 sizes.
- Both MCP servers ran against one Studio at the same time without conflict.
- Still open, verified inside UI-02a once React is installed: React mount determinism, hot reload
  after a Rojo sync, and Edit-vs-Play insets.

**Original tooling checks** (sprint UI-T0, first hour): StarterGui shows in the
Edit viewport and in captures; `execute_luau` can drive `StudioDeviceSimulatorService`; capture size
vs Windows display scaling (the capture PC must run at 100 %, and 1920×1080 needs a monitor bigger
than 1080p or an undocked viewport); repeated captures are identical; React mounts are
deterministic; a remount picks up Rojo edits; Edit vs Play insets; both MCP servers on one Studio;
timing per story (target < 10 s). Input is verified functionally (section 6.6): buttons through
`ButtonActions`, controller through a scripted focus-graph walk (`GuiService.SelectedObject` +
`NextSelection*`), touch through hit-size checks. Physical click, touch and pad testing is yours,
during game testing.


---

## 6. The verification system ("studio gate")

### 6.1 Components

| Piece | What it does | Where |
|---|---|---|
| `mcp_stdio` client | Starts an MCP server over stdio and calls its tools (stdlib only). Verified 2026-10-19 against the official server (28 tools) and `@chrrxs/robloxstudio-mcp@3.2.1` (49 tools). | Foundry-Router `foundry_coordinator/mcp_stdio.py` |
| `studio_gate` runner | One command per sprint: host checks, Lune, story captures and layout checks, wiring playtest, affected regression, tally, verdict JSON, contact sheet. | `foundry_coordinator/projects/animaluprising/studio_gate.py` (run as `py -m …`, like `kilo_checkpoint`) |
| Story harness | Luau module that mounts a named story (screen + fixture state) into a capture ScreenGui in Edit mode and unmounts it. | AU `src/client/UI/v3/stories/` + `tests/StoryHarness.lua` (dev-only) |
| Layout checker | The existing GF-UI1 checker (text fits, bounds, overlap, ≥ 48 px targets, contrast ≥ 4.5, scroll coverage), run on each mounted story at each size. | AU `tests/` (existing, reused) |
| Reference renderer | Renders the approved HTML mockup of the same screen at the same sizes (headless browser) for side-by-side comparison. | Foundry-Router tool, uses the existing `prototype/*.html` |
| Contact sheet | One HTML page per sprint: rows = screen × state, columns = sizes, reference next to capture, checker findings and the vision verdict inline. Victor can open it anytime; he doesn't have to. | Generated into `.plan/tmp/review/<sprint>/` (git-ignored) |
| **Vision approval** | The coordinator's existing revision-bound reviewer (`visual_approval.py`, `[approval] visual_model = true`) on the frontier vision persona: every required frame, the v3 reference renders and the rubric (6.5). APPROVED records the approval; CHANGES sends numbered fixes into the same session. | coordinator, after the runner's mechanical checks pass |
| Verdict JSON | `{sprint, head, checks:[{id, result, detail, artifacts}], captures:[…], regression:{…}}`, written to `.plan/tmp/gate/<sprint>/<head>.json` and summarised into the evidence log in one append. | AU `.plan/tmp/` + one evidence line |

### 6.2 What the runner does, in order

1. **Host checks** (unchanged from `gate.ps1`): HEAD, files under test committed, CRLF, hash map,
   Lune and its negative control.
2. **Stories in Edit mode:** for each story of the sprint's screens, mount it, and for each size
   use the device simulator (one `capture_device_matrix` call covers up to 6 sizes) to capture.
   Then run the layout checker on the mounted tree at that size and unmount. Typical cost: a
   few seconds per story.
3. **Wiring playtest:** start one solo playtest. Run the sprint's wiring checks (server pushes a
   snapshot, the screen shows it; the screen sends an action, the server receives it). Each check
   is a `Server`/`Client` eval issued by the script, so timing windows are met mechanically. Stop.
4. **Affected regression:** pick suites from a file→suite map (the RegressionIndex entries whose
   files or mounts intersect the sprint's changed files). Widely shared files (`Config/Strings.lua`,
   `Config/Ui.lua`, comment-only `Remotes.lua` edits) map to the `ui` subset, not to every suite
   that reads them, so a string edit doesn't trigger the full regression. Execute `GateRunner.run(selection)`'s CALL
   plan exactly as today, but by the script: `vm=server` → community `eval_server_runtime`,
   `vm=client` → `eval_client_runtime`, `vm=edit` → official `execute_luau` Edit. Bridge suites
   use the documented HTTP-bridge + temporary ModuleScript pattern (`docs/ENVIRONMENT.md`
   "Running tests in the Server VM"), which the script performs itself: it serves the repo on
   `127.0.0.1` and runs the loader snippet. DYNARG values are read from earlier outputs by the
   token pattern the suite prints.
5. **Tally and verdict:** call `GateRunner.tally` with the captured outputs, write the verdict JSON,
   append one summary block to `docs/progress/<ID>_EVIDENCE_RAW.log`, and print
   `GATE <ID> PASS|FAIL`.
6. **Review sheet and vision approval:** build the contact sheet; the coordinator then runs the
   vision approval on the committed frames (6.5).

The full `ui` (and later `full`) regression runs with the same command and `--regression full`,
unattended, at the end of a batch or overnight.

### 6.3 What it replaces

| Today | New |
|---|---|
| Model executes 40–80 printed MCP calls and pastes outputs | Script executes them; model reads one verdict |
| Timing windows honoured by hand | Script issues paired calls back-to-back (milliseconds apart) |
| Raw lines pasted into `_studio_lines.txt`, then `-Finish` | Runner writes outputs and the summary itself |
| Per-sprint visual loop with Victor | Frontier vision approval per sprint against the approved references; Victor refines later from playtests |
| Synthetic mouse clicks (pixel offsets, mouse-lock workarounds) | Named button activations through `ButtonActions`, focus-graph walk for controller |
| Five-size checks by playing the game and resizing | Story captures in Edit mode via the device simulator |
| Full suite list every sprint | Affected suites per sprint; full regression per batch |
| Coordinator obligations parse logs | Obligations read the verdict JSON |

### 6.4 Known constraints to keep (from `docs/ENVIRONMENT.md` and KNOWN_ISSUES)

- The official `execute_luau` (Server) eval thread has a **separate require cache**: boot checks
  must run through the community `eval_server_runtime` / `eval_client_runtime` (HK-G2). The runner
  keeps today's VM→tool mapping, so this is unchanged.
- `loadstring`/`readfile` are unavailable in the Server play VM; the bridge pattern above is the
  supported loader.
- No synthetic mouse clicks at all, which removes the UI-01 pixel-offset, inset and mouse-lock
  problems (KNOWN_ISSUES 14, 15, 84) by design.
- Windows display scaling can enlarge emulator screenshots; captures use "Actual Resolution" and
  the runner records the real capture size in the JSON.

### 6.5 Vision approval (replaces Victor's per-sprint look)

- **Who:** the frontier vision persona through the existing `visual_approval.py` path. That path
  is revision- and frame-bound, checks the serving model, and treats an unavailable route,
  missing frame or uncertain answer as "not approved", never as a pass.
- **What it sees:** every required capture (each story state × 5 sizes), the matching v3
  reference render from UI-D1, the checker findings, and the rubric below. The design intent
  (sections 4.1–4.5) is in its prompt, so it knows what we need without Victor.
- **Rubric (all must hold):**
  1. Layout matches the reference zones per device, with no missing or extra persistent elements.
  2. Text is readable and fits, with nothing clipped, overlapped or off-screen (cross-checked
     against the checker).
  3. Tokens are applied consistently (colours, type scale, spacing, corners); rarity and state are
     legible without relying on colour alone.
  4. Phone usability: targets look large enough and sit away from the thumbstick and native
     controls, with nothing tappable in the portrait top 25 % except map, currency and Menu.
  5. Controller: the focus ring is visible on the focused element, the prompt footer is present,
     and tabs show LB/RB.
  6. Every required state renders (loading, empty, normal, pending, error/rejected, disabled with
     reason, downed where relevant).
  7. Overall it reads as close to production quality: no placeholder look, no debug text, nothing
     broken or embarrassing.
- **Outcome:** APPROVED records the model approval at that revision (the sprint can close).
  CHANGES returns numbered, located fixes into the same Kilo session. After two CHANGES rounds on
  the same item, the coordinator accepts with the open notes recorded as follow-ups instead of
  looping. Every verdict is stored, so Victor's later playtest critiques can be compared with it.
- **UI-D1 (the v3 mock)** is approved the same way against the design brief and reference
  teardown. Victor can still look, but isn't required to.

### 6.6 Functional tests by button calls (no mouse clicks)

- **Buttons:** every interactive element registers a stable name with `ButtonActions` (the
  module and Lune spec already exist; UI-01/UI-02 used named `ButtonActions.Click()` calls). A
  wiring check activates a button by name in the client VM and asserts the result: a screen
  opened, an intent sent, a pending state shown, a server reply rendered or a reason string shown.
- **Menus:** for each screen, open → each tab → each primary action → back, through the router,
  asserting focus returns to the opener and nothing leaks (20× reopen with scene analysis).
- **Controller:** a scripted walk of the focus graph: from the first focused element, follow
  `NextSelection*` and the SelectionGroup until every interactive element has been reached; fail
  on unreachable elements, dead ends or focus escaping the modal.
- **Touch:** checker rule that every interactive element on the phone sizes has a hit area of at
  least 44 pt (abilities 64, the anchor ability 76–84, Net 56) and the 12 pt gap between destructive and primary actions.
- **Server reality:** the wiring playtest checks a handful of real round trips per screen
  (purchase rejected with a reason, equip accepted and reflected, tower built). The full
  click-through is Victor's playtest.

---

## 7. The new sprint format ("fast lane")

### 7.1 Plan file (one page)

```
# <ID> — <title>
Goal: one paragraph.
Screens / features: list.
Files this sprint may change: list (globs allowed under src/client/UI/v3/**).
Contracts used: remote names + shapes (verified in source at STEP 0).
Stories: <screen>.<state> list with fixture names.
Wiring checks: list (one line each).
Affected suites: from the file→suite map (runner prints them).
Done when: `py -m …studio_gate --sprint <ID>` prints GATE <ID> PASS, and the contact sheet is produced.
Carry-over fixes included: list.
Decisions needed: none (or the exact question).
```

No pre-code diffs (they caused most DRIFT work), no evidence-log instructions (the runner writes
them), no per-step tick lists beyond the stories and wiring checks.

### 7.2 Session model

- **One Kilo session per sprint** (frontier primary, frontier helpers drafting ahead, see 7.3),
  Studio MCP connected, 2–4 hours.
- Loop inside the session: write code → `studio_gate --stories <screen>` (seconds) → fix → repeat;
  then `studio_gate --sprint <ID>` once at the end.
- On a red gate the same session fixes it. After two failed full gates on the same check, the
  session stops and writes one question.
- Commit by explicit path, message `<ID>: …`, as today (hooks unchanged).
- Status at the end: **DONE** when the gate is green and the vision approval is recorded at
  that revision. Accepted-with-notes items become follow-ups in the next sprint's plan. Victor's
  playtest critiques arrive later as their own small fix sprints.

### 7.3 Where sprints run: Kilo Code through the coordinator

Every sprint in this batch runs **in Kilo Code, driven by the coordinator**, so the new process is
proven on the toolchain you'll use for everything after. The coordinator gets a `fast_lane` mode
in UI-T0 (section 8):

| Role | Who | What it does in a fast-lane sprint |
|---|---|---|
| **Primary (implements)** | Kilo session. **UI-D1, UI-02a:** frontier persona (`Foundry-Kilo-Sonnet` via Claude Max, the persona that ran 24 of UI-02's 31 sessions). **UI-03a…UI-06a:** local Qwen (decided, D11), helped by frontier agents along the escalation ladder (section 14), with an automatic hand-off to the frontier persona after two failed full gates | Holds the sprint for one long session: applies helper drafts, writes the rest, runs `studio_gate --stories` in its loop, then `studio_gate --sprint`, and commits. Studio MCP is already configured in `kilo.jsonc`. |
| **Frontier helpers (draft ahead)** | The existing prework / helper lanes on Claude (Max) and GPT | Foundation sprints: they draft components, stories, fixtures, view-models and reason maps as patches in `.plan/tmp/prework/<task>/` before and during the session (the v0.126 prework lane), and the primary verifies and applies them ("frontier writes the framework, Kilo implements"). Local-primary sprints under D11: they give step guidance, API notes, reviews and triage instead of code, so the code stays local-authored. |
| **Local Strata/Qwen** | Primary on UI-03a…UI-06a (section 14) | Writes those screens, including string tables, reason-map rows and fixtures. There is one local stream (an existing adapter rule), so there are no local helper lanes while it is the primary. |
| **Planner** (`Foundry-Kilo-Planner`) | Before each sprint | Turns this plan into the sprint's one-page plan (stories, wiring checks, file list) against the current HEAD, so the primary starts with zero planning. |
| **Reviewer** (`Foundry-Kilo-Deep-Review`, GPT-6 Astra → Opus 5.5, the existing UI override) | Every sprint | Code review at the reviewed revision. On UI-02a it also gets the foundation checklist (router, action registry, components), because everything after builds on it. |
| **Second opinion** (`Foundry-Kilo-Helper-GPT`, CLIProxyAPI, high-value lanes) | Test-design and triage lanes when GPT has usage | Writes wiring and focus-walk checks, and triages a red gate in parallel with the primary. While GPT is out, these units run on `Foundry-Kilo-Helper-Claude`. |
| **Vision approver** (`Foundry-Kilo-Visual-Review`) | After the mechanical gate | Approves captures against the references and rubric (6.5). |
| **Roblox Assistant** (through Studio MCP) | During the session | `explore` subagent for codebase and game-state questions; the Roblox skills as guidance; the `playtest` subagent for an advisory smoke pass (section 13). |
| **Coordinator** | Fast-lane mode | Starts the sprint and runs the runner as its verify step. It reads the verdict JSON as evidence and keeps the same session alive on a red gate (sends the findings into that session instead of opening a new one or a recovery case). It queues the next sprint when the gate is green and keeps the look pending without blocking. |
| **Lead** | Only for real blockers | For example, a contract question the plan doesn't settle, or two failed full gates on the same check. Not for routine red checks. |

Speed note: UI-02's frontier sessions averaged 8 minutes, the local ones 23 minutes, with 2
errors. Local primaries on templated screens with stories and a fast gate should do better than
that, but the estimate below adds time for it.

### 7.4 Provider failover: Claude and GPT (decided 2026-10-20)

Situation today: Claude (Max, via Meridian) has usage and GPT (via CLIProxyAPI) is out. Either
can run out mid-batch, so no frontier role may depend on one provider.

**What already exists** (live persona records, confirmed 2026-10-20). Two backends: Meridian
(Claude) and CLIProxyAPI (GPT). Each persona has an ordered route; a new call goes to the
preferred model, else the listed fallback. A call already in progress is never switched. If
neither route is eligible, Foundry refuses or defers; it never silently uses local Strata.

| Job | Persona | Preferred → fallback |
|---|---|---|
| Sprint planning | Foundry-Kilo-Planner | Opus 5.5 → GPT-6 Astra |
| Normal code review | Foundry-Kilo-Review | Sonnet 5.5 → GPT-6.1 Sol |
| Deep / high-risk review (also the UI reviewer override) | Foundry-Kilo-Deep-Review | GPT-6 Astra → Opus 5.5 |
| Screenshot review / vision approval | Foundry-Kilo-Visual-Review | Sonnet 5.5 → GPT-6 Astra |
| Research | Foundry-Kilo-Research | GPT-6 Luna → Claude Haiku 5.5 |
| Blocked-sprint lead | Foundry-Kilo-Lead-Manager | Opus 5.5 → GPT-6 Astra |

- Coding helpers are pinned one per backend: `Foundry-Kilo-Helper-Claude` (Meridian / Sonnet 5.5)
  and `Foundry-Kilo-Helper-GPT` (CLIProxyAPI / GPT-6.1 Sol). The helper order is Meridian first,
  with CLIProxyAPI reserved for high-value optional units. Up to 3 helpers per backend (6 total)
  run in parallel, with Strata as the single local primary writer. When both have usage, both
  contribute. When GPT is out, its units defer on the quota gate and Claude helpers carry on.
  An ordinary helper task never spends GPT quota just because Claude is busy.
- Optional helpers are admitted only below 90 % used on every applicable window, with fresh
  readings. After a defer, they resume below 85 % after a 10-minute cooldown. Required planning
  and review have their own admission rules.
- **Decided 2026-10-20: raise the optional-helper cutoff to 95 %** (hysteresis stays 5, so they
  resume below 90 %). It is a global router setting (`helper_policy.cutoff_pct`), changed by
  Victor in the control center's helper-policy settings (an operator session is required) or in the
  backend's `config.yaml`. The separate essential-work limits (for example CLIProxyAPI's broader
  85 % limit) are not changed by this; they need their own decision.
- Measured 2026-10-20 (coordinator events, last 48 h): Meridian admitted and ran 102 helpers but
  was often at its 3-helper cap. CLIProxyAPI was correctly read as out (weekly window at 90 %, or
  unknown). 12 deferrals came from the per-task lane budget, not quota.

So the user's requirement (fail over now, use both when both have usage) is **already
the design**. UI-T0 closes four gaps:
1. **Frontier primary route:** `Foundry-Kilo-Sonnet` (used for UI-D1, UI-02a and the hand-off)
   gets an ordered fallback (Sonnet 5.5 → GPT-6.1 Sol), so the primary isn't Claude-only.
2. **Automatic hand-off:** `[frontier_primary]` is manual-only today. The fast lane adds the
   trigger "two failed full gates on a local-primary sprint" (still one phase per task, the same
   turn and minute limits).
3. **Role overrides for the new task ids:** `[role_overrides.ui]` lists UI-02…UI-GT1. It becomes
   UI-03a…UI-06a and UI-V1 with the Strata implementer, plus UI-D1 and UI-02a with
   `Foundry-Kilo-Sonnet` as implementer, and the Deep-Review reviewer kept for all of them.
4. **Live failover check with GPT out (today's real situation):** before "begin", one real call
   each through Deep-Review and Research (GPT-preferred) must be served by their Claude
   fallback. A high-value helper unit must still dispatch on Meridian, and the control center
   must show which route served each call and why. A failure here is fixed before any sprint
   runs.

Unchanged rule: a frontier role never falls back to local Strata. If both providers are out,
required work waits with "waiting for frontier usage" and optional helpers are skipped; nothing
fails the sprint for that reason.

### 7.5 Rules card (replaces AGENT_RULES for fast-lane sprints)

Keep: scope lock (file list), server authority, verify Roblox APIs, content safety, hard safety
limits (§24), commit by explicit path, no force/rebase/amend, tests before commit, Studio MCP is
knowledge only and code lives in git (§27), asset bible as source of truth (§25).
Drop for fast lane: raw-evidence formatting rules (§13), retroactive evidence (§15), amendment
callout ceremony (§17, replaced by one line in the commit message), boot-line pasting (§21, the
runner does it), status-word rules (§23), tiers (§30, every fast-lane sprint uses the runner),
per-step plan ticking (§34). The full rules stay in the repo for anything outside the fast lane.


---

## 8. The sprints (UI batch)

Estimates are machine time for one Kilo session per sprint with Studio connected, assuming the UI-T0
tooling checks pass. Your time is listed separately.

| # | Sprint | Est. | Your time |
|---|---|---:|---|
| 1 | **UI-T0** Tooling, process, coordinator fast lane, failover gaps, skills, training capture, environment hardening | 11–12 h | 10 min (Studio settings, display scaling) + a backend update for the release |
| 2 | **UI-D1** v3 design mock, vision-approved | 2–3 h | optional look |
| 3 | **UI-02a** Foundation, HUD, entry | 4 h | — |
| 4 | **UI-03a** Hero build | 3 h | — |
| 5 | **UI-04a** Economy | 2.5 h | — |
| 6 | **UI-05a** Defenses (build ring) | 4 h | — |
| 7 | **UI-06a** Match end and overlays | 2.5 h | — |
| 8 | **UI-V1** Batch verification (full regression, functional and focus checks) | 2 h | — |
| | **Total** (as decided: local Strata/Qwen primary for UI-03a…06a, failover gaps and hardening included) | **≈ 36–40 h** | **≈ 10 min, then your own playtesting** |

Back to back, that is two long days plus a bit. Budget three, to absorb the tooling checks, your
review, and Kilo session overhead. The sprints run unattended in the coordinator, so overnight
counts. Helper prework drafts the next sprint while the current one runs, which is where most of
the speed comes from. Running two Kilo primaries at once is not recommended: Studio is a single
exclusive resource and the coordinator keeps one writer.

### UI-T0 — Tooling and process (no game features)
- Run the 12 tooling checks (section 5). If a check fails, the runner design adapts before
  anything else is built (for example, CLI `RunScript` instead of MCP for Edit captures).
- Build `mcp_stdio`, the `studio_gate` runner (host checks ported from `gate.ps1`, story lane,
  wiring lane, regression lane executing `GateRunner.run`'s CALL plan with the bridge and DYNARG
  handling, tally, verdict JSON, evidence summary, contact sheet, hand-off to vision approval), the
  file→suite map, the HTML reference renderer, the story harness and `tests/StoryHarness.lua`.
- **Coordinator fast lane** (Foundry-Router release, tested like every release):
  - a `[fast_lane]` profile mode for a task;
  - the runner as the verify step, with the verdict JSON read as evidence and mapped onto the existing obligations;
  - one session kept alive across red gates, with findings sent into the same session;
  - prework helpers fed from the one-page plan's stories and components list;
  - Lead triggers limited to real blockers;
  - the look tracked as pending without blocking the queue.
- Write the fast-lane rules card and plan template into the AU repo, and point the AU adapter's
  BUILD prompt at them for fast-lane tasks.
- **Skills (section 13):**
  - Rewrite `.kilo/skills/au-roblox-ui-development` for v3. Today it says not to add a UI framework and to wait for human visual approval.
  - Add `au-ui-v3`: templates, tokens, the action registry, the story format, adapters, input rules and the vision rubric.
  - Add `au-fast-lane`: runner commands, the rules card, and how a red gate is handled.
  - Author the same two as Roblox creator Skills (through `rbx-create-skill`), so Assistant and Kilo follow identical rules.
  - Review the community skill packs and adopt the useful, licence-compatible parts.
  - Enable `[approval] visual_model` for fast-lane tasks, with the rubric.
- **Training capture (section 14):** fast-lane episode tags, verdict JSON and vision verdicts as labels, per-step authorship for the escalation ladder, and the policy check described there.
- **Provider failover (section 7.4):** the existing persona routes stay as they are. Add a GPT fallback to the frontier-primary persona and the automatic hand-off trigger, update the role overrides for the new task ids, and run the live failover check with GPT out.
- **Queue reset (section 10):** cancel UI-02 and UI-03…UI-GT1 as superseded, first thing, with the project still paused.
- **Environment hardening** (found 2026-10-20; each one has stalled or slowed a sprint before, or would):
  1. **Kilo snapshots off for AnimalUprising.** `kilo.jsonc` has no `snapshot` setting, so Kilo will raise the "snapshot system is slow, disable?" pop-up again; that stalled UI-02 for about 40 minutes. Set `"snapshot": false`, preferably in the user-level Kilo config so no game commit is needed. Git commits are the record anyway.
  2. **Runner fits Kilo's command limits.** A full gate can exceed a shell call's timeout. The runner gets `--background` (writes progress and verdict JSON) plus `studio_gate status`, and the session polls instead of holding one long call.
  3. **No permission prompts mid-sprint.** Pre-allow in `kilo.jsonc` exactly what fast-lane sessions need: the runner command, Lune, git by explicit path, and the Studio MCP tools the runner and primary use. Keep the existing denies (uploads, monetization, find-and-replace). Helper agents keep `bash: deny` and get no Studio tools, so only the primary and the runner touch Studio.
  4. **One Studio user at a time.** The runner takes the coordinator's existing `studio:victor-workstation` exclusive lock, so no helper or Assistant subagent drives Studio during a gate. It targets the Studio window by place name through `list_roblox_studios`.
  5. **Local Strata/Qwen tuned for long sessions** (it writes UI-03a…06a). Today it runs `--ctx 32768` (`BRAIN_CONTEXT`) with a q4_0 KV cache on the RTX 3050, which is tight. Confirm the local server's context window covers a sprint (prompt + skills + plan + files, about 32–64k). Keep the system prompt and skill block byte-stable, so the prefix cache holds. Avoid thinking-mode flips and guard trims mid-session (each cache bust cost 4–6 minutes before). The rules card replaces the 482-line rules file in the prompt, which also helps.
  6. **Fresh quota readings.** Helpers are admitted only on fresh usage readings, and stale means "unknown, never admitted". Check that Meridian (now on Max) and CLIProxyAPI both report current windows, so Claude helpers aren't silently idle and GPT is correctly shown as out.
  7. **Studio preflight** (`studio_gate doctor`), run before every sprint:
     - Studio is open on the right place and not minimised;
     - Rojo serve is running and the plugin is connected;
     - both MCP servers answer;
     - "Allow HTTP Requests" is on for the test bridge, and "Allow Mesh / Image APIs" is on for captures;
     - the Scene Analysis beta is on;
     - Windows display scaling is 100 % and the viewport is big enough.

     Measured 2026-10-20:
     - Windows scaling is **125 %** (AppliedDPI 120), so it must change before captures;
     - the Studio viewport is 1504×899 logical, smaller than 1920×1080;
     - ShowDevelopmentGui is on and HTTP is enabled;
     - StudioDeviceSimulatorService, SceneAnalysisService, StudioTestService, ButtonActions and RegressionIndex are all present.

     It fails fast with the exact fix instead of a mid-gate surprise.
  8. **Overnight runs survive the machine.** The FoundryStack sign-in task exists. Add keep-awake while a sprint runs, and set Windows Update active hours or pause updates for the batch, so no reboot lands mid-sprint.
  9. **Helper lanes trimmed for UI.** UI-02 showed lane-limit churn (failure_triage and research units repeatedly hitting their caps). Fast-lane tasks enable only prework, test design, triage and review, with per-sprint caps sized to the one-page plan.
  10. **Git hygiene on OneDrive.** The dev checkout's automatic git maintenance fails on OneDrive ("mmap failed"). Turn off auto-maintenance there (`maintenance.auto false`, `gc.auto 0`). The game repo at `C:\Roblox` is unaffected.
  11. **Skills actually load.** Every Kilo agent's skill allowlist in `kilo.jsonc` is `"*": "deny"`, so `au-ui-v3` and `au-fast-lane` must be added per agent (primary, helpers, reviewers), or they silently won't load.
  12. **Old rules scoped away from fast-lane tasks.** These contradict the fast lane and must apply only outside it:
     - `.kilo/rules/animaluprising-adapter.md` ("do not use its playtest agent");
     - `au-ui-review` (human approval);
     - `au-runtime-gate` (run `gate.ps1`'s printed calls).

     `kilo_skills/ROUTING.md` still says a 70 % cutoff; it is updated to the live policy.
  13. **Rules card completeness.** It also keeps style, commit verification and the close-out duties (summary, STATE row, push) from rules §9, §11 and §18. Close-out stays a short close session driven by the runner's verdict.
- This sprint is built here (it is tooling, not game work) and released and deployed as a normal
  Foundry-Router version. From UI-D1 on, **every sprint runs in Kilo through the coordinator**.
- Housekeeping: close UI-02 as **SUPERSEDED by UI-02a** (one summary note, no CLOSE ceremony);
  file the three never-filed issues (KingCoon doc drift, no in-match Gold push, no squad HP); mark
  the stale KNOWN_ISSUES rows resolved; fix the test files the regression needs green: GF_FX1
  FX1.4 (reads the deleted HudUi), and the GF_UI1/GF_UI1b `PANELS` tables (they point at deleted
  ScreenGuis).
- **Done when:** the runner executes today's `ui` subset at HEAD unattended and prints a verdict,
  a sample story is captured at 5 sizes with checker output, and the contact sheet renders. A
  dry-run fast-lane task in the coordinator takes a tiny fixture change through Kilo end to end:
  helper draft, primary applies, runner verdict, commit, and the next task queued.

### UI-D1 — v3 design mock, vision-approved
- Update the HTML prototype to v3: HUD for desktop, landscape phone, portrait phone and controller
  (focus states, prompt footer); build ring and inspect card; rail, tab bar and Menu; T1–T8 with real
  content per screen (hero select, inventory, skills, profile, marketplace, Randy, store, critters,
  walls & gates, results, squad, settings, overlays).
- Render each at the 5 sizes. The frontier vision approver checks them against the design brief
  (section 4) and the reference teardown, using the same rubric. You can look at the sheet, but
  you don't have to.
- The approved renders become the **reference images** every later vision approval compares
  against.

### UI-02a — Foundation, HUD and entry
- React-luau via Wally. Clients need a shared `Packages` Rojo mapping (today it exists only for the server), and changing the project file means restarting Rojo, which rule §27 allows. v3 boots through one module placed directly under `UI/` (ControllerLoader boots only direct children), so the expected client boot count changes by one. `src/client/UI/v3/`; UiRouter (one back stack); action registry and input
  mode layer (key / pad / touch prompts, live switching); theme tokens from `Config/UiTheme`.
- Components: T1 ModalFrame, T8 ConfirmDialog, Button, CurrencyChip, StatusPill, Toast (2 tiers),
  PromptFooter, focus helpers (SelectionGroup, neighbours, focus memory).
- HUD widgets: StatusPill, VitalsBar, AbilityButton + AbilityArc (touch) / AbilityRow (desktop,
  portrait), Minimap host (existing renderer, today's 2000×1200 bounds) + MapOverlay, AlertStrip,
  Intel drawer, TutorialSlip ("Hide hints" included), DownedStrip, Rail / TabBar / Menu.
- Screens: Hero select (T3, grid on phone), lobby briefing.
- Adapters reuse `HudModel` / `HudGeometry` (they work and are Lune-tested).
- Remove `CompactHudUi`, `HeroSelectUi`, `DownedUi`, `TutorialHintUi`, `WaveBannerUi` once green.
- Context actions wired to their existing remotes: Net (`RequestThrowNet`), Field Revive
  (`RequestFieldRevive`).
- Carry-over fixes: 1, 2, 3, 4, 18, 20, 21, 22, 23, 24, 33 (section 9).

### UI-03a — Hero build
- Inventory (T2, exactly 9 slots, tap item → tap slot → Equip, no drag), loot toast, Skills (T4;
  phone uses branch tabs; capstone confirm), Profile / Wardrobe (T3 + T2).
- Harvest the pure models from `UI-03_PRECODE` (InventoryModel, SkillTreeModel, ProfileModel) as
  view-models; the rest of the pre-code is retired.
- Remove InventoryUi, SkillTreeUi, ProfileUi once green.
- Carry-over: 9 (inventory reasons), 13, 14 (unpin S49's designed failure), 16/17 for these screens.

### UI-04a — Economy
- Marketplace and Roving Randy (T2 + T8; never decrement locally; every reason code translated),
  Store screen shell (products arrive with S52), reusable EconomyAdapters for the Bazaar later.
- Randy's shop opens from a ProximityPrompt (`OpenShop` has no caller today).
- Harvest `UI-04_PRECODE` EconomyAdapters / ShopViewModel and the `Strings.Economy` rows.
- Remove MarketplaceUi once green.
- Carry-over: 6, 7 (fix the remote docs), 9, 15.

### UI-05a — Defenses
- RadialRing + InspectCard for towers (build, upgrade, the three mod categories, sell; target mode
  read-only), walls and gates, and castle actions (Boiling Oil, Klaxon with confirm). The client has
  no reference to the castle gate model, so the castle ring's world anchor is unverified; the
  fallback is opening it from the rail's Build/Castle row. Plot highlighting; controller
  nearest-plot targeting; touch tap-tap.
- Critter Corrections (T2 roster + T3 detail; deploy, timers from server deadlines).
- Server pieces from `UI-05_PRECODE`: D1 tower owner in `RequestTowerInfo`, D5 garrison slot race.
- Remove TowerShopUi, TowerPlacementUi, TowerInspectUi, CritterCorrectionsUi once green.
- Carry-over: 8, 9, 10, 11, 12, 28 (oil, klaxon).

### UI-06a — Match end and overlays
- Results and photo moment (T7, Kept / Reset sections, rewards only after the server confirms),
  loading / arrival (A01). The AFK, update / reconnect and profile-failure overlays (T1) have no
  server contract in `src` yet, so they ship as templates and stories with production triggers off,
  ready for GF-AFK1 and GF-LIVE2. Squad, Settings and
  Help shells (T5; settings content belongs to GF-UX1).
- Harvest `UI-06_PRECODE` ResultsModel. Remove ResultsUi once green.

### UI-V1 — Batch verification (replaces UI-GT1's automated part)
- Full `ui` regression unattended. Obsolete suites are already retired **in the sprint that
  deletes their screen**, because deleting a module breaks its suite inside that sprint's own
  gate. Their behaviour moves into v3 stories and wiring checks:
  - UI-02a retires `UI_02_check` and S45's HUD legs;
  - UI-03a retires the S48, S49 and S51 client legs, the `eS51Set` ProfileUi pin, and `GF_FX2_check`'s UI hooks;
  - UI-04a, UI-05a and UI-06a do the same for theirs.

  UI-V1 confirms nothing obsolete is left and keeps the layout checker, AS_C16 and the Lune specs.
- Every screen's functional checks (6.6) together: button activations through `ButtonActions`, menu
  open/tab/back loops, the controller focus-graph walk, touch hit sizes, the 20× reopen leak check.
- The Assistant `playtest` subagent does one advisory end-to-end pass (lobby → select → wave 1 →
  shop → build → downed → results) and its findings are triaged.
- One batch-wide vision approval over all screens together, for consistency across screens. It
  also closes UI-01's and UI-02's pending looks.
- Physical click, touch and controller testing stays with you, in your game testing.


---

## 9. Carry-over fixes from UI-01 / UI-02 (where each one lands)

Full evidence with file:line: `AU_UI_AUDIT.md` §4 (shared notes folder).

| # | Issue | Lands in |
|---|---|---|
| 1 | Banner layer hidden under HideAbilityBar (`HudVisibility.lua:38-39`) | UI-02a |
| 2 | HudModel drops status effects that arrive before the combat id | UI-02a |
| 3 | A defeated boss reappears in the HUD (`HudModel.lua:333-348`) | UI-02a |
| 4 | `SetKeepouts` accepts NaN / inf | UI-02a |
| 5 | KingCoon payload doc drift (never filed) | UI-T0 (file) |
| 6 | `RequestPurchase` doc says table, code is positional | UI-04a |
| 7 | `ShopStockChanged` doc says array, it is a dict | UI-04a |
| 8 | `TriggerBoilingOil` doc vs server | UI-05a |
| 9 | Raw reason codes shown to players (5 screens) | each screen's sprint (one reason map per service, pinned by a Lune spec; the codes from the not-yet-applied SEC1/SEC2 pre-codes go in an explicit allowlist citing the diff lines, so the spec's stale-code check doesn't fail at HEAD) |
| 10 | Trainer name lookup is nil (`Strings.Faction.NamedNpcs`) | UI-05a |
| 11 | Corrections offers MeshPart plots the server rejects | UI-05a |
| 12 | Tower inspect opens on enemies | UI-05a (ring only opens on tagged towers) |
| 13 | Inventory preview shows raw stat keys | UI-03a |
| 14 | Inventory list overlaps the equip slots (S49 designed failure) | UI-03a |
| 15 | Marketplace close button over the Candy pool / off-screen on phones | UI-04a (T1 owns close) |
| 16–17 | Fixed-px panels clip on phones; close buttons over text | each screen's sprint (T1/T2 are viewport-relative) |
| 18 | "Menu" label wraps to "Men/u" | UI-02a (rail icons + labels) |
| 19 | Kit spacing / unit rounding drift | obsolete with v3 components |
| 20 | No `UnregisterPanel` | UI-02a (router) |
| 21 | Panel re-size lags one frame on viewport change | UI-02a |
| 22 | Placement modal has no Back path | UI-02a (router) / UI-05a (ring) |
| 23 | Modal stack doesn't actually disable lower modals | UI-02a (router) |
| 24 | HeroSelect phase param misnamed | UI-02a |
| 25 | MatchTimerTick fires at Heartbeat rate | later (server, S58) |
| 26–27 | No in-match Gold push; no squad HP to clients | UI-T0 (file); UI shows only what exists |
| 28 | No client caller for net, revive, oil, klaxon, Randy's shop | UI-02a (net, revive), UI-04a (Randy), UI-05a (oil, klaxon) |
| 29 | Randy stock never resets at Lobby (server) | contract batch (GF-SIG1 F1) |
| 30 | Stale KNOWN_ISSUES rows | UI-T0 |
| 31 | UI-02 CLOSE never ran | UI-T0 (closed as superseded) |
| 32 | c12 physical input flow | functional version in UI-V1; physical version in your playtest |
| 33 | Intel attention flags, tutorial vs alert priority, no "Hide hints" | UI-02a |
| 34 | Pending visual approvals for UI-01 / UI-02 | UI-V1 (batch-wide vision approval) |
| 35 | GF_FX1 FX1.4 and GF_UI1 `PANELS` point at deleted UI | UI-T0 |
| 36 | Harness procedure lessons (mouse unlock, inset, click route, require cache, timing windows) | UI-T0 (encoded once in the runner) |
| 37–38 | UI-01 carried legs; checker caveats | UI-V1 (re-run), UI-T0 (per-widget touch floor in the checker) |

**Server contracts the screens must already handle** (pre-coded, not applied yet): GF-SEC2
`rate_limited` on every client-callable remote (events drop silently, so every action needs a
no-reply timeout state), GF-SEC1 new reject codes and `NpcBuy` removal, GF-FX5 continuous resource
regen, GF-SP1's 15 new specialty items. The v3 reason maps include these codes from the start, so
the contract batch can land before or after the UI without UI changes.


---

## 10. What happens to UI-02, the pre-codes and the coordinator

- **UI-02** stops here. Its committed code stays in the game until UI-02a replaces each piece, so
  nothing is lost or broken in between. It is closed as "superseded by UI-02a" in UI-T0, with
  one short summary and no CLOSE ceremony. What it proved (HudModel, HudGeometry, the D1–D10
  rulings) carries into UI-02a.
- **Pre-coded UI diffs** (UI-03…06) are not applied. Their pure models, reason maps and string
  tables are harvested in the matching v3 sprint.
- **Server pre-codes** (GF-FX5, SEC1, SEC2, SIG1, SP1) go into one contract batch, scheduled right
  after the UI batch or in parallel if you prefer.
- **The coordinator** stays paused until UI-T0 ships its fast-lane mode. Then it runs UI-D1
  through UI-V1 in Kilo, queued, with frontier helpers drafting ahead.

### Queue reset and start sequence (agreed 2026-10-20)

Current queue: UI-02 (IMPLEMENTING, held by the project pause), then UI-03, UI-04, UI-05, UI-06,
UI-GT1 (QUEUED).

1. **First step of UI-T0, with the project still paused:** cancel UI-02 with the reason
   "superseded by UI-02a (UI v3 plan)", then cancel UI-03, UI-04, UI-05, UI-06 and UI-GT1 with
   "superseded by the UI v3 sprints". Nothing in git is reverted, and UI-02's committed code stays
   until UI-02a replaces it. Its Kilo session is ended, not left idle.
2. UI-T0 is built and released (tooling, fast lane, skills, failover, training labels). The
   one-page plans for UI-D1…UI-V1 are written, and the coordinator dry run passes.
3. **When you say "begin":** the new sprints are queued in order (UI-D1 → UI-02a → UI-03a →
   UI-04a → UI-05a → UI-06a → UI-V1, each depending on the previous) and the project is resumed.
   Nothing runs before you say so.

## 11. Decisions I need from you before starting

| # | Decision | My recommendation |
|---|---|---|
| D1 | Adopt the new process: one-command gate, stories in Edit mode, batched look, one long Kilo session per sprint through the coordinator's fast lane, frontier primary + frontier helpers drafting ahead | **Yes** |
| D2 | UI framework | **React-luau** (official Wally packages); fallback: keep the current Kit with the same story harness |
| D3 | Close UI-02 as superseded; redo as UI-02a | **Yes** |
| D4 | Retire the UI pre-code diffs (harvest models only); server pre-codes as one contract batch after the UI | **Yes** |
| D5 | Phone jump button: keep it (moved into the right-thumb arc) or remove it | **Keep**, moved into the arc |
| D6 | Approve the v3 design changes: build ring at plots replaces the Tower Shed + placement flow; left rail / portrait tab bar; ability arc on phone | **Yes**; the UI-D1 mock shows them before any code |
| D7 | Capture PC settings | **Resolved 2026-10-20 by the tooling check:** device-simulator captures are exact at 125 % Windows scaling, so no scaling change is needed. Still needed: the Scene Analysis beta enabled (for the leak check), and Studio not minimised during runs. |
| D8 | Frontier vision approval replaces your per-sprint look (rubric 6.5; two CHANGES rounds, then accept with notes) | **Yes** |
| D9 | Publish `au-ui-v3` and `au-fast-lane` as Roblox creator Skills as well as Kilo skills; adopt reviewed parts of the community skill packs | **Yes** |
| D10 | Functional input testing only (button calls, focus walk, hit sizes); no mouse or touch emulation; physical testing in your playtests | **Yes** |
| D11 | Training data | **Decided 2026-10-20:** local Qwen primary for UI-03a…06a, with frontier helpers producing guidance, interfaces, tests and reviews; patches only on the escalation ladder (section 14) |
| D12 | Queue reset | **Decided 2026-10-20:** cancel UI-02 and UI-03…UI-GT1 as superseded at the start of UI-T0; queue the v3 sprints only when you say "begin" (section 10) |
| D15 | Wardrobe tab in Profile | **Decided 2026-10-20:** hidden for now |
| D14 | Helpers and Studio skills | **Decided 2026-10-20:** helpers may use `Roblox_Studio_skill` only (read-only guidance); every other Studio tool stays denied |
| D13 | Provider failover | **Decided 2026-10-20:** keep the existing ordered Claude/GPT persona routes and the two-backend helper setup; close the four gaps (frontier-primary fallback, automatic hand-off, new role overrides, live check with GPT out) in UI-T0 (section 7.4) |

**Future options, not in this batch** (they'd add mechanics the specs currently forbid or defer):
a squad "call next wave" vote, auto-upgrade toggles, a phone HUD layout editor (size and position
per button, recommended after launch feedback), a pre-match loadout screen.


---

## 12. After the UI batch: replanning the rest of the roadmap

The master order still lists about 200 sprints (Phases 5–21). At the old pace that is years. The
same method applies to all of them:

1. **Group by surface, not by asset or rule.** Server-contract sprints (GF-FX5, SEC1, SEC2, SIG1,
   SP1) become one or two "contract" batches with Lune tests plus the runner's server suites. Asset
   sprints (AS-*) become production batches with their own gate: import checks, the polycount and
   texture budget, a thumbnail sheet for one look. They need no gameplay regression and can run
   in parallel with code sprints, because they don't hold Studio for long.
2. **Pre-coded diffs are retired.** Each batch is written directly in its session against the
   current HEAD.
3. **Every batch gets stories or fixtures first.** Screens get stories; services get Lune specs and
   one wiring check. "Done" is always one runner command.
4. **Approval is batched.** Looks (art, UI, VFX) are approved per batch on a contact sheet. Rules
   and numbers (balance) are approved once per phase as a table.
5. **Unattended runs.** Once the coordinator's fast lane exists (Phase B), batches queue overnight.
   You come back to contact sheets and a short list of questions, not to a stuck sprint.

The replan itself is a separate one-sitting task after this batch proves the process. It produces
a new `MASTER_ORDER` with batch sizes and estimates, for your approval.


---

## 13. Using Roblox Studio and its Assistant to full capacity

Verified on this PC 2026-10-19 through the official MCP server unless marked.

| Studio capability | How the pipeline uses it |
|---|---|
| `execute_luau` (Edit / Server / Client) | Story mounts, geometry dumps, `ButtonActions` activations, focus-graph walks, tally. |
| `screen_capture` + Device Simulator (`rbx-device-simulator-lua` skill; community `capture_device_matrix`) | 5-size captures of every story, following Roblox's own device-simulator skill. |
| `start_stop_play`, community `solo_playtest` / `multiplayer_playtest`, StudioTestService | Wiring playtests; 2-client late-join checks without manual setup. |
| `get_console_output` | Runtime errors fail the gate (any error during a story mount or wiring check). |
| **Skill `rbx-unit-test`** | Convention for in-Play unit tests; the wiring checks follow it. |
| **Skill `rbx-debug`** | When a gate fails, the primary sets breakpoints and inspects threads instead of guessing. |
| **Skill `rbx-perf-profiling`** + profilers | HUD update budget (≤ 250 µs, S45 c6) measured on the v3 HUD. |
| **Skill `rbx-scene-analysis`** (Scene Analysis beta must be on in File → Beta Features) | 20× open/close leak check (leaked unparented instances), memory per screen. |
| **Skill `rbx-docs-search`** | API verification before using any engine API (rule §4), instead of memory. |
| **Skill `rbx-create-skill`** + creator Skills | We publish `au-ui-v3` and `au-fast-lane` as Roblox Skills. Assistant and every MCP client (Kilo) then share one set of conventions. |
| **Subagent `explore`** | Fast codebase and live game-state questions during a sprint (Roblox's model, so it doesn't spend the primary's context). |
| **Subagent `playtest`** | Advisory smoke pass per screen after the gate, plus one end-to-end pass in UI-V1. Its findings are triaged; its "Pass" never counts by itself (Roblox staff warning). |
| Asset tools (`generate_mesh`, `generate_material`, `generate_procedural_model`, `upload_image`) | Not in this batch; used by the asset track (section 12). |
| Assistant in the Studio panel | Your tool during playtesting, with the same AU Skills loaded. Rojo syncs files into Studio, not back, so a live Studio edit is overwritten on the next sync and never reaches git (rule §27 also forbids Assistant edits). The AU Assistant Skill therefore answers with the source file path and a diff to apply in the repo, and labels any live edit as temporary. |

**Kilo-side skills** (`.kilo/skills`, loaded by every Kilo session): the rewritten
`au-roblox-ui-development`, the new `au-ui-v3` and `au-fast-lane`, the existing AU skills
(Luau, networking, performance, persistence, security, diff review, runtime gate), and reviewed
parts of the community packs (section 3).

## 14. Fine-tuning data from this batch

Foundry's training pipeline (`foundry_training`) already captures Kilo sessions, Foundry model
calls, the coordinator store and git. It builds verified episodes and exports redacted datasets
(SFT, correction, multimodal) to your own router. It never starts training. This batch adds:

- **Labels:** each episode carries the runner's verdict JSON, the vision verdict (APPROVED, or
  CHANGES with its numbered findings), the fix that followed, and later your playtest critiques.
  That gives clean "attempt → precise critique → corrected code" triples for Roblox UI.
- **Tags:** `fast_lane`, `ui_v3`, the template used (T1–T8, widget names) and the device sizes, so a
  Roblox-UI subset can be exported for the next project.
- **Artifacts:** captures are written under `docs/evidence/UI-v3/<sprint>/` (already an allowed
  artifact root), so multimodal episodes have their images.

**One constraint to decide on (D11).** Your current training policy only accepts **locally
authored** steps as training targets (`require = [..., "local_author"]`,
`allow_frontier_targets = false`). Frontier (Claude, GPT) output is never a training target. That
matches the providers' terms, which restrict using their outputs to train other models. So code a
frontier helper or a frontier primary writes is captured for provenance but **excluded** from SFT.
To get Roblox UI training data out of this batch:

- the foundation (UI-T0, UI-02a) stays frontier-built, for speed and quality;
- the template-based screen sprints (UI-03a…UI-06a) use **local Qwen as primary**, with frontier
  helpers giving guidance, reviews and vision verdicts rather than code. The gate and vision
  approval keep quality where it needs to be;
- if a local primary fails the full gate twice on a sprint, the coordinator hands the sprint to
  a frontier primary (the existing `frontier_primary` feature). The local attempt and the
  correction are kept as correction data.

That trades some speed on four sprints for a real local-authored Roblox UI dataset.
The alternative is frontier primary everywhere: fastest, but little usable training data.

**Decided 2026-10-20: local Qwen implements UI-03a…UI-06a, with frontier agents helping.** To
keep the episodes training-eligible, the helpers work like a senior engineer pairing with a
junior. They give the junior everything except the finished implementation:

| Frontier helpers may produce (Qwen's code stays training-eligible) | Counts as frontier-authored (kept for the project, excluded from training targets) |
|---|---|
| The sprint's one-page plan, step order, file list | A patch with the screen's implementation that Qwen applies or copies |
| Interface and type signatures, the props each component takes, adapter contracts | Rewriting Qwen's code in a review |
| Stories, fixtures and wiring checks (tests define "done", Qwen makes them pass) | A frontier primary finishing the sprint |
| API notes from `rbx-docs-search`, Roblox skill guidance, links to the reference renders | |
| Reviews and triage that point at the problem and say what is wrong, without writing the fix | |
| Vision findings ("the tab row overlaps the close button at 390×844") | |

**Escalation ladder per step** (speed is protected, data is maximised):
1. Qwen implements from the guidance.
2. If a step fails twice, a helper explains the failure and the approach (still guidance).
3. If it fails again, a helper drafts that step's patch. Only that step is marked
   frontier-authored and drops out of the training targets.
4. Two failed full gates on the sprint: the existing frontier-primary hand-off takes over the
   sprint.

Models harvested from the UI pre-code diffs were frontier-written, so they are tagged
`helper:precode-harvest` and drop out of the training targets too; the code Strata writes around
them stays eligible.
Provenance is recorded per step: prework patches are already attributed to `helper:<unit>`, and
the curator's `local_author` check uses that. So the export contains only Qwen-written code
as targets, with the frontier guidance as context. Frontier text is never a target, as your
current policy requires (`allow_frontier_targets = false`). This follows the providers' usage
terms as your policy encodes them; it's worth re-checking those terms before any external
release of a trained model.

---

## Sources

Research reports with full source lists, in the shared notes folder
`C:\Users\shock\AppData\Roaming\Claude\side-session-notes\local_504c6f4f-4dbe-48c5-b953-c79fb30ffd7a\`:
`UI_REFERENCE_TEARDOWN.md` (reference games, phone and controller patterns),
`UI_TOOLING_FEASIBILITY.md` (React-luau, stories, capture, device simulator, VirtualInput,
StudioTestService, Open Cloud, CLI), `AU_UI_AUDIT.md` (screens, contracts, carry-over, tests,
pre-codes, assets, commit analysis).

Key external sources:
- [Connect to the Roblox Studio MCP server](https://create.roblox.com/docs/studio/mcp)
- [Build with a coding harness (Roblox)](https://create.roblox.com/docs/en-us/ai/coding-harness.md)
- [Roblox Studio is going agentic](https://about.roblox.com/newsroom/2026/04/roblox-studio-going-agentic)
- [New Studio Testing APIs and Assistant Improvements](https://devforum.roblox.com/t/new-studio-testing-apis-and-assistant-improvements/4657854)
- [Studio Assistant & MCP Playtest Agent beta](https://devforum.roblox.com/t/studio-beta-studio-assistant-mcp-playtest-agent/4566767?page=2)
- [Assistant: Creator-Authored Skills](https://devforum.roblox.com/t/assistant-updates-introducing-creator-authored-skills/4728447)
- [Studio MCP updates and external LLM support](https://devforum.roblox.com/t/studio-mcp-server-updates-and-external-llm-support-for-assistant/4415631)
- [Open Cloud Luau Execution](https://create.roblox.com/docs/en-us/cloud/reference/features/luau-execution.md)
- [StudioDeviceSimulatorService](https://create.roblox.com/docs/en-us/reference/engine/classes/StudioDeviceSimulatorService.md)
- [Roblox cross-platform UI design](https://create.roblox.com/docs/ui/cross-platform-design)
- [react-luau](https://github.com/Roblox/react-luau), [UI Labs](https://github.com/PepeElToro41/ui-labs), [Flipbook storyteller](https://github.com/flipbook-labs/storyteller)
- [Device emulation screenshots and Windows scaling](https://devforum.roblox.com/t/device-emulation-screenshots-appear-to-be-affected-by-windows-display-scaling-when-using-actual-resolution/4764279)
- [UILint v2](https://devforum.roblox.com/t/plugin-uilint-v2-import-figma-ui-into-roblox-studio/4765869), [Framewisp](https://devforum.roblox.com/t/framewisp-%E2%80%94-turn-a-figma-frame-into-native-roblox-ui-in-one-click/4759239), [Bloxsmith](https://bloxsmith.net/), [VizzBees UI Maker](https://vizzbees.com/ui-maker)
- [RbxSync AI testing](https://docs.rbxsync.dev/ai-testing)
- Agent skill packs: [EL4CTEO/roblox-skills](https://github.com/EL4CTEO/roblox-skills), [MSayib/roblox-dev-skill](https://github.com/MSayib/roblox-dev-skill), [m4bwav/roblox-agent](https://github.com/m4bwav/roblox-agent), [zilibobi/roblox-skills](https://github.com/zilibobi/roblox-skills)
- Studio's own skill and subagent list: read from the official MCP server's `skill` and `subagent` tools on this PC, 2026-10-19
- [Kingdom Rush-style controls discussion](https://www.construct.net/en/forum/construct-2/how-do-i-18/kingdom-rush-style-controls-60587), [radial menus in game design](https://300mind.studio/blog/radial-menus-in-game-design/)

