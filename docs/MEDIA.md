# Images: generate_image, edit_image and artifacts

Foundry runs image generation and editing as **tracked jobs** on one
configured backend: normally CLIProxyAPI, signed in to your ChatGPT plan
(`gpt-image-2.5-sunburst` / `gpt-image-2.5-flare`). The results are files with
ids, sizes and SHA-256 hashes.

| Client | How it gets images |
|---|---|
| **Kilo / Cline / Claude Code** (coding) | the **local media adapter** (`contrib/media-adapter`) — an MCP server on your PC that saves results **into the project**, in folders you authorise |
| **OpenWebUI** | Settings → Images → engine *OpenAI*, base URL `http://<foundry>:11435/v1`, key = a Foundry client key with the `media` scope, model `gpt-image-2.5-sunburst` |
| **AnythingLLM** and other MCP-capable chat clients | the Foundry-MCP endpoint `http://<foundry>:11435/mcp/media/` (tools `generate_image`, `edit_image`, `image_job_status`, `cancel_image_job`) |
| scripts | `POST /v1/images/generations` / `/v1/images/edits` (OpenAI format), or the job API below |

## The rules Foundry enforces

- **Only allowed models, only on the configured backend.** `media.models` is the
  allowlist; `final` and `draft` map to `default_model` and `draft_model`.
  An image persona's name also works as the model (its first image model is
  used). A model that isn't allowed, isn't offered by the backend, or is over
  budget **fails with a clear error**. It never falls back to another model,
  another provider, or an API billed per call (`allow_paid_api` is off).
- **No duplicate images.** Send `idempotency_key` (the adapter's skill suggests
  `<sprint>-<step>-<asset>`) and the same key always returns the same job. Even
  without a key, the same request (client, model, prompt, settings, input
  images) within `dedupe_minutes` (30) returns the existing job. Pass
  `allow_duplicate: true` for a deliberate variation. The OpenAI endpoints only
  dedupe on an explicit key or `Idempotency-Key` header, because chat UIs
  regenerate on purpose.
- **Bounded retries, no resubmission on timeout.** A job retries
  (`max_attempts`, default 2) only when the request provably never started an
  image: a connect failure, or a 502/503 from the proxy. A timeout fails the
  job with "not resubmitted". Polling a job never submits anything.
- **A separate image budget.** `jobs_per_5h` (20), `jobs_per_day` (60),
  `client_jobs_per_day` (off), `max_concurrent` (1) and `max_queued` (8). A job
  counts once it reaches the backend. The CLIProxyAPI usage limit (85% of a
  ChatGPT window, see docs/CLIPROXYAPI.md) applies to image models too.
  Refusals are HTTP 429 `budget_denied` with the reset time.
- **No base64 in coding conversations.** Status replies carry paths and URLs.
  Bytes are only returned when a client asks for them: OpenAI's `b64_json`
  (the OpenAI default), or MCP `return_image: true` for chat clients that
  display images.
- **Private jobs.** A client key sees and downloads only its own jobs; an
  admin-scoped key sees all of them. Input images are never stored. Outputs
  live under `<data dir>/media/<job id>/` and are pruned after `retention_days` (30).
- **Restarts** mark in-flight jobs `failed (interrupted)`. They are never
  resubmitted automatically.
- **Image models never serve chat.** CLIProxyAPI lists `gpt-image-*` in
  `/v1/models`. Foundry flags those models so chat routing never picks one,
  and the Registry shows an "image" pill.

## Configure

```yaml
media:
  backend: ""                 # "" = the CLIProxyAPI backend
  models: [gpt-image-2.5-sunburst, gpt-image-2.5-flare, gpt-image-2.5, gpt-image-2]
  default_model: gpt-image-2.5-sunburst
  draft_model: gpt-image-2.5-flare
  jobs_per_5h: 20
  jobs_per_day: 60
  max_concurrent: 1
clients:
  - name: kilo
    key: "${FOUNDRY_KEY_KILO}"
    scopes: [inference, media]
```

Logs → Usage Log → **Images** shows budget use and each model's last success
(a green model has completed at least one real job: discovery alone isn't
proof). It also has the settings and the recent jobs with thumbnails, and can
cancel a running job.

To check a model end to end before relying on it, run one `draft` job from the
adapter (`python foundry_media_mcp.py --check` prints which models have
succeeded).

## Job API (what the adapter uses)

| | |
|---|---|
| `POST /media/jobs` | `{op: generate\|edit, prompt, model, size, quality, background, output_format, n, images: [data URL], mask, input_fidelity, idempotency_key, allow_duplicate, wait_seconds ≤ 60}` → 202 + job (200 + `deduplicated` for an existing one) |
| `GET /media/jobs/{id}?wait=N` | status; long-polls up to 60 s |
| `POST /media/jobs/{id}/cancel` | a queued job is never sent; a running one may still be counted upstream |
| `GET /media/jobs` | this client's recent jobs |
| `GET /media/artifacts/{id}/{n}` | the image (`x-content-sha256` header) |
| `GET /media/models` | allowed models, final/draft, backend, budget use |

Responses from `/v1/images/*` carry `x-foundry-media-job`,
`x-foundry-served-model` and `x-foundry-served-backend`.

## The local adapter (Windows or Linux)

See `contrib/media-adapter/README.md`. In short:

```powershell
# Windows
.\install.ps1 -ProjectRoot C:\Roblox\AnimalUprising -SetKey
```
```bash
# Linux
./install.sh --project ~/Roblox/AnimalUprising --set-key
```

Each installer prints the MCP entries for Kilo and Cline. Copy the skill
(`skills/foundry-images`) into the project's `.kilo/skills/`.
