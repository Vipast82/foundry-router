#!/bin/bash
# Close the slot's Studio and open STUDIO_OPEN again (what the Foundry Client's auto-reopen does
# on the workstation). Studio is closed by killing it: a slot's place content comes from Rojo,
# and auto-recovery is off (entrypoint.sh). The plugin settings are re-applied while Studio is
# closed, so Rojo reconnects and syncs on open without a prompt.
#   studio-reopen [--wait]     --wait: block until the slot's window is back (max 240 s)
set -u
PREFIX="$HOME/.local/share/vinegar/prefixes/studio"
RBX="$PREFIX/drive_c/users/$(id -un)/AppData/Local/Roblox"
touch /tmp/studio-reopening
for p in $(pgrep -f '[R]obloxStudioBeta'); do kill "$p" 2>/dev/null; done
sleep 2
WINEPREFIX="$PREFIX" wineserver -k 2>/dev/null
sleep 2
node /opt/studio-slot/studio-plugin-settings.js "$RBX" ${ROJO_NEVER_PLACE_IDS:-138024886323256} \
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
