---
name: foundry-images
description: Generate or edit game art (icons, UI art, textures, concept sheets) and draw a 3D asset's missing reference views from its approved sheet (the turnaround recipe) through Foundry's image tools (foundry-media MCP server), staging the results as unapproved assets. Use only when the plan's `## Needs` lists `images` and the coordinator's kickoff says image generation is allowed for this sprint (the owner's answer, or the reference-view standing OK); not for reading screenshots.
---

# Foundry images

The `foundry-media` MCP server sends image jobs to Foundry (ChatGPT images) and
saves the results inside this project. You get file paths back, not image data.

Only when the kickoff says the owner allowed 2D generation for this sprint (the
coordinator turns these tools on then, and off otherwise). This path comes
first; Media_Gen's image tools are the fallback when foundry-media refuses or
fails.

## Before generating

1. Read the asset bible, the approved references and the checklist for this
   asset type. Use the established asset IDs and naming.
2. Decide on the **exact** output: size, background (transparent for icons and
   UI elements), format (png unless told otherwise), and the destination path.
3. Call `image_settings` once per session to see the authorised folders, the
   default staging folder and the remaining image budget.

## Generating

- Concept exploration: `model: "draft"` (fast, cheaper). Final assets:
  `model: "final"` (the default).
- Put everything needed in `prompt`: subject, style from the asset bible,
  composition, colours, and what must not appear.
- Give a stable `idempotency_key` such as `UI-01-step3-coin-icon`. Repeating
  the same key returns the same job instead of making another image.
- To change an existing image, or to match a reference, use `edit_image` with
  `images: [paths]`. Add `mask` (a PNG whose transparent pixels mark the area
  to change) to edit only part of an image.
- Leave `save_as` empty to stage in the default `_unapproved` folder, or give a
  path inside the project. Existing files are never overwritten unless you pass
  `overwrite: true`; a `-v1`, `-v2` suffix is added instead.

## While a job runs

- If the reply says `status: running`, do other plan work and call
  `image_job_status` with that `job_id` once per completed step (never a
  polling loop of identical calls). **Never** call `generate_image` or
  `edit_image` again for the same request: that bills a second image.
- A budget or quota refusal (`budget_denied`, `quota_exceeded`) ends the
  attempt. Put it in your report with the reset time, then use Media_Gen's
  image tools if the kickoff allows them; never another model or tool.

## After saving

- Check the saved file: dimensions, transparency and format. Use `preview: true`
  only when a vision-capable model will actually look at the image.
- Every image is **unapproved** (see its `.foundry.json` sidecar). Do not wire
  it into game code, upload it to Roblox or replace an approved asset unless the
  plan says so. List the saved paths in your report; approval follows the
  project's approval process.
- Record the job id and path in the sprint notes, as evidence.

## Turnaround views (reference views from an approved 3D sheet)

Use this when the plan asks for missing views of a 3D asset (the other side, the top, the
underside) or for a redrawn FRONT | SIDE | BACK sheet because the approved sheet fails
`ref_align.py`. A sprint that lists `ref_views` in its `## Needs` may run under the owner's
standing OK: then **only** reference views are covered. An icon, UI art, a texture or a decal
is reported BLOCKED `user_action`.

**Axes (the same as `ref_align.py` and the builders):** the asset faces +Y. In the FRONT panel
the model's +X is on **screen-left**. Never write "his left" or "my left": name sides as +X / -X.

| view | camera | what it shows |
|---|---|---|
| `tq_+X` | front three-quarter, between +Y and +X, slightly above | front and +X side together |
| `side_+X` | on +X looking toward -X | the +X side; the asset faces screen-right |
| `side_-X` | on -X looking toward +X | the -X side; the asset faces screen-left |
| `back` | on -Y looking toward +Y | the back; the model's +X is on screen-right |
| `top` | above, looking down | the top; the front (+Y) at the image top, +X on screen-right |
| `under` | below, looking up | the underside; the front (+Y) at the image top, +X on screen-left |
| `sheet` | FRONT, `side_+X`, BACK in one row, left to right | a redrawn sheet for `ref_align.py` |

**Order (the approved sheet stays the reference every time):**
1. **Three-quarter first:** `edit_image` with `images: [<approved sheet>]` and `view: "tq_+X"`.
2. **Then each missing view:** `edit_image` with `images: [<approved sheet>, <the tq_+X file>]`.
   One view per call.
3. **A `sheet`** only when the approved sheet fails `ref_align.py` (the plan says which):
   `images: [<approved sheet>, <tq_+X>]`, `size: "1536x1024"`.

**Fixed settings for every call:**
- `model: "final"`, `quality: "high"`, `input_fidelity: "high"`, `background: "opaque"`,
  `n: 1`.
- `size: "1024x1024"` (the `sheet` is `1536x1024`).
- `purpose: "reference_view"`, `view: "<view>"`. The sidecar then records the view and the
  input images with their SHA-256.
- `idempotency_key: "<TASK>-<ID>-turn-<view>-d<n>"`.
- `save_as: "assets/source/<cat>/<ID>/ref/drawn/<ID>_<view>_d<n>.png"`. **Never** overwrite,
  rename or replace an approved `<ID>_ref_v<n>.png`.

**Prompt (fill in the brackets, keep the rest):**
> Orthographic reference view of [asset name, from the approved concept sheet], seen
> [camera from the table]. Exactly the same character/object as the attached approved sheet:
> same proportions, colours, materials, parts and pose ([rest pose / T-pose as on the sheet]).
> [For a side: the asset faces screen-right / screen-left.] Whole figure visible and centred,
> feet (or base) on one flat ground line near the bottom, no perspective distortion.
> Plain flat mid-grey background, no gradient, no floor, no shadow, no text, labels, arrows or
> rulers. [Parts this view must show, e.g. "where the four legs attach to the body".]

For a `sheet`, ask for the three views in one row, at the same height, on one ground line,
with clear space between them and no labels.

**Limits:**
- At most six images per asset in one sprint (`tq_+X` + four views + one `sheet`).
- At most one redraw per view. A second bad result is a finding for the report, not a third
  call.

**Checking (sizes are never trusted from a drawn picture):**
- Look at each result once (`preview: true` only with a vision model). Does it show the same
  parts on the same sides as the approved sheet? A part on the wrong side means a redraw.
- When `tools/assets/ref_align.py` exists:
  - run `python tools/assets/ref_align.py <ID> --sheet <drawn sheet> --check-only`;
  - align for real only after it passes;
  - a failing check goes in the report with its numbers.
- Without `ref_align.py`, stop after saving and list the files.
- Report every drawn file with its job id. They stay unapproved: they go to the asset judge and
  the batch review card.
