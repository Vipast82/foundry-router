"""v0.181.262: a tool-loop step that comes back empty is asked again, once.

Media_Gen chat test round 7 (2026-11-26, Foundry-Chat on qwen3.8-flash-next): the model
found create_comic with foundry_find_tools, then its next step returned no text and no
tool call, and the chat got "(worker returned empty output)" with nothing queued. The
same request in a new chat worked."""

from foundry_router.brain import prompts
from foundry_router.pool.protocols import ChatResult

from tests.test_worker_tools import WorkerPool, _make, _tool


class SeeingPool(WorkerPool):
    """Keeps the messages each step was sent."""
    def __init__(self, script):
        super().__init__(script)
        self.sent = []

    async def chat(self, model, messages, tools=None, **kw):
        self.sent.append([dict(m) for m in messages])
        return await super().chat(model, messages, tools=tools, **kw)


def _with(runner, script):
    runner.pool = SeeingPool(script)
    return runner.pool


async def test_an_empty_step_after_a_tool_is_asked_again(tmp_path):
    runner, ctx, _pool, mcp = _make(tmp_path, worker_script=[])
    pool = _with(runner, [_tool("searxng_web_search", query="foundry router"),
                          ChatResult(content=""),
                          ChatResult(content="Foundry Router routes requests to models.")])
    events = [ev async for ev in runner.run_worker_tools(ctx)]
    answers = [ev.text for ev in events if ev.kind == "answer"]
    assert answers == ["Foundry Router routes requests to models."]
    assert len(mcp.calls) == 1 and len(pool.sent) == 3
    # the third step was sent the nudge, after the tool's result
    last = pool.sent[2][-1]
    assert last == {"role": "user", "content": prompts.EMPTY_STEP_NUDGE}
    assert any(m.get("role") == "tool" for m in pool.sent[2])
    thinks = " | ".join(ev.text for ev in events if ev.kind == "think")
    assert "returned nothing on this step" in thinks
    assert any("empty step" in g for g in ctx.logger.guardrail_events)


async def test_it_is_asked_only_once(tmp_path):
    runner, ctx, _pool, _mcp = _make(tmp_path, worker_script=[])
    pool = _with(runner, [ChatResult(content=""), ChatResult(content="   ")])
    events = [ev async for ev in runner.run_worker_tools(ctx)]
    assert [ev.text for ev in events if ev.kind == "answer"] == ["(worker returned empty output)"]
    assert len(pool.sent) == 2
    assert sum(1 for m in pool.sent[1] if m.get("content") == prompts.EMPTY_STEP_NUDGE) == 1


async def test_native_reasoning_with_no_answer_counts_as_empty(tmp_path):
    """Reasoning in the engine's own field and no text. (Reasoning written inline in
    <think> tags with nothing after it is shown as the answer, as before: split_think.)"""
    runner, ctx, _pool, _mcp = _make(tmp_path, worker_script=[])
    pool = _with(runner, [ChatResult(content="", thinking="I should call create_comic now."),
                          ChatResult(content="Here is the answer.")])
    events = [ev async for ev in runner.run_worker_tools(ctx)]
    assert [ev.text for ev in events if ev.kind == "answer"] == ["Here is the answer."]
    assert len(pool.sent) == 2


async def test_an_answer_is_not_asked_again(tmp_path):
    runner, ctx, _pool, _mcp = _make(tmp_path, worker_script=[])
    pool = _with(runner, [ChatResult(content="A plain answer.")])
    events = [ev async for ev in runner.run_worker_tools(ctx)]
    assert [ev.text for ev in events if ev.kind == "answer"] == ["A plain answer."]
    assert len(pool.sent) == 1


def test_the_streamed_main_mode_path_asks_again_too(tmp_path):
    """Foundry-Chat runs in main-model mode with the answer streamed: the live path."""
    from foundry_router import lanes
    from tests.test_main_mode import _call, _run, _say, _stream
    lanes._PINS.clear()
    try:
        runner, ctx, pool, mcp = _stream(tmp_path, {
            "main": [_call("searxng_web_search", query="foundry router"), _say(),
                     _say("It is ", "an LLM router.")],
            "small": []})
        evs = _run(runner.run_main(ctx))
    finally:
        lanes._PINS.clear()
    assert [e.text for e in evs if e.kind == "answer_delta"] == ["It is ", "an LLM router."]
    assert [m for m, _ in pool.asked] == ["main", "main", "main"]      # not handed to "small"
    assert len(mcp.calls) == 1
    assert any("empty step" in g for g in ctx.logger.guardrail_events)
