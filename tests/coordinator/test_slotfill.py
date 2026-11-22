"""Keep the slots busy (slotfill.py): items 12, 13 and 14 of COORDINATOR_AUTOMATION_NEEDS.
A free slot starts the next sprint that is ready, a started sprint idle behind the main
checkout moves to it, and an order-only dependency may be skipped. All of it ships off."""

from __future__ import annotations

import json

from foundry_coordinator import automation, slotfill
from foundry_coordinator import states as S
from foundry_coordinator.tracks import analysis as AN
from tests.coordinator.helpers import by_task, coordinator, make_repo, write_ok, write_profile

TASKS = [
    {"id": "T1", "title": "main sprint", "spec": "Write src/a.txt.", "scope": ["src/a.txt"]},
    {"id": "T2", "title": "its own file", "spec": "Write src/b.txt.", "scope": ["src/b.txt"]},
    {"id": "X1", "title": "shares a file with T1", "spec": "Write src/a.txt.",
     "scope": ["src/a.txt", "src/x.txt"]},
    {"id": "PT-1", "title": "timing work", "spec": "Write src/pt.txt.", "scope": ["src/pt.txt"]},
    {"id": "R1", "title": "release step", "spec": "Write rel/r1.txt.", "scope": ["rel/r1.txt"],
     "deps": ["T1"]},
    {"id": "R2", "title": "needs T1's file", "spec": "Write src/a.txt.", "scope": ["src/a.txt"],
     "deps": ["T1"]},
]
EXTRA = """
[automation]
max_agents = 3
workstation_only_sprints = ["PT-*"]

[slots.slot-1]
container = "au-slot-1"

[slots.slot-2]
container = "au-slot-2"
"""


class Clock:
    def __init__(self):
        import time
        self.t = time.time()

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def env(tmp_path, approve=("T1", "T2", "X1", "PT-1")):
    root, remote = make_repo(tmp_path, "game", TASKS, extra_files={"kilo.jsonc": '{"mcp": {}}\n'})
    prof = write_profile(tmp_path, "game", root, extra=EXTRA, publish="none", review=False)
    scripts = {"game": by_task({t["id"]: write_ok(t["scope"][-1]) for t in TASKS})}
    c = coordinator(tmp_path, [prof], scripts)
    clk = Clock()
    c.clock = clk
    P = c.projects["game"]
    for tid in approve:
        deps = [d for d in next(t for t in TASKS if t["id"] == tid).get("deps", []) if d in approve]
        c.approve("game", tid, deps, "victor")
    return c, P, clk


def working(c, tid):
    """`tid` holds the main checkout."""
    c.store.update_task("game", tid, state=S.IMPLEMENTING, phase="implement", attempt=1,
                        sessions=1, started_ts=c.clock())


def events(c, kind):
    return [json.loads(e["detail"]) if isinstance(e["detail"], str) else e["detail"]
            for e in c.store.events("game", 300) if e["kind"] == kind]


def lanes(c):
    return {r["name"]: r for r in c.store.tracks("game") if r["state"] in ("planned", "running")}


def go(c, P, clk, minutes=6):
    slotfill.step(c, P)                  # sees the slot free
    clk.advance(minutes * 60)
    return slotfill.step(c, P)


def test_off_by_default_nothing_starts_and_the_board_still_says_what_would(tmp_path):
    c, P, clk = env(tmp_path)
    working(c, "T1")
    assert automation.get(P, "slot_auto_fill") is False
    assert go(c, P, clk) is None and lanes(c) == {}
    st = slotfill.state(c, P)
    assert st["on"] is False and st["free"] == ["slot-1", "slot-2"]
    assert st["next"] == {"sprint": "T2", "rule": "ready", "drop": []}


def test_a_free_slot_starts_the_next_ready_sprint_that_shares_no_file(tmp_path):
    c, P, clk = env(tmp_path)
    working(c, "T1")
    automation.set_live(P, "slot_auto_fill", True)
    assert slotfill.step(c, P) is None                         # free, but not for 5 minutes yet
    clk.advance(4 * 60)
    assert slotfill.step(c, P) is None and lanes(c) == {}
    clk.advance(61)
    got = slotfill.step(c, P)
    assert got["sprint"] == "T2" and got["slot"] == "slot-1" and got["rule"] == "ready"
    assert got["text"] == "T2 started on slot-1 (lane t2): ready and never started"
    row = lanes(c)["t2"]
    assert row["state"] == "running" and row["data"]["slot"] == "slot-1" and row["sprints"] == ["T2"]
    assert c.store.task_track("game", "T2") == "t2"
    assert events(c, "slot_auto_filled")[0]["track"] == "t2"
    # slot-2 stays free: X1 shares src/a.txt with T1, PT-1 is workstation-only
    clk.advance(10 * 60)
    assert slotfill.step(c, P) is None and list(lanes(c)) == ["t2"]
    why = slotfill.state(c, P)["why"]
    assert "X1 shares src/a.txt with T1" in why and "PT-1 is workstation-only" in why


def test_one_start_a_minute_never_over_max_agents_never_while_paused(tmp_path):
    c, P, clk = env(tmp_path, approve=("T1", "T2", "R1"))
    working(c, "T1")
    automation.set_live(P, "slot_auto_fill", True)
    automation.set_live(P, "slot_auto_fill_minutes", 0)
    c.store.x("UPDATE projects SET paused=1 WHERE project_id='game'")
    assert slotfill.step(c, P) is None
    c.store.x("UPDATE projects SET paused=0 WHERE project_id='game'")
    assert slotfill.step(c, P) is None                         # asked a second ago
    clk.advance(61)
    assert slotfill.step(c, P)["sprint"] == "T2"
    # every seat taken: nothing starts, and the board says so
    c.max_writers_override = None
    automation.set_live(P, "max_agents", 1)
    c.store.lock("writer:0", "game", "T1", 3600) if hasattr(c.store, "lock") else None
    if c.tracks.agents_running() >= c.tracks.max_agents():
        clk.advance(61)
        assert slotfill.step(c, P) is None
        assert slotfill.state(c, P)["why"] == "max_agents 1 is in use"


def test_a_started_sprint_idle_behind_the_main_checkout_moves_to_the_slot(tmp_path):
    """2026-11-20 FIN-4a: it had run sessions, then waited READY behind FIN-2c for the one
    main checkout while two slots sat idle; the operator typed `--move-started`."""
    c, P, clk = env(tmp_path, approve=("T1", "T2"))
    automation.set_live(P, "slot_auto_fill", True)
    # T2 ran a session and is READY between sessions; T1 holds the main checkout
    c.store.transition("game", "T2", S.READY, {"deps_satisfied": True})
    c.store.update_task("game", "T2", attempt=1, sessions=2, started_ts=clk.t)
    assert go(c, P, clk) is None                               # the main checkout is free:
    assert lanes(c) == {}                                      # it runs there, nothing moves
    assert "T1 runs on the main checkout" in slotfill.state(c, P)["why"]
    working(c, "T1")
    clk.advance(61)
    got = slotfill.step(c, P)
    assert got["rule"] == "moved" and got["sprint"] == "T2"
    assert "started, waiting READY behind the main checkout" in got["text"]
    assert c.store.task_track("game", "T2") == "t2"
    assert c.tracks.move_started == frozenset()                # the operator's flag is not left on


def test_a_started_sprint_with_a_live_session_is_never_moved(tmp_path):
    c, P, clk = env(tmp_path, approve=("T1", "T2"))
    automation.set_live(P, "slot_auto_fill", True)
    working(c, "T1")
    c.store.transition("game", "T2", S.READY, {"deps_satisfied": True})
    c.store.update_task("game", "T2", attempt=1, sessions=1, session_id="ses_live")
    c.store.x("INSERT INTO sessions (session_id, project_id, task_id, run_id, role, phase, "
              "attempt, client, persona, status, created_ts, data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              ("ses_live", "game", "T2", "r", "implementer", "implement", 1, "kilo", "p",
               "running", clk.t, "{}"))
    assert go(c, P, clk) is None and lanes(c) == {}


def test_plans_mark_a_dependency_order_or_files():
    text = """# R1
**Depends on:** FIN-5 (order), FIN-4a (files), BAL-2e
"""
    assert AN.plan_deps(text, "R1") == ["FIN-5", "FIN-4a", "BAL-2e"]
    assert AN.plan_order_deps(text, "R1") == ["FIN-5"]
    p = AN.parse_plan("R1", text)
    assert p.order_deps == ["FIN-5"] and p.to_json()["order_deps"] == ["FIN-5"]
    # prose, other spellings, and nothing marked
    assert AN.plan_order_deps("Depends on FIN-5 (order only) and REL-1a(ORDER).", "X") == [
        "FIN-5", "REL-1a"]
    assert AN.plan_order_deps("**Depends on:** FIN-5, FIN-4a", "X") == []
    assert AN.plan_order_deps("Depends on: X (order)", "X") == []


def test_an_order_dependency_is_skipped_only_when_asked_nothing_else_is_ready_and_no_file_is_shared(
        tmp_path, monkeypatch):
    """REL-1a and REL-1b waited on FIN-5 only for scheduling; dropping that took
    `queue set-deps` by hand. R1 names T1 as order-only; R2 would need T1's file."""
    c, P, clk = env(tmp_path, approve=("T1", "R1", "R2"))
    working(c, "T1")
    automation.set_live(P, "slot_auto_fill", True)
    real = c.tracks.analyze

    def analyze(P_):
        an = real(P_)
        an["sprints"]["R1"]["order_deps"] = ["T1"]
        an["sprints"]["R2"]["order_deps"] = ["T1"]
        return an
    monkeypatch.setattr(c.tracks, "analyze", analyze)
    assert go(c, P, clk) is None and lanes(c) == {}            # order_deps_early is off
    assert "R1 waits for T1" in slotfill.state(c, P)["why"]
    automation.set_live(P, "order_deps_early", True)
    clk.advance(61)
    got = slotfill.step(c, P)
    assert got["sprint"] == "R1" and got["rule"] == "early" and got["text"].endswith("; skipped T1")
    assert c.store.queue_entry("game", "R1")["deps"] == []
    assert c.store.task("game", "R1")["data"]["deps_dropped"] == ["T1"]   # it stays removed
    [e] = events(c, "order_dep_skipped")
    assert e["deps"] == ["T1"] and "marked (order) in the plan" in e["why"]
    # R2 is marked order-only too, but its plan shares src/a.txt with T1: it waits
    clk.advance(10 * 60)
    assert slotfill.step(c, P) is None
    assert c.store.queue_entry("game", "R2")["deps"] == ["T1"]
    assert "R2 shares src/a.txt with T1" in slotfill.state(c, P)["why"]


def test_a_ready_sprint_goes_before_an_early_one(tmp_path, monkeypatch):
    c, P, clk = env(tmp_path, approve=("T1", "R1", "T2"))
    working(c, "T1")
    automation.set_live(P, "slot_auto_fill", True)
    automation.set_live(P, "order_deps_early", True)
    real = c.tracks.analyze

    def analyze(P_):
        an = real(P_)
        an["sprints"]["R1"]["order_deps"] = ["T1"]
        return an
    monkeypatch.setattr(c.tracks, "analyze", analyze)
    assert go(c, P, clk)["sprint"] == "T2"                     # R1 is earlier in the queue
    clk.advance(61)
    assert slotfill.step(c, P)["sprint"] == "R1"               # then the second slot


def test_a_refused_lane_is_an_event_and_is_left_alone_for_half_an_hour(tmp_path, monkeypatch):
    from foundry_coordinator.tracks.manager import TrackError
    c, P, clk = env(tmp_path, approve=("T1", "T2"))
    working(c, "T1")
    automation.set_live(P, "slot_auto_fill", True)
    calls = []

    def create(*a, **k):
        calls.append(a[2])
        raise TrackError("the worktree could not be made")
    monkeypatch.setattr(c.tracks, "create", create)
    assert go(c, P, clk) is None
    [e] = events(c, "slot_fill_refused")
    assert e["error"] == "the worktree could not be made" and e["slot"] == "slot-1"
    for _ in range(10):
        clk.advance(120)
        slotfill.step(c, P)
    assert calls == [["T2"]]
    clk.advance(1800)
    slotfill.step(c, P)
    assert calls == [["T2"], ["T2"]]


def test_the_settings_are_in_the_schema_and_ship_off():
    from foundry_automation import schema
    for k, d in (("slot_auto_fill", False), ("order_deps_early", False),
                 ("slot_auto_fill_minutes", 5)):
        assert schema.AUTOMATION_DEFAULTS[k] == d and schema._AUTO_DESC[k]
        assert schema.find_field(f"automation.{k}") is not None
