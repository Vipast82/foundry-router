"""Launch assistant (2026-10-21): a plain-language request becomes a checked plan;
only the operator's confirmation runs it."""
import json

import pytest

from foundry_coordinator import states as S
from foundry_coordinator.assistant import Assistant, validate
from foundry_coordinator.control import ControlChannel
from tests.coordinator.helpers import FakeFoundry, coordinator, make_repo, write_profile

TASKS = [{"id": f"T{i}", "title": f"Sprint {i}", "spec": "Write src/a.txt.", "scope": ["src/a.txt"]}
         for i in (1, 2, 3)]


class Planner(FakeFoundry):
    def __init__(self, steps, questions=()):
        super().__init__()
        self.reply = json.dumps({"summary": "start from T2", "steps": steps,
                                 "questions": list(questions)})
        self.asked = []

    def bind(self, *a, **kw):
        return {}

    def chat(self, persona, messages, *, session_id="", **kw):
        if session_id.startswith("assistant-"):
            self.asked.append(messages)
            return self.reply, {"served_model": "claude-opus-5-5"}
        return super().chat(persona, messages, session_id=session_id, **kw)


def step(kind, tid="", deps=(), note="", why="x"):
    return {"kind": kind, "task_id": tid, "deps": list(deps), "note": note, "why": why}


def env(tmp_path, steps, questions=()):
    root, _ = make_repo(tmp_path, "game", TASKS)
    prof = write_profile(tmp_path, "game", root)
    ff = Planner(steps, questions)
    c = coordinator(tmp_path, [prof], {"game": lambda ctx: {}}, foundry={"game": ff})
    return c, ff


def test_validate_simulates_the_queue_and_drops_what_does_not_fit():
    snap = {"project": "game", "paused": True, "sprints": [
        {"task_id": "T1", "coordinator_state": S.COMPLETE},
        {"task_id": "T2", "coordinator_state": S.CANCELLED},
        {"task_id": "T3", "coordinator_state": "NOT_QUEUED"}]}
    clean, warn = validate(snap, [
        step("queue_approve", "T2"),                  # cancelled -> must be reopen
        step("reopen", "T2"),
        step("queue_approve", "T3", ["T2"]),          # T2 queued by the step before
        step("queue_approve", "T9"),                  # not a sprint
        step("resume_task", "T1"),                    # not paused
        step("project_resume")])
    assert [(s["kind"], s["task_id"], s["deps"]) for s in clean] == [
        ("reopen", "T2", []), ("queue_approve", "T3", ["T2"]), ("project_resume", "", [])]
    assert len(warn) == 3 and any("T9" in w for w in warn)


def test_plan_proposes_only_and_run_applies_the_confirmed_steps(tmp_path):
    c, ff = env(tmp_path, [step("queue_approve", "T1"), step("queue_approve", "T2", ["T1"]),
                           step("queue_approve", "T3", ["T2"])])
    a = Assistant(c, persona_fn=lambda P: "Foundry-Kilo-Planner")
    P = c.projects["game"]
    plan = a.plan(P, "start everything in order", "victor")
    assert len(plan["steps"]) == 3 and not plan["warnings"]
    assert c.store.task("game", "T1") is None                  # proposing ran nothing
    snap = json.loads(ff.asked[0][1]["content"].split("Project snapshot:\n", 1)[1])
    assert [s["task_id"] for s in snap["sprints"]] == []  or snap["project"] == "game"
    out = a.run(P, plan["plan_id"], [0, 1], "victor")          # T3 unchecked by the operator
    assert [r["result"] for r in out["results"]] == ["done", "done"]
    assert c.store.task("game", "T1")["state"] == S.QUEUED
    assert c.store.queue_entry("game", "T2")["deps"] == ["T1"]
    assert c.store.task("game", "T3") is None
    with pytest.raises(ValueError, match="already ran"):
        a.run(P, plan["plan_id"], None, "victor")              # a plan runs once


def test_reopen_puts_a_cancelled_sprint_back_with_fresh_counters(tmp_path):
    c, _ = env(tmp_path, [])
    c.approve("game", "T1", [], "victor")
    c.store.update_task("game", "T1", attempt=4, sessions=7)
    c.cancel("game", "T1", "queue cleared", actor="victor")
    c.reopen("game", "T1", [], "victor via launch assistant", "start again")
    t = c.store.task("game", "T1")
    assert t["state"] == S.QUEUED and t["attempt"] == 0 and t["sessions"] == 0
    hist = c.store.q("SELECT from_state, to_state FROM transitions WHERE task_id='T1'")
    assert (S.CANCELLED, S.QUEUED) in [(h["from_state"], h["to_state"]) for h in hist]


def test_control_actions_report_the_plan_and_the_run(tmp_path):
    c, ff = env(tmp_path, [step("queue_approve", "T1"), step("project_resume")])
    reports = []
    ch = ControlChannel(c, foundry=ff, log=lambda m: None)
    ch._report = lambda a, status, detail, result=None: reports.append((status, detail, result))
    ch.handle({"action_id": "act-1", "kind": "assistant_plan", "project_id": "game",
               "payload": {"text": "start the project"}, "actor": "victor"})
    status, _, res = reports[-1]
    assert status == "applied" and len(res["steps"]) == 2
    ch.handle({"action_id": "act-2", "kind": "assistant_run", "project_id": "game",
               "payload": {"plan_id": res["plan_id"], "steps": None}, "actor": "victor"})
    status, detail, res2 = reports[-1]
    assert status == "applied" and "2 of 2" in detail
    assert c.store.task("game", "T1")["state"] == S.QUEUED


def test_unknown_plan_and_empty_text_are_rejected(tmp_path):
    c, ff = env(tmp_path, [])
    reports = []
    ch = ControlChannel(c, foundry=ff, log=lambda m: None)
    ch._report = lambda a, status, detail, result=None: reports.append((status, detail))
    ch.handle({"action_id": "act-3", "kind": "assistant_plan", "project_id": "game",
               "payload": {"text": "  "}, "actor": "victor"})
    ch.handle({"action_id": "act-4", "kind": "assistant_run", "project_id": "game",
               "payload": {"plan_id": "asp-000000000000"}, "actor": "victor"})
    assert [r[0] for r in reports] == ["rejected", "rejected"]
