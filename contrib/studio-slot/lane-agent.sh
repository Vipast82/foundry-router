#!/bin/bash
# The slot's lane agent (Distributed Foundry phase b, docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md
# B2): the Foundry Client, headless, with [lane] on. entrypoint.sh starts it when SLOT_LANES=1
# (docker-compose.lanes.yml). It supervises this slot's own `kilo serve` and `opencode serve`
# and answers the coordinator's lane calls (foundry_client/lane.py) through the ssh tunnel.
#
# Secrets, all in /run/secrets (a tmpfs, mode 600, gone when the container stops):
#   foundry_key_kilo.txt / foundry_key_opencode.txt   this slot's own model keys, pulled by
#       slot-secrets from the router (Access → Machines → Lanes: on)
#   lane/client-secrets.json   the client's secret store for this run: the kilo / opencode
#       server passwords (made fresh at each start) and the two model keys
#   lane/lane.json             written by the client at its start: the lane token and the two
#       passwords, which the coordinator reads over ssh when it opens the tunnel
# Nothing is printed, logged or put on a command line.
set -u
ROOT="${SLOT_LANES_ROOT:-/srv/foundry-lanes}"
LANE_DIR=/run/secrets/lane
CONF_DIR="$HOME/.config/foundry-client"
LOG="$HOME/lane-agent.log"
PY=/opt/foundry-venv/bin/python
[ -x "$PY" ] || PY=python3
mkdir -p "$CONF_DIR" "$HOME/.foundry-coordinator"
(umask 077; mkdir -p "$LANE_DIR")

if [ ! -d "$ROOT" ] || [ ! -w "$ROOT" ]; then
    echo "$(date -u +%FT%TZ) lane-agent: $ROOT is missing or not writable: run setup-host.sh with lanes on" >>"$LOG"
    exit 1
fi

# the client's secret store for this run (a JSON file the client reads in `file` mode)
"$PY" - "$LANE_DIR/client-secrets.json" <<'PYEOF'
import json, os, secrets, sys
out = sys.argv[1]
vals = {"KILO_SERVER_PASSWORD": secrets.token_urlsafe(24),
        "OPENCODE_SERVER_PASSWORD": secrets.token_urlsafe(24)}
for name, f in (("FOUNDRY_KEY_KILO", "foundry_key_kilo.txt"),
                ("FOUNDRY_KEY_OPENCODE", "foundry_key_opencode.txt")):
    p = os.path.join("/run/secrets", f)
    if os.path.isfile(p):
        v = open(p, encoding="utf-8").read().strip()
        if v:
            vals[name] = v
tmp = out + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump({"foundry-client": vals}, fh)
os.replace(tmp, out)
PYEOF

# the client's settings for a lane host: kilo + opencode on all addresses inside the container
# (compose publishes them on the host's 127.0.0.1 only), no coordinator / supervisor / Rojo
"$PY" - "$CONF_DIR/client.toml" "$ROOT" "$LANE_DIR" "${FOUNDRY_URL:-}" <<'PYEOF'
import os, sys
path, root, lane_dir, router = sys.argv[1:5]
def q(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'
text = f"""# written by lane-agent.sh at each start (Distributed Foundry phase b); edits are replaced
[client]
backend_url = {q(router or "http://127.0.0.1:11435")}
secret_store = "file"
secrets_file = {q(os.path.join(lane_dir, "client-secrets.json"))}
relay_enabled = false
update_check = false
auto_update = false
open_settings_on_start = false

[workspace]
game = {q(root)}

[lane]
enabled = true
root = {q(root)}
secrets_file = {q(os.path.join(lane_dir, "lane.json"))}
listen = "0.0.0.0"

[components.kilo]
enabled = true
hostname = "0.0.0.0"
port = 4096
secrets = ["KILO_SERVER_PASSWORD", "FOUNDRY_KEY_KILO"]

[components.opencode]
enabled = true
hostname = "0.0.0.0"
port = 4097
secrets = ["OPENCODE_SERVER_PASSWORD", "FOUNDRY_KEY_OPENCODE"]

[components.coordinator]
enabled = false

[components.supervisor]
enabled = false

[components.rojo]
enabled = false
"""
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(text)
os.replace(tmp, path)
PYEOF

echo "$(date -u +%FT%TZ) lane-agent: starting (root $ROOT)" >>"$LOG"
cd "$ROOT" || exit 1
# supervised: the client restarts if it exits (kilo / opencode are the client's own children)
while true; do
    PYTHONPATH=/opt/foundry PYTHONIOENCODING=utf-8 XDG_CONFIG_HOME="$HOME/.config" \
        "$PY" -m foundry_client run --headless >>"$LOG" 2>&1
    echo "$(date -u +%FT%TZ) lane-agent: the client exited ($?); restarting in 10 s" >>"$LOG"
    sleep 10
done
