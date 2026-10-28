"""The self-repair sweep (selfheal.py, SELF_DRIVING_PLAN part 2, 2026-10-28), the hold
owner/expiry API, the generic budget reset on an operator unblock, and the CLI version
guard. Each rule replays what the chat watch fixed by hand."""

from __future__ import annotations

import json
import os
import time

import pytest

from foundry_coordinator import automation, cli, selfheal
from foundry_coordinator import states as S
from foundry_coordinator.obligations import LIMITS
from tests.coordinator.test_lead_manager import env as lead_env  # noqa: F401  (fixture)
from tests.coordinator.test_lead_manager import open_case, task
from tests.coordinator.test_lifecycle_e2e import green_t1
from tests.coordinator.test_lifecycle_engine import _game


class Clock:
    def __init__(self, t=None):
        self.t = float(t if t is not None else time.time())

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def events(c, kind="self_heal", tid="T1"):
    return [json.loads(r["detail"]) for r in c.store.q(
        "SELECT detail FROM events WHERE kind=? AND task_id=? ORDER BY id", (kind, tid))]


def game(tmp_path):
    clk = Clock()
    c, client, ff, root, prof = _game(tmp_path, lambda ctx: green_t1(ctx), clock=clk,
                                      store_path=tmp_path / "state" / "coordinator.sqlite")
    c.approve("game", "T1", [], "victor")
    return c, c.projects["game"], clk, client, ff, prof


# ------------------------------------------------------------------ 1 dead sessions

def test_replay_dead_lead_session_while_the_store_says_created(lead_env):
    """2026-10-27 23:07: lead session ses_e7f5e8ca1 died while the store said `created`
    and case rc-f24672b33aef5998 waited for hours. The sweep ends the dead session, fails
    the call uncounted, and the Lead starts a new call."""
    e = lead_env
    c, P, lm, fake = e["c"], e["P"], e["lm"], e["fake"]
    clk = Clock()
    c.clock = clk
    case = open_case(e)
    for _ in range(10):                                  # create + bind + prompt the call
        case = lm.step(P, task(e), case)
        call = lm._open_call(case["case_id"], "lead_call")
        if call and call["detail"].get("phase") == "prompted":
            break
        time.sleep(0.02)
    sid = call["detail"]["session_id"]
    assert c.store.session(sid)["status"] == "created"
    with fake.lock:
        fake.sessions.pop(sid)                           # Kilo lost it
    assert selfheal.dead_sessions(c, P) == []            # touched just now: not asked
    clk.advance(121)
    assert selfheal.dead_sessions(c, P) == []            # quiet 2 min: first seen dead
    clk.advance(60)
    assert selfheal.dead_sessions(c, P) == []            # dead < 2 min
    clk.advance(61)
    [act] = selfheal.dead_sessions(c, P)
    assert act["rule"] == "dead_session" and act["evidence"]["case_id"] == case["case_id"]
    assert act["digest"] is True and act["evidence"]["store_status"] == "created"
    a = lm._action(call["op_key"])
    assert a["status"] == "failed" and a["detail"].get("infra")       # uncounted
    assert c.store.session(sid)["status"] == "ended"
    assert lm.case(case["case_id"])["counters"].get("diagnoses", 0) == 0
    assert events(c)[-1]["rule"] == "dead_session"
    lm.step(P, task(e), lm.case(case["case_id"]))        # the Lead starts a new call
    calls = [x for x in lm.actions(case["case_id"]) if x["kind"] == "lead_call"]
    assert len(calls) == 2 and calls[-1]["status"] in ("started", "uncertain")


def test_dead_implementer_session_gets_a_free_retry(tmp_path):
    c, P, clk, client, _, _ = game(tmp_path)
    c.store.update_task("game", "T1", state=S.IMPLEMENTING, session_id="ses_gone",
                        run_id="r1", phase="implement")
    c.store.add_session("ses_gone", "game", "T1", "r1", "implementer", "implement", 1,
                        "kilo", "p")
    c.store.update_session("ses_gone", status="prompted", prompt_ts=clk())
    assert selfheal.dead_sessions(c, P) == []
    clk.advance(121)
    assert selfheal.dead_sessions(c, P) == []            # first seen dead
    clk.advance(121)
    [act] = selfheal.dead_sessions(c, P)
    assert act["action"].startswith("ended the dead implementer session")
    t = c.store.task("game", "T1")
    assert t["state"] == S.CHANGES_REQUIRED and t["data"]["free_retry"] is True
    assert c.store.session("ses_gone")["status"] == "ended"


def test_a_live_or_busy_session_is_left_alone(tmp_path):
    c, P, clk, client, _, _ = game(tmp_path)
    sid = client.start_session(directory=P.profile.workspace, title="x", persona="p",
                               agent="code", metadata={}, permissions=[])
    c.store.update_task("game", "T1", state=S.IMPLEMENTING, session_id=sid, run_id="r1")
    c.store.add_session(sid, "game", "T1", "r1", "implementer", "implement", 1, "kilo", "p")
    client.sessions[sid]["pending"] = False                      # busy
    for _ in range(3):
        assert selfheal.dead_sessions(c, P) == []
        clk.advance(300)
    assert c.store.task("game", "T1")["state"] == S.IMPLEMENTING


# ------------------------------------------------------------------ 2 stuck cases

def test_a_case_with_no_live_call_for_30_min_is_cancelled(lead_env):
    e = lead_env
    c, P, lm = e["c"], e["P"], e["lm"]
    clk = Clock()
    c.clock = clk
    case = open_case(e)                                  # DIAGNOSING, nobody steps it
    clk.advance(3600)
    assert selfheal.stuck_cases(c, P) == []              # first seen (a clock jump)
    clk.advance(29 * 60)
    assert selfheal.stuck_cases(c, P) == []
    clk.advance(2 * 60)
    [act] = selfheal.stuck_cases(c, P)
    assert act["rule"] == "stuck_case" and act["evidence"]["case_id"] == case["case_id"]
    got = lm.case(case["case_id"])
    assert got["state"] == "CANCELLED" and "self-heal" in got["outcome"]


def test_a_case_with_a_live_call_or_in_a_paused_project_is_not_stuck(lead_env):
    e = lead_env
    c, P, lm = e["c"], e["P"], e["lm"]
    clk = Clock()
    c.clock = clk
    case = open_case(e)
    case = lm.step(P, task(e), case)                     # a call starts
    assert lm._open_call(case["case_id"], "lead_call")
    clk.advance(3 * 3600)
    assert selfheal.stuck_cases(c, P) == []
    c.store.set_project_paused("game", True, "operator")
    lm._abort_sessions(P, case, "test")
    assert selfheal.stuck_cases(c, P) == []
    assert lm.case(case["case_id"])["state"] != "CANCELLED"


# ------------------------------------------------------------------ 3 fixed by a release

def _old_reset_then_budget_block(c, P):
    """GF-GATE1 2026-10-28: an unblock with reset by an older release (attempts only),
    then the first review refused: "review budget used (4/4)"."""
    for i in range(4):
        c.obligations.charge("game", "T1", "review_calls", 1, f"review:old:{i}", "engine")
    with c.store.tx():
        c.store.transition("game", "T1", S.BLOCKED, {"blocker": "stuck"}, blocker="stuck")
        c.store.transition("game", "T1", S.READY, {"resolution": "fresh budget",
                                                   "reset_attempts": True}, actor="victor")
        c.store.transition("game", "T1", S.BLOCKED, {
            "blocker": "review budget used (4/4 review calls, budget ledger)"},
            blocker="review budget used (4/4 review calls, budget ledger)")
    return c.store.history("game", "T1")[-1]["ts"]


def _deployed(c, ts, before="0.155.0"):
    """`before` ran when the task blocked; the running release was first seen after."""
    c.store.set_meta(selfheal.VERSIONS_META, json.dumps(
        {before: ts - 100, selfheal._running_version(): ts + 50}))


def test_replay_review_budget_block_is_reverified_after_the_fix_deploy(tmp_path):
    c, P, clk, *_ = game(tmp_path)
    block_ts = _old_reset_then_budget_block(c, P)
    lim = c.obligations.effective_limits(P, c.store.task("game", "T1"))["review_calls"]
    assert lim["remaining"] == 0
    _deployed(c, block_ts)
    [act] = selfheal.fixed_releases(c, P)
    assert act["evidence"]["signature"] == "review_budget_after_reset"
    assert "review_calls" in act["evidence"]["authorized"]
    t = c.store.task("game", "T1")
    assert t["state"] == S.READY and t["data"]["free_retry"] is True
    assert t["attempt"] == 0
    lim = c.obligations.effective_limits(P, t)["review_calls"]
    assert lim["remaining"] == lim["base_max"] and lim["used"] == 4
    # once: the same signature blocks again -> left for the automation default
    c.store.transition("game", "T1", S.BLOCKED, {"blocker": "review budget used (4/4)"},
                       blocker="review budget used (4/4)")
    assert selfheal.fixed_releases(c, P) == []


def test_a_block_after_the_fix_deploy_or_without_a_reset_is_left_alone(tmp_path):
    c, P, clk, *_ = game(tmp_path)
    block_ts = _old_reset_then_budget_block(c, P)
    c.store.set_meta(selfheal.VERSIONS_META, json.dumps({selfheal._running_version():
                                                         block_ts - 10}))
    assert selfheal.fixed_releases(c, P) == []           # blocked under the fixed release
    c2, P2, *_ = game(tmp_path / "b")
    with c2.store.tx():
        c2.store.transition("game", "T1", S.BLOCKED, {"blocker": "review budget used (4/4)"},
                            blocker="review budget used (4/4)")
    _deployed(c2, c2.store.history("game", "T1")[-1]["ts"])
    assert selfheal.fixed_releases(c2, P2) == []         # no reset before: a real budget


def test_unrelated_preserved_leftover_block_is_reverified(tmp_path):
    c, P, clk, *_ = game(tmp_path)
    text = ("final verification failed: coordinator.unrelated_preserved: pre-existing "
            "uncommitted changes were modified: docs/progress/GF_GATE1_hashes.json")
    c.store.transition("game", "T1", S.BLOCKED, {"blocker": text}, blocker=text)
    _deployed(c, c.store.history("game", "T1")[-1]["ts"])
    assert selfheal.fixed_releases(c, P) == []           # 0.155.0 >= 0.152.2 already ran
    _deployed(c, c.store.history("game", "T1")[-1]["ts"], before="0.150.0")
    [act] = selfheal.fixed_releases(c, P)
    assert act["evidence"]["signature"] == "unrelated_preserved_task_leftover"
    assert c.store.task("game", "T1")["state"] == S.READY


def test_fixed_signatures_name_their_release():
    assert set(selfheal.SIGNATURE_PATTERNS) <= set(selfheal.FIXED_SIGNATURES)
    assert selfheal.FIXED_SIGNATURES["review_budget_after_reset"] == "0.155.1"
    assert selfheal._vt("0.155.1") > selfheal._vt("0.155.0") > selfheal._vt("0.99.9")


# ------------------------------------------------------------------ 4 stale locks

def test_coordinator_locks_of_a_stopped_task_are_released(tmp_path):
    c, P, clk, *_ = game(tmp_path)
    c.store.transition("game", "T1", S.BLOCKED, {"blocker": "x"}, blocker="x")
    res = c._workspace_locks(P) + ["writer:0"]
    assert c.store.acquire(res, "game", "T1") is None
    assert selfheal.stale_locks(c, P) == []              # fresh: grace period
    c.store.x("UPDATE locks SET acquired_ts=?", (time.time() - 3600,))
    c.store.merge_data("game", "T1", fast_lane={"run": {"pid": 1}})
    assert selfheal.stale_locks(c, P) == []              # a fast-lane run may hold them
    c.store.merge_data("game", "T1", fast_lane={})
    [act] = selfheal.stale_locks(c, P)
    assert sorted(act["evidence"]["resources"]) == sorted(res)
    assert c.store.locks() == []


def test_locks_of_a_live_task_are_kept(tmp_path):
    c, P, clk, *_ = game(tmp_path)
    c.store.update_task("game", "T1", state=S.IMPLEMENTING)
    c.store.acquire(c._workspace_locks(P), "game", "T1")
    c.store.x("UPDATE locks SET acquired_ts=?", (time.time() - 3600,))
    assert selfheal.stale_locks(c, P) == [] and c.store.locks()


def test_gate_runner_lock_with_a_dead_pid_is_released(tmp_path):
    from foundry_coordinator.autoheal import runner_lock_path
    c, P, clk, *_ = game(tmp_path)
    p = runner_lock_path(P.profile.workspace)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    for pid, gone in ((os.getpid(), False), (2 ** 22 + 12345, True)):
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "sprint": "T1"}, f)
        old = time.time() - 3600
        os.utime(p, (old, old))
        got = selfheal.stale_locks(c, P)
        assert os.path.exists(p) is not gone, got
        assert bool(got) is gone


def test_studio_gate_studio_lock_with_no_runner_is_released(tmp_path):
    c, P, clk, *_ = game(tmp_path)
    released = []
    h = {"owner": "studio_gate-T1", "sprint": "T1", "line": "studio_gate-T1 x T1 gate",
         "age_s": 3600, "path": "x"}
    P.adapter.studio_lock_holder = lambda: dict(h)
    P.adapter.release_studio_lock = lambda hh: released.append(hh) or True
    c.store.merge_data("game", "T1", studio_calls={"run": {"head": "x"}})
    assert selfheal.stale_locks(c, P) == [] and not released     # a scripted run is live
    c.store.merge_data("game", "T1", studio_calls={})
    [act] = selfheal.stale_locks(c, P)
    assert released and act["action"] == "released the studio_gate studio.lock"


# ------------------------------------------------------------------ 5 holds

def test_an_expired_hold_is_resumed_by_the_tick(tmp_path):
    c, P, clk, *_ = game(tmp_path)
    c.hold("game", "T1", "operator:victor", "short hold for a prep commit", minutes=30)
    t = c.store.task("game", "T1")
    assert t["state"] == S.PAUSED and t["data"]["hold"]["expires_ts"] == pytest.approx(
        clk() + 1800)
    clk.advance(29 * 60)
    assert selfheal.holds(c, P) == []
    clk.advance(2 * 60)
    c.tick()                                             # the engine hook runs the sweep
    t = c.store.task("game", "T1")
    assert t["state"] != S.PAUSED and "hold" not in t["data"]
    assert events(c)[0]["rule"] == "hold" and events(c)[0]["evidence"]["expired"]


@pytest.mark.parametrize("owner", ["pid:4194303", "session:ses_never", "task:game/NOPE"])
def test_a_hold_whose_owner_is_gone_is_resumed(tmp_path, owner):
    c, P, clk, *_ = game(tmp_path)
    c.hold("game", "T1", owner, "until the owner is done")
    [act] = selfheal.holds(c, P)
    assert act["evidence"]["owner_alive"] is False
    assert c.store.task("game", "T1")["state"] == S.QUEUED


def test_a_hold_with_a_live_or_unknown_owner_stays(tmp_path):
    c, P, clk, *_ = game(tmp_path)
    c.hold("game", "T1", f"pid:{os.getpid()}", "mine")
    assert selfheal.holds(c, P) == []
    selfheal.register_hold_owner("prepq", lambda c_, ref: ref != "done")
    c.store.merge_data("game", "T1", hold={"owner": "prepq:pending", "reason": "x"})
    assert selfheal.holds(c, P) == []
    c.store.merge_data("game", "T1", hold={"owner": "prepq:done", "reason": "x"})
    assert selfheal.holds(c, P) and c.store.task("game", "T1")["state"] == S.QUEUED
    with pytest.raises(ValueError):
        c.hold("game", "T1", "", "no owner")


def test_replay_hk_close1_unowned_pause_gets_one_notice_never_a_resume(tmp_path):
    """HK-CLOSE1, 2026-10-28: paused as a "short hold ... resumed right after", and
    still PAUSED 16 h later."""
    c, P, clk, *_ = game(tmp_path)
    c.pause("game", "T1", "short hold for the prep commits; resumed right after")
    c.store.x("UPDATE transitions SET ts=? WHERE to_state=?", (time.time() - 11 * 3600,
                                                               S.PAUSED))
    assert selfheal.holds(c, P) == []
    c.store.x("UPDATE transitions SET ts=? WHERE to_state=?", (time.time() - 16 * 3600,
                                                               S.PAUSED))
    [act] = selfheal.holds(c, P)
    assert act["evidence"]["alert_class"] == "pause_forgotten"
    [notice] = events(c, "operator_notice")
    assert notice["alert_class"] == "pause_forgotten" and "16 h" in notice["text"]
    assert selfheal.holds(c, P) == []                    # once
    assert c.store.task("game", "T1")["state"] == S.PAUSED


def test_cli_pause_with_owner_and_minutes_is_a_hold(tmp_path):
    c, P, clk, client, ff, prof = game(tmp_path)
    argv = ["--state", str(tmp_path / "state"), "--profiles", str(prof), "pause", "game", "T1",
            "--reason", "prep commits", "--owner", "prep:batch-1", "--minutes", "45"]
    assert cli.main(argv, clients={"game": client}, foundry={"game": ff}) == 0
    t = c.store.task("game", "T1")
    assert t["state"] == S.PAUSED
    assert t["data"]["hold"]["owner"] == "prep:batch-1"
    assert t["data"]["hold"]["expires_ts"] - t["data"]["hold"]["set_ts"] == pytest.approx(2700)
    c.release_hold("game", "T1", actor="victor")
    t = c.store.task("game", "T1")
    assert t["state"] == S.QUEUED and "hold" not in t["data"]


# ------------------------------------------------------------------ 6 generic reset

def test_an_unblock_with_reset_resets_every_budget_counter(tmp_path):
    c, P, clk, *_ = game(tmp_path)
    t = c.store.task("game", "T1")
    lims = c.obligations.effective_limits(P, t)
    for key, (counter, _, _) in LIMITS.items():
        if key == "guided_extra":
            continue
        for i in range(max(1, lims[key]["base_max"])):
            c.obligations.charge("game", "T1", counter, 1, f"{counter}:{i}", "engine")
    c.store.transition("game", "T1", S.BLOCKED, {"blocker": "budgets"}, blocker="budgets")
    c.unblock("game", "T1", "fresh budget", reset_attempts=True)
    lims = c.obligations.effective_limits(P, c.store.task("game", "T1"))
    for key in LIMITS:
        if key == "guided_extra":
            continue
        assert lims[key]["remaining"] == lims[key]["base_max"], key


# ------------------------------------------------------------------ bounds / settings

def test_actions_per_task_per_day_are_capped_with_a_digest_entry(tmp_path):
    c, P, clk, *_ = game(tmp_path)
    automation.set_live(P, "self_heal_max_actions_per_task_per_day", 1)
    c.hold("game", "T1", "pid:4194303", "x")
    assert len(selfheal.holds(c, P)) == 1
    c.hold("game", "T1", "pid:4194303", "x")
    assert selfheal.holds(c, P) == []
    assert c.store.task("game", "T1")["state"] == S.PAUSED
    assert len(events(c, "self_heal_exhausted")) == 1
    automation.set_live(P, "self_heal", False)
    automation.set_live(P, "self_heal_max_actions_per_task_per_day", 6)
    assert selfheal.sweep(c, P, force=True) == []


def test_sweep_runs_once_a_minute(tmp_path):
    c, P, clk, *_ = game(tmp_path)
    c.hold("game", "T1", "pid:4194303", "x")
    assert len(selfheal.sweep(c, P)) == 1
    c.hold("game", "T1", "pid:4194303", "x")
    assert selfheal.sweep(c, P) == []
    clk.advance(61)
    assert len(selfheal.sweep(c, P)) == 1


def test_every_setting_is_typed_in_the_schema():
    from foundry_automation import schema
    for k in ("self_heal", "self_heal_max_actions_per_task_per_day",
              "self_heal_dead_session_minutes", "self_heal_stuck_case_minutes",
              "self_heal_stale_lock_minutes", "self_heal_unowned_pause_hours",
              "self_heal_reverify_fixed"):
        f = schema.find_field(f"automation.{k}")
        assert f is not None and schema._AUTO_DESC.get(k), k
    assert schema.AUTOMATION_DEFAULTS["self_heal_max_actions_per_task_per_day"] == 6
    assert schema.AUTOMATION_DEFAULTS["self_heal_stuck_case_minutes"] == 30
    assert schema.AUTOMATION_DEFAULTS["self_heal_unowned_pause_hours"] == 12


# ------------------------------------------------------------------ CLI version guard

def _heartbeat(state, version, pid=None, age=0):
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "coordinator.heartbeat"), "w", encoding="utf-8") as f:
        json.dump({"pid": pid or os.getpid(), "ts": time.time() - age, "version": version}, f)


def test_cli_refuses_writing_commands_against_another_running_version(tmp_path, capsys):
    from foundry_coordinator import __version__
    c, P, clk, client, ff, prof = game(tmp_path)
    state = str(tmp_path / "state")
    c.store.transition("game", "T1", S.BLOCKED, {"blocker": "x"}, blocker="x")
    base = ["--state", state, "--profiles", str(prof)]
    unblock = base + ["unblock", "game", "T1", "--resolution", "fixed"]
    kw = {"clients": {"game": client}, "foundry": {"game": ff}}
    _heartbeat(state, "9.9.9")
    assert cli.main(unblock, **kw) == 2
    err = capsys.readouterr().err
    assert f"this CLI is v{__version__}" in err and "running coordinator is v9.9.9" in err
    assert "foundry-deploy" in err
    assert c.store.task("game", "T1")["state"] == S.BLOCKED
    assert cli.main(base + ["status"], **kw) == 0                 # read-only: runs
    assert cli.main(base + ["queue", "show"], **kw) == 0
    _heartbeat(state, "9.9.9", age=3600)                          # nothing running
    assert cli.version_guard(cli_args(unblock)) == ""
    _heartbeat(state, __version__)
    assert cli.version_guard(cli_args(unblock)) == ""
    _heartbeat(state, "9.9.9")
    assert cli.main(unblock + ["--force"], **kw) == 0
    assert c.store.task("game", "T1")["state"] == S.READY


def cli_args(argv):
    import argparse
    ns = argparse.Namespace(state=argv[1], cmd=argv[4], force_version=False)
    return ns
