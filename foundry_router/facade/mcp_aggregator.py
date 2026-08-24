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

import logging
from typing import Optional

from ..tools.sync import is_gateway_management_tool

log = logging.getLogger(__name__)


class MCPAggregator:
    """Builds and mounts the Streamable-HTTP MCP endpoints. Created once per
    Services; `start()` is called from the app lifespan with an AsyncExitStack
    that keeps each session manager's task group alive for the app's lifetime."""

    def __init__(self, svc):
        self.svc = svc
        self._endpoints: list[dict] = []   # {scope, path, servers} for the UI

    # -- tool exposure ---------------------------------------------------------

    def _visible_tools(self, server_filter: Optional[set]):
        """Enabled MCP ToolDefs, minus gateway-management control tools, scoped
        to `server_filter` (None = all). The registry already namespaces names
        as `server<sep>bare`, so the client sees stable, collision-free ids."""
        out = []
        for td in self.svc.tool_registry.enabled():
            if td.kind != "mcp" or td.disabled:
                continue
            if is_gateway_management_tool(td.name):   # Foundry's control surface, not for clients
                continue
            if server_filter is not None and td.server not in server_filter:
                continue
            out.append(td)
        return out

    async def _dispatch(self, name: str, arguments: Optional[dict],
                        server_filter: Optional[set]) -> str:
        """Resolve a namespaced tool id back to (server, original name) and run
        it through Foundry's MCP client. Scope-checked so a profile endpoint
        can't be used to reach a server it doesn't expose."""
        td = self.svc.tool_registry.get(name)
        if td is None or td.kind != "mcp":
            raise ValueError(f"unknown MCP tool {name!r}")
        if server_filter is not None and td.server not in server_filter:
            raise ValueError(f"tool {name!r} is not exposed on this profile")
        return await self.svc.mcp.call_tool(
            td.server, td.mcp_tool or td.name, arguments or {})

    def _build_server(self, scope_name: str, server_filter: Optional[set]):
        from mcp.server.lowlevel import Server
        import mcp.types as types

        server = Server(f"foundry-mcp:{scope_name}")

        @server.list_tools()
        async def _list():
            return [
                types.Tool(
                    name=td.name,
                    description=td.description or "",
                    inputSchema=td.parameters or {"type": "object", "properties": {}},
                )
                for td in self._visible_tools(server_filter)
            ]

        @server.call_tool()
        async def _call(name: str, arguments: Optional[dict]):
            result = await self._dispatch(name, arguments, server_filter)
            return [types.TextContent(type="text", text=result)]

        return server

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

        for scope_name, server_filter, path in plan:
            server = self._build_server(scope_name, server_filter)
            manager = StreamableHTTPSessionManager(
                app=server, json_response=True, stateless=True)
            await stack.enter_async_context(manager.run())
            app.router.routes.append(Mount(path, app=self._guarded(manager, cfg)))
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
