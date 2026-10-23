"""[lifecycle] engine wiring: the off switch, readiness, decisions through the
control channel, pause/cancel/restart around recovery and continuations,
nested budgets, crash windows and migrating an existing coordinator DB.

Fakes only (isolated git repos, ToolClient, FakeFoundry); see
test_lifecycle_e2e.py for the shared adapter and client."""

from __future__ import annotations

import time

import pytest

from foundry_coordinator import states as S
from foundry_coordinator.control import ControlChannel
from tests.coordinator.helpers import (FakeFoundry, coordinator, git, make_repo, run_until,
                                       write_profile)
from tests.coordinator.test_lifecycle_e2e import (T1, T2, LifecycleAdapter, ToolClient,
                                                  green_t1, lifecycle_extra, report)
from tests.coordinator.test_scenarios_lifecycle import OPUS, Adviser

ADAPTER_ONLY = '''
[adapter]
module = "tests.coordinator.test_lifecycle_e2e:LifecycleAdapter"
'''
LC_TABLES = ("ev_records", "obligations", "decisions", "budget_ledger", "progress_marks")


def _state(c, tid="T1"):
    return c.store.task("game", tid)["state"]


def _tables(c):
    return {r["name"] for r in c.store.q("SELECT name FROM sqlite_master WHERE type='table'")}


def test_committed_partial_build_continues_without_spending_repair_attempt(tmp_path,
                                                                          monkeypatch):
    """An idle worker with real, in-scope progress does not submit an open BUILD."""
    def pending(_self, _spec, revision, ws):
        return ["- [ ] finish the UI"] if ws.show_at(revision, "src/ui.txt") is None else []

    monkeypatch.setattr(LifecycleAdapter, "pending_build_steps", pending, raising=False)
    turns = []

    def script(ctx):
        turns.append(ctx["session_id"])
        if len(turns) == 1:
            return {"writes": {"src/core.txt": "OK"}, "commit": "T1: partial core",
                    "final": report(summary="more BUILD remains")}
        return green_t1(ctx)

    c, _, _, _, _ = _game(tmp_path, script)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.CHANGES_REQUIRED, limit=30)
    # An older worker could store the progress marker as a repair finding just
    # before a safe coordinator reload. The new worker must treat it as BUILD.
    c.store.merge_data("game", "T1", findings=[
        "Continue the committed plan's open BUILD steps; "
        "final gate and review remain required."])
    assert run_until(c, lambda: _state(c) == S.COMPLETE, limit=60), (
        _state(c), turns, [(h["from_state"], h["to_state"]) for h in
                           c.store.history("game", "T1")[-5:]],
        [dict(e) for e in c.store.q("SELECT kind, detail FROM events ORDER BY id DESC LIMIT 3")])
    history = c.store.history("game", "T1")
    progress = [h for h in history if h["evidence"].get("build_progress")]
    assert len(progress) == 1 and progress[0]["to_state"] == S.CHANGES_REQUIRED
    assert progress[0]["from_state"] == S.IMPLEMENTING
    assert len(turns) == 2 and len(set(turns)) == 2
    limits = c.obligations.effective_limits(c.projects["game"], c.store.task("game", "T1"))
    assert limits["ordinary"]["used"] == 1 and limits["sessions"]["used"] == 2


def test_unreported_open_build_gets_one_same_session_continuation(tmp_path, monkeypatch):
    """Unfinished prose does not submit an open checklist as a final gate."""
    monkeypatch.setattr(LifecycleAdapter, "pending_build_steps",
                        lambda self, spec, revision, ws: ["finish UI"]
                        if ws.show_at(revision, "src/ui.txt") is None else [],
                        raising=False)
    turns = []

    def script(ctx):
        turns.append((ctx["session_id"], ctx["prompt"]))
        if len(turns) == 1:
            return {"writes": {"src/core.txt": "OK"},
                    "final": "The sprint is unfinished. Continue the UI and evidence."}
        return green_t1(ctx)

    c, _, _, _, _ = _game(tmp_path, script)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE, limit=70)
    assert len(turns) == 2 and turns[0][0] == turns[1][0]
    assert "without a structured coordinator-report" in turns[1][1]
    events = c.store.q("SELECT kind FROM events WHERE project_id='game' AND task_id='T1'")
    assert any(e["kind"] == "open_build_continued" for e in events)


def test_fresh_unblock_withdraws_old_recovery_wait_and_starts_new_session(tmp_path,
                                                                         monkeypatch):
    """A reset after partial BUILD progress must reach the new worker, not reblock."""
    monkeypatch.setattr(LifecycleAdapter, "pending_build_steps",
                        lambda self, spec, revision, ws: ["finish current BUILD work"],
                        raising=False)
    c, client, _, _, _ = _game(tmp_path, lambda ctx: {
        "writes": {"src/core.txt": "OK"}, "commit": "T1: partial core",
        "final": report(summary="more BUILD remains")})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.CHANGES_REQUIRED, limit=30)
    t = c.store.task("game", "T1")
    did = c.obligations.open_decision("game", "T1", "recovery_escalation",
                                       "old repeat findings", blocks=["T1"])
    c._lc_wait(c.projects["game"], t, [did], "old repeat findings", S.READY)
    assert _state(c) == S.BLOCKED
    c.unblock("game", "T1", "fresh session from committed BUILD work", reset_attempts=True)
    t = c.store.task("game", "T1")
    assert t["state"] == S.READY and t["data"]["free_retry"] is True
    assert c.obligations.decision(did)["status"] == "withdrawn"
    c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.IMPLEMENTING and t["session_id"]


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


def _game(tmp_path, script, *, lifecycle=True, extra="", tasks=(T1,), continuations=2,
          foundry=None, clock=None, store_path=None):
    root, _ = make_repo(tmp_path, "game", list(tasks))
    body = lifecycle_extra(continuations=continuations, extra=extra) if lifecycle \
        else ADAPTER_ONLY + extra
    prof = write_profile(tmp_path, "game", root, extra=body)
    client = script if isinstance(script, ToolClient) else ToolClient(script, project_id="game")
    ff = foundry or FakeFoundry()
    c = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": ff}, clock=clock,
                    store_path=store_path)
    return c, client, ff, root, prof


def _missing_mobile_then_capture(turns):
    def script(ctx):
        turns.append(ctx["session_id"])
        if ctx["task_id"] == "T2":
            return green_t1(ctx)
        if len(turns) == 1:
            return {"writes": {"src/ui.txt": "OK", "src/core.txt": "OK",
                               "docs/progress/T1_LOG.md": "OK"}, "commit": "T1: work",
                    "tools": [("core.tests", 1, 0), ("ui.tests", 1, 0),
                              ("ui.shot_desktop", 1, 0)], "final": report()}
        return {"tools": [("ui.shot_mobile", 1, 0)], "final": report(summary="captured")}
    return script


# -- 1. the off switch -------------------------------------------------------------------

def test_disabled_lifecycle_is_the_old_engine(tmp_path):
    """Same adapter (it declares required checks the session never ran), lifecycle
    off: no gate, no ledger tables, no session head data, the v0.121 path."""
    def script(ctx):
        return {"writes": {"src/ui.txt": "OK", "src/core.txt": "OK",
                           "docs/progress/T1_LOG.md": "OK"}, "commit": "T1: work",
                "final": report()}
    c, client, ff, _, _ = _game(tmp_path, script, lifecycle=False)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    path = [h["to_state"] for h in c.store.history("game", "T1")]
    assert path == [S.QUEUED, S.READY, S.IMPLEMENTING, S.VERIFYING, S.REVIEWING, S.APPROVED,
                    S.CLOSING, S.COMPLETE]
    assert not (set(LC_TABLES) & _tables(c))
    assert all(s["data"] == {} or "head" not in s["data"] for s in c.store.sessions("game"))
    assert c._ledger is None and c._obligations is None and c._recovery is None
    assert len(ff.chats) == 1


def test_lifecycle_table_is_validated(tmp_path):
    from foundry_coordinator.profile import ProfileError, load
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root, extra="[lifecycle]\nenabled = \"yes\"\n")
    with pytest.raises(ProfileError, match="lifecycle.enabled"):
        load(str(prof))
    prof = write_profile(tmp_path, "game", root, extra="[lifecycle]\nwhatever = true\n")
    with pytest.raises(ProfileError, match="unknown"):
        load(str(prof))
    p = load(str(write_profile(tmp_path, "game", root)))
    assert p.lifecycle["enabled"] is False and not p.lifecycle_on()


# -- 2. readiness ------------------------------------------------------------------------

@pytest.fixture()
def readiness(monkeypatch):
    def set_items(items):
        monkeypatch.setattr(LifecycleAdapter, "readiness_items", list(items))
    return set_items


def test_readiness_block_consumes_no_attempt_and_rechecks_on_a_timer(tmp_path, readiness):
    clock = Clock()
    readiness([{"name": "studio.live_sync", "ok": False, "cls": "runtime_sync",
                "detail": "Studio not connected", "blocking": True, "remedy": "open Studio"},
               {"name": "lune", "ok": None, "cls": "tooling", "detail": "unknown",
                "blocking": True},
               {"name": "optional", "ok": False, "cls": "tooling", "detail": "x",
                "blocking": False}])
    c, client, _, _, _ = _game(tmp_path, green_t1, clock=clock)
    c.approve("game", "T1", [], "victor")
    c.tick()
    c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.BLOCKED and t["attempt"] == 0 and t["sessions"] == 0
    assert "runtime_sync" in t["blocker"] and "studio.live_sync" in t["blocker"]
    assert t["data"]["blocker_class"] == "runtime_sync"
    assert t["data"]["readiness"][0]["name"] == "studio.live_sync"
    ev = c.store.history("game", "T1")[-1]["evidence"]
    assert ev["blocker_class"] == "runtime_sync" and ev["attempt_consumed"] == "no"
    assert client.sessions == {} and c.store.locks() == []
    lim = c.obligations.effective_limits(c.projects["game"], t)
    assert lim["ordinary"]["used"] == 0 and lim["sessions"]["used"] == 0
    # not time yet: nothing happens (no busy loop)
    for _ in range(3):
        c.tick()
    assert _state(c) == S.BLOCKED and t["data"]["readiness_block"]["checks"] == 1
    # still failing at the retry time: backs off
    clock.now += 61
    c.tick()
    rb = c.store.task("game", "T1")["data"]["readiness_block"]
    assert _state(c) == S.BLOCKED and rb["checks"] == 2 and rb["retry_at"] > clock.now + 100
    # fixed: the next re-check returns the task to READY by itself
    readiness([])
    clock.now += 200
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    t = c.store.task("game", "T1")
    assert t["attempt"] == 1 and "blocker_class" not in t["data"]


def test_scope_readiness_block_waits_for_a_person(tmp_path, readiness):
    clock = Clock()
    readiness([{"name": "plan_at_head", "ok": False, "cls": "scope",
                "detail": "plan not committed", "blocking": True}])
    c, client, _, _, _ = _game(tmp_path, green_t1, clock=clock)
    c.approve("game", "T1", [], "victor")
    c.tick()
    c.tick()
    assert _state(c) == S.BLOCKED
    assert c.store.task("game", "T1")["data"]["readiness_block"]["retry_at"] is None
    readiness([])
    clock.now += 10_000
    c.tick()
    assert _state(c) == S.BLOCKED                         # scope never re-checks itself
    c.unblock("game", "T1", "plan committed")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    assert c.store.task("game", "T1")["attempt"] == 1


# -- 3. decisions through the control channel ------------------------------------------

class Ctl:
    def __init__(self):
        self.reports = []

    def action_status(self, action_id, source_id, status, detail, result=None):
        self.reports.append((action_id, status, detail))


def _action(n, did, **payload):
    return {"action_id": f"a{n}", "kind": "decision_answer", "project_id": "game",
            "task_id": "T1", "actor": payload.pop("actor", "victor"),
            "payload": {"decision_id": did, "authority": "operator", **payload},
            "expected": {}}


def test_decision_answers_only_from_the_operator(tmp_path):
    c, _, _, _, _ = _game(tmp_path, green_t1)
    c.approve("game", "T1", [], "victor")
    P = c.projects["game"]
    did = c.obligations.open_decision("game", "T1", "scope_decision", "Is the gallery in scope?",
                                      options=["yes", "no"], blocks=["T1"])
    c._lc_wait(P, c.store.task("game", "T1"), [did], "scope question", S.READY)
    ctl = Ctl()
    ch = ControlChannel(c, foundry=ctl, source_id="coord:test")
    beat = ch._beat()
    assert beat["contract"] == "foundry.cc.v1" and "decision_answer" in beat["features"]
    assert "decision_answer" in beat["source"]["capabilities"]["actions"]
    ch.handle(_action(1, did, answer="yes", authority="delegated_policy"))
    ch.handle(_action(2, did, answer="yes", actor="model-qwen"))
    ch.handle(_action(3, did, answer="yes", question_hash="0" * 24))
    ch.handle(_action(4, "dec-nope", answer="yes"))
    assert [r[1] for r in ctl.reports] == ["rejected"] * 4
    assert "model" in ctl.reports[1][2] and "question changed" in ctl.reports[2][2]
    assert c.obligations.decision(did)["status"] == "open"
    # a refused answer leaves the task waiting
    c.tick()
    assert _state(c) == S.BLOCKED
    import hashlib
    import json
    qh = hashlib.sha256(json.dumps("Is the gallery in scope?", sort_keys=True).encode()
                        ).hexdigest()[:24]
    ch.handle(_action(5, did, option="yes", question_hash=qh))
    assert ctl.reports[-1][1] == "applied"
    d = c.obligations.decision(did)
    assert d["status"] == "answered" and d["authority"] == "operator" and d["answer"] == "yes"
    # the same action again reports the stored outcome, never answers twice
    ch.handle(_action(5, did, option="yes", question_hash=qh))
    assert ctl.reports[-1][1] == "applied"
    # answered -> the engine resumes on its own; the answer rides in every prompt
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    sess = [s for s in c.store.sessions("game", "T1") if s["role"] == "implementer"]
    prompts = [p for s in sess for p in c.projects["game"].client.sessions[s["session_id"]]
               ["prompts"]]
    assert prompts and all("## Authorized decisions" in p and did in p for p in prompts)


def test_decision_answer_on_a_disabled_project_is_rejected(tmp_path):
    c, _, _, _, _ = _game(tmp_path, green_t1, lifecycle=False)
    c.approve("game", "T1", [], "victor")
    ctl = Ctl()
    ControlChannel(c, foundry=ctl).handle(_action(1, "dec-x", answer="yes"))
    assert ctl.reports[-1][1] == "rejected"
    assert "decisions" not in _tables(c)


# -- 4. pause / cancel / restart around recovery and continuations ----------------------

def test_pause_and_resume_during_a_continuation(tmp_path):
    turns = []
    c, client, ff, _, _ = _game(tmp_path, _missing_mobile_then_capture(turns))
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: any("continuation" in h["evidence"]
                                    for h in c.store.history("game", "T1")))
    assert _state(c) == S.IMPLEMENTING and ff.chats == []
    c.pause("game", "T1", "operator check")
    assert _state(c) == S.PAUSED
    c.resume("game", "T1")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    sid = turns[0]
    asked = [p for p in client.sessions[sid]["prompts"] if "Missing before review" in p]
    assert len(asked) == 1 and len(set(turns)) == 1
    lim = c.obligations.effective_limits(c.projects["game"], c.store.task("game", "T1"))
    assert lim["missing_evidence_continuations"]["used"] == 1 and lim["ordinary"]["used"] == 1


def test_cancel_during_a_continuation_releases_everything(tmp_path):
    turns = []
    c, *_ = _game(tmp_path, _missing_mobile_then_capture(turns))
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: any("continuation" in h["evidence"]
                                    for h in c.store.history("game", "T1")))
    c.cancel("game", "T1", "stop")
    assert _state(c) == S.CANCELLED and c.store.locks() == []
    for _ in range(3):
        c.tick()
    assert _state(c) == S.CANCELLED


def test_restart_between_continuation_prompt_and_transition(tmp_path):
    turns = []
    c, client, ff, root, prof = _game(tmp_path, _missing_mobile_then_capture(turns))
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.VERIFYING)
    real = c.store.transition

    def crash(pid, tid, to, evidence, *a, **kw):
        if evidence.get("continuation"):
            raise RuntimeError("killed after the prompt was sent")
        return real(pid, tid, to, evidence, *a, **kw)
    c.store.transition = crash
    c.tick()
    assert _state(c) == S.VERIFYING
    # a fresh engine on the same store (restart): the journaled prompt is not resent
    c2 = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": ff})
    assert run_until(c2, lambda: _state(c2) == S.COMPLETE)
    asked = [p for p in client.sessions[turns[0]]["prompts"] if "Missing before review" in p]
    assert len(asked) == 1
    lim = c2.obligations.effective_limits(c2.projects["game"], c2.store.task("game", "T1"))
    assert lim["missing_evidence_continuations"]["used"] == 1
    assert any(e["kind"] == "helper_delivery_uncertain" or e["kind"] ==
               "same_session_continuation" for e in c2.store.q("SELECT kind FROM events"))


def test_pause_and_restart_during_recovery_never_duplicates_evidence(tmp_path):
    c, client, ff, root, prof = _game(tmp_path, green_t1)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.VERIFYING)
    c.pause("game", "T1", "hold before verifying")
    assert c.store.task("game", "T1")["resume_state"] == S.VERIFYING
    c.ledger.recover(c.projects["game"], c.store.task("game", "T1"))
    n = len(c.ledger.records("game", "T1"))
    assert n == 4
    c2 = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": ff})
    c2.resume("game", "T1")
    assert run_until(c2, lambda: _state(c2) == S.COMPLETE)
    transcript = [r for r in c2.ledger.records("game", "T1") if r["source"] == "transcript"]
    assert len(transcript) == n                    # re-scans after pause/restart add nothing


def test_legacy_429_escalation_reopens_for_real_adviser_call(tmp_path):
    c, _, _, _, _ = _game(tmp_path, green_t1)
    c.approve("game", "T1", [], "victor")
    did = c.obligations.open_decision("game", "T1", "recovery_escalation",
                                      "repeat findings", evidence={"adviser":
                                      "adviser:unavailable"})
    t = c.store.task("game", "T1")
    c.store.update_task("game", "T1", state=S.BLOCKED, data={**t["data"],
        "retry_guidance": {"status": "skipped", "reason": "anthropic usage HTTP 429"},
        "decision_wait": {"ids": [did], "resume": S.READY}})
    c._lc_blocked(c.projects["game"], c.store.task("game", "T1"))
    t = c.store.task("game", "T1")
    assert t["state"] == S.READY
    assert t["data"]["retry_guidance"]["status"] == "deferred"
    assert c.obligations.decision(did)["status"] == "withdrawn"


# -- 5. nested retry limits ------------------------------------------------------------

def test_nested_limits_guided_allowance_and_ledger_reset(tmp_path):
    fixed = {"on": False}

    def script(ctx):
        if fixed["on"]:
            return green_t1(ctx)
        return {"writes": {"src/ui.txt": f"BAD {ctx['attempt']}", "src/core.txt": "OK",
                           "docs/progress/T1_LOG.md": "OK"},
                "commit": f"T1: try {ctx['attempt']}", "final": report()}
    ff = Adviser()
    c, client, _, _, _ = _game(tmp_path, script, foundry=ff, extra=f'''
[retry_guidance]
enabled = true
persona = "{OPUS}"
extra_attempts = 1
''')
    P = c.projects["game"]
    P.profile.limits["max_attempts"] = 1
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.FAILED, limit=40)
    t = c.store.task("game", "T1")
    lim = c.obligations.effective_limits(P, t)
    assert (lim["ordinary"]["used"], lim["ordinary"]["max"]) == (1, 1)
    assert (lim["guided_extra"]["used"], lim["guided_extra"]["max"]) == (1, 1)
    assert lim["sessions"]["used"] == 2 and ff.guidance_calls == [OPUS]
    assert "budget ledger" in c.store.history("game", "T1")[-1]["evidence"]["reason"]
    # an operator reset is an authorized ledger row; the raw count is kept
    c.unblock("game", "T1", "fresh budget", reset_attempts=True)
    rows = c.obligations.ledger("game", "T1")
    assert any(r["counter"] == "authorized:attempt_ordinary" and r["delta"] == 1 for r in rows)
    lim = c.obligations.effective_limits(P, c.store.task("game", "T1"))
    assert lim["ordinary"]["used"] == 1 and lim["ordinary"]["remaining"] == 1
    # the same finding repeated twice with an unchanged strategy: no third blind repair
    c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.BLOCKED and t["data"]["decision_wait"]["ids"]
    did = t["data"]["decision_wait"]["ids"][0]
    assert c.obligations.decision(did)["kind"] == "recovery_escalation"
    assert ff.guidance_calls == [OPUS]                  # consulted at most once
    fixed["on"] = True
    c.decision_answer("game", "T1", did, "Write OK into src/ui.txt, then rerun the checks",
                      actor="victor", channel="control")
    assert run_until(c, lambda: _state(c) == S.COMPLETE, limit=40)
    lim = c.obligations.effective_limits(P, c.store.task("game", "T1"))
    assert lim["ordinary"]["used"] == 2 and lim["attempts"]["used"] == 3


def test_operator_stop_answer_keeps_the_task_blocked_without_a_session(tmp_path):
    def script(ctx):
        return {"writes": {"src/ui.txt": f"BAD {ctx['attempt']}", "src/core.txt": "OK",
                           "docs/progress/T1_LOG.md": "OK"},
                "commit": f"T1: try {ctx['attempt']}", "final": report()}
    ff = Adviser()
    c, client, _, _, _ = _game(tmp_path, script, foundry=ff, extra=f'''
[retry_guidance]
enabled = true
persona = "{OPUS}"
extra_attempts = 1
''')
    P = c.projects["game"]
    P.profile.limits["max_attempts"] = 1
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.FAILED, limit=40)
    c.unblock("game", "T1", "fresh budget", reset_attempts=True)
    c.tick()
    t = c.store.task("game", "T1")
    did = t["data"]["decision_wait"]["ids"][0]
    sessions = len(c.store.sessions("game", "T1"))
    c.decision_answer("game", "T1", did, "stop", actor="victor", channel="control")
    for _ in range(4):
        c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.BLOCKED and "stopped by operator" in (t["blocker"] or "")
    assert len(c.store.sessions("game", "T1")) == sessions        # no new session


# -- 6. crash windows ------------------------------------------------------------------

def _crash_once(c, pred):
    real = c.store.transition
    hit = {"n": 0}

    def wrapped(pid, tid, to, evidence, *a, **kw):
        if not hit["n"] and pred(tid, to, evidence):
            hit["n"] += 1
            raise RuntimeError("process killed")
        return real(pid, tid, to, evidence, *a, **kw)
    c.store.transition = wrapped
    return hit


def test_crash_after_commit_and_after_review(tmp_path):
    c, client, ff, _, _ = _game(tmp_path, green_t1)
    after_commit = _crash_once(c, lambda tid, to, ev: to == S.VERIFYING)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: after_commit["n"] == 1)
    assert _state(c) == S.IMPLEMENTING
    after_review = _crash_once(c, lambda tid, to, ev: to == S.APPROVED)
    assert run_until(c, lambda: after_review["n"] == 1)
    assert _state(c) == S.REVIEWING and len(c.store.reviews("game", "T1")) == 1
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    t = c.store.task("game", "T1")
    lim = c.obligations.effective_limits(c.projects["game"], t)
    assert t["attempt"] == 1 and lim["ordinary"]["used"] == 1 and lim["sessions"]["used"] == 1
    assert len(ff.chats) == 1 and lim["review_calls"]["used"] == 1
    marks = c.store.q("SELECT * FROM progress_marks WHERE task_id='T1'")
    assert len(marks) == 1


def test_crash_before_advancement(tmp_path):
    c, client, ff, root, prof = _game(tmp_path, green_t1, tasks=(T1, T2))
    c.approve("game", "T1", [], "victor")
    c.approve("game", "T2", ["T1"], "victor")
    blocked = _crash_once(c, lambda tid, to, ev: tid == "T2" and to == S.READY)
    assert run_until(c, lambda: blocked["n"] == 1, limit=40)
    assert _state(c, "T1") == S.COMPLETE and _state(c, "T2") == S.QUEUED
    c2 = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": ff})
    assert run_until(c2, lambda: _state(c2, "T2") == S.COMPLETE, limit=40)
    closeouts = c2.store.q("SELECT DISTINCT set_id FROM verifications WHERE task_id='T1' AND "
                           "phase='closeout'")
    assert len(closeouts) == 1


# -- 7. migrating an existing coordinator DB --------------------------------------------

def test_migration_from_an_existing_coordinator_db(tmp_path):
    first = {"n": 0}

    def script(ctx):
        first["n"] += 1
        if first["n"] == 1:      # v0.121 run: fails its project check
            return {"writes": {"src/ui.txt": "BAD", "src/core.txt": "OK",
                               "docs/progress/T1_LOG.md": "OK"}, "commit": "T1: try",
                    "final": report()}
        return green_t1(ctx)
    store_path = tmp_path / "state" / "c.sqlite"
    c, client, ff, root, _ = _game(tmp_path, script, lifecycle=False, store_path=store_path)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.CHANGES_REQUIRED)
    assert not (set(LC_TABLES) & _tables(c))
    # the operator upgrades the profile; the same DB is opened by the new engine
    prof = write_profile(tmp_path, "game", root, extra=lifecycle_extra())
    c2 = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": ff},
                     store_path=store_path)
    assert run_until(c2, lambda: _state(c2) == S.COMPLETE, limit=40)
    assert set(LC_TABLES) <= _tables(c2)
    assert c2.store.meta("schema:evidence") and c2.store.meta("schema:obligations")
    rows = c2.obligations.ledger("game", "T1")
    assert any(r["reason"] == "migrated:attempt" and r["delta"] == 1 for r in rows)
    lim = c2.obligations.effective_limits(c2.projects["game"], c2.store.task("game", "T1"))
    assert lim["ordinary"]["used"] == 2 and lim["sessions"]["used"] == 2
    # pre-existing history is intact
    path = [h["to_state"] for h in c2.store.history("game", "T1")]
    assert path[:5] == [S.QUEUED, S.READY, S.IMPLEMENTING, S.VERIFYING, S.CHANGES_REQUIRED]
    # opening it again is a no-op migration
    c3 = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": ff},
                     store_path=store_path)
    assert c3.obligations is not None and c3.ledger is not None
    assert len(c3.obligations.ledger("game", "T1")) == len(rows)
    assert git(root, "status", "--porcelain") == ""


# -- 8. review routing -------------------------------------------------------------------

def test_evidence_incomplete_review_is_reviewed_again_without_an_attempt(tmp_path):
    from tests.coordinator.helpers import review_json
    verdicts = iter(["CHANGES_REQUIRED", "APPROVED"])

    def reviewer(packet):
        v = next(verdicts)
        return review_json(packet, v, missing=["ui.shot_mobile capture is not in the packet"]
                           if v != "APPROVED" else [])
    c, client, ff, _, _ = _game(tmp_path, green_t1, foundry=FakeFoundry(reviewer))
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE, limit=40)
    t = c.store.task("game", "T1")
    lim = c.obligations.effective_limits(c.projects["game"], t)
    assert len(ff.chats) == 2 and lim["review_calls"]["used"] == 2
    assert t["attempt"] == 1 and lim["ordinary"]["used"] == 1 and lim["sessions"]["used"] == 1
    kinds = [e["detail"] for e in c.store.q("SELECT detail FROM events WHERE kind='review_routed'")]
    assert any("evidence_incomplete" in k for k in kinds)


def test_access_blocked_review_opens_a_decision_and_starts_no_session(tmp_path):
    from tests.coordinator.helpers import review_json

    def reviewer(packet):
        return review_json(packet, "CHANGES_REQUIRED", findings=[
            {"severity": "major", "file": "", "line": None,
             "message": "could not open the attached screenshot: access denied"}])
    c, client, ff, _, _ = _game(tmp_path, green_t1, foundry=FakeFoundry(reviewer))
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.BLOCKED, limit=40)
    t = c.store.task("game", "T1")
    did, = t["data"]["decision_wait"]["ids"]
    assert c.obligations.decision(did)["kind"] == "review_access"
    n_sessions = len(client.sessions)
    for _ in range(3):
        c.tick()
    assert len(client.sessions) == n_sessions == 1 and t["attempt"] == 1
    # answered: the same head is verified and reviewed again (no new session)
    ff.reviewer = lambda packet: review_json(packet)
    c.decision_answer("game", "T1", did, "access fixed: review again", actor="victor",
                      channel="control")
    assert run_until(c, lambda: _state(c) == S.COMPLETE, limit=40)
    assert len(client.sessions) == 1 and len(ff.chats) == 2


# -- 9. screening a model's report ------------------------------------------------------

def test_report_moving_build_captures_to_closeout_opens_a_decision_never_a_move(tmp_path):
    def script(ctx):
        out = green_t1(ctx)
        out["tools"] = out["tools"][:3]                     # no mobile capture
        out["final"] = report(status="NOT DONE", summary="core done",
                              next="defer the remaining ui.shot_mobile capture to the closeout session")
        return out
    c, client, ff, _, _ = _game(tmp_path, script)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.BLOCKED, limit=30)
    t = c.store.task("game", "T1")
    did = t["data"]["decision_wait"]["ids"][0]
    d = c.obligations.decision(did)
    assert d["kind"] == "obligation_conflict" and "refused" in d["question"]
    assert all(o["phase"] == "BUILD" for o in c.obligations.all("game", "T1")
               if o["obligation_id"].startswith("check:ui.shot"))
    assert ff.chats == []                                    # never reviewed


# -- 10. v0.122 review defects (regressions) -------------------------------------------

def _busy_writers(c):
    for i in range(c.max_writers):
        assert c.store.acquire([f"writer:{i}"], "other", "X") is None


def _free_writers(c):
    c.store.release("other", "X")


def _missing_mobile(ctx):
    return {"writes": {"src/ui.txt": "OK", "src/core.txt": "OK",
                       "docs/progress/T1_LOG.md": "OK"}, "commit": "T1: work",
            "tools": [("core.tests", 1, 0), ("ui.tests", 1, 0), ("ui.shot_desktop", 1, 0)],
            "final": report()}


def _qh(text):
    import hashlib
    import json
    return hashlib.sha256(json.dumps(text, sort_keys=True).encode()).hexdigest()[:24]


def _writer_locks(c, tid="T1"):
    return [lk["resource"] for lk in c.store.locks()
            if lk["resource"].startswith("writer:") and lk["task_id"] == tid]


def _access_denied(packet):
    from tests.coordinator.helpers import review_json
    return review_json(packet, "CHANGES_REQUIRED", findings=[
        {"severity": "major", "file": "", "line": None,
         "message": "could not open the attached screenshot: access denied"}])


def test_start_failure_on_the_last_ordinary_attempt_consumes_nothing(tmp_path):
    c, client, ff, _, _ = _game(tmp_path, green_t1)
    c.approve("game", "T1", [], "victor")
    c.tick()                                                  # QUEUED -> READY
    c.obligations.charge("game", "T1", "attempt_ordinary", 2, "prior", "engine")
    orig, calls = client.start_session, {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("kilo down")
        return orig(**kw)
    client.start_session = flaky
    c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.READY and "could not start" in t["next_action"]
    assert [r["reason"] for r in c.obligations.ledger("game", "T1")] == ["prior"]
    assert _writer_locks(c) == []
    c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.IMPLEMENTING
    rows = [(r["counter"], r["reason"]) for r in c.obligations.ledger("game", "T1")]
    assert rows == [("attempt_ordinary", "prior"), ("attempt_ordinary", f"attempt:{t['run_id']}"),
                    ("sessions", f"session:{t['run_id']}")]
    assert run_until(c, lambda: _state(c) == S.COMPLETE)


def test_a_run_charged_before_its_start_failed_is_never_charged_again(tmp_path):
    """A DB from before the fix: the run was charged, then its start failed. The
    resumed start reuses the run id and charges nothing, under any counter."""
    c, client, ff, _, _ = _game(tmp_path, green_t1)
    c.approve("game", "T1", [], "victor")
    c.tick()
    run = "T1-a1-legacy"
    c.store.merge_data("game", "T1", pending_start={"run_id": run, "attempt": 1})
    c.obligations.charge("game", "T1", "attempt_ordinary", 2, "prior", "engine")
    c.obligations.charge("game", "T1", "attempt_ordinary", 1, f"attempt:{run}", "engine")
    c.obligations.charge("game", "T1", "sessions", 1, f"session:{run}", "engine")
    c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.IMPLEMENTING and t["run_id"] == run
    lim = c.obligations.effective_limits(c.projects["game"], t)
    assert lim["ordinary"]["used"] == 3 and lim["guided_extra"]["used"] == 0
    assert lim["sessions"]["used"] == 1


def test_continuation_to_a_failing_prompt_releases_the_writer_and_falls_back(tmp_path):
    c, client, ff, _, _ = _game(tmp_path, _missing_mobile)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.VERIFYING, limit=20)

    def gone(sid, text, **kw):
        raise RuntimeError("session not found (Kilo restarted)")
    client.prompt = gone
    c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.CHANGES_REQUIRED and _writer_locks(c) == []
    assert c.LC_PENDING not in t["data"]
    assert "missing evidence before review" in " ".join(t["data"]["findings"])
    for _ in range(4):
        c.tick()
    assert _writer_locks(c) == []
    assert c.store.q("SELECT COUNT(*) AS n FROM events WHERE kind='error'")[0]["n"] == 0
    lim = c.obligations.effective_limits(c.projects["game"], c.store.task("game", "T1"))
    assert lim["missing_evidence_continuations"]["used"] == 0


def test_continuation_to_a_session_kilo_lost_falls_back_without_a_prompt(tmp_path):
    c, client, ff, _, _ = _game(tmp_path, _missing_mobile)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.VERIFYING, limit=20)
    sid = c.store.task("game", "T1")["session_id"]
    prompts = list(client.sessions[sid]["prompts"])
    del client.sessions[sid]                                   # Kilo restarted
    c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.CHANGES_REQUIRED and _writer_locks(c) == []
    assert not any("Missing before review" in p for p in prompts)
    assert any("is gone" in str(e["detail"]) for e in c.store.q(
        "SELECT detail FROM events WHERE kind='warning'"))


def test_review_waiting_for_the_writer_pays_for_no_extra_review(tmp_path):
    from tests.coordinator.helpers import review_json

    def reviewer(packet):
        return review_json(packet, "CHANGES_REQUIRED", missing=["need the mobile capture"])
    c, client, ff, _, _ = _game(tmp_path, green_t1, foundry=FakeFoundry(reviewer))
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.REVIEWING, limit=20)
    _busy_writers(c)
    for _ in range(8):
        c.tick()
    t = c.store.task("game", "T1")
    assert len(ff.chats) <= 2, f"{len(ff.chats)} paid reviews while waiting for a writer slot"
    n = len(ff.chats)
    m = t["data"][c.LC_PENDING]
    assert t["state"] == S.REVIEWING and m["from_state"] == S.REVIEWING
    assert m["review_id"] and m["head"] == t["head_sha"] and m["key"].startswith("review:")
    _free_writers(c)
    c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.IMPLEMENTING and c.LC_PENDING not in t["data"]
    assert len(ff.chats) == n
    assert c.store.history("game", "T1")[-1]["evidence"]["continuation"] == m["key"]


def test_verifying_waiting_for_the_writer_is_not_verified_again(tmp_path):
    turns = []
    c, client, ff, _, _ = _game(tmp_path, _missing_mobile_then_capture(turns))
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.VERIFYING, limit=20)
    _busy_writers(c)
    tx = {"n": 0}
    orig = client.transcript

    def counting(sid, *, directory):
        tx["n"] += 1
        return orig(sid, directory=directory)
    client.transcript = counting
    for _ in range(6):
        c.tick()
    sets = c.store.q("SELECT COUNT(DISTINCT set_id) AS n FROM verifications WHERE task_id='T1'")
    assert sets[0]["n"] == 1
    assert c.store.q("SELECT COUNT(*) AS n FROM events WHERE kind='submission_refused'"
                     )[0]["n"] == 1
    assert tx["n"] <= 2 and _state(c) == S.VERIFYING
    assert c.store.task("game", "T1")["data"][c.LC_PENDING]["from_state"] == S.VERIFYING
    _free_writers(c)
    assert run_until(c, lambda: _state(c) == S.COMPLETE, limit=30)
    assert len(turns) == 2 and len(set(turns)) == 1


def test_project_pause_stops_a_continuation_and_its_review(tmp_path):
    turns = []
    c, client, ff, _, _ = _game(tmp_path, _missing_mobile_then_capture(turns))
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.VERIFYING, limit=20)
    c.store.set_project_paused("game", True, "stop all work")
    for _ in range(4):
        c.tick()
    assert len(turns) == 1, "a new prompt was sent to the model while the project was paused"
    assert _state(c) == S.VERIFYING and ff.chats == []
    assert "paused" in c.store.task("game", "T1")["next_action"]
    c.store.set_project_paused("game", False, "")
    assert run_until(c, lambda: _state(c) == S.COMPLETE, limit=30)
    assert len(turns) == 2


def _decision_wait(c, kind, question, options, resume=S.READY):
    P = c.projects["game"]
    did = c.obligations.open_decision("game", "T1", kind, question, options=options,
                                      blocks=["T1"])
    c._lc_wait(P, c.store.task("game", "T1"), [did], f"{kind} test", resume)
    return did


@pytest.mark.parametrize("how", ["option", "typed"])
def test_no_progress_stop_answer_stops_and_an_unblock_resumes(tmp_path, how):
    c, client, ff, _, _ = _game(tmp_path, green_t1)
    c.approve("game", "T1", [], "victor")
    c.tick()
    opts = ["answer with a different strategy {strategy: ...}", "authorize more budget",
            "stop the task"]
    did = _decision_wait(c, "no_progress", "3 sessions made no progress", opts)
    if how == "option":
        c.decision_answer("game", "T1", did, "", option="stop the task", actor="victor",
                          channel="control")
    else:
        c.decision_answer("game", "T1", did, "stop the task", actor="victor", channel="control")
    ans = c.obligations.decision(did)["answer"]
    assert ans.get("stop") is True and not ans.get("strategy")
    for _ in range(3):
        c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.BLOCKED and "stopped by operator" in t["blocker"]
    assert t["data"]["lc_stop"]["decision_id"] == did and client.sessions == {}
    c.unblock("game", "T1", "carry on")
    assert c.obligations.decision(did)["status"] == "withdrawn"
    assert run_until(c, lambda: _state(c) == S.COMPLETE)


def test_review_access_stop_answer_stays_blocked_and_pays_for_no_review(tmp_path):
    c, client, ff, _, _ = _game(tmp_path, green_t1, foundry=FakeFoundry(_access_denied))
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.BLOCKED, limit=40)
    did, = c.store.task("game", "T1")["data"]["decision_wait"]["ids"]
    n, hist = len(ff.chats), len(c.store.history("game", "T1"))
    c.decision_answer("game", "T1", did, "", option="stop the task", actor="victor",
                      channel="control")
    for _ in range(4):
        c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.BLOCKED and "stopped by operator" in t["blocker"]
    assert len(ff.chats) == n and len(c.store.history("game", "T1")) == hist


def test_review_access_resume_with_a_moved_head_takes_the_normal_path(tmp_path):
    c, client, ff, root, _ = _game(tmp_path, green_t1, foundry=FakeFoundry(_access_denied))
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.BLOCKED, limit=40)
    did, = c.store.task("game", "T1")["data"]["decision_wait"]["ids"]
    (root / "src" / "core.txt").write_text("OK2", encoding="utf-8")
    git(root, "commit", "-qam", "T1: operator edit")
    c.decision_answer("game", "T1", did, "access fixed: review again", actor="victor",
                      channel="control")
    c.tick()
    h = c.store.history("game", "T1")[-1]
    assert h["to_state"] == S.READY and "moved" in h["evidence"]["resolution"]


def test_a_malformed_budget_answer_reopens_the_decision(tmp_path):
    c, client, ff, _, _ = _game(tmp_path, green_t1)
    c.approve("game", "T1", [], "victor")
    c.tick()
    did = _decision_wait(c, "budget_extension", "Allow more attempts?", ["yes", "no"])
    c.decision_answer("game", "T1", did, '{"counter": "ordinary", "n": "lots"}',
                      actor="victor", channel="control")
    c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.BLOCKED
    old = c.obligations.decision(did)
    new_id, = t["data"]["decision_wait"]["ids"]
    new = c.obligations.decision(new_id)
    assert old["status"] == "superseded" and old["superseded_by"] == new_id
    assert new["status"] == "open" and "refused" in new["question"] and "lots" in new["question"]
    assert any("answer_refused" in str(e["detail"]) for e in c.store.q(
        "SELECT detail FROM events WHERE kind='warning'"))
    c.tick()
    assert _state(c) == S.BLOCKED                         # waits on the re-opened one
    # a LIMITS key ("ordinary") maps to its ledger counter
    c.decision_answer("game", "T1", new_id, '{"counter": "ordinary", "n": 2}',
                      actor="victor", channel="control",
                      question_hash=t["data"]["decision_wait"]["question_hashes"][new_id])
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    rows = c.obligations.ledger("game", "T1")
    assert any(r["counter"] == "authorized:attempt_ordinary" and r["delta"] == 2
               and r["decision_id"] == new_id for r in rows)


def test_a_refused_authorization_reopens_the_decision(tmp_path):
    c, client, ff, _, _ = _game(tmp_path, green_t1)
    c.approve("game", "T1", [], "victor")
    c.tick()
    did = _decision_wait(c, "budget_extension", "Allow more?", ["yes"])
    c.decision_answer("game", "T1", did, '{"counter": "no_such_counter", "n": 1}',
                      actor="victor", channel="control")
    c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.BLOCKED and t["data"]["decision_wait"]["ids"] != [did]
    assert c.obligations.decision(did)["status"] == "superseded"


def test_a_superseded_decision_keeps_the_task_waiting_on_its_successor(tmp_path):
    c, client, ff, _, _ = _game(tmp_path, green_t1)
    c.approve("game", "T1", [], "victor")
    c.tick()
    d1 = _decision_wait(c, "scope_decision", "In scope?", ["yes", "no"])
    d2 = c.obligations.open_decision("game", "T1", "scope_decision", "In scope (v2)?",
                                     options=["yes", "no"], blocks=["T1"], supersedes=d1)
    c.obligations.supersede(d1, d2, "victor", "operator", channel="control")
    for _ in range(3):
        c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.BLOCKED and t["data"]["decision_wait"]["ids"] == [d2]
    c.decision_answer("game", "T1", d2, "yes", actor="victor", channel="control")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)


def test_a_long_question_is_answerable_with_the_control_center_hash(tmp_path):
    c, client, ff, _, _ = _game(tmp_path, green_t1)
    c.approve("game", "T1", [], "victor")
    c.tick()
    q = "Is this in scope? " + "x" * 1900
    did = _decision_wait(c, "scope_decision", q, ["yes", "no"])
    stored = c.obligations.decision(did)["question"]
    assert len(stored) > 1500
    t = c.store.task("game", "T1")
    assert t["data"]["decision_wait"]["question_hashes"][did] == _qh(stored)
    with pytest.raises(ValueError, match="question changed"):
        c.decision_answer("game", "T1", did, "yes", actor="victor", channel="control",
                          question_hash=_qh(stored[:1499]))
    c.decision_answer("game", "T1", did, "yes", actor="victor", channel="control",
                      question_hash=_qh(stored[:1500]))
    assert c.obligations.decision(did)["status"] == "answered"


def test_a_failure_before_the_session_releases_every_lock(tmp_path, monkeypatch):
    c, client, ff, _, _ = _game(tmp_path, green_t1)
    c.approve("game", "T1", [], "victor")
    c.tick()

    def boom(*a, **kw):
        raise RuntimeError("seed failed")
    monkeypatch.setattr(c.obligations, "seed", boom)
    c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.READY and "seed failed" in t["next_action"]
    assert [lk for lk in c.store.locks() if lk["task_id"] == "T1"] == []
    monkeypatch.undo()
    assert run_until(c, lambda: _state(c) == S.COMPLETE)


def test_an_authorized_review_extension_is_honored(tmp_path):
    from tests.coordinator.helpers import review_json
    verdicts = iter(["CHANGES_REQUIRED", "APPROVED"])

    def reviewer(packet):
        v = next(verdicts)
        return review_json(packet, v, missing=["ui.shot_mobile capture is not in the packet"]
                           if v != "APPROVED" else [])
    c, client, ff, _, _ = _game(tmp_path, green_t1, foundry=FakeFoundry(reviewer))
    c.projects["game"].profile.limits["max_review_calls"] = 1
    c.approve("game", "T1", [], "victor")
    c.tick()
    did = c.obligations.open_decision("game", "T1", "budget_extension", "one more review?",
                                      blocks=["T1"])
    c.decision_answer("game", "T1", did, '{"counter": "review_calls", "n": 1}',
                      actor="victor", channel="control")
    c.obligations.authorize("game", "T1", "review_calls", 1, "test", "victor", did)
    assert run_until(c, lambda: _state(c) == S.COMPLETE, limit=40)
    assert len(ff.chats) == 2


def test_the_review_ledger_limit_blocks_without_an_extension(tmp_path):
    from tests.coordinator.helpers import review_json

    def reviewer(packet):
        return review_json(packet, "CHANGES_REQUIRED",
                           missing=["ui.shot_mobile capture is not in the packet"])
    c, client, ff, _, _ = _game(tmp_path, green_t1, foundry=FakeFoundry(reviewer))
    c.projects["game"].profile.limits["max_review_calls"] = 1
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.BLOCKED, limit=40)
    t = c.store.task("game", "T1")
    assert "budget ledger" in t["blocker"] and len(ff.chats) == 1


def test_heartbeat_advertises_lifecycle_only_when_a_project_enables_it(tmp_path):
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    c, *_ = _game(tmp_path / "off", green_t1, lifecycle=False)
    # (project_pause and tracks are always declared: neither depends on [lifecycle])
    feats = ControlChannel(c, foundry=Ctl())._beat()["features"]
    assert "lifecycle" not in feats and "decision_answer" not in feats
    assert "project_pause" in feats and "tracks" in feats
    c2, *_ = _game(tmp_path / "on", green_t1)
    assert "lifecycle" in ControlChannel(c2, foundry=Ctl())._beat()["features"]


def test_unblock_keeps_data_keys_of_a_project_without_lifecycle(tmp_path):
    c, *_ = _game(tmp_path, green_t1, lifecycle=False)
    c.approve("game", "T1", [], "victor")
    P = c.projects["game"]
    c.store.merge_data("game", "T1", readiness=["mine"], blocker_class="ours", question={"x": 1})
    c._block(P, c.store.task("game", "T1"), "operator hold")
    c.unblock("game", "T1", "go")
    d = c.store.task("game", "T1")["data"]
    assert d["readiness"] == ["mine"] and d["blocker_class"] == "ours" and "question" not in d


def test_recover_does_not_refetch_ended_fully_scanned_sessions(tmp_path):
    c, client, ff, _, _ = _game(tmp_path, green_t1)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    P, t = c.projects["game"], c.store.task("game", "T1")
    c.ledger.recover(P, t)
    calls = {"n": 0}
    orig = client.transcript

    def counting(sid, *, directory):
        calls["n"] += 1
        return orig(sid, directory=directory)
    client.transcript = counting
    n = len(c.ledger.records("game", "T1"))
    rep = c.ledger.recover(P, t)
    sessions = c.store.sessions("game", "T1")
    ended = [s for s in sessions if s.get("ended_ts")]
    assert ended and calls["n"] == len(sessions) - len(ended)
    assert len(c.ledger.records("game", "T1")) == n and rep["added"] == 0


# -- decision reevaluate (UI-02, v0.122.5) ------------------------------------------------

def test_cli_decision_reevaluate_keeps_the_audit_and_never_touches_counters(tmp_path, capsys):
    """`decision reevaluate <id>`: dry run changes nothing; a real run withdraws a
    no_progress decision only when progress is now recognized, with an event;
    a recovery escalation without a valid partial BUILD continuation stays open."""
    from foundry_coordinator import cli
    from foundry_coordinator.obligations import DecisionRefused
    store_path = tmp_path / "state" / "coordinator.sqlite"
    c, client, ff, _, prof = _game(tmp_path, green_t1, store_path=store_path)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    P = c.projects["game"]
    opts = ["answer with a different strategy {strategy: ...}", "authorize more budget",
            "stop the task"]
    did = c.obligations.open_decision("game", "T1", "no_progress", "2 sessions no progress",
                                      options=opts, blocks=["T1"])
    other = c.obligations.open_decision("game", "T1", "recovery_escalation", "repeats",
                                        options=opts, blocks=["T1"])
    before = c.store.task("game", "T1")
    unchanged = c.decision_reevaluate(other, "victor", "not partial BUILD")
    assert not unchanged["withdrawn"]
    assert c.obligations.decision(other)["status"] == "open"
    argv = ["--state", str(tmp_path / "state"), "--profiles", str(prof), "decision",
            "reevaluate", did, "--by", "victor", "--reason", "fixed in v0.122.5"]
    assert cli.main(argv + ["--dry-run"], clients={"game": client},
                    foundry={"game": ff}) == 0
    assert '"dry_run": true' in capsys.readouterr().out
    assert c.obligations.decision(did)["status"] == "open"
    assert cli.main(argv, clients={"game": client}, foundry={"game": ff}) == 0
    assert '"withdrawn": true' in capsys.readouterr().out
    d = c.obligations.decision(did)
    assert d["status"] == "withdrawn" and d["question"] == "2 sessions no progress"
    ev = [e for e in c.store.events("game", 20) if e["kind"] == "decision_reevaluated"]
    assert ev[0]["detail"]["by"] == "victor" and ev[0]["detail"]["reason"] == \
        "fixed in v0.122.5"
    after = c.store.task("game", "T1")
    assert (after["attempt"], after["sessions"], after["state"]) == \
        (before["attempt"], before["sessions"], before["state"])
    assert c.obligations.decision(other)["status"] == "open"
    # a withdrawn decision can't be re-evaluated again
    assert cli.main(argv, clients={"game": client}, foundry={"game": ff}) == 2
    assert P.id == "game"
