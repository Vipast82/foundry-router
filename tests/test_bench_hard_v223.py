"""v0.181.223: tiers that are measured, and difficulty as a dial.

Asked: go through the benchmarks and raise the difficulty as models improve -
hard should be hard for a frontier model, expert hard even for one to pass.

Read on the two finished profiles, both local models (three bits a weight)
passed every "hard" code case and 75-88% of "expert". Sent to Sonnet 5.5 and
Opus 5.5 with reasoning allowed, 16 stacked-semantics programs, 14 programs
whose names lie and every 64-step trace came back right; so did counting,
dynamic-programming and search problems up to level 4, worked by hand. What
they miss is size inside a fixed budget: 128-256-step traces (Sonnet 9/10,
Opus 6/10), a 9-item ordering count, a 10-stop random route. So the tiers are
the rungs of generated ladders, labelled by who misses them, and the bench
holds every backend to one budget - the Claude backend ignored max_tokens and
one reply ran to 84,485 tokens.
"""
import asyncio
import inspect
import json
from pathlib import Path

import pytest

import foundry_router
from foundry_router import bench, benchcard, benchhard as bh
from foundry_router.db import Database
from foundry_router.pool.protocols import ChatResult


def _svc(tmp_path, pool=None):
    cfg = type("Cfg", (), {"code_checks": None})()
    return type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "r.sqlite"), "pool": pool,
        "config_store": type("CS", (), {"config": cfg})(),
        "registry": type("R", (), {"get": staticmethod(lambda m: None)})(),
    })()


class _Pool:
    """Answers from a function of the prompt, and remembers what it was asked."""

    def __init__(self, answer):
        self.answer, self.asked = answer, []

    async def chat(self, model, messages, tools=None, max_tokens=0, options=None):
        self.asked.append(max_tokens)
        out = self.answer(messages[-1]["content"])
        return (out if isinstance(out, ChatResult) else
                ChatResult(content=out, finish_reason="stop", completion_tokens=50)), "b"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(bench, "_record", lambda *a, **k: None)
    monkeypatch.setattr(bench, "_state", {"state": "idle"})


# -- only the closing line is the answer ------------------------------------------------

def test_the_final_line_is_what_a_reply_settled_on():
    assert bench.final_answer("step 1: (0, 1)\nstep 2: (4, 2)\nANSWER: (4, 2)") == "(4, 2)"
    assert bench.final_answer("so far 12\n**Answer:** `[1, 2]`") == "[1, 2]"
    assert bench.final_answer("ANSWER: 3\nno wait\nANSWER: 5") == "5", "the last one"
    assert bench.final_answer("it prints\n42") == "42", "no marker: its last line"
    assert bench.final_answer("") == ""


def test_a_value_only_passed_through_on_the_way_does_not_score(tmp_path):
    case = {"reference": {"py_value": "(4, 2)", "final": True}}
    svc = _svc(tmp_path)
    wrong = "after two steps it is `(4, 2)`, then one more\nANSWER: `(5, 3)`"
    right = "first (5, 3), which is not final\nANSWER: (4, 2)"
    assert asyncio.run(bench.score_one(svc, case, {"content": wrong}))["score"] == 0.0
    assert asyncio.run(bench.score_one(svc, case, {"content": right}))["score"] == 100.0
    # without the flag the whole reply is read, as every old case still is
    old = {"reference": {"py_value": "(4, 2)"}}
    assert asyncio.run(bench.score_one(svc, old, {"content": wrong}))["score"] == 100.0


def test_a_decoy_ruled_out_in_the_working_is_not_a_miss(tmp_path):
    case = {"reference": {"must_include": ["481902"], "must_exclude": ["773310"],
                          "final": True}}
    said = "773310 is for LOB-71, the other sprint, so not that one.\nANSWER: 481902"
    assert asyncio.run(bench.score_one(_svc(tmp_path), case, {"content": said}))["score"] == 100.0


# -- one budget for every backend ----------------------------------------------------------

def _hard_case():
    return {"messages": [{"role": "user", "content": "q"}], "max_tokens": bench.THINK_ROOM,
            "reference": {"py_value": "5", "final": True}}


def test_a_hard_case_is_asked_with_its_own_budget(tmp_path):
    pool = _Pool(lambda q: "ANSWER: 5")
    got = asyncio.run(bench.ask(_svc(tmp_path, pool), "m", _hard_case()))
    assert pool.asked == [bench.THINK_ROOM] and got["content"] == "ANSWER: 5"


def test_a_reply_past_the_budget_is_out_of_room_whatever_it_said(tmp_path):
    """The Claude backend's 84,485-token reply to a 16,000-token case."""
    pool = _Pool(lambda q: ChatResult(content="ANSWER: 5", finish_reason="stop",
                                      completion_tokens=84485))
    svc = _svc(tmp_path, pool)
    got = asyncio.run(bench.ask(svc, "claude", _hard_case()))
    assert got["out_of_room"] == bench.THINK_ROOM and got["content"] == ""
    assert asyncio.run(bench.score_one(svc, _hard_case(), got))["score"] == 0.0
    assert pool.asked == [bench.THINK_ROOM], "not asked again: it had its budget"


def test_the_budget_has_a_little_give_and_an_old_case_gets_the_retry_room(tmp_path):
    within = ChatResult(content="ANSWER: 5", completion_tokens=int(bench.THINK_ROOM * 1.05))
    assert not bench.over_budget(within, bench.THINK_ROOM)
    assert bench.over_budget(ChatResult(content="x", completion_tokens=9100), bench.THINK_ROOM)
    # a 2,048-token case may be answered in up to THINK_ROOM by a backend that
    # ignores the cap: a backend that honours it gets the same on its second ask
    pool = _Pool(lambda q: ChatResult(content="[1, 2]", finish_reason="stop",
                                      completion_tokens=5000))
    got = asyncio.run(bench.ask(_svc(tmp_path, pool), "claude",
                                {"messages": [{"role": "user", "content": "q"}]}))
    assert got["content"] == "[1, 2]" and got["out_of_room"] == 0


def test_a_hard_case_that_takes_too_long_is_out_of_time(tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "CASE_SECONDS", 0.05)

    class Slow:
        async def chat(self, *a, **k):
            await asyncio.sleep(5)

    svc = _svc(tmp_path, Slow())
    got = asyncio.run(bench.ask(svc, "m", _hard_case()))
    assert got["timed_out"] and got["out_of_room"] and got["content"] == ""
    row = {"score": 0.0, "error": "", "tags": '["codereason"]',
           "code_detail": "timed_out=300 out_of_room=8192", "tools_expected": "[]",
           "tools_called": "[]"}
    assert benchcard.miss_kind("codereason", row) == "ran out of time before answering"


# -- what is in the suite --------------------------------------------------------------------

@pytest.fixture(scope="module")
def specs():
    return bh.code_specs(lambda src: bench._run_snippet(src, "result"))


def test_every_answer_is_what_the_program_gives_and_none_is_guessable(specs):
    assert len(specs) == 75
    for sp in specs:
        assert bench._run_snippet(sp["src"], "result") == sp["value"], sp["key"]
        assert not bh._trivial(sp["value"]), sp["key"]
    assert len({sp["key"] for sp in specs}) == len(specs)


def test_a_rebuild_is_the_same_suite(specs):
    again = bh.code_specs(lambda src: bench._run_snippet(src, "result"))
    assert [(s["key"], s["src"]) for s in again] == [(s["key"], s["src"]) for s in specs]


def test_a_ladder_climbs_in_order_and_never_gets_easier(specs):
    order = list(bench.TIERS)
    for ladder, (first, _tiers) in bh.LADDERS.items():
        rungs = [s for s in specs if s["ladder"] == ladder]
        assert [s["level"] for s in rungs] == list(range(first, bh.TOP_LEVEL + 1)), ladder
        ranks = [order.index(s["tier"]) for s in rungs]
        assert ranks == sorted(ranks), ladder
        # every level is a bigger problem than the one below it
        sizes = [len(s["src"]) for s in rungs]
        assert sizes[-1] > sizes[0], ladder
    assert {s["tier"] for s in specs} == set(order), "every tier has cases"


def test_the_top_is_one_number_away(specs):
    more = bh.code_specs(lambda src: bench._run_snippet(src, "result"), bh.TOP_LEVEL + 1)
    assert len(more) == len(specs) + len(bh.LADDERS)
    top = [s for s in more if s["level"] == bh.TOP_LEVEL + 1]
    assert all(s["tier"] in ("hard", "expert") for s in top)
    # a level past a size table still grows
    assert bh._size((4, 5, 7, 9), 6) == 13 and bh.trace_steps(7) == 512


def test_the_old_snippets_are_labelled_by_what_was_measured(tmp_path):
    svc = _svc(tmp_path)
    out = bench.build_codereason(svc)
    assert out["tiers"] == {"easy": 70, "medium": 26, "hard": 14, "expert": 9}
    assert out["top_level"] == bh.TOP_LEVEL
    cases = bench.cases(svc, "codereason")
    old = [c for c in cases if not any(t.startswith("family:") for t in c["tags"])]
    assert len(old) == 44
    assert {t for c in old for t in c["tags"] if t.startswith("tier:")} == \
        {"tier:easy", "tier:medium"}, "no old snippet is hard for anything measured"
    hard = [c for c in cases if "tier:hard" in c["tags"] or "tier:expert" in c["tags"]]
    assert all(c["max_tokens"] == bench.THINK_ROOM and c["reference"]["final"]
               and "ANSWER:" in c["messages"][-1]["content"] for c in hard)


def test_every_tier_counts_the_same_in_the_score(tmp_path):
    svc = _svc(tmp_path)
    bench.build_codereason(svc)
    cases = bench.cases(svc, "codereason")
    share: dict = {}
    for c in cases:
        t = next(x for x in c["tags"] if x.startswith("tier:"))
        share[t] = share.get(t, 0) + c["weight"]
    assert len(set(round(v, 3) for v in share.values())) == 1
    assert round(sum(share.values()), 3) == len(cases)
    # a suite with no tiers, or one, is weighted as before
    assert bench.tier_weights([{"tags": ["x"]}, {"tags": ["y"]}]) == {}


def _suite(svc, cases):
    f = bench._dir(svc) / "codereason.jsonl"
    f.write_text("".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")


def _case(cid, want, tier, weight=None, ladder="", level=0):
    c = {"id": cid, "task_id": cid, "tags": ["codereason", f"tier:{tier}"],
         "messages": [{"role": "user", "content": cid}], "tools": [],
         "max_tokens": bench.THINK_ROOM,
         "reference": {"py_value": repr(want), "final": True}}
    if weight:
        c["weight"] = weight
    if ladder:
        c["tags"] += [f"ladder:{ladder}", f"level:{level}"]
    return c


def test_a_run_is_scored_by_tier_not_by_head_count(tmp_path):
    cases = [_case(f"e{i}", 1, "easy") for i in range(6)] + [_case("x", 9, "expert")]
    for c in cases:
        c["weight"] = bench.tier_weights(cases)[c["tags"][1].split(":")[1]]
    pool = _Pool(lambda q: "ANSWER: 1")            # every easy one right, the expert wrong
    svc = _svc(tmp_path, pool)
    _suite(svc, cases)
    out = asyncio.run(bench._run(svc, "codereason", "m", "", 0))
    assert out["score"] == 50.0, "6 of 7 right, and half the tiers"


# -- a ladder stops being climbed -------------------------------------------------------------

def test_two_misses_in_a_row_close_a_ladder_and_the_rest_are_not_sent(tmp_path):
    cases = [_case(f"L{n}", n, "medium", ladder="sat", level=n) for n in range(1, 7)]
    cases += [_case("other", 7, "easy")]
    right = {"L1": 1, "other": 7}                 # passes level 1, then misses 2 and 3
    pool = _Pool(lambda q: f"ANSWER: {right.get(q, -1)}")
    svc = _svc(tmp_path, pool)
    _suite(svc, cases)
    out = asyncio.run(bench._run(svc, "codereason", "m", "", 0))
    assert len(pool.asked) == 4, "levels 1-3 and the case that is on no ladder"
    rows = {r["case_id"]: r for r in svc.db.query(
        "SELECT * FROM bench_results WHERE run_id=?", (out["run_id"],))}
    for cid in ("L4", "L5", "L6"):
        assert rows[cid]["score"] == 0.0 and "not_sent=1 stopped_at=3" in rows[cid]["code_detail"]
        assert benchcard.miss_kind("codereason", rows[cid]) == \
            "above the level it stopped passing (not sent)"
    assert rows["other"]["score"] == 100.0, "another case is not held back"
    assert out["scored"] == 7


def test_one_miss_does_not_close_a_ladder(tmp_path):
    """These runs move about five points between identical runs."""
    cases = [_case(f"L{n}", n, "medium", ladder="route", level=n) for n in range(1, 6)]
    right = {"L1": 1, "L3": 3, "L4": 4}           # misses 2, recovers, misses 5
    pool = _Pool(lambda q: f"ANSWER: {right.get(q, -1)}")
    svc = _svc(tmp_path, pool)
    _suite(svc, cases)
    asyncio.run(bench._run(svc, "codereason", "m", "", 0))
    assert len(pool.asked) == 5


def test_the_card_says_how_far_up_each_ladder_it_got():
    def row(ladder, level, score, family="search"):
        return {"score": score, "error": "", "code_detail": "",
                "tags": json.dumps(["codereason", f"tier:hard", f"family:{family}",
                                    f"ladder:{ladder}", f"level:{level}"]),
                "tools_expected": "[]", "tools_called": "[]"}
    rows = [row("sat", 2, 100.0), row("sat", 3, 100.0), row("sat", 4, 0.0), row("sat", 5, 0.0),
            row("trace:grid", 1, 0.0, "trace"), row("trace:grid", 2, 0.0, "trace"),
            {"score": 100.0, "error": "", "code_detail": "", "tags": '["codereason","tier:easy"]',
             "tools_expected": "[]", "tools_called": "[]"}]
    f = {x["name"]: x["rows"] for x in benchcard.facets("codereason", rows)}
    reach = {r["label"]: r for r in f["How far up each ladder"]}
    assert reach["sat: level 3 of 5"]["passed"] == 2 and reach["sat: level 3 of 5"]["score"] == 50.0
    assert "grid: none of 2" in reach
    kinds = [r["label"] for r in f["Kind of problem"]]
    assert kinds == ["short snippets", "long traces", "too big to trace"]


# -- the route and the control ------------------------------------------------------------------

def test_the_build_takes_a_top_level_from_the_page():
    from foundry_router.ui import routes
    assert 'int(body.get("top_level") or 0)' in inspect.getsource(routes.bench_codereason)
    page = (Path(foundry_router.__file__).parent / "ui" / "static" / "index.html"
            ).read_text(encoding="utf-8")
    assert 'id="bench-code-top"' in page and 'top_level: +($("#bench-code-top")' in page


def test_the_route_builds_the_suite_it_is_asked_for(tmp_path):
    """The function behind the button, called the way the page calls it."""
    from foundry_router.ui import routes
    svc = _svc(tmp_path)

    class Req:
        app = type("A", (), {"state": type("St", (), {"svc": svc})()})()

        async def json(self):
            return {"top_level": 4}

    import unittest.mock as um
    with um.patch.object(routes, "_svc", lambda request: svc):
        out = asyncio.run(routes.bench_codereason(Req()))
    assert out["ok"] and out["top_level"] == 4
    assert out["cases"] == 44 + 16 + sum(4 - first + 1 for first, _t in bh.LADDERS.values())


def test_nothing_a_model_writes_is_run():
    """benchhard hands back source and a value; the only thing ever executed is
    its own tables, at build time, through the runner it is given."""
    src = inspect.getsource(bh)
    assert "exec(" not in src and "eval(" not in src and "import bench" not in src
