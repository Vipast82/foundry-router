# AnythingLLM + Foundry's MCP tools (the Foundry-MCP aggregator)

**Goal:** let AnythingLLM use the MCP servers you attached to Foundry-Router
(acestep-music, TTS, crawl4ai, …) *and* keep its own local MCP servers — as many
tools as possible, toggled on the AnythingLLM side.

## Why the plain persona route didn't do it

Foundry is normally an MCP **client**: it dials out to your MCP servers and runs
their tools *server-side*. AnythingLLM only speaks to an LLM endpoint, so it
never sees those tools. And in AnythingLLM's `@agent` mode, **AnythingLLM owns
the tool loop** — it sends its own tool definitions and executes the calls
itself; Foundry (in `direct` mode) just relays the completion. That's why every
AnythingLLM request logs as `direct · client tools`. It isn't a routing bug —
AnythingLLM simply had no way to *see* Foundry's tools.

## The fix: Foundry-MCP aggregator

Foundry can now also act as an MCP **server**: one Streamable-HTTP endpoint that
lists every tool Foundry knows about and, when called, dispatches through
Foundry's existing MCP client. You point AnythingLLM's MCP config at it, and its
agent gains all of Foundry's tools **alongside** your local MCP servers.

Who decides / runs what:
- **AnythingLLM's agent** drives the loop and decides which tool to call — using
  whichever model Foundry routes its completion to (your Qwen3.8:27b or Claude,
  per the persona). The small routing *brain* is not in this path.
- **Foundry** executes the tool (runs acestep-music etc.) and returns the result.
- Your **local** AnythingLLM MCP servers keep working side-by-side.

## Enable it

1. **Foundry UI → Backends tab → Foundry-MCP aggregator.** Check **enabled**,
   set a **token** (shared secret; header defaults to `X-API-KEY`), set the
   **public base URL** (how clients reach this Foundry, e.g.
   `http://192.168.1.50:8080` — blank uses the configured server host:port), and
   optionally define **profiles** — curated subsets ("MCP personas"):
   ```json
   { "music": ["acestep-music"], "voice": ["kokoro-tts", "chatterbox-tts"] }
   ```
   Save, then click **↻ Restart Foundry** (endpoints mount at startup). The panel
   shows the live endpoints and **paste-ready AnythingLLM *and* Cline configs**
   (with a copy button each), already filled with your base URL and token.

   The endpoints live on **Foundry's own server port** (path-based) — there's no
   separate port to open:
   - `{base}/mcp/` — **all** enabled MCP servers' tools.
   - `{base}/mcp/p/<profile>/` — just that profile's servers.

   (Use the trailing slash — the bare path 307-redirects to it.)

   **Restart button:** it terminates the process; a supervisor (Docker
   `restart: unless-stopped`, systemd, …) brings it back. If nothing supervises
   the process, it just stops — restart it yourself.

2. **AnythingLLM** — edit `anythingllm_mcp_servers.json` in your AnythingLLM
   storage `plugins` directory (the UI shows a ready-to-paste block):
   ```json
   {
     "mcpServers": {
       "foundry": {
         "type": "streamable",
         "url": "http://HOST:PORT/mcp/",
         "headers": { "X-API-KEY": "your-token" }
       },
       "foundry-music": {
         "type": "streamable",
         "url": "http://HOST:PORT/mcp/p/music/",
         "headers": { "X-API-KEY": "your-token" }
       }
     }
   }
   ```
   Keep any **local** MCP servers in the same file — they coexist. Then enable
   the tools you want in AnythingLLM's agent-skills UI, and use `@agent` in chat.

## Cline

Cline can consume the same endpoints as a **remote MCP server**. Paste into
`cline_mcp_settings.json` (or use Cline's **MCP Servers → Add Remote Server**):
```json
{
  "mcpServers": {
    "foundry": {
      "type": "streamableHttp",
      "url": "http://HOST:PORT/mcp/",
      "headers": { "X-API-KEY": "your-token" },
      "disabled": false,
      "autoApprove": []
    }
  }
}
```
This gives Cline Foundry's server-side tools (music/TTS/etc.) in addition to its
own. Note this is *separate* from the `claude-cline-plan`/`act` LLM personas —
those choose the model; this adds tools. Add tool names to `autoApprove` to skip
the per-call approval prompt for trusted ones.

## Long tool calls: heartbeat + activity

Media tools (acestep-music, stable-audio) run for **minutes**. Two things help:

- **Raise the per-server timeout.** Edit the slow server's connection and set
  **timeout (s)** high enough (e.g. `1800`). The default 300s kills a music
  generation mid-render.
- **Progress heartbeat (opt-in).** In the aggregator panel set **heartbeat (s)**
  to e.g. `20`, save, and **restart**. The endpoints then run in SSE-streaming
  mode and emit an MCP progress notification every N seconds while a tool runs
  ("still working… 45s"), so the connection never idles out and a client that
  supports progress shows a live status instead of appearing hung. Off by
  default (single-JSON mode). The heartbeat only surfaces in clients that send a
  progressToken and render progress — verified against the MCP SDK; AnythingLLM
  support may vary.

**Activity view.** The aggregator panel's **Active now** line shows, live, which
models are generating, which MCP tools are mid-execution (e.g. `acestep-music/
generate_music · 90s`), and what's loaded in VRAM — so you can see a long job is
actually running. Same data at `GET /admin/api/activity`.

## Notes & limits

- **Tools appear in `@agent` mode only** — that's how AnythingLLM's MCP works.
- **Restart to change endpoints.** enabled/token/profiles are saved immediately,
  but mounting a new endpoint set happens at startup.
- **Gateway-management tools are never exposed** — those are Foundry's own
  control surface.
- **Auth:** the `X-API-KEY` token is required if set; empty = open (trusted LAN
  only). It's masked in the UI and never returned to the browser.
- This path uses AnythingLLM's model choice (via Foundry's routing), not the
  Foundry brain's tool orchestration. If you'd rather Foundry's brain pick and
  run tools server-side while AnythingLLM just chats, that's the separate
  agent-mode persona path (`brain_handles_tools` OFF = the worker model runs the
  loop, brain as fallback).
