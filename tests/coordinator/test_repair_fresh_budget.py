"""2026-11-03 HK-ASSET1: the local2 merge's Studio batch was red for it, the Lead reopened it on
main (repair_reopen: "fresh attempts"), but the budget ledger still held the original sprint's
6 attempts. Its first repair session made 7/7 and it went straight to a Lead case. A repair or
an operator reopen starts a new budget; the earlier ledger rows stay as history."""

from __future__ import annotations

from foundry_coordinator import states as S
from foundry_coordinator.obligations import budget_epoch
from tests.coordinator.test_lifecycle_e2e import green_t1
from tests.coordinator.test_lifecycle_engine import _game

TID = "T1"


def _spent(c, n):
    for i in range(n):
        c.obligations.charge("game", TID, "attempt_ordinary", 1, f"attempt:run{i}", "engine")
        c.obligations.charge("game", TID, "sessions", 1, f"session:run{i}", "engine")


def test_a_merge_repair_starts_a_fresh_attempt_budget(tmp_path):
    c, *_ = _game(tmp_path, lambda ctx: green_t1(ctx))
    c.approve("game", TID, [], "victor")
    P = c.projects["game"]
    lim = c.obligations.effective_limits(P, c.store.task("game", TID))["ordinary"]
    _spent(c, lim["max"])
    assert c.obligations.effective_limits(P, c.store.task("game", TID))["ordinary"]["exhausted"]
    c.store.x("UPDATE tasks SET state=? WHERE project_id=? AND task_id=?", (S.COMPLETE, "game", TID))
    c.clock = lambda: 9.9e9                      # the repair comes after every spent row
    c.repair_reopen("game", TID, [], "lead_manager", "track t merge Studio batch", "fix row X")
    t = c.store.task("game", TID)
    assert budget_epoch(t) == 9.9e9
    L = c.obligations.effective_limits(P, t)
    assert L["ordinary"]["used"] == 0 and L["ordinary"]["remaining"] == lim["max"]
    assert L["sessions"]["used"] == 0
    # the history is kept
    assert c.obligations.counters("game", TID)["attempt_ordinary"] == lim["max"]


def test_without_a_reopen_every_row_counts():
    assert budget_epoch({"data": {}}) is None
    assert budget_epoch({"data": {"reopened": {"by": "v", "reason": "r"}}}) is None
    assert budget_epoch({"data": {"repair": {"ts": 5.0}, "reopened": {"ts": 7.0}}}) == 7.0
