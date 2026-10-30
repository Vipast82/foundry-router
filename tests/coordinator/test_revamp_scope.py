"""Revamp B10: typed scope requests and the harness allow-list. An allow-listed
harness line is added to the task's file list, committed and the SAME session
continues; anything else becomes a decision; a granted file may only change as
its rule says. Scripted sessions and temp git repos only."""

import json
import textwrap
from types import SimpleNamespace

from foundry_coordinator import scope as SC
from foundry_coordinator import states as S
from foundry_coordinator.adapters.git import Workspace
from tests.coordinator.helpers import coordinator, git, make_repo, run_until, write_profile

GATE_RUNNER = textwrap.dedent('''\
    -- OK harness
    local GateRunner = {}
    GateRunner.TIMEOUT = 30
    GateRunner.EXPECTED_BOOT = {
        client = 22,
        server = 4,
    }
    GateRunner.MOUNTED_TESTS = {
        "S45",
    }
    return GateRunner
    ''')


def req_block(*items):
    return "```SCOPE_REQUEST\n" + json.dumps(list(items) if len(items) > 1 else items[0]) \
        + "\n```"


def report(status, **kw):
    return "```coordinator-report\n" + json.dumps({"status": status, **kw}) + "\n```"


# ------------------------------------------------------------------ parsing / policy

def test_requests_come_from_the_fence_and_the_report():
    text = "I need the boot row.\n" + req_block({"path": "tests/GateRunner.lua",
                                                 "reason": "boot 22 -> 23"},
                                                {"path": "../evil", "reason": "x"})
    got = SC.parse_requests(text, {"scope_requests": [{"path": "default.project.json"},
                                                      {"path": "tests/GateRunner.lua"}]})
    assert got == [{"path": "tests/GateRunner.lua", "reason": "boot 22 -> 23"},
                   {"path": "default.project.json", "reason": ""}]
    assert SC.parse_requests("```SCOPE_REQUEST\nnot json\n```") == []


def test_the_default_allow_list_is_the_harness_lines():
    P = SimpleNamespace(profile=SimpleNamespace(raw={}))
    assert SC.rule_for(P, "UI-02b", "tests/GateRunner.lua")["blocks"] == ["EXPECTED_BOOT",
                                                                         "MOUNTED_TESTS"]
    assert SC.rule_for(P, "UI-02b", "docs/progress/UI-02b_gate.json")
    assert SC.rule_for(P, "UI-02b", "docs/progress/UI-02a_gate.json") is None
    assert SC.rule_for(P, "UI-02b", "default.project.json")["json_paths_in_plan"] is True
    assert SC.rule_for(P, "UI-02b", "src/server/Combat.lua") is None
    edited = SimpleNamespace(profile=SimpleNamespace(raw={"automation": {
        "scope_auto_approve": [{"path": "docs/notes/*.md"}]}}))
    assert SC.rule_for(edited, "T1", "docs/notes/a.md") and \
        SC.rule_for(edited, "T1", "tests/GateRunner.lua") is None


def test_block_ranges_find_table_and_one_line_assignments():
    text = GATE_RUNNER + "GateRunner.EXPECTED_BOOT.client = 23\n"
    r = SC.block_ranges(text, ["EXPECTED_BOOT"])
    assert r[0] == (4, 7) and r[1] == (12, 12)
    assert SC.block_ranges(text, ["MOUNTED_TESTS"]) == [(8, 10)]


def _repo_with_runner(tmp_path):
    root, _ = make_repo(tmp_path, "g", [{"id": "T1", "title": "t", "spec": "s",
                                         "scope": ["src/a.txt"]}],
                        extra_files={"tests/GateRunner.lua": GATE_RUNNER,
                                     "default.project.json": json.dumps(
                                         {"name": "g", "tree": {"A": {"$path": "src/a"}}},
                                         indent=2) + "\n"})
    return root, Workspace(str(root))


def test_a_granted_runner_may_change_only_its_rows(tmp_path):
    root, ws = _repo_with_runner(tmp_path)
    base = ws.head()
    cons = {"blocks": ["EXPECTED_BOOT", "MOUNTED_TESTS"]}
    p = root / "tests" / "GateRunner.lua"
    p.write_text(GATE_RUNNER.replace("client = 22", "client = 23").replace(
        '"S45",', '"S45",\n        "UI_02b",'))
    git(root, "commit", "-qam", "T1: rows")
    assert SC.check_change(ws, "tests/GateRunner.lua", base, ws.head(), cons, []) == []
    p.write_text(p.read_text().replace("TIMEOUT = 30", "TIMEOUT = 999"))
    git(root, "commit", "-qam", "T1: sneak")
    bad = SC.check_change(ws, "tests/GateRunner.lua", base, ws.head(), cons, [])
    assert bad and all("TIMEOUT" in b for b in bad)


def test_a_rojo_mapping_only_for_files_the_plan_lists(tmp_path):
    root, ws = _repo_with_runner(tmp_path)
    base = ws.head()
    cons = {"json_paths_in_plan": True}
    d = {"name": "g", "tree": {"A": {"$path": "src/a"}, "Hud": {"$path": "src/client/Hud.lua"}}}
    (root / "default.project.json").write_text(json.dumps(d, indent=2) + "\n")
    git(root, "commit", "-qam", "T1: map")
    plan = ["src/client/Hud.lua", "default.project.json"]
    assert SC.check_change(ws, "default.project.json", base, ws.head(), cons, plan) == []
    assert SC.check_change(ws, "default.project.json", base, ws.head(), cons,
                           ["src/other.lua"])                       # not a plan file
    d["name"] = "renamed"
    (root / "default.project.json").write_text(json.dumps(d, indent=2) + "\n")
    git(root, "commit", "-qam", "T1: rename")
    assert any("not a mapping" in x for x in SC.check_change(
        ws, "default.project.json", base, ws.head(), cons, plan))


def test_a_grant_is_judged_on_the_sprints_own_commits_when_others_changed_the_file(tmp_path):
    """2026-10-30 AS-P7: the pt and mv2-2 merges added their check mounts to
    default.project.json after AS-P7's base; its rojo_mapping grant failed on their lines."""
    root, ws = _repo_with_runner(tmp_path)
    base = ws.head()
    d = json.loads((root / "default.project.json").read_text())
    d.setdefault("tree", {})["Hud"] = {"$path": "src/client/Hud.lua"}
    (root / "default.project.json").write_text(json.dumps(d, indent=2) + "\n")
    git(root, "commit", "-qam", "T1: map Hud")
    d["tree"]["Pt"] = {"$path": "tests/PT_1_check.lua"}               # another sprint's mount
    (root / "default.project.json").write_text(json.dumps(d, indent=2) + "\n")
    git(root, "commit", "-qam", "PT-1: mount the PT_1 check")
    P = SimpleNamespace(ws=ws)
    t = {"task_id": "T1", "data": {"scope_grants": [
        {"path": "default.project.json", "rule": "rojo_mapping_for_plan_files",
         "constraints": {"json_paths_in_plan": True}}]}}
    spec = SimpleNamespace(scope=["src/client/Hud.lua", "default.project.json"])
    (res,) = SC.grant_checks(P, t, spec, base, ws.head())
    assert res.result == "PASS", res.detail
    # the sprint's own out-of-plan mapping still fails
    d["tree"]["Other"] = {"$path": "src/other.lua"}
    (root / "default.project.json").write_text(json.dumps(d, indent=2) + "\n")
    git(root, "commit", "-qam", "T1: map other")
    (res,) = SC.grant_checks(P, t, spec, base, ws.head())
    assert res.result == "FAIL" and "src/other.lua" in res.detail
    assert "PT_1_check" not in res.detail


# ------------------------------------------------------------------ the engine flow

TASK = {"id": "T1", "title": "HUD", "spec": "Write src/a.txt with OK; boot count rises.",
        "scope": ["src/a.txt"]}


def env(tmp_path, script, extra=""):
    root, _ = make_repo(tmp_path, "game", [TASK], extra_files={"src/a.txt": "start\n",
                                                                "tests/GateRunner.lua": GATE_RUNNER})
    prof = write_profile(tmp_path, "game", root, extra=extra)
    c = coordinator(tmp_path, [prof], {"game": script})
    c.approve("game", "T1", [], "victor")
    return c, root


def test_an_allow_listed_harness_line_is_granted_and_the_same_session_continues(tmp_path):
    prompts = []

    def script(ctx):
        prompts.append(ctx["prompt"])
        if len(prompts) == 1:
            return {"final": req_block({"path": "tests/GateRunner.lua",
                                        "reason": "EXPECTED_BOOT client 22 -> 23"})
                    + report("BLOCKED", blocker="needs tests/GateRunner.lua")}
        return {"writes": {"src/a.txt": "OK\n",
                           "tests/GateRunner.lua": GATE_RUNNER.replace("client = 22",
                                                                       "client = 23")},
                "commit": "T1: hud + boot row", "final": report("DONE")}
    c, root = env(tmp_path, script)
    assert run_until(c, lambda: c.store.task("game", "T1")["state"] == S.COMPLETE, 60), \
        [(h["from_state"], h["to_state"]) for h in c.store.history("game", "T1")]
    t = c.store.task("game", "T1")
    # a fresh session (free) continues: a session's edit allow-list is fixed when it starts, so the granted file needs one (AS-P5 2026-10-28)
    assert t["sessions"] == 2 and t["attempt"] <= 1
    assert "added these paths to this sprint's file list" in prompts[1]
    [g] = t["data"]["scope_grants"]
    assert g["path"] == "tests/GateRunner.lua" and g["rule"] == "gate_runner_boot_and_mounted_rows"
    assert g["actor"] == "policy:scope_auto_approve" and g["file"] == "tasks.json"
    tasks = json.loads((root / "tasks.json").read_text())
    assert "tests/GateRunner.lua" in tasks[0]["scope"]
    assert "T1: scope grant tests/GateRunner.lua" in git(root, "log", "--format=%s")
    checks = {r["check_name"]: r["result"] for r in c.store.verification(
        t["verification_id"], "game", "T1")}
    assert checks["coordinator.scope_grant"] == "PASS" and checks["coordinator.scope"] == "PASS"


def test_a_granted_file_changed_outside_its_rows_fails_verification(tmp_path):
    n = []

    def script(ctx):
        n.append(1)
        if len(n) == 1:
            return {"final": req_block({"path": "tests/GateRunner.lua", "reason": "boot row"})
                    + report("BLOCKED")}
        return {"writes": {"src/a.txt": "OK\n",
                           "tests/GateRunner.lua": GATE_RUNNER.replace("TIMEOUT = 30",
                                                                       "TIMEOUT = 1")},
                "commit": f"T1: try {len(n)}", "final": report("DONE")}
    c, root = env(tmp_path, script)
    assert run_until(c, lambda: bool(c.store.q(
        "SELECT 1 FROM verifications WHERE check_name='coordinator.scope_grant' AND "
        "result='FAIL' AND detail LIKE '%TIMEOUT%'")), 40)


def test_an_unlisted_path_becomes_a_decision_and_its_answer_is_applied(tmp_path):
    prompts = []

    def script(ctx):
        prompts.append(ctx["prompt"])
        if len(prompts) == 1:
            return {"final": req_block({"path": "src/b.txt", "reason": "split the module"})
                    + report("BLOCKED")}
        return {"writes": {"src/a.txt": "OK\n", "src/b.txt": "OK\n"}, "commit": "T1: both",
                "final": report("DONE")}
    c, root = env(tmp_path, script)
    for _ in range(6):
        c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.IMPLEMENTING and len(prompts) == 1   # waiting, not stopped
    [d] = [d for d in c.obligations.all_decisions("game", "T1") if d["kind"] == "scope_request"]
    assert d["status"] == "open" and d["recommendation"].startswith("deny")
    assert "waiting for scope decision" in t["next_action"]
    c.obligations.answer(d["decision_id"], "approve: add src/b.txt to the file list", "victor",
                         "operator", channel="control")
    assert run_until(c, lambda: c.store.task("game", "T1")["state"] == S.COMPLETE, 60)
    assert c.store.task("game", "T1")["sessions"] == 2      # fresh session after the grant
    assert "src/b.txt" in json.loads((root / "tasks.json").read_text())[0]["scope"]


def test_a_denied_request_is_told_once_then_the_stop_is_ordinary(tmp_path):
    prompts = []

    def script(ctx):
        prompts.append(ctx["prompt"])
        return {"final": req_block({"path": "src/b.txt", "reason": "x"}) + report("BLOCKED")}
    c, _ = env(tmp_path, script)
    for _ in range(4):
        c.tick()
    [d] = c.obligations.active_decisions("game", "T1", "scope_request")
    c.obligations.answer(d["decision_id"], "deny: keep the file list", "victor", "operator",
                         channel="control")
    assert run_until(c, lambda: c.store.task("game", "T1")["state"] == S.BLOCKED, 30)
    told = [p for p in prompts if "Answer to your scope request(s): NOT granted" in p]
    assert len(told) == 1 and "denied by victor" in told[0]   # told once, with the reason


def test_the_au_plan_file_list_gets_the_grant_line(tmp_path):
    """AU: the grant is one line under 'Files this sprint may change', read back
    by done_check's own parser (stub here, as in test_au_review_evidence)."""
    from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
    from tests.coordinator.test_au_review_evidence import (DONE_CHECK_STUB, PLAN, PLAN_REL, T,
                                                           _profile, _write)
    ws = tmp_path / "au"
    ws.mkdir()
    _write(ws, "tools/guard/done_check.py", DONE_CHECK_STUB)
    _write(ws, PLAN_REL, PLAN)
    a = AnimalUprisingAdapter(_profile(ws), str(tmp_path / "state"))
    spec = a.describe(T)
    assert "tests/GateRunner.lua" not in spec.scope
    assert a.grant_scope(spec, "tests/GateRunner.lua", "EXPECTED_BOOT 22 -> 23") == PLAN_REL
    spec2 = a.describe(T)
    assert "tests/GateRunner.lua" in spec2.scope and set(spec.scope) < set(spec2.scope)
    text = (ws / PLAN_REL).read_text(encoding="utf-8")
    assert "- `tests/GateRunner.lua` -- EXPECTED_BOOT 22 -> 23" in text
    # the line sits inside the list, before the next section
    assert text.index("tests/GateRunner.lua") < text.index("## Checklist")
    assert a.grant_scope(spec2, "tests/GateRunner.lua", "again") == ""      # idempotent


def test_the_au_grant_works_on_a_plan_with_mixed_line_endings(tmp_path):
    """2026-10-29 GF-SAVE1: a mostly-LF plan with a few CRLF lines; the grant split it on
    CRLF, never found the heading and every scope grant failed."""
    from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
    from tests.coordinator.test_au_review_evidence import (DONE_CHECK_STUB, PLAN, PLAN_REL, T,
                                                           _profile, _write)
    ws = tmp_path / "au"
    ws.mkdir()
    _write(ws, "tools/guard/done_check.py", DONE_CHECK_STUB)
    lines = PLAN.replace("\r\n", "\n").split("\n")
    lines[1] = lines[1] + "\r"                               # one CRLF line, the rest LF
    (ws / PLAN_REL).parent.mkdir(parents=True, exist_ok=True)
    (ws / PLAN_REL).write_bytes("\n".join(lines).encode("utf-8"))
    a = AnimalUprisingAdapter(_profile(ws), str(tmp_path / "state"))
    spec = a.describe(T)
    assert a.grant_scope(spec, "docs/localization/LocalizationTable.csv", "regenerated") == \
        PLAN_REL
    raw = (ws / PLAN_REL).read_bytes()
    assert b"- `docs/localization/LocalizationTable.csv` -- regenerated\n" in raw
    assert raw.count(b"\r\n") == 1                           # the other lines are untouched
    assert "docs/localization/LocalizationTable.csv" in a.describe(T).scope


def test_the_always_approved_classes_are_on_the_allow_list():
    """2026-10-29 permissions review: 79 of 80 scope decisions in 3 days were approved,
    mostly after the timeout. Those classes are granted at once; code still asks."""
    P = SimpleNamespace(profile=SimpleNamespace(raw={}))
    yes = {"HK-STORE1": ["docs/progress/HK-STORE1_PLAN_HISTORY.md",
                         "tests/checks/HK_STORE1_check.lua", "tests/HK_STORE1_check.lua",
                         "tests/RegressionIndex.lua", "docs/localization/LocalizationTable.csv",
                         "docs/localization/LocalizationTable.json",
                         "assets/source/vfx/VFX-LOWHP/checklist.md"],
           "AS-R7": ["docs/progress/AS-P7_PLAN.md"]}
    for tid, paths in yes.items():
        for p in paths:
            assert SC.rule_for(P, tid, p), (tid, p)
    no = ["src/server/Services/MiniBossService.lua", "tests/S24_check.lua",
          "tests/checks/OTHER_check.lua", "docs/progress/AS-P5_EVIDENCE_RAW.log",
          "tests/lune/specs/LocalizationExport.spec.luau", "tools/gate.ps1",
          "docs/design/03-technical-architecture.md"]
    for p in no:
        assert SC.rule_for(P, "HK-STORE1", p) is None, p
    assert SC.rule_pattern({"path": "tests/checks/{task_}_check.lua"}, "GF-SEC1") == \
        "tests/checks/GF_SEC1_check.lua"
