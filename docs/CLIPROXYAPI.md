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
| Quota / window gauge | ⚠ not shown in Foundry. CLIProxyAPI's own panel (`/management.html`) shows quotas; on a rate limit Foundry fails over to the persona's next model. |

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
 "flavor": "cliproxyapi"}
```

`flavor: "cliproxyapi"` is what makes Foundry treat it as a subscription
cloud backend and send the full effort scale. Use `openai-compatible` for
**all** CLIProxyAPI models, including Claude ones. Don't add it as
`anthropic-compatible`: that type turns on Meridian-only quota checks that
CLIProxyAPI doesn't answer.

Any other OpenAI-dialect subscription proxy (ChatMock, …) can be marked the
same way with `"cloud": true`.

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

- **401 from Foundry's discovery**: `api_key` must match one of CLIProxyAPI's
  `access.api-keys`.
- **A model is missing**: CLIProxyAPI lists what the signed-in account can use.
  Check its panel. New models (e.g. GPT-6) need a CLIProxyAPI update.
- **429 / quota errors**: the subscription window is used up. Foundry fails
  over to the next pinned model. Check usage in CLIProxyAPI's panel.
- **No thinking text**: thinking is off for that turn (persona/client effort
  "off"), or the model returned no summary.
