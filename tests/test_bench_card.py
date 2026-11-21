"""v0.181.211: the benchmark audit and the report card.

Covers what the audit found wrong and what the card now says:

- the finder suite ended a case at the model's FIRST call, so a preparatory
  call (a page snapshot before a click, the voices before speech) scored as the
  wrong tool - and its model calls recorded no performance sample at all;
- the needle and code suites were at the ceiling for both local models, so
  they could not tell a better model from a good one: longer contexts, decoys,
  and traced code in hard and expert tiers;
- the code snippets ran with class-body scoping, so a function could not see
  a name defined beside it;
- the card breaks every suite down, classifies every miss, keeps what the
  model answered, reports speed per suite, and flags a suite it aces;
- the live strip estimates how long is left, and says what it estimated from;
- two profiles over different suites, or over a rebuilt suite, are not
  compared as if they measured the same thing;
- a prefill rate no GPU can reach is a prompt cache the backend did not
  report, and is not recorded as a speed.
"""
import asyncio
import json
import time

import pytest

from foundry_router import bench, benchcard, benchprofile as bp, tool_finder
from foundry_router.db import Database


# -- shared fakes ------------------------------------------------------------------

class _Tool:
    def __init__(self, name, server, description):
        self.name, self.server, self.description = name, server, description
        self.mcp_tool = name
        self.parameters = {"type": "object", "properties": {}}


TOOLS = [
    _Tool("playwright__browser_snapshot", "playwright", "Capture the page's accessibility snapshot"),
    _Tool("playwright__browser_click", "playwright", "Click an element on the page"),
    _Tool("generate_image", "Media_Gen", "Generate an image from a text prompt"),
    _Tool("searxng_web_search", "searxng", "Search the public web for pages"),
    _Tool("roblox_get_class", "Roblox-docs", "Look up a Roblox class and its members"),
]


class _Result:
    def __init__(self, tool_calls=None, content=""):
        self.tool_calls = tool_calls or []
        self.content = content
        self.prompt_tokens = 900
        self.completion_tokens = 40
        self.eval_duration_ns = 800_000_000
        self.prompt_eval_duration_ns = 600_000_000
        self.load_duration_ns = 0
        self.draft_n = self.draft_n_accepted = 0
        self.cached_tokens = 0
        self.prefill_tokens = 900
        self.reasoning_tokens = 0
        self.timing_source = "server"
        self.finish_reason = "stop"


def _svc(tmp_path, script=()):
    seq = list(script)

    class Pool:
        async def chat(self, model, convo, **kw):
            return (seq.pop(0) if seq else _Result(content="done")), "fake-lane"

    cfg = type("C", (), {"bench": type("B", (), {"suite_weights": {}})()})()
    return type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "b.sqlite"),
        "pool": Pool(),
        "config_store": type("CS", (), {"config": cfg})(),
        "registry": type("R", (), {"note_inference": lambda *a, **k: None,
                                   "note_ttft": lambda *a, **k: None,
                                   "note_finish": lambda *a, **k: None})(),
        "tool_registry": type("T", (), {
            "mcp_tools_for_persona": staticmethod(lambda p: list(TOOLS))})(),
    })()


def _find(q):
    return _Result([{"id": "1", "name": tool_finder.FIND_TOOL, "arguments": {"query": q}}])


def _call(name):
    return _Result([{"id": "2", "name": tool_finder.CALL_TOOL,
                     "arguments": {"name": name, "arguments": {}}}])


def _flow_case(want="playwright__browser_click", ask="click the submit button"):
    return {"id": "FLOW-001", "task_id": want, "tags": ["toolflow", "finder", "tool_use"],
            "messages": [{"role": "system", "content": "s"},
                         {"role": "user", "content": f"Please {ask}."}],
            "reference": {"finder_tool": want, "content": want,
                          "tool_calls": [{"name": "foundry_call_tool"}]}}


# -- the finder suite --------------------------------------------------------------

def test_a_preparatory_call_before_the_right_one_is_full_marks(tmp_path):
    """Seen live: 22 of flash's 25 toolflow misses were a snapshot before a
    click, the voices before speech, the job list before a cancel - scored as
    the wrong tool because the case ended at the first call."""
    svc = _svc(tmp_path, [_find("click a button"), _call("playwright__browser_snapshot"),
                          _call("playwright__browser_click")])
    got = asyncio.run(bench.ask_through_finder(svc, "m", _flow_case()))
    s = bench.finder_score(got, "playwright__browser_click")
    assert s["score"] == 100.0, (s, got["finder"])
    assert s["used"] == ["playwright__browser_snapshot", "playwright__browser_click"]
    assert s["detours"] == 1


def test_only_ever_using_another_tool_is_still_half(tmp_path):
    svc = _svc(tmp_path, [_find("click"), _call("playwright__browser_snapshot")])
    got = asyncio.run(bench.ask_through_finder(svc, "m", _flow_case()))
    s = bench.finder_score(got, "playwright__browser_click")
    assert s["found"] == 1.0 and s["picked"] == 0.0 and s["score"] == 50.0


def test_wandering_is_still_capped_by_the_rounds(tmp_path):
    svc = _svc(tmp_path, [_find("click")] + [_call("generate_image")] * 40)
    got = asyncio.run(bench.ask_through_finder(svc, "m", _flow_case()))
    assert got["finder"]["rounds"] <= bench.TOOLFLOW_ROUNDS
    assert bench.finder_score(got, "playwright__browser_click")["picked"] == 0.0


def test_every_finder_round_records_a_performance_sample(tmp_path):
    """v0.181.196 gave ask() its telemetry; the finder path was a second
    caller and recorded nothing - run 44 had 0 samples for 40 cases."""
    svc = _svc(tmp_path, [_find("click"), _call("playwright__browser_click")])
    asyncio.run(bench.ask_through_finder(svc, "m", _flow_case(), label="bench44 m"))
    rows = svc.db.query("SELECT run_label, decode_tps FROM perf_samples")
    assert len(rows) == 2, rows
    assert all(r["run_label"] == "bench44 m" for r in rows)
    assert all(r["decode_tps"] for r in rows)


def test_both_benchmark_paths_record_through_one_function():
    import inspect
    assert "_record(" in inspect.getsource(bench.ask)
    assert "_record(" in inspect.getsource(bench.ask_through_finder)
    assert "telemetry.record_call" not in inspect.getsource(bench.ask)


# -- the needle suite ------------------------------------------------------------------

def test_the_needle_suite_now_reaches_where_context_costs(tmp_path):
    svc = _svc(tmp_path)
    bench.build_needle(svc, per_cell=1)
    cases = bench.cases(svc, "needle")
    longest = max(int(next(t for t in c["tags"] if t.startswith("chars:")).split(":")[1])
                  for c in cases)
    assert longest >= 96000
    body = max(cases, key=lambda c: len(c["messages"][1]["content"]))["messages"][1]["content"]
    assert len(body) >= 96000, "a 96k case cut short would still be labelled 96k"


def test_a_decoy_case_has_two_lookalike_sentences(tmp_path):
    svc = _svc(tmp_path)
    bench.build_needle(svc, per_cell=1, lengths=[16000], decoys=2)
    decoys = [c for c in bench.cases(svc, "needle") if "decoy" in c["tags"]]
    assert len(decoys) == 2
    for c in decoys:
        body = c["messages"][1]["content"]
        ref = c["reference"]
        assert ref["must_include"][0] in body and ref["must_exclude"][0] in body
        assert ref["decoy_task"] in body and ref["decoy_task"] != c["task_id"]
        assert ref["decoy_task"].split("-")[0] == c["task_id"].split("-")[0]
        assert not any(t.startswith("depth:") for t in c["tags"]), "kept out of the grid"
        assert any(t.startswith("tier:") for t in c["tags"])


def test_naming_the_decoy_scores_nothing_even_beside_the_right_code():
    assert bench.exact_score("482910", ["482910"], ["771203"]) == 1.0
    assert bench.exact_score("771203", ["482910"], ["771203"]) == 0.0
    assert bench.exact_score("482910 or 771203", ["482910"], ["771203"]) == 0.0


def test_score_one_passes_the_decoy_through():
    case = {"reference": {"content": "482910", "must_include": ["482910"],
                          "must_exclude": ["771203"]}}
    svc = type("S", (), {"config_store": type("CS", (), {"config": type("C", (), {})()})()})()
    s = asyncio.run(bench.score_one(svc, case, {"content": "it is 482910, or 771203"}))
    assert s["score"] == 0.0


def test_the_needle_tiers_climb_with_length():
    tiers = [bench.needle_tier(n) for n in (4000, 16000, 32000, 64000, 96000)]
    assert tiers == ["easy", "medium", "medium", "hard", "expert"]
    assert bench.needle_tier(16000, decoy=True) == "hard"


# -- the code suite --------------------------------------------------------------------

def test_a_snippet_runs_in_one_namespace_like_a_module():
    """Separate globals and locals is class-body scoping: this raised
    NameError at build time although it runs fine as a file."""
    src = "log = []\ndef g():\n    log.append(1)\n    return len(log)\nresult = (g(), g())"
    assert bench._run_snippet(src, "result") == (1, 2)


def test_every_traced_snippet_builds_and_is_tiered(tmp_path):
    svc = _svc(tmp_path)
    out = bench.build_codereason(svc)
    assert out["refused"] == [], out["refused"]
    cases = bench.cases(svc, "codereason")
    tiers = {}
    for c in cases:
        t = next(t for t in c["tags"] if t.startswith("tier:")).split(":")[1]
        tiers[t] = tiers.get(t, 0) + 1
    assert set(tiers) == {"easy", "medium", "hard", "expert"}, tiers
    assert tiers["hard"] >= 8 and tiers["expert"] >= 8


def test_the_original_answers_did_not_move():
    """The namespace fix must not change a key the suite already shipped."""
    assert bench._run_snippet("a = [1, 2, 3]\nb = a\nb.append(4)\nresult = len(a)",
                              "result") == 4
    assert bench._run_snippet(
        "fns = [lambda: i for i in range(3)]\nresult = [f() for f in fns]",
        "result") == [2, 2, 2]


# -- a run keeps what it needs to be read ---------------------------------------------

def test_a_run_keeps_the_answer_the_wanted_and_the_suite_version(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    (bench._dir(svc) / "alpha.jsonl").write_text(json.dumps(
        {"id": "c1", "task_id": "t1", "tags": ["needle"],
         "messages": [{"role": "user", "content": "code?"}],
         "reference": {"content": "123456", "must_include": ["123456"]}}) + "\n",
        encoding="utf-8")

    async def fake_ask(svc_, model, case, *a, **k):
        return {"content": "the code is 654321", "seconds": 2.0,
                "completion_tokens": 7, "prompt_tokens": 50}

    monkeypatch.setattr(bench, "ask", fake_ask)
    monkeypatch.setattr(bench, "_state", {"state": "idle"})
    out = asyncio.run(bench._run(svc, "alpha", "m", "", 0))
    row = svc.db.query_one("SELECT * FROM bench_results WHERE run_id=?", (out["run_id"],))
    assert row["answer"] == "the code is 654321"
    assert row["expected"] == "123456"
    run = svc.db.query_one("SELECT suite_hash FROM bench_runs WHERE id=?", (out["run_id"],))
    assert len(run["suite_hash"]) == 12
    # the hash moves with the suite's content
    before = run["suite_hash"]
    (bench._dir(svc) / "alpha.jsonl").write_text("{}\n", encoding="utf-8")
    assert bench.suite_hash(svc, "alpha") != before


def test_the_live_state_counts_what_the_strip_shows(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    (bench._dir(svc) / "alpha.jsonl").write_text("".join(json.dumps(
        {"id": f"c{i}", "task_id": "t", "messages": [{"role": "user", "content": "x"}],
         "reference": {"content": "ok", "must_include": ["ok"]}}) + "\n" for i in range(3)),
        encoding="utf-8")
    seen = []

    async def fake_ask(svc_, model, case, *a, **k):
        seen.append(dict(bench._state))
        return {"content": "ok" if case["id"] != "c1" else "no", "seconds": 1.5,
                "completion_tokens": 10, "prompt_tokens": 100}

    monkeypatch.setattr(bench, "ask", fake_ask)
    monkeypatch.setattr(bench, "_state", {"state": "idle"})
    asyncio.run(bench._run(svc, "alpha", "m", "", 0))
    assert seen[-1]["done"] == 2 and seen[-1]["out_tokens"] == 20
    assert seen[-1]["passed"] == 1                     # c0 passed, c1 did not
    assert seen[-1]["last"]["case"] == "c1"
    assert bench.state()["passed"] == 2


# -- the card ------------------------------------------------------------------------

def _row(**k):
    base = {"score": 100.0, "error": "", "tags": "[]", "tools_expected": "[]",
            "tools_called": "[]", "code_detail": "", "expected": "", "answer": "",
            "task_id": "t", "case_id": "c", "seconds": 1.0}
    base.update(k)
    return base


@pytest.mark.parametrize("suite,row,kind", [
    ("toolflow", _row(score=0.0, tags='["finder"]', code_detail="found=False picked=- searches=2"),
     "never found the tool"),
    ("toolflow", _row(score=50.0, tags='["finder"]',
                      code_detail="found=True picked=a searches=2 used=a detours=1"),
     "found it, used another tool"),
    ("toolflow", _row(score=50.0, tags='["finder"]', code_detail="found=True picked=- searches=1"),
     "found it, then used no tool"),
    ("needle", _row(score=0.0, tags='["needle","decoy"]',
                    expected="123456 (not the decoy 654321 for LOB-43)", answer="654321"),
     "named the decoy"),
    ("needle", _row(score=0.0, tags='["needle"]', answer="I could not find it"),
     "wrong or missing code"),
    ("codereason", _row(score=0.0, tags='["codereason","exception"]'), "wrong exception"),
    ("codereason", _row(score=0.0, tags='["codereason"]'), "wrong value"),
    ("baseline", _row(score=35.7, tools_called='["bash"]'),
     "used a tool where the verified answer was words"),
    ("baseline", _row(score=20.0, tools_expected='["read"]'),
     "answered in words where a tool was needed"),
    ("baseline", _row(score=0.0, tools_expected='["read"]', tools_called='["bash"]'),
     "reached for a different tool first"),
    ("baseline", _row(score=0.0, error="backend down"), "the call failed"),
])
def test_every_miss_is_named(suite, row, kind):
    assert benchcard.miss_kind(suite, row) == kind


def test_a_full_marks_case_and_a_deliverable_are_not_misses():
    assert benchcard.miss_kind("needle", _row(score=100.0)) is None
    assert benchcard.miss_kind("agent", _row(score=None)) is None


def test_the_difficulty_ladder_is_in_order():
    rows = [_row(score=s, tags=json.dumps(["codereason", f"tier:{t}"]))
            for t, s in (("expert", 0.0), ("easy", 100.0), ("hard", 100.0), ("medium", 0.0))]
    f = benchcard.facets("codereason", rows)
    assert f[0]["name"] == "Difficulty"
    assert [r["label"] for r in f[0]["rows"]] == ["easy", "medium", "hard", "expert"]


def test_the_needle_is_broken_down_by_size_and_decoy():
    rows = [_row(score=100.0, tags='["needle","chars:4000","depth:0"]'),
            _row(score=0.0, tags='["needle","chars:96000","decoy"]')]
    names = [f["name"] for f in benchcard.facets("needle", rows)]
    assert "Context size" in names and "Kind of needle" in names


def test_speed_ignores_short_prompts_and_unreported_caches():
    ss = [{"prompt_tokens": 60, "prefill_tokens": 60, "prefill_tps": 58.0,
           "decode_tps": 48.0, "eff_tps": 40.0, "prefill_ms": 1100.0, "wall_ms": 2000,
           "completion_tokens": 80, "spec_accept_pct": 92.0, "timing_src": "server",
           "backend": "brain-lane", "cached_tokens": 0, "decode_ms": 1.0},
          {"prompt_tokens": 31895, "prefill_tokens": 31895, "prefill_tps": 307569.9,
           "decode_tps": 30.0, "eff_tps": 9.0, "prefill_ms": 104.0, "wall_ms": 1900,
           "completion_tokens": 40, "spec_accept_pct": 93.0, "timing_src": "server",
           "backend": "brain-lane", "cached_tokens": 0, "decode_ms": 1.0},
          {"prompt_tokens": 38000, "prefill_tokens": 38000, "prefill_tps": 1444.0,
           "decode_tps": 29.0, "eff_tps": 7.0, "prefill_ms": 26300.0, "wall_ms": 30000,
           "completion_tokens": 200, "spec_accept_pct": 92.0, "timing_src": "server",
           "backend": "brain-lane", "cached_tokens": 0, "decode_ms": 1.0}]
    sp = benchcard.speed(ss)
    assert sp["prefill_tps"] == 1444.0
    assert sp["unreported_cache"] == 1
    assert sp["decode_tps"] == 30.0


def test_a_suite_at_the_ceiling_is_not_called_a_strength():
    card = {"parts": [
        {"suite": "codereason", "title": "Code reasoning", "score": 100.0, "ceiling": True,
         "facets": []},
        {"suite": "toolflow", "title": "Find and use tools", "score": 40.0, "ceiling": False,
         "facets": [{"name": "Tool server", "rows": [
             {"label": "Media_Gen", "n": 14, "passed": 2, "score": 30.0}]}]}]}
    v = benchcard.verdict(card)
    assert v["ceiling"] == ["Code reasoning"]
    assert not any("Code reasoning" in s["what"] for s in v["strengths"])
    assert v["weaknesses"][0]["what"] == "Find and use tools: Media_Gen"


def _profile_db(tmp_path):
    svc = _svc(tmp_path)
    db = svc.db
    for pid, model in ((1, "flash"), (2, "brain")):
        db.execute("INSERT INTO bench_profiles (id, ts, model, label, suites, repeats, "
                   "status) VALUES (?, datetime('now'), ?, '', '[]', 1, 'done')", (pid, model))
    return svc


def _run(svc, rid, pid, suite, model, score, cases=4, suite_hash=""):
    svc.db.execute("INSERT INTO bench_runs (id, ts, suite, model, label, cases, scored, "
                   "score, seconds, status, profile_id, suite_hash) VALUES "
                   "(?, datetime('now'), ?, ?, '', ?, ?, ?, 2.0, 'done', ?, ?)",
                   (rid, suite, model, cases, cases, score, pid, suite_hash))
    for i in range(cases):
        svc.db.execute("INSERT INTO bench_results (run_id, case_id, task_id, tags, "
                       "tools_expected, tools_called, score, seconds, error, answer, expected) "
                       "VALUES (?, ?, 't', ?, '[]', '[]', ?, 2.0, '', ?, ?)",
                       (rid, f"c{i}", json.dumps([suite, "tier:easy" if i % 2 else "tier:hard"]),
                        100.0 if i < cases * score / 100 else 0.0, f"answer {i}", "want"))


def test_the_card_carries_the_breakdown_the_misses_and_the_ceiling(tmp_path):
    svc = _profile_db(tmp_path)
    _run(svc, 1, 1, "codereason", "flash", 100.0)
    _run(svc, 2, 1, "needle", "flash", 50.0)
    card = bp.build_card(svc, 1)
    parts = {p["suite"]: p for p in card["parts"]}
    assert parts["codereason"]["ceiling"] is True
    assert parts["needle"]["ceiling"] is False
    assert parts["needle"]["passed"] == 2 and parts["needle"]["total"] == 4
    assert parts["needle"]["facets"][0]["name"] == "Difficulty"
    assert parts["needle"]["misses"] == [{"kind": "wrong or missing code", "n": 2}]
    assert parts["needle"]["title"] == "Long-context recall"
    assert "Code reasoning" in card["verdict"]["ceiling"]


def test_the_misses_open_with_what_was_wanted_and_what_came_back(tmp_path):
    svc = _profile_db(tmp_path)
    _run(svc, 2, 1, "needle", "flash", 50.0)
    fs = benchcard.failures(svc, [2], "needle")
    assert len(fs) == 2
    assert fs[0]["expected"] == "want" and fs[0]["answer"].startswith("answer")
    assert fs[0]["answer_kept"] is True


def test_profiles_over_different_suites_are_not_ranked(tmp_path):
    """Seen in the first comparison: a profile still running (three suites)
    against a finished one (five) was reported as 'inside the spread'."""
    svc = _profile_db(tmp_path)
    _run(svc, 1, 1, "codereason", "flash", 100.0)
    _run(svc, 2, 1, "needle", "flash", 50.0)
    _run(svc, 3, 2, "codereason", "brain", 100.0)
    out = bp.compare(svc, [1, 2])
    assert out["same_suites"] is False
    assert "not comparable" in out["note"]


def test_a_rebuilt_suite_is_never_called_a_win_or_a_tie(tmp_path):
    svc = _profile_db(tmp_path)
    _run(svc, 1, 1, "needle", "flash", 100.0, suite_hash="aaaaaaaaaaaa")
    _run(svc, 2, 2, "needle", "brain", 50.0, suite_hash="bbbbbbbbbbbb")
    row = bp.compare(svc, [1, 2])["rows"][0]
    assert row["changed"] is True
    assert row["cells"][1]["tie"] is False and row["cells"][1]["changed"] is True


def test_an_old_toolflow_run_is_not_lined_up_with_a_rescored_one(tmp_path):
    svc = _profile_db(tmp_path)
    _run(svc, 1, 1, "toolflow", "flash", 60.0, suite_hash="")
    _run(svc, 2, 2, "toolflow", "brain", 60.0, suite_hash="cccccccccccc")
    assert bp.compare(svc, [1, 2])["rows"][0]["changed"] is True


# -- live -----------------------------------------------------------------------------

def test_the_strip_says_how_long_is_left_and_from_what(tmp_path, monkeypatch):
    svc = _profile_db(tmp_path)
    for s, n in (("baseline", 10), ("needle", 20)):
        (bench._dir(svc) / f"{s}.jsonl").write_text(
            "".join(json.dumps({"id": f"{s}{i}", "messages": [], "reference": {}}) + "\n"
                    for i in range(n)), encoding="utf-8")
    # another model's history, so the suite still to come can be estimated
    svc.db.execute("INSERT INTO bench_runs (ts, suite, model, cases, scored, score, "
                   "seconds, status) VALUES (datetime('now'), 'needle', 'flash', 20, 20, "
                   "100, 3.0, 'done')")
    svc.db.execute("INSERT INTO bench_runs (ts, suite, model, cases, scored, score, "
                   "seconds, status) VALUES (datetime('now'), 'baseline', 'flash', 10, 10, "
                   "70, 5.0, 'done')")
    now = 10_000.0
    monkeypatch.setattr(bench, "_state", {
        "state": "running", "suite": "baseline", "model": "brain", "cases": 10, "done": 4,
        "started": now - 40.0, "case_started": now - 3.0, "out_tokens": 400,
        "model_seconds": 36.0, "passed": 3, "errors": 0, "run_label": "bench9 brain"})
    monkeypatch.setattr(bp, "_state", {
        "state": "running", "suites": ["baseline", "needle"], "suite": "baseline",
        "done": 0, "total": 2, "repeats": 1, "rep": 1})
    out = benchcard.live(svc, now=now)
    run = out["run"]
    assert run["per_case_s"] == 10.0 and run["eta_s"] == 60.0 and run["pct"] == 40.0
    assert run["this_case_s"] == 3.0
    pe = out["profile_eta"]
    # this model is at 10 s a case where flash took 5: twice as slow, so the
    # needle suite's 20 cases at flash's 3 s become 120 s, plus the 60 left here
    assert pe["eta_s"] == 180.0, pe
    assert "pace" in pe["basis"]
    assert pe["remaining"] == ["needle"]


def test_the_strip_does_not_invent_an_estimate(tmp_path, monkeypatch):
    svc = _profile_db(tmp_path)
    (bench._dir(svc) / "needle.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(bench, "_state", {
        "state": "running", "suite": "baseline", "model": "new", "cases": 10, "done": 2,
        "started": 100.0, "case_started": 110.0})
    monkeypatch.setattr(bp, "_state", {
        "state": "running", "suites": ["baseline", "needle"], "suite": "baseline",
        "done": 0, "total": 2, "repeats": 1, "rep": 1})
    pe = benchcard.live(svc, now=120.0)["profile_eta"]
    assert pe["eta_s"] is None and pe["unknown"] == ["needle"]


def test_nothing_running_is_just_the_states(tmp_path, monkeypatch):
    svc = _profile_db(tmp_path)
    monkeypatch.setattr(bench, "_state", {"state": "done"})
    out = benchcard.live(svc)
    assert "run" not in out and out["bench"]["state"] == "done"


# -- speed recorded honestly -----------------------------------------------------------

def test_an_impossible_prefill_is_not_recorded_as_a_speed(tmp_path):
    from foundry_router import perf_history as ph
    db = Database(tmp_path / "p.sqlite")
    ph.record_sample(db, model="m", backend="brain-lane", prompt_tokens=31895,
                     completion_tokens=40, eval_duration_ns=1_000_000_000,
                     prompt_eval_duration_ns=103_700_000, prefill_tokens=31895,
                     run_label="x")
    ph.record_sample(db, model="m", backend="brain-lane", prompt_tokens=38000,
                     completion_tokens=40, eval_duration_ns=1_000_000_000,
                     prompt_eval_duration_ns=26_300_000_000, prefill_tokens=38000,
                     run_label="x")
    rows = db.query("SELECT prefill_tps, prefill_ms FROM perf_samples ORDER BY id")
    assert rows[0]["prefill_tps"] is None and rows[0]["prefill_ms"] == 103.7
    assert 1400 < rows[1]["prefill_tps"] < 1500


def test_old_impossible_rows_are_left_out_of_the_average(tmp_path):
    from foundry_router import perf_history as ph
    db = Database(tmp_path / "p.sqlite")
    for tps in (307569.9, 1400.0, 1500.0):
        db.execute("INSERT INTO perf_samples (ts, model, backend, prefill_tps, timing_src, "
                   "run_label) VALUES (datetime('now'), 'm', 'b', ?, 'server', 'x')", (tps,))
    out = ph.query_history(db, hours=24, run_label="x")
    assert out["summary"]["prefill_tps"] == 1450.0


# -- the page -------------------------------------------------------------------------

def _page():
    from pathlib import Path
    import foundry_router
    return (Path(foundry_router.__file__).parent / "ui" / "static" / "index.html"
            ).read_text(encoding="utf-8")


def test_the_page_draws_the_live_strip_the_card_and_the_misses():
    page = _page()
    for want in ("function pfLive(", "function pfPoll(", "function pfMisses(",
                 "function pfLadder(", "function pfSpeed(", 'api("bench/live")',
                 "bench/failures?profile_id=", 'id="pf-live"', "Open the ",
                 "Too easy to judge by", "Excels at", "Fails at"):
        assert want in page, want


def test_the_live_strip_does_not_borrow_the_live_tabs_classes():
    """The Live tab already owns .lv and .lv-bar; reusing them squeezed this
    progress bar to 120 px. The global .bad class turned a panel solid red."""
    page = _page()
    i, j = page.index("function pfLive("), page.index("function pfPoll(")
    assert 'class="lv' not in page[i:j]
    assert 'class="bad"' not in page[page.index("function pfRead("):page.index("function pfSpeed(")]


def test_the_routes_exist():
    from foundry_router.ui import routes
    paths = {r.path for r in routes.router.routes}
    assert "/admin/api/bench/live" in paths
    assert "/admin/api/bench/failures" in paths
