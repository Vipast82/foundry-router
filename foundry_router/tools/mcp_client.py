"""MCP server connections.

Listing a server's tool manifest and calling a tool are both standard protocol
capabilities (design doc §4.2 point 3) — this is a thin client, not custom
work. Sessions are opened per operation rather than held: MCP servers on a
homelab restart freely, and a fresh session per call is self-healing at the
cost of a handshake we can easily afford off the hot path (research agent) and
occasionally on it (persona MCP tools).

The `mcp` package is imported lazily so an import-path change in a future SDK
version degrades to "MCP features unavailable" (logged) instead of taking the
whole service down with it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from ..config import MCPServerConfig
from ..db import Database, utcnow
from ..errors import describe_exception

log = logging.getLogger(__name__)

# DB (kv) key prefix for a per-server auth secret set in the UI instead of
# config.yaml. Value is JSON {"header": <name>, "token": <secret>}. Kept out of
# config so operators can add a token without editing the file, and it survives
# config saves untouched.
_SECRET_KEY = "mcp_secret:"


class MCPUnavailable(Exception):
    pass


_SSE_FILTER_MARK = "_foundry_sse_teardown_filter"


class _SSETeardownNoiseFilter(logging.Filter):
    """Suppress the benign SSE-teardown race the streamable-http MCP client
    logs at ERROR with a full traceback.

    Our design opens a fresh MCP session per operation (see the module
    docstring) and closes it on completion — the DELETE /mcp. On close, the
    background GET-SSE reader task can lose its race with teardown and try to
    push one final message into the already-closed read stream, raising
    anyio.BrokenResourceError inside the SDK's _handle_sse_event. Its bare
    `except Exception` then logs 'Error parsing SSE message' with a traceback
    even though every HTTP request succeeded (200/202) and the tool result was
    returned. It is pure teardown noise, and at any real call volume it floods
    the Dev Log with identical, non-actionable tracebacks.

    We drop ONLY that record: a BrokenResourceError/ClosedResourceError logged
    by the streamable-http client. A genuine SSE parse failure is a different
    exception type (JSON/validation) and still surfaces; a real mid-operation
    stream break also fails the operation itself, which call_tool reports
    separately via _record_usage and a re-raise."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not (exc is not None and exc.__class__.__name__ in
                    ("BrokenResourceError", "ClosedResourceError"))


def _install_sse_noise_filter() -> None:
    """Attach the teardown-noise filter to the SDK logger once. Idempotent:
    a filter on the named logger stops the record before it propagates to any
    handler (stderr AND the Dev-Log ring buffer), so one install covers both."""
    sdk_logger = logging.getLogger("mcp.client.streamable_http")
    if any(getattr(f, _SSE_FILTER_MARK, False) for f in sdk_logger.filters):
        return
    noise_filter = _SSETeardownNoiseFilter()
    setattr(noise_filter, _SSE_FILTER_MARK, True)
    sdk_logger.addFilter(noise_filter)


def _is_rate_limited(exc: BaseException) -> bool:
    """A 429 anywhere in the exception chain (the MCP client wraps httpx errors,
    sometimes inside a TaskGroup) — the described text is the reliable signal."""
    text = describe_exception(exc).lower()
    return "429" in text or "too many requests" in text


class MCPManager:
    def __init__(self, servers: list[MCPServerConfig], db: Database):
        _install_sse_noise_filter()  # quiet the SDK's benign teardown traceback
        self.servers = {s.name: s for s in servers}
        self.db = db
        self._last_call: dict[str, float] = {}   # server name -> monotonic ts, for pacing
        # Live, process-lifetime usage per server, incremented for EVERY caller
        # (background research sweep, worker tool loop, brain). The Usage-tab MCP
        # card only sees per-request tool_calls, so a background research sweep's
        # searxng/crawl4ai use was invisible there; this makes it visible in the
        # MCP tab regardless of who called. Resets on restart (in-memory).
        self._usage: dict[str, dict] = {}

    def _record_usage(self, server: str, tool: str, ok: bool,
                      rate_limited: bool = False, error: str = "") -> None:
        u = self._usage.setdefault(
            server, {"calls": 0, "ok": 0, "fail": 0, "rate_limited": 0,
                     "last_ts": "", "last_error": "", "tools": {}})
        u["calls"] += 1
        u["tools"][tool] = u["tools"].get(tool, 0) + 1
        u["last_ts"] = utcnow()
        if rate_limited:
            u["rate_limited"] += 1
        if ok:
            u["ok"] += 1
        else:
            u["fail"] += 1
            if error:
                u["last_error"] = error[:300]

    def usage(self) -> dict[str, dict]:
        """Snapshot of live per-server tool usage (all callers)."""
        return {k: {**v, "tools": dict(v["tools"])} for k, v in self._usage.items()}

    def set_servers(self, servers: list[MCPServerConfig]) -> None:
        self.servers = {s.name: s for s in servers}

    def executes_code(self, server: str) -> bool:
        """Whether this server is operator-declared as executing code — drives
        the full-code audit trail and the persona/UI danger flag. Unknown
        server => False (a call to a vanished server is handled elsewhere)."""
        cfg = self.servers.get(server)
        return bool(cfg and getattr(cfg, "executes_code", False))

    def _apply_call_defaults(self, server: str, arguments: dict) -> dict:
        """Force-merge a server's call_defaults OVER the model-provided
        arguments — operator config is authoritative, so a model cannot flip a
        safety setting (e.g. network) the operator locked. On an executes_code
        server, any key the config actually overrode is logged as a security
        event: a model trying to widen its own sandbox is exactly what the
        audit trail exists to surface."""
        cfg = self.servers.get(server)
        defaults = dict(getattr(cfg, "call_defaults", None) or {})
        if not defaults:
            return arguments
        overridden = {k: (arguments.get(k), v) for k, v in defaults.items()
                      if k in arguments and arguments[k] != v}
        if overridden and self.executes_code(server):
            self.db.log_event(
                "warning", "mcp",
                f"sandbox policy enforced on {server}: config overrode "
                f"model-requested argument(s) "
                f"{', '.join(sorted(overridden))}",
                json.dumps({k: {"requested": req, "forced": forced}
                            for k, (req, forced) in overridden.items()})[:1000])
        return {**arguments, **defaults}

    # -- per-server auth secret (DB-backed, UI-set) --------------------------------

    def set_secret(self, server: str, token: str, header: str = "Authorization") -> None:
        """Store (or clear, if token is empty) a server's auth token in the DB.
        Applied to that server's connection headers at session time."""
        if not token:
            self.db.kv_del(_SECRET_KEY + server)
            return
        self.db.kv_set(_SECRET_KEY + server,
                       json.dumps({"header": (header or "Authorization").strip(),
                                   "token": token}))

    def delete_secret(self, server: str) -> None:
        self.db.kv_del(_SECRET_KEY + server)

    def secret_meta(self, server: str) -> dict:
        """Presence + header name only — the token value is never returned to
        the UI (write-only from the operator's side)."""
        raw = self.db.kv_get(_SECRET_KEY + server)
        if not raw:
            return {"has_token": False, "token_header": "Authorization"}
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            return {"has_token": False, "token_header": "Authorization"}
        return {"has_token": bool(d.get("token")),
                "token_header": d.get("header") or "Authorization"}

    def _secret_headers(self, server: str) -> dict:
        raw = self.db.kv_get(_SECRET_KEY + server)
        if not raw:
            return {}
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        tok, hdr = d.get("token"), (d.get("header") or "Authorization").strip()
        if not tok:
            return {}
        # Authorization defaults to a Bearer token unless a scheme is already
        # present; any other header (x-api-key, etc.) gets the raw value.
        if hdr.lower() == "authorization" and not tok.lower().startswith(("bearer ", "basic ")):
            tok = "Bearer " + tok
        return {hdr: tok}

    @asynccontextmanager
    async def _session(self, name: str):
        cfg = self.servers.get(name)
        if cfg is None:
            raise MCPUnavailable(f"no MCP server named {name!r} configured")
        try:
            from mcp import ClientSession
            if cfg.transport == "sse":
                from mcp.client.sse import sse_client as transport_client
            else:
                from mcp.client.streamable_http import streamablehttp_client as transport_client
        except ImportError as e:
            raise MCPUnavailable(f"mcp package unavailable: {e}") from e

        kwargs: dict = {}
        # config.yaml headers first, then the DB-stored token (UI-set) — the
        # secret overrides/adds so an operator can attach auth without editing
        # the file.
        headers = {**(cfg.headers or {}), **self._secret_headers(name)}
        if headers:
            kwargs["headers"] = headers
        async with transport_client(cfg.url, **kwargs) as streams:
            # streamable-http yields (read, write, get_session_id); sse yields
            # (read, write) — take the first two either way.
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def list_tools(self, name: str) -> list[dict]:
        """[{name, description, input_schema}] for one server. Raises on failure
        — the caller (Tool Sync) decides how to treat an unreachable server."""
        async with self._session(name) as session:
            result = await session.list_tools()
            out = []
            for t in result.tools:
                out.append({
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": getattr(t, "inputSchema", None)
                                    or {"type": "object", "properties": {}},
                })
            return out

    async def list_all(self) -> dict[str, list[dict]]:
        """Manifest per configured server; unreachable servers are logged and
        omitted (their tools simply don't appear this sync cycle)."""
        out: dict[str, list[dict]] = {}
        for name in list(self.servers):
            try:
                out[name] = await self.list_tools(name)
            except Exception as e:
                self.db.log_event("warning", "tool_sync",
                                  f"MCP server {name} unreachable during sync", str(e))
        return out

    async def _pace(self, server: str, cfg: MCPServerConfig) -> None:
        """Per-server minimum gap between calls — every caller (research sweep,
        worker tool loop, brain) funnels through here, so a shared rate-limited
        server (SearXNG) is spaced no matter who's calling."""
        gap = getattr(cfg, "pace_seconds", 0.0) or 0.0
        if gap > 0:
            wait = self._last_call.get(server, 0.0) + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_call[server] = time.monotonic()

    async def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> str:
        cfg = self.servers.get(server)
        timeout = getattr(cfg, "timeout_seconds", 300) if cfg else 300
        retries = max(1, getattr(cfg, "rate_limit_retries", 3) if cfg else 3)
        backoff = getattr(cfg, "rate_limit_backoff_seconds", 30.0) if cfg else 30.0

        # Operator config wins over the model (safety gate for sandboxes).
        effective_args = self._apply_call_defaults(server, arguments or {})

        async def _call() -> str:
            async with self._session(server) as session:
                result = await session.call_tool(tool, effective_args)
                texts = []
                for block in getattr(result, "content", []) or []:
                    text = getattr(block, "text", None)
                    if text:
                        texts.append(text)
                joined = "\n".join(texts) if texts else str(result)
                if getattr(result, "isError", False):
                    raise RuntimeError(
                        f"MCP tool {server}/{tool} returned error: {joined[:500]}")
                return joined

        # 429-backoff around every attempt (SearXNG's external engines rate-limit
        # bursts; this used to live only in the research agent, so worker/brain
        # tool calls hammered on unrecovered). A 429 means "slower", so wait an
        # escalating amount before retrying rather than immediately re-429ing.
        seen_429 = False
        for attempt in range(1, retries + 1):
            await self._pace(server, cfg) if cfg else None
            try:
                # Per-server budget: media generation (ComfyUI/TTS/music) can run
                # many minutes; a search tool should fail fast. Configurable per
                # connection instead of one global assumption.
                out = await asyncio.wait_for(_call(), timeout=timeout)
                self._record_usage(server, tool, ok=True, rate_limited=seen_429)
                return out
            except asyncio.TimeoutError:
                self._record_usage(server, tool, ok=False, rate_limited=seen_429,
                                   error=f"timed out after {timeout}s")
                raise RuntimeError(
                    f"MCP tool {server}/{tool} timed out after {timeout}s "
                    f"(raise timeout_seconds on this server's connection if its "
                    f"jobs legitimately run longer)") from None
            except Exception as e:  # noqa: BLE001
                if _is_rate_limited(e):
                    seen_429 = True
                if _is_rate_limited(e) and attempt < retries:
                    wait = backoff * attempt
                    self.db.log_event(
                        "warning", "mcp",
                        f"{server}/{tool} rate-limited (429) — backing off "
                        f"{wait:.0f}s before retry {attempt + 1}/{retries}",
                        describe_exception(e))
                    await asyncio.sleep(wait)
                    continue
                self._record_usage(server, tool, ok=False, rate_limited=seen_429,
                                   error=describe_exception(e))
                raise
