"""v0.181.242: output a program will read.

Scheduled jobs, the Document MCP and the jobs list all take structured input,
and what fills it in is a model reading somebody's notes. Nothing measured
that (docs/BENCH_GAPS.md, addition 4).

A case is a JSON Schema and notes in prose. The object comes back as the
whole reply or as one tool call, and is scored exactly: valid against the
schema, and every value the one the notes give.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

import foundry_router
from foundry_router import bench, benchcard, benchschema as bs
from foundry_router.db import Database
from foundry_router.pool.protocols import ChatResult

SPECS = bs.schema_specs(8)
BY_TASK = {sp["messages"][1]["content"]: sp for sp in SPECS}
IDS = [sp["task"] for sp in SPECS]


def _svc(tmp_path, pool=None):
    cfg = type("Cfg", (), {"code_checks": None})()
    return type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "r.sqlite"), "pool": pool,
        "config_store": type("CS", (), {"config": cfg})(),
        "registry": type("R", (), {"get": staticmethod(lambda m: None)})(),
    })()


def _hand(sp, obj=None, text=None):
    """A reply that hands `obj` over the way the case asks for it."""
    obj = sp["key"] if obj is None else obj
    if text is not None:
        return {"content": text, "tool_calls": []}
    if sp["mode"] == "tool":
        return {"content": "", "tool_calls": [{"name": bs.TOOL_NAME, "arguments": obj}]}
    return {"content": json.dumps(obj), "tool_calls": []}


def _copy(v):
    return json.loads(json.dumps(v))


# -- the validator ----------------------------------------------------------------------------

JOB = {"type": "object", "additionalProperties": False, "required": ["name", "retries", "days"],
       "properties": {
           "name": {"type": "string"},
           "retries": {"type": "integer", "minimum": 0, "maximum": 5},
           "cost": {"type": "number"},
           "on": {"type": "boolean"},
           "time": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$"},
           "level": {"type": "string", "enum": ["P1", "P2"]},
           "days": {"type": "array", "minItems": 1, "uniqueItems": True,
                    "items": {"type": "string", "enum": ["mon", "tue"]}}}}
GOOD = {"name": "a", "retries": 2, "days": ["mon"]}


def _errors(**change):
    return bs.schema_errors({**GOOD, **change}, JOB)


def test_the_validator_takes_what_a_program_would_and_names_what_it_would_not():
    assert bs.schema_errors(GOOD, JOB) == []
    assert _errors(retries=2.0, cost=3, on=False, time="06:30", level="P2") == []
    assert _errors(retries="2") == [("retries", "type")], "a number written as a string"
    assert _errors(retries=True) == [("retries", "type")], "true is not a count"
    assert _errors(retries=2.5) == [("retries", "type")]
    assert _errors(on=1) == [("on", "type")]
    assert _errors(retries=9) == [("retries", "range")]
    assert _errors(level="p1") == [("level", "enum")], "an enum is exact"
    assert _errors(time="6:30") == [("time", "pattern")]
    assert _errors(days=[]) == [("days", "size")]
    assert _errors(days=["mon", "mon"]) == [("days", "unique")]
    assert _errors(days=["mon", "wed"]) == [("days[1]", "enum")]
    assert _errors(days="mon") == [("days", "type")]
    assert _errors(owner="Dana") == [("owner", "additional")]
    assert bs.schema_errors({"name": "a"}, JOB) == [("retries", "required"),
                                                    ("days", "required")]
    assert bs.schema_errors([GOOD], JOB) == [("$", "type")]
    nested = {"type": "object", "properties": {"jobs": {"type": "array", "items": JOB}}}
    assert bs.schema_errors({"jobs": [GOOD, {**GOOD, "retries": -1}]}, nested) == [
        ("jobs[1].retries", "range")]


def test_two_values_are_the_same_when_a_program_reads_them_the_same():
    assert bs.same(90, 90.0) and bs.same(12.5, 12.50) and bs.same(["a", 1], ["a", 1.0])
    assert not bs.same(True, 1) and not bs.same(0, False) and not bs.same("90", 90)
    assert not bs.same("P1", "p1") and not bs.same(["a", "b"], ["b", "a"])
    assert bs.same({"a": 1, "b": [2]}, {"b": [2.0], "a": 1}) and not bs.same({"a": 1}, {})
    assert bs.flatten({"a": {"b": [1, {"c": True}], "d": []}, "e": "x"}) == {
        "a.b[0]": 1, "a.b[1].c": True, "a.d": [], "e": "x"}
    assert bs._at({"jobs": [{"t": "x"}]}, "jobs[0].t") == "x"
    assert bs._at({"jobs": []}, "jobs[2].t") is None


def test_the_json_is_read_out_of_a_reply_and_how_it_was_wrapped_is_kept():
    obj = {"a": 1}
    assert bs.read_json(' {"a": 1}\n') == (obj, "bare")
    assert bs.read_json('```json\n{"a": 1}\n```') == (obj, "fenced")
    assert bs.read_json('Here it is:\n{"a": 1}\nDone.') == (obj, "prose")
    # Sonnet 5.5 wrote the object, had second thoughts in prose and wrote it again:
    # the last whole object is the one it meant, and a program would still trip
    assert bs.read_json('{"a": 0}\n\nWait - the notes disagree.\n\n{"a": 1}') == (obj, "prose")
    assert bs.read_json('Done: {"a": {"b": [1, {"c": 2}]}} as asked.') == (
        {"a": {"b": [1, {"c": 2}]}}, "prose")
    cut = '{"jobs": [{"a": 1}, {"a": 2}, {"a"'
    assert bs.read_json(cut) == (None, ""), "cut off: the records inside it are not the object"
    assert bs.read_json("I could not work it out.") == (None, "")
    assert bs.read_json('[{"a": 1}]') == (None, ""), "a list is not the object asked for"
    assert bs.read_json("") == (None, "")


# -- every generated case ---------------------------------------------------------------------

@pytest.mark.parametrize("sp", SPECS, ids=IDS)
def test_the_key_is_valid_and_scores_full_handed_over_as_asked(sp):
    assert bs.schema_errors(sp["key"], sp["schema"]) == []
    full = bs.schema_score(_hand(sp), sp)
    assert full["score"] == 100.0 and full["why"] == [] and full["extra"] == 0
    assert full["right"] == full["total"] == sp["values"] == len(bs.flatten(sp["key"]))
    # every value has a name for what makes it hard, so a miss on it can be named
    lists = sp["kinds"]["__lists__"] if "__lists__" in sp["kinds"] else []
    assert set(bs.flatten(sp["key"])) == set(sp["kinds"]) - {"__lists__"}
    assert all(isinstance(bs._at(sp["key"], a), list) for a in lists)
    if sp["mode"] == "tool":
        assert [t["name"] for t in sp["tools"]] == [bs.TOOL_NAME]
        assert sp["tools"][0]["parameters"] == sp["schema"]
        assert '"additionalProperties"' not in sp["messages"][1]["content"]
        assert bs.TOOL_RULE in sp["messages"][1]["content"]
    else:
        assert sp["tools"] == [] and bs.TEXT_RULE in sp["messages"][1]["content"]
        assert json.dumps(sp["schema"], indent=1) in sp["messages"][1]["content"]
    json.dumps(sp)


@pytest.mark.parametrize("sp", SPECS, ids=IDS)
def test_right_but_wrapped_is_half_and_one_wrong_value_is_nothing(sp):
    text = json.dumps(sp["key"], indent=2)
    fenced = bs.schema_score(_hand(sp, text="```json\n" + text + "\n```"), sp)
    talked = bs.schema_score(_hand(sp, text="Here is the object.\n" + text), sp)
    if sp["mode"] == "tool":
        assert fenced["why"] == talked["why"] == ["not_called"]
    else:
        assert fenced["why"] == ["fenced"] and talked["why"] == ["prose"]
    assert fenced["score"] == talked["score"] == 50.0 and fenced["accepted"] == 0.0
    # one value off, anywhere: a program would have read it as true
    path, value = sorted(bs.flatten(sp["key"]).items())[-1]
    wrong = _copy(sp["key"])
    holder, last = wrong, re.findall(r"\[\d+\]|[^.\[\]]+", path)
    for part in last[:-1]:
        holder = holder[int(part[1:-1])] if part.startswith("[") else holder[part]
    leaf = int(last[-1][1:-1]) if last[-1].startswith("[") else last[-1]
    holder[leaf] = (not value) if isinstance(value, bool) else \
        value + 1 if isinstance(value, (int, float)) else str(value) + "x"
    off = bs.schema_score(_hand(sp, wrong), sp)
    assert off["score"] == 0.0 and off["right"] == off["total"] - 1
    assert any(w.startswith(("wrong:", "schema:")) for w in off["why"]), off["why"]


def test_the_same_case_comes_out_of_every_build_and_the_dial_is_size():
    assert bs.schema_spec("list", "text", 5) == bs.schema_spec("list", "text", 5)
    for shape in ("list", "nested", "sparse"):
        sizes = [bs.schema_spec(shape, "text", lv)["values"] for lv in range(1, 9)]
        assert sizes == sorted(sizes) and len(set(sizes)) == 8, (shape, sizes)
    wide = [len(bs.schema_spec("flat", "text", lv)["key"]) for lv in range(1, 7)]
    assert wide == [4, 6, 8, 10, 13, 15], "one record, more fields each level"
    assert len(bs.schema_spec("list", "tool", 6)["key"]["jobs"]) == 32
    for bad in (("heap", "text", 1), ("list", "fax", 1)):
        with pytest.raises(ValueError):
            bs.schema_spec(*bad)
    with pytest.raises(ValueError):
        bs.value_spec("mood")


# -- the notes say what the key says ----------------------------------------------------------

_WORDS = {w: i for i, w in enumerate(("zero", "one", "two", "three", "four", "five", "six",
                                      "seven", "eight", "nine", "ten", "eleven", "twelve"))}


def _read_time(sentence: str) -> str:
    """A start time read back out of the notes, written without the generator."""
    m = re.search(r"at (.+?) in the (morning|afternoon|evening)\.?$", sentence)
    if not m:
        return re.search(r"at (\d\d:\d\d)", sentence).group(1)
    said, pm = m.group(1), m.group(2) != "morning"
    if ":" in said:
        h, mi = (int(x) for x in said.split(":"))
    elif said.endswith("o'clock"):
        h, mi = _WORDS[said.split()[0]], 0
    elif said.startswith("a quarter past"):
        h, mi = _WORDS[said.split()[-1]], 15
    elif said.startswith("half past"):
        h, mi = _WORDS[said.split()[-1]], 30
    else:
        assert said.startswith("a quarter to"), said
        h, mi = _WORDS[said.split()[-1]] - 1, 45
    return f"{h + 12 if pm else h:02d}:{mi:02d}"


def test_times_in_words_read_back_to_the_key():
    for h in [x for x in range(1, 24) if x != 12]:
        for m in (0, 15, 30, 45, 5, 50):
            assert _read_time(bs.time_words(h, m, False) + ".") == f"{h:02d}:{m:02d}", (h, m)
            assert bs.time_words(h, m, True) == f"at {h:02d}:{m:02d}"
    assert bs.time_words(17, 45, False) == "at a quarter to six in the afternoon"
    assert bs.time_words(23, 45, False) == "at a quarter to twelve in the evening"
    assert bs.time_words(1, 0, False) == "at one o'clock in the morning"
    seen = 0
    for sp in SPECS:
        if sp["shape"] != "list":
            continue
        for job, para in zip(sp["key"]["jobs"], sp["messages"][1]["content"].split("\n\n")):
            said = next(s for s in re.split(r"(?<=\.) ", para) if " starts at " in s)
            assert _read_time(said.rstrip()) == job["time"], said
            seen += 1
    assert seen > 300


def test_days_are_keyed_in_week_order_however_the_notes_list_them():
    import random
    order, shuffled = list(bs.DAYS), 0
    for seed in range(300):
        v, said, kinds = bs.field(random.Random(seed), "days")
        assert v == sorted(v, key=order.index) and len(kinds) == len(v)
        if "weekdays" in said:
            assert v == order[:5]
        elif "weekends" in said:
            assert v == ["sat", "sun"]
        elif "every day" in said:
            assert v == order
        else:
            named = [d[:3].lower() for d in re.findall(r"[A-Z][a-z]+day", said)]
            assert sorted(named, key=order.index) == v
            shuffled += named != v
    assert shuffled > 20, "the notes do not always list them in week order"


def test_a_length_of_time_a_code_and_a_count_mean_what_the_schema_says():
    import random
    minutes = {"half an hour": 30, "three quarters of an hour": 45, "an hour": 60,
               "an hour and a half": 90, "two hours": 120, "two and a half hours": 150,
               "three hours": 180, "four hours": 240}
    for seed in range(200):
        rnd = random.Random(seed)
        v, said, _k = bs.field(rnd, "timeout_minutes")
        words = re.search(r"run for (.+) before", said).group(1)
        assert v == (minutes[words] if words in minutes else int(words.split()[0])), said
        v, said, _k = bs.field(rnd, "retries")
        want = 0 if "not tried again" in said else 1 if "once more" in said else \
            2 if "twice" in said else _WORDS[re.search(r"up to (\w+) times", said).group(1)]
        assert v == want, said
        v, said, _k = bs.field(rnd, "priority")
        want = "P1" if ("everything else stops" in said or "nothing else gets done" in said) \
            else "P2" if ("same day" in said or "until tomorrow" in said) \
            else "P3" if "week" in said else "P4"
        assert v == want, said
        v, said, _k = bs.field(rnd, "starts_on")
        day = int(re.search(r"(\d+)(?:st|nd|rd|th)?[ ,]", said).group(1))
        month = next(i for i, mo in enumerate(bs._MONTHS, 1) if mo in said)
        assert v == f"2027-{month:02d}-{day:02d}", said
        v, said, _k = bs.field(rnd, "budget")
        assert f"{v['amount']:,.2f}".rstrip("0").rstrip(".") in said.replace(".00", "") \
            or f"{v['amount']:,.2f}" in said, said
        assert bs._SIGN[v["currency"]] in said
    for word, why in (("P1", "everything else stops"), ("P2", "same day"), ("P4", "no deadline")):
        assert why in bs.FIELD_SCHEMAS["priority"]["description"] and word in \
            bs.FIELD_SCHEMAS["priority"]["description"]


def test_defaults_a_correction_and_the_last_instruction_reach_the_key():
    for level in (3, 5, 7):
        sp = bs.schema_spec("sparse", "text", level)
        notes = sp["messages"][1]["content"].split("\n\nThe schema:")[0]
        paras = notes.split("\n\n")
        assert paras[0].startswith("These hold for every job below")
        owner = re.search(r"Each one belongs to (\w+)\.", paras[0]).group(1)
        off = set(re.findall(r"`([^`]+)`", paras[-1]))
        assert paras[-1].startswith("Last thing: every job above is switched on, except")
        kinds, n_default, n_fixed = sp["kinds"], 0, 0
        for i, (job, para) in enumerate(zip(sp["key"]["jobs"], paras[1:-1])):
            assert f"`{job['name']}`" in para
            assert job["enabled"] == (job["name"] not in off), "the last paragraph sets it"
            assert kinds[f"jobs[{i}].enabled"] == "late"
            if kinds[f"jobs[{i}].owner"] == "default":
                assert job["owner"] == owner and "belongs to" not in para and "owns" not in para
                n_default += 1
            else:
                assert job["owner"] != owner and job["owner"] in para
            if kinds[f"jobs[{i}].time"] == "correction":
                first, second = re.search(r"starts (at .+?) - correction, (at .+?)\.",
                                          para).groups()
                assert _read_time(second) == job["time"] != _read_time(first)
                n_fixed += 1
            assert ("note" in job) == ("The note beside it reads" in para)
            assert ("depends_on" in job) == ("may not start until" in para)
        assert n_default and n_fixed and len(off) >= 1
    # the single cases isolate one of these each
    assert "default" in bs.value_spec("default")["kinds"].values()
    assert "correction" in bs.value_spec("correction")["kinds"].values()
    assert "late" in bs.value_spec("late")["kinds"].values()
    plain = bs.value_spec("optional")
    assert not any("note" in j or "depends_on" in j for j in plain["key"]["jobs"])
    assert {"note", "depends_on"} <= set(plain["schema"]["properties"]["jobs"]["items"]
                                         ["properties"])


# -- how a reply is scored --------------------------------------------------------------------

def test_a_miss_is_named_for_the_first_thing_that_went_wrong():
    sp = bs.schema_spec("sparse", "text", 3)
    jobs = sp["key"]["jobs"]

    def why(change, **kw):
        obj = _copy(sp["key"])
        change(obj)
        s = bs.schema_score(_hand(sp, obj, **kw), sp)
        assert s["score"] == 0.0
        return s["why"]
    fixed = next(i for i in range(len(jobs)) if sp["kinds"][f"jobs[{i}].time"] == "correction")
    assert why(lambda o: o["jobs"][fixed].update(time="20:50")) == ["wrong:correction"]
    dflt = next(i for i in range(len(jobs)) if sp["kinds"][f"jobs[{i}].owner"] == "default")
    assert why(lambda o: o["jobs"][dflt].update(owner="Nobody")) == ["wrong:default"]
    assert why(lambda o: o["jobs"][0].update(enabled=not jobs[0]["enabled"])) == ["wrong:late"]
    assert why(lambda o: o["jobs"].pop()) == ["items"]
    assert why(lambda o: o["jobs"].append(o["jobs"][0])) == ["items"]
    assert why(lambda o: o["jobs"][3].update(note="made up")) == ["invented"]
    assert why(lambda o: o["jobs"][0].update(priority="urgent")) == ["schema:enum", "wrong:code"]
    assert why(lambda o: o["jobs"][0].update(retries="3")) == ["schema:type", "wrong:default"]
    assert why(lambda o: o["jobs"][0].pop("owner")) == ["schema:required", "wrong:default"]
    assert why(lambda o: o["jobs"][0].update(owner_name="x")) == ["schema:additional",
                                                                 "invented"]
    none = bs.schema_score({"content": "Sorry, I cannot do that."}, sp)
    assert none["why"] == ["not_json"] and none["detail"].startswith("accepted=0 values=0/")
    cut = bs.schema_score({"content": json.dumps(sp["key"])[:200]}, sp)
    assert cut["why"] == ["cut_off"] and cut["score"] == 0.0
    assert all(w in bs.WHY_WORDS for w in (
        "wrong:correction", "wrong:default", "wrong:late", "items", "invented", "schema:enum",
        "schema:type", "schema:required", "schema:additional", "not_json", "cut_off",
        "fenced", "prose", "not_called", "called_more_than_once"))
    assert {f"wrong:{k}" for k in sp["kinds"].values() if isinstance(k, str)} <= set(bs.WHY_WORDS)


def test_a_tool_case_wants_exactly_one_call():
    sp = bs.schema_spec("list", "tool", 2)
    call = {"name": bs.TOOL_NAME, "arguments": sp["key"]}
    assert bs.schema_score({"content": "Filed.", "tool_calls": [call]}, sp)["score"] == 100.0
    twice = bs.schema_score({"content": "", "tool_calls": [call, call]}, sp)
    assert twice["score"] == 50.0 and twice["why"] == ["called_more_than_once"]
    # one call a record: each is valid against nothing, and the last is all that is read
    per_job = [{"name": bs.TOOL_NAME, "arguments": {"jobs": [j]}} for j in sp["key"]["jobs"]]
    split = bs.schema_score({"content": "", "tool_calls": per_job}, sp)
    assert split["score"] == 0.0 and split["why"] == ["called_more_than_once", "items"]
    # the call written out as text, name and all: the object is read, and it is half
    written = json.dumps({"name": bs.TOOL_NAME, "arguments": sp["key"]})
    as_text = bs.schema_score({"content": written, "tool_calls": []}, sp)
    assert as_text["score"] == 50.0 and as_text["why"] == ["not_called"]
    other = bs.schema_score({"content": "", "tool_calls": [{"name": "send_message",
                                                           "arguments": sp["key"]}]}, sp)
    assert other["score"] == 0.0 and other["why"] == ["not_json"]
    broken = bs.schema_score({"content": "", "tool_calls": [{"name": bs.TOOL_NAME,
                                                            "arguments": None}]}, sp)
    assert broken["score"] == 0.0


# -- built and run ----------------------------------------------------------------------------

def test_the_suite_is_built_with_ladders_both_ways_and_no_tier_of_its_own(tmp_path):
    svc = _svc(tmp_path)
    out = bench.build_structured(svc)
    cases = bench.cases(svc, "structured")
    n = 2 * (6 + 3 * bs.SCHEMA_TOP) + len(bs.VALUES) * len(bs.VALUE_SEEDS)
    assert out["cases"] == len(cases) == n == 80 and len({c["id"] for c in cases}) == n
    ladders = {}
    for c in cases:
        st = c["reference"]["structured"]
        assert st["mode"] in bs.MODES and st["key"] and st["schema"]["type"] == "object"
        assert c["max_tokens"] == bench.THINK_ROOM
        assert not any(t.startswith("tier:") for t in c["tags"]) and "weight" not in c
        assert any(t.startswith("tokens:") for t in c["tags"])
        assert bool(c["tools"]) == (st["mode"] == "tool")
        lad, lvl = bench.ladder_of(c)
        if lad:
            ladders.setdefault(lad, []).append(lvl)
        else:
            assert any(t.startswith("value:") for t in c["tags"])
    assert len(ladders) == 8 and all(
        v == list(range(1, 7 if "wide" in k else 9)) for k, v in ladders.items())
    assert "schema:many as text" in ladders and "schema:deep as a call" in ladders
    assert bench.expected_of(cases[0]).startswith("the JSON object as the whole reply, valid")
    tool = next(c for c in cases if c["reference"]["structured"]["mode"] == "tool")
    assert bench.expected_of(tool).startswith("one call to file_record")
    assert bench.build_structured(svc, top_level=6)["cases"] == n - 2 * 3 * 2


class _Clerk:
    """Hands over the key of whatever case it is shown, after `twist`."""

    def __init__(self, twist=None, tokens=200):
        self.twist, self.tokens, self.offered = twist, tokens, []

    async def chat(self, model, messages, tools=None, max_tokens=0, options=None):
        sp = BY_TASK[messages[1]["content"]]
        self.offered.append([t["function"]["name"] for t in tools or []])
        got = _hand(sp)
        if self.twist:
            got = self.twist(sp, got)
        return ChatResult(content=got["content"], finish_reason="stop",
                          completion_tokens=self.tokens, prompt_tokens=300,
                          tool_calls=[{"id": "1", **c} for c in got["tool_calls"]]), "b"


def _run(tmp_path, pool, keep):
    svc = _svc(tmp_path, pool)
    bench.build_structured(svc)
    cases = [c for c in bench.cases(svc, "structured") if keep(c)]
    assert cases
    (bench._dir(svc) / "structured.jsonl").write_text(
        "".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
    import unittest.mock as um
    with um.patch.object(bench, "_record", lambda *a, **k: None), \
            um.patch.object(bench, "_state", {"state": "idle"}):
        out = asyncio.run(bench._run(svc, "structured", "m", "", 0))
    rows = svc.db.query("SELECT * FROM bench_results WHERE run_id=? ORDER BY id", (out["run_id"],))
    return out, rows


def test_a_model_that_hands_over_the_key_scores_full_both_ways(tmp_path):
    pool = _Clerk()
    out, rows = _run(tmp_path, pool, lambda c: bench.ladder_of(c)[1] <= 4)
    assert out["score"] == 100.0 and len(rows) == 8 * 4 + 20
    assert all(r["code_detail"].startswith("accepted=1 values=") for r in rows)
    assert all(benchcard.miss_kind("structured", r) is None for r in rows)
    assert [bs.TOOL_NAME] in pool.offered and [] in pool.offered
    called = next(r for r in rows if "tool" in r["case_id"])
    assert json.loads(called["tools_called"]) == [bs.TOOL_NAME]
    assert called["answer"].startswith("called file_record({")


def test_a_fence_is_half_a_wrong_value_is_nothing_and_the_card_says_which(tmp_path):
    def fences(sp, got):
        return {"content": "```json\n" + json.dumps(sp["key"]) + "\n```", "tool_calls": []}
    (tmp_path / "a").mkdir()
    out, rows = _run(tmp_path / "a", _Clerk(fences),
                     lambda c: c["id"] in ("STRUCT-list-text-L2-0", "STRUCT-list-tool-L2-0"))
    assert out["score"] == 50.0
    assert [benchcard.miss_kind("structured", r) for r in rows] == [
        "right, but inside a code fence a program would trip on",
        "right, but written out instead of calling the tool"]

    def slips(sp, got):
        obj = _copy(sp["key"])
        obj["jobs"][1]["time"] = "00:00"
        return _hand(sp, obj)
    (tmp_path / "b").mkdir()
    out, rows = _run(tmp_path / "b", _Clerk(slips), lambda c: c["id"] == "STRUCT-list-text-L3-0")
    assert out["score"] == 0.0 and "values=36/37" in rows[0]["code_detail"]
    assert benchcard.miss_kind("structured", rows[0]) == "wrong value: a time written in words"


def test_a_reply_past_the_budget_counts_for_nothing_even_as_a_call(tmp_path):
    out, rows = _run(tmp_path, _Clerk(tokens=int(bench.THINK_ROOM * 1.2)),
                     lambda c: c["id"] in ("STRUCT-list-text-L1-0", "STRUCT-list-tool-L1-0"))
    assert out["score"] == 0.0
    assert all(r["code_detail"].startswith("out_of_room=") for r in rows)
    assert all(benchcard.miss_kind("structured", r) == "ran out of tokens before answering"
               for r in rows)


# -- the card, the route, the page ------------------------------------------------------------

def test_the_card_names_the_suite_and_breaks_it_down():
    def row(shape, mode, score, detail, extra=()):
        return {"score": score, "error": "", "code_detail": detail,
                "tags": json.dumps(["structured", f"shape:{shape}", f"mode:{mode}", *extra]),
                "tools_expected": "[]", "tools_called": "[]", "task_id": "x", "case_id": "c"}
    ok = "accepted=1 values=7/7 extra=0"
    rows = [row("list", "text", 100.0, ok, ("ladder:schema:many as text", "level:1")),
            row("list", "text", 0.0, "accepted=0 values=20/22 extra=0 why=schema:enum;wrong:code",
                ("ladder:schema:many as text", "level:2")),
            row("nested", "tool", 100.0, ok), row("sparse", "tool", 50.0, ok + " why=not_called"),
            row("value", "text", 100.0, ok, ("value:time",)),
            row("value", "text", 0.0, ok + " why=invented", ("value:optional",))]
    f = {x["name"]: x["rows"] for x in benchcard.facets("structured", rows)}
    assert [r["label"] for r in f["What the notes held"]] == [
        "many records", "records inside records", "defaults, corrections, a last instruction",
        "one kind of value"]
    assert [(r["label"], r["n"]) for r in f["How it was handed over"]] == [
        ("as the whole reply", 4), ("as one tool call", 2)]
    assert [(r["label"], r["passed"]) for r in f["One kind of value"]] == [
        ("a time written in words", 1), ("optional fields the notes never mention", 0)]
    assert [r["label"] for r in f["How far up each ladder"]] == ["many as text: level 1 of 2"]
    assert "What the verified step was" not in f
    assert benchcard.miss_kind("structured", rows[1]) == "a value outside the allowed list"
    assert benchcard.miss_kind("structured", rows[5]) == \
        "filled in an optional field the notes never mention"
    assert benchcard.SUITE_NAMES["structured"] == "Output a program reads"
    assert "structured" in benchcard.SUITE_ASKS


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
        out = asyncio.run(routes.bench_structured(Req({})))
        more = asyncio.run(routes.bench_structured(Req({"top_level": 7, "name": "s7"})))
    assert out["ok"] and out["cases"] == 80 and out["name"] == "structured"
    assert more["cases"] == 74 and more["name"] == "s7"
    assert "/admin/api/bench/structured" in {r.path for r in routes.router.routes}
    page = (Path(foundry_router.__file__).parent / "ui" / "static" / "index.html"
            ).read_text(encoding="utf-8")
    for want in ('id="bench-structured"', 'id="bench-structured-top"',
                 'api("bench/structured"', 'suiteNote("structured")'):
        assert want in page, want
