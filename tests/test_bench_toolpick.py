"""v0.181.200: picking the right tool out of all of them, and finding the tags.

This router offers 132 tools behind the finder, and choosing the right one is
what a model has to do before it can do anything else - it is also where the
brain model lost its benchmark run, 16 cases with a zero tool score against
strata's 6. So the whole catalogue goes in the prompt as the distractor set and
each case asks which tool meets one stated need.

The limitation is written down rather than glossed: the need is phrased FROM the
tool's own description, so this measures mapping a stated need onto the right
entry in a long list, not knowing from experience what a tool does. A case whose
need happens to contain its own answer is dropped at build time.

And the tag list, which is the whole of "build a Luau suite": `build` has taken
a tags filter all along and the console's form passed it, so the feature existed
and nothing said which tags there were.
"""
import json
import os

import pytest

from foundry_router import bench
from foundry_router.db import Database

TOOLS = [
    {"name": "SQLite__list_tables", "description": "[MCP:gw] List all tables in the "
     "SQLite database", "disabled": False},
    {"name": "SQLite__describe_table", "description": "[MCP:gw] Get the schema "
     "information for a specific table", "disabled": False},
    {"name": "blender__render_frame", "description": "Render a single frame to a PNG "
     "file", "disabled": False},
    {"name": "searxng__web_search", "description": "Search the public web and return "
     "ranked results", "disabled": False},
    {"name": "roblox__insert_asset", "description": "Insert an asset from the Creator "
     "Store into the open place", "disabled": False},
    {"name": "off__retired", "description": "Nothing", "disabled": True},
]


def _svc(tmp_path, tools=None):
    return type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "b.sqlite"),
        "tool_registry": type("T", (), {
            "status": staticmethod(lambda: list(TOOLS if tools is None else tools))})(),
    })()


# -- the catalogue -------------------------------------------------------------------

def test_a_disabled_tool_is_not_offered(tmp_path):
    rows = bench._tool_rows(_svc(tmp_path))
    assert "off__retired" not in [n for n, _d in rows]
    assert len(rows) == 5


def test_the_server_prefix_is_stripped_from_the_need(tmp_path):
    rows = dict(bench._tool_rows(_svc(tmp_path)))
    assert rows["SQLite__list_tables"].startswith("List all tables")


def test_every_tool_keeps_a_line_however_long_the_catalogue():
    """Shortening descriptions is fine; dropping tools would take the answer out
    of some cases and then score the model for not finding it."""
    big = [(f"tool_{i}", "x" * 500) for i in range(1500)]
    text, cap = bench._fit_catalogue(big)
    assert cap >= 0
    for i in (0, 1, 749, 1499):
        assert f"tool_{i}" in text
    assert len(text) <= int(bench.MAX_PROMPT_TOKENS * 0.6) * 4


def test_a_small_catalogue_keeps_its_descriptions():
    text, cap = bench._fit_catalogue([("a", "does a thing"), ("b", "does another")])
    assert cap == 160 and "does a thing" in text


# -- the suite -----------------------------------------------------------------------

def test_the_whole_catalogue_is_in_every_prompt(tmp_path):
    svc = _svc(tmp_path)
    bench.build_toolpick(svc, limit=5)
    names = [n for n, _d in bench._tool_rows(svc)]
    for c in bench.cases(svc, "toolpick"):
        body = c["messages"][1]["content"]
        for n in names:
            assert n in body, (c["id"], n)


def test_the_answer_is_the_tool_and_is_scored_exactly(tmp_path):
    svc = _svc(tmp_path)
    bench.build_toolpick(svc, limit=5)
    for c in bench.cases(svc, "toolpick"):
        want = c["reference"]["must_include"]
        assert want == [c["task_id"]]
        assert bench.exact_score(want[0], want) == 1.0
        assert bench.exact_score("I would use some other tool", want) == 0.0


def test_a_near_miss_is_not_credited(tmp_path):
    """`list_tables` is not `SQLite__list_tables`: the boundary matching has to
    hold here or every prefixed tool scores for its own suffix."""
    assert bench.exact_score("list_tables", ["SQLite__list_tables"]) == 0.0
    assert bench.exact_score("SQLite__list_tables", ["SQLite__list_tables"]) == 1.0


def test_a_need_that_names_its_own_answer_is_dropped(tmp_path):
    """Otherwise the case measures copying, not choosing."""
    tools = [
        {"name": "web_search", "description": "Run a web search", "disabled": False},
        {"name": "render_frame", "description": "Draw a picture", "disabled": False},
        {"name": "third_tool", "description": "Do a third thing", "disabled": False},
        {"name": "fourth_tool", "description": "Do a fourth thing", "disabled": False},
        {"name": "fifth_tool", "description": "Do a fifth thing", "disabled": False},
    ]
    svc = _svc(tmp_path, tools)
    out = bench.build_toolpick(svc, limit=10)
    assert out["skipped"] >= 1
    kept = {c["task_id"] for c in bench.cases(svc, "toolpick")}
    assert "web_search" not in kept, "the need said 'web search'"


def test_a_registry_with_almost_nothing_in_it_is_refused(tmp_path):
    """Silently building a three-case suite would read as a model problem later."""
    with pytest.raises(ValueError):
        bench.build_toolpick(_svc(tmp_path, TOOLS[:2]), limit=10)


def test_the_cases_are_tagged_so_the_board_can_group_them(tmp_path):
    svc = _svc(tmp_path)
    bench.build_toolpick(svc, limit=3)
    for c in bench.cases(svc, "toolpick"):
        assert "toolpick" in c["tags"] and "tool_choice" in c["tags"]


def test_building_twice_gives_the_same_suite(tmp_path):
    """A benchmark that reshuffles itself cannot be compared with itself."""
    svc = _svc(tmp_path)
    bench.build_toolpick(svc, limit=5)
    first = [c["task_id"] for c in bench.cases(svc, "toolpick")]
    bench.build_toolpick(svc, limit=5)
    assert [c["task_id"] for c in bench.cases(svc, "toolpick")] == first


# -- the tag list --------------------------------------------------------------------

def test_the_traversal_is_shared_with_build():
    """Two copies of the corpus walk would be two things to keep in step, which
    is the fault this week has been a run of."""
    import inspect
    assert "_examples(" in inspect.getsource(bench.build)
    assert "_examples(" in inspect.getsource(bench.tag_counts)


def test_tag_counts_says_so_when_the_store_is_empty(tmp_path):
    svc = type("S", (), {"capture": type("C", (), {"data_dir": str(tmp_path)})(),
                         "db": Database(tmp_path / "t.sqlite")})()
    out = bench.tag_counts(svc)
    assert out["tags"] == [] and out["cases"] == 0
    assert "prepare" in out["note"]


def test_the_console_offers_the_tags_rather_than_a_free_text_box():
    html = open("foundry_router/ui/static/index.html", encoding="utf-8").read()
    i = html.index('id="bs-tags"')
    assert 'data-options="benchTags"' in html[i:i + 200]
    assert "_pickerOptions.benchTags" in html
    assert "bench/tags" in html

# -- the tag count is expensive, so it is asked for once (v0.181.202) --------------

def test_the_count_is_cached_against_the_store(tmp_path, monkeypatch):
    """It walks the whole prepared store - over two minutes on the live corpus -
    and the answer only changes when the dataset is prepared again."""
    store = tmp_path / "store"
    store.mkdir()
    (store / "normalized.sqlite").write_bytes(b"x")
    svc = type("S", (), {"db": Database(tmp_path / "t.sqlite")})()
    monkeypatch.setattr(bench, "_dirs_for_test", None, raising=False)

    walks = []

    def fake_examples(_store, _sessions, _tiers):
        walks.append(1)
        return iter(())

    import foundry_router.training_prep as tp
    monkeypatch.setattr(tp, "_dirs", lambda _s: {"store": store})
    monkeypatch.setattr(bench, "_examples", fake_examples)
    monkeypatch.setattr("foundry_training.store.TrainingStore",
                        lambda *a, **k: object())

    bench._TAGS_CACHE.clear()
    first = bench.tag_counts(svc)
    second = bench.tag_counts(svc)
    assert len(walks) == 1, "the second call walked the store again"
    assert first["cached"] is False and second["cached"] is True
    assert second["tags"] == first["tags"]

    # and asking for a refresh does walk it
    bench.tag_counts(svc, refresh=True)
    assert len(walks) == 2
    bench._TAGS_CACHE.clear()


def test_a_prepared_dataset_invalidates_the_cache(tmp_path, monkeypatch):
    store = tmp_path / "store2"
    store.mkdir()
    f = store / "normalized.sqlite"
    f.write_bytes(b"x")
    svc = type("S", (), {"db": Database(tmp_path / "t2.sqlite")})()
    walks = []
    import foundry_router.training_prep as tp
    monkeypatch.setattr(tp, "_dirs", lambda _s: {"store": store})
    monkeypatch.setattr(bench, "_examples",
                        lambda *a: (walks.append(1), iter(()))[1])
    monkeypatch.setattr("foundry_training.store.TrainingStore",
                        lambda *a, **k: object())
    bench._TAGS_CACHE.clear()
    bench.tag_counts(svc)
    os.utime(f, (1, 1))                       # the dataset was prepared again
    bench.tag_counts(svc)
    assert len(walks) == 2, "a new store must not serve the old count"
    bench._TAGS_CACHE.clear()


def test_the_cache_does_not_grow_without_bound(tmp_path, monkeypatch):
    """One dataset at a time; a map keyed by mtime would grow for ever."""
    store = tmp_path / "store3"
    store.mkdir()
    f = store / "normalized.sqlite"
    f.write_bytes(b"x")
    svc = type("S", (), {"db": Database(tmp_path / "t3.sqlite")})()
    import foundry_router.training_prep as tp
    monkeypatch.setattr(tp, "_dirs", lambda _s: {"store": store})
    monkeypatch.setattr(bench, "_examples", lambda *a: iter(()))
    monkeypatch.setattr("foundry_training.store.TrainingStore",
                        lambda *a, **k: object())
    bench._TAGS_CACHE.clear()
    for i in range(5):
        os.utime(f, (i + 1, i + 1))
        bench.tag_counts(svc)
    assert len(bench._TAGS_CACHE) == 1
    bench._TAGS_CACHE.clear()


def test_the_console_asks_for_the_tags_on_focus_not_on_render():
    """It was fired from the tab's render and the page hung for two minutes."""
    html = open("foundry_router/ui/static/index.html", encoding="utf-8").read()
    i = html.index('$("#bs-tags").addEventListener("focus"')
    block = html[i:i + 800]
    assert 'api("bench/tags")' in block
    # and nowhere else: the render path must not call it
    assert html.count('api("bench/tags")') == 1
