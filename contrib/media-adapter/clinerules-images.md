# Image generation (foundry-media)

- New or changed image files come from the `foundry-media` MCP tools (`generate_image`, `edit_image`), never from writing base64 by hand.
- Follow the asset bible and approved references. Give a stable `idempotency_key` (`<sprint>-<step>-<asset>`). Use `model: "draft"` for concepts and `"final"` for deliverables.
- If a call returns `status: running`, poll with `image_job_status`. Never resubmit the same request.
- A `budget_denied` or `quota_exceeded` refusal is final: report it with the reset time and don't try another model.
- Saved images are UNAPPROVED (see `<file>.foundry.json`). Don't wire them into the game, upload them or replace approved assets until the user approves them visually.
