"""v0.181.218: accurate, and not too easy.

Reading flash's 93.0 profile case by case: the needle (90/90) and tool-picking
(40/40) suites were at the ceiling, code (43/44) and the finder (38/40) near
it; and of the replay's 23 misses about 17 were the test - ten end-of-turn
reports cut away from the context that made "report now" right, and seven
inspections scored as the wrong tool because `bash cat x` is not `read x` by
name. The card also listed "Long-context recall: expert" as a strength of a
suite at its ceiling, and the live ETA put a five-hour profile at fourteen.
"""
import asyncio
import json

from foundry_router import bench, benchcard, benchprofile as bp
from foundry_router.db import Database


def T(name, args=None):
    return {"name": name, "arguments": args or {}}


# -- the replay: looking is looking ----------------------------------------------------

def test_inspection_moves_are_one_kind_of_move():
    for call in (T("read", {"filePath": "x"}), T("glob", {"pattern": "*.lua"}),
                 T("bash", {"command": "cat src/a.lua"}), T("bash", {"command": "git status"}),
                 T("bash", {"command": "sed -n 1,40p x.lua"}), T("blender-mcp_get_scene_info"),
                 T("blender-mcp_get_addon_status"), T("memory__read_graph"),
                 T("Roblox_Studio_get_studio_state"), T("playwright__browser_snapshot")):
        assert bench.move_kind(call) == "look", call
    for call in (T("bash", {"command": "python build.py > out.txt"}),
                 T("bash", {"command": "git commit -m x"}), T("bash", {"command": "npm test"}),
                 T("edit"), T("blender-mcp_execute_blender_code"),
                 T("Roblox_Studio_generate_mesh")):
        assert bench.move_kind(call) != "look", call


def test_a_look_where_the_session_looked_is_nearly_full_marks():
    assert bench.tool_score([T("read", {"filePath": "x"})],
                            [T("bash", {"command": "cat x"})]) == 0.9
    assert bench.tool_score([T("read", {"filePath": "x"})], [T("read")]) == 1.0
    # a look is not an edit, and running the build is not a look
    assert bench.tool_score([T("edit")], [T("bash", {"command": "cat x"})]) == 0.0
    assert bench.tool_score([T("bash", {"command": "blender -b -P export.py"})],
                            [T("blender-mcp_get_scene_info")]) == 0.0


def test_a_missing_move_still_costs_and_a_reorder_is_still_a_reorder():
    assert bench.tool_score([T("search"), T("read")], [T("search")]) == 0.65
    assert bench.tool_score([T("search"), T("read")], [T("read"), T("search")]) == 0.7


def test_a_words_only_turn_is_not_a_replay_case_but_narration_then_a_call_is():
    ex = {"messages": [
        {"role": "system", "content": "s"}, {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "Let me look."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "content": "ok"},
        {"role": "assistant", "content": "Done.\n```coordinator-report\n{}\n```"}],
          "tools": [], "meta": {"task_id": "T"}}
    cases = bench.cases_from(ex)
    assert cases and all(c["reference"]["tool_calls"] for c in cases)


def test_a_replay_answer_says_what_each_call_was_called_with():
    a = bench.answer_of({"tool_calls": [T("bash", '{"command": "cat src/a.lua"}'),
                                        T("read", {"filePath": "docs/PLAN.md"})]})
    assert a == "called bash(cat src/a.lua), read(docs/PLAN.md)"


# -- the needle: three harder kinds ------------------------------------------------------

def _hay():
    return bench._haystack_text(type("S", (), {})(), 20000)


def test_the_harder_needles_hold_what_they_ask_for():
    hay = _hay()
    for make in (bench._multi_case, bench._updated_case, bench._paraphrase_case):
        c = make(hay, 16000, 0)
        body = c["messages"][1]["content"]
        ref = c["reference"]
        assert all(x in body for x in ref["must_include"] + ref["must_exclude"]), c["id"]
        assert not any(t.startswith("depth:") for t in c["tags"]), "kept out of the grid"
        assert "tier:hard" in c["tags"], "one step harder than a plain 16k needle"
        assert any(t.startswith("tokens:") for t in c["tags"])


def test_three_needles_score_by_the_share_found():
    c = bench._multi_case(_hay(), 16000, 0)
    codes = c["reference"]["must_include"]
    assert bench.exact_score(", ".join(codes), codes) == 1.0
    assert round(bench.exact_score(", ".join(codes[:2]), codes), 2) == 0.67


def test_naming_the_superseded_code_scores_nothing():
    c = bench._updated_case(_hay(), 16000, 0)
    new, old = c["reference"]["must_include"][0], c["reference"]["must_exclude"][0]
    ref = c["reference"]
    assert bench.exact_score(new, ref["must_include"], ref["must_exclude"]) == 1.0
    assert bench.exact_score(old, ref["must_include"], ref["must_exclude"]) == 0.0
    assert bench.exact_score(f"{new} (was {old})", ref["must_include"], ref["must_exclude"]) == 0.0


def test_the_paraphrased_question_shares_no_content_word_with_its_note():
    for said, asked in bench._PARAPHRASES:
        stop = {"the", "a", "an", "of", "to", "and", "by", "their", "each", "where", "is",
                "about", "that", "how", "two", "lets", "makes", "properly", "screen"}
        a = {w for w in said.lower().replace("-", " ").split() if w not in stop}
        b = {w for w in asked.lower().replace("-", " ").split() if w not in stop}
        assert not (a & b), (said, asked)


def test_every_length_gets_one_of_each_harder_kind(tmp_path):
    svc = type("S", (), {"capture": type("C", (), {"data_dir": str(tmp_path)})(),
                         "db": Database(tmp_path / "n.sqlite")})()
    bench.build_needle(svc, per_cell=1, depths=[50], lengths=[4000, 16000], decoys=0)
    kinds = [next((k for k in ("multi", "updated", "paraphrase") if k in c["tags"]), None)
             for c in bench.cases(svc, "needle")]
    assert kinds.count("multi") == kinds.count("updated") == kinds.count("paraphrase") == 2


# -- tool picking: sometimes nothing fits -------------------------------------------------

def _pick_svc(tmp_path):
    rows = [{"name": n, "description": d} for n, d in (
        ("roblox_get_luau_global", "Get detailed information about one Luau global"),
        ("roblox_get_luau_globals", "List every Luau global function and type"),
        ("generate_image", "Create an image from a text prompt"),
        ("searxng_web_search", "Search the public web and return links"),
        ("media_list_jobs", "List the most recent media jobs"),
        ("playwright__browser_click", "Click an element on a web page"))]
    return type("S", (), {"capture": type("C", (), {"data_dir": str(tmp_path)})(),
                          "db": Database(tmp_path / "t.sqlite"),
                          "tool_registry": type("R", (), {"status": staticmethod(lambda: rows)})()})()


def test_some_needs_have_no_tool_and_the_answer_is_none(tmp_path):
    svc = _pick_svc(tmp_path)
    bench.build_toolpick(svc)
    cases = bench.cases(svc, "toolpick")
    none = [c for c in cases if "none-fits" in c["tags"]]
    assert len(none) == len(bench.NONE_NEEDS)
    assert "answer none" in none[0]["messages"][0]["content"]
    ref = none[0]["reference"]
    assert bench.exact_score("none", ref["must_include"]) == 1.0
    assert bench.exact_score("searxng_web_search", ref["must_include"]) == 0.0


def test_a_tool_with_a_near_twin_is_marked(tmp_path):
    svc = _pick_svc(tmp_path)
    bench.build_toolpick(svc)
    tagged = {c["task_id"] for c in bench.cases(svc, "toolpick") if "lookalike" in c["tags"]}
    assert {"roblox_get_luau_global", "roblox_get_luau_globals"} & tagged
    assert "generate_image" not in tagged


# -- the card ------------------------------------------------------------------------------

def _row(**k):
    base = {"score": 0.0, "error": "", "tags": "[]", "tools_expected": "[]",
            "tools_called": "[]", "code_detail": "", "expected": "", "answer": ""}
    base.update(k)
    return base


def test_the_new_misses_are_named():
    assert benchcard.miss_kind("baseline", _row(score=90.0, tools_expected='["read"]',
                                                tools_called='["bash"]')) \
        == "the same move with another tool"
    assert benchcard.miss_kind("toolpick", _row(tags='["toolpick","none-fits"]',
                                                answer="searxng_web_search")) \
        == "named a tool where none fits"
    assert benchcard.miss_kind("toolpick", _row(tags='["toolpick"]', answer="none")) \
        == "said none where a tool fits"
    assert benchcard.miss_kind("needle", _row(
        tags='["needle","updated"]', answer="394788",
        expected="432105 (not the decoy 394788 for MV2-60)")) == "gave the superseded code"
    assert benchcard.miss_kind("needle", _row(tags='["needle","multi"]')) \
        == "missed some of the three codes"


def test_a_ceiling_suites_parts_are_not_listed_as_strengths():
    card = {"parts": [{"suite": "needle", "title": "Long-context recall", "score": 100.0,
                       "ceiling": True, "facets": [{"name": "Difficulty", "rows": [
                           {"label": "expert", "n": 21, "passed": 21, "score": 100.0}]}]}]}
    v = benchcard.verdict(card)
    assert v["strengths"] == [] and v["ceiling"] == ["Long-context recall"]


def test_each_suite_to_come_is_scaled_by_the_most_similar_suite_done(tmp_path, monkeypatch):
    """The brain lane was 13x flash on the 60k-token replay and about 2x on the
    short suites; one ratio for all of them made five hours into fourteen."""
    from foundry_router import bench as b
    cfg = type("C", (), {"bench": type("B", (), {"suite_weights": {}})()})()
    svc = type("S", (), {"db": Database(tmp_path / "e.sqlite"),
                         "capture": type("X", (), {"data_dir": str(tmp_path)})(),
                         "config_store": type("CS", (), {"config": cfg})()})()
    for suite, n in (("baseline", 10), ("codereason", 10), ("toolpick", 10)):
        (b._dir(svc) / f"{suite}.jsonl").write_text("{}\n" * n, encoding="utf-8")
    for suite, flash, brain in (("baseline", 15.0, 200.0), ("codereason", 3.0, 6.0)):
        for model, sec in (("flash", flash), ("brain", brain)):
            svc.db.execute("INSERT INTO bench_runs (ts, suite, model, cases, scored, score, "
                           "seconds, status) VALUES (datetime('now'), ?, ?, 10, 10, 90, ?, 'done')",
                           (suite, model, sec))
    svc.db.execute("INSERT INTO bench_runs (ts, suite, model, cases, scored, score, seconds, "
                   "status) VALUES (datetime('now'), 'toolpick', 'flash', 10, 10, 90, 4.0, 'done')")
    now = 1000.0
    monkeypatch.setattr(b, "_state", {"state": "running", "suite": "codereason", "model": "brain",
                                      "cases": 10, "done": 10, "started": now - 60.0,
                                      "case_started": now})
    monkeypatch.setattr(bp, "_state", {"state": "running", "suite": "codereason", "done": 1,
                                       "suites": ["baseline", "codereason", "toolpick"],
                                       "total": 3, "repeats": 1, "rep": 1})
    pe = benchcard.live(svc, now=now)["profile_eta"]
    # toolpick (flash 4 s a case) is like codereason (3 s, brain 2x), not the
    # replay (15 s, brain 13x): 10 cases x 4 s x 2 = 80 s, not ~530
    assert pe["remaining"] == ["toolpick"]
    assert 70 <= pe["eta_s"] <= 90, pe
