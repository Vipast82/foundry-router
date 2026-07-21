"""Eval harness for persona changes (quality spec Phase 4).

A fixed, editable set of representative prompts per category runs through the
persona's REAL routing path (same event stream the facade serves), each
response scored two ways:

  - shape checks — cheap, deterministic pass/fail on response SHAPE (code
    block present, source cited, valid JSON, no refusal, length bounds),
    never exact-match;
  - LLM-as-judge (optional) — a judge_model rates 0-10, selected the same
    way as the Phase 2 review model (any reachable model, local or paid;
    paid judges pass the guardrail ladder). Judge failures degrade to
    shape-only scoring, never a failed run.

Scores land in eval_runs/eval_results so per-persona trend lines accumulate
over time. Operator convention (docs/CONVENTIONS.md): run the harness after
any persona/prompt change and report the delta with that change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Callable, Optional

from .db import Database, utcnow

log = logging.getLogger(__name__)

PER_PROMPT_TIMEOUT = 420  # seconds — cold loads + multi-step routing are slow

# Persona benchmark_category -> default eval-prompt category. RAG personas
# share benchmark_category general_chat, so name wins over category for them;
# an explicit categories list on the run API overrides everything.
_CATEGORY_MAP = {"coding": "coding", "agentic": "research"}


def default_categories(persona: dict) -> list[str]:
    if "rag" in (persona.get("virtual_name") or "").lower():
        return ["rag"]
    return [_CATEGORY_MAP.get(persona.get("benchmark_category") or "", "chat")]


# --------------------------------------------------------------------------- #
# Shape checks — deterministic, never exact-match                             #
# --------------------------------------------------------------------------- #

_CODE_BLOCK_RE = re.compile(r"```.+?```", re.DOTALL)
_URL_RE = re.compile(r"https?://\S+")


def _check_valid_json(text: str) -> bool:
    from .registry.research_agent import extract_json
    return extract_json(text) is not None


def _looks_like_refusal(text: str) -> bool:
    from .brain.agent import _looks_like_refusal as f
    return f(text)


def run_shape_checks(checks: list[str], response: str) -> tuple[bool, str]:
    """(all_passed, human-readable per-check detail). Unknown check names are
    reported and skipped rather than failing the prompt — an editable check
    list must not brick a run on a typo."""
    results = []
    passed = True
    for raw in checks or []:
        name, _, arg = str(raw).partition(":")
        name = name.strip()
        ok: Optional[bool]
        if name == "code_block":
            ok = bool(_CODE_BLOCK_RE.search(response))
        elif name == "cites_source":
            ok = bool(_URL_RE.search(response))
        elif name == "valid_json":
            ok = _check_valid_json(response)
        elif name == "no_refusal":
            ok = bool(response.strip()) and not _looks_like_refusal(response)
        elif name == "min_length":
            ok = len(response) >= int(arg or 0)
        elif name == "max_length":
            ok = len(response) <= int(arg or 10 ** 9)
        elif name == "mentions":
            ok = arg.strip().lower() in response.lower()
        else:
            results.append(f"?{raw} (unknown check, skipped)")
            continue
        passed = passed and ok
        results.append(f"{'✓' if ok else '✗'} {raw}")
    return passed, "; ".join(results) or "(no checks)"


# --------------------------------------------------------------------------- #
# Seed prompts — a starting point, freely editable in the GUI                 #
# --------------------------------------------------------------------------- #

SEED_PROMPTS = [
    ("coding", "Write a Python function that returns the nth Fibonacci number. "
               "Include a docstring and one usage example.",
     ["code_block", "no_refusal", "min_length:120"]),
    ("coding", "This function has a bug: def add(a, b): return a - b — "
               "explain the bug in one sentence and give the corrected code.",
     ["code_block", "no_refusal"]),
    ("chat", "Explain the difference between RAM and VRAM in plain language "
             "for a non-technical user.",
     ["no_refusal", "min_length:200", "max_length:6000"]),
    ("chat", "Summarize the plot of Romeo and Juliet in exactly three sentences.",
     ["no_refusal", "max_length:2000"]),
    ("chat", "Reply with ONLY a valid JSON object of the form "
             "{\"mission\": string, \"year\": number} describing the first "
             "crewed Moon landing.",
     ["valid_json", "no_refusal"]),
    ("research", "What is the current stable version of Python? Cite the "
                 "source URL you used.",
     ["cites_source", "no_refusal"]),
    ("research", "In what year was SQLite first released? Answer with the "
                 "year and a source URL.",
     ["cites_source", "no_refusal"]),
    ("rag", "Answer strictly from this context: 'The Foundry warehouse "
            "opened in 1912 in Sheffield.' When did the Foundry warehouse "
            "open, and where?",
     ["mentions:1912", "mentions:sheffield", "no_refusal"]),
    ("rag", "Using only this context: 'Model X has 7B parameters and a 32K "
            "context window.' What is Model X's context window?",
     ["mentions:32k", "no_refusal"]),
]


def ensure_seed(db: Database) -> None:
    """Seed once (kv-flagged), never re-run — the set is the operator's to
    edit/extend afterward, same never-clobber rule as persona seeding."""
    if db.kv_get("eval_prompts_seed_v1"):
        return
    now = utcnow()
    for category, prompt, checks in SEED_PROMPTS:
        db.execute(
            "INSERT INTO eval_prompts (category, prompt, checks, enabled, "
            "created_at) VALUES (?,?,?,1,?)",
            (category, prompt, json.dumps(checks), now))
    db.kv_set("eval_prompts_seed_v1", now)
    db.log_event("info", "eval", f"seeded {len(SEED_PROMPTS)} eval prompts")


# --------------------------------------------------------------------------- #
# Judge prompt                                                                #
# --------------------------------------------------------------------------- #

EVAL_JUDGE_PROMPT = """You are scoring how well a response serves a test \
prompt. Judge correctness, completeness, and clarity — not verbosity or style. \
Reply ONLY with JSON: {{"score": <0-10>, "notes": "<one sentence>"}}. \
10 = excellent and fully correct; 5 = usable with real gaps; 0 = wrong, \
empty, or a refusal.

TEST PROMPT:
{prompt}

RESPONSE TO SCORE:
{response}"""


# --------------------------------------------------------------------------- #
# Runner                                                                      #
# --------------------------------------------------------------------------- #

class EvalHarness:
    """Runs eval prompts through the persona's real routing path. `services`
    is the composition root (needs .db, .agent, .personas, .guardrails);
    `answer_fn` is injectable for tests — production uses the same event
    source the facade streams from."""

    def __init__(self, services, answer_fn: Optional[Callable] = None):
        self.svc = services
        self.db: Database = services.db
        self._answer_fn = answer_fn

    async def _answer_for(self, persona: dict, prompt: str) -> str:
        if self._answer_fn is not None:
            return await self._answer_fn(persona, prompt)
        from .brain.agent import RequestContext
        from .facade.ollama_api import _run_events
        from .guardrails import RequestGuardState
        from .usage import RequestLogger
        mode = "pipeline" if (persona.get("execution_mode") or "") == "pipeline" \
            else "agent"
        ctx = RequestContext(
            persona=persona,
            messages=[{"role": "user", "content": prompt}],
            guard=RequestGuardState(),
            logger=RequestLogger(self.db, persona["virtual_name"],
                                 f"eval:{persona['virtual_name']}", mode, prompt))
        answers = []
        try:
            async for ev in _run_events(self.svc, ctx):
                if ev.kind in ("answer", "ask_user"):
                    answers.append(ev.text)
                elif ev.kind in ("error", "brain_down"):
                    answers.append("")
        finally:
            ctx.logger.finish("ok")
        return "\n\n".join(a for a in answers if a).strip()

    async def _judge(self, judge_model: str, prompt: str,
                     response: str) -> tuple[Optional[float], str]:
        """(score 0-10 or None, notes). None = judge unavailable/denied/
        unparseable — shape checks still stand on their own."""
        from .registry.research_agent import extract_json
        info = self.svc.pool.backend_info(judge_model)
        if info is None:
            return None, "judge model unreachable"
        from .guardrails import RequestGuardState
        verdict = await self.svc.guardrails.check_paid_call(
            judge_model, info, self.svc.registry.get(judge_model),
            RequestGuardState(), self.svc.guardrails.effective(None))
        if not verdict.allowed:
            return None, f"judge denied by guardrail: {verdict.reason}"
        try:
            result, _ = await self.svc.agent._dispatch_worker(
                judge_model,
                EVAL_JUDGE_PROMPT.format(prompt=prompt[:2000],
                                         response=response[:8000]),
                max_tokens=512)
        except Exception as e:
            return None, f"judge call failed: {e}"
        data = extract_json(result.content) or {}
        try:
            score = max(0.0, min(10.0, float(data["score"])))
        except (KeyError, TypeError, ValueError):
            return None, "judge returned no parseable score"
        return score, str(data.get("notes") or "")[:300]

    async def run(self, persona_name: str, judge_model: str = "",
                  categories: Optional[list[str]] = None) -> int:
        """Execute one full run synchronously (callers may background it).
        Returns the eval_runs row id; the row goes running -> done/failed and
        results append as they finish, so the GUI can watch progress."""
        persona = self.svc.personas.get(persona_name)
        if persona is None:
            raise ValueError(f"no persona named {persona_name!r}")
        cats = categories or default_categories(persona)
        prompts = self.db.query(
            f"SELECT * FROM eval_prompts WHERE enabled=1 AND category IN "
            f"({','.join('?' * len(cats))}) ORDER BY id", cats)
        run_id = self.db.execute(
            "INSERT INTO eval_runs (ts, persona, judge_model, prompts_run, "
            "status, note) VALUES (?,?,?,?,?,?)",
            (utcnow(), persona["virtual_name"], judge_model, len(prompts),
             "running", f"categories: {', '.join(cats)}"))
        try:
            shape_passes, judge_scores = 0, []
            for p in prompts:
                t0 = time.monotonic()
                try:
                    response = await asyncio.wait_for(
                        self._answer_for(persona, p["prompt"]),
                        timeout=PER_PROMPT_TIMEOUT)
                except asyncio.TimeoutError:
                    response = ""
                except Exception as e:
                    log.exception("eval prompt failed")
                    response = ""
                    self.db.log_event("warning", "eval",
                                      f"prompt {p['id']} errored", str(e))
                try:
                    checks = json.loads(p["checks"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    checks = []
                passed, detail = run_shape_checks(checks, response)
                shape_passes += int(passed)
                score, notes = (None, "")
                if judge_model and response.strip():
                    score, notes = await self._judge(judge_model, p["prompt"],
                                                     response)
                    if score is not None:
                        judge_scores.append(score)
                self.db.execute(
                    "INSERT INTO eval_results (run_id, prompt_id, prompt, "
                    "response_chars, shape_pass, shape_detail, judge_score, "
                    "judge_notes, duration_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                    (run_id, p["id"], p["prompt"][:300], len(response),
                     int(passed), detail[:800], score, notes,
                     int((time.monotonic() - t0) * 1000)))
            self.db.execute(
                "UPDATE eval_runs SET status='done', shape_pass_rate=?, "
                "avg_judge_score=? WHERE id=?",
                (shape_passes / len(prompts) if prompts else None,
                 (sum(judge_scores) / len(judge_scores)) if judge_scores else None,
                 run_id))
        except Exception as e:
            log.exception("eval run failed")
            self.db.execute(
                "UPDATE eval_runs SET status='failed', note=? WHERE id=?",
                (str(e)[:300], run_id))
        return run_id

    # -- reporting -------------------------------------------------------------

    def runs(self, persona: Optional[str] = None, limit: int = 50) -> list[dict]:
        """Run history, newest first, each row annotated with the score DELTA
        vs the previous completed run for the same persona — the number the
        operator convention says to report with a persona change."""
        sql = "SELECT * FROM eval_runs"
        params: list = []
        if persona:
            sql += " WHERE persona=?"
            params.append(persona)
        rows = self.db.query(sql + " ORDER BY id DESC LIMIT ?",
                             params + [min(limit, 200)])
        by_persona: dict[str, list[dict]] = {}
        for r in reversed(rows):        # oldest first per persona
            by_persona.setdefault(r["persona"], []).append(r)
        for series in by_persona.values():
            prev = None
            for r in series:
                r["shape_delta"] = r["judge_delta"] = None
                if prev and r["status"] == "done":
                    if r["shape_pass_rate"] is not None \
                            and prev.get("shape_pass_rate") is not None:
                        r["shape_delta"] = round(
                            r["shape_pass_rate"] - prev["shape_pass_rate"], 3)
                    if r["avg_judge_score"] is not None \
                            and prev.get("avg_judge_score") is not None:
                        r["judge_delta"] = round(
                            r["avg_judge_score"] - prev["avg_judge_score"], 2)
                if r["status"] == "done":
                    prev = r
        return rows

    def results(self, run_id: int) -> list[dict]:
        return self.db.query(
            "SELECT * FROM eval_results WHERE run_id=? ORDER BY id", (run_id,))
