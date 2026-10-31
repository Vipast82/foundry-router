#!/bin/bash
# Handler for roblox-studio-auth: links (the browser sign-in hand-back).
# Studio normally passes the sign-in code from this new copy to the copy already running;
# under Wine in a container that hand-off hangs ("Connecting..." forever). So stop the
# running Studio first: the copy started with the code is then the only one and signs in.
PREFIX="$HOME/.local/share/vinegar/prefixes/studio"
for p in $(pgrep -f 'RobloxStudioBeta.exe' | grep -v "^$$\$"); do kill "$p" 2>/dev/null; done
WINEPREFIX="$PREFIX" wineserver -k 2>/dev/null
sleep 2
exec vinegar "$@"
