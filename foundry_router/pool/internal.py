"""Internal Backend Pool: health checks, priority-ordered failover, discovery.

Priority semantics (§4.3): failover ordering among backends capable of serving
the *same* logical model — not a ranking across categorically different ones.
The two Ollama entries compete for the same local models; Meridian's priority
is mostly inert until a second Claude-capable backend exists (two-subscription
household, or OpenRouter as a window-exhausted fallback).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Optional

import httpx

from ..config import BackendConfig, BackendPoolConfig
from ..db import Database
from ..errors import describe_exception
from .base import AllBackendsFailed, BackendPool
from .protocols import BaseProtocol, ChatResult, ProtocolError, make_protocol

log = logging.getLogger(__name__)


@dataclass
class BackendState:
    config: BackendConfig
    protocol: BaseProtocol
    healthy: bool = False
    ever_checked: bool = False
    consecutive_failures: int = 0
    cooldown_until: float = 0.0        # time.monotonic() deadline
    models: list[str] = field(default_factory=list)
    last_error: str = ""
    last_ok: float = 0.0


class InternalPool(BackendPool):
    def __init__(self, backends: list[BackendConfig], pool_cfg: BackendPoolConfig,
                 client: httpx.AsyncClient, db: Database):
        self.cfg = pool_cfg
        self.db = db
        self.client = client
        self.backends: dict[str, BackendState] = {}
        for b in backends:
            self.backends[b.name] = BackendState(
                config=b, protocol=make_protocol(b.type, b.url, b.api_key, client))
        self._listeners: list[Callable[[], None]] = []
        self._health_task: Optional[asyncio.Task] = None
        self._loaded: set[str] = set()   # VRAM-resident models, cached (see loaded_models)
        self._loaded_ts: float = 0.0
        self._LOADED_TTL = 8.0

    # -- lifecycle ----------------------------------------------------------------

    async def start(self) -> None:
        await self.check_all()  # synchronous first pass so startup has a model map
        self._health_task = asyncio.create_task(self._health_loop())

    async def stop(self) -> None:
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.health_check_interval_seconds)
            try:
                await self.check_all()
            except Exception:
                log.exception("health check sweep failed")

    async def check_all(self) -> None:
        results = await asyncio.gather(
            *(self._check_backend(s) for s in self.backends.values()),
            return_exceptions=True)
        if any(r is True for r in results):
            self._notify()

    async def _check_backend(self, s: BackendState) -> bool:
        """Health check = model discovery (§4.3: discovery is the default for
        every backend type). Returns True if observable state changed."""
        changed = False
        try:
            models = await s.protocol.list_models()
        except Exception as e:
            # No discoverable list. For a backend with a configured fallback
            # `models:` list, a failed *list* call is not by itself proof the
            # backend is down — but we have no cheaper liveness probe that
            # works across all three protocols, so treat it as a failed check
            # and rely on failure_threshold to absorb blips.
            changed = self._record_failure(s, f"discovery failed: {describe_exception(e)}")
            if s.config.models and s.healthy:
                s.models = list(s.config.models)
            return changed

        if not models and s.config.models:
            models = list(s.config.models)
        if not s.healthy or not s.ever_checked:
            changed = True
            if s.ever_checked:
                self.db.log_event("info", "backend_pool",
                                  f"backend {s.config.name} is back online")
        if set(models) != set(s.models):
            changed = True
        s.models = models
        s.healthy = True
        s.ever_checked = True
        s.consecutive_failures = 0
        s.last_error = ""
        s.last_ok = time.monotonic()
        return changed

    def _record_failure(self, s: BackendState, error: str) -> bool:
        """Shared by health checks and live-call failures. State only flips to
        unhealthy after failure_threshold consecutive failures — this same
        threshold is Tool Sync's removal grace period (§4.2)."""
        s.consecutive_failures += 1
        s.last_error = error[:300]
        s.ever_checked = True
        if s.healthy and s.consecutive_failures >= self.cfg.failure_threshold:
            s.healthy = False
            s.cooldown_until = time.monotonic() + self.cfg.cooldown_seconds
            self.db.log_event("warning", "backend_pool",
                              f"backend {s.config.name} marked unhealthy", error)
            return True
        return False

    # -- listeners -------------------------------------------------------------------

    def add_state_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def _notify(self) -> None:
        for cb in self._listeners:
            try:
                cb()
            except Exception:
                log.exception("pool state listener failed")

    # -- resolution --------------------------------------------------------------------

    def _candidates(self, model: str) -> list[BackendState]:
        """Backends serving `model`, best-first: healthy by priority, then
        unhealthy-but-past-cooldown as a last resort (a live request is as good
        a retry probe as any)."""
        now = time.monotonic()
        healthy, retryable = [], []
        for s in self.backends.values():
            if model not in s.models and model not in s.config.models:
                continue
            if s.healthy:
                healthy.append(s)
            elif now >= s.cooldown_until:
                retryable.append(s)
        healthy.sort(key=lambda s: -s.config.priority)
        retryable.sort(key=lambda s: -s.config.priority)
        return healthy + retryable

    def available_models(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for s in sorted(self.backends.values(), key=lambda s: -s.config.priority):
            if not s.healthy:
                continue
            for m in s.models:
                out.setdefault(m, []).append(s.config.name)
        return out

    def backend_info(self, model: str) -> Optional[dict]:
        cands = self._candidates(model)
        if not cands:
            return None
        c = cands[0].config
        # api_key rides along for the guardrails' authenticated quota check —
        # backend_info is internal plumbing, never serialized to the UI.
        return {"name": c.name, "type": c.type, "url": c.url, "api_key": c.api_key}

    async def loaded_models(self) -> set[str]:
        """Union of models resident in VRAM across healthy ollama backends,
        cached briefly (load state changes on the order of minutes, and this is
        polled per request during candidate shaping)."""
        now = time.monotonic()
        if now - self._loaded_ts < self._LOADED_TTL:
            return self._loaded
        loaded: set[str] = set()
        for s in self.backends.values():
            if s.config.type == "ollama" and s.healthy:
                try:
                    loaded |= set(await s.protocol.loaded_models())
                except Exception:
                    pass          # /api/ps missing/unreachable — just skip it
        self._loaded, self._loaded_ts = loaded, now
        return loaded

    def backend_status(self) -> list[dict]:
        return [{
            "name": s.config.name, "type": s.config.type, "url": s.config.url,
            "priority": s.config.priority, "healthy": s.healthy,
            "consecutive_failures": s.consecutive_failures,
            "models": s.models, "last_error": s.last_error,
        } for s in self.backends.values()]

    # -- calls ------------------------------------------------------------------------

    async def chat(self, model: str, messages: list[dict], tools: Optional[list] = None,
                   options: Optional[dict] = None, max_tokens: int = 4096
                   ) -> tuple[ChatResult, str]:
        candidates = self._candidates(model)
        if not candidates:
            raise AllBackendsFailed(f"no backend serves model {model!r}")
        errors = []
        for s in candidates:
            try:
                result = await s.protocol.chat(model, messages, tools=tools,
                                               options=options, max_tokens=max_tokens)
                s.consecutive_failures = 0
                return result, s.config.name
            # ExceptionGroup: anyio TaskGroups can leak through httpcore on
            # transport failures (observed live as an empty error string).
            except (httpx.HTTPError, ProtocolError, OSError, ExceptionGroup) as e:
                detail = describe_exception(e)
                errors.append(f"{s.config.name}: {detail}")
                if self._record_failure(s, detail):
                    self._notify()
                self.db.log_event("warning", "backend_pool",
                                  f"call to {s.config.name} for {model} failed, trying next",
                                  detail)
        raise AllBackendsFailed(f"all backends failed for {model!r}: " + " | ".join(errors))

    async def chat_stream(self, model: str, messages: list[dict],
                          options: Optional[dict] = None) -> AsyncIterator[dict]:
        """Streaming passthrough (no failover mid-stream — once bytes have gone
        to the client we can't restart on another backend)."""
        candidates = self._candidates(model)
        if not candidates:
            raise AllBackendsFailed(f"no backend serves model {model!r}")
        s = candidates[0]
        try:
            async for chunk in s.protocol.chat_stream(model, messages, options=options):
                yield chunk
            s.consecutive_failures = 0
        except (httpx.HTTPError, ProtocolError, OSError, ExceptionGroup) as e:
            detail = describe_exception(e)
            if self._record_failure(s, detail):
                self._notify()
            raise AllBackendsFailed(f"stream from {s.config.name} failed: {detail}") from e

    # -- convenience -------------------------------------------------------------------

    def backends_of_type(self, backend_type: str) -> list[BackendState]:
        return [s for s in self.backends.values() if s.config.type == backend_type]
