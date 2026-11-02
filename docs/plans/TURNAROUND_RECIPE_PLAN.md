# Turnaround recipe: draw an asset's missing views from its approved sheet (gap 5)

**Status:** built with the recommended answers (Victor 2026-11-01: "go ahead and plan everything
and then build it"). Ships off: `[automation] ref_views_images = "ask"`.
**Source:** `reports/ASSET_PIPELINE_REVIEW_2026-11.md` gap 5 (and D2, D3 approved 2026-11-01).

## Why
AU concept sheets show FRONT, one SIDE and BACK. The other side, the top and the underside are
guessed, and the video's four-picture failure came from exactly that: a side with no direct
view. The video drew a three-quarter view first, then the missing sides, all from the approved
design. AU can do the same through foundry-media (ChatGPT images), but today:
- the `foundry-images` skill has no recipe for reference views;
- every sprint that needs `images` waits for Victor's 2D decision, even though D3 gave a
  standing OK for "reference views drawn from an already-approved 3D sheet";
- a saved image's sidecar doesn't say which pictures it was drawn from, so the batch review card
  (D2) and the asset judge can't show where a view came from.

## What changes
1. **`foundry-images` skill: a "Turnaround views" section.**
   - The order: a three-quarter view first (from the approved sheet), then each missing view
     with the approved sheet + the three-quarter view as references.
   - Views are named by axis (the asset faces +Y; in the FRONT panel the model's +X is on
     screen-left, as in AS-P9's `ref_align.py`). The section has a table: `tq_+X`, `side_+X`,
     `side_-X`, `back`, `top`, `under`, plus `sheet` (a redrawn FRONT | SIDE | BACK row).
   - Fixed settings for every view: `edit_image`, `model: "final"`, `quality: "high"`,
     `input_fidelity: "high"`, `background: "opaque"`, a plain flat grey background, no floor,
     no shadow, no text, one image per call, a fixed canvas (1024x1024; 1536x1024 for `sheet`).
   - It saves under `assets/source/<cat>/<ID>/ref/drawn/` as `<ID>_<view>_d<n>.png`. It never
     overwrites or renames an approved `<ID>_ref_v<n>.png`.
   - A stable `idempotency_key` per view: `<TASK>-<ID>-turn-<view>-d<n>`.
   - It is capped at six images per asset per sprint (three-quarter + four views + one sheet),
     with at most one redraw per view.
   - **Checking:** when `tools/assets/ref_align.py` exists (AS-P9), the sprint runs
     `ref_align.py <ID> --sheet <drawn sheet> --check-only`. A drawn sheet that passes may then
     be aligned for real (it writes `<ID>_ref_v<n>.json`). Until AS-P9 lands, the recipe stops
     after saving and lists the files. Sizes are never measured from a drawn picture alone.
2. **The D3 standing OK, as a setting (ships off).**
   - A plan opts in by listing `ref_views` in its `## Needs` block, beside `images` (or in task
     data `ref_views: true`, or a `needs:ref_views` tag).
   - New `[automation] ref_views_images`: `ask` (default) or `allow`.
     - `ask`: as today. The sprint waits for the 2D decision.
     - `allow`: a sprint that declares `ref_views` starts without that decision, and its
       session gets the image tools. Event `ref_views_standing_ok` once per task.
   - The BUILD prompt's 2D paragraph then says the OK covers only reference views drawn from
     the approved sheet. Icons, UI art, textures or decals in the same sprint are not covered:
     the session reports BLOCKED `user_action` for them.
   - `media_gen_images = "deny"` still wins (no images at all), and `allow` still allows all.
3. **foundry-media adapter (1.1.0): provenance in the sidecar.**
   - `generate_image` / `edit_image` take optional `purpose` (e.g. `reference_view`) and `view`
     (e.g. `side_-X`).
   - An edit's sidecar records `inputs`: each reference image's path (project-relative when
     inside the project) and SHA-256. `purpose` and `view` are recorded when given.
   - It is metadata only: what is sent to Foundry and the cost are unchanged.

## Decisions (recommended answers, used for the build)
- **D-T1. Where the recipe lives:** a section of `foundry-images`, not a new skill.
  *Recommended:* one image skill keeps the budget, job and duplicate rules in one place.
- **D-T2. Output files:** separate view files plus, only when the approved sheet fails
  `ref_align.py`, one redrawn `sheet`, all in `ref/drawn/`. *Recommended:* the approved sheet
  stays untouched, and what is drawn stays visibly separate until review.
- **D-T3. How D3 is switched on:** the plan's `ref_views` word plus `ref_views_images = allow`.
  *Recommended:* only plans that say so skip the decision, and Victor turns it on in Settings.
  The alternative (every AS- sprint with `images`) would also cover textures and icons, which
  D3 excludes.
- **D-T4. Provenance:** the adapter records the input images and the view in the sidecar.
  *Recommended:* the review card and the judge can show "drawn from BS-KING_ref_v2.png
  (sha …)".
- **D-T5. Image cap:** six per asset per sprint, one redraw per view, in the skill text.
  Foundry's media budget still applies on top. *Recommended.*

## Not in this release
- `ref_align.py` itself (AS-P9, an AU sprint).
- Showing the sidecar's provenance on the batch review card: the card reads it when the judge
  (v0.167.0) or a later release adds the row.
- Installing the updated skill into AU's Kilo: gap 7's installer
  (`docs/plans/FOUNDRY_SKILLS_DELIVERY_PLAN.md`) does that. Until then AU's copy is the hand
  copy in `C:/Roblox/AnimalUprising/.kilo/skills/foundry-images/`.

## To switch it on (Victor)
1. Rebuild the router, so Settings shows `automation.ref_views_images`.
2. Settings → Decisions → `ref_views_images` = `allow`, then Review & deploy.
3. Pull the OneDrive checkout (the media adapter runs from it). Kilo restarts the adapter with
   its next session.
4. The updated skill reaches AU through gap 7's installer once that is on.

## Tests
- `tests/coordinator/test_ref_views.py`:
  - the `ref_views` word is found in a Needs block, task data and tags;
  - `ask` keeps the hold, and `allow` starts the sprint with no decision opened;
  - a sprint without `ref_views` still waits;
  - `deny` beats the standing OK;
  - the brief names the limits, and the event fires once.
- `tests/test_media_adapter.py`: the sidecar records `inputs` (path + SHA-256), `purpose` and
  `view`, and the request sent to Foundry is unchanged.
- The skill text: the section exists, names every view and the `ref/drawn/` folder.
