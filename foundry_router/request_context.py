"""Per-request client context that backend adapters may forward.

A client's own session / conversation identifiers (OpenCode's
x-opencode-session, a generic x-session-affinity, …) are the only reliable key
a stateful backend such as Meridian has for resuming the same Claude session —
which is what keeps its prompt cache warm and gives the model its own previous
turns. The facade records them here (a ContextVar, so concurrent requests never
see each other's) and the Anthropic adapter forwards the allow-listed ones.
"""

from __future__ import annotations

import contextvars
from typing import Optional

# Client headers worth forwarding to a session-aware backend, lower-case.
SESSION_HEADERS = ("x-session-affinity", "x-opencode-session", "x-session-id",
                   "x-litellm-session-id", "x-opencode-agent-mode",
                   "x-opencode-agent-name")

_client_headers: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "foundry_client_headers", default={})
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "foundry_request_id", default="")


def capture(headers) -> None:
    """Record the forwardable client headers + a request id for this request."""
    import uuid
    picked = {}
    try:
        for k in SESSION_HEADERS:
            v = headers.get(k)
            if v:
                picked[k] = str(v)[:200]
        rid = headers.get("x-request-id") or ""
    except Exception:
        rid = ""
    _client_headers.set(picked)
    _request_id.set(str(rid)[:100] or uuid.uuid4().hex)
    _agent_caller.set(agent_caller_from(headers) or "")


# -- agent loop protection --------------------------------------------------------
# An external agent (Hermes) may use Foundry as its own model provider / MCP
# server. Requests carrying the agent's configured caller_token are marked
# agent-originated so they are never routed back INTO an agent (an agent-backed
# persona or an agent tool) — no agent -> Foundry -> agent loops.
_agent_tokens: dict[str, str] = {}          # token -> agent name
_agent_caller: contextvars.ContextVar[str] = contextvars.ContextVar(
    "foundry_agent_caller", default="")


def set_agent_tokens(tokens: dict[str, str]) -> None:
    global _agent_tokens
    _agent_tokens = {k: v for k, v in (tokens or {}).items() if k}


def agent_caller_from(headers) -> Optional[str]:
    """The agent a request came from (by its caller_token), or None."""
    if not _agent_tokens or headers is None:
        return None
    try:
        cands = [headers.get("x-api-key") or "", headers.get("x-foundry-agent-token") or ""]
        auth = headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            cands.append(auth[7:].strip())
    except Exception:
        return None
    for c in cands:
        if c and c in _agent_tokens:
            return _agent_tokens[c]
    return None


def agent_caller() -> Optional[str]:
    return _agent_caller.get() or None


def set_agent_caller(name: str):
    return _agent_caller.set(name or "")


# Per-request ROUTE record, filled in by dispatch and stamped onto the response
# headers by RequestIdMiddleware when the response starts — so every client
# sees which persona was asked for and which backend/model actually served it.
_route: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "foundry_route", default=None)


def note_route(**fields) -> None:
    """Record route facts for this request (persona, served_model, backend,
    mode, routing). Later calls overwrite earlier ones (e.g. after failover,
    for responses that haven't started yet)."""
    r = _route.get()
    if r is not None:
        r.update({k: v for k, v in fields.items() if v is not None})


def route() -> dict:
    return dict(_route.get() or {})


_ROUTE_HEADERS = (("persona", b"x-foundry-persona"), ("served_model", b"x-foundry-served-model"),
                  ("backend", b"x-foundry-served-backend"), ("mode", b"x-foundry-route-mode"),
                  ("routing", b"x-foundry-routing"))


def client_headers() -> dict:
    return dict(_client_headers.get())


def request_id() -> Optional[str]:
    return _request_id.get() or None


# -- MCP call attribution --------------------------------------------------------
# Who is making an MCP tool call right now — set by each caller (brain, worker
# loop, direct mode, aggregator, research, gateway) so the single MCP funnel
# (MCPManager.call_tool) can log source / caller / client per call.
_mcp_attr: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "foundry_mcp_attr", default={})


def set_mcp_attribution(source: str, caller: str = "", client: str = "") -> contextvars.Token:
    return _mcp_attr.set({"source": source, "caller": caller, "client": client})


def reset_mcp_attribution(token) -> None:
    try:
        _mcp_attr.reset(token)
    except Exception:
        pass


def mcp_attribution() -> dict:
    return dict(_mcp_attr.get())


async def mcp_attributed(coro, source: str, caller: str = "", client: str = ""):
    """Await an MCP call with its attribution set (works whether the caller
    awaits it directly or wraps it in a task)."""
    tok = set_mcp_attribution(source, caller, client)
    try:
        return await coro
    finally:
        reset_mcp_attribution(tok)


class RequestIdMiddleware:
    """One id per request, end to end: the client's X-Request-Id when it sends
    one, else a fresh one. It's put on the request (so capture() / the logs
    use it), forwarded to Meridian and vLLM, written to request_log and
    mcp_call_log, and returned to the client as the X-Request-Id response
    header — so a client-side report can be matched to Foundry's logs and the
    backend's."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        import uuid
        headers = list(scope.get("headers") or [])
        rid = next((v.decode("latin-1") for k, v in headers if k == b"x-request-id"), "")
        rid = rid.strip()[:100]
        if not rid:
            rid = uuid.uuid4().hex
            headers.append((b"x-request-id", rid.encode("latin-1")))
            scope = dict(scope, headers=headers)
        rid_b = rid.encode("latin-1", "replace")
        holder: dict = {}
        token = _route.set(holder)

        async def send_with_id(message):
            if message.get("type") == "http.response.start":
                h = [(k, v) for k, v in (message.get("headers") or []) if k != b"x-request-id"]
                h.append((b"x-request-id", rid_b))
                for key, hname in _ROUTE_HEADERS:
                    v = holder.get(key)
                    if v:
                        h.append((hname, str(v).encode("latin-1", "replace")[:200]))
                message = dict(message, headers=h)
            await send(message)
        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _route.reset(token)
