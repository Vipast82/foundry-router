#!/bin/bash
# Roblox's own Studio MCP server (StudioMCP.exe, shipped with Studio) under Wine, speaking MCP on
# stdio: what Foundry's gate calls the "official" server (FOUNDRY_MCP_OFFICIAL). Studio updates
# land in a new versions/<hash>/ folder, so the newest StudioMCP.exe is used.
export WINEPREFIX="${WINEPREFIX:-$HOME/.local/share/vinegar/prefixes/studio}"
export WINEDEBUG="${WINEDEBUG:--all}"
exe=$(ls -t "$HOME"/.local/share/vinegar/versions/*/StudioMCP.exe 2>/dev/null | head -1)
if [ -z "$exe" ]; then
    echo "studio-mcp-official: no StudioMCP.exe under ~/.local/share/vinegar/versions (Studio not installed yet)" >&2
    exit 1
fi
exec wine "$exe" "$@"
