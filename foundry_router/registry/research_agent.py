"""Research Agent (design doc §4.4): the *background* agent that keeps model
metadata current. Distinct from the live Routing Agent and entirely off the
hot path — `request_model_research` enqueues and returns immediately; the live
request proceeds with a conservative default.

Process per model: search (SearXNG MCP tool) -> fetch top results (Crawl4AI
MCP tool) -> ask an LLM to extract benchmark scores / qualitative estimates ->
write structured rows to model_benchmarks + summary fields on models.

Degradation: with no MCP servers configured (or unreachable) the qualitative
pass is skipped with a logged note — OpenRouter ingestion still keeps the
structured half of the registry alive, honoring §2's "no cloud dependency for
core function".
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Awaitable, Callable, Optional

from ..config import ResearchConfig
from ..db import Database, utcnow
from .models_db import ModelRegistry

log = logging.getLogger(__name__)

CATEGORIES = ["coding", "reasoning", "general_chat", "tool_calling", "agentic"]

# Same benchmark categories used throughout the main guide, for consistency.
_QUERIES = ["{name} benchmarks", "{name} SWE-bench", "{name} review"]

EXTRACTION_PROMPT = """You are extracting model-capability data from web research text.

Model being researched: {model}

Research text (search results and fetched pages, may be noisy):
---
{text}
---

Return ONLY a JSON object, no prose, with this exact shape:
{{
  "reasoning_style": "<one-sentence summary of how this model reasons>",
  "good_for": "<comma-separated task types this model is reportedly good at>",
  "benefits_from_explicit_prompting": true/false,
  "tags": ["<subset of: coding, vision, tool-calling, reasoning, creative-writing, long-context — only tags the text supports>"],
  "benchmarks": [
    {{"category": "coding|reasoning|general_chat|tool_calling|agentic",
      "score": <0-100 number>,
      "score_type": "measured" or "estimated",
      "source_type": "vendor" or "independent" or "community_report",
      "source_url": "<url or empty string>",
      "confidence": <0.0-1.0>}}
  ]
}}

Rules: use "measured" ONLY when a real numeric benchmark result appears in the
text — and quote that number EXACTLY as written in the source. NEVER
recalculate, average, round, or paraphrase a number: if the source says 57.2,
the score is 57.2, not your own synthesis of it. Where no directly-stated
number exists, you may give an "estimated" score from the qualitative
discussion, with confidence <= 0.5. Lower confidence when only a single source
or vendor-published numbers exist. If the text contains nothing useful, return
{{"benchmarks": []}}."""


def measured_score_in_text(score: float, corpus: str) -> bool:
    """Does this exact number literally appear in the fetched material?
    Guards against LLM synthesis errors (confirmed live: a research run cited
    coding 45.3 while every fetched source said 57.2) — a 'measured' score
    that can't be found verbatim gets downgraded to an estimate."""
    candidates = {f"{score:g}", f"{score:.1f}", f"{score:.2f}"}
    if float(score).is_integer():
        candidates.add(str(int(score)))
    return any(c in corpus for c in candidates)


def extract_json(text: str) -> Optional[dict]:
    """Find the first balanced JSON object in LLM output (models love to wrap
    JSON in prose or code fences no matter how firmly asked not to)."""
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


class ResearchAgent:
    def __init__(self, cfg: ResearchConfig, db: Database, registry: ModelRegistry,
                 mcp_manager, llm: Callable[[str], Awaitable[str]],
                 available_models: Callable[[], list[str]]):
        """`llm` is an async prompt->text callable wired up in main.py — by
        default the same brain model, or registry.research.model if set (§7
        open question: a larger off-hot-path model may extract better)."""
        self.cfg = cfg
        self.db = db
        self.registry = registry
        self.mcp = mcp_manager
        self.llm = llm
        self.available_models = available_models
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._tasks: list[asyncio.Task] = []

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        if not self.cfg.enabled:
            return
        self._tasks = [
            asyncio.create_task(self._worker_loop()),
            asyncio.create_task(self._sweep_loop()),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass

    # -- triggers (§4.4: either is sufficient, both exist) -----------------------------

    def prerequisites(self) -> tuple[bool, str]:
        """Can a research job actually succeed right now? The UI gates the
        Research button on this instead of always reporting success — the
        button must never claim an outcome that depends on tools that aren't
        there (SearXNG/Crawl4AI MCP servers)."""
        if not self.cfg.enabled:
            return False, "research agent disabled in config (registry.research.enabled)"
        s, f = self.cfg.search, self.cfg.fetch
        if not s.server or not s.tool:
            return False, "no search MCP tool configured (registry.research.search)"
        if s.server not in self.mcp.servers:
            return False, (f"MCP server '{s.server}' is not connected — add it on the "
                           f"MCP tab (requires your SearXNG MCP server)")
        if f.server and f.server not in self.mcp.servers:
            return False, (f"MCP server '{f.server}' is not connected — add it on the "
                           f"MCP tab (requires your Crawl4AI MCP server)")
        return True, "ok"

    def enqueue(self, model_id: str) -> bool:
        """Non-blocking trigger used by the live agent's request_model_research
        tool and by the web UI. Dedupes while queued."""
        if not self.cfg.enabled or model_id in self._queued:
            return False
        self._queued.add(model_id)
        self.registry.set_research_status(model_id, "queued")
        self.queue.put_nowait(model_id)
        return True

    async def _sweep_loop(self) -> None:
        # DESIGN DECISION (see design doc §7): sweep cadence. Daily default,
        # configurable via registry.research.sweep_hours — balance freshness
        # against SearXNG/Crawl4AI load shared with the rest of the MCP fleet.
        while True:
            try:
                stale = self.registry.stale_or_missing(
                    self.available_models(), self.cfg.stale_days)
                for m in stale:
                    self.enqueue(m)
                if stale:
                    self.db.log_event("info", "research",
                                      f"sweep queued {len(stale)} stale/missing models")
            except Exception:
                log.exception("research sweep failed")
            await asyncio.sleep(self.cfg.sweep_hours * 3600)

    async def _worker_loop(self) -> None:
        while True:
            model_id = await self.queue.get()
            try:
                self.registry.set_research_status(model_id, "running")
                await self.research_model(model_id)
            except Exception as e:
                # Failure is recorded ON THE MODEL ROW, not just the event log
                # — the UI shows it, so a silently-unscored model can't sink to
                # the bottom of its tier with nothing visibly wrong.
                self.registry.set_research_status(model_id, "failed", str(e))
                self.db.log_event("error", "research",
                                  f"research failed for {model_id}", str(e))
            finally:
                self._queued.discard(model_id)

    # -- the actual research pass ---------------------------------------------------

    async def _search(self, query: str) -> str:
        ref = self.cfg.search
        if not ref.server or not ref.tool:
            raise RuntimeError("no search MCP tool configured")
        return await self.mcp.call_tool(ref.server, ref.tool, {ref.query_param: query})

    async def _fetch(self, url: str) -> str:
        ref = self.cfg.fetch
        if not ref.server or not ref.tool:
            raise RuntimeError("no fetch MCP tool configured")
        return await self.mcp.call_tool(ref.server, ref.tool, {ref.url_param: url})

    async def research_model(self, model_id: str) -> None:
        corpus: list[str] = []
        urls: list[str] = []
        for q in _QUERIES:
            try:
                result = await self._search(q.format(name=model_id))
                corpus.append(f"### search: {q.format(name=model_id)}\n{result[:4000]}")
                urls += re.findall(r"https?://[^\s\"'<>\)\]]+", result)
            except Exception as e:
                self.db.log_event("warning", "research",
                                  f"search unavailable for {model_id} — skipping "
                                  f"qualitative pass", str(e))
                # No search => nothing qualitative to do. Record the failure on
                # the model row (UI-visible) — set_research_status also bumps
                # last_updated so the sweep doesn't hammer an unconfigured
                # setup every cycle.
                self.registry.set_research_status(
                    model_id, "failed", f"search MCP tool unreachable: {e}")
                return

        seen: set[str] = set()
        fetched = 0
        for u in urls:
            if fetched >= self.cfg.max_pages_per_model:
                break
            base = u.split("#")[0]
            if base in seen:
                continue
            seen.add(base)
            try:
                page = await self._fetch(base)
                corpus.append(f"### page: {base}\n{page[:6000]}")
                fetched += 1
            except Exception:
                continue

        text = "\n\n".join(corpus)[:24000]
        raw = await self.llm(EXTRACTION_PROMPT.format(model=model_id, text=text))
        data = extract_json(raw)
        if not data:
            self.registry.set_research_status(
                model_id, "failed", "extraction produced no parseable JSON")
            self.db.log_event("warning", "research",
                              f"extraction produced no JSON for {model_id}", raw[:500])
            return

        wrote = self._write_extraction(model_id, data, text)
        self.registry.set_research_status(
            model_id, "ok", f"{wrote} benchmark rows, {fetched} pages")
        self.db.log_event("info", "research",
                          f"researched {model_id}: {wrote} benchmark rows at {utcnow()}")

    def _write_extraction(self, model_id: str, data: dict, text: str) -> int:
        """Persist an extraction: qualitative fields + benchmark rows. Split out
        from research_model so the integrity guards are unit-testable without a
        live search/fetch/LLM round trip. Returns benchmark rows written."""
        tags = data.get("tags")
        tags_json = None
        if isinstance(tags, list) and tags:
            tags_json = json.dumps([str(t)[:24] for t in tags][:8])
        self.registry.upsert_auto(
            model_id, source="research_agent",
            reasoning_style=data.get("reasoning_style"),
            good_for=data.get("good_for"),
            tags=tags_json,
            benefits_from_explicit_prompting=(
                1 if data.get("benefits_from_explicit_prompting") else 0),
        )
        raw_benchmarks = data.get("benchmarks") or []
        # Cross-category synthesis guard (found live: ornith:35b's `agentic` row
        # held its `reasoning` score, 27.8, both stamped vendor/0.95). The same
        # score assigned to two or more DISTINCT categories in one pass is
        # almost never real — coding, reasoning, agentic, etc. are separate
        # evals that essentially never land on the identical number. It's the
        # extractor reusing one figure it found, and (worse) self-reporting it
        # as high-confidence vendor data. Such rows are demoted to low-
        # confidence estimates so a conflated number can't sit in the registry
        # looking trustworthy — a real later pass can still replace them.
        cats_by_score: dict = {}
        for b in raw_benchmarks:
            try:
                c, s = b.get("category"), round(float(b.get("score")), 1)
            except (TypeError, ValueError):
                continue
            if c in CATEGORIES:
                cats_by_score.setdefault(s, set()).add(c)
        duplicated = {s for s, cats in cats_by_score.items() if len(cats) >= 2}

        wrote = 0
        for b in raw_benchmarks:
            try:
                cat = b.get("category")
                if cat not in CATEGORIES:
                    continue
                score = float(b.get("score"))
                score_type = "measured" if b.get("score_type") == "measured" else "estimated"
                source_type = (b.get("source_type")
                               if b.get("source_type") in ("vendor", "independent",
                                                           "community_report")
                               else "community_report")
                confidence_scale = 1.0
                if round(score, 1) in duplicated:
                    # One number across multiple categories — strip its
                    # unwarranted trust rather than believe the conflation.
                    self.db.log_event(
                        "warning", "research",
                        f"demoted {model_id}/{cat} score {score} — the same number "
                        f"was assigned to multiple categories this pass (extractor "
                        f"synthesis, not a real per-category benchmark)")
                    score_type, source_type, confidence_scale = \
                        "estimated", "community_report", 0.4
                elif score_type == "measured" and not measured_score_in_text(score, text):
                    # The model claims "measured" but the number is nowhere in
                    # the fetched material — synthesis, not quotation.
                    self.db.log_event(
                        "warning", "research",
                        f"downgraded {model_id}/{cat} score {score} to estimated — "
                        f"number not found verbatim in sources")
                    score_type, confidence_scale = "estimated", 0.6
                self.registry.upsert_benchmark(
                    model_id, cat, max(0.0, min(100.0, score)),
                    score_type=score_type,
                    source_type=source_type,
                    source_url=str(b.get("source_url") or "")[:500],
                    confidence=max(0.0, min(1.0, float(b.get("confidence", 0.4))
                                            * confidence_scale)),
                )
                wrote += 1
            except (TypeError, ValueError):
                continue
        return wrote
