"""Stage 2: failure triage, incremental review and independent test design,
driven by real event ingestion (tool results in the fake Kilo primary
transcript), through the dependency-aware lane queue, quota admission,
purpose-bound read-only sessions and ONE bounded safe-point delivery."""

import json

import pytest

from tests.coordinator.helpers import git
from tests.coordinator.lane_env import CLAUDE, admit, answer_helper, finish_primary, \
    helper_sessions, primary_tool, setup

ONLY = """
[lanes]
enabled = true
transcript_poll_seconds = 0
{body}
"""
TRIAGE = ONLY.format(body="[lanes.failure_triage]\nenabled = true\n")
REVIEW = ONLY.format(body="[lanes.incremental_review]\nenabled = true\nmin_interval_seconds = 0\n")
TESTS = ONLY.format(body="[lanes.test_design]\nenabled = true\n")


def lane_units(c, lane=None):
    return [u for u in c.helpers.units("game", "T2") if u.get("lane") and
            (lane is None or u["lane"] == lane)]


def primary_prompts(env):
    return [p for p in env["fake"].prompts if p["session_id"] == env["primary"]]


def fail(env, n=1):
    return primary_tool(env, "lune run tests/lune/run.luau",
                        f"LUNE {40 + n}/42 FAIL\nFAIL S45 ordering at src/menu.luau:{n} "
                        f"({n}.1s)", exit_code=1)


@pytest.fixture()
def triage(tmp_path):
    e = setup(tmp_path, lanes=TRIAGE)
    yield e
    e["fake"].stop()


def test_failure_triage_end_to_end_while_primary_busy(triage):
    env = triage
    c, ff = env["c"], env["ff"]
    ff.admission[CLAUDE] = admit()
    fail(env)
    c.tick()
    [u] = lane_units(c, "failure_triage")
    assert u["status"] == "running" and u["backend"] == "meridian"
    b = [x for x in ff.bind_kw if x["session_id"] == u["session_id"]][0]
    assert b["purpose"] == "failure_triage" and b["role"] == "helper"
    assert b["assignment"]["lane"] == "failure_triage" and b["parent"] == env["primary"]
    hp = [p for p in env["fake"].prompts if p["session_id"] == u["session_id"]][0]["text"]
    assert "LUNE 41/42 FAIL" in hp and "helper-result" in hp and u["snapshot_id"] in hp
    assert "must not edit" in hp
    assert len(primary_prompts(env)) == 1                    # nothing sent to the busy primary
    patch = ("--- a/src/menu.luau\n+++ b/src/menu.luau\n@@ -1,2 +1,2 @@\n-local M = {}\n"
             "+local M = {order = {}}\n return M\n")
    answer_helper(env, u["session_id"], {
        "assignment": u["unit_id"], "base": u["snapshot_id"], "summary": "ordering table missing",
        "findings": [{"id": "F1", "severity": "high", "confidence": 0.8,
                      "observed": "FAIL S45 ordering", "hypothesis": "order table never built",
                      "paths": ["src/menu.luau:1"]}],
        "patch": patch, "checks_run": [], "checks_proposed": ["lune run tests/lune/run.luau"],
        "skills_used": ["au-luau-development"], "next_action": "apply and re-run lune"})
    c.tick()
    u = lane_units(c, "failure_triage")[0]
    assert u["status"] == "done" and u["feedback"] == "waiting"
    env_ = json.loads(u["envelope"])
    assert env_["checks"]["patch"]["well_formed"] and not env_["checks"]["patch"]["out_of_scope"]
    assert env_["checks"]["drift"] == {"src/menu.luau": "unchanged"}
    assert len(primary_prompts(env)) == 1                    # still busy: still nothing
    finish_primary(env, {"status": "DONE", "summary": "first pass"})
    c.tick()
    pp = primary_prompts(env)
    assert len(pp) == 2 and "Helper results (advisory)" in pp[-1]["text"]
    assert u["unit_id"] in pp[-1]["text"] and "```diff" in pp[-1]["text"]
    assert "still match the snapshot" in pp[-1]["text"]
    u = lane_units(c, "failure_triage")[0]
    assert u["status"] == "delivered" and u["feedback"] == "delivered"
    # the primary integrates, commits and acknowledges
    (env["root"] / "src/menu.luau").write_text("local M = {order = {}}\nreturn M\nOK\n")
    git(env["root"], "commit", "-qam", "T2: fix ordering")
    finish_primary(env, {"status": "DONE", "summary": "fixed",
                         "helper_feedback": [{"unit": u["unit_id"], "status": "integrated",
                                              "note": "applied, lune passes"}]})
    for _ in range(6):
        c.tick()
    u = lane_units(c, "failure_triage")[0]
    assert u["feedback"] == "integrated" and "claimed_by" in u["feedback_detail"]
    assert len(primary_prompts(env)) == 2                    # one delivery only
    t = c.store.task("game", "T2")
    assert t["state"] in ("REVIEWING", "APPROVED", "CLOSING", "COMPLETE", "AWAITING_HUMAN")
    # the independent reviewer is not the helper persona that authored the patch
    assert all(ch["persona"] != CLAUDE for ch in ff.chats)


def test_repeated_failure_is_coalesced_not_redispatched(triage):
    env = triage
    c, ff = env["c"], env["ff"]
    ff.admission[CLAUDE] = admit("defer", "Session 82% used", nxt=None)
    fail(env, 1)
    c.tick()
    fail(env, 2)                                             # same failure, other numbers
    c.tick()
    fail(env, 3)
    c.tick()
    [u] = lane_units(c, "failure_triage")
    assert u["coalesced"] == 2 and u["status"] == "deferred" and "82%" in u["reason"]
    assert not helper_sessions(env)                          # no session without admission
    ev = c.helpers.lanes.events("game", "T2", "failure")
    assert [e["status"] for e in ev] == ["planned", "coalesced", "coalesced"]


def test_quota_deferral_never_holds_the_primary_and_old_server_purpose(triage):
    env = triage
    c, ff = env["c"], env["ff"]
    ff.admission[CLAUDE] = admit("unknown", "observed 40 min ago (limit 15 min)")
    fail(env)
    c.tick()
    finish_primary(env, {"status": "DONE", "summary": "done"})
    c.tick()
    u = lane_units(c, "failure_triage")[0]
    assert u["status"] == "skipped"
    assert c.store.task("game", "T2")["state"] != "IMPLEMENTING"
    assert len(primary_prompts(env)) == 1


def test_binding_refused_by_an_older_foundry_defers(tmp_path):
    env = setup(tmp_path, lanes=TRIAGE)
    try:
        c, ff = env["c"], env["ff"]
        ff.admission[CLAUDE] = admit()
        ff.refuse_purposes = {"failure_triage"}
        fail(env)
        c.tick()
        u = lane_units(c, "failure_triage")[0]
        assert u["status"] == "deferred" and "binding failed" in u["reason"]
        assert env["fake"].aborts                            # the unbound session never ran
        assert not [p for p in env["fake"].prompts if p["session_id"] != env["primary"]]
    finally:
        env["fake"].stop()


def test_out_of_scope_patch_and_below_threshold_results_are_not_delivered(triage):
    env = triage
    c, ff = env["c"], env["ff"]
    ff.admission[CLAUDE] = admit()
    fail(env)
    c.tick()
    u = lane_units(c, "failure_triage")[0]
    answer_helper(env, u["session_id"], {
        "assignment": u["unit_id"], "base": u["snapshot_id"],
        "findings": [{"severity": "high", "confidence": 0.9, "observed": "x"}],
        "patch": "--- a/tools/gate.ps1\n+++ b/tools/gate.ps1\n@@ -1 +1 @@\n-a\n+b\n"})
    c.tick()
    u = lane_units(c, "failure_triage")[0]
    assert u["feedback"] == "rejected" and "outside the approved scope" in u["reason"]
    finish_primary(env, {"status": "DONE", "summary": "done"})
    c.tick()
    assert len(primary_prompts(env)) == 1
    assert lane_units(c, "failure_triage")[0]["feedback"] == "rejected"


def test_low_severity_result_is_display_only(triage):
    env = triage
    c, ff = env["c"], env["ff"]
    ff.admission[CLAUDE] = admit()
    fail(env)
    c.tick()
    u = lane_units(c, "failure_triage")[0]
    answer_helper(env, u["session_id"], {"assignment": u["unit_id"], "findings": [
        {"severity": "low", "confidence": 0.9, "observed": "cosmetic log noise"}]})
    c.tick()
    finish_primary(env, {"status": "DONE", "summary": "done"})
    c.tick()
    assert len(primary_prompts(env)) == 1
    assert lane_units(c, "failure_triage")[0]["feedback"] == "display_only"


def test_late_result_after_verification_is_expired(triage):
    env = triage
    c, ff = env["c"], env["ff"]
    ff.admission[CLAUDE] = admit()
    fail(env)
    c.tick()
    u = lane_units(c, "failure_triage")[0]
    finish_primary(env, {"status": "DONE", "summary": "done"})
    c.tick()                                                 # idle primary waits (bounded)
    assert c.store.task("game", "T2")["state"] == "IMPLEMENTING"
    env["clock"][0] += 16 * 60                               # past handoff_wait_minutes
    c.tick()
    u = lane_units(c, "failure_triage")[0]
    assert u["status"] in ("timed_out", "cancelled")
    answer_helper(env, u["session_id"], {"findings": [{"severity": "critical", "confidence": 1,
                                                        "observed": "late"}]})
    for _ in range(4):
        c.tick()
    assert len(primary_prompts(env)) == 1


def test_helper_tools_and_skills_are_audited(triage):
    env = triage
    c, ff = env["c"], env["ff"]
    ff.admission[CLAUDE] = admit()
    fail(env)
    c.tick()
    u = lane_units(c, "failure_triage")[0]
    from tests.coordinator.lane_env import tool_part
    env["fake"].add_message(u["session_id"], "assistant", [
        {"type": "tool", "tool": "skill", "state": {"status": "completed",
                                                    "input": {"name": "au-luau-development"}}},
        tool_part("rm -rf src", "denied", status="error")], completed=True)
    answer_helper(env, u["session_id"], {"assignment": u["unit_id"],
                                         "skills_used": ["au-luau-development", "au-roblox-dev"],
                                         "findings": []})
    c.tick()
    u = lane_units(c, "failure_triage")[0]
    sk = json.loads(u["skills"])
    assert sk["observed"] == ["au-luau-development"] and "au-roblox-dev" in sk["claimed"]
    assert json.loads(u["envelope"])["checks"]["violations"][0]["tool"] == "bash"


def test_kill_switch_stops_new_lane_units(triage):
    env = triage
    c, ff = env["c"], env["ff"]
    ff.admission[CLAUDE] = admit()
    c.helpers.lanes.set_paused("game", True, "victor", "rollout check")
    fail(env)
    c.tick()
    assert lane_units(c) == []
    c.helpers.lanes.set_paused("game", False, "victor")
    c.tick()
    assert lane_units(c, "failure_triage")


# ------------------------------------------------------------------ incremental review

def test_incremental_review_of_a_coherent_checkpoint(tmp_path):
    env = setup(tmp_path, lanes=REVIEW)
    try:
        c, ff, root = env["c"], env["ff"], env["root"]
        ff.admission[CLAUDE] = admit("defer", "weekly 75% used")
        c.tick()
        (root / "src/menu.luau").write_text("local M = {a=1}\nreturn M\n")
        git(root, "commit", "-qam", "T2: step 1")
        primary_tool(env, "git commit -m 'T2: step 1' src/menu.luau", "[main 1] T2: step 1")
        c.tick()
        [u1] = lane_units(c, "incremental_review")
        pk = json.loads(u1["packet"])
        assert "+local M = {a=1}" in pk["diff"] and pk["snapshot"] == u1["snapshot_id"]
        (root / "src/menu.luau").write_text("local M = {a=2}\nreturn M\n")
        git(root, "commit", "-qam", "T2: step 2")
        c.tick()
        units = lane_units(c, "incremental_review")
        assert len(units) == 2
        old = [u for u in units if u["unit_id"] == u1["unit_id"]][0]
        assert old["status"] == "skipped" and old["feedback"] == "superseded"
        new = [u for u in units if u["unit_id"] != u1["unit_id"]][0]
        assert "a=2" in json.loads(new["packet"])["diff"]
        assert "a=1" not in json.loads(new["packet"])["diff"].split("\n+")[-1]
        # unchanged code is not reviewed again
        env["fake"].set_status(env["primary"], "busy")
        c.tick()
        assert len(lane_units(c, "incremental_review")) == 2
        ff.admission[CLAUDE] = admit()
        env["clock"][0] += 120
        c.tick()
        new = [u for u in lane_units(c, "incremental_review") if u["unit_id"] != u1["unit_id"]][0]
        assert new["status"] == "running"
        hp = [p for p in env["fake"].prompts if p["session_id"] == new["session_id"]][0]["text"]
        assert "Roblox security" in hp and new["snapshot_id"] in hp
    finally:
        env["fake"].stop()


# ------------------------------------------------------------------ test design

def test_independent_test_design_at_run_start(tmp_path):
    env = setup(tmp_path, lanes=TESTS)
    try:
        c, ff = env["c"], env["ff"]
        ff.admission[CLAUDE] = admit()
        c.tick()
        [u] = lane_units(c, "test_design")
        assert u["status"] == "running"
        hp = [p for p in env["fake"].prompts if p["session_id"] == u["session_id"]][0]["text"]
        assert "pure-Lune" in hp and "Studio" in hp and "src/menu.luau" in hp
        answer_helper(env, u["session_id"], {
            "assignment": u["unit_id"], "summary": "3 cases",
            "findings": [], "checks_proposed": ["lune: empty menu", "Studio: 5-device layout"],
            "patch": "--- a/src/tests/menu.spec.luau\n+++ b/src/tests/menu.spec.luau\n@@ -1 +1,2 @@"
                     "\n -- t\n+it('empty menu')\n"})
        c.tick()
        finish_primary(env, {"status": "DONE", "summary": "done"})
        c.tick()
        pp = primary_prompts(env)
        assert len(pp) == 2 and "Checks proposed (not run)" in pp[-1]["text"]
    finally:
        env["fake"].stop()


def test_queue_priority_ages_and_keeps_ordinary_order(tmp_path):
    env = setup(tmp_path, lanes=TRIAGE)
    try:
        lanes = env["c"].helpers.lanes
        ls = {"aging_seconds": 100}
        a = {"unit_id": "a", "lane": None, "created_ts": 0}
        b = {"unit_id": "b", "lane": None, "created_ts": 5}
        vis = {"unit_id": "v", "lane": "visual", "priority": 3, "created_ts": 0}
        tri = {"unit_id": "t", "lane": "failure_triage", "priority": 0, "created_ts": 50}
        assert [u["unit_id"] for u in lanes.order(None, [b, a], ls, 60)] == ["b", "a"]
        assert [u["unit_id"] for u in lanes.order(None, [vis, a, tri], ls, 60)] == ["t", "a", "v"]
        # a long-waiting visual unit ages up to triage's level (no starvation), never above it
        assert [u["unit_id"] for u in lanes.order(None, [vis, tri], ls, 60)] == ["t", "v"]
        assert [u["unit_id"] for u in lanes.order(None, [vis, tri], ls, 1000)] == ["v", "t"]
        assert [u["unit_id"] for u in lanes.order(None, [tri, vis], ls, 1000)] == ["t", "v"]
    finally:
        env["fake"].stop()


def test_restart_between_session_start_and_binding_resumes_once(triage):
    from tests.coordinator.lane_env import Crash
    env = triage
    c, ff = env["c"], env["ff"]
    ff.admission[CLAUDE] = admit()
    ff.crash_bind = lambda purpose: purpose == "failure_triage"
    fail(env)
    with pytest.raises(Crash):
        c.tick()
    c2 = env["make"]()                                        # a fresh coordinator process
    c2.tick()
    [u] = lane_units(c2, "failure_triage")
    assert u["status"] == "running"
    helper_prompts = [p for p in env["fake"].prompts if p["session_id"] != env["primary"]]
    assert len(helper_prompts) == 1 and helper_prompts[0]["session_id"] == u["session_id"]
    assert [b for b in ff.bind_kw if b["session_id"] == u["session_id"]]
    assert len(helper_sessions(env)) == 1
