"""UI-02 regression: a committed, partially ticked BUILD plan ("[~]" steps with
notes) and the task's own untracked gate outputs must continue BUILD, not fall
into final verification and a false "no progress across sessions" decision.

The live failure (v0.122.4): UI-02 reached a committed HEAD with plan steps 1-3
[x], 4-6 [~] (remaining work noted), 7 [x] and 8 [ ]. `[~]` rows were invisible
to the AU checklist parser, the partial-BUILD continuation's clean check failed
on three untracked docs/progress/UI-02_gate*.json files that tools/gate.ps1 had
written, final verification repeated the expected unfinished-BUILD findings,
and recovery.progress counted every committing session as a stall (same
findings) -> decision dec-8cdd05214b919060.

Synthetic repositories and fakes only (never the game repository): the
AnimalUprising adapter's own checklist / pending-steps / intermediate-output
code runs on top of the lifecycle test adapter."""

from __future__ import annotations

import json
import os

import pytest

from foundry_coordinator import states as S
from foundry_coordinator.adapters.base import FAIL, PASS, VerificationResult
from foundry_coordinator.adapters.git import Workspace
from foundry_coordinator.obligations import DecisionRefused
from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
from tests.coordinator.helpers import FakeFoundry, coordinator, make_repo, run_until, \
    write_profile
from tests.coordinator.test_lifecycle_e2e import LifecycleAdapter, ToolClient, \
    lifecycle_extra, report

TID = "UI-02"
PLAN = f"docs/progress/{TID}_PLAN.md"
LOG = f"docs/progress/{TID}_EVIDENCE_RAW.log"
GATE_JSONS = [f"docs/progress/{TID}_gate_expect.json",
              f"docs/progress/{TID}_gate_expect_full.json",
              f"docs/progress/{TID}_gate_verdicts.json"]
BASELINE = f"docs/progress/{TID}_BASELINE.md"
CAPTURE = f"docs/evidence/{TID}/hud_desktop.png"
TASK = {"id": TID, "title": "UI kit rollout", "tier": "B", "plan": PLAN,
        "spec": "Roll the UI kit out to the HUD screens, following the committed plan.",
        "scope": ["src/ui02/kit.lua", "src/ui02/hud.lua", "src/ui02/legacy.lua",
                  PLAN, LOG, BASELINE, CAPTURE]
        + GATE_JSONS}

STEPS = ["1 Kit module", "2 Theme tokens", "3 Alert primitive",
         "4 c3-c15 green (c6 2-client, c7/c8 x 5 sizes), BASELINE written",
         "5 HeroSelect / TutorialHint / WaveBanner / Downed on the Kit",
         "6 regression S28 S38 S39 + ui", "7 legacy deleted, boot 20/4",
         "8 captures viewed + described",
         "9 docs, gate -Finish, done_check, pushed"]


def plan(marks: dict, notes: dict | None = None) -> str:
    """The plan with step n marked marks.get(n, ' '); notes append to [~] rows."""
    rows = []
    for i, s in enumerate(STEPS, 1):
        m = marks.get(i, " ")
        rows.append(f"- [{m}] {s}" + (f" -- {notes[i]}" if notes and i in notes else ""))
    return (f"# {TID} — UI kit rollout — PLAN OK\n\n## Checklist\n" + "\n".join(rows)
            + "\n\nNEXT: continue in plan order\n")


S1 = plan({1: "x", 2: "x", 3: "x"})
S2 = plan({1: "x", 2: "x", 3: "x", 4: "~", 5: "~", 6: "~"},
          {4: "desktop legs green; c6 two-client and c7/c8 sizes remain",
           5: "Downed + TutorialHint rebuilt; WaveBanner, HeroSelect remain",
           6: "S28 green; S38 S39 remain"})
S3 = plan({1: "x", 2: "x", 3: "x", 4: "~", 5: "~", 6: "~", 7: "x"},
          {4: "desktop legs green; c6 two-client and c7/c8 sizes remain",
           5: "Downed + TutorialHint rebuilt; WaveBanner, HeroSelect remain",
           6: "S28 green; S38 S39 remain"})
DONE = plan({i: "x" for i in range(1, 9)})


class UI02Adapter(LifecycleAdapter):
    """The lifecycle test adapter with the AU adapter's committed-plan hooks."""

    cfg: dict = {}
    GATE_INTERMEDIATE = AnimalUprisingAdapter.GATE_INTERMEDIATE
    pending_build_steps = AnimalUprisingAdapter.pending_build_steps
    plan_progress = AnimalUprisingAdapter.plan_progress
    intermediate_outputs = AnimalUprisingAdapter.intermediate_outputs

    def required_checks(self, spec, phase="BUILD"):
        return [{"check_id": c, "chain_id": "", "seq": None, "prereqs": [], "runtime": False,
                 "min_tier": "verified"} for c in ("ui02.gate", "ui02.done_check")] \
            if phase == "BUILD" else []

    def check_dependencies(self, spec, check_id):
        return ["src/**", LOG, PLAN]

    def _gate_binding(self, spec, revision, ws=None):
        log = (ws or Workspace(self.p.workspace)).show_at(revision, LOG) or ""
        ok = f"GATE {spec.task_id} PASS" in log
        return VerificationResult("ui02.gate", PASS if ok else FAIL, True, LOG,
                                  "gate PASS" if ok else f"no 'GATE {spec.task_id} PASS'",
                                  revision)

    def verify(self, spec, phase, revision, base, ws=None):
        out = super().verify(spec, phase, revision, base)
        if phase == "verify":
            w = Workspace(self.p.workspace)
            out.append(self._gate_binding(spec, revision, w))
            # the done_check analogue: every BUILD row [x] ([~] is not done)
            open_rows = self.pending_build_steps(spec, revision, w)
            out.append(VerificationResult(
                "ui02.done_check", FAIL if open_rows else PASS, True, PLAN,
                "open BUILD rows: " + "; ".join(open_rows) if open_rows else "BUILD rows done",
                revision))
        return out


def _game(tmp_path, script):
    root, _ = make_repo(tmp_path, "game", [TASK])
    body = lifecycle_extra().replace(
        'module = "tests.coordinator.test_lifecycle_e2e:LifecycleAdapter"',
        'module = "tests.coordinator.test_ui02_partial_build:UI02Adapter"')
    prof = write_profile(tmp_path, "game", root, extra=body)
    client = ToolClient(script, project_id="game")
    ff = FakeFoundry()
    c = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": ff})
    return c, client, ff, root


def _untracked(ws, files):
    for rel in files:
        p = os.path.join(ws, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"OK": rel, "n": os.urandom(4).hex()}))


def _state(c):
    return c.store.task("game", TID)["state"]


def _hist(c):
    return [(h["from_state"], h["to_state"]) for h in c.store.history("game", TID)]


def _vsets(c):
    """[{check: {result, detail}}] per verification set, oldest first."""
    sets: dict = {}
    for r in c.store.q("SELECT set_id, check_name, result, detail FROM verifications WHERE "
                       "project_id='game' AND task_id=? AND phase='verify' ORDER BY rowid",
                       (TID,)):
        sets.setdefault(r["set_id"], {})[r["check_name"]] = r
    return list(sets.values())


def _decisions(c, kind=None):
    return [d for d in c.obligations.all_decisions("game", TID)
            if kind is None or d["kind"] == kind]


def _partial_sessions(turns):
    """S1..S3: HEAD advances every session, steps go [ ] -> [~] -> [x], and the
    gate leaves its three JSON files untracked each time."""
    def script(ctx):
        turns.append(ctx)
        n = len(turns)
        ws = ctx["workspace"]
        if n == 1:
            out = {"writes": {"src/ui02/kit.lua": "-- OK kit\n", PLAN: S1},
                   "commit": f"{TID}: kit, theme, alert (steps 1-3)"}
        elif n == 2:
            out = {"writes": {"src/ui02/hud.lua": "-- OK hud v1\n", PLAN: S2,
                              LOG: "OK\nPASS S28 c1\n"},
                   "commit": f"{TID}: steps 4-6 in progress"}
        elif n == 3:
            out = {"writes": {"src/ui02/legacy.lua": "-- OK legacy removed\n", PLAN: S3},
                   "commit": f"{TID}: step 7 legacy deleted"}
        else:
            return None
        _untracked(ws, GATE_JSONS)
        out["final"] = report(status="NOT DONE", summary="BUILD continues per plan")
        return out
    return script


# -- the regression ----------------------------------------------------------------------

def test_partial_steps_and_gate_outputs_continue_build_then_final_verification_is_strict(
        tmp_path):
    turns: list = []
    partial = _partial_sessions(turns)

    def script(ctx):
        n = len(turns) + 1
        if n <= 3:
            return partial(ctx)
        turns.append(ctx)
        ws = ctx["workspace"]
        if n == 4:          # 4-8 ticked; the final BUILD gate has not run yet
            _untracked(ws, GATE_JSONS)
            return {"writes": {"src/ui02/hud.lua": "-- OK hud final\n", PLAN: DONE},
                    "commit": f"{TID}: steps 4-8 complete", "final": report()}
        if n == 5:          # the gate PASSes, but its JSON outputs are left untracked
            _untracked(ws, GATE_JSONS)
            return {"writes": {LOG: "OK\nPASS: GATE HEAD x\nGATE UI-02 PASS\n"},
                    "commit": f"{TID}: final gate", "final": report()}
        # the repair: commit the gate outputs (final verification requires them)
        return {"run": [["git", "add", "--", *GATE_JSONS]],
                "commit": f"{TID}: commit gate outputs", "final": report()}

    c, client, ff, root = _game(tmp_path, script)
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: len(turns) >= 3 and _state(c) == S.CHANGES_REQUIRED,
                     limit=60), (_state(c), _hist(c))
    hist = c.store.history("game", TID)
    progress = [h for h in hist if h["evidence"].get("build_progress")]
    # every partial session was a BUILD continuation: never final verification
    assert len(progress) == 3, _hist(c)
    assert not [h for h in hist if h["to_state"] == S.VERIFYING], _hist(c)
    for h in progress:
        # control-center keys: "N open of total_steps" (CLOSE row excluded)
        assert h["evidence"]["build_progress"] is True and h["evidence"]["head_sha"]
        assert h["evidence"]["total_steps"] == 8 and h["evidence"]["open_steps"]
        assert "intermediate gate output (task-owned, untracked)" in \
            h["evidence"]["intermediate_outputs"]
        assert all(g in h["evidence"]["intermediate_outputs"] for g in GATE_JSONS)
    # [~] rows are open BUILD steps, [x] rows are not
    last = progress[-1]["evidence"]["open_steps"]
    assert [s[:7] for s in last[:4]] == ["- [~] 4", "- [~] 5", "- [~] 6", "- [ ] 8"]
    assert "Run and commit the final BUILD gate at this revision" in last
    assert not any("7 legacy" in s for s in last)
    # no attempt spent on the continuations; no decision of any kind
    t = c.store.task("game", TID)
    assert t["attempt"] == 1
    limits = c.obligations.effective_limits(c.projects["game"], t)
    assert limits["ordinary"]["used"] == 1 and limits["sessions"]["used"] == 3
    assert _decisions(c) == []
    # the continuation prompt names the intermediate outputs instead of a
    # "left uncommitted -- commit them" repair finding
    assert "Intermediate gate outputs" in turns[1]["prompt"]
    assert "left these task files uncommitted" not in turns[1]["prompt"]
    # the engine's own marks: committed BUILD progress every session
    pr = c._lc_progress(c.projects["game"], t)
    assert pr["sessions"] == 3 and pr["stalls"] == 0 and not pr["no_progress"]
    assert pr["last_session_build"]["committed"] and pr["last_session_build"]["work_files"]

    # -- complete steps 4-8: the final gate, then final verification is strict
    assert run_until(c, lambda: _state(c) == S.COMPLETE, limit=80), (_state(c), _hist(c))
    hist = c.store.history("game", TID)
    assert len([h for h in hist if h["evidence"].get("build_progress")]) == 4   # gate open
    vers = _vsets(c)
    assert len(vers) == 2, vers
    first, final = vers
    # untracked gate JSONs fail final verification's clean check, labelled as such
    assert first["coordinator.clean"]["result"] == FAIL
    assert "intermediate gate output (task-owned, untracked)" in \
        first["coordinator.clean"]["detail"]
    assert "coordinator.intermediate_outputs" not in first
    # committed, the final set passes: clean, gate, done-check, project check
    for name in ("coordinator.clean", "coordinator.scope", "ui02.gate", "ui02.done_check",
                 "project-check"):
        assert final[name]["result"] == PASS, (name, final[name])
    # evidence: the gate and done-check obligations were verified from the final set
    obs = {o["obligation_id"]: o["status"] for o in c.obligations.all("game", TID)}
    assert obs["check:ui02.gate"] == "verified" and obs["check:ui02.done_check"] == "verified"
    assert c.store.reviews("game", TID) and ff.chats          # the independent review ran
    assert _decisions(c) == []


def test_true_stall_still_opens_a_no_progress_decision(tmp_path):
    """No commits, no checklist change, the same findings: escalates as before."""
    turns: list = []
    partial = _partial_sessions(turns)

    def script(ctx):
        if not turns:
            return partial(ctx)
        turns.append(ctx)
        return {"final": report(status="NOT DONE", summary="looked at the plan again")}

    c, client, ff, root = _game(tmp_path, script)
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: _state(c) == S.BLOCKED, limit=60), (_state(c), _hist(c))
    t = c.store.task("game", TID)
    np_ = _decisions(c, "no_progress")
    assert len(np_) == 1 and np_[0]["status"] == "open", (_decisions(c), _hist(c))
    assert t["data"]["decision_wait"]["ids"] == [np_[0]["decision_id"]]
    pr = np_[0]["evidence"]
    assert pr["no_progress"] and pr["stalls"] >= 2
    assert len(turns) == 3                       # 1 partial session + 2 stalled ones
    # re-evaluating a true stall changes nothing
    out = c.decision_reevaluate(np_[0]["decision_id"], "victor", "check after deploy")
    assert out["withdrawn"] is False
    assert c.obligations.decision(np_[0]["decision_id"])["status"] == "open"


def test_partial_build_carries_own_baseline_and_capture_without_waiving_final_clean(tmp_path):
    """The live v0.122.6 block: committed code plus an unfinished, in-scope
    baseline and capture must continue BUILD, with both named in the next prompt.
    Final verification still rejects either file while uncommitted."""
    turns = []

    def script(ctx):
        turns.append(ctx)
        if len(turns) > 1:
            return None
        ws = ctx["workspace"]
        _untracked(ws, [BASELINE, CAPTURE])
        return {"writes": {"src/ui02/kit.lua": "-- committed repair\n", PLAN: S1},
                "commit": f"{TID}: c5b repair", "final": report(status="NOT DONE")}

    c, client, ff, root = _game(tmp_path, script)
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: _state(c) == S.CHANGES_REQUIRED, limit=25), _hist(c)
    assert (S.IMPLEMENTING, S.VERIFYING) not in _hist(c)
    hist = c.store.history("game", TID)
    assert hist[-1]["evidence"].get("build_progress") is True
    assert _decisions(c) == []
    t = c.store.task("game", TID)
    spec = c.projects["game"].adapter.describe(TID)
    final = {r.check: r for r in c._generic_checks(c.projects["game"], t, spec,
                                                    Workspace(root).head(), phase="verify")}
    assert final["coordinator.clean"].result == FAIL
    assert BASELINE in final["coordinator.clean"].detail
    assert CAPTURE in final["coordinator.clean"].detail
    assert run_until(c, lambda: len(turns) == 2, limit=12), _hist(c)
    assert BASELINE in turns[1]["prompt"] and CAPTURE in turns[1]["prompt"]


def test_reevaluate_premature_recovery_escalation_only_for_valid_build_continuation(tmp_path):
    """An already-blocked task can withdraw the stale repair decision when
    the committed plan is still in BUILD and only task-owned files are dirty."""
    turns = []

    def script(ctx):
        turns.append(ctx)
        if len(turns) > 1:
            return None
        _untracked(ctx["workspace"], [BASELINE, CAPTURE])
        return {"writes": {"src/ui02/kit.lua": "-- committed repair\n", PLAN: S1},
                "commit": f"{TID}: c5b repair", "final": report(status="NOT DONE")}

    c, client, ff, root = _game(tmp_path, script)
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: _state(c) == S.CHANGES_REQUIRED, limit=25), _hist(c)
    P = c.projects["game"]
    t = c.store.task("game", TID)
    did = c.obligations.open_decision("game", TID, "recovery_escalation",
                                      "premature final checks repeated",
                                      evidence={"repeats": []}, blocks=[TID])
    c._lc_wait(P, t, [did], "findings repeat", S.READY)
    dry = c.decision_reevaluate(did, "operator", "partial BUILD", dry_run=True)
    assert dry["withdrawn"] and dry["continuation"]["open_steps"]
    assert c.obligations.decision(did)["status"] == "open"
    out = c.decision_reevaluate(did, "operator", "partial BUILD")
    assert out["withdrawn"] and c.obligations.decision(did)["status"] == "withdrawn"
    after = c.store.task("game", TID)
    assert after["attempt"] == t["attempt"] and after["sessions"] == t["sessions"]
    assert after["data"]["free_retry"] is True


def test_unrelated_untracked_files_still_block_the_continuation(tmp_path):
    """Task-owned unfinished work can continue BUILD, but another sprint's gate
    JSON and a near-miss name still stop it (normal verification reports them)."""
    turns: list = []

    def script(ctx):
        turns.append(ctx)
        if len(turns) > 1:
            return None
        ws = ctx["workspace"]
        _untracked(ws, GATE_JSONS + ["src/ui02/hud.lua", "docs/progress/UI-01_gate_verdicts.json",
                                     f"docs/progress/{TID}_gate_expect.json.bak"])
        return {"writes": {"src/ui02/kit.lua": "-- OK kit\n", PLAN: S2},
                "commit": f"{TID}: partial", "final": report(status="NOT DONE")}

    c, client, ff, root = _game(tmp_path, script)
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: _state(c) not in (S.QUEUED, S.READY, S.IMPLEMENTING,
                                                  S.VERIFYING), limit=40), _hist(c)
    hist = c.store.history("game", TID)
    assert not [h for h in hist if h["evidence"].get("build_progress")], _hist(c)
    assert (S.IMPLEMENTING, S.VERIFYING) in _hist(c)
    res = _vsets(c)[-1]
    assert res["coordinator.clean"]["result"] == FAIL
    assert "src/ui02/hud.lua" in res["coordinator.clean"]["detail"]
    assert res["coordinator.unrelated_preserved"]["result"] == FAIL
    assert "docs/progress/UI-01_gate_verdicts.json" in \
        res["coordinator.unrelated_preserved"]["detail"]
    assert f"docs/progress/{TID}_gate_expect.json.bak" in (
        res["coordinator.clean"]["detail"] + res["coordinator.unrelated_preserved"]["detail"])


def test_reevaluate_withdraws_a_false_no_progress_decision_and_keeps_the_audit(tmp_path):
    """The deployed fix re-checks an existing decision: progress is recognized
    from the engine's marks (work files + [~] steps), the decision is withdrawn
    (kept, with an event naming the actor and reason), the task's stale
    final-verification findings become the BUILD continuation, and no attempt,
    session or ledger counter moves."""
    turns: list = []
    partial = _partial_sessions(turns)
    c, client, ff, root = _game(tmp_path, partial)
    c.approve("game", TID, [], "victor")
    assert run_until(c, lambda: len(turns) >= 3 and _state(c) == S.CHANGES_REQUIRED,
                     limit=60), _hist(c)
    P = c.projects["game"]
    # reproduce the live block: stale final-verification findings and an open
    # no_progress decision the old rule opened
    stale = ["check coordinator.clean: FAIL — uncommitted changes in task files: "
             + ", ".join(GATE_JSONS), "check au.done_check.GATE: FAIL — no 'GATE UI-02 "
             "PASS|FAIL' line in the evidence log"]
    c.store.merge_data("game", TID, findings=stale, free_retry=None, build_progress=None)
    did = c.obligations.open_decision(
        "game", TID, "no_progress", "4 sessions in a row made no progress (no newly verified "
        "obligation, same findings, 0 summary-only); another broad retry would repeat them",
        evidence={"no_progress": True, "stalls": 4},
        options=["answer with a different strategy {strategy: ...}", "authorize more budget",
                 "stop the task"], blocks=[TID])
    c._lc_wait(P, c.store.task("game", TID), [did], "no progress across sessions", S.READY)
    before = c.store.task("game", TID)
    ledger = c.store.q("SELECT * FROM budget_ledger WHERE project_id='game' AND task_id=?",
                       (TID,))
    dry = c.decision_reevaluate(did, "victor", "re-check", dry_run=True)
    assert dry["withdrawn"] and c.obligations.decision(did)["status"] == "open"
    out = c.decision_reevaluate(did, "victor", "reevaluate: false no-progress (checklist [~] "
                                "+ intermediate gate outputs), fixed in v0.122.5")
    assert out["withdrawn"] and not out["progress"]["no_progress"]
    assert out["continuation"]["replaced_findings"] == stale
    d = c.obligations.decision(did)
    assert d["status"] == "withdrawn" and d["question"].startswith("4 sessions")  # kept
    ev = [e for e in c.store.events("game", 50) if e["kind"] == "decision_reevaluated"]
    assert ev and ev[0]["detail"]["by"] == "victor" and ev[0]["detail"]["result"] == \
        "withdrawn" and "fixed in v0.122.5" in ev[0]["detail"]["reason"]
    after = c.store.task("game", TID)
    for k in ("attempt", "sessions", "state"):
        assert after[k] == before[k]
    assert after["data"].get("findings") is None and after["data"]["free_retry"] is True
    assert c.store.q("SELECT * FROM budget_ledger WHERE project_id='game' AND task_id=?",
                     (TID,)) == ledger
    # a second re-evaluation is refused (nothing open), the decision is never deleted
    with pytest.raises(DecisionRefused):
        c.decision_reevaluate(did, "victor", "again")
    with pytest.raises(DecisionRefused):
        c.decision_reevaluate(did, "Foundry-Kilo-Opus", "a model may not")
    # the engine resumes the waiting task as a BUILD continuation on its next tick
    c.tick()
    assert _state(c) in (S.READY, S.IMPLEMENTING), _hist(c)
    resumed = next(h for h in c.store.history("game", TID)
                   if h["from_state"] == S.BLOCKED and h["to_state"] == S.READY)
    ev = resumed["evidence"]
    assert ev["build_progress"] is True and ev["total_steps"] == 8
    assert ev["head_sha"] == before["head_sha"] and ev["open_steps"][:3] == \
        out["continuation"]["open_steps"][:3]
    assert "withdrawn" in ev["resolution"]
    assert "build_resume" not in c.store.task("game", TID)["data"]
