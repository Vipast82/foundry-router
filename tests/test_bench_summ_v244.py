"""v0.181.244: summaries that invent nothing.

Video notes and infrastructure pulses are summaries (docs/BENCH_GAPS.md,
addition 6). A case is a service log or a call transcript with one subject in
it - a service, an item - and look-alikes beside it that carry the same kinds
of facts. Every figure the summary gives is looked up in the source: on a
line about the subject it is fine, found only about something else or found
nowhere it scores nothing, and so does running over the word limit.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

import foundry_router
from foundry_router import bench, benchcard, benchsumm as bs
from foundry_router.db import Database
from foundry_router.pool.protocols import ChatResult

SPECS = bs.summ_specs()
IDS = [sp["task"] for sp in SPECS]
BY_ASK = {sp["messages"][-1]["content"]: sp for sp in SPECS}
REF = ("facts", "mine", "theirs", "names", "places", "codes", "cap")


def _svc(tmp_path, pool=None):
    cfg = type("Cfg", (), {"code_checks": None})()
    return type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "r.sqlite"), "pool": pool,
        "config_store": type("CS", (), {"config": cfg})(),
        "registry": type("R", (), {"get": staticmethod(lambda m: None)})(),
    })()


def _ref(sp) -> dict:
    return {k: sp[k] for k in REF}


def _source(sp) -> list:
    return sp["messages"][-1]["content"].split("\n\n")[1].split("\n")


# -- what a summary states ----------------------------------------------------------------------

def test_a_time_is_the_same_time_however_it_is_written():
    for text in ("at 14:05", "at 14:05:31", "at 2:05 pm", "at 2:05pm", "at 2.05 p.m.",
                 "at 02:05 PM"):
        assert "t:14:05" in bs.tokens(text), text
    assert bs.tokens("at 7 am") == {"t:07:00"} and bs.tokens("12:30 am") == {"t:00:30"}
    assert bs.tokens("13 pm") == set() and bs.tokens("25:10") == set()


def test_numbers_of_eleven_or_more_count_and_parts_of_other_things_do_not():
    got = bs.tokens("1,204 requests failed, 3 retries, 10 minutes, INC-48213, KX-4471, "
                    "deposit " + bs.POUND + "2,118, 44%")
    assert got == {"n:1204", "n:48213", "n:4471", "n:2118", "n:44"}
    assert bs.tokens("deployed v4.17.2 on 2026-11-18") == {"v:4.17.2"}
    assert bs.tokens("p99 and /v1/items and worker pool 3/8") == set()
    assert bs.tokens("the 11:30 train") == {"t:11:30"}, "a time is not two numbers"


def test_a_code_reads_the_same_in_capitals_or_in_words():
    codes = ["ERR_POOL_EXHAUSTED", "ERR_POOL_TIMEOUT"]
    assert bs.tokens("ERR_POOL_EXHAUSTED: no free connections", codes=codes) == {
        "c:pool exhausted"}
    assert bs.tokens("the connection pool was exhausted", codes=codes) == set()
    assert bs.tokens("a pool timeout at noon", codes=codes) == {"c:pool timeout"}
    assert bs.tokens("ERR_DNS_STALE") == {"c:dns stale"}, "a code the source never had"


def test_names_and_places_count_only_when_the_source_uses_them():
    got = bs.tokens("Dana Okafor booked Cardross Hall with Will", names=["Dana", "Okafor"],
                    places=["Cardross"])
    assert got == {"w:dana", "w:okafor", "p:cardross"}
    assert bs.tokens("dana booked it", names=["Dana"]) == set(), "names are written as names"


def _r(**kw):
    ref = {"facts": [{"label": "count", "any": ["n:1204"]}, {"label": "time", "any": ["t:10:03"]},
                     {"label": "name", "any": ["w:dana", "w:okafor"]},
                     {"label": "error", "any": ["c:pool exhausted"]}],
           "mine": ["n:1204", "t:10:03", "w:dana", "w:okafor", "c:pool exhausted", "n:2026",
                    "n:18", "t:09:58"],
           "theirs": ["n:5686", "t:13:01", "w:nadia", "c:pool timeout"],
           "names": ["Dana", "Okafor", "Nadia"], "places": [],
           "codes": ["ERR_POOL_EXHAUSTED", "ERR_POOL_TIMEOUT"], "cap": 30}
    ref.update(kw)
    return ref


def test_every_fact_and_nothing_else_is_full_and_anything_else_is_nothing():
    full = "1,204 requests failed; restarted at 10:03; paged Dana; ERR_POOL_EXHAUSTED."
    s = bs.summ_score(full, _r())
    assert s["score"] == 100 and s["why"] == "" and s["detail"] == "facts=4/4 words=9/30"
    s = bs.summ_score(full + " The gateway also restarted at 13:01.", _r())
    assert s["score"] == 0 and s["why"] == "theirs" and s["theirs"] == ["t:13:01"]
    s = bs.summ_score(full + " Down for 23 minutes.", _r())
    assert s["score"] == 0 and s["why"] == "invented" and "invented=n:23" in s["detail"]
    s = bs.summ_score(full + " A pool timeout too.", _r())
    assert s["why"] == "theirs" and "theirs=c:pool_timeout" in s["detail"]
    assert bs.summ_score(full, _r(cap=8))["why"] == "too_long"
    assert bs.summ_score("  ", _r())["why"] == "nothing"


def test_leaving_facts_out_is_half_down_to_half_of_them_and_nothing_below():
    s = bs.summ_score("1,204 requests failed and it restarted at 10:03.", _r())
    assert s["score"] == 50 and s["why"] == "some_left_out" and s["missing"] == ["name", "error"]
    s = bs.summ_score("It restarted at 10:03.", _r())
    assert s["score"] == 0 and s["why"] == "left_out"
    s = bs.summ_score("It restarted at 10:03, after 09:58.", _r())
    assert s["why"] == "left_out", "another figure about the subject is fine, not a fact"


# -- every case ---------------------------------------------------------------------------------

@pytest.mark.parametrize("sp", SPECS, ids=IDS)
def test_the_ideal_summary_is_full_and_one_fact_about_a_look_alike_is_nothing(sp):
    ref = _ref(sp)
    assert bs.summ_score(sp["ideal"], ref)["score"] == 100
    slip = bs.summ_score(sp["slip"], ref)
    assert slip["score"] == 0 and slip["why"] == "theirs"
    mine, theirs = set(sp["mine"]), set(sp["theirs"])
    assert not mine & theirs
    for f in sp["facts"]:
        assert f["any"] and all(t in mine for t in f["any"]), f
    assert sp["cap"] == bs.word_cap(len(sp["facts"]))
    lines = _source(sp)
    assert len(lines) == sp["lines"]
    ask = sp["messages"][-1]["content"]
    assert f"at most {sp['cap']} words" in ask and sp["subject"] in ask
    assert sp["messages"][0] == {"role": "system", "content": bs.SYSTEM}


@pytest.mark.parametrize("sp", [s for s in SPECS if s["kind"] == "log"],
                         ids=[s["task"] for s in SPECS if s["kind"] == "log"])
def test_a_log_names_its_service_on_every_line_and_every_figure_is_said_once(sp):
    lines = _source(sp)
    services = [sp["subject"]] + sp["others"]
    stamps = []
    for ln in lines:
        t, svc, lvl = ln.split(" ")[:3]
        assert svc in services and lvl in ("INFO", "WARN", "ERROR", "DEBUG"), ln
        stamps.append(t)
    assert stamps == sorted(stamps), "a log runs forward in time"
    text = "\n".join(lines)
    for n in re.findall(r"\d{1,3}(?:,\d{3})+|\b\d{3,}\b", text):
        if n not in ("200",):
            assert text.count(n) == 1 or "correction" in text, n
    for f in sp["facts"]:
        if f["any"][0].startswith("t:"):        # a look-alike never shares the subject's minute
            assert f["any"][0] not in sp["theirs"]


def test_the_three_dials_climb_and_hold_everything_else():
    ladders: dict = {}
    for sp in SPECS:
        if sp["ladder"]:
            ladders.setdefault(sp["ladder"], []).append(sp)
    assert len(ladders) == 6 and all(len(v) == bs.SUMM_TOP for v in ladders.values())
    for kind in bs.KINDS:
        length = ladders[f"summary:{kind} length"]
        assert [sp["lines"] for sp in length] == sorted(sp["lines"] for sp in length)
        assert length[-1]["lines"] >= bs.LENGTHS[-1] - 2
        assert {len(sp["facts"]) for sp in length} == {bs.BASE_FACTS}
        facts = ladders[f"summary:{kind} facts"]
        assert [len(sp["facts"]) for sp in facts] == list(bs.FACT_COUNTS)
        near = ladders[f"summary:{kind} near"]
        assert [sp["decoys"] for sp in near] == list(bs.NEAR_COUNTS)
    for sp in ladders["summary:log near"]:
        fam = sp["subject"].split("-")[0]
        assert all(o.startswith(fam + "-") for o in sp["others"]), "all of the same family"
    for sp in ladders["summary:talk near"]:
        tail = sp["subject"].split(" ", 2)[-1]
        assert all(o.endswith(tail) for o in sp["others"]), (sp["subject"], sp["others"])
    assert bs.summ_specs() == SPECS, "generated the same way every time"


def _single(kind, variant, seed=0):
    return next(sp for sp in SPECS if sp["kind"] == kind and sp["variant"] == variant
                and sp["seed"] == seed)


@pytest.mark.parametrize("kind", bs.KINDS)
def test_a_figure_corrected_later_is_kept_in_its_corrected_form(kind):
    sp = _single(kind, "correction")
    lines = _source(sp)
    fixed = [i for i, ln in enumerate(lines) if "correction" in ln.lower()
             or "got that wrong" in ln]
    assert len(fixed) == 1
    new, old = re.findall(r"(\d[\d,]*)[^\d]*?, not (\d[\d,]*)", lines[fixed[0]])[0]
    first = [i for i, ln in enumerate(lines) if re.search(rf"(?<![\d,]){re.escape(old)}(?![\d,])",
                                                          ln) and i != fixed[0]]
    assert first and first[0] < fixed[0], "said before it is corrected"
    labels = {f["label"]: f["any"] for f in sp["facts"]}
    assert labels["corrected " + ("count" if kind == "log" else "number")] == [
        "n:" + new.replace(",", "")]
    assert all("n:" + old.replace(",", "") not in f["any"] for f in sp["facts"])


def test_a_quiet_subject_has_two_facts_and_loud_neighbours():
    for kind in bs.KINDS:
        sp = _single(kind, "quiet")
        assert len(sp["facts"]) == 2 and sp["cap"] == 50
        assert len([t for t in sp["theirs"] if t.startswith("n:")]) >= 4
    log = _single("log", "quiet")
    mine = [ln for ln in _source(log) if f" {log['subject']} " in ln]
    assert not [ln for ln in mine if " WARN " in ln or " ERROR " in ln]


def test_the_late_log_puts_every_fact_in_the_last_lines_and_the_aside_comes_in_another_item():
    sp = _single("log", "late")
    tail = _source(sp)[-len(sp["facts"]):]
    assert all(f" {sp['subject']} " in ln for ln in tail)
    sp = _single("talk", "aside")
    lines = _source(sp)
    at = next(i for i, ln in enumerate(lines) if "before I forget" in ln)
    assert lines[at].startswith(bs.CHAIR + ": Oh, before I forget, on " + sp["subject"])
    intro = max(i for i, ln in enumerate(lines[:at]) if ln.startswith(bs.CHAIR + ": Next, ")
                or ln.startswith(bs.CHAIR + ": First, "))
    assert sp["subject"] not in lines[intro], "said inside another item"
    assert "reference" in [f["label"] for f in sp["facts"]]


# -- built and run ------------------------------------------------------------------------------

def test_the_suite_is_built_as_six_ladders_and_twelve_singles_with_no_tier(tmp_path):
    svc = _svc(tmp_path)
    out = bench.build_summaries(svc)
    cases = bench.cases(svc, "summaries")
    assert out["cases"] == len(cases) == 60 and len({c["id"] for c in cases}) == 60
    ladders: dict = {}
    for c in cases:
        sm = c["reference"]["summary"]
        assert sm["facts"] and sm["mine"] and sm["cap"] and c["max_tokens"] == bench.THINK_ROOM
        assert not any(t.startswith("tier:") for t in c["tags"]) and "weight" not in c
        assert any(t.startswith("tokens:") for t in c["tags"]) and not c.get("tools")
        lad, lvl = bench.ladder_of(c)
        if lad:
            ladders.setdefault(lad, []).append(lvl)
    assert len(ladders) == 6 and all(v == list(range(1, 9)) for v in ladders.values())
    one = next(c for c in cases if c["id"] == "SUMM-log-length-1")
    exp = bench.expected_of(one)
    assert exp.startswith(f"a summary of {one['reference']['summary']['subject']} in at most 70 "
                          f"words that keeps ") and "nothing the source does not say" in exp
    assert bench.build_summaries(svc, top_level=3)["cases"] == 6 * 3 + 12


class _Voice:
    """Says `how(case)` to whatever source it is shown."""

    def __init__(self, how):
        self.how, self.seen = how, 0

    async def chat(self, model, messages, tools=None, max_tokens=0, options=None):
        self.seen += 1
        assert not tools, "these cases offer no tools"
        return ChatResult(content=self.how(BY_ASK[messages[-1]["content"]]),
                          finish_reason="stop", completion_tokens=60,
                          prompt_tokens=900), "b"


def _run(tmp_path, how, keep):
    svc = _svc(tmp_path, _Voice(how))
    bench.build_summaries(svc)
    cases = [c for c in bench.cases(svc, "summaries") if keep(c)]
    assert cases
    (bench._dir(svc) / "summaries.jsonl").write_text(
        "".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
    import unittest.mock as um
    with um.patch.object(bench, "_record", lambda *a, **k: None), \
            um.patch.object(bench, "_state", {"state": "idle"}):
        out = asyncio.run(bench._run(svc, "summaries", "m", "", 0))
    rows = svc.db.query("SELECT * FROM bench_results WHERE run_id=? ORDER BY id", (out["run_id"],))
    return out, rows


def test_a_model_that_keeps_to_its_subject_scores_full(tmp_path):
    out, rows = _run(tmp_path, lambda sp: sp["ideal"],
                     lambda c: bench.ladder_of(c)[1] in (1, 5) or "dial:quiet" in c["tags"])
    assert out["score"] == 100.0 and len(rows) == 16
    assert all(re.fullmatch(r"facts=(\d+)/\1 words=\d+/\d+", r["code_detail"]) for r in rows)
    assert all(benchcard.miss_kind("summaries", r) is None for r in rows)


def test_a_fact_about_a_look_alike_or_a_figure_from_nowhere_is_nothing(tmp_path):
    (tmp_path / "a").mkdir()
    out, rows = _run(tmp_path / "a", lambda sp: sp["slip"],
                     lambda c: c["id"] in ("SUMM-log-near-6", "SUMM-talk-near-6"))
    assert out["score"] == 0.0
    assert [benchcard.miss_kind("summaries", r) for r in rows] == [
        "a fact about something else"] * 2
    (tmp_path / "b").mkdir()
    out, rows = _run(tmp_path / "b", lambda sp: sp["ideal"] + " It took 9,999 seconds.",
                     lambda c: c["id"] == "SUMM-log-facts-3")
    assert out["score"] == 0.0 and "invented=n:9999" in rows[0]["code_detail"]
    assert benchcard.miss_kind("summaries", rows[0]) == "a figure that is not in the source"


def test_too_long_silence_and_half_the_facts_are_scored_and_named(tmp_path):
    (tmp_path / "a").mkdir()
    out, rows = _run(tmp_path / "a", lambda sp: sp["ideal"] + " and so on" * 40,
                     lambda c: c["id"] == "SUMM-talk-facts-3")
    assert out["score"] == 0.0 and benchcard.miss_kind("summaries", rows[0]) == \
        "over the word limit"
    (tmp_path / "b").mkdir()
    out, rows = _run(tmp_path / "b", lambda sp: "", lambda c: c["id"] == "SUMM-talk-facts-3")
    assert out["score"] == 0.0 and benchcard.miss_kind("summaries", rows[0]) == "said nothing"
    (tmp_path / "c").mkdir()
    out, rows = _run(tmp_path / "c",
                     lambda sp: sp["ideal"].split(";")[0] + ";" + sp["ideal"].split(";")[1] + ".",
                     lambda c: c["id"] == "SUMM-talk-facts-3")
    assert out["score"] == 50.0 and rows[0]["code_detail"].startswith("facts=2/4 ")
    assert benchcard.miss_kind("summaries", rows[0]) == "left out some facts"


def test_a_summary_past_the_budget_counts_for_nothing(tmp_path):
    sp = SPECS[0]
    case = {"reference": {"summary": {k: sp[k] for k in REF}}}
    svc = _svc(tmp_path)
    ok = asyncio.run(bench.score_one(svc, case, {"content": sp["ideal"]}))
    late = asyncio.run(bench.score_one(svc, case, {"content": sp["ideal"], "out_of_room": 8192}))
    assert ok["score"] == 100.0 and ok["tools"] == 1.0 and ok["answer"] == 1.0
    assert late["score"] == 0.0 and "why=nothing" in late["code_detail"]


# -- the card, the route, the page --------------------------------------------------------------

def test_the_card_names_the_suite_and_breaks_it_down():
    def row(source, dial, lines, score, detail, ladder="", level=0):
        tags = ["summaries", f"source:{source}", f"dial:{dial}", f"lines:{lines}"]
        if ladder:
            tags += [f"ladder:summary:{source} {ladder}", f"level:{level}"]
        return {"score": score, "error": "", "code_detail": detail, "tags": json.dumps(tags),
                "tools_expected": "[]", "tools_called": "[]", "task_id": "x", "case_id": "c"}
    rows = [row("log", "length", 24, 100.0, "facts=4/4 words=12/70", "length", 1),
            row("log", "length", 1280, 0.0, "facts=4/4 words=20/70 why=theirs theirs=n:5686",
                "length", 8),
            row("talk", "near", 200, 50.0, "facts=2/4 words=20/70 why=some_left_out", "near", 4),
            row("talk", "quiet", 119, 0.0, "facts=2/2 words=9/50 why=invented invented=n:23"),
            row("log", "correction", 120, 100.0, "facts=4/4 words=30/70")]
    f = {x["name"]: x["rows"] for x in benchcard.facets("summaries", rows)}
    assert [(r["label"], r["n"]) for r in f["The source"]] == [
        ("a service log", 3), ("a call transcript", 2)]
    assert [r["label"] for r in f["What was raised"]] == [
        "length of the source", "look-alikes beside it", "a figure corrected later",
        "a quiet subject"]
    assert [(r["label"], r["n"]) for r in f["Length of the source"]] == [
        ("up to 100 lines", 1), ("101 to 400 lines", 3), ("more than 400 lines", 1)]
    assert "What the verified step was" not in f
    assert [benchcard.miss_kind("summaries", r) for r in rows] == [
        None, "a fact about something else", "left out some facts",
        "a figure that is not in the source", None]
    assert benchcard.SUITE_NAMES["summaries"] == "Summaries that invent nothing"
    assert "summaries" in benchcard.SUITE_ASKS


def test_the_route_builds_it_and_the_page_has_its_plate(tmp_path):
    from foundry_router.ui import routes
    svc = _svc(tmp_path)

    class Req:
        def __init__(self, body):
            self.body = body

        async def json(self):
            return self.body

    import unittest.mock as um
    with um.patch.object(routes, "_svc", lambda request: svc):
        out = asyncio.run(routes.bench_summaries(Req({})))
        less = asyncio.run(routes.bench_summaries(Req({"top_level": 4, "name": "s4"})))
    assert out["ok"] and out["cases"] == 60 and out["name"] == "summaries"
    assert less["cases"] == 36 and less["name"] == "s4"
    assert "/admin/api/bench/summaries" in {r.path for r in routes.router.routes}
    page = (Path(foundry_router.__file__).parent / "ui" / "static" / "index.html"
            ).read_text(encoding="utf-8")
    for want in ('id="bench-summaries"', 'id="bench-summaries-top"', 'api("bench/summaries"',
                 'suiteNote("summaries")'):
        assert want in page, want
