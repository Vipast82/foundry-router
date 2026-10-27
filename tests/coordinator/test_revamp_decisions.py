"""Revamp B11: defects vs decisions, a recommended option with a timeout that is
applied and journaled, and a no-progress stop only when it is real (infra
sessions don't count; an operator resolution starts a fresh count)."""

import json
from types import SimpleNamespace

import pytest

from foundry_coordinator import decision_policy as DP
from foundry_coordinator.lead import validate_report
from foundry_coordinator.obligations import DecisionRefused, Obligations
from foundry_coordinator.store import Store
from tests.coordinator.helpers import coordinator, make_repo, write_profile
from tests.coordinator.test_lead_e2e import Clock
from tests.coordinator.test_recovery import setup as rec_setup
from tests.coordinator.test_revamp_scope import GATE_RUNNER, report, req_block

P0 = SimpleNamespace(profile=SimpleNamespace(raw={}))


def test_classification():
    # W1.2: every lead escalation and obligation conflict is a decision with a default;
    # only the reviewer-access / flaky-check kinds stay defects
    assert DP.classify("obligation_conflict", {}) == "decision"
    assert DP.classify("review_access", {}) == "defect"
    assert DP.classify("lead_escalation", {"reason": "guard_violation"}) == "decision"
    assert DP.classify("lead_escalation", {"reason": "max_diagnoses"}) == "decision"
    assert DP.classify("lead_escalation", {"reason": "lead_asks"}) == "decision"
    assert DP.classify("scope_request", {}) == "decision"
    assert DP.classify("no_progress", {}) == "decision"


def test_meta_gives_a_default_only_when_one_is_safe():
    opts = ["approve: add x", "deny: keep the file list"]
    m = DP.meta(P0, "scope_request", {}, opts)
    assert m == {"decision_class": "decision", "recommended": json.dumps(opts[1]),
                 "timeout_minutes": 30}
    # no recommended option -> no timeout (it waits for the operator)
    assert DP.meta(P0, "no_progress", {}, ["stop the task"])["timeout_minutes"] == 0
    # the recommendation= text names the default too (W1.2), when it is one of the options
    m = DP.meta(P0, "obligation_conflict", {}, ["keep BUILD (default)", "operator decision"],
                recommendation="keep BUILD: captures are BUILD evidence")
    assert m == {"decision_class": "decision", "recommended": json.dumps("keep BUILD (default)"),
                 "timeout_minutes": 30}
    assert DP.meta(P0, "no_progress", {}, ["a", "b"], recommendation="something else")[
        "recommended"] is None
    assert DP.meta(P0, "review_access", {"recommended": "x"}, ["x"])["recommended"] is None
    lead = DP.meta(P0, "lead_escalation", {"reason": "lead_asks", "recommended": "layout B"},
                   ["layout A", "layout B"])
    assert lead["recommended"] == json.dumps("layout B") and lead["timeout_minutes"] == 30
    edited = SimpleNamespace(profile=SimpleNamespace(raw={"automation": {
        "decision_timeout_minutes": 5, "scope_unlisted_default": "approve"}}))
    m = DP.meta(edited, "scope_request", {}, opts)
    assert m["recommended"] == json.dumps(opts[0]) and m["timeout_minutes"] == 5


def _obs(tmp_path, meta, now):
    store = Store(str(tmp_path / "c.sqlite"))
    ob = Obligations(store, clock=lambda: now[0],
                     decision_policy=lambda p, t, k, ev, opts: meta)
    return store, ob


def test_the_timeout_authority_applies_only_the_recommended_option_after_the_deadline(tmp_path):
    now = [1000.0]
    _, ob = _obs(tmp_path, {"decision_class": "decision",
                            "recommended": json.dumps("deny: keep"), "timeout_minutes": 30}, now)
    did = ob.open_decision("game", "T1", "scope_request", "widen?",
                           options=["approve: add", "deny: keep"])
    d = ob.decision(did)
    assert d["deadline_ts"] == 1000.0 + 1800 and d["decision_class"] == "decision"
    with pytest.raises(DecisionRefused):                    # not timed out yet
        ob.answer(did, "deny: keep", DP.TIMEOUT_ACTOR, DP.TIMEOUT_AUTHORITY)
    now[0] += 1801
    with pytest.raises(DecisionRefused):                    # only the recommended option
        ob.answer(did, "approve: add", DP.TIMEOUT_ACTOR, DP.TIMEOUT_AUTHORITY)
    out = ob.answer(did, "deny: keep", DP.TIMEOUT_ACTOR, DP.TIMEOUT_AUTHORITY)
    assert out["status"] == "answered" and out["authority"] == "timeout_default"


def test_a_defect_is_never_answered_by_a_timeout(tmp_path):
    now = [1000.0]
    _, ob = _obs(tmp_path, {"decision_class": "defect", "recommended": json.dumps("x"),
                            "timeout_minutes": 30}, now)
    did = ob.open_decision("game", "T1", "obligation_conflict", "fix?", options=["x"])
    now[0] += 99999
    with pytest.raises(DecisionRefused):
        ob.answer(did, "x", DP.TIMEOUT_ACTOR, DP.TIMEOUT_AUTHORITY)


def test_an_unanswered_scope_decision_times_out_to_its_default_and_is_journaled(tmp_path):
    clock = Clock()
    prompts = []

    def script(ctx):
        prompts.append(ctx["prompt"])
        return {"final": req_block({"path": "src/b.txt", "reason": "split"}) + report("BLOCKED")}
    root, _ = make_repo(tmp_path, "game", [{"id": "T1", "title": "t", "spec": "s",
                                            "scope": ["src/a.txt"]}],
                        extra_files={"src/a.txt": "x\n", "tests/GateRunner.lua": GATE_RUNNER})
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": script}, clock=clock)
    c.approve("game", "T1", [], "victor")
    for _ in range(5):
        c.tick()
    [d] = c.obligations.active_decisions("game", "T1", "scope_request")
    assert d["status"] == "open" and d["timeout_minutes"] == 30
    clock.advance(29 * 60)
    c.tick()
    assert c.obligations.decision(d["decision_id"])["status"] == "open"
    clock.advance(2 * 60)
    c.tick()
    d2 = c.obligations.decision(d["decision_id"])
    assert d2["status"] == "answered" and d2["authority"] == "timeout_default"
    assert d2["answer"].startswith("deny")
    ev = {r["kind"]: json.loads(r["detail"]) for r in c.store.q(
        "SELECT kind, detail FROM events WHERE kind IN ('decision_timeout_applied', "
        "'operator_notice')")}
    assert ev["decision_timeout_applied"]["decision_id"] == d["decision_id"]
    assert "timed out after 30 min" in ev["operator_notice"]["text"]
    for _ in range(3):
        c.tick()
    # the default was applied, and the session is told why (2026-10-27)
    assert any("NOT granted" in p and "decision timeout" in p for p in prompts)


def test_lead_questions_may_carry_options_and_a_recommendation():
    base = {"classification": "external", "hypothesis": "The approved layout is unknown here.",
            "evidence_refs": ["docs/x.md:1"], "action": {"kind": "ask_victor"},
            "question": "Which gallery layout did you approve?"}
    clean, errs = validate_report({**base, "options": ["layout A", "layout B"],
                                   "recommended": "layout B"})
    assert not errs and clean["options"] == ["layout A", "layout B"]
    assert clean["recommended"] == "layout B"
    assert validate_report({**base, "options": ["layout A", "layout B"],
                            "recommended": "layout C"})[0] is None
    assert validate_report({**base, "options": ["only one"]})[0] is None
    # W1.2: both are required for ask_victor (the repair prompt names what is missing)
    _, errs = validate_report(base)
    assert any("needs options" in e for e in errs) and any("needs recommended" in e for e in errs)
    assert validate_report({**base, "options": ["layout A", "layout B"]})[0] is None


# ------------------------------------------------------------------ no progress

@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "c.sqlite"))


def _session(store, sid, outcome="idle", report_=None):
    store.add_session(sid, "game", "UI-01", "r", "implementer", "implement", 1, "kilo", "p")
    store.update_session(sid, status="ended", outcome=outcome,
                         data={"report": report_} if report_ else {})


def test_sessions_that_died_on_infrastructure_are_not_stalls(store):
    ob, rec, P, t = rec_setup(store)
    _session(store, "s1")
    _session(store, "s2", outcome="prompt_not_admitted")
    _session(store, "s3", report_={"status": "BLOCKED",
                                   "blocker": "Kilo did not admit the prompt (HTTP 503)"})
    for sid in ("s1", "s2", "s3"):
        rec.mark_session(P, t, sid, prev_head="h2", head="h2", actions=f"look {sid}")
    p = rec.progress(P, t)
    assert p["stalls"] == 1 and not p["no_progress"] and p["infra_sessions_skipped"] == 2


def test_an_operator_resolution_starts_a_fresh_count(store):
    ob, rec, P, t = rec_setup(store)
    rec.mark_session(P, t, "s1", prev_head="h2", head="h2", actions="a")
    store.x("UPDATE progress_marks SET ts=100 WHERE session_id='s1'")
    rec.mark_session(P, t, "s2", prev_head="h2", head="h2", actions="b")
    store.x("UPDATE progress_marks SET ts=300 WHERE session_id='s2'")
    assert rec.progress(P, t)["no_progress"]
    did = ob.open_decision("game", "UI-01", "recovery_escalation", "what now?")
    ob.answer(did, {"strategy": "try X"}, "victor", "operator", channel="control")
    store.x("UPDATE decisions SET answered_ts=200 WHERE decision_id=?", (did,))
    p = rec.progress(P, t)
    assert p["stalls"] == 1 and not p["no_progress"]


def test_the_stall_count_is_a_profile_key(store):
    ob, rec, P, t = rec_setup(store)
    P.profile.raw = {"automation": {"no_progress_sessions": 3}}
    for sid in ("s1", "s2"):
        rec.mark_session(P, t, sid, prev_head="h2", head="h2", actions=sid)
    assert not rec.progress(P, t)["no_progress"]
    rec.mark_session(P, t, "s3", prev_head="h2", head="h2", actions="s3")
    p = rec.progress(P, t)
    assert p["no_progress"] and p["recommendation"] == "consult"
