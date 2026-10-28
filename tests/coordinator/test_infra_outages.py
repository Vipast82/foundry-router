"""C (automation gap audit, 2026-10-28): outages and infrastructure are never failures.

  - Kilo "retry" for infra_retry_abort_seconds -> the session stops as a free infra wait
  - a lead call that dies on infrastructure (retry, dead session, Kilo unreachable)
    defers without counting against max_diagnoses / lead_hard_max_calls
  - the busy-session context guard covers lead sessions; write-access leads get 90 min
  - a failed push is retried with backoff in CLOSING, never a closeout finding
  - new infra signatures; "500 ms" is not a 5xx; the preflight probes the router
  - a session that made progress wraps up for free before session_timeout_minutes;
    the compaction loop end is free; max_task_minutes counts active time only
"""

import json
import os
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest

from foundry_coordinator import context_window as CW
from foundry_coordinator import infra
from foundry_coordinator import states as S
from foundry_coordinator.adapters.base import SessionState
from foundry_coordinator.autoheal import infra_signature
from foundry_coordinator.lead import LeadManager
from tests.coordinator.test_context_guard import NOT_DONE, BusyKilo
from tests.coordinator.test_lead_e2e import Clock
from tests.coordinator.test_lead_manager import env as lead_env  # noqa: F401  (fixture)
from tests.coordinator.test_lead_manager import open_case, reply, report, state_is, task
from tests.coordinator.test_revamp_infra import FlakyKilo, env, events, good


# ------------------------------------------------------------------ signatures / preflight

@pytest.mark.parametrize("text,sig", [
    ("HTTP 429 Too Many Requests", "rate_limit"),
    ("Rate limit reached for claude-opus", "rate_limit"),
    ("You exceeded your current quota", "rate_limit"),
    ("provider error: quota exceeded", "rate_limit"),
    ("TypeError: fetch failed", "connection"),
    ("connect ETIMEDOUT 10.0.0.2:443", "connection"),
    ("getaddrinfo ENOTFOUND router.local", "connection"),
    ("Error: socket hang up", "connection"),
    ("read ECONNRESET", "connection"),
    ("the request (40960 tokens) exceeds the available context size (32768 tokens)",
     "context_overflow"),
    ("HTTP 503 Service Unavailable", "backend_5xx"),
    ("Kilo kept retrying the model call for 301 s (router or model outage)", "kilo_retry"),
])
def test_new_infra_signatures(text, sig):
    assert infra_signature(text) == sig


@pytest.mark.parametrize("text", ["the gate took 500 ms", "frame budget 502ms exceeded by 3",
                                  "429 tokens in the summary", "RateLimiter test fails"])
def test_quantities_and_product_words_are_not_infra(text):
    assert infra_signature(text) == ""


def test_preflight_probes_the_router_health(monkeypatch):
    probed = []
    monkeypatch.setattr(infra, "check_endpoint",
                        lambda url, timeout=3.0: probed.append(url) or "connection refused")
    prof = SimpleNamespace(foundry={"url": "http://127.0.0.1:8000/v1"}, workspace=".",
                           automation={})
    P = SimpleNamespace(id="g", profile=prof, client=None, adapter=None,
                        foundry=SimpleNamespace(url="http://127.0.0.1:8000/v1"))
    monkeypatch.setattr(infra, "auto_get", lambda P, k: {"preflight": True}.get(k, []))
    c = SimpleNamespace(clock=time.time)
    bad = infra.preflight(c, P, "")
    assert probed == ["http://127.0.0.1:8000/health"]
    assert bad == [{"name": "router", "detail": "http://127.0.0.1:8000/health: "
                                                "connection refused"}]
    # an explicit router entry is not probed twice; no Foundry client, no probe
    monkeypatch.setattr(infra, "auto_get", lambda P, k: {
        "preflight": True, "preflight_endpoints": ["router=http://r:1/health"]}.get(k, []))
    assert infra.router_probe(P) == []
    P.foundry = object()
    monkeypatch.setattr(infra, "auto_get", lambda P, k: {"preflight": True}.get(k, []))
    assert infra.router_probe(P) == []


# ------------------------------------------------------------------ active task time

def test_task_minutes_count_active_states_only():
    rows = [{"to_state": "READY", "ts": 1000.0}, {"to_state": "IMPLEMENTING", "ts": 1100.0},
            {"to_state": "PAUSED", "ts": 1700.0},              # 600 s active
            {"to_state": "IMPLEMENTING", "ts": 50000.0},       # overnight pause: free
            {"to_state": "VERIFYING", "ts": 50300.0},
            {"to_state": "BLOCKED", "ts": 50400.0},            # 400 s active
            {"to_state": "READY", "ts": 90000.0}]
    c = SimpleNamespace(store=SimpleNamespace(history=lambda pid, tid: rows))
    t = {"project_id": "g", "task_id": "T1", "started_ts": 1100.0}
    assert infra.active_seconds(c, t) == 1000.0
    rows.append({"to_state": "CLOSING", "ts": 90010.0})       # open interval: to `now`
    assert infra.active_seconds(c, t, now=90110.0) == 1100.0


def test_max_task_minutes_ignores_parked_time(tmp_path):
    clock = Clock()
    c = env(tmp_path, FlakyKilo(good, down=False), clock)
    c.projects["game"].profile.limits["max_task_minutes"] = 30
    # the task was started two days ago and sat parked: wall time far over 30 min
    c.store.update_task("game", "T1", started_ts=clock() - 2 * 86400)
    for _ in range(12):
        c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.COMPLETE, (t["state"], t["next_action"])


# ------------------------------------------------------------------ Kilo retry (engine)

class RetryKilo(BusyKilo):
    """The first session sits in Kilo's retry state (router down) until aborted."""

    def state(self, session_id, *, directory, since_ts):
        s = self.sessions.get(session_id)
        if s is not None and s.get("busy"):
            return SessionState(status="busy", retry=True, error="fetch failed")
        return super().state(session_id, directory=directory, since_ts=since_ts)


def test_kilo_retry_stops_the_session_as_a_free_infra_wait(tmp_path):
    clock = Clock()
    k = RetryKilo(good, [])
    c = env(tmp_path, k, clock)
    for _ in range(40):
        c.tick()
        clock.advance(31)
        if c.store.task("game", "T1")["state"] == S.COMPLETE:
            break
    t = c.store.task("game", "T1")
    assert t["state"] == S.COMPLETE, (t["state"], t["next_action"])
    assert len(k.aborts) == 1
    [a] = events(c, "kilo_retry_abort")
    assert 300 <= a["seconds"] < 400 and a["error"] == "fetch failed"
    assert [s["attempt"] for s in k.starts] == [1, 1]          # no attempt charged
    waits = events(c, "infra_wait")
    assert waits and waits[0]["kind"] == "session" and waits[0]["delay_s"] >= 60


# ------------------------------------------------------------------ session timeout wrap-up

def _commit(ws, rel, text, msg):
    with open(os.path.join(ws, rel), "w", encoding="utf-8") as f:
        f.write(text)
    subprocess.run(["git", "-C", ws, "add", rel], check=True)
    subprocess.run(["git", "-C", ws, "commit", "-q", "-m", msg], check=True)


def test_progress_near_the_session_timeout_wraps_up_free(tmp_path):
    clock = Clock()
    prompts = []

    def script(ctx):
        prompts.append(ctx["prompt"])
        if "is stopped at the limit" in ctx["prompt"]:
            return {"final": NOT_DONE}
        return good(ctx)
    k = BusyKilo(script, [])
    c = env(tmp_path, k, clock)
    P = c.projects["game"]
    P.profile.limits["session_timeout_minutes"] = 20
    for _ in range(3):
        c.tick()
    assert c.store.task("game", "T1")["state"] == S.IMPLEMENTING and k.starts
    _commit(P.profile.workspace, "src/a.txt", "half\n", "T1: wip half")    # the session's work
    clock.advance(6 * 60)                                                # 14 min left
    for _ in range(30):
        c.tick()
        clock.advance(31)
        if c.store.task("game", "T1")["state"] == S.COMPLETE:
            break
    t = c.store.task("game", "T1")
    assert t["state"] == S.COMPLETE, (t["state"], t["next_action"])
    assert len(k.aborts) == 1
    [w] = events(c, "timeout_wrapup")
    assert w["timeout"] == 20 and "HEAD moved" in w["progress"]
    assert sum("is stopped at the limit" in p for p in prompts) == 1
    assert [s["attempt"] for s in k.starts] == [1, 1]          # the handoff was free
    assert any("timeout wrap-up" in h["why"] for h in events(c, "context_handoff"))


def test_no_progress_at_the_timeout_is_still_charged(tmp_path):
    clock = Clock()
    k = BusyKilo(good, [])
    c = env(tmp_path, k, clock)
    c.projects["game"].profile.limits["session_timeout_minutes"] = 20
    for _ in range(3):
        c.tick()
    clock.advance(21 * 60)
    for _ in range(12):
        c.tick()
        clock.advance(31)
    assert not events(c, "timeout_wrapup")
    assert [s["attempt"] for s in k.starts][:2] == [1, 2]


# ------------------------------------------------------------------ compaction loop

def test_the_compaction_loop_end_is_free(tmp_path):
    clock = Clock()

    class CompactKilo(BusyKilo):
        def state(self, session_id, *, directory, since_ts):
            s = self.sessions.get(session_id)
            if s is not None and s.get("busy"):
                return SessionState(status="busy", compaction={"message_id": "m1"})
            return super().state(session_id, directory=directory, since_ts=since_ts)

        def prompt(self, session_id, text, *, directory, persona, agent):
            super().prompt(session_id, text, directory=directory, persona=persona, agent=agent)
            if "compacted" in text and session_id == self.loop_sid:   # still compacting
                self.sessions[session_id]["busy"] = True
                self.sessions[session_id]["pending"] = False

    k = CompactKilo(good, [])
    k.loop_sid = None
    real_start = k.start_session

    def start(**kw):
        sid = real_start(**kw)
        k.loop_sid = k.loop_sid or sid           # only the first session loops
        return sid
    k.start_session = start
    c = env(tmp_path, k, clock)
    for _ in range(40):
        c.tick()
        clock.advance(31)
        if c.store.task("game", "T1")["state"] == S.COMPLETE:
            break
    assert c.store.task("game", "T1")["state"] == S.COMPLETE
    assert len(k.starts) == 2 and [s["attempt"] for s in k.starts] == [1, 1]


# ------------------------------------------------------------------ push retry

def test_a_failed_push_is_retried_with_backoff_not_a_closeout_finding(tmp_path):
    clock = Clock()
    k = FlakyKilo(good, down=False)
    c = env(tmp_path, k, clock)
    remote = tmp_path / "game-remote.git"
    gone = tmp_path / "game-remote.gone"
    for _ in range(30):
        st = c.store.task("game", "T1")["state"]
        if st == S.CLOSING and not gone.exists():
            shutil.move(str(remote), str(gone))              # the remote goes away
        c.tick()
        if c.store.task("game", "T1")["data"].get("push_wait"):
            break
    t = c.store.task("game", "T1")
    pw = t["data"]["push_wait"]
    assert t["state"] == S.CLOSING and pw["n"] == 1 and "infra wait" in t["next_action"]
    sessions = len(k.starts)
    for _ in range(3):                                       # not due: nothing happens
        c.tick()
    assert c.store.task("game", "T1")["data"]["push_wait"]["n"] == 1
    clock.advance(61)
    c.tick()                                                 # still down: retry #2 in 120 s
    assert c.store.task("game", "T1")["data"]["push_wait"]["n"] == 2
    shutil.move(str(gone), str(remote))
    clock.advance(121)
    c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.COMPLETE, (t["state"], t["next_action"])
    assert len(k.starts) == sessions                         # no paid session for a push
    assert not any(h["to_state"] == S.CHANGES_REQUIRED for h in c.store.history("game", "T1"))
    assert events(c, "push_retry_ok")


# ------------------------------------------------------------------ lead sessions

class _Clk:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


def _lead(e):
    clk = _Clk()
    e["lm"] = LeadManager(e["c"], clock=clk)
    return e["lm"], clk


def _drive(e, case, until, clk, step_s=0.0, limit=60):
    for _ in range(limit):
        case = e["lm"].step(e["P"], task(e), case)
        if until(case):
            return case
        clk.t += step_s
        time.sleep(0.02)
    raise AssertionError([(a["kind"], a["status"], a["result"], a["detail"])
                          for a in e["lm"].actions(case["case_id"])])


def _uncounted(e, case):
    call = next(a for a in e["lm"].actions(case["case_id"]) if a["kind"] == "lead_call")
    assert call["detail"].get("infra"), call
    assert case["counters"].get("diagnoses", 0) == 0
    assert case["counters"].get("quota_calls", 0) == 0
    assert e["lm"].lifetime_calls("game", "T1") == 0
    assert e["lm"].task_totals("game", "T1")["lead_calls"] == 0
    return call


def test_lead_call_in_kilo_retry_defers_uncounted(lead_env):
    e = lead_env
    lm, clk = _lead(e)
    e["fake"].on_prompt = lambda f, sid, text: f.set_status(sid, "retry")
    case = _drive(e, open_case(e), state_is("DEFERRED", "NEEDS_VICTOR"), clk, step_s=120)
    assert case["state"] == "DEFERRED"
    call = _uncounted(e, case)
    assert "kept retrying" in call["result"]
    assert e["P"].client.starts[0]["session_id"] in e["fake"].aborts


def test_dead_lead_session_context_overflow_defers_uncounted(lead_env):
    e = lead_env
    lm, clk = _lead(e)

    def die(f, sid, text):
        f.add_message(sid, "assistant", [{"type": "text", "text": ""}],
                      error={"name": "ContextOverflowError", "data": {"message": "too long"}})
        f.set_status(sid, None)
    e["fake"].on_prompt = die
    case = _drive(e, open_case(e), state_is("DEFERRED", "NEEDS_VICTOR"), clk)
    assert case["state"] == "DEFERRED"
    call = _uncounted(e, case)
    assert "context_overflow" in call["result"]
    sid = e["P"].client.starts[0]["session_id"]
    assert e["c"].store.session(sid)["status"] == "ended"


def test_stalled_lead_session_is_detected(lead_env):
    e = lead_env
    lm, clk = _lead(e)

    def stall(f, sid, text):                  # Kilo restarted mid-turn: never completes
        f.add_message(sid, "assistant", [{"type": "text", "text": "partial"}], completed=False)
        f.set_status(sid, None)
    e["fake"].on_prompt = stall
    case = _drive(e, open_case(e), state_is("DEFERRED", "NEEDS_VICTOR"), clk, step_s=120)
    assert case["state"] == "DEFERRED"
    assert "dead session" in _uncounted(e, case)["result"]


def test_lead_context_guard_asks_for_the_report(lead_env, monkeypatch):
    e = lead_env
    lm, clk = _lead(e)
    monkeypatch.setattr(CW, "wrapup", lambda P, persona: 1000)
    monkeypatch.setattr(CW, "hard_stop", lambda P, persona: 10 ** 9)
    monkeypatch.setattr(CW, "context_of", lambda tr: 5000)

    def on_prompt(f, sid, text):
        if "Do NOT investigate further" in text:
            f.finish(sid, reply(report()))
        else:
            f.set_status(sid, "busy")
    e["fake"].on_prompt = on_prompt
    # the clock stays put: Kilo's message times are real, the prompt time is this clock
    case = _drive(e, open_case(e), state_is("RESUMING", "DEFERRED", "NEEDS_VICTOR"), clk)
    assert case["state"] == "RESUMING"                       # the wrap-up report was used
    acts = lm.actions(case["case_id"])
    assert any(a["kind"] == "context_wrapup" for a in acts)
    sid = e["P"].client.starts[0]["session_id"]
    assert sid in e["fake"].aborts and len(e["P"].client.starts) == 1


def test_write_access_lead_timeout_defaults_to_90(tmp_path):
    from foundry_automation.schema import lead_manager_table
    base = {"enabled": True, "persona": "L", "agent": "lead", "kilo_client": "kilo",
            "expected_models": ["m"]}
    assert lead_manager_table("p", base)["lead_timeout_minutes"] == 45
    assert lead_manager_table("p", {**base, "write_access": True})[
        "lead_timeout_minutes"] == 90
    assert lead_manager_table("p", {**base, "write_access": True,
                                    "lead_timeout_minutes": 30})["lead_timeout_minutes"] == 30
