"""AnimalUprising evidence / lifecycle adapter contract (v0.122).

Synthetic AU-like repositories only (the fixture of test_au_review_evidence),
never the real game repository. Suite ids and counts are deliberately not
UI-01's: the parsers are generic."""

import json
from types import SimpleNamespace

from foundry_coordinator.adapters.base import (FAIL, PASS, UNKNOWN, ReviewContractDefaults,
                                              VerificationResult)
from foundry_coordinator.profile import matches_any
from foundry_coordinator.profile import ApprovalRule
from foundry_coordinator.obligations import Obligations
from foundry_coordinator.store import Store
from foundry_coordinator.projects.animaluprising.adapter import (AnimalUprisingAdapter,
                                                                _fnv1a32)
from tests.coordinator.test_au_review_evidence import (LOG_REL, PLAN, PLAN_REL, T, _commit,
                                                       _log, _write, repo)  # noqa: F401

READINESS_CLASSES = {"environment", "runtime_sync", "tooling", "capability", "scope"}


def _view(tool, inp, output, *, status="completed", started=None, ended=None, part="p1",
          truncated=False):
    return {"session_id": "ses_x", "message_id": "m1", "part_id": part, "call_id": "c-" + part,
            "tool": tool, "input": inp, "status": status, "output": output,
            "truncated": truncated, "started_ts": started, "ended_ts": ended, "exit_code": None}


def _env(lines):
    """A Studio MCP result envelope: returnValue holding a JSON array string."""
    return json.dumps({"returnValue": json.dumps(lines)})


def _by(recs, check_id):
    return next(r for r in recs if r["check_id"] == check_id)


# -- classify_tool_result ---------------------------------------------------------------

def test_suite_tally_output_is_classified_with_exact_counts(repo):
    a = repo["a"]
    out = _env(["SUITE QA_07 pass=193 fail=0 expect=193 designedFails=2 VERDICT PASS",
                "PASS: GATE suite QA_07",
                "GATE regression QA_07 — pass=1 skip=0 fail=0 of 1 suite(s)",
                "PASS: GATE regression QA_07"])
    v = _view("robloxstudio-mcp_execute_luau",
              {"target": "edit", "code": 'local GR = require(game.ServerScriptService.GateRunner)'
               '\nreturn table.concat(GR.tally("QA_07", r), "\\n")'}, out)
    recs = a.classify_tool_result(v, {"task_id": T})
    s = _by(recs, "suite:QA_07")
    assert s["result"] == PASS and s["exclusion"] is None
    assert s["counts"] == {"pass": 193, "fail": 0, "expect": 193, "designed_fails": 2}
    assert s["chain_id"] == "QA_07"
    reg = _by(recs, "regression:QA_07")
    assert reg["result"] == PASS and reg["counts"] == {"pass": 1, "skip": 0, "fail": 0}
    # a failing tally is FAIL with its own numbers
    bad = a.classify_tool_result(_view(v["tool"], v["input"], _env(
        ["SUITE QA_07 pass=190 fail=3 expect=193 VERDICT FAIL", "FAIL: GATE suite QA_07"])),
        {"task_id": T})
    assert _by(bad, "suite:QA_07")["result"] == FAIL
    assert _by(bad, "suite:QA_07")["counts"]["fail"] == 3
    # disagreeing verdict lines are UNKNOWN, never the friendlier one
    odd = a.classify_tool_result(_view(v["tool"], v["input"], _env(
        ["SUITE QA_07 pass=193 fail=0 expect=193 VERDICT PASS", "FAIL: GATE suite QA_07"])),
        {"task_id": T})
    assert _by(odd, "suite:QA_07")["result"] == UNKNOWN


def test_individual_pass_lines_without_a_verdict_are_unknown(repo):
    a = repo["a"]
    lines = ["THEME scale=2", "PASS: QA_07 c1 first", "PASS: QA_07 c2 second",
             "FAIL: QA_07 c3 third"]
    step = _view("robloxstudio-mcp_eval_client_runtime",
                 {"code": 'local S = require(game:GetService("StarterPlayer"):WaitForChild('
                          '"StarterPlayerScripts"):WaitForChild("QA_07_check")); '
                          'return (S.clientFlow())'},
                 json.dumps({"ok": True, "bridge": "ok", "result": json.dumps(lines)}))
    recs = a.classify_tool_result(step, {"task_id": T})
    assert len(recs) == 1
    r = recs[0]
    assert r["check_id"] == "call:QA_07:clientFlow" and r["chain_id"] == "QA_07"
    assert r["result"] == UNKNOWN and r["counts"] == {"pass": 2, "fail": 1}
    # 300 PASS lines in plain shell output, still no verdict: UNKNOWN
    many = "\n".join(f"PASS: QA_07 c{i}" for i in range(300))
    recs = a.classify_tool_result(_view("bash", "pwsh -File tools/run_suite.ps1", many),
                                  {"task_id": T})
    assert [r["result"] for r in recs] == [UNKNOWN]
    assert recs[0]["check_id"] == "suite:QA_07" and recs[0]["counts"]["pass"] == 300
    assert not any(r["result"] == PASS for r in recs)


def _plan_view(suite="QB_3", started=10):
    code = 'local S = require(game:GetService("ServerScriptService"):WaitForChild("{0}_check")); ' \
           'return (S.{1}())'
    ccode = 'local S = require(game:GetService("StarterPlayer"):WaitForChild(' \
            '"StarterPlayerScripts"):WaitForChild("{0}_check")); return (S.{1}())'
    plan = ["NOTE boot-gate-first: CALL 1-2 first",
            "CALL 1 [vm=server] robloxstudio-mcp eval_server_runtime :: return (boot())",
            "CALL 2 [vm=client] robloxstudio-mcp eval_client_runtime :: return (boot())",
            f"CALL 3 [vm=server] robloxstudio-mcp eval_server_runtime :: "
            f"{code.format(suite, 'setupServer')}",
            f"CALL 4 [vm=server] robloxstudio-mcp eval_server_runtime :: "
            f"{code.format(suite, 'teardownServer')}",
            f"CALL 5 [vm=client] robloxstudio-mcp eval_client_runtime :: "
            f"{ccode.format(suite, 'clientCleanup')}",
            f"CALL 6 [vm=client] robloxstudio-mcp eval_client_runtime :: "
            f"{ccode.format(suite, 'clientPostTeardown')}"]
    v = _view("robloxstudio-mcp_execute_luau",
              {"target": "edit", "code": f'return require(game.ServerScriptService.GateRunner)'
                                         f'.run("{suite}")'},
              json.dumps({"returnValue": "\n".join(plan)}), started=started, ended=started + 1,
              part="plan")
    return v, code, ccode


def test_teardown_before_the_last_step_is_visible_through_chain_and_seq(repo):
    a = repo["a"]
    pv, code, ccode = _plan_view()
    plan_recs = a.classify_tool_result(pv, {"task_id": T})
    assert _by(plan_recs, "plan:QB_3")["result"] == "NA"
    assert [s["seq"] for s in _by(plan_recs, "plan:QB_3")["chain_plan"]] == [1, 2, 3, 4, 5, 6]
    out = _env(["PASS: QB_3 step ok"])
    calls = [  # executed: setup, teardown, cleanup ... and the last step (6) after teardown
        ("robloxstudio-mcp_eval_server_runtime", code.format("QB_3", "setupServer"), 100),
        ("robloxstudio-mcp_eval_server_runtime", code.format("QB_3", "teardownServer"), 200),
        ("robloxstudio-mcp_eval_client_runtime", ccode.format("QB_3", "clientPostTeardown"), 300),
        ("robloxstudio-mcp_eval_client_runtime", ccode.format("QB_3", "clientCleanup"), 400),
    ]
    recs = []
    for i, (tool, c, ts) in enumerate(calls):
        recs += a.classify_tool_result(_view(tool, {"code": c}, out, started=ts, ended=ts + 5,
                                             part=f"c{i}"), {"task_id": T})
    by_time = [r["seq"] for r in recs]
    assert by_time == [3, 4, 6, 5]                       # 6 ran before 5: an order violation
    assert all(r["chain_id"] == "QB_3" for r in recs)
    assert _by(recs, "call:QB_3:5")["prereqs"] == ["call:QB_3:4"]
    assert _by(recs, "call:QB_3:6")["prereqs"] == ["call:QB_3:5"]
    # the same plan supplied by the caller (a ledger resuming from a cursor) gives the same seq
    fresh = AnimalUprisingAdapter(a.p, a.state_dir)
    steps = _by(plan_recs, "plan:QB_3")["chain_plan"]
    r = fresh.classify_tool_result(_view(calls[1][0], {"code": calls[1][1]}, out),
                                   {"task_id": T, "chain_plans": {"QB_3": steps}})
    assert r[0]["check_id"] == "call:QB_3:4" and r[0]["seq"] == 4
    # without any plan the step is still identified, with seq unknown (not guessed)
    r = AnimalUprisingAdapter(a.p, a.state_dir).classify_tool_result(
        _view(calls[1][0], {"code": calls[1][1]}, out), {"task_id": T})
    assert r[0]["check_id"] == "call:QB_3:teardownServer" and r[0]["seq"] is None


def test_read_only_commands_over_a_log_are_excluded(repo):
    a = repo["a"]
    text = ("SUITE S45 pass=37 fail=0 expect=37 VERDICT PASS\nPASS: GATE HEAD abcdef1\n"
            f"GATE {T} PASS\n")
    for tool, cmd in (("bash", f"cat {LOG_REL}"),
                      ("bash", f"Get-Content {LOG_REL} | Select-String GATE"),
                      ("bash", f"grep -n VERDICT {LOG_REL}"),
                      ("bash", f"git show HEAD:{LOG_REL}"),
                      ("read", LOG_REL)):
        inp = {"filePath": cmd} if tool == "read" else {"command": cmd}
        recs = a.classify_tool_result(_view(tool, inp, text), {"task_id": T})
        assert recs and all(r["exclusion"] == "read_only_command" for r in recs), cmd
    # a gate run is not read-only
    recs = a.classify_tool_result(_view("bash", {"command": f"pwsh -File tools/gate.ps1 -Sprint "
                                                            f"{T} -Finish"}, text),
                                  {"task_id": T})
    gate = _by(recs, f"gate:{T}")
    assert gate["result"] == PASS and gate["exclusion"] is None and gate["gate_head"] == "abcdef1"
    # a verdict typed into the command itself is never a result
    typed = "SUITE S45 pass=37 fail=0 expect=37 VERDICT PASS"
    recs = a.classify_tool_result(_view("robloxstudio-mcp_execute_luau",
                                        {"code": f'return "{typed}"'}, _env([typed])),
                                  {"task_id": T})
    assert _by(recs, "suite:S45")["exclusion"] == "verdict_in_input"


def test_void_runtime_is_excluded(repo):
    a = repo["a"]
    stop = _view("robloxstudio-mcp_solo_playtest", {"action": "stop"}, json.dumps(
        {"success": False, "timedOut": True, "playtestTaskPending": True,
         "error": "Playtest teardown did not complete."}), status="error")
    assert a.void_runtime(stop) is True
    assert a.void_runtime(_view("robloxstudio-mcp_eval_server_runtime", {"code": "x"},
                                json.dumps({"ok": False, "bridge": "disconnected"}))) is True
    good = _view("robloxstudio-mcp_execute_luau", {"target": "edit", "code": "tally"},
                 _env(["SUITE QA_07 pass=5 fail=0 expect=5 VERDICT PASS"]))
    assert a.void_runtime(good) is False
    assert a.void_runtime(_view("bash", {"command": "ls"}, "no player in world")) is False
    recs = a.classify_tool_result(good, {"task_id": T, "runtime_void": True})
    assert _by(recs, "suite:QA_07")["exclusion"] == "void_runtime"
    assert a.classify_tool_result(good, {"task_id": T})[0]["exclusion"] is None
    # two calls overlapping in the same VM: parallel, not credited
    a1 = _view("robloxstudio-mcp_eval_client_runtime", {"code": "x"},
               _env(["SUITE QA_07 pass=5 fail=0 expect=5 VERDICT PASS"]), started=100,
               ended=200, part="a1")
    a2 = _view("robloxstudio-mcp_eval_client_runtime", {"code": "y"}, "", started=150,
               ended=160, part="a2")
    a3 = _view("robloxstudio-mcp_eval_server_runtime", {"code": "z"}, "", started=150,
               ended=160, part="a3")
    assert a.classify_tool_result(a1, {"concurrent": [a3]})[0]["exclusion"] is None
    assert a.classify_tool_result(a1, {"concurrent": [a2, a3]})[0]["exclusion"] == "parallel"
    # an errored call and a truncated one are flagged
    err = dict(good, status="error")
    assert a.classify_tool_result(err, {})[0]["exclusion"] == "tool_error"
    cut = a.classify_tool_result(dict(good, truncated=True), {})[0]
    assert cut["truncated"] is True and "truncated" in cut["note"]


def test_runtime_identity_and_per_file_match_at_the_revision(repo):
    a, w, code = repo["a"], repo["w"], repo["code"]
    kit = "src/client/Kit.lua"
    live = "%08x" % _fnv1a32(w.run("show", f"{code}:{kit}").encode())
    out = _env(["BOOT(server) loaded=12 errors=0", "PASS: BOOT counts (server — 12 loaded)",
                f"MATCH {kit} live={live} head={live}",
                "VERIFY matched=1/1 mismatched=0 unresolved=0 skipped=0"])
    v = _view("robloxstudio-mcp_execute_luau", {"code": "G.verifyLive(m)"}, out)
    ident = a.runtime_identity(v)
    assert ident["boot"]["server"] == {"loaded": 12, "errors": 0}
    assert ident["files"] == {kit: live} and ident["loaded_fp"].startswith("fnv1a32-manifest:")
    recs = a.classify_tool_result(v, {"task_id": T})
    assert _by(recs, "boot:server")["result"] == PASS
    assert _by(recs, "runtime:verify_live")["result"] == PASS
    assert _by(recs, "boot:server")["runtime_hint"]["loaded_fp"] == ident["loaded_fp"]
    spec = a.describe_at(T, code, w)
    assert a.runtime_matches(ident, spec, code, w)[0] is True
    assert a.runtime_matches(ident, spec, repo["base"], w)[0] is False   # other code at base
    assert a.runtime_matches(None, spec, code, w)[0] is None
    # not derivable offline unless the profile opts into the hash rule
    assert a.runtime_expected(spec, code) is None
    assert a.runtime_identity("plain text with nothing") is None


# -- required checks / dependencies / obligations ----------------------------------------

STALE_PLAN = PLAN.replace(
    "## Acceptance lines\n",
    "## Regression\nRun S45 and S39 on the committed code.\n\n## Acceptance lines\n") + \
    "\nNEXT: skip S45, rerun S99 later; do captures at CLOSE.\n"


def _stale_repo(repo):
    ws = repo["ws"]
    _write(ws, PLAN_REL, STALE_PLAN)
    rev = _commit(ws, f"{T}: plan with a stale NEXT line")
    return rev


def test_required_checks_come_from_the_plan_at_the_revision_not_next(repo):
    a, w = repo["a"], repo["w"]
    rev = _stale_repo(repo)
    # a dirty working-tree plan naming another suite is not the reviewed plan
    _write(repo["ws"], PLAN_REL, STALE_PLAN.replace("S45 and S39", "S45, S39 and S77"))
    spec = a.describe_at(T, rev, w)
    req = a.required_checks(spec, "BUILD")
    ids = [r["check_id"] for r in req]
    assert ids[:2] == ["boot:server", "boot:client"]
    assert {"suite:UX_01", "suite:S45", "suite:S39", f"gate:{T}"} <= set(ids)
    assert "suite:S99" not in ids and "suite:S77" not in ids
    assert {f"capture:docs/evidence/{T}/gallery_desktop.png",
            f"capture:docs/evidence/{T}/gallery_phone.png",
            f"capture:docs/evidence/{T}/size_1366x768.png"} <= set(ids)
    s45 = _by([dict(r, check_id=r["check_id"]) for r in req], "suite:S45")
    assert s45["prereqs"] == ["boot:server", "boot:client"] and s45["min_tier"] == "verified"
    gate = _by(req, f"gate:{T}")
    assert set(gate["prereqs"]) == {"suite:UX_01", "suite:S45", "suite:S39"}
    assert a.required_checks(spec, "CLOSE") == [
        {"check_id": f"done_check:{T}", "chain_id": None, "seq": None, "prereqs": [],
         "runtime": False, "min_tier": "verified"}]
    assert a.required_checks(spec, "APPROVAL") == []


def test_obligations_keep_build_captures_in_build_despite_next(repo):
    a, w = repo["a"], repo["w"]
    rev = _stale_repo(repo)
    spec = a.describe_at(T, rev, w)
    obs = a.obligations(spec)
    by = {o["obligation_id"]: o for o in obs}
    caps = [o for o in obs if o["obligation_id"].startswith("build.capture.")]
    assert {o["obligation_id"] for o in caps} == {
        f"build.capture.docs/evidence/{T}/gallery_desktop.png",
        f"build.capture.docs/evidence/{T}/gallery_phone.png",
        f"build.capture.docs/evidence/{T}/size_1366x768.png"}
    assert all(o["phase"] == "BUILD" for o in caps)
    assert by["build.gate"]["phase"] == "BUILD" and by["build.suite.S45"]["phase"] == "BUILD"
    close = [o["obligation_id"] for o in obs if o["phase"] == "CLOSE"]
    assert close == [f"close.{d['id']}" for d in a._close_duties(spec)]
    assert by["approval.visual"]["phase"] == "APPROVAL"
    assert by["approval.visual"]["status"] == "pending_human"
    accept = [o for o in obs if o["obligation_id"].startswith("build.accept.")]
    assert accept and all(o["phase"] == "BUILD" for o in accept)
    for o in obs:
        assert {"obligation_id", "phase", "title", "source", "deps", "acceptance",
                "required_evidence"} <= set(o)
        assert all(e["kind"] in ("check", "path", "blob_changed", "log_marker")
                   for e in o["required_evidence"])
    # ids are structural: reordering the plan's sections and acceptance lines keeps them
    moved = STALE_PLAN.replace(
        "Gate PASS; captures", "A2: Studio walk-through recorded.\nGate PASS; captures")
    _write(repo["ws"], PLAN_REL, moved)
    rev2 = _commit(repo["ws"], f"{T}: acceptance line added above")
    ids2 = {o["obligation_id"] for o in a.obligations(a.describe_at(T, rev2, w))}
    assert set(by) <= ids2 and "build.accept.A2" in ids2


def test_delegated_visual_review_updates_existing_inspector_label_without_waiver(repo):
    a, w = repo["a"], repo["w"]
    spec = a.describe_at(T, repo["head"], w)
    project = SimpleNamespace(id=a.p.id, adapter=a)
    store = Store(str(repo["ws"].parent / "visual-obligations.sqlite"))
    obligations = Obligations(store)
    obligations.seed(project, spec)
    old = obligations.get(a.p.id, T, "approval.visual")
    assert "Victor" in old["title"]

    a.p.raw["approval"] = {"visual_model": True}
    delegated = a.describe_at(T, repo["head"], w)
    proposed = {o["obligation_id"]: o for o in a.obligations(delegated)}
    assert proposed["approval.visual"]["status"] == "pending_model"
    assert "coordinator's vision reviewer" in delegated.spec
    obligations.seed(project, delegated)
    current = obligations.get(a.p.id, T, "approval.visual")
    assert "Coordinator vision reviewer" in current["title"]
    assert current["status"] == old["status"] == "open"
    assert current["phase"] == old["phase"] == "APPROVAL"
    assert current["required_evidence"] == old["required_evidence"]


def test_visual_obligation_needs_recorded_approval_not_just_images(repo):
    a, w = repo["a"], repo["w"]
    a.p.raw["approval"] = {"visual_model": True}
    a.p.approvals = [ApprovalRule(match="visual", kind="visual", stage="after_close",
                                artifacts=[f"docs/evidence/{T}/*.png"])]
    spec = a.describe_at(T, repo["head"], w)
    project = SimpleNamespace(id=a.p.id, adapter=a, profile=a.p, ws=w)
    store = Store(str(repo["ws"].parent / "visual-evaluation.sqlite"))
    obligations = Obligations(store)
    obligations.seed(project, spec)
    rev = repo["head"]
    first = obligations.evaluate(project, {"task_id": T}, rev, phases=("APPROVAL",))
    assert "approval.visual" in first["open"]
    arts = a.approval_artifacts(spec, a.p.approvals[0].artifacts)
    store.add_approval(a.p.id, T, "visual", rev, arts, "model:vision", "all frames seen")
    second = obligations.evaluate(project, {"task_id": T}, rev, phases=("APPROVAL",))
    assert "approval.visual" in second["open"]  # the third required frame is absent
    _write(repo["ws"], f"docs/evidence/{T}/size_1366x768.png",
           b"\x89PNG third-frame", binary=True)
    rev = _commit(repo["ws"], f"{T}: third required frame")
    spec = a.describe_at(T, rev, w)
    arts = a.approval_artifacts(spec, a.p.approvals[0].artifacts)
    store.add_approval(a.p.id, T, "visual", rev, arts, "model:vision", "all frames seen")
    third = obligations.evaluate(project, {"task_id": T}, rev, phases=("APPROVAL",))
    assert "approval.visual" in third["verified"]
    _write(repo["ws"], f"docs/evidence/{T}/gallery_phone.png", b"\x89PNG changed", binary=True)
    changed = obligations.evaluate(project, {"task_id": T}, rev, phases=("APPROVAL",))
    assert "approval.visual" in changed["regressed"]


def test_check_dependencies_docs_change_vs_src_change(repo):
    a, w, ws = repo["a"], repo["w"], repo["ws"]
    head = repo["head"]
    spec = a.describe_at(T, head, w)
    suite = a.check_dependencies(spec, "suite:S45")
    cap = a.check_dependencies(spec, f"capture:docs/evidence/{T}/gallery_desktop.png")
    assert "src/client/Kit.lua" in cap and "tests/UX_01_check.lua" in cap
    assert a.check_dependencies(spec, "mystery:thing") is None
    _write(ws, "docs/notes.md", "unrelated\n")
    docs = _commit(ws, "docs only")
    _write(ws, "src/client/Kit.lua", "return {ok = false}\n")
    src = _commit(ws, f"{T}: code change")
    docs_changed = w.changed_files(head, docs)
    src_changed = w.changed_files(docs, src)
    for deps in (suite, cap):
        assert not any(matches_any(p, deps) for p in docs_changed)
        assert any(matches_any(p, deps) for p in src_changed)
    # a capture with no UI source in the plan scope: unknown, not "no dependencies"
    bare = a.describe_at(T, head, w)
    bare.scope = [s for s in bare.scope if not s.startswith("src/")]
    assert a.check_dependencies(bare, f"capture:docs/evidence/{T}/x.png") is None


# -- readiness ----------------------------------------------------------------------------

def _res(items, name):
    return next(i for i in items if i["name"] == name)


def test_readiness_classes_and_offline_live_hook(repo):
    a, w, ws = repo["a"], repo["w"], repo["ws"]
    spec = a.describe_at(T, repo["head"], w)
    r = a.readiness(spec)
    assert all(i["cls"] in READINESS_CLASSES for i in r)
    assert _res(r, "au.plan_at_head")["ok"] is True
    assert _res(r, "au.tool.tools/gate.ps1") == {**_res(r, "au.tool.tools/gate.ps1"),
                                                  "ok": False, "cls": "tooling"}
    assert _res(r, "au.tool.tools/guard/done_check.py")["ok"] is True
    mcp = _res(r, "au.mcp.roblox_studio_configured")
    assert mcp["ok"] is False and mcp["cls"] == "environment" and mcp["blocking"]
    live = _res(r, "au.studio.live_sync")
    assert live["cls"] == "runtime_sync" and live["ok"] is None and not live["blocking"]
    # tools present + kilo.jsonc listing Roblox Studio (comments, URLs, trailing commas)
    for rel in ("tools/gate.ps1", "tests/GateRunner.lua", "tests/lune/run.luau"):
        _write(ws, rel, "-- stub\n")
    _write(ws, "kilo.jsonc", '{\n  // servers\n  "mcp": {\n    "Roblox_Studio": {"type": "local",'
                             ' "url": "http://127.0.0.1:44755/mcp"}, /* x */\n  },\n}\n')
    r = a.readiness(spec)
    assert _res(r, "au.tool.tools/gate.ps1")["ok"] is True
    assert _res(r, "au.mcp.roblox_studio_configured")["ok"] is True
    _write(ws, "kilo.jsonc", '{"mcp": {"Roblox_Studio": {"enabled": false}, "other": {}}}')
    assert _res(a.readiness(spec), "au.mcp.roblox_studio_configured")["ok"] is False
    # a live probe: disconnected -> runtime_sync failure (blocking), never product
    r = a.readiness(spec, ctx={"studio_probe": lambda s: {"connected": False,
                                                          "error": "Rojo not serving"}})
    live = _res(r, "au.studio.live_sync")
    assert live["ok"] is False and live["cls"] == "runtime_sync" and live["blocking"]
    # connected, loaded source equal to HEAD
    kit = "src/client/Kit.lua"
    h = "%08x" % _fnv1a32(w.run("show", f"HEAD:{kit}").encode())
    r = a.readiness(spec, ctx={"studio_probe": lambda s: {"connected": True,
                                                          "files": {kit: h}}})
    assert _res(r, "au.studio.live_sync")["ok"] is True
    r = a.readiness(spec, ctx={"studio_probe": lambda s: {"connected": True,
                                                          "files": {kit: "00000000"}}})
    assert _res(r, "au.studio.live_sync")["ok"] is False
    # the plan not committed at HEAD: scope
    w.run("rm", "-q", "--cached", PLAN_REL)
    w.run("commit", "-q", "--no-verify", "-m", "plan untracked (still in the working tree)")
    assert _res(a.readiness(spec), "au.plan_at_head") == {
        **_res(a.readiness(spec), "au.plan_at_head"), "ok": False, "cls": "scope"}
    assert not any(i["cls"] == "product" for i in a.readiness(spec))


# -- _gate_binding reads the log at the revision -----------------------------------------

def test_gate_binding_reads_the_log_at_the_revision_not_the_working_tree(repo):
    a, w, ws = repo["a"], repo["w"], repo["ws"]
    spec = a.describe_at(T, repo["head"], w)
    assert a._gate_binding(spec, repo["head"], w).result == PASS
    # committed: FAIL. Working tree (dirty, uncommitted): PASS. The commit decides.
    _write(ws, LOG_REL, _log(repo["code"][:10]).replace(f"GATE {T} PASS", f"GATE {T} FAIL"))
    failed = _commit(ws, f"{T}: gate failed")
    _write(ws, LOG_REL, _log(repo["code"][:10]))
    for g in (a._gate_binding(spec, failed, w), a._gate_binding(spec, failed)):
        assert g.result == FAIL and f"GATE {T} FAIL" in g.detail
    # and the reverse: committed PASS, working tree FAIL -> PASS at that commit
    _write(ws, LOG_REL, _log(repo["code"][:10]).replace(f"GATE {T} PASS", f"GATE {T} FAIL"))
    assert a._gate_binding(spec, repo["head"], w).result == PASS
    # a log that exists only in the working tree is missing at the revision
    assert a._gate_binding(spec, repo["code"], w).result == FAIL
    assert "missing at" in a._gate_binding(spec, repo["code"], w).detail


# -- generic defaults ----------------------------------------------------------------------

def test_generic_defaults_are_inert():
    class Bare(ReviewContractDefaults):
        pass
    b = Bare()
    assert b.classify_tool_result({}, {}) == [] and b.required_checks(None, "BUILD") == []
    assert b.check_dependencies(None, "suite:x") is None and b.runtime_identity("x") is None
    assert b.runtime_expected(None, "r") is None and b.void_runtime({}) is False
    assert b.obligations(None) is None and b.readiness(None) == []


# -- v0.122 review fixes -----------------------------------------------------------------

_OLD_LOG = (f"SUITE QA_07 pass=193 fail=0 expect=193 VERDICT PASS\nPASS: GATE HEAD abcdef1234\n"
            f"GATE {T} PASS\n")


def test_shell_chains_that_only_read_are_never_verdict_sources(repo):
    a = repo["a"]
    for cmd in (f"cd C:/ws && cat {LOG_REL}",
                f"Set-Location C:/ws; Get-Content {LOG_REL}",
                f'powershell -NoProfile -Command "Get-Content {LOG_REL}"',
                f'pwsh -c "& {{ Get-Content {LOG_REL} }}"',
                f'cmd /c "type {LOG_REL}"',
                f"git -C . show HEAD~3:{LOG_REL}",
                f"python -c \"print(open('{LOG_REL}').read())\"",
                f"cat tools/gate.ps1 && cat {LOG_REL}",
                # the runner ran, but an old log is read in the same chain
                f"pwsh -File tools/gate.ps1 -Sprint {T}; Get-Content {LOG_REL} -Tail 40"):
        recs = a.classify_tool_result(_view("bash", {"command": cmd}, _OLD_LOG), {"task_id": T})
        assert recs and all(r["exclusion"] == "read_only_command" for r in recs), cmd
    # the gate actually invoked (wrapped / chained / piped through a filter): credited
    for cmd in (f"cd C:/ws && pwsh -NoProfile -File tools/gate.ps1 -Sprint {T} -Finish",
                f'powershell -NoProfile -Command "& {{ ./tools/gate.ps1 -Sprint {T} }}"',
                f'cmd /c "pwsh -File tools\\gate.ps1 -Sprint {T}"',
                f"& .\\tools\\gate.ps1 -Sprint {T} | Select-String GATE"):
        recs = a.classify_tool_result(_view("bash", {"command": cmd}, _OLD_LOG), {"task_id": T})
        gate = _by(recs, f"gate:{T}")
        assert gate["result"] == PASS and gate["exclusion"] is None, cmd
    # lune run credits the lune tally only, not a Studio suite verdict in its output
    recs = a.classify_tool_result(_view("bash", {"command": "lune run tests/lune/run.luau"},
                                        "LUNE 73/73 PASS\n" + _OLD_LOG), {"task_id": T})
    assert _by(recs, "lune")["exclusion"] is None
    assert _by(recs, "suite:QA_07")["exclusion"] == "read_only_command"
    # any other tool (a subagent's report, an edit) is not a verdict source
    recs = a.classify_tool_result(_view("task", {"prompt": "summarise"}, _OLD_LOG), {"task_id": T})
    assert recs and all(r["exclusion"] == "not_verdict_source" for r in recs)


def test_verdicts_built_by_concatenation_in_the_input_are_excluded(repo):
    a = repo["a"]
    line = "SUITE QA_07 pass=5 fail=0 expect=5 VERDICT PASS"
    for code in ('local n = 5 return "SUITE " .. "QA_07" .. " pass=" .. n .. " fail=0 expect="'
                 ' .. n .. " VERDICT " .. "PASS"',
                 "return string.format('%s QA_07 pass=%d fail=0 expect=%d VERDICT %s', 'SUITE',"
                 " 5, 5, 'PASS')"):
        recs = a.classify_tool_result(_view("robloxstudio-mcp_execute_luau", {"code": code},
                                            _env([line])), {"task_id": T})
        assert _by(recs, "suite:QA_07")["exclusion"] == "verdict_in_input", code
    recs = a.classify_tool_result(
        _view("robloxstudio-mcp_execute_luau", {"code": 'return "GATE " .. "' + T + '" .. " PASS"'},
              _env([f"GATE {T} PASS"])), {"task_id": T})
    assert _by(recs, f"gate:{T}")["exclusion"] == "verdict_in_input"
    # the real tally call (no verdict words in its literals) stays credited
    recs = a.classify_tool_result(
        _view("robloxstudio-mcp_execute_luau",
              {"code": 'local GR = require(game.ServerScriptService.GateRunner)\n'
                       'return table.concat(GR.tally("QA_07", r), "\\n")'}, _env([line])),
        {"task_id": T})
    assert _by(recs, "suite:QA_07")["exclusion"] is None


def test_a_pass_contradicted_by_its_own_counts_is_unknown(repo):
    a = repo["a"]

    def one(lines, cid):
        return _by(a.classify_tool_result(_view("robloxstudio-mcp_execute_luau", {"code": "x"},
                                                _env(lines)), {"task_id": T}), cid)
    assert one(["S45 PASS 3/12"], "suite:S45")["result"] == UNKNOWN
    assert one(["S45 PASS 12/12"], "suite:S45")["result"] == PASS
    assert one(["LUNE 3/73 PASS"], "lune")["result"] == UNKNOWN
    assert one(["LUNE 73/73 PASS"], "lune")["result"] == PASS
    assert one(["SUITE QA_07 pass=190 fail=3 expect=193 VERDICT PASS"],
               "suite:QA_07")["result"] == UNKNOWN
    assert one(["SUITE QA_07 pass=190 fail=0 expect=193 VERDICT PASS"],
               "suite:QA_07")["result"] == UNKNOWN
    assert one(["SUITE QA_07 pass=190 fail=0 VERDICT PASS"], "suite:QA_07")["result"] == PASS
    assert one(["GATE regression QA_07 — pass=1 skip=0 fail=2 of 3 suite(s)",
                "PASS: GATE regression QA_07"], "regression:QA_07")["result"] == UNKNOWN
    # a FAIL is never made friendlier
    assert one(["S45 FAIL 12/12"], "suite:S45")["result"] == FAIL
    # contradicted counts stay UNKNOWN even when a GATE suite line agrees with the verdict
    r = one(["SUITE QA_07 pass=3 fail=0 expect=12 VERDICT PASS", "PASS: GATE suite QA_07"],
            "suite:QA_07")
    assert r["result"] == UNKNOWN and "expect" in r["note"]


def test_match_call_prefers_exact_then_longest_prefix_and_refuses_ambiguity():
    from foundry_coordinator.projects.animaluprising.adapter import _match_call
    steps = [{"seq": 3, "vm": "server", "code": "local C=require(x.S12_check) C.a()"},
             {"seq": 4, "vm": "server", "code": "local C=require(x.S12_check) C.a() C.b()"}]
    assert _match_call(steps, "local C=require(x.S12_check) C.a() C.b()", "x") == 4
    assert _match_call(steps, "local C=require(x.S12_check) C.a()", "x") == 3
    # the call extends a step's code: the longest such step
    assert _match_call(steps, "local C=require(x.S12_check) C.a() C.b() return 1", "x") == 4
    # a truncated call that is a prefix of two steps: ambiguous
    assert _match_call(steps, "local C=require(x.S12_check)", "x") is None
    steps2 = [{"seq": 3, "vm": "server", "code": "local C=require(x.S12_check) C.setup() --[[..]]"},
              {"seq": 4, "vm": "server", "code": "local C=require(x.S12_check) C.act()"}]
    assert _match_call(steps2, "local C=require(x.S12_check)", "x") is None
    assert _match_call(steps2, "local C=require(x.S12_check) C.act", "x") == 4
    # two steps with the same code: ambiguous, never the first
    dup = [{"seq": 3, "vm": None, "code": "a()"}, {"seq": 7, "vm": None, "code": "a()"}]
    assert _match_call(dup, "a()", "x") is None


def test_boot_and_verify_live_use_the_last_report(repo):
    a = repo["a"]
    out = _env(["BOOT(server) loaded=12 errors=3", "FAIL: BOOT counts (server — 12 loaded)",
                "VERIFY matched=0/1 mismatched=1 unresolved=0",
                "-- rerun after the fix --",
                "BOOT(server) loaded=12 errors=0", "PASS: BOOT counts (server — 12 loaded)",
                "VERIFY matched=1/1 mismatched=0 unresolved=0"])
    recs = a.classify_tool_result(_view("robloxstudio-mcp_execute_luau", {"code": "boot()"}, out),
                                  {"task_id": T})
    boot = _by(recs, "boot:server")
    assert boot["result"] == PASS and boot["counts"] == {"loaded": 12, "errors": 0}
    vl = _by(recs, "runtime:verify_live")
    assert vl["result"] == PASS and vl["counts"]["matched"] == 1
    # and the other way round: an earlier PASS doesn't hide the final FAIL
    out = _env(["BOOT(client) loaded=21 errors=0", "PASS: BOOT counts (client — 21)",
                "BOOT(client) loaded=21 errors=2", "FAIL: BOOT counts (client — 21)"])
    recs = a.classify_tool_result(_view("robloxstudio-mcp_execute_luau", {"code": "boot()"}, out),
                                  {"task_id": T})
    assert _by(recs, "boot:client")["result"] == FAIL


def test_next_blocks_are_dropped_in_every_form(repo):
    a, ws = repo["a"], repo["ws"]
    plan = PLAN.replace("## Acceptance lines\n",
                        "## Regression\nRun S45 on the committed code.\n\n## Acceptance lines\n")
    plan += ("- NEXT: rerun\n  then S77 and QA_99 must pass\n\n    and S78 too\n"
             "Next: S88 suite run\n1. NEXT: run S66\n**next**: run S55\n"
             "  - [ ] NEXT: run S44\n")
    _write(ws, PLAN_REL, plan)
    spec = a.describe(T)
    spec.data.pop("spec_revision", None)
    suites = a._plan_requirements(spec)["suites"]
    assert suites == ["UX_01", "S45"]
    ids = {o["obligation_id"] for o in a.obligations(spec)}
    for s in ("S77", "QA_99", "S78", "S88", "S66", "S55", "S44"):
        assert f"build.suite.{s}" not in ids


def test_suite_requirements_need_structure_or_a_run_verb(repo):
    a, ws = repo["a"], repo["ws"]
    plan = PLAN.replace("## Acceptance lines\n", (
        "## Test plan\n"
        "The layout differs from the HUD, unlike S9.\n"
        "- S45, S39 (regression subset)\n"
        "- S28 must pass; unlike S12 it needs a client.\n"
        "| Suite | Why |\n|---|---|\n| `S50` | combat |\n"
        "S61 is green on master. We skip S62 here.\n"
        "Gate covers QA_7 and, other than S63, nothing else.\n\n"
        "## Acceptance lines\n"))
    _write(ws, PLAN_REL, plan)
    spec = a.describe(T)
    spec.data.pop("spec_revision", None)
    suites = a._plan_requirements(spec)["suites"]
    assert set(suites) == {"UX_01", "S45", "S39", "S28", "S50", "S61", "QA_7"}
    for s in ("S9", "S12", "S62", "S63"):
        assert s not in suites


def test_acceptance_heading_variants(repo):
    a, ws = repo["a"], repo["ws"]
    for head in ("## Acceptance", "## Acceptance criteria", "## acceptance lines"):
        _write(ws, PLAN_REL, PLAN.replace("## Acceptance lines", head))
        spec = a.describe(T)
        spec.data.pop("spec_revision", None)
        req = a._plan_requirements(spec)
        assert "COMPLETE-PENDING-VISUAL-REVIEW" in req["acceptance"], head
        assert f"docs/evidence/{T}/size_1366x768.png" in req["captures"], head
        assert "visual" in spec.tags, head
        assert any(o["obligation_id"].startswith("build.accept.")
                   for o in a.obligations(spec)), head
    # not an acceptance section: a longer, different heading
    _write(ws, PLAN_REL, PLAN.replace("## Acceptance lines", "## Acceptance notes from review"))
    spec = a.describe(T)
    spec.data.pop("spec_revision", None)
    assert a._plan_requirements(spec)["acceptance"] == ""


def test_readiness_reports_tooling_faults_instead_of_raising(repo, tmp_path):
    import os
    from foundry_coordinator.adapters.git import Workspace
    a, ws = repo["a"], repo["ws"]
    spec = a.describe(T)
    os.remove(os.path.join(str(ws), "tools/guard/done_check.py"))
    a._dc = None
    r = a.readiness(spec)
    plan = _res(r, "au.plan_at_head")
    assert plan["ok"] is None and plan["cls"] == "scope"
    tool = _res(r, "au.plan_scope_parser")
    assert tool["ok"] is False and tool["cls"] == "tooling" and tool["blocking"]
    assert _res(r, "au.done_check_importable")["ok"] is False
    # a live probe while the workspace is not a git repository: no GitError escapes
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    r = a.readiness(spec, ctx={"studio_probe": lambda s: {"connected": True,
                                                          "files": {"src/x.lua": "0"}}},
                    ws=Workspace(str(bare)))
    assert _res(r, "au.studio.live_sync")["ok"] is None
    assert _res(r, "au.plan_at_head")["ok"] in (None, False)


# -- [~] in-progress checklist rows and intermediate gate outputs (UI-02, v0.122.5) ---------

PARTIAL_PLAN = PLAN.replace(
    "- [x] 1 Kit module\n- [x] 2 Captures desktop + phone\n- [ ] 3 Regression subset + gate\n",
    "- [x] 1 Kit module\n- [~] 2 Captures desktop + phone -- desktop done; phone remains\n"
    "- [~] 3 S28 S39 regression subset + gate -- S28 green, S39 remains\n"
    "- [x] 3b legacy deleted\n- [ ] 3c captures viewed + described\n").replace(
    "## Acceptance lines\n", "## Acceptance lines\n- [~] NEXT: rerun S77 on phone\n")


def test_in_progress_rows_are_open_build_steps_everywhere(repo):
    a, w, ws = repo["a"], repo["w"], repo["ws"]
    _write(ws, PLAN_REL, PARTIAL_PLAN)
    rev = _commit(ws, f"{T}: steps 2-3 in progress")
    spec = a.describe_at(T, rev, w)
    # the spec the session reads shows the [~] rows with their notes
    assert "- [~] 2 Captures desktop + phone -- desktop done; phone remains" in spec.spec
    assert "- [~] 3 S28 S39 regression" in spec.spec
    # pending BUILD steps: [~] and [ ] rows are open, [x] rows are done; CLOSE excluded
    pending = a.pending_build_steps(spec, rev, w)
    assert [p.split(" -- ")[0] for p in pending] == [
        "- [~] 2 Captures desktop + phone", "- [~] 3 S28 S39 regression subset + gate",
        "- [ ] 3c captures viewed + described"]
    assert not any("1 Kit" in p or "3b legacy" in p or "4 Docs" in p for p in pending)
    # the CLOSE row reserved for CLOSE is still the last checklist row
    assert spec.data["close_step"].startswith("4 Docs + gate -Finish")
    # plan_progress: marks per row, CLOSE row included
    rows = a.plan_progress(spec, rev, w)
    assert [m for m, _ in rows] == ["done", "partial", "partial", "done", "open", "open"]
    assert rows[1][1].endswith("desktop done; phone remains")
    # the review packet checklist: [~] counted as in progress, never ticked
    item = next(i for i in a.review_evidence(spec, rev, w)
                if i["title"] == f"{T} plan BUILD checklist")
    assert "2 of 6 items ticked; 2 in progress ([~]: partly done, still open)" in item["note"]
    assert "- [~] 2 Captures" in item["text"]
    # requirements: a suite led by a [~] row is still required; NEXT under [~] is not
    req = a._plan_requirements(spec, w)
    assert {"S28", "S39"} <= set(req["suites"]) and "S77" not in req["suites"]
    ids = {o["obligation_id"] for o in a.obligations(spec, w)}
    assert {"build.suite.S28", "build.suite.S39", "build.gate"} <= ids
    assert "build.suite.S77" not in ids
    checks = {r["check_id"] for r in a.required_checks(spec, "BUILD", w)}
    assert {"suite:S28", "suite:S39", f"gate:{T}"} <= checks


def test_all_rows_ticked_but_the_gate_keeps_build_open_until_it_passes(repo, monkeypatch):
    a, w, ws = repo["a"], repo["w"], repo["ws"]
    done = PLAN.replace("- [ ] 3 Regression subset + gate", "- [x] 3 Regression subset + gate")
    _write(ws, PLAN_REL, done)
    rev = _commit(ws, f"{T}: BUILD rows ticked")
    spec = a.describe_at(T, rev, w)
    assert a.pending_build_steps(spec, rev, w) == []            # gate PASS bound to code
    monkeypatch.setattr(a, "_gate_binding", lambda *_: VerificationResult(
        "au.gate_bound_to_head", FAIL, True, "", "stale", rev))
    assert a.pending_build_steps(spec, rev, w) == [
        "Run and commit the final BUILD gate at this revision"]
    # a [~] CLOSE row is still the CLOSE row, and the gate is still BUILD work
    _write(ws, PLAN_REL, done.replace("- [ ] 4 Docs", "- [~] 4 Docs"))
    rev2 = _commit(ws, f"{T}: close row in progress")
    assert a.pending_build_steps(a.describe_at(T, rev2, w), rev2, w) == [
        "Run and commit the final BUILD gate at this revision"]


def test_intermediate_outputs_are_exact_task_scoped_gate_paths(repo):
    a = repo["a"]
    spec = a.describe(T)
    assert a.intermediate_outputs(spec) == [
        f"docs/progress/{T}_gate_expect.json", f"docs/progress/{T}_gate_expect_full.json",
        f"docs/progress/{T}_gate_verdicts.json"]
    out = a.intermediate_outputs(spec)
    assert not any(ch in p for p in out for ch in "*?[")              # never a glob
    assert all(T in p for p in out)                                     # this task only
    # a profile may add exact '{task}' paths; globs and untemplated paths are refused
    a.cfg["intermediate_outputs"] = ["docs/progress/{task}_gate_timing.json",
                                     "docs/progress/*_gate*.json", "docs/progress/shared.json"]
    assert a.intermediate_outputs(spec)[-1] == f"docs/progress/{T}_gate_timing.json"
    assert len(a.intermediate_outputs(spec)) == 4
    assert ReviewContractDefaults().intermediate_outputs(spec) == []
    assert ReviewContractDefaults().plan_progress(spec, "HEAD", None) is None
