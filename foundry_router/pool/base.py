"""Backend Pool interface + factory (design doc §4.3).

The pool has exactly one job for the rest of the system: given a logical model
name, return a working endpoint to call — exposed here as `chat()` (which also
performs the call, so failover stays encapsulated). Nothing above this layer
(Agent Brain, tools, facade) knows which physical host serves a model.

Three modes, one interface. `olla` and `litellm` are deliberately thin: an
Olla instance speaks the Ollama wire protocol and a LiteLLM proxy speaks the
OpenAI one, so both are realized as an InternalPool containing exactly one
synthesized backend that forwards everything — model resolution, failover,
retries — to the external tool. That keeps this codebase to a single pool
implementation while honoring "don't stand up a second, redundant failover
layer" for people who already run one of those tools.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx
    from ..config import AppConfig
    from ..db import Database


class AllBackendsFailed(Exception):
    """Every backend capable of serving the requested model failed. The agent
    catches this and can choose a different model tier instead of failing the
    whole request (§4.3)."""


class ContextTooLarge(AllBackendsFailed):
    """The request wouldn't fit the target model's context window — rejected
    BEFORE dispatch rather than sent to earn a raw API error. Subclasses
    AllBackendsFailed so every existing degrade path (agent tool loop, pipeline,
    judge) treats it as a controlled failure and reroutes/degrades."""


class BackendPool:  # interface — see internal.InternalPool for the implementation
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def chat(self, model, messages, tools=None, options=None, max_tokens=4096):
        """Returns (ChatResult, backend_name). Raises AllBackendsFailed."""
        raise NotImplementedError

    def chat_stream(self, model, messages, options=None):
        raise NotImplementedError

    def available_models(self) -> dict[str, list[str]]:
        """{logical model name: [backend names serving it]}, healthy backends
        only — 'healthy' flips only after failure_threshold consecutive
        failures, which is what gives Tool Sync its removal grace period."""
        raise NotImplementedError

    def backend_status(self) -> list[dict]:
        raise NotImplementedError

    def backend_info(self, model: str) -> dict | None:
        """Metadata about the highest-priority backend serving `model`
        (name/type/url) — guardrails use `type` to spot subscription backends."""
        raise NotImplementedError

    def add_state_listener(self, callback) -> None:
        """callback() is invoked (sync) whenever a backend changes health or
        its model list changes — Tool Sync subscribes for immediate re-sync."""
        raise NotImplementedError


def build_pool(config: "AppConfig", client: "httpx.AsyncClient", db: "Database") -> BackendPool:
    from ..config import BackendConfig
    from .internal import InternalPool

    pc = config.backend_pool
    if pc.mode == "internal":
        backends = pc.internal.backends
    elif pc.mode == "olla":
        backends = [BackendConfig(name="olla", type="ollama", url=pc.olla.url, priority=100)]
    elif pc.mode == "litellm":
        backends = [BackendConfig(name="litellm", type="openai-compatible",
                                  url=pc.litellm.url, api_key=pc.litellm.api_key, priority=100)]
    else:  # pragma: no cover — pydantic already validates the literal
        raise ValueError(f"unknown backend_pool.mode {pc.mode!r}")
    return InternalPool(backends, pc, client, db)
