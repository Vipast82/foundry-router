"""The model powering the Agent Brain — an explicit, swappable config choice
(design doc §4.2), reusing the same wire-protocol adapters as the Backend Pool
so `provider: ollama | meridian | openrouter` is just a protocol selection.

Reference default: a local Ornith-9B on the 3050 node with keep_alive=-1.
A paid brain (meridian/openrouter provider) works identically but inverts the
"routing itself is free" property — see the config comments and §4.2.
"""

from __future__ import annotations

import logging
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
            _PROVIDER_TO_PROTOCOL[cfg.provider], cfg.endpoint, cfg.api_key, client)

    @property
    def model(self) -> str:
        return self.cfg.model

    async def chat(self, messages: list[dict], tools: Optional[list[dict]] = None,
                   on_retry=None) -> ChatResult:
        """`on_retry` (optional zero-arg callable) fires when the malformed-
        tool-call retry engages — the agent passes an emitter so the retry is
        narrated to the client, not just written to the Events log."""
        msgs = messages
        for attempt in (1, 2):
            try:
                return await self.protocol.chat(
                    self.cfg.model, msgs, tools=tools,
                    options=self.cfg.options or None,
                    keep_alive=self.cfg.keep_alive if self.cfg.provider == "ollama" else None,
                    max_tokens=self.cfg.max_tokens)
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
                raise BrainUnreachable(
                    f"agent brain ({self.cfg.provider} {self.cfg.model} @ "
                    f"{self.cfg.endpoint}): {describe_exception(e)}") from e
        raise AssertionError("unreachable")  # loop always returns or raises

    async def complete(self, prompt: str) -> str:
        """Single-shot completion for auxiliary jobs (refine_prompt, research
        extraction). Same degrade semantics."""
        result = await self.chat([{"role": "user", "content": prompt}])
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
