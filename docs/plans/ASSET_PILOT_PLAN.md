# Phase C pilot: rebuild the AS-J03 lever the new way, judged blind against the old one

**Status:** started 2026-11-01 (Victor: "Yes, start the pilot"). Built with the recommended
answers below.
**Source:** `reports/ASSET_PIPELINE_REVIEW_2026-11.md` Phase C. It runs on AU after AS-P9
(COMPLETE), with Phase B's Foundry releases (v0.167.0 judge, v0.171.0 turnaround, v0.173.0
skills, v0.168.0-v0.175.0 build replay).

## The asset: `PRP-KING-PROPS_Lever` (AS-J03's trap lever + warning beacon)
Why this one and not AS-G02's cleaver:
- **It is asymmetric where it matters.** The handle leans to one side at rest, and the beacon
  stands on one side of the switch box.
- **It has a known left/right risk.** Its builder says `LEVER_REST = 45.0 # handle leans 45°
  to the right (+X)`. Screen-right in the FRONT panel is **-X** by the house rule. AS-P9
  side-checked only the beacon (`SIDE BeaconLight -X PASS`), not the handle.
- **No owner decision bends its sides.** The cleaver was built to its IN HAND panel on purpose
  (AS-G02 D5), the mirror of its FRONT panel. A FRONT-based reference pack would then count a
  documented decision against the old model and skew the comparison.
- **It is small:** 3 parts, 672 tris, one sheet row with FRONT, SIDE and BACK panels.

## The pilot sprint (AU `AS-P10`, pre-coded as a prep patch)
- **STEP 0:**
  - `side_check.py` on the shipped `PRP-KING-PROPS_Lever.blend`, with Handle and BeaconLight
    listed by axis from the FRONT panel;
  - `ref_iou.py` on the same file.
  - This is the old model's measured baseline.
- **The new way:**
  1. **A reference pack** from the sheet's row 1 (`rects` in `AS-P10_targets.json`). If it
     fails, a redrawn `sheet` through the turnaround recipe.
  2. **Turnaround `tq_+X` and `top` views** for what the panels hide (where the beacon stands,
     the handle's lean seen from above), under the D3 standing OK.
  3. **A short note:** sizes with sources, palette, hidden details, the model contract, and
     the conflicts listed before building.
  4. **A fresh builder** (`build_prp_lever_p10.py`) written from the pack + note with the
     `au-blender-asset` skill. It may use the shared helpers, never the old `lever()`.
  5. Then `mesh_report`, `side_check` PASS, `ref_iou` and renders.
- **The old way:** the shipped model itself, re-rendered with the same `render_views.py`
  settings. Rebuilding it the old way would only repeat AS-J03.
- **Blind A/B:**
  - a coin from the approved sheet's SHA-256 decides which model goes to
    `docs/evidence/AS-P10/cand_A/` and which to `cand_B/`;
  - the mapping is written to `docs/progress/AS-P10_ab.json` (the judge never reads it) and
    is not in the plan.
- **Out of scope:** no export, upload, Studio or ledger change. If Victor picks the new
  candidate, a follow-up sprint ships it.

## Foundry change: the judge scores candidates (v0.178.0)
The v0.167.0 judge scores one set of images per task. An asset rule may now name
`candidates`: artifact globs, one per candidate.
- One checklist, written once from the design.
- One blind score call per candidate: only that candidate's images, letters shuffled per
  candidate, no file names, and a line saying it shows ONE candidate.
- Event `asset_judge_candidates`: per candidate, its score, verdict, findings and files.
- **The sprint's verdict** comes from the best candidate: approved first, then the most items
  passed, then the fewest findings. A CHANGES verdict sends the best candidate's findings back.
- A rule whose candidates don't have images in at least two folders scores as before. Rules
  without `candidates` are unchanged, and the single-model prompt text is byte-identical.
- This is also the base for D5 (a second candidate below a score threshold) after the pilot.

## Decisions (recommended answers, used)
- **D-C1. Asset:** the AS-J03 lever, not the AS-G02 cleaver (reasons above). *Recommended.*
- **D-C2. The old way:** the shipped model re-rendered, not rebuilt. *Recommended:* it is what
  the old way produced; a second old-way build costs a sprint and proves nothing new.
- **D-C3. Blindness:**
  - separate blind calls per candidate, against one checklist;
  - the A/B mapping is decided at run time by a coin and kept out of what the judge reads.
  - *Recommended:* the judge can't anchor on the other candidate or on the history. Victor
    compares A and B on the review page before opening `AS-P10_ab.json`.
- **D-C4. Turnaround views:** two images (`tq_+X`, `top`) under D3, and a redrawn `sheet` only
  if the row-1 panels fail `ref_align.py`. *Recommended:* it exercises gap 5 on something the
  panels really don't show, at a small, capped cost.
- **D-C5. No shipping in the pilot.** *Recommended:* the pilot answers "is the new way better?".
  Shipping the winner is a follow-up with its Studio checks.

## What Victor switches on, and when
**Before AS-P10's first session** (Settings, one Review & deploy):
1. `skills.install` = `au-blender-asset, foundry-images`. The new way's skill, plus the
   turnaround recipe in AU's copy of `foundry-images`.
2. `automation.ref_views_images` = `allow`. Without it the sprint waits for the 2D decision;
   answering "Generate them" also works.
3. `approval.asset_judge` = on.
4. One approval rule added to `approval.rules` (the existing visual rule stays):

```json
{"match": "visual", "kind": "visual", "stage": "after_close", "rubric": "asset",
 "artifacts": ["docs/evidence/{task}/cand_*/*.png"],
 "candidates": ["docs/evidence/{task}/cand_A/*.png", "docs/evidence/{task}/cand_B/*.png"],
 "references": ["assets/source/*/{asset}/ref/{asset}_Lever_views_v*.png"],
 "notes": ["docs/progress/AS-P10_note.md"],
 "scope_any": ["docs/evidence/*/cand_A/*"],
 "asset_from": ["^assets/source/props/(PRP-KING-PROPS)/"],
 "max_frames": 8}
```
`scope_any` limits the rule to a plan that lists a `cand_A` folder, so today only AS-P10.

**After the sprint closes:**
1. Compare A and B on the batch review page (card `PILOT-LEVER`) and pick one.
2. Then open `docs/progress/AS-P10_ab.json`.
3. Then read the judge's `asset_judge_candidates` event on the task page.

## Tests
`tests/coordinator/test_asset_judge.py`:
- the profile parses `candidates`;
- each candidate is scored in its own blind call (it sees only its own images; no folder
  names), the comparison is recorded once, the best decides, and a second pass pays nothing;
- one candidate with images is judged as before;
- the ranking order.
