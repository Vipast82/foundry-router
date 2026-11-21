"""v0.181.225: the last three suites, and four faults found by reading misses.

Sonnet 5.5 was run on the router against find-and-use-tools (96.2), the
coordinator protocol (91.8) and half the replay suite. Read case by case, what
it missed was the cases:

* two finder cases every model misses - one answerable from memory, one that
  gives nothing to act on;
* nine real protocol turns, all "wrong status", because the case had lost the
  prompt that says a BUILD session is DONE at the gate;
* three replay cases that open on the header of a picture that is not there;
* and `found` was judged on the finder's first eight results while the model
  was shown the twenty it asked for.

None of these three suites has a tier that is hard for a frontier model; the
replay suite's key is a frontier session's own move. What they get is cases
that are sound, an argument check, and tiers taken from what was measured.
"""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from foundry_router import bench, benchcard, benchhard as bh, protocolbench as pb, tool_finder
from foundry_router.db import Database
from foundry_router.pool.protocols import ChatResult

F = "```"


def _svc(tmp_path, pool=None):
    cfg = type("Cfg", (), {"code_checks": None})()
    return type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "r.sqlite"), "pool": pool,
        "config_store": type("CS", (), {"config": cfg})(),
        "registry": type("R", (), {"get": staticmethod(lambda m: None)})(),
    })()


class _Script:
    """A model that makes the tool calls it is given, one round each."""

    def __init__(self, *rounds):
        self.rounds = list(rounds)

    async def chat(self, model, messages, tools=None, max_tokens=0, options=None):
        calls = self.rounds.pop(0) if self.rounds else []
        return ChatResult(content="" if calls else "done", tool_calls=calls,
                          finish_reason="stop", completion_tokens=20), "b"


def _find(query, limit=None):
    args = {"query": query}
    if limit:
        args["limit"] = limit
    return {"id": "f", "name": tool_finder.FIND_TOOL, "arguments": args}


def _call(name, **arguments):
    return {"id": "c", "name": tool_finder.CALL_TOOL,
            "arguments": {"name": name, "arguments": arguments}}


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(bench, "_record", lambda *a, **k: None)


# -- the finder ladder ---------------------------------------------------------------------

def _flow_case(level=4, seed=0):
    return next(c for c in bench._flow_ladder_cases()
                if c["id"] == f"FLOW-lookalike-L{level}-{seed}")


def test_a_ladder_case_searches_its_own_catalogue_and_checks_the_arguments(tmp_path):
    case = _flow_case()
    want, args = case["reference"]["finder_tool"], case["reference"]["finder_args"]
    desc = dict(case["catalogue"])[want]
    pool = _Script([_find(desc, 20)], [_call(want, **args)])
    svc = _svc(tmp_path, pool)
    got = asyncio.run(bench.ask_through_finder(svc, "m", case))
    assert got["finder"]["tools"] == len(case["catalogue"]) == 300, "not the live registry"
    s = asyncio.run(bench.score_one(svc, case, got))
    assert s["score"] == 100.0 and "args=True" in s["code_detail"]


def test_the_right_tool_on_the_wrong_file_loses_the_argument_marks(tmp_path):
    case = _flow_case()
    want = case["reference"]["finder_tool"]
    desc = dict(case["catalogue"])[want]
    pool = _Script([_find(desc, 20)], [_call(want, file="some/other.dat", label="x")])
    svc = _svc(tmp_path, pool)
    got = asyncio.run(bench.ask_through_finder(svc, "m", case))
    s = asyncio.run(bench.score_one(svc, case, got))
    assert s["score"] == 70.0 and "args=False" in s["code_detail"]
    row = {"score": 70.0, "error": "", "tags": json.dumps(case["tags"]),
           "code_detail": s["code_detail"], "tools_expected": "[]", "tools_called": "[]"}
    assert benchcard.miss_kind("toolflow", row) == "right tool, wrong arguments"
    # a sibling instead of the tool: found, not picked, no argument marks
    twin = next(n for n, _d in case["catalogue"] if n != want)
    pool = _Script([_find(desc, 20)], [_call(twin, **case["reference"]["finder_args"])])
    got = asyncio.run(bench.ask_through_finder(_svc(tmp_path / "b", pool) if (
        tmp_path / "b").mkdir() is None else None, "m", case))
    assert asyncio.run(bench.score_one(svc, case, got))["score"] == 30.0


def test_the_request_does_not_use_the_catalogues_words(tmp_path):
    """Or one search with the request as the query would find it."""
    for case in bench._flow_ladder_cases():
        want = case["reference"]["finder_tool"]
        task = case["messages"][1]["content"]
        assert want not in task
        tools = bench._catalogue_tools(case)
        desc = dict(case["catalogue"])[want]
        # the description's own clauses are not in the request
        assert not any(clause in task for clause in desc.split(". ")[:3]), case["id"]
        assert len(tools) >= 300 and case["reference"]["finder_args"]["file"] in task
        assert "tier:easy" in case["tags"], "measured: every model run passes these"


def test_found_counts_what_the_model_was_actually_shown(tmp_path):
    """It asked for 20 and was shown 20; `found` looked at the first 8."""
    tools = [NS(name=f"convert_image_{i:02d}", mcp_tool=f"convert_image_{i:02d}", server="s",
                description="Converts images.", kind="mcp",
                parameters={"type": "object", "properties": {}}) for i in range(15)]
    want = "convert_image_12"                       # 13th of 15 tied results
    case = {"messages": [{"role": "user", "content": "q"}],
            "reference": {"finder_tool": want}}
    import unittest.mock as um
    for limit, found in ((None, False), (20, True)):
        pool = _Script([_find("convert image", limit)], [_call(want)])
        with um.patch.object(bench, "_granted_tools", lambda svc, p=None: tools):
            got = asyncio.run(bench.ask_through_finder(_svc(tmp_path / str(limit), pool)
                                                       if (tmp_path / str(limit)).mkdir() is None
                                                       else None, "m", case))
        assert got["finder"]["found"] is found, limit
        assert (want in got["finder"]["shown"][0]) is found


def test_a_tool_that_requires_nothing_is_still_given_something_to_act_on():
    wait = {"type": "object", "properties": {"time": {"type": "number"},
                                             "text": {"type": "string"}}}
    assert bench.task_inputs(wait) == {"time": 2}
    task = bench.toolflow_task("Wait for text to appear or disappear or a time to pass.", wait)
    assert "time = 2" in task
    assert bench.task_inputs({"type": "object", "properties": {}}) == {}
    assert "even if you think you know the answer" in bench._FLOW_RULE


# -- the protocol: a real case says what the session was asked to do --------------------------

FIRST = ("BUILD session for sprint GF-FIX1 — review fixes (coordinator run 91, attempt 2).\n\n"
         "This session's job: the plan's remaining BUILD steps, each committed by explicit "
         "path with a message starting \"GF-FIX1: \", then ask for the gate on the committed "
         "code with the STUDIO_GATE block below.\n\n  pwsh -NoProfile -File tools/gate.ps1")


def _report(d):
    return f"{F}coordinator-report\n{json.dumps(d)}\n{F}"


def test_the_job_is_read_from_the_conversations_own_prompts():
    phase, job = pb.session_job([{"role": "system", "content": "x"},
                                 {"role": "user", "content": FIRST},
                                 {"role": "user", "content": "Continue task GF-FIX1."}])
    assert phase == "BUILD"
    assert job.startswith("This session's job: the plan's remaining BUILD steps")
    assert "pwsh" not in job and "\n" not in job
    assert pb.session_job([{"role": "user", "content": "CLOSE session for sprint X — t."}]) \
        == ("CLOSE", "")
    assert pb.session_job([{"role": "user", "content": "Continue task X."}]) == ("", "")
    # the latest prompt that says wins: a repair after a build is a repair
    later = [{"role": "user", "content": FIRST},
             {"role": "user", "content": "REPAIR session for sprint GF-FIX1 — review fixes.\n\n"
                                         "This repair session's job: fix the findings below."}]
    assert pb.session_job(later)[0] == "REPAIR"


def _real(monkeypatch, tmp_path, ex):
    from foundry_router import training_prep
    store = tmp_path / "store"
    store.mkdir()
    (store / "normalized.sqlite").write_text("")
    monkeypatch.setattr(training_prep, "_dirs", lambda svc: {"store": store})
    import foundry_training.store as fts
    monkeypatch.setattr(fts, "TrainingStore", lambda *a, **k: object())
    monkeypatch.setattr(bench, "_examples", lambda *a, **k: iter([ex]))
    return pb.real_cases(type("S", (), {})(), limit=5)


def test_a_real_case_carries_the_job_its_cut_prefix_lost(monkeypatch, tmp_path):
    # the first prompt is far back; only the last turns fit the case
    monkeypatch.setattr(bench, "_fit", lambda msgs, at: [msgs[0]] + msgs[at - 1:at])
    ex = {"messages": [
        {"role": "system", "content": "You are Kilo"},
        {"role": "user", "content": FIRST},
        {"role": "assistant", "content": "working"},
        {"role": "user", "content": "Continue task GF-FIX1 where you stopped (the scripted "
                                    "Studio gate PASS at e04a7acb92d7)."},
        {"role": "assistant", "content": _report({"status": "DONE", "summary": "BUILD done; "
                                                  "CLOSE is left to the CLOSE session"})}],
        "meta": {"task_id": "GF-FIX1"}}
    c = _real(monkeypatch, tmp_path, ex)[0]
    assert c["reference"]["protocol"] == {"status": "DONE"}
    said = c["messages"][-1]["content"]
    assert FIRST not in json.dumps(c["messages"][:-1]), "the prefix does not have it"
    assert "This is a BUILD session" in said
    assert "closeout still to do does not make this session NOT DONE" in said
    assert 'Its first prompt said: "This session\'s job: the plan\'s remaining BUILD steps' in said


def test_a_conversation_that_never_says_is_not_used(monkeypatch, tmp_path):
    ex = {"messages": [
        {"role": "system", "content": "You are Kilo"},
        {"role": "user", "content": "Continue task GF-FIX1 where you stopped."},
        {"role": "assistant", "content": _report({"status": "DONE", "summary": "s"})}],
        "meta": {"task_id": "GF-FIX1"}}
    assert _real(monkeypatch, tmp_path, ex) == []


# -- replay: never a case with nothing in it ---------------------------------------------------

def test_a_case_never_opens_on_a_picture_that_is_not_there(monkeypatch):
    import foundry_training.sessions as fs
    stub = {"role": "user", "content": "Attached media from tool result:"}
    assert bench.media_stub(stub)
    assert not bench.media_stub({"role": "user", "content": "Attached media from tool result:\n"
                                 + "the gallery shows the hero cut off at the top " * 3})
    assert not bench.media_stub({"role": "tool", "content": "Attached media from tool result:"})
    big = {"role": "user", "content": "x" * 400}
    msgs = [{"role": "system", "content": "s"}, big,
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "bash"}]},
            {"role": "tool", "tool_call_id": "1", "content": "y" * 400},
            stub,
            {"role": "assistant", "content": "", "tool_calls": [{"id": "2", "name": "bash"}]}]
    # everything before the stub is too long to fit: only [system, stub] would
    monkeypatch.setattr(bench, "MAX_PROMPT_TOKENS", 60)
    monkeypatch.setattr(fs, "_tokens", lambda text: (len(text) // 4, "est"))
    assert bench._fit(msgs, 5) is None, "no case, rather than a case with no task in it"
    monkeypatch.setattr(bench, "MAX_PROMPT_TOKENS", 100000)
    assert bench._fit(msgs, 5)[1] is big


# -- replay: tiers from what was measured -------------------------------------------------------

def _run(db, suite, model, scores, h="abc", errors=()):
    rid = db.execute("INSERT INTO bench_runs (ts, suite, model, cases, score, status, suite_hash) "
                     "VALUES (datetime('now'),?,?,?,50,'done',?)", (suite, model, len(scores), h))
    for cid, sc in scores.items():
        db.execute("INSERT INTO bench_results (run_id, case_id, tags, score, error) "
                   "VALUES (?,?,?,?,?)", (rid, cid, '["replay"]', sc, "boom" if cid in errors else ""))
    return rid


def test_replay_cases_are_tiered_by_who_missed_them(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    monkeypatch.setattr(benchcard, "_frontier", lambda s, m: m.startswith("claude"))
    _run(svc.db, "baseline", "flash", {"a": 100, "b": 100, "c": 0, "d": 100, "e": 0, "solo": 0})
    _run(svc.db, "baseline", "brain", {"a": 90, "b": 0, "c": 0, "d": 100, "e": 0})
    _run(svc.db, "baseline", "claude-sonnet", {"a": 100, "b": 100, "c": 100, "d": 35, "e": 0,
                                               "down": 0}, errors=("down",))
    _run(svc.db, "baseline", "claude-opus", {"e": 0, "d": 100})
    _run(svc.db, "baseline", "flash", {"a": 0}, h="older-suite")     # another version: not counted
    tiers = benchcard.measured_tiers(svc, "baseline", ["abc"])
    assert tiers == {"a": "easy", "b": "medium", "c": "medium", "d": "hard", "e": "expert"}
    assert "solo" not in tiers, "one model alone tiers nothing"
    assert "down" not in tiers, "a call that failed is not a miss"
    assert benchcard.measured_tiers(svc, "baseline", []) == {}


def test_the_card_shows_measured_tiers_only_for_a_suite_without_its_own(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    monkeypatch.setattr(benchcard, "_frontier", lambda s, m: m.startswith("claude"))
    monkeypatch.setattr(benchcard, "samples_for", lambda db, labels: [])
    rid = _run(svc.db, "baseline", "flash", {"a": 100, "b": 100, "c": 0})
    _run(svc.db, "baseline", "brain", {"a": 100, "b": 0, "c": 0})
    runs = svc.db.query("SELECT * FROM bench_runs WHERE id=?", (rid,))
    part = benchcard.enrich_part(svc, {"suite": "baseline", "score": 66.7}, runs)
    first = part["facets"][0]
    assert first["name"] == "Difficulty, from every model run on these cases"
    assert [(r["label"], r["n"], r["passed"]) for r in first["rows"]] == \
        [("easy", 1, 1), ("medium", 2, 1)]


def test_the_flow_ladder_is_in_the_suite_and_the_live_cases_are_tiered(tmp_path):
    tools = [NS(name=f"srv__tool_{i}", mcp_tool=f"tool_{i}", server="srv", kind="mcp",
                description=f"Does thing number {i} to a file.",
                parameters={"type": "object", "properties": {"path": {"type": "string"}},
                            "required": ["path"]}) for i in range(8)]
    svc = _svc(tmp_path)
    import unittest.mock as um
    with um.patch.object(bench, "_granted_tools", lambda s, p=None: tools):
        out = bench.build_toolflow(svc, limit=4)
    cases = bench.cases(svc, "toolflow")
    assert out["cases"] == len(cases) == 4 + len(bh.FLOW_LEVELS) * len(bh.FLOW_SEEDS)
    live = [c for c in cases if "lookalikes" not in c["tags"]]
    assert all("tier:easy" in c["tags"] and c["messages"][1]["content"].endswith(
        bench._FLOW_RULE) for c in live)
    ladders = {bench.ladder_of(c) for c in cases if "lookalikes" in c["tags"]}
    assert ladders == {(f"flow:fit:{s}", n) for s in bh.FLOW_SEEDS for n in bh.FLOW_LEVELS}
