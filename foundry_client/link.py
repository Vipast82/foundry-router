"""The router link: can this workstation reach the Foundry router (client.backend_url), which
version is it, is it still warming up. `snapshot()` is what the relay sends upstream and what
the status page shows; it never holds secrets."""
from __future__ import annotations

import threading
import time
from typing import Callable

from . import __version__
from .probe import get_json


class RouterLink:
    def __init__(self, url_fn: Callable[[], str], clock: Callable[[], float] = time.time,
                 get: Callable = get_json):
        self.url_fn, self.clock, self.get = url_fn, clock, get
        self.lock = threading.Lock()
        self.state = {"reachable": None, "version": "", "warming_up": False, "latency_ms": None,
                      "error": "", "checked_at": 0.0, "last_ok": 0.0}

    def poll(self) -> dict:
        """One check: /health (newer routers), else /api/version."""
        base = self.url_fn().rstrip("/")
        t0 = time.monotonic()
        code, body, err = self.get(base + "/health", None, 5)
        if code != 200 or not isinstance(body, dict):
            code, body, err = self.get(base + "/api/version", None, 5)
        ms = round((time.monotonic() - t0) * 1000)
        now = self.clock()
        with self.lock:
            s = self.state
            s["checked_at"] = now
            if code == 200 and isinstance(body, dict):
                s.update(reachable=True, latency_ms=ms, error="",
                         version=str(body.get("foundry_router") or body.get("version") or ""),
                         warming_up=bool(body.get("warming_up")), last_ok=now)
            else:
                s.update(reachable=False, latency_ms=None, warming_up=False,
                         error=err or f"HTTP {code}")
            return dict(s)

    def snapshot(self) -> dict:
        with self.lock:
            return {**self.state, "url": self.url_fn(), "client_version": __version__}
