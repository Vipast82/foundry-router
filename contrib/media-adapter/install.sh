#!/usr/bin/env bash
# Install the Foundry media adapter (image tools for Kilo / Cline) on Linux/macOS.
#
#   ./install.sh --project ~/Roblox/AnimalUprising [--foundry http://192.168.0.112:11435]
#                [--default-dir assets/generated/_unapproved] [--extra-root DIR]...
#                [--wait 50] [--set-key]
#
# Copies foundry_media_mcp.py to ~/.local/share/foundry-media, writes its config,
# optionally stores your Foundry client key in ~/.config/foundry-media/key (0600),
# runs a connection check and prints the MCP entries for Kilo and Cline.
set -euo pipefail

FOUNDRY="http://192.168.0.112:11435"
DEFAULT_DIR="assets/generated/_unapproved"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/foundry-media"
WAIT=50
PROJECT=""
SET_KEY=0
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --foundry) FOUNDRY="$2"; shift 2 ;;
    --default-dir) DEFAULT_DIR="$2"; shift 2 ;;
    --extra-root) EXTRA+=("$2"); shift 2 ;;
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    --wait) WAIT="$2"; shift 2 ;;
    --set-key) SET_KEY=1; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
[ -n "$PROJECT" ] || { echo "--project is required" >&2; exit 2; }

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 not found" >&2; exit 1; }
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo "python 3.8+ needed" >&2; exit 1; }

PROJECT="$(cd "$PROJECT" && pwd)"
mkdir -p "$INSTALL_DIR" "$PROJECT/$DEFAULT_DIR"
HERE="$(cd "$(dirname "$0")" && pwd)"
cp "$HERE/foundry_media_mcp.py" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/foundry_media_mcp.py"

ROOTS_JSON="$("$PY" -c 'import json,os,sys; print(json.dumps([os.path.realpath(p) for p in sys.argv[1:]]))' \
  "$PROJECT" ${EXTRA[@]+"${EXTRA[@]}"})"
cat > "$INSTALL_DIR/foundry-media.json" <<EOF
{"foundry_url": "$FOUNDRY", "roots": $ROOTS_JSON, "default_dir": "$DEFAULT_DIR",
 "wait_seconds": $WAIT, "sidecar": true}
EOF

LAUNCH="$INSTALL_DIR/foundry_media_mcp.py"
if [ "$SET_KEY" = 1 ]; then
  KEY_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/foundry-media"
  mkdir -p "$KEY_DIR"
  read -r -s -p "Foundry client key with the 'media' scope: " KEY; echo
  umask 077; printf '%s' "$KEY" > "$KEY_DIR/key"
  # A tiny launcher that reads the key file, so no secret sits in MCP configs.
  cat > "$INSTALL_DIR/foundry-media" <<EOF
#!/usr/bin/env bash
export FOUNDRY_KEY="\$(cat "$KEY_DIR/key")"
exec "$PY" "$INSTALL_DIR/foundry_media_mcp.py" "\$@"
EOF
  chmod 700 "$INSTALL_DIR/foundry-media"
  LAUNCH="$INSTALL_DIR/foundry-media"
  export FOUNDRY_KEY="$KEY"
fi

"$PY" "$INSTALL_DIR/foundry_media_mcp.py" --check || true
echo
if [ "$LAUNCH" = "$INSTALL_DIR/foundry-media" ]; then CMD="\"$LAUNCH\""; ARGS="[]"; KCMD="[\"$LAUNCH\"]"
else CMD="\"$PY\""; ARGS="[\"$LAUNCH\"]"; KCMD="[\"$PY\", \"$LAUNCH\"]"; fi
echo "Kilo (kilo.jsonc, merge into the existing \"mcp\" section; match your other entries' shape):"
echo "{\"mcp\": {\"foundry-media\": {\"type\": \"local\", \"command\": $KCMD, \"enabled\": true}}}"
echo
echo "Cline (cline_mcp_settings.json):"
echo "{\"mcpServers\": {\"foundry-media\": {\"command\": $CMD, \"args\": $ARGS, \"timeout\": 300,"
echo "  \"disabled\": false, \"autoApprove\": [\"image_job_status\", \"list_image_jobs\", \"image_settings\"]}}}"
echo
echo "Skill: cp -r \"$HERE/skills/foundry-images\" \"$PROJECT/.kilo/skills/\""
