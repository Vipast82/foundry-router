"""User-directed paid routing: an EXPLICIT "use claude/opus/<model>" in the
user's message overrides the free-first default — detected deterministically
(regex, not the brain), surfaced as a [REQUESTED BY THE USER] candidate group
plus a USER MODEL REQUEST prompt block. Before a user-requested PAID dispatch
when the usage window is tight, the router pauses with ask_user to confirm;
the user's "yes" (parsed next turn) bypasses adaptive tier conservation for
that request — never the dollar spend caps."""

import json

from foundry_router.brain import prompts
from foundry_router.brain.agent import (AgentRunner, RequestContext,
                                        _mark_user_requested)
from foundry_router.brain.user_intent import (detect_model_request,
                                              parse_confirmation)
from foundry_router.config import AgentBrainConfig, GuardrailsConfig, MeridianConfig
from foundry_router.db import Database
from foundry_router.guardrails import GuardrailEngine, RequestGuardState
from foundry_router.pool.protocols import ChatResult
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.tools.sync import _ASK_PARAMS, ToolDef, ToolRegistry
from foundry_router.usage import MeridianUsage, RequestLogger


# -- detection ------------------------------------------------------------------

def test_detects_explicit_tier_and_claude_requests():
    assert detect_model_request("use claude for this")["kind"] == "claude"
    r = detect_model_request("Use Opus for this refactor")
    assert r == {"kind": "tier", "target": "opus", "level": 3}
    assert detect_model_request("route this to claude")["kind"] == "claude"
    # "claude sonnet" names the tier, not bare claude
    r = detect_model_request("I want claude sonnet to review it")
    assert r["kind"] == "tier" and r["target"] == "sonnet"
    assert detect_model_request("can you ask opus to check this")["level"] == 3
    assert detect_model_request("please use a paid model for this")["kind"] == "paid"


def test_detects_specific_model_id():
    r = detect_model_request("use qwen3:14b for this", ["qwen3:14b", "other"])
    assert r == {"kind": "model", "target": "qwen3:14b"}
    # base name before the :tag works too
    r = detect_model_request("run it on qwen3 please", ["qwen3:14b"])
    assert r == {"kind": "model", "target": "qwen3:14b"}


def test_mentions_and_negations_are_not_requests():
    assert detect_model_request("what do you think of claude?") is None
    assert detect_model_request("how does opus compare to sonnet?") is None
    assert detect_model_request("don't use claude for this") is None
    assert detect_model_request("do not use opus") is None
    assert detect_model_request("") is None
    assert detect_model_request("summarize this article") is None


# -- confirmation reply parsing ---------------------------------------------------

def test_parse_confirmation():
    assert parse_confirmation("yes") is True
    assert parse_confirmation("Yes please, go ahead") is True
    assert parse_confirmation("sure, do it") is True
    assert parse_confirmation("y") is True
    assert parse_confirmation("no") is False
    assert parse_confirmation("no, stay local") is False
    assert parse_confirmation("nah, use a free model") is False
    # leads with its decision: "no, go ahead" is the no it leads with
    assert parse_confirmation("no, go ahead and use the local one") is False
    # unclear replies are neither — treated as new input
    assert parse_confirmation("actually summarize it differently") is None
    assert parse_confirmation("") is None


# -- candidate marking + prompt rendering -----------------------------------------

def _rows(*ids):
    return [{"id": i, "relative_cost_tier": "free", "score": None} for i in ids]


class _EmptyRegistry:
    def get(self, mid):
        return None


def test_mark_user_requested_floats_and_marks():
    ranked = _mark_user_requested(_rows("local-a", "claude-sonnet-4-6"),
                                  ["claude-sonnet-4-6"], _EmptyRegistry())
    assert ranked[0]["id"] == "claude-sonnet-4-6" and ranked[0]["_user_requested"]
    assert "_user_requested" not in ranked[1]


def test_mark_recovers_model_ranking_dropped():
    # requested model absent from ranked (filtered/limited) — recovered anyway
    ranked = _mark_user_requested(_rows("local-a"), ["claude-opus-4-8"],
                                  _EmptyRegistry())
    assert ranked[0]["id"] == "claude-opus-4-8" and ranked[0]["_user_requested"]


def test_prompt_renders_request_group_and_override_block():
    ranked = _mark_user_requested(_rows("local-a", "claude-sonnet-4-6"),
                                  ["claude-sonnet-4-6"], _EmptyRegistry())
    system = prompts.build_system_prompt(
        None, ranked,
        {"local-a": "ask_local_a", "claude-sonnet-4-6": "ask_claude_sonnet_4_6"},
        "5-hour window 30% used", None,
        user_request={"target": "Claude", "model_ids": ["claude-sonnet-4-6"],
                      "confirmed": None})
    assert "[REQUESTED BY THE USER THIS TURN" in system
    assert "USER MODEL REQUEST" in system
    assert system.index("REQUESTED BY THE USER") < system.index("[FREE / LOCAL")
    # confirmed/declined variants change the block
    confirmed = prompts.build_system_prompt(
        None, ranked, {"claude-sonnet-4-6": "ask_claude_sonnet_4_6"}, "n/a", None,
        user_request={"target": "Claude", "confirmed": True})
    assert "ALREADY CONFIRMED" in confirmed
    declined = prompts.build_system_prompt(
        None, ranked, {"claude-sonnet-4-6": "ask_claude_sonnet_4_6"}, "n/a", None,
        user_request={"target": "Claude", "confirmed": False})
    assert "DECLINED" in declined


def test_spend_note_renders():
    system = prompts.build_system_prompt(
        None, _rows("local-a"), {"local-a": "ask_local_a"}, "n/a", None,
        spend_note="$1.20 of $5.00 daily cap used")
    assert "METERED SPEND: $1.20 of $5.00 daily cap used" in system


# -- pending_paid server-side state ----------------------------------------------

def test_pending_paid_roundtrip(tmp_path):
    db = Database(tmp_path / "p.sqlite")
    asked = [{"role": "user", "content": "use claude for this"}]
    prompts.store_pending_paid(db, asked, {"target": "Claude",
                                           "model_ids": ["claude-sonnet-4-6"]})
    resumed = asked + [{"role": "assistant", "content": "window is 80% used — continue?"},
                       {"role": "user", "content": "yes"}]
    found = prompts.find_pending_paid(db, resumed)
    assert found == {"target": "Claude", "model_ids": ["claude-sonnet-4-6"]}
    # consumed — a confirmation is answered at most once
    assert prompts.find_pending_paid(db, resumed) is None


# -- guardrail bypass on explicit approval ----------------------------------------

class FixedUsage(MeridianUsage):
    def __init__(self, cfg, db, worst_used, fable_used=None):
        super().__init__(cfg, client=None, db=db)
        self._worst = worst_used
        self._fable = fable_used

    async def snapshot(self, base_url, api_key=None):
        remaining = 1.0 - (self._worst or 0)
        return {"available": self._worst is None
                             or remaining >= self.cfg.min_window_fraction,
                "buckets": [], "worst_used": self._worst,
                "fable_used": self._fable,
                "note": f"{self._worst:.0%} used" if self._worst is not None else "n/a"}


MERIDIAN_INFO = {"name": "meridian", "type": "anthropic-compatible",
                 "url": "http://m", "api_key": "k"}


def _engine(tmp_path, worst_used, **meridian_kwargs):
    db = Database(tmp_path / "g.sqlite")
    usage = FixedUsage(MeridianConfig(**meridian_kwargs), db, worst_used)
    return GuardrailEngine(GuardrailsConfig(), db, usage)


async def test_approval_bypasses_tier_conservation(tmp_path):
    engine = _engine(tmp_path, 0.9)  # >= conserve_strong_at: Sonnet normally denied
    denied = await engine.check_paid_call("claude-sonnet-4-6", MERIDIAN_INFO,
                                          None, RequestGuardState(),
                                          engine.effective(None))
    assert not denied.allowed
    approved_state = RequestGuardState(user_approved_paid=True, credits_warned=True)
    allowed = await engine.check_paid_call("claude-sonnet-4-6", MERIDIAN_INFO,
                                           None, approved_state,
                                           engine.effective(None))
    assert allowed.allowed


async def test_approval_spends_credits_when_exhausted(tmp_path):
    engine = _engine(tmp_path, 0.97, usage_credits="last_resort")
    state = RequestGuardState(user_approved_paid=True, credits_warned=True)
    verdict = await engine.check_paid_call("claude-sonnet-4-6", MERIDIAN_INFO,
                                           None, state, engine.effective(None))
    assert verdict.allowed
    assert any("PURCHASED USAGE CREDITS" in e for e in state.events)


async def test_approval_cannot_override_credits_never(tmp_path):
    engine = _engine(tmp_path, 0.97, usage_credits="never")
    state = RequestGuardState(user_approved_paid=True, credits_warned=True)
    verdict = await engine.check_paid_call("claude-sonnet-4-6", MERIDIAN_INFO,
                                           None, state, engine.effective(None))
    assert not verdict.allowed


# -- integration: pause-to-confirm, then dispatch on yes --------------------------

GOOD = "Sure — that plant is a Boston fern."


class PaidPool:
    """One local + one subscription model."""

    def __init__(self):
        self.calls = []

    def available_models(self):
        return {"local-chat": ["b"], "claude-sonnet-4-6": ["meridian"]}

    def backend_info(self, m):
        if m == "claude-sonnet-4-6":
            return dict(MERIDIAN_INFO)
        if m == "local-chat":
            return {"name": "b", "type": "ollama", "url": "http://x", "api_key": None}
        return None

    async def chat(self, model, messages, tools=None, options=None, max_tokens=4096):
        self.calls.append(model)
        return ChatResult(content=GOOD), "meridian"


class ScriptedBrain:
    def __init__(self, responses):
        self.responses = list(responses)
        self.cfg = AgentBrainConfig()

    async def chat(self, messages, tools=None, **kwargs):
        return self.responses.pop(0)

    async def complete(self, prompt):
        return ""


def _tc(name, **args):
    return ChatResult(tool_calls=[{"id": "x", "name": name, "arguments": args}])


def _make(tmp_path, brain_responses, worst_used):
    db = Database(tmp_path / "i.sqlite")
    registry = ModelRegistry(db)
    registry.upsert_auto("local-chat", source="discovery", relative_cost_tier="free")
    registry.upsert_auto("claude-sonnet-4-6", source="discovery",
                         relative_cost_tier="medium")
    tool_registry = ToolRegistry(db, registry, MCPManager([], db))
    for m in ("local-chat", "claude-sonnet-4-6"):
        name = "ask_" + m.replace("-", "_").replace(":", "_")
        tool_registry.tools[name] = ToolDef(name=name, kind="model", description="",
                                            model_id=m, parameters=_ASK_PARAMS)
    usage = FixedUsage(MeridianConfig(), db, worst_used)
    pool = PaidPool()
    runner = AgentRunner(ScriptedBrain(brain_responses), pool, tool_registry,
                         registry, GuardrailEngine(GuardrailsConfig(), db, usage),
                         usage)
    return runner, pool, db


def _ctx(db, messages):
    return RequestContext(
        persona={"virtual_name": "Foundry-Chat", "benchmark_category": "general_chat"},
        messages=messages, guard=RequestGuardState(),
        logger=RequestLogger(db, "Foundry-Chat", "Foundry-Chat", "agent", "q"))


async def test_user_requested_paid_pauses_for_confirmation(tmp_path):
    # window 80% used (>= confirm_user_paid_at 0.5): explicit "use claude"
    # pauses with a confirmation question instead of dispatching or denying
    runner, pool, db = _make(
        tmp_path, [_tc("ask_claude_sonnet_4_6", prompt="identify this plant")],
        worst_used=0.8)
    asked = [{"role": "user", "content": "use claude to identify this plant"}]
    events = [ev async for ev in runner.run(_ctx(db, asked))]
    assert pool.calls == []                      # nothing spent
    questions = [ev for ev in events if ev.kind == "ask_user"]
    assert len(questions) == 1
    assert "80%" in questions[0].text and "yes" in questions[0].text.lower()
    # server-side pending record exists for the resuming turn
    resumed = asked + [{"role": "assistant", "content": questions[0].text},
                       {"role": "user", "content": "yes"}]
    pending = prompts.find_pending_paid(db, resumed)
    assert pending and "claude-sonnet-4-6" in pending["model_ids"]


async def test_confirmed_yes_dispatches_past_conservation(tmp_path):
    # window 90% used — conservation would deny Sonnet — but the user said yes
    runner, pool, db = _make(
        tmp_path, [_tc("ask_claude_sonnet_4_6", prompt="identify this plant"),
                   _tc("return_to_user", use_last_result=True)],
        worst_used=0.9)
    ctx = _ctx(db, [{"role": "user", "content": "use claude to identify this plant"},
                    {"role": "assistant", "content": "window is 90% used — continue?"},
                    {"role": "user", "content": "yes"}])
    ctx.user_model_request = {"target": "Claude",
                              "model_ids": ["claude-sonnet-4-6"],
                              "paid": True, "confirmed": True}
    ctx.paid_confirmation = True
    ctx.guard.user_approved_paid = True
    ctx.guard.credits_warned = True
    events = [ev async for ev in runner.run(ctx)]
    assert pool.calls == ["claude-sonnet-4-6"]
    answers = [ev for ev in events if ev.kind == "answer"]
    assert answers and answers[0].text == GOOD


async def test_plenty_of_window_dispatches_without_ceremony(tmp_path):
    # window 20% used (< 0.5): explicit request dispatches straight through
    runner, pool, db = _make(
        tmp_path, [_tc("ask_claude_sonnet_4_6", prompt="identify this plant"),
                   _tc("return_to_user", use_last_result=True)],
        worst_used=0.2)
    events = [ev async for ev in runner.run(
        _ctx(db, [{"role": "user", "content": "use claude to identify this plant"}]))]
    assert pool.calls == ["claude-sonnet-4-6"]
    assert not [ev for ev in events if ev.kind == "ask_user"]
    assert [ev for ev in events if ev.kind == "answer"][0].text == GOOD


async def test_brain_choice_unaffected_by_gate_without_user_request(tmp_path):
    # No explicit request: the gate never fires; normal guardrails still rule
    # (0.8 < conserve_strong_at 0.85, so Sonnet passes conservation).
    runner, pool, db = _make(
        tmp_path, [_tc("ask_claude_sonnet_4_6", prompt="identify this plant"),
                   _tc("return_to_user", use_last_result=True)],
        worst_used=0.8)
    events = [ev async for ev in runner.run(
        _ctx(db, [{"role": "user", "content": "identify this plant"}]))]
    assert not [ev for ev in events if ev.kind == "ask_user"]
    assert pool.calls == ["claude-sonnet-4-6"]
