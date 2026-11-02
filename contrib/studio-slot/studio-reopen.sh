#!/bin/bash
# Close the slot's Studio and open STUDIO_OPEN again (what the Foundry Client's auto-reopen does
# on the workstation). Studio is closed by killing it: a slot's place content comes from Rojo,
# and auto-recovery is off (entrypoint.sh). The plugin settings are re-applied while Studio is
# closed, so Rojo reconnects and syncs on open without a prompt, and missing plugins are put back.
#   studio-reopen [--wait]     --wait: block until the slot's window is back (max 240 s)
set -u
# the router's slot settings (slot-secrets): this slot's place link and never-sync places
[ -f "$HOME/.config/foundry-slot/slot.env" ] && . "$HOME/.config/foundry-slot/slot.env"
PREFIX="$HOME/.local/share/vinegar/prefixes/studio"
RBX="$PREFIX/drive_c/users/$(id -un)/AppData/Local/Roblox"
touch /tmp/studio-reopening
for p in $(pgrep -f '[R]obloxStudioBeta'); do kill "$p" 2>/dev/null; done
sleep 2
WINEPREFIX="$PREFIX" wineserver -k 2>/dev/null
sleep 2
# Studio's plugins (Rojo + robloxstudio-mcp) into the prefix it reads. entrypoint.sh copies them
# only at container start, so a prefix rebuilt while the container runs had none: no MCP, no
# Rojo (2026-11-02, slot-2 after its half-built prefix was moved aside)
mkdir -p "$RBX/Plugins" && cp -u /opt/studio-slot/plugins/* "$RBX/Plugins/" 2>/dev/null || true
node /opt/studio-slot/studio-plugin-settings.js "$RBX" $(rojo-seed) ${ROJO_NEVER_PLACE_IDS:-138024886323256} \
    >>"$HOME/rojo.log" 2>&1 || true
rm -f "$HOME"/.local/share/vinegar/appdata/Roblox/RobloxStudio/AutoSaves/*_AutoRecovery_*.rbxl 2>/dev/null
export DISPLAY="${DISPLAY:-:1}" GTK_A11Y=none
nohup vinegar ${STUDIO_OPEN:+"$STUDIO_OPEN"} >>"$HOME/vinegar.log" 2>&1 &
rm -f /tmp/studio-reopening
if [ "${1:-}" = "--wait" ]; then
    for _ in $(seq 1 80); do
        xdotool search --name "${STUDIO_FULLSCREEN_TITLE:-\\[SLOT-}.* - Roblox Studio" >/dev/null 2>&1 \
            && { sleep 20; echo "studio-reopen: Studio is back"; exit 0; }
        sleep 3
    done
    echo "studio-reopen: Studio did not come back within 240 s" >&2
    exit 1
fi
