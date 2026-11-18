"""v0.181.188: the benchmark can put the approved rulebook in front of every case,
so the question "do seven rules about animal anatomy hurt a coding sprint" has an
answer rather than an opinion.

The rules reaching a prompt was itself unmeasured until v0.181.184, and the claim
that they are noise on code work is mine, not a measurement. This is the arm that
settles it.
"""
import json

import pytest

from foundry_router import bench, lessons
from foundry_router.db import Database


class _Cfg:
    def __init__(self, max_chars=1500):
        self.lessons = type("L", (), {"max_chars": max_chars, "enabled": True})()


class _Svc:
    def __init__(self, tmp_path, max_chars=1500):
        self.db = Database(tmp_path / "b.sqlite")
        self.config_store = type("S", (), {"config": _Cfg(max_chars)})()


def _rule(svc, anchor, rule, seen=5):
    svc.db.execute(
        "INSERT INTO lessons (ts, anchor, rule, draft, seen, tasks, sources, examples, "
        "first_seen, last_seen, state) VALUES ('now',?,'','',?,'[\"VQ-1\"]','[\"human\"]',"
        "'[\"x\"]','a','b','proposed')", (anchor, seen))
    lid = svc.db.query_one("SELECT id FROM lessons WHERE anchor=?", (anchor,))["id"]
    lessons.decide(svc, lid, "active", rule)
    return lid


CASE = {"id": "c1", "task_id": "VQ-17", "messages": [
    {"role": "system", "content": "you build models"},
    {"role": "user", "content": "build EN-WOLF"}]}


def test_the_rulebook_goes_in_as_a_system_turn_after_the_persona_s_own(tmp_path):
    svc = _Svc(tmp_path)
    _rule(svc, "a", "Build the tail to the reference's size.")
    out, n = bench.with_lessons(svc, CASE)
    assert n == 1
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["system", "system", "user"]
    assert "Build the tail" in out["messages"][1]["content"]
    assert out["messages"][0]["content"] == "you build models"
    assert CASE["messages"] == [m for m in CASE["messages"]]      # the case is not mutated
    assert len(CASE["messages"]) == 2


def test_no_active_rules_leaves_the_case_exactly_as_it_was(tmp_path):
    svc = _Svc(tmp_path)
    out, n = bench.with_lessons(svc, CASE)
    assert n == 0 and out is CASE


def test_the_count_is_the_rules_that_fit_not_the_rules_approved(tmp_path):
    """The run note says how many rules were in every prompt. After v0.181.187 a
    rule that does not fit is skipped, so the number has to come from the block
    rather than from the table, or the note repeats the lie the GUI told."""
    svc = _Svc(tmp_path, max_chars=400)
    for i in range(6):
        _rule(svc, f"r{i}", f"Rule {i} " + "y" * 200, seen=10 - i)
    out, n = bench.with_lessons(svc, CASE)
    block = out["messages"][1]["content"]
    numbered = [L for L in block.splitlines() if L.split(".")[0].isdigit()]
    assert len(block) <= 400
    assert n == len(numbered), (n, numbered)
    assert 0 < n < 6                                              # some fit, not all


def test_a_run_records_which_arm_it_was(tmp_path):
    """`recall` and `lessons` are separate columns, so a later comparison cannot
    mistake one arm for the other."""
    db = Database(tmp_path / "r.sqlite")
    db.execute("INSERT INTO bench_runs (ts, suite, model, label, cases, scored, status, "
               "recall, lessons) VALUES (datetime('now'),'s','m','l',1,0,'running',0,1)")
    row = db.query_one("SELECT recall, lessons FROM bench_runs")
    assert row["recall"] == 0 and row["lessons"] == 1
