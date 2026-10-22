"""W1 follow-up: the four W2 hooks wired into the engine / lead flow (2026-10-22).

  * the scope check grants an allow-listed file the session changed without asking
  * a Kilo permission ask is rejected by the coordinator, never a person
  * the startup overlay applies the cached router revision before objects are built
  * the lead never self-approves a test it changed itself: an expectation_change decision
"""

from __future__ import annotations

import json

from foundry_coordinator import states as S
from tests.coordinator.helpers import coordinator, make_repo, run_until, write_profile
from tests.coordinator.test_lead_e2e import TASK, TID, UI_OVERRIDE, E2EClient, E2EFoundry, lead_toml
from tests.coordinator.test_revamp_lead import blocked, events
from tests.coordinator.test_w1_nohuman import T1, _state, report


# ======================================================================= scope allow-list

def test_scope_check_grants_an_allow_listed_file_the_session_changed(tmp_path):
    """W2.5 wired: a changed file the scope_auto_approve allow-list covers (here the
    GateRunner EXPECTED_BOOT row) is granted at verification instead of failing the
    scope check; its rule is still checked (grant_checks)."""
    from tests.coordinator.test_revamp_scope import GATE_RUNNER

    def agent(ctx):
        return {"writes": {"src/a.txt": "OK", "tests/GateRunner.lua":
                           GATE_RUNNER.replace("client = 22", "client = 23")},
                "commit": "T1: work plus the boot row", "final": report()}
    root, _ = make_repo(tmp_path, "game", [T1], extra_files={"tests/GateRunner.lua": GATE_RUNNER})
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": agent})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE), \
        [(h["from_state"], h["to_state"], h["evidence"].get("findings")) for h in
         c.store.history("game", "T1")]
    t = c.store.task("game", "T1")
    [g] = t["data"]["scope_grants"]
    assert g["path"] == "tests/GateRunner.lua" and g["actor"] == "policy:scope_auto_approve"
    assert g["constraints"] == {"blocks": ["EXPECTED_BOOT", "MOUNTED_TESTS"]}
    rows = c.store.q("SELECT check_name, result, detail FROM verifications WHERE task_id='T1' "
                     "AND phase='verify' ORDER BY rowid")
    scope = [r for r in rows if r["check_name"] == "coordinator.scope"]
    assert scope and all(r["result"] == "PASS" for r in scope)
    assert "granted by the allow-list" in scope[0]["detail"]
    assert any(r["check_name"] == "coordinator.scope_grant" and r["result"] == "PASS"
               for r in rows)
    assert events({"c": c}, "scope_granted")
    # the task source (tasks.json) now lists the path, committed under the prefix
    assert "tests/GateRunner.lua" in (root / "tasks.json").read_text()


def test_scope_check_still_fails_a_change_outside_the_allow_listed_block(tmp_path):
    from tests.coordinator.test_revamp_scope import GATE_RUNNER

    def agent(ctx):
        return {"writes": {"src/a.txt": "OK", "tests/GateRunner.lua":
                           GATE_RUNNER.replace("GateRunner.TIMEOUT = 30",
                                               "GateRunner.TIMEOUT = 99")},
                "commit": "T1: timeout", "final": report()}
    root, _ = make_repo(tmp_path, "game", [T1], extra_files={"tests/GateRunner.lua": GATE_RUNNER})
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": agent})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.CHANGES_REQUIRED)
    findings = " ".join(c.store.task("game", "T1")["data"]["findings"])
    assert "scope_grant" in findings and "outside its grant" in findings


# ======================================================================= permission asks

def test_permission_asks_are_rejected_by_the_coordinator(tmp_path):
    seen = []

    def agent(ctx):
        if not ctx.get("answers"):
            return {"question": {"text": "permission edit for .kilo/tally/r1.txt",
                                 "kind": "permission"}}
        seen.append(ctx["answers"])
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: work", "final": report()}
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": agent})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    assert not any(h["to_state"] == S.AWAITING_HUMAN for h in c.store.history("game", "T1"))
    assert seen == [["reject", "not allowed here; report BLOCKED with options if you need it"]]
    [ev] = events({"c": c}, "permission_auto_rejected")
    assert "tally" in ev["text"] and ev["phase"] == "implement"


def test_a_rejected_turn_without_a_report_is_continued_once(tmp_path):
    from foundry_coordinator.adapters.base import SessionState
    from foundry_coordinator.adapters.scripted import ScriptedClient
    prompts = []

    def agent(ctx):
        prompts.append(ctx["prompt"])
        if len(prompts) == 1:
            return {"question": {"text": "edit src/a.txt?", "kind": "permission"}}
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: finish", "final": report()}

    class RejectedWrite(ScriptedClient):
        def answer(self, sid, request_id, answers, *, directory, kind="question"):
            self.answers[sid] = answers
            self.sessions[sid]["pending"] = False
            self.sessions[sid]["state"] = SessionState(status="idle")   # turn ended, no report
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": RejectedWrite(agent, project_id="game")})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    assert len(prompts) == 2 and "The permission was rejected" in prompts[1]
    assert events({"c": c}, "permission_reply_continued")
    assert c.store.task("game", "T1")["sessions"] == 1


def test_a_failed_auto_reject_falls_back_to_a_person(tmp_path):
    from foundry_coordinator.adapters.scripted import ScriptedClient

    class Refusing(ScriptedClient):
        def answer(self, sid, request_id, answers, *, directory, kind="question"):
            if answers and answers[0] == "reject":
                raise RuntimeError("HTTP 500")
            return super().answer(sid, request_id, answers, directory=directory, kind=kind)

    def agent(ctx):
        if not ctx.get("answers"):
            return {"question": {"text": "edit src/a.txt?", "kind": "permission"}}
        return {"writes": {"src/a.txt": "OK"}, "commit": "T1: work", "final": report()}
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": Refusing(agent, project_id="game")})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.AWAITING_HUMAN)
    c.answer("game", "T1", ["once"])
    assert run_until(c, lambda: _state(c) == S.COMPLETE)


def test_kilo_permission_reply_carries_the_message():
    from foundry_coordinator.adapters.kilo import KiloClient
    k = KiloClient("http://kilo.invalid")
    sent = []
    k._req = lambda method, path, body=None, query=None, ok404=False: sent.append((path, body))
    k.answer("s1", "per_1", ["reject", "not allowed here"], directory="d", kind="permission")
    k.answer("s1", "per_2", ["once"], directory="d", kind="permission")
    assert sent == [("/permission/per_1/reply", {"reply": "reject", "message": "not allowed here"}),
                    ("/permission/per_2/reply", {"reply": "once"})]


# ======================================================================= startup overlay

def test_startup_overlay_applies_the_cached_revision_before_objects_are_built(tmp_path):
    import tomllib
    from types import SimpleNamespace as NS
    from foundry_automation import document as D
    from foundry_coordinator import cli
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root)
    state = tmp_path / "state"
    from foundry_coordinator.adapters.scripted import ScriptedClient
    args = NS(state=str(state), profiles=[str(prof)], max_writers=1)
    clients = {"game": ScriptedClient(lambda ctx: None, project_id="game")}
    assert cli.build(args, clients=clients).projects["game"].profile.limit(
        "max_attempts", 0) == 3
    raw = tomllib.loads(prof.read_text(encoding="utf-8"))
    doc, _, _ = D.import_profile(raw, "game")
    doc["settings"]["limits"]["max_attempts"] = 7
    assert D.validate(doc, "game")["ok"]
    (state / "config").mkdir(parents=True, exist_ok=True)
    (state / "config" / "game.json").write_text(json.dumps({"rev": 3, "doc": doc}))
    P = cli.build(args, clients=clients).projects["game"]
    assert P.profile.limit("max_attempts", 0) == 7                # applied at startup
    assert P.profile.workspace == str(root)                       # local keys kept


# ======================================================================= lead self-approval

def test_lead_self_approval_of_its_own_test_change_is_an_operator_decision(tmp_path):
    """W2.3 wired: with write access the lead's report is validated against the tests
    it changed itself; and the approve_test_change op opens an expectation_change
    decision (operator-only: shown with a recommendation, never applied by a
    timeout) instead of the lead answering its own lead_test_change."""
    from foundry_coordinator.lead import validate_report
    rep = {"classification": "test_harness", "hypothesis": "The assertion was stale; fixed.",
           "evidence_refs": ["tests/test_a.py:3"],
           "action": {"kind": "operate", "then": "reverify", "operations": [
               {"op": "approve_test_change", "path": "tests/test_a.py",
                "reason": "the expectation was stale after the fix"}]}}
    clean, errs = validate_report(rep, full_authority=True, acceptance_globs=["tests/*"],
                                  self_changed_tests={"tests/test_a.py": ["assert changed"]})
    assert clean is None and any("itself in this session" in e for e in errs)
    assert validate_report(rep, full_authority=True, acceptance_globs=["tests/*"])[0]
    # the engine op, when the lead's own session changed the file
    root, _ = make_repo(tmp_path, "game", [TASK], extra_files={
        "src/a.txt": "start\n", "tests/test_a.py": "def test_a():\n    assert 1 == 1\n"})
    prof = write_profile(tmp_path, "game", root, extra=UI_OVERRIDE + lead_toml(
        write_access=True, full_authority=True))
    c = coordinator(tmp_path, [prof], {"game": E2EClient(lambda ctx: blocked("x"))},
                    foundry={"game": E2EFoundry()},
                    store_path=tmp_path / "state" / "coordinator.sqlite")
    P = c.projects["game"]
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: _state(c, TID) == S.BLOCKED, limit=20)
    t = c.store.task("game", TID)
    case = c.lead.active_case("game", TID) or c.lead.open_case(
        P, t, {"state": S.BLOCKED, "reason": "x", "class": "session_blocked"},
        {"revision": P.ws.head()})
    (root / "tests" / "test_a.py").write_text("def test_a():\n    assert 1 == 2\n")
    ok, result, val = c._lead_op(P, t, case, {"op": "approve_test_change",
                                              "path": "tests/test_a.py",
                                              "reason": "the expectation was stale"},
                                 P.adapter.describe(TID), c.lead.settings(P),
                                 "lead_manager:" + case["case_id"])
    assert ok is False and "expectation_change" in result
    d = c.obligations.decision(val["decision_id"])
    assert d["kind"] == "expectation_change" and d["status"] == "open"
    assert d["recommended"] is None and d["deadline_ts"] is None   # operator-only: no timeout
    assert d["recommendation"].startswith("reject") and d["evidence"]["self_changed"]
    assert not any(x["kind"] == "lead_test_change"
                   for x in c.obligations.all_decisions("game", TID))
    assert events({"c": c}, "lead_self_approval_refused")
