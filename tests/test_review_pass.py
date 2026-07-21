"""Tiered review pass (quality spec Phase 2): per-persona, default OFF,
review model explicitly selected (local or paid — paid passes the guardrail
ladder), free brain pre-filter, visible correction marker, every event logged
to review_log, and fail-open on any reviewer failure."""

import json

from foundry_router.brain.agent import AgentRunner, RequestContext
from foundry_router.config import AgentBrainConfig, GuardrailsConfig, MeridianConfig
from foundry_router.db import Database
from foundry_router.guardrails import GuardrailEngine, RequestGuardState
from foundry_router.pool.protocols import ChatResult
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.tools.sync import _ASK_PARAMS, ToolDef, ToolRegistry
from foundry_router.usage import MeridianUsage, RequestLogger

ANSWER = "The capital of Australia is Sydney."
CORRECTED = "The capital of Australia is Canberra."


class Pool:
    def __init__(self, responses, backend_types=None):
        self.responses = responses
        self.types = backend_types or {}
        self.calls = []

    def available_models(self):
        return {m: ["b"] for m in self.responses}

    def backend_info(self, m):
        if m not in self.responses:
            return None
        return {"name": "b", "type": self.types.get(m, "ollama"),
                "url": "http://x", "api_key": "k"}

    async def chat(self, model, messages, tools=None, options=None, max_tokens=4096):
        self.calls.append(model)
        return ChatResult(content=self.responses[model]), "b"


class ScriptedBrain:
    def __init__(self, responses, prefilter_reply=""):
        self.responses = list(responses)
        self.prefilter_reply = prefilter_reply
        self.complete_calls = 0
        self.cfg = AgentBrainConfig()

    async def chat(self, messages, tools=None, **kwargs):
        return self.responses.pop(0)

    async def complete(self, prompt):
        self.complete_calls += 1
        return self.prefilter_reply


class FixedUsage(MeridianUsage):
    def __init__(self, cfg, db, worst_used=None):
        super().__init__(cfg, client=None, db=db)
        self._worst = worst_used

    async def snapshot(self, base_url, api_key=None):
        remaining = 1.0 - (self._worst or 0)
        return {"available": self._worst is None
                             or remaining >= self.cfg.min_window_fraction,
                "buckets": [], "worst_used": self._worst, "fable_used": None,
                "note": f"{self._worst:.0%} used" if self._worst is not None else "n/a"}


def _tc(name, **args):
    return ChatResult(tool_calls=[{"id": "x", "name": name, "arguments": args}])


def _make(tmp_path, pool_responses, persona_extra=None, prefilter_reply="",
          backend_types=None, worst_used=None):
    db = Database(tmp_path / "r.sqlite")
    registry = ModelRegistry(db)
    for m in pool_responses:
        tier = "medium" if (backend_types or {}).get(m) == "anthropic-compatible" else "free"
        registry.upsert_auto(m, source="discovery", relative_cost_tier=tier)
    tool_registry = ToolRegistry(db, registry, MCPManager([], db))
    for m in pool_responses:
        name = "ask_" + m.replace("-", "_").replace(":", "_")
        tool_registry.tools[name] = ToolDef(name=name, kind="model", description="",
                                            model_id=m, parameters=_ASK_PARAMS)
    pool = Pool(pool_responses, backend_types)
    usage = FixedUsage(MeridianConfig(), db, worst_used)
    brain = ScriptedBrain([_tc("ask_worker", prompt="q"),
                           _tc("return_to_user", use_last_result=True)],
                          prefilter_reply=prefilter_reply)
    runner = AgentRunner(brain, pool, tool_registry, registry,
                         GuardrailEngine(GuardrailsConfig(), db, usage), usage)
    persona = {"virtual_name": "Foundry-Chat", "benchmark_category": "general_chat",
               **(persona_extra or {})}
    ctx = RequestContext(
        persona=persona,
        messages=[{"role": "user", "content": "capital of australia?"}],
        guard=RequestGuardState(),
        logger=RequestLogger(db, "Foundry-Chat", "Foundry-Chat", "agent", "q"))
    return runner, ctx, pool, db, brain


async def _answer(runner, ctx):
    events = [ev async for ev in runner.run(ctx)]
    return [ev for ev in events if ev.kind == "answer"][0].text, events


REVIEW_FIX = json.dumps({"adequate": False, "notes": "wrong capital",
                         "corrected_answer": CORRECTED})
REVIEW_OK = json.dumps({"adequate": True, "notes": "looks right"})


async def test_review_off_by_default(tmp_path):
    runner, ctx, pool, db, brain = _make(
        tmp_path, {"worker": ANSWER, "reviewer": REVIEW_FIX})
    answer, _ = await _answer(runner, ctx)
    assert answer == ANSWER                       # untouched
    assert pool.calls == ["worker"]               # reviewer never invoked
    assert db.query("SELECT * FROM review_log") == []


async def test_enabled_without_model_skips_and_logs(tmp_path):
    runner, ctx, pool, db, _ = _make(
        tmp_path, {"worker": ANSWER}, persona_extra={"review_enabled": 1})
    answer, _ = await _answer(runner, ctx)
    assert answer == ANSWER
    row = db.query_one("SELECT * FROM review_log")
    assert row["trigger_reason"] == "not_configured" and row["corrected"] == 0


async def test_prefilter_pass_skips_review_model(tmp_path):
    runner, ctx, pool, db, brain = _make(
        tmp_path, {"worker": ANSWER, "reviewer": REVIEW_FIX},
        persona_extra={"review_enabled": 1, "review_model": "reviewer"},
        prefilter_reply=json.dumps({"review": False, "reason": "plainly fine"}))
    answer, _ = await _answer(runner, ctx)
    assert answer == ANSWER
    assert brain.complete_calls == 1              # prefilter ran (free)
    assert pool.calls == ["worker"]               # review model saved
    row = db.query_one("SELECT * FROM review_log")
    assert row["trigger_reason"] == "prefilter_passed"


async def test_correction_is_delivered_with_visible_marker(tmp_path):
    runner, ctx, pool, db, _ = _make(
        tmp_path, {"worker": ANSWER, "reviewer": REVIEW_FIX},
        persona_extra={"review_enabled": 1, "review_model": "reviewer",
                       "review_prefilter": 0})
    answer, events = await _answer(runner, ctx)
    assert answer.startswith(CORRECTED)
    assert "🔎" in answer and "reviewer" in answer   # never a silent correction
    assert pool.calls == ["worker", "reviewer"]
    row = db.query_one("SELECT * FROM review_log")
    assert row["corrected"] == 1 and row["review_model"] == "reviewer"
    assert "wrong capital" in row["verdict"]


async def test_adequate_answer_delivered_unmarked(tmp_path):
    runner, ctx, pool, db, _ = _make(
        tmp_path, {"worker": ANSWER, "reviewer": REVIEW_OK},
        persona_extra={"review_enabled": 1, "review_model": "reviewer",
                       "review_prefilter": 0})
    answer, _ = await _answer(runner, ctx)
    assert answer == ANSWER and "🔎" not in answer
    assert db.query_one("SELECT * FROM review_log")["corrected"] == 0


async def test_paid_reviewer_respects_conservation_ladder(tmp_path):
    # window 90% used >= conserve_strong_at: Sonnet-class reviewer is denied,
    # answer delivered unreviewed — review is never an unbounded cost path
    runner, ctx, pool, db, _ = _make(
        tmp_path, {"worker": ANSWER, "claude-sonnet-4-6": REVIEW_FIX},
        persona_extra={"review_enabled": 1, "review_model": "claude-sonnet-4-6",
                       "review_prefilter": 0},
        backend_types={"claude-sonnet-4-6": "anthropic-compatible"},
        worst_used=0.9)
    answer, _ = await _answer(runner, ctx)
    assert answer == ANSWER
    assert pool.calls == ["worker"]               # paid reviewer never called
    row = db.query_one("SELECT * FROM review_log")
    assert row["trigger_reason"] == "guardrail_denied"


async def test_garbage_reviewer_output_fails_open(tmp_path):
    runner, ctx, pool, db, _ = _make(
        tmp_path, {"worker": ANSWER, "reviewer": "not json at all"},
        persona_extra={"review_enabled": 1, "review_model": "reviewer",
                       "review_prefilter": 0})
    answer, _ = await _answer(runner, ctx)
    assert answer == ANSWER                       # broken judge never blocks
