# Foundry Router

Self-hosted **agentic LLM routing middleware**. It presents an
**Ollama-compatible API** to your chat and coding clients (AnythingLLM, Open
WebUI, Kilo Code, Cline — anything that can point at an Ollama base URL), but
instead of one fixed model behind it, a small local **routing brain** decides
per request whether the work stays local (free, fast, private) or escalates to
Claude via a [Meridian] bridge (subscription usage, not API billing) — and can
hand work back and forth between models within a single task.

> ## ⚠️ Internal use only
> **This service has no authentication and is not hardened for the public
> internet.** It is built for private/LAN deployment (a homelab, a tailnet).
> Do not expose the port to the internet. If your Docker host has any
> internet-facing interface, pin the compose port mapping to a LAN IP
> (see `docker-compose.yml` comments).

The full design rationale lives in
[`docs/foundry-router-build-design.md`](docs/foundry-router-build-design.md) —
this README is the operator's view.

## What you get

- **Ollama-compatible facade** — `/api/chat`, `/api/tags`, `/api/generate`,
  `/api/show`, `/api/version`. Clients need only a base-URL change.
- **Personas as virtual models** — `/api/tags` advertises routing *policies*
  (`Foundry-Coding`, `Foundry-Chat`, `Foundry-Research`, `Foundry-RAG`), not
  raw models. Picking one in any model dropdown selects the policy. Add your
  own from the web UI — no code changes.
- **Routing brain** — a LangGraph agent (reference default: a small local
  model on its own Ollama instance) that picks models via live registry
  rankings, refines vague prompts, asks clarifying questions, and narrates
  every decision in `<think>` blocks your client already renders.
- **Backend pool with failover** — multiple Ollama hosts + Meridian +
  optionally OpenRouter, health-checked, priority-ordered. Olla or LiteLLM can
  replace the internal pool if you already run one (`backend_pool.mode`).
- **Self-maintaining model registry** — OpenRouter metadata polled daily; a
  background Research Agent (SearXNG + Crawl4AI over MCP) fills in benchmark
  scores and qualitative notes. Manual edits are pinned and never clobbered.
- **Dynamic tool sync** — the brain's `ask_<model>` tools are generated from
  what's actually reachable right now. Install a model, it appears; a backend
  dies, its tools go away. MCP server tools are discovered the same way.
- **Usage-aware guardrails** — Meridian window checks before Claude calls,
  max steps/paid-calls per request, optional spend caps, per-persona overrides.
- **Web UI** at `/ui` — backends, registry, personas, tools, guardrails, MCP
  connections, usage log, troubleshooting log.
- **Degrades, never dies** — brain host down? A static rule routes to the best
  local model. Internet down? Local-only operation keeps working end to end.

## Quick start

```bash
git clone <this repo> foundry-router && cd foundry-router
cp .env.example .env            # set MERIDIAN_API_KEY etc.
mkdir -p config data            # create BEFORE compose up, so they aren't root-owned
cp config.example.yaml config/config.yaml
# edit config/config.yaml: your Ollama URLs, Meridian URL, brain model
docker compose up -d --build
```

Then point a client at it:

| Client | Setting |
|---|---|
| AnythingLLM / Open WebUI | Ollama base URL → `http://<host>:11435`, pick `Foundry-Chat` |
| Kilo Code / Cline | Ollama provider, base URL as above, model `Foundry-Coding` |
| Admin panel | `http://<host>:11435/ui` |

Port **11435** by default (Ollama's 11434 + 1, so a real Ollama can coexist).

### Persistence

`./config` (YAML) and `./data` (SQLite) are volume mounts. The container runs
as uid 1000; if you created those directories as root, `chown -R 1000:1000
config data` once. They survive
`docker compose down && up` and image rebuilds. Nothing you configure lives in
the image layer. Secrets come from `.env` — never write them into config.yaml
(use `${VAR}` references, which are expanded at load time).

## Architecture in brief

```
clients ──Ollama API──▶ facade ──▶ agent brain (LangGraph, small local model)
                                     │  tools: ask_<model>… (auto-generated),
                                     │  refine_prompt, ask_user, MCP tools
                                     ▼
                              backend pool ──▶ Ollama hosts / Meridian / OpenRouter
                                     ▲
             model registry (SQLite) ┴─ OpenRouter poll + background Research Agent
```

Single FastAPI service, single SQLite file. The Research Agent runs off the
hot path — it never blocks a live request.

## Running without Docker (dev)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export FOUNDRY_CONFIG=./config/config.yaml FOUNDRY_DATA_DIR=./data
python -m foundry_router
pytest                      # tests need no network or GPUs
```

## Operational notes

- **Brain residency:** set `OLLAMA_KEEP_ALIVE=-1` (or rely on the router's
  `keep_alive: -1`) on the Ollama instance hosting the brain model so it never
  unloads mid-session.
- **Raw model bypass:** `/api/tags` lists personas only, but `/api/chat`
  accepts any raw backend model name for manual, routing-free access.
- **Coding tools that send their own tools:** when a client supplies `tools`
  (Kilo/Cline agent loops), the router picks one model by persona policy and
  forwards the tools verbatim rather than running its own agent loop.
- **Config edits from the UI** re-serialize `config.yaml` (comments in a
  hand-edited file are lost; `config.example.yaml` stays the commented
  reference). Backend/brain/guardrail/MCP changes apply live; `server.host`/
  `server.port` need a restart.

## License

MIT — see [LICENSE](LICENSE).

[Meridian]: https://github.com/search?q=meridian+claude+bridge
