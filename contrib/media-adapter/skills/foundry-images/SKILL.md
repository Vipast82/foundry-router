---
name: foundry-images
description: Generate or edit game art (icons, UI art, textures, concept sheets) through Foundry's image tools (foundry-media MCP server) and stage the results as unapproved assets. Use only when the plan's `## Needs` lists `images` and the coordinator's kickoff says the owner allowed image generation for this sprint; not for reading screenshots.
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
