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


def test_a_steady_verdict_is_full_repeatability(tmp_path):
    db = _db(tmp_path)
    for _ in range(3):
        r = _run(db, "P")
        _result(db, r, 1, True, "1912")
        _result(db, r, 2, True, "408")
    rep = B.repeatability(db, [r["id"] for r in db.query("SELECT id FROM eval_runs")])
    assert rep["verdict"] == 1.0 and rep["prompts"] == 2 and rep["flipped"] == 0


def test_prose_that_varies_is_not_a_wobbling_verdict(tmp_path):
    """The first version of this board called byte-identical answers
    "repeat-identity" and warned that every score sat on a noise floor. On the
    first real campaign it read 6% while the verdicts agreed 81% of the time: it
    was measuring whether a model emits the same bytes twice (v0.181.191)."""
    db = _db(tmp_path)
    ids = []
    for a in ("the year was 1912", "it opened in 1912", "1912, in Sheffield"):
        r = _run(db, "P")
        ids.append(r)
        _result(db, r, 1, True, a)          # different words, same verdict
    rep = B.repeatability(db, ids)
    assert rep["verdict"] == 1.0            # the score does not move
    assert rep["answers"] == 0.0            # and the text never repeated
    assert rep["flipped"] == 0


def test_a_verdict_that_flips_is_reported_as_the_score_s_own_movement(tmp_path):
    db = _db(tmp_path)
    ids = []
    for passed in (True, False, True):
        r = _run(db, "P")
        ids.append(r)
        _result(db, r, 1, passed, "x")      # this one changed its mind
        _result(db, r, 2, True, "y")        # this one did not
    rep = B.repeatability(db, ids)
    assert rep["verdict"] == 0.5 and rep["flipped"] == 1 and rep["prompts"] == 2


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
    assert rows["Strong"]["repeat_verdict"] == 1.0
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


def test_a_board_whose_verdicts_move_says_so_before_any_score(tmp_path):
    db = _db(tmp_path)
    for i in range(5):
        r = _run(db, "A")
        for pid in range(6):
            # prompt 0 cannot make its mind up; the rest are steady
            _result(db, r, pid, (i % 2 == 0) if pid == 0 else True, f"a{pid}")
    out = B.scoreboard(db)
    assert "verdicts repeated" in out["note"] and "run to run" in out["note"]
    assert out["rows"][0]["flipped"] == 1


def test_prose_variation_alone_never_raises_the_warning(tmp_path):
    """Every answer different, every verdict the same: nothing to warn about."""
    db = _db(tmp_path)
    for a in ("x", "y", "z", "w", "v"):
        r = _run(db, "A")
        for pid in range(6):
            _result(db, r, pid, True, a + str(pid))
    out = B.scoreboard(db)
    assert "verdicts repeated" not in out["note"], out["note"]
    assert out["rows"][0]["repeat_verdict"] == 1.0
    assert out["rows"][0]["repeat_answers"] == 0.0


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

# -- a prompt asked once cannot disagree with itself (v0.181.191) ------------------

def test_a_single_run_reports_no_repeatability_rather_than_perfect(tmp_path):
    """With repeats=1 every prompt has exactly one verdict, so counting a prompt
    as steady when it agrees with itself read 100% off evidence that contains
    none - the same fault as a check that cannot fail an answer. It says the
    number is not measured instead."""
    db = Database(tmp_path / "once.sqlite")
    r = _run(db, "P")
    for pid in (1, 2, 3):
        _result(db, r, pid, True, f"answer {pid}")
    out = B.repeatability(db, [r])
    assert out["verdict"] is None, out
    assert out["answers"] is None, out
    assert out["prompts"] == 0, out


def test_only_the_repeated_prompts_are_counted(tmp_path):
    """Two prompts asked twice, one of them flipping, and a third asked once:
    the rate is 1 of 2, not 2 of 3."""
    db = Database(tmp_path / "mixed.sqlite")
    a, b = _run(db, "P"), _run(db, "P")
    _result(db, a, 1, True, "same")
    _result(db, b, 1, True, "same")          # steady, identical text
    _result(db, a, 2, True, "yes it does")
    _result(db, b, 2, False, "no it does not")   # flipped
    _result(db, a, 3, True, "asked once")        # must not count
    out = B.repeatability(db, [a, b])
    assert out["prompts"] == 2, out
    assert out["verdict"] == 0.5, out
    assert out["flipped"] == 1, out
    assert out["answers"] == 0.5, out


def test_the_verdict_holds_while_the_wording_moves(tmp_path):
    """The number the first board put up as the one a score rests on. Prose does
    not repeat byte for byte, so the text rate is 0 while both verdicts are
    steady, which is how it came to say every score was unfounded."""
    db = Database(tmp_path / "prose.sqlite")
    a, b = _run(db, "P"), _run(db, "P")
    for pid in (1, 2):
        _result(db, a, pid, True, f"I cannot verify that, number {pid}.")
        _result(db, b, pid, True, f"That is not something I can confirm, {pid}.")
    out = B.repeatability(db, [a, b])
    assert out["verdict"] == 1.0, out
    assert out["answers"] == 0.0, out
    assert out["flipped"] == 0, out

# -- both rates, because an unanswered prompt is not an answer (v0.181.193) --------

def test_an_unanswered_prompt_counts_against_one_rate_and_not_the_other(tmp_path):
    """The board's denominator was COUNT(*) and an error row carries shape_pass
    0, so a prompt that got no answer scored as an answer the model got wrong -
    defensible, but v0.181.191's notes claimed the opposite. Both are reported
    now and neither is called the true one."""
    db = Database(tmp_path / "drops.sqlite")
    r = _run(db, "P")
    for i in range(1, 9):
        _result(db, r, i, True, f"right {i}")
    _result(db, r, 9, False, "wrong")
    _result(db, r, 10, False, "", err="no answer within 420s")
    board = B.scoreboard(db)
    row = board["rows"][0]
    assert row["scored"] == 10 and row["dropped"] == 1 and row["answered"] == 9
    assert row["score"] == 80.0            # 8 of 10 attempts
    assert row["score_answered"] == 88.9   # 8 of 9 answers
    assert row["lo"] < row["lo_answered"]  # the answered interval sits higher


def test_the_note_says_where_the_unanswered_prompts_fell(tmp_path):
    """When every drop is in one category the two rates stop being the same
    measurement, because that category's weight in the score falls. In the first
    five-repeat campaign all twelve were honesty prompts."""
    db = Database(tmp_path / "cat.sqlite")
    db.execute("INSERT INTO eval_prompts (id, category, prompt, checks, enabled) "
               "VALUES (1,'honesty','q1','[]',1)")
    db.execute("INSERT INTO eval_prompts (id, category, prompt, checks, enabled) "
               "VALUES (2,'objective','q2','[]',1)")
    r = _run(db, "P")
    for i in range(4):
        _result(db, r, 2, True, f"fine {i}")
    for i in range(2):
        _result(db, r, 1, False, "", err="no answer within 420s")
    board = B.scoreboard(db)
    row = board["rows"][0]
    assert row["dropped_by_category"] == {"honesty": 2}, row["dropped_by_category"]
    assert "every unanswered prompt was in 'honesty'" in board["note"], board["note"]
    assert "counts as a wrong answer in the first rate" in board["note"]


def test_a_board_with_no_drops_says_nothing_about_them(tmp_path):
    db = Database(tmp_path / "clean.sqlite")
    r = _run(db, "P")
    for i in range(1, 7):
        _result(db, r, i, True, f"right {i}")
    board = B.scoreboard(db)
    assert board["rows"][0]["dropped"] == 0
    assert board["rows"][0]["score_answered"] == board["rows"][0]["score"]
    assert "unanswered" not in board["note"]


def test_every_prompt_unanswered_leaves_the_answered_rate_undefined(tmp_path):
    """Not 0%, which would read as "answered them all wrong"."""
    db = Database(tmp_path / "none.sqlite")
    r = _run(db, "P")
    for i in range(1, 4):
        _result(db, r, i, False, "", err="no answer within 420s")
    row = B.scoreboard(db)["rows"][0]
    assert row["answered"] == 0
    assert row["score_answered"] is None
    assert row["score"] == 0.0
