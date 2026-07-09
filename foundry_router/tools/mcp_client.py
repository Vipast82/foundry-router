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

import logging
from contextlib import asynccontextmanager
from typing import Any

from ..config import MCPServerConfig
from ..db import Database

log = logging.getLogger(__name__)


class MCPUnavailable(Exception):
    pass


class MCPManager:
    def __init__(self, servers: list[MCPServerConfig], db: Database):
        self.servers = {s.name: s for s in servers}
        self.db = db

    def set_servers(self, servers: list[MCPServerConfig]) -> None:
        self.servers = {s.name: s for s in servers}

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
        if cfg.headers:
            kwargs["headers"] = cfg.headers
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

    async def call_tool(self, server: str, tool: str, arguments: dict[str, Any]) -> str:
        async with self._session(server) as session:
            result = await session.call_tool(tool, arguments)
            texts = []
            for block in getattr(result, "content", []) or []:
                text = getattr(block, "text", None)
                if text:
                    texts.append(text)
            joined = "\n".join(texts) if texts else str(result)
            if getattr(result, "isError", False):
                raise RuntimeError(f"MCP tool {server}/{tool} returned error: {joined[:500]}")
            return joined
