"""v0.181.201: the finder benchmark — the ecosystem as it is actually run.

v0.181.200 put the whole catalogue in the prompt and asked which tool fits.
That is the opposite of how these personas run: with `mcp_tool_mode: finder` a
model gets two tools and about 400 tokens and has to SEARCH. A model can be
perfect at picking from a list in front of it and useless at finding anything,
and it is the second that decides whether it can run this ecosystem.

So a case is a task, the searches really run against the live registry, and the
tool the model chooses is recorded and never executed — a benchmark must not
generate a video. Scored in halves: finding it, then choosing it.
"""
import json

import pytest

from foundry_router import bench, tool_finder
from foundry_router.db import Database


class _Tool:
    def __init__(self, name, server, description, params=None):
        self.name, self.server, self.description = name, server, description
        self.mcp_tool = name
        self.parameters = params or {"type": "object", "properties": {}}


TOOLS = [
    _Tool("generate_image", "Media_Gen", "Generate an image from a text prompt"),
    _Tool("generate_video", "Media_Gen", "Generate a video from a text prompt"),
    _Tool("searxng_web_search", "searxng", "Search the public web for pages"),
    _Tool("roblox_get_class", "Roblox-docs", "Look up a Roblox class and its members"),
    _Tool("SQLite__read_query", "gw", "Run a read-only SQL query against the database"),
    _Tool("upscale_image", "Media_Gen", "Increase the resolution of an existing image"),
]


class _Result:
    def __init__(self, tool_calls=None, content=""):
        self.tool_calls = tool_calls or []
        self.content = content
        self.prompt_tokens = self.completion_tokens = 10
        self.eval_duration_ns = self.prompt_eval_duration_ns = 0
        self.load_duration_ns = 0
        self.draft_n = self.draft_n_accepted = 0
        self.cached_tokens = 0
        self.finish_reason = "stop"


def _svc(tmp_path, script):
    """`script` is a list of results the fake model returns, in order."""
    seq = list(script)
    sent = []

    class Pool:
        async def chat(self, model, convo, **kw):
            sent.append(convo)
            return (seq.pop(0) if seq else _Result(content="I give up")), "fake"

    svc = type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "b.sqlite"),
        "pool": Pool(),
        "registry": type("R", (), {"note_inference": lambda *a, **k: None,
                                   "note_ttft": lambda *a, **k: None,
                                   "note_finish": lambda *a, **k: None})(),
        "tool_registry": type("T", (), {
            "mcp_tools_for_persona": staticmethod(lambda p: list(TOOLS))})(),
    })()
    svc._sent = sent
    return svc


def _case(want="generate_image", ask="make a picture of a red barn"):
    return {"id": "FLOW-001", "task_id": want,
            "tags": ["toolflow", "finder"],
            "messages": [{"role": "system", "content": "s"},
                         {"role": "user", "content": f"Please {ask}."}],
            "reference": {"finder_tool": want, "content": want,
                          "tool_calls": [{"name": "foundry_call_tool"}]}}


def _find(query):
    return _Result([{"id": "1", "name": tool_finder.FIND_TOOL,
                     "arguments": {"query": query}}])


def _call(name):
    return _Result([{"id": "2", "name": tool_finder.CALL_TOOL,
                     "arguments": {"name": name, "arguments": {}}}])


# -- the loop ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_good_search_then_the_right_call_scores_full(tmp_path):
    svc = _svc(tmp_path, [_find("generate an image"), _call("generate_image")])
    got = await bench.ask_through_finder(svc, "m", _case())
    assert got["finder"]["found"] is True
    assert got["finder"]["picked"] == "generate_image"
    assert bench.finder_score(got, "generate_image")["score"] == 100.0


@pytest.mark.asyncio
async def test_finding_it_and_then_choosing_wrongly_scores_half(tmp_path):
    """A different failure from never finding it, and one number would hide
    which."""
    svc = _svc(tmp_path, [_find("image"), _call("generate_video")])
    got = await bench.ask_through_finder(svc, "m", _case())
    s = bench.finder_score(got, "generate_image")
    assert s["found"] == 1.0 and s["picked"] == 0.0 and s["score"] == 50.0


@pytest.mark.asyncio
async def test_a_search_that_never_surfaces_it_scores_nothing(tmp_path):
    svc = _svc(tmp_path, [_find("quantum tunnelling"), _call("")])
    got = await bench.ask_through_finder(svc, "m", _case())
    assert bench.finder_score(got, "generate_image")["score"] == 0.0


@pytest.mark.asyncio
async def test_answering_in_prose_without_searching_scores_nothing(tmp_path):
    """The whole point: a model that will not use the finder cannot run this."""
    svc = _svc(tmp_path, [_Result(content="I would use an image generator.")])
    got = await bench.ask_through_finder(svc, "m", _case())
    assert got["finder"]["searched"] == 0
    assert bench.finder_score(got, "generate_image")["score"] == 0.0


@pytest.mark.asyncio
async def test_several_searches_are_allowed_and_counted(tmp_path):
    svc = _svc(tmp_path, [_find("barn"), _find("photo"), _find("generate an image"),
                          _call("generate_image")])
    got = await bench.ask_through_finder(svc, "m", _case())
    assert got["finder"]["searched"] == 3
    assert bench.finder_score(got, "generate_image")["searched"] == 3
    assert got["finder"]["found"] is True


@pytest.mark.asyncio
async def test_the_target_tool_is_never_executed(tmp_path):
    """A benchmark must not generate a video.

    The choice is recorded and the loop stops. The fake service here has no tool
    surface at all, so an attempt to run one would raise rather than pass
    quietly."""
    svc = _svc(tmp_path, [_find("video"), _call("generate_video")])
    assert not hasattr(svc, "mcp")
    got = await bench.ask_through_finder(svc, "m", _case("generate_video",
                                                         "make a short clip"))
    assert got["finder"]["picked"] == "generate_video"
    assert "error" not in got, got.get("error")


def test_the_finder_path_has_no_way_to_execute_a_tool():
    """Read from the source: `tool_finder.run` and the rich call are the two
    ways a tool actually runs, and neither is reachable from here."""
    import inspect
    src = inspect.getsource(bench.ask_through_finder)
    assert "tool_finder.run" not in src
    assert "call_tool_rich" not in src
    assert "_dispatch" not in src
    # it searches and it reads the result text, and that is all
    assert "tool_finder.search" in src
    assert "tool_finder.find_text" in src
    assert "not executed" in src


@pytest.mark.asyncio
async def test_the_model_is_offered_only_the_two_finder_tools(tmp_path):
    svc = _svc(tmp_path, [_Result(content="done")])
    await bench.ask_through_finder(svc, "m", _case())
    # the fake pool records convos; the specs are checked through tool_finder
    names = {td.name for td in tool_finder.tooldefs()}
    assert names == {tool_finder.FIND_TOOL, tool_finder.CALL_TOOL}


@pytest.mark.asyncio
async def test_it_gives_up_rather_than_looping_for_ever(tmp_path):
    """A model that only ever searches must end the case, not the run."""
    svc = _svc(tmp_path, [_find("x")] * 50)
    got = await bench.ask_through_finder(svc, "m", _case())
    assert got["finder"]["searched"] <= bench.TOOLFLOW_ROUNDS
    assert got["finder"]["picked"] == ""


@pytest.mark.asyncio
async def test_a_backend_error_is_reported_not_scored(tmp_path):
    class Pool:
        async def chat(self, *a, **k):
            raise RuntimeError("backend down")
    svc = _svc(tmp_path, [])
    svc.pool = Pool()
    got = await bench.ask_through_finder(svc, "m", _case())
    assert "backend down" in got["error"]


# -- naming ---------------------------------------------------------------------------

def test_the_unprefixed_name_still_counts():
    """foundry_call_tool resolves a name without its server prefix, so failing
    it here would score the harness rather than the model."""
    assert bench.tool_finder_match("generate_image", "Media_Gen__generate_image")
    assert bench.tool_finder_match("generate_image", "generate_image")
    assert not bench.tool_finder_match("generate_video", "generate_image")
    assert not bench.tool_finder_match("", "generate_image")


# -- the suite -------------------------------------------------------------------------

def test_the_suite_is_built_from_the_live_registry(tmp_path):
    svc = _svc(tmp_path, [])
    out = bench.build_toolflow(svc, limit=10)
    assert out["granted"] == len(TOOLS)
    cases = bench.cases(svc, "toolflow")
    assert {c["reference"]["finder_tool"] for c in cases} <= {t.name for t in TOOLS}
    for c in cases:
        # v0.181.215: a clean request with its inputs, not "Please <docstring>"
        assert c["messages"][1]["content"].startswith("I need this done: ")
        assert "toolflow" in c["tags"] and "finder" in c["tags"]


def test_the_task_does_not_hand_over_the_tool_name(tmp_path):
    """It should have to search, not copy."""
    svc = _svc(tmp_path, [])
    bench.build_toolflow(svc, limit=10)
    for c in bench.cases(svc, "toolflow"):
        assert c["reference"]["finder_tool"] not in c["messages"][1]["content"]


def test_a_registry_with_almost_nothing_is_refused(tmp_path):
    svc = _svc(tmp_path, [])
    svc.tool_registry = type("T", (), {
        "mcp_tools_for_persona": staticmethod(lambda p: TOOLS[:2])})()
    with pytest.raises(ValueError):
        bench.build_toolflow(svc, limit=5)


def test_the_suite_grows_with_the_tool_list(tmp_path):
    """The registry went from 132 tools to 159 in a day; the benchmark has to
    follow it rather than be a frozen list."""
    svc = _svc(tmp_path, [])
    first = bench.build_toolflow(svc, limit=50)["cases"]
    svc.tool_registry = type("T", (), {"mcp_tools_for_persona": staticmethod(
        lambda p: TOOLS + [_Tool(f"new_tool_{i}", "s", f"Do new thing {i}")
                           for i in range(10)])})()
    assert bench.build_toolflow(svc, limit=50)["cases"] > first


@pytest.mark.asyncio
async def test_the_run_loop_sends_finder_cases_down_the_finder_path():
    import inspect
    src = inspect.getsource(bench._run)
    assert "ask_through_finder" in src
    assert '"finder_tool"' in src


@pytest.mark.asyncio
async def test_score_one_uses_the_finder_scorer(tmp_path):
    svc = _svc(tmp_path, [])
    got = {"finder": {"found": True, "picked": "generate_image", "searched": 2},
           "content": "generate_image", "tool_calls": []}
    s = await bench.score_one(svc, _case(), got)
    assert s["score"] == 100.0
    assert "found=True" in s["code_detail"] and "searches=2" in s["code_detail"]
