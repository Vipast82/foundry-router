#!/usr/bin/env python3
"""Docker MCP Gateway inspect companion — a tiny HTTP shim on the gateway host.

`mcp-find` (the gateway's catalog SEARCH tool) returns only a summary — name,
description, sometimes config — but NOT tool count, publisher, or the full
config/secrets detail. That richer data comes from the host CLI:

    docker mcp catalog server inspect <catalog> <server>

which has no MCP tool, so Foundry Router (a remote MCP client) can't run it.
This service closes that gap: it runs on `general-ai` next to the gateway and
exposes ONE endpoint that runs that command and returns its output. Foundry's
"MCP > Gateway Servers" panel calls it per-row on an Inspect click.

Same trust model as the (future) secrets companion: it can run docker commands
on the host, so bind it to localhost and firewall it to Foundry Router's host,
and set a bearer token. Stdlib only — no pip install.

Run:
    GATEWAY_INSPECT_TOKEN=$(openssl rand -hex 16) \
    python3 gateway_inspect_service.py            # 127.0.0.1:8899 by default

Env:
    GATEWAY_INSPECT_BIND    default 127.0.0.1
    GATEWAY_INSPECT_PORT    default 8899
    GATEWAY_INSPECT_TOKEN   optional bearer token (STRONGLY recommended)
    GATEWAY_INSPECT_CATALOG default catalog name (default "docker-mcp")
    GATEWAY_DOCKER          docker binary (default "docker")
    GATEWAY_INSPECT_TIMEOUT per-command timeout seconds (default 30)

Endpoints:
    GET  /health           -> {"ok": true}
    POST /inspect          -> {"ok", "exit_code", "raw", "server", "catalog"}
         body: {"server": "playwright", "catalog": "docker-mcp"(optional)}
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = os.environ.get("GATEWAY_INSPECT_BIND", "127.0.0.1")
PORT = int(os.environ.get("GATEWAY_INSPECT_PORT", "8899"))
TOKEN = os.environ.get("GATEWAY_INSPECT_TOKEN", "")
DEFAULT_CATALOG = os.environ.get("GATEWAY_INSPECT_CATALOG", "docker-mcp")
DOCKER = os.environ.get("GATEWAY_DOCKER", "docker")
TIMEOUT = int(os.environ.get("GATEWAY_INSPECT_TIMEOUT", "30"))

# A catalog/server ref is operator-facing but still goes on a command line —
# constrain it hard so this can never become a shell-injection foothold.
_SAFE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") == "/health":
            return self._send(200, {"ok": True})
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path.rstrip("/") != "/inspect":
            return self._send(404, {"ok": False, "error": "not found"})
        if not self._authed():
            return self._send(401, {"ok": False, "error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or "{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"ok": False, "error": "bad JSON body"})

        server = str(body.get("server") or "").strip()
        catalog = str(body.get("catalog") or DEFAULT_CATALOG).strip()
        if not _SAFE.match(server) or not _SAFE.match(catalog):
            return self._send(400, {"ok": False,
                                    "error": "server/catalog must match [A-Za-z0-9._/-]"})

        cmd = [DOCKER, "mcp", "catalog", "server", "inspect", catalog, server]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            return self._send(504, {"ok": False, "error": f"timed out after {TIMEOUT}s",
                                    "server": server, "catalog": catalog})
        except FileNotFoundError:
            return self._send(500, {"ok": False,
                                    "error": f"{DOCKER!r} not found on PATH"})
        raw = proc.stdout or ""
        if proc.returncode != 0:
            raw = (raw + "\n" + (proc.stderr or "")).strip()
        self._send(200, {"ok": proc.returncode == 0, "exit_code": proc.returncode,
                         "raw": raw, "server": server, "catalog": catalog})

    def log_message(self, *args):  # keep stdout quiet
        pass


if __name__ == "__main__":
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"gateway inspect service on http://{BIND}:{PORT} "
          f"(token {'set' if TOKEN else 'NOT set — set GATEWAY_INSPECT_TOKEN'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
