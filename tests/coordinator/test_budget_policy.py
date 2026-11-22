"""Budgets and time limits follow plan progress (budget_policy.py; items 9 and 10 of
COORDINATOR_AUTOMATION_NEEDS). The cases are last week's hand resets: FIN-4a ("33 sessions",
reset 11-19 and 11-20 with BUILD steps still open), FIN-2c ("32 sessions", 9 steps not
ticked), FIN-2a (9 of 14 rows done), and POL-7's max_task_minutes raised by hand five
times (1440 -> 3360)."""

from __future__ import annotations

import json

import pytest

from foundry_coordinator import automation, budget_policy, cli, infra
from foundry_coordinator import states as S
from foundry_coordinator.obligations import RENEWAL_ACTOR, LedgerRefused
from tests.coordinator.test_lifecycle_e2e import LifecycleAdapter, green_t1
from tests.coordinator.test_lifecycle_engine import _game
from tests.coordinator.test_selfheal import Clock

DAY = 86400


def game(tmp_path, **limits):
    clk = Clock()
    c, client, ff, root, prof = _game(tmp_path, lambda ctx: green_t1(ctx), clock=clk,
                                      store_path=tmp_path / "state" / "coordinator.sqlite")
    P = c.projects["game"]
    P.profile.limits.update(limits)
    c.cli_args = {"prof": str(prof), "clients": {"game": client}, "foundry": {"game": ff}}
    c.approve("game", "T1", [], "victor")
    c.store.transition("game", "T1", S.READY, {"deps_satisfied": True})
    return c, P, clk


def task(c):
    return c.store.task("game", "T1")


def tick_step(c, P, n, sid=None):
    """A session of the sprint ticked plan step `n` (what _lc_session_ended records)."""
    before = [["done" if i < n else "open", f"{i} step {i}"] for i in range(1, 11)]
    after = [["done" if i <= n else "open", f"{i} step {i}"] for i in range(1, 11)]
    c.recovery.mark_session(P, task(c), sid or f"ses_{n}_{int(c.clock())}", prev_head="a" * 40,
                            head=f"{n:040d}", checklist={"before": before, "after": after})


def use(c, counter, n):
    for i in range(n):
        c.obligations.charge("game", "T1", counter, 1, f"test:{counter}:{c.clock()}:{i}", "engine")


def room(c, P, key="sessions"):
    return c.obligations.effective_limits(P, task(c))[key]


def notices(c, kind):
    return [json.loads(r["detail"]) for r in c.store.q(
        "SELECT detail FROM events WHERE kind='operator_notice' ORDER BY id")
        if json.loads(r["detail"]).get("kind") == kind]


def events(c, kind):
    return [json.loads(r["detail"]) for r in c.store.q(
        "SELECT detail FROM events WHERE kind=? ORDER BY id", (kind,))]


# ------------------------------------------------------------------ renewal

def test_replay_out_of_sessions_while_ticking_steps_is_renewed_and_reported(tmp_path):
    """FIN-4a 11-20: "33 sessions used", 6 BUILD steps left, gates passing. The operator
    asked ("Yes, reset it") and ran unblock --reset-attempts. Now the sprint that ticked a
    step since its last renewal gets its budget back by itself, once a day, and says so."""
    c, P, clk = game(tmp_path, max_sessions_per_task=4, max_attempts=3)
    use(c, "sessions", 4)
    use(c, "attempt_ordinary", 2)
    clk.advance(60)
    tick_step(c, P, 4)
    assert room(c, P)["remaining"] == 0
    # the session start's budget check: no stop, the attempt is charged as usual
    assert c._lc_budget(P, task(c), free=False) == ("attempt_ordinary", False)
    assert task(c)["state"] == S.READY
    assert room(c, P)["remaining"] == 4 and room(c, P, "ordinary")["remaining"] == 3
    rows = [r for r in c.obligations.ledger("game", "T1")
            if str(r["decision_id"] or "").startswith("renewal:")]
    assert {r["counter"] for r in rows} == {"authorized:sessions", "authorized:attempt_ordinary"}
    assert {r["actor"] for r in rows} == {RENEWAL_ACTOR}
    [n] = notices(c, "budget_renewed")
    assert n["digest"] is True and n["ran_out"] == "sessions" and n["counters"]["sessions"] == 4
    assert n["steps"] == ["step 4: open -> done"] and n["renewals_today"] == 1
    assert "budget renewed by itself" in n["text"] and "Renewal 1 of 1 in a day" in n["text"]
    # the same day again, another step ticked: the day's renewal is used, it stops as before
    use(c, "sessions", 4)
    clk.advance(3600)
    tick_step(c, P, 5)
    assert c._lc_budget(P, task(c), free=False) == (None, True)
    t = task(c)
    assert t["state"] == S.FAILED or "sessions used" in str(t.get("blocker") or t.get("next_action"))
    [skip] = events(c, "budget_renewal_skipped")
    assert skip["why"] == "1/1 renewals in the last day" and skip["steps_ticked"] == 1
    assert len(notices(c, "budget_renewed")) == 1


def test_no_step_ticked_since_the_last_renewal_goes_to_the_owner_as_before(tmp_path):
    """POL-7 11-18: 59 sessions, the code approved, only visual findings going round. No
    plan step moved, so nothing renews it: that one is the owner's call."""
    c, P, clk = game(tmp_path, max_sessions_per_task=3)
    use(c, "sessions", 3)
    assert budget_policy.try_renew(c, P, task(c), "sessions") is False
    assert events(c, "budget_renewal_skipped")[0]["why"] == \
        "no plan step was ticked since the last renewal"
    tick_step(c, P, 1)
    assert budget_policy.try_renew(c, P, task(c), "sessions") is True
    # a day later: the step above was before that renewal, so it does not count again
    use(c, "sessions", 3)
    clk.advance(DAY + 60)
    assert budget_policy.try_renew(c, P, task(c), "sessions") is False
    tick_step(c, P, 2)
    assert budget_policy.try_renew(c, P, task(c), "sessions") is True
    assert [n["renewals_today"] for n in notices(c, "budget_renewed")] == [1, 1]


def test_attempts_and_review_calls_renew_the_same_way(tmp_path):
    c, P, clk = game(tmp_path, max_attempts=2, max_review_calls=2, max_sessions_per_task=9)
    use(c, "attempt_ordinary", 2)
    use(c, "review_calls", 2)
    use(c, "sessions", 5)
    tick_step(c, P, 3)
    assert c._lc_budget(P, task(c), free=False) == ("attempt_ordinary", False)
    # one renewal brings every counter back to its base room, not only the one that ran out
    for key in ("ordinary", "review_calls", "sessions"):
        r = room(c, P, key)
        assert r["remaining"] == r["base_max"], key
    assert notices(c, "budget_renewed")[0]["ran_out"] == "attempt_ordinary"


def test_off_and_projects_without_the_ledger_never_renew(tmp_path):
    c, P, clk = game(tmp_path, max_sessions_per_task=2)
    use(c, "sessions", 2)
    tick_step(c, P, 1)
    automation.set_live(P, "budget_renewals_per_day", 0)
    assert budget_policy.try_renew(c, P, task(c), "sessions") is False
    automation.set_live(P, "budget_renewals_per_day", 2)
    assert budget_policy.try_renew(c, P, task(c), "sessions") is True
    use(c, "sessions", 2)
    clk.advance(600)
    tick_step(c, P, 2)
    assert budget_policy.try_renew(c, P, task(c), "sessions") is True     # 2 a day allowed
    use(c, "sessions", 2)
    clk.advance(600)
    tick_step(c, P, 3)
    assert budget_policy.try_renew(c, P, task(c), "sessions") is False
    assert events(c, "budget_renewal_skipped")[-1]["why"] == "2/2 renewals in the last day"


def test_the_ledger_itself_refuses_a_renewal_it_should_not_get(tmp_path):
    c, P, clk = game(tmp_path, max_sessions_per_task=2)
    use(c, "sessions", 2)
    ob = c.obligations

    def give(ref, actor=RENEWAL_ACTOR, counter="sessions", n=2):
        return ob.authorize("game", "T1", counter, n, "x", actor, ref)
    with pytest.raises(LedgerRefused, match="no plan step was ticked"):
        give("renewal:sessions:k1")
    tick_step(c, P, 1)
    with pytest.raises(LedgerRefused, match="only the 'renewal' actor"):
        give("renewal:sessions:k1", actor="operator")
    with pytest.raises(LedgerRefused, match="renewable counter"):
        give("renewal:closeout:k1", counter="closeout")
    with pytest.raises(LedgerRefused, match="renewable counter"):
        give("renewal:sessions:k1", counter="review_calls")
    assert give("renewal:sessions:k1")
    assert give("renewal:review_calls:k1", counter="review_calls", n=1)     # the same renewal
    with pytest.raises(LedgerRefused, match="already authorized"):
        give("renewal:sessions:k1")
    clk.advance(60)
    tick_step(c, P, 2)
    with pytest.raises(LedgerRefused, match="1/1 budget renewals"):
        give("renewal:sessions:k2")                                        # a second one today


# ------------------------------------------------------------------ time

def _plan(monkeypatch, c, open_steps, total):
    monkeypatch.setattr(LifecycleAdapter, "pending_build_steps",
                        lambda self, spec, rev, ws: [f"- [ ] step {i}" for i in range(open_steps)],
                        raising=False)
    monkeypatch.setattr(type(c), "_build_total", lambda self, P, spec, head: total)


def test_the_suggestion_is_what_the_open_steps_need_at_the_sprints_own_pace(tmp_path,
                                                                           monkeypatch):
    c, P, clk = game(tmp_path, max_task_minutes=1440)
    _plan(monkeypatch, c, open_steps=6, total=10)
    # 600 active minutes for 4 steps = 150 a step; 6 open = 900, plus the close-out
    want, basis = budget_policy.suggest_minutes(c, P, task(c), 600 * 60)
    assert want == 960 and basis == "6 of 10 plan steps open at 150 min a step, plus the close-out"
    _plan(monkeypatch, c, open_steps=0, total=10)
    assert budget_policy.suggest_minutes(c, P, task(c), 600 * 60)[0] == 60
    _plan(monkeypatch, c, open_steps=10, total=10)
    assert budget_policy.suggest_minutes(c, P, task(c), 600 * 60)[0] == 360     # nothing done yet
    monkeypatch.setattr(LifecycleAdapter, "pending_build_steps",
                        lambda self, spec, rev, ws: None, raising=False)
    want, basis = budget_policy.suggest_minutes(c, P, task(c), 600 * 60)
    assert want == 360 and basis.startswith("no plan checklist")


def test_replay_pol7_a_sprint_over_its_minutes_that_ticks_steps_extends_itself(tmp_path,
                                                                             monkeypatch):
    """POL-7: max_task_minutes raised by hand 1440 -> 1920 -> 2400 -> 2640 -> 3360 between
    11-16 and 11-18, a restart each time. The extra now belongs to the sprint, is sized
    from its open steps, never more than the limit again per day, and needs no restart."""
    c, P, clk = game(tmp_path, max_task_minutes=1440)
    _plan(monkeypatch, c, open_steps=2, total=10)
    c.store.update_task("game", "T1", started_ts=clk.t - 30 * 3600)
    t = task(c)
    assert budget_policy.minutes_limit(P, t) == 1440
    assert "no plan step was ticked" in budget_policy.try_extend_time(c, P, t, 1450 * 60)
    tick_step(c, P, 8)
    assert budget_policy.try_extend_time(c, P, task(c), 1450 * 60) == ""
    t = task(c)
    # 1450 min / 8 steps = 181 a step; 2 open = 363, plus 60
    assert t["data"]["task_minutes_extra"] == 423
    assert budget_policy.minutes_limit(P, t) == 1863
    assert P.profile.limit("max_task_minutes", 0) == 1440            # the project's is untouched
    [n] = notices(c, "time_extended")
    assert n["minutes"] == 423 and n["limit"] == 1863 and n["base"] == 1440
    assert "by itself" in n["text"] and "2 of 10 plan steps open" in n["text"]
    # the same extension does not buy another: a step has to be ticked after it
    assert "no plan step" in budget_policy.try_extend_time(c, P, task(c), 1870 * 60)
    # never more than the base limit again per day
    _plan(monkeypatch, c, open_steps=5, total=10)
    clk.advance(60)
    tick_step(c, P, 9)
    assert budget_policy.try_extend_time(c, P, task(c), 1870 * 60) == ""
    assert task(c)["data"]["task_minutes_extra"] == 1440              # 423 + 1017, not + 1930
    clk.advance(60)
    tick_step(c, P, 10)
    assert "1440 of 1440 extra minutes" in budget_policy.try_extend_time(c, P, task(c), 3000 * 60)
    # the next day it may go on
    clk.advance(DAY)
    tick_step(c, P, 10, sid="ses_next_day")
    assert budget_policy.try_extend_time(c, P, task(c), 3000 * 60) == ""


def test_the_session_start_extends_or_stops_with_the_amount_to_give(tmp_path, monkeypatch):
    c, P, clk = game(tmp_path, max_task_minutes=100)
    _plan(monkeypatch, c, open_steps=3, total=6)
    c.store.update_task("game", "T1", started_ts=clk.t - 7200)
    monkeypatch.setattr(infra, "active_seconds", lambda c_, t_, now=None: 120 * 60)
    # no step ticked: the stop, and its reason says what to give
    c._start_implementation(P, task(c))
    t = task(c)
    assert t["state"] == S.FAILED
    why = c.store.history("game", "T1")[-1]["evidence"]["reason"]
    assert why.startswith("over max_task_minutes (100 active minutes); not extended by itself: "
                          "no plan step was ticked since the last extension.")
    assert "If it should go on: 180 more minutes (3 of 6 plan steps open at 40 min a step" in why
    assert "`extend-time game T1 --minutes 180`" in why
    # the owner gives it: live, this sprint only, and the stopped sprint goes on
    out = c.extend_time("game", "T1", 180, actor="victor", note="keep going")
    assert out["limit"] == 280 and out["by"] == "victor"
    t = task(c)
    assert t["state"] == S.READY and t["data"]["task_minutes_extra"] == 180
    assert P.profile.limit("max_task_minutes", 0) == 100
    assert notices(c, "time_extended")[-1]["by"] == "victor"
    # under its own limit now: no stop
    monkeypatch.setattr(infra, "active_seconds", lambda c_, t_, now=None: 200 * 60)
    assert budget_policy.minutes_limit(P, t) == 280
    with pytest.raises(ValueError):
        c.extend_time("game", "T1", 0)


def test_extending_by_itself_can_be_switched_off(tmp_path, monkeypatch):
    c, P, clk = game(tmp_path, max_task_minutes=100)
    _plan(monkeypatch, c, open_steps=3, total=6)
    tick_step(c, P, 3)
    automation.set_live(P, "task_minutes_auto_extend", False)
    assert "switched off" in budget_policy.try_extend_time(c, P, task(c), 120 * 60)
    assert "task_minutes_extra" not in (task(c)["data"] or {})
    P.profile.limits["max_task_minutes"] = 0
    assert budget_policy.minutes_limit(P, task(c)) == 0                 # no limit: nothing to do


def test_cli_extend_time_and_the_settings_in_the_schema(tmp_path, capsys):
    c, P, clk = game(tmp_path)
    # the CLI loads the profile from its file: the limit has to be in it
    with open(c.cli_args["prof"], encoding="utf-8") as f:
        text = f.read()
    assert "[limits]" in text and "max_task_minutes" not in text
    with open(c.cli_args["prof"], "w", encoding="utf-8") as f:
        f.write(text.replace("[limits]", "[limits]" + chr(10) + "max_task_minutes = 100", 1))
    rc = cli.main(["--state", str(tmp_path / "state"), "--profiles", c.cli_args["prof"],
                   "extend-time", "game", "T1", "--minutes", "240", "--by", "victor",
                   "--note", "four more hours"],
                  clients=c.cli_args["clients"], foundry=c.cli_args["foundry"])
    out = capsys.readouterr().out
    assert rc == 0, out
    line = next(ln for ln in out.splitlines() if ln.startswith("{"))
    assert json.loads(line)["limit"] == 340
    assert c.store.task("game", "T1")["data"]["task_minutes_extra"] == 240
    assert ("extend-time", None) in cli._WRITING
    from foundry_automation import schema
    for k, default in (("budget_renewals_per_day", 1), ("task_minutes_auto_extend", True)):
        assert schema.find_field(f"automation.{k}") is not None and schema._AUTO_DESC[k]
        assert schema.AUTOMATION_DEFAULTS[k] == default
