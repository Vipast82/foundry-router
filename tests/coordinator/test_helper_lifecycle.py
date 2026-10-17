"""Helper requests from a REAL primary response, end to end: the primary's
coordinator-report (sent through the fake `kilo serve`, parsed by the Kilo
adapter) asks for help; the coordinator creates, binds and runs the helper,
waits a bounded time for it at the safe point, delivers its proposal once,
then verifies and reviews independently. Also: unknown/deferred quota,
handoff timeout with a late result, restarts at each safe point, duplicates,
no eligible work, pause/cancel. No predeclared units; simulated quota and
clock; no model calls."""

import json
import time

import pytest

from foundry_coordinator.adapters import build_project
from foundry_coordinator.adapters.kilo import KiloClient
from foundry_coordinator.devtools.fake_kilo import FakeKilo
from foundry_coordinator.engine import Coordinator
from foundry_coordinator.profile import load
from foundry_coordinator.store import Store
from tests.coordinator.helpers import FakeFoundry, git, make_repo, review_json, write_profile

HELPERS = """
[helpers]
enabled = {enabled}
order = ["meridian", "cpa"]
selective = ["cpa"]
agent = "au-helper"
worker_client = "kilo"
max_running = 3
poll_seconds = 30
handoff_wait_minutes = 15

[helpers.personas]
meridian = "Foundry-Kilo-Helper-Claude"
cpa = "Foundry-Kilo-Helper-GPT"
"""
T2 = {"id": "T2", "title": "S45 ordering", "spec": "Fix S45 ordering.",
      "scope": ["src/a.txt", "src/tests/*"]}          # no "helpers": nothing predeclared
CLAUDE, GPT = "Foundry-Kilo-Helper-Claude", "Foundry-Kilo-Helper-GPT"
REQ = {"kind": "test_design", "responsibility": "S45 ordering test",
       "prompt": "Design a test for the S45 ordering.", "files": ["src/tests/s45.lua", "../x.lua"]}


class Crash(BaseException):
    """A coordinator process dying mid-tick (not caught like an Exception)."""


class SeqFoundry(FakeFoundry):
    def __init__(self, seq):
        super().__init__()
        self.seq, self.asked, self.crash_bind = seq, [], None
        self.reviewer = lambda packet: review_json(packet)

    def helper_admission(self, persona):
        self.asked.append(persona)
        return super().helper_admission(persona)

    def bind(self, session_id, *a, **kw):
        if self.crash_bind and self.crash_bind(session_id, kw):
            self.crash_bind = None
            raise Crash("died before binding")
        self.seq.append(("bind", session_id, kw.get("purpose")))
        return super().bind(session_id, *a, **kw)


def admit(state, reason="fixture", nxt=None):
    return {"admit": state == "admit", "state": state, "reason": reason, "next_eligible": nxt,
            "routes": [{"source": "fixture"}]}


def report(status, reqs=None, summary="pass"):
    d = {"status": status, "summary": summary}
    if reqs is not None:
        d["helper_requests"] = reqs
    return "Work so far.\n```coordinator-report\n" + json.dumps(d) + "\n```"


def setup(tmp_path, enabled=True, behind=0.0):
    fake = FakeKilo()
    url = fake.start()
    seq = []
    fake.on_prompt = lambda f, sid, text: seq.append(("prompt", sid, text))
    root, _ = make_repo(tmp_path, "game", [T2])
    prof = load(str(write_profile(tmp_path, "game", root,
                                  extra=HELPERS.format(enabled=str(enabled).lower()))))
    # Kilo stamps messages with real time: a simulated clock that advances
    # must start behind it, or later replies would predate their prompts
    clock = [time.time() - behind]
    ff = SeqFoundry(seq)
    state = str(tmp_path / "state")

    def make():
        P = build_project(prof, state, client=KiloClient(url, password=fake.password), foundry=ff)
        return Coordinator(Store(str(tmp_path / "state" / "c.sqlite")), [P], state,
                           clock=lambda: clock[0])
    c = make()
    c.approve("game", "T2", [], "victor")
    c.tick()
    c.tick()
    assert c.store.task("game", "T2")["state"] == "IMPLEMENTING"
    return {"fake": fake, "c": c, "ff": ff, "clock": clock, "make": make, "root": root,
            "seq": seq, "primary": c.store.task("game", "T2")["session_id"]}


def advances_clock(f):
    f.advances_clock = True
    return f


@pytest.fixture()
def env(tmp_path, request):
    e = setup(tmp_path, behind=3600 if getattr(request.function, "advances_clock", False) else 0)
    yield e
    e["fake"].stop()


def task(c):
    return c.store.task("game", "T2")


def units(c):
    return c.helpers.units("game", "T2")


def helpers_of(e):
    return [s for s in e["fake"].sessions if s != e["primary"]]


def to_primary(e, needle):
    return [p for p in e["fake"].prompts if p["session_id"] == e["primary"] and needle in p["text"]]


def events(c, kind):
    return c.store.q("SELECT * FROM events WHERE task_id='T2' AND kind=?", (kind,))


def commit_ok(root):
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "a.txt").write_text("OK")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "T2: S45 ordering")


def run_to_review(e, limit=8):
    for _ in range(limit):
        e["c"].tick()
        if task(e["c"])["state"] in ("APPROVED", "CLOSING", "COMPLETE", "BLOCKED", "FAILED"):
            break
    return task(e["c"])


# ------------------------------------------------------------------ the full path

def test_report_request_to_bound_helper_to_single_delivery_to_independent_review(env):
    e, c, fake, ff = env, env["c"], env["fake"], env["ff"]
    first = [p for p in fake.prompts if p["session_id"] == e["primary"]][0]["text"]
    assert '"helper_requests"' in first and "au-helper through" in first   # request guidance
    ff.admission = {CLAUDE: admit("admit")}
    fake.finish(e["primary"], report("NOT DONE", [REQ]))
    c.tick()

    t = task(c)
    assert t["state"] == "IMPLEMENTING"                         # not straight to VERIFYING
    [u] = units(c)
    assert (u["source"], u["kind"], u["value"], u["status"]) == \
        ("report", "test_design", "normal", "running")
    assert u["files"] == ["src/tests/s45.lua"]                 # out-of-scope path dropped
    [hs] = helpers_of(e)
    assert u["session_id"] == hs and u["persona"] == CLAUDE
    b = [x for x in ff.binds if x["session_id"] == hs][0]
    assert (b["role"], b["purpose"], b["parent"]) == ("helper", "test_design", e["primary"])
    # bound before its first (and only) prompt
    order = [x[0] for x in e["seq"] if x[1] == hs]
    assert order == ["bind", "prompt"]
    assert GPT not in ff.asked                                  # normal value: not the selective one

    # primary idle, helper running: bounded wait, nothing sent to the primary
    n = len(fake.prompts)
    c.tick()
    assert task(c)["state"] == "IMPLEMENTING" and "waiting for 1 running helper" in \
        task(c)["next_action"]
    assert len(fake.prompts) == n

    fake.finish(hs, "Proposal: assert S45 fires before S46.")
    c.tick()
    assert task(c)["state"] == "IMPLEMENTING" and task(c)["session_id"] == e["primary"]
    assert len(to_primary(e, "S45 fires before S46")) == 1
    assert units(c)[0]["status"] == "delivered"
    assert (hs, "done") in ff.ended                             # helper binding ended

    commit_ok(e["root"])
    fake.finish(e["primary"], report("DONE", summary="integrated hu proposal"))
    t = run_to_review(e)
    assert t["state"] in ("APPROVED", "CLOSING", "COMPLETE"), (t["state"], t["blocker"])
    assert len(to_primary(e, "S45 fires before S46")) == 1      # never re-delivered
    assert ff.chats and all(ch["persona"] not in (CLAUDE, GPT) for ch in ff.chats)
    assert "Code authorship" in ff.chats[-1]["packet"]
    assert c.helpers.authors("game", "T2") == {CLAUDE}


# ------------------------------------------------------------------ quota not available

@pytest.mark.parametrize("state", ["unknown", "defer"])
def test_unknown_or_deferred_quota_never_holds_the_primary(env, state):
    e, c, fake, ff = env, env["c"], env["fake"], env["ff"]
    ff.admission = {CLAUDE: admit(state, "weekly window unobserved" if state == "unknown"
                                  else "5-hour 71% used", nxt=None if state == "unknown"
                                  else e["clock"][0] + 900),
                    GPT: admit("admit")}                        # selective: model can't unlock it
    fake.finish(e["primary"], report("NOT DONE", [{**REQ, "value": "high"}]))
    c.tick()
    [u] = units(c)
    assert u["status"] == "skipped" and state.upper() in u["reason"]
    assert helpers_of(e) == [] and GPT not in ff.asked
    # the primary asked and stopped: told once that no proposal is coming
    assert task(c)["state"] == "IMPLEMENTING"
    assert len(to_primary(e, "No helper proposal is coming")) == 1
    assert events(c, "helper_skipped")
    commit_ok(e["root"])
    fake.finish(e["primary"], report("DONE"))
    c.tick()
    assert task(c)["state"] != "IMPLEMENTING"
    assert len(to_primary(e, "No helper proposal is coming")) == 1


def test_done_report_with_unadmitted_request_goes_straight_to_verification(env):
    e, c, fake = env, env["c"], env["fake"]
    commit_ok(e["root"])
    fake.finish(e["primary"], report("DONE", [REQ]))           # default fake admission: unknown
    c.tick()
    assert task(c)["state"] != "IMPLEMENTING"
    assert units(c)[0]["status"] == "skipped"
    assert not to_primary(e, "No helper proposal")


# ------------------------------------------------------------------ timeout / late result

@advances_clock
def test_handoff_timeout_stops_helper_and_late_result_is_never_delivered(env):
    e, c, fake, ff, clock = env, env["c"], env["fake"], env["ff"], env["clock"]
    ff.admission = {CLAUDE: admit("admit")}
    fake.finish(e["primary"], report("NOT DONE", [REQ]))
    c.tick()
    [hs] = helpers_of(e)
    clock[0] += 14 * 60
    c.tick()
    assert units(c)[0]["status"] == "running" and task(c)["state"] == "IMPLEMENTING"
    clock[0] += 2 * 60                                          # past handoff_wait_minutes
    c.tick()
    [u] = units(c)
    assert u["status"] == "timed_out" and hs in fake.aborts and (hs, "timed_out") in ff.ended
    assert events(c, "helper_handoff_timeout")
    assert len(to_primary(e, "No helper proposal is coming")) == 1
    assert task(c)["data"].get("helper_wait_since") is None

    fake.finish(hs, "LATE proposal text")                       # arrives after the stop
    commit_ok(e["root"])
    fake.finish(e["primary"], report("DONE"))
    t = run_to_review(e)
    assert t["state"] in ("APPROVED", "CLOSING", "COMPLETE"), (t["state"], t["blocker"])
    assert not to_primary(e, "LATE proposal")
    assert not any("LATE proposal" in ch["packet"] for ch in ff.chats)
    assert c.helpers.authors("game", "T2") == set()


def test_pause_and_cancel_stop_running_helpers(env):
    e, c, fake, ff = env, env["c"], env["fake"], env["ff"]
    ff.admission = {CLAUDE: admit("admit")}
    fake.finish(e["primary"], report("NOT DONE", [REQ]))
    c.tick()
    [hs] = helpers_of(e)
    c.cancel("game", "T2", "operator stop", "victor")
    assert units(c)[0]["status"] == "cancelled" and hs in fake.aborts


# ------------------------------------------------------------------ restarts

def _restart(e):
    e["c"] = e["make"]()
    return e["c"]


def test_restart_after_requests_consumed_before_dispatch(env):
    e, c, fake, ff = env, env["c"], env["fake"], env["ff"]
    ff.admission = {CLAUDE: admit("admit")}
    real = c.helpers.tick

    def dying(P, t):
        if units(c):
            raise Crash("died after consuming the report, before dispatch")
        return real(P, t)
    c.helpers.tick = dying
    fake.finish(e["primary"], report("NOT DONE", [REQ]))
    with pytest.raises(Crash):
        c.tick()
    c.helpers.tick = real
    assert [u["status"] for u in units(c)] == ["pending"] and helpers_of(e) == []
    c = _restart(e)
    c.tick()                                     # the report isn't consumed twice
    assert len(units(c)) == 1 and units(c)[0]["status"] == "running"
    assert len(helpers_of(e)) == 1


def test_restart_between_helper_session_and_binding_binds_before_prompt(env):
    e, c, fake, ff = env, env["c"], env["fake"], env["ff"]
    ff.admission = {CLAUDE: admit("admit")}
    ff.crash_bind = lambda sid, kw: kw.get("purpose") == "test_design"
    fake.finish(e["primary"], report("NOT DONE", [REQ]))
    with pytest.raises(Crash):
        c.tick()
    [hs] = helpers_of(e)
    assert not [x for x in e["seq"] if x[1] == hs]           # neither bound nor prompted
    c = _restart(e)
    c.tick()
    assert helpers_of(e) == [hs]                               # same session, not a second one
    assert [x[0] for x in e["seq"] if x[1] == hs] == ["bind", "prompt"]
    assert units(c)[0]["status"] == "running"


def test_restart_between_binding_and_prompt_prompts_once(env):
    e, c, fake, ff = env, env["c"], env["fake"], env["ff"]
    ff.admission = {CLAUDE: admit("admit")}
    P = c.projects["game"]
    real = P.client.prompt

    def dying(sid, text, **kw):
        if sid != e["primary"]:
            raise Crash("died before the helper prompt")
        return real(sid, text, **kw)
    P.client.prompt = dying
    fake.finish(e["primary"], report("NOT DONE", [REQ]))
    with pytest.raises(Crash):
        c.tick()
    [hs] = helpers_of(e)
    c = _restart(e)
    c.tick()
    assert [x[0] for x in e["seq"] if x[1] == hs] == ["bind", "prompt"]
    assert len([p for p in fake.prompts if p["session_id"] == hs]) == 1


@advances_clock
def test_restart_while_waiting_keeps_the_bounded_wait(env):
    e, c, fake, ff, clock = env, env["c"], env["fake"], env["ff"], env["clock"]
    ff.admission = {CLAUDE: admit("admit")}
    fake.finish(e["primary"], report("NOT DONE", [REQ]))
    c.tick()
    c.tick()
    since = task(c)["data"]["helper_wait_since"]
    assert since
    c = _restart(e)
    clock[0] += 10 * 60
    c.tick()
    assert task(c)["data"]["helper_wait_since"] == since and units(c)[0]["status"] == "running"
    clock[0] += 6 * 60
    c.tick()
    assert units(c)[0]["status"] == "timed_out"


def test_restart_during_delivery_never_delivers_twice(env):
    e, c, fake, ff = env, env["c"], env["fake"], env["ff"]
    ff.admission = {CLAUDE: admit("admit")}
    fake.finish(e["primary"], report("NOT DONE", [REQ]))
    c.tick()
    [hs] = helpers_of(e)
    fake.finish(hs, "Proposal P-77")
    P = c.projects["game"]
    real = P.client.prompt

    def sent_then_died(sid, text, **kw):
        real(sid, text, **kw)
        if "Proposal P-77" in text:
            raise Crash("died after sending")
    P.client.prompt = sent_then_died
    with pytest.raises(Crash):
        c.tick()
    c = _restart(e)
    c.tick()                                     # the primary is busy with the proposal
    fake.finish(e["primary"], report("NOT DONE", summary="integrating"))
    for _ in range(2):
        c.tick()
    assert len(to_primary(e, "Proposal P-77")) == 1
    assert units(c)[0]["status"] == "delivered"                 # counted as an author
    assert events(c, "helper_delivery_uncertain")


# ------------------------------------------------------------------ duplicates / nothing eligible

def test_duplicate_requests_make_one_unit_and_one_session(env):
    e, c, fake, ff = env, env["c"], env["fake"], env["ff"]
    ff.admission = {CLAUDE: admit("admit")}
    dup = {**REQ, "responsibility": "  S45   ordering test "}
    fake.finish(e["primary"], report("NOT DONE", [REQ, dup, REQ]))
    c.tick()
    assert len(units(c)) == 1 and len(helpers_of(e)) == 1
    [hs] = helpers_of(e)
    fake.finish(hs, "Proposal D-1")
    c.tick()
    # asking again for a unit it already received: no new unit, no new session
    fake.finish(e["primary"], report("NOT DONE", [REQ], summary="again"))
    c.tick()
    assert len(units(c)) == 1 and len(helpers_of(e)) == 1
    assert len(to_primary(e, "Proposal D-1")) == 1


@advances_clock
def test_skipped_unit_can_be_requested_again_within_the_limit(env):
    e, c, fake, ff = env, env["c"], env["fake"], env["ff"]
    fake.finish(e["primary"], report("NOT DONE", [REQ], summary="ask 1"))
    c.tick()
    assert units(c)[0]["status"] == "skipped"
    ff.admission = {CLAUDE: admit("admit")}
    fake.finish(e["primary"], report("NOT DONE", [REQ], summary="ask 2"))
    c.tick()
    [u] = units(c)
    assert u["status"] == "running" and u["requests"] == 2
    e["clock"][0] += 16 * 60
    c.tick()
    assert units(c)[0]["status"] == "timed_out"
    fake.finish(e["primary"], report("NOT DONE", [REQ], summary="ask 3"))
    c.tick()
    assert units(c)[0]["status"] == "timed_out" and len(helpers_of(e)) == 1   # limit 2 reached


@pytest.mark.parametrize("reqs", [None, [], [{"kind": "review", "responsibility": ""}],
                                  ["not a dict"]])
def test_no_eligible_optional_work_goes_to_verification(env, reqs):
    e, c, fake, ff = env, env["c"], env["fake"], env["ff"]
    commit_ok(e["root"])
    fake.finish(e["primary"], report("DONE", reqs))
    c.tick()
    assert task(c)["state"] != "IMPLEMENTING"
    assert units(c) == [] and ff.asked == [] and helpers_of(e) == []


def test_disabled_helpers_ignore_requests(tmp_path):
    e = setup(tmp_path, enabled=False)
    try:
        c, fake = e["c"], e["fake"]
        first = [p for p in fake.prompts if p["session_id"] == e["primary"]][0]["text"]
        assert "helper_requests" not in first
        fake.finish(e["primary"], report("NOT DONE", [REQ]))
        c.tick()
        assert task(c)["state"] != "IMPLEMENTING" and units(c) == []
    finally:
        e["fake"].stop()
