"""Client identity and access scopes.

Each client (Kilo, Cline, OpenWebUI, AnythingLLM, a script) gets its own
Foundry key in `clients:`. A request's key identifies the client (`client_id`
in the logs and budgets) and its scopes decide what it may reach:

  inference  /api/* (Ollama) and /v1/* chat/completions/responses/embeddings/models
  media      /v1/images/*, /media/*, /mcp/media (image jobs and artifacts)
  admin      /admin/* (the web UI's API; the /ui page shell itself is static)

`client_auth.mode`:
  optional (default) — keyless requests pass as client "anonymous" (so Cline's
                       Ollama provider and existing chat clients keep working
                       while keys are rolled out); a WRONG key is refused.
  required           — inference/media need a valid key.
`client_auth.protect_admin` — also require an admin-scoped key for /admin and
                       /ui (off by default so nobody locks themselves out).

Keys are read from `Authorization: Bearer`, `x-api-key` or `x-foundry-key`.
Hermes agent caller tokens remain valid (client "agent:<name>").
Correlation headers a client may add — x-foundry-session, x-foundry-task,
x-foundry-parent-task — are recorded with the client id. Prompt text is never
used to infer them.
"""

from __future__ import annotations

import contextvars
import hmac
import json
import secrets
from typing import Callable, Optional

_client: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "foundry_client", default=None)

CORRELATION_HEADERS = {"x-foundry-session": "session_id", "x-foundry-task": "task_id",
                       "x-foundry-parent-task": "parent_task_id"}
_OPEN_PATHS = ("/", "/api/version")          # liveness probes stay open

# Foundry calling itself (persona validation runs the real HTTP route
# in-process). A per-process random secret: never configured, logged or sent
# anywhere, so only code inside this process can present it.
INTERNAL_HEADER = "x-foundry-internal"
INTERNAL_TOKEN = secrets.token_urlsafe(32)


def current() -> dict:
    """{client_id, scopes, personas, session_id, task_id, parent_task_id}."""
    return dict(_client.get() or {"client_id": "anonymous"})


def scope_for(path: str) -> Optional[str]:
    if path in _OPEN_PATHS:
        return None
    if path.startswith("/mcp/media"):
        return "media"         # Foundry's built-in image tools
    if path.startswith("/mcp"):
        return None            # Foundry-MCP has its own token (mcp_aggregator.token)
    if path.startswith("/admin"):
        # The /ui page is a static shell (no data); its data comes from /admin/api,
        # which the UI calls with the admin key the user enters once.
        return "admin"
    if path.startswith("/v1/images") or path.startswith("/media"):
        return "media"
    if path.startswith("/api/") or path.startswith("/v1/"):
        return "inference"
    return None


def _keys_from(headers: dict) -> list[str]:
    out = []
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        out.append(auth[7:].strip())
    for h in ("x-api-key", "x-foundry-key"):
        if headers.get(h):
            out.append(headers[h].strip())
    return [k for k in out if k]


def identify(cfg, headers: dict, agent_tokens: Optional[dict] = None) -> tuple[Optional[dict], bool]:
    """(client or None, key_was_presented). Constant-time key comparison."""
    internal = headers.get(INTERNAL_HEADER)
    if internal and hmac.compare_digest(internal.encode(), INTERNAL_TOKEN.encode()):
        return ({"client_id": "foundry:validate", "scopes": ["inference", "media"],
                 "personas": [], "internal": True}, True)
    keys = _keys_from(headers)
    for k in keys:
        for c in getattr(cfg, "clients", []) or []:
            if not c.enabled or not c.key:
                continue
            if hmac.compare_digest(k.encode(), str(c.key).encode()):
                return ({"client_id": c.name, "scopes": list(c.scopes or ["inference"]),
                         "personas": list(c.personas or [])}, True)
        for tok, name in (agent_tokens or {}).items():
            if tok and hmac.compare_digest(k.encode(), tok.encode()):
                return ({"client_id": f"agent:{name}", "scopes": ["inference", "media"],
                         "personas": []}, True)
    return None, bool(keys)


def _deny(status: int, message: str, code: str) -> tuple[int, bytes]:
    body = {"error": {"message": message, "type": "authentication_error"
                      if status == 401 else "permission_error", "code": code}}
    return status, json.dumps(body).encode()


class ClientAuthMiddleware:
    def __init__(self, app, get_config: Callable, get_agent_tokens: Callable = lambda: {}):
        self.app = app
        self.get_config = get_config
        self.get_agent_tokens = get_agent_tokens

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        cfg = self.get_config()
        ca = getattr(cfg, "client_auth", None)
        path = scope.get("path") or "/"
        need = scope_for(path)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers") or []}
        client, presented = identify(cfg, headers, self.get_agent_tokens())
        corr = {f: headers[h][:120] for h, f in CORRELATION_HEADERS.items() if headers.get(h)}
        mode = getattr(ca, "mode", "optional") if ca else "optional"
        protect_admin = bool(getattr(ca, "protect_admin", False)) if ca else False
        problem = None
        if need and presented and client is None and getattr(cfg, "clients", None):
            # A key that matches nothing is always refused — a typo must not
            # silently become "anonymous".
            problem = _deny(401, "unknown Foundry client key", "invalid_api_key")
        elif need in ("inference", "media"):
            if client is None and mode == "required":
                problem = _deny(401, "this Foundry requires a client key "
                                     "(Authorization: Bearer <key>)", "missing_api_key")
            elif client is not None and need not in client["scopes"]:
                problem = _deny(403, f"client {client['client_id']!r} has no {need} access",
                                "insufficient_scope")
        elif need == "admin" and protect_admin:
            if client is None or "admin" not in client["scopes"]:
                problem = _deny(401 if client is None else 403,
                                "the Foundry admin UI/API needs an admin-scoped client key",
                                "admin_key_required")
        if problem is not None:
            status, body = problem
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": body})
            return
        info = {**(client or {"client_id": "anonymous", "scopes": [], "personas": []}), **corr}
        token = _client.set(info)
        try:
            await self.app(scope, receive, send)
        finally:
            _client.reset(token)


def persona_allowed(persona_name: str) -> bool:
    """False when the client is restricted to certain personas and this isn't one."""
    allowed = (_client.get() or {}).get("personas") or []
    if not allowed:
        return True
    base = (persona_name or "").split(":")[0]
    return persona_name in allowed or base in allowed
