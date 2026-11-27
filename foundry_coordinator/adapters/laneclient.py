"""Coding clients that live on a slot (Distributed Foundry phase b, B4).

docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md. A slot with `[slots.<id>] lanes = true` runs its own
`kilo serve` and `opencode serve`, reached through its lane tunnel (lane_tunnel.py). Each
becomes a `ClientSet` member, `<slot>/kilo` and `<slot>/opencode`; a session whose metadata
names a `lane_host` starts on that slot's member of the same kind the role would use here.

`TunnelledClient` asks the tunnel manager for the server's URL and password on every call and
builds a fresh KiloClient / OpenCodeClient whenever they change, so a tunnel that comes back on
new local ports keeps the same member (and the same stored session ids). While the tunnel is
down every call raises `LaneDown` (an AgentServerError), which the engine's existing
unreachable-server handling treats like a Kilo server that is restarting.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from .oc1 import AgentServerError

KINDS = ("kilo", "opencode")
LANES_ROOT = "/srv/foundry-lanes"


class LaneDown(AgentServerError):
    pass


class TunnelledClient:
    def __init__(self, sid: str, kind: str, endpoint: Callable[[str, str], Optional[dict]],
                 provider: str = "foundry", api: str = "auto"):
        if kind not in KINDS:
            raise ValueError(f"no lane client kind {kind!r}")
        self.sid, self.kind = sid, kind
        self._endpoint = endpoint
        self.provider = provider
        self.api = api
        self._key: tuple = ()
        self._inner = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:                 # what ClientSet / the store record: kilo | opencode
        return self.kind

    @property
    def url(self) -> str:
        ep = self._endpoint(self.sid, self.kind) or {}
        return str(ep.get("url") or "")

    def _client(self):
        ep = self._endpoint(self.sid, self.kind)
        if not ep or not ep.get("url"):
            raise LaneDown(f"{self.sid}: the lane tunnel is down ({self.kind} on the slot "
                           f"cannot be reached)")
        key = (ep["url"], ep.get("password") or "")
        with self._lock:
            if key != self._key or self._inner is None:
                if self.kind == "kilo":
                    from .kilo import KiloClient
                    self._inner = KiloClient(ep["url"], password=ep.get("password") or "",
                                             provider=self.provider)
                else:
                    from .opencode import OpenCodeClient
                    self._inner = OpenCodeClient(ep["url"], password=ep.get("password") or "",
                                                 provider=self.provider, api=self.api)
                self._key = key
            return self._inner

    def __getattr__(self, attr):
        if attr.startswith("_"):
            raise AttributeError(attr)
        return getattr(self._client(), attr)


def lane_root(slot: dict) -> str:
    """The folder a slot keeps its lane worktrees in, as the slot's servers see it."""
    return str(slot.get("lanes_root") or LANES_ROOT).rstrip("/") or LANES_ROOT


def serves(root: Optional[str], directory: str) -> bool:
    """A member with a lane root serves only folders under it; a local member serves all
    (unchanged behaviour)."""
    if not root:
        return True
    d = str(directory or "").replace("\\", "/")
    return d == root or d.startswith(root + "/")


def lane_host_meta(P) -> dict:
    """{"lane_host": <slot>} for a session of a lane whose worktree is on a slot, else {}:
    every start_session (engine, helpers, Lead) adds it, so ClientSet starts the session on
    that slot's server (memory note fix-every-path-not-the-one-you-read)."""
    host = str(getattr(getattr(P, "profile", None), "__dict__", {}).get("lane_host") or "")
    return {"lane_host": host} if host else {}


__all__ = ["TunnelledClient", "LaneDown", "lane_root", "serves", "lane_host_meta", "KINDS",
           "LANES_ROOT"]
