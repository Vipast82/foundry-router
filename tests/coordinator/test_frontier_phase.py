"""Stage 6: the optional frontier-primary phase. Default off; manual request;
ownership moves only at a verified idle safe point (same session, one writer);
the frontier turn is verified from Kilo's own transcript, else the phase
aborts as unsupported; bounded return conditions; restart-safe; the frontier
persona can't be the final reviewer. Fake Kilo + fake Foundry only."""

import json

import pytest

from tests.coordinator.lane_env import finish_primary, primary_tool, setup

FP = """
[frontier_primary]
enabled = true
persona = "Foundry-Kilo-Frontier"
max_turns = 2
cooldown_minutes = 60
"""
LOCAL, FRONTIER = "Foundry-Kilo-Local", "Foundry-Kilo-Frontier"


@pytest.fixture()
def env(tmp_path):
    e = setup(tmp_path, lanes=FP)
    yield e
    e["fake"].stop()


def pprompts(env):
    return [p for p in env["fake"].prompts if p["session_id"] == env["primary"]]


def test_disabled_by_default(tmp_path):
    e = setup(tmp_path, lanes="")
    try:
        with pytest.raises(ValueError, match="disabled"):
            e["c"].frontier.request("game", "T2", "victor", "hard bug")
    finally:
        e["fake"].stop()


def test_phase_starts_at_idle_is_verified_and_hands_back(env):
    c, ff = env["c"], env["ff"]
    env["fake"].set_status(env["primary"], "busy")
    ph = c.frontier.request("game", "T2", "victor", "S45 ordering defeats the local model")
    assert ph["state"] == "requested"
    for _ in range(3):
        c.tick()                                             # busy: nothing happens
    assert len(pprompts(env)) == 1 and c.frontier.phase("game", "T2")["state"] == "requested"
    finish_primary(env, {"status": "NOT DONE", "summary": "stuck on S45"})
    c.tick()
    ph = c.frontier.phase("game", "T2")
    assert ph["state"] == "active" and ph["head"] and ph["snapshot_id"]
    pp = pprompts(env)
    assert len(pp) == 2 and pp[-1]["model"] == FRONTIER and "Frontier primary phase" in pp[-1]["text"]
    b = [x for x in ff.bind_kw if x["session_id"] == env["primary"]][-1]
    assert b["persona"] == FRONTIER and b["purpose"] == "frontier_primary"      # gated as optional
    finish_primary(env, {"status": "NOT DONE", "summary": "hard part fixed"})
    c.tick()
    [done] = c.frontier.phases("game", "T2")
    assert done["state"] == "returned" and done["verified_model"] == FRONTIER and done["turns"] == 1
    pp = pprompts(env)
    assert len(pp) == 3 and pp[-1]["model"] == LOCAL                      # ownership restored
    assert c.store.task("game", "T2")["session_id"] == env["primary"]      # same single session
    assert FRONTIER in c.frontier.authors("game", "T2")                    # can't self-review


def test_unsupported_switch_aborts_and_disables(env):
    c = env["c"]
    env["fake"].ignore_prompt_model = True
    c.frontier.request("game", "T2", "victor", "x")
    finish_primary(env, {"status": "NOT DONE", "summary": "s"})
    c.tick()
    finish_primary(env, {"status": "NOT DONE", "summary": "s"})
    c.tick()
    [ph] = c.frontier.phases("game", "T2")
    assert ph["state"] == "aborted" and "not supported" in ph["end_reason"]
    assert pprompts(env)[-1]["model"] == LOCAL
    assert c.store.meta("frontier_unsupported")
    c.store.x("DELETE FROM primary_phases")
    with pytest.raises(ValueError, match="does not serve"):
        c.frontier.request("game", "T2", "victor", "again")


def test_running_tool_is_not_a_safe_point(env):
    c = env["c"]
    c.frontier.request("game", "T2", "victor", "x")
    env["fake"].add_message(env["primary"], "assistant", [{
        "type": "tool", "tool": "bash", "state": {"status": "running", "input": {"command": "x"}}}],
        completed=True)
    finish_primary(env, {"status": "NOT DONE", "summary": "s"})
    c.tick()
    assert c.frontier.phase("game", "T2")["state"] == "requested"
    assert all(p["model"] != FRONTIER for p in pprompts(env))


def test_done_report_cancels_a_request_and_limits_prevent_oscillation(env):
    c = env["c"]
    c.frontier.request("game", "T2", "victor", "x")
    with pytest.raises(ValueError, match="already"):
        c.frontier.request("game", "T2", "victor", "y")
    finish_primary(env, {"status": "DONE", "summary": "solved locally"})
    c.tick()
    [ph] = c.frontier.phases("game", "T2")
    assert ph["state"] == "cancelled"
    assert all(p["model"] != FRONTIER for p in pprompts(env))


def test_max_phases_and_cooldown(tmp_path):
    e = setup(tmp_path, lanes=FP.replace("max_turns = 2", "max_turns = 2\nmax_phases_per_task = 2"))
    try:
        c = e["c"]
        c.frontier.request("game", "T2", "victor", "x")
        c.frontier.request_return("game", "T2", "victor")
        with pytest.raises(ValueError, match="cooldown"):
            c.frontier.request("game", "T2", "victor", "x")
        e["clock"][0] += 61 * 60
        c.frontier.request("game", "T2", "victor", "x")
        c.frontier.request_return("game", "T2", "victor")
        e["clock"][0] += 61 * 60
        with pytest.raises(ValueError, match="max_phases_per_task"):
            c.frontier.request("game", "T2", "victor", "x")
    finally:
        e["fake"].stop()


def test_restart_keeps_the_ownership_record(env):
    c = env["c"]
    c.frontier.request("game", "T2", "victor", "x")
    finish_primary(env, {"status": "NOT DONE", "summary": "s"})
    c2 = env["make"]()                                       # coordinator restarted
    c2.tick()
    assert c2.frontier.phase("game", "T2")["state"] == "active"
    assert pprompts(env)[-1]["model"] == FRONTIER
    c3 = env["make"]()
    c3.tick()                                                # frontier still busy: no new prompt
    assert len([p for p in pprompts(env) if p["model"] == FRONTIER]) == 1


def test_refusal_returns_ownership(env):
    c = env["c"]
    c.frontier.request("game", "T2", "victor", "x")
    finish_primary(env, {"status": "NOT DONE", "summary": "s"})
    c.tick()
    assert c.frontier.phase("game", "T2")["state"] == "active"
    env["fake"].add_message(env["primary"], "assistant", [], completed=True, error={
        "name": "APIError", "data": {"message": json.dumps({"error": {
            "code": "optional_deferred", "foundry_category": "budget", "reset_seconds": 600}})}})
    env["fake"].set_status(env["primary"], None)
    c.tick()
    [ph] = c.frontier.phases("game", "T2")
    assert ph["state"] == "returned" and "refused" in ph["end_reason"]
    assert c.store.task("game", "T2")["state"] == "PAUSED"


def test_new_session_ends_a_phase(env):
    c = env["c"]
    c.frontier.request("game", "T2", "victor", "x")
    primary_tool(env, "lune run x", "ok")
    c.frontier.on_new_session(list(c.projects.values())[0], "T2")
    assert c.frontier.phases("game", "T2")[0]["state"] == "cancelled"
