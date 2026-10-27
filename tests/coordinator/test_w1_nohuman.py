"""Workstream W1 (2026-10-22): no human in the loop unless it is a product decision.

Fakes only (scripted Kilo, fake Foundry, temp git repos). Covers:
  1. an implementer's BLOCKED-with-options report becomes a timed decision
  2. decision defaults: no_progress continues once then parks; a lead bound parks
     the sprint and the queue moves on; recovery escalations and obligation
     conflicts carry a default
  3. the after-close visual approval carries prior approvals, never re-asks
     accepted frames, retries "uncertain" once, and turns missing frames into a
     continuation
  4. the coordinator rewords its own unpushed commits (revert / merge / pushed exempt)
  5. uncommitted write-access lead edits are committed as the lead's
  6. memory delta per entry; an invalid review is re-sent once with the error
  7. profile drift doctor rows, kilo.jsonc snapshot, AU infra checks
  8. auto-heal repairs: stale runner lock, dead gate pid, Rojo restart, Rojo stale
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
import time
from types import SimpleNamespace

import pytest

from foundry_coordinator import autoheal
from foundry_coordinator import commits as CM
from foundry_coordinator import decision_policy as DP
from foundry_coordinator import states as S
from foundry_coordinator import visual_approval as VA
from foundry_coordinator.recovery import Recovery
from tests.coordinator.helpers import (FakeFoundry, coordinator, git, make_repo, review_json,
                                       run_until, write_profile)
from tests.coordinator.test_lead_e2e import (DONE, LEAD, TASK, TID, UI_OVERRIDE, Clock,
                                             E2EClient, E2EFoundry, case, hist, lead_reply,
                                             lead_toml, make_game)
from tests.coordinator.test_revamp_lead import blocked, events

T1 = {"id": "T1", "title": "t", "spec": "Write src/a.txt containing OK.", "scope": ["src/a.txt"]}
LC = "\n[lifecycle]\nenabled = true\n"


def report(status="DONE", **kw):
    return "```coordinator-report\n" + json.dumps({"status": status, **kw}) + "\n```"


def _state(c, tid="T1"):
    return c.store.task("game", tid)["state"]


def _decisions(c, tid, kind=None):
    return [d for d in c.obligations.all_decisions("game", tid)
            if kind is None or d["kind"] == kind]


# ======================================================================= 1. BLOCKED + options

def test_blocked_report_with_options_becomes_a_timed_decision_and_the_sprint_goes_on(tmp_path):
    from tests.coordinator.test_lifecycle_e2e import green_t1
    from tests.coordinator.test_lifecycle_engine import _game as lc_game
    clock = Clock()
    prompts = []

    def agent(ctx):
        prompts.append(ctx["prompt"])
        if len(prompts) == 1:
            return {"final": report("BLOCKED", blocker="the plan does not say which gallery "
                                    "layout to build",
                                    options=["grid layout", "list layout", "ask the designer"])}
        return green_t1(ctx)
    c, client, ff, root, prof = lc_game(tmp_path, agent, clock=clock)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.BLOCKED, limit=12)
    [d] = _decisions(c, "T1", "implementer_blocked")
    assert d["status"] == "open" and d["options"] == ["grid layout", "list layout",
                                                      "ask the designer"]
    assert json.loads(d["recommended"]) == "grid layout" and d["timeout_minutes"] == 30
    assert d["evidence"]["blocker_kind"] == "product"
    t = c.store.task("game", "T1")
    assert t["data"]["decision_wait"]["ids"] == [d["decision_id"]]
    assert "recommended 'grid layout' applies after the timeout" in t["blocker"]
    # nobody answers: the recommended option applies and the sprint continues
    clock.advance(31 * 60)
    assert run_until(c, lambda: _state(c) == S.COMPLETE, limit=40), \
        [(h["from_state"], h["to_state"]) for h in c.store.history("game", "T1")]
    d2 = c.obligations.decision(d["decision_id"])
    assert d2["status"] == "answered" and d2["authority"] == "timeout_default"
    assert len(prompts) == 2
    assert "grid layout" in prompts[1] and "decided by the timeout default" in prompts[1]
    assert not any(h["to_state"] == S.AWAITING_HUMAN for h in c.store.history("game", "T1"))


def test_blocked_report_with_the_models_own_recommendation(tmp_path):
    clock = Clock()

    def agent(ctx):
        return {"final": report("BLOCKED", blocker="x", options=["a", "b"], recommended="b",
                                blocker_kind="tooling")}
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root, extra=LC)
    c = coordinator(tmp_path, [prof], {"game": agent}, clock=clock)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.BLOCKED, limit=12)
    [d] = _decisions(c, "T1", "implementer_blocked")
    assert json.loads(d["recommended"]) == "b" and d["evidence"]["blocker_kind"] == "tooling"


def test_blocked_report_without_options_or_with_an_infra_signature_takes_the_old_path(tmp_path):
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root, extra=LC)
    c = coordinator(tmp_path, [prof], {"game": lambda ctx: {"final": report(
        "BLOCKED", blocker="rojo serve exited with code -1", options=["retry", "stop"])}})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.BLOCKED, limit=12)
    assert not _decisions(c, "T1", "implementer_blocked")
    c2 = coordinator(tmp_path / "b", [write_profile(tmp_path / "b", "game", make_repo(
        tmp_path / "b", "game", [T1])[0], extra=LC)], {"game": lambda ctx: {"final": report(
            "BLOCKED", blocker="no options here")}})
    c2.approve("game", "T1", [], "victor")
    assert run_until(c2, lambda: _state(c2) == S.BLOCKED, limit=12)
    assert not _decisions(c2, "T1", "implementer_blocked")


def test_the_lead_takes_a_tooling_question_but_not_a_product_one(tmp_path):
    """W1.1: with the lead on, an implementer's BLOCKED question goes to a lead case
    only when the report says it is a tooling question; a product choice waits for
    its default (or the operator) without a paid lead call."""
    def primary(kind):
        def script(ctx):
            return {"final": report("BLOCKED", blocker="which?", options=["a", "b"],
                                    blocker_kind=kind)}
        return script
    for kind, wants_case in (("product", False), ("tooling", True)):
        e = make_game(tmp_path / kind, primary(kind), lead=lambda ctx: {"final": "hmm"},
                      extra=LC + lead_toml(), clock=Clock())
        e["c"].approve("game", TID, [], "victor")
        assert run_until(e["c"], lambda: e["c"].store.task("game", TID)["state"] == S.BLOCKED,
                         limit=12)
        for _ in range(4):
            e["c"].tick()
        assert bool(e["c"].lead.cases("game", TID)) is wants_case, kind
        assert bool(e["client"].lead_starts()) is wants_case, kind


# ======================================================================= 2. defaults

def test_no_progress_continues_once_then_parks(tmp_path):
    clock = Clock()
    turns = []

    def agent(ctx):
        turns.append(ctx["prompt"])
        # never commits anything useful: a true stall every session
        return {"writes": {"src/a.txt": f"broken {len(turns)}"}, "commit": f"T1: try {len(turns)}",
                "final": report("NOT DONE", summary="looked again")}
    root, _ = make_repo(tmp_path, "game", [T1], extra_files={"src/a.txt": "start\n"})
    prof = write_profile(tmp_path, "game", root, extra=LC)
    c = coordinator(tmp_path, [prof], {"game": agent}, clock=clock)
    # the stall classifier is Recovery's business (tested there); here it always stalls
    stall = {"sessions": 3, "verified_delta": 0, "repeated_actions": 0,
             "summary_only_sessions": 2, "stalls": 2, "no_progress": True,
             "recommendation": "decision", "last_session_build": {}}
    c._lc_progress = lambda P, t, spec=None: dict(stall)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: bool(_decisions(c, "T1", "no_progress")), limit=20), \
        [(h["from_state"], h["to_state"]) for h in c.store.history("game", "T1")]
    [d] = _decisions(c, "T1", "no_progress")
    assert json.loads(d["recommended"]) == DP.CONTINUE_ONCE and d["timeout_minutes"] == 30
    assert d["options"] == [DP.CONTINUE_ONCE, DP.STRATEGY, DP.MORE_BUDGET, DP.PARK]
    assert _state(c) == S.BLOCKED
    n_before = len(turns)
    clock.advance(31 * 60)                                  # nobody answers: continue once
    assert run_until(c, lambda: len(turns) > n_before, limit=20)
    assert c.obligations.decision(d["decision_id"])["authority"] == "timeout_default"
    assert events({"c": c}, "no_progress_continue_once")
    # the next stall opens the second decision, whose default is to park
    assert run_until(c, lambda: len(_decisions(c, "T1", "no_progress")) == 2, limit=30)
    d2 = [x for x in _decisions(c, "T1", "no_progress") if x["decision_id"] != d["decision_id"]][0]
    assert json.loads(d2["recommended"]) == DP.PARK and "continuation was used" in d2["question"]
    clock.advance(31 * 60)
    for _ in range(4):
        c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.BLOCKED and t["blocker"].startswith("parked")
    assert t["data"]["lc_stop"]["decision_id"] == d2["decision_id"]
    [parked] = events({"c": c}, "sprint_parked")
    assert parked["decision_id"] == d2["decision_id"] and parked["task_id"] == "T1"
    assert len(turns) == n_before + 1                        # exactly one continuation


def test_a_lead_bound_parks_the_sprint_after_the_timeout_and_the_queue_moves_on(tmp_path):
    clock = Clock()
    t2 = {"id": "T2", "title": "b", "spec": "Write src/b.txt containing OK.",
          "scope": ["src/b.txt"]}

    def primary(ctx):
        if ctx["task_id"] == "T2":
            return {"writes": {"src/b.txt": "OK"}, "commit": "T2: work", "final": DONE}
        return blocked("which gallery layout is approved?")
    # (the project check reads every task's files: T1's file must not fail T2's check)
    root, _ = make_repo(tmp_path, "game", [TASK, t2], extra_files={"src/a.txt": "OK start\n"})
    prof = write_profile(tmp_path, "game", root, extra=UI_OVERRIDE + lead_toml(max_diagnoses=1)
                         + "\n[automation]\nlead_report_repairs = 0\n")
    client = E2EClient(primary, lambda ctx: {"final": "I could not tell."})
    c = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": E2EFoundry()},
                    clock=clock, store_path=tmp_path / "state" / "coordinator.sqlite")
    c.approve("game", TID, [], "victor")
    c.approve("game", "T2", [], "victor")
    e = {"c": c, "client": client}
    assert run_until(c, lambda: (case(e) or {}).get("state") == "NEEDS_VICTOR", limit=60)
    k = case(e)
    d = c.obligations.decision(k["decision_id"])
    assert d["kind"] == "lead_escalation" and d["decision_class"] == "decision"
    assert json.loads(d["recommended"]) == DP.PARK and d["timeout_minutes"] == 30
    assert d["options"][0] == DP.PARK
    # the independent sprint runs meanwhile
    assert run_until(c, lambda: _state(c, "T2") == S.COMPLETE, limit=40)
    assert _state(c, TID) == S.BLOCKED
    clock.advance(31 * 60)
    assert run_until(c, lambda: bool(events(e, "sprint_parked")), limit=20)
    t = c.store.task("game", TID)
    assert t["state"] == S.BLOCKED and t["blocker"].startswith("parked")
    assert "timeout default" in t["blocker"]
    assert c.lead.case(k["case_id"])["state"] == "RESOLVED"
    [parked] = events(e, "sprint_parked")
    assert parked["case_id"] == k["case_id"] and parked["lead_calls"] == 1
    assert any("parked BLOCKED" in json.loads(r["detail"])["text"] for r in c.store.q(
        "SELECT detail FROM events WHERE kind='operator_notice'"))


def test_recovery_escalation_and_obligation_conflict_carry_a_default():
    assert Recovery._escalation_answer({"answer": DP.PARK}) == ("stop", DP.PARK)
    assert Recovery._escalation_answer({"answer": {"option": DP.PARK}})[0] == "stop"
    assert DP.is_park(DP.PARK) and DP.is_continue_once(DP.CONTINUE_ONCE)
    assert not DP.is_park(DP.CONTINUE_ONCE)
    P0 = SimpleNamespace(profile=SimpleNamespace(raw={}))
    m = DP.meta(P0, "recovery_escalation", {"recommended": DP.PARK},
                [DP.STRATEGY, DP.MORE_BUDGET, DP.PARK])
    assert json.loads(m["recommended"]) == DP.PARK and m["timeout_minutes"] == 30
    m = DP.meta(P0, "obligation_conflict", {}, ["fix the plan or scope", "operator decision"],
                recommendation="fix the plan or scope")
    assert json.loads(m["recommended"]) == "fix the plan or scope"
    assert DP.match_option(["keep BUILD (default)", "x"], "keep BUILD: captures") == \
        "keep BUILD (default)"
    assert DP.match_option(["layout A", "layout B"], "layout C") is None


def test_open_decision_passes_the_recommendation_to_the_policy(tmp_path):
    from foundry_coordinator.obligations import Obligations
    from foundry_coordinator.store import Store
    P0 = SimpleNamespace(profile=SimpleNamespace(raw={}))
    ob = Obligations(Store(str(tmp_path / "c.sqlite")), clock=lambda: 1000.0,
                     decision_policy=lambda p, t, k, ev, opts, recommendation="":
                     DP.meta(P0, k, ev, opts, recommendation))
    did = ob.open_decision("game", "T1", "obligation_conflict", "evidence outside scope",
                           options=["fix the plan or scope", "operator decision"],
                           recommendation="fix the plan or scope", blocks=["ob1"])
    d = ob.decision(did)
    assert d["deadline_ts"] == 1000.0 + 1800 and json.loads(d["recommended"]) == \
        "fix the plan or scope"
    # an older five-argument policy still works
    ob2 = Obligations(Store(str(tmp_path / "d.sqlite")), clock=lambda: 1.0,
                      decision_policy=lambda p, t, k, ev, opts: {"decision_class": "decision"})
    assert ob2.decision(ob2.open_decision("g", "T", "x", "q", recommendation="r"))["recommended"] \
        is None


# ======================================================================= 3. visual approval

PNG = b"\x89PNG\r\n\x1a\n" + b"frame-bytes"


def _va_env(tmp_path, chat, data=None, approvals=None, frames=("a.png",)):
    import hashlib
    rels = [f"docs/evidence/UI-02/{f}" for f in frames]
    arts = {}
    for rel in rels:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(PNG + rel.encode())
        arts[rel] = hashlib.sha256(PNG + rel.encode()).hexdigest()
    profile = SimpleNamespace(workspace=str(tmp_path), in_workspace=lambda p: p in arts,
                              raw={"approval": {"visual_model": True}},
                              persona=lambda role, task: "vision",
                              fast_lane={"max_vision_rounds": 1})
    project = SimpleNamespace(id="au", profile=profile,
                              ws=SimpleNamespace(head=lambda: "rev2", status=lambda: []),
                              adapter=SimpleNamespace(approval_artifacts=lambda *_: dict(arts)),
                              foundry=SimpleNamespace(
                                  persona_info=lambda _: {"exists": True, "enabled": True,
                                                          "strict": True, "routes": [
                                                              {"model": "vm", "vision": True}]},
                                  bind=lambda *a, **k: None, chat=chat))
    spec = SimpleNamespace(task_id="UI-02", scope=list(rels), spec="HUD")
    rule = SimpleNamespace(kind="visual", artifacts=["docs/evidence/{task}/*.png"])
    data = data if data is not None else {}
    approvals = approvals if approvals is not None else []
    ops: dict = {}
    calls: dict = {"reject": [], "stop": [], "events": []}
    store = SimpleNamespace(
        task=lambda pid, tid: {"task_id": tid, "data": data},
        update_task=lambda *a, **k: None,
        merge_data=lambda pid, tid, **kw: data.update(kw),
        event=lambda pid, tid, kind, **kw: calls["events"].append((kind, kw.get("detail"))),
        op_begin=lambda key, *a: ops.get(key) or ops.setdefault(key, {"status": "started"})
        and None,
        op_update=lambda key, st, res: ops.__setitem__(key, {"status": st, "result": res}),
        session=lambda sid: None, add_session=lambda *a, **k: None,
        update_session=lambda *a, **k: None,
        add_approval=lambda pid, tid, kind, rev, arts_, approver, note="": approvals.append(
            {"kind": kind, "revision": rev, "artifacts": dict(arts_), "approver": approver,
             "note": note}) or "apr",
        approvals=lambda pid, tid, kind=None: [a for a in approvals if kind in (None, a["kind"])])
    c = SimpleNamespace(store=store, clock=lambda: 1.0, _valid_approval=lambda *a: None,
                        reject_human=lambda pid, tid, findings, actor: calls["reject"].append(
                            (findings, actor)),
                        _stop=lambda P, t, to, ev, cls="", **kw: calls["stop"].append((to, ev, cls)))
    t = {"task_id": "UI-02", "run_id": "r", "session_id": "p", "attempt": 1, "data": data}
    return c, project, spec, rule, arts, approvals, calls


def _answer(rels, arts, verdict="approve", findings=(), seen=True):
    return json.dumps({"verdict": verdict, "limitations": "", "frames": [
        {"path": rel, "sha256": arts[rel], "image_seen": seen, "subject_present": True,
         "validity": "ok", "observation": "HUD", "findings": list(findings)} for rel in rels]})


def test_after_close_visual_approval_carries_frames_accepted_earlier(tmp_path):
    """W1.3: the templated closeout moved HEAD, so the fast lane's approval (accepted
    with notes) at the gate head never matched the exact revision and the frames
    went back to vision. Frames any earlier visual approval covers are never sent."""
    sent = []
    c, P, spec, rule, arts, approvals, calls = _va_env(
        tmp_path, chat=lambda *a, **k: sent.append(1) or ("", {}), frames=("a.png", "b.png"))
    approvals.append({"kind": "visual", "revision": "gatehead", "artifacts": dict(arts),
                      "approver": "coordinator:fast_lane (model:x)",
                      "note": "accepted with notes after 1 CHANGES round(s): overlap"})
    VA.step(c, P, {"task_id": "UI-02", "run_id": "r", "session_id": "p", "attempt": 1}, spec,
            rule, "rev2")
    assert not sent
    assert approvals[-1]["revision"] == "rev2" and approvals[-1]["approver"] == \
        "coordinator:vision-memory"
    assert approvals[-1]["artifacts"] == arts


def test_after_close_visual_uncertain_is_retried_once_then_accepted_with_notes(tmp_path):
    sent = []

    def chat(persona, messages, **kw):
        sent.append(kw.get("schema"))
        rels = list(arts)
        return _answer(rels, arts, "uncertain", seen=False), {"served_model": "vm"}
    c, P, spec, rule, arts, approvals, calls = _va_env(tmp_path, chat)
    t = {"task_id": "UI-02", "run_id": "r", "session_id": "p", "attempt": 1}
    VA.step(c, P, t, spec, rule, "rev2")                   # first call: uncertain -> retry
    assert len(sent) == 1 and sent[0] is None and not approvals
    assert [k for k, _ in calls["events"] if k == "visual_approval_retry"]
    VA.step(c, P, t, spec, rule, "rev2")                   # the one structured retry
    assert len(sent) == 2 and sent[1] is VA.VISION_SCHEMA
    assert approvals and approvals[-1]["note"].startswith("accepted with notes")
    assert "inconclusive after one structured retry" in approvals[-1]["note"]
    assert [k for k, _ in calls["events"] if k == "visual_approval_accepted_with_notes"]
    VA.step(c, P, t, spec, rule, "rev2")                   # never a third call
    assert len(sent) == 2


def test_after_close_visual_changes_get_one_round_then_notes(tmp_path):
    def chat(persona, messages, **kw):
        return _answer(list(arts), arts, "changes_required", findings=["clipped"]), \
            {"served_model": "vm"}
    c, P, spec, rule, arts, approvals, calls = _va_env(tmp_path, chat)
    t = {"task_id": "UI-02", "run_id": "r", "session_id": "p", "attempt": 1}
    VA.step(c, P, t, spec, rule, "rev2")
    assert calls["reject"] and "clipped" in calls["reject"][0][0][0]   # round 1: back to fix
    P.ws.head = lambda: "rev3"
    VA.step(c, P, t, spec, rule, "rev3")                   # the same findings again
    assert len(calls["reject"]) == 1 and approvals[-1]["note"].startswith("accepted with notes")


def test_after_close_missing_frame_is_a_continuation_then_a_stop(tmp_path):
    c, P, spec, rule, arts, approvals, calls = _va_env(tmp_path, chat=lambda *a, **k: ("", {}))
    rel = next(iter(arts))
    os.remove(os.path.join(str(tmp_path), rel))
    P.adapter.approval_artifacts = lambda *_: {}            # the frame is gone
    t = {"task_id": "UI-02", "run_id": "r", "session_id": "p", "attempt": 1}
    VA.step(c, P, t, spec, rule, "rev2")
    assert len(calls["reject"]) == 1 and "capture and commit" in calls["reject"][0][0][0].lower()
    assert not calls["stop"]
    VA.step(c, P, t, spec, rule, "rev2")                   # still missing after the round
    assert len(calls["reject"]) == 1 and calls["stop"]
    assert calls["stop"][0][0] == S.BLOCKED and calls["stop"][0][2] == "visual_frames_missing"


def test_after_close_missing_frame_is_carried_when_a_prior_approval_still_matches(tmp_path):
    c, P, spec, rule, arts, approvals, calls = _va_env(tmp_path, chat=lambda *a, **k: ("", {}))
    approvals.append({"kind": "visual", "revision": "gatehead", "artifacts": dict(arts),
                      "approver": "model:v/vm", "note": "fast-lane vision approval"})
    spec.scope = []                                         # the plan names no frames
    t = {"task_id": "UI-02", "run_id": "r", "session_id": "p", "attempt": 1}
    VA.step(c, P, t, spec, rule, "rev2")
    assert not calls["reject"] and approvals[-1]["revision"] == "rev2"
    assert "carried from the visual approval" in approvals[-1]["note"]
    assert S.check(S.AWAITING_HUMAN, S.BLOCKED, {"blocker": "x"}) is None


# ======================================================================= 4. commit reword

def _git_log(root):
    return git(root, "log", "--format=%s", "-5").splitlines()


def test_the_coordinator_rewords_its_own_unpushed_commit_messages(tmp_path):
    prompts = []

    def agent(ctx):
        prompts.append(ctx["prompt"])
        return {"writes": {"src/a.txt": "OK"}, "commit": "work without the prefix",
                "final": report()}
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": agent})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE), \
        [(h["from_state"], h["to_state"], h["evidence"]) for h in c.store.history("game", "T1")]
    assert _git_log(root)[0] == "T1: work without the prefix"
    assert len(prompts) == 1                                # never a repair for the message
    [ev] = events({"c": c}, "commits_reworded")
    assert ev["reworded"][0]["subject"] == "work without the prefix"
    data = c.store.task("game", "T1")["data"]
    assert data["reworded_commits"][0]["subject"] == "work without the prefix"
    assert git(root, "ls-remote", "origin", "refs/heads/main").split()[0] == \
        c.store.task("game", "T1")["head_sha"]
    # the author is kept; only the subject changed
    assert git(root, "log", "-1", "--format=%an") == "t"


def test_revert_and_merge_commits_are_exempt_and_pushed_commits_stay_advisory(tmp_path):
    root, _ = make_repo(tmp_path, "game", [T1], extra_files={"src/a.txt": "x\n"})
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": lambda ctx: {"final": ""}})
    P = c.projects["game"]
    base = P.ws.head()
    (root / "src" / "a.txt").write_text("OK\n")
    git(root, "commit", "-qam", "revert: drop the boot row")
    (root / "src" / "a.txt").write_text("OK 2\n")
    git(root, "commit", "-qam", "plain message")
    git(root, "push", "-q", "origin", "main")               # both already on the remote
    head = P.ws.head()
    prefix = CM.prefix_of(P, "T1")
    bad, exempt = CM.unprefixed(P, P.ws.commits(base, head), prefix)
    assert [m for _, m in bad] == ["plain message"] and [m for _, m in exempt] == [
        "revert: drop the boot row"]
    t = {"task_id": "T1", "data": {"task_base": base}, "base_sha": base}
    assert "already pushed" in CM.rewordable(P, t, [s for s, _ in bad])
    out = CM.reword(c, P, t, SimpleNamespace(task_id="T1"))
    assert "already pushed" in out["skipped"] and P.ws.head() == head
    # the generic check: advisory, not a FAIL the session would be asked to rebase for
    spec = P.adapter.describe("T1")
    c.approve("game", "T1", [], "victor")
    c.store.update_task("game", "T1", base_sha=base, data={"task_base": base})
    rows = c._generic_checks_uncached(P, c.store.task("game", "T1"), spec, head, "verify")
    cm = next(r for r in rows if r.check == "coordinator.commit_messages")
    assert cm.result == "PASS" and cm.required is False and "already pushed" in cm.detail


def test_reword_rebuilds_the_range_above_the_oldest_bad_commit_and_remaps_coord_commits(tmp_path):
    root, _ = make_repo(tmp_path, "game", [T1], extra_files={"src/a.txt": "x\n"})
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": lambda ctx: {"final": ""}})
    P = c.projects["game"]
    base = P.ws.head()
    (root / "src" / "a.txt").write_text("1\n")
    git(root, "commit", "-qam", "first, unprefixed")
    (root / "src" / "a.txt").write_text("2\n")
    git(root, "commit", "-qam", "T1: second, fine")
    coord_sha = P.ws.head()
    (root / "src" / "a.txt").write_text("3\n")              # a dirty tree is fine
    c.approve("game", "T1", [], "victor")
    c.store.update_task("game", "T1", base_sha=base,
                        data={"task_base": base, "coord_commits": [coord_sha]})
    out = CM.reword(c, P, c.store.task("game", "T1"), SimpleNamespace(task_id="T1"))
    assert [r["subject"] for r in out["reworded"]] == ["first, unprefixed"]
    assert _git_log(root)[:2] == ["T1: second, fine", "T1: first, unprefixed"]
    assert (root / "src" / "a.txt").read_text() == "3\n"   # the working tree untouched
    data = c.store.task("game", "T1")["data"]
    assert data["coord_commits"] == [P.ws.head()] and P.ws.head() != coord_sha
    assert CM.mapped(c.store.task("game", "T1"), coord_sha) == P.ws.head()
    assert P.ws.is_ancestor(base, P.ws.head())
    assert CM.reword(c, P, c.store.task("game", "T1"), SimpleNamespace(task_id="T1")) == {}


# ======================================================================= 5. lead commits

class _LeadFixer(E2EClient):
    """A write-access lead session that fixes the task file but never commits."""

    def _dispatch(self, ctx):
        md = self.sessions[ctx["session_id"]]["metadata"]
        if md.get("role") == "lead":
            with open(os.path.join(ctx["workspace"], "src", "a.txt"), "w") as fh:
                fh.write("OK fixed by the lead\n")
        return super()._dispatch(ctx)


def test_uncommitted_lead_edits_are_committed_as_the_leads_and_reverified_in_place(tmp_path):
    fix = {"classification": "test_harness",
           "hypothesis": "The task file never got the OK marker; I wrote it.",
           "evidence_refs": ["src/a.txt:1"], "action": {"kind": "reverify"}}
    client = _LeadFixer(lambda ctx: blocked("which gallery layout is approved?"),
                        lambda ctx: lead_reply(fix))
    root, _ = make_repo(tmp_path, "game", [TASK], extra_files={"src/a.txt": "start\n"})
    prof = write_profile(tmp_path, "game", root, extra=UI_OVERRIDE + lead_toml(write_access=True))
    c = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": E2EFoundry()},
                    store_path=tmp_path / "state" / "coordinator.sqlite")
    c.approve("game", TID, [], "victor")
    e = {"c": c, "client": client}
    assert run_until(c, lambda: _state(c, TID) == S.COMPLETE, limit=60), hist(e)
    [ev] = events(e, "lead_commit")
    assert ev["paths"] == ["src/a.txt"] and ev["message"].startswith(f"{TID}: lead case ")
    assert _git_log(root)[0].startswith(f"{TID}: lead case ")
    # verified in place: the primary was not started again after the lead's fix
    assert len(client.primary_starts()) == 1
    assert c.store.task("game", TID)["data"]["lead_commits"]


# ======================================================================= 6. token hygiene

MEM = ("## Coordinator memory\n- DECISION: use layout B [victor/operator]\n"
       "- CONSTRAINT: never edit GateRunner [plan/engine]\n\n"
       "## Authorized decisions (operator; these stand until superseded)\n"
       "- [scope_request] widen? -> deny (operator, dec-1)")


def test_memory_delta_sends_only_new_entries(tmp_path):
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": lambda ctx: {"final": ""}})
    P = c.projects["game"]
    assert c._memory_delta(P, "T1", "s1", MEM) == MEM
    c._notes_delivered([], "s1")
    assert c._memory_delta(P, "T1", "s1", MEM) == ""
    c._notes_delivered([], "s1")
    changed = MEM.replace("[plan/engine]", "[plan/engine]\n- NOTE: S46 flaky [agent/session]")
    out = c._memory_delta(P, "T1", "s1", changed)
    assert "S46 flaky" in out and "use layout B" not in out and "widen?" not in out
    assert "## Coordinator memory" in out and "## Authorized decisions" not in out
    [ev] = events({"c": c}, "memory_delta")
    assert ev == {"sent": 1, "unchanged": 3}
    c._notes_delivered([], "s1")
    newdec = changed + "\n- [no_progress] stall -> continue once (timeout_default, dec-2)"
    out2 = c._memory_delta(P, "T1", "s1", newdec)
    assert "dec-2" in out2 and "S46" not in out2 and "## Authorized decisions" in out2


def test_an_invalid_review_is_resent_once_with_the_error_appended(tmp_path):
    packets = []

    def reviewer(p):
        packets.append(p)
        return "not json at all" if len(packets) == 1 else review_json(p)
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": lambda ctx: {"writes": {"src/a.txt": "OK"},
                                                           "commit": "T1: work",
                                                           "final": report()}},
                    foundry={"game": FakeFoundry(reviewer)})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    assert len(packets) == 2 and packets[0] != packets[1]
    assert "REJECTED" not in packets[0] and "was REJECTED" in packets[1]
    assert "this is the one re-send" in packets[1]


# ======================================================================= 7. profile drift

def test_profile_drift_doctor_rows(tmp_path):
    from foundry_coordinator import profile_drift
    raw = {"fast_lane": {"enabled": True, "max_vision_rounds": 2},
           # a fixed ceiling (pct 0) is what can disagree with fixed telemetry checkpoints
           "automation": {"lead_hard_max_calls": 8, "preflight_endpoints": [],
                          "context_ceiling_pct": 0},
           "lead_manager": {"max_lead_calls_per_task": 12},
           "lanes": {"enabled": True},
           "helpers": {"max_units_per_task": 24, "max_running": 6},
           "telemetry": {"context_checkpoints": [140000, 190000]}}
    P = SimpleNamespace(profile=SimpleNamespace(raw=raw, workspace=str(tmp_path)),
                        adapter=SimpleNamespace())
    rows = {name: (st, detail) for st, name, detail in profile_drift.checks(P)}
    for name in ("drift: max_vision_rounds", "drift: preflight rojo",
                 "drift: preflight local model", "drift: adapter infra_checks",
                 "drift: lead_hard_max_calls", "drift: lanes under on_demand",
                 "drift: helper caps", "drift: context thresholds"):
        assert rows[name][0] == "WARN", (name, rows[name])
    assert "effective hard cap is 12" in rows["drift: lead_hard_max_calls"][1]
    (tmp_path / "kilo.jsonc").write_text('{"agent": {}}')
    rows = {name: st for st, name, _ in profile_drift.checks(P)}
    assert rows["drift: kilo snapshot"] == "WARN"
    (tmp_path / "kilo.jsonc").write_text('{"snapshot": false, "agent": {}}')
    good = {"fast_lane": {"enabled": True, "max_vision_rounds": 1},
            "automation": {"lead_hard_max_calls": 12, "preflight_endpoints": [
                "rojo=http://127.0.0.1:34872/api/rojo", "local-model=http://127.0.0.1:8080/h"]},
            "lead_manager": {"max_lead_calls_per_task": 12}}
    P2 = SimpleNamespace(profile=SimpleNamespace(raw=good, workspace=str(tmp_path)),
                         adapter=SimpleNamespace(infra_checks=lambda: []))
    assert all(st == "PASS" for st, _, _ in profile_drift.checks(P2))


def test_kilo_config_plan_turns_the_snapshot_question_off():
    from foundry_coordinator.kilo_config import plan
    text = '{\n  // c\n  "model": "x",\n  "agent": {}\n}\n'
    p = plan(text, {}, "ws")
    assert p["snapshot_changed"] is True and '"snapshot": false' in p["text"]
    from foundry_automation import jsonc
    assert jsonc.parse(p["text"])["snapshot"] is False and "// c" in p["text"]
    assert plan(p["text"], {}, "ws")["snapshot_changed"] is False


def test_au_infra_checks_probe_rojo(tmp_path, monkeypatch):
    import urllib.request
    from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
    a = AnimalUprisingAdapter.__new__(AnimalUprisingAdapter)
    a.p = SimpleNamespace(workspace=str(tmp_path), raw={})
    a.cfg, a.studio_probe = {}, None
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *args, **kw: (_ for _ in ()).throw(OSError("refused")))
    [r] = a.infra_checks()
    assert r["name"] == "rojo" and r["ok"] is False and "34872" in r["detail"]
    a.cfg = {"infra_checks": ["studio"]}
    [r] = a.infra_checks()
    assert r["ok"] is None                                  # not checkable without a probe
    a.studio_probe = lambda spec: {"connected": False, "error": "plugin not connected"}
    assert a.infra_checks()[0]["ok"] is False
    from foundry_automation.schema import FIELDS
    assert any(f.key == "animaluprising.infra_checks" for f in FIELDS)


def test_example_profile_matches_the_revamp_decisions():
    from foundry_coordinator.profile import load as load_profile
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(here, "foundry_coordinator", "projects", "animaluprising",
                        "project.example.toml")
    raw = open(path, encoding="utf-8").read()
    assert "# max_vision_rounds = 1" in raw and "# max_vision_rounds = 2" not in raw
    assert 'infra_checks = ["rojo"]' in raw
    assert "rojo=http://127.0.0.1:34872/api/rojo" in raw and "local-model=" in raw
    assert "lead_hard_max_calls = 12" in raw
    skill = os.path.join(os.path.dirname(path), "kilo_skills", "au-fast-lane", "SKILL.md")
    text = open(skill, encoding="utf-8").read()
    assert "two CHANGES rounds" not in text and "ONE CHANGES round" in text
    assert "sprint --sprint <ID> --files" not in text and "SCOPE_REQUEST" in text
    assert "`options`" in text and "question` tool is off" in text


# ======================================================================= 8. auto-heal repairs

def test_stale_runner_lock_is_cleared_only_for_a_dead_pid(tmp_path):
    lock = autoheal.runner_lock_path(str(tmp_path))
    os.makedirs(os.path.dirname(lock))
    json.dump({"pid": os.getpid(), "sprint": "UI-1", "mode": "sprint"}, open(lock, "w"))
    assert autoheal.clear_stale_runner_lock(str(tmp_path))["cleared"] is False
    assert os.path.exists(lock)
    dead = subprocess.run(["git", "--version"], capture_output=True)   # a pid that exited
    json.dump({"pid": 4000000, "sprint": "UI-1", "mode": "sprint"}, open(lock, "w"))
    os.makedirs(os.path.join(os.path.dirname(lock), "UI-1"), exist_ok=True)
    json.dump({"pid": 4000000, "phase": "stories", "playing": True},
              open(os.path.join(os.path.dirname(lock), "UI-1", "status.json"), "w"))
    out = autoheal.clear_stale_runner_lock(str(tmp_path))
    assert out == {"cleared": True, "pid": 4000000, "playing": True, "sprint": "UI-1"}
    assert not os.path.exists(lock) and dead.returncode == 0


def test_repairs_restart_rojo_through_the_client_and_are_journaled(tmp_path, monkeypatch):
    from foundry_coordinator import client_api
    asked = []
    monkeypatch.setattr(client_api, "restart_component",
                        lambda name, force=False, api=None: asked.append(name) or "")
    monkeypatch.setattr(autoheal, "stop_playtest", lambda P: "")
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": lambda ctx: {"final": ""}})
    P = c.projects["game"]
    lock = autoheal.runner_lock_path(P.profile.workspace)
    os.makedirs(os.path.dirname(lock))
    json.dump({"pid": 4000000, "sprint": "T1"}, open(lock, "w"))
    out = autoheal.repair(c, P, {"task_id": "T1"}, "rojo_stale", "verify")
    assert [r["repair"] for r in out] == ["clear_stale_runner_lock", "restart_rojo"]
    assert all(r["ok"] for r in out) and asked == ["rojo"]
    [ev] = events({"c": c}, "auto_heal_repair")
    assert ev["signature"] == "rojo_stale" and len(ev["repairs"]) == 2
    assert autoheal.infra_signature("host.live_hash FAIL: Studio differs") == "rojo_stale"
    assert autoheal.infra_signature("rojo serve exited with code -1") == "rojo"
    monkeypatch.setattr(client_api, "restart_component",
                        lambda name, force=False, api=None: "no Foundry Client on this workstation")
    out = autoheal.repair(c, P, {"task_id": "T1"}, "rojo", "restart")
    assert out[-1]["repair"] == "restart_rojo" and out[-1]["ok"] is False


def test_client_api_discovery_and_restart(tmp_path, monkeypatch):
    from foundry_coordinator import client_api
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(client_api.sys, "platform", "win32")
    monkeypatch.delenv("FOUNDRY_CLIENT_CONFIG", raising=False)
    assert client_api.discover() is None
    d = tmp_path / "FoundryClient"
    d.mkdir()
    (d / "api.token").write_text("t" * 40)
    (d / "client.toml").write_text("[client]\napi_port = 47899\n")
    api = client_api.discover()
    assert api.url == "http://127.0.0.1:47899" and api.token == "t" * 40
    calls = []

    def fake_req(self, method, path, body=None):
        calls.append((method, path))
        if path == "/api/status":
            return {"components": [{"name": "rojo", "enabled": True, "want": True}]}
        return {"name": "rojo", "state": "starting"}
    monkeypatch.setattr(client_api.ClientAPI, "_req", fake_req)
    assert client_api.restart_component("rojo") == ""
    assert calls == [("GET", "/api/status"), ("POST", "/api/components/rojo/restart")]
    assert "manages no component" in client_api.restart_component("studio")


def test_rojo_stale_only_classifies_the_live_hash_check():
    from foundry_coordinator.fast_lane import rojo_stale_only
    v = {"checks": [{"id": "host.live_hash", "result": "FAIL", "required": True},
                    {"id": "story.Hud", "result": "PASS", "required": True}]}
    assert rojo_stale_only(v)
    v["checks"].append({"id": "story.Shop", "result": "FAIL", "required": True})
    assert not rojo_stale_only(v)
    assert not rojo_stale_only({"checks": [{"id": "story.Hud", "result": "PASS"}]})


def _fast_lane_env(tmp_path, monkeypatch, plan):
    from tests.coordinator import test_fast_lane as FL
    repo, _ = make_repo(tmp_path, "game", [FL.T1], extra_files={".gitignore": ".plan/\n"})
    runner = tmp_path / "fake_gate.py"
    runner.write_text(FL.FAKE_GATE)
    plan_file = tmp_path / "gate_plan.json"
    plan_file.write_text(json.dumps(plan))
    monkeypatch.setenv("FAKE_GATE_PLAN", str(plan_file))
    prof = FL.fl_profile(tmp_path, repo, runner, FL.VISUAL + FL.vis_override(), None)
    now = [1_000_000.0]
    client = FL.Recorder(FL.agent, project_id="game")
    c = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": FL.VisionFoundry()},
                    clock=lambda: now[0])
    return SimpleNamespace(c=c, client=client, now=now, repo=repo)


def test_a_dead_gate_runner_is_tooling_not_a_45_minute_stall(tmp_path, monkeypatch):
    from tests.coordinator import test_fast_lane as FL
    e = _fast_lane_env(tmp_path, monkeypatch, [{"result": "PASS", "sleep": 60}])
    e.c.approve("game", "T1", [], "victor")
    FL.drive(e, lambda t: bool((t["data"].get("fast_lane") or {}).get("run")), step=1)
    (key, proc), = list(e.c.fast_lane._procs.items())
    run = FL.fl(e)["run"]
    e.c.fast_lane._procs.pop(key)                            # as after a coordinator restart
    proc.kill()
    proc.wait(timeout=20)
    gate_dir = os.path.join(str(e.repo), ".plan", "tmp", "gate", "T1")
    os.makedirs(gate_dir, exist_ok=True)
    with open(os.path.join(gate_dir, "status.json"), "w") as f:
        json.dump({"pid": run["pid"], "phase": "stories", "progress": "3/9", "playing": False},
                  f)
    t = FL.drive(e, lambda t: t["state"] == S.BLOCKED, step=1, limit=20)
    assert "is gone without a verdict" in t["blocker"]
    assert e.now[0] - run["started"] < 10 * 60               # not the 45-minute timeout
    assert FL.events(e, "fast_lane_runner_dead")
    assert not FL.fl(e).get("red_gates")                     # tooling, never a red gate
