"""Failure classes, repeat fingerprints, repair assignments, the submission gate,
review routing and progress (lifecycle, plain Store + fakes)."""
import pytest

from foundry_coordinator.obligations import Obligations
from foundry_coordinator.recovery import (CLASSES, Recovery, classify, fingerprint, match,
                                          review_route, tokens)
from foundry_coordinator.store import Store
from tests.coordinator.test_obligations import GATE_PASS, FakeWS, project, spec, task_row

CAP = "docs/evidence/UI-01/gallery_desktop.png"


class FakeLedger:
    def __init__(self, current=None, missing=None):
        self._current, self._missing = current or {}, missing or []

    def current(self, P, spec, revision):
        return self._current

    def missing(self, P, spec, revision):
        return self._missing


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "c.sqlite"))


def setup(store, ws=None, limits=None, data=None):
    ob, rec = Obligations(store), Recovery(store)
    P = project(ws=ws or FakeWS(trees={"h1": {CAP: "old"}, "h2": {CAP: "old"},
                                      "h3": {CAP: "new"}}, head="h2"), limits=limits)
    t = task_row(store, data=data)
    ob.seed(P, spec())
    return ob, rec, P, t


# -- classification ----------------------------------------------------------------------

@pytest.mark.parametrize("source,item,ctx,want", [
    ("denial", {"category": "budget", "reason": "monthly budget"}, {}, "quota"),
    ("review", {"error": "HTTP 429 Too Many Requests"}, {}, "quota"),
    ("session", {"status": "timeout"}, {}, "transport"),
    ("review", {"kind": "review_call_failed", "error": "FoundryError: reset"}, {}, "transport"),
    ("check", {"check": "coordinator.unrelated_preserved", "result": "FAIL",
               "detail": "new uncommitted files outside the task: .kilo/plans/ui01_tmp"},
     {}, "env_sync"),
    ("check", {"check": "au.lune", "result": "UNKNOWN", "detail": "lune not on PATH"}, {},
     "env_sync"),
    ("check", {"check": "coordinator.history", "result": "FAIL", "detail": "rewritten"}, {},
     "env_sync"),
    ("check", {"check": "au.gate_bound_to_head", "result": "FAIL", "detail": "gate at old sha"},
     {}, "missing_evidence"),
    ("evidence", {"status": "stale", "path": CAP}, {}, "missing_evidence"),
    ("review", {"severity": "major", "file": "src/ui/Gallery.lua",
                "text": "cannot judge the layout"}, {"coverage_omitted": ["src/ui/Gallery.lua"]},
     "packet_omission"),
    ("check", {"check": "au.done_check.SUMMARY", "result": "FAIL", "detail": "status word"},
     {}, "docs_inconsistency"),
    ("check", {"check": "au.done_check", "result": "UNKNOWN",
               "detail": "done_check printed no rows: ..."}, {}, "test_harness"),
    ("check", {"check": "au.tests", "result": "FAIL", "detail": "1 failed"}, {"flaky": True},
     "test_harness"),
    ("check", {"check": "coordinator.scope", "result": "FAIL",
               "detail": "changed outside the task's files: src/other.lua"}, {}, "scope_decision"),
    ("guidance", {"action": "needs_human"}, {}, "scope_decision"),
    ("review", {"severity": "major", "file": "src/ui/Gallery.lua",
                "text": "Gallery still overlaps the header"},
     {"checks_green": True, "repair_touched": ["src/ui/Gallery.lua"],
      "prev_fingerprints": [fingerprint("product_defect", "review:major", "src/ui/Gallery.lua",
                                        "Gallery still overlaps the header")]},
     "review_disagreement"),
    ("check", {"check": "au.tests", "result": "FAIL", "detail": "expected 3 got 2"}, {},
     "product_defect"),
])
def test_classify(source, item, ctx, want):
    cls, why = classify(source, item, ctx)
    assert cls == want and cls in CLASSES and why


def test_preexisting_dirty_is_not_env_sync():
    cls, _ = classify("check", {"check": "coordinator.unrelated_preserved", "result": "FAIL",
                                "detail": "pre-existing uncommitted changes were modified: a.txt"},
                      {})
    assert cls == "product_defect"


# -- fingerprints ---------------------------------------------------------------------------

A2 = ("Capture docs/evidence/UI-01/gallery_desktop.png is stale: the blob is unchanged "
      "since attempt 2 (sha 3f9a1c2d4e)")
A3 = ("Stale capture: docs/evidence/UI-01/gallery_desktop.png blob still unchanged "
      "(attempt 3, 7b2e9f01aa)")


def test_reworded_repeat_matches_and_different_does_not():
    cid = "au.recovery_capture_freshness"
    fp2 = fingerprint("missing_evidence", cid, None, A2)
    stored = [{"fingerprint": fp2, "class": "missing_evidence", "check_id": cid,
               "path": CAP.lower(), "tokens": tokens(A2)}]
    assert match("missing_evidence", cid, None, A3, stored) == fp2
    other = "Capture docs/evidence/UI-01/gallery_desktop.png shows the wrong theme colours"
    assert match("missing_evidence", cid, None, other, stored) is None
    assert match("missing_evidence", "au.tests", None, A3, stored) is None
    # ids, numbers and shas don't change a fingerprint
    assert fingerprint("x", "c", "p", "failed at line 12 in run-77 abc1234f") == \
        fingerprint("x", "c", "p", "failed at line 98 in run-12 deadbee9")


def test_record_findings_counts_distinct_attempts(store):
    ob, rec, P, t = setup(store)
    it = lambda text: {"class": "missing_evidence", "check_id": "au.freshness", "text": text}
    assert rec.record_findings(P, t, 2, [it(A2)]) == []
    assert rec.record_findings(P, t, 2, [it(A2)]) == []         # same attempt: not a repeat
    reps = rec.record_findings(P, t, 3, [it(A3)])
    assert len(reps) == 1 and reps[0]["count"] == 2 and reps[0]["attempts"] == [2, 3]
    # a different finding in the same attempt is new, not a repeat
    new = {"class": "product_defect", "check_id": "au.tests", "text": "expected 3 got 2"}
    assert [r["check_id"] for r in rec.record_findings(P, t, 3, [new])] == []
    assert len(rec.findings("game", "UI-01")) == 2


# -- assignment ----------------------------------------------------------------------------------

def test_assignment_lists_only_unresolved_work_and_affected_checks(store):
    ob, rec, P, t = setup(store)
    ob.evaluate(P, t, "h2", {"au.gate": GATE_PASS}, [])     # gate done, capture open
    items = [{"path": CAP, "title": "gallery capture", "status": "stale", "note": "blob old"},
             {"path": "docs/other.md", "title": "unrelated", "status": "ok"}]
    a = rec.assignment(P, t, ledger=FakeLedger(), obligations=ob, ws=P.ws,
                       evidence_items=items, last_report="I fixed the layout")
    ids = [u["obligation_id"] for u in a["unresolved"]]
    assert ids == ["cap-gallery_desktop"]                 # not gate (verified), not CLOSE
    assert a["affected_checks_to_rerun"] == []             # gate verified: nothing to rerun
    assert [o["path"] for o in a["overlooked"]] == [CAP]
    assert "close.summary" not in a["prompt_text"] and "cap-gallery_desktop" in a["prompt_text"]
    assert "no check may be waived" in a["prompt_text"]
    assert len(a["prompt_text"]) <= 6000
    # idempotent per attempt
    assert rec.assignment(P, t, obligations=ob, ws=P.ws) == a


def test_assignment_keeps_open_build_checks_and_refuses_waiving_strategy(store):
    ob, rec, P, t = setup(store)
    a = rec.assignment(P, t, obligations=ob, ws=P.ws,
                       strategy="Skip the au.gate check and let the CLOSE session capture it")
    assert "au.gate" in a["affected_checks_to_rerun"]
    assert a["rejected_guidance"] and "Skip" not in a["strategy"]
    assert {u["phase"] for u in a["unresolved"]} == {"BUILD"}


def test_three_repeats_with_unchanged_strategy_escalate(store):
    ob, rec, P, t = setup(store)
    f = lambda text: [{"class": "missing_evidence", "check_id": "au.freshness", "text": text}]

    def attempt(n, text, strategy, adviser=None):
        rec.record_findings(P, t, n, f(text))
        store.update_task("game", "UI-01", attempt=n + 1)
        return rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws,
                              strategy=strategy, adviser=adviser)

    a2 = attempt(1, A2, "Re-capture the gallery from the running build")
    assert "decision" not in a2 and not a2["repeats"]
    a3 = attempt(2, A3, "Re-capture the gallery after a clean Studio restart")
    assert "decision" not in a3 and a3["strategy_required_different"]
    # third occurrence, same strategy, no adviser -> a decision, no assignment
    a4 = attempt(3, A2, "Re-capture the gallery after a clean Studio restart")
    assert set(a4) >= {"decision"} and a4["repeats"][0]["count"] == 3
    d = ob.decision(a4["decision"])
    assert d["kind"] == "recovery_escalation" and d["status"] == "open"
    assert not any(r["attempt"] == 4 and r.get("assignment") for r in rec.repairs("game", "UI-01"))
    # the operator answers with a different strategy: the assignment goes through
    ob.answer(a4["decision"], {"strategy": "Delete nothing; regenerate captures with the "
                                           "capture tool at head and commit them"},
              "victor", "operator", channel="control")
    a4b = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws,
                         strategy="Re-capture the gallery after a clean Studio restart")
    assert "decision" not in a4b and "capture tool" in a4b["strategy"]


def test_fresh_lead_strategy_crosses_repeat_guard_once(store):
    ob, rec, P, t = setup(store)
    finding = {"class": "missing_evidence", "check_id": "au.freshness", "text": A2}
    for n in (1, 2, 3):
        rec.record_findings(P, t, n, [finding])
        store.update_task("game", "UI-01", attempt=n + 1)
        if n < 3:
            rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws,
                           strategy=f"Previous repair approach {n}")

    lead = "Collect the raw gate tally before reviewing the captured evidence"
    a = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws,
                       strategy=lead, trusted_strategy=True)
    assert "decision" not in a and a["strategy"] == lead
    assert a["strategy_required_different"] and a["affected_checks_to_rerun"]

    # An ordinary strategy cannot bypass the guard, and the same lead
    # assignment cannot be replayed on the next attempt.
    rec.record_findings(P, t, 4, [finding])
    store.update_task("game", "UI-01", attempt=5)
    next_a = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws,
                            strategy=lead, trusted_strategy=True)
    assert "decision" in next_a


FB = {"class": "missing_evidence", "check_id": "au.gate_bound_to_head",
      "text": "gate verdicts were recorded at an older commit than HEAD"}


def _answer(ob, did, strategy):
    return ob.answer(did, {"strategy": strategy}, "victor", "operator", channel="control")


def _second_repeat_set(store):
    """UI-02's history: repeat set A escalates and is answered (and re-asked
    and answered again), then a new finding joins the repeats (set B)."""
    ob, rec, P, t = setup(store)
    fa = {"class": "missing_evidence", "check_id": "au.freshness", "text": A2}

    def attempt(n, items, strategy, **kw):
        rec.record_findings(P, t, n, items)
        store.update_task("game", "UI-01", attempt=n + 1)
        return rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws,
                              strategy=strategy, **kw)

    attempt(1, [fa], "Re-capture the gallery from the running build")
    attempt(2, [fa], "Re-capture the gallery after a clean Studio restart")
    a = attempt(3, [fa], "Re-capture the gallery after a clean Studio restart")
    _answer(ob, a["decision"], "Regenerate captures with the capture tool at head")
    assert "decision" not in rec.assignment(P, store.task("game", "UI-01"), obligations=ob,
                                            ws=P.ws)
    a = attempt(4, [fa, FB], "Regenerate captures with the capture tool at head")
    first = ob.decision(a["decision"])           # the same set again: re-asked in its chain
    _answer(ob, first["decision_id"], "Commit the plan step first, then capture at head")
    assert ob.decision(first["supersedes"])["status"] == "superseded"
    assert "decision" not in rec.assignment(P, store.task("game", "UI-01"), obligations=ob,
                                            ws=P.ws)
    return ob, rec, P, t, first, attempt, fa


def test_new_repeat_set_question_supersedes_the_older_answer(store):
    """UI-02 dec-d046...: the new repeat set's question must name the older set's
    answered escalation, so the operator's different answer replaces it instead
    of tripping the contradiction guard."""
    ob, rec, P, t, first, attempt, fa = _second_repeat_set(store)
    b = attempt(5, [fa, FB], "Commit the plan step first, then capture at head")
    d = ob.decision(b["decision"])
    assert d["status"] == "open" and d["question"] != first["question"]
    assert (d["scope"] or {}).get("escalation_key") != (first["scope"] or {}).get(
        "escalation_key")
    assert d["supersedes"] == first["decision_id"]
    _answer(ob, d["decision_id"], "Collect the raw GateRunner tally, then run the gate")
    assert ob.decision(first["decision_id"])["status"] == "superseded"
    assert ob.decision(first["decision_id"])["superseded_by"] == d["decision_id"]
    assert [x["decision_id"] for x in ob.active_decisions("game", "UI-01",
                                                          "recovery_escalation")] ==         [d["decision_id"]]


def test_legacy_unlinked_escalation_is_reconciled_without_weakening_the_guard(store):
    """A row opened before the fix (no supersedes) still trips the guard; the
    coordinator links it to the older answered escalation, and only that one."""
    from foundry_coordinator.obligations import DecisionConflict
    from foundry_coordinator.recovery import link_prior_escalation
    ob, rec, P, t, first, attempt, fa = _second_repeat_set(store)
    legacy = ob.open_decision("game", "UI-01", "recovery_escalation",
                              "Findings repeat across attempts: legacy set B",
                              blocks=["UI-01"], scope={"escalation_key": "legacy-b"})
    with pytest.raises(DecisionConflict):
        _answer(ob, legacy, "A different strategy")              # the guard is intact
    # never linked: another kind, or a question that is not the newer one
    other = ob.open_decision("game", "UI-01", "no_progress", "no progress", blocks=["UI-01"])
    assert link_prior_escalation(ob, "game", "UI-01", other) == ""
    assert link_prior_escalation(ob, "game", "UI-02", legacy) == ""
    assert link_prior_escalation(ob, "game", "UI-01", first["decision_id"]) == ""  # answered
    assert link_prior_escalation(ob, "game", "UI-01", legacy) == first["decision_id"]
    assert link_prior_escalation(ob, "game", "UI-01", legacy) == ""                # once
    assert ob.decision(first["decision_id"])["status"] == "answered"   # until answered
    _answer(ob, legacy, "A different strategy")
    assert ob.decision(first["decision_id"])["status"] == "superseded"
    ev = [e["detail"] for e in store.events("game", 50) if e["kind"] == "decision_linked"]
    assert ev and ev[0]["supersedes"] == first["decision_id"]


def test_fresh_lead_strategy_withdraws_the_open_repeat_question(store):
    """After a lead case resolves, its new strategy reaches the primary once:
    no new escalation, and the still-open repeat question it answers is
    withdrawn (kept for the audit). A repeated or waiving lead text escalates."""
    ob, rec, P, t, first, attempt, fa = _second_repeat_set(store)
    b = attempt(5, [fa, FB], "Commit the plan step first, then capture at head")
    lead = "Run GateRunner.run then GateRunner.tally in Studio and keep the raw output"
    a = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws,
                       strategy=lead, trusted_strategy=True)
    assert "decision" not in a and a["strategy"] == lead and a["lead_strategy"]
    assert a["withdrawn_decisions"] == [b["decision"]]
    assert ob.decision(b["decision"])["status"] == "withdrawn"
    assert not [d for d in ob.active_decisions("game", "UI-01") if d["status"] == "open"]
    # the same lead text on the next attempt: asks again (no second free pass)
    again = attempt(6, [fa, FB], lead, trusted_strategy=True)
    assert "decision" in again


def test_waiving_or_repeated_lead_strategy_still_escalates(store):
    ob, rec, P, t, first, attempt, fa = _second_repeat_set(store)
    b = attempt(5, [fa, FB], "Commit the plan step first, then capture at head")
    bad = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws,
                         strategy="Skip the au.gate check and let the CLOSE session capture it",
                         trusted_strategy=True)
    assert bad["decision"] == b["decision"] and bad["rejected_guidance"]
    assert ob.decision(b["decision"])["status"] == "open"
    # a lead text that repeats an earlier strategy is not fresh either
    old = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws,
                         strategy="Commit the plan step first, then capture at head",
                         trusted_strategy=True)
    assert old["decision"] == b["decision"]


def test_repeat_with_adviser_is_consulted_once(store):
    ob, rec, P, t = setup(store)
    calls = []

    def adviser(packet):
        calls.append(packet)
        return {"action": "repair", "summary": "different approach",
                "steps": ["Waive the freshness check", "Regenerate the capture via the tool"]}
    rec.record_findings(P, t, 1, [{"class": "missing_evidence", "check_id": "c", "text": A2}])
    store.update_task("game", "UI-01", attempt=2)
    rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws)
    rec.record_findings(P, t, 2, [{"class": "missing_evidence", "check_id": "c", "text": A3}])
    store.update_task("game", "UI-01", attempt=3)
    a = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws, adviser=adviser)
    assert len(calls) == 1 and "decision" not in a
    assert "Regenerate" in a["strategy"] and "Waive" not in a["strategy"]
    assert any("waives_check" in r for r in a["rejected_guidance"])
    # next repeat: the adviser is not asked again -> decision
    rec.record_findings(P, t, 3, [{"class": "missing_evidence", "check_id": "c", "text": A2}])
    store.update_task("game", "UI-01", attempt=4)
    b = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws, adviser=adviser,
                       strategy=a["strategy"])
    assert "decision" in b and len(calls) == 1


def test_transient_adviser_defers_without_operator_decision(store):
    ob, rec, P, t = setup(store)
    f = lambda text: [{"class": "missing_evidence", "check_id": "c", "text": text}]
    rec.record_findings(P, t, 1, f(A2))
    store.update_task("game", "UI-01", attempt=2)
    rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws)
    rec.record_findings(P, t, 2, f(A3))
    store.update_task("game", "UI-01", attempt=3)
    a = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws,
                       adviser=lambda _: {"action": "deferred", "summary": "usage HTTP 429"})
    assert a["defer"] is True and not ob.active_decisions("game", "UI-01")
    b = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws,
                       adviser=lambda _: {"action": "repair", "summary": "different route",
                                          "steps": ["Regenerate the current evidence"]})
    assert "decision" not in b and "Regenerate" in b["strategy"]


# -- submission gate ----------------------------------------------------------------------------------

def test_gate_blocks_while_build_evidence_missing_then_passes(store):
    ob, rec, P, t = setup(store)
    P.adapter.describe = lambda tid: spec(tid)
    led = FakeLedger(current={"au.gate": GATE_PASS})
    out = rec.gate_submission(P, t, "h2", led, ob)
    assert [o["item"].get("obligation_id") for o in out] == ["cap-gallery_desktop"]
    assert out[0]["class"] == "missing_evidence" and out[0]["action"] == "continue_same_session"
    # a stale packet item also blocks
    out = rec.gate_submission(P, t, "h3", led, ob,
                              evidence_items=[{"path": CAP, "status": "stale", "priority": 1}])
    assert [o["item"].get("path") for o in out] == [CAP]
    assert rec.gate_submission(P, t, "h3", led, ob) == []
    # CLOSE obligations stay open and don't block the BUILD review
    assert ob.get("game", "UI-01", "close.summary")["status"] == "open"


def test_gate_blocks_on_open_obligation_conflict_and_spent_continuations(store):
    ob, rec, P, t = setup(store)
    P.adapter.describe = lambda tid: spec(tid)
    ob.screen_text(P, "UI-01", "Defer captures to CLOSE", "last_report")
    for _ in range(2):
        ob.charge("game", "UI-01", "same_session_continuations", 1, "x", "coordinator")
    out = rec.gate_submission(P, t, "h2", FakeLedger(), ob)
    assert any(o["action"] == "decision" for o in out)
    assert {o["action"] for o in out if o["class"] == "missing_evidence"} == {"changes_required"}


# -- review routing ---------------------------------------------------------------------------------------

def test_missing_attachment_routes_to_evidence_incomplete():
    review = {"verdict": "CHANGES_REQUIRED", "findings": [
        {"severity": "major", "file": CAP,
         "text": f"The capture attachment {CAP} is missing from the packet"}]}
    assert review_route(review, {}, {}) == "evidence_incomplete"
    review["findings"].append({"severity": "major", "file": "src/ui/Gallery.lua",
                               "text": "Gallery overlaps the header bar"})
    assert review_route(review, {}, {}) == "product_changes"
    assert review_route({"verdict": "CHANGES_REQUIRED", "error": "HTTP 403 forbidden"}) == \
        "access_blocked"
    assert review_route({"verdict": "CHANGES_REQUIRED", "findings": [
        {"severity": "minor", "file": "src/x.lua", "text": "cannot judge"}]},
        {"omitted": ["src/x.lua"]}) == "evidence_incomplete"


# -- progress ---------------------------------------------------------------------------------------------

def test_no_progress_detection(store):
    ob, rec, P, t = setup(store)
    rec.mark_session(P, t, "s1", prev_head="h2", head="h2", actions="I reviewed the plan")
    rec.mark_session(P, t, "s1", prev_head="h2", head="h2", actions="dup")      # idempotent
    p = rec.progress(P, t)
    assert p["stalls"] == 1 and not p["no_progress"]
    rec.mark_session(P, t, "s2", prev_head="h2", head="h2", actions="I reviewed the plan")
    p = rec.progress(P, t)
    assert p == {**p, "stalls": 2, "summary_only_sessions": 2, "repeated_actions": 1,
                 "no_progress": True, "recommendation": "consult", "verified_delta": 0}
    rec.mark_session(P, t, "s3", prev_head="h2", head="h2", actions="Summary again")
    assert rec.progress(P, t)["recommendation"] == "decision"
    # a session that verifies an obligation is progress
    ob.evaluate(P, t, "h3", {"au.gate": GATE_PASS}, [])
    rec.mark_session(P, t, "s4", prev_head="h2", head="h3", actions="captured")
    p = rec.progress(P, t)
    assert p["verified_delta"] == 2 and p["stalls"] == 0 and not p["no_progress"]


def test_recovery_isolated_by_project(store):
    ob, rec, P, t = setup(store)
    Q = project("other")
    task_row(store, pid="other")
    rec.record_findings(P, t, 1, [{"class": "product_defect", "check_id": "c", "text": A2}])
    rec.record_findings(P, t, 2, [{"class": "product_defect", "check_id": "c", "text": A2}])
    assert rec.findings("other", "UI-01") == []
    assert rec.record_findings(Q, "UI-01", 3, [{"class": "product_defect", "check_id": "c",
                                                "text": A2}]) == []
    rec.mark_session(P, t, "s1", prev_head="h2", head="h2")
    assert rec.progress(Q, "UI-01")["sessions"] == 0


# -- v0.122 review regressions ------------------------------------------------------------------

def _escalated(store):
    ob, rec, P, t = setup(store)
    item = {"class": "product_defect", "check_id": "au.gate",
            "text": "inventory serializer drops the last slot"}
    for a in (1, 2, 3):
        rec.record_findings(P, t, a, [item])
    store.update_task("game", "UI-01", attempt=4)
    a = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws)
    assert ob.decision(a["decision"])["status"] == "open"
    return ob, rec, P, item, a["decision"]


def _answer(ob, did, value):
    ob.answer(did, value, "victor", "operator", channel="control")


def test_escalation_budget_answer_proceeds_once_then_asks_anew(store):
    # review item 10: the answered decision used to be returned as blocking forever
    ob, rec, P, item, did = _escalated(store)
    _answer(ob, did, {"option": "authorize more budget"})
    a4 = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws)
    assert "decision" not in a4 and a4["escalation_decision"] == did
    # the next attempt repeats with the same strategy: a NEW decision superseding it
    rec.record_findings(P, store.task("game", "UI-01"), 4, [item])
    store.update_task("game", "UI-01", attempt=5)
    a5 = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws)
    new = ob.decision(a5["decision"])
    assert a5["decision"] != did and new["status"] == "open"
    assert new["supersedes"] == did and "attempt 5" in new["question"]
    # asking again returns the same open decision, never an answered one
    again = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws)
    assert again["decision"] == a5["decision"]
    _answer(ob, a5["decision"], {"strategy": "Rewrite the serializer's slot loop bounds"})
    assert ob.decision(did)["status"] == "superseded"
    a5b = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws)
    assert "decision" not in a5b and "slot loop" in a5b["strategy"]


def test_escalation_stop_answer_keeps_the_task_stopped(store):
    ob, rec, P, item, did = _escalated(store)
    _answer(ob, did, {"option": "stop the task", "reason": "needs a design change"})
    for _ in range(2):
        out = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws)
        assert out["stop"] is True and "decision" not in out
        assert out["decision_id"] == did and "needs a design change" in out["reason"]


def test_escalation_unusable_answer_opens_a_new_decision(store):
    ob, rec, P, item, did = _escalated(store)
    _answer(ob, did, {"strategy": "Skip the au.gate check"})        # waives: refused
    out = rec.assignment(P, store.task("game", "UI-01"), obligations=ob, ws=P.ws)
    new = ob.decision(out["decision"])
    assert out["decision"] != did and new["status"] == "open" and new["supersedes"] == did
    assert any("waives_check" in r for r in out["rejected_guidance"])


@pytest.mark.parametrize("source,item,want", [
    ("check", {"check": "au.tests", "result": "FAIL", "detail": "Ran 429 cases, 3 failed"},
     "product_defect"),
    ("check", {"check": "au.tests", "result": "FAIL", "detail": "rate limit test: 503 path"},
     "product_defect"),
    ("review", {"severity": "major", "file": "src/net.lua",
                "text": "Handler maps a 503 response to success"}, "product_defect"),
    ("review", {"severity": "major", "file": "src/net.lua",
                "text": "No backoff on 429 Too Many Requests from the shop API"},
     "product_defect"),
    ("review", {"severity": "major", "file": "src/ui/Shop.lua",
                "text": "Shop renders without image caching; textures reload every frame"},
     "product_defect"),
    ("review", {"severity": "minor", "file": "src/log.lua", "text": "no logs on save failure"},
     "product_defect"),
    # still classified from the right fields
    ("review", {"error": "HTTP 503 Service Unavailable"}, "transport"),
    ("session", {"status": "ended", "error": "rate limit exceeded"}, "quota"),
    ("denial", {"category": "x", "reason": "429 Too Many Requests"}, "quota"),
    ("review", {"severity": "major", "file": "src/ui/Shop.lua",
                "text": "the gate output is not attached to the packet"}, "missing_evidence"),
    ("review", {"severity": "major", "file": "docs/evidence/UI-01/shop.png",
                "text": "screenshot is stale"}, "missing_evidence"),
    ("review", {"severity": "major", "file": "src/ui/Shop.lua", "text": "blah",
                "missing_evidence": ["gate log"]}, "missing_evidence"),
])
def test_classify_reads_quota_transport_and_evidence_only_from_their_fields(source, item, want):
    # review item 11
    assert classify(source, item, {})[0] == want


def test_product_finding_with_image_words_routes_to_product_changes():
    review = {"verdict": "CHANGES_REQUIRED", "findings": [
        {"severity": "major", "file": "src/ui/Shop.lua",
         "text": "Shop renders without image caching"}]}
    assert review_route(review, {}, {}) == "product_changes"


def test_long_findings_differing_late_do_not_collide():
    # review item 12: sorted(tokens)[:24] cut the set alphabetically
    base = " ".join(f"a{c}word" for c in "abcdefghijklmnopqrstuvwx")
    f1 = base + " zebra crash in inventory serializer"
    f2 = base + " yak overflow in combat damage calculation"
    assert fingerprint("product_defect", "c", "", f1) != fingerprint("product_defect", "c", "", f2)
    assert {"zebra", "serializer"} <= set(tokens(f1))
    assert len(tokens(f1)) > 24
    stored = [{"fingerprint": fingerprint("product_defect", "c", "", f1),
               "class": "product_defect", "check_id": "c", "path": "", "tokens": tokens(f1)}]
    # Jaccard runs on the full sets: a reworded finding (27 of 28 tokens shared) repeats
    assert match("product_defect", "c", "", base + " zebra crash in the inventory",
                 stored) == stored[0]["fingerprint"]


def test_scope_globs_do_not_cross_a_slash():
    ctx = {"scope": ["src/ui/*.lua"]}
    assert classify("review", {"severity": "major", "file": "src/ui/deep/x.lua",
                               "text": "wrong colour"}, ctx)[0] == "scope_decision"
    assert classify("review", {"severity": "major", "file": "src/ui/x.lua",
                               "text": "wrong colour"}, ctx)[0] == "product_defect"


# -- committed BUILD progress (UI-02, v0.122.5) ----------------------------------------------

ROWS0 = [["done", "1 kit"], ["open", "4 legs"], ["open", "5 screens"], ["open", "8 captures"],
         ["open", "9 close"]]
ROWS1 = [["done", "1 kit"], ["partial", "4 legs -- desktop green; c6 remains"],
         ["partial", "5 screens -- Downed done"], ["open", "8 captures"], ["open", "9 close"]]
ROWS2 = [["done", "1 kit"], ["partial", "4 legs -- desktop + c6 green; c7/c8 remain"],
         ["partial", "5 screens -- Downed done"], ["open", "8 captures"], ["open", "9 close"]]
ROWS3 = [["done", "1 kit"], ["done", "4 legs"], ["partial", "5 screens -- Downed done"],
         ["open", "8 captures"], ["open", "9 close"]]


def test_checklist_transitions_count_partial_steps_and_new_notes_only():
    from foundry_coordinator.adapters.base import checklist_mark, checklist_transitions
    assert [checklist_mark(x) for x in ("- [x] a", "- [X] a", "- [~] a -- n", "- [ ] a",
                                        "- [-] a", "* [x] a", "text")] == \
        ["done", "done", "partial", "open", None, None, None]
    assert checklist_transitions(ROWS0, ROWS1) == ["step 4: open -> partial",
                                                   "step 5: open -> partial"]
    assert checklist_transitions(ROWS1, ROWS2) == ["step 4: in-progress notes changed"]
    assert checklist_transitions(ROWS2, ROWS3) == ["step 4: partial -> done"]
    assert checklist_transitions(ROWS3, ROWS3) == []                       # no change
    assert checklist_transitions(ROWS3, ROWS2) == []                       # a regression
    edited = [["done", "1 kit (reworded)"]] + ROWS3[1:]
    assert checklist_transitions(ROWS3, edited) == []                      # a done row edit
    added = ROWS3 + [["done", "10 extra"]]
    assert checklist_transitions(ROWS3, added) == []                       # a new ticked row
    assert checklist_transitions(None, ROWS1) == [] and checklist_transitions(ROWS1, None) == []


def _same_findings(rec, P, t):
    """The live UI-02 shape: the same unfinished-BUILD findings repeat."""
    for a in (1, 2):
        rec.record_findings(P, t, a, [{"class": "missing_evidence",
                                       "check_id": "au.gate_bound_to_head",
                                       "text": "no 'GATE UI-02 PASS|FAIL' line"}])


def test_committed_build_progress_is_progress_despite_the_same_findings(store):
    ob, rec, P, t = setup(store)
    _same_findings(rec, P, t)
    # HEAD advances with in-scope work every session, findings never change
    for i, (a, b) in enumerate([(ROWS0, ROWS0), (ROWS0, ROWS1), (ROWS1, ROWS1)]):
        rec.mark_session(P, t, f"s{i}", prev_head=f"c{i}", head=f"c{i + 1}",
                         work_files=2, checklist={"before": a, "after": b})
    p = rec.progress(P, t)
    assert p["stalls"] == 0 and not p["no_progress"] and p["recommendation"] == "continue"
    # only the plan changed, but a step advanced ([~] notes) -> progress
    rec.mark_session(P, t, "s3", prev_head="c3", head="c4", work_files=0,
                     checklist={"before": ROWS1, "after": ROWS2})
    p = rec.progress(P, t)
    assert p["stalls"] == 0 and p["last_session_build"]["plan_steps"] == [
        "step 4: in-progress notes changed"]


def test_a_true_stall_still_escalates_with_the_new_columns(store):
    ob, rec, P, t = setup(store)
    _same_findings(rec, P, t)
    for i in range(3):          # no commit, no checklist change, the same findings
        rec.mark_session(P, t, f"s{i}", prev_head="c1", head="c1", work_files=0,
                         checklist={"before": ROWS1, "after": ROWS1})
    p = rec.progress(P, t)
    assert p["stalls"] == 3 and p["no_progress"] and p["recommendation"] == "decision"
    # commits that touch only the plan's NEXT line (no work file, no step change)
    # and leave the same findings are a stall too
    for i in range(3, 5):
        rec.mark_session(P, t, f"s{i}", prev_head=f"n{i}", head=f"n{i + 1}", work_files=0,
                         checklist={"before": ROWS1, "after": ROWS1})
    assert rec.progress(P, t)["stalls"] == 5


def test_marks_from_before_the_columns_are_resolved_or_judged_by_the_old_rule(store):
    ob, rec, P, t = setup(store)
    _same_findings(rec, P, t)
    for i in range(3):          # pre-v0.122.5 rows: no work_files / checklist
        rec.mark_session(P, t, f"s{i}", prev_head=f"c{i}", head=f"c{i + 1}")
    old = rec.progress(P, t)
    assert old["stalls"] == 2 and old["no_progress"]        # the old rule: same findings
    seen = []

    def resolve(m):
        seen.append(m["session_id"])
        return {"work_files": 1, "checklist": {"before": ROWS0, "after": ROWS1}}
    new = rec.progress(P, t, resolve=resolve)
    assert new["stalls"] == 0 and not new["no_progress"] and seen == ["s0", "s1", "s2"]
    # a resolver that fails leaves the old rule in force (never a crash)
    assert rec.progress(P, t, resolve=lambda m: 1 / 0)["no_progress"]


def test_a_later_idle_of_the_same_session_records_its_later_commits(store):
    """Live UI-02: a session's first idle (a helper handoff) was frozen by INSERT
    OR IGNORE, so commits it made afterwards never reached the progress marks."""
    ob, rec, P, t = setup(store)
    rec.mark_session(P, t, "s1", prev_head="c1", head="c1", actions="first turn",
                     work_files=0, checklist={"before": ROWS0, "after": ROWS0})
    rec.mark_session(P, t, "s1", prev_head="c1", head="c4", actions="later turn",
                     work_files=3, checklist={"before": ROWS1, "after": ROWS3})
    rows = store.q("SELECT * FROM progress_marks WHERE session_id='s1'")
    assert len(rows) == 1
    r = rows[0]
    assert r["prev_head"] == "c1" and r["head"] == "c4" and r["work_files"] == 3
    import json
    cl = json.loads(r["checklist"])
    assert cl["before"] == ROWS0 and cl["after"] == ROWS3          # started from ROWS0
    assert r["action_fp"] == __import__("foundry_coordinator.recovery", fromlist=["x"]) \
        .strategy_fp("first turn")                                  # the session's first
    p = rec.progress(P, t)
    assert p["stalls"] == 0 and p["last_session_build"]["committed"]


def test_progress_marks_migrate_to_the_new_columns(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.sqlite")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE progress_marks (id INTEGER PRIMARY KEY, project_id TEXT, "
                "task_id TEXT, session_id TEXT, attempt INT, prev_head TEXT, head TEXT, "
                "verified TEXT, findings_fp TEXT, action_fp TEXT, reported INT, ts REAL, "
                "UNIQUE (project_id, task_id, session_id))")
    con.execute("INSERT INTO progress_marks (project_id, task_id, session_id, attempt, "
                "prev_head, head, verified, findings_fp, action_fp, reported, ts) VALUES "
                "('game','UI-01','old',1,'a','b','[]','f','',1,0)")
    con.commit()
    con.close()
    st = Store(path)
    Recovery(st)
    cols = {r["name"] for r in st.q("PRAGMA table_info(progress_marks)")}
    assert {"work_files", "checklist"} <= cols
    r = st.q("SELECT * FROM progress_marks WHERE session_id='old'")[0]
    assert r["work_files"] is None and r["checklist"] is None       # unknown, not zero


def test_gate_leaves_deferred_evidence_to_the_merge_gate(store):
    """0.145.3: a track sprint has no Studio; the merge gate owns its Studio
    evidence, so the submission gate doesn't ask for it (GF-NAME1 looped)."""
    ob, rec, P, t = setup(store)
    P.adapter.describe = lambda tid: spec(tid)
    led = FakeLedger(current={"au.gate": GATE_PASS})
    assert rec.gate_submission(P, t, "h2", led, ob)                     # missing without deferral
    assert rec.gate_submission(P, t, "h2", led, ob, deferred=lambda kind, ref: True) == []
    out = rec.gate_submission(P, t, "h3", led, ob, deferred=lambda kind, ref: kind == "file",
                              evidence_items=[{"path": CAP, "status": "stale", "priority": 1}])
    assert CAP not in [o["item"].get("path") for o in out]


def test_merge_gate_owns_studio_evidence_only():
    from foundry_coordinator.tracks.manager import TrackManager
    owns = TrackManager.merge_gate_owns
    for ref in ("suite:GF_NAME1", "gate:GF-NAME1", "boot:server", "capture:hud", "regression:X"):
        assert owns("check", ref), ref
    for ref in ("au.lune", "done_check:GF-NAME1", "au.done_check.SCOPE", ""):
        assert not owns("check", ref), ref
    assert owns("log_marker", "docs/progress/GF-NAME1_EVIDENCE_RAW.log::GATE GF-NAME1 PASS")
    assert owns("file", "docs\progress\GF-NAME1_EVIDENCE_RAW.log")
    assert not owns("file", "src/shared/Enemies.lua")
