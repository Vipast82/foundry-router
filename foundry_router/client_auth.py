"""Client identity and access scopes.

Each client (Kilo, Cline, OpenWebUI, AnythingLLM, a script) gets its own
Foundry key in `clients:`. A request's key identifies the client (`client_id`
in the logs and budgets) and its scopes decide what it may reach:

  inference  /api/* (Ollama) and /v1/* chat/completions/responses/embeddings/models
  media      /v1/images/*, /v1/audio/*, /media/*, /mcp/media (image jobs and artifacts,
             speech) — except /media/a/<token>: signed, expiring links to one
             stored image/audio file, readable WITHOUT a key so a browser can
             render them in a chat. The only deliberate unauthenticated read.
  admin      /admin/* (the web UI's API; the /ui page shell itself is static)
  orchestration  /v1/foundry/* (coordinator bindings/state, claiming operator actions)
  telemetry  /v1/foundry/telemetry/* (the workstation bridge's observations; also
             accepted from an orchestration key)
  operator   the coordinator control center (/control/api/*): a person signs in
             once with this key and gets an HttpOnly session; the key is never
             kept by the browser (control_api.py). admin also works.

`client_auth.mode`:
  optional (default) — keyless requests pass as client "anonymous" (so Cline's
                       Ollama provider and existing chat clients keep working
                       while keys are rolled out); a WRONG key is refused.
  required           — inference/media need a valid key.
`client_auth.unknown_keys` — refuse (default) a key that matches no client, or
                       (optional mode only) let it through as anonymous.
Keys can be managed in the web UI (Setup → Clients & keys); those are stored
hashed (client_keys.py). Recent unknown keys are listed there by fingerprint.
`client_auth.protect_admin` — also require an admin-scoped key for /admin and
                       /ui (off by default so nobody locks themselves out).

Keys are read from `Authorization: Bearer`, `x-api-key` or `x-foundry-key`.
Hermes agent caller tokens remain valid (client "agent:<name>").
Correlation headers a client may add — x-foundry-session, x-foundry-task,
x-foundry-parent-task — are recorded with the client id. Without
x-foundry-session, a client's own session header (Kilo/OpenCode send
x-session-id) is used as the session id. A session a coordinator registered
(orchestration.py) adds its project, coordinator task, run and role. Prompt
text is never used to infer any of them.
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
# A client's own session id when it sends no x-foundry-session (Kilo and
# OpenCode send x-session-id = their session id on every model call).
SESSION_FALLBACK_HEADERS = ("x-session-id", "x-opencode-session", "x-session-affinity")
_OPEN_PATHS = ("/", "/api/version", "/health")          # liveness probes stay open
# Signed media links (see scope_for) — open by design, token-checked by the route.
PUBLIC_MEDIA_PREFIX = "/media/a/"

# Foundry calling itself (persona validation runs the real HTTP route
# in-process). A per-process random secret: never configured, logged or sent
# anywhere, so only code inside this process can present it.
INTERNAL_HEADER = "x-foundry-internal"
INTERNAL_TOKEN = secrets.token_urlsafe(32)
# The operator chat's in-process admin reads/writes (chat_router_tools.py):
# admin scope, never sent off the process, a fresh value per start.
INTERNAL_ADMIN_HEADER = "x-foundry-internal-admin"
INTERNAL_ADMIN_TOKEN = secrets.token_urlsafe(32)
# The Foundry Client's relay (foundry_client/relay.py) heart-beats, claims its
# stack actions and reports them with an operator-scoped key. These routes
# admit that scope; control_api.py then limits such a key to source kind
# `client` and to client_* actions (it never claims coordinator actions).
CLIENT_RELAY_PATHS = ("/v1/foundry/telemetry/heartbeat", "/v1/foundry/control/claim")


def current() -> dict:
    """{client_id, scopes, personas, session_id, task_id, parent_task_id}."""
    return dict(_client.get() or {"client_id": "anonymous"})


def scope_for(path: str) -> Optional[str]:
    if path in _OPEN_PATHS:
        return None
    if path.startswith(PUBLIC_MEDIA_PREFIX):
        # THE ONE DELIBERATE UNAUTHENTICATED READ: a signed, expiring link to
        # one stored image/audio file (tool_media.py). A browser rendering a
        # chat's markdown image sends no Foundry key; the HMAC token in the
        # path is the capability. No listing, nothing else under this prefix.
        return None
    if path.startswith("/mcp/media"):
        return "media"         # Foundry's built-in image tools
    if path.startswith("/mcp"):
        return None            # Foundry-MCP has its own token (mcp_aggregator.token)
    if path.startswith("/v1/foundry/telemetry/"):
        return "telemetry"       # workstation bridge: read-only observations in (control_api.py)
    if path.startswith("/v1/foundry/"):
        return "orchestration"   # coordinator bindings/state, training capture, pipeline status
    if path.startswith("/admin"):
        # The /ui page is a static shell (no data); its data comes from /admin/api,
        # which the UI calls with the admin key the user enters once.
        return "admin"
    if path.startswith("/v1/images") or path.startswith("/media"):
        return "media"
    if path.startswith("/v1/audio"):
        return "media"         # speech (TTS MCP servers), audio_api.py
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
    admin = headers.get(INTERNAL_ADMIN_HEADER)
    if admin and hmac.compare_digest(admin.encode(), INTERNAL_ADMIN_TOKEN.encode()):
        return ({"client_id": "foundry:operator-chat", "scopes": ["admin"],
                 "personas": [], "internal": True}, True)
    from .client_keys import matches
    keys = _keys_from(headers)
    for k in keys:
        for c in getattr(cfg, "clients", []) or []:
            if not c.enabled or not (c.key or getattr(c, "key_hash", None)):
                continue
            if matches(k, c):
                return ({"client_id": c.name, "scopes": list(c.scopes or ["inference"]),
                         "personas": list(c.personas or []),
                         "projects": list(getattr(c, "projects", None) or [])}, True)
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
        corr = correlation(headers, (client or {}).get("client_id") or "")
        mode = getattr(ca, "mode", "optional") if ca else "optional"
        protect_admin = bool(getattr(ca, "protect_admin", False)) if ca else False
        unknown = getattr(ca, "unknown_keys", "refuse") if ca else "refuse"
        problem = None
        from .client_keys import activity
        addr = (headers.get("x-forwarded-for", "").split(",")[0].strip()
                or headers.get("x-real-ip", "") or (scope.get("client") or ("", 0))[0] or "")
        agent = headers.get("user-agent", "")
        if client is not None and need:
            activity.client_seen(client["client_id"], addr, agent)
        lenient = (unknown == "anonymous" and mode == "optional"
                   and need in ("inference", "media"))
        if need and presented and client is None and getattr(cfg, "clients", None):
            for k in _keys_from(headers)[:1]:
                activity.key_rejected(k, path, addr, agent,
                                      "allowed as anonymous" if lenient else "refused")
        if need and presented and client is None and getattr(cfg, "clients", None) \
                and not lenient:
            # A key that matches nothing is refused — a typo must not silently
            # become "anonymous" (unless the operator chose unknown_keys:
            # anonymous for a rollout). Setup -> Clients & keys lists them.
            problem = _deny(401, "unknown Foundry client key — make one in Foundry's web UI "
                                 "(Setup → Clients & keys) and put it in this client's API "
                                 "key field", "invalid_api_key")
        elif need == "telemetry":
            # the Foundry Client heart-beats with an operator key (control_api
            # refuses any other source kind from such a key)
            ok = {"telemetry", "orchestration", "admin"} | (
                {"operator"} if path in CLIENT_RELAY_PATHS else set())
            if client is None or not (ok & set(client["scopes"])):
                problem = _deny(401 if client is None else 403,
                                "telemetry needs a client key with the telemetry (or "
                                "orchestration) scope", "insufficient_scope")
        elif need == "orchestration":
            ok = {"orchestration", "admin"} | (
                {"operator"} if path in CLIENT_RELAY_PATHS or path.startswith(
                    "/v1/foundry/control/actions/") else set())
            if client is None or not (ok & set(client["scopes"])):
                problem = _deny(401 if client is None else 403,
                                "work bindings need a client key with the orchestration "
                                "scope", "insufficient_scope")
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
        info = {**(client or {"client_id": "anonymous", "scopes": [], "personas": []}), **corr,
                "client_addr": addr[:80], "user_agent": agent[:200]}
        token = _client.set(info)
        from .capture import reset_request
        reset_request()
        try:
            await self.app(scope, receive, send)
        finally:
            _client.reset(token)


def correlation(headers: dict, client_id: str = "") -> dict:
    """Correlation fields for a request: explicit x-foundry-* headers, else the
    client's own session header, plus what a registered work binding adds."""
    corr = {f: headers[h][:120] for h, f in CORRELATION_HEADERS.items() if headers.get(h)}
    if "session_id" not in corr:
        for h in SESSION_FALLBACK_HEADERS:
            if headers.get(h):
                corr["session_id"] = headers[h][:120]
                break
    if corr.get("session_id"):
        from .orchestration import get_registry
        reg = get_registry()
        if reg is not None:
            for k, v in reg.correlation(corr["session_id"], client_id).items():
                corr.setdefault(k, v)
    return corr


def persona_allowed(persona_name: str) -> bool:
    """False when the client is restricted to certain personas and this isn't one."""
    allowed = (_client.get() or {}).get("personas") or []
    if not allowed:
        return True
    base = (persona_name or "").split(":")[0]
    return persona_name in allowed or base in allowed
