"""Live test counts (2026-10-27). Victor: "is there a way to have a status bar, with 8 out
of 30 tests completed, pass / fail values, live tracker?" The counts come from the runs'
own lines (PASS:/FAIL:, LUNE n/m, -> N CALLs / CALL n, EXIT=), read incrementally."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from types import SimpleNamespace

from foundry_coordinator import check_progress, live_tests
from foundry_coordinator import states as S
from foundry_coordinator.engine import Coordinator
from foundry_coordinator.telemetry import live_tests_view


def test_counts_parse_lune_studio_and_runner_lines():
    c = live_tests.Counts()
    c.feed("CMD: lune run tests/lune/run.luau\nSTATUS RUNNING\nPASS: A :: a\nFAIL: A :: b — x\nPA")
    assert c.row() == {"done": 2, "total": None, "pass": 1, "fail": 1, "result": None}
    c.feed("SS: B :: c\nLUNE 2/3 FAIL (5 ms)\nEXIT=1\n")    # the split line counts once whole
    assert c.row() == {"done": 3, "total": 3, "pass": 2, "fail": 1, "result": "FAIL"}
    assert c.finished and c.cmd.startswith("lune run")
    s = live_tests.Counts()
    s.feed("R1 plan: GateRunner.run(\"GF_GATE1,S51\") -> 17 CALLs (boot 1-2)\n"
           "CALL 1 [vm=server] eval_server_runtime :: x\nPASS: BOOT counts\n"
           "CALL 2 [vm=client] eval_client_runtime :: y\nFAIL: BOOT client\n")
    r = s.row()
    assert (r["done"], r["total"], r["pass"], r["fail"]) == (2, 17, 1, 1) and not s.finished


def test_read_is_incremental_and_a_rewritten_file_starts_over(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("PASS: a\n", encoding="utf-8")
    assert live_tests.read(str(p)).passed == 1
    with open(p, "a", encoding="utf-8") as f:
        f.write("PASS: b\nFAIL: c\n")
    c = live_tests.read(str(p))
    assert (c.passed, c.failed) == (2, 1)
    p.write_text("FAIL: z\n", encoding="utf-8")                  # shorter: a new run
    c = live_tests.read(str(p))
    assert (c.passed, c.failed) == (0, 1)
    os.remove(p)
    assert live_tests.read(str(p)) is None


def _log(ws, name, text, age=0.0):
    d = ws / "tmp" / "guard" / "cmd"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    t = time.time() - age
    os.utime(p, (t, t))
    return p


PATS = ["tmp/guard/cmd/*.log", "docs/progress/{task}_studio_lines.txt"]


def test_collect_shows_running_and_recent_runs_with_the_last_known_total(tmp_path):
    ws, state = tmp_path / "ws", tmp_path / "state"
    ws.mkdir(), state.mkdir()
    _log(ws, "20261027-100000_lune_full.log",
         "CMD: lune run tests/lune/run.luau\n" + "PASS: a :: b\n" * 5 + "LUNE 5/5 PASS (1 ms)\n"
         "EXIT=0\n", age=300)
    rows = live_tests.collect(str(ws), "GF-X", PATS, str(state))
    assert [(r["state"], r["done"], r["total"]) for r in rows] == [("PASS", 5, 5)]
    assert rows[0]["name"] == "Lune suite · lune_full"
    # the next run, still going: its bar uses the last full run's total, marked estimated
    _log(ws, "20261027-110000_lune_again.log",
         "CMD: lune run tests/lune/run.luau\nSTATUS RUNNING\nPASS: a :: b\nFAIL: a :: c\n")
    r = live_tests.collect(str(ws), "GF-X", PATS, str(state))[0]
    assert (r["state"], r["done"], r["total"], r["estimated"], r["fail"]) == \
        ("running", 2, 5, True, 1)
    # not a test run (no test lines) and long finished: not shown
    _log(ws, "20261027-090000_hash.log", "CMD: powershell tools/hash.ps1\nEXIT=0\n", age=60)
    _log(ws, "20261027-080000_old.log", "CMD: lune run x\nPASS: a\nEXIT=0\n", age=5000)
    names = [r["name"] for r in live_tests.collect(str(ws), "GF-X", PATS, str(state))]
    assert names == ["Lune suite · lune_again", "Lune suite · lune_full"]


def test_collect_counts_studio_calls_of_the_sprint(tmp_path):
    ws = tmp_path / "ws"
    p = ws / "docs" / "progress" / "GF-X_studio_lines.txt"
    p.parent.mkdir(parents=True)
    p.write_text("R1 plan: -> 30 CALLs\n" + "".join(
        f"CALL {i} [vm=server] x\nPASS: ok {i}\n" for i in range(1, 9)), encoding="utf-8")
    (ws / "docs" / "progress" / "GF-Y_studio_lines.txt").write_text("CALL 1\n", encoding="utf-8")
    rows = live_tests.collect(str(ws), "GF-X", PATS)
    assert len(rows) == 1
    r = rows[0]
    assert (r["name"], r["state"], r["done"], r["total"], r["pass"], r["fail"]) == \
        ("Studio gate CALLs", "running", 8, 30, 8, 0)


def test_check_progress_carries_counts_into_the_summary_and_heartbeat():
    check_progress.reset("p", "T1", "verify", "abc")
    check_progress.start("p", "T1", "Lune suite")
    check_progress.counts("p", "T1", "Lune suite",
                          {"done": 812, "total": 1609, "pass": 810, "fail": 2, "estimated": True})
    assert "Lune suite running 812/~1609, 2 fail" in check_progress.summary("p", "T1")
    run = [r for r in check_progress.running() if r["task"] == "T1"][0]
    assert run["checks"][0]["done"] == 812 and run["checks"][0]["fail"] == 2
    check_progress.clear("p", "T1")


def test_lune_run_reports_counts_while_it_runs(tmp_path, monkeypatch):
    from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
    a = AnimalUprisingAdapter.__new__(AnimalUprisingAdapter)
    a.p = SimpleNamespace(id="p", workspace=str(tmp_path))
    a.cfg, a.state_dir = {}, str(tmp_path)
    a.LUNE_POLL_S = 0.02
    seen, half = [], threading.Event()

    def fake(cmd, **kw):
        out = kw["stdout"]
        out.write("PASS: a :: 1\nPASS: a :: 2\nFAIL: a :: 3\n")
        out.flush()
        half.wait(2)
        out.write("PASS: a :: 4\nLUNE 3/4 FAIL (2 ms)\n")
        out.flush()
        return subprocess.CompletedProcess(cmd, 1, None, None)

    def progress(c, final=False):
        seen.append((c.row()["done"], c.row()["fail"], final))
        if c.row()["done"] == 3:
            half.set()

    monkeypatch.setattr(subprocess, "run", fake)
    r = a._lune_run("lune", "au.lune", [], r"^LUNE \d+/\d+ PASS", None, str(tmp_path), "rev",
                    progress=progress)
    assert r.result == "FAIL" and "LUNE 3/4 FAIL" in open(r.evidence, encoding="utf-8").read()
    assert (3, 1, False) in seen                         # seen mid-run
    assert seen[-1] == (4, 1, True)
    # the real callback stores counts and remembers the suite's total for the next run
    check_progress.reset("p", "T2")
    check_progress.start("p", "T2", "Lune suite")
    cb = a._lune_progress("T2", "Lune suite", "normal")
    c = live_tests.Counts()
    c.feed("PASS: x\nLUNE 1/1 PASS\n")
    cb(c, final=True)
    assert live_tests._known_total(str(tmp_path), "lune:full") == 1
    c2 = live_tests.Counts()
    c2.feed("PASS: x\n")
    cb(c2)
    snap = check_progress.snapshot("p", "T2")[0]
    assert (snap["done"], snap["total"], snap["estimated"]) == (1, 1, True)
    check_progress.clear("p", "T2")


class _Store:
    def __init__(self, t):
        self.t, self.writes = t, 0

    def merge_data(self, pid, tid, **kv):
        self.writes += 1
        for k, v in kv.items():
            if v is None:
                self.t["data"].pop(k, None)
            else:
                self.t["data"][k] = v


def test_engine_writes_live_tests_only_when_counts_change_and_clears_them(tmp_path):
    ws = tmp_path / "ws"
    _log(ws, "20261027-120000_lune.log", "CMD: lune run tests/lune/run.luau\nPASS: a\n")
    t = {"task_id": "GF-X", "state": S.IMPLEMENTING, "data": {}}
    st = _Store(t)
    clock = [100.0]
    eng = SimpleNamespace(store=st, clock=lambda: clock[0], state_dir=str(tmp_path),
                          LIVE_TEST_STATES=Coordinator.LIVE_TEST_STATES, LIVE_TEST_EVERY_S=3.0)
    P = SimpleNamespace(id="p", profile=SimpleNamespace(workspace=str(ws)),
                        adapter=SimpleNamespace(live_test_sources=lambda tid: PATS))
    Coordinator._live_tests(eng, P, t)
    rows = t["data"]["live_tests"]["rows"]
    assert rows[0]["done"] == 1 and rows[0]["state"] == "running" and st.writes == 1
    clock[0] += 5
    Coordinator._live_tests(eng, P, t)                   # nothing changed: no write
    assert st.writes == 1
    t["state"] = S.REVIEWING
    Coordinator._live_tests(eng, P, t)
    assert "live_tests" not in t["data"]
    # the bridge shows them only in the states that write them
    t["data"]["live_tests"] = {"rows": [{"name": "x" * 200, "done": 3, "total": 9, "pass": True}]}
    assert live_tests_view(t) is None
    t["state"] = S.VERIFYING
    v = live_tests_view(t)
    assert len(v[0]["name"]) <= 81 and v[0]["done"] == 3 and v[0]["pass"] is None


def test_router_bounds_the_tests_view():
    from foundry_router.control_center import ControlCenter
    v = ControlCenter._tests_view
    rows = [{"name": "Lune suite", "state": "running", "done": 900, "total": 800, "pass": 899,
             "fail": 1, "source": "coordinator"}] * 12
    out = v({"live_tests": rows}, "VERIFYING")
    assert len(out) == 8 and out[0]["total"] is None and out[0]["fail"] == 1
    assert v({"live_tests": rows}, "REVIEWING") is None
    assert v({"live_tests": "x"}, "VERIFYING") is None


def test_a_passed_negative_control_s_fails_are_not_failures():
    c = live_tests.Counts()
    c.feed("PASS: a\nPASS: b\nLUNE 2/2 PASS\nFAIL: a\nFAIL: b\nNEGATIVE CONTROL PASS (2/2)\n")
    r = c.row()
    assert (r["done"], r["pass"], r["fail"]) == (4, 2, 0)
    c = live_tests.Counts()
    c.feed("FAIL: a\nPASS: b\nNEGATIVE CONTROL FAIL (1/2)\n")
    assert c.row()["fail"] == 1
