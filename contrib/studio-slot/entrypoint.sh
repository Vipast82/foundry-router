#!/bin/bash
# Start a virtual X desktop (TigerVNC), a window manager and the noVNC browser bridge,
# then Vinegar. Open http://localhost:6080/vnc.html to see the desktop.
set -u
GEOM="${GEOMETRY:-2560x1440}"
mkdir -p "$HOME/.vnc" "$XDG_RUNTIME_DIR"

# This slot's secrets and settings from the router (Setup → Secrets), with the slot's own key
# (a mounted file): values go to /run/secrets (a tmpfs, memory only), never the image, the
# environment or a log. The router's slot settings (place, universe, never-sync places) come as
# slot.env and win over the compose values. Re-pulled by slot-checkout before every gate run.
slot-secrets >>"$HOME/slot-secrets.log" 2>&1 && [ -f "$HOME/.config/foundry-slot/slot.env" ] && \
    . "$HOME/.config/foundry-slot/slot.env"

# VNC itself only listens inside the container (localhost); noVNC is the way in. A VNC password
# comes from the router like any secret (SLOT_VNC_PASSWORD, file slot_vnc_password.txt, Access →
# Secrets): turned into TigerVNC's password file on the tmpfs, never on a command line or in a
# log. VNC uses its first 8 characters. The password is optional: without it the desktop has
# none.
VNC_SEC=(-SecurityTypes None)
PW_IN=/run/secrets/slot_vnc_password.txt PW_OUT=/run/secrets/.vncpasswd
rm -f "$PW_OUT"
if [ -s "$PW_IN" ] && (umask 077; vncpasswd -f <"$PW_IN" >"$PW_OUT") 2>/dev/null && [ -s "$PW_OUT" ]; then
    VNC_SEC=(-SecurityTypes VncAuth -PasswordFile "$PW_OUT")
    echo "VNC password: on" >"$HOME/xvnc.log"
else
    rm -f "$PW_OUT"
    echo "VNC password: none (optional: SLOT_VNC_PASSWORD on Access → Secrets)" >"$HOME/xvnc.log"
fi
Xvnc :1 -geometry "$GEOM" -depth 24 "${VNC_SEC[@]}" -localhost yes \
     -AlwaysShared -AcceptSetDesktopSize >>"$HOME/xvnc.log" 2>&1 &
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
# AU_SOURCE is a working checkout (this PC: the mounted workstation folder) or a bare mirror
# (a Proxmox slot host: /srv/studio-slot/AnimalUprising.git, which Foundry pushes track commits to)
if [ ! -d "$AU_CHECKOUT/.git" ] && git -C "$AU_SOURCE" rev-parse --git-dir >/dev/null 2>&1 \
        && git -C "$AU_SOURCE" rev-parse -q --verify HEAD >/dev/null 2>&1; then
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
[ -f "$ROJO_PROJECT" ] || [ ! -f "$AU_SOURCE/default.project.json" ] ||     ROJO_PROJECT="$AU_SOURCE/default.project.json"
# A new slot host's game mirror may still be empty: then the first gate's slot-checkout clones
# the game, and Rojo starts as soon as the project file exists.
if true; then
    # supervised: slot-checkout restarts Rojo by killing it (Rojo 7.7 misses new files)
    ( while [ ! -f "$ROJO_PROJECT" ]; do sleep 10; done
      cd "$(dirname "$ROJO_PROJECT")" && while true; do
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
    $(ROJO_PROJECT="$ROJO_PROJECT" rojo-seed) \
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
# Under Wine, Studio's splash (a 600x300 window titled "RobloxStudio") can stay on top of the
# editor after it loads (2026-11-01, slot-1): once the editor window is there, it is hidden
# (unmapped, not closed: closing a window of Studio's process could close Studio).
FS_TITLE="${STUDIO_FULLSCREEN_TITLE:-\[SLOT-}"   # a regex (xdotool --name)
SCREEN_W="${GEOM%x*}"; SCREEN_H="${GEOM#*x}"
(
    while sleep 5; do
        editor=0
        for w in $(xdotool search --name "$FS_TITLE" 2>/dev/null); do
            case "$(xdotool getwindowname "$w" 2>/dev/null)" in *"- Roblox Studio") ;; *) continue ;; esac
            editor=1
            g=$(xdotool getwindowgeometry "$w" 2>/dev/null | awk '/Geometry/{print $2}')
            [ "$g" = "${SCREEN_W}x${SCREEN_H}" ] && continue
            xdotool windowmove "$w" 0 0 windowsize "$w" "$SCREEN_W" "$SCREEN_H" 2>/dev/null
        done
        [ "$editor" = 1 ] || continue
        for s in $(xdotool search --onlyvisible --name '^RobloxStudio$' 2>/dev/null); do
            xdotool windowunmap "$s" 2>/dev/null
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
                # the EDIT Studio only: its program is RobloxStudioBeta.exe and it is not a playtest
                # server/client (-studiotestservicemode). Those, and the WebView2 helpers (whose
                # args name RobloxStudioBeta.exe), outlive a crashed editor and kept this watchdog
                # quiet (2026-10-31, AS-J05)
                if pgrep -fa 'RobloxStudioBeta' | awk '$2 ~ /RobloxStudioBeta\.exe$/' \
                        | grep -qv -- '-studiotestservicemode' || [ -e /tmp/studio-reopening ]; then
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
