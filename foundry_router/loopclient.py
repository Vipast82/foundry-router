"""One HTTP client per event loop (v0.181.219).

The router makes one httpx.AsyncClient at startup, on the server's loop, and
hands it to the pool, the agents, the usage probes and the brain. A benchmark,
a model profile and a judge calibration each run in their own thread under
their own asyncio.run() loop - and called the pool through that same client.
httpx's connection pool is not shared across loops: a request that had to wait
for a connection waited on an asyncio.Event made on the other loop, and raised
"<asyncio.locks.Event ...> is bound to a different event loop", or - when the
other loop was meant to wake it - waited out the whole 30-minute read timeout.
The brain lane's profile lost three cases that way (one after 1,800 s), scored
0 as "the call failed": a fact about the router, not the model.

LoopLocalClient stands in for the client everywhere it was passed. Each loop
that uses it gets its own AsyncClient, made with the same settings; the server's
loop keeps the first. A loop's client is closed with aclose() on that loop, and
the client of a loop that has ended is dropped the next time any loop asks.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Callable, Optional

import httpx


def _running_loop() -> Optional[asyncio.AbstractEventLoop]:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class LoopLocalClient:
    """Duck-types httpx.AsyncClient; every attribute is the current loop's client."""

    def __init__(self, factory: Callable[[], httpx.AsyncClient]):
        self._factory = factory
        self._clients: dict = {}                    # loop OBJECT (or None) -> client
        self._guard = threading.Lock()

    def for_loop(self, loop=None) -> httpx.AsyncClient:
        """The client for `loop` (default: the running one), made on first use.

        Keyed on the loop object, not id(loop): two asyncio.run() loops one after
        the other can land on the same address (the v0.181.196 MCP fault)."""
        loop = loop if loop is not None else _running_loop()
        with self._guard:
            # a loop that has ended can never use its client again
            for old in [k for k in self._clients if k is not None and k.is_closed()]:
                del self._clients[old]
            c = self._clients.get(loop)
            if c is None:
                c = self._clients[loop] = self._factory()
            return c

    def loops(self) -> int:
        """How many loops hold a client now (for tests and /health)."""
        with self._guard:
            return len(self._clients)

    async def aclose(self) -> None:
        """Close the running loop's client (the server's at shutdown, a
        benchmark thread's when its run ends)."""
        loop = _running_loop()
        with self._guard:
            c = self._clients.pop(loop, None)
        if c is not None:
            await c.aclose()

    @property
    def is_closed(self) -> bool:
        with self._guard:
            c = self._clients.get(_running_loop())
        return c.is_closed if c is not None else False

    def __getattr__(self, name):
        # only reached for what this class does not define: get, post, stream,
        # request, send, build_request, timeout, headers ...
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.for_loop(), name)


async def close_loop_client(http) -> None:
    """At the end of a run on its own loop: close that loop's client. A plain
    AsyncClient (tests, older callers) belongs to the server and is left open."""
    if isinstance(http, LoopLocalClient):
        try:
            await http.aclose()
        except Exception:                                         # noqa: BLE001
            pass


def run_on_own_loop(svc, coro):
    """asyncio.run(coro) for a background thread, closing that loop's HTTP
    client when the run ends - every thread that runs the pool on its own loop
    goes through here (bench, model profile, judge calibration)."""
    async def _go():
        try:
            return await coro
        finally:
            await close_loop_client(getattr(svc, "http", None))
    return asyncio.run(_go())
