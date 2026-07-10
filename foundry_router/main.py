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
        name), so ranking/ingest/research all have somewhere to attach."""
        for s in getattr(self.pool, "backends", {}).values():
            if not s.healthy:
                continue
            for model_id in s.models:
                try:
                    self.registry.upsert_auto(model_id, source="discovery",
                                              provider=s.config.name,
                                              display_name=model_id)
                except Exception:
                    log.exception("failed to register discovered model %s", model_id)

    def _on_pool_change(self) -> None:
        """Pool health/model-list change -> immediate Tool Sync (§4.2 cadence:
        event-driven first, periodic sweep as the safety net)."""
        self.register_discovered()
        asyncio.get_running_loop().create_task(self.tool_registry.sync(self.pool))

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

    def rebuild_brain(self) -> None:
        self.brain = BrainClient(self.config_store.config.agent_brain, self.http,
                                 db=self.db)
        self.agent.brain = self.brain

    # -- background loops -----------------------------------------------------------

    async def start_background(self) -> None:
        await self.pool.start()
        self.pool.add_state_listener(self._on_pool_change)
        self.register_discovered()
        await self.tool_registry.sync(self.pool)
        self.research.start()
        self._bg_tasks = [
            asyncio.create_task(self._openrouter_loop()),
            asyncio.create_task(self._tool_sync_loop()),
        ]

    async def _openrouter_loop(self) -> None:
        while True:
            try:
                await poll_openrouter(
                    self.db, self.registry, self.http,
                    poll_hours=self.config_store.config.registry.openrouter_poll_hours)
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

    app = FastAPI(title="Foundry Router", version="0.1.0",
                  docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.services = services
    app.include_router(facade_router)
    app.include_router(ui_router)
    return app
