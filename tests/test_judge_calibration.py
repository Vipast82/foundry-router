r"""v0.181.207: checking a judge against answers already known to be right.

The frontier usage window is nearly spent, so the judge has to be a local
model. Nothing stops that - judge_refusal bars only Fable and self-judging,
and check_paid_call denies subscription and metered backends rather than free
local ones - so the question is not permission but TRUST.

A pairwise win rate from a judge that cannot tell two answers apart is the most
convincing nonsense this benchmark can produce. The protocol already catches a
judge reading position, because it disagrees with itself when the sides are
swapped; it cannot catch a judge that is consistently wrong. This can, against
a suite's own answer key, and it is deliberately a FLOOR: these tests assert
that it disqualifies a bad judge and that it does not claim a good one is good
at anything else.
"""
import pytest

from foundry_router import bench, judgeproto as jp
from foundry_router.db import Database


@pytest.fixture
def svc(tmp_path):
    cfg = type("CFG", (), {"code_checks": None})()
    s = type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "b.sqlite"),
        "config_store": type("CS", (), {"config": cfg})(),
    })()
    bench.build_codereason(s)
    return s


# -- the near miss -------------------------------------------------------------------

@pytest.mark.parametrize("key,want", [
    ("3", "4"), ("3.5", "4.0"), ("True", "False"), ("False", "True"),
    ("'ababab'", "'ababa'"),
    ("[0, 1, 2]", "[2, 1, 0]"),
    ("[2, 2, 2]", "[2, 2]"),                 # reversing it changes nothing
    ("(True, False)", "(False, True)"),
    ("[('a', 3), ('b', 2)]", "[('b', 2), ('a', 3)]"),
    ("{1: 2}", "{}"),
    # an exception name is not a literal, and this is the near miss that
    # matters for those cases
    ("RuntimeError", "ZeroDivisionError"),
    ("KeyError", "IndexError"),
])
def test_a_near_miss_is_the_same_kind_of_answer_and_definitely_wrong(key, want):
    assert jp.perturb(key) == want


@pytest.mark.parametrize("key", ["'x'", "[]", "None", "NotAnError", "", "{}"])
def test_a_key_with_no_distinct_near_miss_is_left_out_not_paired_with_itself(key):
    """Pairing an answer against itself would score the judge on a question
    with no right answer, and every judge would look bad or lucky."""
    assert jp.perturb(key) == ""


def test_a_near_miss_is_never_equal_to_the_right_answer(svc):
    for p in jp.calibration_pairs(svc, "codereason"):
        assert p["wrong"] != p["right"], p


# -- the pairs ------------------------------------------------------------------------

def test_both_kinds_of_pair_are_built_from_a_suite_with_keys(svc):
    rows = jp.calibration_pairs(svc, "codereason")
    kinds = {r["kind"] for r in rows}
    assert kinds == {"other", "near"}
    assert len(rows) >= len(bench.cases(svc, "codereason"))


def test_every_pair_carries_the_question_it_belongs_to(svc):
    for p in jp.calibration_pairs(svc, "codereason"):
        assert p["task"].strip() and p["right"].strip()


@pytest.mark.asyncio
async def test_a_suite_with_no_answer_keys_cannot_calibrate_anything(svc):
    """The agent tasks have no keys by design, and saying so beats reporting
    a rate over nothing."""
    from foundry_router import agenttasks
    agenttasks.build(svc)
    out = await jp.calibrate(svc, "judge-x", "agent", ask=_never_called)
    assert out["pairs"] == []
    assert "nothing here a judge can be checked against" in out["note"]


@pytest.mark.asyncio
async def test_a_fable_judge_is_not_even_checked(svc):
    out = await jp.calibrate(svc, "claude-fable-5-2", ask=_never_called)
    assert "Fable" in out["note"] and out["pairs"] == []


# -- what the verdict says ------------------------------------------------------------

async def _never_called(model, body):
    """For the two cases that must refuse before judging anything."""
    raise AssertionError("the judge must not be called at all")


async def _first_position(model, body):
    """Always picks whatever it read first - the bias both orders exist to
    catch, and the one that would score 100% if a pair were judged once."""
    return '{"winner": "A"}'


@pytest.mark.asyncio
async def test_a_judge_that_always_picks_the_first_answer_agrees_on_nothing(svc):
    """The whole reason both orders are judged. A first-position judge would
    score 100% if each pair were judged once, since the right answer is
    passed first."""
    out = await jp.calibrate(svc, "judge-x", ask=_first_position)
    assert out["by_kind"]["near"]["flips"] == out["by_kind"]["near"]["pairs"]
    assert out["by_kind"]["near"]["decided"] == 0
    assert "cannot be calibrated" in out["verdict"]


@pytest.mark.asyncio
async def test_a_judge_that_reads_the_answers_is_called_usable(svc):
    async def ask(model, body):
        a = body.split("ANSWER A:")[1].split("ANSWER B:")[0]
        # the right answer of a codereason pair is the one the suite stores,
        # and the test knows it by looking for the perturbed marker instead:
        # whichever side is NOT the perturbation wins
        return '{"winner": "A"}' if _looks_right(a, body) else '{"winner": "B"}'

    out = await jp.calibrate(svc, "judge-x", ask=ask)
    assert out["by_kind"]["other"]["rate"] == 1.0
    assert out["by_kind"]["near"]["rate"] == 1.0
    assert out["verdict"].startswith("usable")
    assert "floor" in out["verdict"]


def _looks_right(side_a: str, body: str) -> bool:
    """Side A holds the right answer. The harness passes right as `left`, so
    in the "ab" order A is right and in the "ba" order B is - this reads the
    order from the body rather than guessing."""
    first = body.split("ANSWER A:")[1].split("ANSWER B:")[0].strip()
    del side_a
    return first == _RIGHT.get(_task_of(body))


_RIGHT: dict = {}


def _task_of(body: str) -> str:
    return body.split("TASK:")[1].split("ANSWER A:")[0].strip()[:200]


@pytest.fixture(autouse=True)
def _remember_right(svc):
    """Record each pair's right answer so the fake judge can be correct
    without being told which side it is on."""
    _RIGHT.clear()
    for p in jp.calibration_pairs(svc, "codereason"):
        _RIGHT[p["task"].strip()[:200]] = p["right"]
    yield
    _RIGHT.clear()


@pytest.mark.asyncio
async def test_a_judge_that_prefers_the_wrong_answer_is_disqualified(svc):
    async def ask(model, body):
        return '{"winner": "B"}' if _looks_right(
            body.split("ANSWER A:")[1], body) else '{"winner": "A"}'

    out = await jp.calibrate(svc, "judge-x", ask=ask)
    assert out["by_kind"]["other"]["rate"] == 0.0
    assert out["verdict"].startswith("NOT usable")
    assert "different question" in out["verdict"]


@pytest.mark.asyncio
async def test_a_judge_good_on_the_obvious_and_bad_on_the_near_miss_is_weak(svc):
    """The interesting case, and the one a small local model is likeliest to
    be: it beats nonsense and cannot see a near miss."""
    async def ask(model, body):
        right = _looks_right(body.split("ANSWER A:")[1], body)
        near = _is_near(body)
        if near:
            return '{"winner": "B"}' if right else '{"winner": "A"}'
        return '{"winner": "A"}' if right else '{"winner": "B"}'

    out = await jp.calibrate(svc, "judge-x", ask=ask)
    assert out["by_kind"]["other"]["rate"] == 1.0
    assert out["by_kind"]["near"]["rate"] == 0.0
    assert out["verdict"].startswith("weak")
    assert "indicative" in out["verdict"]


_NEAR: dict = {}


def _is_near(body: str) -> bool:
    task = _task_of(body)
    a = body.split("ANSWER A:")[1].split("ANSWER B:")[0].strip()
    b = body.split("ANSWER B:")[1].strip()
    wrong = b if a == _RIGHT.get(task) else a
    return wrong in _NEAR.get(task, set())


@pytest.fixture(autouse=True)
def _remember_near(svc):
    _NEAR.clear()
    for p in jp.calibration_pairs(svc, "codereason"):
        if p["kind"] == "near":
            _NEAR.setdefault(p["task"].strip()[:200], set()).add(p["wrong"])
    yield
    _NEAR.clear()


def test_the_floors_are_named_rather_than_buried_in_a_comparison():
    assert 0.5 < jp.FLOOR_OTHER <= 1.0
    assert 0.5 < jp.FLOOR_NEAR <= jp.FLOOR_OTHER


def test_a_judge_that_decided_nothing_is_unusable_not_neutral():
    v = jp._verdict({})
    assert "unusable" in v and "neutral" in v


def test_the_verdict_never_gives_a_rate_without_the_floor_caveat():
    v = jp._verdict({"other": {"rate": 1.0, "agreed": 10, "decided": 10,
                               "pairs": 10, "flips": 0},
                     "near": {"rate": 1.0, "agreed": 8, "decided": 8,
                              "pairs": 8, "flips": 0}})
    assert "floor" in v and "not the same skill" in v


# -- wiring ----------------------------------------------------------------------------

def test_the_route_and_the_control_exist():
    import inspect
    from pathlib import Path

    import foundry_router
    from foundry_router.ui import routes
    # v0.181.209 made the route START a check rather than hold one open for
    # fourteen minutes, so this asserts the wiring and not the old contract
    assert "start_check" in inspect.getsource(routes.eval_judge_check)
    page = (Path(foundry_router.__file__).parent / "ui" / "static"
            / "index.html").read_text(encoding="utf-8")
    assert 'id="jc-run"' in page and 'api("eval/judge-check"' in page
    assert "function judgeCheckView(" in page


def test_the_panel_says_it_is_a_floor_before_it_shows_a_rate():
    from pathlib import Path

    import foundry_router
    page = (Path(foundry_router.__file__).parent / "ui" / "static"
            / "index.html").read_text(encoding="utf-8")
    panel = page[page.index("Check the judge"):][:1800]
    assert "floor, not a" in panel
    assert "local" in panel
    assert "disqualified" in panel
