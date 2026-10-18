"""Project-wide PAUSE / RESUME for operator upgrades (control actions
project_pause / project_resume): in "safe" mode a running model turn finishes
and nothing new starts (no session, no prompt to a primary, no review call, no
helper dispatch); "now" also pauses every writing task through the engine and
resumes the same session afterwards. The status the control center shows
(busy sessions, running helpers, running reviews, safe_to_upgrade) comes from
the store. Real git repositories, the generic adapter, a scripted coding client
(or the fake `kilo serve` for helpers) and a fake Foundry."""

import json
import time

import pytest

from foundry_coordinator import states as S
from foundry_coordinator.adapters import build_project
from foundry_coordinator.adapters.base import SessionState
from foundry_coordinator.adapters.kilo import KiloClient
from foundry_coordinator.adapters.scripted import ScriptedClient
from foundry_coordinator.control import ControlChannel
from foundry_coordinator.devtools.fake_kilo import FakeKilo
from foundry_coordinator.engine import Coordinator
from foundry_coordinator.profile import load
from foundry_coordinator.store import Store
from foundry_coordinator.telemetry import CoordReader, project_pause_status
from tests.coordinator.helpers import (FakeFoundry, by_task, coordinator, make_repo, run_until,
                                       write_ok, write_profile)

T1 = {"id": "T1", "title": "Make the thing", "spec": "Write src/a.txt containing OK.",
      "scope": ["src/a.txt"]}
T2 = {"id": "T2", "title": "Other thing", "spec": "Write src/b.txt containing OK.",
      "scope": ["src/b.txt"]}


class Gate(ScriptedClient):
    """A scripted coding client whose turn stays busy until `go` (the model is
    still working), with every session start, prompt and abort recorded."""

    def __init__(self, script):
        super().__init__(script, project_id="game")
        self.go = True
        self.started, self.aborted = [], []
        self.compacting = set()

    def start_session(self, **kw):
        sid = super().start_session(**kw)
        self.started.append((sid, kw["metadata"].get("phase"), kw["metadata"].get("task")))
        return sid

    def state(self, sid, *, directory, since_ts):
        if sid in self.compacting:
            return SessionState(status="busy", compaction={"message_id": "m-compact"})
        s = self.sessions.get(sid)
        if s is not None and s.get("pending") and not self.go:
            return SessionState(status="busy")
        return super().state(sid, directory=directory, since_ts=since_ts)

    def abort(self, sid, *, directory):
        self.aborted.append(sid)
        if sid in self.sessions:
            self.sessions[sid]["pending"] = False

    def prompts(self, sid):
        return self.sessions[sid]["prompts"]


class Ctl:
    """Foundry's control endpoints for one ControlChannel: actions handed out
    once, every report and heartbeat recorded."""

    def __init__(self):
        self.queue, self.reports, self.beats = [], [], []

    def heartbeat(self, p):
        self.beats.append(p)

    def claim_actions(self, source_id, projects, limit=20):
        q, self.queue = self.queue, []
        return q

    def action_status(self, action_id, source_id, status, detail, result=None):
        self.reports.append({"action_id": action_id, "status": status, "detail": detail,
                             "result": result or {}})


_n = [0]


def send(ch, kind, project="game", task="", actor="victor", **payload):
    """Hand one action to the channel and return its report."""
    _n[0] += 1
    aid = f"act-{_n[0]}"
    ch.foundry.queue.append({"action_id": aid, "kind": kind, "project_id": project,
                             "task_id": task, "actor": actor, "payload": payload,
                             "expected": {}})
    ch.poll()
    return [r for r in ch.foundry.reports if r["action_id"] == aid][-1]


def state(c, tid="T1"):
    return c.store.task("game", tid)["state"]


def status(c):
    return project_pause_status(c.store.q, "game")


@pytest.fixture()
def game(tmp_path):
    root, _ = make_repo(tmp_path, "game", [T1, T2])
    prof = write_profile(tmp_path, "game", root)
    ff = FakeFoundry()
    client = Gate(by_task({"T1": write_ok("src/a.txt"), "T2": write_ok("src/b.txt")}))

    def make(max_writers=1):
        c = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": ff},
                        max_writers=max_writers)
        return c, ControlChannel(c, foundry=Ctl(), source_id="coordinator:test",
                                 heartbeat_every_s=0)
    return {"make": make, "client": client, "ff": ff, "root": root, "prof": prof,
            "tmp": tmp_path}


def _implementing(g, busy=True):
    c, ch = g["make"]()
    g["client"].go = not busy
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: state(c) == S.IMPLEMENTING)
    return c, ch, c.store.task("game", "T1")["session_id"]


# -- safe ------------------------------------------------------------------------------

def test_safe_pause_lets_the_turn_finish_then_starts_nothing_until_resume(game):
    c, ch, sid = _implementing(game)
    client, ff = game["client"], game["ff"]
    r = send(ch, "project_pause", mode="safe", note="upgrading to v0.123")
    assert r["status"] == "applied" and "paused (safe)" in r["detail"]
    st = r["result"]["project_pause"]
    assert st["paused"] and st["mode"] == "safe" and st["note"] == "upgrading to v0.123"
    assert st["actor"] == "victor (control center)" and st["since"]
    # the turn is still running: not safe yet, and it is not aborted
    assert st["busy_sessions"] == 1 and not st["safe_to_upgrade"]
    for _ in range(3):
        c.tick()
    assert client.aborted == [] and state(c) == S.IMPLEMENTING and status(c)["busy_sessions"] == 1
    # the model finishes its turn
    client.go = True
    for _ in range(6):
        c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.IMPLEMENTING and "project paused" in t["next_action"]
    assert ff.chats == []                                   # no review call
    assert [s for s, *_ in client.started] == [sid]          # no new session
    assert len(client.prompts(sid)) == 1                     # no continuation prompt
    st = status(c)
    assert st == {**st, "busy_sessions": 0, "running_helpers": 0, "running_reviews": 0,
                  "safe_to_upgrade": True}
    beat = ch._beat()
    assert beat["project_pause"]["game"]["safe_to_upgrade"] is True
    # resume: the held handoff runs and the task completes
    r = send(ch, "project_resume")
    assert r["status"] == "applied" and "resumed" in r["detail"]
    assert not status(c)["paused"] and c.store.meta("project_pause:game") is None
    assert run_until(c, lambda: state(c) == S.COMPLETE)
    assert len(ff.chats) == 1
    assert [p for _, p, _ in client.started] == ["implement"]


def test_a_paused_project_starts_no_review_for_a_project_without_lifecycle(game):
    c, ch = game["make"]()
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: state(c) == S.REVIEWING)
    assert send(ch, "project_pause")["status"] == "applied"
    for _ in range(3):
        c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.REVIEWING and game["ff"].chats == []
    assert "review waits" in t["next_action"]
    assert status(c)["safe_to_upgrade"]
    send(ch, "project_resume")
    assert run_until(c, lambda: state(c) == S.COMPLETE) and len(game["ff"].chats) == 1


def test_no_closeout_session_starts_while_paused(game):
    c, ch = game["make"]()
    c.projects["game"].profile.closeout["mode"] = "session"
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: state(c) == S.CLOSING)
    send(ch, "project_pause")
    for _ in range(3):
        c.tick()
    assert [p for _, p, _ in game["client"].started] == ["implement"]
    assert "closeout session start waits" in c.store.task("game", "T1")["next_action"]
    send(ch, "project_resume")
    c.tick()
    assert [p for _, p, _ in game["client"].started] == ["implement", "closeout"]


def test_no_compaction_nudge_while_paused(game, monkeypatch):
    clock = [time.time()]
    c, ch, sid = _implementing(game)
    c.clock = lambda: clock[0]
    client = game["client"]
    client.compacting.add(sid)
    c.tick()                                                 # compaction seen
    send(ch, "project_pause")
    clock[0] += 120                                          # past the grace period
    for _ in range(3):
        c.tick()
    assert len(client.prompts(sid)) == 1
    assert "compaction nudge waits" in c.store.task("game", "T1")["next_action"]
    send(ch, "project_resume")
    c.tick()
    assert len(client.prompts(sid)) == 2                     # the nudge, after the resume


def test_budget_retry_time_does_not_auto_resume_a_paused_project(game):
    c, ch, sid = _implementing(game)
    c.pause("game", "T1", "budget")
    c.store.update_task("game", "T1", pause_until=time.time() - 1)
    send(ch, "project_pause")
    c.tick()
    assert state(c) == S.PAUSED and len(game["client"].prompts(sid)) == 1
    send(ch, "project_resume")
    c.tick()
    assert state(c) == S.IMPLEMENTING and len(game["client"].prompts(sid)) == 2


# -- now -------------------------------------------------------------------------------

def test_now_pause_aborts_the_session_keeps_state_and_resume_continues_it(game):
    c, ch, sid = _implementing(game)
    client = game["client"]
    r = send(ch, "project_pause", mode="now", note="hotfix")
    assert r["status"] == "applied" and r["result"]["paused_tasks"] == ["T1"]
    t = c.store.task("game", "T1")
    assert client.aborted == [sid] and t["state"] == S.PAUSED
    assert t["resume_state"] == S.IMPLEMENTING and t["data"]["resume_session"] == sid
    assert t["data"]["paused_by_project"] is True
    st = status(c)
    assert st["mode"] == "now" and st["busy_sessions"] == 0 and st["safe_to_upgrade"]
    for _ in range(3):
        c.tick()
    assert state(c) == S.PAUSED and [s for s, *_ in client.started] == [sid]
    r = send(ch, "project_resume")
    assert r["status"] == "applied" and r["result"]["resumed"] == ["T1 -> IMPLEMENTING"]
    t = c.store.task("game", "T1")
    assert t["state"] == S.IMPLEMENTING and t["session_id"] == sid
    assert "paused_by_project" not in t["data"]
    assert len(client.prompts(sid)) == 2                     # continue, same session
    client.go = True
    assert run_until(c, lambda: state(c) == S.COMPLETE)
    assert [s for s, *_ in client.started] == [sid]


def test_project_resume_leaves_an_individually_paused_task_alone(game):
    c, ch, sid = _implementing(game)
    client = game["client"]
    assert send(ch, "pause", task="T1", reason="look at it")["status"] == "applied"
    r = send(ch, "project_pause", mode="now")
    assert r["result"]["paused_tasks"] == []                  # T1 was not writing any more
    assert not c.store.task("game", "T1")["data"].get("paused_by_project")
    # an individual resume while the project is paused would start new work
    r = send(ch, "resume", task="T1")
    assert r["status"] == "rejected" and "project game is paused" in r["detail"]
    r = send(ch, "project_resume")
    assert r["status"] == "applied" and r["result"]["left_paused"] == ["T1"]
    assert r["result"]["resumed"] == []
    for _ in range(3):
        c.tick()
    assert state(c) == S.PAUSED and len(client.prompts(sid)) == 1
    # the operator's own resume still works once the project runs again
    assert send(ch, "resume", task="T1")["status"] == "applied"
    assert state(c) == S.IMPLEMENTING and len(client.prompts(sid)) == 2


def test_project_resume_resumes_only_the_tasks_it_paused(game):
    """A project-paused task and an individually paused one side by side (the
    second one parked in PAUSED from READY: no session, no workspace held)."""
    c, ch, sid = _implementing(game)
    c.approve("game", "T2", [], "victor")
    c.tick()
    c.store.transition("game", "T2", S.READY, {"deps_satisfied": ["(none)"]}) \
        if state(c, "T2") == S.QUEUED else None
    c.pause("game", "T2", "operator hold", actor="victor")
    r = send(ch, "project_pause", mode="now")
    assert r["result"]["paused_tasks"] == ["T1"]
    r = send(ch, "project_resume")
    assert r["result"]["resumed"] == ["T1 -> IMPLEMENTING"]
    assert r["result"]["left_paused"] == ["T2"]
    assert state(c) == S.IMPLEMENTING and state(c, "T2") == S.PAUSED


def test_pause_and_steer_is_refused_while_the_project_is_paused(game):
    c, ch, sid = _implementing(game)
    send(ch, "project_pause")
    r = send(ch, "instruction", task="T1", text="use the blue palette",
             delivery="pause_and_steer")
    assert r["status"] == "rejected" and "paused" in r["detail"]
    assert game["client"].aborted == [] and len(game["client"].prompts(sid)) == 1


# -- restart -----------------------------------------------------------------------------

def test_a_restart_while_paused_starts_nothing(game):
    c, ch, sid = _implementing(game)
    client, ff = game["client"], game["ff"]
    send(ch, "project_pause", note="upgrade")
    client.go = True
    c.approve("game", "T2", [], "victor")
    for _ in range(4):
        c.tick()
    del c, ch                                                # the coordinator is upgraded
    c2, ch2 = game["make"]()
    for _ in range(6):
        c2.tick()
    assert [s for s, *_ in client.started] == [sid] and ff.chats == []
    assert len(client.prompts(sid)) == 1
    assert state(c2, "T2") == S.QUEUED and state(c2) == S.IMPLEMENTING
    st = ch2._beat()["project_pause"]["game"]
    assert st["paused"] and st["note"] == "upgrade" and st["safe_to_upgrade"]
    assert send(ch2, "project_resume")["status"] == "applied"
    assert run_until(c2, lambda: state(c2) == state(c2, "T2") == S.COMPLETE, limit=60)


# -- the action contract ---------------------------------------------------------------------

def test_unknown_project_and_bad_mode_are_rejected(game):
    c, ch = game["make"]()
    r = send(ch, "project_pause", project="nope")
    assert r["status"] == "rejected" and "nope" in r["detail"]
    r = send(ch, "project_pause", mode="later")
    assert r["status"] == "rejected" and "mode" in r["detail"]
    assert not status(c)["paused"]
    r = send(ch, "project_resume")
    assert r["status"] == "rejected" and "not paused" in r["detail"]


def test_an_applied_action_is_never_applied_twice(game):
    c, ch, sid = _implementing(game)
    a = {"action_id": "dup-1", "kind": "project_pause", "project_id": "game", "task_id": "",
         "actor": "victor", "payload": {"mode": "now"}, "expected": {}}
    ch.foundry.queue.append(dict(a))
    ch.poll()
    send(ch, "project_resume")
    ch.foundry.queue.append(dict(a))                         # Foundry offers it again
    ch.poll()
    assert [r["status"] for r in ch.foundry.reports if r["action_id"] == "dup-1"] == \
        ["applied", "applied"]
    assert not status(c)["paused"] and state(c) == S.IMPLEMENTING
    assert game["client"].aborted == [sid]


def test_heartbeat_declares_project_pause(game):
    c, ch = game["make"]()
    ch.poll()
    beat = ch.foundry.beats[-1]
    assert "project_pause" in beat["features"]
    assert {"project_pause", "project_resume"} <= set(beat["source"]["capabilities"]["actions"])
    assert beat["project_pause"]["game"] == {
        "paused": False, "mode": None, "note": "", "actor": None, "since": None,
        "busy_sessions": 0, "running_helpers": 0, "running_reviews": 0,
        "safe_to_upgrade": False}


def test_the_read_only_bridge_reader_reports_the_same_status(game):
    c, ch, sid = _implementing(game)
    send(ch, "project_pause", note="n")
    reader = CoordReader(c.store.path)
    assert project_pause_status(reader._q, "game") == status(c)
    # a review call in flight counts; a crash leftover from long ago does not
    c.store.op_begin("review:game:T1:x:reviewer:0:p2", "game", "T1", "review")
    assert status(c)["running_reviews"] == 1
    c.store.x("UPDATE ops SET created_ts=0, updated_ts=0 WHERE kind='review'")
    assert status(c)["running_reviews"] == 0


def test_cli_pause_without_a_record_reads_as_safe(game):
    c, ch = game["make"]()
    c.store.set_project_paused("game", True, "from the cli")
    st = status(c)
    assert st["paused"] and st["mode"] == "safe" and st["note"] == "from the cli"
    assert send(ch, "project_resume")["status"] == "applied"


# -- helpers ---------------------------------------------------------------------------------

HELPERS = """
[helpers]
enabled = true
order = ["meridian"]
agent = "au-helper"
worker_client = "kilo"
max_running = 3
poll_seconds = 30

[helpers.personas]
meridian = "Foundry-Kilo-Helper-Claude"
"""
HT1 = {"id": "T1", "title": "UI frames", "spec": "Do it.", "scope": ["src/a.txt", "src/tests/*"],
       "helpers": [{"responsibility": "S45 timing test design", "kind": "test_design",
                    "prompt": "Design the S45 ordering test.", "files": ["src/tests/s45.lua"]}]}


def _admit():
    return {"admit": True, "state": "admit", "reason": "fixture", "next_eligible": None,
            "routes": [{"source": "fixture"}]}


@pytest.fixture()
def kilo(tmp_path):
    fake = FakeKilo()
    url = fake.start()
    root, _ = make_repo(tmp_path, "game", [HT1])
    prof = load(str(write_profile(tmp_path, "game", root, extra=HELPERS)))
    clock = [time.time()]
    ff = FakeFoundry()
    state_dir = str(tmp_path / "state")
    project = build_project(prof, state_dir, client=KiloClient(url, password=fake.password),
                            foundry=ff)
    c = Coordinator(Store(str(tmp_path / "state" / "c.sqlite")), [project], state_dir,
                    clock=lambda: clock[0])
    c.approve("game", "T1", [], "victor")
    c.tick()
    c.tick()
    assert c.store.task("game", "T1")["state"] == "IMPLEMENTING"
    ch = ControlChannel(c, foundry=Ctl(), source_id="coordinator:test", clock=lambda: clock[0])
    yield {"fake": fake, "c": c, "ff": ff, "clock": clock, "ch": ch}
    fake.stop()


def _helper_sessions(fake, c):
    primary = c.store.task("game", "T1")["session_id"]
    return [s for s in fake.sessions if s != primary]


def test_no_helper_dispatch_while_paused_and_now_stops_running_helpers(kilo):
    fake, c, ff, clock, ch = (kilo[k] for k in ("fake", "c", "ff", "clock", "ch"))
    assert send(ch, "project_pause")["status"] == "applied"
    ff.admission["Foundry-Kilo-Helper-Claude"] = _admit()
    clock[0] += 31
    for _ in range(3):
        c.tick()
    assert _helper_sessions(fake, c) == []
    assert {u["status"] for u in c.helpers.units("game", "T1")} <= {"pending", "deferred"}
    send(ch, "project_resume")
    c.tick()
    assert len(_helper_sessions(fake, c)) == 1
    # a running helper keeps the project from being safe to upgrade
    send(ch, "project_pause")
    st = status(c)
    assert st["running_helpers"] == 1 and not st["safe_to_upgrade"]
    r = send(ch, "project_pause", mode="now")
    assert r["status"] == "applied" and "already paused" in r["detail"]
    assert {u["status"] for u in c.helpers.units("game", "T1")} == {"cancelled"}
    st = status(c)
    assert st["mode"] == "now" and st["running_helpers"] == 0 and st["busy_sessions"] == 0
    assert st["safe_to_upgrade"]


# -- readiness: only active work counts ------------------------------------------------------

def test_safe_to_upgrade_ignores_locks_held_by_an_idle_task_but_not_a_running_turn(game):
    """A task held idle by the pause keeps its workspace reservation and writer
    lock between sessions; only a running writer turn keeps the upgrade waiting."""
    c, ch, sid = _implementing(game)
    client = game["client"]
    send(ch, "project_pause", note="upgrade")
    locks = c.store.q("SELECT resource FROM locks WHERE project_id='game' AND task_id='T1'")
    assert locks                                             # held by the running task
    st = status(c)
    assert st["busy_sessions"] == 1 and st["safe_to_upgrade"] is False
    client.go = True                                         # the turn finishes
    for _ in range(3):
        c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.IMPLEMENTING and c.store.session(sid)["ended_ts"]
    held = c.store.q("SELECT resource FROM locks WHERE project_id='game' AND task_id='T1'")
    assert held == locks                                     # still reserved while idle
    st = status(c)
    assert st["busy_sessions"] == 0 and st["safe_to_upgrade"] is True
    assert ch._beat()["project_pause"]["game"]["safe_to_upgrade"] is True


def test_an_in_flight_retry_guidance_call_keeps_the_upgrade_waiting(game):
    c, ch = game["make"]()
    send(ch, "project_pause", note="upgrade")
    assert status(c)["safe_to_upgrade"] is True
    c.store.add_session("retry-guidance-x", "game", "T1", "r", "helper", "retry_guidance", 1,
                        "foundry-direct", "Frontier")
    st = status(c)
    assert st["running_helpers"] == 1 and st["safe_to_upgrade"] is False
    c.store.update_session("retry-guidance-x", status="ended", ended_ts=time.time())
    assert status(c)["safe_to_upgrade"] is True


# -- restart heartbeat and resume after an upgrade -------------------------------------------

def _build(monkeypatch, version, fingerprint="f" * 40):
    import foundry_coordinator.control as control
    monkeypatch.setattr(control, "build_identity", lambda: {
        "version": version, "coordinator_version": "0.122.0", "fingerprint": fingerprint})


def test_first_heartbeat_after_a_restart_reports_the_pause(game, monkeypatch):
    _build(monkeypatch, "0.122.4")
    c, ch, sid = _implementing(game)
    send(ch, "project_pause", mode="now", note="upgrade", resume_after_upgrade=True)
    del c, ch
    c2, ch2 = game["make"]()                                 # same store, new process
    ch2.poll()
    beat = ch2.foundry.beats[0]
    assert "project_pause" in beat["features"]
    assert {"project_pause", "project_resume"} <= set(beat["source"]["capabilities"]["actions"])
    st = beat["project_pause"]["game"]
    assert st["paused"] is True and st["mode"] == "now" and st["note"] == "upgrade"
    assert st["actor"] == "victor (control center)" and st["since"]
    assert st["safe_to_upgrade"] is True and st["resume_after_upgrade"] is True
    assert st["paused_version"] == "0.122.4" and "upgrade_resume" not in st
    assert state(c2) == S.PAUSED


def test_restart_on_the_same_build_stays_paused(game, monkeypatch):
    _build(monkeypatch, "0.122.4")
    c, ch, sid = _implementing(game)
    send(ch, "project_pause", mode="now", note="upgrade", resume_after_upgrade=True)
    del c, ch
    c2, ch2 = game["make"]()
    ch2.poll()
    for _ in range(3):
        c2.tick()
    assert status(c2)["paused"] and state(c2) == S.PAUSED
    assert len(game["client"].prompts(sid)) == 1
    assert c2.store.meta("project_upgrade_resume:game") is None
    assert not [e for e in c2.store.events("game") if e["kind"] == "project_resumed_after_upgrade"]


def test_restart_on_a_new_build_resumes_only_the_project_paused_tasks(game, monkeypatch):
    _build(monkeypatch, "0.122.4")
    c, ch, sid = _implementing(game)
    c.approve("game", "T2", [], "victor")
    c.tick()
    if state(c, "T2") == S.QUEUED:
        c.store.transition("game", "T2", S.READY, {"deps_satisfied": ["(none)"]})
    c.pause("game", "T2", "operator hold", actor="victor")      # paused individually
    r = send(ch, "project_pause", mode="now", note="upgrading", resume_after_upgrade=True)
    assert r["result"]["paused_tasks"] == ["T1"]
    meta = c.store.meta("project_pause:game")
    assert '"resume_after_upgrade": true' in meta and '"0.122.4"' in meta
    del c, ch
    _build(monkeypatch, "0.122.5")                            # the operator upgraded
    c2, ch2 = game["make"]()
    ch2.poll()
    assert not status(c2)["paused"] and c2.store.meta("project_pause:game") is None
    t1 = c2.store.task("game", "T1")
    assert t1["state"] == S.IMPLEMENTING and t1["session_id"] == sid
    assert "paused_by_project" not in t1["data"]
    assert len(game["client"].prompts(sid)) == 2              # continued, same session
    assert state(c2, "T2") == S.PAUSED                        # left alone
    actor = "coordinator (after upgrade, authorized by victor (control center))"
    assert c2.store.history("game", "T1")[-1]["actor"] == actor
    ev = [e for e in c2.store.events("game") if e["kind"] == "project_resumed_after_upgrade"]
    assert len(ev) == 1 and ev[0]["detail"]["to_version"] == "0.122.5"
    assert ev[0]["detail"]["from_version"] == "0.122.4"
    assert ev[0]["detail"]["resumed"] == ["T1 -> IMPLEMENTING"]
    assert ev[0]["detail"]["left_paused"] == ["T2"]
    up = ch2.foundry.beats[0]["project_pause"]["game"]["upgrade_resume"]
    assert up["to_version"] == "0.122.5" and up["actor"] == actor
    assert up["authorized_by"] == "victor (control center)"
    # only once: a later poll does nothing more
    ch2.poll()
    assert len(game["client"].prompts(sid)) == 2
    # a new pause clears the old report
    send(ch2, "project_pause", note="again")
    assert "upgrade_resume" not in status(c2)


def test_a_new_commit_on_the_same_version_counts_as_an_upgrade(game, monkeypatch):
    _build(monkeypatch, "0.122.4", fingerprint="a" * 40)
    c, ch = game["make"]()
    send(ch, "project_pause", note="hotfix", resume_after_upgrade=True)
    del c, ch
    _build(monkeypatch, "0.122.4", fingerprint="b" * 40)
    c2, ch2 = game["make"]()
    ch2.poll()
    assert not status(c2)["paused"]


def test_restart_on_a_new_build_without_the_option_stays_paused(game, monkeypatch):
    _build(monkeypatch, "0.122.4")
    c, ch, sid = _implementing(game)
    send(ch, "project_pause", mode="now", note="upgrade")
    assert '"resume_after_upgrade": false' in c.store.meta("project_pause:game")
    del c, ch
    _build(monkeypatch, "0.122.5")
    c2, ch2 = game["make"]()
    ch2.poll()
    for _ in range(3):
        c2.tick()
    assert status(c2)["paused"] and state(c2) == S.PAUSED
    assert status(c2)["resume_after_upgrade"] is False
    assert len(game["client"].prompts(sid)) == 1


def test_a_cli_pause_or_an_older_record_is_never_auto_resumed(game, monkeypatch):
    """No recorded build (a pause from before this feature, or the CLI's flag
    alone): never an upgrade, even with the option set by hand."""
    c, ch = game["make"]()
    c.store.set_project_paused("game", True, "cli")
    c.store.set_meta("project_pause:game", json.dumps({"mode": "safe",
                                                       "resume_after_upgrade": True}))
    _build(monkeypatch, "9.9.9")
    c2, ch2 = game["make"]()
    ch2.poll()
    assert status(c2)["paused"]
    from foundry_coordinator.control import upgraded
    assert not upgraded({}, {"version": "1"}) and not upgraded({"version": "1"}, {"version": ""})
    assert upgraded({"version": "1"}, {"version": "2"})
    assert not upgraded({"version": "1", "fingerprint": "a"}, {"version": "1", "fingerprint": "a"})


def test_build_identity_reads_the_checkout(tmp_path):
    from foundry_coordinator.control import _git_head, build_identity
    b = build_identity()
    assert b["version"] and b["coordinator_version"]
    assert _git_head(str(tmp_path)) == ""                    # not a checkout
    root, _ = make_repo(tmp_path, "g2", [T1])
    assert len(_git_head(str(root))) == 40
