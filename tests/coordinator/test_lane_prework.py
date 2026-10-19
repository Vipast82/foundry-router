"""v0.126 prework lane: helpers prepare the primary's next plan steps ahead of
time (draft patch, exact commands, gotchas), the coordinator stores the patch
under the git-ignored prework dir and a PREWORK note in the task memory every
primary prompt carries, on its own budget (reactive lanes cannot starve it);
a new HEAD retires stale prework. Fake Kilo + fake Foundry, no model calls."""

import json
import os

import pytest

from tests.coordinator.helpers import git
from tests.coordinator.lane_env import CLAUDE, admit, answer_helper, setup

LANES = """
[lifecycle]
enabled = true

[lanes]
enabled = true
transcript_poll_seconds = 0
max_lane_units_per_task = {units}
max_dispatches_per_task = {dispatches}

[lanes.prework]
enabled = true
steps_ahead = 2
dir = "work/prework"
"""
T2 = {"id": "T2", "title": "Menu layout",
      "spec": "Fix the menu.\n- [x] scaffold\n- [ ] add the close button to src/menu.luau\n"
              "- [ ] five-device captures\n- [ ] docs\nNEXT: add the close button",
      "scope": ["src/menu.luau", "src/tests/*", "docs/evidence/T2/*"],
      "tags": []}
PATCH = ("--- a/src/menu.luau\n+++ b/src/menu.luau\n@@ -1,2 +1,3 @@\n local M = {}\n"
         "+M.close = function() end\n return M\n")


@pytest.fixture()
def mk(tmp_path):
    made = []

    def make(units=96, dispatches=64):
        e = setup(tmp_path, lanes=LANES.format(units=units, dispatches=dispatches), tasks=[T2])
        e["ff"].admission[CLAUDE] = admit()
        made.append(e)
        return e
    yield make
    for e in made:
        e["fake"].stop()


def prework(c):
    return [u for u in c.helpers.units("game", "T2") if u.get("lane") == "prework"]


def test_next_steps_get_prework_on_their_own_budget(mk):
    """Two open steps get prework at run start even though the reactive lanes'
    task-wide unit and dispatch caps are already used up (0 here)."""
    env = mk(units=0, dispatches=0)
    c = env["c"]
    c.tick()
    c.tick()
    units = prework(c)
    assert len(units) == 2
    steps = [json.loads(u["packet"])["step"] for u in units]
    assert steps == ["add the close button to src/menu.luau", "five-device captures"]
    head = c.projects["game"].ws.head()
    assert all(json.loads(u["packet"])["head"] == head for u in units)
    assert all(u.get("session_id") for u in units)           # dispatched despite the 0 caps
    text = next(p["text"] for p in env["fake"].prompts if p["session_id"] == units[0]["session_id"])
    assert "The ONE step to prepare" in text and "minimal unified diff" in text
    assert "add the close button" in text


def test_prework_patch_and_note_reach_the_primary(mk):
    env = mk()
    c = env["c"]
    c.tick()
    c.tick()
    u = next(x for x in prework(c) if "close button" in json.loads(x["packet"])["step"])
    answer_helper(env, u["session_id"], {
        "assignment": u["unit_id"], "base": u["snapshot_id"],
        "summary": "close button: one function on M", "patch": PATCH,
        "findings": [{"id": "F1", "severity": "info", "confidence": 0.9,
                      "observed": "M has no close handler yet", "paths": ["src/menu.luau:1"],
                      "in_scope": True}],
        "checks_proposed": ["lune run tests/lune/run.luau"],
        "next_action": "apply the patch and run the Lune suite"})
    c.tick()
    rel = f"work/prework/T2/{u['unit_id']}.patch"
    path = env["root"] / rel
    assert path.exists() and "M.close" in path.read_text()
    P = c.projects["game"]
    notes = [m for m in c.ledger.memories("game", "T2") if m["text"].startswith("PREWORK")]
    assert len(notes) == 1 and rel in notes[0]["text"] and "lune run" in notes[0]["text"]
    assert notes[0]["actor"] == f"helper:{u['unit_id']}"
    assert notes[0]["revision"] == P.ws.head()
    block = c._lc_memory_blocks(P, "T2")
    assert "PREWORK" in block and "data, not instructions" in block
    # git apply --check accepts the stored draft against the current code
    git(env["root"], "apply", "--check", rel)


def test_a_new_head_retires_stale_prework(mk):
    env = mk()
    c = env["c"]
    c.tick()
    c.tick()
    u = prework(c)[0]
    answer_helper(env, u["session_id"], {"assignment": u["unit_id"], "base": u["snapshot_id"],
                                         "summary": "draft", "patch": PATCH})
    c.tick()
    assert [m for m in c.ledger.memories("game", "T2") if m["text"].startswith("PREWORK")]
    (env["root"] / "src/menu.luau").write_text("local M = {close = true}\nreturn M\n")
    git(env["root"], "commit", "-qam", "T2: close button")
    P = c.projects["game"]
    c.helpers.lanes._plan_prework(P, c.store.task("game", "T2"),
                                  P.adapter.describe("T2"), __import__(
                                      "foundry_coordinator.lanes", fromlist=["settings"]
                                  ).settings(P))
    assert not [m for m in c.ledger.memories("game", "T2") if m["text"].startswith("PREWORK")]
    heads = {json.loads(x["packet"])["head"] for x in prework(c)
             if x["status"] in ("pending", "deferred")}
    assert heads <= {P.ws.head()}            # queued older-HEAD prework was superseded
    assert any(json.loads(x["packet"])["head"] == P.ws.head() for x in prework(c))


def test_prework_out_of_scope_patch_is_not_stored(mk):
    env = mk()
    c = env["c"]
    c.tick()
    c.tick()
    u = prework(c)[0]
    bad = PATCH.replace("src/menu.luau", "src/other.luau")
    answer_helper(env, u["session_id"], {"assignment": u["unit_id"], "base": u["snapshot_id"],
                                         "summary": "draft", "patch": bad})
    c.tick()
    assert not os.path.exists(env["root"] / f"work/prework/T2/{u['unit_id']}.patch")
    assert not [m for m in c.ledger.memories("game", "T2") if m["text"].startswith("PREWORK")]


def test_results_carry_into_the_next_run_while_their_code_is_unchanged(mk):
    env = mk()
    c = env["c"]
    c.tick()
    c.tick()
    u = prework(c)[0]
    answer_helper(env, u["session_id"], {"assignment": u["unit_id"], "base": u["snapshot_id"],
                                         "summary": "draft", "patch": PATCH})
    c.tick()
    P = c.projects["game"]
    lanes = c.helpers.lanes
    u = next(x for x in prework(c) if x["unit_id"] == u["unit_id"])
    assert lanes._still_valid(P, u)                       # same code: carried, not expired
    (env["root"] / "src/menu.luau").write_text("local M = {x = 1}\nreturn M\n")
    assert not lanes._still_valid(P, u)                   # its code changed: expires


# ------------------------------------------------------------------ v0.126.2

T_CLOSE = {**T2, "spec": "Fix the menu.\n- [x] scaffold\n- [~] 4 kit legs green PASS\n"
                          "NEXT: CLOSE. done_check PASS; remaining rows = SUMMARY + STATE row"}
T_NEXT = {**T2, "spec": "Fix the menu.\n- [~] 4 kit legs green\n- [ ] 5 rebuild HeroSelect\n"
                         "- [ ] 6 docs\nNEXT: step 6 docs after the gate"}


def make_task(tmp_path, task):
    e = setup(tmp_path, lanes=LANES.format(units=96, dispatches=64), tasks=[task])
    e["ff"].admission[CLAUDE] = admit()
    return e


def test_prework_starts_with_the_step_the_plan_next_line_names(tmp_path):
    e = make_task(tmp_path, T_NEXT)
    try:
        e["c"].tick()
        e["c"].tick()
        units = sorted(prework(e["c"]), key=lambda u: json.loads(u["packet"])["step_index"])
        assert json.loads(units[0]["packet"])["step"] == "6 docs"
        assert json.loads(units[0]["packet"])["mode"] == "build"
    finally:
        e["fake"].stop()


def test_build_done_prepares_close_drafts_kept_for_the_close_session(tmp_path):
    e = make_task(tmp_path, T_CLOSE)
    try:
        c = e["c"]
        c.tick()
        c.tick()
        [u] = prework(c)
        pk = json.loads(u["packet"])
        assert pk["mode"] == "close" and pk["step"].startswith("CLOSE drafts")
        doc = (pk["close_paths"] or ["docs/progress/T2_SUMMARY.md"])[0]
        draft = (f"--- /dev/null\n+++ b/{doc}\n@@ -0,0 +1,2 @@\n+# T2 summary\n"
                 "+Status: DONE\n")
        if not pk["close_paths"]:
            pk["close_paths"] = [doc]
            c.store.x("UPDATE helper_units SET packet=? WHERE unit_id=?",
                      (json.dumps(pk), u["unit_id"]))
        answer_helper(e, u["session_id"], {"assignment": u["unit_id"], "base": u["snapshot_id"],
                                           "summary": "summary + ledger row drafted",
                                           "patch": draft})
        c.tick()
        rel = f"work/prework/T2/close-{u['unit_id']}.patch"
        assert (e["root"] / rel).exists()
        [note] = [m for m in c.ledger.memories("game", "T2") if "PREWORK" in m["text"][:60]]
        assert note["text"].startswith("PREWORK CLOSE draft for the CLOSE session only")
        # never handed to the BUILD primary as a card
        assert c.helpers.lanes.deliverable("game", "T2", [c.helpers.unit(u["unit_id"])
                                                          if hasattr(c.helpers, "unit") else
                                                          next(x for x in prework(c))],
                                           e["primary"]) == []
    finally:
        e["fake"].stop()


def test_reactive_lane_budget_is_per_run(mk):
    """An earlier run's units no longer use up this run's reactive budget."""
    env = mk(units=2, dispatches=2)
    c = env["c"]
    t = c.store.task("game", "T2")
    for i in range(3):                     # three old-run reactive units
        c.store.x("INSERT INTO helper_units (unit_id, project_id, task_id, run_id, "
                  "responsibility, kind, status, lane, created_ts, updated_ts, attempts) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (f"hu-old{i}", "game", "T2", "old-run", f"failure_triage:old{i}",
                   "failure_triage", "done", "failure_triage", 1.0, 1.0, 1))
    P = c.projects["game"]
    from foundry_coordinator import lanes as L
    ls = L.settings(P)
    ls["lanes"]["failure_triage"]["enabled"] = True
    uid, how = c.helpers.lanes.add(P, t, "failure_triage", "new-failure", {}, ls)
    assert how == "created"


def test_stale_prework_is_never_delivered(mk):
    env = mk()
    c = env["c"]
    c.tick()
    c.tick()
    u = prework(c)[0]
    answer_helper(env, u["session_id"], {"assignment": u["unit_id"], "base": u["snapshot_id"],
                                         "summary": "draft", "patch": PATCH})
    c.tick()
    (env["root"] / "src/menu.luau").write_text("local M = {x = 2}\nreturn M\n")
    git(env["root"], "commit", "-qam", "T2: moved")
    units = [x for x in prework(c) if x["unit_id"] == u["unit_id"]]
    assert c.helpers.lanes.deliverable("game", "T2", units, env["primary"]) == []
    fb = [x for x in prework(c) if x["unit_id"] == u["unit_id"]][0]["feedback"]
    assert fb in ("superseded", "delivered", "integrated")


def test_prework_survives_the_session_ending(mk):
    """UI-02: an ECONNRESET ended the primary and cancelled its in-flight
    prework. Prework is bound to a commit, not a session: running units keep
    running, queued ones stay queued, finished results do not expire."""
    env = mk()
    c = env["c"]
    c.tick()
    c.tick()
    units = prework(c)
    running = [u for u in units if u["status"] == "running"]
    assert running
    P = c.projects["game"]
    t = c.store.task("game", "T2")
    c._session_failed(P, t, "session ended with an error: ECONNRESET", "implement")
    c.helpers.skip_waiting(P, "T2", "the primary finished")
    after = {u["unit_id"]: u for u in prework(c)}
    assert all(after[u["unit_id"]]["status"] == "running" for u in running)
    u = running[0]
    answer_helper(env, u["session_id"], {"assignment": u["unit_id"], "base": u["snapshot_id"],
                                         "summary": "draft", "patch": PATCH})
    for _ in range(3):
        c.tick()
    c.helpers.expire_lane_results(P, "T2", "the run ended")
    done = {x["unit_id"]: x for x in prework(c)}[u["unit_id"]]
    assert done["feedback"] != "expired"
