#!/usr/bin/env bash
# Install the Meridian auth companion as a systemd service.
#
# Run as ROOT on the Meridian host — the LXC/VM where the `meridian` CLI works
# (the one you `pct enter` into and run `meridian profile login` on). It fetches
# the service, writes a systemd unit, and starts it.
#
#   curl -fsSL https://raw.githubusercontent.com/Vipast82/foundry-router/main/contrib/meridian-auth-service/install.sh | bash
#
# Or download and run with overrides:
#   MERIDIAN_AUTH_PORT=8898 MERIDIAN_AUTH_TOKEN=$(openssl rand -hex 16) bash install.sh
#
# Env (all optional):
#   REPO_RAW             raw repo base (default: GitHub main)
#   DEST                 install dir           (default /opt/meridian-auth)
#   MERIDIAN_AUTH_BIND   bind address          (default 0.0.0.0 — see note below)
#   MERIDIAN_AUTH_PORT   port                  (default 8898)
#   MERIDIAN_AUTH_TOKEN  bearer token          (default: generated)
#   MERIDIAN_BIN         meridian CLI path     (default: meridian on PATH)
#   SERVICE              systemd unit name     (default meridian-auth)
set -euo pipefail

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/Vipast82/foundry-router/main}"
DEST="${DEST:-/opt/meridian-auth}"
BIND="${MERIDIAN_AUTH_BIND:-0.0.0.0}"
PORT="${MERIDIAN_AUTH_PORT:-8898}"
TOKEN="${MERIDIAN_AUTH_TOKEN:-$(openssl rand -hex 16)}"
MERIDIAN_BIN="${MERIDIAN_BIN:-$(command -v meridian || echo meridian)}"
SERVICE="${SERVICE:-meridian-auth}"

echo "==> Installing $SERVICE to $DEST (bind $BIND:$PORT)"
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
command -v "$MERIDIAN_BIN" >/dev/null 2>&1 || \
  echo "WARNING: 'meridian' not found on PATH — set MERIDIAN_BIN if it lives elsewhere"

mkdir -p "$DEST"
curl -fsSL "$REPO_RAW/contrib/meridian-auth-service/meridian_auth_service.py" \
  -o "$DEST/meridian_auth_service.py"

cat >"/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Meridian auth companion for Foundry Router
After=network.target

[Service]
Environment=MERIDIAN_AUTH_BIND=$BIND
Environment=MERIDIAN_AUTH_PORT=$PORT
Environment=MERIDIAN_AUTH_TOKEN=$TOKEN
Environment=MERIDIAN_BIN=$MERIDIAN_BIN
ExecStart=/usr/bin/python3 $DEST/meridian_auth_service.py
Restart=on-failure
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE"
sleep 1
systemctl --no-pager --full status "$SERVICE" | head -n 6 || true

echo
echo "==> Done. Health check:"
curl -fsS "http://127.0.0.1:$PORT/health" && echo
echo
echo "In Foundry Router → Backend Pool → Meridian re-authentication → Auth companion service:"
echo "   companion URL : http://<this-host-LAN-ip>:$PORT"
echo "   token         : $TOKEN"
echo
echo "SECURITY: bound to $BIND. Firewall port $PORT to Foundry's host/subnet only."
