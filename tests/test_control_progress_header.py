"""The control center's sprint progress header (v0.128): phase from the task
state, BUILD steps ticked out of the plan's total (the engine's own record,
never model text), the next step, Kilo's to-do count as a sub-line, the
required-work note, and what needs the operator. Run in node against the real
inline script; every value is escaped."""

import pytest

from tests.test_control_ui import _script
from tests.test_control_ui_behavior import between, run_js

import shutil

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")

HOSTILE = "<img src=x onerror=alert(1)>"


def _model(tmp_path, task, ctx=None, compact=False):
    s = _script()
    block = (between(s, "/*sprint-start*/", "/*sprint-end*/")
             + between(s, "/*lead-start*/", "/*lead-end*/")
             + between(s, "/*progress-start*/", "/*progress-end*/"))
    import json
    return run_js(tmp_path, """
const S = {tasks: new Map(), openProv: new Set()}; const taskKey = (p, t) => p + "/" + t;
const curTask = () => null; const short = (s, n = 10) => s ? String(s).slice(0, n) : "";
const tsText = t => "" + t; const fullTs = t => "" + t; const nowS = () => 0; const NA = () => "N/A";
const stateCls = () => "";
""" + block + """
const m = sprintModel(%s, %s);
console.log(JSON.stringify({m, html: sprintHtml(m, %s)}).replace(/[^\x00-\x7f]/g, c => String.fromCharCode(92) + "u" + c.charCodeAt(0).toString(16).padStart(4, "0")));
""" % (json.dumps(task), json.dumps(ctx or {}), "true" if compact else "false"))


def task(state, **data):
    tv = data.pop("view", {})
    return {"project_id": "au", "task_id": "UI-02", "state": state, "data": data, "task_view": tv}


def test_build_steps_ticked_of_total_with_next_step_and_note(tmp_path):
    b = {"open_steps": 5, "steps": ["- [ ] Capture the frame " + HOSTILE, "- [ ] b"], "truncated": False,
         "total_steps": 12, "head_sha": "a" * 40}
    t = task("CHANGES_REQUIRED", phase="implement", attempt=1, sessions=3,
             next_action="continue BUILD from committed plan",
             view={"build": b, "checklist": {"total": 24, "open": 22, "items": []}})
    todo = {"data": {"items": [{"status": "completed", "content": "a"}, {"status": "in_progress", "content": "b"},
                               {"status": "pending", "content": "c"}]}}
    out = _model(tmp_path, t, {"todo": todo})
    m, h = out["m"], out["html"]
    assert m["phase"] == "BUILD" and [p["status"] for p in m["phases"]] == ["done", "now", "todo", "todo", "todo"]
    assert m["build"]["done"] == 7 and m["build"]["total"] == 12 and round(m["build"]["pct"]) == 58
    assert m["build"]["next"].startswith("Capture the frame")          # checkbox marker stripped
    assert m["todo"] == {"done": 1, "total": 3, "current": "b"}
    assert m["obligations"] == {"open": 22, "total": 24} and m["needs"] == []
    assert "7</b> of <b class=\"num\">12</b> ticked" in h and 'aria-valuenow="7"' in h
    assert "--fill:58.3%" in h                                         # the BUILD chevron pours
    assert "Session to-do" in h and "closes together at the final gate" in h
    assert "Nothing right now" in h
    assert "<img" not in h and "&lt;img" in h


def test_without_total_or_with_truncation_never_claims_a_count(tmp_path):
    b = {"open_steps": 5, "steps": [], "truncated": False, "head_sha": ""}
    m = _model(tmp_path, task("IMPLEMENTING", view={"build": b}))
    assert m["m"]["build"]["total"] is None and m["m"]["build"]["pct"] is None
    assert "total not reported" in m["html"] and "progressbar" not in m["html"]
    # the engine keeps only 12 open rows: done is an upper bound then
    b = {"open_steps": 12, "steps": ["x"] * 12, "truncated": True, "total_steps": 30}
    m = _model(tmp_path, task("CHANGES_REQUIRED", view={"build": b}))
    assert m["m"]["build"]["atMost"] is True and "≤ 18" in m["html"] and "12+ open" in m["html"]
    # BUILD with no committed plan record yet: said so, no bar
    m = _model(tmp_path, task("IMPLEMENTING", view={}))
    assert m["m"]["build"]["known"] is False and "first commit" in m["html"]


def test_phase_follows_state_and_waiting_or_stopped_tasks_say_what_they_need(tmp_path):
    phase = lambda st, **d: _model(tmp_path, task(st, **d))["m"]       # noqa: E731
    assert phase("READY")["phase"] == "PLAN"
    assert phase("VERIFYING")["phase"] == "VERIFY" and phase("REVIEWING")["phase"] == "REVIEW"
    assert phase("CLOSING")["phase"] == "CLOSE"
    done = phase("COMPLETE")
    assert all(p["status"] == "done" for p in done["phases"]) and done["mode"] == "complete"
    past = phase("REVIEWING")
    assert past["build"] == {"known": True, "complete": True}
    # PAUSED shows where it was; BLOCKED during closeout stays on CLOSE
    assert phase("PAUSED", resume_state="VERIFYING", pause_reason="upgrade")["phase"] == "VERIFY"
    blk = phase("BLOCKED", phase="closeout", blocker="gate failed " + HOSTILE)
    assert blk["phase"] == "CLOSE" and blk["mode"] == "stopped"
    assert blk["needs"][0]["act"] == "command" and blk["blocker"].startswith("gate failed")
    out = _model(tmp_path, task("BLOCKED", phase="closeout", blocker=HOSTILE))
    assert "sp-blocker" in out["html"] and "<img" not in out["html"]
    # a pending question for this task (not another task's) and an open decision
    t = task("AWAITING_HUMAN", phase="implement", awaiting="Should I?",
             view={"decisions": {"open": [{"decision_id": "d1"}, {"decision_id": "d2", "answer_action": {}}]}})
    pend = [{"task_id": "UI-02", "project_id": "au", "kind": "question", "status": "open"},
            {"task_id": "UI-09", "project_id": "au", "kind": "question", "status": "open"},
            {"task_id": "UI-02", "project_id": "au", "kind": "permission", "status": "answered"}]
    m = _model(tmp_path, t, {"pending": pend})["m"]
    assert [n["text"] for n in m["needs"]] == ["Answer 1 question", "Make 1 decision"]
    assert m["mode"] == "waiting" and m["phase"] == "BUILD"
    # no pending request recorded: the awaiting text itself is shown
    m = _model(tmp_path, task("AWAITING_HUMAN", awaiting="Pick " + HOSTILE))
    assert m["m"]["needs"][0]["kind"] == "awaiting" and "&lt;img" in m["html"]


def test_compact_header_keeps_the_phase_line_and_needs(tmp_path):
    out = _model(tmp_path, task("AWAITING_HUMAN", awaiting="?"), compact=True)
    assert 'class="pour"' in out["html"] and "Needs you" in out["html"]
    assert "Coordinator</div>" not in out["html"] and 'aria-expanded="false"' in out["html"]
    assert _model(tmp_path, None)["m"] is None
