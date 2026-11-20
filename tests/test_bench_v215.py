"""v0.181.215: making sure a low score is the model's and not the test's.

Reading flash's 25 toolflow misses against the live registry showed most were
the TEST: the task was the tool's description pasted in - docstring blocks,
words cut in half, phrases that are not requests - and it named none of the
inputs the tool needs, so snapshotting the page, listing the jobs or asking
"which URL?" was the right move and was scored as a miss. The finder itself was
fine: every reasonable phrasing ranked the one never-found tool first.

The replay's "answered in words" cases came in consecutive pairs in one
conversation (VR-MAP2 turns 6 and 7), the shape of one move written as two
messages: narration, then the call.
"""
import asyncio
import json

from foundry_router import bench, tool_finder
from foundry_router.db import Database


# -- the task a toolflow case asks -----------------------------------------------------

LUAU = ("[MCP:Roblox-docs] \nGet detailed information about a specific Luau global.\n\n"
        "Args:\n    name: Name of the global (e.g., \"wait\", \"task\", \"tick\")\n\n"
        "Returns:\n    Detailed information about the global.")
TUTORIAL = ("[MCP:Media_Gen] Narrated tutorial / how-to video from screenshots or screen "
            "recordings (or slides + speaker notes):\nper step narration, highlight boxes "
            "with labels, smooth zoom onto them, numbered step badges, captions, optional "
            "title cards and an outro, music bed and loudness for YouTube.")


def test_the_task_is_the_summary_not_the_docstring():
    t = bench._first_sentence(LUAU)
    assert t == "Get detailed information about a specific Luau global."
    assert "Args" not in t and "Returns" not in t


def test_a_long_description_is_cut_between_words():
    t = bench._first_sentence(TUTORIAL, cap=120)
    assert t.endswith("...")
    assert not t[:-3].endswith(("captio", "speak", "highli")), t
    assert len(t) <= 124


def test_the_task_carries_the_inputs_the_tool_requires():
    schema = {"type": "object", "required": ["url", "delay"],
              "properties": {"url": {"type": "string"}, "delay": {"type": "integer"},
                             "full_page": {"type": "boolean"}}}
    t = bench.toolflow_task("Capture a full-page PNG screenshot of the URL.", schema)
    assert t.startswith("I need this done: Capture a full-page PNG screenshot")
    assert 'url = "https://example.com' in t and "delay = 5" in t
    assert "full_page" not in t, "only what is required"


def test_a_job_tool_gets_a_job_id_so_listing_the_jobs_is_not_needed():
    t = bench.toolflow_task("Cancel a queued or running media job.",
                            {"required": ["job_id"], "properties": {"job_id": {"type": "string"}}})
    assert 'job_id = "a1b2c3d4e5f6"' in t


def test_an_enum_input_uses_one_of_its_values():
    assert bench.task_inputs({"required": ["mode"], "properties": {
        "mode": {"type": "string", "enum": ["fit", "raw"]}}}) == {"mode": "fit"}


class _Tool:
    def __init__(self, name, server, description, params=None):
        self.name, self.server, self.description = name, server, description
        self.mcp_tool = name
        self.parameters = params or {"type": "object", "properties": {}}


TOOLS = [_Tool(f"tool_{i:02d}", "srv", f"Do thing number {i} to a page.",
               {"type": "object", "required": ["url"],
                "properties": {"url": {"type": "string"}}}) for i in range(12)]


def _svc(tmp_path, script=()):
    seq = list(script)

    class Pool:
        async def chat(self, model, convo, **kw):
            from tests.test_bench_card import _Result
            return (seq.pop(0) if seq else _Result(content="Which URL should I use?")), "x"

    return type("S", (), {
        "capture": type("C", (), {"data_dir": str(tmp_path)})(),
        "db": Database(tmp_path / "b.sqlite"), "pool": Pool(),
        "registry": type("R", (), {"note_inference": lambda *a, **k: None,
                                   "note_ttft": lambda *a, **k: None,
                                   "note_finish": lambda *a, **k: None})(),
        "tool_registry": type("T", (), {
            "mcp_tools_for_persona": staticmethod(lambda p: list(TOOLS))})(),
    })()


def test_the_rebuilt_suite_tests_the_same_tools_in_the_same_order(tmp_path):
    """Only the wording and the inputs change, so the old and new runs ask
    about the same forty tools."""
    import random
    svc = _svc(tmp_path)
    bench.build_toolflow(svc, limit=8)
    got = [c["task_id"] for c in bench.cases(svc, "toolflow")]
    order = sorted(TOOLS, key=lambda t: t.name)
    random.Random("toolflow-v1").shuffle(order)
    assert got == [t.name for t in order][:8]
    first = bench.cases(svc, "toolflow")[0]["messages"][1]["content"]
    assert first.startswith("I need this done:") and "url = " in first


# -- a finder miss says what happened --------------------------------------------------

def test_a_finder_miss_keeps_its_searches_and_what_it_said(tmp_path):
    from tests.test_bench_card import _Result
    find = _Result([{"id": "1", "name": tool_finder.FIND_TOOL,
                     "arguments": {"query": "crawl a page to markdown"}}])
    svc = _svc(tmp_path, [find])
    case = {"id": "F1", "task_id": "tool_03", "tags": ["finder"],
            "messages": [{"role": "user", "content": "x"}],
            "reference": {"finder_tool": "tool_03", "tool_calls": [{"name": "foundry_call_tool"}]}}
    got = asyncio.run(bench.ask_through_finder(svc, "m", case))
    assert got["finder"]["queries"] == ["crawl a page to markdown"]
    assert got["finder"]["said"] == "Which URL should I use?"
    a = bench.answer_of(got)
    assert 'searched: "crawl a page to markdown"' in a
    assert "then said: Which URL should I use?" in a


# -- the replay: one move written as two messages ----------------------------------------

def _example(msgs):
    return {"messages": msgs, "tools": [{"name": "bash"}],
            "meta": {"task_id": "VR-MAP2", "tags": ["luau"]}}


def test_narration_followed_by_a_call_is_scored_as_the_call():
    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": "fix the map"},
            {"role": "assistant", "content": "Let me check the file first."},
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "bash", "arguments": "{}"}}]}]
    c = bench._case_at(_example(msgs), 2)
    assert [t["name"] for t in c["reference"]["tool_calls"]] == ["bash"]
    assert "split-turn" in c["tags"]
    assert c["reference"]["content"] == "Let me check the file first."


def test_a_real_end_of_turn_is_still_words():
    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": "fix the map"},
            {"role": "assistant", "content": "Done: the map loads."},
            {"role": "user", "content": "thanks, next"}]
    c = bench._case_at(_example(msgs), 2)
    assert c["reference"]["tool_calls"] == []
    assert "split-turn" not in c["tags"]


def test_a_split_turn_is_not_a_kind_of_work():
    from foundry_router import benchcard
    rows = [{"score": 100.0, "tags": json.dumps(["luau", "split-turn"]),
             "tools_expected": '["bash"]', "tools_called": '["bash"]'}] * 3
    kinds = next(f for f in benchcard.facets("baseline", rows) if f["name"] == "Kind of work")
    assert [r["label"] for r in kinds["rows"]] == ["luau"]


# -- the prompt behind a miss -----------------------------------------------------------

def test_a_case_can_be_read_as_the_model_saw_it(tmp_path):
    svc = _svc(tmp_path)
    (bench._dir(svc) / "baseline.jsonl").write_text(json.dumps({
        "id": "VR-MAP2--7", "task_id": "VR-MAP2", "tags": ["luau"],
        "messages": [{"role": "system", "content": "you are kilo"}]
        + [{"role": "user", "content": f"turn {i}"} for i in range(12)],
        "tools": [{"type": "function", "function": {"name": "bash"}}, {"name": "read"}],
        "reference": {"content": "ok", "tool_calls": []}}) + "\n", encoding="utf-8")
    v = bench.case_view(svc, "baseline", "VR-MAP2--7", last=4)
    assert v["turns"] == 13 and len(v["shown"]) == 4
    assert v["shown"][-1]["content"] == "turn 11"
    assert v["tools_offered"] == ["bash", "read"]
    assert v["system"] == "you are kilo"
    assert "error" in bench.case_view(svc, "baseline", "nope")


def test_the_case_route_and_the_button_exist():
    from pathlib import Path

    import foundry_router
    from foundry_router.ui import routes
    assert "/admin/api/bench/case" in {r.path for r in routes.router.routes}
    page = (Path(foundry_router.__file__).parent / "ui" / "static" / "index.html"
            ).read_text(encoding="utf-8")
    assert "Show the prompt" in page and "bench/case?suite=" in page


# -- deleting a profile ---------------------------------------------------------------

def _two_profiles(tmp_path):
    from foundry_router import benchprofile as bp
    cfg = type("C", (), {"bench": type("B", (), {"suite_weights": {}})()})()
    svc = type("S", (), {"db": Database(tmp_path / "p.sqlite"),
                         "config_store": type("CS", (), {"config": cfg})()})()
    db = svc.db
    for pid, model in ((1, "flash"), (2, "brain")):
        db.execute("INSERT INTO bench_profiles (id, ts, model, label, suites, repeats, status) "
                   "VALUES (?, datetime('now'), ?, '', '[]', 1, 'done')", (pid, model))
        for k, suite in enumerate(("needle", "codereason")):
            rid = pid * 10 + k
            db.execute("INSERT INTO bench_runs (id, ts, suite, model, label, cases, scored, "
                       "score, status, profile_id) VALUES (?, datetime('now'), ?, ?, 'p', 2, 2, "
                       "50, 'done', ?)", (rid, suite, model, pid))
            for i in range(2):
                db.execute("INSERT INTO bench_results (run_id, case_id, score) VALUES (?, ?, 50)",
                           (rid, f"c{i}"))
            db.execute("INSERT INTO perf_samples (ts, model, run_label) VALUES (datetime('now'), ?, ?)",
                       (model, bench.run_label(rid, model, "p")))
    return bp, svc


def test_a_profile_is_deleted_with_everything_it_produced(tmp_path, monkeypatch):
    bp, svc = _two_profiles(tmp_path)
    monkeypatch.setattr(bp, "_state", {"state": "idle"})
    monkeypatch.setattr(bench, "_state", {"state": "idle"})
    out = bp.delete(svc, [1])
    assert out == {"deleted": 1, "runs": 2, "cases": 4, "samples": 2,
                   "refused": [], "missing": []}
    db = svc.db
    assert db.query_one("SELECT COUNT(*) AS n FROM bench_profiles")["n"] == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM bench_runs WHERE profile_id=1")["n"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM bench_results WHERE run_id IN (10, 11)")["n"] == 0
    # the other profile is untouched
    assert db.query_one("SELECT COUNT(*) AS n FROM bench_runs WHERE profile_id=2")["n"] == 2
    assert db.query_one("SELECT COUNT(*) AS n FROM perf_samples")["n"] == 2


def test_the_running_profile_is_refused_not_half_deleted(tmp_path, monkeypatch):
    bp, svc = _two_profiles(tmp_path)
    monkeypatch.setattr(bp, "_state", {"state": "running", "profile_id": 2})
    monkeypatch.setattr(bench, "_state", {"state": "idle"})
    out = bp.delete(svc, [2, 1, 99])
    assert out["refused"] == [2] and out["deleted"] == 1 and out["missing"] == [99]
    assert svc.db.query_one("SELECT COUNT(*) AS n FROM bench_runs WHERE profile_id=2")["n"] == 2


def test_the_delete_route_and_button_exist():
    from pathlib import Path

    import foundry_router
    from foundry_router.ui import routes
    assert "/admin/api/bench/profile/delete" in {r.path for r in routes.router.routes}
    page = (Path(foundry_router.__file__).parent / "ui" / "static" / "index.html"
            ).read_text(encoding="utf-8")
    assert 'id="pf-delete"' in page and 'api("bench/profile/delete"' in page
    assert "cannot be undone" in page.lower()
