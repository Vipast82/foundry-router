"""v0.181.250: an interrupted benchmark carries on where it stopped.

Asked after a rebuild cut the qwen3.8-flash profile off in its ninth suite of
thirteen: "when it shows interrupted, can it have a resume function". The card
said which suites had not run, and the only way back was ticking them into a
rerun by hand, which started the suite it was in from its first case again.
Now an interrupted or stopped profile has Resume: the suites it finished stay,
the suite it was in carries on at its case when that suite has not changed,
and the suites it never reached run. A single run has the same.
"""
import asyncio
import json
import time

import pytest

from foundry_router import bench, benchprofile as bp
from tests.test_bench_card import _svc
from tests.test_bench_profile import profile, svc_with


def _suite(svc, name, n, ladder=False):
    rows = []
    for i in range(n):
        tags = [f"ladder:x", f"level:{i + 1}"] if ladder else []
        rows.append({"id": f"c{i}", "task_id": "t", "tags": tags,
                     "messages": [{"role": "user", "content": f"q{i}"}],
                     "reference": {"content": "ok", "must_include": ["ok"]}})
    (bench._dir(svc) / f"{name}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _asker(sent, answers=None, stop_after=None):
    async def ask(svc_, model, case, *a, **k):
        sent.append(case["id"])
        if stop_after is not None and len(sent) >= stop_after:
            bench._state["stop"] = True
        ok = (answers or {}).get(case["id"], True)
        return {"content": "ok" if ok else "no", "seconds": 2.0,
                "completion_tokens": 5, "prompt_tokens": 40}
    return ask


@pytest.fixture(autouse=True)
def _idle(monkeypatch):
    monkeypatch.setattr(bench, "_state", {"state": "idle"})
    bp._state.clear()
    bp._state.update({"state": "idle"})
    yield
    bp._state.clear()
    bp._state.update({"state": "idle"})


def _cut_off(svc, monkeypatch, n=6, after=3, answers=None, ladder=False):
    """A run of `n` cases that a restart stopped after `after` of them."""
    _suite(svc, "alpha", n, ladder=ladder)
    first = []
    monkeypatch.setattr(bench, "ask", _asker(first, answers, stop_after=after))
    out = asyncio.run(bench._run(svc, "alpha", "m", "lbl", 0))
    svc.db.execute("UPDATE bench_runs SET status='interrupted' WHERE id=?", (out["run_id"],))
    bench._state.clear()
    bench._state.update({"state": "idle"})
    return out["run_id"], first


# -- a single run -----------------------------------------------------------------------

def test_a_run_carries_on_at_the_case_it_stopped(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    answers = {"c1": False, "c4": False}
    rid, first = _cut_off(svc, monkeypatch, answers=answers)
    assert first == ["c0", "c1", "c2"]
    sent = []
    monkeypatch.setattr(bench, "ask", _asker(sent, answers))
    out = asyncio.run(bench._run(svc, "alpha", "m", "lbl", 0, resume=rid))
    assert sent == ["c3", "c4", "c5"], "the finished cases are not sent again"
    assert out["run_id"] == rid, "the same run, not a new one"
    run = svc.db.query_one("SELECT * FROM bench_runs WHERE id=?", (rid,))
    assert run["status"] == "done" and run["scored"] == 6
    rows = svc.db.query("SELECT case_id, score FROM bench_results WHERE run_id=? ORDER BY id",
                        (rid,))
    assert [r["case_id"] for r in rows] == [f"c{i}" for i in range(6)]
    want = round(sum(r["score"] for r in rows) / 6, 1)
    assert run["score"] == want and out["score"] == want, "every case counts in the score"
    assert run["seconds"] == 2.0
    assert "resumed after case 3 of 6" in run["note"]


def test_a_ladder_closed_before_the_cut_stays_closed(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    answers = {f"c{i}": False for i in range(8)}
    rid, first = _cut_off(svc, monkeypatch, n=8, after=2, answers=answers, ladder=True)
    assert first == ["c0", "c1"], "two misses close the ladder"
    sent = []
    monkeypatch.setattr(bench, "ask", _asker(sent, answers))
    asyncio.run(bench._run(svc, "alpha", "m", "lbl", 0, resume=rid))
    assert sent == [], "the levels above were not sent before the cut and are not now"
    detail = svc.db.query("SELECT code_detail FROM bench_results WHERE run_id=? ORDER BY id",
                          (rid,))
    assert sum("not_sent=1" in (r["code_detail"] or "") for r in detail) == 6


def test_resume_is_refused_when_it_would_not_be_the_same_test(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    rid, _ = _cut_off(svc, monkeypatch)
    assert bench.resumable(svc, rid)[0] is not None
    assert "no run 999" in bench.resumable(svc, 999)[1]
    svc.db.execute("UPDATE bench_runs SET status='done' WHERE id=?", (rid,))
    assert "only an interrupted or stopped run" in bench.resumable(svc, rid)[1]
    svc.db.execute("UPDATE bench_runs SET status='interrupted' WHERE id=?", (rid,))
    _suite(svc, "alpha", 7)                       # the suite was rebuilt since
    why = bench.resumable(svc, rid)[1]
    assert "has changed since" in why and "run it again" in why
    with pytest.raises(ValueError, match="has changed since"):
        asyncio.run(bench._run(svc, "alpha", "m", "lbl", 0, resume=rid))


def test_resume_behind_the_button_runs_on_its_thread(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    rid, _ = _cut_off(svc, monkeypatch)
    sent = []
    monkeypatch.setattr(bench, "ask", _asker(sent))
    import foundry_router.loopclient as lc
    monkeypatch.setattr(lc, "run_on_own_loop", lambda svc_, coro: asyncio.run(coro))
    out = bench.resume(svc, rid)
    assert out["state"] in ("running", "done"), "started (a fake run can finish at once)"
    for _ in range(200):
        if bench._state.get("state") != "running":
            break
        time.sleep(0.02)
    assert sent == ["c3", "c4", "c5"]
    assert svc.db.query_one("SELECT status FROM bench_runs WHERE id=?", (rid,))["status"] == "done"
    assert bench.resume(svc, rid)["state"] == "refused", "a finished run does not resume"


def test_resume_waits_while_anything_runs(tmp_path, monkeypatch):
    svc = _svc(tmp_path)
    rid, _ = _cut_off(svc, monkeypatch)
    bench._state.update({"state": "running"})
    out = bench.resume(svc, rid)
    assert out["state"] == "refused" and "running" in out["error"]


# -- a profile --------------------------------------------------------------------------

def _cut_profile(svc, done, wanted, status="interrupted", repeats=1):
    pid = profile(svc, "flash", done, label="v246 all", repeats=repeats)
    svc.db.execute("UPDATE bench_profiles SET status=?, suites=? WHERE id=?",
                   (status, json.dumps(wanted), pid))
    return pid


statuses: list = []


def _profile_fakes(svc, monkeypatch, built, scores, seen):
    statuses.clear()
    monkeypatch.setattr(bench, "suites", lambda s: [{"name": n} for n in built])
    monkeypatch.setattr(bench, "resumable", lambda s, rid: ({"id": rid}, ""))
    monkeypatch.setattr(bench, "state", lambda: {"state": "idle"})

    async def run(_svc, suite, model, label, limit, resume=0):
        seen.append((suite, resume, label))
        statuses.append(svc.db.query_one("SELECT status FROM bench_profiles ORDER BY id DESC "
                                         "LIMIT 1")["status"])
        if resume:
            svc.db.execute("UPDATE bench_runs SET status='done', score=? WHERE id=?",
                           (scores[suite], resume))
            return {"run_id": resume}
        rid = svc.db.execute(
            "INSERT INTO bench_runs (ts, suite, model, label, cases, scored, score, seconds, "
            "status) VALUES (datetime('now'),?,?,?,10,10,?,1.0,'done')",
            (suite, model, label, scores[suite]))
        return {"run_id": rid}
    monkeypatch.setattr(bench, "_run", run)
    import foundry_router.loopclient as lc
    monkeypatch.setattr(lc, "run_on_own_loop", lambda svc_, coro: asyncio.run(coro))


def _wait():
    for _ in range(200):
        if bp._state.get("state") != "running":
            return
        time.sleep(0.02)


def test_a_profile_resumes_the_suite_it_was_in_and_runs_the_rest(tmp_path, monkeypatch):
    svc = svc_with(tmp_path)
    pid = _cut_profile(svc, {"baseline": 80.0, "needle": 100.0},
                       ["baseline", "needle", "summaries", "toolflow", "toolpick"])
    # the suite a restart cut off: on no card yet, under the profile's label
    part = svc.db.execute(
        "INSERT INTO bench_runs (ts, suite, model, label, cases, scored, status) "
        "VALUES (datetime('now','+1 second'),'summaries','flash','v246 all',60,31,'interrupted')")
    seen = []
    _profile_fakes(svc, monkeypatch, ["baseline", "needle", "summaries", "toolflow",
                                      "toolpick"],
                   {"summaries": 90.0, "toolflow": 70.0, "toolpick": 60.0}, seen)
    assert bp.missing(svc, pid) == ["summaries", "toolflow", "toolpick"]
    out = bp.resume(svc, pid)
    assert out["suites"] == ["summaries", "toolflow", "toolpick"]
    assert out["carried_on"] == {"summaries": part}
    _wait()
    assert statuses == ["running"] * 3, "running while it runs: a second restart is caught too"
    assert seen == [("summaries", part, "v246 all"), ("toolflow", 0, "v246 all"),
                    ("toolpick", 0, "v246 all")]
    p = svc.db.query_one("SELECT * FROM bench_profiles WHERE id=?", (pid,))
    assert p["status"] == "done"
    card = bp.build_card(svc, pid)
    assert {x["suite"]: x["score"] for x in card["parts"]} == {
        "baseline": 80.0, "needle": 100.0, "summaries": 90.0, "toolflow": 70.0,
        "toolpick": 60.0}
    assert p["score"] == card["composite"]
    assert bp.missing(svc, pid) == []


def test_a_profile_counts_the_passes_it_lacks(tmp_path):
    svc = svc_with(tmp_path)
    pid = _cut_profile(svc, {"a": [50.0, 60.0], "b": [70.0]}, ["a", "b", "c"], repeats=2)
    assert bp.missing(svc, pid) == ["c", "b", "c"]


def test_a_profile_with_nothing_left_is_marked_done(tmp_path, monkeypatch):
    """The flash profile resumed by ticking its missing suites into it before
    this release still said interrupted when they had all run."""
    svc = svc_with(tmp_path)
    pid = _cut_profile(svc, {"a": 50.0, "b": 70.0}, ["a", "b"])
    monkeypatch.setattr(bench, "suites", lambda s: [{"name": "a"}, {"name": "b"}])
    monkeypatch.setattr(bench, "state", lambda: {"state": "idle"})
    out = bp.resume(svc, pid)
    assert out["state"] == "done" and out["composite"] == 60.0
    p = svc.db.query_one("SELECT status, score FROM bench_profiles WHERE id=?", (pid,))
    assert p["status"] == "done" and p["score"] == 60.0


def test_a_rerun_into_a_cut_off_card_that_completes_it_marks_it_done(tmp_path, monkeypatch):
    svc = svc_with(tmp_path)
    pid = _cut_profile(svc, {"a": 50.0}, ["a", "b"])
    seen = []
    _profile_fakes(svc, monkeypatch, ["a", "b"], {"b": 70.0}, seen)
    asyncio.run(bp._run(svc, pid, "flash", ["b"], "", 1, 0, into=True))
    assert svc.db.query_one("SELECT status FROM bench_profiles WHERE id=?",
                            (pid,))["status"] == "done"


def test_a_profile_refuses_what_it_cannot_resume(tmp_path, monkeypatch):
    svc = svc_with(tmp_path)
    done = profile(svc, "flash", {"a": 50.0})
    monkeypatch.setattr(bench, "suites", lambda s: [{"name": "a"}])
    monkeypatch.setattr(bench, "state", lambda: {"state": "idle"})
    assert "only an interrupted or stopped" in bp.resume(svc, done)["error"]
    assert "no profile 77" in bp.resume(svc, 77)["error"]
    pid = _cut_profile(svc, {"a": 50.0}, ["a", "gone"], status="stopped")
    assert "not built any more: gone" in bp.resume(svc, pid)["error"]
    monkeypatch.setattr(bench, "state", lambda: {"state": "running"})
    assert "running" in bp.resume(svc, pid)["error"]


def test_a_cut_off_run_of_another_label_or_a_changed_suite_starts_over(tmp_path, monkeypatch):
    svc = svc_with(tmp_path)
    pid = _cut_profile(svc, {"a": 50.0}, ["a", "b"])
    svc.db.execute("INSERT INTO bench_runs (ts, suite, model, label, cases, status) "
                   "VALUES (datetime('now','+1 second'),'b','flash','another run',9,'interrupted')")
    seen = []
    _profile_fakes(svc, monkeypatch, ["a", "b"], {"b": 70.0}, seen)
    out = bp.resume(svc, pid)
    _wait()
    assert out["carried_on"] == {} and seen == [("b", 0, "v246 all")]


# -- the wire and the page ---------------------------------------------------------------

def test_the_routes_and_the_buttons_are_there():
    from pathlib import Path
    root = Path(bench.__file__).parent
    routes = (root / "ui" / "routes.py").read_text(encoding="utf-8")
    assert '@router.post("/admin/api/bench/profile/resume")' in routes
    assert 'body.get("resume")' in routes
    page = (root / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'class="act pf-resume"' in page and 'api("bench/profile/resume"' in page
    assert 'class="act bench-resume"' in page and "resume: +btn.dataset.id" in page
