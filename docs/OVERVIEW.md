# Foundry Router — what everything is and does

A map of the whole system for someone new to it: the moving parts, how a request
flows, what each tab in the web UI controls, and how to install the optional
companion services. For client setup and the "why", see the root
[README](../README.md) and the design doc it links.

---

## In one paragraph

Foundry Router presents an **Ollama-compatible API** to your chat/coding clients,
but instead of one fixed model it puts a small **routing brain** in front of a
pool of backends (local Ollama models, llama.cpp/OpenAI-compatible servers, and
Claude via a Meridian subscription bridge). Per request the brain decides whether
work stays local (free/private) or escalates to Claude, runs multi-step agentic
loops with **MCP tools**, and keeps a self-maintaining **model registry** current
via a background **Research Agent**. It's internal/LAN-only — no auth.

---

## How a request flows

```
client (AnythingLLM / Open WebUI / Kilo / Cline)
   │  picks a "model" that is actually a PERSONA (Foundry-Coding, Foundry-Chat…)
   ▼
Ollama-compatible facade  (/api/chat, /api/tags, /api/show, /api/generate)
   ▼
Routing brain  — reads the persona policy + live usage, picks a worker model,
   │             decides local-vs-Claude, plans tool use
   ▼
Backend pool  — dispatches to the chosen backend (one dispatch path for every
   │             mode), sets Ollama num_ctx from the persona's context_window,
   │             guards context-fit, records reliability + timing
   ▼
Worker model (± MCP tools, ± tiered review) → response streamed back to the client
```

---

## The components

| Part | File(s) | What it does |
|---|---|---|
| **Facade** | `facade/ollama_api.py`, `facade/translate.py` | Speaks the Ollama wire protocol so any Ollama client works unchanged. Advertises personas as models via `/api/tags`; reports context via `/api/show`. |
| **Routing brain** | `brain/agent.py` | The LangGraph agent that makes the per-request routing decision, runs the agentic tool loop, and dispatches workers. One canonical `_dispatch_worker` path for chat, pipelines, and judges. |
| **Backend pool** | `pool/` | Typed backends: `ollama`, `openai-compatible`, `anthropic-compatible` (Claude/Meridian). Health, failover, context probing (`/api/show`), and per-request options (`num_ctx`). |
| **Personas** | `personas` store, UI Personas tab | Virtual models = routing *policies*. Category, local-bias, context_window, preferred MCP servers/tools, escalation triggers, review settings. Add one = add a row, never a code change. |
| **MCP / Tool Sync** | `tools/sync.py`, `tools/mcp_client.py` | Discovers tools from configured MCP servers, syncs them into the registry with read/write annotations, and enforces per-persona whole-server or per-tool grants. |
| **Gateway admin** | `gateway_admin.py` | Operator-only control panel over a Docker MCP Gateway (browse catalog, attach/detach servers, set config) — backend-initiated, never persona-exposed. See [GATEWAY_SERVERS.md](GATEWAY_SERVERS.md). |
| **Model registry** | `registry/models_db.py` | Every model's row: cost tier, context length, tags, benchmark rows, reliability counters, source-authority scoring, conflation guards. |
| **Research Agent** | `registry/research_agent.py` | *Background* agent that keeps the registry current: search (SearXNG) → fetch (Crawl4AI) → LLM extraction of benchmarks/tags. Distinct from the user-facing Foundry-Research persona. Tunable in the Research tab. |
| **Guardrails + usage** | `guardrails.py`, `usage.py` | Meridian quota snapshots, adaptive tier conservation, hard-stop at exhaustion, OAuth-staleness detection, user-paid confirmation prompts. |
| **Context sizing** | brain + facade | Persona `context_window` is the single source of truth: it drives Ollama `num_ctx` AND what the frontend is told. See [CONTEXT_SIZING.md](CONTEXT_SIZING.md). |
| **Output-quality infra** | semantic cache, eval harness, feedback/tiered review | Logging + feedback, tiered review passes, semantic cache, an eval harness, and client-aware persona compat notes. |

---

## The web UI, tab by tab

- **Backend Pool** — backends, health, the routing brain, and **Meridian
  re-authentication** (refresh token / full re-login — [MERIDIAN_AUTH.md](MERIDIAN_AUTH.md)).
- **Models** — the registry: score badges, `~seed` vs researched, cross-model
  conflation ⚠ flags, per-model benchmark editing, "research this model now".
- **Personas** — the routing policies advertised to clients; the persona editor
  (context_window, MCP tool grants, review, escalation triggers…).
- **MCP** — connected MCP servers, Tool Sync, and **Gateway Servers** (browse the
  Docker MCP Catalog, attach/detach without SSH).
- **Research** — the background Research Agent's knobs: model, engines, sweep
  cadence, corpus/page/snippet sizes, extra queries, context_window. "Test
  search+fetch" verifies the pipeline end to end.
- **Dev-Log / Events** — live app log tail and the operational event stream
  (usage alerts, research outcomes, gateway changes).

---

## Companion services (host-side helpers)

Foundry Router is a container/remote client, so a few operations it legitimately
can't do itself — running a host CLI, touching host credentials — are handled by
**tiny, locked-down HTTP companions** you run next to the thing they operate on.
Same trust model for all of them: **stdlib-only, localhost/LAN-bound, bearer
token, firewalled to Foundry's host.** They're optional — the features that need
them simply stay disabled until you point Foundry at one.

| Companion | Runs on | Enables | Install |
|---|---|---|---|
| **Meridian auth** | the Meridian host (where the `meridian` CLI works) | Full OAuth re-login from the UI (the interactive paste-back step) | `contrib/meridian-auth-service/install.sh` |
| **Gateway inspect** | the Docker MCP Gateway host | Rich per-server catalog detail (`docker mcp catalog server inspect`) | `contrib/gateway-admin-service/install.sh` |

### One-line install (run as root on the relevant host)

Meridian auth companion:

```bash
curl -fsSL https://raw.githubusercontent.com/Vipast82/foundry-router/main/contrib/meridian-auth-service/install.sh | bash
```

Gateway inspect companion:

```bash
curl -fsSL https://raw.githubusercontent.com/Vipast82/foundry-router/main/contrib/gateway-admin-service/install.sh | bash
```

Each script downloads the service, writes a systemd unit, starts it, prints a
`/health` check, and shows the **URL + token** to paste into the matching UI
panel. Override any default via env vars (see the top of each script) — e.g.
`MERIDIAN_AUTH_PORT`, `GATEWAY_INSPECT_TOKEN`.

### Networking note (container → host)

If Foundry runs as a Docker container on the **same** host as a companion,
`127.0.0.1` inside the container is *not* the host. Point Foundry at the host's
**LAN IP** (e.g. `http://192.168.0.112:8898`) — the companion binds `0.0.0.0`, so
it's listening there. Verify from inside the container:

```bash
docker exec foundry-router python -c "import urllib.request;print(urllib.request.urlopen('http://<HOST_LAN_IP>:8898/health',timeout=5).read().decode())"
```

You want `{"ok": true}`. Then firewall the port to Foundry's host/subnet only.

---

## Where to read more

| Topic | Doc |
|---|---|
| Cline (VS Code) plan/act routing | [CLINE.md](CLINE.md) |
| Context sizing (num_ctx, advertise, thrash) | [CONTEXT_SIZING.md](CONTEXT_SIZING.md) |
| Meridian re-auth (refresh + full login) | [MERIDIAN_AUTH.md](MERIDIAN_AUTH.md) |
| Docker MCP Gateway admin | [GATEWAY_SERVERS.md](GATEWAY_SERVERS.md) |
| Code-execution sandbox MCP | [CODE_SANDBOX.md](CODE_SANDBOX.md) |
| Contributor conventions (versioning, branches) | [CONVENTIONS.md](CONVENTIONS.md) |
| Client setup + the "why" | [../README.md](../README.md) |
