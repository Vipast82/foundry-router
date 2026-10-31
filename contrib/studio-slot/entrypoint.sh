#!/bin/bash
# Start a virtual X desktop (TigerVNC), a window manager and the noVNC browser bridge,
# then Vinegar. Open http://localhost:6080/vnc.html to see the desktop.
set -u
GEOM="${GEOMETRY:-2560x1440}"
mkdir -p "$HOME/.vnc" "$XDG_RUNTIME_DIR"

# VNC itself only listens inside the container (localhost); noVNC is the way in.
Xvnc :1 -geometry "$GEOM" -depth 24 -SecurityTypes None -localhost yes \
     -AlwaysShared -AcceptSetDesktopSize >"$HOME/xvnc.log" 2>&1 &
for _ in $(seq 1 50); do [ -e /tmp/.X11-unix/X1 ] && break; sleep 0.2; done

eval "$(dbus-launch --sh-syntax)"
openbox >"$HOME/openbox.log" 2>&1 &
websockify --web /usr/share/novnc 6080 localhost:5901 >"$HOME/novnc.log" 2>&1 &

# a terminal on the desktop for manual checks
xterm -geometry 120x30+20+20 -iconic >/dev/null 2>&1 &

# Vinegar settings that work in a container (see vinegar-config.toml); kept once edited
mkdir -p "$HOME/.config/vinegar"
[ -s "$HOME/.config/vinegar/config.toml" ] || \
    cp /etc/studio-slot/vinegar-config.toml "$HOME/.config/vinegar/config.toml"

# Studio plugins (Rojo + the robloxstudio-mcp plugin). Studio reads them from the Wine
# prefix's %LOCALAPPDATA%\Roblox\Plugins, not Vinegar's appdata folder. The prefix only
# exists after Vinegar's first run, so wait for it, then copy (newer files only).
PLUGDIR="$HOME/.local/share/vinegar/prefixes/studio/drive_c/users/$(id -un)/AppData/Local/Roblox/Plugins"
(
    for _ in $(seq 1 600); do
        [ -d "$(dirname "$(dirname "$PLUGDIR")")" ] && break; sleep 2
    done
    mkdir -p "$PLUGDIR" && cp -u /opt/studio-slot/plugins/* "$PLUGDIR/"
) &

# The slot's own AU checkout (gate runs check out the commit under test there), cloned
# once from the mounted read-only checkout (AU_SOURCE) into the home volume (AU_CHECKOUT).
# Its "origin" is the mount, so `git fetch` picks up new commits and track branches.
AU_SOURCE="${AU_SOURCE:-/work/AnimalUprising}"
AU_CHECKOUT="${AU_CHECKOUT:-$HOME/work/AnimalUprising}"
# the mount is owned by another user: git needs both the folder and its .git listed
for d in "$AU_SOURCE" "$AU_SOURCE/.git"; do
    git config --global --get-all safe.directory | grep -qxF "$d" || \
        git config --global --add safe.directory "$d"
done
if [ ! -d "$AU_CHECKOUT/.git" ] && [ -d "$AU_SOURCE/.git" ]; then
    mkdir -p "$(dirname "$AU_CHECKOUT")"
    git clone -q "$AU_SOURCE" "$AU_CHECKOUT" >"$HOME/checkout.log" 2>&1
fi
[ -d "$AU_CHECKOUT/.git" ] && git -C "$AU_CHECKOUT" fetch -q origin >>"$HOME/checkout.log" 2>&1

# AU's toolchain (lune, selene, stylua, wally) from its rokit.toml, then its wally
# packages (Packages/ is not committed; default.project.json mounts it, so Rojo cannot
# serve without it). Before Rojo starts; slow only on the first start.
export PATH="$HOME/.rokit/bin:$PATH"
if [ -f "$AU_CHECKOUT/rokit.toml" ]; then
    rokit self-install >>"$HOME/rokit.log" 2>&1
    ( cd "$AU_CHECKOUT" && rokit install --no-trust-check >>"$HOME/rokit.log" 2>&1
      [ -d Packages ] || [ ! -f wally.toml ] || wally install >>"$HOME/rokit.log" 2>&1 )
fi

# Rojo: serve the slot's checkout (else the mount). The image's Rojo (7.7.1), not the one
# rokit.toml pins, so it matches the Studio plugin the image installs.
ROJO_PROJECT="${ROJO_PROJECT:-$AU_CHECKOUT/default.project.json}"
[ -f "$ROJO_PROJECT" ] || ROJO_PROJECT="$AU_SOURCE/default.project.json"
if [ -f "$ROJO_PROJECT" ]; then
    # supervised: slot-checkout restarts Rojo by killing it (Rojo 7.7 misses new files)
    ( cd "$(dirname "$ROJO_PROJECT")" && while true; do
          /usr/local/bin/rojo serve --port "${ROJO_PORT:-34872}" "$(basename "$ROJO_PROJECT")" \
              >>"$HOME/rojo.log" 2>&1
          sleep 2
      done ) &
fi

# Plugin settings (Studio is not running yet, so the file can be written): Rojo connects
# and syncs by itself on place open; Studio's MCP server is on. ROJO_NEVER_PLACE_IDS:
# places never auto-synced (LIVE).
node /opt/studio-slot/studio-plugin-settings.js \
    "$HOME/.local/share/vinegar/prefixes/studio/drive_c/users/$(id -un)/AppData/Local/Roblox" \
    ${ROJO_NEVER_PLACE_IDS:-138024886323256} >>"$HOME/rojo.log" 2>&1 || true

# No "Auto Recovery File Found" question on start: a slot's place content comes from
# Rojo, and stopping the container kills Studio (which leaves recovery files behind)
RBX="$HOME/.local/share/vinegar/appdata/Roblox"
rm -f "$RBX"/RobloxStudio/AutoSaves/*_AutoRecovery_*.rbxl 2>/dev/null
[ -f "$RBX/GlobalSettings_13.xml" ] && sed -i \
    's#<bool name="Auto-Recovery Enabled">true#<bool name="Auto-Recovery Enabled">false#' \
    "$RBX/GlobalSettings_13.xml"

# The slot's editor window fills the screen (screenshots capture its viewport): any
# window whose title contains STUDIO_FULLSCREEN_TITLE, checked every few seconds so a
# reopened Studio gets it too. Test server/client windows ("Place1 ...") are left alone.
FS_TITLE="${STUDIO_FULLSCREEN_TITLE:-\[SLOT-}"   # a regex (xdotool --name)
SCREEN_W="${GEOM%x*}"; SCREEN_H="${GEOM#*x}"
(
    while sleep 5; do
        for w in $(xdotool search --name "$FS_TITLE" 2>/dev/null); do
            case "$(xdotool getwindowname "$w" 2>/dev/null)" in *"- Roblox Studio") ;; *) continue ;; esac
            g=$(xdotool getwindowgeometry "$w" 2>/dev/null | awk '/Geometry/{print $2}')
            [ "$g" = "${SCREEN_W}x${SCREEN_H}" ] && continue
            xdotool windowmove "$w" 0 0 windowsize "$w" "$SCREEN_W" "$SCREEN_H" 2>/dev/null
        done
    done
) &

export GTK_A11Y=none
if [ "${START_VINEGAR:-1}" = "1" ]; then
    # STUDIO_OPEN: a roblox-studio: link to open at start (opening one into a running
    # Studio hangs under Wine, so it has to be the first launch)
    vinegar ${STUDIO_OPEN:+"$STUDIO_OPEN"} >"$HOME/vinegar.log" 2>&1 &
    # Watchdog (the Foundry Client's job on the workstation): if Studio has been gone for a
    # minute (a crash, an OOM kill), open STUDIO_OPEN again. Skipped while studio-reopen runs
    # and when there is no place to open (the sign-in start page).
    if [ -n "${STUDIO_OPEN:-}" ]; then
        (
            sleep 120
            gone=0
            while sleep 20; do
                if pgrep -f '[R]obloxStudioBeta' >/dev/null || [ -e /tmp/studio-reopening ]; then
                    gone=0
                else
                    gone=$((gone + 1))
                    if [ "$gone" -ge 3 ]; then
                        echo "$(date -u +%FT%TZ) watchdog: Studio gone for 60 s, reopening" \
                            >>"$HOME/watchdog.log"
                        studio-reopen >>"$HOME/watchdog.log" 2>&1
                        gone=0
                        sleep 120
                    fi
                fi
            done
        ) &
    fi
fi

echo "studio-slot ready: open http://localhost:6080/vnc.html"
wait
