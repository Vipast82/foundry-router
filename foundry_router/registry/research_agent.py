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
from ..errors import describe_exception
from .models_db import CONFLATION_DEMOTED_URL, ModelRegistry

log = logging.getLogger(__name__)

CATEGORIES = ["coding", "reasoning", "general_chat", "tool_calling", "agentic"]

# Named, real benchmarks recognized during research and stored SEPARATELY (as
# a tiebreaker source, not a ranking category). key (lowercased) -> (canonical
# name, router category, scale). scale: "percent" (0-100 pass rate — the only
# one that tiebreaks), "elo" (Chatbot Arena), "count" (AIME raw), "score"
# (MT-Bench 0-10) — the latter three are stored but flagged out of naive
# comparison. Ordered so a longest-substring match prefers the specific variant
# (e.g. "swe-bench verified" over "swe-bench", "humaneval+" over "humaneval").
KNOWN_BENCHMARKS = {
    "swe-bench verified": ("SWE-Bench Verified", "coding", "percent"),
    "swe-bench multilingual": ("SWE-Bench Multilingual", "coding", "percent"),
    "swe-bench pro": ("SWE-Bench Pro", "coding", "percent"),
    "swe-bench": ("SWE-Bench", "coding", "percent"),
    "terminal-bench": ("Terminal-Bench", "coding", "percent"),
    "livecodebench": ("LiveCodeBench", "coding", "percent"),
    "humaneval+": ("HumanEval+", "coding", "percent"),
    "humaneval": ("HumanEval", "coding", "percent"),
    "mbpp": ("MBPP", "coding", "percent"),
    "nl2repo": ("NL2Repo", "coding", "percent"),
    # ClawEval is a "real-user task distribution" agentic-coding benchmark;
    # pinned to `agentic` (broad task completion) rather than left ambiguous
    # between coding/agentic, which was repeatedly tripping the conflation guard.
    "claweval": ("ClawEval", "agentic", "percent"),
    "mmlu-pro": ("MMLU-Pro", "reasoning", "percent"),
    "mmlu": ("MMLU", "reasoning", "percent"),
    "gpqa diamond": ("GPQA Diamond", "reasoning", "percent"),
    "gpqa": ("GPQA", "reasoning", "percent"),
    "arc-agi": ("ARC-AGI", "reasoning", "percent"),
    "aime": ("AIME", "reasoning", "count"),
    "bfcl": ("BFCL", "tool_calling", "percent"),
    "chatbot arena": ("Chatbot Arena", "general_chat", "elo"),
    "lmsys arena": ("Chatbot Arena", "general_chat", "elo"),
    "mt-bench": ("MT-Bench", "general_chat", "score"),
}


def match_known_benchmark(name: Optional[str]) -> Optional[tuple[str, str, str]]:
    """Map an extractor-supplied benchmark name to (canonical, category, scale)
    via longest-substring match, or None if unrecognized (or not even a
    string — the extraction LLM's JSON can hand us anything)."""
    if not isinstance(name, str):
        return None
    norm = " ".join(name.lower().split())
    if not norm:
        return None
    best_key = None
    for key in KNOWN_BENCHMARKS:
        if key in norm and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return KNOWN_BENCHMARKS[best_key] if best_key else None

# Research is a background/on-demand task with no user waiting, so its brain
# call can afford more patience than an interactive request: a cold-loading
# local brain (or a momentary backend hiccup) throws a transient ReadTimeout
# that a retry clears — previously this failed the whole pass and needed manual
# requeuing.
LLM_RETRY_ATTEMPTS = 3
LLM_RETRY_BACKOFF_SECONDS = 8

# Category-TARGETED query variations (deeper research, 2026-07-14): one broad
# search rarely surfaces grounding for all five categories at once, so the
# extractor synthesizes for the uncovered ones. These aim searches at specific
# capabilities (coding / agentic-tool-use / reasoning) plus a general and a
# review query, widening the net of real per-category material before any
# guessing. Each is a paced search call, so more queries = more pacing time on
# a background sweep — acceptable for an unattended task.
_QUERIES = [
    "{name} benchmark results",
    "{name} coding SWE-bench HumanEval evaluation",
    "{name} agentic tool use function calling evaluation",
    "{name} reasoning MMLU GPQA math evaluation",
    "{name} capabilities review",
]
# Per-search snippet budget, trimmed from 4000 so five queries' snippets don't
# crowd fetched PAGES (the richer material) out of the corpus cap.
_SEARCH_SNIPPET_CHARS = 2500

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
  ],
  "named_benchmarks": [
    {{"name": "<one of the recognized names listed below>",
      "score": <the number EXACTLY as written in the source>,
      "source_url": "<url or empty string>"}}
  ]
}}

Rules: use "measured" ONLY when a real numeric benchmark result appears in the
text — and quote that number EXACTLY as written in the source. NEVER
recalculate, average, round, or paraphrase a number: if the source says 57.2,
the score is 57.2, not your own synthesis of it. Where no directly-stated
number exists, you may give an "estimated" score from the qualitative
discussion, with confidence <= 0.5. Lower confidence when only a single source
or vendor-published numbers exist.

CRITICAL — never assign the SAME numeric score to two different categories.
Each category's score must reflect evidence specific to THAT capability
(coding != reasoning != agentic != tool_calling != general_chat). If you have
only one number, or cannot tell two categories apart from the sources, report
the score for the single best-fitting category and OMIT the others entirely. An
omitted category (no data) is ALWAYS better than duplicating one guess across
categories — do not pad out the list to five entries.

For "named_benchmarks": include a row ONLY when one of these well-known
benchmark names appears in the text WITH a numeric result next to it, quoting
the number exactly — SWE-Bench Verified, SWE-Bench Pro, SWE-Bench Multilingual,
Terminal-Bench, HumanEval, HumanEval+, MBPP, LiveCodeBench, NL2Repo, ClawEval,
MMLU, MMLU-Pro, GPQA Diamond, AIME, ARC-AGI, BFCL, Chatbot Arena, MT-Bench.
Omit the array if none appear. Never invent a named-benchmark number.

If the text contains nothing useful, return {{"benchmarks": []}}."""


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
                # the bottom of its tier with nothing visibly wrong. Unwrap the
                # exception (TaskGroup/transport errors stringify uselessly)
                # so "why" is actually readable.
                detail = describe_exception(e)
                self.registry.set_research_status(model_id, "failed", detail)
                self.db.log_event("error", "research",
                                  f"research failed for {model_id}", detail)
            finally:
                self._queued.discard(model_id)

    async def _extract_with_retry(self, prompt: str) -> str:
        """Call the research LLM with retry-then-raise (item 1). A cold-loading
        brain throws a transient timeout the first attempt clears; failing the
        whole pass on it meant manual requeuing. Background task -> patience is
        cheap, no user waiting on a heartbeat."""
        last: Optional[BaseException] = None
        for attempt in range(1, LLM_RETRY_ATTEMPTS + 1):
            try:
                return await self.llm(prompt)
            except Exception as e:  # noqa: BLE001 — retry any transient failure
                last = e
                self.db.log_event(
                    "warning", "research",
                    f"brain extraction attempt {attempt}/{LLM_RETRY_ATTEMPTS} failed",
                    describe_exception(e))
                if attempt < LLM_RETRY_ATTEMPTS:
                    await asyncio.sleep(LLM_RETRY_BACKOFF_SECONDS * attempt)
        raise last if last else RuntimeError("extraction failed")

    # -- the actual research pass ---------------------------------------------------

    async def _search(self, query: str) -> str:
        ref = self.cfg.search
        if not ref.server or not ref.tool:
            raise RuntimeError("no search MCP tool configured")
        # SearXNG pacing + 429-backoff now live in MCPManager, so they cover
        # this sweep AND the worker/brain tool paths uniformly (set them on the
        # search server's MCP config: pace_seconds / rate_limit_*).
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
                corpus.append(f"### search: {q.format(name=model_id)}\n"
                              f"{result[:_SEARCH_SNIPPET_CHARS]}")
                urls += re.findall(r"https?://[^\s\"'<>\)\]]+", result)
            except Exception as e:
                # Unwrap the real cause — TaskGroup/SSE/transport failures (e.g.
                # searxng dropping mid-sweep and taking every concurrent task
                # with it) otherwise stringify as the useless "unhandled errors
                # in a TaskGroup (1 sub-exception)" (item 2).
                detail = describe_exception(e)
                self.db.log_event("warning", "research",
                                  f"search unavailable for {model_id} — skipping "
                                  f"qualitative pass", detail)
                # No search => nothing qualitative to do. Record the failure on
                # the model row (UI-visible) — set_research_status also bumps
                # last_updated so the sweep doesn't hammer an unconfigured
                # setup every cycle.
                self.registry.set_research_status(
                    model_id, "failed", f"search MCP tool unreachable: {detail}")
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

        text = "\n\n".join(corpus)[:self.cfg.corpus_char_limit]
        raw = await self._extract_with_retry(
            EXTRACTION_PROMPT.format(model=model_id, text=text))
        data = extract_json(raw)
        if not data:
            self.registry.set_research_status(
                model_id, "failed", "extraction produced no parseable JSON")
            self.db.log_event("warning", "research",
                              f"extraction produced no JSON for {model_id}", raw[:500])
            return

        wrote = self._write_extraction(model_id, data, text)
        # Cross-pass conflation reconcile: catch a fresh score that matches a
        # DIFFERENT category's score written on an earlier run (the in-pass guard
        # only sees this pass's own output).
        self.registry.reconcile_cross_category_collisions(model_id)
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

        def _nonblank(v):
            # A web-research pass on an obscure/alias name often returns an empty
            # or whitespace good_for/reasoning_style. Treat those as "no data"
            # (-> None, which upsert_auto filters) so they don't OVERWRITE a
            # richer seed value with a blank — the reference seed then supplements
            # the gap instead.
            v = v.strip() if isinstance(v, str) else v
            return v or None

        self.registry.upsert_auto(
            model_id, source="research_agent",
            reasoning_style=_nonblank(data.get("reasoning_style")),
            good_for=_nonblank(data.get("good_for")),
            tags=tags_json,
            benefits_from_explicit_prompting=(
                1 if data.get("benefits_from_explicit_prompting") else 0),
        )
        # Named benchmarks (real, verifiable) — processed FIRST and
        # independently of the generic-score path: they carry their own verbatim
        # check, and must be captured even when the insufficient-source gate
        # below skips synthesized generic scores (the motivating case: a niche
        # model whose only real data IS a named SWE-Bench number).
        named_wrote = 0
        named_raw = data.get("named_benchmarks")
        for nb in named_raw if isinstance(named_raw, list) else []:
            if not isinstance(nb, dict):
                continue
            matched = match_known_benchmark(nb.get("name"))
            if matched is None:
                continue
            canonical, category, scale = matched
            try:
                score = float(nb.get("score"))
            except (TypeError, ValueError):
                continue
            # same discipline as generic scores: trust it only if the number
            # actually appears in the fetched sources.
            if not measured_score_in_text(score, text):
                continue
            self.registry.upsert_named_benchmark(
                model_id, canonical, category, max(0.0, score), scale,
                source_url=str(nb.get("source_url") or "")[:500],
                measured_date=(str(nb.get("measured_date"))[:32]
                               if nb.get("measured_date") else None))
            named_wrote += 1
        if named_wrote:
            self.db.log_event("info", "research",
                              f"{model_id}: recorded {named_wrote} named benchmark(s)")

        _bench = data.get("benchmarks")
        # Only dict entries survive — the extraction LLM's JSON can hand us a
        # bare string or a nested list where an object was asked for.
        raw_benchmarks = [b for b in (_bench if isinstance(_bench, list) else [])
                          if isinstance(b, dict)]
        # Insufficient-source gate (item 3): a low-web-presence model (e.g.
        # ornith:35b, "3 pages, 0 real benchmarks") has no per-category numbers
        # to extract, so the LLM fabricates — and that just re-feeds the
        # conflation guard every pass, leaving the model perpetually
        # "demoted/flagged/needs manual reset". If NOT ONE extracted score
        # appears verbatim in the fetched sources, the whole set is synthesized:
        # record NO benchmark scores (qualitative fields are still saved).
        # Absent rows rank gracefully (bottom of tier, §4.4), and a later pass
        # that finds real sources can populate them — an honest "no data" state
        # instead of a fabricated one. A single grounded number means the model
        # has real coverage, so estimates for other categories are kept.
        valid_scores = []
        for b in raw_benchmarks:
            try:
                if b.get("category") in CATEGORIES:
                    valid_scores.append(float(b.get("score")))
            except (TypeError, ValueError):
                continue
        if valid_scores and not any(measured_score_in_text(s, text) for s in valid_scores):
            self.db.log_event(
                "info", "research",
                f"no benchmark numbers for {model_id} found verbatim across "
                f"{len(text)} chars of sources — recorded qualitative fields only, "
                f"skipped {len(valid_scores)} synthesized score(s)")
            return 0
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
                source_url = str(b.get("source_url") or "")[:500]
                if round(score, 1) in duplicated:
                    # One number across multiple categories — strip its
                    # unwarranted trust rather than believe the conflation. Stamp
                    # the demotion marker so the cross-pass reconciliation leaves
                    # it alone (idempotent).
                    self.db.log_event(
                        "warning", "research",
                        f"demoted {model_id}/{cat} score {score} — the same number "
                        f"was assigned to multiple categories this pass (extractor "
                        f"synthesis, not a real per-category benchmark)")
                    score_type, source_type, confidence_scale = \
                        "estimated", "community_report", 0.4
                    source_url = CONFLATION_DEMOTED_URL
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
                    source_url=source_url,
                    confidence=max(0.0, min(1.0, float(b.get("confidence", 0.4))
                                            * confidence_scale)),
                )
                wrote += 1
            except (TypeError, ValueError):
                continue
        return wrote
