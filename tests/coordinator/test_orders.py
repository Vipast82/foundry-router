"""Standing orders (orders.py) and the watch log (watchlog.py): COORDINATOR_AUTOMATION_NEEDS
items 27 and 28. The orders are the three the owner gave the operator session by word:
"Keep it going, need POL-7 to complete" (2026-11-18), "pause all sprints ~1.5 h until the
ChatGPT 5-hour usage window resets" (11-19), and "1 Claude agent only"."""

from __future__ import annotations

import json
import os
import time

import pytest

from foundry_coordinator import cli, infra, orders, watchlog
from foundry_coordinator import states as S
from foundry_coordinator.control import ControlChannel
from foundry_coordinator.tracks.resources import claude_capacity
from tests.coordinator.test_budget_policy import game, task, tick_step, use

DAY = 86400


def events(c, kind):
    return [json.loads(r["detail"]) for r in c.store.q(
        "SELECT detail FROM events WHERE kind=? ORDER BY id", (kind,))]


def notices(c, kind):
    return [d for d in events(c, "operator_notice") if d.get("kind") == kind]


def stop(c, text, to=S.BLOCKED, cls=""):
    ev = {"blocker": text, "reason": text}
    if cls:
        ev["blocker_class"] = cls
    c.store.transition("game", "T1", to, ev, blocker=text if to == S.BLOCKED else None)


def run(c, P, clk, seconds=31):
    clk.advance(seconds)
    return orders.tick(c, P)


# ------------------------------------------------------------------ keep_going

def test_replay_keep_pol7_going_until_it_completes(tmp_path, monkeypatch):
    """11-18 21:0x Victor: "Keep it going, need POL-7 to complete". The operator session
    then unblocked it at 21:59 (a gate stall), reset its budget at 22:14 (59 sessions) and
    had raised its minutes four times. The order does each of those by itself, says what
    it did, and ends with a report when the sprint closes."""
    c, P, clk = game(tmp_path, max_sessions_per_task=3, max_task_minutes=100)
    o = orders.add(c, P, "keep_going", task_id="T1", by="victor", note="need it to complete")
    assert orders.describe(o) == "keep T1 going until it completes"
    assert events(c, "order_added")[0]["by"] == "victor"
    assert run(c, P, clk) == []                              # it is running: nothing to do
    # 1. a tooling stop: put back, and the next session is told what stopped it
    stop(c, "scripted Studio gate: no CALL progress for 20 min; the runner was stopped",
         cls="studio_calls_error")
    [act] = run(c, P, clk)
    assert act["action"] == "put it back" and act["stop_class"] == "studio_calls_error"
    t = task(c)
    assert t["state"] == S.READY
    h = c.store.history("game", "T1")[-1]
    assert h["actor"] == f"order:{o['order_id']}" and not h["evidence"].get("reset_attempts")
    assert "Standing order from victor: keep T1 going until it completes" in h["evidence"]["resolution"]
    assert "no CALL progress for 20 min" in h["evidence"]["resolution"]
    # 2. out of sessions: a budget reset (no plan step needed: this is the owner's order)
    use(c, "sessions", 3)
    stop(c, "3 sessions used (max_sessions_per_task 3, budget ledger)", to=S.FAILED,
         cls="sessions_exhausted")
    assert run(c, P, clk, 60) == []                          # not twice within the gap
    [act] = run(c, P, clk, 20 * 60)
    assert act["action"] == "reset its budget and put it back"
    t = task(c)
    assert t["state"] == S.READY
    assert c.obligations.effective_limits(P, t)["sessions"]["remaining"] == 3
    assert c.store.history("game", "T1")[-1]["evidence"]["reset_attempts"] is True
    # 3. over its minutes: more minutes for this sprint, sized from its open steps
    c.store.update_task("game", "T1", started_ts=clk.t - 7200)
    monkeypatch.setattr(infra, "active_seconds", lambda c_, t_, now=None: 120 * 60)
    stop(c, "over max_task_minutes (100 active minutes)", to=S.FAILED, cls="task_minutes")
    [act] = run(c, P, clk, 20 * 60)
    assert act["action"].startswith("gave it ") and "more minutes" in act["action"]
    t = task(c)
    assert t["state"] == S.READY and t["data"]["task_minutes_extra"] >= 60
    assert P.profile.limit("max_task_minutes", 0) == 100
    # 4. the sprint closes: the order ends and reports
    c.store.update_task("game", "T1", state=S.COMPLETE)
    done = run(c, P, clk)[0]
    assert done["status"] == orders.DONE and done["result"] == "T1 is COMPLETE"
    [n] = notices(c, "order_done")
    assert n["digest"] is True and "It acted 3 time(s)" in n["text"]
    assert "keep T1 going until it completes (from victor)" in n["text"]
    assert run(c, P, clk, 3600) == [] and len(events(c, "order_action")) == 3


def test_an_order_never_overtakes_the_owner_or_something_already_waiting(tmp_path):
    c, P, clk = game(tmp_path)
    o = orders.add(c, P, "keep_going", task_id="T1", by="victor")
    stop(c, "review asked a product question")
    did = c.obligations.open_decision("game", "T1", "scope_request", "May it edit X?",
                                      {"path": "src/x.lua"}, ["approve", "deny"])
    assert run(c, P, clk) == []
    assert orders.view(c, P)["active"][0]["waiting"].startswith("a decision of yours is open")
    c.store.x("UPDATE decisions SET status='withdrawn'")
    c.store.merge_data("game", "T1", auto_heal={"blocker": "x"})
    assert run(c, P, clk) == [] and "infrastructure fault" in orders.view(c, P)["active"][0]["waiting"]
    c.store.merge_data("game", "T1", auto_heal=None, hold={"owner": "pid:1"})
    assert run(c, P, clk) == [] and orders.view(c, P)["active"][0]["waiting"] == "it is held"
    c.store.merge_data("game", "T1", hold=None)
    c.store.set_project_paused("game", True, "upgrade")
    assert run(c, P, clk) == []
    c.store.set_project_paused("game", False)
    assert len(run(c, P, clk)) == 1 and task(c)["state"] == S.READY
    assert orders.view(c, P)["active"][0]["waiting"] == ""
    assert did


def test_a_sprint_that_keeps_stopping_gets_one_alert_not_a_loop(tmp_path):
    """CR-2 once restarted a session every four minutes. An order acts at most once per
    gap and `per_day` times a day; past that, one alert and nothing until tomorrow."""
    c, P, clk = game(tmp_path)
    o = orders.add(c, P, "keep_going", task_id="T1", by="victor",
                   params={"gap_minutes": 5, "per_day": 3})
    acted = 0
    for i in range(12):
        if task(c)["state"] != S.BLOCKED:
            stop(c, f"session reported BLOCKED: stuck {i}", cls="session_blocked")
        acted += len(run(c, P, clk, 6 * 60))
    assert acted == 3
    [n] = notices(c, "order_capped")
    assert n["alert_class"] == "watch" and "put T1 back 3 times today" in n["title"]
    assert orders.get(c.store, o["order_id"])["status"] == orders.ACTIVE     # the order stays
    clk.advance(DAY)
    assert len(run(c, P, clk)) == 1                                           # a new day


# ------------------------------------------------------------------ pause_all

class Router:
    """A router whose admission answer names two backends and their windows."""

    def __init__(self):
        self.url = ""

    def helper_admission(self, persona, **kw):
        return {"routes": [
            {"backend": "meridian", "accounts": [{"windows": [
                {"name": "5h", "used": 40.0, "resets_in_s": 9000}]}]},
            {"backend": "chatgpt-codex", "accounts": [{"windows": [
                {"name": "5h", "used": 97.0, "resets_in_s": 5400},
                {"name": "weekly", "used": 30.0, "resets_in_s": 400000},
                {"name": "fable", "used": 99.0, "resets_in_s": 60, "applies": False}]}]}]}


def test_replay_pause_all_until_the_gpt_window_resets(tmp_path):
    """11-19 16:47 Victor: "pause all sprints ~1.5 h until the ChatGPT 5-hour usage window
    resets". The operator paused each sprint and released them by hand at 18:19."""
    c, P, clk = game(tmp_path)
    P.foundry = Router()
    o = orders.add(c, P, "pause_all", until={"reset": "gpt"}, by="victor")
    assert o["until"]["type"] == "time" and o["until"]["ts"] == pytest.approx(clk.t + 5400)
    assert o["until"]["why"] == "chatgpt-codex 5h at 97% resets"
    assert orders.describe(o).startswith("pause all sprints until ")
    proj = c.store.project("game")
    assert proj["paused"] and o["order_id"] in proj["pause_note"]
    assert events(c, "order_action")[0]["action"].startswith("paused the project")
    assert run(c, P, clk, 3600) == [] and c.store.project("game")["paused"]
    [done] = run(c, P, clk, 1900)
    assert done["status"] == orders.DONE and "the project is resumed" in done["result"]
    assert not c.store.project("game")["paused"]
    assert "chatgpt-codex 5h at 97% resets: done" in notices(c, "order_done")[0]["text"]


def test_pause_all_needs_an_end_and_leaves_someone_elses_pause_alone(tmp_path):
    c, P, clk = game(tmp_path)
    with pytest.raises(orders.OrderError, match="needs an end"):
        orders.add(c, P, "pause_all", by="victor")
    with pytest.raises(orders.OrderError, match="no router to ask"):
        orders.add(c, P.__class__(**{**P.__dict__, "foundry": None}), "pause_all",
                   until={"reset": "gpt"}, by="victor")
    P.foundry = Router()
    with pytest.raises(orders.OrderError, match="no reset time for 'gemini'.*chatgpt-codex, meridian"):
        orders.add(c, P, "pause_all", until={"reset": "gemini"}, by="victor")
    assert not c.store.project("game")["paused"] and orders.orders(c.store, "game") == []
    # the project is already paused by someone else: the order only ends, it does not resume
    c.store.set_project_paused("game", True, "operator upgrade")
    orders.add(c, P, "pause_all", until={"minutes": 30}, by="victor")
    with pytest.raises(orders.OrderError, match="active already"):
        orders.add(c, P, "pause_all", until={"minutes": 5}, by="victor")
    [done] = run(c, P, clk, 31 * 60)
    assert done["result"] == ("the time is up; the project was paused before this order and "
                              "stays as it is")
    assert c.store.project("game")["paused"]
    # cancelled early: resumed at once
    c.store.set_project_paused("game", False)
    o = orders.add(c, P, "pause_all", until={"minutes": 90}, by="victor")
    out = orders.cancel(c, o["order_id"], by="victor", why="resume now")
    assert out["status"] == orders.CANCELLED and "the project is resumed" in out["result"]
    assert not c.store.project("game")["paused"]


# ------------------------------------------------------------------ claude_agents

def test_one_claude_agent_only_holds_through_a_settings_deploy_and_is_put_back(tmp_path):
    c, P, clk = game(tmp_path)
    P.profile.raw.setdefault("resources", {})["claude_agents"] = 4
    assert claude_capacity(P) == 4
    o = orders.add(c, P, "claude_agents", params={"n": 1}, until={"minutes": 120}, by="victor")
    assert claude_capacity(P) == 1
    assert events(c, "order_action")[0]["action"] == "claude_agents set to 1 (was 4)"
    # a settings deploy puts the file's value back: the order holds it
    P.profile.raw["resources"] = {"claude_agents": 4}
    assert run(c, P, clk) == [] and claude_capacity(P) == 1
    [done] = run(c, P, clk, 121 * 60)
    assert "claude_agents is back to 4" in done["result"] and claude_capacity(P) == 4
    # no end given: until cancelled
    o = orders.add(c, P, "claude_agents", params={"n": 2}, by="victor")
    assert orders.describe(o) == "2 Claude agents only until cancelled"
    assert run(c, P, clk, 5 * DAY) == [] and claude_capacity(P) == 2
    orders.cancel(c, o["order_id"], by="victor")
    assert claude_capacity(P) == 4
    with pytest.raises(orders.OrderError, match="n: 1 to 32"):
        orders.add(c, P, "claude_agents", params={"n": 0}, by="victor")


# ------------------------------------------------------------------ refusals, CLI, channel

def test_bad_orders_are_refused_with_the_reason(tmp_path):
    c, P, clk = game(tmp_path)
    for kw, msg in (({"kind": "nonsense"}, "kind must be one of"),
                    ({"kind": "keep_going"}, "needs a sprint on the queue"),
                    ({"kind": "keep_going", "task_id": "NOPE"}, "needs a sprint on the queue"),
                    ({"kind": "keep_going", "task_id": "T1", "params": {"per_day": 500}}, "per_day"),
                    ({"kind": "pause_all", "until": {"minutes": 0}}, "minutes: between"),
                    ({"kind": "pause_all", "until": {"ts": clk.t - 5}}, "next 14 days")):
        with pytest.raises(orders.OrderError, match=msg):
            orders.add(c, P, kw.pop("kind"), by="victor", **kw)
    orders.add(c, P, "keep_going", task_id="T1", by="victor")
    with pytest.raises(orders.OrderError, match="already has a keep_going order"):
        orders.add(c, P, "keep_going", task_id="T1", by="victor")
    with pytest.raises(orders.OrderError, match="no standing order"):
        orders.cancel(c, "so-0000000000")
    c.store.update_task("game", "T1", state=S.COMPLETE)
    orders.tick(c, P)                                         # (rate-limited: nothing yet)


def test_the_cli_gives_lists_and_cancels_orders(tmp_path, capsys):
    c, P, clk = game(tmp_path)
    base = ["--state", str(tmp_path / "state"), "--profiles", c.cli_args["prof"]]
    kw = {"clients": c.cli_args["clients"], "foundry": c.cli_args["foundry"]}
    assert cli.main(base + ["orders", "add", "game", "keep_going", "--task", "T1", "--by",
                            "victor", "--note", "need it to complete"], **kw) == 0
    out = capsys.readouterr().out
    oid = out.strip().splitlines()[-1].split(":")[0]
    assert orders.ORDER_ID_RE.match(oid) and "keep T1 going until it completes" in out
    assert cli.main(base + ["orders", "list", "game"], **kw) == 0
    assert f"{oid}  active" in capsys.readouterr().out
    assert cli.main(base + ["orders", "add", "game", "pause_all"], **kw) == 2
    assert "refused: pause_all needs an end" in capsys.readouterr().out
    assert cli.main(base + ["orders", "cancel", oid, "--by", "victor", "--why", "done"], **kw) == 0
    assert f"{oid} cancelled: cancelled by victor: done" in capsys.readouterr().out
    assert orders.get(c.store, oid)["status"] == orders.CANCELLED
    assert {("orders", "add"), ("orders", "cancel")} <= cli._WRITING


class Recorder:
    def __init__(self):
        self.reports = []

    def action_status(self, aid, source, status, detail, result=None):
        self.reports.append((aid, status, detail, result or {}))


def test_the_control_channel_applies_orders_and_reports_them_in_the_heartbeat(tmp_path):
    c, P, clk = game(tmp_path, max_task_minutes=100)
    ff = Recorder()
    ch = ControlChannel(c, ff, source_id="coordinator:test", log=lambda *_: None)

    def act(n, action, **payload):
        ch.handle({"action_id": f"act-{n}", "kind": action, "project_id": "game", "task_id": "",
                   "actor": "victor", "payload": payload})
        return ff.reports[-1]
    _, status, detail, res = act(1, "order_add", kind="keep_going", task="T1", note="go")
    assert status == "applied" and "keep T1 going until it completes" in detail
    oid = res["order_id"]
    assert orders.get(c.store, oid)["created_by"] == "victor (control center)"
    assert act(2, "order_add", kind="pause_all")[1:3] == (
        "rejected", "pause_all needs an end: minutes, a time, or a backend whose window "
                    "should reset")
    _, status, detail, res = act(3, "extend_time", task="T1", minutes=45, note="a bit more")
    assert status == "applied" and res["limit"] == 145 and res["minutes"] == 45
    assert task(c)["data"]["task_minutes_extra"] == 45
    assert act(4, "extend_time", task="T1", minutes=0)[1] == "rejected"
    beat = ch._beat()
    assert "orders" in beat["features"]
    assert {"order_add", "order_cancel", "extend_time"} <= set(
        beat["source"]["capabilities"]["actions"])
    assert [o["order_id"] for o in beat["orders"]["game"]["active"]] == [oid]
    assert act(5, "order_cancel", order_id=oid, why="enough")[1] == "applied"
    assert ch._beat()["orders"]["game"]["active"] == []
    assert ch._beat()["orders"]["game"]["ended"][0]["status"] == orders.CANCELLED
    # the same action claimed twice is applied once
    act(1, "order_add", kind="keep_going", task="T1")
    assert len(orders.orders(c.store, "game")) == 1


# ------------------------------------------------------------------ the watch log

def test_the_watch_log_says_what_was_done_and_why_with_the_orders_and_their_scope(tmp_path):
    c, P, clk = game(tmp_path)
    o = orders.add(c, P, "keep_going", task_id="T1", by="victor", note="need it to complete")
    stop(c, "scripted Studio gate: timed out after 2400s", cls="studio_calls_error")
    run(c, P, clk)
    c.store.event("game", "T1", "self_heal", detail={
        "rule": "stale_playtest", "action": "stopped a playtest nobody owns",
        "evidence": {"why": "Studio is in Play with no gate runner"}, "digest": True})
    c.unblock("game", "T1", "operator 2026-11-21: Foundry 0286000 is live; re-run the review",
              actor="operator") if task(c)["state"] == S.BLOCKED else None
    c.store.event("game", "T1", "verification", detail={"result": "PASS"})    # not logged
    assert watchlog.tick(c, force=True) >= 4
    lines = watchlog.tail(c, 50)
    text = "\n".join(lines)
    day = time.strftime("%Y-%m-%d")
    assert all(ln.startswith(day) for ln in lines)
    assert "T1  ORDER from victor: keep T1 going until it completes - need it to complete" in text
    assert "T1  order 'keep T1 going until it completes': put it back - was: scripted Studio" in text
    assert f"T1  BLOCKED -> READY by order:{o['order_id']}: Standing order from victor" in text
    assert "T1  self-repair (stale_playtest): stopped a playtest nobody owns - Studio is in Play" in text
    assert "verification" not in text and "QUEUED -> READY" not in text      # the engine's own
    # only what is new is appended; the file is plain text in the state dir
    assert watchlog.tick(c, force=True) == 0
    c.store.set_project_paused("game", True, "upgrade to v0.181.228")
    assert watchlog.tick(c, force=True) == 1
    assert watchlog.tail(c, 1)[0].endswith("game  project paused: upgrade to v0.181.228")
    files = os.listdir(os.path.join(c.state_dir, "watch-log"))
    assert files == [time.strftime("%Y-%m") + ".md"]
    assert watchlog.tick(c) == 0                                              # once a minute
    # a broken store never breaks the tick
    c.store.set_meta(watchlog.EV_CURSOR, "not a number")
    assert watchlog.tick(c, force=True) >= 0


def test_the_engine_tick_runs_the_orders_and_writes_the_log(tmp_path):
    c, P, clk = game(tmp_path)
    c.store.update_task("game", "T1", state=S.BLOCKED, blocker="session reported BLOCKED: x")
    orders.add(c, P, "keep_going", task_id="T1", by="victor")
    clk.advance(31)
    c.tick()
    assert events(c, "order_action") and watchlog.tail(c, 20)
    assert any("ORDER from victor" in ln for ln in watchlog.tail(c, 20))
