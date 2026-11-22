"""The crash-loop guard (crashguard.py, item 4) and the check after the router comes back
(selfheal.after_router_back, item 8) of COORDINATOR_AUTOMATION_NEEDS.

11-16 and 11-18: the coordinator restarted every ~6.5 minutes because one step of a tick ran
past the heartbeat limit (an evidence rewrite; one git call per commit over 10,573 commits),
and nothing counted the restarts or remembered the step. 11-21 14:00: the network router
was rebooted and nothing said afterwards which sessions had gone on."""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

from foundry_coordinator import crashguard, keepalive, selfheal
from foundry_coordinator import states as S
from foundry_coordinator.adapters.foundry import FoundryError
from foundry_coordinator.control import ControlChannel
from tests.coordinator.test_budget_policy import game, task


@pytest.fixture(autouse=True)
def _clean():
    crashguard._cur = None
    crashguard._cmd.clear()
    crashguard._written["path"] = ""
    keepalive.reset()
    yield
    crashguard._cur = None
    keepalive.reset()


def events(c, kind):
    return [json.loads(r["detail"]) for r in c.store.q(
        "SELECT detail FROM events WHERE kind=? ORDER BY id", (kind,))]


def notices(c, kind):
    return [d for d in events(c, "operator_notice") if d.get("kind") == kind]


def leftover(state_dir, uncovered_s=400, task_id="T1", kind="task", label="", command="",
             seconds=420, state=S.VERIFYING):
    """The mark a process killed mid-step leaves behind."""
    doc = {"pid": 4242, "ts": time.time(), "kind": kind, "project": "game" if task_id else "",
           "task": task_id, "state": state if task_id else "", "since": time.time() - seconds,
           "seconds": seconds, "label": label, "uncovered_s": uncovered_s, "command": command}
    doc["key"] = crashguard.key_of(doc)
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, crashguard.STEP_FILE), "w", encoding="utf-8") as f:
        json.dump(doc, f)
    return doc


# ------------------------------------------------------------------ the mark

def test_a_slow_step_is_marked_with_its_cover_and_command_and_cleared_when_it_ends(tmp_path):
    sd = str(tmp_path)
    path = tmp_path / crashguard.STEP_FILE
    assert crashguard.write(sd) is False and not path.exists()        # nothing is running
    with crashguard.step("task", "game", "POL-7", S.VERIFYING):
        t0 = crashguard.current()["since"]
        assert crashguard.write(sd, None, now=t0 + 10) is False       # not slow yet
        crashguard.note_command(["git", "-C", "C:/Roblox/AU", "log", "--format=%H", "a..b"])
        with keepalive.busy("verify POL-7 checks", 600):
            assert crashguard.write(sd, keepalive.current(), now=t0 + 45) is True
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert (doc["kind"], doc["project"], doc["task"], doc["state"]) == (
            "task", "game", "POL-7", S.VERIFYING)
        assert doc["seconds"] == 45 and doc["label"] == "verify POL-7 checks"
        assert doc["uncovered_s"] == 0 and doc["command"].startswith("git -C C:/Roblox/AU log")
        # the cover lapsed: from here the step counts as uncovered
        assert crashguard.write(sd, None, now=t0 + 60) is True
        assert crashguard.write(sd, None, now=t0 + 200) is True
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["label"] == "" and 140 <= doc["uncovered_s"] <= 160 and doc["seconds"] == 200
        assert doc["key"] == "task|game|POL-7|VERIFYING"
    assert not path.exists() and crashguard.current() is None         # the step ended


def test_steps_nest_and_a_command_older_than_the_step_is_not_blamed(tmp_path):
    crashguard.note_command(["git", "status"])
    time.sleep(0.01)
    with crashguard.step("tracks", "game"):
        with crashguard.step("task", "game", "T1", S.CLOSING):
            assert crashguard.current()["task"] == "T1"
        cur = crashguard.current()
        assert cur["kind"] == "tracks" and cur["task"] == ""
        assert crashguard.write(str(tmp_path), None, now=cur["since"] + 60) is True
        doc = json.loads((tmp_path / crashguard.STEP_FILE).read_text(encoding="utf-8"))
        assert doc["command"] == ""                                   # it ran before the step


# ------------------------------------------------------------------ at the next start

def test_replay_the_same_step_twice_in_a_row_alerts_and_holds_the_sprint(tmp_path):
    c, P, clk = game(tmp_path)
    c.store.update_task("game", "T1", state=S.VERIFYING)
    sd = str(tmp_path / "state")
    cmd = "git -C C:/Roblox/AnimalUprising log --merges --format=%H 1a2b3c..HEAD"
    leftover(sd, command=cmd)
    got = crashguard.on_start(c, sd)
    assert got["n"] == 1 and got["counted"] and not got["held"]
    assert not os.path.exists(os.path.join(sd, crashguard.STEP_FILE))    # read once
    assert task(c)["state"] == S.VERIFYING and notices(c, "crash_loop") == []
    assert events(c, "crash_guard_seen")[0]["what"] == "T1's VERIFYING"
    # the new process walks into the same step and is restarted again
    leftover(sd, command=cmd)
    got = crashguard.on_start(c, sd)
    assert got["n"] == 2 and got["held"] is True
    t = task(c)
    assert t["state"] == S.PAUSED and t["data"]["hold"]["owner"] == f"crash_guard:{crashguard._version()}"
    [n] = notices(c, "crash_loop")
    assert n["alert_class"] == "watch" and n["held"] is True
    assert n["title"] == "the coordinator restarted 2 times inside the same step"
    assert "T1's VERIFYING, running 420 s with no keep-alive cover" in n["text"]
    assert f"last command: {cmd}" in n["text"] and "T1 is held so the loop ends" in n["text"]
    # the hold lasts while this release runs, and ends with the next one
    assert selfheal.owner_alive(c, t["data"]["hold"]["owner"]) is True
    assert selfheal.owner_alive(c, "crash_guard:0.0.1") is False


def test_a_clean_start_or_another_step_ends_the_streak(tmp_path):
    c, P, clk = game(tmp_path)
    sd = str(tmp_path / "state")
    leftover(sd)
    assert crashguard.on_start(c, sd)["n"] == 1
    assert crashguard.on_start(c, sd) is None                 # a start with no mark: over
    leftover(sd)
    assert crashguard.on_start(c, sd)["n"] == 1               # counted from one again
    leftover(sd, kind="tracks", task_id="")
    assert crashguard.on_start(c, sd)["n"] == 1               # a different step
    assert notices(c, "crash_loop") == []


def test_a_covered_long_step_cut_by_a_deploy_is_not_a_hang(tmp_path):
    """A 10-minute verify under keep-alive cover, restarted for a deploy twice: no hang, no
    alert, no hold."""
    c, P, clk = game(tmp_path)
    c.store.update_task("game", "T1", state=S.VERIFYING)
    sd = str(tmp_path / "state")
    for _ in range(3):
        leftover(sd, uncovered_s=0, label="verify T1 checks", seconds=500)
        assert crashguard.on_start(c, sd) is None
    assert task(c)["state"] == S.VERIFYING and notices(c, "crash_loop") == []
    # garbage in the file never stops the start
    with open(os.path.join(sd, crashguard.STEP_FILE), "w", encoding="utf-8") as f:
        f.write("{not json")
    assert crashguard.on_start(c, sd) is None


def test_a_pass_of_the_coordinators_own_is_alerted_but_cannot_be_skipped(tmp_path):
    c, P, clk = game(tmp_path)
    sd = str(tmp_path / "state")
    for _ in range(2):
        leftover(sd, kind="after-tick Telemetry.tick", task_id="", label="")
        got = crashguard.on_start(c, sd)
    assert got["n"] == 2 and got["held"] is False
    [n] = notices(c, "crash_loop")
    assert "the coordinator's own pass 'after-tick Telemetry.tick'" in n["text"]
    assert "cannot be skipped" in n["text"] and n["held"] is False
    assert task(c)["state"] == S.READY


def test_the_engine_names_its_steps_and_the_git_runner_its_command(tmp_path, monkeypatch):
    c, P, clk = game(tmp_path)
    seen = []
    real = c._step

    def spy(P_, t):
        seen.append(dict(crashguard.current() or {}))
        return real(P_, t)
    monkeypatch.setattr(c, "_step", spy)
    c.tick()
    assert seen and seen[0]["kind"] == "task" and seen[0]["task"] == "T1"
    assert seen[0]["project"] == "game" and seen[0]["state"] == S.READY
    assert crashguard.current() is None                       # nothing is left open
    P.ws.run("status", "--porcelain")
    assert crashguard._cmd["text"].startswith("git -C ") and "status --porcelain" in crashguard._cmd["text"]


def test_the_keepalive_thread_writes_the_mark_and_the_start_reads_it(tmp_path):
    from foundry_coordinator import cli
    c, P, clk = game(tmp_path)
    sd = str(tmp_path / "pf")
    leftover(sd)
    life = cli._ProcessFiles(sd, c)
    try:
        assert life.crash["n"] == 1 and life.state_dir == sd
        with crashguard.step("task", "game", "T1", S.VERIFYING):
            crashguard._cur["since"] -= 120
            cover = keepalive.current()
            crashguard.write(life.state_dir, cover)
            assert os.path.exists(os.path.join(sd, crashguard.STEP_FILE))
    finally:
        life._stop_keepalive.set()


# ------------------------------------------------------------------ after a router outage

class Router:
    def __init__(self):
        self.down = False

    def claim_actions(self, source_id, projects):
        if self.down:
            raise FoundryError("POST /v1/foundry/control/claim: connection refused")
        return []

    def heartbeat(self, body):
        if self.down:
            raise FoundryError("heartbeat: connection refused")

    def action_status(self, *a, **k):
        pass


def test_replay_the_router_comes_back_and_every_live_session_is_checked_and_reported(tmp_path):
    """11-21 14:00-14:01: the coordinator lost the router (heartbeat, claim and ingest
    failures). Afterwards somebody had to look at each session to see that it had gone on."""
    c, P, clk = game(tmp_path)
    ff = Router()
    ch = ControlChannel(c, ff, source_id="coordinator:test", log=lambda *_: None, clock=clk)
    ch.poll()
    assert "_router_back" not in c.__dict__
    ff.down = True
    ch.poll()
    clk.advance(70)
    ch.poll()
    assert "_router_back" not in c.__dict__                   # still away
    ff.down = False
    ch.poll()
    rb = c.__dict__["_router_back"]
    assert rb["down_s"] == 70 and "connection refused" in rb["why"]
    assert events(c, "router_back")[0]["down_s"] == 70
    # a live session Kilo still runs, and one that died in the outage
    c.store.update_task("game", "T1", state=S.IMPLEMENTING, session_id="ses_alive")
    c.store.x("INSERT INTO sessions (session_id, project_id, task_id, run_id, role, phase, "
              "attempt, client, persona, status, created_ts, data) VALUES "
              "(?,?,?,?,?,?,?,?,?,?,?,?)", ("ses_alive", "game", "T1", "r1", "implementer",
                                            "implement", 1, "kilo", "p", "running", clk.t, "{}"))
    states = {"ses_alive": SimpleNamespace(status="busy", stalled=False, denial=None, error="")}
    P.client.state = lambda sid, directory="", since_ts=0: states[sid]
    swept = []
    real_sweep = selfheal.sweep

    def sweep(c_, P_, force=False):
        swept.append((clk.t, force))
        return real_sweep(c_, P_, force=force)
    selfheal.sweep, keep = sweep, selfheal.sweep
    try:
        assert selfheal.after_router_back(c) is None          # the first check, at once
        assert swept[-1][1] is True and c.__dict__["_router_back"]["stage"] == 1
        clk.advance(60)
        assert selfheal.after_router_back(c) is None          # the grace has not passed
        assert len(swept) == 1
        clk.advance(90)
        report = selfheal.after_router_back(c)
    finally:
        selfheal.sweep = keep
    assert len(swept) == 2 and "_router_back" not in c.__dict__
    assert report["down_s"] == 70 and report["digest"] is True
    assert report["sessions"] == [{"task": "T1", "session": "ses_alive", "role": "implementer",
                                   "kilo": "busy"}]
    assert report["text"] == ("The router was unreachable for 70 s and answers again. 1 live "
                              "session(s) checked: 1 going on (T1); none had to be restarted.")
    assert notices(c, "router_back")[0]["text"] == report["text"]
    assert selfheal.after_router_back(c) is None              # once per outage


def test_a_blip_is_not_an_outage_and_a_session_that_is_not_right_is_named(tmp_path):
    c, P, clk = game(tmp_path)
    ff = Router()
    ch = ControlChannel(c, ff, source_id="coordinator:test", log=lambda *_: None, clock=clk)
    ff.down = True
    ch.poll()
    clk.advance(5)
    ff.down = False
    ch.poll()
    assert "_router_back" not in c.__dict__ and events(c, "router_back") == []
    # an outage after which Kilo no longer knows a session
    c.store.update_task("game", "T1", state=S.IMPLEMENTING, session_id="ses_gone")
    c.store.x("INSERT INTO sessions (session_id, project_id, task_id, run_id, role, phase, "
              "attempt, client, persona, status, created_ts, data) VALUES "
              "(?,?,?,?,?,?,?,?,?,?,?,?)", ("ses_gone", "game", "T1", "r1", "implementer",
                                            "implement", 1, "kilo", "p", "running", clk.t, "{}"))
    P.client.state = lambda sid, directory="", since_ts=0: SimpleNamespace(
        status="retry", stalled=False, denial=None, error="")
    c.__dict__["_router_back"] = {"ts": clk.t, "down_s": 300, "why": "x", "stage": 1,
                                  "restarted": [{"task": "T9", "action": "ended the dead session"}]}
    clk.advance(200)
    report = selfheal.after_router_back(c)
    assert "restarted: T9" in report["text"]
    assert "still not right: T1 (retry) (the session rules take them from here)" in report["text"]
    # the engine tick runs the check
    c.__dict__["_router_back"] = {"ts": clk.t, "down_s": 30, "why": "x", "stage": 0, "restarted": []}
    c.tick()
    assert c.__dict__["_router_back"]["stage"] == 1
