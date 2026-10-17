"""Stage 5: preflight (reusing the planner instead of duplicating it),
documentation research with provenance and a cross-task cache, evidence
reconciliation with deterministic contradictions, and context handoff carried
into the next session prompt. Triggers come from real fake-Kilo transcript
events."""

import json

import pytest

from tests.coordinator.helpers import git
from tests.coordinator.lane_env import CLAUDE, admit, answer_helper, finish_primary, \
    primary_tool, setup

ONLY = """
[lanes]
enabled = true
transcript_poll_seconds = 0
{body}
"""
T2 = {"id": "T2", "title": "Menu layout", "spec": "Fix the menu.\n- [x] build menu\n- [ ] five-device "
      "captures\nNEXT: capture phone frame", "scope": ["src/menu.luau", "src/tests/*",
                                                         "docs/evidence/T2/*"], "tags": ["visual"]}


def lane_units(c, lane=None):
    return [u for u in c.helpers.units("game", "T2") if u.get("lane") and
            (lane is None or u["lane"] == lane)]


def prompts_to(env, sid):
    return [p for p in env["fake"].prompts if p["session_id"] == sid]


@pytest.fixture()
def mk(tmp_path):
    made = []

    def make(body, **kw):
        e = setup(tmp_path, lanes=ONLY.format(body=body), tasks=[T2], **kw)
        e["ff"].admission[CLAUDE] = admit()
        made.append(e)
        return e
    yield make
    for e in made:
        e["fake"].stop()


def test_preflight_runs_once_for_an_approved_run(mk):
    env = mk("[lanes.preflight]\nenabled = true\n")
    env["c"].tick()
    env["c"].tick()
    [u] = lane_units(env["c"], "preflight")
    hp = prompts_to(env, u["session_id"])[0]["text"]
    assert "no invented blockers" in hp and "capture phone frame" in hp


def test_preflight_reuses_the_existing_planner(mk):
    env = mk("[lanes.preflight]\nenabled = true\n")
    P = list(env["c"].projects.values())[0]
    P.profile.roles["planner"] = "Foundry-Kilo-Planner"
    orig = P.adapter.implement_prompt
    P.adapter.implement_prompt = lambda spec, ctx: orig(spec, ctx) + "\nInvoke au-planner once."
    env["c"].tick()
    assert lane_units(env["c"]) == []
    assert [e for e in env["c"].store.events("game", 30) if e["kind"] == "lane_reused"]


def test_research_provenance_and_cache(mk):
    env = mk("[lanes.research]\nenabled = true\n")
    c = env["c"]
    primary_tool(env, "lune run tests/lune/run.luau",
                 "PlayerGui.Menu: SafeAreaCompat is not a valid member of ScreenGui")
    c.tick()
    [u] = lane_units(c, "research")
    pk = json.loads(u["packet"])
    assert pk["symbol"] == "ScreenGui.SafeAreaCompat" and "at most 6" in pk["question"]
    answer_helper(env, u["session_id"], {"assignment": u["unit_id"], "summary": "use ScreenInsets",
                                         "sources": ["https://devforum.example/thread"],
                                         "next_action": "use ScreenGui.ScreenInsets"})
    c.tick()
    u = lane_units(c, "research")[0]
    env_ = json.loads(u["envelope"])
    assert env_["checks"]["provenance"]["ok"] is False and "unverified" in env_["limitations"]
    assert not c.store.q("SELECT * FROM lane_cache")             # unofficial: not cached
    # an answer with an official source is cached and served again without inference
    lanes = c.helpers.lanes
    lanes._after_research(list(c.projects.values())[0], {**u, "packet": u["packet"]},
                          {"sources": ["https://create.roblox.com/docs/reference/engine/classes/"
                                       "ScreenGui#ScreenInsets"]})
    assert c.store.q("SELECT cache_key FROM lane_cache")[0]["cache_key"] == \
        "research:ScreenGui.SafeAreaCompat"
    P = list(c.projects.values())[0]
    from foundry_coordinator import lanes as L
    t3 = {"task_id": "T3", "run_id": "r3", "session_id": env["primary"]}
    ev = {"event_key": "x", "signature": "ScreenGui.SafeAreaCompat", "detail": {"line": "again"},
          "tries": 0, "session_id": env["primary"]}
    c.store.x("INSERT INTO lane_events (event_key, project_id, task_id, kind, status) VALUES "
              "('x','game','T3','api','new')")
    lanes._plan_api(P, t3, P.adapter.describe("T2"), L.settings(P), ev)
    [cached] = c.store.q("SELECT * FROM helper_units WHERE task_id='T3'")
    assert cached["status"] == "done" and cached["persona"] == "cache" and not cached["session_id"]
    assert "no inference" in cached["reason"]


def test_evidence_reconciliation_finds_contradictions_by_code(mk):
    env = mk("[lanes.evidence]\nenabled = true\n", extra_files={
        "docs/progress/T2_EVIDENCE_RAW.log": "PASS: GATE HEAD 0123456789ab\nGATE T2 FAIL\n"})
    c = env["c"]
    primary_tool(env, "pwsh tools/gate.ps1 -Sprint T2", "...\nGATE T2 PASS (local run)")
    c.tick()
    [u] = lane_units(c, "evidence")
    pk = json.loads(u["packet"])
    probs = pk["deterministic"]["problems"]
    assert any("wrong revision" in p for p in probs)
    assert any("last verdict is GATE T2 FAIL" in p for p in probs)
    assert any("unticked" in p for p in probs)
    assert any("without a valid evidence image" in p for p in probs)
    hp = prompts_to(env, u["session_id"])[0]["text"]
    assert "Never claim a check ran" in hp and "five-device captures" in hp
    answer_helper(env, u["session_id"], {"assignment": u["unit_id"], "summary": "2 gaps",
                                         "findings": [{"severity": "high", "confidence": 1,
                                                       "observed": "gate verdict contradicts log"}]})
    c.tick()
    finish_primary(env, {"status": "DONE", "summary": "gate passed"})
    c.tick()
    assert "gate verdict contradicts log" in prompts_to(env, env["primary"])[-1]["text"]


def test_context_handoff_reaches_the_next_session_while_head_is_unchanged(mk):
    env = mk("[lanes.handoff]\nenabled = true\ncontext_threshold = 100000\n")
    c = env["c"]
    env["fake"].set_status(env["primary"], "busy")
    env["fake"].add_message(env["primary"], "assistant", [{"type": "text", "text": "..."}],
                            completed=False, tokens={"input": 3000, "output": 5,
                                                     "cache": {"read": 120000, "write": 0}})
    c.tick()
    [u] = lane_units(c, "handoff")
    pk = json.loads(u["packet"])
    assert pk["context_tokens"] == 123000 and "capture phone frame" in pk["plan_next"]
    answer_helper(env, u["session_id"], {"assignment": u["unit_id"],
                                         "summary": "menu built; captures pending",
                                         "next_action": "capture the phone frame"})
    c.tick()
    P = list(c.projects.values())[0]
    note, hu = c.helpers.lanes.handoff_note(P, "T2")
    assert "captures pending" in note and hu == u["unit_id"]
    finish_primary(env, {"status": "DONE", "summary": "ran out of context"})
    c.tick()                                                       # the handoff card first
    assert "captures pending" in prompts_to(env, env["primary"])[-1]["text"]
    finish_primary(env, {"status": "DONE", "summary": "stopping"})
    for _ in range(4):
        c.tick()
    t = c.store.task("game", "T2")
    assert t["session_id"] != env["primary"]                       # a repair session started
    first = prompts_to(env, t["session_id"])[0]["text"]
    assert "Handoff prepared by a read-only helper" in first and "capture the phone frame" in first
    (env["root"] / "src/menu.luau").write_text("OK\n")
    git(env["root"], "commit", "-qam", "T2: moved on")
    assert c.helpers.lanes.handoff_note(P, "T2") == ("", "")       # stale revision: not offered


def test_every_lane_binding_is_optional_and_required_work_is_unchanged(tmp_path):
    from foundry_router.orchestration import OPTIONAL_PURPOSES
    from tests.coordinator.lane_env import LANES_ALL, VISION, capture_output, png
    env = setup(tmp_path, lanes=LANES_ALL, tasks=[T2])
    try:
        c, ff = env["c"], env["ff"]
        ff.admission[CLAUDE] = admit()
        ff.admission[VISION] = admit(routes=[{"backend": "m", "model": "v", "vision": True}])
        data = png(6, 4)
        f = env["root"] / "docs/evidence/T2/m.png"
        f.write_bytes(data)
        primary_tool(env, "pwsh tools/capture-window.ps1", capture_output(str(f), data, 6, 4))
        primary_tool(env, "lune run tests/lune/run.luau", "FAIL x\nUnknown global 'wait2'",
                     exit_code=1)
        primary_tool(env, "pwsh tools/gate.ps1 -Sprint T2", "GATE T2 FAIL")
        for _ in range(3):
            c.tick()
            c.helpers.lanes.wait_direct()
        lanes_seen = {u["lane"] for u in lane_units(c)}
        assert {"failure_triage", "research", "evidence", "visual", "test_design",
                "preflight"} <= lanes_seen
        helper_binds = [b for b in ff.bind_kw if b["role"] == "helper"]
        assert helper_binds and all(b["purpose"] in OPTIONAL_PURPOSES for b in helper_binds)
        assert [b["purpose"] for b in ff.bind_kw if b["role"] == "implementer"] == ["implementer"]
        # the aggregate optional-helper cap (max_running = 3) holds across all lanes
        assert len([u for u in c.helpers.units("game", "T2") if u["status"] == "running"]) <= 3
    finally:
        env["fake"].stop()
