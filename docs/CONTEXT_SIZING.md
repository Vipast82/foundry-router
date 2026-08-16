# Context sizing — how it flows end to end

Foundry Router is the **single source of truth** for a persona's context. You set
it once, on the persona's `context_window`, and it drives everything below.

## The one knob: persona `context_window`

Set it in the persona editor (tokens). It has **two** effects:

1. **Frontend advertise** — reported to clients (AnythingLLM, Open WebUI, …) via
   `/api/show`, so they know how large a conversation they may send.
2. **Backend load (Ollama)** — sent to an Ollama worker as `num_ctx`, so the
   model is *loaded* with that much context instead of the backend default.

`context_window = 0` (blank) = **auto**: advertise the max known window across
routable workers, and inject **no** `num_ctx` (the worker inherits the backend's
own default, e.g. `OLLAMA_CONTEXT_LENGTH`).

## Per-backend reality

Only Ollama takes a per-request context size. The rest set it their own way:

| Backend | How context is set | What Foundry does |
|---|---|---|
| **Ollama** | per-request `num_ctx` | Sends `num_ctx = min(context_window, model's trained max)` on every worker/research call. |
| **llama.cpp / `llama-server`** (openai-compatible) | **launch flag** `-c N` (or `--ctx-size`) — no per-request override | Foundry can't set it; **you** launch llama-server with `-c` to match the persona's `context_window`. Foundry still advertises `context_window` to the frontend. |
| **Claude** (anthropic-compatible) | fixed (200K) | Nothing to set. |

So for a future llama.cpp backend: pick a `context_window` for the persona,
launch `llama-server -c <that number>` (sized to your VRAM), and the two line up
— Foundry advertises it, llama.cpp serves it.

## Sizing to your hardware (`num_ctx` costs VRAM)

`num_ctx` reserves a KV cache proportional to its size. Load the model and check:

```bash
ollama ps          # CONTEXT column = what it's actually loaded with
nvidia-smi         # VRAM headroom
```

- **Steady-state speed is the same** at a large `num_ctx` as a small one *for a
  given request* — attention scales with the tokens actually used, not the
  reserved size. The only cost of a big `num_ctx` is the reserved VRAM.
- **Changing `num_ctx` reloads the model** (a cold load). Because Foundry ties
  `num_ctx` to the persona's `context_window`, it stays constant for that persona
  — so no reload churn: the model loads once and stays warm.
- If `context_window` exceeds what the model was trained for, Foundry caps it at
  the trained max. If a request is bigger than the effective window, it's
  **rejected and rerouted** (to a larger-context model) rather than silently
  truncated at the backend.

Example: dual 16GB GPUs (32GB) hold `qwen3.8:27b` at ~150K context (~22GB). Set
the persona `context_window` to 150000 and Foundry loads the worker there;
the frontend is told 150000 too. Leave a small model's persona blank to inherit
the 8192 default instead of wastefully loading it at 150K.

## The background Research Agent sizes its own model

The **model-registry Research Agent** (Research tab) is *not* a persona — it
scrapes model cards and extracts benchmarks. It sizes its worker's `num_ctx` to
just fit the corpus (`corpus_chars/4 + reply`), e.g. `corpus_chars=26000` →
`num_ctx≈11620`. That's efficient for extraction, and it's what you'll see in
`ollama ps` right after a sweep — **not** a persona load.

The catch: if the research model is the **same** model a persona also uses,
whichever ran last wins the load, and the two sizes thrash (each switch reloads
the model cold). Two ways to keep it clean:

- Point **Research → research model** at a *dedicated small* model, so the big
  persona worker is never reloaded down; or
- Set **Research → context_window** to match the persona's `context_window`
  (0 = auto, the corpus formula above). Then both load at the same size — no
  reload churn — and Foundry stays the single source of truth for that model too.
