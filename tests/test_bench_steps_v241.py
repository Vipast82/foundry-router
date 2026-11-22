"""v0.181.241: tool work that takes more than one call.

Every media, video and document tool on this router queues a job. The finder
suite stops at the first right call, so nothing measured what comes after it:
waiting for the job, checking it, collecting the link, and what a model does
when a call or a job fails (docs/BENCH_GAPS.md, addition 3).

A case is a task and a scripted tool environment - a small state machine with
a clock. Nothing real is run. Every link is derived from the exact tool, input
and setting that produced it, so the links at the end are right only when
every call before them was.
"""
import asyncio
import inspect
import json
from pathlib import Path

import pytest

import foundry_router
from foundry_router import bench, benchcard, benchsteps as bs
from foundry_router.db import Database
from foundry_router.pool.protocols import ChatResult

SPECS = bs.steps_specs(8)
BY_TASK = {sp["messages"][1]["content"]: sp for sp in SPECS}


def _svc(tmp_path, pool=None):
    cfg = type("Cfg", (), {"code_checks": None})()
    return type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "r.sqlite"), "pool": pool,
        "config_store": type("CS", (), {"config": cfg})(),
        "registry": type("R", (), {"get": staticmethod(lambda m: None)})(),
    })()


def _env(queued=True, **extra):
    """One file, one tool, nothing planted unless asked for."""
    spec = {"seed": "t", "files": ["clip_1.mov"], "cap": 30,
            "tools": {"upscale_video": {"param": "height", "ext": "mp4", "queued": queued},
                      "encode_video": {"param": "codec", "ext": "mp4", "queued": queued}},
            "wanted": ["upscale_video(clip_1.mov;height=1080)"]}
    spec.update(extra)
    return bs.StepEnv(spec)


def _do(env, name, **args):
    return json.loads(env.call(name, args))


KEY = "upscale_video(clip_1.mov;height=1080)"


# -- the scripted tools -----------------------------------------------------------------------

def test_a_job_is_queued_runs_for_a_while_and_only_then_has_a_link():
    env = _env()
    job = _do(env, "upscale_video", input_id="clip_1.mov", height=1080)
    assert job["status"] == "queued" and job["job_id"].startswith("job-")
    st = _do(env, "job_status", job_id=job["job_id"])
    assert st["status"] == "running" and st["eta_seconds"] > 0
    early = _do(env, "job_result", job_id=job["job_id"])
    assert "still running" in early["error"], "no link before the job is done"
    # asking again does not finish it: only the clock does, a second a call
    again = _do(env, "job_status", job_id=job["job_id"])
    assert again["eta_seconds"] == st["eta_seconds"] - 2 * bs.TICK
    assert _do(env, "wait", seconds=again["eta_seconds"]) == {
        "waited_seconds": again["eta_seconds"]}
    assert _do(env, "job_status", job_id=job["job_id"])["status"] == "done"
    out = _do(env, "job_result", job_id=job["job_id"])
    assert out["url"].startswith(bs.BASE) and out["output_id"].startswith("out-")
    rep = env.report()
    assert rep["flags"] == [] and rep["reached"] == rep["steps"] == 1 and rep["open"] == 0
    assert rep["shown"] == [out["url"]] and rep["calls"] == 7


def test_the_link_depends_on_exactly_what_was_asked_for():
    def link(**args):
        env = _env(queued=False)
        return _do(env, "upscale_video", **args)["url"]
    right = link(input_id="clip_1.mov", height=1080)
    assert link(input_id="clip_1.mov", height="1080") == right, "1080 and '1080' are one value"
    assert link(input_id="clip_1.mov", height=1080.0) == right
    assert link(input_id="clip_1.mov", height=720) != right, "another setting, another output"
    # ... and on the step before it: the same call on a different input
    env = _env(queued=False)
    first = _do(env, "encode_video", input_id="clip_1.mov", codec="av1")
    chained = _do(env, "upscale_video", input_id=first["output_id"], height=1080)
    assert chained["url"] != right
    assert bs.norm_value(" H265 ") == "h265" and bs.norm_value(-16.0) == "-16"


def test_a_call_that_cannot_work_says_why():
    env = _env()
    assert "input_id" in _do(env, "upscale_video", height=1080)["error"]
    assert "height" in _do(env, "upscale_video", input_id="clip_1.mov")["error"]
    assert "no file or output" in _do(env, "upscale_video", input_id="nope.mov",
                                      height=1080)["error"]
    assert "no tool called" in _do(env, "render_everything")["error"]
    assert "no job called" in _do(env, "job_status", job_id="job-zzzzz")["error"]
    job = _do(env, "upscale_video", input_id="clip_1.mov", height=1080)
    # a job id where an output id belongs: the error names the call that gives one
    mixed = _do(env, "encode_video", input_id=job["job_id"], codec="av1")
    assert "job_result" in mixed["error"]
    for bad in (0, -5, bs.MAX_WAIT + 1, "soon", None):
        assert "seconds must be" in _do(env, "wait", seconds=bad)["error"]
    assert env.report()["flags"] == [], "an error queued nothing"


def test_the_five_ways_a_call_or_a_job_goes_wrong():
    # moved: the error names the argument to use, and that one works
    env = _env(files=["archive/clip_1.mov"], moved={"clip_1.mov": "archive/clip_1.mov"},
               wanted=["upscale_video(archive/clip_1.mov;height=1080)"])
    err = _do(env, "upscale_video", input_id="clip_1.mov", height=1080)["error"]
    assert "Use input_id 'archive/clip_1.mov'" in err
    assert "job_id" in _do(env, "upscale_video", input_id="archive/clip_1.mov", height=1080)
    # gone: nothing to be done about it
    env = _env(files=[], gone=["clip_1.mov"], wanted=[])
    assert "exists" in _do(env, "upscale_video", input_id="clip_1.mov", height=1080)["error"]
    # busy: refused once, nothing queued, the same call then works
    env = _env(faults={KEY: {"kind": "busy"}})
    assert "Call this again" in _do(env, "upscale_video", input_id="clip_1.mov",
                                    height=1080)["error"]
    assert env.jobs == {}
    assert "job_id" in _do(env, "upscale_video", input_id="clip_1.mov", height=1080)
    assert env.report()["flags"] == []
    # lost: the first run fails and says it can be queued again; the second is fine
    env = _env(faults={KEY: {"kind": "lost"}})
    one = _do(env, "upscale_video", input_id="clip_1.mov", height=1080)["job_id"]
    _do(env, "wait", seconds=120)
    assert "safe to queue the job again" in _do(env, "job_status", job_id=one)["error"]
    two = _do(env, "upscale_video", input_id="clip_1.mov", height=1080)["job_id"]
    _do(env, "wait", seconds=120)
    assert "url" in _do(env, "job_result", job_id=two)
    assert env.report()["flags"] == [], "queueing a lost job again is the right move"
    # broken: it fails the same way every time, and queueing it again is waste
    env = _env(faults={KEY: {"kind": "broken"}})
    one = _do(env, "upscale_video", input_id="clip_1.mov", height=1080)["job_id"]
    _do(env, "wait", seconds=120)
    assert "fail the same way" in _do(env, "job_result", job_id=one)["error"]
    assert env.report()["reached"] == 1, "taken as far as it goes"
    _do(env, "upscale_video", input_id="clip_1.mov", height=1080)
    assert env.report()["flags"] == ["requeued_a_job_that_cannot_succeed"]


def test_a_job_that_runs_on_gives_a_new_estimate_each_time_the_last_one_is_up():
    env = _env(faults={KEY: {"kind": "slow", "phases": [30, 40, 20]}})
    job = _do(env, "upscale_video", input_id="clip_1.mov", height=1080)["job_id"]
    seen = []
    for _ in range(3):
        st = _do(env, "job_status", job_id=job)
        assert st["status"] == "running"
        seen.append(st["eta_seconds"])
        _do(env, "wait", seconds=st["eta_seconds"])
    assert seen[0] == 29 and len(seen) == 3
    assert _do(env, "job_status", job_id=job)["status"] == "done"
    assert env.report()["flags"] == []


def test_queueing_a_job_twice_or_one_nobody_asked_for_is_seen():
    env = _env()
    _do(env, "upscale_video", input_id="clip_1.mov", height=1080)
    _do(env, "upscale_video", input_id="clip_1.mov", height=1080)
    assert env.report()["flags"] == ["queued_twice"]
    env = _env()
    _do(env, "upscale_video", input_id="clip_1.mov", height=720)
    assert env.report()["flags"] == ["unasked_job"]
    # a lost job queued again before anyone looked at it is a second copy, not a retry
    env = _env(faults={KEY: {"kind": "lost"}})
    _do(env, "upscale_video", input_id="clip_1.mov", height=1080)
    _do(env, "wait", seconds=120)
    _do(env, "upscale_video", input_id="clip_1.mov", height=1080)
    assert env.report()["flags"] == ["queued_twice"]


# -- every generated case ---------------------------------------------------------------------

@pytest.mark.parametrize("sp", SPECS, ids=[sp["task"] for sp in SPECS])
def test_the_reference_run_finishes_every_case_cleanly(sp):
    want, env = bs.solve(sp["env"], sp["plans"])
    rep = env.report()
    assert want == sp["want"] and len(want) == sp["files"]
    assert rep["flags"] == [] and rep["open"] == 0
    assert rep["reached"] == rep["steps"] == sp["steps"]
    assert rep["calls"] == sp["needed"] and sp["cap"] == 2 * sp["needed"] + bs.CAP_SLACK
    assert bs.steps_score("ANSWER: " + ", ".join(want), rep, sp["want"])["score"] == 100.0
    names = {t["name"] for t in sp["tools"]}
    queued = any(t["queued"] for t in sp["env"]["tools"].values())
    assert (names >= {"job_status", "job_result", "wait"}) == queued
    assert {s[0] for p in sp["plans"] for s in p["steps"]} == names - {
        "job_status", "job_result", "wait"}
    # every link wanted is one only the right calls produce: none is in what the model reads
    shown = json.dumps(sp["messages"]) + json.dumps(sp["tools"])
    assert bs.BASE not in shown
    # the task names files as the user knows them; a moved file's new place is not in it
    assert "archive/" not in sp["messages"][1]["content"]
    json.dumps(sp)


def test_the_dial_is_the_number_of_steps():
    for kind in bs.STEP_KINDS:
        needed = [bs.steps_spec(kind, lv)["needed"] for lv in range(1, 9)]
        assert needed == sorted(needed) and len(set(needed)) == 8, (kind, needed)
    assert bs.steps_spec("pipeline", 6)["steps"] == 27
    assert bs.steps_spec("handoff", 5)["files"] == 8
    assert bs.steps_spec("batch", 4) == bs.steps_spec("batch", 4), "the same case every build"
    with pytest.raises(ValueError):
        bs.steps_spec("juggle", 1)
    with pytest.raises(ValueError):
        bs.fault_spec("haunted")


def test_a_file_that_cannot_be_finished_is_wanted_as_failed():
    for fault, entry in (("gone", [bs.FAILED]), ("broken", [bs.FAILED])):
        assert bs.fault_spec(fault)["want"] == entry
    for fault in ("moved", "busy", "lost"):
        want = bs.fault_spec(fault)["want"]
        assert len(want) == 1 and want[0].startswith(bs.BASE), fault
    mixed = bs.steps_spec("recover", 5)
    assert bs.FAILED in mixed["want"] and any(w != bs.FAILED for w in mixed["want"])


# -- how a run is scored ----------------------------------------------------------------------

WANT = [bs.BASE + "aaaa1111.mp4", bs.FAILED, bs.BASE + "bbbb2222.txt"]
REP = {"calls": 12, "cap": 30, "flags": [], "reached": 3, "steps": 3, "open": 0,
       "shown": [WANT[0], WANT[2], bs.BASE + "cccc3333.txt"]}
RIGHT = "All done.\nANSWER: " + ", ".join(WANT)


def _score(text, **rep):
    return bs.steps_score(text, {**REP, **rep}, WANT)


def test_the_right_links_cleanly_is_full_marks_and_untidily_is_half():
    full = _score(RIGHT)
    assert full["score"] == 100.0 and full["why"] == []
    assert full["detail"] == "steps=3/3 calls=12/30 result=1 clean=1"
    twice = _score(RIGHT, flags=["queued_twice"])
    assert twice["score"] == 50.0 and twice["why"] == ["queued_twice"]
    assert twice["detail"].endswith("result=1 clean=0 why=queued_twice")
    # however the line is dressed: bold, one link a line, a full stop
    for text in ("**ANSWER:** " + ", ".join(WANT) + ".",
                 "ANSWER:\n1. " + WANT[0] + "\n2. FAILED\n3. " + WANT[2],
                 f"Answer: [first]({WANT[0]}), FAILED, <{WANT[2]}>",
                 "ANSWER: nothing yet\nlater\n" + RIGHT):
        assert _score(text)["score"] == 100.0, text


def test_without_the_right_links_it_is_zero_and_the_reason_is_named():
    def why(text, **rep):
        s = _score(text, **rep)
        assert s["score"] == 0.0
        return s["why"]
    assert why("The job is still running, check back later.", open=1) == [
        "stopped_with_a_job_unfinished"]
    assert why("Here you go: " + ", ".join(WANT)) == ["no_links_handed_over"], \
        "only the ANSWER line is read"
    # it thought about the next step and then said and did nothing
    assert why("", open=2) == ["ended_a_turn_with_nothing"]
    assert why("  \n") == ["ended_a_turn_with_nothing"]
    assert why("ANSWER: " + WANT[0]) == ["wrong_number_of_entries"]
    assert why(f"ANSWER: {WANT[0]}, FAILED, FAILED") == [
        "said_failed_for_work_that_could_be_finished"]
    assert why(f"ANSWER: {WANT[0]}, FAILED, {bs.BASE}dddd4444.txt") == [
        "handed_over_a_link_no_tool_returned"]
    assert why(f"ANSWER: {WANT[0]}, {bs.BASE}cccc3333.txt, {WANT[2]}") == [
        "handed_over_a_link_for_a_file_that_failed"]
    assert why(f"ANSWER: {WANT[0]}, FAILED, {bs.BASE}cccc3333.txt") == [
        "handed_over_the_wrong_output"]
    assert why(f"ANSWER: {WANT[2]}, FAILED, {WANT[0]}") == ["handed_over_the_wrong_output"], \
        "the order of the files is part of the answer"
    assert why(RIGHT, capped=True) == ["ran_past_the_call_cap"], \
        "past the cap nothing it says counts"
    assert why(RIGHT, timed_out=True) == ["ran_out_of_time"]
    assert set(bs.WHY_WORDS) >= {"queued_twice", "unasked_job", "ran_past_the_call_cap",
                                 "requeued_a_job_that_cannot_succeed"}


# -- built and run ----------------------------------------------------------------------------

def test_the_suite_is_built_with_ladders_tiers_and_its_scripted_tools(tmp_path):
    svc = _svc(tmp_path)
    out = bench.build_toolsteps(svc)
    cases = bench.cases(svc, "toolsteps")
    n = len(bs.STEP_KINDS) * bs.STEPS_TOP + len(bs.FAULTS) * len(bs.FAULT_SEEDS)
    assert out["cases"] == len(cases) == n == 40 and len({c["id"] for c in cases}) == n
    assert "tiers" not in out
    ladders = {}
    for c in cases:
        ref = c["reference"]
        assert ref["steps"]["cap"] > 0 and ref["want"]
        assert not ref.get("tool_calls") and not ref.get("must_include")
        assert not any(t.startswith(("tokens:", "tier:")) for t in c["tags"]), \
            "no tier of its own: the card tiers it from what was measured"
        assert "weight" not in c
        lad, lvl = bench.ladder_of(c)
        if lad:
            ladders.setdefault(lad, []).append(lvl)
        else:
            assert any(t.startswith("fault:") for t in c["tags"])
        assert bench.wire(c["messages"])[0]["role"] == "system"
        assert {t["function"]["name"] for t in bench.wire_tools(c["tools"])} == \
            {t["name"] for t in c["tools"]}
    assert ladders == {"steps:" + bs.LADDER_NAMES[k]: list(range(1, bs.STEPS_TOP + 1))
                       for k in bs.STEP_KINDS}
    assert "nothing queued twice" in bench.expected_of(cases[0])
    assert bench.build_toolsteps(svc, top_level=8)["cases"] == n + 2 * len(bs.STEP_KINDS)


class _Worker:
    """A model that works through the reference run of whatever case it is
    shown, a call a round, with `twist` free to change any call or the end."""

    def __init__(self, twist=None, together=False):
        self.twist, self.together, self.gen, self.offered = twist, together, None, []
        self.pending: list = []

    def _next(self, result):
        try:
            return self.gen.send(result) if result is not None else next(self.gen)
        except StopIteration as done:
            return done.value

    async def chat(self, model, messages, tools=None, max_tokens=0, options=None):
        self.offered.append(sorted(t["function"]["name"] for t in tools or []))
        if len(messages) == 2:                       # a new case
            self.gen = bs.reference_calls(BY_TASK[messages[1]["content"]]["plans"])
            step = self._next(None)
        else:
            step = self._next(json.loads(messages[-1]["content"]))
        if self.twist:
            step = self.twist(step, messages)
        if isinstance(step, list):
            return ChatResult(content="ANSWER: " + ", ".join(step), finish_reason="stop",
                              completion_tokens=20, prompt_tokens=100), "b"
        if isinstance(step, str):
            return ChatResult(content=step, finish_reason="stop", completion_tokens=9), "b"
        calls = [{"id": f"c{len(messages)}", "name": step[0], "arguments": step[1]}]
        if self.together and step[0] not in ("job_status", "job_result", "wait"):
            calls.append({"id": f"d{len(messages)}", "name": step[0], "arguments": step[1]})
        return ChatResult(content="", finish_reason="tool_calls", tool_calls=calls,
                          completion_tokens=15, prompt_tokens=100), "b"


def _run(tmp_path, pool, keep):
    svc = _svc(tmp_path, pool)
    bench.build_toolsteps(svc)
    cases = [c for c in bench.cases(svc, "toolsteps") if keep(c)]
    assert cases
    (bench._dir(svc) / "toolsteps.jsonl").write_text(
        "".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")
    import unittest.mock as um
    with um.patch.object(bench, "_record", lambda *a, **k: None), \
            um.patch.object(bench, "_state", {"state": "idle"}):
        out = asyncio.run(bench._run(svc, "toolsteps", "m", "", 0))
    rows = svc.db.query("SELECT * FROM bench_results WHERE run_id=? ORDER BY id", (out["run_id"],))
    return out, rows


def _low(c):
    return bench.ladder_of(c)[1] <= 3


def test_a_model_that_does_the_work_scores_full_on_every_kind(tmp_path):
    pool = _Worker()
    out, rows = _run(tmp_path, pool, _low)
    assert out["score"] == 100.0 and len(rows) == 5 * 3 + 10
    assert all("result=1 clean=1" in r["code_detail"] for r in rows)
    assert all(benchcard.miss_kind("toolsteps", r) is None for r in rows)
    # the scripted tools were really on offer, and every call it made was recorded
    assert any("job_status" in names for names in pool.offered)
    called = json.loads(rows[0]["tools_called"])
    assert called and set(called) <= set(pool.offered[0])
    assert "call(s) of" in rows[0]["answer"] and "ANSWER:" in rows[0]["answer"]
    assert rows[0]["expected"].startswith("hand over " + bs.BASE)


def test_a_model_that_queues_everything_twice_gets_half(tmp_path):
    out, rows = _run(tmp_path, _Worker(together=True),
                     lambda c: c["id"] in ("STEPS-handoff-L2-0", "STEPS-batch-L1-0"))
    assert out["score"] == 50.0
    assert all("result=1 clean=0 why=queued_twice" in r["code_detail"] for r in rows)
    assert benchcard.miss_kind("toolsteps", rows[0]) == "queued the same job twice"
    assert "queued the same job twice" in rows[0]["answer"]


def test_a_model_that_stops_while_the_job_runs_gets_nothing(tmp_path):
    def quits(step, messages):
        if '"status": "running"' in str(messages[-1].get("content") or ""):
            return "The job is running. It should be ready in a little while."
        return step
    out, rows = _run(tmp_path, _Worker(quits), lambda c: c["id"] == "STEPS-wait-L2-0")
    assert out["score"] == 0.0
    assert benchcard.miss_kind("toolsteps", rows[0]) == "stopped while a job was still unfinished"


def test_a_model_that_ends_a_turn_with_nothing_is_named_for_it(tmp_path):
    # qwen3.8-flash, 6 of 21 runs with three files or more: it reasoned about the
    # next step and then ended the turn with no call and no answer
    def blank(step, messages):
        return "" if len(messages) > 6 else step
    out, rows = _run(tmp_path, _Worker(blank), lambda c: c["id"] == "STEPS-batch-L2-0")
    assert out["score"] == 0.0 and "why=ended_a_turn_with_nothing" in rows[0]["code_detail"]
    assert benchcard.miss_kind("toolsteps", rows[0]) == \
        "ended its turn with nothing: no call and no answer"


def test_a_model_that_only_asks_for_the_status_runs_into_the_cap(tmp_path):
    class Nag(_Worker):
        async def chat(self, model, messages, tools=None, max_tokens=0, options=None):
            if len(messages) > 3:
                job = json.loads(messages[3]["content"])["job_id"]
                return ChatResult(content="", finish_reason="tool_calls", completion_tokens=5,
                                  tool_calls=[{"id": f"c{len(messages)}", "name": "job_status",
                                               "arguments": {"job_id": job}}]), "b"
            return await super().chat(model, messages, tools, max_tokens, options)
    out, rows = _run(tmp_path, Nag(), lambda c: c["id"] == "STEPS-wait-L6-0")
    assert out["score"] == 0.0
    cap = bs.steps_spec("wait", 6)["cap"]
    assert f"calls={cap}/{cap}" in rows[0]["code_detail"], "not one call past the cap is answered"
    assert benchcard.miss_kind("toolsteps", rows[0]) == "used up the cap on calls"
    assert "the cap on calls was used up" in rows[0]["answer"]


def test_a_link_no_tool_returned_and_a_wrong_setting_are_both_zero(tmp_path):
    def invents(step, messages):
        return [bs.BASE + "0badc0de.mp4"] * len(step) if isinstance(step, list) else step
    (tmp_path / "a").mkdir()
    out, rows = _run(tmp_path / "a", _Worker(invents),
                     lambda c: c["id"] == "STEPS-handoff-L1-0")
    assert out["score"] == 0.0
    assert benchcard.miss_kind("toolsteps", rows[0]) == "handed over a link no tool returned"

    def careless(step, messages):
        # the right tools in the right order, one setting off
        if isinstance(step, tuple) and step[0] in bs.STAGE_TOOLS and len(messages) == 2:
            param = bs.STAGE_TOOLS[step[0]][1]
            other = next(v for v in bs.STAGE_TOOLS[step[0]][3] if v != step[1][param])
            return step[0], {**step[1], param: other}
        return step
    (tmp_path / "b").mkdir()
    out, rows = _run(tmp_path / "b", _Worker(careless),
                     lambda c: c["id"] == "STEPS-pipeline-L2-0")
    assert out["score"] == 0.0 and "result=0" in rows[0]["code_detail"]
    assert benchcard.miss_kind("toolsteps", rows[0]).startswith("handed over the wrong output")


def test_a_case_out_of_time_is_stopped_and_says_so(tmp_path):
    import unittest.mock as um
    with um.patch.object(bs, "wall_seconds", lambda cap: 0):
        out, rows = _run(tmp_path, _Worker(), lambda c: c["id"] == "STEPS-handoff-L1-0")
    assert out["score"] == 0.0 and rows[0]["code_detail"].startswith("timed_out=")
    assert benchcard.miss_kind("toolsteps", rows[0]) == "ran out of time before answering"
    assert bs.wall_seconds(10) == 300 and bs.wall_seconds(60) == 1200
    assert bs.wall_seconds(500) == 1800


def test_a_failed_model_call_is_an_error_not_a_score_of_its_own(tmp_path):
    class Down:
        async def chat(self, *a, **k):
            raise RuntimeError("backend unreachable")
    out, rows = _run(tmp_path, Down(), lambda c: c["id"] == "STEPS-handoff-L1-0")
    assert rows[0]["error"] and benchcard.miss_kind("toolsteps", rows[0]) == "the call failed"


def test_nothing_real_is_ever_called():
    for fn in (bench.ask_through_steps, bs.StepEnv.call, bs.StepEnv._stage):
        src = inspect.getsource(fn)
        for word in ("tool_registry", "call_tool", "execute", "httpx", "subprocess"):
            assert word not in src, (fn.__name__, word)
    whole = inspect.getsource(bs)
    assert "import httpx" not in whole and "import requests" not in whole
    assert "subprocess" not in whole and "tool_registry" not in whole


# -- the card, the route, the page ------------------------------------------------------------

def test_the_card_names_the_suite_and_breaks_it_down_by_the_work():
    def row(work, score, detail, extra=()):
        return {"score": score, "error": "", "code_detail": detail,
                "tags": json.dumps(["toolsteps", f"work:{work}", "tier:easy", *extra]),
                "tools_expected": "[]", "tools_called": "[]", "task_id": "x", "case_id": "c"}
    ok = "steps=1/1 calls=5/16 result=1 clean=1"
    rows = [row("handoff", 100.0, ok, ("ladder:steps:hand-over", "level:1")),
            row("handoff", 0.0, "steps=1/2 calls=9/24 result=0 clean=1 "
                                "why=stopped_with_a_job_unfinished",
                ("ladder:steps:hand-over", "level:2")),
            row("wait", 100.0, ok), row("batch", 50.0, ok.replace("clean=1", "clean=0")
                                        + " why=queued_twice;unasked_job"),
            row("fault", 100.0, ok, ("fault:moved",)), row("fault", 0.0, ok, ("fault:broken",))]
    f = {x["name"]: x["rows"] for x in benchcard.facets("toolsteps", rows)}
    assert [r["label"] for r in f["What the work was"]] == [
        "queue, check, hand over the link", "a job that keeps running",
        "many files through several queued steps", "one file, one thing wrong"]
    assert [(r["label"], r["passed"]) for r in f["One thing wrong"]] == [
        ("an argument to fix and call again", 1), ("a job that cannot succeed: report it", 0)]
    assert [r["label"] for r in f["How far up each ladder"]] == ["hand-over: level 1 of 2"]
    assert "What the verified step was" not in f
    assert benchcard.miss_kind("toolsteps", rows[3]) == "queued the same job twice"
    assert benchcard.SUITE_NAMES["toolsteps"] == "Tool work in steps"
    assert "toolsteps" in benchcard.SUITE_ASKS


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
        out = asyncio.run(routes.bench_steps(Req({})))
        more = asyncio.run(routes.bench_steps(Req({"top_level": 7, "name": "steps7"})))
    assert out["ok"] and out["cases"] == 40 and out["name"] == "toolsteps"
    assert more["cases"] == 45 and more["name"] == "steps7"
    assert "/admin/api/bench/steps" in {r.path for r in routes.router.routes}
    page = (Path(foundry_router.__file__).parent / "ui" / "static" / "index.html"
            ).read_text(encoding="utf-8")
    for want in ('id="bench-steps"', 'id="bench-steps-top"', 'api("bench/steps"',
                 'suiteNote("toolsteps")'):
        assert want in page, want
