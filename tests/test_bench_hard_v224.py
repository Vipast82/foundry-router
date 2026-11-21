"""v0.181.224: long-context recall and tool picking, tiered by measurement.

Both suites were at the ceiling for both local models (recall 100.0 and 99.2,
tool picking 100.0 and 100.0). Sent to qwen3.8-flash, Sonnet 5.5 and Opus 5.5:

* following a chain of notes, finding the note in force on a date and comparing
  totals are solved at every size tried (frontier 23 of 24 at levels 3-7, top to
  level 10);
* adding up a ledger and working out the status of many sprints separate the
  models by size: flash passes level 6 and misses 8, Opus passes level 10 where
  Sonnet runs over the budget, and both fail levels 11 and 12;
* tool picking has no hard tier: Sonnet and Opus chose right in all 28 cases, to
  1,200 look-alike tools and 160 rivals, and flash in 10 of 11 (it misses the
  160-rival rung, the suite's one medium case). One case both Claude models
  "missed" was the key's fault - "without touching the graphics card" fitted
  two values.

Every answer below is worked out again here, a second way, from the notes.
"""
import asyncio
import inspect
import json
import re
from pathlib import Path

import pytest

import foundry_router
from foundry_router import bench, benchcard, benchhard as bh
from foundry_router.db import Database
from foundry_router.pool.protocols import ChatResult


def _svc(tmp_path, pool=None, ctx=None):
    cfg = type("Cfg", (), {"code_checks": None})()
    return type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "r.sqlite"), "pool": pool,
        "config_store": type("CS", (), {"config": cfg})(),
        "registry": type("R", (), {"get": staticmethod(
            lambda m: {"context_length": ctx} if ctx else None)})(),
    })()


LEVELS = (1, 3, 6, 9, 12)


def _notes(sp):
    return [n for _pct, n in sp["notes"]]


# -- the long-context answers, worked out a second way ---------------------------------

@pytest.mark.parametrize("level", LEVELS)
def test_a_chain_ends_where_following_it_ends(level):
    sp = bh.context_spec("chain", level, 0)
    same, plus, code = {}, {}, {}
    for n in _notes(sp):
        if m := re.search(r"sprint (\S+) uses the same verification code as sprint (\S+)\.", n):
            same[m[1]] = m[2]
        elif m := re.search(r"code for sprint (\S+) is sprint (\S+)'s code plus (\d+)\.", n):
            plus[m[1]] = (m[2], int(m[3]))
        elif m := re.search(r"code for sprint (\S+) is (\d+)\.", n):
            code[m[1]] = int(m[2])
    at, add = sp["task"], 0
    while at not in code:
        if at in same:
            at = same[at]
        else:
            at, d = plus[at]
            add += d
    assert sp["want"] == [str(code[at] + add)]
    assert str(code[at] + add) not in sp["avoid"]


@pytest.mark.parametrize("level", LEVELS)
def test_the_code_in_force_is_the_latest_on_or_before_the_day(level):
    sp = bh.context_spec("asof", level, 0)
    when = re.search(r"on (\d{4}-\d\d-\d\d)\?", sp["question"])[1]
    mine = sorted((m[1], m[2]) for n in _notes(sp)
                  if (m := re.search(rf"dated (\S+): the verification code for sprint "
                                     rf"{re.escape(sp['task'])} was set to (\d+)", n)))
    before = [c for d, c in mine if d <= when]
    assert before, "the day asked about is never before the first note"
    assert sp["want"] == [before[-1]] and before[-1] not in sp["avoid"]
    assert len(before) < len(mine), "and never after the last: the newest is not the answer"


@pytest.mark.parametrize("level", LEVELS)
def test_blocked_means_the_latest_note_says_so(level):
    sp = bh.context_spec("blocked", level, 0)
    last: dict = {}
    for n in _notes(sp):
        m = re.search(r"dated (\S+): sprint (\S+) is (\w+)\.", n)
        if m[2] not in last or m[1] > last[m[2]][0]:
            last[m[2]] = (m[1], m[3])
    blocked = {s for s, (_d, st) in last.items() if st == "BLOCKED"}
    assert set(sp["want"]) == blocked and set(sp["avoid"]) == set(last) - blocked
    assert blocked and set(last) - blocked, "never all of them, never none"


@pytest.mark.parametrize("level", LEVELS)
def test_a_ledger_adds_up_and_the_top_spender_is_the_top(level):
    sp = bh.context_spec("ledger", level, 0)
    total = sum(int(m[1]) for n in _notes(sp)
                if (m := re.search(rf"sprint {re.escape(sp['task'])} spent (\d+) credits", n)))
    assert sp["want"] == [str(total)]
    sp = bh.context_spec("top", level, 0)
    spent: dict = {}
    for n in _notes(sp):
        m = re.search(r"sprint (\S+) spent (\d+) credits", n)
        spent[m[1]] = spent.get(m[1], 0) + int(m[2])
    best = max(spent, key=spent.get)
    assert sp["want"] == [best, str(spent[best])]
    assert sorted(spent.values())[-1] != sorted(spent.values())[-2], "never a tie"


def test_no_sprint_id_sits_inside_another():
    """AS-29 inside VR-AS-29 would let the scorer find a wrong id in a right one."""
    for a in bh._PREFIXES:
        for b in bh._PREFIXES:
            assert a == b or a not in b, (a, b)


def test_a_level_is_a_bigger_problem_and_a_rebuild_is_the_same_one():
    for kind in bh.CONTEXT_KINDS:
        sizes = [len(bh.context_spec(kind, n, 0)["notes"]) for n in (2, 6, 10, 13)]
        assert sizes == sorted(sizes) and sizes[-1] > sizes[0], kind
        assert bh.context_spec(kind, 5, 0) == bh.context_spec(kind, 5, 0)
        assert bh.context_spec(kind, 5, 0)["want"] != bh.context_spec(kind, 5, 1)["want"]


# -- the look-alike catalogue ------------------------------------------------------------

def _matches(desc: str, combo: tuple) -> bool:
    return all(axis[i][1] in desc for axis, i in zip(bh._AXES, combo))


def _target(sp) -> tuple:
    """The combination the request asks for, read back from its wording."""
    return tuple(next(i for i, v in enumerate(axis) if v[2] in sp["need"]) for axis in bh._AXES)


@pytest.mark.parametrize("level", (1, 3, 5))
def test_exactly_one_tool_fits_or_none_does(level):
    fit = bh.pick_spec("fit", level, 0)
    hits = [n for n, d in fit["tools"] if _matches(d, _target(fit))]
    assert hits == [fit["want"]], "one tool meets every part"
    assert len(fit["avoid"]) == bh._size(bh._PICK_TWINS, level)
    none = bh.pick_spec("none", level, 0)
    assert none["want"] == "none"
    assert not [n for n, d in none["tools"] if _matches(d, _target(none))]
    # ... while a sibling meets all but one of them
    near = dict(none["tools"])[none["avoid"][0]]
    assert sum(axis[i][1] in near for axis, i in zip(bh._AXES, _target(none))) == 4
    assert len({n for n, _d in fit["tools"]}) == len(fit["tools"]) == bh._PICK_SIZES[level - 1]


@pytest.mark.parametrize("level", (2, 5, 7))
def test_best_is_the_cheapest_that_can_take_the_file(level):
    sp = bh.pick_spec("best", level, 0)
    size = int(re.search(r"The file is (\d+) MB", sp["need"])[1])
    rivals = []
    for n, d in sp["tools"]:
        if _matches(d, _target(sp)):
            mb, cost = (int(x) for x in re.search(r"up to (\d+) MB\. (\d+) credits", d).groups())
            if mb >= size:
                rivals.append((cost / mb if level >= bh.PICK_RATIO_FROM else cost, n))
    assert len(rivals) >= 1 and min(rivals)[1] == sp["want"]
    assert sorted(rivals)[0][0] != (sorted(rivals) + [(None, "")])[1][0], "one winner"
    assert ("lowest price per megabyte" in sp["need"]) == (level >= bh.PICK_RATIO_FROM)


def test_a_request_phrase_fits_one_value_only():
    """Both Claude models named an edge-node tool for "without touching the
    graphics card", and they were right: the key said none."""
    for axis in bh._AXES:
        said = [v[2] for v in axis]
        assert len(set(said)) == len(said)
        for a in said:
            assert sum(a in b for b in said) == 1, a
    where = {v[0]: v[2] for v in bh._WHERE}
    assert "not its graphics card" in where["cpu"] and "processor" in where["cpu"]


# -- in the suites -------------------------------------------------------------------------

def test_the_recall_suite_keeps_its_grid_and_adds_the_ladders(tmp_path):
    svc = _svc(tmp_path)
    out = bench.build_needle(svc, lengths=(4000, 16000), per_cell=1)
    assert out["tiers"] == {"easy": 31, "medium": 7, "hard": 4, "expert": 8}
    cases = bench.cases(svc, "needle")
    grid = [c for c in cases if any(t.startswith("depth:") for t in c["tags"])]
    assert len(grid) == 10 and all("tier:easy" in c["tags"] for c in grid)
    ladder = [c for c in cases if any(t.startswith("ladder:") for t in c["tags"])]
    assert len(ladder) == 28
    for c in ladder:
        assert c["max_tokens"] == bench.THINK_ROOM and c["reference"]["final"]
        body = c["messages"][1]["content"]
        if "ctx-asof" in c["tags"] or "ctx-blocked" in c["tags"]:
            # the answer is quoted in the notes; the other kinds compute theirs
            assert all(w in body for w in c["reference"]["must_include"]), c["id"]
        assert all(w in body for w in c["reference"]["must_exclude"][:5]) \
            or "ctx-chain" in c["tags"], c["id"]
        assert not any(t.startswith("depth:") for t in c["tags"]), "kept out of the grid"
    # each ladder is in level order, and its tiers never go down
    order = list(bench.TIERS)
    by: dict = {}
    for c in ladder:
        lad, lvl = bench.ladder_of(c)
        by.setdefault(lad, []).append((lvl, order.index(benchcard._tag(c["tags"], "tier"))))
    for lad, rungs in by.items():
        assert rungs == sorted(rungs), lad
    share: dict = {}
    for c in cases:
        t = benchcard._tag(c["tags"], "tier")
        share[t] = round(share.get(t, 0) + c["weight"], 3)
    assert max(share.values()) - min(share.values()) < 0.01, "every tier counts the same"


def test_the_top_level_adds_rungs_only_to_the_ladders_that_climb(tmp_path):
    base = bh.context_specs()
    more = bh.context_specs(bh.CONTEXT_TOP + 1)
    assert len(more) == len(base) + 4, "blocked and ledger, two sets each"
    assert all(s["tier"] == "expert" for s in more if s["level"] == bh.CONTEXT_TOP + 1)


def test_the_tool_suite_keeps_the_live_catalogue_and_adds_look_alikes(tmp_path):
    svc = _svc(tmp_path)
    svc.tool_registry = type("R", (), {"status": staticmethod(lambda: [
        {"name": f"srv__tool_{i}", "description": f"Does thing number {i} to files of kind {i}"}
        for i in range(12)])})()
    out = bench.build_toolpick(svc, limit=5)
    assert out["tiers"] == {"easy": 23, "medium": 1}
    assert [c["id"] for c in bench.cases(svc, "toolpick")
            if "tier:medium" in c["tags"]] == ["PICK-best-L9"], "the one rung flash misses"
    cases = bench.cases(svc, "toolpick")
    # one medium case is not a tier to weigh: it would be half the score
    assert all("weight" not in c for c in cases)
    assert bench.tier_weights(cases) == {} and bench.MIN_TIER == 4
    live = [c for c in cases if "lookalikes" not in c["tags"]]
    assert len(live) == 13 and all("tier:easy" in c["tags"] for c in live)
    fake = [c for c in cases if "lookalikes" in c["tags"]]
    assert [c["id"] for c in fake][:3] == ["PICK-fit-L2", "PICK-fit-L4", "PICK-fit-L6"]
    for c in fake:
        want = c["reference"]["must_include"][0]
        assert want == "none" or f"\n{want} - " in "\n" + c["messages"][1]["content"]
        assert c["task_id"].startswith("lookalikes__") and c["reference"]["final"]


# -- a case too long for the window is not sent, in any suite ----------------------------------

def test_a_catalogue_longer_than_the_window_is_beyond_not_a_miss(tmp_path):
    class Pool:
        asked = 0

        async def chat(self, model, messages, tools=None, max_tokens=0, options=None):
            Pool.asked += 1
            return ChatResult(content="ANSWER: a_b_c", finish_reason="stop",
                              completion_tokens=5), "b"

    svc = _svc(tmp_path, Pool(), ctx=16000)
    cases = [{"id": "small", "task_id": "x", "tags": ["toolpick", "tier:easy", "tokens:3000"],
              "max_tokens": bench.THINK_ROOM, "messages": [{"role": "user", "content": "q"}],
              "tools": [], "reference": {"must_include": ["a_b_c"], "final": True}},
             {"id": "big", "task_id": "x", "tags": ["toolpick", "tier:medium", "tokens:12000"],
              "max_tokens": bench.THINK_ROOM, "messages": [{"role": "user", "content": "q"}],
              "tools": [], "reference": {"must_include": ["a_b_c"], "final": True}}]
    (bench._dir(svc) / "toolpick.jsonl").write_text(
        "".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
    import unittest.mock as um
    with um.patch.object(bench, "_record", lambda *a, **k: None), \
            um.patch.object(bench, "_state", {"state": "idle"}):
        out = asyncio.run(bench._run(svc, "toolpick", "m", "", 0))
    rows = {r["case_id"]: r for r in svc.db.query(
        "SELECT * FROM bench_results WHERE run_id=?", (out["run_id"],))}
    assert Pool.asked == 1 and rows["small"]["score"] == 100.0
    assert rows["big"]["score"] is None and rows["big"]["code_detail"].startswith("beyond")
    assert out["beyond"] == 1 and out["score"] == 100.0, "left out of the average, and said"


# -- the card and the controls -------------------------------------------------------------------

def test_the_card_names_the_new_kinds_and_how_far_each_ladder_went():
    def row(tags, score):
        return {"score": score, "error": "", "code_detail": "", "tags": json.dumps(tags),
                "tools_expected": "[]", "tools_called": "[]", "task_id": "x"}
    rows = [row(["needle", "tier:easy", "depth:50", "chars:4000"], 100.0),
            row(["needle", "tier:medium", "ctx-ledger", "chars:32000", "ladder:ctx:ledger:0",
                 "level:8"], 100.0),
            row(["needle", "tier:hard", "ctx-ledger", "chars:32000", "ladder:ctx:ledger:0",
                 "level:10"], 0.0),
            row(["needle", "tier:hard", "ctx-ledger", "chars:32000", "ladder:ctx:ledger:1",
                 "level:10"], 100.0)]
    f = {x["name"]: x["rows"] for x in benchcard.facets("needle", rows)}
    kinds = {r["label"] for r in f["Kind of needle"]}
    assert kinds == {"one needle", "add up a ledger"}
    reach = [r["label"] for r in f["How far up each ladder"]]
    assert reach == ["ledger: level 8 of 10", "ledger (second set): level 10 of 10"]


def test_the_builds_take_a_top_level_from_the_page():
    from foundry_router.ui import routes
    assert 'top_level=int(body.get("top_level") or 0)' in inspect.getsource(routes.bench_needle)
    assert 'int(body.get("top_level") or 0)' in inspect.getsource(routes.bench_toolpick)
    page = (Path(foundry_router.__file__).parent / "ui" / "static" / "index.html"
            ).read_text(encoding="utf-8")
    for box in ('id="bench-needle-top"', 'id="bench-pick-top"',
                'top_level: +($("#bench-needle-top")', 'top_level: +($("#bench-pick-top")'):
        assert box in page, box
