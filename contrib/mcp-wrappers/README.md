# Contrib MCP wrappers

Small, purpose-built MCP servers wrapping REST APIs that don't ship an HTTP
MCP endpoint of their own (media-generation spec §6). Same pattern as the
SearXNG wrapper: `FastMCP` + `streamable-http`, one file each.

**Status: reference implementations, untested against live services.** The
target REST APIs (ACE-Step's `acestep-api`, devnen/Chatterbox-TTS-Server)
were not reachable from the machine that authored these — endpoint paths and
payload shapes are configurable via environment variables so you can adjust
to your installed versions without editing code. Treat first run as a
debugging session, same as every other integration tonight.

| Wrapper | Wraps | Run |
|---|---|---|
| `acestep_mcp.py` | ACE-Step-1.5 REST API (`uv run acestep-api`, port 8001) | `python acestep_mcp.py` |
| `chatterbox_mcp.py` | devnen/Chatterbox-TTS-Server (OpenAI-compatible TTS) | `python chatterbox_mcp.py` |

Both need: `pip install "mcp>=1.2" httpx`

Then add to Foundry Router (MCP tab or config.yaml):

```yaml
mcp_servers:
  - name: acestep
    url: http://<host>:8765/mcp
    transport: streamable-http
    timeout_seconds: 900        # music generation is slow
  - name: chatterbox
    url: http://<host>:8766/mcp
    transport: streamable-http
    timeout_seconds: 300
```

Generated artifacts are written to `ARTIFACT_DIR` (default `./artifacts`) and
returned as file paths / URLs — serve that directory with any static file
server (or a bind mount your clients can reach) so the returned references
resolve for end users.

For ComfyUI, Voicebox, InvokeAI, and faster-whisper, use the existing
community MCP servers named in the spec — no wrapper needed here (InvokeAI
and faster-whisper may need their stdio transport bridged to HTTP with any
standard mcp stdio→http proxy).
