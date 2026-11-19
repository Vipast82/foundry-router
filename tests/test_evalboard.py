"""v0.181.190: the scoreboard — the same prompts across several personas, ranked
into tiers, with the spread reported rather than hidden.

The three things these tests pin are the three ways such a board misleads: a
ranking finer than the evidence, a score with no repeatability behind it, and a
"best" with no cost beside it.
"""
import json

import pytest

from foundry_router import evalboard as B
from foundry_router import evalharness as E
from foundry_router.db import Database


# -- intervals ---------------------------------------------------------------------

def test_a_perfect_score_on_a_few_prompts_is_not_certainty():
    lo, hi = B.wilson(8, 8)
    assert hi == 1.0
    assert lo < 0.8, lo          # 8 of 8 is not "100%, no doubt"
    assert B.wilson(80, 80)[0] > lo    # more prompts, narrower claim


def test_an_empty_row_claims_nothing():
    assert B.wilson(0, 0) == (0.0, 1.0)


def test_the_interval_narrows_as_prompts_are_added():
    widths = [B.wilson(k, n)[1] - B.wilson(k, n)[0]
              for k, n in ((4, 8), (40, 80), (400, 800))]
    assert widths == sorted(widths, reverse=True)


# -- tiers -------------------------------------------------------------------------

def _row(persona, score, lo, hi, scored=30):
    return {"persona": persona, "score": score, "lo": lo, "hi": hi, "scored": scored}


def test_personas_whose_intervals_overlap_share_a_tier():
    rows = B.tier_of([_row("a", 90, .80, .96), _row("b", 85, .74, .93)])
    assert [r["tier"] for r in rows] == ["S", "S"]


def test_a_persona_clear_of_the_tier_above_drops_one():
    rows = B.tier_of([_row("a", 90, .80, .96), _row("b", 40, .28, .53)])
    assert [r["tier"] for r in rows] == ["S", "A"]


def test_a_tier_is_measured_against_its_leader_not_its_neighbour():
    """Three personas each overlapping the next but not the first: without a
    leader the chain would keep them all in one tier forever."""
    rows = B.tier_of([_row("a", 90, .82, .96), _row("b", 76, .66, .85),
                      _row("c", 60, .50, .70)])
    assert [r["tier"] for r in rows] == ["S", "S", "A"]


def test_too_few_prompts_gets_no_tier_at_all():
    rows = B.tier_of([_row("a", 100, .0, 1.0, scored=2)])
    assert rows[0]["tier"] == ""


# -- repeatability -----------------------------------------------------------------

def _db(tmp_path):
    return Database(tmp_path / "e.sqlite")


def _run(db, persona, campaign=None, status="done"):
    return db.execute(
        "INSERT INTO eval_runs (ts, persona, judge_model, prompts_run, status, "
        "campaign_id) VALUES (datetime('now'),?,'',0,?,?)", (persona, status, campaign))


def _result(db, run_id, prompt_id, passed, answer, ptok=10, ctok=20, ms=1000, err=""):
    db.execute(
        "INSERT INTO eval_results (run_id, prompt_id, prompt, response_chars, "
        "shape_pass, shape_detail, duration_ms, error, prompt_tokens, "
        "completion_tokens, answer_hash, answer_head) VALUES (?,?,'p',?,?,'',?,?,?,?,?,?)",
        (run_id, prompt_id, len(answer), int(passed), ms, err, ptok, ctok,
         E.fingerprint(answer), answer[:200]))


def test_the_same_answer_every_time_is_full_repeatability(tmp_path):
    db = _db(tmp_path)
    for _ in range(3):
        r = _run(db, "P")
        _result(db, r, 1, True, "1912")
        _result(db, r, 2, True, "408")
    rate, prompts, same = B.repeat_identity(db, [r["id"] for r in db.query(
        "SELECT id FROM eval_runs")])
    assert (rate, prompts, same) == (1.0, 2, 2)


def test_one_prompt_that_wandered_shows_up(tmp_path):
    db = _db(tmp_path)
    answers = ["1912", "1913", "1912"]
    ids = []
    for a in answers:
        r = _run(db, "P")
        ids.append(r)
        _result(db, r, 1, True, a)          # this one wandered
        _result(db, r, 2, True, "408")      # this one did not
    rate, prompts, same = B.repeat_identity(db, ids)
    assert prompts == 2 and same == 1 and rate == 0.5


# -- the board ---------------------------------------------------------------------

def test_the_board_ranks_and_prices_each_persona(tmp_path):
    db = _db(tmp_path)
    for i in range(2):                       # strong persona: 6 of 6
        r = _run(db, "Strong", campaign=1)
        for pid in range(6):
            _result(db, r, pid, True, f"a{pid}", ptok=100, ctok=50, ms=500)
    for i in range(2):                       # weak persona: 1 of 6
        r = _run(db, "Weak", campaign=1)
        for pid in range(6):
            _result(db, r, pid, pid == 0, f"b{pid}", ptok=10, ctok=5, ms=4000)
    out = B.scoreboard(db, campaign_id=1)
    rows = {r["persona"]: r for r in out["rows"]}
    assert out["rows"][0]["persona"] == "Strong"
    assert rows["Strong"]["score"] == 100.0 and rows["Weak"]["score"] < 20
    assert rows["Strong"]["tier"] == "S" and rows["Weak"]["tier"] == "A"
    # cost sits beside the score, in tokens and seconds
    assert rows["Strong"]["tokens"] == 2 * 6 * 150
    assert rows["Weak"]["seconds"] == 4.0
    assert rows["Strong"]["repeat_identity"] == 1.0
    assert "interval" in out["note"] or "tier" in out["note"]


def test_a_campaign_filter_keeps_other_runs_off_the_board(tmp_path):
    db = _db(tmp_path)
    r1 = _run(db, "A", campaign=1)
    _result(db, r1, 1, True, "x")
    r2 = _run(db, "B", campaign=2)
    _result(db, r2, 1, True, "y")
    assert [r["persona"] for r in B.scoreboard(db, campaign_id=1)["rows"]] == ["A"]
    assert len(B.scoreboard(db)["rows"]) == 2       # no filter: both


def test_an_unfinished_run_is_not_scored(tmp_path):
    db = _db(tmp_path)
    r = _run(db, "A", status="running")
    _result(db, r, 1, True, "x")
    assert B.scoreboard(db)["rows"] == []


def test_prompts_that_answered_nothing_are_counted_separately(tmp_path):
    db = _db(tmp_path)
    r = _run(db, "A")
    _result(db, r, 1, False, "", err="brain skipped: routing mode is passthrough")
    _result(db, r, 2, True, "ok")
    row = B.scoreboard(db)["rows"][0]
    assert row["silent"] == 1, row
    # the board says a number is thin rather than grading it
    assert row["tier"] == ""


def test_a_wandering_board_says_so_before_any_score(tmp_path):
    db = _db(tmp_path)
    for a in ("x", "y", "z", "w", "v"):
        r = _run(db, "A")
        for pid in range(6):
            _result(db, r, pid, True, a + str(pid))
    note = B.scoreboard(db)["note"]
    assert "repeat-identity" in note and "noise floor" in note


def test_an_ungraded_persona_never_sits_above_a_graded_one(tmp_path):
    db = _db(tmp_path)
    r = _run(db, "Thin")                      # one prompt, scores 100%
    _result(db, r, 1, True, "x")
    r = _run(db, "Real")                      # six prompts, scores 83%
    for pid in range(6):
        _result(db, r, pid, pid != 0, f"y{pid}")
    rows = B.scoreboard(db)["rows"]
    assert [x["persona"] for x in rows] == ["Real", "Thin"]
    assert rows[0]["tier"] and rows[1]["tier"] == ""


# -- the campaign runner -----------------------------------------------------------

@pytest.mark.asyncio
async def test_personas_are_interleaved_by_repeat(tmp_path, monkeypatch):
    """One persona after another would hand a machine's warm-up, throttling or a
    busy spell entirely to whichever persona ran then."""
    db = _db(tmp_path)
    order = []

    class FakeHarness:
        def __init__(self, svc):
            pass

        async def run(self, persona, judge_model="", categories=None, campaign_id=None):
            order.append(persona)
            return _run(db, persona, campaign=campaign_id)

    monkeypatch.setattr(E, "EvalHarness", FakeHarness)
    svc = type("S", (), {"db": db, "personas": None})()
    cid = await B.run_campaign(svc, ["A", "B"], repeats=3)
    assert order == ["A", "B", "A", "B", "A", "B"]
    row = db.query_one("SELECT * FROM eval_campaigns WHERE id=?", (cid,))
    assert row["status"] == "done" and row["repeats"] == 3
    assert json.loads(row["personas"]) == ["A", "B"]
