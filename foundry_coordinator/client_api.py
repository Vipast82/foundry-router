"""The Foundry Client's local API, as the coordinator sees it (W1.8, 2026-10-22).

The client (foundry_client) manages Kilo, Rojo, the coordinator and the upgrade
supervisor on the workstation and serves a token-guarded API on 127.0.0.1 only
(foundry_client/api.py): `GET /api/status`, `POST /api/components/<name>/<verb>`
(start | stop | restart, `?force=true` skips the safe-point wait). The per-install
token lives in the client's config folder (`%APPDATA%\\FoundryClient\\api.token` on
Windows, `$XDG_CONFIG_HOME/foundry-client/api.token` elsewhere) and the port in
its `client.toml` (`client.api_port`, default 47811).

`discover()` reads both; `None` when the client is not installed here, so every
caller degrades to "not reachable" and never raises. The coordinator uses it for
deterministic repairs (autoheal.repair): restart Rojo when it is down or stale.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

DEFAULT_PORT = 47811
TIMEOUT = 5.0


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "FoundryClient"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "foundry-client"


def token_path() -> Path:
    return config_dir() / "api.token"


def read_token() -> str:
    try:
        t = token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return t if len(t) >= 32 else ""


def read_port() -> int:
    p = Path(os.environ.get("FOUNDRY_CLIENT_CONFIG") or (config_dir() / "client.toml"))
    try:
        with open(p, "rb") as f:
            cfg = tomllib.load(f)
        port = int((cfg.get("client") or {}).get("api_port") or DEFAULT_PORT)
        return port if 1024 <= port <= 65535 else DEFAULT_PORT
    except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError):
        return DEFAULT_PORT


class ClientAPI:
    def __init__(self, url: str, token: str, timeout: float = TIMEOUT):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, method: str, path: str, body=None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("x-client-token", self.token)
        req.add_header("content-type", "application/json")
        req.add_header("host", "127.0.0.1")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise ClientError(f"{method} {path}: HTTP {e.code} {e.read()[:200]!r}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
                ValueError) as e:
            raise ClientError(f"{method} {path}: {e}") from None

    def status(self) -> dict:
        return self._req("GET", "/api/status")

    def component(self, name: str) -> Optional[dict]:
        for c in (self.status().get("components") or []):
            if c.get("name") == name:
                return c
        return None

    def restart(self, name: str, force: bool = False) -> dict:
        return self._req("POST", f"/api/components/{name}/restart"
                         + ("?force=true" if force else ""))

    def reachable(self) -> bool:
        try:
            self.status()
            return True
        except ClientError:
            return False


class ClientError(RuntimeError):
    pass


def discover(timeout: float = TIMEOUT) -> Optional[ClientAPI]:
    """The local client API when its token exists here, else None."""
    token = read_token()
    if not token:
        return None
    return ClientAPI(f"http://127.0.0.1:{read_port()}", token, timeout=timeout)


def restart_component(name: str, *, force: bool = False, api: Optional[ClientAPI] = None) -> str:
    """Ask the client to restart `name` (rojo, kilo, ...). Returns '' on success,
    else why not (no client, unreachable, the component is not managed)."""
    api = api or discover()
    if api is None:
        return "no Foundry Client on this workstation (no api.token)"
    try:
        comp = api.component(name)
    except ClientError as e:
        return f"client unreachable: {e}"[:200]
    if comp is None:
        return f"the client manages no component {name!r}"
    if not comp.get("enabled", True) and not comp.get("want"):
        return f"component {name!r} is disabled in the client"
    try:
        api.restart(name, force=force)
    except ClientError as e:
        return f"restart refused: {e}"[:200]
    return ""
