"""studio_gate `calls` mode (2026-10-27, reports/STUDIO_GATE_SCRIPTED_PLAN.md): the session
gate's Studio half as a script, with the fake MCP servers of test_studio_gate.py (never
Studio). The fake Studio plans exactly what GateRunner.run would for the explicit id list
(plan_for) and tallies with the pure-Python GateRunner.tally mirror."""

import json
import os
import re
import subprocess

import pytest

from foundry_coordinator import live_tests
from foundry_coordinator.projects.animaluprising import studio_gate as G
from tests.coordinator.test_studio_gate import (  # noqa: F401  (fixtures)
    BOOT_C, BOOT_S, ENV_BRIDGE, INDEX, OFFICIAL_OBJ, PLAYER, VERIFY_OK, commit, fakes, gate_file,
    repo)

UI = ["S28", "S46", "S49", "S45", "S04"]


@pytest.fixture(autouse=True)
def lock_dir(tmp_path, monkeypatch):
    """AU's studio.lock lives in a temp folder in every test (never C:/Roblox/_scratch)."""
    d = tmp_path / "locks"
    d.mkdir()
    monkeypatch.setenv("AU_LOCK_DIR", str(d))
    return d


def plan_for(index_text: str, ids: list) -> str:
    """What GateRunner.run(<ids>) prints (buildPlan + emitCall)."""
    idx = G.parse_regression_index(index_text)
    by = {e["id"]: e for e in idx["entries"]}
    out = [f"REGRESSION PLAN {'+'.join(ids)} - {len(ids)} suite(s)",
           f"CALL 1 [vm=server] robloxstudio-mcp eval_server_runtime :: {BOOT_S}",
           f"CALL 2 [vm=client] robloxstudio-mcp eval_client_runtime :: {BOOT_C}"]
    n = 2
    for i in ids:
        e = by[i]
        for si, step in enumerate(e["steps"], 1):
            n += 1
            vm = G.step_vm(step, e)
            if vm == "offstudio":
                out.append(f"CALL {n} [vm=host] SKIP (offstudio - run x) :: recorded as SKIP, never as PASS")
                continue
            if e.get("loader") == "bridge" or step == "main":
                out.append(f"CALL {n} [vm={vm}] MANUAL (bridge) :: run tests/{e['file']}.lua per its "
                           f"header runbook in a fresh {vm.upper()} VM")
                continue
            if si in (e.get("tightBefore") or []):
                out.append(f"NOTE {i}: CALL {n} must be issued back-to-back with CALL {n - 1} in ONE "
                           f"tool response (client timing window)")
            if re.search(r"<[\w]+>", step):
                out.append(f"NOTE {i}: CALL {n} is DYNARG - paste the <token> value")
            tool = {"server": "robloxstudio-mcp eval_server_runtime",
                    "client": "robloxstudio-mcp eval_client_runtime",
                    "edit": "Roblox_Studio execute_luau (datamodel Edit)"}[vm]
            out.append(f"CALL {n} [vm={vm}] {tool} :: {G.step_payload(e, step)[1]}")
    # GateRunner.run always ends its plan with this NOTE (a cut-off reply lacks it)
    out.append("NOTE when all CALLs are done: run GateRunner.tally(...)")
    return "\n".join(out)


def mirror_for(index_text: str, ids: list) -> dict:
    idx = G.parse_regression_index(index_text)
    by = {e["id"]: e for e in idx["entries"]}
    suites, n = [], 2
    for i in ids:
        e = by[i]
        calls = list(range(n + 1, n + 1 + len(e["steps"])))
        n += len(e["steps"])
        suites.append({"id": i, "calls": calls, "expect": e.get("expectPass"),
                       "designedFails": e.get("designedFails", 0),
                       "offstudio": e.get("vm") == "offstudio"})
    return {"label": "+".join(ids), "boot": [1, 2], "suites": suites}


def rules(ids, index=INDEX, extra_off=(), extra_comm=(), verify=VERIFY_OK):
    official = list(extra_off) + [
        verify,
        {"tool": "execute_luau", "match": r"\.run\(", "text": plan_for(index, ids)},
        {"tool": "execute_luau", "match": r"\.tally\(", "mirror": mirror_for(index, ids)},
        {"tool": "execute_luau", "match": "S.src", "text": OFFICIAL_OBJ},
        {"tool": "get_console_output", "text": ""}]
    community = list(extra_comm) + [
        PLAYER,
        {"tool": "eval_server_runtime", "match": "boot\\(\\)",
         "text": "BOOT(server) loaded=36 errors=0\nPASS: BOOT counts == EXPECTED_BOOT, 0 errors (server - 36 loaded)"},
        {"tool": "eval_client_runtime", "match": "boot\\(\\)",
         "text": "BOOT(client) loaded=13 errors=0\nPASS: BOOT counts == EXPECTED_BOOT, 0 errors (client - 13 loaded)"},
        {"tool": "eval_server_runtime", "match": "setupServer", "text": "PASS: s1\nINFO unitId=99"},
        {"tool": "eval_client_runtime", "match": "clientFog", "text": "PASS: fog"},
        {"tool": "eval_server_runtime", "match": "S.prep", "text": "PASS: prep"},
        {"tool": "eval_server_runtime", "match": "S.seed",
         "text": json.dumps({"ok": True, "bridge": "ok", "result": json.dumps(
             ["PASS: seed", json.dumps({"unitId": 77})]), "output": []})},
        {"tool": "eval_client_runtime", "match": "clientCorrections1", "text": "PASS: corr"},
        {"tool": "execute_luau", "match": "GetAsync", "text": ENV_BRIDGE},
    ]
    return official, community


def host_half(repo, sprint, ok=True, head=None):
    """What gate.ps1's host half leaves behind: the HEAD-stamped verdicts + the file map."""
    head = head or subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                                  text=True).stdout.strip()
    d = repo / "docs" / "progress"
    (d / f"{sprint}_gate_verdicts.json").write_text(json.dumps({
        "headSha": head, "sprint": sprint, "tier": "A", "HEAD": True, "clean tree": True,
        "index suites": True, "crlf": True, "hash expect": True, "lune": ok,
        "lune negative control": True, "NEXT": True}), encoding="utf-8")
    (d / f"{sprint}_gate_expect.json").write_text(json.dumps({
        "rev": head, "files": {"tests/S28_check.lua": "1dd2c64f"}}), encoding="utf-8")
    return head


def run_calls(repo, sprint, *extra, out=None, **inject):
    out = [] if out is None else out
    rc = G.main(["--repo", str(repo), "--timeout", "20", "--run-id", f"rid-{sprint}", "calls",
                 "--sprint", sprint, *extra], out=out.append, **inject)
    return rc, out


def lines_of(repo, sprint):
    return (repo / "docs" / "progress" / f"{sprint}_studio_lines.txt").read_text(encoding="ascii")


def finish_would_pass(text: str) -> bool:
    """tools/gate.ps1 -Finish's Studio-file rules (tier A), ported line for line."""
    ls = text.splitlines()
    studio_fail = sum(1 for x in ls if re.match(r"^FAIL: GATE[\[\] ]", x))
    suites = sum(1 for x in ls if x.startswith("SUITE ") or re.match(r"^(PASS|FAIL|SKIP): GATE suite ", x))
    regs = sum(1 for x in ls if x.startswith("GATE regression "))
    boot_pass = sum(1 for x in ls if x.startswith("PASS: BOOT counts"))
    boot_fail = sum(1 for x in ls if x.startswith("FAIL: GATE boot"))
    return studio_fail == 0 and boot_pass >= 2 and boot_fail == 0 and (suites + regs) > 0


def tool_calls(calls, tool):
    return [c for c in calls() if c["tool"] == tool]


# ---- pure parts --------------------------------------------------------------------
def test_select_gate_ports_gate_ps1_d3_subset_and_suites():
    idx = G.parse_regression_index(INDEX.replace(
        'id = "S60", file = "S60_check", vm = "server", steps = { "main" } }',
        'id = "S60", file = "S60_check", vm = "server", steps = { "main" }, expectPass = nil }'))
    full = G.select_gate(idx, "full")
    # D3: S60 (expectPass = nil) is out of full; S50 has no expectPass field at all
    assert [e["id"] for e in full["rows"]] == UI + ["S50"]
    assert any("S50 has no expectPass field" in p for p in full["problems"])
    sub = G.select_gate(idx, "subset:ui", "S49")
    assert [e["id"] for e in sub["rows"]] == UI                 # gate.ps1's NEXT order kept
    assert sub["suites"] == ["S49"] and not sub["problems"]
    own = G.select_gate(idx, "S45", "S28,S46")
    assert [e["id"] for e in own["rows"]] == ["S45", "S28", "S46"]   # own suites appended
    assert [e["id"] for e in G.select_gate(idx, "ui")["rows"]] == UI               # bare subset name
    assert G.select_gate(idx, "S45, S46")["regression"] == ["S45", "S46"]
    assert any("unknown subset 'nope'" in p for p in G.select_gate(idx, "subset:nope")["problems"])
    assert any("'S99' has no index row" in p for p in G.select_gate(idx, "S99")["problems"])
    assert any("S60 is selected but unpinned" in p for p in G.select_gate(idx, "S60")["problems"])
    assert any("-Suites X1 has no index row" in p for p in G.select_gate(idx, "none", "X1")["problems"])
    assert G.select_gate(idx, "none")["rows"] == []
    two = G.parse_regression_index(INDEX.replace('id = "S45", file', 'id = "S45", players = 2, file'))
    assert G.select_gate(two, "subset:ui")["manual"] == ["S45"]
    disp, last = G.display_numbers(G.select_gate(idx, "subset:ui")["rows"])
    assert last == 9 and disp[("S46", 2)] == 5 and disp[("S04", 1)] == 9


REAL_REPO = os.environ.get("AU_INDEX_REPO", "C:/Roblox/_scratch/lc-studio")
REAL_REV = "fec207b4"


def _real_index():
    try:
        p = subprocess.run(["git", "-C", REAL_REPO, "show", f"{REAL_REV}:tests/RegressionIndex.lua"],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.decode("utf-8") if p.returncode == 0 else None


@pytest.mark.skipif(_real_index() is None, reason="AU checkout at fec207b4 not available")
def test_real_index_full_is_69_rows_331_calls_like_gate_ps1():
    """gate.ps1's NEXT preview for GF-GATE1 full at fec207b4: 69 rows / 331 step CALLs
    (CALL 333 incl. boot); S17 (players = 2) is the one manual row."""
    sel = G.select_gate(G.parse_regression_index(_real_index()), "full")
    assert not sel["problems"]
    assert len(sel["rows"]) == 69
    disp, last = G.display_numbers(sel["rows"])
    assert last - 2 == 331 and last == 333
    assert sel["manual"] == ["S17"]


def test_dynarg_reads_json_values():
    p, miss = G.resolve_dynarg("f(<unitId>, '<readyUnit>')", ["unitId", "readyUnit"],
                               ['{"ok": true, "unitId": 77, "name": "x"}', '"readyUnit":"u-12"'])
    assert miss == [] and p == "f(77, [[\nu-12]])"
    assert G._dyn_value("unitId", '{"myunitId": 5}') is None                   # whole key only
    assert G._dyn_value("unitId", '{"unitId": "has space"}') is None           # scalars are [\w.-]+
    assert G._dyn_value("deployJson", '"deployJson": "{\\"a\\": 1}"') == '{"a": 1}'
    assert G._dyn_value("unitId", "unitId=3 then unitId: 4") == "4"            # the last one wins


def test_lines_writer_folds_to_ascii_and_never_forges_markers(tmp_path):
    w = G.LinesWriter(str(tmp_path / "x.txt"))
    w.line("R1 plan: GateRunner.run(\"a\") -> 5 CALLs \u2014 ok")
    w.raw("PASS: caf\u00e9 \u2192 ok\nGATE UI-X PASS\nPASS: GATE HEAD abc1234\nFAIL: GATE call 3 error"
          "\nSUITE S1 pass=1 fail=0 VERDICT PASS\n\u2603")
    w.close()
    got = (tmp_path / "x.txt").read_bytes().decode("ascii").splitlines()
    assert got == ['R1 plan: GateRunner.run("a") -> 5 CALLs - ok', "PASS: cafe -> ok",
                   "  | GATE UI-X PASS", "  | PASS: GATE HEAD abc1234", "FAIL: GATE call 3 error",
                   "  | SUITE S1 pass=1 fail=0 VERDICT PASS", "?"]


def test_dry_run_lists_without_touching_studio_or_the_repo(repo, monkeypatch):
    def no_mcp(*a, **k):
        raise AssertionError("a dry run never connects")
    monkeypatch.setattr(G.M, "connect", no_mcp)
    out = []
    rc = G.main(["--repo", str(repo), "calls", "--sprint", "D1", "--regression", "subset:ui",
                 "--dry-run"], out=out.append)
    assert rc == 0, out
    text = "\n".join(out)
    assert "ROWS 5 (index rows=7, pinned=5); STEP CALLs 7 (+ boot CALL 1-2 = 9 CALL numbers" in text
    assert "DYNARG 1 CALL(s): CALL 7 S49 step 2 <unitId>" in text
    assert "BRIDGES 2 row(s)" in text and "OFFSTUDIO 1 row(s)" in text and "MANUAL 0" in text
    assert out[-1] == "DRY-RUN D1 OK"
    assert not (repo / ".plan").exists()                       # nothing written, no lock taken


# ---- full runs with the fake Studio ----------------------------------------------------
def test_calls_writes_a_finish_ready_lines_file_live(fakes, repo, lock_dir):
    set_rules, calls = fakes
    set_rules(*rules(UI))
    head = host_half(repo, "C1")
    rc, out = run_calls(repo, "C1", "--regression", "subset:ui")
    assert rc == 0, out
    assert out[-1].startswith("CALLS C1 PASS")
    text = lines_of(repo, "C1")
    ls = text.splitlines()
    assert ls[0].startswith(f"C1 scripted Studio run at HEAD {head} (studio_gate calls, run_id rid-C1;")
    assert any(x.startswith("R1 plan: GateRunner.run(\"S28,S46,S49,S45,S04\") -> 9 CALLs (boot 1-2, "
                            "S28 3, S46 4-5, S49 6-7, S45 8, S04 9)") for x in ls)
    i_pre = next(i for i, x in enumerate(ls) if x.startswith("PRE-PLAY VERIFY"))
    i_post = next(i for i, x in enumerate(ls) if x.startswith("POST-PLAY VERIFY"))
    i_first_call = next(i for i, x in enumerate(ls) if x.startswith("CALL "))
    assert i_pre < i_first_call < i_post
    assert "VERIFY matched=1/1 mismatched=0 unresolved=0 skipped=0" in ls[i_pre + 1:i_first_call]
    for n in range(3, 10):
        assert any(x.startswith(f"CALL {n} [vm=") for x in ls), n
    assert any(x.startswith("CALL 7 [vm=client] eval_client_runtime DYNARG :: ") and
               "clientCorrections1(77)" in x for x in ls)                   # JSON DYNARG resolved
    assert "SUITE S46 pass=2 fail=0 expect=2 designedFails=0 VERDICT PASS" in ls
    assert text.isascii() and finish_would_pass(text)
    # the control center's tracker reads it as a Studio CALL file: CALL 9 of 9
    c = live_tests.read(str(repo / "docs" / "progress" / "C1_studio_lines.txt"))
    assert c.calls == 9 and c.call == 9 and c.failed == 0
    v = json.load(open(gate_file(repo, "C1", f"{head[:12]}.calls.json")))
    assert v["mode"] == "calls" and v["result"] == "PASS" and v["run_id"] == "rid-C1"
    assert v["counts"] == {"rows": 5, "fresh": 5, "carried": 0, "passed": 4, "failed": 0,
                           "unknown": 0, "manual": 0}
    assert v["rows"]["S46"]["calls"] == [4, 5] and v["lines_sha256"]
    assert "PASS: BOOT counts" in v["evidence"]["boot"] and "VERIFY matched=1/1" in v["evidence"]["verify_pre"]
    assert "SUITE S28" in v["evidence"]["tally"]
    summary = (gate_file(repo, "C1", "calls_summary.txt")).read_text(encoding="utf-8")
    assert summary.splitlines()[0].startswith("GATE C1 scripted Studio half PASS")
    assert len(summary.splitlines()) == 1                        # failures only: none
    assert json.load(open(gate_file(repo, "C1", "status.json")))["phase"] == "done"
    assert not (repo / ".plan" / "tmp" / "gate" / G.LOCK_NAME).exists()
    assert not (lock_dir / "studio.lock").exists()               # AU's studio.lock released
    run = next(c for c in calls() if ".run(" in c["args"].get("code", ""))
    assert "S28,S46,S49,S45,S04" in run["args"]["code"]          # the explicit ordered id list


def test_manual_rows_are_listed_skipped_and_never_red(fakes, repo):
    set_rules, calls = fakes
    index = INDEX.replace('id = "S45", file', 'id = "S45", players = 2, file')
    (repo / "tests" / "RegressionIndex.lua").write_text(index, encoding="utf-8")
    commit(repo, "S45 needs two players")
    set_rules(*rules(["S28", "S46", "S49", "S04"], index=index))
    host_half(repo, "M1")
    rc, out = run_calls(repo, "M1", "--regression", "subset:ui")
    assert rc == 0, out
    text = lines_of(repo, "M1")
    assert "NOTE MANUAL S45 (players=2) CALL 8: needs a 2-player playtest (session runs it with " \
           "multiplayer_playtest)" in text
    assert "CALL 8 [vm=edit] MANUAL (players=2) :: S45 - needs a 2-player playtest" in text
    assert "SKIP: GATE suite S45 - MANUAL: needs a 2-player playtest" in text
    assert finish_would_pass(text)
    assert not any("S.src" in c["args"].get("code", "") for c in calls())       # never run
    v = json.load(open(gate_file(repo, "M1", "latest/calls.json")))
    assert v["result"] == "PASS" and v["counts"]["manual"] == 1
    assert v["manual"] == [{"row": "S45", "players": 2, "calls": [8], "why": G.MANUAL_WHY}]
    assert v["rows"]["S45"]["verdict"] == "MANUAL"
    assert "MANUAL S45 (CALL 8)" in "\n".join(v["summary"])


def test_rerun_failed_carries_only_passed_untouched_regression_rows(fakes, repo):
    set_rules, calls = fakes
    off, comm = rules(UI, extra_comm=[{"tool": "eval_client_runtime", "match": "clientFog",
                                       "text": "FAIL: fog missing"}])
    set_rules(off, comm)
    h1 = host_half(repo, "R1")
    rc, out = run_calls(repo, "R1", "--regression", "subset:ui")
    assert rc == 1, out
    v1 = json.load(open(gate_file(repo, "R1", f"{h1[:12]}.calls.json")))
    assert [f["row"] for f in v1["failures"]] == ["S46"]
    assert v1["failures"][0]["fail_lines"] == ["FAIL: fog missing"]
    assert v1["failures"][0]["calls"] == [4, 5] and v1["failures"][0]["lines"]
    assert not finish_would_pass(lines_of(repo, "R1"))           # FAIL: GATE suite S46
    summary = gate_file(repo, "R1", "calls_summary.txt").read_text(encoding="utf-8")
    assert "FAIL S46 (CALL 4-5; docs/progress/R1_studio_lines.txt lines" in summary
    assert "S28" not in summary                                   # passing rows never listed
    # the fix lands (a docs-only commit here: no file a suite covers changes)
    (repo / "docs" / "notes.md").write_text("fixed\n", encoding="utf-8")
    commit(repo, "fix")
    set_rules(*rules(["S28", "S46", "S04"]))
    h2 = host_half(repo, "R1")
    n0 = len(calls())
    rc, out = run_calls(repo, "R1", "--suites", "S28", "--regression", "subset:ui", "--rerun", "failed")
    assert rc == 0, out
    text = lines_of(repo, "R1")
    # S49 and S45 passed at h1 and nothing they cover changed: carried. S28 is the sprint's
    # own suite (never carried), S46 failed (never carried), S04 was a SKIP (not a PASS)
    assert "CALL 6 CARRIED from " + h1[:12] + " PASS (S49 step 1)" in text
    assert "CALL 8 CARRIED from " + h1[:12] + " PASS (S45 step 1)" in text
    assert "PASS: GATE suite S49 - CARRIED from " + h1[:12] in text
    assert "CALL 3 [vm=server] bridge" in text and "CALL 9 [vm=host] SKIP" in text
    assert finish_would_pass(text)
    run = next(c for c in calls()[n0:] if ".run(" in c["args"].get("code", ""))
    assert "S28,S46,S04" in run["args"]["code"]
    v2 = json.load(open(gate_file(repo, "R1", f"{h2[:12]}.calls.json")))
    assert v2["counts"]["carried"] == 2 and v2["rows"]["S49"]["carried_from"] == h1
    # --rerun all: nothing carries
    out = []
    G.main(["--repo", str(repo), "calls", "--sprint", "R1", "--regression", "subset:ui",
            "--rerun", "all", "--dry-run"], out=out.append)
    assert "CARRY 0 row(s) (--rerun all)" in "\n".join(out)
    out = []
    G.main(["--repo", str(repo), "calls", "--sprint", "R1", "--regression", "subset:ui",
            "--dry-run"], out=out.append)
    # everything passed at h2 (S45/S49 as carried checks): all four regression rows carry
    # (S04 was a SKIP)
    assert (f"CARRY 4 row(s): S28 from {h2[:12]}, S45 from {h2[:12]}, S46 from {h2[:12]}, "
            f"S49 from {h2[:12]}") in "\n".join(out)


def test_refuses_without_a_host_half_pass_at_head(fakes, repo, monkeypatch):
    set_rules, calls = fakes
    set_rules(*rules(UI))
    rc, out = run_calls(repo, "H1", "--regression", "subset:ui")
    assert rc == 2 and "C1" not in out[-1] and "missing or unreadable" in out[-1]
    host_half(repo, "H1", head="a" * 40)                          # stamped at another HEAD
    rc, out = run_calls(repo, "H1", "--regression", "subset:ui")
    assert rc == 2 and "the host half must pass at the current HEAD" in out[-1]
    host_half(repo, "H1", ok=False)                               # lune failed
    rc, out = run_calls(repo, "H1", "--regression", "subset:ui")
    assert rc == 2 and "did not pass" in out[-1] and "lune" in out[-1]
    assert calls() == []                                          # Studio never touched


def test_head_moved_during_the_run_is_a_tooling_error(fakes, repo):
    set_rules, calls = fakes
    set_rules(*rules(UI))
    host_half(repo, "HM")
    seen = []

    def run_cmd(argv, cwd, timeout):
        if argv == ["git", "rev-parse", "HEAD"]:
            seen.append(1)
            if len(seen) > 1:
                return 0, "b" * 40
        return G.default_run_cmd(argv, cwd, timeout)
    rc, out = run_calls(repo, "HM", "--regression", "subset:ui", run_cmd=run_cmd)
    assert rc == 2, out
    assert any("HEAD moved during the run" in o for o in out)
    assert "FAIL: GATE HEAD moved during the run" in lines_of(repo, "HM")
    assert json.load(open(gate_file(repo, "HM", "latest/calls.json")))["result"] == "ERROR"


def test_verify_mismatch_runs_no_call(fakes, repo):
    set_rules, calls = fakes
    bad = {"tool": "execute_luau", "match": "verifyLive",
           "text": "MISMATCH tests/S28_check.lua live=00000000 head=1dd2c64f\n"
                   "VERIFY matched=0/1 mismatched=1 unresolved=0 skipped=0"}
    set_rules(*rules(UI, verify=bad))
    host_half(repo, "V1")
    rc, out = run_calls(repo, "V1", "--regression", "subset:ui")
    assert rc == 2, out
    text = lines_of(repo, "V1")
    assert "FAIL: GATE verify PRE-PLAY - matched=0/1 mismatched=1" in text
    assert not any(x.startswith("CALL ") for x in text.splitlines())
    assert tool_calls(calls, "solo_playtest") == []
    assert not any(".run(" in c["args"].get("code", "") for c in calls())
    v = json.load(open(gate_file(repo, "V1", "latest/calls.json")))
    assert v["result"] == "ERROR" and v["tooling"] == "verify"


def test_group_budget_makes_a_hung_group_unknown_and_the_run_moves_on(fakes, repo):
    set_rules, calls = fakes
    off, comm = rules(UI, extra_comm=[{"tool": "eval_server_runtime", "match": "S.seed",
                                       "text": "PASS: seed\nINFO unitId=77", "sleep": 4}])
    set_rules(off, comm)
    host_half(repo, "B1")
    rc, out = run_calls(repo, "B1", "--regression", "subset:ui", "--group-min", "0.05")
    assert rc == 1, out
    text = lines_of(repo, "B1")
    assert re.search(r"^FAIL: GATE call 7 timeout - suite group S49 over its 0\.05 min budget", text, re.M)
    assert not any("clientCorrections1" in c["args"].get("code", "") for c in calls())
    assert any("clientFog" in c["args"].get("code", "") for c in calls())   # S46 still ran (last)
    v = json.load(open(gate_file(repo, "B1", "latest/calls.json")))
    assert v["rows"]["S49"]["verdict"] == "UNKNOWN" and v["rows"]["S46"]["verdict"] == "PASS"
    assert sum(1 for c in calls() if "S.seed" in c["args"].get("code", "")) == 1   # never retried


def test_two_mcp_failures_in_a_row_are_a_tooling_error(fakes, repo):
    set_rules, calls = fakes
    off, comm = rules(UI, extra_comm=[{"tool": "solo_playtest", "match": "start", "exit": True}])
    set_rules(off, comm)
    host_half(repo, "T1")
    rc, out = run_calls(repo, "T1", "--regression", "subset:ui")
    assert rc == 2, out
    v = json.load(open(gate_file(repo, "T1", "latest/calls.json")))
    assert v["result"] == "ERROR" and "MCP connect/transport failures in a row" in v["error"]


def test_studio_lock_held_by_another_sprint_refuses_and_is_left_alone(fakes, repo, lock_dir):
    set_rules, calls = fakes
    set_rules(*rules(UI))
    host_half(repo, "L1")
    (lock_dir / "studio.lock").write_text("coordinator 2026-10-27T10:00:00Z OTHER-1 playtest\n")
    rc, out = run_calls(repo, "L1", "--regression", "subset:ui")
    assert rc == 2 and "studio.lock is held: coordinator 2026-10-27T10:00:00Z OTHER-1" in out[-1]
    assert calls() == []
    assert (lock_dir / "studio.lock").read_text().startswith("coordinator")
    assert not (repo / ".plan" / "tmp" / "gate" / G.LOCK_NAME).exists()
    # the runner's own lock line: taken and released around a run
    (lock_dir / "studio.lock").unlink()
    assert G.studio_lock_acquire("L1", "x\ny") is None
    assert G.studio_lock_read().split()[0] == G.STUDIO_LOCK_OWNER and "x y" in G.studio_lock_read()
    assert G.studio_lock_acquire("L2", "z").startswith(G.STUDIO_LOCK_OWNER)
    assert G.studio_lock_release("L2") is False and G.studio_lock_release("L1") is True


def test_a_long_plan_is_fetched_in_pages_and_renumbered(repo):
    """2026-10-28: Studio's MCP reply is cut at ~100,000 characters; GF-GATE1's full plan
    (68 rows, 330 CALLs) came back as 254 CALLs and every row was refused. The plan is
    fetched in pages and renumbered as one plan; a cut-off page is a clear error."""
    text = (repo / "tests" / "RegressionIndex.lua").read_text(encoding="utf-8")
    idx = G.parse_regression_index(text)
    ids = [e["id"] for e in idx["entries"]][:5]
    g = G.Gate.__new__(G.Gate)
    g.index = idx
    g.PLAN_PAGE_STEPS = 2                       # force several pages
    asked = []

    def edit(code, label):
        sel = re.search(r"\.run\(\[(=*)\[(.*?)\]\1\]\)", code, re.S).group(2).strip()
        asked.append(sel)
        return plan_for(text, sel.split(",")), None
    g.edit = edit
    whole = G.parse_plan(plan_for(text, ids))
    paged = g.plan(",".join(ids))
    assert paged["error"] is None and len(asked) > 1
    assert [(c["n"], c["vm"], c["payload"]) for c in paged["calls"]] == \
        [(c["n"], c["vm"], c["payload"]) for c in whole["calls"]]
    assert G.map_calls(paged, idx, ",".join(ids)) is not None
    # a page that comes back cut off (no closing NOTE) is reported, never guessed
    cut = plan_for(text, ids).rsplit("\nNOTE when", 1)[0] + "... (truncated)"
    g.edit = lambda code, label: (cut, None)
    g.PLAN_PAGE_STEPS = 1000
    assert "cut off" in g.plan(",".join(ids))["error"]
