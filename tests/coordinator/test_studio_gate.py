"""studio_gate: the AU studio gate runner, with fake MCP servers (never Studio).

The fake official server's tally is a pure-Python GateRunner.tally (fixtures/
fake_mcp_server.py mirror_tally: countLines + the bootBad rule), so these tests prove
the runner itself fails closed instead of trusting a canned tally answer."""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from foundry_coordinator.projects.animaluprising import studio_gate as G

FAKE = os.path.join(os.path.dirname(__file__), "fixtures", "fake_mcp_server.py")
BOOT_S = 'return (require(game:GetService("ServerScriptService"):WaitForChild("GateRunner")).boot())'
BOOT_C = ('return (require(game:GetService("StarterPlayer"):WaitForChild("StarterPlayerScripts")'
          ':WaitForChild("GateRunner")).boot())')

PLAN = f"""REGRESSION PLAN subset:ui — 5 suite(s), resolved from ServerScriptService.RegressionIndex
NOTE start a solo playtest FIRST if not running (real player in world).
NOTE boot-gate-first (21): execute CALL 1-2 before any suite CALL; any boot error stops the line.
CALL 1 [vm=server] robloxstudio-mcp eval_server_runtime :: {BOOT_S}
CALL 2 [vm=client] robloxstudio-mcp eval_client_runtime :: {BOOT_C}
CALL 3 [vm=server] MANUAL (bridge) :: run tests/S28_check.lua per its header runbook in a fresh SERVER VM (host bridge per its notes where named); capture every raw PASS:/FAIL: line as CALL 3's result
CALL 4 [vm=server] robloxstudio-mcp eval_server_runtime :: local S = require(x46); local P1 = (game:GetService("Players"):GetPlayers()[1] or error("no player in world")); return (S.setupServer(P1))
NOTE S46: CALL 5 must be issued back-to-back with CALL 4 in ONE tool response (client timing window)
CALL 5 [vm=client] robloxstudio-mcp eval_client_runtime :: local S = require(y46); local P1 = game:GetService("Players").LocalPlayer; return (S.clientFog())
CALL 6 [vm=server] robloxstudio-mcp eval_server_runtime :: local S = require(x49); local P1 = (game:GetService("Players"):GetPlayers()[1] or error("no player in world")); return (S.seed(P1))
NOTE S49: CALL 7 is DYNARG — paste the <token> value from the marked earlier CALL's raw output before executing (see S49 notes)
CALL 7 [vm=client] robloxstudio-mcp eval_client_runtime :: local S = require(z49); return (S.clientCorrections1(<unitId>))
CALL 8 [vm=edit] Roblox_Studio execute_luau (datamodel Edit) :: return (S.src():find("a < b"))
CALL 9 [vm=host] SKIP (offstudio — run lupa via tools/run_s04.py) :: recorded as SKIP, never as PASS
NOTE when all CALLs are done: from execute_luau (datamodel Edit) run require(...).tally("ui", {{...}})"""

MIRROR = {"label": "subset:ui", "boot": [1, 2], "suites": [
    {"id": "S28", "calls": [3], "expect": 3},
    {"id": "S46", "calls": [4, 5], "expect": 2},
    {"id": "S49", "calls": [6, 7], "expect": 2},
    {"id": "S45", "calls": [8], "expect": 1},
    {"id": "S04", "calls": [9], "expect": 5, "offstudio": True}]}

INDEX = r'''
local entries = {
    {
        id = "S28", file = "S28_check", vm = "server", steps = { "main" }, -- comment
        expectPass = 3, loader = "bridge", notes = "a \"quoted\" note",
    },
    {
        id = "S46", file = "S46_check", vm = "server", expectPass = 2,
        steps = { "server.S.setupServer(P1)", "client.S.clientFog()" }, tightBefore = { 2 },
        loader = "mounted",
        mounts = { server = "ServerScriptService.S46_check", client = "StarterPlayer.StarterPlayerScripts.S46_check" },
    },
    {
        id = "S49", file = "S49_check", vm = "server", expectPass = 2,
        steps = { "server.S.seed(P1)", "client.S.clientCorrections1(<unitId>)" },
        setup = { "server.S.prep(P1)" }, waits = { [2] = 0.01 },
        mounts = { server = "ServerScriptService.S49_check", client = "StarterPlayer.StarterPlayerScripts.S49_check" },
    },
    { id = "S45", file = "S45_check", vm = "edit", steps = { "edit.S.src():find(\"a < b\")" }, expectPass = 1 },
    { id = "S04", file = "S04_check", vm = "offstudio", steps = { "main" }, expectPass = 5 },
    --[[ block
    comment ]]
    { id = "S50", file = "S50_check", vm = "server", steps = { "main" }, files = { "src/server/Services/X.lua" } },
    { id = "S60", file = "S60_check", vm = "server", steps = { "main" } },
}
local subsets = {
    ui = { "S28", "S46", "S49", "S45", "S04" },
    profile = { "S50" },
}
return { entries = entries, subsets = subsets }
'''

# real envelope shapes from the 2026-10-20 run log (.plan/tmp/gate/UI-T0/33a7b5f86eb4.raw.log)
ENV_ARRAY = json.dumps({"ok": True, "bridge": "ok", "result": json.dumps(["PASS: seed", "INFO unitId=77"]),
                        "output": []})
ENV_BRIDGE = json.dumps({"returnValue": "PASS: S28 c1\nPASS: S28 c2\nPASS: S28 c3",
                         "message": "Code executed successfully", "success": True, "output": []})
ENV_BRIDGE_EMPTY = json.dumps({"message": "Code executed successfully", "success": True, "output": []})
OFFICIAL_OBJ = json.dumps({"1": "PASS: c3-edit"})


# ---- pure parts ------------------------------------------------------------------
def test_parse_plan_kinds_notes_and_tokens():
    p = G.parse_plan(PLAN)
    assert p["error"] is None
    by = {c["n"]: c for c in p["calls"]}
    assert [c["n"] for c in p["calls"]] == list(range(1, 10))
    assert by[1]["kind"] == "server" and by[2]["kind"] == "client"
    assert by[3]["kind"] == "bridge" and by[3]["file"] == "S28_check.lua"
    assert by[5]["tight"] and not by[4]["tight"]
    assert by[7]["dynarg"] and by[7]["tokens"] == ["unitId"]
    assert by[8]["kind"] == "edit" and not by[8]["dynarg"] and by[8]["tokens"] == []
    assert by[9]["kind"] == "skip"
    assert by[4]["payload"].endswith("return (S.setupServer(P1))")
    assert G.BOOT_RE.search(by[1]["payload"]) and G.BOOT_RE.search(by[2]["payload"])


def test_parse_plan_errors():
    assert "FAIL: GATE regression" in G.parse_plan("FAIL: GATE regression (run) — unknown id S99")["error"]
    assert G.parse_plan("")["error"] == "no CALL lines in GateRunner.run output"


def test_resolve_dynarg_values_quoting_and_scope():
    p, miss = G.resolve_dynarg("f(<unitId>, '<deployJson>')", ["unitId", "deployJson"],
                               ["unitId=1", "INFO unitId: 42, more)", 'deploySixJson x deployJson={"name": "Red Fox", "n": [1, 2]} tail'])
    assert miss == []
    assert p == 'f(42, [[\n{"name": "Red Fox", "n": [1, 2]}]])'           # spaces kept, quotes safe
    p, _ = G.resolve_dynarg('g("<unitId>")', ["unitId"], ["unitId=it's"])
    assert p == "g([[\nit]])"                                             # scalars stop at [\w.-]
    p, miss = G.resolve_dynarg("f(<nope>)", ["nope"], ["unitId=1"])
    assert miss == ["nope"] and p == "f(<nope>)"
    p, miss = G.resolve_dynarg("f(<deployJson>)", ["deployJson"], ["deployJson=notjson"])
    assert miss == ["deployJson"]                                          # *Json needs JSON


def test_flatten_values_from_the_live_log():
    assert G.flatten_value('["PASS: a","PASS: b"]') == "PASS: a\nPASS: b"
    assert G.flatten_value('{"1":"PASS: c5 structure","2":"PASS: c5/single-driver"}') == \
        "PASS: c5 structure\nPASS: c5/single-driver"
    assert G.flatten_value('{"ok":true}') == '{"ok":true}'                  # a real object stays
    assert G.flatten_value(["PASS: x", {"k": 1}]) == 'PASS: x\n{"k": 1}'
    assert G.flatten_value("PASS: plain\nPASS: two") == "PASS: plain\nPASS: two"
    assert G.count_lines(G.flatten_value('["PASS: a","FAIL: b","  indented"]')) == (1, 1, 2)


def test_unwrap_community_envelopes_and_errors():
    u = G.unwrap_community
    # eval_*_runtime: result is a JSON-ENCODED array string (AS_C16: 12 lines tallied 0)
    r = u({"text": ENV_ARRAY, "is_error": False})
    assert r["text"] == "PASS: seed\nINFO unitId=77" and not r["is_error"]
    # execute_luau (bridge): returnValue, not result (S28/S39 tallied 0)
    r = u({"text": ENV_BRIDGE, "is_error": False})
    assert r["text"].splitlines() == ["PASS: S28 c1", "PASS: S28 c2", "PASS: S28 c3"]
    assert u({"text": ENV_BRIDGE_EMPTY, "is_error": False})["text"] == ""
    # H3: an error field is an error even without ok:false (or with ok:true)
    for env in ({"output": ["PASS: W1"], "error": "boom"}, {"ok": True, "result": "PASS: W1", "error": "x"},
                {"success": False, "error": "Code execution failed", "output": []},
                {"result": "PASS: W1", "output": [{"message": "oops", "messageType": "MessageError"}]}):
        r = u({"text": json.dumps(env), "is_error": False})
        assert r["is_error"], env
        assert G.lines_result(r["text"], "PASS: W1", r["is_error"])[0] == "UNKNOWN"
    assert u({"text": "plain text", "is_error": False})["text"] == "plain text"


def test_parse_tally_and_unknown():
    sys.path.insert(0, os.path.dirname(FAKE))
    import fake_mcp_server as F
    t = G.parse_tally(F.mirror_tally("{[3] = [[\nPASS: a\nPASS: b\nPASS: c]], [9] = [[\nSKIP x]]}",
                                     {"suites": [{"id": "S28", "calls": [3], "expect": 3},
                                                 {"id": "S04", "calls": [9], "expect": 5, "offstudio": True}]}))
    assert t["verdict"] == "PASS"
    assert {k: v["verdict"] for k, v in t["suites"].items()} == {"S28": "PASS", "S04": "SKIP"}
    assert G.parse_tally("garbage")["verdict"] == "UNKNOWN"
    assert G.parse_tally("PASS: GATE regression x\nFAIL: GATE regression x")["verdict"] == "FAIL"


def test_regression_index_parser_mapping_and_file_to_suite_map():
    idx = G.parse_regression_index(INDEX)
    assert [e["id"] for e in idx["entries"]] == ["S28", "S46", "S49", "S45", "S04", "S50", "S60"]
    assert idx["entries"][0]["notes"] == 'a "quoted" note'
    assert idx["entries"][1]["tightBefore"] == [2]
    assert idx["entries"][2]["setup"] == ["server.S.prep(P1)"] and G.entry_waits(idx["entries"][2]) == {2: 0.01}
    assert idx["subsets"]["ui"] == ["S28", "S46", "S49", "S45", "S04"]
    m = G.map_calls(G.parse_plan(PLAN), idx, "ui")
    assert {n: (e["id"], si) for n, (e, si) in m.items()} == {
        3: ("S28", 1), 4: ("S46", 1), 5: ("S46", 2), 6: ("S49", 1), 7: ("S49", 2), 8: ("S45", 1), 9: ("S04", 1)}
    assert G.map_calls(G.parse_plan(PLAN), idx, "S28,S46") is None          # count/numbering differ
    sel = G.affected_selection
    assert sel(["src/shared/Config/Strings.lua"], idx) == "ui"
    assert sel(["src/shared/Config/Ui.lua", "tests/S46_check.lua"], idx) == "ui"
    assert sel(["src/client/UI/v3/Hud.lua"], idx) == "ui"                  # unmapped UI file
    assert sel(["src/server/Services/Unmapped.lua"], idx) == "ui"          # M11: never zero
    assert sel(["tests/S60_check.lua"], idx) == "S60"
    assert sel(["src/server/Services/X.lua"], idx) == "S50"                # entry files[]
    assert sel(["src/shared/Config/Ui.lua", "tests/S60_check.lua"], idx) == "S28,S46,S49,S45,S04,S60"
    assert sel(["docs/notes.md"], idx) == ""
    assert sel([".plan/x.lua"], idx) == ""                                 # removeprefix, not lstrip
    texts = {"S60_check": "local Remotes = require(...Remotes)"}
    assert sel([G.REMOTES_FILE], idx, suite_text=texts.get, comment_only=lambda f: True) == "ui"
    assert sel([G.REMOTES_FILE], idx, suite_text=texts.get, comment_only=lambda f: False) == "S60"


def test_suite_groups_isolation_order_and_reruns():
    idx = G.parse_regression_index(INDEX.replace('id = "S45", file', 'id = "S45", isolation = "shared", file')
                                   .replace('id = "S04", file', 'id = "S04", isolation = "shared", file')
                                   .replace('expectPass = 3, loader = "bridge"',
                                            'expectPass = 3, loader = "bridge", isolation = "shared"')
                                   .replace('setup = {', 'rerunAfter = { "S45" }, setup = {'))
    plan = G.parse_plan(PLAN)
    g = G.Gate.__new__(G.Gate)
    groups = g.suite_groups(G.map_calls(plan, idx, "ui"), {c["n"]: c for c in plan["calls"]},
                            G.select_entries(idx, "ui"))
    # bridge S28 alone even though it asks to share; S45+S04 share; S46 last; S49 re-runs after S45
    assert [x["label"] for x in groups] == ["S28", "S49", "S45+S04", "S49@rerun-after-S45", "S46"]
    assert [c["n"] for c in groups[1]["calls"]] == [6, 7] and groups[1]["waits"] == {7: 0.01}


def test_luau_literals():
    assert G.luau_long("x]]y") == "[=[\nx]]y]=]"
    assert G.luau_long("ends]") == "[=[\nends]]=]"
    assert G.luau_long("a]=]b]==]") == "[===[\na]=]b]==]]===]"
    assert G.luau_value({3: "ok", "k": [True, None, 1.5]}) == "{[3] = [[\nok]], [ [[\nk]] ] = {true, nil, 1.5}}"
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            G.luau_value({"x": bad})


def test_overall_unknown_and_skip_rules():
    c = G.check
    assert G.overall([c("a", "PASS"), c("b", "SKIP", required=False)]) == "PASS"
    assert G.overall([c("a", "PASS"), c("b", "UNKNOWN")]) == "FAIL"
    assert G.overall([c("a", "PASS"), c("b", "SKIP")]) == "FAIL"           # required SKIP is not PASS
    assert G.overall([c("b", "UNKNOWN", required=False)]) == "FAIL"         # nothing required passed
    assert G.overall([c("a", "PASS")], error="boom") == "ERROR"
    assert G.lines_result("x\nPASS: W1 ok", "PASS: W1", False)[0] == "PASS"
    assert G.lines_result("PASS: W1\nFAIL: W1 nope", "PASS: W1", False)[0] == "FAIL"
    assert G.lines_result("", "PASS: W1", True)[0] == "UNKNOWN"
    assert G.lines_result("nothing", "PASS: W1", False)[0] == "FAIL"


def test_plan_checks():
    pc = G.plan_checks
    assert [c["id"] for c in pc({}, ["src/client/UI/v3/Hud.lua"], None, "ui")] == ["plan.stories"]
    assert [c["id"] for c in pc({"stories": [{}]}, ["src/x.lua"], "", "ui")] == ["plan.selection"]
    assert pc({"stories": [{}]}, ["src/client/UI/v3/Hud.lua"], "", "") == []
    assert pc({}, ["tests/lib/A.lua"], None, "") == []


def test_png_size_and_extract_json():
    sys.path.insert(0, os.path.dirname(FAKE))
    import fake_mcp_server
    assert G.png_size(fake_mcp_server.png(390, 844)) == (390, 844)
    assert G.png_size(b"\xff\xd8junk") is None
    assert G.extract_json('print\n{"ok": true}') == {"ok": True}
    assert G.extract_json("Result: {\"a\": 1} trailing") == {"a": 1}
    assert G.extract_json("none") is None
    assert G.studio_mode('{"mode": "Edit"}') == "edit"
    assert G.studio_mode("Studio is playing (run mode)") == "play"
    assert G.studio_mode("???") == "unknown"


def test_rojo_mount_resolution():
    proj = {"tree": {"$className": "DataModel", "ReplicatedStorage": {
        "Shared": {"$path": "src/shared"}, "TestLib": {"$path": "tests/lib"}},
        "ServerScriptService": {"GateRunner": {"$path": "tests/GateRunner.lua"},
                                "Server": {"$path": "src/server"}}}}
    m = G.rojo_mounts(proj)
    assert G.rojo_resolve(m, "tests/lib/StoryHarness.lua") == ["ReplicatedStorage.TestLib.StoryHarness"]
    assert G.rojo_resolve(m, "src/server/Runtime.server.lua") == ["ServerScriptService.Server.Runtime"]
    assert G.rojo_resolve(m, "src/shared/Net/init.lua") == ["ReplicatedStorage.Shared.Net"]
    assert G.rojo_resolve(m, "tests/GateRunner.lua") == ["ServerScriptService.GateRunner"]
    assert G.rojo_resolve(m, "docs/x.md") == []
    # a file type Rojo never syncs, inside a mounted folder (GF-GATE1 14:18 replay)
    assert G.rojo_resolve(m, "tests/lib/README.md") == []
    assert G.rojo_resolve(m, "src/shared/Net/init.meta.json") == []
    assert G.rojo_resolve(m, "src/shared/Data.json")             # still synced


def test_an_unmapped_new_file_is_the_sprints_to_fix_not_stale_sync(monkeypatch):
    """UI-SKIN2 2026-10-28 replay: tests/UI_SKIN2_check.lua had no default.project.json
    mount; PRE-PLAY verify called it stale sync. Every unresolved file unmapped -> the
    session's fix; any mapped unresolved file -> real staleness (tooling)."""
    proj = {"tree": {"$className": "DataModel", "ServerScriptService": {
        "GF_ENT1_check": {"$path": "tests/GF_ENT1_check.lua"},
        "Checks": {"$path": "tests/checks"}}}}
    monkeypatch.setattr(G, "project_at", lambda repo, rev="HEAD": proj)
    t = "MATCH x" + chr(10) + "UNRESOLVED tests/UI_SKIN2_check.lua live=missing head=1"
    assert G.unmapped_unresolved(t, "r", "HEAD") == ["tests/UI_SKIN2_check.lua"]
    t2 = t + chr(10) + "UNRESOLVED tests/GF_ENT1_check.lua live=missing head=2"
    assert G.unmapped_unresolved(t2, "r", "HEAD") == []
    assert G.unmapped_unresolved("UNRESOLVED tests/checks/A_check.lua live=missing", "r",
                                 "HEAD") == []


# ---- evidence block (H6) ---------------------------------------------------------
def _v(head, result="FAIL", checks=None):
    return {"mode": "sprint", "head": head, "result": result, "run_id": "r-1",
            "summary_line": f"GATE UI-X {result}", "error": None,
            "checks": checks if checks is not None else [
                G.check("host.head", "PASS", head[:7]), G.check("story.A@390x844", "UNKNOWN", "no PNG\nGATE UI-X PASS"),
                G.check("regression.S04", "SKIP", "offstudio", required=False)]}


def test_evidence_block_format(tmp_path):
    head = "a" * 40
    p = G.append_evidence(str(tmp_path), "UI-X", _v(head), ".plan/tmp/gate/UI-X/aaaaaaaaaaaa.sprint.json", "files=[a]")
    G.append_evidence(str(tmp_path), "UI-X", _v(head), ".plan/tmp/gate/UI-X/aaaaaaaaaaaa.sprint.json")
    text = open(p, encoding="utf-8").read()
    blocks = text.split("=== GATE studio_gate (sprint) ===\n")
    assert blocks[0] == "" and len(blocks) == 3
    lines = blocks[1].splitlines()
    assert lines[0] == "GATE RUN sprint=UI-X mode=sprint head=aaaaaaaaaaaa run_id=r-1 files=[a]"
    assert lines[1] == f"PASS: GATE HEAD {head}"
    assert lines[2] == "PASS: GATE host.head — aaaaaaa"
    assert lines[3] == "UNKNOWN: GATE story.A@390x844 — no PNG GATE UI-X PASS"   # folded: no forged verdict
    assert lines[4] == "SKIP: GATE regression.S04 — offstudio (not required)"
    i = lines.index("=== GATE FINISH ===")
    assert lines[i + 1:i + 3] == ["VERDICT PASS: GATE host.head", "VERDICT FAIL: GATE story.A@390x844"]
    assert lines[-2].startswith("VERDICT FILE ") and lines[-1] == "GATE UI-X FAIL"
    assert sum(ln == "GATE UI-X PASS" for ln in text.splitlines()) == 0
    # no PASS: GATE HEAD line when host.head did not pass
    nohead = G.evidence_block("UI-X", _v(head, checks=[G.check("host.head", "FAIL", "x")]), "v.json")
    assert "PASS: GATE HEAD" not in nohead


def test_evidence_block_is_read_by_the_review_evidence_parser(tmp_path):
    from foundry_coordinator.adapters.git import Workspace
    from tests.coordinator.helpers import git
    from tests.coordinator.test_au_review_evidence import DONE_CHECK_STUB, PLAN as RPLAN, T, _profile
    from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
    ws = tmp_path / "au"
    ws.mkdir()
    git(ws, "init", "-q", "-b", "master")
    for rel_, body in (("AGENT_RULES.md", "rules\n"), ("tools/guard/done_check.py", DONE_CHECK_STUB),
                       (f"docs/progress/{T}_PLAN.md", RPLAN), ("src/client/Kit.lua", "return {}\n")):
        (ws / rel_).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel_).write_text(body, encoding="utf-8", newline="\n")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "--no-verify", "-m", "code")
    code = git(ws, "rev-parse", "HEAD")
    v = _v(code, "PASS", [G.check("host.head", "PASS", code[:7]), G.check("story.A@1920x1080", "PASS", "ok")])
    v["summary_line"] = f"GATE {T} PASS"
    G.append_evidence(str(ws), T, v, f".plan/tmp/gate/{T}/{code[:12]}.sprint.json", "files=[src/client/Kit.lua]")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "--no-verify", "-m", "evidence")
    head = git(ws, "rev-parse", "HEAD")
    a = AnimalUprisingAdapter(_profile(ws), str(tmp_path / "state"))
    w = Workspace(str(ws))
    items = a._log_evidence(a.describe_at(T, head, w), head, w, 20000)
    gate = next(i for i in items if i["title"] == f"{T} last gate block")
    assert gate["status"] == "ok", gate["note"]
    assert "=== GATE FINISH ===" in gate["text"] and f"GATE {T} PASS" in gate["text"]
    assert "VERDICT PASS: GATE story.A@1920x1080" in gate["text"]
    assert "bound to" in gate["note"]                                       # PASS: GATE HEAD bound


# ---- fakes wiring ----------------------------------------------------------------
@pytest.fixture
def fakes(tmp_path, monkeypatch):
    """Point both servers at the fake; returns set_rules(official=[...], community=[...]) and the log path."""
    log = tmp_path / "calls.jsonl"
    state = tmp_path / "state.json"
    monkeypatch.setattr(G, "SETTLE_S", 0.0)
    monkeypatch.setattr(G, "VIEWPORT_SETTLE_S", 0.0, raising=False)

    def set_rules(official=(), community=()):
        if state.exists():
            state.unlink()
        for kind, rules in (("official", official), ("community", community)):
            p = tmp_path / f"{kind}.json"
            p.write_text(json.dumps({"rules": list(rules)}), encoding="utf-8")
            monkeypatch.setenv(f"FOUNDRY_MCP_{kind.upper()}", json.dumps(
                [sys.executable, FAKE, "--kind", kind, "--scenario", str(p), "--log", str(log),
                 "--state", str(state)]))

    def calls():
        if not log.exists():
            return []
        return [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]
    set_rules()
    return set_rules, calls


def commit(r, msg="c"):
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(r), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg],
                   check=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "au"
    (r / "docs" / "progress").mkdir(parents=True)
    (r / "tests" / "lib").mkdir(parents=True)
    (r / ".gitignore").write_text(".plan/\n")
    (r / "default.project.json").write_text(json.dumps({"name": "AnimalUprising", "tree": {
        "ReplicatedStorage": {"TestLib": {"$path": "tests/lib"}}}}))
    (r / "tests" / "RegressionIndex.lua").write_text(INDEX, encoding="utf-8")
    (r / "tests" / "S28_check.lua").write_text("return 'PASS: c1'", encoding="utf-8")
    (r / "tests" / "lib" / "StoryHarness.lua").write_text("return {}", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    commit(r, "init")
    return r


PLAYER = {"tool": "eval_server_runtime", "match": "return #game", "text": "1"}


def regression_rules(extra_off=(), extra_comm=()):
    official = list(extra_off) + [
        {"tool": "execute_luau", "match": r"\.run\(", "text": PLAN},
        {"tool": "execute_luau", "match": r"\.tally\(", "mirror": MIRROR},
        {"tool": "execute_luau", "match": "S.src", "text": OFFICIAL_OBJ},
        {"tool": "get_console_output", "text": "[ServiceLoader] Boot complete"}]
    community = list(extra_comm) + [
        PLAYER,
        {"tool": "eval_server_runtime", "match": "boot\\(\\)", "text": "BOOT(server) loaded=36 errors=0\nPASS: BOOT counts == EXPECTED_BOOT"},
        {"tool": "eval_client_runtime", "match": "boot\\(\\)", "text": "PASS: BOOT client"},
        {"tool": "eval_server_runtime", "match": "setupServer", "text": "PASS: s1\nINFO unitId=99"},
        {"tool": "eval_client_runtime", "match": "clientFog", "text": "PASS: fog"},
        {"tool": "eval_server_runtime", "match": "S.prep", "text": "PASS: prep"},
        {"tool": "eval_server_runtime", "match": "S.seed", "text": ENV_ARRAY},
        {"tool": "eval_client_runtime", "match": "clientCorrections1", "text": "PASS: corr"},
        {"tool": "execute_luau", "match": "GetAsync", "text": ENV_BRIDGE},
    ]
    return official, community


def gate_file(repo, sid, name):
    return repo / ".plan" / "tmp" / "gate" / sid / name


def run_regression(repo, sid, timeout="20"):
    out = []
    rc = G.main(["--repo", str(repo), "--timeout", timeout, "regression", "--selection", "ui", "--sprint", sid],
                out=out.append)
    v = json.load(open(gate_file(repo, sid, "latest.json")))
    return rc, out, v


def tally_code(calls):
    return next(c for c in reversed(calls()) if ".tally(" in c["args"].get("code", ""))["args"]["code"]


def test_regression_per_suite_playtests_boot_setup_and_tally(fakes, repo):
    set_rules, calls = fakes
    set_rules(*regression_rules())
    rc, out, v = run_regression(repo, "R1")
    assert rc == 0, out
    assert out[-1] == "GATE R1 PASS"
    log = calls()
    codes = [c["args"].get("code", "") for c in log]
    # one fresh playtest per suite that needs one: S28 (bridge, alone), S49, then S46 LAST
    assert [c["args"]["action"] for c in log if c["tool"] == "solo_playtest"] == ["start", "stop"] * 3
    assert sum("WaitForChild(\"GateRunner\")).boot()" in x for x in codes if "ServerScriptService" in x) == 3
    order = [i for i, x in enumerate(codes) if "GetAsync" in x or "S.prep" in x or "S.seed" in x
             or "setupServer" in x]
    assert ["GetAsync" in codes[order[0]], "S.prep" in codes[order[1]], "S.seed" in codes[order[2]],
            "setupServer" in codes[order[3]]] == [True] * 4
    i4 = codes.index(next(x for x in codes if "setupServer" in x))
    assert "clientFog" in codes[i4 + 1]                                       # tight pair back-to-back
    dyn = next(x for x in codes if "clientCorrections1" in x)
    assert "clientCorrections1(77)" in dyn                                    # S49's own seed, not S46's 99
    bridge = next(c for c in log if "GetAsync" in c["args"].get("code", ""))
    assert bridge["kind"] == "community" and bridge["args"]["target"] == "server"
    assert "if true then" in bridge["args"]["code"]                            # re-Boot guard on
    edit = next(c for c in log if "S.src" in c["args"].get("code", ""))
    assert edit["kind"] == "official" and edit["args"]["datamodel_type"] == "Edit"
    code = tally_code(calls)
    assert "[6] = [[\nPASS: seed\nINFO unitId=77]]" in code                   # JSON array flattened
    assert "[8] = [[\nPASS: c3-edit]]" in code                                # official {"1":..} flattened
    assert "[3] = [[\nPASS: S28 c1\nPASS: S28 c2\nPASS: S28 c3]]" in code     # bridge returnValue
    assert "[9] = [[\nSKIP offstudio" in code and "[1] = [[\nBOOT(server)" in code
    assert v["schema"] == 1 and v["mode"] == "regression" and v["result"] == "PASS"
    assert v["regression"]["suites"] == {"S28": "PASS", "S46": "PASS", "S49": "PASS", "S45": "PASS", "S04": "SKIP"}
    ids = {c["id"]: c for c in v["checks"]}
    assert ids["regression.boot"]["result"] == "PASS" and ids["regression.S49.setup1"]["result"] == "PASS"
    assert ids["regression.S04"]["required"] is False
    assert gate_file(repo, "R1", f"{v['head'][:12]}.regression.json").exists()
    assert json.load(open(gate_file(repo, "R1", "latest/regression.json")))["head"] == v["head"]
    assert json.load(open(gate_file(repo, "R1", "status.json")))["phase"] == "done"
    raw = gate_file(repo, "R1", f"{v['head'][:12]}.regression.raw.log").read_text(encoding="utf-8")
    assert "CALL 5 tight" in raw
    assert not (repo / ".plan" / "tmp" / "gate" / G.LOCK_NAME).exists()         # released


def test_boot_timeout_sinks_the_regression_and_stops_the_line(fakes, repo):
    """H1: a boot CALL that times out is not a PASS line: the runner fails it itself
    (regression.boot) and hands the tally a `FAIL: GATE boot` line (bootBad)."""
    set_rules, calls = fakes
    set_rules(*regression_rules(extra_comm=[{"tool": "eval_server_runtime", "match": "boot\\(\\)",
                                             "text": "PASS: BOOT late", "sleep": 3}]))
    rc, out, v = run_regression(repo, "R2", timeout="1")
    assert rc == 1 and out[-1] == "GATE R2 FAIL"
    ids = {c["id"]: c for c in v["checks"]}
    assert ids["regression.boot"]["result"] == "UNKNOWN" and ids["regression.boot"]["required"]
    assert ids["regression.stopped"]["result"] == "FAIL"
    code = tally_code(calls)
    assert "[1] = [[\nFAIL: GATE boot CALL 1" in code and "PASS: BOOT late" not in code
    assert "clientCorrections1" not in json.dumps(calls())                     # the line stopped
    assert ids["regression.verdict"]["result"] == "FAIL"


def test_boot_without_pass_line_fails(fakes, repo):
    set_rules, calls = fakes
    set_rules(*regression_rules(extra_comm=[{"tool": "eval_client_runtime", "match": "boot\\(\\)",
                                             "text": "BOOT_STATE loader not visible"}]))
    rc, out, v = run_regression(repo, "R2b")
    assert rc == 1
    assert {c["id"]: c["result"] for c in v["checks"]}["regression.boot"] == "FAIL"


def test_errored_step_never_reaches_the_tally_as_success(fakes, repo):
    """H2: an eval that printed PASS lines and then threw: the tally gets ONE failing
    line, the runner adds regression.call<n>, DYNARG skips the errored output, and a
    tool-error-only failure is retried once (both runs logged)."""
    set_rules, calls = fakes
    bad = json.dumps({"ok": False, "bridge": "ok", "error": "boom",
                      "output": ["PASS: half", "INFO unitId=77"]})
    set_rules(*regression_rules(extra_comm=[{"tool": "eval_server_runtime", "match": "S.seed", "text": bad}]))
    rc, out, v = run_regression(repo, "R3")
    assert rc == 1
    code = tally_code(calls)
    assert "[6] = [[\nFAIL: GATE call 6 error — PASS: half INFO unitId=77 ERROR: boom]]" in code
    assert "FAIL: GATE dynarg CALL 7" in code
    ids = {c["id"]: c for c in v["checks"]}
    assert ids["regression.call6"]["result"] == "UNKNOWN" and ids["regression.S49"]["result"] == "FAIL"
    raw = gate_file(repo, "R3", f"{v['head'][:12]}.regression.raw.log").read_text(encoding="utf-8")
    assert "RETRY S49" in raw
    assert sum(1 for c in calls() if "S.seed" in c["args"].get("code", "")) == 2


def test_tool_error_retry_once_in_a_fresh_playtest(fakes, repo):
    set_rules, calls = fakes
    set_rules(*regression_rules(extra_comm=[{"tool": "eval_server_runtime", "match": "S.seed", "times": 1,
                                             "text": "Eval bridge timeout", "is_error": True}]))
    rc, out, v = run_regression(repo, "R4")
    assert rc == 0, out
    ids = {c["id"]: c for c in v["checks"]}
    assert ids["regression.S49.retry"]["required"] is False and "isolation-sensitive" in ids["regression.S49.retry"]["detail"]
    assert "regression.call6" not in ids                                       # the second run is used
    assert [c["args"]["action"] for c in calls() if c["tool"] == "solo_playtest"] == ["start", "stop"] * 4


def test_assertion_fail_is_never_retried(fakes, repo):
    set_rules, calls = fakes
    set_rules(*regression_rules(extra_comm=[{"tool": "eval_client_runtime", "match": "clientCorrections1",
                                             "text": "FAIL: corr wrong"}]))
    rc, out, v = run_regression(repo, "R5")
    assert rc == 1
    assert [c["args"]["action"] for c in calls() if c["tool"] == "solo_playtest"] == ["start", "stop"] * 3
    assert {c["id"]: c["result"] for c in v["checks"]}["regression.S49"] == "FAIL"


def test_empty_bridge_reply_uses_console_lines_or_fails(fakes, repo):
    set_rules, calls = fakes
    off, comm = regression_rules(extra_comm=[{"tool": "execute_luau", "match": "GetAsync", "text": ENV_BRIDGE_EMPTY}])
    set_rules(off, comm)
    rc, out, v = run_regression(repo, "R6")
    assert rc == 1
    assert "[3] = [[\nFAIL: GATE call 3 empty reply]]" in tally_code(calls)
    assert {c["id"]: c["result"] for c in v["checks"]}["regression.call3"] == "FAIL"
    # the suite print()ed its lines: the console delta around the bridge call is used
    off = [{"tool": "get_console_output", "times": 2, "text": "[ServiceLoader] Boot complete"},
           {"tool": "get_console_output", "text": "[ServiceLoader] Boot complete\n12:00:01 PASS: S28 c1\n"
                                                  "PASS: S28 c2\n  -- PASS: S28 c3"}] + off
    set_rules(off, comm)
    rc, out, v = run_regression(repo, "R7")
    assert rc == 0, out
    assert "[3] = [[\nPASS: S28 c1\nPASS: S28 c2\nPASS: S28 c3]]" in tally_code(calls)


def test_unmapped_plan_and_manual_call_fail_closed(fakes, repo):
    set_rules, calls = fakes
    plan = PLAN.replace("NOTE when all", "CALL 10 [vm=server] MANUAL :: no hostCmd in index — operator\nNOTE when all")
    off, comm = regression_rules(extra_off=[{"tool": "execute_luau", "match": r"\.run\(", "text": plan}])
    set_rules(off, comm)
    rc, out, v = run_regression(repo, "R8")
    assert rc == 1
    ids = {c["id"]: c for c in v["checks"]}
    assert ids["regression.mapping"]["result"] == "UNKNOWN" and ids["regression.call10"]["result"] == "UNKNOWN"
    assert [c["args"]["action"] for c in calls() if c["tool"] == "solo_playtest"] == ["start", "stop"]


def test_unknown_check_makes_gate_fail(fakes, repo):
    set_rules, _ = fakes
    off, comm = regression_rules(extra_off=[{"tool": "get_console_output", "text": "x", "is_error": True}])
    set_rules(off, comm)
    out = []
    assert G.main(["--repo", str(repo), "regression", "--selection", "ui", "--sprint", "R9"], out=out.append) == 1
    assert any(o.startswith("UNKNOWN: console.errors") for o in out)


def test_studio_closed_is_an_error_without_retrying_connect(repo, monkeypatch):
    """M2: a failed connect is cached for the run; the preflight turns it into ERROR (exit 2)."""
    n = []

    def connect(kind, **kw):
        n.append(kind)
        raise G.M.McpError(f"{kind}: cannot start")
    monkeypatch.setattr(G.M, "connect", connect)
    g = G.Gate(str(repo), "C1", "regression")
    assert g.edit("return 1")[1] is True and g.edit("return 2")[1] is True
    assert n == ["official"]
    out = []
    assert G.main(["--repo", str(repo), "regression", "--selection", "ui", "--sprint", "C2"], out=out.append) == 2
    assert out[-1] == "GATE C2 FAIL" and any(o.startswith("ERROR: McpError") for o in out)


def test_playtest_without_player_is_stopped(fakes, repo):
    set_rules, calls = fakes
    set_rules([], [{"tool": "eval_server_runtime", "match": "return #game", "text": "0"}])
    g = G.Gate(str(repo), "P1", "regression", call_timeout=10)
    try:
        assert g.start_playtest(wait=1.2) is False
    finally:
        g.close()
    assert [c["args"]["action"] for c in calls() if c["tool"] == "solo_playtest"] == ["start", "stop"]
    assert g.checks[-1]["result"] == "UNKNOWN" and not g._playing


STORY_OK = ('{"ok": true, "screen": {"w": {W}, "h": {H}}, "defects": 0, '
            '"lines": ["PASS: UI-LAYOUT ok"], "summary": "BASELINE {W}x{H} Sample: labels=2"}')


def story_plan(repo, do_commit=True, **extra):
    plan = {"schema": 1, "stories": [{"name": "Sample.normal", "module": "ReplicatedStorage.TestLib.SampleStory",
                                      "fixture": {"title": "Hi"}}]}
    plan.update(extra)
    (repo / "docs" / "progress" / "UI-T0_gate.json").write_text(json.dumps(plan), encoding="utf-8")
    if do_commit:
        commit(repo, "plan")


def test_stories_mode_captures_five_sizes(fakes, repo):
    set_rules, calls = fakes
    set_rules([{"tool": "execute_luau", "match": "mountAndCheck", "text": STORY_OK},
               {"tool": "get_console_output", "text": ""}])
    story_plan(repo)
    out = []
    assert G.main(["--repo", str(repo), "stories", "--sprint", "UI-T0"], out=out.append) == 0, out
    v = json.load(open(gate_file(repo, "UI-T0", "latest.json")))
    assert v["mode"] == "stories"
    assert gate_file(repo, "UI-T0", f"{v['head'][:12]}.stories.json").exists()
    assert not gate_file(repo, "UI-T0", f"{v['head'][:12]}.sprint.json").exists()   # H4
    assert [c["size"] for c in v["captures"]] == G.SIZE_NAMES
    for c in v["captures"]:
        p = repo / c["path"]
        assert p.exists() and G.png_size(p.read_bytes()) == tuple(map(int, c["size"].split("x")))
        assert c["path"] == f"docs/evidence/UI-v3/UI-T0/Sample.normal@{c['size']}.png"
    ids = [c["id"] for c in v["checks"]]
    assert "story.Sample.normal@390x844" in ids
    log = calls()
    assert sum("H.unmount()" in c["args"].get("code", "") for c in log) == 5
    assert "StopSimulationAsync" in [c for c in log if c["tool"] == "execute_luau"][-1]["args"]["code"]
    assert not (repo / "docs" / "progress" / "UI-T0_EVIDENCE_RAW.log").exists()   # sprint mode only


def test_plan_is_read_from_head_not_the_working_tree(fakes, repo):
    set_rules, calls = fakes
    set_rules([{"tool": "execute_luau", "match": "mountAndCheck", "text": STORY_OK},
               {"tool": "get_console_output", "text": ""}])
    story_plan(repo)
    # an uncommitted edit that would make the gate a no-op is ignored
    (repo / "docs" / "progress" / "UI-T0_gate.json").write_text(json.dumps({"stories": []}))
    out = []
    assert G.main(["--repo", str(repo), "stories", "--sprint", "UI-T0"], out=out.append) == 0, out
    assert sum("mountAndCheck" in c["args"].get("code", "") for c in calls()) == 5
    out = []
    assert G.main(["--repo", str(repo), "stories", "--sprint", "UI-T9"], out=out.append) == 1
    assert any("host.gate_plan" in o and "not committed" in o for o in out)


@pytest.mark.parametrize("story,want", [
    ({"name": "../../../x", "module": "m"}, "bad story name"),
    ({"name": "A", "module": "m", "sizes": ["1280x720"]}, "must be a non-empty list"),
    ({"name": "A", "module": "m", "sizes": "1920x1080"}, "must be a non-empty list"),
    ({"name": "A", "module": "m", "sizes": []}, "must be a non-empty list")])
def test_bad_story_names_and_sizes_fail(fakes, repo, story, want):
    set_rules, calls = fakes
    set_rules([{"tool": "execute_luau", "match": "mountAndCheck", "text": STORY_OK},
               {"tool": "get_console_output", "text": ""}])
    (repo / "docs" / "progress" / "UI-T0_gate.json").write_text(json.dumps({"stories": [story]}))
    commit(repo, "plan")
    out = []
    assert G.main(["--repo", str(repo), "stories", "--sprint", "UI-T0"], out=out.append) == 1
    v = json.load(open(gate_file(repo, "UI-T0", "latest.json")))
    assert any(c["result"] == "FAIL" and want in c["detail"] for c in v["checks"]), v["checks"]
    assert not any("mountAndCheck" in c["args"].get("code", "") for c in calls())
    assert not (repo.parent / "x@1920x1080.png").exists()


def test_story_defects_fail_lines_and_wrong_capture_size_fail(fakes, repo):
    set_rules, _ = fakes
    bad = ('{"ok": true, "screen": {"w": {W}, "h": {H}}, "defects": 2, '
           '"lines": ["FAIL: UI-LAYOUT SMALL-TARGET Sample/Btn - 30px < 48px"]}')
    lines_only = ('{"ok": true, "screen": {"w": {W}, "h": {H}}, "defects": 0, '
                  '"lines": ["FAIL: UI-LAYOUT CLIP Sample/T - 4px"]}')
    as_bool = '{"ok": true, "screen": {"w": {W}, "h": {H}}, "defects": false, "lines": []}'
    set_rules([{"tool": "execute_luau", "match": "AU_390x844", "text": bad},
               {"tool": "execute_luau", "match": "AU_844x390", "text": lines_only},
               {"tool": "execute_luau", "match": "AU_1024x768", "text": as_bool},
               {"tool": "execute_luau", "match": "mountAndCheck", "text": STORY_OK},
               {"tool": "get_console_output", "text": ""}],
              [{"tool": "capture_screenshot", "match": "png", "times": 1, "text": "{}", "png": [1920, 1080],
                "is_error": True},
               {"tool": "capture_screenshot", "text": "{}", "png": "state"}])
    story_plan(repo)
    out = []
    assert G.main(["--repo", str(repo), "stories", "--sprint", "UI-T0", "--story", "Sample.normal"],
                  out=out.append) == 1
    v = json.load(open(gate_file(repo, "UI-T0", "latest.json")))
    r = {c["id"]: c for c in v["checks"]}
    assert r["story.Sample.normal@1920x1080"]["result"] == "UNKNOWN"         # capture is_error ignored before
    assert r["story.Sample.normal@390x844"]["result"] == "FAIL" and "2 layout defect" in r["story.Sample.normal@390x844"]["detail"]
    assert r["story.Sample.normal@844x390"]["result"] == "FAIL" and "FAIL layout line" in r["story.Sample.normal@844x390"]["detail"]
    assert r["story.Sample.normal@1024x768"]["result"] == "FAIL" and "no defect count" in r["story.Sample.normal@1024x768"]["detail"]
    assert r["story.Sample.normal@1366x768"]["result"] == "PASS"
    cap = next(c for c in v["captures"] if c["size"] == "390x844")
    assert cap["layout_lines"] == ["FAIL: UI-LAYOUT SMALL-TARGET Sample/Btn - 30px < 48px"]


def fake_host(repo, lune_ok=True, files_map=None):
    """run_cmd stub for git / hash_fnv / lune (real git for HEAD and status)."""
    def run(argv, cwd, timeout):
        if argv[0] == "git" and argv[1] in ("rev-parse", "status"):
            return G.default_run_cmd(argv, cwd, timeout)
        if argv[0] == "git":
            return 0, ""
        if "-Command" in argv:
            cmd = argv[-1]
            jpath = cmd.split("-Json '")[1].rstrip("'")
            # like hash_fnv.ps1: Join-Path (Get-Location) $Json, i.e. relative to the cwd
            assert not os.path.isabs(jpath), jpath
            with open(os.path.join(cwd, jpath), "w", encoding="utf-8") as f:
                json.dump({"rev": "x", "files": files_map or {"tests/S28_check.lua": "1dd2c64f"}}, f)
            return 0, "REV tests/S28_check.lua 1dd2c64f (5 bytes)\nHASH_FNV 1 files at abc123 PASS"
        if "--negative" in argv:
            return 0, "NEGATIVE CONTROL PASS (3 designed failures)"
        return 0, "LUNE 12/12 PASS" if lune_ok else "LUNE 11/12 FAIL"
    return run


VERIFY_OK = {"tool": "execute_luau", "match": "verifyLive",
             "text": "MATCH tests/S28_check.lua\nVERIFY matched=1/1 mismatched=0 unresolved=0 skipped=0"}


def test_sprint_mode_full_order_and_evidence(fakes, repo, monkeypatch):
    set_rules, calls = fakes
    off, comm = regression_rules(extra_off=[
        VERIFY_OK, {"tool": "execute_luau", "match": "mountAndCheck", "text": STORY_OK},
        {"tool": "execute_luau", "match": "FocusWalk", "text": "PASS: FOCUS sample reached=3/3 deadEnds=0"}],
        extra_comm=[{"tool": "eval_client_runtime", "match": "W1", "text": "PASS: W1 snapshot shown"}])
    set_rules(off, comm)
    story_plan(repo, wiring=[{"id": "hud.snapshot", "vm": "client", "code": "return 'W1'", "expect": "PASS: W1"}],
               focus=[{"id": "sample", "story": "Sample.normal"}], files=["tests/S28_check.lua"], selection="ui")
    monkeypatch.setattr(G.shutil, "which", lambda n: n)
    out = []
    rc = G.main(["--repo", str(repo), "--run-id", "nonce-1", "sprint", "--sprint", "UI-T0",
                 "--files", "tests/S28_check.lua"], out=out.append, run_cmd=fake_host(repo))
    assert rc == 0, out
    assert out[-1] == "GATE UI-T0 PASS"
    v = json.load(open(gate_file(repo, "UI-T0", "latest/sprint.json")))
    assert v["run_id"] == "nonce-1" and v["mode"] == "sprint"
    assert gate_file(repo, "UI-T0", f"{v['head'][:12]}.sprint.json").exists()
    ids = [c["id"] for c in v["checks"]]
    order = ["host.gate_plan", "host.head", "host.clean_tree", "host.crlf", "host.hash_expect", "lune.specs",
             "lune.negative_control", "host.live_hash", "story.Sample.normal@1920x1080", "focus.sample",
             "host.playtest", "wiring.hud.snapshot", "regression.boot", "regression.S28", "regression.verdict",
             "console.errors"]
    assert [i for i in ids if i in order] == order
    log = calls()
    idx_run = next(i for i, c in enumerate(log) if ".run(" in c["args"].get("code", ""))
    starts = [i for i, c in enumerate(log) if c["tool"] == "solo_playtest" and c["args"]["action"] == "start"]
    assert len(starts) == 4 and starts[0] < idx_run < starts[1]                 # wiring playtest, then suites
    ev = (repo / "docs" / "progress" / "UI-T0_EVIDENCE_RAW.log").read_text(encoding="utf-8")
    assert ev.count("=== GATE studio_gate (sprint) ===") == 1 and ev.count("=== GATE FINISH ===") == 1
    assert f"PASS: GATE HEAD {v['head']}" in ev
    assert ev.rstrip().endswith("GATE UI-T0 PASS")
    assert "selection=ui tier=B" in ev and "run_id=nonce-1" in ev
    assert "docs/progress/UI-T0_gate.json" in ev                               # plan under clean-tree


def test_sprint_mode_host_failure_and_plan_checks_fail_gate(fakes, repo, monkeypatch):
    set_rules, _ = fakes
    off, comm = regression_rules(extra_off=[
        {"tool": "execute_luau", "match": "verifyLive", "text": "VERIFY matched=0/1 mismatched=1 unresolved=0"},
        {"tool": "execute_luau", "match": "mountAndCheck", "text": STORY_OK}])
    set_rules(off, comm)
    story_plan(repo, files=["tests/S28_check.lua", "src/client/UI/v3/Hud.lua"], selection="", stories=[])
    monkeypatch.setattr(G.shutil, "which", lambda n: n)
    out = []
    rc = G.main(["--repo", str(repo), "sprint", "--sprint", "UI-T0"], out=out.append,
                run_cmd=fake_host(repo, lune_ok=False))
    assert rc == 1 and out[-1] == "GATE UI-T0 FAIL"
    assert any(o.startswith("FAIL: lune.specs") for o in out)
    assert any(o.startswith("FAIL: host.live_hash") for o in out)
    assert any(o.startswith("FAIL: plan.stories") for o in out)
    assert any(o.startswith("FAIL: plan.selection") for o in out)
    v = json.load(open(gate_file(repo, "UI-T0", "latest.json")))
    assert v["regression"]["selection"] == "ui"                                # ran the affected one


def test_live_hash_resolves_testlib_mounts(fakes, repo):
    set_rules, calls = fakes
    skipped = ("SKIP (unmounted) tests/lib/StoryHarness.lua\nSKIP (unmounted) tests/lib/Gone.lua\n"
               "SKIP (unmounted) docs/progress/UI-T0_gate.json\nVERIFY matched=0/0 mismatched=0 unresolved=0 skipped=3")
    set_rules([{"tool": "execute_luau", "match": "fnv1a32", "text": json.dumps({
                   "ReplicatedStorage.TestLib.StoryHarness": "d678bae6", "ReplicatedStorage.TestLib.Gone": "missing"})},
               {"tool": "execute_luau", "match": "verifyLive", "text": skipped}])
    g = G.Gate(str(repo), "L1", "sprint", call_timeout=10)
    try:
        g.live_verify({"tests/lib/StoryHarness.lua": "d678bae6", "docs/progress/UI-T0_gate.json": "1"})
        assert g.checks[-1]["result"] == "PASS" and "matched=1/1" in g.checks[-1]["detail"]
        g.live_verify({"tests/lib/StoryHarness.lua": "d678bae6", "tests/lib/Gone.lua": "2"})
        assert g.checks[-1]["result"] == "FAIL" and "UNRESOLVED tests/lib/Gone.lua" in g.checks[-1]["detail"]
        g.live_verify({"docs/progress/UI-T0_gate.json": "1"})
        assert g.checks[-1]["result"] == "SKIP" and g.checks[-1]["required"] is False   # never PASS at 0
    finally:
        g.close()
    code = next(c for c in calls() if "fnv1a32" in c["args"].get("code", ""))["args"]["code"]
    assert "ReplicatedStorage.TestLib.StoryHarness" in code
    # a Studio-loaded file mounted nowhere is UNKNOWN, never FAIL 0/0 or PASS
    set_rules([{"tool": "execute_luau", "match": "verifyLive",
                "text": "SKIP (unmounted) tests/Nowhere.lua\nVERIFY matched=0/0 mismatched=0 unresolved=0 skipped=1"}])
    g = G.Gate(str(repo), "L2", "sprint", call_timeout=10)
    try:
        g.live_verify({"tests/Nowhere.lua": "3"})
        assert g.checks[-1]["result"] == "UNKNOWN" and "tests/Nowhere.lua" in g.checks[-1]["detail"]
    finally:
        g.close()


def test_hash_maps_reject_unsafe_names(repo):
    seen = []
    g = G.Gate(str(repo), "H1", "sprint", run_cmd=lambda *a: seen.append(a) or (0, ""))
    assert g._hash_maps(["src/a\u2019; Remove-Item x.lua"]) == {}
    assert g.checks[-1]["id"] == "host.hash_expect" and g.checks[-1]["result"] == "FAIL"
    assert seen == []                                                          # pwsh never ran


def test_bridge_serves_tests_from_head_only(repo):
    (repo / "tests" / "S28_check.lua").write_text("return 'WORKING TREE'", encoding="utf-8")
    b = G.RepoBridge(str(repo))
    port = b.start()
    try:
        body = urllib.request.urlopen(f"http://127.0.0.1:{port}/tests/S28_check.lua", timeout=10).read()
        assert body == b"return 'PASS: c1'"
        for bad in ("/.git/config", "/default.project.json", "/tests/../default.project.json",
                    "/tests/missing.lua"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(f"http://127.0.0.1:{port}{bad}", timeout=10)
    finally:
        b.stop()


# ---- bridge execution shapes (GF-GATE1 full run 2026-10-28) ------------------------------
def _lune():
    """A runnable Lune: AU_LUNE, rokit's tool storage (the rokit shim needs a project
    manifest in cwd), else lune on PATH."""
    stored = sorted(glob.glob(os.path.join(os.path.expanduser("~"), ".rokit", "tool-storage",
                                           "lune-org", "lune", "*", "lune*")))
    return os.environ.get("AU_LUNE") or (stored[-1] if stored else shutil.which("lune"))


LUNE = _lune()
BRIDGE_CASES = {
    # S00-S03: a script that prints its lines and returns nothing
    "script": 'local r = {}\ntable.insert(r, "PASS: a")\ntable.insert(r, "FAIL: b - why")\n'
              'print(table.concat(r, "\\n"))',
    # S05-S43: prints AND returns the same report (must not be doubled)
    "module": 'local out = "PASS: m1\\nPASS: m2"\nprint(out)\nreturn out',
    # S44: "module returns a function (call it for the report)"
    "function": 'local x = 1\nreturn function()\n    return "PASS: f" .. x\nend',
    # a suite that dies after printing: lines kept, the error named
    "crash": '--!nonstrict\nprint("PASS: before")\nlocal t = nil\nlocal y = t.field\nreturn "never"',
    # an early top-level return (failAll) inside a branch
    "early": 'local function failAll(r) return "FAIL: all - " .. r end\n'
             'if true then\n    return failAll("no Server")\nend\nprint("PASS: unreachable")',
}


@pytest.mark.skipif(LUNE is None, reason="lune (Luau runtime) not installed")
def test_bridge_wrapper_runs_every_suite_shape_under_luau(tmp_path):
    """The Luau the bridge builds, run for real (Lune's luau.load stands in for require of
    the throwaway ModuleScript). On the old bridge the script shape returned nothing
    ("Module code did not return exactly one value") and the function shape printed
    `function: 0x...` (S44 pass=0 fail=0)."""
    body = ["local luau = require(\"@lune/luau\")", G.BRIDGE_FORMAT]
    for name, src in BRIDGE_CASES.items():
        body.append(f"do local chunk = luau.load({G.luau_long(G.BRIDGE_PREFIX)} .. "
                    f"{G.luau_long(src)} .. {G.luau_long(G.BRIDGE_SUFFIX)}, {{debugName = \"{name}\"}})\n"
                    f"print(\"<<{name}>>\" .. __gateFormat(\"{name}.lua\", chunk()) .. \"<<end>>\") end")
    script = tmp_path / "bridge_shapes.luau"
    script.write_text("\n".join(body), encoding="utf-8")
    p = subprocess.run([LUNE, "run", str(script)], capture_output=True, text=True, encoding="utf-8",
                       timeout=60)
    assert p.returncode == 0, p.stderr + p.stdout
    got = dict(re.findall(r"<<(\w+)>>(.*?)<<end>>", p.stdout, re.S))
    assert got["script"] == "PASS: a\nFAIL: b - why"
    assert got["module"] == "PASS: m1\nPASS: m2"
    assert got["function"] == "PASS: f1"
    assert got["crash"].startswith("PASS: before\nFAIL: GATE bridge crash.lua — error: ")
    assert ":4: attempt to index nil" in got["crash"]               # the suite's own line number
    assert got["early"] == "FAIL: all - no Server"


def test_bridge_limits_name_what_the_runner_cannot_provide():
    assert "io.*" in G.bridge_limits('local s = io.read("C:/x")')
    assert G.bridge_limits('-- io.read(path) in a comment only\nreturn "PASS: x"') is None
    src = '-- start tools/runner_s136.py first\nlocal B = "http://127.0.0.1:8436"\nreturn 1'
    why = G.bridge_limits(src, port_open=lambda p: False)
    assert "127.0.0.1:8436 (tools/runner_s136.py)" in why
    assert G.bridge_limits(src, port_open=lambda p: True) is None
    assert G.capability_gaps("PASS: a\nFAIL: c1 - Needs a 2-client playtest (got 1 player)") != []
    assert G.capability_gaps("FAIL: C6 x - needs a Studio PLAY session with the client relay armed") != []
    assert G.capability_gaps("FAIL: c1 - Needs a 2-client playtest\nFAIL: c2 - hp 0 want 5") == []
    assert G.capability_gaps("PASS: a") == []


def test_bridge_payload_wraps_hands_over_and_skips_reboot_where_the_runbook_says(repo):
    """S33 gets AbilityService (HEAD) on _G.S33_ABILITY_SOURCE; S20 runs without the runner's
    ServiceLoader re-boot; every bridge wraps the source and formats through __gateFormat."""
    (repo / "src" / "server" / "Services").mkdir(parents=True)
    (repo / "src" / "server" / "Services" / "AbilityService.lua").write_text(
        "local AbilityService = {}\nreturn AbilityService", encoding="utf-8")
    for stem in ("S33_check", "S20_check", "S30_check"):
        (repo / "tests" / f"{stem}.lua").write_text('print("PASS: x")', encoding="utf-8")
    commit(repo, "suites")
    g = G.Gate(str(repo), "U1", "regression", create_dir=False)
    sent = {}

    def fake_call(label, payload, fn):
        sent[label.split()[-1]] = payload
        return "PASS: x", False
    g._call = fake_call
    g.console_lines = lambda: []
    try:
        for stem in ("S33_check", "S20_check", "S30_check"):
            assert g.run_bridge({"n": 3, "file": f"{stem}.lua", "vm": "server"}) == ("PASS: x", False)
    finally:
        g.close()
    s33, s20, s30 = sent["S33_check.lua"], sent["S20_check.lua"], sent["S30_check.lua"]
    assert "_G.S33_ABILITY_SOURCE = [[\nlocal AbilityService = {}\nreturn AbilityService]]" in s33
    assert s33.index("_G.S33_ABILITY_SOURCE = [[") < s33.index("pcall(require, mod)") \
        < s33.index("_G.S33_ABILITY_SOURCE = nil")
    assert "_G.S33_ABILITY_SOURCE" not in s30
    assert "if false then" in s20 and "if true then" in s30          # S20: no runner re-boot
    for code in (s33, s20, s30):
        assert G.BRIDGE_PREFIX in code and G.BRIDGE_SUFFIX in code
        assert "return __gateFormat(file, res)" in code and "__PREFIX__" not in code


def test_runner_lock_refuses_a_second_runner(repo):
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lock = repo / ".plan" / "tmp" / "gate" / G.LOCK_NAME
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": p.pid, "sprint": "UI-T0", "mode": "sprint"}))
        out = []
        assert G.main(["--repo", str(repo), "stories", "--sprint", "UI-T0"], out=out.append) == 2
        assert "BUSY" in out[-1] and f"pid {p.pid}" in out[-1]
        assert not gate_file(repo, "UI-T0", "status.json").exists()             # untouched
        out = []
        assert G.main(["--repo", str(repo), "stories", "--sprint", "UI-T0", "--background"], out=out.append) == 2
        assert "BUSY" in out[-1]
    finally:
        p.kill()
        p.wait()
    assert G.acquire_lock(str(repo), "x", "stories") is None                   # stale lock replaced
    G.release_lock(str(repo))


# ---- status / --wait / --background ----------------------------------------------
def _gate_dir(repo, sid):
    d = repo / ".plan" / "tmp" / "gate" / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_status_wait_until_done(repo):
    d = _gate_dir(repo, "W1")
    G.write_json(str(d / "status.json"), {"sprint": "W1", "pid": os.getpid(), "started": 0, "phase": "stories",
                                          "progress": "3/10", "updated": 0})

    def finish():
        time.sleep(0.4)
        G.write_json(str(d / "latest.json"), {"result": "PASS", "summary_line": "GATE W1 PASS", "checks": [],
                                              "head": "b" * 40, "mode": "sprint"})
        G.write_json(str(d / "status.json"), {"sprint": "W1", "pid": os.getpid(), "phase": "done"})
    threading.Thread(target=finish).start()
    out = []
    assert G.main(["--repo", str(repo), "status", "--sprint", "W1", "--wait", "0.2"], out=out.append, poll=0.05) == 0
    assert out[0] == "PHASE stories 3/10" and out[-1] == "GATE W1 PASS"
    assert sum(o.startswith("PHASE") for o in out) == 1                         # printed on change only


def test_status_wait_elapses_and_clamps(repo, monkeypatch):
    d = _gate_dir(repo, "W2")
    G.write_json(str(d / "status.json"), {"sprint": "W2", "pid": os.getpid(), "phase": "regression",
                                          "progress": "4/30"})
    out = []
    t0 = time.monotonic()
    assert G.main(["--repo", str(repo), "status", "--sprint", "W2", "--wait", "0.01"], out=out.append, poll=0.05) == 2
    assert time.monotonic() - t0 < 5
    assert out[-1].startswith("GATE W2 RUNNING phase=regression progress=4/30")
    assert "status --sprint W2 --wait 8" in out[-1]
    a = G.build_parser().parse_args(["status", "--sprint", "W2", "--wait"])
    assert a.wait == G.WAIT_DEFAULT_MINUTES
    seen = {}
    monkeypatch.setattr(G.time, "monotonic", lambda: seen.setdefault("t", 0.0))

    def sleep(s):
        seen["slept"] = s
        raise KeyboardInterrupt
    monkeypatch.setattr(G.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        G.run_status(str(repo), "W2", 60, out=lambda s: None, poll=1000)
    assert seen["slept"] == pytest.approx(G.WAIT_MAX_MINUTES * 60)              # 60 min clamped to 9


def test_status_dead_pid_and_none(repo):
    d = _gate_dir(repo, "W3")
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    G.write_json(str(d / "status.json"), {"sprint": "W3", "pid": p.pid, "phase": "stories"})
    out = []
    assert G.main(["--repo", str(repo), "status", "--sprint", "W3"], out=out.append, poll=0.05) == 2
    assert "is not running" in out[-1]
    out = []
    assert G.main(["--repo", str(repo), "status", "--sprint", "NOPE"], out=out.append) == 2
    assert out[-1].startswith("GATE NOPE NONE")


def test_background_regression_then_status_wait(fakes, repo):
    set_rules, _ = fakes
    set_rules(*regression_rules())
    out = []
    assert G.main(["--repo", str(repo), "regression", "--selection", "ui", "--sprint", "BG1", "--background"],
                  out=out.append) == 0
    assert out[0].startswith("GATE BG1 STARTED pid=")
    assert out[-1] == f"NEXT: {G.status_cmd(str(repo), 'BG1')}"
    assert out[-1].endswith("status --sprint BG1 --wait 8")
    st = json.load(open(gate_file(repo, "BG1", "status.json")))
    assert st["pid"] and st["sprint"] == "BG1"
    out = []
    rc = G.main(["--repo", str(repo), "status", "--sprint", "BG1", "--wait", "1.5"], out=out.append, poll=0.2)
    log = gate_file(repo, "BG1", "run.log").read_text(encoding="utf-8", errors="replace")
    assert rc == 0, (out, log)
    assert out[-1] == "GATE BG1 PASS"


def test_utf8_output_survives_a_cp1252_stdout(fakes, repo, tmp_path):
    """M9: Studio text (arrows, emoji) in a detail must not crash the runner after the
    verdict when stdout is a cp1252 file (the fast lane's log)."""
    set_rules, _ = fakes
    off, comm = regression_rules(extra_off=[{"tool": "get_console_output", "times": 1, "text": ""},
                                            {"tool": "get_console_output", "text": "\u2192 \U0001F600 error: x"}])
    set_rules(off, comm)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONPATH=root)
    p = subprocess.run([sys.executable, "-m", G.MODULE, "--repo", str(repo), "regression", "--selection", "ui",
                        "--sprint", "U1"], capture_output=True, env=env, timeout=120)
    text = p.stdout.decode("utf-8", "replace")
    assert p.returncode in (0, 1), p.stderr.decode("utf-8", "replace")[-800:]
    assert text.rstrip().splitlines()[-1] in ("GATE U1 PASS", "GATE U1 FAIL") and "Traceback" not in text


# ---- doctor ----------------------------------------------------------------------
DOCTOR_OK = json.dumps({"placeName": "AnimalUprising", "showDev": True, "http": True, "harness": True,
                        "gateRunner": True, "regressionIndex": True, "created": ["AU_390x844"],
                        "devices": {n: True for n, _, _ in G.DEVICES}})


def test_doctor_ready_and_not_ready(fakes, repo, monkeypatch):
    set_rules, _ = fakes
    set_rules([{"tool": "get_studio_state", "text": '{"mode": "Edit"}'},
               {"tool": "execute_luau", "match": "ShowDevelopmentGui", "text": DOCTOR_OK}])

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(G.urllib.request, "urlopen", lambda url, timeout: Resp())
    monkeypatch.setattr(G.shutil, "which", lambda n: "C:/lune.exe")
    out = []
    assert G.main(["--repo", str(repo), "doctor"], out=out.append) == 0, out
    assert out[-1] == "DOCTOR READY" and any("created AU_390x844" in o for o in out)
    assert any(o.startswith("PASS: doctor.community_studio") for o in out)

    bad = json.loads(DOCTOR_OK)
    bad.update(http=False, devices={"AU_1920x1080": True})
    set_rules([{"tool": "get_studio_state", "text": "Studio is playing"},
               {"tool": "execute_luau", "match": "ShowDevelopmentGui", "text": json.dumps(bad)}])

    def refuse(url, timeout):
        raise OSError("connection refused")
    monkeypatch.setattr(G.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(G.shutil, "which", lambda n: None)
    out = []
    assert G.main(["--repo", str(repo), "--place", "AnimalUprising", "doctor"], out=out.append) == 2
    fixes = [o for o in out if o.startswith("FIX ")]
    assert any("doctor.http_enabled" in f and "Allow HTTP Requests" in f for f in fixes)
    assert any("doctor.edit_mode" in f for f in fixes)
    assert any("doctor.devices" in f for f in fixes)
    assert any("doctor.rojo" in f and "34872" in f for f in fixes)
    assert any("doctor.lune" in f for f in fixes)
    assert out[-1] == "DOCTOR NOT READY"
    out = []
    assert G.main(["--repo", str(repo), "--place", "OtherPlace", "doctor"], out=out.append) == 2
    assert any(o.startswith("FIX doctor.studio") for o in out)
    assert any(o.startswith("FIX doctor.community_studio") for o in out)    # --place binds both


def test_stop_playtest_command(fakes, repo):
    set_rules, calls = fakes
    set_rules([], [{"tool": "solo_playtest", "text": '{"success": true, "action": "stop"}'}])
    out = []
    assert G.main(["--repo", str(repo), "stop-playtest"], out=out.append) == 0
    assert out[-1].startswith("STOPPED") and calls()[-1]["args"] == {"action": "stop"}


def test_mock_lane_renders_frames_as_checks_and_captures(tmp_path, monkeypatch):
    """lane "mock" (UI-D1): reference-renderer frames become story checks + captures;
    a failed frame is a FAIL check and no capture; no Studio call is made."""
    from foundry_coordinator.projects.animaluprising import reference_render as RR
    seen = []

    def fake_render(workspace, stories, sizes, *, out_dir=None, out=print, **_):
        seen.append((stories, sizes, out_dir))
        res = []
        for w, h in sizes or RR.SIZES:
            ok = not (stories == ["hud.combat"] and (w, h) == (390, 844))
            res.append({"path": os.path.join(out_dir, RR.ref_filename(stories[0], w, h)),
                        "size": RR.size_name(w, h), "ok": ok, "detail": "ok" if ok else "PNG is 1x1"})
        return res
    monkeypatch.setattr(RR, "render_references", fake_render)
    g = G.Gate(str(tmp_path), "UI-D1", "sprint", official=object(), community=object(),
               run_cmd=lambda *a, **k: (0, ""))
    g.run_mock([{"name": "hud.combat"}, {"name": "hud.pad", "sizes": ["1920x1080"]}, {"bad": 1}])
    by = {c["id"]: c["result"] for c in g.checks}
    assert by["story.hud.combat@1920x1080"] == "PASS"
    assert by["story.hud.combat@390x844"] == "FAIL"
    assert by["story.hud.pad@1920x1080"] == "PASS" and "story.hud.pad@390x844" not in by
    assert len(g.captures) == 5 and all(c["source"] == "mock" for c in g.captures)
    assert seen[1][1] == [(1920, 1080)]
    assert seen[0][2].endswith(os.path.join("docs", "evidence", "UI-v3", "UI-D1"))


def test_mock_lane_with_no_stories_fails(tmp_path):
    g = G.Gate(str(tmp_path), "UI-D1", "stories", official=object(), community=object(),
               run_cmd=lambda *a, **k: (0, ""))
    g.run_mock([])
    assert [c["result"] for c in g.checks if c["id"] == "story.none"] == ["FAIL"]


@pytest.mark.parametrize("bad", ["C:RobloxAnimalUprising", "relative/repo", "{missing}", "{nogit}"])
def test_repo_must_be_the_absolute_game_repo_root(repo, tmp_path, bad):
    nogit = tmp_path / "nogit"
    nogit.mkdir()
    (nogit / "default.project.json").write_text("{}")
    bad = bad.format(missing=tmp_path / "missing", nogit=nogit)
    out = []
    assert G.main(["--repo", bad, "status", "--sprint", "UI-T0"], out=out.append) == 2
    assert "ERROR: --repo" in out[-1] and "forward slashes" in out[-1]
    assert not (repo / "RobloxAnimalUprising").exists()


def _rc(cid, result, detail, required=True):
    return {"id": cid, "result": result, "required": required, "detail": detail, "artifacts": []}


def test_baseline_marks_pre_existing_regression_failures_non_required():
    """UI-02a (2026-10-20): six suites and 15 calls were red identically at the base commit,
    so the all-green gate could never pass. A baseline makes them non-blocking."""
    base = {"head": "a1b298bc9a4b" + "0" * 28, "checks": [
        _rc("regression.S49", "FAIL", "SUITE S49 pass=89 fail=20 expect=147 VERDICT FAIL"),
        _rc("regression.call50", "FAIL", "FAIL: GATE dynarg CALL 50 unresolved <unitId>"),
        _rc("regression.verdict", "FAIL", "FAIL: GATE regression 6 suite(s) failing")]}
    checks = [_rc("regression.S49", "FAIL", "SUITE S49 pass=89 fail=20 expect=147 VERDICT FAIL"),
              # renumbered call (the sprint retired suites): matched by text, not number
              _rc("regression.call47", "FAIL", "FAIL: GATE dynarg CALL 47 unresolved <unitId>"),
              _rc("regression.verdict", "FAIL", "FAIL: GATE regression 6 suite(s) failing"),
              _rc("story.Hud.normal@1920x1080", "PASS", "ok")]
    summary = G.apply_baseline(checks, base)
    assert summary["new_or_worse"] == [] and len(summary["pre_existing"]) == 2
    assert all(not c["required"] for c in checks if c["id"].startswith("regression."))
    assert "PRE-EXISTING at a1b298bc9a4b" in checks[0]["detail"]
    assert G.overall(checks) == "PASS"


def test_baseline_keeps_worse_and_new_failures_blocking():
    base = {"head": "b" * 40, "checks": [
        _rc("regression.S49", "FAIL", "SUITE S49 pass=89 fail=20 expect=147 VERDICT FAIL")]}
    checks = [_rc("regression.S49", "FAIL", "SUITE S49 pass=88 fail=21 expect=147 VERDICT FAIL"),
              _rc("regression.S50", "FAIL", "SUITE S50 pass=1 fail=1 expect=2 VERDICT FAIL"),
              _rc("regression.call9", "FAIL", "FAIL: something new CALL 9"),
              _rc("regression.verdict", "FAIL", "FAIL: GATE regression")]
    summary = G.apply_baseline(checks, base)
    assert set(summary["new_or_worse"]) == {"regression.S49", "regression.S50", "regression.call9"}
    assert all(c["required"] for c in checks)
    assert G.overall(checks) == "FAIL"
    assert G.apply_baseline([_rc("regression.S49", "FAIL", "x")], None) is None   # no baseline


def _carry_gate(tmp_path, changed, prev_checks, index=None):
    """A Gate with a fake git: HEAD descends from the previous sprint verdict's head and
    `changed` files differ between them."""
    gd = tmp_path / "gate"
    gd.mkdir(exist_ok=True)
    prev_head = "a" * 40
    (gd / f"{prev_head[:12]}.sprint.json").write_text(json.dumps(
        {"head": prev_head, "finished": 1.0, "checks": prev_checks, "captures": []}))

    def run_cmd(argv, cwd, timeout):
        if "merge-base" in argv:
            return 0, ""
        if "diff" in argv:
            return 0, "\n".join(changed)
        return 1, ""
    g = G.Gate.__new__(G.Gate)
    g.head, g.gate_dir, g.repo, g.run_cmd = "b" * 40, str(gd), str(tmp_path), run_cmd
    g.index = index or {"entries": [{"id": "S45", "file": "S45_check"}, {"id": "S46", "file": "S46_check"}],
                        "subsets": {"ui": ["S45", "S46"]}}
    return g


def test_carry_keeps_passed_suites_whose_files_did_not_change(tmp_path, monkeypatch):
    """2026-10-21: every sprint gate re-ran every suite and story, even those that passed and
    depend on nothing that changed. Passed suites with untouched files now carry."""
    monkeypatch.setattr(G, "suite_reader", lambda repo, rev: (lambda f: ""))
    passed = [_rc("regression.S45", "PASS", "SUITE S45 pass=12 fail=0 expect=12 VERDICT PASS"),
              _rc("regression.S46", "PASS", "SUITE S46 pass=23 fail=0 expect=23 VERDICT PASS"),
              _rc("story.Hud.normal@1920x1080", "PASS", "ok")]
    g = _carry_gate(tmp_path, ["tests/S46_check.lua"], passed)
    c = g.carry_plan("S45,S46", "docs/progress/UI-T0_gate.json")
    assert c["suites"] == ["S45"]                       # S46's own file changed: re-run
    assert c["stories"] is True                         # no src/ change: stories carry
    g = _carry_gate(tmp_path, ["src/client/UI/v3/Screens/HeroSelect.lua"], passed)
    assert g.carry_plan("S45,S46", "docs/progress/UI-T0_gate.json")["stories"] is False
    g = _carry_gate(tmp_path, ["tests/RegressionIndex.lua"], passed)     # harness change
    c = g.carry_plan("S45,S46", "docs/progress/UI-T0_gate.json")
    assert c["suites"] == [] and c["stories"] is False


def test_carry_never_carries_a_failed_suite(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "suite_reader", lambda repo, rev: (lambda f: ""))
    prev = [_rc("regression.S45", "FAIL", "SUITE S45 pass=11 fail=1 expect=12 VERDICT FAIL"),
            _rc("regression.S46", "PASS", "SUITE S46 pass=23 fail=0 expect=23 VERDICT PASS")]
    g = _carry_gate(tmp_path, ["docs/progress/UI-T0_PLAN.md"], prev)
    assert g.carry_plan("S45,S46", "docs/progress/UI-T0_gate.json")["suites"] == ["S46"]
