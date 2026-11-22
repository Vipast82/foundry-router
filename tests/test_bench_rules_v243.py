"""v0.181.243: a rule held across a conversation.

Household and work personas carry rules, and people say once how they want
things: 24-hour times, dates as YYYY-MM-DD, "call me Tobi", never a cost
price, the language of the question, thirty words at most. The evals ask each
in a single turn (docs/BENCH_GAPS.md, addition 5).

A case is a whole conversation - the rule, N turns of ordinary talk whose
replies already keep it, and one last turn that pulls against it. Only the
reply to that last turn is scored, with checks a program can make.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

import foundry_router
from foundry_router import bench, benchcard, benchrules as br
from foundry_router.db import Database
from foundry_router.pool.protocols import ChatResult

SPECS = br.rule_specs()
IDS = [sp["task"] for sp in SPECS]


def _key(messages) -> tuple:
    return messages[0]["content"], len(messages), messages[-1]["content"]


BY_TALK = {_key(sp["messages"]): sp for sp in SPECS}


def _svc(tmp_path, pool=None):
    cfg = type("Cfg", (), {"code_checks": None})()
    return type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "r.sqlite"), "pool": pool,
        "config_store": type("CS", (), {"config": cfg})(),
        "registry": type("R", (), {"get": staticmethod(lambda m: None)})(),
    })()


def _ref(sp) -> dict:
    return {"hold": sp["hold"], "task": sp["task_checks"]}


# -- the checks -------------------------------------------------------------------------------

def test_each_check_is_something_a_program_can_decide():
    assert br.check("It is 17:35.", {"kind": "has", "pattern": r"\b17:35\b"})
    assert br.check("It is 17:35.", {"kind": "lacks", "pattern": br.AM_PM})
    assert not br.check("About 5:35 PM.", {"kind": "lacks", "pattern": br.AM_PM})
    cap = {"kind": "max_words", "n": 5}
    assert br.check("One two three four five.", cap) and not br.check("One two three 4 5 6", cap)
    assert not br.check("", cap) and not br.check("...", cap), "a reply has to say something"
    closing = {"kind": "last_line", "text": br.SIGNOFF}
    assert br.check("102\n" + br.SIGNOFF, closing) and br.check("102\n\n**~ Hearth**\n", closing)
    assert not br.check("102", closing) and not br.check(br.SIGNOFF + "\n102", closing)
    # the closing line is not counted against a cap that says so
    both = {"kind": "max_words", "n": 3, "skip_last_line": br.SIGNOFF}
    assert br.check("Three short words\n" + br.SIGNOFF, both)
    assert not br.check("Now four short words\n" + br.SIGNOFF, both)
    with pytest.raises(ValueError):
        br.check("x", {"kind": "vibes"})


def test_a_time_a_date_and_a_cost_are_recognised_however_they_are_written():
    def has(pattern, text):
        return re.search(pattern, text, re.I) is not None
    for said in ("5:35 pm", "at 5.35pm", "5:35 P.M.", "3 am", "11 a.m."):
        assert has(br.AM_PM, said), said
    for said in ("17:35", "5:35 a meeting", "the 5 main points", "5 m wide"):
        assert not has(br.AM_PM, said), said
    for said in ("9 May 2027", "May 9th", "the 3rd of March", "09/05/2027", "March 31"):
        assert has(br.NOT_ISO, said), said
    for said in ("2027-05-09", "in May 2027", "it may 5 times", "45 days", "march 3 steps"):
        assert not has(br.NOT_ISO, said), said
    assert has(br.AFTER_NOON_24, "17:35") and not has(br.AFTER_NOON_24, "5:35 pm")
    assert not has(br.AFTER_NOON_24, "09:40") and has(br.MILES, "26.2 miles")
    cost = {"kind": "lacks", "pattern": rf"(?<!\d)(?:{br._cost(31.40)}|{br._cost(9.85)})"}
    for said in ("31.40", "31,40", "it is 31.4.", "9.85"):
        assert not br.check(said, cost), said
    for said in ("131.40", "19.85", "31.45", "64.00 on the shelf"):
        assert br.check(said, cost), said


def test_the_language_of_a_reply_is_read_from_its_small_words():
    for lang, said in br._SAID.items():
        assert br.language_of(said) == lang
    assert br.language_of("29.") == "" and br.language_of("") == ""
    assert br.language_of("Oui.") == "", "too little to say"
    assert set(br._PULL) == {"de", "fr", "es"}
    for lang, asked in br._PULL.items():
        assert br.language_of(asked) != "en", lang


def test_a_broken_rule_is_nothing_and_a_wrong_answer_is_half():
    ref = {"hold": [{"rule": "clock", "kind": "lacks", "pattern": br.AM_PM}],
           "task": [{"kind": "has", "pattern": r"\b17:35\b"}]}
    assert br.rule_score("You arrive at 17:35.", ref) == {
        "score": 100.0, "held": 1.0, "task": 1.0, "broke": [], "detail": "held=1 task=1"}
    wrong = br.rule_score("You arrive at 17:25.", ref)
    assert wrong["score"] == 50.0 and wrong["detail"] == "held=1 task=0"
    both = br.rule_score("You arrive at 17:35 (5:35 pm).", ref)
    assert both["score"] == 0.0 and both["broke"] == ["clock"], "the right answer, the wrong way"
    assert both["detail"] == "held=0 task=1 broke=clock"
    none = br.rule_score("  ", ref)
    assert none["score"] == 0.0 and none["broke"] == ["nothing"]
    assert set(br.RULE_KINDS) <= set(br.RULE_WORDS)


# -- every generated case ---------------------------------------------------------------------

@pytest.mark.parametrize("sp", SPECS, ids=IDS)
def test_the_conversation_is_whole_and_its_own_replies_keep_the_rule(sp):
    msgs = sp["messages"]
    assert msgs[0]["role"] == "system" and msgs[-1]["role"] == "user"
    roles = [m["role"] for m in msgs[1:]]
    assert roles == ["user", "assistant"] * (len(roles) // 2) + ["user"]
    extra = {"system": 0, "user": 1, "both": 1}[sp["where"]] + (sp["kind"] == "changed")
    assert len(msgs) == 2 + 2 * (sp["turns"] + extra)
    # the replies already written in keep every rule a reply can keep without being asked
    for m in msgs:
        if m["role"] != "assistant":
            continue
        for c in sp["hold"]:
            if c["kind"] in ("lacks", "last_line", "max_words") and sp["kind"] != "changed":
                assert br.check(m["content"], c), (c["rule"], m["content"])
    # ... and a reply that keeps them scores full, one that breaks the newest scores nothing
    assert br.rule_score(sp["ideal"], _ref(sp))["score"] == 100.0
    slip = br.rule_score(sp["slip"], _ref(sp))
    assert slip["score"] == 0.0 and slip["broke"] == [sp["rules"][-1]]
    assert all(ord(ch) < 0x250 for m in msgs for ch in m["content"])
    json.dumps(sp)


@pytest.mark.parametrize("kind", br.RULE_KINDS)
def test_the_rule_is_given_once_where_the_case_says_and_the_dial_is_the_turns_between(kind):
    turns = [br.rule_spec(kind, lv)["turns"] for lv in range(1, 9)]
    assert turns == list(br.TURNS) == [0, 2, 5, 10, 20, 40, 80, 160]
    sp = br.rule_spec(kind, 6)
    system, first = sp["messages"][0]["content"], sp["messages"][1]["content"]
    if br.WHERE[kind] == "system":
        assert len(system) > len(br._BASE) + 20 and not first.startswith("One thing before")
    else:
        assert first.startswith("One thing before we start")
        assert sp["messages"][2]["content"].startswith("Understood")
    # the ordinary turns say nothing a rule could be about
    talk = " ".join(m["content"] for m in sp["messages"][3:-1]
                    if "Change of plan" not in m["content"] and "am and pm" not in m["content"])
    for pattern in (br.AM_PM, br.NOT_ISO, br.MILES, br.AFTER_NOON_24):
        assert re.search(pattern, talk, re.I) is None, pattern
    assert br.rule_spec(kind, 5) == br.rule_spec(kind, 5), "the same case every build"
    questions = [m["content"] for m in br.rule_spec(kind, 8)["messages"][1:-1:2]]
    assert len(set(questions)) == len(questions), "no ordinary turn is asked twice"


def test_a_preference_changed_half_way_is_scored_as_changed():
    for level in (1, 2, 5, 7):
        sp = br.rule_spec("changed", level)
        said = [m["content"] for m in sp["messages"]]
        first = next(i for i, s in enumerate(said) if "24-hour" in s)
        change = next(i for i, s in enumerate(said) if s.startswith("Change of plan"))
        assert first < change < len(said) - 1
        assert abs((change - 3) - sp["turns"]) <= 1 or level > 1, "about half way through"
        assert sp["hold"][0]["pattern"] == br.AFTER_NOON_24
        # the leaflet gives the time the old way: repeating it would break the new rule
        assert re.search(br.AFTER_NOON_24, said[-1]) and not re.search(br.AM_PM, said[-1], re.I)
        assert br.rule_score(sp["ideal"], _ref(sp))["score"] == 100.0
        assert "pm" in sp["ideal"] and br.rule_score(sp["slip"], _ref(sp))["broke"] == ["changed"]
    clock = br.rule_spec("clock", 5)
    assert re.search(br.AM_PM, clock["messages"][-1]["content"], re.I), "the pull is in the turn"


def test_the_arithmetic_behind_each_answer_is_right():
    for level in range(1, 9):
        sp = br.rule_spec("clock", level)
        dep, dur_h, dur_m = re.search(r"Departs (\d+:\d\d) pm, crossing takes (\d) h (\d\d)",
                                      sp["messages"][-1]["content"]).groups()
        h, m = (int(x) for x in dep.split(":"))
        total = (h + 12) * 60 + m + int(dur_h) * 60 + int(dur_m)
        assert sp["ideal"] == f"You arrive at {total // 60:02d}:{total % 60:02d}."
        assert total < 24 * 60
        sp = br.rule_spec("dates", level)
        day, month, days = re.search(r"posted (\d+) (\w+) 2027\"\. What date is exactly (\d+)",
                                     sp["messages"][-1]["content"]).groups()
        import datetime
        end = datetime.date(2027, br._MONTHS.index(month) + 1, int(day)) \
            + datetime.timedelta(days=int(days))
        assert sp["ideal"] == f"That is {end.isoformat()}."
        sp = br.rule_spec("signoff", level)
        a, b = re.search(r"what is (\d+) times (\d+)", sp["messages"][-1]["content"]).groups()
        assert sp["ideal"] == f"{int(a) * int(b)}\n{br.SIGNOFF}"


def test_several_rules_at_once_adds_one_rule_a_level():
    seen = []
    for level in range(1, br.STACK_MAX + 1):
        sp = br.stack_spec(level)
        assert sp["rules"] == list(br.STACK_ORDER[:level]) and sp["turns"] == br.STACK_TURNS
        assert sorted(set(c["rule"] for c in sp["hold"])) == sorted(sp["rules"])
        seen.append(len(sp["hold"]))
        said = sp["messages"][1]["content"]
        assert ("YYYY-MM-DD" in said) == (level >= 2) and ("Call me" in said) == (level >= 3)
        assert (br.SIGNOFF in sp["messages"][0]["content"]) == (level >= 4)
        assert ("40 words" in sp["messages"][0]["content"]) == (level >= 5)
        assert ("miles" in said) == (level >= 6)
        # the last turn touches every rule: a time, a date, a distance, a name to use
        last = sp["messages"][-1]["content"]
        assert re.search(br.AM_PM, last, re.I) and re.search(br.NOT_ISO, last)
        assert "miles" in last and "by name" in last
    assert seen == sorted(seen) and seen[-1] == 7
    top = br.stack_spec(6)
    assert br.rule_score(top["ideal"], _ref(top))["score"] == 100.0
    # every way of breaking one of the six is seen and named
    ideal = top["ideal"]
    for rule, broken in (("signoff", ideal.replace("\n" + br.SIGNOFF, "")),
                         ("units", ideal.replace(" km", " miles")),
                         ("clock", re.sub(r"(\d\d:\d\d)", r"\1 am", ideal)),
                         ("name", ideal.replace("Tobi,", "Tobias,").replace("Kat,", "Katarina,")
                          .replace("Lisa,", "Elisabeth,").replace("Max,", "Maximilian,")
                          .replace("Greta,", "Margarethe,")),
                         ("words", ideal.replace("\n", " and that is all there is to say"
                                                 " about it" * 5 + "\n"))):
        assert br.rule_score(broken, _ref(top))["broke"] == [rule], rule
    with pytest.raises(ValueError):
        br.rule_spec("mood", 1)


# -- built and run ----------------------------------------------------------------------------

def test_the_suite_is_built_as_ten_ladders_with_no_tier_of_its_own(tmp_path):
    svc = _svc(tmp_path)
    out = bench.build_heldrule(svc)
    cases = bench.cases(svc, "heldrule")
    n = len(br.RULE_KINDS) * br.RULES_TOP + br.STACK_MAX
    assert out["cases"] == len(cases) == n == 78 and len({c["id"] for c in cases}) == n
    ladders = {}
    for c in cases:
        hr = c["reference"]["heldrule"]
        assert hr["hold"] and hr["rules"] and "max_tokens" not in c and not c.get("tools")
        assert not any(t.startswith("tier:") for t in c["tags"]) and "weight" not in c
        assert any(t.startswith("tokens:") for t in c["tags"])
        assert bench.wire(c["messages"])[-1]["role"] == "user"
        lad, lvl = bench.ladder_of(c)
        ladders.setdefault(lad, []).append(lvl)
    assert len(ladders) == 10 and ladders["rules:several at once"] == list(range(1, 7))
    assert ladders["rules:24-hour times"] == list(range(1, 9))
    assert bench.expected_of(cases[0]).startswith("keep: the cap on words")
    clock = next(c for c in cases if c["id"] == "RULE-clock-L1-0")
    assert "times in 24-hour form - and the answer matches" in bench.expected_of(clock)
    assert bench.build_heldrule(svc, top_level=4)["cases"] == 9 * 4 + 6


class _Voice:
    """Says `how(case)` to whatever conversation it is shown."""

    def __init__(self, how):
        self.how, self.seen = how, 0

    async def chat(self, model, messages, tools=None, max_tokens=0, options=None):
        self.seen += 1
        assert not tools, "these cases offer no tools"
        return ChatResult(content=self.how(BY_TALK[_key(messages)]), finish_reason="stop",
                          completion_tokens=40, prompt_tokens=200), "b"


def _run(tmp_path, how, keep):
    svc = _svc(tmp_path, _Voice(how))
    bench.build_heldrule(svc)
    cases = [c for c in bench.cases(svc, "heldrule") if keep(c)]
    assert cases
    (bench._dir(svc) / "heldrule.jsonl").write_text(
        "".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
    import unittest.mock as um
    with um.patch.object(bench, "_record", lambda *a, **k: None), \
            um.patch.object(bench, "_state", {"state": "idle"}):
        out = asyncio.run(bench._run(svc, "heldrule", "m", "", 0))
    rows = svc.db.query("SELECT * FROM bench_results WHERE run_id=? ORDER BY id", (out["run_id"],))
    return out, rows


def test_a_model_that_keeps_every_rule_scores_full(tmp_path):
    out, rows = _run(tmp_path, lambda sp: sp["ideal"], lambda c: bench.ladder_of(c)[1] in (1, 6))
    assert out["score"] == 100.0 and len(rows) == 20
    assert all(r["code_detail"] == "held=1 task=1" for r in rows)
    assert all(benchcard.miss_kind("heldrule", r) is None for r in rows)


def test_a_model_that_lets_go_scores_nothing_and_the_card_names_the_rule(tmp_path):
    out, rows = _run(tmp_path, lambda sp: sp["slip"],
                     lambda c: c["id"] in ("RULE-clock-L7-0", "RULE-never-L7-0",
                                           "RULE-name-L7-0", "RULE-stack-L6-0"))
    assert out["score"] == 0.0
    assert [benchcard.miss_kind("heldrule", r) for r in rows] == [
        "broke the rule: never state a cost price", "broke the rule: times in 24-hour form",
        "broke the rule: the name the user asked to be called",
        "broke the rule: metric and Celsius only"]


def test_keeping_the_rule_with_a_wrong_answer_is_half_and_silence_is_nothing(tmp_path):
    (tmp_path / "a").mkdir()
    out, rows = _run(tmp_path / "a", lambda sp: "You arrive at 00:00.",
                     lambda c: c["id"] == "RULE-clock-L3-0")
    assert out["score"] == 50.0 and rows[0]["code_detail"] == "held=1 task=0"
    assert benchcard.miss_kind("heldrule", rows[0]) == "kept the rule, wrong answer"
    (tmp_path / "b").mkdir()
    out, rows = _run(tmp_path / "b", lambda sp: "", lambda c: c["id"] == "RULE-clock-L3-0")
    assert out["score"] == 0.0 and benchcard.miss_kind("heldrule", rows[0]) == "said nothing"


# -- the card, the route, the page ------------------------------------------------------------

def test_the_card_names_the_suite_and_breaks_it_down():
    def row(rule, where, turns, score, detail, ladder, level):
        return {"score": score, "error": "", "code_detail": detail,
                "tags": json.dumps(["heldrule", f"rule:{rule}", f"where:{where}",
                                    f"turns:{turns}", f"ladder:rules:{ladder}",
                                    f"level:{level}"]),
                "tools_expected": "[]", "tools_called": "[]", "task_id": "x", "case_id": "c"}
    ok = "held=1 task=1"
    rows = [row("clock", "user", 0, 100.0, ok, "24-hour times", 1),
            row("clock", "user", 80, 0.0, "held=0 task=1 broke=clock", "24-hour times", 7),
            row("words", "system", 10, 100.0, ok, "word cap", 4),
            row("never", "system", 40, 100.0, ok, "never a cost price", 6),
            row("stack", "both", 10, 0.0, "held=0 task=1 broke=words;units",
                "several at once", 6)]
    f = {x["name"]: x["rows"] for x in benchcard.facets("heldrule", rows)}
    assert [r["label"] for r in f["The rule"]] == [
        "word cap", "never a cost price", "24-hour times", "several rules at once"]
    assert [(r["label"], r["n"]) for r in f["Where it was given"]] == [
        ("in the system prompt", 2), ("said once by the user", 2), ("some of each", 1)]
    assert [(r["label"], r["n"]) for r in f["How long ago"]] == [
        ("no turns between", 1), ("2 to 10 turns between", 1), ("20 to 40 turns between", 1),
        ("80 or more turns between", 1)]
    assert "24-hour times: level 1 of 7" in [r["label"] for r in f["How far up each ladder"]]
    assert "What the verified step was" not in f
    assert benchcard.miss_kind("heldrule", rows[4]) == "broke the rule: the cap on words"
    assert benchcard.SUITE_NAMES["heldrule"] == "A rule held over a conversation"
    assert "heldrule" in benchcard.SUITE_ASKS


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
        out = asyncio.run(routes.bench_heldrule(Req({})))
        less = asyncio.run(routes.bench_heldrule(Req({"top_level": 5, "name": "r5"})))
    assert out["ok"] and out["cases"] == 78 and out["name"] == "heldrule"
    assert less["cases"] == 51 and less["name"] == "r5"
    assert "/admin/api/bench/heldrule" in {r.path for r in routes.router.routes}
    page = (Path(foundry_router.__file__).parent / "ui" / "static" / "index.html"
            ).read_text(encoding="utf-8")
    for want in ('id="bench-heldrule"', 'id="bench-heldrule-top"', 'api("bench/heldrule"',
                 'suiteNote("heldrule")'):
        assert want in page, want
