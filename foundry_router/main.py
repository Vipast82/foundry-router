"""Application wiring: one FastAPI service, one SQLite file, no intermediary
proxy layers (design doc §3).

`Services` is the composition root — everything is constructed here and
reached via `request.app.state.services`, which is also what lets the admin
API rebuild the pool or brain in place after a config edit.

Background work (all skipped when FOUNDRY_DISABLE_BACKGROUND=1, used by tests):
  - Backend Pool health loop (which doubles as model discovery)
  - Tool Sync: immediate on pool state changes + periodic fallback sweep
  - OpenRouter registry poll (daily by default)
  - Research Agent worker + staleness sweep
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI

from . import __version__
from .brain.agent import AgentRunner
from .brain.client import BrainClient
from .config import ConfigStore, data_dir
from .db import Database
from .facade import router as facade_router
from .guardrails import GuardrailEngine
from .personas import PersonaStore
from .pool.base import build_pool
from .registry.models_db import ModelRegistry
from .registry.openrouter_ingest import poll_openrouter
from .registry.research_agent import ResearchAgent
from .tools.mcp_client import MCPManager
from .tools.sync import ToolRegistry
from .ui import router as ui_router
from .usage import MeridianUsage

log = logging.getLogger(__name__)


class Services:
    """Composition root; every route handler and background task reaches
    dependencies through this object."""

    def __init__(self, config_store: ConfigStore, db: Database):
        self.config_store = config_store
        self.db = db
        cfg = config_store.config

        # One shared HTTP client: connection pooling for every backend, the
        # brain, telemetry, and OpenRouter. Long read timeout because local
        # GPU generations are slow; short connect timeout so failover is fast.
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(
            connect=5.0, read=float(cfg.backend_pool.request_timeout_seconds),
            write=30.0, pool=5.0))

        self.pool = build_pool(cfg, self.http, db)
        self.registry = ModelRegistry(db)
        self.personas = PersonaStore(db)
        self.mcp = MCPManager(cfg.mcp_servers, db)
        self.tool_registry = ToolRegistry(db, self.registry, self.mcp)
        self.meridian_usage = MeridianUsage(cfg.meridian, self.http, db)
        self.guardrails = GuardrailEngine(cfg.guardrails, db, self.meridian_usage,
                                          pool_mode=cfg.backend_pool.mode)
        self.brain = BrainClient(cfg.agent_brain, self.http, db=db)
        self.research = ResearchAgent(
            cfg.registry.research, db, self.registry, self.mcp,
            llm=self._research_llm, available_models=lambda: list(self.pool.available_models()))
        self.agent = AgentRunner(self.brain, self.pool, self.tool_registry,
                                 self.registry, self.guardrails, self.meridian_usage,
                                 research=self.research)
        self._bg_tasks: list[asyncio.Task] = []

    # -- research LLM: dedicated model if configured, else the brain (§7) ------

    async def _research_llm(self, prompt: str) -> str:
        research_model = self.config_store.config.registry.research.model
        if research_model:
            try:
                result, _ = await self.pool.chat(
                    research_model, [{"role": "user", "content": prompt}], max_tokens=4096)
                return result.content
            except Exception as e:
                self.db.log_event("warning", "research",
                                  f"dedicated research model {research_model} failed, "
                                  f"falling back to brain", str(e))
        return await self.brain.complete(prompt)

    # -- discovery -> registry bridge ------------------------------------------

    def register_discovered(self) -> None:
        """Give every discovered model a registry row (provider = backend
        name), so ranking/ingest/research all have somewhere to attach.

        Cost tier is stamped from the BACKEND TYPE at ingestion — a fact, not
        an inference: ollama backends serve for free ("free" tier, zero cost
        fields), anthropic-compatible ones draw subscription window ("high"
        tier default). Capability tags and content policy are seeded from
        name heuristics; the Research Agent refines them, manual overrides
        pin them (upsert_auto never replaces a hand-set value)."""
        import json as _json

        from .registry.tagging import content_policy_from_name, tags_from_name
        from .usage import CLAUDE_DEFAULT_CONTEXT

        for s in getattr(self.pool, "backends", {}).values():
            if not s.healthy:
                continue
            for model_id in s.models:
                try:
                    fields: dict = {"provider": s.config.name, "display_name": model_id}
                    if s.config.type == "ollama":
                        fields.update(relative_cost_tier="free",
                                      cost_per_1k_input=0.0, cost_per_1k_output=0.0)
                    elif s.config.type == "anthropic-compatible":
                        # Stamp a conservative context ceiling so oversized
                        # requests are gated before escalation (local windows can
                        # exceed Claude's). A manual override still wins.
                        fields.update(relative_cost_tier="high",
                                      context_length=CLAUDE_DEFAULT_CONTEXT)
                    tags = tags_from_name(model_id)
                    if tags:
                        fields["tags"] = _json.dumps(tags)
                    policy = content_policy_from_name(model_id)
                    if policy:
                        fields["content_policy"] = policy
                    self.registry.upsert_auto(model_id, source="discovery", **fields)
                except Exception:
                    log.exception("failed to register discovered model %s", model_id)

    async def populate_context_lengths(self) -> None:
        """Fill context_length for local Ollama models from /api/show GGUF
        metadata — a direct API fact (no research/verification needed), and the
        data completeness that lets the context-fit gate in ranked_for_category
        actually fire for local models. Only probes models still missing the
        value; once set it persists (upsert_auto fills NULLs, never clobbers a
        manual override)."""
        for s in getattr(self.pool, "backends", {}).values():
            if not s.healthy or s.config.type != "ollama":
                continue
            probe = getattr(s.protocol, "show_context_length", None)
            if probe is None:
                continue
            for model_id in list(s.models):
                meta = self.registry.get(model_id)
                if meta and meta.get("context_length"):
                    continue  # already known
                try:
                    ctx = await probe(model_id)
                except Exception as e:  # a probe failure must never break sync
                    log.debug("context-length probe failed for %s: %s", model_id, e)
                    continue
                if ctx:
                    self.registry.upsert_auto(model_id, source="discovery",
                                              context_length=int(ctx))

    def _on_pool_change(self) -> None:
        """Pool health/model-list change -> immediate Tool Sync (§4.2 cadence:
        event-driven first, periodic sweep as the safety net)."""
        self.register_discovered()
        loop = asyncio.get_running_loop()
        loop.create_task(self.tool_registry.sync(self.pool))
        loop.create_task(self.populate_context_lengths())

    # -- live rebuilds used by the admin UI --------------------------------------

    async def rebuild_pool(self) -> None:
        old = self.pool
        self.pool = build_pool(self.config_store.config, self.http, self.db)
        self.pool.add_state_listener(self._on_pool_change)
        self.agent.pool = self.pool
        self.guardrails.pool_mode = self.config_store.config.backend_pool.mode
        await old.stop()
        if os.environ.get("FOUNDRY_DISABLE_BACKGROUND") != "1":
            await self.pool.start()
        self.register_discovered()
        await self.tool_registry.sync(self.pool)
        await self.populate_context_lengths()

    def rebuild_brain(self) -> None:
        self.brain = BrainClient(self.config_store.config.agent_brain, self.http,
                                 db=self.db)
        self.agent.brain = self.brain

    # -- background loops -----------------------------------------------------------

    async def start_background(self) -> None:
        await self.pool.start()
        self.pool.add_state_listener(self._on_pool_change)
        self.register_discovered()
        self._apply_seed()
        # One-shot cross-pass conflation reconcile — demotes identical-score
        # collisions across categories that accumulated before the cross-pass
        # guard existed (e.g. claude-opus-4-8 agentic==tool_calling). Idempotent.
        try:
            fixed = self.registry.reconcile_all_cross_category_collisions()
            if fixed:
                self.db.log_event("info", "registry",
                                  f"cross-pass conflation sweep demoted {fixed} row(s)")
        except Exception:
            log.exception("cross-pass conflation sweep failed")
        await self.tool_registry.sync(self.pool)
        await self.populate_context_lengths()
        self.research.start()
        self._bg_tasks = [
            asyncio.create_task(self._openrouter_loop()),
            asyncio.create_task(self._tool_sync_loop()),
            asyncio.create_task(self._quota_poll_loop()),
        ]

    def _apply_seed(self) -> None:
        """Reference research seed (registry redesign item 4): fills tags,
        good_for, and estimated scores for known model families so the
        registry is useful before any research runs. Never-clobber semantics
        live in reference_seed.py; idempotent, so calling per cycle is cheap."""
        try:
            from .registry.reference_seed import apply_reference_seed
            count = apply_reference_seed(self.registry)
            if count:
                self.db.log_event("info", "registry",
                                  f"reference seed applied to {count} models")
        except Exception:
            log.exception("reference seed application failed")

    async def _openrouter_loop(self) -> None:
        while True:
            try:
                await poll_openrouter(
                    self.db, self.registry, self.http,
                    poll_hours=self.config_store.config.registry.openrouter_poll_hours)
                self._apply_seed()  # newly-ingested models get seeded on arrival
            except Exception:
                log.exception("openrouter poll loop error")
            await asyncio.sleep(3600)  # due-ness is checked inside via kv timestamp

    async def _tool_sync_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config_store.config.tool_sync.periodic_seconds)
            try:
                await self.tool_registry.sync(self.pool)
            except Exception:
                log.exception("periodic tool sync failed")

    async def _quota_poll_loop(self) -> None:
        """Active oauth-staleness watch (Meridian's oauth quota source has
        silently gone null twice, quietly corrupting usage figures): poll the
        free read-only quota endpoint every few minutes so the flip raises an
        Events alert + UI banner the moment it happens. The alerting itself is
        edge-triggered inside MeridianUsage — this loop just guarantees a
        fetch happens even when no requests are flowing."""
        while True:
            interval = self.config_store.config.meridian.quota_poll_seconds
            if interval <= 0:
                await asyncio.sleep(60)  # disabled; re-check the knob later
                continue
            await asyncio.sleep(max(30, interval))
            try:
                for s in getattr(self.pool, "backends_of_type",
                                 lambda t: [])("anthropic-compatible"):
                    # snapshot's 30s cache is far shorter than any sane poll
                    # interval, so this always reaches the endpoint.
                    await self.meridian_usage.snapshot(s.config.url, s.config.api_key)
            except Exception:
                log.exception("quota poll loop error")

    async def shutdown(self) -> None:
        for t in self._bg_tasks:
            t.cancel()
        await self.research.stop()
        await self.pool.stop()
        await self.http.aclose()
        self.db.close()


def create_app(config_path: Optional[Path] = None,
               database_path: Optional[Path] = None) -> FastAPI:
    config_store = ConfigStore(config_path)
    cfg = config_store.load()
    logging.basicConfig(
        level=getattr(logging, cfg.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    db = Database(database_path or (data_dir() / "foundry.sqlite"))
    services = Services(config_store, db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if os.environ.get("FOUNDRY_DISABLE_BACKGROUND") != "1":
            await services.start_background()
        db.log_event("info", "main", "foundry-router started")
        yield
        await services.shutdown()

    app = FastAPI(title="Foundry Router", version=__version__,
                  docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.services = services
    app.include_router(facade_router)
    app.include_router(ui_router)
    return app
