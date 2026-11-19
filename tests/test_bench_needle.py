"""v0.181.199: a needle-in-a-haystack suite, and exact scoring for it.

Asked for context-search benchmarks and for work matching what is actually done
here, so the haystack is this project's own writing rather than synthetic
filler, and the needle is an unguessable six-digit code tied to an invented
sprint id, placed at a known depth through a context of known length.

The scoring had to be new. `answer_score` is a length heuristic - it returns 1.0
for any answer over twenty characters within 8x of the reference length - and
`tool_score` returns 1.0 when no tool was needed and none was called, which is
every needle case. So before this, a model that found NOTHING scored 100 on a
needle case. That is the fault this codebase has been turning up all week: a
check that cannot fail a wrong answer.
"""
import json
import pathlib
import tempfile

import pytest

from foundry_router import bench
from foundry_router.db import Database


@pytest.fixture
def svc(tmp_path):
    return type("S", (), {"capture": type("C", (), {"data_dir": str(tmp_path)})(),
                          "db": Database(tmp_path / "b.sqlite")})()


# -- exact scoring ------------------------------------------------------------------

def test_a_case_with_no_required_string_is_not_scored_this_way():
    assert bench.exact_score("anything", None) is None
    assert bench.exact_score("anything", []) is None
    assert bench.exact_score("anything", [""]) is None


@pytest.mark.parametrize("answer,want", [
    ("481902", True),
    ("The code is 481902.", True),
    ("The code is 481,902.", True),          # a thousands comma is still the number
    ("(481902)", True),
    ("4819021", False),                      # not a longer run of digits
    ("1481902", False),
    ("x481902", False),
    ("I could not find a code.", False),
    ("", False),
])
def test_the_needle_is_matched_on_its_own_boundaries(answer, want):
    assert bool(bench.exact_score(answer, ["481902"])) is want


def test_a_phrase_is_matched_plainly():
    """Its own punctuation and spaces are the boundary."""
    assert bench.exact_score("the warehouse opened in 1912 in Sheffield",
                             ["opened in 1912"]) == 1.0


def test_part_marks_for_part_of_what_was_required():
    assert bench.exact_score("1912 only", ["1912", "sheffield"]) == 0.5


def test_a_comma_between_words_does_not_join_them():
    """Stripping every comma would make "a,b" match the needle "ab"."""
    assert bench._norm("items a,b") == "items a,b"


# -- why the scoring had to change ---------------------------------------------------

def test_without_exact_scoring_finding_nothing_scored_full_marks():
    """The reason this exists, kept as a test so it cannot come back: a needle
    case expects no tool call, so tool_score gives 1.0, and answer_score gives
    1.0 to any text - 100 for a model that found nothing."""
    assert bench.combine(1.0, None, 1.0) == 100.0          # the old path
    assert bench.combine(1.0, None, 1.0, 0.0) == 0.0       # with the needle missed
    assert bench.combine(1.0, None, 1.0, 1.0) == 100.0     # and found


def test_an_ordinary_case_is_unaffected():
    assert bench.combine(0.8, 1.0, 1.0) == 90.0
    assert bench.combine(None, None, None) == 0.0


# -- the suite ----------------------------------------------------------------------

def test_every_cell_of_the_grid_is_built(svc):
    out = bench.build_needle(svc, per_cell=2)
    assert out["cases"] == 2 * len(bench.NEEDLE_DEPTHS) * len(bench.NEEDLE_LENGTHS)
    cases = bench.cases(svc, "needle")
    cells = {(t for t in c["tags"]) and
             (next(t for t in c["tags"] if t.startswith("chars:")),
              next(t for t in c["tags"] if t.startswith("depth:"))) for c in cases}
    assert len(cells) == len(bench.NEEDLE_DEPTHS) * len(bench.NEEDLE_LENGTHS)


def test_the_needle_is_actually_in_the_prompt(svc):
    for c in bench.cases(svc, "needle") or bench.cases(
            svc, bench.build_needle(svc, per_cell=1)["name"]):
        code = c["reference"]["must_include"][0]
        assert code in c["messages"][1]["content"], c["id"]


def test_every_case_has_its_own_code(svc):
    """A repeated code would let one lucky answer score another case."""
    bench.build_needle(svc, per_cell=3)
    cases = bench.cases(svc, "needle")
    codes = [c["reference"]["must_include"][0] for c in cases]
    assert len(set(codes)) == len(codes)


def test_the_code_is_not_guessable_from_the_question(svc):
    """The question names the sprint, not the code."""
    bench.build_needle(svc, per_cell=1)
    for c in bench.cases(svc, "needle"):
        code = c["reference"]["must_include"][0]
        question = c["messages"][1]["content"].rsplit("\n\n", 1)[-1]
        assert code not in question, c["id"]
        assert len(code) == 6 and code.isdigit()


def test_the_depth_puts_the_needle_where_it_says(svc):
    """A depth that does not move the needle measures nothing."""
    bench.build_needle(svc, per_cell=1, depths=[0, 100], lengths=[8000])
    where = {}
    for c in bench.cases(svc, "needle"):
        body = c["messages"][1]["content"]
        depth = next(t for t in c["tags"] if t.startswith("depth:")).split(":")[1]
        where[depth] = body.index(c["reference"]["must_include"][0]) / len(body)
    assert where["0"] < 0.2, where
    assert where["100"] > 0.8, where


def test_the_haystack_does_not_walk_the_whole_drive(svc):
    """This repository sits in a synced OneDrive tree beside a large checkout;
    a recursive glob hung for over two minutes.

    Read from the PARSED source, not its text: the first version of this test
    matched the comment that explains the rule."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(bench._haystack_text).strip())
    patterns, names = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute):
            names.append(fn.attr)
            if fn.attr in ("glob", "rglob"):
                for a in node.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        patterns.append(a.value)
    assert "rglob" not in names, "rglob walks the whole tree"
    assert patterns, "expected at least one glob to check"
    for p in patterns:
        assert "**" not in p, f"recursive glob {p!r}"


def test_building_is_quick_enough_to_be_a_button(svc):
    import time
    t0 = time.monotonic()
    bench.build_needle(svc, per_cell=3)
    assert time.monotonic() - t0 < 10.0


# -- reading the result --------------------------------------------------------------

def test_the_breakdown_is_per_depth_and_length(tmp_path):
    db = Database(tmp_path / "nb.sqlite")
    db.execute("INSERT INTO bench_runs (ts, suite, model, cases, scored, status) "
               "VALUES (datetime('now'),'needle','m',4,4,'done')")
    rid = db.query_one("SELECT MAX(id) AS id FROM bench_runs")["id"]
    for depth, score in ((0, 100.0), (0, 100.0), (50, 0.0), (100, 100.0)):
        db.execute("INSERT INTO bench_results (run_id, case_id, tags, score) "
                   "VALUES (?,?,?,?)",
                   (rid, f"c{depth}", json.dumps(["needle", f"depth:{depth}",
                                                  "chars:4000"]), score))
    cells = bench.needle_breakdown(db, rid)
    by = {(c["chars"], c["depth"]): c for c in cells}
    assert by[(4000, 0)]["score"] == 100.0 and by[(4000, 0)]["cases"] == 2
    assert by[(4000, 50)]["score"] == 0.0
    assert by[(4000, 100)]["score"] == 100.0


def test_a_run_with_no_needle_cases_has_no_grid(tmp_path):
    db = Database(tmp_path / "plain.sqlite")
    db.execute("INSERT INTO bench_runs (ts, suite, model, cases, scored, status) "
               "VALUES (datetime('now'),'baseline','m',1,1,'done')")
    rid = db.query_one("SELECT MAX(id) AS id FROM bench_runs")["id"]
    db.execute("INSERT INTO bench_results (run_id, case_id, tags, score) "
               "VALUES (?,?,?,?)", (rid, "c", json.dumps(["luau"]), 50.0))
    assert bench.needle_breakdown(db, rid) == []
