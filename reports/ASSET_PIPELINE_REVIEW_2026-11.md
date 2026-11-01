# Animal Uprising 3D asset pipeline: review against "4 ways to make 3D models with AI" (2026-11-01)

Status: **approved 2026-11-01** (Victor: "Yes, recommended": D1-D5 as recommended below; start
Phase A). Phase A is the AU sprint AS-P9 "reference packs": its plan is pre-coded (a one-file
prep patch that adds `docs/progress/AS-P9_PLAN.md`) and not queued yet. Phase B waits until AS-P9 is
queued.
Sources read: the video page, chapters, description and transcript (built-in browser, 2026-11-01);
AU (read only) `docs/ASSET_PIPELINE.md`, `docs/ASSET_AUTOPILOT.md`, `docs/design/ASSET_BIBLE.md`
§0/§2.5/§2.6/§4.0, `tools/blender/*` (`build_bs_king.py`, `ref_setup.py`, `ref_template.json`,
`ai_mesh_eval.py`), `tools/assets/*`, the `docs/progress/AS-*` summaries; Foundry
`docs/plans/SLOT_BLENDER_PLAN.md`, `foundry_coordinator/slot_blender.py`, `services.py` (2D hold),
`visual_approval.py`, the live `projects/animaluprising/project.toml`, `contrib/media-adapter`
(foundry-images skill) and the AU kilo skills.

## TL;DR

- The video's best recipe: **four reference pictures of every side that agree with each other
  (one scale, a mirror test, sizes fixed in code) + a short note with only what the pictures
  can't show**, handed all at once to **one fresh builder per model**, checked by **an independent
  judge with its own checklist and shuffled letters**, with a **replay of every build**.
- AU already does the hard half well: deterministic headless builders with every size in code,
  8 fixed renders, numeric gates (`mesh_report.py`), Studio captures, a rework cap.
- AU's gaps are on the **input** and **judging** side:
  1. **No reference sheet has a scale file:** 0 of 93 3D sheets has a `_ref_vN.json`, so every
     model is built as `UNSCALED CONCEPT`. Nothing checks that the front, side and back panels
     agree.
  2. **Only one side view.** The other side, the top and the underside are guessed. Three
     sprints hit mirror mistakes (AS-E10, AS-J03, AS-G02).
  3. **The look gate is the builder grading its own work.** Foundry's independent vision
     approval sees Studio captures with a UI checklist, and never sees the concept.
  4. **One long session builds the model, faces, clips, Studio checks and upload**, instead of
     one fresh builder per model.
- Top three to do first (small effort, high impact): **a reference-pack tool that aligns the
  views and writes the scale file**, **sides named by axis plus an automatic asymmetry check**,
  and **an automatic silhouette match against the aligned views**.

---

## Part 1. What the video found

**Video:** "I Tested 4 Ways to Make 3D Models with AI", AI Dev Challenge, published about
2026-10-31, 12:33 long. The description opens with "How much does an AI need to know to build
YOUR 3D model". It follows the same channel's earlier "Blender MCP vs Headless" video.

### Test setup
- **Builder:** Claude Sonnet 5.5 on high effort, **a fresh agent every run**, Blender 5.2
  headless. The only thing that changed between runs was the input.
- **Objects (4):** wind-up toy robot, 80s arcade cabinet, 1950s fuel pump, red mini excavator.
- **Input modes (4):** one sentence; a 400-700 word description; one picture; four pictures
  (front, side, back, three-quarter).
- **Runs:** each mode 3 times with identical input: 4 × 4 × 3 = **48 models**. Repeating the
  runs also measured how much the results vary.
- **Cost and time:** each model took under 10 min and cost under $1. The 48 builds cost $23.64
  in total (API list price, builders only). **Four pictures were faster and cheaper on average
  than the long description.**

### How the models were scored
- **Judge:** a fresh Sonnet. It sees the design and every model under a **random letter**
  (shuffled, so it can't tell which mode made which), and scores them against **its own
  checklist**.
- The author treats the judge as one opinion and gives his own pick after every round. He
  disagreed once (excavator).

### Results per mode
| Mode | What came out | Typical failure |
|---|---|---|
| One sentence (plus height and file names) | A generic example of the category, never the author's object. Judge: under half the points; about **1/3** on average | The model's own defaults: a wind-up key nobody asked for, "ARCADE" text on top, a modern pump with a card reader, a yellow excavator (it was red) |
| Long description (~500 words) | All parts, **hidden back parts included** | Left/right mix-up: the author used the robot's own left for some parts and his own left for the gauge, so the gauge was on the wrong side 3 of 3 times. "Diagonal" stripes came out at the wrong angle and order. Shapes followed the text's numbers, e.g. a globe that was too round |
| One picture | Front close to the design | Back and sides empty. The **visible side was copied onto the hidden side**, and one excavator was fully mirrored. Hidden parts: **0 in every run** |
| Four pictures | Hidden parts present almost every time. Widths matched the design once the pictures were fixed | A side seen only in the three-quarter view got a copied hatch in 3 of 3 runs, because there was **no direct view** of that side |

- **Overall:** four pictures first, the description close behind, one picture third, one
  sentence far last.
- **The big difference was the hidden parts.**
- **Weak spot in every mode:** small precise details (gauge needle angle, dial tick marks).

### The pictures must agree (fixed before every four-picture build)
- GPT's views did not agree:
  - the robot changed size between views;
  - the key sat at different heights;
  - the arcade cabinet's front and back had different widths.
- The author lined the views up, put them on one scale, checked that parts sit at the same
  heights in every view, and ran a mirror test. **GPT redrew the pictures, but the exact sizes
  were fixed in code.**
- Fixing the views made a clear difference for the arcade cabinet (all three builds came out
  almost exactly as wide as the design) and little for the robot.

### Bonus: pictures plus words
- **Four pictures + the whole long description:** where they disagreed, the builder **trusted
  the numbers in the text over the picture**, and the round globe came back.
- **Four pictures + a short note** (real size, exact colours, details the pictures can't show
  clearly): the pictures led. Robot and arcade cabinet improved, the pump was mixed. It was not
  a magic fix.
- **His recommendation:** pictures from several sides plus a short note that holds only what the
  pictures can't show, and nothing that contradicts them. He calls it advice, not a law: 3 runs
  each is a small sample.

### The recipe (part 2)
1. A short brief from the human.
2. **Claude Code is the boss.** It plans, hands out the work and checks it. It does not draw the
   pictures or build the models.
3. **Pictures:** Claude asks GPT through the Codex CLI (signed in with a ChatGPT account). GPT
   draws the **three-quarter view first, then the other sides in the same chat**.
4. Claude checks that **every view shows the same parts**, puts them on **one scale**, and
   **fixes the sizes in code**.
5. **One fresh builder session** (a Sonnet agent with its own folder) gets **all four pictures,
   the short note and the task at once**.
6. The builder writes a Python script and Blender runs it headless. It then looks at renders,
   fixes the script, and runs it again.
7. **A replay of every build** is kept (named in the description).
8. **What you need:** Claude Code, Blender, Python, the Codex CLI with a ChatGPT account, and
   **two small skill files**: one for talking to GPT, one for headless Blender.

From the earlier MCP vs headless video:
- Headless was slower (crate: 9.4 vs 3.3 min) because it checked its work with full renders.
  The author still prefers headless.
- Five headless agents in parallel worked fine. Five agents in one MCP Blender queued up behind
  each other.

### What carries over to AU, and what doesn't
- **Carries over:**
  - views of every side that agree;
  - a short note instead of a long spec that contradicts the pictures;
  - left/right ambiguity in text;
  - copying across sides that no view shows;
  - an independent, blind judge;
  - a fresh builder per model;
  - a replay of every build.
- **Doesn't fully carry over:**
  - The test objects were hard-surface props. AU's animals are low-poly organic shapes with
    rigs, faces and size budgets.
  - AU's builders are long hand-written scripts (`build_bs_king.py` is 2,640 lines), not one
    short session per model.
  - The video doesn't test rigs, animation or Roblox import, which AU's pipeline also has to pass.

---

## Part 2. AU's current 3D process

**Flow:** approved ChatGPT concept sheet (bible §2.6) → `checklist_from_bible.py` → (optional)
`ref_setup.py` → hand-written deterministic builder `tools/blender/build_<id>.py` (`--plan` /
`--preview` without Blender, then a headless build) → `mesh_report.py` (budget, size ±10 %,
pivot) → `render_views.py` (8 fixed views + close-ups) → `contact_sheet.py --refs <concept>` →
**Claude look gate** (the same session, at most 2 rework passes) → `export_fbx.py` → Open Cloud
upload → `studio_asset_check.luau` and 4 Studio captures → batch review card for Victor
(ASSET_AUTOPILOT). Blender now also runs on slot-1 through `foundry-blender` (v0.163-0.165).

### Already matches the video (keep)
| Video principle | AU today |
|---|---|
| Sizes in code | Every dimension is a constant in the builder (e.g. `TARGET_REST`, `BUDGET_TRIS` in `build_bs_king.py`), checked by `mesh_report.py --size … --tol 0.10` |
| Headless builder that renders and fixes | `blender -b … -P build_<id>.py`, `render_views.py` (8 views), `--preview` pictures before Blender, rework passes |
| Fixed views for checking | front/back/left/right/top/bottom/two three-quarter views + close-ups; Studio front/side/back/three-quarter captures |
| Compare against the design | `contact_sheet.py --refs`; `build_bs_king.py --preview` already draws a cyan silhouette over the concept's FRONT/SIDE panels; `ai_mesh_eval.py` already computes silhouette IoU against a reference |
| Human gets the last word | Victor's batch review page, flags come back as rework |

### Evidence of the gaps
- **0 of 93** 3D concept sheets under `assets/source/<cat>/<ID>/ref/` have a `_ref_vN.json`.
  `ref_setup.py` exits 2 (`UNSCALED CONCEPT`) for all of them. Every model is built to the
  bible's target height alone, and nothing checks that the panels agree.
- **Sheets are single ChatGPT images** with FRONT / SIDE / BACK panels plus extras (e.g.
  BS-KING_ref_v2: front, one side, back, three phase poses, faces, monocle, rig concept). The
  panels are not on a checked common scale, and there is one side view, not two.
- **Mirror mistakes:**
  - **AS-E10:** "the grill was mirrored to the concept side" in the look gate.
  - **AS-J03:** a lever and a crate brace were mirrored to the concept front in a look-gate
    rework.
  - **AS-G02:** the concept's FRONT and IN HAND panels are mirror images of each other. The
    cleaver was built from one, crossed the rig's face in Studio, and had to be rebuilt and
    re-uploaded.
- **Left/right words in specs:** the King's builder says "His RIGHT side is +X … the monocle is
  on his LEFT eye (−X)". That is the character's own left, the same convention the video's author
  tripped over when he mixed it with his own left.
- **Self-judging:** summaries read "my own look gate passed". Foundry's independent check
  (`visual_approval.py`, `visual_model = true`, rule `docs/evidence/{task}/*.png`):
  - its prompt is a UI checklist ("readable text, clipping, … target sizes, safe areas");
  - it attaches only the Studio captures, with **no concept reference, no per-asset checklist
    and no blinding**.
- **One session does everything:** an AS- sprint builds the model, faces and 15-17 clips, then
  runs the Studio checks, uploads, and writes the ledger and evidence. Limits are 240 min per
  session and up to 18 sessions per task. The builder's context also carries all of the Studio
  and upload work.
- **Replays:** AU keeps "the previous pass's renders for a before/after strip". `foundry-blender`
  logs slot, seconds and files back per run, but no renders or script versions.
- **Skills:**
  - AU's kilo skills are about Luau, UI and runtime. There is **no Blender/asset skill**; the
    know-how lives in the 170-line `ASSET_PIPELINE.md` §7 and AGENT_RULES.
  - `foundry-images` (2D) has nothing for turnaround views.
  - Housekeeping: 11 of the 12 AU skills exist only as untracked files in the OneDrive checkout.
    `_base` and `foundry-deploy` have only `au-fast-lane`.
- **2D hold:** a sprint that needs `images` waits for Victor's answer (`services.images_hold`).
  New reference views would hit that hold every time.

---

## Part 3. Gaps and improvements, ranked

Ranked by impact per effort. "AU" means a change in the AU repo, done by a coordinator sprint
(this report changes nothing there). "Foundry" means a Foundry release (version bump and tag).

| # | Gap | Fix | Where | Impact | Effort |
|---|---|---|---|---|---|
| 1 | Views don't agree; no scale file (0/93) | Reference-pack tool: align views, write the scale file | AU `tools/assets/ref_align.py` (new); bible §2.5 note | High | S-M |
| 2 | Left/right words; mirror mistakes | Name sides by axis; asymmetry list + automatic side check | AU bible §2.5/§4.0, `checklist_from_bible.py`, `mesh_report.py` or new check | Med-High | S |
| 3 | No measured shape match | Silhouette IoU + overlay vs the aligned views per view | AU `tools/blender/ref_iou.py` (reuse `ai_mesh_eval.py` IoU code) | High | S-M |
| 4 | Builder grades itself; independent check has no reference | Asset judge: fresh vision session, reference pack + renders, own checklist first, shuffled letters | Foundry `visual_approval.py` (asset rubric), AU `project.toml` rule | High | M |
| 5 | Only one side view; top/underside guessed | Turnaround recipe through foundry-media: three-quarter first, then the missing sides from the approved sheet | Foundry `contrib/media-adapter` (skill + recipe); AU bible §2.5; 2D-hold decision | High | M |
| 6 | Long specs can contradict the pictures | "Short note" rule: only size, palette, hidden details, rig/budget; list conflicts before building | AU plan template / pre-code prompt; bible §0 | Med | S |
| 7 | No Blender/asset skill | `au-blender-asset` skill + turnaround section in `foundry-images` | Foundry `projects/animaluprising/kilo_skills/`, `contrib/media-adapter/skills/` | Med | S |
| 8 | No replay of builds | Build journal per foundry-blender run: script hash/diff, args, render strip, mesh_report line | Foundry `slot_blender.py` + task page / review card | Med | S-M |
| 9 | One long session per sprint | One fresh builder session per model, then the sprint session exports and checks; optional 2 candidates on slot CPU | Foundry engine (build sub-step), AU plan template | Med-High | M-L |
| 10 | Model choice for builders | Keep builder authoring on frontier; local Qwen only for the scripted steps | AU `project.toml` (config) | Low-Med | S |
| 11 | 16 tools hard-code the asset library path | Every tool reads `AU_ASSET_LIBRARY` (already planned as SLOT_BLENDER B2b "proper fix") | AU `tools/blender/*` | Low | S |

### 1. Reference pack: align the views and write the scale file
- **Why:** the video's biggest fix before the four-picture builds. AU's bible already asks for it
  (§2.5: "aligned orthographic front / side / back views at identical scale", a scale file per
  sheet), and `ref_setup.py` is ready to read it, but no tool makes the file. So 93 of 93 sheets
  stay `UNSCALED CONCEPT`.
- **What changes:** a deterministic tool, `tools/assets/ref_align.py` (system Python with
  Pillow/numpy; no AI in the measuring). It:
  1. **Finds each panel's silhouette.** The sheets use a plain grey background; `whitebg_cut.py`
     already does flood fill from the border.
  2. **Crops each view and puts it on one canvas** with a shared ground line, scaled so the
     target dimension (the bible class × R15 5.2, the same number the builder uses) gives one
     pixels-per-stud value for all views.
  3. **Runs the agreement checks:**
     - front width vs back width;
     - side depth vs top depth (when a top view exists);
     - key part heights across views (eyes, belt, crown top) within a few %;
     - **mirror test:** front vs horizontally flipped back, silhouette IoU above a threshold,
       with the differing regions listed (where asymmetric parts are).
  4. **Writes the outputs:**
     - `<ID>_ref_v<n>.json` with `scale.status: "calibrated"`, `silhouette_px` and
       `target.source` = the bible line (the bible's allowed method: "approved target dimension
       and the measured silhouette extent");
     - `<ID>_views_v<n>.png` (aligned views);
     - `<ID>_align_check.png` (overlays, ground line, height lines).
  5. **Fails with a reason** when the panels disagree beyond tolerance. The fix is then a redraw
     (gap 5), not a guess.
- **Backfill:** run it over the sheets for the assets still in the queue first, then the rest.
  An asset that is already built and approved doesn't need rework.
- **Done when:** `ref_setup.py` exits 0 for the next asset in the queue; a selftest with a
  deliberately mismatched sheet fails; and the result is a gate row in `selftest_all.py`.

### 2. Name sides by axis, and check asymmetric parts automatically
- **Why:** the video's long description failed on left/right in 3 of 3 runs. AU had three mirror
  incidents, plus a concept whose two panels were mirror images.
- **What changes:**
  - **Bible §2.5/§4.0:** specs, checklists and notes name sides as **+X / −X** (the asset faces
    +Y), plus "screen-left in the FRONT panel" where a picture is meant. "His left" / "my left"
    are no longer used alone.
  - **Scale file gets an `asymmetry` list:** `[{"part": "Monocle", "side": "-X", "seen_in":
    ["front", "left"]}]`.
  - **`checklist_from_bible.py`** copies the list into `checklist.md`.
  - **A small check** (in `mesh_report.py` or a new `side_check.py`) reads the built `.blend` and
    confirms each listed part's bounding-box centre is on the declared side of X. A mirrored part
    then fails in seconds, before export.
  - **When two panels of one sheet are mirror images** (the AS-G02 case), `ref_align.py` reports
    it and the plan picks one before building.

### 3. Measure how well the shape matches the views
- **Why:** the video's author fixed sizes in code and checked widths. AU sizes the whole model in
  code, but nothing measures how well each view's outline matches once the views are on one scale.
- **What changes:** `tools/blender/ref_iou.py`, or a `--ref-json` option on `render_views.py`:
  - renders orthographic front/side/back silhouettes at the scale file's pixels-per-stud;
  - computes IoU per view against the aligned views;
  - writes an overlay (reference only = red, model only = blue).
- **Code to reuse:** `ai_mesh_eval.py` already has the silhouette IoU (`REFIOU`), and
  `build_bs_king.py --preview` already draws the overlay.
- **How it is used:** advisory at first. It prints `REF_IOU front 0.87 side 0.81 back 0.85`, goes
  in the evidence log, and is fed to the judge (gap 4). It becomes a gate once the thresholds have
  been seen on 5-10 assets.

### 4. An independent asset judge with its own checklist
- **Why:**
  - The video's judge was a fresh agent that saw the design plus models under shuffled letters
    and scored with its own checklist.
  - AU's look gate is the builder judging itself.
  - Foundry's independent vision approval is built for UI screens and never sees the concept.
- **What changes (Foundry `visual_approval.py` + AU `project.toml`):**
  - **An `asset` rubric** for AS- tasks, selected by a rule such as `match = "AS-*"`,
    `rubric = "asset"`, with reference artifacts `assets/source/*/{id}/ref/*_views_*.png` and
    renders `docs/evidence/{task}/views/*.png`.
  - **Step 1, before seeing the model:** the judge gets the aligned reference pack, the bible
    must-pass list and the short note, and writes **its own checklist**: parts, side (+X/−X),
    colour, proportion, small details.
  - **Step 2:** it gets the 8 renders + close-ups + Studio captures + the `REF_IOU` overlay and
    scores each item: present, right side, right size, right colour.
  - **Shuffled letters** whenever there is more than one candidate (rework passes, or gap 9's
    second candidate), so the judge doesn't know which is newest.
  - **Same safety as today:** fail-closed, bound to the revision and artifacts, never paid twice,
    vision route verified. The builder's own look gate stays as a cheap first pass; this judge
    replaces nothing in Victor's batch review.
- **Sequencing:** project.toml changes go live through a deploy, after the Lead check (memory
  rule).

### 5. Draw the missing sides (turnaround) through foundry-media
- **Why:** the four-picture failure in the video came from a side with no direct view. AU sheets
  have one side panel, and quadrupeds often hide the legs' attachment points (the bible already
  asks for top/underside views "when the three main views conceal them").
- **What changes:**
  - **A turnaround recipe in the `foundry-images` skill** (and, if needed, a small option in the
    media adapter). It follows the video's order:
    1. a three-quarter view first, from the approved sheet (`edit_image` with the sheet as
       reference);
    2. then each missing view (other side, top, underside), each call given the approved sheet +
       the new three-quarter view as references, on a fixed canvas, plain background, same pose.
  - **The video kept one chat** for consistency. foundry-media calls are separate, so the
    approved sheet + the three-quarter view are passed as references each time instead.
  - **The output always goes through `ref_align.py`** (gap 1). Sizes are never trusted from the
    picture.
- **Needs a decision on the 2D hold** (D3 below). Today every sprint with `images` waits for
  Victor.

### 6. A short note instead of a long spec
- **Why:** where text and pictures disagreed, the video's builder followed the text's numbers. A
  short note that adds only what pictures can't show worked best.
- **What changes:**
  - **AU's plan / build-spec template gets a "note" section** limited to: target dimensions (with
    the bible source), the palette (hex), details the views don't show clearly, and the rig and
    budget.
  - **Shape and placement point to the views** instead of being re-described.
  - **Before building, the plan lists every place where the spec and the views disagree** and
    says which one wins.
- **Existing specs** (e.g. `BS-KING_BUILD_SPEC.md`) stay as they are; the template applies to
  new plans.

### 7. Two small skills, like the video's
- **`au-blender-asset`** (AU kilo skill): the conventions (+Y front, studs, 1 material, budgets),
  the one-asset loop, the `foundry-blender` line, the reference pack, sides by axis, and the
  §7.6 traps. It is loaded by AS- sprints and replaces reading all of §7 each time.
- **The turnaround section in `foundry-images`** (gap 5).
- **Housekeeping, separate from this plan:** decide whether the 11 untracked AU skills in the
  OneDrive checkout should be committed. Today `_base` and the live deploy have only
  `au-fast-lane`.

### 8. A replay of every build
- **What changes:** `foundry-blender` (`slot_blender.py`) keeps a short journal per run:
  - the builder script's hash and a diff from the previous run;
  - the args;
  - the `mesh_report` / `REF_IOU` lines;
  - small thumbnails of the 8 renders.
- **Where it shows:** as a strip on the task page and as a link on the batch review card, so
  Victor (and the judge) can see how a model got where it is. Only small files; the big outputs
  still come back to the worktree as today.

### 9. One fresh builder per model
- **Why:** the video's builder started fresh for each model with everything it needed at once. AU
  sprints mix authoring with Studio, upload and evidence work in sessions of up to 240 min, which
  costs context and cache (memory: cache busts cost minutes each).
- **What changes:**
  - **A build sub-step per model:** a fresh session with its own scratch folder gets the
    reference pack + short note + the builder task. It stops at `mesh_report` PASS + renders +
    `REF_IOU`.
  - **The sprint session** then runs the judge, export, upload, Studio and records.
  - **Optional:** when the judge scores below a threshold, run a second candidate on the slot's
    spare CPU and let the judge pick under shuffled letters.
- **Rollout:** start as a trial on 2 assets (D4) before changing the engine for all AS- sprints.

### 10. Model choice
- The video's builder was a frontier model on high effort.
- AU moved asset sprints to local Qwen on 2026-10-28 and back to frontier on 2026-10-30.
- **Recommendation:** keep authoring on frontier. Local models can run the scripted steps (align,
  render, report, export) or be the second candidate in gap 9 later.

### 11. Asset library path
16 AU tools hard-code `C:\Roblox\AssetLibrary`. The slot shim works today, but the proper fix
(every tool reads `AU_ASSET_LIBRARY`) is a small AU sprint item that was already noted in
SLOT_BLENDER_PLAN B2b.

---

## Proposed order

1. **Phase A, AU tooling sprint** (e.g. "AS-P9 reference packs"): gaps 1, 2, 3 + backfill of the
   queued assets' sheets + gap 11. Pre-code it, then queue it; the AU repo is changed only by
   that sprint.
2. **Phase B, Foundry release(s):** gap 4 (asset judge), gap 7 (skills), gap 8 (replay), gap 5
   (turnaround recipe).
3. **Phase C, pilot:** rebuild one asymmetric asset that is already done (e.g. the AS-J03 lever
   prop or AS-G02's cleaver), the old way vs the new way. The judge scores both under shuffled
   letters; Victor compares on the review page.
4. **Phase D** (only if the pilot shows a gain): gap 9 (fresh builder per model, optional second
   candidate) for all AS- sprints. Gaps 6 and 10 ride along in plan templates and config.

## Decisions for Victor

**Decided 2026-11-01:** Victor approved all five as recommended ("Yes, recommended"):
- **D1:** `ref_align.py` writes calibrated scale files.
- **D2:** new views are checked by the tool and the asset judge, then go on the batch review card.
- **D3:** a standing OK for reference views drawn from an approved 3D sheet.
- **D4:** a two-asset trial of the fresh builder per model.
- **D5:** a second candidate only below the judge's threshold.

- **D1. Scale files written by a tool:** `ref_align.py` writes `_ref_vN.json` as `calibrated`
  from the measured silhouette + the bible's target size. The bible §2.5 method allows that.
  *Recommended.* The alternative, Victor/ChatGPT writing each file by hand, is why there are 0
  today.
- **D2. Approving new views drawn from an approved sheet:** checked by `ref_align.py` + the asset
  judge, and shown on the batch review card. *Recommended.* The alternative is Victor approving
  each new view before the build.
- **D3. The 2D hold for reference views:** a standing OK for "views drawn from an already-approved
  3D sheet". *Recommended.* Icons, UI art and textures still ask as today.
- **D4. Fresh builder per model:** trial on 2 assets first. *Recommended.*
- **D5. Second candidate:** only when the judge's score is below a threshold. *Recommended.* The
  alternative, always 2 candidates, doubles builder cost.

## Caveats
- The video's sample is small (3 runs × 4 objects, hard-surface props). It also had one case
  (the excavator) where the text-only model looked better to the author.
- Fine details (dials, needles) stayed weak in every mode. AU's faces are separate textures, which
  sidesteps the worst of that.
- Rig, animation and Roblox import checks are outside the video's scope; AU's existing gates for
  them stay as they are.
- None of the AU numbers above were re-measured. They come from the files and summaries as
  committed on 2026-11-01.
