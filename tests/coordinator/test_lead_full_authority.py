"""Lead manager with full delegated recovery authority (v0.125, the default when
the lead is enabled): the read-only lead directs `operate` actions that the
coordinator executes, journals and verifies as the single writer; an exhausted
counter becomes a journaled delegated round that needs a materially different
action; a new concrete failure gets its own repair opportunity; Victor is
asked only for input that needs him personally. Fakes only, as test_lead_e2e."""

from __future__ import annotations

import json
import os

from foundry_coordinator import states as S
from foundry_coordinator.lead import action_fp, validate_report
from foundry_coordinator.obligations import DecisionRefused
from tests.coordinator.helpers import run_until
from tests.coordinator.test_lead_e2e import (DONE, TID, acts, hist, lead_reply, lead_toml,
                                             make_game, state, task, transitions)

SCRATCH = ".plan/eFullVerify_call.lua"


# like Animal Uprising's done_check CLEAN: any stray scratch file fails every run
CLEAN_CHECKS = f'''
[[verify.checks]]
name = "project-check"
command = ["{{python}}", "verify.py"]
pass_regex = "^ALL PASS$"

[[verify.checks]]
name = "done-clean"
command = ["{{python}}", "-c", "import os; print('FAIL: CLEAN {SCRATCH}' if os.path.exists('{SCRATCH}') else 'ALL PASS')"]
pass_regex = "^ALL PASS$"
'''


def full_toml(tmp, **kw):
    """Full authority, with a stand-in Foundry-Router checkout: the lead's
    guard snapshot must not depend on the real checkout other tests touch."""
    from tests.coordinator.test_lead_e2e import make_router
    return lead_toml(full_authority=True, router_repo=str(make_router(tmp)), **kw)


def scratch_primary(log, scratch=SCRATCH):
    """Writes good, committed task work; the first session also leaves an
    untracked generated scratch file outside the task scope (the UI-02 case)."""
    def script(ctx):
        log.append(ctx)
        if len(log) == 1 and scratch:
            p = os.path.join(ctx["workspace"], scratch)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write("-- generated verification call\n")
        return {"writes": {"src/a.txt": f"OK attempt {len(log)}\n"},
                "commit": f"{TID}: attempt {len(log)}", "final": DONE}
    return script


def operate_clean(paths=(SCRATCH,), then="reverify", **extra):
    return {"classification": "environment",
            "hypothesis": "A generated scratch file outside the task scope fails the "
                          "unrelated-files check; the committed task work itself is fine.",
            "evidence_refs": ["coordinator.unrelated_preserved"],
            "action": {"kind": "operate", "then": then, "operations": [
                {"op": "clean_untracked", "paths": list(paths),
                 "reason": "generated verification scratch, never part of the sprint"}],
                **extra}}


def decisions(e, kind=None):
    return [d for d in e["c"].obligations.all_decisions("game", TID)
            if kind is None or d["kind"] == kind]


def seed_exhausted_case(e):
    """An earlier, resolved case of a different blocker that uses up
    max_cases_per_task = 1 (the UI-02 history)."""
    c = e["c"]
    t = task(e)
    old = c.lead.open_case(e["P"], t, {"state": "BLOCKED", "reason": "older blocker",
                                       "class": "check_failed"}, {"failed_checks": ["au.old"]})
    c.lead.resolve(old["case_id"], "resolved earlier")
    return old


# ------------------------------------------------------------------ the UI-02 case

def test_scratch_file_block_is_resolved_by_the_lead_without_victor(tmp_path):
    """The verification failure is a new untracked scratch file outside the
    scope while earlier cases already used the task's case limit: the new
    failure still gets a case, the lead (read-only) directs clean_untracked, the
    coordinator backs the file up, removes it, journals and re-verifies, and the
    sprint completes through the normal gates. No question reaches Victor."""
    log = []
    e = make_game(tmp_path, scratch_primary(log),
                  lead=lambda ctx: lead_reply(operate_clean()),
                  extra=full_toml(tmp_path, max_cases_per_task=1), checks=CLEAN_CHECKS)
    c = e["c"]
    c.approve("game", TID, [], "victor")
    seed_exhausted_case(e)
    assert run_until(c, lambda: state(e) == S.BLOCKED and
                     (task(e)["data"] or {}).get("recovery_case"), limit=40), hist(e)
    cid = task(e)["data"]["recovery_case"]
    assert "new_failure" in [a["kind"] for a in acts(e, cid)]
    assert run_until(c, lambda: state(e) == S.COMPLETE, limit=60), hist(e)
    assert not (e["root"] / SCRATCH).exists()
    k = c.lead.case(cid)
    assert k["state"] == "RESOLVED"
    op = next(a for a in acts(e, cid) if a["kind"] == "lead_op")
    assert op["status"] == "done" and op["actor"] == f"lead_manager:{cid}"
    assert op["revision"] and op["scope_files"] == [SCRATCH]
    removed = op["detail"]["validation"]["removed"][0]
    assert removed["path"] == SCRATCH and os.path.exists(removed["backup"])
    assert op["detail"]["validation"]["still_present"] == []
    assert "operate_done" in [a["kind"] for a in acts(e, cid)]
    resume = next(h for h in transitions(e) if h["evidence"].get("recovery_case") == cid
                  and h["from_state"] == S.BLOCKED)
    assert resume["to_state"] == S.VERIFYING and resume["evidence"]["action"] == "reverify"
    assert not decisions(e, "lead_escalation")
    # the lead session itself stayed read-only: the coordinator did the write
    from tests.coordinator.test_lead_e2e import assert_lead_sessions_read_only
    assert_lead_sessions_read_only(e)
    # truthful verification: the normal checks and review decided
    assert c.store.reviews("game", TID)[-1]["verdict"] == "APPROVED"


def test_refused_clean_sends_the_case_back_for_a_different_action(tmp_path):
    """A tracked, in-scope file is never removed: the operation fails (journaled),
    nothing is deleted and the case returns to DIAGNOSING, where the same action
    is refused as a repeat."""
    log = []
    replies = [lead_reply(operate_clean(paths=["src/a.txt"]))]
    e = make_game(tmp_path, scratch_primary(log),
                  lead=lambda ctx: replies.pop(0) if replies else {"final": "thinking"},
                  extra=full_toml(tmp_path), checks=CLEAN_CHECKS)
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: any(a["kind"] == "operate_failed" for k in
                                    c.lead.cases("game", TID) for a in acts(e, k["case_id"])),
                     limit=60), hist(e)
    k = c.lead.cases("game", TID)[-1]
    op = next(a for a in acts(e, k["case_id"]) if a["kind"] == "lead_op")
    assert op["status"] == "failed" and "refused" in op["result"]
    assert (e["root"] / "src" / "a.txt").exists()
    assert c.lead.case(k["case_id"])["state"] == "DIAGNOSING"
    prior = c.lead._prior_actions(e["P"], c.lead.case(k["case_id"]))
    assert action_fp(operate_clean(paths=["src/a.txt"])["action"]) in prior


# ------------------------------------------------------------------ operations

def _blocked_case(tmp_path, **kw):
    e = make_game(tmp_path, scratch_primary([], scratch=""),
                  lead=lambda ctx: {"final": "thinking"}, extra=full_toml(tmp_path, **kw))
    c = e["c"]
    c.approve("game", TID, [], "victor")
    c.store.update_task("game", TID, state=S.BLOCKED, blocker="x", run_id="run-1")
    case = c.lead.open_case(e["P"], task(e), {"state": "BLOCKED", "reason": "x",
                                              "class": "check_failed"},
                            {"failed_checks": ["au.x"]})
    c.lead._save(case["case_id"], state="RESUMING", proposed_action={"kind": "operate"})
    return e, c, c.lead.case(case["case_id"])


def test_operations_are_journaled_and_idempotent_after_a_crash(tmp_path):
    e, c, case = _blocked_case(tmp_path)
    cid = case["case_id"]
    p = e["root"] / "tmp" / "scratch.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("scratch\n")
    did = c.obligations.open_decision("game", TID, "no_progress", "no progress", blocks=[TID])
    act = {"kind": "operate", "then": "resume", "operations": [
        {"op": "clean_untracked", "paths": ["tmp/scratch.txt"], "reason": "generated scratch"},
        {"op": "extend_budget", "attempts": 2, "reason": "a new blocker needs two attempts"},
        {"op": "resolve_decision", "decision_id": did, "resolution": "continue the BUILD steps",
         "reason": "routine recovery question"},
        {"op": "raise_bound", "bound": "max_diagnoses", "by": 1, "reason": "one more round"}]}
    # a crash after the first operation: it is recorded done, the rest never ran
    real = c._lead_op
    calls = []

    def crash_after_first(*a, **kw):
        calls.append(a[3]["op"])
        if len(calls) == 2:
            raise KeyboardInterrupt("crash")
        return real(*a, **kw)
    c._lead_op = crash_after_first
    try:
        c._lead_operate(e["P"], task(e), case, act)
    except KeyboardInterrupt:
        pass
    c._lead_op = real
    first = c.lead._action(f"{cid}:op:0")
    assert first["status"] == "done" and not p.exists()
    assert c.lead._action(f"{cid}:op:1")["status"] == "started"
    c._lead_operate(e["P"], task(e), c.lead.case(cid), act)       # resumes after the restart
    ops = [a for a in acts(e, cid) if a["kind"] == "lead_op"]
    assert [a["status"] for a in ops] == ["done"] * 4
    assert all(a["actor"] == f"lead_manager:{cid}" and a["revision"] for a in ops)
    grant = c.lead._action(f"{cid}:op:1")["detail"]["validation"]
    if c._lc(e["P"], "obligations"):
        rows = c.store.q("SELECT counter, delta FROM budget_ledger WHERE decision_id=?",
                         (f"unblock:lead:{cid}",))
        assert {r["counter"] for r in rows} == {"authorized:attempt_ordinary",
                                                 "authorized:sessions"}
        assert all(r["delta"] == 2 for r in rows)
    else:                                   # no lifecycle ledger: the task's grant allowance
        assert grant["counters"] == ["lead_grants"]
        assert task(e)["data"]["lead_grants"][cid] == 2
    d = c.obligations.decision(did)
    assert d["status"] == "answered" and d["authority"] == "lead_policy"
    assert d["actor"] == f"lead_manager:{cid}"
    assert c.lead.case(cid)["validation"]["raised"]["max_diagnoses"] == 1
    assert state(e) == S.READY and c.lead.case(cid)["state"] == "RESOLVED"


def test_reserved_decisions_and_foreign_actors_are_refused(tmp_path):
    e, c, case = _blocked_case(tmp_path)
    cid = case["case_id"]
    scope = c.obligations.open_decision("game", TID, "scope_change", "widen?", blocks=[TID])
    act = {"kind": "operate", "then": "resume", "operations": [
        {"op": "resolve_decision", "decision_id": scope, "resolution": "widen the scope",
         "reason": "the lead wants more files"}]}
    c._lead_operate(e["P"], task(e), case, act)
    assert c.obligations.decision(scope)["status"] == "open"
    assert c.lead.case(cid)["state"] == "DIAGNOSING"
    np = c.obligations.open_decision("game", TID, "no_progress", "stall", blocks=[TID])
    for actor in ("lead_manager:rc-unknown", "victor", f"model:lead/{cid}"):
        try:
            c.obligations.answer(np, {"strategy": "x"}, actor, "lead_policy")
            raise AssertionError(f"{actor} answered")
        except DecisionRefused:
            pass


def test_single_writer_operations_wait_for_the_workspace(tmp_path):
    e, c, case = _blocked_case(tmp_path)
    p = e["root"] / "tmp" / "scratch.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("scratch\n")
    lock = c._workspace_locks(e["P"])
    assert c.store.acquire(lock, "other", "T9") is None             # another writer holds it
    act = {"kind": "operate", "then": "resume", "operations": [
        {"op": "clean_untracked", "paths": ["tmp/scratch.txt"], "reason": "generated scratch"}]}
    c._lead_operate(e["P"], task(e), case, act)
    assert p.exists() and not [a for a in acts(e, case["case_id"]) if a["kind"] == "lead_op"]
    c.store.release("other", "T9", lock)
    c._lead_operate(e["P"], task(e), case, act)
    assert not p.exists()


def test_test_change_approval_is_a_recorded_decision(tmp_path):
    e, c, case = _blocked_case(tmp_path)
    cid = case["case_id"]
    act = {"kind": "operate", "then": "resume", "operations": [
        {"op": "approve_test_change", "path": "tests/test_gallery.py",
         "reason": "the expectation names a removed widget"}]}
    c._lead_operate(e["P"], task(e), case, act)
    d = [x for x in decisions(e, "lead_test_change")][0]
    assert d["status"] == "answered" and d["authority"] == "lead_policy"
    assert "tests/test_gallery.py" in c.lead.case(cid)["validation"]["approved_test_changes"]


# ------------------------------------------------------------------ bounds and decisions

def test_exhausted_counter_is_a_delegated_round_not_a_question(tmp_path):
    """max_diagnoses reached on invalid reports: the lead journals a delegated
    round and continues; a valid, different action then resolves the case."""
    good = lead_reply({"classification": "evidence",
                       "hypothesis": "The evidence is current; only re-verification is needed.",
                       "evidence_refs": ["verify.py:1"], "action": {"kind": "reverify"}})
    replies = [{"final": "no report"}, {"final": "no report"}, good]
    log = []
    e = make_game(tmp_path, scratch_primary(log, scratch=""),
                  lead=lambda ctx: replies.pop(0) if replies else {"final": "thinking"},
                  extra=full_toml(tmp_path, max_diagnoses=2))
    c = e["c"]
    c.approve("game", TID, [], "victor")
    c.store.update_task("game", TID, state=S.BLOCKED, blocker="x", run_id="run-1")
    case = c.lead.open_case(e["P"], task(e), {"state": "BLOCKED", "reason": "x",
                                              "class": "check_failed"},
                            {"failed_checks": ["au.x"]})
    for _ in range(30):
        case = c.lead.step(e["P"], task(e), case)
        if case["state"] != "DIAGNOSING":
            break
    assert case["state"] == "RESUMING", [(a["kind"], a["result"]) for a in acts(e, case["case_id"])]
    rounds = [a for a in acts(e, case["case_id"]) if a["kind"] == "delegated_round"]
    assert len(rounds) == 1 and rounds[0]["actor"] == "lead_manager"
    assert "max_diagnoses" in rounds[0]["result"]
    assert not decisions(e, "lead_escalation")


def test_runaway_guard_asks_victor_with_facts_not_a_counter(tmp_path):
    e = make_game(tmp_path, scratch_primary([], scratch=""),
                  lead=lambda ctx: {"final": "no report"},
                  extra=full_toml(tmp_path, max_diagnoses=1, max_delegated_rounds=1))
    c = e["c"]
    c.approve("game", TID, [], "victor")
    c.store.update_task("game", TID, state=S.BLOCKED, blocker="x", run_id="run-1")
    case = c.lead.open_case(e["P"], task(e), {"state": "BLOCKED", "reason": "gallery broken",
                                              "class": "check_failed"},
                            {"failed_checks": ["au.x"]})
    for _ in range(30):
        case = c.lead.step(e["P"], task(e), case)
        if case["state"] == "NEEDS_VICTOR":
            break
    assert case["state"] == "NEEDS_VICTOR"
    d = decisions(e, "lead_escalation")[0]
    assert "delegated recovery rounds" in d["question"] and "gallery broken" in d["question"]
    assert "Actions tried" in d["question"]


def test_legacy_counter_escalation_is_taken_back_by_the_lead(tmp_path):
    """rc-db6eef8a...: a case that went to Victor only because max_cases_per_task
    was reached, before full authority: the lead withdraws its own question
    (journaled) and continues with a delegated round."""
    e = make_game(tmp_path, scratch_primary([], scratch=""),
                  lead=lambda ctx: {"final": "thinking"},
                  extra=lead_toml(max_cases_per_task=1))                  # strict at first
    c = e["c"]
    c.approve("game", TID, [], "victor")
    seed_exhausted_case(e)
    c.store.update_task("game", TID, state=S.BLOCKED, blocker="x", run_id="run-1")
    case = c.lead.open_case(e["P"], task(e), {"state": "BLOCKED", "reason": "x",
                                              "class": "check_failed"},
                            {"failed_checks": ["au.old"]})
    assert case["state"] == "NEEDS_VICTOR"
    did = case["decision_id"]
    # the upgrade: same store, now with full authority
    from tests.coordinator.test_lead_e2e import restart
    e["prof"].write_text(e["prof"].read_text().replace("full_authority = false",
                                                       "full_authority = true"))
    restart(e)
    c = e["c"]
    case = c.lead.step(e["P"], task(e), c.lead.case(case["case_id"]))
    assert case["state"] == "DIAGNOSING"
    assert c.obligations.decision(did)["status"] == "withdrawn"
    kinds = [a["kind"] for a in acts(e, case["case_id"])]
    assert "withdraw_escalation" in kinds and "delegated_round" in kinds


# ------------------------------------------------------------------ report validation

REP = {"classification": "product", "hypothesis": "The gallery file lacks the OK marker.",
       "evidence_refs": ["src/a.txt:1"],
       "action": {"kind": "assign_game_repair", "assignment": "Write OK at the top of src/a.txt",
                  "files": ["src/a.txt"]}}


def test_validation_requires_materially_different_actions_and_personal_questions():
    ok, errs = validate_report(REP, scope=["src/a.txt"], acceptance_globs=["tests/*"],
                               full_authority=True)
    assert ok and not errs
    _, errs = validate_report(REP, scope=["src/a.txt"], acceptance_globs=["tests/*"],
                              full_authority=True, prior_actions=[action_fp(ok["action"])])
    assert any("materially different action" in x for x in errs)
    ask = {"classification": "product", "hypothesis": "The counter limit was reached again.",
           "evidence_refs": [], "action": {"kind": "ask_victor"},
           "question": "Should I authorize another round?"}
    _, errs = validate_report(ask, full_authority=True)
    assert any("resolves routine recovery itself" in x for x in errs)
    ask["action"]["user_action"] = "Sign in to Roblox Studio on the workstation"
    assert validate_report(ask, full_authority=True)[0]
    # operate is only for full authority; forbidden self-grants stay forbidden
    op = operate_clean()
    assert validate_report(op, acceptance_globs=["tests/*"])[1]
    assert validate_report(op, acceptance_globs=["tests/*"], full_authority=True)[0]
    bad = json.loads(json.dumps(op))
    bad["action"]["mark_pass"] = True
    assert any("forbidden keys" in x for x in
               validate_report(bad, acceptance_globs=["tests/*"], full_authority=True)[1])
    tst = json.loads(json.dumps(op))
    tst["action"]["operations"][0]["paths"] = ["tests/test_gallery.py"]
    assert any("acceptance test" in x for x in
               validate_report(tst, acceptance_globs=["tests/*"], full_authority=True)[1])


def test_operate_resume_after_repeats_reaches_one_session_without_a_re_ask(tmp_path):
    """The v0.125.0 pilot: an operate that ends in READY must not let the
    repeat guard re-ask the question the case just answered (dec-96cec...):
    its operations ride as the lead's strategy for exactly one session."""
    from tests.coordinator.test_lead_e2e import _lifecycle_game, make_router
    rep = {"classification": "evidence",
           "hypothesis": "The stall question is answered by the committed plan; the gate "
                         "recheck only needs one more bounded diagnosis budget.",
           "evidence_refs": ["docs/progress/UI-02_PLAN.md:7"],
           "action": {"kind": "operate", "then": "resume", "operations": [
               {"op": "raise_bound", "bound": "max_diagnoses", "by": 1,
                "reason": "one more bounded diagnosis if the next attempt stalls"}]}}
    c, client, ff, did, utid = _lifecycle_game(
        tmp_path, lambda ctx: lead_reply(rep),
        lead_kw={"full_authority": True, "router_repo": str(make_router(tmp_path))})
    P = c.projects["game"]
    f = {"class": "missing_evidence", "check_id": "au.gate_bound_to_head",
         "text": "gate verdicts were recorded at an older commit than HEAD"}
    for n in (1, 2, 3):
        c.recovery.record_findings(P, c.store.task("game", utid), n, [f])

    def lead_sessions():
        return [x for x in client.sessions.values() if x["metadata"].get("role") != "lead"
                and any("delegated operations" in p for p in x.get("prompts") or [])]
    assert run_until(c, lambda: lead_sessions(), limit=30),         [(d["kind"], d["status"]) for d in c.obligations.all_decisions("game", utid)]
    for _ in range(3):
        c.tick()
    assert len(lead_sessions()) == 1
    reasks = [d for d in c.obligations.all_decisions("game", utid)
              if d["kind"] == "recovery_escalation" and d["decision_id"] != did]
    assert not reasks, [(d["decision_id"], d["status"]) for d in reasks]
    assert "lead_assignment" not in (c.store.task("game", utid)["data"] or {})


def test_reverify_without_a_new_commit_resumes_the_primary_instead(tmp_path):
    e, c, case = _blocked_case(tmp_path)
    head = e["P"].ws.head()
    c.store.update_task("game", TID, base_sha=head, head_sha=head)
    p = e["root"] / "tmp" / "scratch.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("scratch\n")
    act = {"kind": "operate", "then": "reverify", "hypothesis": "only scratch was wrong",
           "operations": [{"op": "clean_untracked", "paths": ["tmp/scratch.txt"],
                           "reason": "generated scratch"}]}
    c._lead_operate(e["P"], task(e), case, act)
    done = c.lead._action(f"{case['case_id']}:operate_done")
    assert done["result"].endswith("then resume")
    assert state(e) == S.READY
    assert task(e)["data"]["lead_assignment"]["via"] == "operate"


def test_a_validated_action_proceeds_when_a_bound_is_reached_while_routing(tmp_path):
    """rc-fa9340ea...: a valid assign_game_repair arrived after the task's extra
    attempts were used; it must proceed under a delegated round, not be thrown
    away for another paid diagnosis."""
    rep = lead_reply({"classification": "evidence",
                      "hypothesis": "Three evidence files were rewritten by an unneeded gate "
                                    "rerun and left uncommitted; restoring them clears CLEAN.",
                      "evidence_refs": ["docs/progress/UI-02_EVIDENCE_RAW.log:4611"],
                      "action": {"kind": "assign_game_repair",
                                 "assignment": "Run git restore on the three uncommitted "
                                               "evidence files in src/a.txt scope, then stop.",
                                 "files": ["src/a.txt"]}})
    calls = []
    e = make_game(tmp_path, scratch_primary([], scratch=""),
                  lead=lambda ctx: calls.append(1) or rep,
                  extra=full_toml(tmp_path, max_extra_attempts_per_task=0))
    c = e["c"]
    c.approve("game", TID, [], "victor")
    c.store.update_task("game", TID, state=S.BLOCKED, blocker="x", run_id="run-1")
    case = c.lead.open_case(e["P"], task(e), {"state": "BLOCKED", "reason": "x",
                                              "class": "check_failed"},
                            {"failed_checks": ["au.x"]})
    for _ in range(20):
        case = c.lead.step(e["P"], task(e), case)
        if case["state"] != "DIAGNOSING":
            break
    assert case["state"] == "RESUMING" and len(calls) == 1
    kinds = [a["kind"] for a in acts(e, case["case_id"])]
    assert "delegated_round" in kinds and "assign_game_repair" in kinds


def test_a_prohibition_covers_its_whole_clause():
    from foundry_coordinator.lead import test_edit_directives
    ok = ("Evidence-only repair; do not change src/, tests, RegressionIndex pins or update "
          "any acceptance expectation. Then git restore the three logs and commit.")
    assert test_edit_directives(ok, ["tests/*"], []) == []
    assert test_edit_directives("Restore the logs. Then update the tests for the widget.",
                                ["tests/*"], [])


def test_prohibitions_naming_tests_or_closeout_are_not_violations():
    """rc-6e288461...: the lead's 'do not edit ...' lines were rejected as
    test edits (an acceptance path named in a prohibition) and as moving BUILD
    to CLOSE (closeout named in a prohibition, a deferral word elsewhere)."""
    from foundry_coordinator.lead import test_edit_directives
    from foundry_coordinator.obligations import directive_violations
    live = ("Evidence-only repair at current HEAD. Do not edit any src/ or tests/ file, "
            "tests/UI_02_check.lua, closeout docs or the scope. Do not create untracked "
            "files outside the scope; put scratch under .plan/tmp and leave nothing else.")
    assert directive_violations(live) == []
    assert test_edit_directives(live, ["tests/*"], []) == []
    # real instructions are still caught, also after a prohibition clause
    assert directive_violations("Do not edit src. Move the remaining gate run to the "
                                "CLOSE session.")
    assert test_edit_directives("Keep src as is. Update tests/UI_02_check.lua to expect "
                                "3 rows.", ["tests/*"], [])
    assert test_edit_directives("The known issue: update the tests for the widget.",
                                ["tests/*"], [])                 # 'known' is not 'no'
    assert directive_violations("Skip the au.gate check this time.")


def test_operator_unblock_resolution_crosses_the_repeat_guard_once(tmp_path):
    """UI-02 2026-10-19: an operator unblock (reset + written resolution) was
    re-asked by the repeat guard within seconds (dec-a687...), opening yet
    another lead case. The resolution is now the next start's strategy."""
    from tests.coordinator.test_lead_e2e import _lifecycle_game, make_router
    c, client, ff, did, utid = _lifecycle_game(
        tmp_path, lambda ctx: {"final": "thinking"},
        lead_kw={"full_authority": True, "router_repo": str(make_router(tmp_path))})
    P = c.projects["game"]
    f = {"class": "missing_evidence", "check_id": "au.gate_bound_to_head",
         "text": "gate verdicts were recorded at an older commit than HEAD"}
    for n in (1, 2, 3):
        c.recovery.record_findings(P, c.store.task("game", utid), n, [f])
    c.tick()                                  # the wait becomes a lead case first
    note = "Victor: make only the S46 setup hermetic, then one tally over all suites."
    c.unblock("game", utid, note, reset_attempts=True, actor="victor")

    def with_note():
        return [x for x in client.sessions.values() if x["metadata"].get("role") != "lead"
                and any("S46 setup hermetic" in p for p in x.get("prompts") or [])]
    assert run_until(c, lambda: with_note(), limit=20), \
        [(d["kind"], d["status"]) for d in c.obligations.all_decisions("game", utid)]
    reasks = [d for d in c.obligations.all_decisions("game", utid)
              if d["kind"] == "recovery_escalation" and d["decision_id"] != did
              and d["status"] == "open"]
    assert not reasks
    assert len(with_note()) == 1


def test_negation_after_the_verb_is_a_prohibition_too():
    """rc-5b39e448...: 'Edit nothing in src/ or tests/; do not touch
    tests/S46_check.lua' was rejected as a test edit."""
    from foundry_coordinator.lead import test_edit_directives
    live = ("UI-02 BUILD evidence repair for the S46 suite only, at current HEAD (src/tests "
            "identical to bee8500). Edit nothing in src/ or tests/; do not touch "
            "tests/S46_check.lua. Nothing in tests/ changes and neither src/ nor the specs "
            "are edited.")
    assert test_edit_directives(live, ["tests/*"], []) == []
    assert test_edit_directives("Edit the tests for the new widget.", ["tests/*"], [])
    assert test_edit_directives("Change no assertions; update the tests/S46_check.lua setup.",
                                ["tests/*"], [])
