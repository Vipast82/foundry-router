# CLIProxyAPI: ChatGPT / Codex (and more) as a Foundry backend

[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) signs in to your
**ChatGPT Plus/Pro (Codex)**, **Claude Code**, **Gemini** and other accounts
and serves them behind one OpenAI-compatible endpoint. Foundry treats it like
Meridian: a **paid subscription cloud backend** your personas can escalate to,
for example a Plan persona that cascades Claude → GPT → local.

> It is unofficial. It uses the Codex CLI's sign-in and OpenAI's internal
> ChatGPT endpoints, not the public API, so it can break or be blocked, and
> using a consumer subscription this way may conflict with the provider's
> terms. Keep it for your own use, on your LAN, with an access key.

## What works through Foundry

Checked against CLIProxyAPI v8 (its source) and covered by
`tests/test_cliproxyapi.py`:

| Feature | Status |
|---|---|
| Model discovery (`/v1/models`, Bearer key) | ✅ every signed-in account's models |
| Streaming and non-streaming chat | ✅ |
| Tool calling (client tools, parallel calls, tool history) | ✅ |
| Images (Cline screenshots) | ✅ sent as data-URI `image_url` parts → Codex `input_image` |
| Reasoning in Cline's thinking panel | ✅ CLIProxyAPI returns reasoning summaries when `reasoning_effort` is set, which Foundry does whenever thinking is on |
| Effort scale | ✅ Foundry sends the full Codex scale: Low/Medium/High, **Xhigh/Max → `xhigh`**, **Off → `none`**. CLIProxyAPI clamps each level to what the model supports. |
| Compaction summaries | ✅ thinking forced off (`none`) like every backend |
| Usage (prompt, completion, cached, reasoning tokens) | ✅ |
| Paid-call guardrail, persona paid cascades, cost report | ✅ counted as **subscription**, never as free/local |
| Context guard | ✅ default windows: GPT/Codex 272k, Claude 200k, Gemini 1M (a persona `context_window` or registry override wins) |
| `max_tokens` / `temperature` for Codex models | ⚠ ignored upstream: Codex accepts neither. Replies end when the model is done. |
| Image generation / editing (`gpt-image-2.5-sunburst` / `-flare`) | ✅ as Foundry image jobs: allowlisted models, duplicate protection, image budget, artifacts saved into your project by the local adapter. See docs/MEDIA.md. Image models are never used for chat. |
| Usage windows (ChatGPT 5-hour / weekly, Claude Code 5h / 7d) | ✅ read from CLIProxyAPI's management API (set `management_key`): shown on the Usage tab and in each turn's status line; a model stops being used at **85%** (configurable) so the persona cascade moves on before the plan runs out. See "Usage limits" below. |

## 1. Run CLIProxyAPI

On victor-ai or TrueNAS, using `contrib/cliproxyapi/`:

```bash
mkdir -p ~/cliproxyapi && cd ~/cliproxyapi
cp /path/to/foundry-router/contrib/cliproxyapi/{docker-compose.yml,config.yaml} .
# edit config.yaml: set access.api-keys and management.secret-key
docker compose up -d
```

Sign in to ChatGPT. The device-code flow works headless, inside the container:

```bash
docker exec -it cli-proxy-api ./CLIProxyAPI --codex-device-login
# open the URL it prints on any device, enter the code, sign in with ChatGPT
```

Optional extra accounts: `--claude-login` (Claude Code) or the other `--*-login`
flags. CLIProxyAPI can hold several accounts per provider and rotates between them.

Check it: `curl -H "Authorization: Bearer <your key>" http://<host>:8317/v1/models`

## 2. Add it to Foundry

Backends → Pool → backend list (JSON):

```json
{"name": "cliproxy", "type": "openai-compatible",
 "url": "http://<host>:8317/v1", "api_key": "${CLIPROXY_API_KEY}",
 "flavor": "cliproxyapi", "management_key": "${CLIPROXY_MGMT_KEY}"}
```

`management_key` is CLIProxyAPI's `management.secret-key`. It's optional but
turns on usage tracking (below). The UI shows it masked, like `api_key`.

`flavor: "cliproxyapi"` is what makes Foundry treat it as a subscription
cloud backend and send the full effort scale. Use `openai-compatible` for
**all** CLIProxyAPI models, including Claude ones. Don't add it as
`anthropic-compatible`: that type turns on Meridian-only quota checks that
CLIProxyAPI doesn't answer.

Any other OpenAI-dialect subscription proxy (ChatMock, …) can be marked the
same way with `"subscription": true` (optionally with `call_budget_5h` /
`call_budget_daily` caps, since these plans report no usage to Foundry).

## Usage limits (like Meridian's usage window, for ChatGPT)

With `management_key` set, Foundry reads the usage each signed-in account
reported to CLIProxyAPI (`GET /v0/management/auth-files`):

- **ChatGPT/Codex**: the 5-hour and weekly windows (% used, reset time), the
  plan, and any named per-model limits (e.g. GPT-5.3-Codex-Spark).
- **Claude Code accounts**: the 5h / 7d unified windows.
- An account still under a provider `Retry-After` counts as 100%.

What Foundry does with it:

- **Stops using a model at 85%** of any window that covers it (Usage tab →
  "stop using at", or `subscription_usage.limit_pct`; per-backend
  `usage_limit_pct`). The guardrail denies it, so a Plan persona's cascade
  moves to its next pinned model (e.g. Claude via Meridian), then local. It
  resumes automatically once the window resets.
- With several accounts of the same kind, the model stays usable while **any**
  account is under the limit (CLIProxyAPI rotates between them).
- **Usage tab** card: every account's windows, % used, reset time and the
  reading's age; **Events** logs when a model hits the limit.
- **Status line** on each turn served by a CLIProxyAPI model, e.g.
  `ChatGPT plus usage: weekly 61%, 5-hour 42%`.

Readings come from the headers ChatGPT/Claude send with each reply, so they
refresh as requests flow through CLIProxyAPI. Before the first request
there's no reading and the model is allowed; the optional `call_budget_5h` /
`call_budget_daily` caps still apply. A window whose reset time has passed
counts as reset.

**Foundry on a different host from CLIProxyAPI** (e.g. Foundry on victor-ai,
CLIProxyAPI on TrueNAS): set `management.allow-remote: true` in CLIProxyAPI's
`config.yaml` and firewall port 8317 to Foundry's host. The management API
can change CLIProxyAPI's settings, so don't expose it further. Running both
on the same host avoids this.

## 3. Use it for hard problems (Plan escalation)

Put the GPT model in your Plan persona's paid cascade (Personas → `claude-cline-plan`):

- `local_bias_strength`: `prefer_paid`
- `model_allowlist`: add e.g. `gpt-5.5`
- `pinned_models`: the order to try, e.g. `["claude-opus-4-8", "gpt-5.5"]`.
  Swap them to make GPT first. If the first is rate-limited or down,
  Foundry moves to the next, then to local.

Then follow the hand-off workflow in `docs/CLINE.md` ("Escalation"). Start a
**new** Plan task with the failing output and file paths, have the big model
write `docs/fix-plans/<topic>.md` without writing code, and let the local
model apply it in Act. Keep the escalation context small: ChatGPT Plus
budgets drain in minutes when every request resends a 200k-token conversation.

## Troubleshooting

- **Usage tab says "usage unavailable (HTTP 403/404)"**: CLIProxyAPI only
  allows the management API from localhost by default. Set
  `management.allow-remote: true` (see "Usage limits"), or check that
  `management_key` matches `management.secret-key`.

- **401 from Foundry's discovery**: `api_key` must match one of CLIProxyAPI's
  `access.api-keys`.
- **A model is missing**: CLIProxyAPI lists what the signed-in account can use.
  Check its panel. New models (e.g. GPT-6) need a CLIProxyAPI update.
- **429 / quota errors**: the subscription window is used up. Foundry fails
  over to the next pinned model. Check usage in CLIProxyAPI's panel.
- **No thinking text**: thinking is off for that turn (persona/client effort
  "off"), or the model returned no summary.
