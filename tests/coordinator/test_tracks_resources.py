"""tracks/resources.py with a fake clock: Studio "in use" = the gate runner lock
or the main session's last Studio tool call (transcript), not the session's
existence; merge-gate admission only when the main sprint is idle on Studio for
studio_idle_minutes, no main request is pending and no main session is in
VERIFYING; the lease is bounded by studio_track_max_minutes and never taken
from the holder; a journal with acquire/wait/release rows; capacity resources
(gpu, blender) from a track's needs."""

from __future__ import annotations

import json
import os

from foundry_coordinator import states as S
from foundry_coordinator.tracks import resources as R
from tests.coordinator.helpers import by_task, coordinator, make_repo, write_profile

TASKS = [{"id": "T1", "title": "main", "spec": "x", "scope": ["src/a.txt"]},
         {"id": "B1", "title": "track", "spec": "x", "scope": ["art/b.txt"]}]
EXTRA = """
[resources]
exclusive = ["studio:ws", "rojo:34872"]

[automation]
studio_idle_minutes = 10
studio_track_max_minutes = 45
max_agents = 2
"""


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def env(tmp_path):
    root, _ = make_repo(tmp_path, "game", TASKS)
    prof = write_profile(tmp_path, "game", root, extra=EXTRA, publish="none", review=False)
    clock = Clock()
    c = coordinator(tmp_path, [prof], {"game": by_task({})}, clock=clock)
    return c, c.projects["game"], clock, root


def msg(tool, created_ms, end_ms=None):
    return {"info": {"role": "assistant", "time": {"created": created_ms}},
            "parts": [{"type": "tool", "tool": tool,
                       "state": {"status": "completed", "time": {"start": created_ms,
                                                                   "end": end_ms or created_ms}}}]}


def test_needs_map_to_capacity_rows_and_studio_never(tmp_path):
    assert R.needs_locks(["blender"]) == ["blender"]
    assert R.needs_locks(["audio", "images", "gpu"]) == ["gpu"]
    assert R.needs_locks(["studio", "backend"]) == []
    assert R.is_studio_tool("Roblox_Studio_execute_luau")
    assert R.is_studio_tool("robloxstudio-mcp_get_place_info")
    assert not R.is_studio_tool("bash")


def test_studio_in_use_comes_from_the_transcript_and_the_runner_lock(tmp_path):
    c, P, clock, root = env(tmp_path)
    rm = c.tracks.resources
    assert rm.studio_in_use(P) == "" and rm.last_studio_call(P) is None
    now_ms = int(clock() * 1000)
    n = rm.observe_transcript(P, [msg("bash", now_ms - 5000), msg("Roblox_Studio_execute_luau",
                                                                  now_ms - 60_000),
                                  msg("robloxstudio-mcp_capture_screenshot", now_ms - 30_000)],
                              "ses_1")
    assert n == 2 and rm.last_studio_call(P) == clock() - 30
    assert rm.studio_in_use(P).startswith("main session used Studio 30s ago")
    clock.t += 9 * 60
    assert "ago" in rm.studio_in_use(P)
    clock.t += 2 * 60
    assert rm.studio_in_use(P) == ""                              # idle after 10 minutes
    # a live gate runner lock pins Studio whatever the transcript says
    lock = os.path.join(root, ".plan", "tmp", "gate", "runner.lock")
    os.makedirs(os.path.dirname(lock))
    with open(lock, "w") as f:
        json.dump({"pid": os.getpid(), "sprint": "T1"}, f)
    assert rm.studio_in_use(P).startswith(f"gate runner pid {os.getpid()}")
    with open(lock, "w") as f:
        json.dump({"pid": 999_999_999, "sprint": "T1"}, f)          # dead pid: not in use
    assert rm.studio_in_use(P) == ""


def test_admission_rules_lease_bound_and_journal(tmp_path):
    c, P, clock, root = env(tmp_path)
    rm = c.tracks.resources
    # a main task about to start (READY) is a pending request: no lease
    c.approve("game", "T1", [], "victor")
    c.tick()
    assert c.store.task("game", "T1")["state"] in (S.READY, S.IMPLEMENTING)
    ok, why = rm.admit_studio(P, "blender", "merge gate")
    assert not ok and "main T1" in why
    rows = c.store.journal_rows("studio", "game")
    assert rows[-1]["event"] == "wait" and rows[-1]["owner"] == "track:blender"
    assert rm.admit_studio(P, "blender", "merge gate") == (False, why)
    assert len(c.store.journal_rows("studio", "game")) == len(rows)   # the same wait is not repeated
    c.cancel("game", "T1", "x", "victor")
    # the main session used Studio 5 minutes ago: still in use
    rm.note_studio_call(P, clock() - 300, "ses_1")
    ok, why = rm.admit_studio(P, "blender", "merge gate")
    assert not ok and "used Studio" in why
    clock.t += 6 * 60
    ok, why = rm.admit_studio(P, "blender", "merge gate")
    assert ok and why == ""
    lease = rm.lease(P)
    assert lease["track"] == "blender" and lease["until"] == clock() + 45 * 60
    assert lease["resources"] == ["studio", "studio:ws", "rojo:34872"]
    held = {l["resource"]: l["task_id"] for l in c.store.locks()}
    assert held["studio"] == held["studio:ws"] == held["rojo:34872"] == "track:blender"
    assert rm.admit_studio(P, "blender", "merge gate") == (True, "already held")
    # another track waits on the lease; the main sprint's own acquire sees the lock held
    ok, why = rm.admit_studio(P, "audio", "merge gate")
    assert not ok and "held by game/track:blender" in why
    assert c.store.acquire(["studio:ws"], "game", "T1") is not None
    # the lease runs over: reported, never interrupted
    clock.t += 50 * 60
    assert rm.lease_over(P) and rm.lease(P)["track"] == "blender"
    rm.release_studio(P, "blender", "gate PASS")
    assert rm.lease(P) is None and not [l for l in c.store.locks() if l["resource"] == "studio"]
    ev = c.store.journal_rows("studio", "game")
    kinds = [(r["owner"], r["event"]) for r in ev]
    assert ("track:blender", "acquire") in kinds and ("track:audio", "wait") in kinds
    rel = [r for r in ev if r["event"] == "release" and r["owner"] == "track:blender"][0]
    assert rel["detail"]["held_s"] == 50 * 60 and rel["detail"]["over_lease"] is True
    view = rm.studio_view(P)
    assert view["lease"] is None and view["idle_minutes"] == 10 and view["max_minutes"] == 45
    assert view["journal"][-1]["event"] == "release" and view["capacity"]["studio"] == 1


def test_main_session_in_verifying_blocks_admission(tmp_path):
    c, P, clock, root = env(tmp_path)
    rm = c.tracks.resources
    c.approve("game", "T1", [], "victor")
    c.store.transition("game", "T1", S.READY, {"deps_satisfied": ["(none)"]})
    c.store.transition("game", "T1", S.IMPLEMENTING, {"session_id": "s", "base_sha": "b",
                                                      "run_id": "r"}, session_id="s")
    assert rm.main_pending(P) == ""                              # editing files is not Studio
    c.store.transition("game", "T1", S.VERIFYING, {"head_sha": "h", "session_idle": True})
    ok, why = rm.admit_studio(P, "blender", "merge gate")
    assert not ok and "VERIFYING" in why


def test_track_capacity_resources_and_the_main_sprint_journal(tmp_path):
    c, P, clock, root = env(tmp_path)
    rm = c.tracks.resources
    assert rm.acquire_for_track(P, "b1", ["blender"]) is None
    held = rm.acquire_for_track(P, "b2", ["blender"])
    assert held and held["task_id"] == "track:b1"
    assert rm.acquire_for_track(P, "b2", ["backend"]) is None    # nothing to hold
    rm.release_for_track(P, "b1")
    assert rm.acquire_for_track(P, "b2", ["blender"]) is None
    rows = c.store.journal_rows("blender", "game")
    assert [(r["owner"], r["event"]) for r in rows] == [
        ("track:b1", "acquire"), ("track:b2", "wait"), ("track:b1", "release"),
        ("track:b2", "acquire")]
    # the engine's own Studio locks are journaled under the task
    t = {"task_id": "T1", "phase": "implement"}
    rm.on_locks(P, t, ["workspace:x", "studio:ws", "rojo:34872", "writer:0"], "acquire")
    assert [r["resource"] for r in c.store.journal_rows(None, "game")][-2:] == ["studio:ws",
                                                                              "rojo:34872"]
