"""Helpers (v0.181.143): models a main-model persona may ask for help, listed inside
the persona. One extra tool, ask_helper; roles instead of model names; limits; the
same paid guardrail; a long tool result condensed by a helper before the main model
reads it."""

import json
import types

import pytest

from foundry_router import helpers, lanes, persona_rules
from foundry_router.db import Database
from foundry_router.personas import PersonaStore
from foundry_router.pool.base import AllBackendsFailed
from foundry_router.pool.protocols import ChatResult
from tests.test_main_mode import Pool, _make, _run, _tool


@pytest.fixture(autouse=True)
def _no_homes():
    lanes._PINS.clear()
    yield
    lanes._PINS.clear()


class RecPool(Pool):
    """Records what each call was given."""

    def __init__(self, scripts, infos=None):
        super().__init__(scripts)
        self.seen = []
        self.infos = infos or {}

    def backend_info(self, m):
        return self.infos.get(m) or super().backend_info(m)

    async def chat(self, model, messages, tools=None, options=None, max_tokens=4096, think=None, fmt=None):
        self.seen.append({"model": model, "messages": [dict(x) for x in messages],
                          "tools": [t["function"]["name"] for t in (tools or [])]})
        return await super().chat(model, messages, tools=tools)


def _setup(tmp_path, scripts, rows, infos=None, pins=("main",)):
    runner, ctx, _, mcp = _make(tmp_path, scripts, pins=pins)
    pool = RecPool(scripts, infos)
    runner.pool = pool
    ctx.persona["helpers"] = json.dumps(rows)
    return runner, ctx, pool, mcp


def _last_tool_text(pool):
    return next(m["content"] for m in reversed(pool.seen[-1]["messages"]) if m.get("role") == "tool")


# -- the setting ----------------------------------------------------------------------------------

def test_rows_are_normalised_and_checked():
    rows, errs = helpers.clean([
        {"role": " Deep ", "models": ["claude-opus", "gpt", "claude-opus"], "what": "hard reasoning",
         "paid": True, "tools": False, "max_calls": "", "digest_over": 0},
        {"role": "reader", "models": "haiku, luna", "digest_over": 20000, "paid": False, "tools": True,
         "max_calls": 5, "instruction": " be brief "}])
    assert errs == []
    assert rows[0] == {"role": "deep", "models": ["claude-opus", "gpt"], "what": "hard reasoning"}
    assert rows[1] == {"role": "reader", "models": ["haiku", "luna"], "what": "", "instruction": "be brief",
                       "tools": True, "paid": False, "max_calls": 5, "digest_over": 20000}
    for bad, word in (([{"role": "has space", "models": ["m"]}], "short name"),
                      ([{"role": "a", "models": []}], "at least one model"),
                      ([{"role": "a", "models": ["m"]}, {"role": "A", "models": ["m"]}], "listed twice"),
                      ([{"role": "a", "models": ["m"], "max_calls": 0}], "between 1 and 50"),
                      ("{not json", "must be a list"), ({"role": "a"}, "must be a list")):
        assert word in " ".join(helpers.clean(bad)[1]), bad
    assert helpers.clean(None) == ([], []) and helpers.parse({"helpers": "nonsense"}) == []


def test_the_main_model_sees_roles_in_one_tool():
    p = {"helpers": json.dumps([{"role": "deep", "models": ["opus", "gpt"], "what": "hard problems"},
                                {"role": "scout", "models": ["haiku"], "tools": True}])}
    t = helpers.tooldef(p)
    assert t.name == "ask_helper" and t.server == helpers.SERVER
    assert t.parameters["properties"]["helper"]["enum"] == ["deep", "scout"]
    assert t.parameters["required"] == ["helper", "task"]
    assert "- deep: hard problems (models: opus, gpt)" in t.description
    assert "- scout: general help (models: haiku; can use your tools)" in t.description
    assert "does NOT see this conversation" in t.description
    assert helpers.tooldef({"helpers": None}) is None


def test_the_setting_is_stored_and_validated(tmp_path):
    db = Database(tmp_path / "h.sqlite")
    store = PersonaStore(db)
    clean, errors, _ = persona_rules.validate(
        types.SimpleNamespace(), {"helpers": [{"role": "Deep", "models": ["m1"]}]}, allow_unknown=True)
    assert errors == [] and clean["helpers"] == [{"role": "deep", "models": ["m1"], "what": ""}]
    store.upsert("Foundry-Chat", **clean)
    assert helpers.parse(store.get("Foundry-Chat")) == clean["helpers"]
    _, errors, _ = persona_rules.validate(types.SimpleNamespace(), {"helpers": [{"role": "x"}]}, allow_unknown=True)
    assert errors and "at least one model" in errors[0]


# -- ask_helper ---------------------------------------------------------------------------------------

def test_the_main_model_asks_a_helper_and_uses_its_answer(tmp_path):
    runner, ctx, pool, _ = _setup(tmp_path, {
        "main": [_tool("ask_helper", helper="deep", task="What is 17 * 23?", context="show the product only"),
                 ChatResult(content="The helper says 391.")],
        "small": [ChatResult(content="<think>17*23</think>391")]},
        [{"role": "deep", "models": ["small"], "what": "arithmetic"}])
    evs = _run(runner.run_main(ctx))
    assert [e.text for e in evs if e.kind == "answer"] == ["The helper says 391."]
    assert [c["model"] for c in pool.seen] == ["main", "small", "main"]
    assert "ask_helper" in pool.seen[0]["tools"]                         # offered next to the MCP tools
    h = pool.seen[1]
    assert h["tools"] == [] and h["messages"][0]["content"] == helpers.HELPER_SYSTEM
    assert h["messages"][1]["content"] == "What is 17 * 23?\n\n---\nshow the product only"
    assert len(h["messages"]) == 2                                       # it does not see the conversation
    assert _last_tool_text(pool) == "[answer from the deep helper, small]\n391"
    think = " ".join(e.text for e in evs if e.kind == "think")
    assert "main asks the deep helper (small)" in think and "answered in" in think
    assert {u["model"] for u in ctx.logger.models_used} == {"main", "small"}      # both are accounted
    tc = ctx.logger.tool_calls[-1]
    assert tc["tool"] == "ask_helper" and tc["ok"]


def test_a_helper_has_a_call_limit(tmp_path):
    runner, ctx, pool, _ = _setup(tmp_path, {
        "main": [_tool("ask_helper", helper="deep", task="one"), _tool("ask_helper", helper="deep", task="two"),
                 ChatResult(content="done")],
        "small": [ChatResult(content="first")]},
        [{"role": "deep", "models": ["small"], "max_calls": 1}])
    _run(runner.run_main(ctx))
    assert [c["model"] for c in pool.seen] == ["main", "small", "main", "main"]
    assert "already asked 1 time(s)" in _last_tool_text(pool)


def test_problems_are_told_to_the_main_model_not_raised(tmp_path):
    runner, ctx, pool, _ = _setup(tmp_path, {
        "main": [_tool("ask_helper", helper="nope", task="x"), _tool("ask_helper", helper="deep", task=""),
                 _tool("ask_helper", helper="deep", task="real"), ChatResult(content="I did it myself.")],
        "small": [AllBackendsFailed("small is down")]},
        [{"role": "deep", "models": ["gone", "small"]}])
    evs = _run(runner.run_main(ctx))
    assert [e.text for e in evs if e.kind == "answer"] == ["I did it myself."]
    texts = [m["content"] for m in pool.seen[-1]["messages"] if m.get("role") == "tool"]
    assert "no helper named 'nope'" in texts[0] and "Helpers: deep" in texts[0]
    assert "`task` is empty" in texts[1]
    assert "the deep helper (small) failed" in texts[2] and "Do this part yourself" in texts[2]
    assert not [e for e in evs if e.kind == "error"]


def test_a_paid_model_needs_the_rows_leave(tmp_path):
    paid = {"name": "meridian", "type": "anthropic-compatible", "url": "http://x", "api_key": None}
    runner, ctx, pool, _ = _setup(tmp_path, {
        "main": [_tool("ask_helper", helper="deep", task="x"), ChatResult(content="ok")],
        "small": [ChatResult(content="local answer")], "claude": []},
        [{"role": "deep", "models": ["claude", "small"], "paid": False}], infos={"claude": paid})
    _run(runner.run_main(ctx))
    assert [c["model"] for c in pool.seen] == ["main", "small", "main"]  # the paid one was passed over
    assert any("claude is a paid model and this helper may not use one" in g
               for g in ctx.logger.guardrail_events)


def test_a_helper_never_takes_the_main_models_only_stream(tmp_path):
    same = {"name": "main-be", "type": "ollama", "url": "http://x", "api_key": None}
    runner, ctx, pool, _ = _setup(tmp_path, {
        "main": [_tool("ask_helper", helper="deep", task="x"), ChatResult(content="ok")],
        "small": [], "twin": []},
        [{"role": "deep", "models": ["twin"]}], infos={"twin": same})
    pool.backends = {"main-be": types.SimpleNamespace(config=types.SimpleNamespace(max_concurrent=1), busy=0)}
    evs = _run(runner.run_main(ctx))
    assert [c["model"] for c in pool.seen] == ["main", "main"]
    assert "shares main-be's single stream with main" in _last_tool_text(pool)
    # with room for two it is used
    lanes._PINS.clear()
    runner, ctx, pool, _ = _setup(tmp_path, {
        "main": [_tool("ask_helper", helper="deep", task="x"), ChatResult(content="ok")],
        "small": [], "twin": [ChatResult(content="from the twin")]},
        [{"role": "deep", "models": ["twin"]}], infos={"twin": same})
    pool.backends = {"main-be": types.SimpleNamespace(config=types.SimpleNamespace(max_concurrent=2), busy=0)}
    _run(runner.run_main(ctx))
    assert [c["model"] for c in pool.seen] == ["main", "twin", "main"]


def test_a_helper_with_tools_runs_its_own_loop(tmp_path):
    runner, ctx, pool, mcp = _setup(tmp_path, {
        "main": [_tool("ask_helper", helper="scout", task="Find what foundry router is."),
                 ChatResult(content="It is an LLM router.")],
        "small": [_tool("searxng_web_search", query="foundry router"), ChatResult(content="An LLM router.")]},
        [{"role": "scout", "models": ["small"], "tools": True, "instruction": "Research and report."}])
    evs = _run(runner.run_main(ctx))
    assert [e.text for e in evs if e.kind == "answer"] == ["It is an LLM router."]   # only the main model's
    assert [c["model"] for c in pool.seen] == ["main", "small", "small", "main"]
    assert len(mcp.calls) == 1
    assert pool.seen[1]["tools"] == ["searxng_web_search"]               # the tools, never ask_helper
    assert "Research and report." in pool.seen[1]["messages"][0]["content"]
    assert _last_tool_text(pool) == "[answer from the scout helper, small]\nAn LLM router."
    assert any(e.text.startswith("  ↳ ") for e in evs if e.kind == "think")           # its steps are shown


# -- a long tool result is condensed first ----------------------------------------------------------

def test_a_long_tool_result_is_condensed_by_the_reader(tmp_path):
    runner, ctx, pool, mcp = _setup(tmp_path, {
        "main": [_tool("searxng_web_search", query="foundry"), ChatResult(content="A router.")],
        "small": [ChatResult(content="router")]},
        [{"role": "reader", "models": ["small"], "digest_over": 20}])
    evs = _run(runner.run_main(ctx))
    assert [c["model"] for c in pool.seen] == ["main", "small", "main"]
    d = pool.seen[1]["messages"]
    assert d[0]["content"] == helpers.DIGEST_SYSTEM
    assert "what is foundry router" in d[1]["content"] and "search result: foundry router" in d[1]["content"]
    assert _last_tool_text(pool).startswith("[condensed by small from 46 characters;")
    assert _last_tool_text(pool).endswith("\nrouter")
    assert any("condenses it first" in e.text for e in evs if e.kind == "think")


def test_a_failed_or_useless_digest_lets_the_result_through(tmp_path):
    for script in ([AllBackendsFailed("down")], [ChatResult(content="x" * 500)]):
        lanes._PINS.clear()
        runner, ctx, pool, _ = _setup(tmp_path, {
            "main": [_tool("searxng_web_search", query="foundry"), ChatResult(content="A router.")],
            "small": list(script)}, [{"role": "reader", "models": ["small"], "digest_over": 20}])
        evs = _run(runner.run_main(ctx))
        assert _last_tool_text(pool) == "search result: foundry router is an LLM router"
        assert [e.text for e in evs if e.kind == "answer"] == ["A router."]
    # under the limit: the reader is not asked
    lanes._PINS.clear()
    runner, ctx, pool, _ = _setup(tmp_path, {
        "main": [_tool("searxng_web_search", query="foundry"), ChatResult(content="A router.")], "small": []},
        [{"role": "reader", "models": ["small"], "digest_over": 5000}])
    _run(runner.run_main(ctx))
    assert [c["model"] for c in pool.seen] == ["main", "main"]


# -- wiring --------------------------------------------------------------------------------------------

def test_wiring():
    import inspect
    from pathlib import Path

    from foundry_router.brain.agent import AgentRunner
    from foundry_router.facade import ollama_api
    from foundry_router.personas import PERSONA_FIELDS
    assert "helpers" in PERSONA_FIELDS
    assert "_persona_has_mcp_tools(persona) or _helpers.parse(persona)" in inspect.getsource(ollama_api)
    src = inspect.getsource(AgentRunner._run_helper)
    assert '"helpers": None' in src                                      # a helper cannot ask a helper
    html = (Path(helpers.__file__).parent / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    assert '<textarea id="pe-helpers" data-jform="helpers"' in html
    assert 'helpers: JSON.parse($("#pe-helpers").value || "[]")' in html
    assert 'helpers: {kind: "rows", required: "role"' in html
    import re
    form = html[html.index('  helpers: {kind: "rows"'):html.index("  a2aAgents: {kind")]
    assert set(re.findall(r'\{key: "([a-z_]+)"', form)) == {
        "role", "what", "models", "agent", "tools", "paid", "max_calls", "digest_over", "instruction"}
