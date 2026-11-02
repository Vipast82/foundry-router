---
name: au-blender-asset
description: Build, check and ship one Animal Uprising 3D asset (model, faces, clips) with the headless Blender tools - the house conventions (+Y front, studs, budgets), the one-asset loop, the reference pack and sides by axis, how Blender is run (foundry-blender / the pool), and the 3D traps. Load it at the start of every AS- 3D sprint instead of re-reading all of docs/ASSET_PIPELINE.md section 7.
---

# AU Blender asset

> Source of truth: `foundry_coordinator/projects/animaluprising/kilo_skills/au-blender-asset/`
> in Foundry-Router (asset-pipeline review gap 7, 2026-11-01). Foundry installs it into
> `.kilo/skills/` when `skills.install` lists it. The full runbook stays
> `docs/ASSET_PIPELINE.md` section 7: when this card and the runbook disagree, the runbook wins.
> Report the disagreement.

## Conventions (the tools enforce them)
- 1 Blender unit = 1 stud. The asset **faces +Y**, Z up, base centre at the origin.
- Transforms applied (scale 1, rotation 0).
- At most 1 material per mesh, 20,000 tris per mesh and 4 bone influences.
- `REF*` and `RV_*` objects are ignored by every tool.
- Named parts = the Studio MeshPart names (`Body`, accessories, `Face`).
- **Sides by axis, never by "left/right":** in the FRONT panel of a concept sheet, the model's
  **+X is on screen-left**. Write `+X` / `-X` in plans, checklists and notes. "His left" and
  "my left" are not used alone.

## How Blender runs
- If the kickoff has a **BLENDER RUNS ON A POOL** / **ON STUDIO SLOT** line, use its command
  wherever ENVIRONMENT.md says blender.exe, with the same arguments. The plain blender.exe is
  denied.
- Several models: one guard run per model with `--bg` (its own `--name`, its own output
  paths), then `py -3 tools\guard\run.py --wait <log>` for each. Exit 75 = every worker was
  busy: retry once, then report it.
- Every `--out` is an **absolute** path inside the worktree.
- Set `PYTHONIOENCODING=utf-8` for the tools that print `≤` or `—`.

## The one-asset loop (section 7.5)
**A. Before modelling**
1. `python tools\assets\checklist_from_bible.py <ID> --sprint <SPRINT> --force`. Fill in the
   goal and the parts list, and open the concept PNG with vision.
2. **Reference pack:** `python tools\assets\ref_align.py <ID> --targets <targets json>`, then
   `ref_setup.py --json <ID>_ref_vN.json`.
   - Exit 0 = a calibrated pack. Its `asymmetry` list names the sided parts.
   - Exit 2 = UNSCALED CONCEPT: write it in `checklist.md` and model to the brief's target
     (bible class × R15 5.2). Never measure the drawing.
   - A sheet on the redraw list (`docs\progress\AS-P9_ref_backfill.md`): the missing views are
     drawn with the `foundry-images` "Turnaround views" recipe only when the plan lists
     `ref_views`. Otherwise use what is there.
3. **A short note, not a long spec:** sizes (with the bible source), palette hex, details the
   views don't show, rig and budget. Before building, list every place where the spec and the
   views disagree and say which one wins.

**B. Build and look**
1. A builder per asset: `tools/blender/build_<id>.py` (pattern: `build_en_wboar.py`). It has a
   plain-Python geometry core (`--plan`, `--preview`, a selftest with negatives) and a bpy half
   (`--save`, `--selftest`). Sizes are constants in the code.
2. Iterate on `--plan` / `--preview` (seconds, no Blender). Then the real build, then
   `mesh_report.py` PASS (budget, size ±10 %, pivot, transforms).
3. Faces: `face_textures.py`. Its `plate` must equal the builder's Face box.
4. `side_check.py` PASS when the pack lists sided parts. The `ref_iou.py` numbers go into the
   evidence and the look gate (advisory: never pass `--min` in a gate).
5. `render_views.py -- --out <absolute dir>`, then `contact_sheet.py ... --refs <concept png>`.
   Keep the previous pass's renders for a before/after strip.
6. **Look gate:** concept vs renders vs `mesh_report` numbers vs the bible must-pass list, with
   at most 2 rework passes. The independent asset judge looks after you. Your gate is the first
   pass, not the verdict.

**C. Export and upload** (standing upload OK for models, faces and clips)
1. `export_fbx.py -- --out <abs assets\exports\<ID>.fbx> --objects <parts>` → `EXPORT_FBX PASS`
   with the `turned 180°` line. Never save the `.blend` after it.
2. `roblox_cloud.py upload-batch --manifest <json> ...`. Every item must be
   `moderation=Approved` before going on.

**D. Studio checks** (Roblox_Studio MCP, one `execute_luau` per step)
- First take the studio lock with
  `tools/coord/lock.py acquire studio <ID> "<what>" --owner <session id>` in its own call.
  Release it right after.
- `studio_asset_check.luau`:
  - `prep` → `ASSET_CHECK <ID> PASS`;
  - `camera` + `capture-window.ps1 -Evidence` for front / side / back / q34 (`VALID=OK`);
  - **`cleanup`, always.**

**E. Records**
- Raw lines go into `docs/progress/<SPRINT>_EVIDENCE_RAW.log`.
- Commit the evidence PNGs.
- Add an `assets/models/model_ids.json` entry → `gen_assetids.py` (never hand-edit
  `Config/AssetIds.lua`).
- Run `ledger.py set ...` and refresh the review-page card.

## Traps (section 7.6, the ones that cost the most)
- `export_fbx.py` replaces the open scene with the re-imported FBX: run it last, never `--save`
  after it.
- `blender -b <file> -P tool -- args`: the `.blend` goes **before** `-P`.
- Imported MeshParts carry a 90° X rotation in `PivotOffset`. Reset it before
  `GetBoundingBox` / `PivotTo`.
- `CollisionFidelity` writes from the MCP are ignored silently. Use
  `CreateMeshPartAsync(..., {CollisionFidelity = Box})` + `ApplyMesh`, then restore `TextureID`
  (`studio_asset_check.luau` does this).
- Fresh uploads take about a minute before their faces show. Preload with retries before
  capturing.
- On a track bound to a Studio slot, `capture_screenshot` shows you the image but saves nothing,
  and `tools/capture-window.ps1` captures the workstation's Studio, never the slot. Frame the
  camera with `execute_luau`, then save the frame with
  `python tools/assets/slot_capture.py docs/evidence/<ID>/studio_front.png` (one call per view;
  it refuses to run outside a slot track).
- `tools\guard\run.py` with a quoted exe path: use `--shell none`.
- Blender's bundled Python has numpy but no Pillow: Pillow steps run with the system `python`.
- A mirrored part (AS-E10 grill, AS-J03 lever, AS-G02 cleaver): check it with the `asymmetry`
  list and `side_check.py` before export, not in the look gate.
