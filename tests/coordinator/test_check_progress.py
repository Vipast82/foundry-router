"""2026-10-27: the per-check progress of background verify / closeout runs, for the
task note and the control center's activity graph."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from foundry_coordinator import check_progress as CP
from foundry_coordinator.bg_checks import BackgroundChecks


def row(result, required=True):
    return SimpleNamespace(result=result, required=required)


def test_track_records_results_and_running_lists_live_checks():
    CP.reset("p", "T", kind="verify", head="abcdef1234567890")
    assert CP.track("p", "T", "done_check", lambda: [row("PASS"), row("FAIL", False)]) \
        == [row("PASS"), row("FAIL", False)]
    CP.carried("p", "T", "Lune negative control")
    gate = threading.Event()
    t = threading.Thread(target=lambda: CP.track("p", "T", "Lune suite",
                                                 lambda: (gate.wait(5), [row("FAIL")])[1]))
    t.start()
    try:
        names = [r["name"] for r in CP.snapshot("p", "T")]
        assert names[0] == "done_check" and "Lune suite" in names
        run = [r for r in CP.running() if r["task"] == "T"][0]
        assert run["kind"] == "verify" and run["head"] == "abcdef123456"
        states = {c["name"]: c["state"] for c in run["checks"]}
        assert states == {"done_check": "PASS", "Lune negative control": "PASS (reused)",
                          "Lune suite": "running"}
        assert "Lune suite running" in CP.summary("p", "T")
    finally:
        gate.set()
        t.join()
    assert {r["name"]: r["state"] for r in CP.snapshot("p", "T")}["Lune suite"] == "FAIL"
    assert not [r for r in CP.running() if r["task"] == "T"]          # nothing running now
    CP.clear("p", "T")


def test_a_background_run_resets_its_task_progress():
    CP.start("p", "T2", "stale check")
    bg = BackgroundChecks()
    st, _ = bg.run(("p", "T2", "verify", "h1"), lambda: [], wait_s=2)
    assert st == "done" and CP.snapshot("p", "T2") == []
    CP.clear("p", "T2")
