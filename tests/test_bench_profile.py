r"""v0.181.208: a model profile - every suite, one model, one card.

Asked for: run the suites together, get one score, and read two models side by
side well enough to decide whether a new model or a quantisation variant will
carry this ecosystem.

The dangerous part of a composite score is that it is an opinion with a number
on it. Three judgement calls are made here and each is asserted rather than
assumed: the weights (equal per SUITE, not per case), the noise (measured from
this database, not remembered), and what has no score at all (the agent tasks,
which must never be averaged in).
"""
import json

import pytest

from foundry_router import benchprofile as bp
from foundry_router.db import Database


def svc_with(tmp_path, weights=None):
    cfg = type("C", (), {"bench": type("B", (), {
        "suite_weights": dict(weights or {})})()})()
    db = Database(tmp_path / "p.sqlite")
    return type("S", (), {"db": db,
                          "config_store": type("CS", (), {"config": cfg})()})()


def profile(svc, model, scores, cases=None, label="", repeats=1):
    """scores: {suite: score or [score per repeat]}."""
    cases = cases or {}
    db = svc.db
    db.execute("INSERT INTO bench_profiles (ts, model, label, suites, repeats, "
               "status) VALUES (datetime('now'),?,?,?,?,'done')",
               (model, label, json.dumps(list(scores)), repeats))
    pid = db.query_one("SELECT MAX(id) AS id FROM bench_profiles")["id"]
    for suite, sc in scores.items():
        for one in (sc if isinstance(sc, list) else [sc]):
            db.execute(
                "INSERT INTO bench_runs (ts, suite, model, label, cases, scored, "
                "score, seconds, status, profile_id) VALUES "
                "(datetime('now'),?,?,?,?,?,?,1.0,'done',?)",
                (suite, model, label, cases.get(suite, 10),
                 cases.get(suite, 10), one, pid))
    return pid


# -- the weights -----------------------------------------------------------------------

def test_a_suite_weighs_the_same_however_many_cases_it_has(tmp_path):
    """Per case would let a 60-case suite outvote a 28-case one by sitting
    still, which is a fact about how much work the suite was to build."""
    svc = svc_with(tmp_path)
    pid = profile(svc, "m", {"big": 100.0, "small": 0.0},
                  cases={"big": 60, "small": 28})
    assert bp.build_card(svc, pid)["composite"] == 50.0


def test_the_operator_can_change_what_matters(tmp_path):
    svc = svc_with(tmp_path, {"toolflow": 3.0})
    pid = profile(svc, "m", {"toolflow": 0.0, "baseline": 100.0})
    # 3:1 against the suite it fails
    assert bp.build_card(svc, pid)["composite"] == 25.0


def test_a_suite_weighted_zero_is_still_run_and_still_shown(tmp_path):
    """Excluded on purpose and absent are different things, and a card that
    quietly drops a suite is the second one pretending to be the first."""
    svc = svc_with(tmp_path, {"needle": 0})
    pid = profile(svc, "m", {"needle": 10.0, "baseline": 80.0})
    card = bp.build_card(svc, pid)
    assert card["composite"] == 80.0
    assert "needle" in card["excluded"]
    assert any(p["suite"] == "needle" for p in card["parts"])
    assert "weighted 0" in card["note"]


def test_the_weights_are_shown_with_the_number_they_produced(tmp_path):
    svc = svc_with(tmp_path)
    card = bp.build_card(svc, profile(svc, "m", {"a": 60.0, "b": 80.0}))
    shares = {w["suite"]: w["share"] for w in card["weighted"]}
    assert shares == {"a": 0.5, "b": 0.5}


def test_a_bad_weight_in_the_config_does_not_break_the_card(tmp_path):
    svc = svc_with(tmp_path, {"a": "lots"})
    assert bp.build_card(svc, profile(svc, "m", {"a": 50.0}))["composite"] == 50.0


# -- what has no score ------------------------------------------------------------------

def test_the_agent_tasks_are_never_averaged_in(tmp_path):
    """They have no answer key by design (v0.181.206). Averaging them in would
    put a number on the one thing that must not have one."""
    svc = svc_with(tmp_path)
    pid = profile(svc, "m", {"baseline": 80.0, "agent": None})
    card = bp.build_card(svc, pid)
    assert card["composite"] == 80.0
    assert [p["suite"] for p in card["unscored"]] == ["agent"]
    assert "no answer key" in card["note"]
    assert "agent" not in {w["suite"] for w in card["weighted"]}


def test_a_profile_of_only_unscored_suites_has_no_composite(tmp_path):
    svc = svc_with(tmp_path)
    card = bp.build_card(svc, profile(svc, "m", {"agent": None}))
    assert card["composite"] is None
    assert "no suite in this profile produced a score" in card["note"]


# -- the noise, measured ----------------------------------------------------------------

def test_a_spread_needs_enough_history_to_mean_anything(tmp_path):
    svc = svc_with(tmp_path)
    db = svc.db
    for v in (70.0, 72.0):
        db.execute("INSERT INTO bench_runs (ts, suite, model, cases, scored, "
                   "score, status) VALUES (datetime('now'),'s','m',1,1,?,'done')",
                   (v,))
    assert bp.spread(db, "s", "m") is None
    db.execute("INSERT INTO bench_runs (ts, suite, model, cases, scored, score, "
               "status) VALUES (datetime('now'),'s','m',1,1,74.0,'done')")
    assert bp.spread(db, "s", "m") == 2.0


def test_a_run_is_never_part_of_its_own_noise_estimate(tmp_path):
    svc = svc_with(tmp_path)
    db = svc.db
    for v in (70.0, 72.0, 74.0, 200.0):
        db.execute("INSERT INTO bench_runs (ts, suite, model, cases, scored, "
                   "score, status) VALUES (datetime('now'),'s','m',1,1,?,'done')",
                   (v,))
    rid = db.query_one("SELECT MAX(id) AS id FROM bench_runs")["id"]
    assert bp.spread(db, "s", "m", before_id=rid) == 2.0


def test_a_failed_run_is_not_evidence_about_the_spread(tmp_path):
    svc = svc_with(tmp_path)
    db = svc.db
    for v, st in ((70.0, "done"), (72.0, "done"), (74.0, "done"),
                  (5.0, "failed")):
        db.execute("INSERT INTO bench_runs (ts, suite, model, cases, scored, "
                   "score, status) VALUES (datetime('now'),'s','m',1,1,?,?)",
                   (v, st))
    assert bp.spread(db, "s", "m") == 2.0


@pytest.mark.parametrize("a,b,sd_a,sd_b,tie", [
    (70.0, 72.0, 1.0, 1.0, False),      # 2 points against a combined 1.41
    (70.0, 71.0, 1.0, 1.0, True),
    (70.0, 73.0, 3.0, 3.0, True),       # a wide spread swallows 3 points
    (70.0, 90.0, 3.0, 3.0, False),
    (70.0, 73.0, None, None, True),     # no history: treated as noisy
    (70.0, 80.0, None, None, False),
    (70.0, None, 1.0, 1.0, True),       # nothing to compare is not a win
])
def test_a_difference_smaller_than_the_spread_is_not_a_result(a, b, sd_a, sd_b, tie):
    assert bp.within_noise(a, b, sd_a, sd_b) is tie


def test_one_pass_says_a_small_gap_is_not_a_gap(tmp_path):
    svc = svc_with(tmp_path)
    card = bp.build_card(svc, profile(svc, "m", {"a": 70.0}, repeats=1))
    assert "read a gap smaller than that as no gap" in card["note"]


def test_repeats_report_the_spread_this_profile_itself_saw(tmp_path):
    svc = svc_with(tmp_path)
    pid = profile(svc, "m", {"a": [60.0, 70.0, 80.0]}, repeats=3)
    part = bp.build_card(svc, pid)["parts"][0]
    assert part["score"] == 70.0 and part["low"] == 60.0 and part["high"] == 80.0
    assert part["runs"] == 3


# -- what the card warns about ----------------------------------------------------------

def test_a_weak_suite_is_named_because_the_average_hides_it(tmp_path):
    """The whole point: a model strong everywhere and hopeless at finding
    tools cannot run this ecosystem, whatever its composite says."""
    svc = svc_with(tmp_path)
    card = bp.build_card(svc, profile(svc, "m", {
        "baseline": 90.0, "codereason": 88.0, "toolflow": 12.0}))
    assert card["composite"] > 60
    assert "toolflow" in card["note"] and "the average hides this" in card["note"]


def test_a_missing_profile_says_so_rather_than_returning_an_empty_card(tmp_path):
    assert "no profile 404" in bp.build_card(svc_with(tmp_path), 404)["error"]


# -- side by side -----------------------------------------------------------------------

def test_two_models_line_up_suite_by_suite(tmp_path):
    svc = svc_with(tmp_path)
    a = profile(svc, "alpha", {"baseline": 75.7, "toolflow": 44.0})
    b = profile(svc, "beta", {"baseline": 61.5, "toolflow": 12.0})
    out = bp.compare(svc, [a, b])
    assert [r["suite"] for r in out["rows"]] == ["baseline", "toolflow"]
    assert out["rows"][0]["cells"][1]["delta"] == -14.2
    assert out["rows"][0]["cells"][1]["tie"] is False


def test_a_gap_inside_the_noise_is_marked_a_tie_not_a_win(tmp_path):
    svc = svc_with(tmp_path)
    a = profile(svc, "alpha", {"s": 70.0})
    b = profile(svc, "beta", {"s": 73.0})
    out = bp.compare(svc, [a, b])
    cell = out["rows"][0]["cells"][1]
    assert cell["delta"] == 3.0 and cell["tie"] is True


def test_a_suite_only_one_model_ran_is_shown_as_not_run(tmp_path):
    svc = svc_with(tmp_path)
    a = profile(svc, "alpha", {"baseline": 70.0, "needle": 80.0})
    b = profile(svc, "beta", {"baseline": 72.0})
    out = bp.compare(svc, [a, b])
    needle = next(r for r in out["rows"] if r["suite"] == "needle")
    assert needle["cells"][1]["missing"] is True
    assert needle["cells"][1]["score"] is None


def test_two_composites_too_close_to_call_say_so(tmp_path):
    svc = svc_with(tmp_path)
    a = profile(svc, "alpha", {"s": 70.0})
    b = profile(svc, "beta", {"s": 72.0})
    out = bp.compare(svc, [a, b])
    assert "does not separate them" in out["note"]
    assert "add repeats" in out["note"]


def test_a_clear_win_is_not_hedged(tmp_path):
    svc = svc_with(tmp_path)
    a = profile(svc, "alpha", {"s": 90.0})
    b = profile(svc, "beta", {"s": 40.0})
    assert bp.compare(svc, [a, b])["note"] == ""


def test_comparing_nothing_does_not_raise(tmp_path):
    out = bp.compare(svc_with(tmp_path), [999])
    assert out["cards"] == [] and out["note"]


# -- the context grid --------------------------------------------------------------------

def test_the_needle_grid_keeps_depth_and_length_apart(tmp_path):
    """One number cannot say WHERE a model stops finding things, which is the
    only question that suite answers."""
    svc = svc_with(tmp_path)
    pid = profile(svc, "m", {"needle": 60.0})
    rid = svc.db.query_one("SELECT MAX(id) AS id FROM bench_runs")["id"]
    for chars, depth, score in ((4000, 0, 100), (4000, 50, 100),
                                (32000, 0, 100), (32000, 50, 20)):
        svc.db.execute(
            "INSERT INTO bench_results (run_id, case_id, task_id, tags, score, "
            "seconds) VALUES (?,?,?,?,?,1)",
            (rid, "c", "t", json.dumps([f"depth:{depth}", f"chars:{chars}"]),
             score))
    grid = bp.build_card(svc, pid)["needle"]
    assert grid["lengths"] == [4000, 32000] and grid["depths"] == [0, 50]
    cell = next(c for c in grid["cells"]
                if c["chars"] == 32000 and c["depth"] == 50)
    assert cell["score"] == 20.0


def test_a_profile_without_the_needle_suite_has_no_grid(tmp_path):
    svc = svc_with(tmp_path)
    assert bp.build_card(svc, profile(svc, "m", {"a": 50.0}))["needle"] == {}


# -- running -----------------------------------------------------------------------------

def test_the_unscored_suites_are_run_last(tmp_path):
    """So a profile that is stopped part way still has the scored ones."""
    svc = svc_with(tmp_path)
    import foundry_router.bench as bench
    svc.capture = type("C", (), {"data_dir": str(tmp_path)})()
    for name in ("agent", "baseline", "toolflow"):
        (bench._dir(svc) / f"{name}.jsonl").write_text(
            json.dumps({"id": "x", "task_id": "x", "messages": [],
                        "reference": {}}) + "\n", encoding="utf-8")
    assert bp.runnable_suites(svc)[-1] == "agent"


def test_the_profile_column_exists_on_an_older_database(tmp_path):
    db = Database(tmp_path / "m.sqlite")
    cols = {r["name"] for r in
            db.query("SELECT name FROM pragma_table_info('bench_runs')")}
    assert "profile_id" in cols


def test_the_config_route_accepts_the_weights(tmp_path):
    """The allow-list is the second place every setting lives."""
    import inspect

    from foundry_router.ui import routes
    assert "suite_weights" in inspect.getsource(routes.set_bench)


def test_the_page_draws_the_card_and_the_comparison():
    from pathlib import Path

    import foundry_router
    page = (Path(foundry_router.__file__).parent / "ui" / "static"
            / "index.html").read_text(encoding="utf-8")
    for want in ("function pfCard(", "function pfCompare(", "function pfGrid(",
                 'id="pf-go"', 'api("bench/profile/run"',
                 # the compare call wraps across a line, so this asserts the
                 # endpoint rather than how the call happens to be formatted
                 "bench/profile/compare?ids="):
        assert want in page, want
    # whitespace-normalised: the panel is prose and reflowing it must not
    # break an assertion about what it says. Three assertions here failed in a
    # row on line breaks rather than on content.
    import re
    i = page.index("Run every suite — one model, one card")
    panel = re.sub(r"\s+", " ", page[i:i + 2000])
    assert "weights decide the answer" in panel
    assert "no answer key" in panel
    assert "five points" in panel
    assert "in sequence" in panel, "why they are not run at once"
