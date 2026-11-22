"""v0.181.228: grounded answers over work records.

The ecosystem's main work use is answering from retrieved, tiered documents,
and it was measured by two eval prompts with one sentence of context each
(docs/BENCH_GAPS.md, addition 1). This suite hands a model a stack of records
with ids, tiers and dates and asks something with exactly one right reply.

Calibrated on qwen3.8-flash, Sonnet 5.5 and Opus 5.5. The six look-ups -
including "use only this tier", "the record against what you believe" and
"no record has it" - came back right from all three, 54 of 54, to 160
records. Adding up across records is what separates them: flash to 26
records, Sonnet to 140, Opus to 190, nobody at 240. Two findings on the way
were the generator's own faults, each first seen as both Claude models
"missing" the same case: records with duplicate ids, and a key that counted
records it had run out of names to write.

Every answer below is worked out again here from the records themselves.
"""
import asyncio
import inspect
import re
from pathlib import Path

import pytest

import foundry_router
from foundry_router import bench, benchcard, benchhard as bh
from foundry_router.db import Database

FIELD = {"retainer": r"([\d,]+) USD", "renewal": r"(2027-\d\d-\d\d)",
         "lead": r"Account lead: (\w+ \w+)|(\w+ \w+) looks after the account"
                 r"|(\w+ \w+) was named account lead",
         "terms": r"within (\d+) days|at (\d+) days"}
ASKS = {"monthly retainer": "retainer", "renew": "renewal", "account lead": "lead",
        "invoices": "terms"}


def _parse(record: str) -> dict:
    head, body = record.split("\n", 1)
    rid, coll, tier, dated = (x.strip() for x in head.split("|"))
    out = {"id": rid, "collection": coll.split(": ")[1], "tier": tier.split(": ")[1],
           "date": dated.split(" ")[1], "body": body, "name": ""}
    m = (re.match(r"Note on (.+?)\. ", body) or re.match(r"Minutes, (.+?) review: ", body)
         or re.match(r"(.+?): ", body))
    if m and out["collection"] == "clients":
        out["name"] = m.group(1)
    for f, rx in FIELD.items():
        g = re.search(rx, body)
        out[f] = next((x for x in g.groups() if x), None) if g else None
    return out


def _field(question: str) -> str:
    return next(f for words, f in ASKS.items() if words in question)


def _svc(tmp_path):
    cfg = type("Cfg", (), {"code_checks": None})()
    return type("S", (), {"capture": type("C", (), {"data_dir": str(tmp_path)})(),
                          "db": Database(tmp_path / "r.sqlite"),
                          "config_store": type("CS", (), {"config": cfg})()})()


CASES = [(lv, sd) for lv in (1, 3, 6) for sd in (0, 1)]


# -- every answer, a second way -------------------------------------------------------------

@pytest.mark.parametrize("level,seed", CASES)
def test_cite_and_join_name_the_record_that_has_it(level, seed):
    sp = bh.ground_spec("cite", level, seed)
    recs = [_parse(r) for r in sp["records"]]
    mine = [r for r in recs if r["name"] == sp["task"]]
    assert len(mine) == 1
    assert sp["want"] == [mine[0][_field(sp["question"])], mine[0]["id"]]
    sp = bh.ground_spec("join", level, seed)
    recs = [_parse(r) for r in sp["records"]]
    code = re.search(r"project (\w+) is run for", sp["question"])[1]
    proj = [r for r in recs if f"Project {code} is run for" in r["body"]]
    assert len(proj) == 1, "one project of that name"
    client = re.search(r"is run for (.+?)\. It has", proj[0]["body"])[1]
    mine = next(r for r in recs if r["name"] == client)
    assert sp["want"] == [mine[_field(sp["question"])], mine["id"], proj[0]["id"]]


@pytest.mark.parametrize("level,seed", CASES)
def test_the_latest_dated_record_wins_a_conflict(level, seed):
    sp = bh.ground_spec("conflict", level, seed)
    f = _field(sp["question"])
    mine = sorted((r for r in map(_parse, sp["records"]) if r["name"] == sp["task"]),
                  key=lambda r: r["date"])
    assert len(mine) == min(6, 2 + level // 2), "more dated versions the higher the level"
    assert len({r["date"] for r in mine}) == len(mine)
    assert sp["want"] == [mine[-1][f], mine[-1]["id"]]
    for old in mine[:-1]:
        assert old["id"] in sp["avoid"] and old[f] in sp["avoid"] and old[f] != mine[-1][f]


@pytest.mark.parametrize("level,seed", CASES)
def test_not_found_means_no_record_says(level, seed):
    sp = bh.ground_spec("absent", level, seed)
    assert sp["want"] == [bh.NOT_FOUND]
    asked = re.search(r"(?:for|with|to) ([A-Z]\w+ [A-Z]\w+)", sp["question"])[1]
    f = _field(sp["question"])
    mine = [r for r in map(_parse, sp["records"]) if r["name"] == asked]
    if seed % 2:
        assert len(mine) == 1 and mine[0][f] is None, "it has a record, and the record does not say"
    else:
        assert mine == [], "nothing is about that client"
    # ... while a client with a near name does have the field
    near = [r for r in map(_parse, sp["records"])
            if r["name"] and r["name"] != asked and set(r["name"].split()) & set(asked.split())]
    assert near and all(r[f] for r in near)


@pytest.mark.parametrize("level,seed", CASES)
def test_the_tier_rule_decides_which_record_may_answer(level, seed):
    sp = bh.ground_spec("scope", level, seed)
    f = _field(sp["question"])
    mine = [r for r in map(_parse, sp["records"]) if r["name"] == sp["task"]]
    secret = [r for r in mine if r["tier"] == "work_confidential"]
    public = [r for r in mine if r["tier"] == "work_general"]
    assert len(secret) == 1 and secret[0]["id"] in sp["avoid"] and secret[0][f] in sp["avoid"]
    assert "none tiered work_confidential" in sp["question"]
    if seed % 2:
        assert public == [] and sp["want"] == [bh.NOT_FOUND]
    else:
        assert len(public) == 1 and sp["want"] == [public[0][f], public[0]["id"]]
        assert public[0][f] != secret[0][f]


@pytest.mark.parametrize("level,seed", CASES + [(9, 0), (12, 0), (14, 1)])
def test_a_tally_counts_and_adds_every_matching_record(level, seed):
    sp = bh.ground_spec("tally", level, seed)
    lead = re.search(r"have (.+?) as account lead", sp["question"])[1]
    recs = [_parse(r) for r in sp["records"]]
    hits = [r for r in recs if r["lead"] == lead]
    total = sum(int(r["retainer"].replace(",", "")) for r in hits)
    assert sp["want"] == [str(len(hits)), str(total)]
    assert len(hits) == bh._size(bh._GROUND_TALLY, level), "the key counts what was written"


def test_a_tally_too_big_for_the_names_is_refused_not_miscounted():
    """The first version wrote fewer records than its key counted, and both
    Claude models scored exactly half on it."""
    with pytest.raises(ValueError, match="more client names"):
        bh.ground_spec("tally", 20, 0)
    assert len(bh.ground_specs(99)) == len(bh.ground_specs(bh.GROUND_MAX))


def test_the_record_wins_over_what_a_model_believes():
    seen = set()
    for seed in range(12):
        sp = bh.ground_spec("counter", 3, seed)
        book = [r for r in map(_parse, sp["records"]) if r["collection"] == "handbook"]
        assert len(book) == 1 and sp["want"][1] == book[0]["id"]
        assert re.search(rf"\b{sp['want'][0]}\b", book[0]["body"])
        assert len(sp["avoid"]) == 1 and sp["avoid"][0] != sp["want"][0]
        seen.add(sp["question"])
    assert len(seen) >= 5
    for _q, _says, value, believed in bh._G_COUNTER:
        assert value != believed


def test_no_id_can_be_mistaken_for_a_value_and_none_repeats():
    for kind in bh.GROUND_KINDS:
        for level, seed in CASES + [(8, 0)]:
            sp = bh.ground_spec(kind, level, seed)
            ids = [r.split(" ")[0] for r in sp["records"]]
            assert len(set(ids)) == len(ids), (kind, level, seed)
            assert all(re.fullmatch(r"KB-[A-Z]{4}", i) for i in ids)
            assert bh.ground_spec(kind, level, seed) == sp, "a rebuild is the same case"
    # records are written three ways, so a value is not always in the same place
    bodies = [r.split("\n", 1)[1] for r in bh.ground_spec("cite", 6, 0)["records"]]
    assert {b.split(" ")[0] for b in bodies} >= {"Note", "Minutes,"}


# -- in the suite, and scored ------------------------------------------------------------------

def test_the_suite_has_every_tier_and_one_ladder(tmp_path):
    svc = _svc(tmp_path)
    out = bench.build_grounded(svc)
    assert out["tiers"] == {"easy": 27, "medium": 4, "hard": 4, "expert": 4}
    cases = bench.cases(svc, "grounded")
    assert len(cases) == 39 and len({c["id"] for c in cases}) == 39
    ladder = [c for c in cases if bench.ladder_of(c)[0]]
    assert {bench.ladder_of(c)[0] for c in ladder} == {"ground:tally"}
    levels = [bench.ladder_of(c)[1] for c in ladder]
    assert levels == sorted(levels), "lowest first, so a run can stop climbing"
    order = list(bench.TIERS)
    ranks = [order.index(benchcard._tag(c["tags"], "tier")) for c in ladder]
    assert ranks == sorted(ranks)
    share: dict = {}
    for c in cases:
        t = benchcard._tag(c["tags"], "tier")
        share[t] = share.get(t, 0) + c["weight"]
        assert c["max_tokens"] == bench.THINK_ROOM and c["reference"]["final"]
        assert "ANSWER: NOT FOUND" in c["messages"][1]["content"]
    assert max(share.values()) - min(share.values()) < 0.01
    assert len(bench.cases(svc, bench.build_grounded(svc, top_level=8)["name"])) == 24 + 5


def test_a_reply_is_scored_on_its_value_and_its_citation(tmp_path):
    svc = _svc(tmp_path)
    bench.build_grounded(svc)
    by = {c["id"]: c for c in bench.cases(svc, "grounded")}

    def score(cid, said):
        return asyncio.run(bench.score_one(svc, by[cid], {"content": said}))["score"]

    c = by["GROUND-cite-L3-0"]
    value, rid = c["reference"]["must_include"]
    assert score("GROUND-cite-L3-0", f"It is in {rid}.\nANSWER: {value} [{rid}]") == 100.0
    assert score("GROUND-cite-L3-0", f"ANSWER: {value}") == 50.0, "the value, no record named"
    near = c["reference"]["must_exclude"][0]
    assert score("GROUND-cite-L3-0", f"ANSWER: {near} [{rid}]") == 0.0, "a near client's value"
    assert score("GROUND-absent-L3-0", "No record is about that client.\nANSWER: NOT FOUND") == 100.0
    wrong = by["GROUND-absent-L3-0"]["reference"]["must_exclude"][0]
    assert score("GROUND-absent-L3-0", f"ANSWER: {wrong}") == 0.0
    c = by["GROUND-counter-L3-0"]
    value, rid = c["reference"]["must_include"]
    believed = c["reference"]["must_exclude"][0]
    assert score("GROUND-counter-L3-0", f"ANSWER: {value} [{rid}]") == 100.0
    assert score("GROUND-counter-L3-0", f"ANSWER: {believed}") == 0.0
    c = by["GROUND-tally-L2-0"]
    n, total = c["reference"]["must_include"]
    assert score("GROUND-tally-L2-0", f"ANSWER: {n} clients, {int(total):,} USD") == 100.0
    assert score("GROUND-tally-L2-0", f"ANSWER: {n} clients, {int(total) + 50:,} USD") == 50.0


def test_the_card_says_how_a_grounded_answer_went_wrong():
    def row(tags, score, expected, answer):
        return {"score": score, "error": "", "code_detail": "", "expected": expected,
                "answer": answer, "tags": '["grounded","' + tags + '"]',
                "tools_expected": "[]", "tools_called": "[]", "task_id": "x"}
    kind = lambda *a: benchcard.miss_kind("grounded", row(*a))            # noqa: E731
    assert kind("g-absent", 0.0, "NOT FOUND", "ANSWER: 4,750") == \
        "answered when the documents did not say"
    assert kind("g-cite", 0.0, "4,750 and KB-ABCD", "ANSWER: NOT FOUND") == \
        "said not found when a document had it"
    assert kind("g-cite", 50.0, "4,750 and KB-ABCD", "ANSWER: 4,750") == \
        "right value, citation missing or wrong"
    assert kind("g-counter", 0.0, "8443 and KB-ABCD", "ANSWER: 443") == \
        "answered from what it believes, not the document"
    assert kind("g-scope", 0.0, "4,750 and KB-ABCD", "ANSWER: 6,100 [KB-WXYZ]") == \
        "used a document outside the tier it was told to use"
    assert kind("g-tally", 50.0, "26 and 154800", "ANSWER: 26, 150000") == "wrong count or total"
    assert benchcard.SUITE_NAMES["grounded"] == "Grounded answers"
    rows = [row("g-cite", 100.0, "x", "x"), row("g-tally", 0.0, "x", "x")]
    f = {x["name"]: x["rows"] for x in benchcard.facets("grounded", rows)}
    assert [r["label"] for r in f["Kind of question"]] == \
        ["one record has it", "a total over many records"]


def test_the_route_builds_it_and_the_page_has_its_plate(tmp_path):
    from foundry_router.ui import routes
    svc = _svc(tmp_path)

    class Req:
        async def json(self):
            return {"top_level": 6}

    import unittest.mock as um
    with um.patch.object(routes, "_svc", lambda request: svc):
        out = asyncio.run(routes.bench_grounded(Req()))
    assert out["ok"] and out["cases"] == 27 and out["tiers"] == {"easy": 27}
    assert "/admin/api/bench/grounded" in {r.path for r in routes.router.routes}
    page = (Path(foundry_router.__file__).parent / "ui" / "static" / "index.html"
            ).read_text(encoding="utf-8")
    for want in ('id="bench-grounded"', 'id="bench-grounded-top"', 'api("bench/grounded"',
                 'top_level: +($("#bench-grounded-top")', 'suiteNote("grounded")'):
        assert want in page, want
    assert "exec(" not in inspect.getsource(bh)
