#!/usr/bin/env bash
# Install the Docker MCP Gateway inspect companion as a systemd service.
#
# Run as ROOT on the GATEWAY host — the machine where `docker mcp catalog server
# inspect` works (the host running your Docker MCP Gateway). It fetches the
# service, writes a systemd unit, and starts it.
#
#   curl -fsSL https://raw.githubusercontent.com/Vipast82/foundry-router/main/contrib/gateway-admin-service/install.sh | bash
#
# Env (all optional):
#   REPO_RAW               raw repo base       (default: GitHub main)
#   DEST                   install dir         (default /opt/gateway-inspect)
#   GATEWAY_INSPECT_BIND   bind address        (default 0.0.0.0 — firewall it)
#   GATEWAY_INSPECT_PORT   port                (default 8899)
#   GATEWAY_INSPECT_TOKEN  bearer token        (default: generated)
#   GATEWAY_INSPECT_CATALOG default catalog    (default docker-mcp)
#   GATEWAY_DOCKER         docker binary       (default: docker on PATH)
#   SERVICE                systemd unit name   (default gateway-inspect)
set -euo pipefail

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/Vipast82/foundry-router/main}"
DEST="${DEST:-/opt/gateway-inspect}"
BIND="${GATEWAY_INSPECT_BIND:-0.0.0.0}"
PORT="${GATEWAY_INSPECT_PORT:-8899}"
TOKEN="${GATEWAY_INSPECT_TOKEN:-$(openssl rand -hex 16)}"
CATALOG="${GATEWAY_INSPECT_CATALOG:-docker-mcp}"
DOCKER_BIN="${GATEWAY_DOCKER:-$(command -v docker || echo docker)}"
SERVICE="${SERVICE:-gateway-inspect}"

echo "==> Installing $SERVICE to $DEST (bind $BIND:$PORT)"
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
command -v "$DOCKER_BIN" >/dev/null 2>&1 || \
  echo "WARNING: 'docker' not found on PATH — set GATEWAY_DOCKER if it lives elsewhere"

mkdir -p "$DEST"
curl -fsSL "$REPO_RAW/contrib/gateway-admin-service/gateway_inspect_service.py" \
  -o "$DEST/gateway_inspect_service.py"

cat >"/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Docker MCP Gateway inspect companion for Foundry Router
After=network.target docker.service

[Service]
Environment=GATEWAY_INSPECT_BIND=$BIND
Environment=GATEWAY_INSPECT_PORT=$PORT
Environment=GATEWAY_INSPECT_TOKEN=$TOKEN
Environment=GATEWAY_INSPECT_CATALOG=$CATALOG
Environment=GATEWAY_DOCKER=$DOCKER_BIN
ExecStart=/usr/bin/python3 $DEST/gateway_inspect_service.py
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
echo "In Foundry Router → MCP → Gateway Servers → Inspect companion service:"
echo "   inspect URL : http://<this-host-LAN-ip>:$PORT"
echo "   token       : $TOKEN"
echo
echo "SECURITY: bound to $BIND. Firewall port $PORT to Foundry's host/subnet only."
