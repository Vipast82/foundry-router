"""UI-T0 fast lane: a [fast_lane] task's verify step is the studio_gate runner
(a background subprocess polled per tick), red gates and vision CHANGES go
back into the SAME primary session, two red gates on a local primary request
the frontier primary automatically, a runner timeout is a red gate, vision
CHANGES are accepted with notes after max_vision_rounds, only the configured
lanes run, and a task outside [fast_lane] behaves exactly as before.

The runner is a tiny fake script (written per test) that reads a JSON plan
and writes status.json + the verdict JSON the real runner writes."""

from __future__ import annotations

import json
import os
import re
import textwrap
import time
from types import SimpleNamespace

import pytest

from foundry_coordinator import lanes as L
from foundry_coordinator import states as S
from foundry_coordinator.adapters.scripted import ScriptedClient
from foundry_coordinator.profile import ProfileError, load
from tests.coordinator.helpers import FakeFoundry, coordinator, make_repo, write_profile

LOCAL, FRONTIER, VISION = "Foundry-Kilo-Local", "Foundry-Kilo-Frontier", \
    "Foundry-Kilo-Visual-Review"
CAP = "docs/evidence/UI-v3/T1/Hud.normal@1920x1080.png"
T1 = {"id": "T1", "title": "HUD", "spec": "Build the v3 HUD.",
      "scope": ["src/a.txt", "docs/evidence/UI-v3/T1/*"]}
T2 = {"id": "T2", "title": "Other", "spec": "Write src/b.txt containing OK.",
      "scope": ["src/b.txt"]}

FAKE_GATE = textwrap.dedent('''\
    """Fake studio_gate: `--repo R sprint --sprint ID --files F`; behaviour per call
    from the JSON plan named by FAKE_GATE_PLAN (a list, one entry per run)."""
    import json, os, subprocess, sys, time
    a = sys.argv[1:]
    repo, sprint = a[a.index("--repo") + 1], a[a.index("--sprint") + 1]
    if "baseline" in a:              # B2: the automatic baseline (never a plan step)
        head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        d = os.path.join(repo, ".plan", "tmp", "gate", sprint)
        os.makedirs(d, exist_ok=True)
        now = time.time()
        v = {"schema": 1, "sprint": sprint, "head": head, "mode": "baseline",
             "run_id": a[a.index("--run-id") + 1], "started": now, "finished": now,
             "result": os.environ.get("FAKE_BASELINE_RESULT", "PASS"),
             "checks": json.loads(os.environ.get("FAKE_BASELINE_CHECKS", "[]")), "captures": []}
        for name in (head[:12] + ".baseline.json", "baseline.json"):
            json.dump(v, open(os.path.join(d, name), "w"))
        sys.exit(0)
    plan_path = os.environ["FAKE_GATE_PLAN"]
    calls = json.load(open(plan_path))
    n_path = plan_path + ".n"
    n = int(open(n_path).read()) if os.path.exists(n_path) else 0
    open(n_path, "w").write(str(n + 1))
    step = calls[min(n, len(calls) - 1)]
    head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True,
                          text=True).stdout.strip()
    d = os.path.join(repo, ".plan", "tmp", "gate", sprint)
    os.makedirs(d, exist_ok=True)
    started = time.time()
    json.dump({"sprint": sprint, "pid": os.getpid(), "started": started, "phase": "stories",
               "progress": "1/3", "updated": started}, open(os.path.join(d, "status.json"), "w"))
    time.sleep(step.get("sleep", 0))
    for rel, text in (step.get("append") or {}).items():       # runner-owned outputs
        os.makedirs(os.path.dirname(os.path.join(repo, *rel.split("/"))), exist_ok=True)
        with open(os.path.join(repo, *rel.split("/")), "a") as f:
            f.write(text)
    caps = []
    for c in step.get("captures", []):
        p = os.path.join(repo, *c.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"\\x89PNG\\r\\n\\x1a\\n fake frame " + c.encode())
        caps.append({"story": "Hud.normal", "size": "1920x1080", "path": c,
                     "geometry_defects": 0, "layout_lines": []})
    run_id = a[a.index("--run-id") + 1] if "--run-id" in a else ""
    assert run_id and run_id == os.environ.get("FOUNDRY_GATE_RUN_ID"), (run_id, a)
    if not step.get("no_verdict"):
        v = {"schema": 1, "sprint": sprint, "head": head, "mode": step.get("mode", "sprint"),
             "run_id": step.get("run_id", run_id), "started": started,
             "finished": time.time(), "result": step["result"],
             "checks": step.get("checks") or [{"id": "host.clean_tree", "result": "PASS",
                                               "required": True, "detail": "clean",
                                               "artifacts": []}],
             "captures": caps, "regression": {}, "summary_line": f"GATE {sprint} {step['result']}",
             "error": step.get("error")}
        for name in (head[:12] + "." + v["mode"] + ".json", "latest.json"):
            json.dump(v, open(os.path.join(d, name + ".tmp"), "w"))
            os.replace(os.path.join(d, name + ".tmp"), os.path.join(d, name))
    json.dump({"sprint": sprint, "pid": os.getpid(), "started": started, "phase": "done",
               "progress": "3/3", "updated": time.time()}, open(os.path.join(d, "status.json"), "w"))
    print(f"GATE {sprint} {step['result']}")
    sys.exit(step.get("exit", 0 if step["result"] == "PASS" else 1))
    ''')

RED = {"result": "FAIL", "checks": [
    {"id": "story.Hud.normal@390x844", "result": "FAIL", "required": True,
     "detail": "FAIL: UI-LAYOUT VIEWPORT-CLIP Hud/TopBar - 12px off-screen", "artifacts": []}]}
GREEN = {"result": "PASS", "captures": [CAP]}


class Recorder(ScriptedClient):
    """Scripted Kilo that records each prompt's persona and serves the turn
    with the persona it was prompted with (a working per-prompt model switch)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.log = []                            # (session, persona, text)

    def start_session(self, **kw):
        sid = super().start_session(**kw)
        self.sessions[sid]["served"] = kw.get("persona")
        return sid

    def prompt(self, session_id, text, *, directory, persona, agent) -> None:
        self.log.append((session_id, persona, text))
        self.sessions[session_id]["served"] = persona
        super().prompt(session_id, text, directory=directory, persona=persona, agent=agent)

    def transcript(self, session_id, *, directory) -> list:
        s = self.sessions.get(session_id) or {}
        return [{"info": {"role": "assistant", "modelID": s.get("served") or ""}, "parts": []}]


def agent(ctx):
    """The primary: initial work, gate-capture commits, red-gate / vision fixes."""
    p = ctx["prompt"]
    n = int(ctx.get("_n", 0))
    done = '```coordinator-report\n{"status": "DONE", "summary": "ok"}\n```'
    if "commit exactly the gate's capture files" in p or "commit the runner's capture files" in p:
        return {"commit": f"{ctx['task_id']}: gate captures", "final": done}
    if ctx["task_id"] == "T2":
        return {"writes": {"src/b.txt": "OK"}, "commit": "T2: work", "final": done}
    stamp = f"OK {time.time_ns()}"
    return {"writes": {"src/a.txt": stamp}, "commit": f"T1: work {n}", "final": done}


class VisionFoundry(FakeFoundry):
    """Fake Foundry with a strict, vision-capable visual persona; vision
    verdicts are taken from `self.vision` in order (approve by default)."""

    def __init__(self):
        super().__init__()
        self.vision = []

    def persona_info(self, persona, client=""):
        return {"exists": True, "enabled": True, "strict": True,
                "routes": [{"model": "vision-model", "vision": True}]}

    def chat(self, persona, messages, *, session_id="", max_tokens=4000, schema=None):
        if not session_id.startswith("fast-lane-vision"):
            return super().chat(persona, messages, session_id=session_id,
                                max_tokens=max_tokens, schema=schema)
        verdict = self.vision.pop(0) if self.vision else "approve"
        self.chats.append({"persona": persona, "session_id": session_id, "packet": "vision"})
        frames = []
        for part in messages[-1]["content"]:
            m = re.match(r"Capture (\S+) .*SHA256 ([0-9a-f]{64})", part.get("text") or "")
            if m:
                frames.append({"path": m.group(1), "sha256": m.group(2), "image_seen": True,
                               "subject_present": True, "validity": "ok",
                               "observation": "HUD rendered",
                               "findings": [] if verdict == "approve" else
                               ["1. Hud.normal@390x844: the tab row overlaps the close button"]})
        return json.dumps({"verdict": verdict, "frames": frames, "limitations": ""}), \
            {"served_model": "vision-model"}


VISUAL = "\n[approval]\nvisual_model = true\n"


def vis_override(implementer=None):
    return (f'\n[role_overrides.t1]\ntasks = ["T1"]\nvisual_reviewer = "{VISION}"\n'
            + (f'implementer = "{implementer}"\n' if implementer else ""))


def fl_profile(tmp_path, repo, runner, extra="", checks=None):
    return write_profile(tmp_path, "game", repo, checks=checks, extra=textwrap.dedent(f'''\
        [fast_lane]
        enabled = true
        tasks = ["T1"]
        gate_module = "{str(runner).replace(chr(92), '/')}"
        gate_timeout_minutes = 45
        max_red_gates_before_handoff = 2
        max_vision_rounds = 2
        # these tests script vision verdicts in call order: one batch call at a time
        # (B3's concurrent batches are covered in test_revamp_gate.py)
        vision_parallel = 1
        lanes = ["prework", "test_design", "failure_triage", "incremental_review"]

        [frontier_primary]
        enabled = true
        persona = "{FRONTIER}"
        max_turns = 3
        cooldown_minutes = 60
        ''') + textwrap.dedent(extra))


@pytest.fixture
def env(tmp_path, monkeypatch):
    def build(plan, extra=None, vision=None, tasks=(T1,), checks=None, files=None):
        extra = VISUAL + vis_override() if extra is None else extra
        repo, _ = make_repo(tmp_path, "game", list(tasks),
                            extra_files={".gitignore": ".plan/\n", **(files or {})})
        runner = tmp_path / "fake_gate.py"
        runner.write_text(FAKE_GATE)
        plan_file = tmp_path / "gate_plan.json"
        plan_file.write_text(json.dumps(plan))
        monkeypatch.setenv("FAKE_GATE_PLAN", str(plan_file))
        prof = fl_profile(tmp_path, repo, runner, extra, checks)
        now = [1_000_000.0]
        client = Recorder(agent, project_id="game")
        ff = VisionFoundry()
        ff.vision = list(vision or [])
        c = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": ff},
                        clock=lambda: now[0])
        return SimpleNamespace(c=c, client=client, ff=ff, now=now, repo=repo, prof=prof,
                               plan=plan_file)
    return build


def task(e, tid="T1"):
    return e.c.store.task("game", tid)


def drive(e, pred, tid="T1", limit=400, step=5.0):
    """Tick (the runner is a real background process) until pred(task)."""
    for _ in range(limit):
        e.c.tick()
        e.now[0] += step
        t = task(e, tid)
        if pred(t):
            return t
        time.sleep(0.02)
    raise AssertionError(f"timed out in {task(e, tid)['state']}: {task(e, tid)['next_action']}")


def fl(e, tid="T1"):
    return task(e, tid)["data"].get("fast_lane") or {}


def events(e, kind):
    return [r for r in e.c.store.q("SELECT * FROM events WHERE kind=? ORDER BY id", (kind,))]


def red_gates_seen(n):
    return lambda t: int((t["data"].get("fast_lane") or {}).get("red_gates") or 0) >= n \
        and t["state"] == S.IMPLEMENTING


# --------------------------------------------------------------------------- profile


def test_profile_defaults_and_validation(tmp_path):
    repo, _ = make_repo(tmp_path, "game", [T1])
    p = load(str(write_profile(tmp_path, "game", repo)))
    assert p.fast_lane["enabled"] is False and not p.fast_lane_task("T1")
    # max_vision_rounds default 1 (revamp decision 2026-10-21: one fix round, then accept
    # with notes); vision_parallel 4, auto_baseline and early_review on (B2-B4)
    assert p.fast_lane["max_red_gates_before_handoff"] == 2 and p.fast_lane["max_vision_rounds"] == 1
    assert p.fast_lane["vision_parallel"] == 4 and p.fast_lane["auto_baseline"] is True
    for bad in ('[fast_lane]\nfoo = 1\n', '[fast_lane]\ntasks = ["UI-*"]\n',
                '[fast_lane]\nlanes = ["nope"]\n', '[fast_lane]\ngate_timeout_minutes = 0\n',
                '[fast_lane]\ngate_module = "not a module"\n'):
        with pytest.raises(ProfileError):
            load(str(write_profile(tmp_path, "game", repo, extra=bad)))


def test_lane_trimming_only_for_fast_lane_tasks(tmp_path):
    repo, _ = make_repo(tmp_path, "game", [T1, T2])
    lanes = "\n".join(f"[lanes.{x}]\nenabled = true" for x in L.LANES)
    p = load(str(fl_profile(tmp_path, repo, tmp_path / "x.py", "[automation]\nhelpers = \"proactive\"\n[lanes]\nenabled = true\n"
                            + lanes + "\n")))
    P = SimpleNamespace(profile=p)
    fast = L.settings(P, "T1")
    on = {x for x in L.LANES if L.lane_on(fast, x)}
    assert on == {"prework", "test_design", "failure_triage", "incremental_review"}
    assert all(L.lane_on(L.settings(P, "T2"), x) for x in L.LANES)      # untouched
    assert all(L.lane_on(L.settings(P), x) for x in L.LANES)            # no task: untouched


# --------------------------------------------------------------------------- flows


def test_pass_vision_review_close(env):
    e = env([GREEN])
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] == S.COMPLETE)
    assert t["sessions"] == 1 and t["attempt"] == 1                   # one session, no repair
    started = events(e, "fast_lane_gate_started")
    assert len(started) == 1
    [g] = events(e, "fast_lane_gate")
    assert json.loads(g["detail"])["result"] == "PASS"
    # numbers for the router's sprint scorecard: how long the gate ran, how many checks
    gd = json.loads(g["detail"])
    assert gd["duration_s"] is not None and gd["duration_s"] >= 0
    assert gd["checks_total"] >= 1 and gd["checks_failed"] == 0
    [va] = events(e, "fast_lane_vision_approved")
    assert json.loads(va["detail"])["frames"] >= 1
    # B1 (2026-10-21): the coordinator commits the gate's captures itself (UI-D1 spent
    # attempts 3-7 on "commit its captures" turns); the session got only its first prompt
    assert not any("capture files" in x[2] for x in e.client.log), [x[2][-600:] for x in e.client.log[1:]]
    assert len(e.client.log) == 1
    [cc] = events(e, "coordinator_commit")
    assert CAP in json.loads(cc["detail"])["paths"]
    assert json.loads(cc["detail"])["sha"] in t["data"]["coord_commits"]
    [a] = e.c.store.approvals("game", "T1", "visual")
    assert a["approver"] == f"model:{VISION}/vision-model" and CAP in a["artifacts"]
    rows = e.c.store.q("SELECT check_name, result, required FROM verifications WHERE set_id=?",
                       (t["verification_id"],))
    got = {r["check_name"]: r["result"] for r in rows}
    assert got["fast_lane.gate"] == "PASS" and got["fast_lane.vision"] == "PASS"
    assert "project-check" not in got                 # the runner is the verify step
    assert e.c.store.reviews("game", "T1")[-1]["verdict"] == "APPROVED"
    assert "au-fast-lane" not in e.client.log[0][2]   # generic adapter: its own prompt


def test_red_gate_continues_the_same_session(env):
    e = env([RED, GREEN])
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, red_gates_seen(1))
    sid = t["session_id"]
    assert t["sessions"] == 1 and t["attempt"] == 1 and not t["data"].get("recovery_case")
    last = e.client.log[-1]
    assert last[0] == sid and last[1] == LOCAL
    assert "Studio gate FAIL" in last[2] and "VIEWPORT-CLIP" in last[2]
    hist = [(h["from_state"], h["to_state"]) for h in e.c.store.history("game", "T1")]
    assert (S.VERIFYING, S.IMPLEMENTING) in hist and S.CHANGES_REQUIRED not in {b for _, b in hist}
    t = drive(e, lambda t: t["state"] == S.COMPLETE)
    assert t["sessions"] == 1 and fl(e)["red_gates"] == 1
    assert len(events(e, "fast_lane_gate_started")) == 2


def test_two_reds_on_local_primary_request_the_frontier(env):
    e = env([RED, RED, GREEN])
    e.c.approve("game", "T1", [], "victor")
    drive(e, red_gates_seen(2))
    [ph] = e.c.frontier.phases("game", "T1")
    assert ph["state"] == "active" and ph["requested_by"] == "engine:fast_lane"
    sid, persona, text = e.client.log[-1]
    assert persona == FRONTIER and "handed this sprint to the frontier primary" in text
    assert sid == task(e)["session_id"] and task(e)["sessions"] == 1
    b = [x for x in e.ff.binds if x["session_id"] == sid][-1]
    assert b["persona"] == FRONTIER and b["purpose"] == "frontier_primary"
    t = drive(e, lambda t: t["state"] == S.COMPLETE)
    assert e.c.frontier.phases("game", "T1")[0]["state"] == "returned"
    assert FRONTIER in e.c.frontier.authors("game", "T1")              # not the sole reviewer


def test_no_handoff_for_a_frontier_primary_task(env):
    e = env([RED, RED, GREEN], extra=VISUAL + vis_override(FRONTIER))
    e.c.approve("game", "T1", [], "victor")
    drive(e, red_gates_seen(2))
    assert e.c.frontier.phases("game", "T1") == []
    assert not events(e, "frontier_requested")
    drive(e, lambda t: t["state"] == S.COMPLETE)


def test_red_gate_limit_blocks(env):
    e = env([RED])
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] == S.BLOCKED)
    assert fl(e)["red_gates"] == 4 and "red studio gates" in t["blocker"]
    assert t["sessions"] == 1


def test_runner_timeout_is_a_red_gate(env):
    e = env([{"result": "PASS", "sleep": 60}, GREEN])
    e.c.approve("game", "T1", [], "victor")
    drive(e, lambda t: bool((t["data"].get("fast_lane") or {}).get("run")), step=1)
    proc = next(iter(e.c.fast_lane._procs.values()))
    assert proc.poll() is None
    e.now[0] += 46 * 60
    t = drive(e, red_gates_seen(1), step=1)
    assert proc.poll() is not None                                       # killed
    assert "did not finish within 45 minutes" in e.client.log[-1][2]
    assert t["session_id"] == e.client.log[-1][0]
    drive(e, lambda t: t["state"] == S.COMPLETE)


def test_runner_exit_without_verdict_blocks(env):
    e = env([{"result": "FAIL", "no_verdict": True, "exit": 2}])
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] == S.BLOCKED)
    assert "without a verdict" in t["blocker"] and "not ready" in t["blocker"]


def test_vision_changes_rounds_then_accept_with_notes(env):
    e = env([GREEN], vision=["changes_required"] * 3)
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] == S.COMPLETE)
    assert t["sessions"] == 1
    vision_prompts = [x for x in e.client.log if "Vision review: CHANGES" in x[2]]
    assert len(vision_prompts) == 2 and "round 2/2" in vision_prompts[-1][2]
    assert "tab row overlaps the close button" in vision_prompts[0][2]
    assert fl(e)["vision_rounds"] == 2 and fl(e)["vision_notes"]
    [acc] = events(e, "fast_lane_vision_accepted_with_notes")
    assert json.loads(acc["detail"])["rounds"] == 2
    apr = e.c.store.approvals("game", "T1", "visual")
    assert apr and apr[-1]["note"].startswith("accepted with notes")
    assert len(events(e, "model_visual_review")) == 3


def test_non_fast_lane_task_unchanged(env):
    e = env([GREEN], tasks=(T1, T2))
    e.c.approve("game", "T2", [], "victor")
    t = drive(e, lambda t: t["state"] == S.COMPLETE, tid="T2")
    assert not events(e, "fast_lane_gate_started") and not t["data"].get("fast_lane")
    rows = e.c.store.q("SELECT check_name FROM verifications WHERE set_id=?",
                       (t["verification_id"],))
    names = {r["check_name"] for r in rows}
    assert "project-check" in names and not any(n.startswith("fast_lane") for n in names)
    assert e.c.frontier.phases("game", "T2") == []
    with pytest.raises(ValueError):                    # still manual-only and IMPLEMENTING-only
        e.c.frontier.request("game", "T2", "victor", "x")


def test_fast_lane_records_evidence_with_lifecycle(env):
    # (a fast-lane profile's verify step is the runner: no [verify] checks whose
    # obligations the runner would never satisfy)
    e = env([GREEN], extra=VISUAL + vis_override() + "[lifecycle]\nenabled = true\n", checks="")
    e.c.approve("game", "T1", [], "victor")
    drive(e, lambda t: t["state"] in (S.COMPLETE, S.BLOCKED))
    assert task(e)["state"] != S.BLOCKED, task(e)["blocker"]
    recs = e.c.store.q("SELECT * FROM ev_records WHERE check_id='fast_lane.gate'")
    assert recs and recs[0]["result"] == "PASS" and recs[0]["tier"] in ("verified", "reviewed")
    gate_head = json.loads(events(e, "fast_lane_gate_started")[0]["detail"])["head"]
    assert recs[0]["source_revision"] == gate_head


def test_au_adapter_fast_lane_prompt_and_obligations(tmp_path):
    from foundry_coordinator.adapters.base import TaskSpec
    from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
    raw = {"fast_lane": {"enabled": True, "tasks": ["UI-03a"]},
           "training": {"local_personas": [LOCAL]}, "frontier_primary": {"persona": FRONTIER}}
    prof = SimpleNamespace(
        id="animaluprising", workspace=str(tmp_path), raw=raw, roles={"implementer": LOCAL},
        fast_lane={"enabled": True, "tasks": ["UI-03a"], "gate_module":
                   "foundry_coordinator.projects.animaluprising.studio_gate"},
        fast_lane_task=lambda t: t == "UI-03a", persona=lambda role, tid="": LOCAL,
        exempt=lambda tid: [], deps_accept_pending=[])
    a = AnimalUprisingAdapter.__new__(AnimalUprisingAdapter)
    a.p, a.cfg, a.state_dir = prof, {}, str(tmp_path)
    spec = TaskSpec(project_id="animaluprising", task_id="UI-03a", title="Hero build",
                    spec="Goal: hero build screen.", scope=["src/client/UI/v3/**"], tags=[],
                    deps=[], data={"tier": "B", "plan": "docs/progress/UI-03a_PLAN.md"})
    p = a.implement_prompt(spec, {"run_id": "r1", "attempt": 1})
    for want in ("au-fast-lane", "docs/rules/FAST_LANE_RULES.md", "docs/progress/UI-03a_PLAN.md",
                 "docs/progress/UI-03a_gate.json", "studio_gate --repo", "stories --sprint UI-03a",
                 "never run `sprint`", "LOCAL"):
        assert want in p, want
    # B2 (2026-10-21): the session runs stories + Lune only; the coordinator owns the one
    # full gate (UI-02a: session and coordinator both ran it at the same head)
    assert "sprint --sprint UI-03a" not in p and "status --sprint" not in p
    assert "AGENT_RULES.md (full)" not in p and "tools/gate.ps1" not in p
    assert a.required_checks(spec, "BUILD")[0]["check_id"] == "fast_lane.gate"
    a._plan_requirements = lambda s, ws=None: {"suites": ["S45"], "gate": True, "captures": [],
                                               "acceptance": "", "plan": "", "lune": False}
    obs = a.obligations(spec)
    build = [o for o in obs if o["phase"] == "BUILD"]
    assert [o["obligation_id"] for o in build] == ["build.gate"]
    assert build[0]["required_evidence"] == [{"kind": "check", "ref": "fast_lane.gate"}]
    assert a.check_dependencies(spec, "fast_lane.gate") == ["src/**", "tests/**",
                                                             "default.project.json"]


# --------------------------------------------------------------------------- verdict acceptance (H4)


@pytest.mark.parametrize("step", [{"result": "PASS", "mode": "stories"},
                                  {"result": "PASS", "run_id": "someone-else"}])
def test_only_this_runs_sprint_verdict_counts(env, step):
    """A stories/regression verdict, or a sprint verdict without this run's nonce, at
    the same head never stands in for the coordinator's own sprint run."""
    e = env([dict(step, exit=0)])
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] in (S.BLOCKED, S.COMPLETE))
    assert t["state"] == S.BLOCKED and "without a verdict" in t["blocker"]
    assert not events(e, "fast_lane_gate")
    [started] = events(e, "fast_lane_gate_started")
    assert json.loads(started["detail"])["run_id"].startswith("T1-")


def test_kill_stops_the_whole_tree_and_the_playtest(tmp_path, monkeypatch):
    from foundry_coordinator import fast_lane as FL
    ws = tmp_path / "ws"
    d = ws / ".plan" / "tmp" / "gate" / "T1"
    d.mkdir(parents=True)
    (d / "status.json").write_text(json.dumps({"pid": 4242, "phase": "regression", "playing": True}))
    seen, events_ = [], []
    monkeypatch.setattr(FL.subprocess, "run", lambda argv, **kw: seen.append(("run", argv)))
    monkeypatch.setattr(FL.subprocess, "Popen", lambda argv, **kw: seen.append(("popen", argv)))
    monkeypatch.setattr(FL.os, "killpg", lambda *a: seen.append(("killpg", a)), raising=False)
    monkeypatch.setattr(FL.os, "getpgid", lambda pid: pid, raising=False)
    lane = FL.FastLane.__new__(FL.FastLane)
    lane._procs = {}
    lane.store = SimpleNamespace(event=lambda *a, **kw: events_.append((a, kw)))
    P = SimpleNamespace(id="game", profile=SimpleNamespace(
        workspace=str(ws), fast_lane={"gate_module": "foundry_coordinator.projects.animaluprising.studio_gate"}))
    lane._kill(P, "T1", {"head": "a" * 40, "pid": 4242}, "timeout")      # after a restart: pid only
    kinds = [k for k, _ in seen]
    if os.name == "nt":
        assert ("run", ["taskkill", "/T", "/F", "/PID", "4242"]) in seen
    else:
        assert "killpg" in kinds
    popen = next(a for k, a in seen if k == "popen")
    assert popen[-1] == "stop-playtest" and "--repo" in popen
    stopped = next(kw for a, kw in events_ if a[2] == "fast_lane_gate_stopped")
    assert stopped["detail"]["playtest_stop"] is True
    # a status.json that is not this run's pid: nothing is killed or stopped
    seen.clear()
    lane._kill(P, "T1", {"head": "a" * 40, "pid": 9999}, "moved on")
    assert seen == []


def test_vision_reviews_in_batches_and_retries_only_inconclusive(env):
    """UI-D1 (2026-10-20): ~560 frames in one vision call came back uncertain. Frames
    go in batches of [fast_lane] vision_batch_frames, one call per tick; every batch
    must approve; a retry re-asks only the inconclusive batch."""
    caps = [CAP.replace(".png", f"-{i}.png") for i in range(5)]
    e = env([{"result": "PASS", "captures": caps}],
            vision=["approve", "uncertain", "approve", "approve"])
    e.c.projects["game"].profile.fast_lane["vision_batch_frames"] = 2
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] == S.COMPLETE)
    vision_calls = [x for x in e.ff.chats if x.get("packet") == "vision"]
    # 3 batches + the uncertain batch re-asked in small chunks (v0.130.2: 1 frame each here);
    # the approved batches' frames are remembered as clean, so only that batch is re-asked
    assert len(vision_calls) == 5
    # B3: a round's batches are frozen when it starts (memory no longer re-cuts them), so the
    # retry names the inconclusive batch itself: batch 2 of 3, in two 1-frame chunks
    assert [x["session_id"].rsplit("-", 1)[-1] for x in vision_calls[-2:]] == [
        "b2of3.c1of2", "b2of3.c2of2"]
    [a] = e.c.store.approvals("game", "T1", "visual")
    assert set(a["artifacts"]) == set(caps)


def test_vision_batch_changes_merge_findings(env):
    caps = [CAP.replace(".png", f"-{i}.png") for i in range(3)]
    e = env([{"result": "PASS", "captures": caps}],
            vision=["approve", "changes_required"] + ["approve"] * 6)
    e.c.projects["game"].profile.fast_lane["vision_batch_frames"] = 2
    e.c.approve("game", "T1", [], "victor")
    drive(e, lambda t: any("[batch 2/2]" in x[2] for x in e.client.log))


def test_runner_outputs_dirty_at_run_start_never_trip_the_clean_checks(env):
    """UI-D1 (2026-10-20): the runner's evidence log was already uncommitted when the run
    started and the runner appends to it every gate run, so coordinator.unrelated_preserved
    failed on every cycle until the task ran out of attempts."""
    log = "docs/progress/T1_EVIDENCE_RAW.log"
    e = env([dict(GREEN, append={log: "GATE T1 PASS\n"})], files={log: "old\n", CAP: "old"})
    (e.repo / log).write_text("old\nGATE T1 FAIL\n")             # dirty before the task starts
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] in (S.COMPLETE, S.BLOCKED, S.CHANGES_REQUIRED))
    assert t["state"] == S.COMPLETE, (t["blocker"], t["next_action"])
    assert t["sessions"] == 1 and t["attempt"] == 1
    assert "GATE T1 PASS" in (e.repo / log).read_text()           # the runner's line is kept


def test_capture_commit_covers_other_renders_in_the_capture_folder(env):
    """UI-D1 (2026-10-20): the session's own stories run re-rendered 463 frames besides the
    gate's 40 captures and coordinator.clean looped. B1 (2026-10-21): the coordinator commits
    the whole runner capture folder itself; no model turn is spent on it."""
    other = "docs/evidence/UI-v3/T1/Other.normal@390x844.png"
    e = env([dict(GREEN, append={other: "re-rendered"}), GREEN])
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] in (S.COMPLETE, S.BLOCKED, S.CHANGES_REQUIRED))
    assert t["state"] == S.COMPLETE, (t["blocker"], t["next_action"])
    assert not [x[2] for x in e.client.log if "gate captures" in x[2]]
    [cc] = events(e, "coordinator_commit")
    d = json.loads(cc["detail"])
    assert other in d["paths"] and CAP in d["paths"] and d["message"].startswith("T1:")
    assert len(events(e, "fast_lane_gate_started")) == 1          # the commit carried the gate


# ------------------------------------------------------- background work (v0.130)

def test_running_gate_publishes_live_progress_for_the_control_center(env):
    """UI-D1, 2026-10-20: the gate runner isn't a Kilo session, so the control
    center looked idle while it ran. The fast lane keeps a structured record the
    bridge passes on (phase, n/total, start time, limit, log); the elapsed note
    reads m:ss, so it visibly moves within a minute."""
    from foundry_coordinator.telemetry import background_work
    e = env([{"result": "PASS", "sleep": 3, "captures": [CAP]}])
    e.c.approve("game", "T1", [], "victor")
    # (revamp B13: cached per-tick checks make ticks fast enough to see the record before
    # the runner wrote its first phase; wait for the phase this test asserts)
    t = drive(e, lambda t: (fl(e).get("live") or {}).get("kind") == "gate" and
              (fl(e).get("live") or {}).get("phase") == "stories", step=1)
    live = fl(e)["live"]
    assert live["phase"] == "stories" and live["progress"] == "1/3" and live["limit_s"] == 45 * 60
    assert live["log"].endswith("fast_lane_gate.log") and live["started_ts"] > 1_700_000_000
    assert live["head"] == t["head_sha"] or len(live["head"]) == 40
    assert re.search(r"\d+:\d\d of 45 min$", t["next_action"]), t["next_action"]
    bg = background_work(t)
    assert bg["kind"] == "gate" and bg["progress"] == "1/3" and bg["log"] == live["log"]
    # a record left from an earlier pass never shows once the task left VERIFYING
    assert background_work({**t, "state": "IMPLEMENTING"}) is None
    assert background_work({"state": "VERIFYING", "data": {"fast_lane": {"live": {"kind": "x"}}}}) is None


def test_vision_batches_publish_done_of_total_and_findings_so_far(env):
    caps = [CAP.replace(".png", f"-{i}.png") for i in range(5)]
    e = env([{"result": "PASS", "captures": caps}],
            vision=["changes_required", "approve", "approve"] + ["approve"] * 6)
    e.c.projects["game"].profile.fast_lane["vision_batch_frames"] = 2
    e.c.approve("game", "T1", [], "victor")
    drive(e, lambda t: (fl(e).get("live") or {}).get("kind") == "vision"
          and fl(e)["live"].get("done", 0) >= 1, step=1)
    live = fl(e)["live"]
    assert live["batches"] == 3 and live["batch"] == live["done"] + 1 and live["total_frames"] == 5
    assert live["changes"] == 1 and live["findings"], live
    from foundry_coordinator.telemetry import background_work
    bg = background_work(task(e))
    assert bg["kind"] == "vision" and bg["changes"] == 1 and bg["findings"] == live["findings"][:8]


def test_consumed_verdict_survives_the_session_rewriting_the_workspace_file(tmp_path):
    """UI-D1 (2026-10-20 11:00): the gate passed and was consumed; the session then ran the
    runner itself at the same head, which rewrote <head>.sprint.json with another run id.
    The carried run looked unfinished and was timed out as a red gate. The coordinator's
    own copy (next to the run log) is the fallback."""
    from foundry_coordinator import fast_lane as FL
    head = "a" * 40
    ws = tmp_path / "ws"
    vdir = ws / ".plan" / "tmp" / "gate" / "T1"
    vdir.mkdir(parents=True)
    logdir = tmp_path / "ev"
    logdir.mkdir()
    run = {"head": head, "run_id": "T1-aaaa-1", "log": str(logdir / "fast_lane_gate.log"),
           "wall": 100.0}
    ours = {"schema": 1, "sprint": "T1", "head": head, "mode": "sprint", "run_id": "T1-aaaa-1",
            "result": "PASS", "finished": 200.0, "captures": []}
    other = dict(ours, run_id="-", finished=300.0)
    (vdir / f"{head[:12]}.sprint.json").write_text(json.dumps(other))
    fl = FL.FastLane.__new__(FL.FastLane)
    P = SimpleNamespace(profile=SimpleNamespace(workspace=str(ws)))
    assert fl._verdict(P, "T1", run) is None                    # not ours, no copy yet
    (logdir / "verdict-T1-aaaa-1.json").write_text(json.dumps(ours))
    assert fl._verdict(P, "T1", run)["run_id"] == "T1-aaaa-1"   # the copy counts
    assert FL._verdict_copy(dict(run, run_id="../x")) == ""     # never a path from a bad id


def test_inconclusive_batch_gets_one_structured_retry_then_notes(env):
    """UI-D1 (2026-10-20 11:44): a batch inconclusive twice blocked the task. Revamp B3
    (decision 2026-10-21): one structured retry in chunks, then the frames still
    inconclusive are accepted with notes; vision never stops a sprint."""
    caps = [CAP.replace(".png", f"-{i}.png") for i in range(4)]
    e = env([{"result": "PASS", "captures": caps}],
            vision=["uncertain", "uncertain", "approve", "approve", "approve", "approve",
                    "approve"])
    e.c.projects["game"].profile.fast_lane["vision_batch_frames"] = 4
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] in (S.COMPLETE, S.BLOCKED))
    assert t["state"] == S.COMPLETE, t["blocker"]
    calls = [x["session_id"] for x in e.ff.chats if x.get("packet") == "vision"]
    # try 0: the whole batch (uncertain); try 1: 1-frame chunks, the first one uncertain;
    # no third try: accepted with notes
    assert ".c" not in calls[0] and any(".c1of4" in c for c in calls[1:])
    assert len(calls) == 1 + 4
    [a] = e.c.store.approvals("game", "T1", "visual")
    assert set(a["artifacts"]) == set(caps) and "inconclusive" in a["note"]
    [acc] = events(e, "fast_lane_vision_accepted_with_notes")
    assert json.loads(acc["detail"])["inconclusive"] == 1


def test_vision_after_an_approval_reviews_only_changed_frames(env):
    """Token cost (UI-D1, 2026-10-20): every small commit re-sent all frames to vision.
    After an approval, unchanged frames carry; only changed ones are reviewed."""
    from foundry_coordinator.adapters.base import PASS as P_
    caps = [CAP.replace(".png", f"-{i}.png") for i in range(4)]
    e = env([{"result": "PASS", "captures": caps}])
    e.c.approve("game", "T1", [], "victor")
    drive(e, lambda t: t["state"] == S.COMPLETE)
    n0 = len([x for x in e.ff.chats if x.get("packet") == "vision"])
    fl = e.c.fast_lane
    P = e.c.projects["game"]
    t = task(e)
    frames, arts, _ = fl._frames(P, [{"path": c} for c in caps])
    r = fl._vision(P, t, None, t["head_sha"], {"head": t["head_sha"]},
                   {"captures": [{"path": c} for c in caps]}, [])
    assert r.result == P_ and "unchanged" in r.detail
    assert len([x for x in e.ff.chats if x.get("packet") == "vision"]) == n0   # no new call


def test_vision_never_resends_a_frame_already_judged_clean(env):
    """Token cost (2026-10-21): a CHANGES round re-sent every frame, even ones the reviewer
    already passed. A frame judged clean at the same bytes is never reviewed again."""
    caps = [CAP.replace(".png", f"-{i}.png") for i in range(3)]
    e = env([{"result": "PASS", "captures": caps}],
            vision=["approve", "approve", "changes_required"])
    e.c.projects["game"].profile.fast_lane["vision_batch_frames"] = 1
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] == S.COMPLETE)
    calls = [x for x in e.ff.chats if x.get("packet") == "vision"]
    assert len(calls) == 4, [x["session_id"] for x in calls]    # 3 + only the CHANGES frame
    assert len(fl(e)["vision_clean"]) == 3          # round 2 approved the last one


def test_red_gate_never_carries_to_a_newer_commit():
    """UI-02a (2026-10-21): only evidence files changed after a red gate, so the coordinator
    re-consumed the red verdict instead of running the gate on the new commit (where the
    session's own run passed)."""
    from foundry_coordinator import fast_lane as FL
    fl = FL.FastLane.__new__(FL.FastLane)
    ws = SimpleNamespace(is_ancestor=lambda a, b: True,
                         changed_files=lambda a, b: ["docs/evidence/UI-v3/T1/x.png"])
    P = SimpleNamespace(ws=ws, adapter=SimpleNamespace(exempt_paths=lambda spec: ["docs/evidence/**"]))
    green = {"result": "PASS", "checks": [], "captures": []}
    red = {"result": "FAIL", "checks": [{"id": "x", "result": "FAIL", "required": True}],
           "captures": []}
    assert fl._carries(P, None, "a" * 40, "b" * 40, green) is True
    assert fl._carries(P, None, "a" * 40, "b" * 40, red) is False
    assert fl._carries(P, None, "a" * 40, "a" * 40, red) is True       # same commit: same run
