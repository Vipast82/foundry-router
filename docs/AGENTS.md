# External agents — Hermes Agent

Foundry can route to **agents** as well as models. An agent runs its own
multi-step loop with its own tools (terminal, files, web, browser), skills and
memory. Foundry supports [Hermes Agent](https://github.com/NousResearch/hermes-agent)
(Nous Research) through its OpenAI-compatible API server. You can use it in
two ways, and they can run side by side:

| | Hermes as a **tool** | Hermes as a **backend** |
|---|---|---|
| What the client sees | three tools: `hermes_run`, `hermes_status`, `hermes_stop` | a persona (e.g. `Foundry-Agent`) in its model list |
| Who drives the loop | your model (Cline, a persona's worker, AnythingLLM) calls Hermes when it decides to | Hermes answers the whole turn |
| Good for | handing off a long side task (investigate a repo, run and fix tests, research) while Cline keeps its own tools and context | chat clients (Open WebUI, AnythingLLM) where you want the agent itself to answer |
| Context cost | about 3 small tool definitions | none (Hermes manages its own context) |

## 1. Enable Hermes' API server

In `~/.hermes/.env` on the Hermes host:

```
API_SERVER_ENABLED=true
API_SERVER_KEY=<a long random key>
API_SERVER_HOST=0.0.0.0      # only if Foundry runs on another machine
```

Start it with `hermes gateway`. The server listens on port **8642**.

## 2. Add the agent to Foundry

Go to **Tools & MCP → Agents → Add / edit agent**, or edit `config.yaml`:

```yaml
agents:
  - name: hermes
    url: http://192.168.1.50:8642
    api_key: ${HERMES_API_KEY}          # the API_SERVER_KEY above
    caller_token: ${HERMES_CALLER_TOKEN}
```

Click **test**. The Agents table then shows health, the model name Hermes
advertises, and its skills and toolsets.

### Loop protection: the caller token

Hermes can also use Foundry, either as its model provider (a custom
OpenAI-compatible endpoint at `http://foundry:11435/v1`) or as an MCP server.
If it does, give it an API key and put the **same value** in `caller_token`.
Requests that carry that key:

- never see or run agent tools;
- are never answered by an agent-backed persona. That persona falls back to its
  normal model routing, and the reason is written to Events.

Without a caller token, Hermes could end up calling itself through Foundry.

## 3a. Hermes as a tool

With `expose_as_tool: true` (the default), Tool Sync registers the MCP server
**`agent-hermes`**. It has three tools:

- **`hermes_run(task, context?, session_id?)`** starts a task through Hermes'
  Runs API. The agent's tool activity is sent back as MCP progress (for
  example "🔧 terminal: pytest -q"). The call waits up to `tool_wait_seconds`
  for the answer, which ends with a summary of the tools Hermes used and its
  `session_id`. A longer task returns a `run_id` instead.
- **`hermes_status(run_id, wait_seconds?)`** returns the status, and the answer
  once the task is done. It waits up to `wait_seconds` (default 60), which
  keeps clients from polling in a tight loop.
- **`hermes_stop(run_id)`** stops a task.

If a client disconnects in the middle of a `hermes_run`, Foundry stops the run.

**To use it from Cline:** make an aggregator profile that contains only this
server, so Cline loads 3 tools and nothing else. Under **Backends → Pool →
Foundry-MCP aggregator → profiles**, add `"hermes": ["agent-hermes"]`. Then add the profile to Cline's MCP
settings:

```json
{ "mcpServers": { "hermes": { "type": "streamableHttp",
    "url": "http://foundry:11435/mcp/p/hermes/" } } }
```

Keep `tool_wait_seconds` below Cline's MCP request timeout. For long jobs, rely
on the `run_id` handoff.

**For a persona:** tick `agent-hermes` under *preferred MCP servers*. The
persona's worker loop, direct mode (with *persona tools in direct mode* on), and
its `/mcp/persona/<Persona>/` endpoint all get the tools.

## 3b. Hermes as a backend

Create a persona (for example `Foundry-Agent`) and set **served by agent** to
`hermes`. From then on:

- The conversation is sent to Hermes (`/v1/chat/completions`) and the answer
  streams back to Ollama and OpenAI clients alike.
- Hermes' tool progress appears as **thinking** ("🔍 web_search: …"). A
  "still working…" line is sent every `heartbeat_seconds` while the agent is
  busy, so the connection never goes idle.
- Each client conversation maps to one Hermes session (`X-Hermes-Session-Id`),
  so Hermes keeps its memory of that chat. Turn off `session_continuity` if you
  want it stateless.
- Client tools are **not** forwarded, because Hermes uses its own tools. For
  Cline, the tool option (3a) usually fits better.
- If the agent is down (failed health check) or the request came from the agent
  itself, the persona is served by its normal model policy instead.

The final chunk carries `foundry.agent`, `foundry.agent_tools` and
`foundry.agent_session`, plus the token usage Hermes reports.

## Metrics

**Tools & MCP → Agents → Agent runs** records one row per task, whether Hermes
ran as a tool or as a backend. Each row has the status (completed / failed /
cancelled / timeout / handed_off), the duration, time to first output, the
tools *Hermes* ran and how often, tokens, the model Hermes itself used, and the
run and session IDs. A task that was handed off is watched in the background
until it finishes, so its row stays accurate even if nobody calls `_status`.

The `hermes_*` tool calls also appear in **MCP Metrics**, like any other MCP
tool.
