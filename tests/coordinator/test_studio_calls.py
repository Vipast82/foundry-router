"""[studio_calls] (2026-10-27, reports/STUDIO_GATE_SCRIPTED_PLAN.md): the coordinator runs a
sprint gate's Studio half for an idle session that asked with a STUDIO_GATE block.

The pipeline is a tiny fake script (written per test) that writes what the real one
leaves behind: calls_pipeline.json, the lines file, the evidence log's -Finish block, the
gate verdicts and <head12>.calls.json. The real pipeline's sequencing is tested in-process
with a fake step runner."""

from __future__ import annotations

import json
import textwrap
import time
from types import SimpleNamespace

import pytest

from foundry_coordinator import states as S
from foundry_coordinator import studio_calls as SC
from foundry_coordinator.adapters.scripted import ScriptedClient
from foundry_coordinator.profile import ProfileError, load
from tests.coordinator.helpers import coordinator, git, make_repo, write_profile

T1 = {"id": "T1", "title": "Gate", "spec": "Build the thing.",
      "scope": ["src/a.txt", "docs/progress/T1_*"]}
BLOCK = ('```STUDIO_GATE\n{"suites": "S51", "regression": "subset:ui", "tier": "A", '
         '"rerun": "failed"}\n```\n')
NOT_DONE = '```coordinator-report\n{"status": "NOT DONE", "summary": "gate requested", "next": "gate"}\n```'
DONE = '```coordinator-report\n{"status": "DONE", "summary": "ok"}\n```'

FAKE_PIPELINE = textwrap.dedent('''\
    """Fake studio_calls pipeline: `pipeline --repo R --sprint ID --run-id X ...`; one plan
    entry per run from the JSON list named by FAKE_CALLS_PLAN."""
    import json, os, subprocess, sys, time
    a = sys.argv[1:]
    assert a[0] == "pipeline", a
    repo, sprint, rid = a[a.index("--repo") + 1], a[a.index("--sprint") + 1], a[a.index("--run-id") + 1]
    plan_path = os.environ["FAKE_CALLS_PLAN"]
    steps = json.load(open(plan_path))
    n_path = plan_path + ".n"
    n = int(open(n_path).read()) if os.path.exists(n_path) else 0
    open(n_path, "w").write(str(n + 1))
    step = steps[min(n, len(steps) - 1)]
    open(plan_path + ".args." + str(n), "w").write(json.dumps(a))
    head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    gd = os.path.join(repo, ".plan", "tmp", "gate", sprint)
    os.makedirs(gd, exist_ok=True)
    pj = os.path.join(gd, "calls_pipeline.json")
    def save(**kw):
        d = {"run_id": rid, "sprint": sprint, "pid": os.getpid(), "head": head,
             "updated": time.time(), **kw}
        with open(pj + ".tmp", "w") as f:
            json.dump(d, f)
        for _ in range(100):        # the coordinator may be reading it (Windows)
            try:
                os.replace(pj + ".tmp", pj)
                break
            except PermissionError:
                time.sleep(0.05)
    save(phase="calls")
    json.dump({"sprint": sprint, "pid": os.getpid(), "phase": "calls", "progress": "S51 0/3",
               "updated": time.time(), "mode": "calls", "playing": False},
              open(os.path.join(gd, "status.json"), "w"))
    time.sleep(step.get("sleep", 0))
    res = step["result"]
    if res == "ERROR":
        save(phase="done", result="ERROR", stage="calls", error=step.get("error", "boom"))
        sys.exit(2)
    prog = os.path.join(repo, "docs", "progress")
    os.makedirs(prog, exist_ok=True)
    lines = [f"{sprint} scripted Studio run at HEAD {head} (studio_gate calls, run_id {rid}; executor studio_gate)",
             "R1 plan: GateRunner.run(\\"S51,S46\\") -> 6 CALLs (boot 1-2, S51 3-4, S46 5-6)",
             "PRE-PLAY VERIFY (GateRunner.verifyLive): PASS",
             "MATCH tests/S51_check.lua live=a444ba0b head=a444ba0b",
             "VERIFY matched=1/1 mismatched=0 unresolved=0 skipped=0",
             "CALL 1 [vm=server] eval_server_runtime :: boot",
             "BOOT(server) loaded=48 errors=0",
             "PASS: BOOT counts == EXPECTED_BOOT, 0 errors (server - 48 loaded)",
             "CALL 2 [vm=client] eval_client_runtime :: boot",
             "BOOT(client) loaded=13 errors=0",
             "PASS: BOOT counts == EXPECTED_BOOT, 0 errors (client - 13 loaded)"]
    fail = res == "FAIL"
    tally = ["SUITE S51 pass=2 fail=0 expect=2 designedFails=0 VERDICT PASS", "PASS: GATE suite S51",
             ("SUITE S46 pass=1 fail=1 expect=2 designedFails=0 VERDICT FAIL - pass=1 (want 2)" if fail
              else "SUITE S46 pass=2 fail=0 expect=2 designedFails=0 VERDICT PASS"),
             "FAIL: GATE suite S46 - pass=1 (want 2)" if fail else "PASS: GATE suite S46",
             "GATE regression S51+S46 - pass=%d skip=0 fail=%d of 2 suite(s)" % ((1, 1) if fail else (2, 0)),
             "FAIL: GATE regression S51+S46 - 1 suite(s) failing" if fail else "PASS: GATE regression S51+S46"]
    open(os.path.join(prog, f"{sprint}_studio_lines.txt"), "w", newline="\\n").write(
        "\\n".join(lines + ["CALL 5 [vm=client] eval_client_runtime :: S.fog()",
                           "FAIL: fog missing" if fail else "PASS: fog", "PASS: S46 c2"] + tally) + "\\n")
    open(os.path.join(prog, f"{sprint}_gate_verdicts.json"), "w").write(json.dumps(
        {"headSha": head, "sprint": sprint, "tier": "A", "HEAD": True}))
    with open(os.path.join(prog, f"{sprint}_EVIDENCE_RAW.log"), "a", newline="\\n") as f:
        f.write(f"GATE RUN sprint={sprint} tier=A regression=subset:ui suites=[S51] files=[src/a.txt]\\n"
                f"=== GATE HEAD ===\\n{head[:8]}\\nPASS: GATE HEAD {head[:8]}\\n=== GATE FINISH ===\\n"
                f"VERDICT PASS: GATE HEAD\\n" + "\\n".join(lines + tally) + f"\\nGATE {sprint} {res}\\n")
    failures = [{"row": "S46", "verdict": "FAIL", "fresh": True, "calls": [5, 6], "lines": [12, 14],
                 "detail": "SUITE S46 pass=1 fail=1 VERDICT FAIL - pass=1 (want 2)",
                 "fail_lines": ["FAIL: fog missing"]}] if fail else []
    v = {"schema": 1, "sprint": sprint, "head": head, "mode": "calls", "run_id": rid,
         "started": time.time() - 1, "finished": time.time(), "result": res, "error": None,
         "counts": {"rows": 3, "fresh": 2, "carried": 0, "passed": 1 if fail else 2,
                    "failed": 1 if fail else 0, "unknown": 0, "manual": 1},
         "failures": failures, "manual": [{"row": "S17", "players": 2, "calls": [7],
                                           "why": "needs a 2-player playtest (session runs it with multiplayer_playtest)"}],
         "rows": {"S51": {"verdict": "PASS"}, "S46": {"verdict": res}},
         "evidence": {"verify_pre": "\\n".join(lines[3:5]), "boot": "\\n".join(lines[6:8] + lines[9:11]),
                      "tally": "\\n".join(tally)},
         "summary": [f"GATE {sprint} scripted Studio half {res}"]}
    json.dump(v, open(os.path.join(gd, head[:12] + ".calls.json"), "w"))
    save(phase="done", result=res, stage="done", finish_line=f"GATE {sprint} {res}")
    print(f"PIPELINE {sprint} {res}")
    ''')


def agent(ctx):
    p = ctx["prompt"]
    if "Scripted Studio gate: GATE T1 PASS" in p:
        return {"final": "all green\n" + DONE}
    stamp = f"OK {time.time_ns()}"
    return {"writes": {"src/a.txt": stamp}, "commit": "T1: work",
            "final": "committed; requesting the gate\n" + BLOCK + NOT_DONE}


class Recorder(ScriptedClient):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.log = []

    def prompt(self, session_id, text, *, directory, persona, agent) -> None:
        self.log.append((session_id, text))
        super().prompt(session_id, text, directory=directory, persona=persona, agent=agent)


def sc_profile(tmp_path, repo, pipeline, extra=""):
    return write_profile(tmp_path, "game", repo, extra=textwrap.dedent(f'''\
        [studio_calls]
        enabled = true
        tasks = ["T1"]
        pipeline_module = "{str(pipeline).replace(chr(92), '/')}"
        runner_module = "{str(pipeline).replace(chr(92), '/')}"
        ''') + textwrap.dedent(extra))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AU_LOCK_DIR", str(tmp_path / "locks"))
    (tmp_path / "locks").mkdir()

    def build(plan, extra="", script=agent, approve=True):
        repo, _ = make_repo(tmp_path, "game", [T1], extra_files={".gitignore": ".plan/\n"})
        pipe = tmp_path / "fake_pipeline.py"
        pipe.write_text(FAKE_PIPELINE)
        pf = tmp_path / "calls_plan.json"
        pf.write_text(json.dumps(plan))
        monkeypatch.setenv("FAKE_CALLS_PLAN", str(pf))
        prof = sc_profile(tmp_path, repo, pipe, extra)
        now = [1_000_000.0]
        client = Recorder(script, project_id="game")
        c = coordinator(tmp_path, [prof], {"game": client}, clock=lambda: now[0])
        if approve:
            c.approve("game", "T1", [], "victor")
        return SimpleNamespace(c=c, client=client, now=now, repo=repo, plan=pf, tmp=tmp_path)
    return build


def task(e):
    return e.c.store.task("game", "T1")


def drive(e, pred, limit=400, step=5.0):
    for _ in range(limit):
        e.c.tick()
        e.now[0] += step
        t = task(e)
        if pred(t):
            return t
        time.sleep(0.02)
    raise AssertionError(f"timed out in {task(e)['state']}: {task(e)['next_action']} blocker={task(e)['blocker']}")


def events(e, kind):
    return e.c.store.q("SELECT * FROM events WHERE kind=? ORDER BY id", (kind,))


def sc(e):
    return task(e)["data"].get("studio_calls") or {}


# ---------------------------------------------------------------- request parsing / profile
def test_parse_request_normalizes_and_refuses_bad_blocks():
    r = SC.parse_request("text\n" + BLOCK + NOT_DONE)
    assert r == {"suites": ["S51"], "regression": "subset:ui", "tier": "A", "rerun": "failed",
                 "files": []}
    two = BLOCK.replace("S51", "S1") + "\n" + BLOCK.replace('"failed"', '"all"')
    assert SC.parse_request(two)["rerun"] == "all"                    # the last block wins
    assert SC.parse_request("no block") is None
    # the one-line form gate.ps1's NEXT prints (SCRIPTED ALTERNATIVE)
    one = SC.parse_request('  ```STUDIO_GATE {"suites": "GF_GATE1,S51", "regression": "full", '
                           '"tier": "A", "rerun": "failed"} ```')
    assert one["suites"] == ["GF_GATE1", "S51"] and one["regression"] == "full"
    for bad, why in (('{"suites": "S1", "nope": 1}', "unknown STUDIO_GATE keys"),
                     ("{not json", "not JSON"), ('{"regression": "none"}', "nothing to run"),
                     ('{"suites": "S1", "rerun": "some"}', "bad rerun"),
                     ('{"suites": "S1", "tier": "Z"}', "bad tier"),
                     ('{"suites": "S1; rm -rf"}', "bad suite ids"),
                     ('{"suites": "S1", "files": "../x.lua"}', "unsafe file paths"),
                     ('{"regression": "full; x"}', "bad regression")):
        assert why in SC.parse_request(f"```STUDIO_GATE\n{bad}\n```")["error"], bad


def test_profile_defaults_off_and_validation(tmp_path):
    repo, _ = make_repo(tmp_path, "game", [T1])
    p = load(str(write_profile(tmp_path, "game", repo)))
    assert p.studio_calls["enabled"] is False and p.studio_calls_task("T1", "A") is False
    p = load(str(write_profile(tmp_path, "game", repo, extra="[studio_calls]\nenabled = true\n")))
    assert p.studio_calls_task("T1", "A") and p.studio_calls_task("T1", "b")
    assert not p.studio_calls_task("T1", "C")                         # tiers default A, B
    p = load(str(write_profile(tmp_path, "game", repo,
                               extra='[studio_calls]\nenabled = true\ntasks = ["GF-GATE1"]\n')))
    assert p.studio_calls_task("GF-GATE1", "C") and not p.studio_calls_task("T1", "A")
    for bad in ('tasks = ["T*"]', 'tiers = ["D"]', "run_budget_minutes = 0", "nope = 1",
                'runner_module = "not a module"'):
        with pytest.raises(ProfileError):
            load(str(write_profile(tmp_path, "game", repo, extra=f"[studio_calls]\n{bad}\n")))


# ---------------------------------------------------------------- the pipeline (in process)
def _pipe_args(repo, **kw):
    d = dict(repo=str(repo), sprint="P1", run_id="rid-1", files="src/a.txt", suites="S51",
             regression="subset:ui", tier="A", rerun="failed", runner="x.y", call_timeout=120,
             group_min=10, budget_min=30, host_timeout_min=30)
    d.update(kw)
    return SimpleNamespace(**d)


def test_pipeline_runs_host_calls_finish_in_order(tmp_path, monkeypatch):
    repo = tmp_path / "au"
    (repo / "docs" / "progress").mkdir(parents=True)
    head = "c" * 40
    seen = []

    def run(cmd, cwd, timeout, env=None):
        seen.append(cmd)
        if cmd[:2] == ["git", "-C"]:
            return 0, head
        if "-Finish" in cmd:
            return 0, "GATE P1 PASS"
        if "calls" in cmd:
            assert env["FOUNDRY_GATE_RUN_ID"] == "rid-1"
            gd = repo / ".plan" / "tmp" / "gate" / "P1"
            gd.mkdir(parents=True, exist_ok=True)
            (gd / f"{head[:12]}.calls.json").write_text(json.dumps(
                {"run_id": "rid-1", "head": head, "result": "PASS", "mode": "calls"}))
            return 0, "CALLS P1 PASS"
        (repo / "docs" / "progress" / "P1_gate_verdicts.json").write_text(json.dumps(
            {"headSha": head, "lune": True}))
        (repo / "docs" / "progress" / "P1_gate_expect.json").write_text(json.dumps(
            {"files": {"src/a.txt": "1"}}))
        return 0, "host ok"
    st = SC.run_pipeline(_pipe_args(repo), run=run, log=lambda s: None)
    assert st["result"] == "PASS" and st["finish_line"] == "GATE P1 PASS" and st["phase"] == "done"
    host, calls, fin = seen[1], seen[2], seen[3]
    assert "-File" in host and "tools/gate.ps1" in host and "-Finish" not in host
    assert "-ExecutionPolicy" not in host
    assert host[host.index("-Suites") + 1] == "S51" and host[host.index("-Files") + 1] == "src/a.txt"
    assert calls[calls.index("calls") + 1:calls.index("calls") + 3] == ["--sprint", "P1"]
    assert fin[-3:] == ["-Finish", "-StudioFile", "docs/progress/P1_studio_lines.txt"]
    rec = json.load(open(repo / ".plan" / "tmp" / "gate" / "P1" / SC.PIPELINE_FILE))
    assert rec["result"] == "PASS" and rec["run_id"] == "rid-1"


def test_pipeline_host_failure_and_calls_error(tmp_path):
    repo = tmp_path / "au"
    (repo / "docs" / "progress").mkdir(parents=True)
    head = "d" * 40

    def host_fail(cmd, cwd, timeout, env=None):
        if cmd[:2] == ["git", "-C"]:
            return 0, head
        (repo / "docs" / "progress" / "P1_gate_verdicts.json").write_text(json.dumps(
            {"headSha": head, "lune": False}))
        return 0, "FAIL: GATE lune - see the LUNE line above\nother"
    st = SC.run_pipeline(_pipe_args(repo), run=host_fail, log=lambda s: None)
    assert st["result"] == "FAIL" and st["stage"] == "host"
    assert st["host_fail_lines"] == ["FAIL: GATE lune - see the LUNE line above"]

    def calls_err(cmd, cwd, timeout, env=None):
        if cmd[:2] == ["git", "-C"]:
            return 0, head
        if "calls" in cmd:
            return 2, "CALLS P1 NOT READY: x"
        if "-Finish" in cmd:
            raise AssertionError("-Finish never runs after a tooling ERROR")
        (repo / "docs" / "progress" / "P1_gate_verdicts.json").write_text(json.dumps(
            {"headSha": head, "lune": True}))
        (repo / "docs" / "progress" / "P1_gate_expect.json").write_text(json.dumps({"files": {"a": "1"}}))
        return 0, ""
    st = SC.run_pipeline(_pipe_args(repo), run=calls_err, log=lambda s: None)
    assert st["result"] == "ERROR" and st["stage"] == "calls" and "NOT READY" in st["error"]


# ---------------------------------------------------------------- the coordinator flow
def test_session_request_runs_scripted_gate_then_failures_only_prompt(env):
    e = env([{"result": "FAIL", "sleep": 0.6}, {"result": "PASS"}])
    t = drive(e, lambda t: bool(sc(e).get("run")))
    assert t["state"] == S.IMPLEMENTING
    n_prompts = len(e.client.log)
    # while the run is active: no prompt, no re-request, nothing committed by the coordinator
    for _ in range(5):
        e.c.tick()
        e.now[0] += 5
    if sc(e).get("run"):
        assert len(e.client.log) == n_prompts
    t = drive(e, lambda t: len(e.client.log) > n_prompts)
    fail_prompt = e.client.log[-1][1]
    assert "## Scripted Studio gate: GATE T1 FAIL at" in fail_prompt
    assert "- S46 FAIL (CALL 5-6, docs/progress/T1_studio_lines.txt lines 12-14)" in fail_prompt
    assert "    FAIL: fog missing" in fail_prompt
    assert "S51" not in fail_prompt.split("## Scripted Studio gate")[1].split("MANUAL rows")[0]
    assert "MANUAL rows" in fail_prompt and "S17 (CALL 7)" in fail_prompt
    assert "multiplayer_playtest" in fail_prompt
    assert "end your turn with the same STUDIO_GATE block again" in fail_prompt
    assert len(events(e, "studio_calls_started")) == 1
    done = json.loads(events(e, "studio_calls_done")[0]["detail"])
    assert done["result"] == "FAIL" and done["commit"]
    # the coordinator committed the runner's outputs (closeout-exempt docs/progress/T1_*)
    files = git(e.repo, "show", "--name-only", "--format=", done["commit"]).split()
    assert sorted(files) == ["docs/progress/T1_EVIDENCE_RAW.log", "docs/progress/T1_gate_verdicts.json",
                             "docs/progress/T1_studio_lines.txt"]
    # second request: PASS -> one PASS prompt -> the session reports DONE -> VERIFYING
    t = drive(e, lambda t: any("GATE T1 PASS" in x for _, x in e.client.log))
    assert "The gate is green" in e.client.log[-1][1]
    assert len(events(e, "studio_calls_started")) == 2
    t = drive(e, lambda t: t["state"] != S.IMPLEMENTING)
    assert t["state"] in (S.VERIFYING, S.REVIEWING, S.APPROVED, S.CLOSING, S.COMPLETE)
    # the gate binding: only closeout-exempt files changed since the gate's HEAD
    last = sc(e)["last"]
    assert e.c.studio_calls.gate_fresh(e.c.projects["game"], "T1", e.c.projects["game"].ws.head())
    assert last["result"] == "PASS"


def test_tooling_error_blocks_the_task(env):
    e = env([{"result": "ERROR", "error": "tooling: 2 MCP connect/transport failures in a row"}])
    t = drive(e, lambda t: t["state"] == S.BLOCKED)
    assert "2 MCP connect/transport failures" in (t["blocker"] or "")
    assert json.loads(events(e, "studio_calls_error")[0]["detail"])["result"] == "ERROR"
    assert not sc(e).get("run")


def test_timeout_kills_the_pipeline_tree(env):
    e = env([{"result": "PASS", "sleep": 60}])
    drive(e, lambda t: bool(sc(e).get("run")))
    run = sc(e)["run"]
    e.now[0] += run["limit_s"] + 10
    t = drive(e, lambda t: t["state"] == S.BLOCKED, step=0)
    assert "did not finish within" in (t["blocker"] or "")
    from foundry_coordinator.autoheal import pid_alive
    for _ in range(50):
        if not pid_alive(run["pid"]):
            break
        time.sleep(0.1)
    assert not pid_alive(run["pid"])
    assert json.loads(events(e, "studio_calls_stopped")[0]["detail"])["reason"] == "timeout"


def test_cancel_aborts_a_running_pipeline(env):
    e = env([{"result": "PASS", "sleep": 60}])
    drive(e, lambda t: bool(sc(e).get("run")))
    run = sc(e)["run"]
    e.c.cancel("game", "T1", "operator")
    assert task(e)["state"] == S.CANCELLED
    from foundry_coordinator.autoheal import pid_alive
    for _ in range(50):
        if not pid_alive(run["pid"]):
            break
        time.sleep(0.1)
    assert not pid_alive(run["pid"])


def test_disabled_studio_calls_is_todays_flow(env):
    e = env([{"result": "PASS"}], extra="")
    P = e.c.projects["game"]
    P.profile.studio_calls["enabled"] = False                 # the kill switch
    t = drive(e, lambda t: t["state"] != S.IMPLEMENTING or len(e.client.log) > 1, limit=200)
    assert not events(e, "studio_calls_started") and not sc(e).get("run")


def test_session_lock_left_behind_is_released_another_sprints_waits(env):
    e = env([{"result": "PASS"}])
    lock = e.tmp / "locks" / "studio.lock"
    lock.write_text("someone 2026-10-27T10:00:00Z OTHER-1 playtest\n")
    drive(e, lambda t: "studio.lock" in (t["next_action"] or ""), limit=100)
    assert not sc(e).get("run") and sc(e).get("pending")
    lock.write_text("ses_x 2026-10-27T10:00:00Z T1 gate\n")     # this task's own leftover
    drive(e, lambda t: bool(sc(e).get("run")) or bool(sc(e).get("last")), limit=100)
    assert json.loads(events(e, "studio_calls_lock_released")[0]["detail"])["line"].startswith("ses_x")


# ---------------------------------------------------------------- evidence
def test_records_are_verified_with_runtime_identity_and_provenance(env, tmp_path):
    from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
    from tests.coordinator.test_au_review_evidence import _profile
    e = env([{"result": "PASS"}])
    P = e.c.projects["game"]
    au = AnimalUprisingAdapter(_profile(e.repo), str(tmp_path / "au-state"))
    P.adapter.classify_tool_result = au.classify_tool_result
    P.adapter.runtime_identity = au.runtime_identity
    e.c._lc = lambda P, part="": part == "evidence"
    t = drive(e, lambda t: bool(sc(e).get("last")))
    recs = {r["check_id"]: r for r in e.c.ledger.records("game", "T1")}
    assert {"boot:server", "boot:client", "runtime:verify_live", "suite:S51", "suite:S46",
            "gate:T1"} <= set(recs)
    run_id = sc(e)["last"]["run_id"]
    for cid in ("boot:server", "suite:S51", "gate:T1"):
        r = recs[cid]
        assert r["tier"] == "verified" and r["result"] == "PASS" and r["source"] == "coordinator"
        assert r["provenance"]["executor"] == "studio_gate"
        assert r["provenance"]["run_id"] == run_id and len(r["provenance"]["lines_sha256"]) == 64
        assert r["runtime_id"]
    rt = e.c.ledger.runtime("game", recs["suite:S51"]["runtime_id"])
    assert rt and rt["loaded_fp"]                                  # PRE-PLAY verify fingerprint
    assert recs["gate:T1"]["source_revision"] == sc(e)["last"]["head"]


# ---------------------------------------------------------------- CLI (no session)
def test_cli_run_turns_failing_rows_into_findings(env, capsys):
    from foundry_coordinator.cli import _studio_calls_cmd
    e = env([{"result": "FAIL"}])
    out = []
    a = SimpleNamespace(project="game", task="T1", suites="S51", regression="full", tier="",
                        rerun="failed", files="", dry_run=False, poll=0.05)
    rc = _studio_calls_cmd(e.c, a, out=out.append, sleep=lambda s: time.sleep(0.05))
    assert rc == 1, out
    t = task(e)
    assert any("row S46 FAIL (CALL 5-6" in f for f in t["data"]["findings"])
    assert any("row S17 MANUAL" in f for f in t["data"]["findings"])
    assert not e.c.store.holds("game", "T1")                      # Studio released
    args = json.loads(open(str(e.plan) + ".args.0").read())
    assert args[args.index("--regression") + 1] == "full" and args[args.index("--suites") + 1] == "S51"
    assert any("Scripted Studio gate: GATE T1 FAIL" in o for o in out)


def test_cli_dry_run_uses_the_real_runner_without_studio(env):
    from foundry_coordinator.cli import _studio_calls_cmd
    from tests.coordinator.test_studio_gate import INDEX
    e = env([{"result": "PASS"}])
    (e.repo / "tests").mkdir(exist_ok=True)
    (e.repo / "tests" / "RegressionIndex.lua").write_text(INDEX, encoding="utf-8")
    (e.repo / "default.project.json").write_text('{"name": "x", "tree": {}}', encoding="utf-8")
    git(e.repo, "add", "-A")
    git(e.repo, "commit", "-q", "-m", "index")
    P = e.c.projects["game"]
    P.profile.studio_calls["runner_module"] = "foundry_coordinator.projects.animaluprising.studio_gate"
    out = []
    a = SimpleNamespace(project="game", task="T1", suites="", regression="subset:ui", tier="",
                        rerun="failed", files="", dry_run=True, poll=0.05)
    assert _studio_calls_cmd(e.c, a, out=out.append) == 0, out
    assert "ROWS 5 (index rows=7, pinned=5)" in out[-1] and "DRY-RUN T1 OK" in out[-1]
    assert not (e.repo / ".plan").exists()


def test_a_failed_auto_verify_run_sends_the_rows_to_a_session_never_reruns(env):
    """AS-P5 2026-10-28 replay: auto_verify's FAIL tried VERIFYING -> CHANGES_REQUIRED
    without a verification_id; the refused transition left the task VERIFYING, the run's
    output commit made the gate stale and it ran again (9 FAIL runs in 16 minutes)."""
    e = env([{"result": "FAIL"}])
    P = e.c.projects["game"]
    e.c.store.update_task("game", "T1", state="VERIFYING")
    outcome = {"run_id": "r-auto-1", "head": P.ws.head(), "result": "FAIL", "auto": True,
               "failures": [{"row": "S24", "verdict": "FAIL", "calls": "5-6", "lines": []}]}
    e.c.studio_calls._after_verify(P, task(e), outcome)
    t = task(e)
    assert t["state"] == "CHANGES_REQUIRED" and t["data"]["findings"]
    # and a FAIL whose code did not change since is never re-run
    d = dict(sc(e)); d["last"] = outcome
    assert e.c.studio_calls._failed_at_same_code(P, "T1", outcome, P.ws.head())


def test_a_scripted_run_holds_the_main_checkout(env):
    """UI-SKIN2 2026-10-28 replay: AS-P5's gate run wrote docs/progress/AS-P5_* into the main
    checkout while UI-SKIN2 verified there; UI-SKIN2's clean checks failed on them. A run
    takes the checkout lock with Studio/Rojo, so another main sprint waits for it."""
    e = env([{"result": "PASS"}])
    P = e.c.projects["game"]
    locks = e.c.studio_calls._run_locks(P)
    assert set(e.c._workspace_locks(P)) <= set(locks)
    assert set(e.c._session_locks(P)) <= set(locks)


def test_gate_ps1_cmd_drops_files_deleted_at_head(tmp_path):
    import argparse
    import os
    import subprocess as sp
    from foundry_coordinator import studio_calls as SC
    r = str(tmp_path)
    sp.run(["git", "init", "-q", r], check=True)
    os.makedirs(os.path.join(r, "src"))
    for n in ("a.lua", "b.lua"):
        open(os.path.join(r, "src", n), "w").write("x")
    sp.run(["git", "-C", r, "add", "-A"], check=True)
    sp.run(["git", "-C", r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "1"], check=True)
    os.remove(os.path.join(r, "src", "b.lua"))
    sp.run(["git", "-C", r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "2"], check=True)
    a = argparse.Namespace(repo=r, sprint="X1", files="src/a.lua,src/b.lua", tier="A",
                           regression="none", suites="")
    cmd = SC.gate_ps1_cmd(a)
    assert cmd[cmd.index("-Files") + 1] == "src/a.lua"
