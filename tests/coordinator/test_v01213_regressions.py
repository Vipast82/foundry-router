"""Regression tests for the v0.121.3 fixes: helper cap vs lane units, lane
work of a failed run, per-run delivery, prompt-failure journal rollback,
frontier start rollback, lane planning (run scoping, coalescing, pause,
per-project retention), the filesystem-fallback artifact path, vision frame
validity, artifact store scoping/safety/A-B-A lineage, and unblock resets.
Fake Kilo + fake Foundry + simulated clock; no model calls."""

import copy
import json
import os
from types import SimpleNamespace

import pytest

from foundry_coordinator import lanes as L
from foundry_coordinator.artifacts import ArtifactStore
from foundry_coordinator.proactive import Proactive
from foundry_coordinator.store import Store
from tests.coordinator.lane_env import CLAUDE, VISION, admit, answer_helper, capture_output, \
    finish_primary, helper_sessions, png, primary_tool, setup

ONLY = """
[lanes]
enabled = true
transcript_poll_seconds = 0
{body}
"""
TRIAGE = ONLY.format(body="[lanes.failure_triage]\nenabled = true\n")
VIS = ONLY.format(body="""
[lanes.visual]
enabled = true
debounce_seconds = 0
capture_globs = ["docs/evidence/{task}/*.png"]
reference_globs = ["docs/design/{task}_ref.png"]

[lanes.proposer]
enabled = true
""")
VISFS = VIS.replace("debounce_seconds = 0", "debounce_seconds = 0\nfs_fallback = true")
FP = """
[frontier_primary]
enabled = true
persona = "Foundry-Kilo-Frontier"
max_turns = 2
cooldown_minutes = 60
"""
FRONTIER = "Foundry-Kilo-Frontier"
VROUTES = [{"backend": "meridian", "model": "claude-sonnet", "vision": True}]
HIGH = [{"id": "V1", "severity": "high", "confidence": 0.9, "region": "bottom right",
         "observed": "Play button clipped", "hypothesis": "inset ignored",
         "paths": ["src/menu.luau:1"], "in_scope": True}]


def lane_units(c, lane=None):
    return [u for u in c.helpers.units("game", "T2") if u.get("lane") and
            (lane is None or u["lane"] == lane)]


def unit(c, uid):
    return [u for u in c.helpers.units("game", "T2") if u["unit_id"] == uid][0]


def primary_prompts(env, sid=None):
    return [p for p in env["fake"].prompts if p["session_id"] == (sid or env["primary"])]


def fail(env, text="FAIL S45 ordering at src/menu.luau:1 (1.1s)"):
    return primary_tool(env, "lune run tests/lune/run.luau", "LUNE 41/42 FAIL\n" + text,
                        exit_code=1)


def answer_high(env, u):
    answer_helper(env, u["session_id"], {
        "assignment": u["unit_id"], "base": u["snapshot_id"], "summary": "found it",
        "findings": [{"id": "F1", "severity": "high", "confidence": 0.9,
                      "observed": "FAIL S45 ordering", "hypothesis": "order table missing",
                      "paths": ["src/menu.luau:1"]}]})


def ctx(env):
    c = env["c"]
    P = c.projects["game"]
    return c, P, c.store.task("game", "T2"), L.settings(P)


def fail_prompts_for(monkeypatch, env, flag):
    """Kilo refuses prompts to the primary session while flag[0] is True."""
    P = env["c"].projects["game"]
    real = P.client.prompt

    def prompt(sid, *a, **k):
        if flag[0] and sid == env["primary"]:
            raise RuntimeError("kilo refused the prompt")
        return real(sid, *a, **k)
    monkeypatch.setattr(P.client, "prompt", prompt)


@pytest.fixture()
def triage(tmp_path):
    e = setup(tmp_path, lanes=TRIAGE)
    yield e
    e["fake"].stop()


# ------------------------------------------------------------------ 1. helper cap

def test_lane_units_never_use_up_the_ordinary_helper_cap(triage):
    c, P, t, ls = ctx(triage)
    for i in range(3):
        assert c.helpers.lanes.add(P, t, "failure_triage", f"k{i}", {}, ls)[1] == "created"
    h = {**c.helpers.settings(P), "max_units_per_task": 2}
    a = c.helpers._add("game", "T2", t["run_id"], {"responsibility": "ord one"}, "report", h)
    b = c.helpers._add("game", "T2", t["run_id"], {"responsibility": "ord two"}, "report", h)
    assert a and b                                   # 3 lane units did not count
    assert c.helpers._add("game", "T2", t["run_id"], {"responsibility": "ord three"}, "report",
                          h) is None
    lim = [e for e in c.store.events("game", 200) if e["kind"] == "helper_limit"]
    assert len(lim) == 1 and lim[0]["detail"]["responsibility"] == "ord three"
    assert "max_units_per_task (2)" in lim[0]["detail"]["reason"]
    # lane planning is unaffected by the ordinary cap being reached
    assert c.helpers.lanes.add(P, t, "failure_triage", "k9", {}, ls)[1] == "created"


# ------------------------------------------------------------------ 2/3. failed run

def test_abort_lane_running_stops_lane_units_only(triage):
    env = triage
    c, P, t, ls = ctx(env)
    env["ff"].admission[CLAUDE] = admit()
    fail(env)
    c.tick()
    [lu] = lane_units(c, "failure_triage")
    assert lu["status"] == "running"
    h = c.helpers.settings(P)
    oid = c.helpers._add("game", "T2", t["run_id"], {"responsibility": "ordinary job"}, "task", h)
    c.store.x("UPDATE helper_units SET status='running', session_id='ses_ordinary' "
              "WHERE unit_id=?", (oid,))
    out = c.helpers.abort_lane_running(P, "T2", "the session ended: boom")
    assert out == [lu["unit_id"]]
    assert unit(c, lu["unit_id"])["status"] == "cancelled"
    assert lu["session_id"] in env["fake"].aborts
    assert unit(c, oid)["status"] == "running" and "ses_ordinary" not in env["fake"].aborts


def test_failed_session_cancels_lane_work_and_its_results_never_reach_the_next_session(
        triage, monkeypatch):
    env = triage
    c, ff, fake = env["c"], env["ff"], env["fake"]
    seen = []
    real = c.helpers.proposals_for

    def spy(pid, tid, session_id="", run_id=""):
        seen.append((session_id, run_id))
        return real(pid, tid, session_id, run_id=run_id)
    monkeypatch.setattr(c.helpers, "proposals_for", spy)
    ff.admission[CLAUDE] = admit()
    fail(env)
    c.tick()
    fail(env, "FAIL gallery crashed on open")         # a second, different failure
    c.tick()
    first, second = lane_units(c, "failure_triage")
    assert first["status"] == second["status"] == "running"
    answer_high(env, second)
    c.tick()
    assert unit(c, second["unit_id"])["status"] == "done"
    assert unit(c, second["unit_id"])["feedback"] == "waiting"
    old_run = c.store.task("game", "T2")["run_id"]
    with fake.lock:
        del fake.sessions[env["primary"]]             # the primary session is lost
    c.tick()
    u1, u2 = unit(c, first["unit_id"]), unit(c, second["unit_id"])
    assert u1["status"] == "cancelled" and "the session ended" in u1["reason"]
    assert first["session_id"] in fake.aborts
    assert u2["feedback"] == "expired"
    assert json.loads(u2["feedback_detail"])["note"] == "the run ended before delivery"
    for _ in range(4):
        env["clock"][0] += 120
        c.tick()
    t = c.store.task("game", "T2")
    assert t["session_id"] != env["primary"] and t["run_id"] != old_run
    new_prompts = primary_prompts(env, t["session_id"])
    assert new_prompts and all(second["unit_id"] not in p["text"] for p in new_prompts)
    assert unit(c, second["unit_id"])["status"] == "done"          # never delivered
    # the new session's proposals were judged against the NEW run
    assert (t["session_id"], t["run_id"]) in seen


def test_proposals_for_judges_lane_results_against_the_given_run(triage):
    env = triage
    c, ff = env["c"], env["ff"]
    ff.admission[CLAUDE] = admit()
    fail(env)
    c.tick()
    [u] = lane_units(c, "failure_triage")
    answer_high(env, u)
    c.tick()
    run = c.store.task("game", "T2")["run_id"]
    assert [x["unit_id"] for x in c.helpers.proposals_for("game", "T2", "ses_new", run_id=run)] \
        == [u["unit_id"]]
    # v0.126: a result is carried into another run only while its code is
    # unchanged; once the snapshot's files changed it expires as before
    (env["root"] / "src" / "menu.luau").write_text("local M = {changed = true}\nreturn M\n")
    assert c.helpers.proposals_for("game", "T2", "ses_new", run_id="run-next") == []
    u = unit(c, u["unit_id"])
    assert u["feedback"] == "expired" and "earlier run" in json.loads(u["feedback_detail"])["note"]


# ------------------------------------------------------------------ 4. prompt failure

def test_prompt_primary_failure_forgets_the_journal_entry(triage, monkeypatch):
    env = triage
    c, P, t, _ = ctx(env)
    okey = f"deliver:game:T2:{t['session_id']}:k1"

    def bad_bind(*a, **k):
        raise RuntimeError("binding refused")
    monkeypatch.setattr(c, "_bind", bad_bind)
    with pytest.raises(RuntimeError, match="binding refused"):
        c._prompt_primary(P, t, "hello primary", "k1")
    assert c.store.op(okey) is None
    flag = [True]
    monkeypatch.undo()
    fail_prompts_for(monkeypatch, env, flag)
    with pytest.raises(RuntimeError, match="kilo refused"):
        c._prompt_primary(P, t, "hello primary", "k1")
    assert c.store.op(okey) is None
    flag[0] = False
    n = len(primary_prompts(env))
    assert c._prompt_primary(P, t, "hello primary", "k1") is True     # not "uncertain"
    assert len(primary_prompts(env)) == n + 1 and c.store.op(okey)["status"] == "done"
    assert not [e for e in c.store.events("game", 200)
                if e["kind"] == "helper_delivery_uncertain"]


def test_failed_delivery_prompt_is_retried_at_a_later_safe_point(triage, monkeypatch):
    env = triage
    c, ff = env["c"], env["ff"]
    ff.admission[CLAUDE] = admit()
    fail(env)
    c.tick()
    [u] = lane_units(c, "failure_triage")
    answer_high(env, u)
    c.tick()
    flag = [True]
    fail_prompts_for(monkeypatch, env, flag)
    finish_primary(env, {"status": "NOT DONE", "summary": "first pass"})
    c.tick()
    x = unit(c, u["unit_id"])
    assert x["status"] == "done" and not x["delivered_session"]
    assert not c.store.q("SELECT key FROM ops WHERE key LIKE ?", (f"deliver:%{u['unit_id']}%",))
    assert len(primary_prompts(env)) == 1
    flag[0] = False
    c.tick()
    pp = primary_prompts(env)
    assert len(pp) == 2 and u["unit_id"] in pp[-1]["text"]
    assert unit(c, u["unit_id"])["status"] == "delivered"


# ------------------------------------------------------------------ 5. frontier start

def test_frontier_start_prompt_failure_reverts_phase_to_requested(tmp_path, monkeypatch):
    env = setup(tmp_path, lanes=FP)
    try:
        c = env["c"]
        flag = [True]
        fail_prompts_for(monkeypatch, env, flag)
        c.frontier.request("game", "T2", "victor", "hard bug")
        finish_primary(env, {"status": "NOT DONE", "summary": "stuck"})
        c.tick()
        ph = c.frontier.phase("game", "T2")
        assert ph["state"] == "requested" and ph["started_ts"] is None
        assert not c.store.q("SELECT key FROM ops WHERE key LIKE ?",
                             (f"%frontier:{ph['phase_id']}:start",))
        flag[0] = False
        c.tick()
        ph = c.frontier.phase("game", "T2")
        assert ph["state"] == "active" and ph["started_ts"]
        assert primary_prompts(env)[-1]["model"] == FRONTIER
    finally:
        env["fake"].stop()


# ------------------------------------------------------------------ 6. lane planning

def test_events_from_an_earlier_run_are_expired_not_planned(triage):
    env = triage
    c, P, t, _ = ctx(env)
    c.helpers.lanes._event(P, {**t, "run_id": "run-old"}, {
        "kind": "failure", "key": "old-failure", "signature": "sig-old",
        "detail": {"command": "lune", "exit": 1}}, env["primary"])
    c.tick()
    [ev] = [e for e in c.helpers.lanes.events("game", "T2", "failure")]
    assert ev["status"] == "expired"
    assert lane_units(c) == []


def test_same_failure_after_a_code_change_is_triaged_again(triage):
    env = triage
    c, ff = env["c"], env["ff"]
    ff.admission[CLAUDE] = admit("defer", "Session 82% used")
    fail(env)
    c.tick()
    fail(env, "FAIL S45 ordering at src/menu.luau:2 (2.1s)")      # unchanged code: coalesced
    c.tick()
    [u] = lane_units(c, "failure_triage")
    assert u["coalesced"] == 1
    (env["root"] / "src/menu.luau").write_text("local M = {order = {}}\nreturn M\n")
    fail(env, "FAIL S45 ordering at src/menu.luau:3 (3.1s)")
    c.tick()
    units = lane_units(c, "failure_triage")
    assert len(units) == 2
    a, b = (x["responsibility"] for x in units)
    assert a != b and a.rsplit(":", 1)[0] == b.rsplit(":", 1)[0]  # same signature, new content
    fail(env, "FAIL S45 ordering at src/menu.luau:4 (4.1s)")      # repeat on the new code
    c.tick()
    assert len(lane_units(c, "failure_triage")) == 2


def test_operator_pause_holds_queued_lane_work_instead_of_dropping_it(triage):
    env = triage
    c, ff = env["c"], env["ff"]
    ff.admission[CLAUDE] = admit("defer", "Session 82% used")
    fail(env)
    c.tick()
    [u] = lane_units(c, "failure_triage")
    assert u["status"] == "deferred"
    c.helpers.lanes.set_paused("game", True, "victor", "check")
    _, P, t, ls = ctx(env)
    assert c.helpers.lanes.hold_reason(P, t, u, ls, []).startswith("wait:")
    ff.admission[CLAUDE] = admit()
    for _ in range(3):
        env["clock"][0] += 120
        c.tick()
    u = unit(c, u["unit_id"])
    assert u["status"] == "deferred" and not helper_sessions(env)
    c.helpers.lanes.set_paused("game", False, "victor")
    env["clock"][0] += 120
    c.tick()
    assert unit(c, u["unit_id"])["status"] == "running"


def test_lane_turned_off_in_profile_is_still_skipped(triage):
    env = triage
    c, P, t, ls = ctx(env)
    uid, _ = c.helpers.lanes.add(P, t, "failure_triage", "k", {}, ls)
    u = unit(c, uid)
    off = copy.deepcopy(ls)
    off["lanes"]["failure_triage"]["enabled"] = False
    assert c.helpers.lanes.hold_reason(P, t, u, off, []).startswith("skip:")
    c.helpers.lanes.set_paused("game", True, "victor")
    assert c.helpers.lanes.hold_reason(P, t, u, off, []).startswith("skip:")


def test_retention_prune_uses_each_projects_own_retention_days(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"))
    clock = [1_000_000.0]
    arts = ArtifactStore(store, str(tmp_path / "state"), clock=lambda: clock[0])
    ws = tmp_path / "ws"
    (ws / "docs/evidence/T2").mkdir(parents=True)
    rows = {}
    for pid, rgb in (("short", (1, 1, 1)), ("long", (2, 2, 2))):
        (ws / "docs/evidence/T2" / f"{pid}.png").write_bytes(png(4, 3, rgb))
        r = arts.record(project_id=pid, task_id="T2", run_id="r", session_id="s",
                        workspace=str(ws), path=f"docs/evidence/T2/{pid}.png",
                        allowed=["docs/evidence/{task}/*.png"])
        rows[pid] = r["artifact"]["artifact_id"]

    def proj(days):
        return SimpleNamespace(profile=SimpleNamespace(raw={"lanes": {
            "enabled": True, "visual": {"enabled": True, "retention_days": days}}}))
    projects = {"short": proj(5), "long": proj(30)}
    lane = SimpleNamespace(store=store, clock=lambda: clock[0], artifacts=arts,
                           mgr=SimpleNamespace(c=SimpleNamespace(projects=projects)))
    clock[0] += 10 * 86400
    Proactive._retention(lane, L.settings(projects["long"]))
    assert arts.get(rows["short"])["status"] == "expired"
    assert arts.get(rows["long"])["status"] == "current"
    assert not [e for e in store.events(None, 50) if e["kind"] == "warning"]


# ------------------------------------------------------------------ 7. visual

def test_filesystem_fallback_capture_becomes_a_visual_unit(tmp_path):
    env = setup(tmp_path, lanes=VISFS)
    try:
        c = env["c"]
        env["ff"].admission[VISION] = admit("defer", "Session 71% used", routes=VROUTES)
        env["fake"].set_status(env["primary"], "busy")
        (env["root"] / "docs/evidence/T2/menu.png").write_bytes(png(6, 4, (9, 9, 9)))
        for _ in range(4):
            env["clock"][0] += 5
            c.tick()
            c.helpers.lanes.wait_direct()
        [a] = c.helpers.lanes.artifacts.list("game", "T2", "capture")
        assert a["source"] == "filesystem"
        [ev] = c.helpers.lanes.events("game", "T2", "artifact")
        assert ev["status"] == "planned"
        [u] = lane_units(c, "visual")
        assert u["artifact_id"] == a["artifact_id"]
    finally:
        env["fake"].stop()


@pytest.mark.parametrize("validity,subject,why", [("occluded", True, "occluded"),
                                                  ("ok", False, "subject missing")])
def test_unusable_frame_clears_findings_and_says_so(tmp_path, validity, subject, why):
    env = setup(tmp_path, lanes=VIS)
    try:
        c, ff = env["c"], env["ff"]
        ff.admission[VISION] = admit(routes=VROUTES)
        ff.admission[CLAUDE] = admit()
        ff.vision_answer = lambda messages: ("```helper-result\n" + json.dumps({
            "image_seen": True, "subject_present": subject, "validity": validity,
            "summary": "menu clipped", "findings": HIGH}) + "\n```",
            {"served_model": "claude-sonnet"})
        data = png(6, 4)
        f = env["root"] / "docs/evidence/T2/menu.png"
        f.write_bytes(data)
        primary_tool(env, "pwsh tools/capture-window.ps1 -Out docs/evidence/T2/menu.png",
                     capture_output(str(f), data, 6, 4))
        for _ in range(2):
            c.tick()
            c.helpers.lanes.wait_direct()
        [u] = lane_units(c, "visual")
        assert u["status"] == "done"
        e = json.loads(u["envelope"])
        assert e["findings"] == [] and e["frame_problem"] == why
        assert "Frame not usable" in e["limitations"]
        assert lane_units(c, "proposer") == []
    finally:
        env["fake"].stop()


# ------------------------------------------------------------------ 8. artifact store

def _arts(tmp_path):
    ws = tmp_path / "ws"
    (ws / "docs" / "evidence" / "T2").mkdir(parents=True)
    return ws, ArtifactStore(Store(str(tmp_path / "s.sqlite")), str(tmp_path / "state"))


def _rec(a, ws, rel, pid="game"):
    return a.record(project_id=pid, task_id="T2", run_id="r1", session_id="s1",
                    workspace=str(ws), path=rel, allowed=["docs/evidence/{task}/*.png"])


def test_reverting_to_earlier_bytes_makes_them_the_newest_generation(tmp_path):
    ws, a = _arts(tmp_path)
    f, rel = ws / "docs/evidence/T2/menu.png", "docs/evidence/T2/menu.png"
    f.write_bytes(png(4, 3, (1, 1, 1)))
    r1 = _rec(a, ws, rel)
    f.write_bytes(png(4, 3, (2, 2, 2)))
    r2 = _rec(a, ws, rel)
    a_id, b_id = r1["artifact"]["artifact_id"], r2["artifact"]["artifact_id"]
    f.write_bytes(png(4, 3, (1, 1, 1)))                           # back to A
    r3 = _rec(a, ws, rel)
    assert r3["status"] == "recorded" and r3["artifact"]["artifact_id"] == a_id
    assert r3["superseded"] == [b_id]
    cur = a.get(a_id)
    assert cur["status"] == "current" and cur["generation"] == 3
    assert cur["prev_artifact"] == b_id
    assert a.get(b_id)["status"] == "superseded" and a.get(b_id)["superseded_by"] == a_id
    assert [x["artifact_id"] for x in a.list("game", "T2", "capture")
            if x["status"] == "current"] == [a_id]
    assert _rec(a, ws, rel)["status"] == "duplicate"              # unchanged repeat


def test_prune_is_scoped_to_one_project(tmp_path):
    ws, a = _arts(tmp_path)
    clock = [1000.0]
    a.clock = lambda: clock[0]
    (ws / "docs/evidence/T2/a.png").write_bytes(png(4, 3, (1, 2, 3)))
    (ws / "docs/evidence/T2/b.png").write_bytes(png(4, 3, (3, 2, 1)))
    ra = _rec(a, ws, "docs/evidence/T2/a.png", pid="pa")["artifact"]
    rb = _rec(a, ws, "docs/evidence/T2/b.png", pid="pb")["artifact"]
    clock[0] += 31 * 86400
    assert a.prune(30, project_id="pa") == 1
    assert a.get(ra["artifact_id"])["status"] == "expired" and not os.path.exists(ra["store_path"])
    assert a.get(rb["artifact_id"])["status"] != "expired" and os.path.exists(rb["store_path"])
    assert a.prune(30) == 1 and a.get(rb["artifact_id"])["status"] == "expired"


def test_drift_never_opens_paths_outside_the_workspace(tmp_path, monkeypatch):
    ws, a = _arts(tmp_path)
    (ws / "src").mkdir()
    (ws / "src/a.lua").write_text("x = 1\n")
    (tmp_path / "x_outside.lua").write_text("secret\n")
    absolute = str(tmp_path / "x_outside.lua")
    opened = []
    real = os.path.isfile
    monkeypatch.setattr(os.path, "isfile", lambda p: opened.append(str(p)) or real(p))
    out = a.drift("no-such-snapshot", str(ws), ["../x_outside.lua", absolute, "src/a.lua"],
                  "game", "T2")
    assert out["../x_outside.lua"] == "outside the workspace"
    assert out[absolute] == "outside the workspace"
    assert out["src/a.lua"] != "outside the workspace"
    assert not [p for p in opened if "x_outside" in p]


# ------------------------------------------------------------------ 9. unblock

@pytest.mark.parametrize("reset,status", [(True, "reset"), (False, "repair")])
def test_unblock_reset_ends_the_guided_attempt_but_keeps_the_adviser_excluded(
        tmp_path, reset, status):
    from foundry_coordinator.retry_guidance import RetryGuidance
    env = setup(tmp_path, lanes="")
    try:
        c = env["c"]
        c.store.transition("game", "T2", "BLOCKED", {"blocker": "stuck"})
        rg = {"status": "repair", "extra_attempts": 1, "persona": "Foundry-Frontier-Adviser",
              "summary": "do x", "steps": ["x"]}
        c.store.merge_data("game", "T2", retry_guidance=rg, question={"text": "q"})
        assert RetryGuidance.allowance(c.store.task("game", "T2")) == 1
        c.unblock("game", "T2", "fixed it", reset_attempts=reset)
        t = c.store.task("game", "T2")
        assert t["state"] == "READY" and "question" not in t["data"]
        assert t["data"]["retry_guidance"] == {**rg, "status": status}   # kept, not dropped
        assert RetryGuidance.allowance(t) == (0 if reset else 1)
        assert RetryGuidance.authors(t) == {"Foundry-Frontier-Adviser"}
        if reset:
            assert t["attempt"] == 0 and t["sessions"] == 0
    finally:
        env["fake"].stop()


def test_unblock_reset_leaves_non_repair_guidance_unchanged(tmp_path):
    from foundry_coordinator.retry_guidance import RetryGuidance
    env = setup(tmp_path, lanes="")
    try:
        c = env["c"]
        c.store.transition("game", "T2", "BLOCKED", {"blocker": "stuck"})
        rg = {"status": "stop", "extra_attempts": 0, "persona": "Foundry-Frontier-Adviser"}
        c.store.merge_data("game", "T2", retry_guidance=rg)
        c.unblock("game", "T2", "fixed it", reset_attempts=True)
        t = c.store.task("game", "T2")
        assert t["data"]["retry_guidance"] == rg
        assert RetryGuidance.allowance(t) == 0 and RetryGuidance.authors(t) == set()
    finally:
        env["fake"].stop()
