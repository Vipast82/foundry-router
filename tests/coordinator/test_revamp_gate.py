"""Automation revamp, workstream B (2026-10-21): gate, vision, review and closeout.

B1  hygiene: the coordinator commits the runner's outputs itself; clean / scope /
    unrelated_preserved fail only on files the agent authored.
B2  one gate per change: prompts never ask for `sprint`; the runner carries every
    passed check whose inputs did not change (same tree, ancestor heads, baseline);
    an automatic baseline at the sprint's base commit.
B3  vision: bounded concurrent batches, per-batch clean memory, one structured retry
    then accept with notes (never a stop), memory in the after_close vision path.
B4  review: starts while vision runs, one merged fix round into the same session,
    incremental evidence re-review, coordinator-ticked checklist, capped packets.
B7  closeout: templated by the coordinator; a model session only when it fails.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from foundry_coordinator import bookkeeping as BK
from foundry_coordinator import states as S
from foundry_coordinator.adapters.git import Workspace
from tests.coordinator.helpers import git
from tests.coordinator.test_repair_workspace import router, ws  # noqa: F401
from tests.coordinator.test_studio_gate import fakes, repo  # noqa: F401
from tests.coordinator.test_fast_lane import (CAP, GREEN, agent, drive, env, events,  # noqa: F401
                                              fl, task)


def _prompts(e, needle):
    return [x[2] for x in e.client.log if needle in x[2]]


# ------------------------------------------------------------------------------- B1


def test_commit_paths_commits_exactly_the_named_paths(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "keep.txt").write_text("1")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    ws = Workspace(str(root))
    (root / "a.png").write_text("frame")
    (root / "keep.txt").write_text("2")                      # someone else's dirty file
    (root / "staged.txt").write_text("s")
    git(root, "add", "staged.txt")                           # and someone else's staged file
    sha = ws.commit_paths(["a.png"], "T1: gate captures")
    assert sha and sha == ws.head()
    assert git(root, "show", "--name-only", "--format=", "HEAD").split() == ["a.png"]
    st = dict((p, x) for x, p in ws.status())
    assert st["keep.txt"].strip() == "M" and st["staged.txt"].startswith("A")
    assert ws.commit_paths(["a.png"], "again") == ""         # nothing left to commit
    assert ws.commit_paths([], "none") == ""
    files = ws.commit_files(git(root, "rev-parse", "HEAD~1"), sha)
    assert files == [(sha, ["a.png"])]


def test_session_renders_never_stop_the_gate_and_the_coordinator_commits_them(env):
    """UI-D1 / UI-02b: the session's own `stories` runs left re-rendered frames dirty, so
    coordinator.clean failed before the gate (15 "Coordinator checks failed before the
    studio gate" continuations) and again after it (26 clean FAILs)."""
    extra = "docs/evidence/UI-v3/T1/Extra.normal@390x844.png"
    e = env([GREEN])
    real = e.client.script

    def script(ctx):
        out = real(ctx)
        p = e.repo / extra
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\x89PNG\r\n\x1a\n session render")
        return out
    e.client.script = script
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] in (S.COMPLETE, S.BLOCKED, S.CHANGES_REQUIRED))
    assert t["state"] == S.COMPLETE, (t["blocker"], t["next_action"])
    assert len(e.client.log) == 1 and not _prompts(e, "checks failed")
    [cc] = events(e, "coordinator_commit")
    assert extra in json.loads(cc["detail"])["paths"]
    assert not [p for _, p in e.c.projects["game"].ws.status() if "UI-v3" in p]


def test_runner_output_outside_the_plan_scope_is_committed_and_never_a_scope_fail(env):
    log = "logs/T1_runner.log"
    e = env([dict(GREEN, append={log: "GATE T1 PASS\n"})])
    e.c.projects["game"].profile.fast_lane["runner_outputs"].append("logs/{task}_runner.log")
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] in (S.COMPLETE, S.BLOCKED, S.CHANGES_REQUIRED))
    assert t["state"] == S.COMPLETE, (t["blocker"], t["next_action"])
    [cc] = events(e, "coordinator_commit")
    assert log in json.loads(cc["detail"])["paths"]
    rows = e.c.store.q("SELECT check_name, result FROM verifications WHERE set_id=?",
                       (t["verification_id"],))
    assert {r["check_name"]: r["result"] for r in rows}["coordinator.scope"] == "PASS"


def test_scope_blames_only_files_the_agent_committed(env):
    e = env([GREEN])
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] == S.COMPLETE)
    P = e.c.projects["game"]
    spec = P.adapter.describe("T1")
    (e.repo / "other").mkdir()
    (e.repo / "other" / "x.txt").write_text("coordinator bookkeeping")
    sha, err = BK.commit(e.c, P, task(e), ["other/x.txt"], "bookkeeping", "test")
    assert sha and not err and sha in task(e)["data"]["coord_commits"]

    def scope():
        rows = e.c._generic_checks(P, task(e), spec, P.ws.head(), phase="closeout")
        return next(r for r in rows if r.check == "coordinator.scope")
    assert scope().result == "PASS"
    (e.repo / "other" / "y.txt").write_text("agent work")
    git(e.repo, "add", "other/y.txt")
    git(e.repo, "commit", "-q", "-m", "T1: sneak")
    r = scope()
    assert r.result == "FAIL" and "other/y.txt" in r.detail and "other/x.txt" not in r.detail
    assert t["state"] == S.COMPLETE


def test_coordinator_commit_message_is_prefixed():
    from types import SimpleNamespace
    P = SimpleNamespace(profile=SimpleNamespace(commit={"prefix": "{task}: "}))
    assert BK.message(P, "UI-02b", "gate captures") == "UI-02b: gate captures"
    P.profile.commit = {"prefix": "{task}"}
    assert BK.message(P, "T1", "x") == "T1: x"


def test_newest_fast_lane_gate_pass_is_never_flaky(tmp_path):
    """UI-D1 13:49 / UI-02a: a FAIL then a PASS of the fast-lane gate on unchanged
    dependencies read as "flaky" and blocked the submission. The gate is the
    coordinator's own revision-bound run: its newest PASS is the verdict. Any other
    check keeps the flaky rule."""
    from foundry_coordinator.evidence import EvidenceLedger
    from foundry_coordinator.store import Store
    from tests.coordinator.fixtures_evidence import FakeAdapter, make_P, make_ws, spec
    store = Store(str(tmp_path / "s.sqlite"))
    clock = iter(range(10_000, 20_000))
    led = EvidenceLedger(store, str(tmp_path / "st"), clock=lambda: float(next(clock)))
    ws, r1 = make_ws(tmp_path, {"src/a.lua": "1"})
    req = [{"check_id": c, "chain_id": "", "seq": None, "prereqs": [], "runtime": False}
           for c in ("fast_lane.gate", "other.check")]
    P = make_P("p", ws, {}, FakeAdapter(required=req, deps={"fast_lane.gate": ["src/**"],
                                                             "other.check": ["src/**"]}))
    for n, (cid, res) in enumerate([("fast_lane.gate", "FAIL"), ("fast_lane.gate", "PASS"),
                                    ("other.check", "FAIL"), ("other.check", "PASS")]):
        led.record(project_id="p", task_id="T1", check_id=cid, idem_key=f"k{n}", run_id="r",
                   source="coordinator", check_kind="verification", exec_status="completed",
                   result=res, tier="verified", source_revision=r1, started_ts=100.0 + n,
                   ended_ts=101.0 + n, provenance={"summary_line": f"GATE T1 {res}"},
                   output=res)
    cur = led.current(P, spec(), r1)
    assert cur["fast_lane.gate"]["result"] == "PASS" and not cur["fast_lane.gate"].get("flaky")
    assert [m["check_id"] for m in led.missing(P, spec(), r1)] == ["other.check"]


def test_newest_scripted_studio_gate_pass_is_never_flaky(tmp_path):
    """GF-GATE1 15:21 replay: the session's own gate.ps1 FAIL, then the coordinator's
    scripted Studio gate PASS on unchanged dependencies, read as flaky and blocked review.
    A session PASS after a FAIL keeps the flaky rule."""
    from foundry_coordinator.evidence import EvidenceLedger
    from foundry_coordinator.store import Store
    from tests.coordinator.fixtures_evidence import FakeAdapter, make_P, make_ws, spec
    store = Store(str(tmp_path / "s.sqlite"))
    clock = iter(range(10_000, 20_000))
    led = EvidenceLedger(store, str(tmp_path / "st"), clock=lambda: float(next(clock)))
    ws, r1 = make_ws(tmp_path, {"src/a.lua": "1"})
    req = [{"check_id": c, "chain_id": "", "seq": None, "prereqs": [], "runtime": False}
           for c in ("gate:T1", "other.check")]
    P = make_P("p", ws, {}, FakeAdapter(required=req, deps={"gate:T1": ["src/**"],
                                                             "other.check": ["src/**"]}))
    rows = [("gate:T1", "FAIL", "bash", "transcript"), ("gate:T1", "PASS", "studio_gate",
                                                         "coordinator"),
            ("other.check", "FAIL", "bash", "transcript"), ("other.check", "PASS", "bash",
                                                             "transcript")]
    for n, (cid, res, tool, src) in enumerate(rows):
        led.record(project_id="p", task_id="T1", check_id=cid, idem_key=f"k{n}", run_id="r",
                   tool=tool, source=src, check_kind="verification", exec_status="completed",
                   result=res, tier="verified", source_revision=r1, started_ts=100.0 + n,
                   ended_ts=101.0 + n, provenance={"summary_line": f"GATE T1 {res}"},
                   output=res)
    cur = led.current(P, spec(), r1)
    assert cur["gate:T1"]["result"] == "PASS" and not cur["gate:T1"].get("flaky")
    assert [m["check_id"] for m in led.missing(P, spec(), r1)] == ["other.check"]


def test_tick_lines_ticks_verified_rows_but_never_the_close_row():
    plan = ("# Plan\n- [ ] 1. Build the HUD\n- [ ] 2. Run the sprint gate green\n"
            "- [x] 3. Stories render\n- [ ] 4. Captures committed for vision\n"
            "- [ ] 5. CLOSE: summary, state row, gate\n")
    out, ticked = BK.tick_lines(plan, {r"\bgate\b": "GATE T1 PASS at abc",
                                       r"\bcaptures?\b": "12 captures at abc"})
    lines = out.splitlines()
    assert lines[1] == "- [ ] 1. Build the HUD"
    assert lines[2].startswith("- [x] 2. Run the sprint gate green (verified by the coordinator")
    assert lines[4].startswith("- [x] 4. Captures committed")
    assert lines[5] == "- [ ] 5. CLOSE: summary, state row, gate"          # closeout's row
    assert len(ticked) == 2
    assert BK.tick_lines("no checklist", {"x": "y"}) == ("no checklist", [])


# ------------------------------------------------------------------------------- B2


def _rc(cid, result, detail="", required=True):
    return {"id": cid, "result": result, "required": required, "detail": detail, "artifacts": []}


SUITE_OK = "SUITE {} pass=3 fail=0 expect=3 VERDICT PASS"


def _store_gate(tmp_path, verdicts, changed_from, head="h" * 40, trees=None):
    """A Gate over a result store of `verdicts`; changed_from[prev_head] = files changed
    since (None = not an ancestor). trees maps a commit to its root tree id."""
    from foundry_coordinator.projects.animaluprising import studio_gate as G
    gd = tmp_path / "gate"
    gd.mkdir(parents=True, exist_ok=True)
    for i, v in enumerate(verdicts):
        v.setdefault("finished", float(i + 1))
        (gd / f"{v['head'][:12]}.{v.get('mode', 'sprint')}.json").write_text(json.dumps(v))
    trees = trees or {}

    def run_cmd(argv, cwd, timeout):
        if "rev-parse" in argv:
            rev = argv[-1].split("^")[0]
            return (0, trees[rev]) if rev in trees else (1, "")
        if "merge-base" in argv:
            return (0, "") if changed_from.get(argv[-2]) is not None else (1, "")
        if "diff" in argv:
            return 0, "\n".join(changed_from.get(argv[-2]) or [])
        return 1, ""
    g = G.Gate.__new__(G.Gate)
    g.head, g.gate_dir, g.repo, g.run_cmd = head, str(gd), str(tmp_path), run_cmd
    g.index = {"entries": [{"id": x, "file": f"{x}_check"} for x in ("S45", "S46", "S49")],
               "subsets": {"ui": ["S45", "S46", "S49"]}}
    return g


def test_same_head_verdict_is_reused_never_rerun(tmp_path, monkeypatch):
    from foundry_coordinator.projects.animaluprising import studio_gate as G
    monkeypatch.setattr(G, "suite_reader", lambda repo, rev: (lambda f: ""))
    h = "h" * 40
    v = {"head": h, "checks": [_rc("regression.S45", "PASS", SUITE_OK.format("S45")),
                               _rc("regression.S46", "FAIL", "SUITE S46 pass=1 fail=1 VERDICT FAIL"),
                               _rc("story.Hud.normal@390x844", "PASS", "ok"),
                               _rc("story.Menu.normal@390x844", "FAIL", "clip")],
         "captures": [{"story": "Hud.normal", "path": "a.png"},
                      {"story": "Menu.normal", "path": "b.png"}]}
    c = _store_gate(tmp_path, [v], {}).carry_plan("S45,S46", "docs/progress/T_gate.json")
    assert c["suites"] == ["S45"]                          # a red suite never carries
    assert c["story_from"]["head"] == h and c["stories"] is False
    assert c["stories_failed"] == ["Menu.normal"]          # only the failed story re-runs


def test_each_suite_carries_from_the_newest_verdict_that_still_covers_it(tmp_path, monkeypatch):
    """The newest verdict ran only S46 (S45 was carried); an older one and the baseline
    hold S45's and S49's passes. Nothing they cover changed: none re-runs."""
    from foundry_coordinator.projects.animaluprising import studio_gate as G
    monkeypatch.setattr(G, "suite_reader", lambda repo, rev: (lambda f: ""))
    base, old, new = "b" * 40, "o" * 40, "n" * 40

    def vs():
        return [{"head": base, "mode": "baseline", "checks": [
                    _rc("regression.S49", "PASS", SUITE_OK.format("S49")),
                    _rc("regression.S45", "FAIL", "SUITE S45 pass=2 fail=1 VERDICT FAIL")]},
                {"head": old, "checks": [_rc("regression.S45", "PASS", SUITE_OK.format("S45"))]},
                {"head": new, "checks": [_rc("regression.S46", "PASS", SUITE_OK.format("S46"))]}]
    g = _store_gate(tmp_path / "a", vs(), {base: ["docs/x.md"], old: ["docs/x.md"],
                                           new: ["docs/x.md"]})
    c = g.carry_plan("S45,S46,S49", "docs/progress/T_gate.json")
    assert c["suites"] == ["S45", "S46", "S49"]
    assert c["suite_from"]["S45"]["head"] == old and c["suite_from"]["S49"]["mode"] == "baseline"
    assert c["from"]["head"] == new                       # the newest verdict, for the log
    # a change S46's own file covers re-runs S46 only
    g = _store_gate(tmp_path / "b", vs(), {base: [], old: [], new: ["tests/S46_check.lua"]})
    assert g.carry_plan("S45,S46,S49", "p")["suites"] == ["S45", "S49"]
    # commits off HEAD's history say nothing about it
    g = _store_gate(tmp_path / "c", vs(), {base: None, old: None, new: None})
    assert g.carry_plan("S45,S46,S49", "p")["suites"] == []


def test_identical_tree_carries_like_the_same_commit(tmp_path, monkeypatch):
    """Content addressing: a verdict at another commit with the same root tree judged the
    same bytes (e.g. a message-only rewrite): everything it passed carries."""
    from foundry_coordinator.projects.animaluprising import studio_gate as G
    monkeypatch.setattr(G, "suite_reader", lambda repo, rev: (lambda f: ""))
    other, head = "o" * 40, "h" * 40
    v = {"head": other, "tree": "c" * 40, "checks": [
        _rc("regression.S45", "PASS", SUITE_OK.format("S45")),
        _rc("story.Hud.normal@390x844", "PASS", "ok")]}
    g = _store_gate(tmp_path, [v], {other: None}, head=head, trees={head: "c" * 40})
    c = g.carry_plan("S45", "p")
    assert c["suites"] == ["S45"] and c["stories"] is True


def test_runner_baseline_mode_records_baseline_json(fakes, repo):
    from foundry_coordinator.projects.animaluprising import studio_gate as G
    from tests.coordinator.test_studio_gate import gate_file, regression_rules, story_plan
    set_rules, _ = fakes
    set_rules(*regression_rules())
    story_plan(repo, files=["tests/S28_check.lua"], selection="ui")
    out = []
    rc = G.main(["--repo", str(repo), "--timeout", "20", "--run-id", "base-1", "baseline",
                 "--sprint", "UI-T0"], out=out.append)
    assert rc == 0, out
    b = json.load(open(gate_file(repo, "UI-T0", "baseline.json")))
    assert b["mode"] == "baseline" and b["run_id"] == "base-1"
    assert any(c["id"].startswith("regression.S") for c in b["checks"])
    assert gate_file(repo, "UI-T0", f"{b['head'][:12]}.baseline.json").exists()
    assert G.load_baseline(str(gate_file(repo, "UI-T0", "x").parent)) is not None
    assert not (repo / "docs" / "progress" / "UI-T0_EVIDENCE_RAW.log").exists()   # no evidence


def test_runner_baseline_of_a_mock_sprint_runs_nothing(repo):
    from foundry_coordinator.projects.animaluprising import studio_gate as G
    from tests.coordinator.test_studio_gate import gate_file, story_plan
    story_plan(repo, lane="mock")
    out = []
    assert G.main(["--repo", str(repo), "--run-id", "b2", "baseline", "--sprint", "UI-T0"],
                  out=out.append) == 0, out
    b = json.load(open(gate_file(repo, "UI-T0", "baseline.json")))
    assert [c["id"] for c in b["checks"]] == ["regression.selection"]


def test_automatic_baseline_runs_once_before_the_first_session(env):
    from tests.coordinator.test_fast_lane import RED
    e = env([RED, GREEN])
    e.c.approve("game", "T1", [], "victor")
    first = drive(e, lambda t: t["state"] == S.IMPLEMENTING or bool(e.client.log))
    assert events(e, "fast_lane_baseline"), first["next_action"]   # done before the session
    t = drive(e, lambda t: t["state"] == S.COMPLETE)
    started = events(e, "fast_lane_baseline_started")
    [done] = events(e, "fast_lane_baseline")
    assert len(started) == 1 and json.loads(done["detail"])["result"] == "PASS"
    b = fl(e)["baseline"]
    assert b["status"] == "done" and b["head"] == t["data"]["task_base"]
    assert (e.repo / ".plan" / "tmp" / "gate" / "T1" / "baseline.json").exists()


def test_an_errored_baseline_never_blocks_the_sprint(env, monkeypatch):
    monkeypatch.setenv("FAKE_BASELINE_RESULT", "ERROR")
    e = env([GREEN])
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] in (S.COMPLETE, S.BLOCKED))
    assert t["state"] == S.COMPLETE
    assert fl(e)["baseline"]["status"] == "error" and events(e, "fast_lane_baseline_error")


def test_a_present_baseline_is_not_run_again(env):
    e = env([GREEN])
    d = e.repo / ".plan" / "tmp" / "gate" / "T1"
    d.mkdir(parents=True)
    (d / "baseline.json").write_text(json.dumps({"head": "x", "checks": []}))
    e.c.approve("game", "T1", [], "victor")
    drive(e, lambda t: t["state"] == S.COMPLETE)
    assert not events(e, "fast_lane_baseline_started")
    assert fl(e)["baseline"]["status"] == "skipped"


def test_prompts_never_ask_the_session_to_run_the_sprint_gate(env):
    from tests.coordinator.test_fast_lane import RED
    e = env([RED, GREEN])
    e.c.approve("game", "T1", [], "victor")
    drive(e, lambda t: t["state"] == S.COMPLETE)
    [red] = _prompts(e, "Studio gate FAIL")
    assert "never run `sprint` or `regression` yourself" in red
    assert "`regression --selection" not in red


# ------------------------------------------------------------------------------- B3


def _caps(n):
    return [CAP.replace(".png", f"-{i}.png") for i in range(n)]


def test_vision_batches_run_concurrently(env):
    """A full pass took 30-39 min because batches ran one call per tick (UI-D1)."""
    import threading
    import time as _time
    e = env([{"result": "PASS", "captures": _caps(8)}])
    s = e.c.projects["game"].profile.fast_lane
    s["vision_batch_frames"], s["vision_parallel"] = 2, 4
    lock, live, peak = threading.Lock(), [0], [0]
    real = e.ff.chat

    def chat(*a, **kw):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        _time.sleep(0.3)
        try:
            return real(*a, **kw)
        finally:
            with lock:
                live[0] -= 1
    e.ff.chat = chat
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] in (S.COMPLETE, S.BLOCKED))
    assert t["state"] == S.COMPLETE, t["blocker"]
    assert len([x for x in e.ff.chats if x.get("packet") == "vision"]) == 4
    assert peak[0] >= 2                               # the 4 batches were in flight together
    [a] = e.c.store.approvals("game", "T1", "visual")
    assert set(a["artifacts"]) == set(_caps(8))


def test_clean_memory_is_saved_after_each_batch(env):
    """A restart or an error mid-round lost every batch already judged clean: memory was
    saved only when the whole round finished."""
    e = env([{"result": "PASS", "captures": _caps(3)}])
    e.c.projects["game"].profile.fast_lane["vision_batch_frames"] = 1
    e.c.approve("game", "T1", [], "victor")
    drive(e, lambda t: len(events(e, "model_visual_review")) == 1)
    assert task(e)["state"] == S.VERIFYING
    assert len(fl(e)["vision_clean"]) == 1           # batch 1's frame, before the round ends
    assert fl(e)["vision_round"]["batches"] == [[c] for c in _caps(3)]
    drive(e, lambda t: t["state"] == S.COMPLETE)
    assert len(fl(e)["vision_clean"]) == 3


def test_inconclusive_vision_never_stops_the_sprint(env):
    """UI-D1 04:24, 08:56, 11:44 and UI-02a 03:13: an inconclusive vision answer stopped
    the task (BLOCKED, lead case, operator unblock). One structured retry, then notes."""
    e = env([{"result": "PASS", "captures": _caps(2)}], vision=["uncertain"] * 10)
    schemas = []
    real = e.ff.chat

    def chat(persona, messages, **kw):
        if kw.get("session_id", "").startswith("fast-lane-vision"):
            schemas.append(kw.get("schema"))
        return real(persona, messages, **kw)
    e.ff.chat = chat
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] in (S.COMPLETE, S.BLOCKED))
    assert t["state"] == S.COMPLETE, t["blocker"]
    assert schemas[0] is None and schemas[1:] and all(
        s and s["properties"]["verdict"]["enum"] for s in schemas[1:])   # the retry is structured
    assert len(schemas) == 1 + 1                       # one retry (2 frames fit one chunk)
    assert not events(e, "fast_lane_gate_error") and not t["data"].get("recovery_case")
    assert "inconclusive" in e.c.store.approvals("game", "T1", "visual")[-1]["note"]


def test_vision_changes_get_one_fix_round_by_default(env):
    """Decision 2026-10-21: vision blocks only on changed frames, one fix round, then
    accept with notes (max_vision_rounds default 1)."""
    e = env([GREEN], vision=["changes_required"] * 3)
    e.c.projects["game"].profile.fast_lane["max_vision_rounds"] = 1
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] == S.COMPLETE)
    assert len(_prompts(e, "Vision review: CHANGES")) == 1 and t["sessions"] == 1
    assert len(events(e, "model_visual_review")) == 2


def test_after_close_vision_skips_frames_already_judged_clean(tmp_path):
    """The legacy after_close path (visual_approval.step) sent every frame in one call with
    no memory. It now shares the clean-frame memory with the fast lane."""
    from types import SimpleNamespace
    from foundry_coordinator import visual_approval as VA
    from tests.coordinator.test_visual_model_approval import _review_setup
    project, spec, rule, rel, digest = _review_setup(tmp_path)
    rule.kind = "visual"
    project.id = "animaluprising"
    project.profile.raw = {"approval": {"visual_model": True}}
    project.profile.persona = lambda role, task: "vision"
    project.ws = SimpleNamespace(head=lambda: "revision", status=lambda: [])
    project.adapter = SimpleNamespace(approval_artifacts=lambda *_: {rel: digest})
    sent = []
    project.foundry = SimpleNamespace(
        persona_info=lambda _: {"exists": True, "enabled": True, "strict": True,
                                "routes": [{"model": "vision-model", "vision": True}]},
        bind=lambda *a, **k: None,
        chat=lambda persona, messages, **kw: (sent.append(1) or json.dumps({
            "verdict": "approve", "frames": [{"path": rel, "sha256": digest,
                                              "image_seen": True, "subject_present": True,
                                              "validity": "ok", "observation": "HUD",
                                              "findings": []}]}),
            {"served_model": "vision-model"}))
    data = {"fast_lane": {"vision_clean": [digest]}}
    approvals, ops = [], {}
    store = SimpleNamespace(
        task=lambda pid, tid: {"data": data}, update_task=lambda *a, **k: None,
        merge_data=lambda pid, tid, **kw: data.update(kw), event=lambda *a, **k: None,
        op_begin=lambda key, *a: ops.get(key) or ops.setdefault(key, {"status": "started"})
        and None, op_update=lambda key, st, res: ops.__setitem__(key, {"status": st,
                                                                       "result": res}),
        session=lambda sid: None, add_session=lambda *a, **k: None,
        update_session=lambda *a, **k: None, add_approval=lambda *a: approvals.append(a))
    c = SimpleNamespace(store=store, clock=lambda: 1.0, _valid_approval=lambda *a: None)
    spec.spec = "HUD"
    t = {"task_id": "UI-02", "run_id": "r", "session_id": "p", "attempt": 1}
    VA.step(c, project, t, spec, rule, "revision")
    assert not sent and approvals[-1][5] == "coordinator:vision-memory"
    data.clear()                                       # no memory: one call, then remembered
    VA.step(c, project, t, spec, rule, "revision")
    assert len(sent) == 1 and data["vision_clean"] == [digest]
    assert VA.clean_frames("changes_required", [f"{rel}: clipped"], [rel, "b.png"]) == ["b.png"]
    assert VA.clean_frames("changes_required", ["unplaced finding"], [rel]) == []


# ------------------------------------------------------------------------------- B4


def _review_calls(e):
    return [x for x in e.ff.chats if x.get("packet") != "vision"]


def _reviewer(verdicts, seen=None):
    """A reviewer answering CHANGES_REQUIRED / APPROVED in order (then APPROVED)."""
    from tests.coordinator.helpers import review_json
    queue = list(verdicts)

    def answer(packet):
        if seen is not None:
            seen.append(packet)
        v = queue.pop(0) if queue else "APPROVED"
        if v == "CHANGES_REQUIRED":
            return review_json(packet, v, findings=[{"severity": "major", "file": "src/a.txt",
                                                     "line": 1, "message": "name the HUD id"}])
        return review_json(packet, v)
    return answer


def test_code_review_runs_alongside_vision_and_is_not_paid_twice(env):
    """Review and vision are independent once the gate is green: the review now starts
    with the vision batches instead of after a 30+ minute vision pass."""
    e = env([{"result": "PASS", "captures": _caps(3)}])
    e.c.projects["game"].profile.fast_lane["vision_batch_frames"] = 1
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] == S.COMPLETE)
    [started] = events(e, "early_review_started")
    visions = events(e, "model_visual_review")
    assert started["id"] < visions[-1]["id"]            # started before vision finished
    assert len(_review_calls(e)) == 1                   # collected, never called again
    [rev] = e.c.store.reviews("game", "T1")
    assert rev["verdict"] == "APPROVED" and rev["result"]["packet"]["early"] is True
    assert t["sessions"] == 1


def test_review_and_vision_changes_go_out_as_one_fix_round(env):
    seen = []
    e = env([GREEN], vision=["changes_required", "approve"])
    e.ff.reviewer = _reviewer(["CHANGES_REQUIRED", "APPROVED"], seen)
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] == S.COMPLETE)
    rounds = _prompts(e, "Vision review: CHANGES")
    assert len(rounds) == 1 and "code review" in rounds[0] and "CHANGES_REQUIRED" in rounds[0]
    assert "tab row overlaps the close button" in rounds[0] and "name the HUD id" in rounds[0]
    assert len(e.client.log) == 2 and t["sessions"] == 1    # one warm session, one round
    assert events(e, "fast_lane_merged_fix_round")
    revs = e.c.store.reviews("game", "T1")
    assert [r["verdict"] for r in revs] == ["CHANGES_REQUIRED", "APPROVED"]
    # the re-review after the merged round was incremental
    assert revs[1]["result"]["packet"]["incremental_from"] == revs[0]["reviewed_sha"]
    assert "incremental re-review" in seen[1]


def test_review_changes_alone_go_back_to_the_same_session(env):
    """8 of 11 Deep-Reviews were CHANGES; each started a cold implementer session."""
    e = env([GREEN])
    e.ff.reviewer = _reviewer(["CHANGES_REQUIRED", "APPROVED"])
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] == S.COMPLETE)
    [fix] = _prompts(e, "Code review")
    assert "name the HUD id" in fix and t["sessions"] == 1 and t["attempt"] == 1
    hist = [h["to_state"] for h in e.c.store.history("game", "T1")]
    assert S.CHANGES_REQUIRED not in hist


def test_coordinator_ticks_the_checklist_rows_the_gate_verifies(env):
    from tests.coordinator.test_fast_lane import T1
    plan = "docs/progress/T1_PLAN.md"
    text = ("# T1\n- [ ] 1. Build the HUD\n- [ ] 2. Studio gate green\n"
            "- [ ] 3. Captures committed\n- [ ] 4. CLOSE: summary and gate\n")
    e = env([GREEN], tasks=(dict(T1, plan=plan),), files={plan: text})
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] == S.COMPLETE)
    got = (e.repo / plan).read_text().splitlines()
    assert got[1] == "- [ ] 1. Build the HUD"
    assert got[2].startswith("- [x] 2. Studio gate green (verified by the coordinator: GATE T1 PASS")
    assert got[3].startswith("- [x] 3. Captures committed (verified by the coordinator: 1 capture")
    assert got[4] == "- [ ] 4. CLOSE: summary and gate"
    assert not [p for _, p in e.c.projects["game"].ws.status() if p == plan]
    [cc] = events(e, "coordinator_commit")
    assert plan in json.loads(cc["detail"])["paths"]
    rows = e.c.store.q("SELECT check_name, result FROM verifications WHERE set_id=?",
                       (t["verification_id"],))
    assert {r["check_name"]: r["result"] for r in rows}["coordinator.scope"] == "PASS"


def test_review_packet_carries_the_coordinators_own_evidence(env):
    seen = []
    e = env([GREEN])
    e.ff.reviewer = _reviewer(["APPROVED"], seen)
    e.c.approve("game", "T1", [], "victor")
    drive(e, lambda t: t["state"] == S.COMPLETE)
    pk = seen[0]
    assert "studio gate verdict (read by the coordinator)" in pk
    assert "coordinator bookkeeping (not for review)" in pk and "never a review finding" in pk
    assert "capture(s) from the gate, committed by the coordinator" in pk


def test_same_commit_re_review_is_incremental(env):
    """[lifecycle] evidence_incomplete re-reviewed the same commit with the FULL packet."""
    e = env([GREEN])
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] == S.COMPLETE)
    P = e.c.projects["game"]
    head = t["head_sha"]
    [ok] = e.c.store.reviews("game", "T1")
    res = {"verdict": "CHANGES_REQUIRED", "findings": [{"severity": "major", "file": "",
                                                        "line": 0, "message": "attach log"}],
           "missing_evidence": ["the gate log"], "required_repairs": [],
           "packet": {"version": 2}}
    rid = e.c.store.save_review("game", "T1", head, "reviewer", "Foundry-Kilo-Sonnet",
                                "CHANGES_REQUIRED", True, "", res, "{}")
    e.c.store.merge_data("game", "T1", lc_rereview=[ok["review_id"], rid])
    e.c._lc = lambda P_, part="": True if part == "" else False
    prep = e.c._review_prepare(P, e.c.store.task("game", "T1"), head)
    assert prep and not prep.get("apply")
    pk = prep["packet"]
    assert "incremental re-review of the SAME commit" in pk and "attach log" in pk
    assert "## Diff (per file, priority order)\n- (no changes)" in pk
    assert prep["meta_packet"]["incremental_from"] == head


def test_evidence_is_capped_and_unchanged_items_lose_their_text():
    from types import SimpleNamespace
    from foundry_coordinator.engine import Coordinator
    c = Coordinator.__new__(Coordinator)
    P = SimpleNamespace(profile=SimpleNamespace(review={"max_evidence_items": 3}),
                        ws=SimpleNamespace(diff_files=lambda a, b: [{"path": "changed.log"}]))
    ev = [{"title": "a", "priority": 3, "text": "x", "path": "u.log", "status": "ok"},
          {"title": "b", "priority": 0, "text": "y", "path": "", "status": "ok"},
          {"title": "c", "priority": 1, "text": "z", "path": "changed.log", "status": "ok"},
          {"title": "d", "priority": 0, "text": "w", "path": "", "status": "missing"}]
    kept, dropped = c._review_evidence_cap(P, ev)
    assert [x["title"] for x in kept] == ["b", "c", "d"] and dropped == 1   # essentials kept
    out, n = c._review_evidence_delta(P, ev, "a" * 40, "b" * 40, {"review_id": "rv-1"})
    assert n == 1 and out[0]["text"] == "" and "unchanged since review rv-1" in out[0]["note"]
    assert out[2]["text"] == "z"                         # its file changed: sent again


def test_earlier_findings_are_summarized_newest_first():
    from foundry_coordinator import review as R

    def rv(i, n):
        return {"review_id": f"rv-{i}", "reviewed_sha": f"{i}" * 40, "verdict": "CHANGES_REQUIRED",
                "persona": "X", "result": {"packet": {"version": 2}, "findings": [
                    {"severity": "major", "file": "a", "line": 1, "message": f"r{i} f{k} " * 40}
                    for k in range(n)]}}
    out = "\n".join(R._earlier_section([rv(1, 3), rv(2, 25)], "h" * 40))
    assert out.index("rv-2") < out.index("rv-1")        # newest first
    assert "... and 5 more finding(s)" in out           # 20 listed, the rest counted
    assert "- 3 finding(s); first:" in out              # the older one summarized
    assert max(len(ln) for ln in out.splitlines()) <= 320


def test_lead_repair_review_is_incremental(ws, router):
    from tests.coordinator.test_repair_workspace import FakeFoundry, P as RP, _committed
    path = _committed(ws, router)
    first = FakeFoundry("CHANGES_REQUIRED", [{"severity": "major", "file": "calc.py", "line": 2,
                                              "message": "also handle mul by zero"}])
    r1 = ws.review(RP(first), path, case_id="case-r", lead_persona="Lead",
                   reviewer_persona="Reviewer", project_id="p1")
    assert r1["verdict"] == "CHANGES_REQUIRED" and r1["incremental_from"] is None
    (Path(path) / "notes.txt").write_text("follow-up\n")
    subprocess.run(["git", "-C", path, "add", "notes.txt"], check=True)
    subprocess.run(["git", "-C", path, "commit", "-q", "-m", "follow-up"], check=True)
    second = FakeFoundry("APPROVED")
    r2 = ws.review(RP(second), path, case_id="case-r", lead_persona="Lead",
                   reviewer_persona="Reviewer", project_id="p1")
    pk = second.calls[0]["packet"]
    assert r2["approved"] and r2["incremental_from"] == r1["reviewed_sha"]
    assert "incremental re-review" in pk and "also handle mul by zero" in pk
    assert "+follow-up" in pk and "return a + b" not in pk.split("## Diff", 1)[1]


# ------------------------------------------------------------------------------- B7


def _session_closeout(e, template=None, finish=None):
    """The project closes out with a CLOSE session ([closeout] mode session); the
    adapter optionally templates it. The scripted CLOSE session writes a marker."""
    P = e.c.projects["game"]
    P.adapter.closeout_mode = lambda: "session"
    if template is not None:
        P.adapter.closeout_template = template
    if finish is not None:
        P.adapter.closeout_template_finish = finish
    real = e.client.script

    def script(ctx):
        if ctx.get("phase") == "closeout":
            return {"writes": {"docs/progress/T1_SUMMARY.md": "by the CLOSE session\n"},
                    "commit": "T1: closeout",
                    "final": '```coordinator-report\n{"status": "DONE", "summary": "ok"}\n```'}
        return real(ctx)
    e.client.script = script
    return P


def test_closeout_is_templated_by_the_coordinator_without_a_session(env):
    seen = []

    def template(spec, ctx):
        seen.append(ctx)
        return {"files": {f"docs/progress/{spec.task_id}_SUMMARY.md":
                          f"# {spec.task_id}\nDONE at {ctx['head'][:12]}, review "
                          f"{ctx['review']['verdict']}\n"}, "notes": ["summary"]}
    e = env([GREEN])
    _session_closeout(e, template)
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] in (S.COMPLETE, S.BLOCKED, S.CHANGES_REQUIRED))
    assert t["state"] == S.COMPLETE, (t["blocker"], t["next_action"])
    assert t["sessions"] == 1 and not t["data"].get("closeout_session")   # no CLOSE session
    assert (e.repo / "docs/progress/T1_SUMMARY.md").read_text().startswith("# T1\nDONE at")
    assert seen[0]["gate"]["result"] == "PASS" and seen[0]["review"]["verdict"] == "APPROVED"
    kinds = [json.loads(x["detail"])["kind"] for x in events(e, "coordinator_commit")]
    assert "closeout" in kinds and events(e, "closeout_templated")
    head = git(e.repo, "rev-parse", "HEAD")
    assert git(e.repo, "ls-remote", "origin", "main").split()[0] == head      # published


def test_a_failed_template_hands_over_to_the_close_session(env):
    e = env([GREEN])
    _session_closeout(e, lambda spec, ctx: {"error": "STATE.md has no ledger table"})
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] in (S.COMPLETE, S.BLOCKED, S.CHANGES_REQUIRED))
    assert t["state"] == S.COMPLETE, (t["blocker"], t["next_action"])
    assert t["sessions"] == 2 and t["data"].get("closeout_session")
    assert (e.repo / "docs/progress/T1_SUMMARY.md").read_text() == "by the CLOSE session\n"
    [f] = events(e, "closeout_template_failed")
    assert "no ledger table" in json.loads(f["detail"])["reason"]


def test_a_template_whose_finish_fails_hands_over_with_its_documents(env):
    e = env([GREEN])
    _session_closeout(e, lambda spec, ctx: {"files": {"docs/progress/T1_SUMMARY.md": "draft\n"}},
                      lambda spec, ctx: {"error": "done_check did not pass"})
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] in (S.COMPLETE, S.BLOCKED, S.CHANGES_REQUIRED))
    assert t["state"] == S.COMPLETE and t["sessions"] == 2
    assert t["data"]["closeout_template"]["status"] == "failed"


def test_templated_closeout_pushes_even_when_publish_is_verify_only(env):
    """verify_only expected the CLOSE session to push; with no session the coordinator
    pushes once itself."""
    e = env([GREEN])
    P = _session_closeout(e, lambda spec, ctx: {"files": {"docs/progress/T1_SUMMARY.md": "x\n"}})
    P.profile.publish["mode"] = "verify_only"
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] in (S.COMPLETE, S.BLOCKED, S.CHANGES_REQUIRED))
    assert t["state"] == S.COMPLETE, t["blocker"]
    assert git(e.repo, "ls-remote", "origin", "main").split()[0] == t["head_sha"] or \
        git(e.repo, "ls-remote", "origin", "main").split()[0] == git(e.repo, "rev-parse", "HEAD")


def _au_spec(tmp_path, visual=False, plan_text=None):
    from types import SimpleNamespace
    root = tmp_path / "au"
    (root / "docs" / "progress").mkdir(parents=True)
    if plan_text is not None:
        (root / "docs" / "progress" / "UI-03_PLAN.md").write_text(plan_text)
    spec = SimpleNamespace(task_id="UI-03", title="Hero build", data={
        "visual": visual, "plan": "docs/progress/UI-03_PLAN.md"})
    adapter = SimpleNamespace(p=SimpleNamespace(workspace=str(root)))
    ctx = {"head": "a" * 40, "base": "b" * 40, "reviewed_sha": "a" * 40, "date": "2026-10-21",
           "changed": ["src/client/UI/v3/Hero.lua"], "commits": [["a" * 40, "UI-03: x"]],
           "review": {"review_id": "rv-1", "verdict": "APPROVED", "persona": "Astra"},
           "visual": None, "vision_notes": ["Hero@390x844: tighten spacing"],
           "gate": {"head": "a" * 40, "verdict": "v.json", "summary": "GATE UI-03 PASS",
                    "result": "PASS", "checks": 12, "captures": 5}}
    return root, spec, adapter, ctx


def test_au_template_edits_or_appends_the_ledger_row(tmp_path):
    from foundry_coordinator.projects.animaluprising import closeout_template as CT
    _root, spec, _a, ctx = _au_spec(tmp_path)
    table = "# STATE\r\n| Sprint | Status | Date | Notes |\r\n|---|---|---|---|\r\n" \
            "| UI-02 | DONE | 2026-10-20 | x |\r\n"
    new, why = CT.state_row(table, spec, ctx)
    last = next(ln for ln in new.split("\r\n") if ln.startswith("| UI-03"))
    assert why == "appended a ledger row" and last.startswith("| UI-03 | DONE | 2026-10-21 | ")
    assert last.count("|") == 5 and len(last) <= 600 and "\r\n" in new
    again, why = CT.state_row(new.replace("| UI-03 | DONE", "| UI-03 | IN PROGRESS"), spec, ctx)
    assert why == "edited the sprint's ledger row" and again.count("| UI-03 |") == 1
    assert "| UI-03 | DONE |" in again
    assert CT.state_row("no table here", spec, ctx)[0] is None
    spec.data["visual"] = True
    assert "COMPLETE-PENDING-VISUAL-REVIEW" in CT.state_row(table, spec, ctx)[0]


def test_au_template_master_order_close_row_and_summary(tmp_path):
    from foundry_coordinator.projects.animaluprising import closeout_template as CT
    plan = ("# UI-03\n- [x] 1. Build\n- [ ] 2. CLOSE: summary, STATE row, MASTER_ORDER\n")
    root, spec, adapter, ctx = _au_spec(tmp_path, plan_text=plan)
    (root / "docs" / "progress" / "STATE.md").write_text("| UI-02 | DONE | d | n |\n")
    (root / "docs" / "MASTER_ORDER.md").write_text("1. UI-02 DONE\n2. UI-03 IN PROGRESS\n")
    (root / "docs" / "progress" / "_SUMMARY_TEMPLATE.md").write_text(
        "# <ID>\n## What shipped\n## Evidence\n## Risks and follow-ups\n")
    out = CT.build(adapter, spec, ctx)
    f = out["files"]
    assert f["docs/MASTER_ORDER.md"].splitlines()[1] == "2. UI-03 DONE"
    assert f["docs/progress/UI-03_PLAN.md"].splitlines()[2].startswith(
        "- [x] 2. CLOSE: summary, STATE row, MASTER_ORDER (verified by the coordinator: closed")
    s = f["docs/progress/UI-03_SUMMARY.md"]
    assert "**Status:** DONE" in s and "src/client/UI/v3/Hero.lua" in s
    assert "GATE UI-03 PASS" in s and "tighten spacing" in s
    assert s.index("## What shipped") < s.index("## Evidence") < s.index("## Risks")
    # no STATE.md: the template refuses (the CLOSE session handles it)
    os.remove(root / "docs" / "progress" / "STATE.md")
    assert "error" in CT.build(adapter, spec, ctx)


def test_au_template_finish_requires_done_check_pass(tmp_path):
    from types import SimpleNamespace
    from foundry_coordinator.projects.animaluprising import closeout_template as CT
    root, spec, adapter, ctx = _au_spec(tmp_path)
    log = tmp_path / "dc.log"
    log.write_text("ROW PASS x\nDONE-CHECK UI-03 PASS\n")
    res = [SimpleNamespace(check="au.done_check.SUMMARY", result="PASS", detail="", evidence=""),
           SimpleNamespace(check="au.done_check", result="PASS", detail="DONE-CHECK UI-03 PASS",
                           evidence=str(log))]
    adapter._done_check = lambda spec_, phase, rev: res
    (root / "docs" / "progress" / "UI-03_EVIDENCE_RAW.log").write_bytes(b"GATE UI-03 PASS\n")
    out = CT.finish(adapter, spec, ctx)
    text = out["files"]["docs/progress/UI-03_EVIDENCE_RAW.log"]
    assert text.startswith("GATE UI-03 PASS\n=== DONE-CHECK UI-03 (coordinator closeout")
    assert text.rstrip().endswith("DONE-CHECK UI-03 PASS")
    res[1].result = "FAIL"
    assert "did not pass" in CT.finish(adapter, spec, ctx)["error"]


def test_a_refused_early_review_is_tried_once_per_head(env):
    e = env([GREEN])
    calls = []
    e.c._review_prepare = lambda P, t, head, early=False: calls.append(head) and None
    P = e.c.projects["game"]
    t = {"task_id": "T1", "data": {}}
    e.c.store.task = (lambda real: (lambda pid, tid: real(pid, tid) or {"task_id": tid,
                                                                       "data": {}}))(e.c.store.task)
    assert e.c.early_review_start(P, t, "h" * 40, "v1") is False
    assert e.c.early_review_start(P, t, "h" * 40, "v1") is False
    assert calls == ["h" * 40]                      # the packet is not rebuilt every tick
    assert e.c.early_review_start(P, t, "k" * 40, "v1") is False and len(calls) == 2


def test_coordinator_commits_never_invalidate_the_review(env):
    e = env([GREEN])
    e.c.approve("game", "T1", [], "victor")
    t = drive(e, lambda t: t["state"] == S.COMPLETE)
    P = e.c.projects["game"]
    spec = P.adapter.describe("T1")
    (e.repo / "docs" / "MASTER_ORDER.md").write_text("1. T1 DONE\n")
    sha, err = BK.commit(e.c, P, task(e), ["docs/MASTER_ORDER.md"], "closeout", "closeout")
    assert sha and not err
    assert e.c._review_still_valid(P, task(e), spec, P.ws.head()) is None
    (e.repo / "src").mkdir(exist_ok=True)
    (e.repo / "src" / "a.txt").write_text("changed by someone else OK")
    git(e.repo, "add", "src/a.txt")
    git(e.repo, "commit", "-q", "-m", "T1: later")
    why = e.c._review_still_valid(P, task(e), spec, P.ws.head())
    assert why and "src/a.txt" in why and "MASTER_ORDER" not in why
    assert t["reviewed_sha"]


def test_scope_never_blames_a_sprint_for_another_sprints_commits():
    """AS-P5 2026-10-28 replay: UI-SKIN2's captures and coordinator commits for AS-V0x /
    AS-D13 landed on master mid-sprint; AS-P5's scope check blamed it and every scope
    grant it then asked for restarted it (sessions at 23:38, 23:47, 23:58)."""
    from types import SimpleNamespace
    from foundry_coordinator.bookkeeping import agent_files, other_sprint
    assert other_sprint("UI-SKIN2: hero captures", "AS-P5")
    assert other_sprint("S57a: balance rows", "AS-P5")
    assert not other_sprint("AS-P5: fix the S24 fixture", "AS-P5")
    assert not other_sprint("prep: rules streamline", "AS-P5")
    assert not other_sprint("Merge branch 'x'", "AS-P5")
    ws = SimpleNamespace(
        commits=lambda b, h: [("a1", "AS-P5: own work"), ("b2", "UI-SKIN2: captures"),
                              ("c3", "AS-V01: docs (coordinator)")],
        commit_files=lambda b, h: [("a1", ["src/x.lua"]), ("b2", ["docs/evidence/UI-SKIN2/h.jpg"]),
                                   ("c3", ["docs/progress/AS-V01_PLAN.md"])])
    P = SimpleNamespace(ws=ws)
    assert agent_files(P, {"task_id": "AS-P5", "data": {}}, "base", "head") == {"src/x.lua"}


def test_a_double_prefixed_commit_belongs_to_its_inner_sprint():
    """2026-10-29: commits reworded to "UI-SKIN2: AS-P5: ..." before v0.157.19 stay in
    history; they are AS-P5's (UI-SKIN2 then asked for scope on AS-P5's files)."""
    from foundry_coordinator.bookkeeping import other_sprint, sprint_owner
    s = "UI-SKIN2: AS-P5: R-B frame re-shot at the reviewed HEAD"
    assert sprint_owner(s) == "AS-P5"
    assert other_sprint(s, "UI-SKIN2") and not other_sprint(s, "AS-P5")
    assert sprint_owner("UI-SKIN2: hero captures") == "UI-SKIN2"
    assert sprint_owner("prep: rules") == ""
