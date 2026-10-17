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
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

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
    busy: int = 0                      # calls in flight on THIS backend right now


class QueueTimeout(Exception):
    pass


class FairGate:
    """At most `capacity` calls on one backend at a time. Callers beyond that
    wait; a freed slot goes to the next CLIENT in turn (round robin), FIFO
    within a client, so one client's burst can't starve another."""

    def __init__(self, capacity: int):
        self.capacity = max(1, int(capacity))
        self.active = 0
        self.waiting: dict = {}                 # client -> deque[(future, since)]
        self.order: deque = deque()             # clients with waiters, in turn order
        self.served_from_queue = 0
        self.timeouts = 0

    def waiting_count(self) -> int:
        return sum(len(q) for q in self.waiting.values())

    async def acquire(self, client: str, timeout: float) -> float:
        """Seconds waited. Raises QueueTimeout."""
        if self.active < self.capacity and not self.waiting:
            self.active += 1
            return 0.0
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        t0 = time.monotonic()
        q = self.waiting.setdefault(client, deque())
        q.append((fut, t0))
        if client not in self.order:
            self.order.append(client)
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            if fut.done() and not fut.cancelled():
                self.release()                  # the slot arrived just as we gave up
            else:
                fut.cancel()
                self._forget(client, fut)
            if isinstance(e, asyncio.TimeoutError):
                self.timeouts += 1
                raise QueueTimeout(f"waited {int(timeout)}s for a free slot") from None
            raise
        return time.monotonic() - t0

    def _forget(self, client, fut) -> None:
        q = self.waiting.get(client)
        if q is None:
            return
        for item in list(q):
            if item[0] is fut:
                q.remove(item)
        if not q:
            self.waiting.pop(client, None)
            try:
                self.order.remove(client)
            except ValueError:
                pass

    def release(self) -> None:
        while self.order:
            client = self.order.popleft()
            q = self.waiting.get(client)
            if not q:
                self.waiting.pop(client, None)
                continue
            fut, _t0 = q.popleft()
            if q:
                self.order.append(client)       # back of the line for its next waiter
            else:
                self.waiting.pop(client, None)
            if not fut.done():
                fut.set_result(True)            # slot handed over: active unchanged
                self.served_from_queue += 1
                return
        self.active = max(0, self.active - 1)

    def status(self) -> dict:
        now = time.monotonic()
        oldest = min([t for q in self.waiting.values() for _f, t in q] or [now])
        return {"capacity": self.capacity, "active": self.active,
                "waiting": {c: len(q) for c, q in self.waiting.items()},
                "waiting_total": self.waiting_count(),
                "oldest_wait_s": round(now - oldest, 1) if self.waiting else 0,
                "served_from_queue": self.served_from_queue, "timeouts": self.timeouts}


def _estimate(messages) -> int:
    from ..budget import estimate_tokens
    return estimate_tokens(messages)


class InternalPool(BackendPool):
    def __init__(self, backends: list[BackendConfig], pool_cfg: BackendPoolConfig,
                 client: httpx.AsyncClient, db: Database):
        self.cfg = pool_cfg
        self.db = db
        self.client = client
        # Budget ledger (budget.py), set by Services: every call is reserved
        # before it is sent and settled after. None = no budget enforcement.
        self.ledger = None
        self.gates: dict[str, FairGate] = {}
        self.backends: dict[str, BackendState] = {}
        # Disabled backends are kept only for the status list: nothing probes,
        # routes to, or raises alerts about them.
        self.disabled: list[BackendConfig] = [b for b in backends
                                              if not getattr(b, "enabled", True)]
        for b in backends:
            if not getattr(b, "enabled", True):
                continue
            self.backends[b.name] = BackendState(
                config=b, protocol=make_protocol(b.type, b.url, b.api_key, client,
                                                 flavor=getattr(b, "effective_flavor", None),
                                                 meridian_profile=getattr(b, "meridian_profile", None),
                                                 meridian_agent=getattr(b, "meridian_agent", None),
                                                 meridian_session_affinity=bool(
                                                     getattr(b, "meridian_session_affinity", False))))
        self._listeners: list[Callable[[], None]] = []
        self._health_task: Optional[asyncio.Task] = None
        self._loaded: set[str] = set()   # VRAM-resident models, cached (see loaded_models)
        self._loaded_ts: float = 0.0
        self._LOADED_TTL = 8.0
        # Live in-flight model calls: model -> {count, since}. Incremented around
        # every chat/chat_stream so Foundry knows which models are actively
        # generating right now (vs merely loaded) — the basis for activity
        # reporting and, later, load-aware routing.
        self._inflight: dict[str, dict] = {}
        self._metrics: list[dict] = []   # engine /metrics snapshot (see server_metrics)
        self._metrics_ts: float = 0.0
        self._METRICS_TTL = 2.0

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
            # Busy is not down: a server grinding through a huge prompt (llama.cpp
            # prefilling 100k+ tokens) can be too busy to answer a model-list
            # probe in time. While it has calls in flight, a probe TIMEOUT is not
            # counted — only a refused/reset connection (the process really gone)
            # is. Otherwise the next request is routed away mid-task.
            if s.busy > 0 and isinstance(e, (httpx.TimeoutException, asyncio.TimeoutError)):
                s.last_error = (f"health probe timed out while busy with {s.busy} "
                                f"call(s) — not counted as a failure")
                return False
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
        out = healthy + retryable
        from ..request_context import backend_pin
        pin = backend_pin()
        if pin == "*first":
            return out[:1]
        if pin:
            return [s for s in out if s.config.name == pin]
        return out

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
        return {"name": c.name, "type": c.type, "url": c.url, "api_key": c.api_key,
                "flavor": getattr(c, "effective_flavor", None),
                "subscription": bool(getattr(c, "is_subscription", False)),
                "call_budget_5h": getattr(c, "call_budget_5h", None),
                "call_budget_daily": getattr(c, "call_budget_daily", None),
                "meridian_profile": getattr(c, "meridian_profile", None),
                "management_key": getattr(c, "management_key", None),
                "management_url": getattr(c, "management_url", None),
                "usage_limit_pct": getattr(c, "usage_limit_pct", None)}

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

    async def loaded_models_detail(self) -> list[dict]:
        """Per-model residency across healthy backends (uncached — the Live view
        wants a fresh number). Each entry carries its backend. Ollama reports
        VRAM bytes via /api/ps; local openai-dialect servers (llama.cpp/unsloth/
        vLLM) report the served model + context via /props but no VRAM bytes."""
        out: list[dict] = []
        for s in self.backends.values():
            if not s.healthy:
                continue
            probe = (s.config.type == "ollama"
                     or hasattr(s.protocol, "loaded_models_detail"))
            if not probe:
                continue
            try:
                for d in await s.protocol.loaded_models_detail():
                    out.append({**d, "backend": s.config.name})
            except Exception:
                pass          # /api/ps or /props missing/unreachable — just skip it
        out.sort(key=lambda d: -(d.get("size_vram") or 0))
        return out

    async def server_metrics(self) -> list[dict]:
        """Engine-level telemetry (KV-cache fill, queue depth, busy slots,
        preemptions, lifetime throughput…) from every healthy llama.cpp / vLLM
        backend, probed concurrently. Cached ~2s: the Live view polls every 2s
        per open tab, and each probe is one or two tiny GETs."""
        now = time.monotonic()
        if now - self._metrics_ts < self._METRICS_TTL:
            return self._metrics
        targets = [s for s in self.backends.values()
                   if s.healthy and hasattr(s.protocol, "server_metrics")]

        async def one(s: BackendState) -> Optional[dict]:
            try:
                m = await s.protocol.server_metrics()
            except Exception as e:
                return {"backend": s.config.name, "flavor": getattr(s.config, "effective_flavor", None),
                        "metrics_ok": False, "metrics_error": describe_exception(e)}
            return {**m, "backend": s.config.name} if m else None

        res = await asyncio.gather(*(one(s) for s in targets))
        self._metrics = sorted((r for r in res if r), key=lambda r: r["backend"])
        self._metrics_ts = now
        return self._metrics

    def backend_status(self) -> list[dict]:
        return [{
            "name": s.config.name, "type": s.config.type, "url": s.config.url,
            "flavor": getattr(s.config, "effective_flavor", None),
            "subscription": bool(getattr(s.config, "is_subscription", False)),
            "priority": s.config.priority, "healthy": s.healthy,
            "consecutive_failures": s.consecutive_failures,
            "models": s.models, "last_error": s.last_error, "busy": s.busy,
            "enabled": True,
        } for s in self.backends.values()] + [{
            "name": b.name, "type": b.type, "url": b.url,
            "flavor": getattr(b, "effective_flavor", None), "priority": b.priority,
            "subscription": bool(getattr(b, "is_subscription", False)),
            "healthy": None, "consecutive_failures": 0, "models": [],
            "last_error": "", "busy": 0, "enabled": False,
        } for b in getattr(self, "disabled", [])]

    # -- in-flight tracking ------------------------------------------------------------

    def _inflight_enter(self, model: str, backend: str = "") -> int:
        e = self._inflight.get(model)
        if e:
            e["count"] += 1
        else:
            self._inflight[model] = {"count": 1, "since": time.monotonic()}
        # Per-call record (request id + start), so the Live view can list each
        # call with the same id and clock the client's status line uses.
        from .. import request_context
        self._call_seq = getattr(self, "_call_seq", 0) + 1
        calls = getattr(self, "_calls", None)
        if calls is None:
            calls = self._calls = {}
        calls[self._call_seq] = {"model": model, "backend": backend,
                                 "since": time.monotonic(),
                                 "request_id": request_context.request_id() or ""}
        return self._call_seq

    def active_requests(self) -> list[dict]:
        """Every backend call in flight: [{model, backend, seconds, request_id}],
        longest first."""
        now = time.monotonic()
        return sorted(({"model": c["model"], "backend": c["backend"],
                        "seconds": round(now - c["since"], 1),
                        "request_id": c["request_id"],
                        "progress": c.get("progress"),
                        "phase": ("generating" if c.get("generating") else
                                  "reading prompt" if (c.get("progress") or {}).get("prefill")
                                  else "writing tool call" if c.get("progress")
                                  else "waiting for server")}
                       for c in (getattr(self, "_calls", None) or {}).values()),
                      key=lambda x: -x["seconds"])

    def _inflight_exit(self, model: str, call_id: int = 0) -> None:
        if call_id:
            (getattr(self, "_calls", None) or {}).pop(call_id, None)
        e = self._inflight.get(model)
        if not e:
            return
        e["count"] -= 1
        if e["count"] <= 0:
            self._inflight.pop(model, None)

    def active_calls(self) -> list[dict]:
        """Models with a call in flight right now: [{model, count, seconds}],
        longest-running first. Distinct from loaded_models() (resident in VRAM
        but possibly idle) — this is who is actually generating."""
        now = time.monotonic()
        return sorted(
            ({"model": m, "count": e["count"], "seconds": round(now - e["since"], 1)}
             for m, e in self._inflight.items()),
            key=lambda x: -x["seconds"])

    # -- calls ------------------------------------------------------------------------

    async def chat(self, model: str, messages: list[dict], tools: Optional[list] = None,
                   options: Optional[dict] = None, max_tokens: int = 4096,
                   keep_alive: Any = None, think: Any = None,
                   fmt: Any = None) -> tuple[ChatResult, str]:
        candidates = self._candidates(model)
        if not candidates:
            raise AllBackendsFailed(f"no backend serves model {model!r}")
        errors = []
        est = _estimate(messages) if self.ledger else 0
        call_id = self._inflight_enter(model, candidates[0].config.name)
        try:
            for s in candidates:
                # Budget: reserved per attempt (retries count); a denial is raised
                # as-is, never answered by trying another backend.
                eid = await self._reserve(model, s, est)
                try:
                    await self._acquire(s)
                except QueueTimeout as e:
                    detail = f"{s.config.name}: queue — {e}"
                    errors.append(detail)
                    self._settle(eid, "released", detail=detail)
                    continue
                except BaseException:
                    self._settle(eid, "released", detail="cancelled while queued")
                    raise
                await self._recheck_optional(s, eid)
                s.busy += 1
                settled = False
                cap = getattr(self, "capture", None)
                ctok = cap.begin_call() if cap is not None else None
                csettings = {"options": options, "max_tokens": max_tokens, "think": think,
                             "format": fmt, "keep_alive": keep_alive}
                try:
                    result = await s.protocol.chat(model, messages, tools=tools,
                                                   options=options, max_tokens=max_tokens,
                                                   keep_alive=keep_alive, think=think, fmt=fmt)
                    s.consecutive_failures = 0
                    self._settle(eid, "settled", result.prompt_tokens, result.completion_tokens)
                    settled = True
                    if ctok is not None:
                        cap.finish_call(ctok, model=model, backend=s.config.name,
                                        messages=messages, tools=tools, settings=csettings,
                                        result=result)
                    return result, s.config.name
                # ExceptionGroup: anyio TaskGroups can leak through httpcore on
                # transport failures (observed live as an empty error string).
                except (httpx.HTTPError, ProtocolError, OSError, ExceptionGroup) as e:
                    detail = describe_exception(e)
                    self._settle(eid, "released" if isinstance(
                        e, (httpx.ConnectError, httpx.ConnectTimeout)) else "error", detail=detail)
                    settled = True
                    errors.append(f"{s.config.name}: {detail}")
                    if ctok is not None:
                        cap.finish_call(ctok, model=model, backend=s.config.name,
                                        messages=messages, tools=tools, settings=csettings,
                                        error=detail, status="error")
                    if self._record_failure(s, detail):
                        self._notify()
                    self.db.log_event("warning", "backend_pool",
                                      f"call to {s.config.name} for {model} failed, trying next",
                                      detail)
                finally:
                    s.busy = max(0, s.busy - 1)
                    self._release(s)
                    if not settled:            # cancelled or an unexpected error
                        self._settle(eid, "error", detail="call did not complete")
                        if ctok is not None:
                            cap.finish_call(ctok, model=model, backend=s.config.name,
                                            messages=messages, tools=tools, settings=csettings,
                                            error="call did not complete (cancelled)",
                                            status="cancelled")
        finally:
            self._inflight_exit(model, call_id)
        raise AllBackendsFailed(f"all backends failed for {model!r}: " + " | ".join(errors))

    def _gate(self, s: BackendState) -> Optional[FairGate]:
        cap = getattr(s.config, "max_concurrent", None)
        if not cap:
            return None
        g = self.gates.get(s.config.name)
        if g is None or g.capacity != max(1, int(cap)):
            g = self.gates[s.config.name] = FairGate(cap) if g is None else _resized(g, cap)
        return g

    async def _acquire(self, s: BackendState) -> None:
        g = self._gate(s)
        if g is None:
            return
        from .. import client_auth, request_context
        waited = await g.acquire(client_auth.current().get("client_id") or "anonymous",
                                 float(getattr(s.config, "queue_timeout_seconds", 300) or 300))
        if waited >= 0.05:
            r = request_context.route()
            request_context.note_route(queue_ms=round((r.get("queue_ms") or 0) + waited * 1000))

    def _release(self, s: BackendState) -> None:
        g = self.gates.get(s.config.name)
        if g is not None and getattr(s.config, "max_concurrent", None):
            g.release()

    def queue_status(self) -> list[dict]:
        out = []
        for s in self.backends.values():
            cap = getattr(s.config, "max_concurrent", None)
            g = self.gates.get(s.config.name)
            out.append({"backend": s.config.name, "limited": bool(cap),
                        **(g.status() if g else {"capacity": cap, "active": s.busy if cap else None,
                                                 "waiting": {}, "waiting_total": 0,
                                                 "oldest_wait_s": 0, "served_from_queue": 0,
                                                 "timeouts": 0}),
                        "busy": s.busy})
        return out

    def breaker_status(self) -> list[dict]:
        """closed = in use; open = failing, skipped until the cooldown ends;
        half-open = cooldown over, the next call is the trial."""
        now = time.monotonic()
        out = []
        for s in self.backends.values():
            if s.healthy or not s.ever_checked:
                state, retry = "closed", None
            elif now < s.cooldown_until:
                state, retry = "open", round(s.cooldown_until - now)
            else:
                state, retry = "half-open", 0
            out.append({"backend": s.config.name, "state": state,
                        "consecutive_failures": s.consecutive_failures,
                        "threshold": self.cfg.failure_threshold,
                        "retry_in_s": retry, "last_error": s.last_error or None})
        return out

    def reset_breaker(self, name: str) -> bool:
        s = self.backends.get(name)
        if s is None:
            return False
        s.consecutive_failures = 0
        s.cooldown_until = 0.0
        s.healthy = True
        s.last_error = ""
        self.db.log_event("info", "backend_pool", f"breaker for {name} reset by the operator")
        self._notify()
        return True

    async def _reserve(self, model: str, s: BackendState, est: int) -> Optional[int]:
        if self.ledger is None:
            return None
        from ..budget import context_for
        return await self.ledger.reserve_wait(context_for(model, s.config, est))

    async def _recheck_optional(self, s: BackendState, eid) -> None:
        """An optional-purpose call that waited in the backend queue is admitted
        again as it starts (quota may have crossed the cutoff meanwhile)."""
        if self.ledger is None or not eid:
            return
        ctx = self.ledger._active.get(eid)
        if ctx is None or not ctx.purpose:
            return
        denial = await self.ledger.optional_denial(ctx)
        if denial is not None:
            self._release(s)
            self._settle(eid, "released", detail="optional work deferred after waiting in "
                                                 "the queue")
            self.ledger.record(ctx, "denied", denial.rule, denial.message)
            raise denial

    def _settle(self, eid, status, pt=0, ct=0, detail=""):
        if self.ledger is not None and eid:
            self.ledger.settle(eid, status, pt, ct, detail)

    async def chat_stream(self, model: str, messages: list[dict],
                          tools: Optional[list] = None,
                          options: Optional[dict] = None,
                          keep_alive: Any = None, think: Any = None,
                          max_tokens: Any = None, fmt: Any = None) -> AsyncIterator[dict]:
        """Streaming passthrough (no failover mid-stream — once bytes have gone
        to the client we can't restart on another backend). Carries tools +
        keep_alive + think + max_tokens so direct-dispatch can stream a coding
        client's turn (num_predict caps a runaway generation)."""
        candidates = self._candidates(model)
        if not candidates:
            raise AllBackendsFailed(f"no backend serves model {model!r}")
        s = candidates[0]
        eid = await self._reserve(model, s, _estimate(messages) if self.ledger else 0)
        try:
            await self._acquire(s)
        except QueueTimeout as e:
            self._settle(eid, "released", detail=f"queue — {e}")
            raise AllBackendsFailed(f"{s.config.name}: queue — {e}") from None
        except BaseException:
            self._settle(eid, "released", detail="cancelled while queued")
            raise
        await self._recheck_optional(s, eid)
        usage = {"pt": 0, "ct": 0, "status": "error", "detail": "stream did not complete"}
        cap = getattr(self, "capture", None)
        ctok = cap.begin_call() if cap is not None else None
        if ctok is not None:
            from ..capture import accumulate, stream_accumulator
            cacc = stream_accumulator()
        call_id = self._inflight_enter(model, s.config.name)
        s.busy += 1
        calls = getattr(self, "_calls", None) or {}
        try:
            async for chunk in s.protocol.chat_stream(model, messages, tools=tools,
                                                      options=options,
                                                      keep_alive=keep_alive,
                                                      think=think,
                                                      max_tokens=max_tokens,
                                                      fmt=fmt):
                if chunk.get("progress") and call_id in calls:
                    calls[call_id]["progress"] = chunk["progress"]
                elif (chunk.get("content") or chunk.get("thinking")) and call_id in calls:
                    calls[call_id].pop("progress", None)
                    calls[call_id]["generating"] = True
                if chunk.get("done"):
                    usage.update(pt=chunk.get("prompt_tokens") or 0,
                                 ct=chunk.get("completion_tokens") or 0)
                if ctok is not None:
                    accumulate(cacc, chunk)
                yield chunk
            s.consecutive_failures = 0
            usage.update(status="settled", detail="")
        except (httpx.HTTPError, ProtocolError, OSError, ExceptionGroup) as e:
            detail = describe_exception(e)
            usage.update(detail=detail, status="released" if isinstance(
                e, (httpx.ConnectError, httpx.ConnectTimeout)) else "error")
            if self._record_failure(s, detail):
                self._notify()
            raise AllBackendsFailed(f"stream from {s.config.name} failed: {detail}") from e
        finally:
            s.busy = max(0, s.busy - 1)
            self._release(s)
            self._inflight_exit(model, call_id)
            self._settle(eid, usage["status"], usage["pt"], usage["ct"], usage["detail"])
            if ctok is not None:
                ok = usage["status"] == "settled" and cacc["done"]
                cap.finish_call(
                    ctok, model=model, backend=s.config.name, messages=messages, tools=tools,
                    settings={"options": options, "max_tokens": max_tokens, "think": think,
                              "format": fmt, "keep_alive": keep_alive},
                    stream=True, stream_acc=cacc,
                    reconstruction="complete" if ok else "incomplete",
                    status="ok" if ok else ("error" if usage["status"] in ("error", "released")
                                            and usage["detail"] != "stream did not complete"
                                            else "cancelled"),
                    error="" if ok else usage["detail"])

    async def embed(self, model: str, inputs: list[str], **kw) -> tuple[dict, str]:
        """Embeddings with the same priority-ordered failover as chat()."""
        candidates = self._candidates(model)
        if not candidates:
            raise AllBackendsFailed(f"no backend serves model {model!r}")
        errors = []
        for s in candidates:
            try:
                return await s.protocol.embed(model, inputs, **kw), s.config.name
            except (httpx.HTTPError, ProtocolError, OSError, ExceptionGroup) as e:
                errors.append(f"{s.config.name}: {describe_exception(e)}")
        raise AllBackendsFailed(f"embedding failed for {model!r}: " + " | ".join(errors))

    # -- convenience -------------------------------------------------------------------

    def backends_of_type(self, backend_type: str) -> list[BackendState]:
        return [s for s in self.backends.values() if s.config.type == backend_type]


def _resized(g: FairGate, cap: int) -> FairGate:
    """Capacity changed in config: keep the waiters and in-flight count."""
    g.capacity = max(1, int(cap))
    return g
