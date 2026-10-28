"""2026-10-28: the Lead's hard cap (lead_hard_max_calls) counted every lead call of a task's
whole life, so once GF-GATE1 had used 12, every later escalation went straight to Victor even
after his fresh-budget unblocks. An operator unblock with --reset-attempts refills it."""

import json
import time
from types import SimpleNamespace

from foundry_coordinator.lead import LeadManager
from foundry_coordinator.store import Store


def _store(tmp_path):
    st = Store(str(tmp_path / "c.sqlite"))
    lm = LeadManager.__new__(LeadManager)
    lm.store = st
    st.x("""CREATE TABLE IF NOT EXISTS recovery_cases (case_id TEXT PRIMARY KEY, project_id TEXT,
            task_id TEXT)""")
    st.x("""CREATE TABLE IF NOT EXISTS recovery_actions (action_id TEXT PRIMARY KEY, case_id TEXT,
            kind TEXT, detail TEXT, status TEXT, ts REAL)""")
    return st, lm


def _call(st, n, ts):
    st.x("INSERT OR IGNORE INTO recovery_cases (case_id, project_id, task_id) VALUES (?,?,?)",
         ("rc1", "au", "T1"))
    st.x("INSERT INTO recovery_actions (action_id, case_id, kind, detail, status, ts) VALUES "
         "(?,?,?,?,?,?)", (f"a{n}", "rc1", "lead_call", json.dumps({"phase": "done"}), "done", ts))


def _transition(st, ts, reset):
    st.x("INSERT INTO transitions (ts, project_id, task_id, from_state, to_state, evidence, actor)"
         " VALUES (?,?,?,?,?,?,?)", (ts, "au", "T1", "BLOCKED", "READY",
                                     json.dumps({"reset_attempts": reset}), "operator"))


def test_an_operator_fresh_budget_refills_the_lead_cap(tmp_path):
    st, lm = _store(tmp_path)
    now = time.time()
    for i in range(12):
        _call(st, i, now - 3600 + i)
    assert lm.lifetime_calls("au", "T1") == 12
    _transition(st, now - 100, reset=False)          # a plain unblock: no refill
    assert lm.lifetime_calls("au", "T1") == 12
    _transition(st, now - 50, reset=True)            # a fresh budget: the count starts over
    assert lm.lifetime_calls("au", "T1") == 0
    _call(st, 99, now)
    assert lm.lifetime_calls("au", "T1") == 1
