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


class BrainClient:
    def __init__(self, cfg: AgentBrainConfig, client: httpx.AsyncClient):
        self.cfg = cfg
        self.protocol = make_protocol(
            _PROVIDER_TO_PROTOCOL[cfg.provider], cfg.endpoint, cfg.api_key, client)

    @property
    def model(self) -> str:
        return self.cfg.model

    async def chat(self, messages: list[dict], tools: Optional[list[dict]] = None) -> ChatResult:
        try:
            return await self.protocol.chat(
                self.cfg.model, messages, tools=tools,
                options=self.cfg.options or None,
                keep_alive=self.cfg.keep_alive if self.cfg.provider == "ollama" else None,
                max_tokens=self.cfg.max_tokens)
        except Exception as e:
            # Any brain failure — connection refused, timeout, model missing,
            # malformed reply — funnels into the same degrade path. The brain
            # host rebooting must never take the whole service down.
            raise BrainUnreachable(f"agent brain ({self.cfg.provider} "
                                   f"{self.cfg.model} @ {self.cfg.endpoint}): {e}") from e

    async def complete(self, prompt: str) -> str:
        """Single-shot completion for auxiliary jobs (refine_prompt, research
        extraction). Same degrade semantics."""
        result = await self.chat([{"role": "user", "content": prompt}])
        return result.content
