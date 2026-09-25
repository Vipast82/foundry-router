"""Foundry-MCP aggregator — re-expose Foundry's own connected MCP tools as a
single Streamable-HTTP MCP server.

Foundry is normally an MCP *client*: it dials out to standalone MCP servers
(acestep-music, TTS, crawl4ai, …) and calls their tools server-side. A plain
chat client like AnythingLLM can't reach those — it only speaks to an LLM
endpoint. This module turns Foundry into an MCP *server* too: one endpoint that
lists every tool Foundry already knows about (from the synced tool registry)
and, when called, dispatches through Foundry's existing MCP client.

The consuming client owns the tool loop — it decides which tool to call (using
whatever model Foundry routes its completion to) and executes it against this
endpoint. So the client's own local MCP servers and Foundry's tools coexist in
one toolbox, toggled on the client side.

Transport: Streamable HTTP, stateless, JSON responses (AnythingLLM's
`streamable` type). The base path exposes every tool; each configured profile
serves a curated subset at `{base}/p/{name}` — the "MCP personas" idea.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from ..tools.sync import is_gateway_management_tool

log = logging.getLogger(__name__)

_POLL_WINDOW = 120.0   # seconds: identical calls within this window count toward the poll guard


class MCPAggregator:
    """Builds and mounts the Streamable-HTTP MCP endpoints. Created once per
    Services; `start()` is called from the app lifespan with an AsyncExitStack
    that keeps each session manager's task group alive for the app's lifetime."""

    def __init__(self, svc):
        self.svc = svc
        self._endpoints: list[dict] = []   # {scope, path, servers} for the UI
        self._recent: dict = {}            # tool_key -> {count, first, last} for the poll guard

    # -- tool exposure ---------------------------------------------------------

    def _persona_scope(self, persona_name: str):
        """(persona, tool name set) for a persona endpoint — exactly the MCP
        tools attached to that persona (whole servers or scoped per-tool
        grants), so an external client loads that set and nothing more."""
        persona = self.svc.personas.get(persona_name) if persona_name else None
        if persona is None:
            return None, set()
        tools = self.svc.tool_registry.mcp_tools_for_persona(persona)
        return persona, {t.name for t in tools}

    def _visible_tools(self, server_filter: Optional[set], name_filter: Optional[set] = None):
        """Enabled MCP ToolDefs, minus gateway-management control tools, scoped
        to `server_filter` (None = all). The registry already namespaces names
        as `server<sep>bare`, so the client sees stable, collision-free ids."""
        from .. import request_context
        from_agent = bool(request_context.agent_caller())
        out = []
        for td in self.svc.tool_registry.enabled():
            if td.kind != "mcp" or td.disabled:
                continue
            if is_gateway_management_tool(td.name):   # Foundry's control surface, not for clients
                continue
            if server_filter is not None and td.server not in server_filter:
                continue
            if name_filter is not None and td.name not in name_filter:
                continue
            if from_agent and (td.server or "").startswith("agent-"):
                continue                                  # agent loop protection
            out.append(td)
        return out

    async def _dispatch(self, name: str, arguments: Optional[dict],
                        server_filter: Optional[set], name_filter: Optional[set] = None,
                        scope: str = "all", client: str = "") -> str:
        """Text view of _dispatch_rich (kept for callers that want a string)."""
        return (await self._dispatch_rich(name, arguments, server_filter, name_filter,
                                          scope, client)).text

    async def _dispatch_rich(self, name: str, arguments: Optional[dict],
                             server_filter: Optional[set], name_filter: Optional[set] = None,
                             scope: str = "all", client: str = "", progress_callback=None):
        """Resolve a namespaced tool id back to (server, original name) and run
        it through Foundry's MCP client. Scope-checked so a profile endpoint
        can't be used to reach a server it doesn't expose."""
        td = self.svc.tool_registry.get(name)
        if td is None or td.kind != "mcp":
            raise ValueError(f"unknown MCP tool {name!r}")
        if server_filter is not None and td.server not in server_filter:
            raise ValueError(f"tool {name!r} is not exposed on this profile")
        if name_filter is not None and name not in name_filter:
            raise ValueError(f"tool {name!r} is not attached to this persona")
        guard = self._poll_guard(name, arguments)
        if guard is not None:
            from ..tools.mcp_client import ToolResult
            return ToolResult([{"type": "text", "text": guard}])
        from .. import request_context
        from ..brain.agent import call_tool_rich
        result = await request_context.mcp_attributed(
            call_tool_rich(self.svc.mcp, td.server, td.mcp_tool or td.name,
                           arguments or {},
                           **({"progress_callback": progress_callback}
                              if progress_callback else {})),
            "aggregator", scope, client)
        self._record_call(name, arguments, result.text)
        return result

    # -- poll guard ------------------------------------------------------------

    def _poll_guard(self, name: str, arguments) -> Optional[str]:
        """If this exact (tool + args) call has already run `threshold` times in
        the last window, DON'T execute it again — return a firm 'stop polling'
        message. Breaks a client agent tight-polling a status tool while a job is
        still in progress (it can't sleep, so it spins to its own call limit)."""
        import json as _json
        threshold = getattr(self.svc.config_store.config.mcp_aggregator,
                            "poll_guard_threshold", 0) or 0
        if threshold <= 0:
            return None
        try:
            key = name + "|" + _json.dumps(arguments or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            key = name + "|" + str(arguments)
        now = time.monotonic()
        rec = self._recent.get(key)
        if not rec or now - rec["first"] > _POLL_WINDOW:
            return None
        if rec["count"] >= threshold:
            self.svc.db.log_event(
                "info", "mcp_aggregator",
                f"poll guard: {name} called {rec['count']}× in "
                f"{int(now - rec['first'])}s — returning stop-polling to the client")
            last = (rec.get("last") or "").strip()
            return (f"POLL GUARD: you have already called `{name}` {rec['count']} times "
                    f"with these arguments in the last {int(now - rec['first'])} seconds "
                    f"and the job is still in progress. STOP polling now. Tell the user "
                    f"it is still running and to ask again in a minute — do NOT call this "
                    f"tool again this turn."
                    + (f"\nLast status: {last[:300]}" if last else ""))
        return None

    def _record_call(self, name: str, arguments, result: str) -> None:
        import json as _json
        threshold = getattr(self.svc.config_store.config.mcp_aggregator,
                            "poll_guard_threshold", 0) or 0
        if threshold <= 0:
            return
        try:
            key = name + "|" + _json.dumps(arguments or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            key = name + "|" + str(arguments)
        now = time.monotonic()
        rec = self._recent.get(key)
        if not rec or now - rec["first"] > _POLL_WINDOW:
            rec = {"count": 0, "first": now, "last": ""}
        rec["count"] += 1
        rec["last"] = result
        self._recent[key] = rec
        # opportunistic prune so the map can't grow unbounded
        if len(self._recent) > 256:
            self._recent = {k: v for k, v in self._recent.items()
                            if now - v["first"] <= _POLL_WINDOW}

    @staticmethod
    def _to_mcp_content(result) -> list:
        """ToolResult -> MCP content blocks, keeping images, audio and embedded
        resources intact (a text-only relay used to drop them)."""
        import mcp.types as types
        out = []
        for b in result.blocks:
            t = b.get("type")
            try:
                if t == "image":
                    out.append(types.ImageContent(type="image", data=b["data"],
                                                  mimeType=b.get("mimeType") or "image/png"))
                    continue
                if t == "audio" and hasattr(types, "AudioContent"):
                    out.append(types.AudioContent(type="audio", data=b["data"],
                                                  mimeType=b.get("mimeType") or "audio/wav"))
                    continue
                if t == "resource" and (b.get("text") is not None or b.get("blob")):
                    res = (types.TextResourceContents(uri=b["uri"], mimeType=b.get("mimeType"),
                                                      text=b["text"])
                           if b.get("text") is not None else
                           types.BlobResourceContents(uri=b["uri"], mimeType=b.get("mimeType"),
                                                      blob=b["blob"]))
                    out.append(types.EmbeddedResource(type="resource", resource=res))
                    continue
            except Exception:                                   # noqa: BLE001
                pass
            if t == "text":
                out.append(types.TextContent(type="text", text=b.get("text") or ""))
        rendered = len(out)
        lost = result.structured is not None or rendered < len(result.blocks)
        if lost and not any(getattr(c, "type", "") == "text" for c in out) and result.text:
            # structuredContent / resource links / unrenderable blocks: keep
            # their text rendering so nothing is silently lost.
            out = [types.TextContent(type="text", text=result.text)] + out
        return out or [types.TextContent(type="text", text="")]

    def _tool_meta(self, td):
        import mcp.types as types
        ann = dict(td.annotations or {})
        if td.read_only is not None:
            ann.setdefault("readOnlyHint", td.read_only)
        if td.destructive is not None:
            ann.setdefault("destructiveHint", td.destructive)
        kw = {}
        if ann:
            try:
                kw["annotations"] = types.ToolAnnotations(**ann)
            except Exception:                                   # noqa: BLE001
                pass
        return types.Tool(name=td.name, description=td.description or "",
                          inputSchema=td.parameters or {"type": "object", "properties": {}},
                          **kw)

    def _build_server(self, scope_name: str, server_filter: Optional[set],
                      persona_mode: bool = False):
        """One MCP server per endpoint. persona_mode: a single server mounted
        at {base}/persona that resolves the persona from the request path on
        every call, so new personas / changed tool grants apply live."""
        from mcp.server.lowlevel import Server

        server = Server(f"foundry-mcp:{scope_name}")

        def _scope():
            ctx = server.request_context
            req = getattr(ctx, "request", None)
            client = ""
            from .. import request_context
            if req is not None:
                try:
                    client = (req.headers.get("user-agent") or "")[:120]
                except Exception:
                    client = ""
                # Mark agent-originated calls (Hermes using Foundry as its MCP
                # server): agent tools are hidden from / refused for them.
                request_context.set_agent_caller(
                    request_context.agent_caller_from(req.headers) or "")
            if not persona_mode:
                return scope_name, None, client
            name = ""
            if req is not None:
                path = str(req.url.path)
                marker = "/persona/"
                if marker in path:
                    name = path.split(marker, 1)[1].strip("/").split("/")[0]
            from urllib.parse import unquote
            name = unquote(name)
            persona, names = self._persona_scope(name)
            if persona is None:
                raise ValueError(f"no persona named {name!r}")
            return f"persona:{name}", names, client

        @server.list_tools()
        async def _list():
            scope, names, _client = _scope()
            return [self._tool_meta(td) for td in self._visible_tools(server_filter, names)]

        @server.call_tool()
        async def _call(name: str, arguments: Optional[dict]):
            scope, names, client = _scope()
            hb = self.svc.config_store.config.mcp_aggregator.progress_heartbeat_seconds or 0
            result = await self._dispatch_with_heartbeat(
                server, name, arguments, server_filter, hb, names, scope, client)
            return self._to_mcp_content(result)

        return server

    async def _dispatch_with_heartbeat(self, server, name, arguments,
                                       server_filter, hb: int, name_filter=None,
                                       scope: str = "all", client: str = ""):
        """Run the tool; relay the DOWNSTREAM server's own progress
        notifications (e.g. "step 12/30") to the client, and — when hb > 0 —
        emit a "still working… Ns" notification every hb seconds a tool is
        silent. Both need the client's progressToken (and SSE mode)."""
        ctx = server.request_context
        token = getattr(getattr(ctx, "meta", None), "progressToken", None)
        relayed = {"n": 0}

        async def relay(progress, total, message):
            if token is None:
                return
            relayed["n"] += 1
            try:
                await ctx.session.send_progress_notification(
                    token, float(progress), total, message=message)
            except Exception:              # notification path is best-effort
                pass

        task = asyncio.create_task(self._dispatch_rich(
            name, arguments, server_filter, name_filter, scope, client,
            progress_callback=relay if token is not None else None))
        if hb <= 0:
            return await task
        start = time.monotonic()
        ticks = 0
        try:
            while True:
                done, _pending = await asyncio.wait({task}, timeout=hb)
                if task in done:
                    break
                ticks += 1
                elapsed = int(time.monotonic() - start)
                if token is not None:
                    try:
                        await ctx.session.send_progress_notification(
                            token, float(ticks + relayed["n"]), None,
                            message=f"{name}: still working… {elapsed}s")
                    except Exception:      # notification path is best-effort
                        pass
        except asyncio.CancelledError:
            task.cancel()
            raise
        if ticks:                          # only log calls that actually ran long
            self.svc.db.log_event(
                "info", "mcp_aggregator",
                f"{name} finished after {int(time.monotonic() - start)}s "
                f"({ticks} heartbeat(s))")
        return await task                  # result, or re-raise the tool's error

    # -- transport / mounting --------------------------------------------------

    def _guarded(self, manager, cfg):
        """ASGI wrapper enforcing the shared-secret header before delegating to
        the MCP session manager. Empty token = open (trusted-LAN deployments)."""
        token = (cfg.token or "").encode()
        header = (cfg.token_header or "X-API-KEY").lower().encode()

        async def asgi(scope, receive, send):
            if scope["type"] == "http" and token:
                headers = dict(scope.get("headers") or [])
                if headers.get(header) != token:
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"content-type", b"text/plain")]})
                    await send({"type": "http.response.body",
                                "body": b"foundry-mcp: unauthorized"})
                    return
            await manager.handle_request(scope, receive, send)

        return asgi

    async def start(self, app, stack) -> None:
        """Mount the endpoints and open each session manager's lifespan on
        `stack`. No-op when disabled or when the mcp SDK server side is absent."""
        cfg = self.svc.config_store.config.mcp_aggregator
        if not cfg.enabled:
            return
        try:
            from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
            from starlette.routing import Mount
        except ImportError as e:
            log.warning("mcp aggregator unavailable (mcp server SDK missing): %s", e)
            return

        base = (cfg.base_path or "/mcp").rstrip("/") or "/mcp"
        plan = [("all", None, base)]
        for pname, servers in (cfg.profiles or {}).items():
            plan.append((pname, set(servers), f"{base}/p/{pname}"))

        # Heartbeat needs an open SSE stream AND a persistent session to route
        # server->client progress notifications, so it flips the transport off
        # single-JSON stateless mode. Off = the simpler stateless JSON mode.
        hb_on = (cfg.progress_heartbeat_seconds or 0) > 0
        json_response = not hb_on
        stateless = not hb_on

        if getattr(cfg, "persona_endpoints", True):
            plan.append(("persona", None, f"{base}/persona"))
        # Mount the SPECIFIC endpoints (profiles, personas) before the base:
        # Starlette Mounts match by path prefix, so a base "/mcp" mounted first
        # swallowed "/mcp/p/<name>" and "/mcp/persona/<name>" and served every
        # tool on them (profile scoping silently did nothing).
        plan = plan[1:] + plan[:1]
        for scope_name, server_filter, path in plan:
            server = self._build_server(scope_name, server_filter,
                                        persona_mode=(scope_name == "persona"))
            manager = StreamableHTTPSessionManager(
                app=server, json_response=json_response, stateless=stateless)
            await stack.enter_async_context(manager.run())
            app.router.routes.append(Mount(path, app=self._guarded(manager, cfg)))
            if scope_name == "persona":
                self._endpoints.append({"scope": "persona", "path": path + "/<persona>",
                                        "servers": None})
                log.info("foundry-mcp persona endpoints mounted: %s/<persona>", path)
                continue
            self._endpoints.append({
                "scope": scope_name, "path": path,
                "servers": sorted(server_filter) if server_filter is not None else None,
            })
            log.info("foundry-mcp endpoint mounted: %s (%s)", path, scope_name)
        self.svc.db.log_event(
            "info", "mcp_aggregator",
            f"Foundry-MCP aggregator up: {len(self._endpoints)} endpoint(s)")

    # -- UI helper -------------------------------------------------------------

    def _base_url(self) -> str:
        """Externally reachable base URL for client config examples: the operator
        override if set, else the configured server host:port (0.0.0.0 shown as
        HOST so the operator fills in the real address)."""
        cfg = self.svc.config_store.config.mcp_aggregator
        if cfg.advertise_url:
            return cfg.advertise_url.rstrip("/")
        srv = self.svc.config_store.config.server
        host = srv.host if srv.host not in ("0.0.0.0", "::", "") else "HOST"
        return f"http://{host}:{srv.port}"

    def _planned_endpoints(self) -> list[dict]:
        """Endpoints as they WILL mount from current config (so the UI shows the
        right examples before a restart), falling back to the live set."""
        cfg = self.svc.config_store.config.mcp_aggregator
        base = (cfg.base_path or "/mcp").rstrip("/") or "/mcp"
        plan = [{"scope": "all", "path": base, "servers": None}]
        for pname, servers in (cfg.profiles or {}).items():
            plan.append({"scope": pname, "path": f"{base}/p/{pname}",
                         "servers": sorted(servers)})
        if getattr(cfg, "persona_endpoints", True) and getattr(self.svc, "personas", None):
            from urllib.parse import quote
            for p in self.svc.personas.list(enabled_only=True):
                tools = self.svc.tool_registry.mcp_tools_for_persona(p)
                if tools:
                    plan.append({"scope": f"persona-{p['virtual_name']}",
                                 "path": f"{base}/persona/{quote(p['virtual_name'])}",
                                 "servers": sorted({t.server for t in tools}),
                                 "tools": len(tools)})
        return plan

    def describe(self) -> dict:
        """Endpoints (live + planned) + ready-to-paste AnythingLLM and Cline
        config blocks for the UI, using the resolved base URL and real token."""
        cfg = self.svc.config_store.config.mcp_aggregator
        base_url = self._base_url()
        token = cfg.token or ""
        header = cfg.token_header or "X-API-KEY"
        planned = self._planned_endpoints()

        anythingllm, cline = {}, {}
        for ep in planned:
            key = "foundry" if ep["scope"] == "all" else f"foundry-{ep['scope']}"
            # Trailing slash = canonical, redirect-free URL.
            url = f"{base_url}{ep['path'].rstrip('/')}/"
            allm = {"type": "streamable", "url": url}
            cln = {"type": "streamableHttp", "url": url,
                   "disabled": False, "autoApprove": []}
            if token:
                allm["headers"] = {header: token}
                cln["headers"] = {header: token}
            anythingllm[key] = allm
            cline[key] = cln
        return {
            "enabled": cfg.enabled,
            "base_url": base_url,
            "endpoints": self._endpoints,          # actually mounted this run
            "planned_endpoints": planned,          # from current (possibly unsaved-restart) config
            "token_set": bool(cfg.token),
            "token_header": header,
            "anythingllm_config": {"mcpServers": anythingllm} if anythingllm else None,
            "cline_config": {"mcpServers": cline} if cline else None,
        }
