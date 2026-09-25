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
from typing import Any, Optional

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


class ToolCallError(RuntimeError):
    """The tool ran and reported isError — carries the (partial) result."""
    def __init__(self, msg: str, result: "ToolResult"):
        super().__init__(msg)
        self.result = result


class ToolResult:
    """Everything an MCP tool returned, protocol-neutral.

    blocks: [{"type": "text", "text"} | {"type": "image"|"audio", "data" (b64),
              "mimeType"} | {"type": "resource", "uri", "mimeType", "text"?,
              "blob"?} | {"type": "resource_link", "uri", "name", "mimeType"?}]
    structured: the tool's structuredContent (MCP 2025-06) or None.
    text: a model-ready text rendering — text blocks verbatim, other blocks
    described in place, structuredContent as JSON when there's no text.
    """

    def __init__(self, blocks: list[dict], structured: Any = None, is_error: bool = False):
        self.blocks = blocks
        self.structured = structured
        self.is_error = is_error
        parts = []
        for b in blocks:
            t = b.get("type")
            if t == "text":
                parts.append(b.get("text") or "")
            elif t in ("image", "audio"):
                size = len(b.get("data") or "") * 3 // 4
                parts.append(f"[{t}: {b.get('mimeType') or 'unknown'}, {size // 1024} KB]")
            elif t == "resource":
                if b.get("text"):
                    parts.append(f"[resource {b.get('uri')}]\n{b['text']}")
                else:
                    parts.append(f"[resource {b.get('uri')} ({b.get('mimeType') or 'binary'})]")
            elif t == "resource_link":
                parts.append(f"[link: {b.get('name') or ''} {b.get('uri')}]".replace("  ", " "))
        if structured is not None and not any(b.get("type") == "text" for b in blocks):
            try:
                parts.append(json.dumps(structured, ensure_ascii=False, default=str))
            except (TypeError, ValueError):
                parts.append(str(structured))
        self.text = "\n".join(p for p in parts if p)

    @property
    def content_types(self) -> list[str]:
        out = list(dict.fromkeys(b.get("type") for b in self.blocks if b.get("type")))
        if self.structured is not None:
            out.append("structured")
        return out

    def images(self) -> list[str]:
        """Base64 image payloads (for vision-capable model context)."""
        return [b["data"] for b in self.blocks if b.get("type") == "image" and b.get("data")]

    @classmethod
    def from_mcp(cls, result) -> "ToolResult":
        blocks: list[dict] = []
        for c in getattr(result, "content", None) or []:
            t = getattr(c, "type", None)
            if t == "text":
                blocks.append({"type": "text", "text": getattr(c, "text", "") or ""})
            elif t in ("image", "audio"):
                blocks.append({"type": t, "data": getattr(c, "data", "") or "",
                               "mimeType": getattr(c, "mimeType", "") or ""})
            elif t == "resource":
                r = getattr(c, "resource", None)
                blocks.append({"type": "resource", "uri": str(getattr(r, "uri", "") or ""),
                               "mimeType": getattr(r, "mimeType", None),
                               "text": getattr(r, "text", None),
                               "blob": getattr(r, "blob", None)})
            elif t == "resource_link":
                blocks.append({"type": "resource_link", "uri": str(getattr(c, "uri", "") or ""),
                               "name": getattr(c, "name", "") or "",
                               "mimeType": getattr(c, "mimeType", None)})
            else:
                txt = getattr(c, "text", None)
                if txt:
                    blocks.append({"type": "text", "text": txt})
        return cls(blocks, getattr(result, "structuredContent", None),
                   bool(getattr(result, "isError", False)))


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
        # In-flight tool calls right now: id -> {server, tool, since}. Lets the
        # operator see a long media generation (acestep/stable-audio) is actively
        # running rather than hung.
        self._inflight: dict[int, dict] = {}
        self._inflight_seq = 0
        # Persistent (pooled) MCP sessions, one per server: the handshake
        # (connect + initialize) is paid once, not on every tool call.
        self._holders: dict[str, "_SessionHolder"] = {}

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

    def active_calls(self) -> list[dict]:
        """Tool calls in flight right now: [{server, tool, seconds}], longest
        first — so a multi-minute media generation is visibly *running*."""
        now = time.monotonic()
        return sorted(
            ({"server": v["server"], "tool": v["tool"],
              "seconds": round(now - v["since"], 1)} for v in self._inflight.values()),
            key=lambda x: -x["seconds"])

    IDLE_CLOSE = 600.0     # seconds before an unused pooled session is closed

    def set_servers(self, servers: list[MCPServerConfig]) -> None:
        self.servers = {s.name: s for s in servers}
        # Connection details may have changed — drop pooled sessions so the next
        # call reconnects with the new URL / headers / auth.
        holders, self._holders = self._holders, {}
        for h in holders.values():
            try:
                asyncio.get_event_loop().create_task(h.reset())
            except RuntimeError:
                pass

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
        # CRITICAL for long media jobs: both SSE and streamable-http clients
        # default sse_read_timeout to 300s, so the stream to the server is torn
        # down after 5 min of no events EVEN IF our per-call wait_for is higher —
        # the result of a 9-minute music render then never arrives ("Connection
        # closed"). Match the read timeout to this server's tool budget so the
        # stream stays open as long as the job may legitimately run. Connect
        # timeout stays short.
        read_to = float(getattr(cfg, "timeout_seconds", 300) or 300)
        kwargs["sse_read_timeout"] = read_to
        kwargs["timeout"] = min(30.0, read_to)
        async with transport_client(cfg.url, **kwargs) as streams:
            # streamable-http yields (read, write, get_session_id); sse yields
            # (read, write) — take the first two either way.
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def list_tools(self, name: str) -> list[dict]:
        """[{name, description, input_schema, read_only, destructive}] for one
        server. read_only/destructive come from the tool's MCP `annotations`
        (readOnlyHint/destructiveHint) when the server provides them — ground
        truth for the write/destructive badges, replacing the name heuristic.
        None means the server didn't annotate that tool. Raises on failure —
        the caller (Tool Sync) decides how to treat an unreachable server."""
        async with self._session(name) as session:
            result = await session.list_tools()
            out = []
            for t in result.tools:
                ann = getattr(t, "annotations", None)
                out.append({
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": getattr(t, "inputSchema", None)
                                    or {"type": "object", "properties": {}},
                    "read_only": getattr(ann, "readOnlyHint", None) if ann else None,
                    "destructive": getattr(ann, "destructiveHint", None) if ann else None,
                    "annotations": ({k: v for k, v in ann.model_dump().items() if v is not None}
                                    if ann is not None and hasattr(ann, "model_dump") else None),
                })
            return out

    async def list_all(self) -> dict[str, list[dict]]:
        """Manifest per ENABLED server; disabled servers are skipped (so Tool
        Sync drops their tools this cycle) and unreachable ones are logged and
        omitted (their tools simply don't appear this sync cycle)."""
        out: dict[str, list[dict]] = {}
        for name, cfg in list(self.servers.items()):
            if not getattr(cfg, "enabled", True):
                continue                      # operator-disabled: exclude its tools
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

    async def call_tool(self, server: str, tool: str, arguments: dict[str, Any],
                        progress_callback=None) -> str:
        """Text view of a tool call (what model prompts consume). Non-text
        content is described in place ("[image: image/png, 84 KB]") instead of
        silently dropped; call_tool_rich() returns every block intact."""
        return (await self.call_tool_rich(server, tool, arguments,
                                          progress_callback=progress_callback)).text

    async def call_tool_rich(self, server: str, tool: str, arguments: dict[str, Any],
                             progress_callback=None) -> "ToolResult":
        """Run one MCP tool call and return EVERYTHING it produced: text, images,
        audio, embedded resources / resource links and structuredContent.

        Every call, from every caller, funnels through here — pacing, 429
        backoff, per-server timeout, persistent-session reuse, progress relay
        and one mcp_call_log metrics row (source/caller from the attribution
        context the caller set)."""
        from .. import request_context
        cfg = self.servers.get(server)
        if cfg is not None and not getattr(cfg, "enabled", True):
            raise MCPUnavailable(f"MCP server {server!r} is disabled")
        timeout = getattr(cfg, "timeout_seconds", 300) if cfg else 300
        retries = max(1, getattr(cfg, "rate_limit_retries", 3) if cfg else 3)
        backoff = getattr(cfg, "rate_limit_backoff_seconds", 30.0) if cfg else 30.0

        # Operator config wins over the model (safety gate for sandboxes).
        effective_args = self._apply_call_defaults(server, arguments or {})

        tid = self._inflight_seq
        self._inflight_seq += 1
        self._inflight[tid] = {"server": server, "tool": tool, "since": time.monotonic()}
        m = {"connect_ms": 0, "pace_ms": 0, "attempts": 0, "session": "per-call"}
        t_start = time.monotonic()

        async def _invoke(session) -> "ToolResult":
            kw = {"progress_callback": progress_callback} if progress_callback else {}
            result = await session.call_tool(tool, effective_args, **kw)
            tr = ToolResult.from_mcp(result)
            if tr.is_error:
                raise ToolCallError(f"MCP tool {server}/{tool} returned error: {tr.text[:500]}", tr)
            return tr

        async def _call() -> "ToolResult":
            # Pooled session first (no handshake); a transport failure on a
            # pooled session retries ONCE on a fresh per-call session, so a
            # server restart never costs the operator a failed call.
            holder = self._holder(server) if cfg is not None and getattr(
                cfg, "persistent_session", True) else None
            if holder is not None:
                t0 = time.monotonic()
                session, fresh = await holder.ensure()
                m["connect_ms"] = int((time.monotonic() - t0) * 1000) if fresh else 0
                m["session"] = "new" if fresh else "reused"
                try:
                    return await _invoke(session)
                except ToolCallError:
                    raise
                except Exception as e:                            # noqa: BLE001
                    # Only a REUSED session may have gone stale (server
                    # restarted, idle stream closed) — retry that once on a
                    # fresh connection. A just-opened session failing is a
                    # real error and is reported as such.
                    await holder.reset()
                    if fresh or _is_rate_limited(e):
                        raise
                    log.info("MCP %s: pooled session failed (%s) — retrying per-call",
                             server, describe_exception(e))
            t0 = time.monotonic()
            async with self._session(server) as session:
                m["connect_ms"] = int((time.monotonic() - t0) * 1000)
                m["session"] = "per-call"
                return await _invoke(session)

        # 429-backoff around every attempt (SearXNG's external engines rate-limit
        # bursts). A 429 means "slower", so wait an escalating amount before
        # retrying rather than immediately re-429ing.
        seen_429 = timed_out = False
        result: Optional[ToolResult] = None
        err = ""
        try:
            for attempt in range(1, retries + 1):
                m["attempts"] = attempt
                if cfg:
                    tp = time.monotonic()
                    await self._pace(server, cfg)
                    m["pace_ms"] += int((time.monotonic() - tp) * 1000)
                try:
                    # Per-server budget: media generation (ComfyUI/TTS/music) can
                    # run many minutes; a search tool should fail fast.
                    result = await asyncio.wait_for(_call(), timeout=timeout)
                    self._record_usage(server, tool, ok=True, rate_limited=seen_429)
                    return result
                except asyncio.TimeoutError:
                    timed_out = True
                    err = f"timed out after {timeout}s"
                    self._record_usage(server, tool, ok=False, rate_limited=seen_429, error=err)
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
                    err = describe_exception(e)
                    if isinstance(e, ToolCallError):
                        result = e.result
                    self._record_usage(server, tool, ok=False, rate_limited=seen_429,
                                       error=err)
                    raise
        finally:
            self._inflight.pop(tid, None)
            attr = request_context.mcp_attribution()
            self._log_call(
                server=server, tool=tool, ok=(result is not None and not err),
                duration_ms=int((time.monotonic() - t_start) * 1000),
                rate_limited=seen_429, timed_out=timed_out, error=err,
                args=effective_args, result=result, attr=attr, **m)

    def _log_call(self, *, server, tool, ok, duration_ms, rate_limited, timed_out,
                  error, args, result, attr, connect_ms, pace_ms, attempts, session) -> None:
        """One mcp_call_log row. Best-effort: metrics never break a call."""
        from .. import request_context
        try:
            args_chars = len(json.dumps(args, ensure_ascii=False, default=str))
        except Exception:
            args_chars = 0
        rc = len(result.text) if result is not None else 0
        try:
            self.db.execute(
                "INSERT INTO mcp_call_log (ts, source, caller, client, server, tool, ok, "
                "duration_ms, connect_ms, pace_ms, attempts, rate_limited, timed_out, "
                "session, error, args_chars, result_chars, result_tokens, content_types, "
                "request_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (utcnow(), attr.get("source") or "other", (attr.get("caller") or "")[:120],
                 (attr.get("client") or "")[:120], server, tool, 1 if ok else 0,
                 duration_ms, connect_ms, pace_ms, attempts, 1 if rate_limited else 0,
                 1 if timed_out else 0, session, (error or "")[:500], args_chars, rc,
                 (rc + 3) // 4, ",".join(result.content_types) if result is not None else "",
                 request_context.request_id() or ""))
        except Exception:
            log.debug("mcp_call_log write failed", exc_info=True)

    # -- persistent sessions -----------------------------------------------------------

    def _holder(self, server: str) -> "_SessionHolder":
        h = self._holders.get(server)
        if h is None:
            h = self._holders[server] = _SessionHolder(self, server)
        self._reap_idle()
        return h

    def _reap_idle(self) -> None:
        """Close pooled sessions idle for over IDLE_CLOSE seconds (a homelab MCP
        server shouldn't hold a connection for a tool nobody is using)."""
        now = time.monotonic()
        for name, h in list(self._holders.items()):
            if h.session is not None and now - h.last_used > self.IDLE_CLOSE:
                asyncio.get_event_loop().create_task(h.reset())

    async def close_sessions(self) -> None:
        for h in list(self._holders.values()):
            await h.reset()
        self._holders.clear()

    def session_status(self) -> dict:
        now = time.monotonic()
        return {name: {"open": h.session is not None,
                       "idle_s": round(now - h.last_used, 1) if h.last_used else None,
                       "connects": h.connects}
                for name, h in self._holders.items()}


class _SessionHolder:
    """Keeps one MCP ClientSession open in a background task (anyio cancel
    scopes must be entered and exited in the same task, so the session lives in
    its own task and callers borrow it). ClientSession multiplexes requests, so
    concurrent calls share it safely. A broken or closed session is detected on
    the next ensure() and transparently reopened."""

    def __init__(self, mgr: "MCPManager", name: str):
        self.mgr, self.name = mgr, name
        self.session = None
        self.task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        self._lock = asyncio.Lock()
        self.last_used = 0.0
        self.connects = 0

    async def ensure(self):
        """(session, fresh) — fresh=True when a new session had to be opened."""
        self.last_used = time.monotonic()
        if self.session is not None and self.task is not None and not self.task.done():
            return self.session, False
        async with self._lock:
            if self.session is not None and self.task is not None and not self.task.done():
                return self.session, False
            ready: asyncio.Future = asyncio.get_running_loop().create_future()
            self._stop = asyncio.Event()
            self.task = asyncio.create_task(self._run(ready, self._stop))
            session = await asyncio.wait_for(ready, timeout=30)
            self.connects += 1
            return session, True

    async def _run(self, ready: asyncio.Future, stop: asyncio.Event) -> None:
        try:
            async with self.mgr._session(self.name) as session:
                self.session = session
                if not ready.done():
                    ready.set_result(session)
                await stop.wait()
        except BaseException as e:                     # noqa: BLE001
            if not ready.done():
                ready.set_exception(e if isinstance(e, Exception) else RuntimeError(str(e)))
        finally:
            self.session = None

    async def reset(self) -> None:
        self.session = None
        if self._stop is not None:
            self._stop.set()
        t, self.task = self.task, None
        if t is not None:
            try:
                await asyncio.wait_for(t, timeout=5)
            except BaseException:                      # noqa: BLE001
                t.cancel()
