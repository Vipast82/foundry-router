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

## Images and audio from persona MCP tools (any chat client)

When a persona's MCP tool (ComfyUI, Kokoro, Chatterbox, Stable Audio,
ACE-Step, a screenshot tool…) returns an image or audio file, Foundry shows it
to the person chatting, in any client, on the brain path (plain chat) and the
direct path (the client brings its own tools):

1. **Stored.** MCP image/audio blocks, image/audio resource blobs, and media
   URLs in the tool's text reply are saved as a `media_jobs` row (`op: tool`)
   with a SHA-256, under `<data dir>/media/<id>/`. A URL is fetched only from
   **that MCP server's own host** (plus `tool_fetch_hosts`), so a link a model
   or a web page wrote is never fetched. The file type comes from the bytes,
   not from what the server claims. Limits: `tool_output_max_mb` per file,
   `tool_output_max_total_gb` for all tool files (oldest go first), and
   `retention_days`. Tool files never count against the image budget.
2. **Signed link.** `GET /media/a/<token>` serves that one file with no key.
   This is **the one deliberate unauthenticated read in Foundry**: a browser
   rendering a chat's markdown image sends no Foundry key. The token is an
   HMAC-SHA256 over (file, expiry), with a secret kept in the database, and it
   expires after `public_link_ttl_hours` (7 days). Anyone holding the link can
   load that file until then, so treat links like the files themselves. A bad,
   expired or pruned token is a bare 404. There is no listing. Responses carry
   the right content type, `nosniff`, `inline`, and support Range requests so
   audio players can seek. Links use the request's host (proxy headers
   respected), or `public_base_url` when set.
3. **In the reply.** After the model's text, Foundry adds `![<tool> image](link)`
   per image and `🔊 [<tool> audio (wav, 1.2 MB)](link)` per audio file (plus
   `<audio controls>` when `audio_html` is on). Only `content` changes: tool
   calls handed to the client are untouched, and JSON-format requests and
   compaction summaries get no links. The model sees the text note
   (`[image: image/png, 512 KB]`), a line saying the file is already shown,
   and the image itself when it has vision. It never sees the link, so it
   can't repeat or garble it. Answers with links skip the semantic cache.
4. **Foundry-MCP (`/mcp`).** Clients that attach Foundry as an MCP server still
   get the real image/audio content, plus a text block with the link for
   clients that drop MCP media.

### Long-running generation tools

Mark the server `long_running: true` (ComfyUI, Stable Audio: one call can take
10+ minutes). A call then runs as a background **tool job** (`op: tool_job`):

- The chat request waits up to `tool_inline_wait_seconds` (90, under
  AnythingLLM's ~120 s cut-off) and streams progress as thinking lines (the
  server's own progress messages, otherwise "still working · 1m30s").
- Finished in time: a normal result with the links.
- Still running: the model gets the job id and the reply shows
  `⏳ comfyui/run is still running (job img_…) — ask me to check on it.` The
  persona gains the Foundry-owned tool `tool_job_status(job_id, wait_seconds ≤ 60)`.
  It long-polls and never resubmits. When the job is done, it returns the
  tool's text and the reply gets the links.
- The same call from the same client while one is still running returns that
  job, so a second generation never starts. A client sees only its own jobs. A
  restart marks running jobs interrupted. They are never re-run.

ACE-Step already hands back its own job id (`check_music_job`). Its "done. URL:"
reply is fetched and linked like any other tool result.

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
