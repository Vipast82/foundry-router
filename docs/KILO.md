# Kilo personas: defaults, strict routes, metadata, validation

Foundry ships a versioned default persona set for the AnimalUprising Kilo
trial (`animaluprising-kilo`). Kilo coordinates the agents; these personas fix
each one's inference route and limits.

| Persona | Backend / model | Starts |
|---|---|---|
| `Foundry-Kilo-Local` | local Strata / `qwen3.8-flash-next-iq3_s` | **enabled** |
| `Foundry-Kilo-Haiku` / `-Sonnet` / `-Opus` | Meridian / `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5` | disabled |
| `Foundry-Kilo-Luna` / `-Sol` / `-Astra` | CLIProxyAPI / `gpt-6-luna`, `gpt-6.1-sol`, `gpt-6-astra` | disabled |
| `Foundry-Kilo-Vision` | Meridian / Sonnet 5.5, image input | disabled |
| `Foundry-Kilo-Vision-Deep` | CLIProxyAPI / GPT-6 Astra, image input | disabled |
| `Foundry-Kilo-Images` / `-Images-Draft` | image policies: `gpt-image-2.5-sunburst` / `-flare` (docs/MEDIA.md) | disabled |

To run sprints one after another (each in a fresh Kilo session, verified and
reviewed before the next), see docs/COORDINATOR.md.

All of them use **strict routing** (`routing: strict`, `execution_mode:
direct`, `fallback_policy: refuse`).

## Install or update

Routing → Personas → **Default personas** → Install. Or set
`persona_profiles: [animaluprising-kilo]` in config.yaml to apply it at
every start. The card shows what each persona will get before you click.

- **Create**: a persona that doesn't exist yet is created.
- **Update**: a profile persona nobody has edited follows a newer profile
  version. Foundry stores a hash of the values it set; an unchanged hash means
  "untouched".
- **Your edits kept**: a persona you edited is never overwritten.
- **Name taken**: a persona with the same name that the profile didn't create
  is left alone.
- The **enabled** switch and validation results are always yours.

The backend is recorded by name when the profile is applied (the Meridian
backend, the CLIProxyAPI backend, the local backend serving the model). If
none exists yet, the persona gets a placeholder such as `<meridian>` and
**refuses** requests ("strict · no meridian backend" in red). It is never
served by another backend that happens to list the same model, such as a
Claude account inside CLIProxyAPI. Re-apply once the backend exists and the
real name is filled in.

## What strict routing guarantees

- Only the persona's `model_allowlist` (in `pinned_models` order) and only on
  its `backend`. The pool can't fail over to another backend serving the same
  model id. With no backend set, the persona uses the single best backend.
- No brain, no pipeline, no "any local" degrade, no busy-escalation.
- A budget denial moves to the next **listed** model (shown to the client) or
  ends in a clear refusal: `route_unavailable` (503) or `budget_denied` (429).
- **Disabled personas are not served** (`persona_disabled`, 403) and are left
  out of `/v1/models`, `/api/tags` and `/v1/models/{id}`. This applies to every
  persona, not only Kilo ones. Before this, a disabled persona was hidden but
  still answered if a client named it.
- **Image personas** (`kind: media`) are never chat models: chat requests get
  `image_persona` (400). Use them as the `model` of `generate_image` /
  `edit_image` or `/v1/images/*`. Their `backend` is honoured the same way.
- Every response carries `x-foundry-persona`, `x-foundry-served-model`,
  `x-foundry-served-backend` and `x-foundry-routing`.

## What clients are told

`/v1/models` (and `/v1/models/{id}`) entries now include:

```json
{"id": "Foundry-Kilo-Local", "context_length": 131072, "max_output_tokens": 32768,
 "top_provider": {"context_length": 131072, "max_completion_tokens": 32768},
 "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
 "capabilities": {"tools": true, "vision": false, "reasoning": false},
 "foundry": {"routing": "strict", "served_model": "qwen3.8-flash-next-iq3_s",
             "backend": "strata", "validation": "passed"}}
```

For strict personas the context is the **effective** one: the lower of the
persona's `context_window` and the window the backend reports for the model
(llama.cpp `/props`). The output cap is the persona's `max_output_tokens`,
capped by that context. Vision is advertised only when the persona declares
**image input**. `/api/show` reports the same numbers to Ollama-dialect
clients (Cline, AnythingLLM, OpenWebUI). Flexible personas keep their
previous behaviour (largest routable window).

### Kilo configuration

Kilo sizes compaction from its own provider config, not from `/v1/models`.
On the Default personas card, **Kilo provider config** prints a block for
`kilo.jsonc` with each persona's limits, tool-calling and image support:

```json
{"provider": {"foundry": {
  "npm": "@ai-sdk/openai-compatible", "name": "Foundry Router",
  "options": {"baseURL": "http://192.168.0.112:11435/v1", "apiKey": "{env:FOUNDRY_KEY_KILO}"},
  "models": {"Foundry-Kilo-Local": {"limit": {"context": 131072, "output": 32768},
                                    "tool_call": true, "attachment": false, ...}}}}}
```

Give Kilo its own client key (docs/CLIENTS.md), optionally restricted to the
Kilo personas with `personas: [Foundry-Kilo-Local, ...]`. If the shape differs
from what your Kilo version expects, match it to your existing provider entry;
the numbers are what matter.

## Validate before enabling

Each persona has a **validate** button (and `POST /admin/api/personas/validate`).
It makes real requests through Foundry's own HTTP route, with Foundry's
internal identity, so it exercises the same client auth, strict routing,
budget checks and translation Kilo gets. It works on disabled personas
without enabling them for clients.

| Check | Passes when |
|---|---|
| route | the strict route resolves inside the allowlist/backend and the budget allows it |
| text | an answer arrives from the expected model **and** backend |
| stream | streamed deltas, a finish reason and `[DONE]` |
| tools | a `set_flag(name, enabled: boolean)` call arrives with a real boolean, and the tool result round trip continues |
| image_input | (personas declaring image input) a generated red image is recognised as red |
| length | `max_tokens: 16` ends with `finish_reason: "length"` (skipped for Codex, which ignores max_tokens) |
| image_job | (image personas) one real low-quality generation succeeds |

The result is stored on the persona: a pill in the list (hover for each
check), `foundry.validation` in `/v1/models`, and an Events entry. Validating
a frontier persona spends about five subscription calls; the UI asks first.

Suggested rollout (spec §15):
1. Install the profile; validate `Foundry-Kilo-Local`.
2. Validate one helper (e.g. `Foundry-Kilo-Sonnet`), then enable it.
3. Validate the vision and image personas with real screenshots and assets
   before enabling them.
