"""0.145.2: a decision's timeout default never fires while a lead case is still
working the same stop (it parked GF-NAME1 and GF-GATE1 mid-repair on 2026-10-27)."""

from __future__ import annotations

import json

from foundry_coordinator import decision_policy as DP
from tests.coordinator.test_tracks_manager import env as tracks_env


def _setup(tmp_path, case_state):
    c, P, _ = tracks_env(tmp_path)
    now = [10_000.0]
    c.clock = lambda: now[0]
    c.approve("game", "T1", [], "victor")
    c.store.merge_data("game", "T1", recovery_case="rc-x")
    c.store.x("CREATE TABLE IF NOT EXISTS recovery_cases (case_id TEXT PRIMARY KEY, "
              "state TEXT, deadline_ts REAL)")
    cols = [r["name"] for r in c.store.q("PRAGMA table_info(recovery_cases)")]
    vals = {"case_id": "rc-x", "state": case_state, "deadline_ts": now[0] + 3600}
    for k in cols:
        if k in ("project_id",):
            vals[k] = "game"
        elif k in ("task_id",):
            vals[k] = "T1"
    keys = [k for k in vals if k in cols]
    c.store.x(f"INSERT INTO recovery_cases ({', '.join(keys)}) VALUES "
              f"({', '.join('?' for _ in keys)})", [vals[k] for k in keys])
    did = c.obligations.open_decision("game", "T1", "recovery_escalation", "findings repeat?",
                                      options=["retry", "stop the task"], blocks=["T1"])
    c.store.x("UPDATE decisions SET recommended=?, deadline_ts=?, timeout_minutes=30, "
              "decision_class='decision' WHERE decision_id=?",
              (json.dumps("stop the task"), now[0] - 1, did))
    return c, now, did


def test_the_timeout_waits_while_the_lead_is_working(tmp_path):
    c, now, did = _setup(tmp_path, "REPAIRING")
    assert DP.sweep(c, force=True) == []
    d = c.obligations.decision(did)
    assert d["status"] == "open" and d["deadline_ts"] >= now[0] + 3600
    assert [e for e in c.store.events("game", 50) if e["kind"] == "decision_timeout_deferred"]
    # the lead gave up: the default applies at the (moved) deadline
    c.store.x("UPDATE recovery_cases SET state='NEEDS_VICTOR' WHERE case_id='rc-x'")
    now[0] = d["deadline_ts"] + 1
    assert DP.sweep(c, force=True) == [did]
    assert c.obligations.decision(did)["status"] == "answered"


def test_the_timeout_applies_without_a_working_lead(tmp_path):
    c, now, did = _setup(tmp_path, "CANCELLED")
    assert DP.sweep(c, force=True) == [did]
