"""The status board (board.py): COORDINATOR_AUTOMATION_NEEDS items 26 and 12. One call
answers what the operator's "status" answered each time: every seat with its sprint or why
it is idle, what is stopped and what it holds up, open decisions, what was done, the
standing orders."""

from __future__ import annotations

import json
import time

from foundry_coordinator import automation, board, cli, orders
from foundry_coordinator import states as S
from foundry_coordinator.control import ControlChannel
from tests.coordinator.test_budget_policy import game

H = 3600


def put(c, tid, deps=(), track="", state=None, **fields):
    c.store.approve("game", tid, list(deps), "victor", track=track)
    if state:
        c.store.update_task("game", tid, state=state, **fields)


def stop(c, tid, text, ago_s):
    """BLOCKED `ago_s` seconds ago, through the real transitions."""
    c.store.transition("game", tid, S.READY, {"deps_satisfied": True})
    c.store.transition("game", tid, S.BLOCKED, {"blocker": text}, blocker=text)
    c.store.x("UPDATE transitions SET ts=? WHERE project_id='game' AND task_id=? AND to_state=?",
              (time.time() - ago_s, tid, S.BLOCKED))


def world(tmp_path):
    """A day like 2026-11-20: the main line working, one slot lane stopped and holding up
    the release sprints, one slot with nothing bound, a lane without Studio verifying."""
    c, P, clk = game(tmp_path)
    P.profile.raw["slots"] = {"slot-1": {"container": "au-slot-1"},
                              "slot-3": {"container": "au-slot-3", "host": "studio@192.168.0.117"},
                              "slot-9": {"container": "x", "enabled": False}}
    automation.set_live(P, "workstation_only_sprints", ["PT-*"])
    # the main line: T1 is working
    c.store.update_task("game", "T1", state=S.IMPLEMENTING, phase="implement",
                        started_ts=clk.t - 2 * H, next_action="session working")
    c.store.x("INSERT INTO transitions (ts, project_id, task_id, from_state, to_state, evidence, "
              "actor) VALUES (?,?,?,?,?,?,?)",
              (time.time() - 40 * 60, "game", "T1", S.READY, S.IMPLEMENTING, "{}", "engine"))
    # slot-3: lane fin4a, its sprint stopped three hours ago; two sprints wait on it
    put(c, "FIN-4a", track="fin4a")
    stop(c, "FIN-4a", "scripted Studio gate: S46 c6 expected 12 rows, got 11", 3 * H)
    c.store.add_track("game", "fin4a", "parallel", str(tmp_path / "wt-fin4a"), "track/fin4a", "a" * 40,
                      ["studio"], ["FIN-4a"], "victor", state="running", data={"slot": "slot-3"})
    put(c, "REL-1a", deps=["FIN-4a"])
    put(c, "REL-1b", deps=["REL-1a"])
    # a lane with no Studio, verifying
    put(c, "CR-2", track="cr2", state=S.VERIFYING, phase="verify", next_action="lune 14/31")
    c.store.add_track("game", "cr2", "parallel", str(tmp_path / "wt-cr2"), "track/cr2", "a" * 40,
                      [], ["CR-2"], "victor", state="running")
    # the queue: one sprint that could start on a slot now, one workstation-only
    put(c, "BAL-2e")
    put(c, "PT-4")
    # done today
    put(c, "FIN-2c", state=S.COMPLETE)
    c.store.x("INSERT INTO transitions (ts, project_id, task_id, from_state, to_state, evidence, "
              "actor) VALUES (?,?,?,?,?,?,?)",
              (time.time() - 2 * H, "game", "FIN-2c", S.CLOSING, S.COMPLETE, "{}", "engine"))
    return c, P, clk


def test_replay_the_status_the_operator_gave_each_time(tmp_path):
    c, P, clk = world(tmp_path)
    did = c.obligations.open_decision("game", "T1", "scope_request", "May T1 edit src/x.lua?",
                                      {"path": "src/x.lua"}, ["approve", "deny"])
    orders.add(c, P, "claude_agents", params={"n": 1}, by="victor")
    b = board.build(c, P)
    assert "errors" not in b
    seats = {s["seat"]: s for s in b["seats"]}
    assert list(seats) == ["workstation", "slot-1", "slot-3", "lane cr2"]      # slot-9 is off
    ws = seats["workstation"]
    assert (ws["idle"], ws["sprint"], ws["state"], ws["phase"]) == (False, "T1", S.IMPLEMENTING,
                                                                    "implement")
    assert 38 <= ws["minutes"] <= 42 and ws["doing"] == "session working"
    # a free slot says what could go on it; the workstation-only sprint is not offered
    s1 = seats["slot-1"]
    assert s1["idle"] and s1["could_run"] == ["BAL-2e"]
    assert s1["why"] == "no lane is bound to it; ready to start on a slot: BAL-2e"
    # a slot whose lane is stopped is idle, with the reason
    s3 = seats["slot-3"]
    assert s3["idle"] and s3["lane"] == "fin4a" and s3["sprint"] == "FIN-4a"
    assert s3["why"].startswith("FIN-4a is BLOCKED: scripted Studio gate: S46 c6")
    assert seats["lane cr2"]["idle"] is False and seats["lane cr2"]["doing"] == "lune 14/31"
    # what is stopped, the most held-up first, with everything that waits on it
    [blk] = b["blockers"]
    assert blk["sprint"] == "FIN-4a" and blk["holds_up"] == ["REL-1a", "REL-1b"]
    assert 178 <= blk["minutes"] <= 182 and blk["waits_itself"] is False
    [d] = b["decisions"]
    assert d["task"] == "T1" and d["whose"] == "yours" and d["question"] == "May T1 edit src/x.lua?"
    assert [r["sprint"] for r in b["done"]] == ["FIN-2c"]
    assert b["orders"][0]["order"] == "1 Claude agent only until cancelled"
    assert b["paused"] is False and did
    # as the CLI prints it
    text = board.text(b, last_check=time.time() - 3 * H,
                      models={"claude-sonnet-5-5": 81, "qwen3.8-flash": 19})
    for line in ("workstation   T1  IMPLEMENTING/implement  for ",
                 "slot-1        idle: no lane is bound to it; ready to start on a slot: BAL-2e",
                 "slot-3        idle: FIN-4a is BLOCKED",
                 "FIN-4a        BLOCKED for 3.0 h; holds up 2: REL-1a, REL-1b",
                 "T1            yours (scope_request), open 0 min",
                 "Done in the last 24 h (1): FIN-2c",
                 "1 Claude agent only until cancelled (from victor; acted 0x)",
                 "Models, last 100 requests: claude-sonnet-5-5 81, qwen3.8-flash 19"):
        assert line in text, line
    assert "Done since the last check (" in text and "): FIN-2c" in text
    # an order to keep the stopped sprint going puts it back at once: the board follows
    orders.add(c, P, "keep_going", task_id="FIN-4a", by="victor")
    b = board.build(c, P)
    assert b["blockers"] == []
    s3 = {s["seat"]: s for s in b["seats"]}["slot-3"]
    assert s3["idle"] and s3["why"].startswith("FIN-4a is READY")
    assert "keep FIN-4a going until it completes" in [o["order"] for o in b["orders"]]


def test_an_idle_workstation_and_empty_slots_say_why(tmp_path):
    c, P, clk = game(tmp_path)
    P.profile.raw["slots"] = {"slot-1": {"container": "au-slot-1"}}
    c.store.update_task("game", "T1", state=S.COMPLETE)
    # FIN-1 is queued behind a sprint the owner paused
    put(c, "FIN-0", state=S.PAUSED, pause_reason="owner: waits for the concept pass")
    put(c, "FIN-1", deps=["FIN-0"])
    c.store.update_task("game", "FIN-1", next_action="waiting for FIN-0")
    seats = {s["seat"]: s for s in board.seats(c, P)}
    assert seats["workstation"]["idle"] is True
    assert seats["workstation"]["why"] == ("FIN-0 is PAUSED: owner: waits for the concept "
                                           "pass")
    assert seats["slot-1"]["why"] == ("no lane is bound to it; nothing is ready: FIN-1 waits "
                                      "for FIN-0")
    [blk] = board.blockers(c, P)
    assert blk["sprint"] == "FIN-0" and blk["holds_up"] == ["FIN-1"]
    # nothing at all
    for tid in ("FIN-1", "FIN-0"):
        c.store.update_task("game", tid, state=S.COMPLETE)
    seats = {s["seat"]: s for s in board.seats(c, P)}
    assert seats["workstation"]["why"] == "the queue has no main-line sprint"
    assert "the queue has no sprint that has not started" in seats["slot-1"]["why"]
    assert board.blockers(c, P) == []


def test_one_part_failing_never_takes_the_board_down(tmp_path, monkeypatch):
    c, P, clk = world(tmp_path)
    monkeypatch.setattr(board, "seats", lambda c_, P_: 1 / 0)
    b = board.build(c, P)
    assert b["seats"] == [] and b["errors"]["seats"].startswith("ZeroDivisionError")
    assert b["blockers"] and b["done"]
    assert "Not read: seats: ZeroDivisionError" in board.text(b)


def test_cli_status_board_prints_it_and_remembers_the_check(tmp_path, capsys):
    c, P, clk = world(tmp_path)
    argv = ["--state", str(tmp_path / "state"), "--profiles", c.cli_args["prof"], "status",
            "--board"]
    kw = {"clients": c.cli_args["clients"], "foundry": c.cli_args["foundry"]}
    assert cli.main(argv, **kw) == 0
    out = capsys.readouterr().out
    assert "Seats" in out and "Stopped (1)" in out and "Done in the last 24 h (1): FIN-2c" in out
    assert "Done since the last check" not in out                 # the first check
    assert board.last_check(c, "game") is not None
    assert cli.main(argv, **kw) == 0
    assert "Done since the last check (" in capsys.readouterr().out
    assert cli.main(argv + ["--json"], **kw) == 0
    out = capsys.readouterr().out
    data = json.loads(out[out.index("{"):])
    assert [s["seat"] for s in data["game"]["seats"]][0] == "workstation"


def test_the_heartbeat_carries_the_board(tmp_path):
    c, P, clk = world(tmp_path)

    class FF:
        def action_status(self, *a, **k):
            pass
    beat = ControlChannel(c, FF(), source_id="coordinator:test", log=lambda *_: None)._beat()
    b = beat["board"]["game"]
    assert [s["seat"] for s in b["seats"]][:2] == ["workstation", "slot-1"]
    assert json.dumps(beat["board"])                              # plain data
