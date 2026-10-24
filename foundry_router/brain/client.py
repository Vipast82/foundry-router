"""The model powering the Agent Brain — an explicit, swappable config choice
(design doc §4.2), reusing the same wire-protocol adapters as the Backend Pool
so `provider: ollama | meridian | openrouter` is just a protocol selection.

Reference default: a local Ornith-9B on the 3050 node with keep_alive=-1.
A paid brain (meridian/openrouter provider) works identically but inverts the
"routing itself is free" property — see the config comments and §4.2.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from ..config import AgentBrainConfig
from ..pool.protocols import ChatResult, make_protocol

log = logging.getLogger(__name__)

_PROVIDER_TO_PROTOCOL = {
    "ollama": "ollama",
    "meridian": "anthropic-compatible",
    "openrouter": "openai-compatible",
}


class BrainUnreachable(Exception):
    """The one failure mode that must degrade, not fail (§4.2): raised when the
    brain can't produce a decision, caught by the facade, which switches to the
    static keyword/length fallback rule instead of failing the request."""


# Marker for the one brain failure that is a self-correctable model mistake,
# not an unreachable host: llama-server rejecting a tool call whose JSON
# arguments were cut off mid-generation. Observed live with a small local
# brain deciding to write a long answer directly into return_to_user's
# `answer` field and running out of generation budget mid-JSON. It's
# nondeterministic model judgment, so one corrective retry usually fixes it.
_MALFORMED_TOOL_CALL_MARKER = "invalid tool call arguments"

_CORRECTION_MESSAGE = {
    "role": "user",
    "content": "Your previous response could not be parsed — it was cut off "
               "mid-generation because you tried to write a long answer "
               "directly instead of delegating. Do not write long content "
               "yourself. Call an ask_<model> tool to delegate this task, then "
               "call return_to_user with use_last_result set to true.",
}


class BrainClient:
    def __init__(self, cfg: AgentBrainConfig, client: httpx.AsyncClient, db=None):
        self.cfg = cfg
        self.db = db  # optional — retry events land in the troubleshooting log
        self.protocol = make_protocol(
            _PROVIDER_TO_PROTOCOL[cfg.provider], cfg.endpoint, cfg.api_key, client,
            meridian_profile=getattr(cfg, "meridian_profile", None))

    @property
    def model(self) -> str:
        return self.cfg.model

    # -- metrics (v0.141.0) ---------------------------------------------------------
    # brain_calls rows (one per call) and the calls in flight right now, for the
    # Performance tab's Brain panel and the live route view. Best-effort: a
    # metrics failure never touches the call.
    _active: Optional[dict] = None
    _seq: int = 0

    def active(self) -> list[dict]:
        now = time.monotonic()
        return sorted(({**{k: v for k, v in c.items() if k != "t0"},
                        "seconds": round(now - c["t0"], 1)}
                       for c in (self._active or {}).values()), key=lambda x: -x["seconds"])

    def _who(self) -> dict:
        try:
            from .. import request_context
            from ..client_auth import current
            c = current()
            return {"request_id": request_context.request_id() or None,
                    "persona": request_context.route().get("persona") or None,
                    "client_id": c.get("client_id") or None,
                    "client_addr": c.get("client_addr") or None}
        except Exception:                                          # noqa: BLE001
            return {}

    def _record(self, purpose: str, t0: float, outcome: str, result=None,
                reason: str = "", who: Optional[dict] = None) -> None:
        if self.db is None:
            return
        try:
            from ..db import utcnow
            ms = (time.monotonic() - t0) * 1000 if t0 else 0.0
            pt = int(getattr(result, "prompt_tokens", 0) or 0)
            ct = int(getattr(result, "completion_tokens", 0) or 0)
            tps = round(ct / (ms / 1000), 1) if ct and ms > 0 else None
            w = who if who is not None else self._who()
            self.db.execute(
                "INSERT INTO brain_calls (ts, purpose, provider, model, latency_ms, "
                "prompt_tokens, completion_tokens, tps, outcome, reason, request_id, persona, "
                "client_id, client_addr) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (utcnow(), purpose, self.cfg.provider, self.cfg.model, round(ms, 1), pt, ct,
                 tps, outcome, (reason or "")[:300], w.get("request_id"), w.get("persona"),
                 w.get("client_id"), w.get("client_addr")))
        except Exception:                                          # noqa: BLE001
            log.debug("brain_calls row not written", exc_info=True)

    # -- routing-mode gate -------------------------------------------------------
    # Set by the service from the periodic health probe; a real call failure
    # also opens the breaker for FAILURE_COOLDOWN seconds (auto mode), so a dead
    # brain costs one timeout, not one per request.
    health_down: Optional[str] = None
    _failed_at: float = 0.0
    FAILURE_COOLDOWN = 30.0

    def skip_reason(self, aux: bool = False) -> Optional[str]:
        """Why the brain must NOT be called right now (None = call it).
        aux=True is a background job (model research, price matching): those
        still use a configured, healthy brain in passthrough mode — passthrough
        only takes the brain out of request ROUTING."""
        import time
        mode = getattr(self.cfg, "routing_mode", "auto") or "auto"
        if mode == "passthrough" and not aux:
            return "routing mode is passthrough"
        if not (self.cfg.model or "").strip() or not (self.cfg.endpoint or "").strip():
            return "no brain configured"
        if mode == "auto":
            if self.health_down:
                return f"brain unhealthy ({self.health_down})"
            if self._failed_at and time.monotonic() - self._failed_at < self.FAILURE_COOLDOWN:
                return "brain failed moments ago"
        return None

    def mode_status(self) -> dict:
        reason = self.skip_reason()
        return {"routing_mode": getattr(self.cfg, "routing_mode", "auto") or "auto",
                "active": reason is None, "reason": reason or ""}

    async def loaded_detail(self) -> dict:
        """Is the brain model warm on its endpoint, and how much VRAM is it
        holding? Queries the brain endpoint's /api/ps when it's an Ollama
        provider (best-effort). Returns {loaded, size_vram} — loaded=None when
        the endpoint can't report (non-ollama, or unreachable)."""
        fn = getattr(self.protocol, "loaded_models_detail", None)
        if fn is None:
            return {"loaded": None, "size_vram": 0}
        try:
            base = (self.cfg.model or "").split(":")[0]
            for m in await fn():
                if m["model"] == self.cfg.model or m["model"].split(":")[0] == base:
                    return {"loaded": True, "size_vram": m.get("size_vram") or 0}
            return {"loaded": False, "size_vram": 0}
        except Exception:
            return {"loaded": None, "size_vram": 0}

    async def chat(self, messages: list[dict], tools: Optional[list[dict]] = None,
                   on_retry=None, aux: bool = False) -> ChatResult:
        """`on_retry` (optional zero-arg callable) fires when the malformed-
        tool-call retry engages — the agent passes an emitter so the retry is
        narrated to the client, not just written to the Events log."""
        purpose = "background" if aux else ("routing" if tools else "assist")
        who = self._who()
        reason = self.skip_reason(aux=aux)
        if reason:
            # Instant degrade — every caller already handles BrainUnreachable.
            self._record(purpose, 0.0, "skipped", reason=reason, who=who)
            raise BrainUnreachable(f"brain skipped: {reason}")
        msgs = messages
        t0 = time.monotonic()
        if self._active is None:
            self._active = {}
        self._seq += 1
        call_id = self._seq
        self._active[call_id] = {"t0": t0, "model": self.cfg.model, "purpose": purpose, **who}
        try:
            return await self._chat_attempts(msgs, tools, on_retry, purpose, t0, who)
        finally:
            self._active.pop(call_id, None)

    async def _chat_attempts(self, msgs, tools, on_retry, purpose, t0, who) -> ChatResult:
        for attempt in (1, 2):
            try:
                res = await self.protocol.chat(
                    self.cfg.model, msgs, tools=tools,
                    options=self.cfg.options or None,
                    keep_alive=self.cfg.keep_alive if self.cfg.provider == "ollama" else None,
                    max_tokens=self.cfg.max_tokens)
                self._record(purpose, t0, "ok" if attempt == 1 else "retried", res, who=who)
                return res
            except Exception as e:
                emsg = str(e)
                # Malformed tool-call JSON: the brain isn't down, it made a
                # mistake — retry ONCE with corrective feedback before
                # degrading. Gated on `tools` because the correction tells the
                # brain to delegate via ask_* tools, which only exist on agent
                # calls (complete() has no tools and can't hit this anyway).
                if attempt == 1 and tools and _MALFORMED_TOOL_CALL_MARKER in emsg:
                    if on_retry is not None:
                        try:
                            on_retry()
                        except Exception:  # narration must never break the retry
                            log.exception("on_retry callback failed")
                    if self.db is not None:
                        self.db.log_event(
                            "warning", "brain",
                            "brain emitted a malformed tool call — retrying once "
                            "with corrective feedback instead of falling back",
                            emsg[:500])
                    msgs = list(msgs) + [_CORRECTION_MESSAGE]
                    continue
                # Everything else — connection refused, timeout, model
                # missing, second malformed attempt — funnels into the same
                # degrade path. The brain host rebooting must never take the
                # whole service down.
                from ..errors import describe_exception
                self._failed_at = time.monotonic()
                self._record(purpose, t0, "failed", reason=describe_exception(e), who=who)
                raise BrainUnreachable(
                    f"agent brain ({self.cfg.provider} {self.cfg.model} @ "
                    f"{self.cfg.endpoint}): {describe_exception(e)}") from e
        raise AssertionError("unreachable")  # loop always returns or raises

    async def complete(self, prompt: str, aux: bool = False) -> str:
        """Single-shot completion for auxiliary jobs (refine_prompt, research
        extraction). Same degrade semantics; aux=True for background jobs."""
        result = await self.chat([{"role": "user", "content": prompt}], aux=aux)
        return result.content

    async def health(self) -> dict:
        """Cheap reachability probe of the brain endpoint — the brain runs on
        EVERY request, so an unnoticed brain outage silently degrades every
        request to the static fallback rule (found live: brain down, no visible
        signal). Uses the endpoint's free model list (a GET, never a paid
        generation), and reports whether the configured model is actually
        present — the endpoint being up but the model unpulled is a distinct,
        common failure. Returns {healthy, model_present, provider, model,
        error}; never raises."""
        from ..errors import describe_exception
        base = {"provider": self.cfg.provider, "model": self.cfg.model}
        try:
            models = await self.protocol.list_models()
            root = (self.cfg.model or "").split(":")[0].lower()
            present = bool(root) and any(root in (m or "").lower() for m in models)
            return {**base, "healthy": True, "model_present": present, "error": ""}
        except Exception as e:
            return {**base, "healthy": False, "model_present": None,
                    "error": describe_exception(e)}
