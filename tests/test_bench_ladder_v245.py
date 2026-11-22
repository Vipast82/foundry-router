"""v0.181.245: the all-local ladder report (plan A9, docs/BENCH_GAPS.md addition 9).

The benchmark measures each model on each kind of work; this puts a rule on
top: a threshold per kind of work, and a report that reads "Grounded answers:
qwen3.8-flash passes at 72, threshold 70" - per kind of work, and per persona
along the local models of its own chain, with the cloud rungs nothing still
needs. Reports only: no route changes.
"""
import asyncio
import json
from pathlib import Path

import pytest

import foundry_router
from foundry_router import bench, benchladder as bl
from foundry_router.config import BenchConfig
from foundry_router.db import Database
from foundry_router.personas import PersonaStore

FLASH, BRAIN = "qwen3.8-flash", "brain-35b"
SONNET, OPUS = "claude-sonnet-5-5", "claude-opus-5-5"


class _Backend:
    def __init__(self, typ, models):
        self.models = list(models)
        self.config = type("B", (), {"type": typ, "models": list(models),
                                     "is_subscription": typ == "anthropic-compatible",
                                     "effective_flavor": None})()


class _Store:
    def __init__(self, **bench_cfg):
        self.raw = {"bench": dict(bench_cfg)}
        self.config = type("C", (), {"bench": BenchConfig(**bench_cfg)})()

    def mutate(self, fn):
        fn(self.raw)
        self.config.bench = BenchConfig(**self.raw["bench"])


def _svc(tmp_path, **bench_cfg):
    pool = type("P", (), {"backends": {
        "strata": _Backend("ollama", [FLASH]), "lane": _Backend("ollama", [BRAIN]),
        "meridian": _Backend("anthropic-compatible", [SONNET, OPUS])}})()
    return type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "r.sqlite"), "pool": pool,
        "config_store": _Store(**bench_cfg),
        "registry": type("R", (), {"get": staticmethod(lambda m: None)})(),
    })()


def _suite(svc, suite, text="v1"):
    d = bench._dir(svc)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{suite}.jsonl").write_text(json.dumps({"id": text}) + "\n", encoding="utf-8")
    return bench.suite_hash(svc, suite)


def _run(svc, suite, model, tiers, seconds=30.0, h=None, status="done"):
    """One finished run: `tiers` is {tier: [case scores]}; the run's score is
    their mean, as the bench reports it."""
    h = bench.suite_hash(svc, suite) if h is None else h
    scores = [s for v in tiers.values() for s in v]
    db = svc.db
    db.execute("INSERT INTO bench_runs (ts, suite, model, label, cases, scored, status, score, "
               "seconds, suite_hash) VALUES (datetime('now'), ?, ?, '', ?, ?, ?, ?, ?, ?)",
               (suite, model, len(scores), len(scores), status,
                round(sum(scores) / len(scores), 1), seconds, h))
    rid = db.query_one("SELECT MAX(id) AS id FROM bench_runs")["id"]
    i = 0
    for t, v in tiers.items():
        for s in v:
            i += 1
            db.execute("INSERT INTO bench_results (run_id, case_id, task_id, tags, score) "
                       "VALUES (?,?,?,?,?)", (rid, f"c{i}", "t", json.dumps([f"tier:{t}"]), s))
    return rid


def _persona(svc, name, chain, category="general_chat", enabled=1):
    PersonaStore(svc.db).upsert(name, pinned_models=chain, benchmark_category=category,
                                enabled=enabled, description="")


# -- the rule ---------------------------------------------------------------------------------

def test_through_is_the_highest_tier_reached_with_every_tier_below_it():
    assert bl.through({"easy": 100, "medium": 80, "hard": 60}, 70) == "medium"
    assert bl.through({"easy": 100, "medium": 60, "hard": 90}, 70) == "easy", "no skipping"
    assert bl.through({"medium": 90, "expert": 75}, 70) == "expert", "a tier with no cases"
    assert bl.through({"easy": 50}, 70) == "" and bl.through({}, 70) == ""


def test_the_threshold_is_seventy_unless_the_operator_sets_one(tmp_path):
    svc = _svc(tmp_path, ladder_thresholds={"grounded": 85})
    assert bl.threshold(svc, "grounded") == 85.0 and bl.threshold(svc, "needle") == 70.0
    with pytest.raises(ValueError):
        BenchConfig(ladder_thresholds={"grounded": 120})
    cfg = BenchConfig(local_ladders={"Foundry-Chat": "a, b ,, c"}, ladder_families={"x": []})
    assert cfg.local_ladders == {"Foundry-Chat": ["a", "b", "c"]} and cfg.ladder_families == {}


def test_a_kind_of_work_reads_like_the_plan_asks(tmp_path):
    svc = _svc(tmp_path)
    _suite(svc, "grounded")
    _run(svc, "grounded", FLASH, {"easy": [100, 100], "medium": [100, 60, 100], "hard": [20, 30]},
         seconds=31)
    _run(svc, "grounded", BRAIN, {"easy": [100, 0], "medium": [40, 0, 50], "hard": [0, 0]},
         seconds=38)
    _run(svc, "grounded", OPUS, {"easy": [100, 100], "medium": [100, 100, 100],
                                 "hard": [100, 80]}, seconds=9)
    f = bl.report(svc)["families"][0]
    assert f["suite"] == "grounded" and f["good_enough"] == [FLASH]
    assert f["sentence"] == ("Grounded answers (threshold 70): qwen3.8-flash passes at 72.9, "
                             "through medium, 31 s a case; hard needs claude-opus-5-5")
    assert [r["model"] for r in f["rungs"]] == [FLASH, BRAIN, OPUS], "local first, then cloud"
    assert [r["local"] for r in f["rungs"]] == [True, True, False]
    assert f["rungs"][0]["backend"] == "strata"


def test_nothing_local_passing_names_the_best_local_and_the_cloud_that_does(tmp_path):
    svc = _svc(tmp_path)
    _suite(svc, "codereason")
    _run(svc, "codereason", FLASH, {"easy": [60, 40]}, seconds=50)
    _run(svc, "codereason", SONNET, {"easy": [100, 80]}, seconds=7)
    s = bl.report(svc)["families"][0]["sentence"]
    assert s == ("Code reasoning (threshold 70): no local model passes; qwen3.8-flash misses at 50, "
                 "50 s a case; claude-sonnet-5-5 passes at 90, through easy, 7 s a case")


def test_only_the_current_version_counts_and_the_latest_three_runs_are_averaged(tmp_path):
    svc = _svc(tmp_path)
    old = _suite(svc, "needle", "v1")
    _run(svc, "needle", FLASH, {"easy": [100]}, h=old)
    _run(svc, "needle", BRAIN, {"easy": [100]}, h=old)
    _suite(svc, "needle", "v2")
    for score in (10, 70, 80, 90):
        _run(svc, "needle", FLASH, {"easy": [score]}, seconds=score)
    _run(svc, "needle", FLASH, {"easy": [100]}, status="running")
    ev = bl.evidence(svc)["needle"]
    assert ev[BRAIN] is None, "measured only on an older version"
    r = ev[FLASH]
    assert r["runs"] == 3 and r["score"] == 80.0 and (r["low"], r["high"]) == (70, 90)
    assert r["seconds"] == 80.0
    f = bl.report(svc)["families"][0]
    assert f["stale"] == [BRAIN] and "(3 runs, 70 to 90)" in f["sentence"]


# -- per persona ------------------------------------------------------------------------------

def _measured(svc):
    for s in ("grounded", "summaries", "heldrule"):
        _suite(svc, s)
    _run(svc, "grounded", FLASH, {"easy": [100], "medium": [50]})        # 75: passes
    _run(svc, "grounded", SONNET, {"easy": [100], "medium": [100]})
    _run(svc, "summaries", FLASH, {"easy": [40], "medium": [60]})        # 50: misses
    _run(svc, "summaries", BRAIN, {"easy": [100], "medium": [80]})       # 90: passes
    _run(svc, "heldrule", FLASH, {"easy": [30]})
    _run(svc, "heldrule", BRAIN, {"easy": [20]})
    _run(svc, "heldrule", OPUS, {"easy": [90]})


def test_a_persona_takes_the_first_rung_of_its_own_chain_that_is_good_enough(tmp_path):
    svc = _svc(tmp_path, ladder_families={"general_chat": ["grounded", "summaries", "heldrule"]})
    _measured(svc)
    _persona(svc, "Chat", [FLASH, BRAIN, SONNET, OPUS])
    p = next(x for x in bl.report(svc)["personas"] if x["persona"] == "Chat")
    assert p["ladder"] == [FLASH, BRAIN] and p["cloud"] == [SONNET, OPUS]
    # v0.181.251: flash holds only the easy tier of grounded answers and Sonnet the
    # medium one too, so Sonnet is needed there; before, flash's mean covered it
    assert [(f["suite"], f["rung"], f["cloud"]) for f in p["families"]] == [
        ("grounded", FLASH, SONNET), ("summaries", BRAIN, ""), ("heldrule", "", OPUS)]
    assert p["families"][0]["sentence"] == (
        "Grounded answers: qwen3.8-flash passes at 75, through easy, threshold 70; "
        "medium needs claude-sonnet-5-5")
    assert p["families"][2]["sentence"] == (
        "A rule held over a conversation: no local rung passes (qwen3.8-flash reaches 30, "
        "threshold 70); claude-opus-5-5 passes at 90")
    assert p["could_go"] == [] and p["verdict"] == (
        "every cloud rung is still needed for some kind of work: claude-sonnet-5-5 "
        "(Grounded answers), claude-opus-5-5 (A rule held over a conversation)")
    assert p["needed"] == {SONNET: ["grounded"], OPUS: ["heldrule"]}


def test_an_unmeasured_kind_of_work_means_no_cloud_rung_is_called_unneeded(tmp_path):
    svc = _svc(tmp_path)                       # general_chat: six suites, three measured
    _measured(svc)
    _persona(svc, "Chat", [FLASH, SONNET])
    p = next(x for x in bl.report(svc)["personas"] if x["persona"] == "Chat")
    assert p["could_go"] == [] and p["verdict"].startswith("not every kind of work is measured")
    assert "Instructions in the data" in p["verdict"]
    assert any(f["sentence"].endswith("not measured on this persona's models")
               for f in p["families"])


def test_the_operator_can_set_a_ladder_and_the_thresholds_move_the_answer(tmp_path):
    svc = _svc(tmp_path, ladder_families={"general_chat": ["grounded", "summaries"]},
               local_ladders={"Chat": [BRAIN, FLASH]}, ladder_thresholds={"grounded": 80})
    _measured(svc)
    _persona(svc, "Chat", [FLASH, SONNET])
    _persona(svc, "Off", [FLASH, SONNET], enabled=0)
    _persona(svc, "Empty", [])
    people = [p for p in bl.report(svc)["personas"] if p["persona"] in ("Chat", "Off", "Empty")]
    assert [p["persona"] for p in people] == ["Chat"], "enabled, with a chain"
    p = people[0]
    assert p["ladder"] == [BRAIN, FLASH] and p["ladder_set"]
    assert [(f["suite"], f["rung"], f["cloud"]) for f in p["families"]] == [
        ("grounded", "", SONNET), ("summaries", BRAIN, "")]
    assert p["verdict"] == ("every cloud rung is still needed for some kind of work: "
                            "claude-sonnet-5-5 (Grounded answers)")


def test_a_persona_whose_local_rungs_pass_everything_can_do_without_its_cloud(tmp_path):
    svc = _svc(tmp_path, ladder_families={"general_chat": ["grounded", "summaries"]})
    for s in ("grounded", "summaries"):
        _suite(svc, s)
        for _ in range(3):
            _run(svc, s, FLASH, {"easy": [100], "medium": [90]})
            _run(svc, s, SONNET, {"easy": [100], "medium": [100]})
    _persona(svc, "Chat", [FLASH, BRAIN, SONNET])
    _persona(svc, "Local", [FLASH, BRAIN])
    by = {p["persona"]: p for p in bl.report(svc)["personas"]}
    assert by["Chat"]["could_go"] == [SONNET]
    assert by["Chat"]["verdict"] == ("a local rung is good enough for every kind of work; the "
                                     "cloud rungs claude-sonnet-5-5 are not needed for it")
    assert by["Local"]["verdict"] == "no cloud rung in the chain"


def test_the_report_changes_nothing(tmp_path):
    svc = _svc(tmp_path, ladder_families={"general_chat": ["grounded", "summaries"]})
    _measured(svc)
    _persona(svc, "Chat", [FLASH, BRAIN, SONNET])
    before = json.dumps(svc.db.query("SELECT * FROM personas ORDER BY virtual_name"))
    runs = svc.db.query("SELECT COUNT(*) AS n FROM bench_runs")[0]["n"]
    bl.report(svc)
    assert json.dumps(svc.db.query("SELECT * FROM personas ORDER BY virtual_name")) == before
    assert svc.db.query("SELECT COUNT(*) AS n FROM bench_runs")[0]["n"] == runs
    assert svc.config_store.raw == {"bench": {"ladder_families": {
        "general_chat": ["grounded", "summaries"]}}}


def test_with_nothing_measured_the_report_says_so(tmp_path):
    out = bl.report(_svc(tmp_path))
    assert out["families"] == [] and out["note"].startswith("nothing measured yet")
    assert out["thresholds"]["grounded"] == 70.0
    assert out["category_families"]["coding"][0] == "codereason"


# -- the route, the settings, the page --------------------------------------------------------

class _Req:
    def __init__(self, body=None):
        self.body = body or {}

    async def json(self):
        return self.body


def test_the_route_reports_and_the_settings_are_saved_through_the_allow_list(tmp_path):
    from foundry_router.ui import routes
    svc = _svc(tmp_path)
    _measured(svc)
    import unittest.mock as um
    with um.patch.object(routes, "_svc", lambda request: svc):
        out = asyncio.run(routes.bench_ladder(_Req()))
        saved = asyncio.run(routes.set_bench(_Req({
            "ladder_thresholds": {"grounded": 80}, "local_ladders": {"Chat": "brain-35b"},
            "ladder_families": {"general_chat": "grounded, summaries"}, "nonsense": 1})))
        bad = asyncio.run(routes.set_bench(_Req({"ladder_thresholds": {"grounded": 300}})))
    assert [f["suite"] for f in out["families"]] == ["grounded", "heldrule", "summaries"]
    assert saved["ok"] and saved["bench"]["local_ladders"] == {"Chat": ["brain-35b"]}
    assert saved["bench"]["ladder_families"] == {"general_chat": ["grounded", "summaries"]}
    assert "nonsense" not in svc.config_store.raw["bench"]
    assert bad.status_code == 400 and svc.config_store.config.bench.ladder_thresholds == {
        "grounded": 80.0}
    assert "/admin/api/bench/ladder" in {r.path for r in routes.router.routes}


def test_the_page_has_the_ladder_card_and_saves_its_settings():
    page = (Path(foundry_router.__file__).parent / "ui" / "static" / "index.html"
            ).read_text(encoding="utf-8")
    tab = page[page.index("async function tabBench("):]
    assert 'id="ld-out"' in tab and 'id="ld-form"' in tab and 'id="ld-save"' in tab
    assert tab.index('id="ld-out"') < tab.index('id="suite-msg"'), "above the suites"
    assert "loadLadder();" in tab
    fn = page[page.index("async function loadLadder("):page.index("async function tabBench(")]
    for want in ('api("bench/ladder")', 'api("config/bench"', "ladder_thresholds",
                 "local_ladders", "ladder_families"):
        assert want in fn, want


# -- v0.181.251: what "could go" may claim ----------------------------------------------------

def _two_suites(svc, flash, sonnet, runs=1):
    for s in ("grounded", "summaries"):
        _suite(svc, s)
        for _ in range(runs):
            _run(svc, s, FLASH, flash[s])
            if s in sonnet:
                _run(svc, s, SONNET, sonnet[s])


def test_a_cloud_rung_never_run_is_not_called_unneeded(tmp_path):
    svc = _svc(tmp_path, ladder_families={"general_chat": ["grounded", "summaries"]})
    ok = {"easy": [100], "medium": [100]}
    _two_suites(svc, {"grounded": ok, "summaries": ok}, {"grounded": ok, "summaries": ok}, 3)
    _persona(svc, "Chat", [FLASH, SONNET, OPUS])
    p = next(x for x in bl.report(svc)["personas"] if x["persona"] == "Chat")
    assert p["could_go"] == [SONNET], "measured everywhere and never better"
    assert p["not_run"] == {OPUS: ["grounded", "summaries"]}
    assert p["verdict"] == ("not run on every kind of work, so not judged: claude-opus-5-5; "
                            "not needed for any kind of work: claude-sonnet-5-5")


def test_a_kind_of_work_nothing_passes_still_needs_the_cloud_rung_that_gets_further(tmp_path):
    svc = _svc(tmp_path, ladder_families={"general_chat": ["grounded"]})
    _suite(svc, "grounded")
    _run(svc, "grounded", FLASH, {"easy": [100], "medium": [0], "hard": [0], "expert": [0]})
    _run(svc, "grounded", SONNET, {"easy": [100], "medium": [100], "hard": [0],
                                   "expert": [0]})
    _persona(svc, "Chat", [FLASH, SONNET])
    p = next(x for x in bl.report(svc)["personas"] if x["persona"] == "Chat")
    assert p["could_go"] == [] and p["needed"] == {SONNET: ["grounded"]}
    assert p["families"][0]["sentence"] == (
        "Grounded answers: no local rung passes (qwen3.8-flash reaches 25, threshold 70); "
        "no model in the chain passes, claude-sonnet-5-5 gets furthest (through medium)")


def test_a_kind_of_work_nothing_passes_and_the_cloud_does_no_better_needs_no_cloud(tmp_path):
    svc = _svc(tmp_path, ladder_families={"general_chat": ["grounded"]})
    _suite(svc, "grounded")
    _run(svc, "grounded", FLASH, {"easy": [100], "medium": [0], "hard": [0]})
    _run(svc, "grounded", SONNET, {"easy": [100], "medium": [20], "hard": [0]})
    _persona(svc, "Chat", [FLASH, SONNET])
    p = next(x for x in bl.report(svc)["personas"] if x["persona"] == "Chat")
    assert p["could_go"] == [SONNET] and p["needed"] == {}
    f = bl.report(svc)["families"][0]
    assert "no cloud model passes either; claude-sonnet-5-5 misses at 40" in f["sentence"]


def test_could_go_on_one_run_says_it_is_a_lead_not_a_result(tmp_path):
    svc = _svc(tmp_path, ladder_families={"general_chat": ["grounded", "summaries"]})
    ok = {"easy": [100], "medium": [100]}
    _two_suites(svc, {"grounded": ok, "summaries": ok}, {"grounded": ok, "summaries": ok}, 1)
    _persona(svc, "Chat", [FLASH, SONNET])
    p = next(x for x in bl.report(svc)["personas"] if x["persona"] == "Chat")
    assert p["could_go"] == [SONNET]
    assert p["verdict"].endswith("so a lead to check, not a result")


def _untiered(svc, suite, model, scores):
    """A run of a suite whose cases carry no tier: the card tiers them from the runs."""
    db = svc.db
    h = bench.suite_hash(svc, suite)
    db.execute("INSERT INTO bench_runs (ts, suite, model, label, cases, scored, status, score, "
               "seconds, suite_hash) VALUES (datetime('now'), ?, ?, '', ?, ?, 'done', ?, 9, ?)",
               (suite, model, len(scores), len(scores), round(sum(scores) / len(scores), 1), h))
    rid = db.query_one("SELECT MAX(id) AS id FROM bench_runs")["id"]
    for i, s in enumerate(scores):
        db.execute("INSERT INTO bench_results (run_id, case_id, task_id, tags, score) "
                   "VALUES (?,?,?,?,?)", (rid, f"c{i}", "t", "[]", s))


def test_tiers_made_from_the_runs_are_not_compared_between_models(tmp_path):
    """Live, 2026-11-22: on the held-rule suite flash scored 99.4 and Sonnet 91,
    and with the tiers made from which model missed what, Sonnet "held medium"
    (the one case flash missed) and flash only easy - so "medium needs Sonnet"."""
    svc = _svc(tmp_path, ladder_families={"general_chat": ["heldrule"]})
    _suite(svc, "heldrule")
    flash = [100.0] * 20
    flash[3] = 0.0
    sonnet = [100.0] * 20
    for i in (10, 11, 12):
        sonnet[i] = 0.0
    _untiered(svc, "heldrule", FLASH, flash)
    _untiered(svc, "heldrule", SONNET, sonnet)
    ev = bl.evidence(svc)["heldrule"]
    assert ev[FLASH]["tiers_from"] == "runs"
    _persona(svc, "Chat", [FLASH, SONNET])
    p = next(x for x in bl.report(svc)["personas"] if x["persona"] == "Chat")
    assert p["needed"] == {} and p["could_go"] == [SONNET]
    f = bl.report(svc)["families"][0]
    assert "needs" not in f["sentence"]


def test_a_cloud_rung_more_than_the_noise_higher_is_needed_without_tiers(tmp_path):
    svc = _svc(tmp_path, ladder_families={"general_chat": ["heldrule"]})
    _suite(svc, "heldrule")
    _untiered(svc, "heldrule", FLASH, [100.0] * 8 + [0.0] * 2)      # 80
    _untiered(svc, "heldrule", SONNET, [100.0] * 10)                # 100
    _persona(svc, "Chat", [FLASH, SONNET])
    p = next(x for x in bl.report(svc)["personas"] if x["persona"] == "Chat")
    assert p["needed"] == {SONNET: ["heldrule"]}
    assert p["families"][0]["sentence"].endswith("; claude-sonnet-5-5 does better at 100")
    f = bl.report(svc)["families"][0]
    assert f["sentence"].endswith("claude-sonnet-5-5 does better at 100")
