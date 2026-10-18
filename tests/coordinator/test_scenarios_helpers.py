"""Scenario review of the optional-helper and capacity paths, FAKES ONLY.

Every scenario drives the real coordinator (engine + HelperManager + the Kilo
adapter) against the fake `kilo serve` (with fault injection added here) and a
fake Foundry, on a simulated clock. Nothing here starts a real coordinator,
Kilo, Foundry or any inference; the one HTTP server besides the fake Kilo is a
localhost stub used to observe FoundryClient's error mapping.

Scenarios
  S1  helper model filtered by Kilo / async startup failure after prompt_async
  S2  quota deferral: DEFER / UNKNOWN / stale-only refresh -> re-poll admits
  S3  backend outage: admission raises, times out or Foundry is unreachable
  S4  long-running helper work: timeout, cancellation, no duplicate dispatch
  S5  control channel fails while reads work (and the reverse)

A test that asserts the CORRECT behaviour of a path that currently misbehaves
is marked xfail(strict=True) with the defect in its reason.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from foundry_coordinator.adapters import build_project
from foundry_coordinator.adapters.foundry import FoundryClient, FoundryError
from foundry_coordinator.adapters.kilo import KiloClient
from foundry_coordinator.devtools.fake_kilo import FakeKilo
from foundry_coordinator.engine import Coordinator
from foundry_coordinator.profile import load
from foundry_coordinator.store import Store
from tests.coordinator.helpers import FakeFoundry, make_repo, write_profile
from tests.coordinator.test_helpers_cycle import HELPERS, T1

CLAUDE, GPT = "Foundry-Kilo-Helper-Claude", "Foundry-Kilo-Helper-GPT"
S45, GALLERY = "S45 timing test design", "gallery layout proposal"
POLL = 30            # [helpers] poll_seconds in HELPERS
TIMEOUT_MIN = 30     # [helpers] timeout_minutes default


# ---------------------------------------------------------------- fakes

class FaultKilo(FakeKilo):
    """The fake `kilo serve` plus fault injection.

    get_fault(path) -> True: that GET answers HTTP 500.
    post_fault(path, body) -> "before": answer 500 without any effect;
                              "after":  apply the request, then answer 500
                                        (accepted, but the response is lost).
    retry[sid] = message: /session/status reports {"type": "retry", "message"}.
    """

    def __init__(self):
        super().__init__()
        self.get_fault = None
        self.post_fault = None
        self.retry: dict[str, str] = {}

    def get(self, path, q):
        if self.get_fault and self.get_fault(path):
            return 500, {"error": "injected read failure"}
        code, body = super().get(path, q)
        if path == "/session/status" and code == 200:
            for sid, msg in self.retry.items():
                body[sid] = {"type": "retry", "message": msg}
        return code, body

    def post(self, path, q, body):
        mode = self.post_fault(path, body) if self.post_fault else None
        if mode == "before":
            return 500, {"error": "injected control failure"}
        code, out = super().post(path, q, body)
        if mode == "after":
            return 500, {"error": "injected: accepted, response lost"}
        return code, out


class ScenarioFoundry(FakeFoundry):
    """Fake Foundry. admission: persona -> answer, or a callable(persona)
    returning an answer or raising. bind_error: raised by bind for helpers."""

    def __init__(self):
        super().__init__()
        self.asked: list[str] = []
        self.answer = None
        self.bind_error = None

    def helper_admission(self, persona):
        self.asked.append(persona)
        if self.answer is not None:
            return self.answer(persona)
        return super().helper_admission(persona)

    def bind(self, session_id, project_id, task_id, run_id, role, persona, parent="", meta=None,
             purpose="", worker_client="", assignment=None):
        if role == "helper" and self.bind_error is not None:
            raise self.bind_error
        return super().bind(session_id, project_id, task_id, run_id, role, persona, parent, meta,
                            purpose, worker_client, assignment)


def adm(state, reason="fixture", nxt=None):
    return {"admit": state == "admit", "state": state, "reason": reason, "next_eligible": nxt,
            "routes": [{"source": "fixture"}]}


@pytest.fixture()
def env(tmp_path):
    fake = FaultKilo()
    url = fake.start()
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = load(str(write_profile(tmp_path, "game", root, extra=HELPERS)))
    clock = [time.time()]
    ff = ScenarioFoundry()
    state = str(tmp_path / "state")

    def make():
        P = build_project(prof, state, client=KiloClient(url, password=fake.password,
                                                         timeout=5), foundry=ff)
        return Coordinator(Store(str(tmp_path / "state" / "c.sqlite")), [P], state,
                           clock=lambda: clock[0])
    c = make()
    c.approve("game", "T1", [], "victor")
    c.tick()
    c.tick()
    assert c.store.task("game", "T1")["state"] == "IMPLEMENTING"
    yield {"fake": fake, "c": c, "ff": ff, "clock": clock, "make": make,
           "primary": c.store.task("game", "T1")["session_id"]}
    fake.stop()


def units(c):
    return {u["responsibility"]: u for u in c.helpers.units("game", "T1")}


def helper_sids(env):
    return [s for s in env["fake"].sessions if s != env["primary"]]


def events(c, kind):
    return c.store.q("SELECT * FROM events WHERE project_id='game' AND task_id='T1' AND kind=?",
                     (kind,))


def all_events(c):
    return c.store.q("SELECT * FROM events WHERE project_id='game' AND task_id='T1'")


def start_both(env):
    """Admit on meridian: both T1 units start (one session each)."""
    env["ff"].admission = {CLAUDE: adm("admit")}
    env["c"].tick()
    u = units(env["c"])
    assert {x["status"] for x in u.values()} == {"running"}
    return u


# ================================================================ S1 Kilo model / startup

def test_s1_kilo_config_read_failure_defers_without_session_or_admission(env):
    """The preflight can't read Kilo's effective config (HTTP 500): defer,
    open-ended, before asking Foundry or creating a session."""
    fake, c, ff = env["fake"], env["c"], env["ff"]
    fake.get_fault = lambda p: p == "/config/effective"
    ff.admission = {CLAUDE: adm("admit")}
    c.tick()
    u = units(c)
    assert {x["status"] for x in u.values()} == {"deferred"}
    assert all("Kilo model preflight failed" in x["reason"] for x in u.values())
    assert all(x["next_eligible"] is None for x in u.values())
    assert helper_sids(env) == [] and ff.asked == []
    fake.get_fault = None
    env["clock"][0] += POLL + 1
    c.tick()
    assert {x["status"] for x in units(c).values()} == {"running"}


@pytest.mark.parametrize("error", [
    {"name": "APIError", "data": {"message": "model Foundry-Kilo-Helper-Claude not found",
                                  "statusCode": 404, "isRetryable": False}},
    {"name": "ProviderAuthError", "data": {"providerID": "foundry",
                                           "message": "invalid api key"}},
])
def test_s1_async_startup_error_on_assistant_message_fails_fast(env, error):
    """Kilo accepted prompt_async, then recorded an assistant message with an
    error (model 404 / provider auth): the unit fails on the next poll, with
    the error text, and its binding is ended. No 30-minute wait."""
    fake, c, ff = env["fake"], env["c"], env["ff"]
    u = start_both(env)
    sid = u[S45]["session_id"]
    fake.add_message(sid, "assistant", [], error=error)
    fake.set_status(sid, None)
    env["clock"][0] += 1
    c.tick()
    s45 = units(c)[S45]
    assert s45["status"] == "failed"
    assert error["name"] in s45["reason"]
    assert (sid, "failed") in ff.ended
    assert units(c)[GALLERY]["status"] == "running"     # the other unit is unaffected


# fixed in v0.121.4
def test_s1_idle_session_without_any_reply_is_detected_early(env):
    fake, c = env["fake"], env["c"]
    u = start_both(env)
    sid = u[S45]["session_id"]
    fake.set_status(sid, None)                 # idle; only the user prompt exists
    env["clock"][0] += 5 * 60                  # several polls, far below 30 min
    c.tick()
    c.tick()
    assert units(c)[S45]["status"] == "failed"


# fixed in v0.121.4
def test_s1_retry_on_foundry_denial_is_not_held_until_timeout(env):
    fake, c = env["fake"], env["c"]
    u = start_both(env)
    sid = u[S45]["session_id"]
    fake.retry[sid] = ('{"error": {"message": "budget denied", "code": "budget_denied", '
                       '"foundry_category": "budget", "reset_seconds": 900}}')
    env["clock"][0] += POLL + 1
    c.tick()
    st = units(c)[S45]["status"]
    assert st in ("deferred", "failed", "pending")   # anything but 'running' for 30 min
    assert sid in fake.aborts


# ================================================================ S2 quota deferral

def test_s2_unknown_refresh_started_then_next_poll_admits_without_busy_loop(env):
    c, ff, clock = env["c"], env["ff"], env["clock"]
    ff.admission = {CLAUDE: adm("unknown", "5-hour (session): observed 70 min ago (limit 30 "
                                           "min); refresh probe started, ask again shortly")}
    c.tick()
    u = units(c)
    assert {x["status"] for x in u.values()} == {"deferred"}
    assert all("refresh probe started" in x["reason"] for x in u.values())
    assert all(x["next_eligible"] is None for x in u.values())          # open-ended
    assert all(x["next_check"] == pytest.approx(clock[0] + POLL) for x in u.values())
    n = len(ff.asked)
    t0 = clock[0]
    for dt in (1, 10, POLL - 1):                       # no polls before next_check
        clock[0] = t0 + dt
        c.tick()
    assert len(ff.asked) == n
    clock[0] = t0 + POLL + 1
    ff.admission = {CLAUDE: adm("admit", "5-hour 11.0%, weekly 35.0% (below 70%)")}
    c.tick()
    assert {x["status"] for x in units(c).values()} == {"running"}
    assert len(helper_sids(env)) == 2
    assert len(events(c, "helper_deferred")) == 2       # one per unit, not one per tick


def test_s2_defer_next_eligible_is_honoured_and_open_ended_backend_polls(env):
    c, ff, clock = env["c"], env["ff"], env["clock"]
    t0 = clock[0]
    ff.admission = {CLAUDE: adm("defer", "5-hour 72.0% used", nxt=t0 + 600),
                    GPT: adm("unknown", "no management key")}
    c.tick()
    u = units(c)
    # normal unit: only meridian -> waits for its reset, not the poll interval
    assert u[S45]["next_eligible"] == pytest.approx(t0 + 600)
    assert u[S45]["next_check"] == pytest.approx(t0 + 600)
    # high-value unit: an UNKNOWN fallback could clear any time -> poll interval
    assert u[GALLERY]["next_eligible"] is None
    assert u[GALLERY]["next_check"] == pytest.approx(t0 + POLL)
    asked = []
    for t in (t0 + POLL + 1, t0 + 300, t0 + 599):
        clock[0] = t
        before = list(ff.asked)
        c.tick()
        asked.append([p for p in ff.asked[len(before):]])
    # S45 never re-asked before its reset; GALLERY re-asked (2 personas) once per interval
    assert all(CLAUDE in a for a in asked)              # the gallery unit's chain
    assert all(len(a) == 2 for a in asked)
    clock[0] = t0 + 601
    ff.admission[CLAUDE] = adm("admit")
    c.tick()
    assert units(c)[S45]["status"] == "running"


def test_s2_gate_background_refresh_once_then_admits(app):
    """Foundry side (quota_gate.evaluate_fresh wait=False): a second admission
    poll while the probe is in flight starts no second probe; after it lands,
    the next poll admits from the refreshed reading."""
    import asyncio
    from tests.test_helper_policy import FakeHealth, codex_account, make_gate
    gate, fake, clock = make_gate(app)
    fake.cliproxy_usage.snap = {"ok": True, "accounts": [codex_account("a", 10, 35, age_s=4000)]}
    gate_open = asyncio.Event

    class SlowHealth(FakeHealth):
        async def probe(self, backend, model=None):
            await self.release.wait()
            return await super().probe(backend, model)
    fake.health = SlowHealth(fake, after={"ok": True,
                                          "accounts": [codex_account("a", 11, 35, age_s=5)]})
    fake.pool.backends["cpa"].busy = 0

    async def go():
        fake.health.release = gate_open()
        d1 = await gate.evaluate_fresh("cpa", "gpt-6.1-sol", wait=False)
        clock[0] += 5
        d2 = await gate.evaluate_fresh("cpa", "gpt-6.1-sol", wait=False)
        fake.health.release.set()
        await asyncio.gather(*list(gate._bg))
        clock[0] += 5
        d3 = await gate.evaluate_fresh("cpa", "gpt-6.1-sol", wait=False)
        return d1, d2, d3
    d1, d2, d3 = asyncio.run(go())
    assert d1["state"] == "unknown" and d1["refresh"]["started"]
    assert d2["state"] == "unknown" and "already tried" in d2["refresh"]["skipped"]
    assert d3["state"] == "admit"
    assert fake.health.calls == [("cpa", "gpt-6.1-sol")]


# ================================================================ S3 backend outage

def test_s3_admission_foundry_error_defers_open_ended_and_primary_unaffected(env):
    fake, c, ff, clock = env["fake"], env["c"], env["ff"], env["clock"]
    primary = env["primary"]

    def down(persona):
        raise FoundryError("GET /v1/foundry/helpers/admission: timed out")
    ff.answer = down
    c.tick()
    u = units(c)
    assert {x["status"] for x in u.values()} == {"deferred"}
    assert all("admission check failed" in x["reason"] for x in u.values())
    assert all(x["next_eligible"] is None and x["next_check"] == pytest.approx(clock[0] + POLL)
               for x in u.values())
    assert helper_sids(env) == []
    # the primary keeps working and reaches verification; never-admitted units are skipped
    fake.finish(primary, 'done\n```coordinator-report\n{"status": "DONE", "summary": "x"}\n```')
    for _ in range(3):
        c.tick()
    # the primary's DONE was acted on (it left IMPLEMENTING at least once: with
    # nothing committed the engine moves on to verification/repair as usual);
    # the helper outage neither held it nor raised into the primary's step
    states = [h["to_state"] for h in c.store.history("game", "T1")]
    assert "VERIFYING" in states, states
    assert not [e for e in events(c, "error")]
    assert {x["status"] for x in units(c).values()} == {"skipped"}
    assert all("admission check failed" in x["reason"] for x in units(c).values())


def test_s3_bind_failure_aborts_session_and_defers(env):
    """Foundry answers admission but the binding fails: the unbound session is
    aborted before any prompt and the unit is deferred, not running."""
    fake, c, ff = env["fake"], env["c"], env["ff"]
    ff.admission = {CLAUDE: adm("admit")}
    ff.bind_error = FoundryError("POST /v1/foundry/work/sessions: [Errno 111] refused")
    c.tick()
    u = units(c)
    assert {x["status"] for x in u.values()} == {"deferred"}
    assert all("binding failed" in x["reason"] for x in u.values())
    hs = helper_sids(env)
    assert len(hs) == 2 and set(hs) <= set(fake.aborts)
    assert not [p for p in fake.prompts if p["session_id"] in hs]       # never prompted
    ff.bind_error = None
    env["clock"][0] += POLL + 1
    c.tick()
    assert {x["status"] for x in units(c).values()} == {"running"}


# fixed in v0.121.4
def test_s3_foundry_client_non_json_200_is_a_foundry_error():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            raw = b"<html>502 Bad Gateway (proxy)</html>"
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        fc = FoundryClient(f"http://127.0.0.1:{srv.server_address[1]}", timeout=5)
        with pytest.raises(FoundryError):
            fc.helper_admission(CLAUDE)
    finally:
        srv.shutdown()
        srv.server_close()


# fixed in v0.121.4
def test_s3_unexpected_admission_error_defers_with_reason(env):
    c, ff, clock = env["c"], env["ff"], env["clock"]
    calls = []

    def broken(persona):
        calls.append(persona)
        raise json.JSONDecodeError("Expecting value", "<html>", 0)
    ff.answer = broken
    c.tick()
    n = len(calls)
    c.tick()
    c.tick()
    u = units(c)
    assert {x["status"] for x in u.values()} == {"deferred"}
    assert all(x["reason"] for x in u.values())
    assert len(calls) == n                              # no re-ask before next_check


# fixed in v0.121.4
def test_s3_admission_outage_is_asked_once_per_backend_per_tick(env):
    c, ff = env["c"], env["ff"]

    def hang(persona):
        raise FoundryError("GET /v1/foundry/helpers/admission: timed out")
    ff.answer = hang
    c.tick()
    # observed today: CLAUDE (S45), CLAUDE + GPT (gallery) = 3 x 20 s
    assert ff.asked.count(CLAUDE) == 1


# ================================================================ S4 long-running work

def test_s4_timeout_aborts_ends_binding_and_frees_the_slot(env):
    fake, c, ff, clock = env["fake"], env["c"], env["ff"], env["clock"]
    u = start_both(env)
    sid = u[S45]["session_id"]
    fake.finish(u[GALLERY]["session_id"], "gallery proposal")   # one returns normally
    clock[0] += (TIMEOUT_MIN - 1) * 60
    c.tick()
    assert units(c)[S45]["status"] == "running" and sid not in fake.aborts
    assert units(c)[GALLERY]["status"] == "done"
    clock[0] += 2 * 60
    c.tick()
    s45 = units(c)[S45]
    assert s45["status"] == "failed" and "timed out after 30 min" in s45["reason"]
    assert sid in fake.aborts and (sid, "failed") in ff.ended
    assert len(helper_sids(env)) == 2                   # no replacement session started


def test_s4_no_duplicate_dispatch_while_running(env):
    fake, c = env["fake"], env["c"]
    u = start_both(env)
    P = c.projects["game"]
    t = c.store.task("game", "T1")
    again = c.helpers.collect_report(P, t, P.adapter.describe("T1"), {"helper_requests": [
        {"kind": "test_design", "responsibility": S45, "prompt": "again please"}]})
    assert again == []                                 # running units can't be re-requested
    for _ in range(4):
        env["clock"][0] += POLL + 1
        c.tick()
    hs = helper_sids(env)
    assert len(hs) == 2
    for s in hs:
        assert sum(p["session_id"] == s for p in fake.prompts) == 1
    assert units(c)[S45]["attempts"] == 1 and units(c)[S45]["session_id"] == u[S45]["session_id"]


def test_s4_pause_cancels_running_helpers_and_nothing_new_starts(env):
    fake, c, ff = env["fake"], env["c"], env["ff"]
    u = start_both(env)
    c.pause("game", "T1", "operator hold")
    assert {x["status"] for x in units(c).values()} == {"cancelled"}
    assert {u[S45]["session_id"], u[GALLERY]["session_id"]} <= set(fake.aborts)
    n = len(ff.asked)
    env["clock"][0] += POLL + 1
    c.tick()
    assert len(ff.asked) == n and len(helper_sids(env)) == 2


def test_s4_abort_lane_running_leaves_ordinary_units(env):
    fake, c = env["fake"], env["c"]
    u = start_both(env)
    c.store.x("UPDATE helper_units SET lane='research' WHERE unit_id=?", (u[GALLERY]["unit_id"],))
    stopped = c.helpers.abort_lane_running(c.projects["game"], "T1", "the session ended")
    assert stopped == [u[GALLERY]["unit_id"]]
    assert units(c)[GALLERY]["status"] == "cancelled"
    assert units(c)[S45]["status"] == "running" and u[S45]["session_id"] not in fake.aborts


def test_s4_rerequest_after_timeout_is_a_new_session_once(env):
    fake, c, clock = env["fake"], env["c"], env["clock"]
    u = start_both(env)
    clock[0] += (TIMEOUT_MIN + 1) * 60
    c.tick()
    assert units(c)[S45]["status"] == "failed"
    P = c.projects["game"]
    t = c.store.task("game", "T1")
    assert c.helpers.collect_report(P, t, P.adapter.describe("T1"), {"helper_requests": [
        {"kind": "test_design", "responsibility": S45}]}) == [u[S45]["unit_id"]]
    clock[0] += 1
    c.tick()
    c.tick()
    s45 = units(c)[S45]
    assert s45["status"] == "running" and s45["requests"] == 2
    assert s45["session_id"] != u[S45]["session_id"]
    assert sum(p["session_id"] == s45["session_id"] for p in fake.prompts) == 1


# ================================================================ S5 control vs read channel

def _helper_prompt(path, body):
    return path.endswith("/prompt_async") and "read-only" in json.dumps(body or {})


def test_s5_prompt_refused_is_not_reported_running_and_retry_reuses_the_session(env):
    fake, c, ff = env["fake"], env["c"], env["ff"]
    ff.admission = {CLAUDE: adm("admit")}
    fake.post_fault = lambda p, b: "before" if _helper_prompt(p, b) else None
    c.tick()
    u = units(c)
    assert all(x["status"] != "running" for x in u.values())       # honest: not running
    assert any("helpers" in (e.get("detail") or "") for e in events(c, "warning"))
    created = helper_sids(env)
    assert len(created) == 1          # the first unit's failure stops this tick's dispatch
    fake.post_fault = None
    env["clock"][0] += 1
    c.tick()
    u = units(c)
    assert {x["status"] for x in u.values()} == {"running"}
    assert created[0] in {x["session_id"] for x in u.values()}      # same session reused
    assert len(helper_sids(env)) == 2
    for s in helper_sids(env):
        assert sum(p["session_id"] == s for p in fake.prompts) == 1


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: prompt_async accepted by Kilo but the response lost -> helpers._start re-sends "
    "the prompt on the next tick (helpers.py:384-387 journals 'prompted' only after "
    "success), so the helper session gets a duplicate turn (double optional spend)"))
def test_s5_prompt_accepted_but_response_lost_is_not_resent(env):
    fake, c, ff = env["fake"], env["c"], env["ff"]
    ff.admission = {CLAUDE: adm("admit")}
    once = {"n": 0}

    def lose_first(p, b):
        if _helper_prompt(p, b) and once["n"] == 0:
            once["n"] += 1
            return "after"
        return None
    fake.post_fault = lose_first
    c.tick()
    env["clock"][0] += 1
    c.tick()
    for s in helper_sids(env):
        assert sum(p["session_id"] == s for p in fake.prompts) == 1


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: POST /session accepted but the response lost -> the op journal has no session "
    "id (helpers.py:339-352), the retry creates a second Kilo session and the first is "
    "orphaned (never bound, never aborted)"))
def test_s5_session_create_response_lost_leaves_no_orphan(env):
    fake, c, ff = env["fake"], env["c"], env["ff"]
    ff.admission = {CLAUDE: adm("admit")}
    once = {"n": 0}

    def lose_first(p, b):
        if p == "/session" and once["n"] == 0:
            once["n"] += 1
            return "after"
        return None
    fake.post_fault = lose_first
    c.tick()
    env["clock"][0] += 1
    c.tick()
    live = {x["session_id"] for x in units(c).values()}
    orphans = [s for s in helper_sids(env) if s not in live and s not in fake.aborts]
    assert orphans == []


# fixed in v0.121.4
def test_s5_unconfirmed_abort_is_recorded_honestly(env):
    fake, c, ff, clock = env["fake"], env["c"], env["ff"], env["clock"]
    u = start_both(env)
    sid = u[S45]["session_id"]
    fake.post_fault = lambda p, b: "before" if p == f"/session/{sid}/abort" else None
    clock[0] += (TIMEOUT_MIN + 1) * 60
    c.tick()
    s45 = units(c)[S45]
    text = json.dumps([s45["reason"], [e.get("detail") for e in all_events(c)]])
    assert "abort" in text.lower() or (sid, "failed") not in ff.ended


def test_s5_reads_failing_keep_unit_running_honestly(env):
    """Reads (state/transcript) fail while control works: the unit is not
    reported done or failed from missing data; it recovers when reads do."""
    fake, c = env["fake"], env["c"]
    u = start_both(env)
    sid = u[S45]["session_id"]
    fake.get_fault = lambda p: p.startswith(f"/session/{sid}")
    fake.finish(sid, "S45 proposal text")
    for _ in range(2):
        env["clock"][0] += POLL + 1
        c.tick()
    assert units(c)[S45]["status"] == "running" and units(c)[S45]["result"] is None
    fake.get_fault = None
    env["clock"][0] += 1
    c.tick()
    assert units(c)[S45]["status"] == "done" and "S45 proposal" in units(c)[S45]["result"]


# fixed in v0.121.4
def test_s5_one_unreadable_session_does_not_block_other_dispatch(env):
    fake, c, ff, clock = env["fake"], env["c"], env["ff"], env["clock"]
    first = {"done": False}

    def one_then_unknown(persona):
        if not first["done"]:
            first["done"] = True
            return adm("admit")
        return adm("unknown", "no reading")
    ff.answer = one_then_unknown
    c.tick()
    running = [x for x in units(c).values() if x["status"] == "running"]
    assert len(running) == 1
    sid = running[0]["session_id"]
    fake.get_fault = lambda p: p.startswith(f"/session/{sid}")
    ff.answer = None
    ff.admission = {CLAUDE: adm("admit")}
    clock[0] += POLL + 1
    c.tick()
    assert {x["status"] for x in units(c).values()} == {"running"}


def test_s5_primary_delivery_failure_is_not_marked_delivered_and_retries_once(env):
    fake, c = env["fake"], env["c"]
    primary = env["primary"]
    u = start_both(env)
    fake.finish(u[S45]["session_id"], "PROPOSAL-S45")
    fake.finish(u[GALLERY]["session_id"], "PROPOSAL-GALLERY")
    env["clock"][0] += 1
    c.tick()
    assert {x["status"] for x in units(c).values()} == {"done"}
    fake.post_fault = lambda p, b: "before" if p == f"/session/{primary}/prompt_async" else None
    fake.finish(primary, "first pass done")
    env["clock"][0] += 1
    c.tick()
    assert {x["status"] for x in units(c).values()} == {"done"}    # not claimed delivered
    fake.post_fault = None
    env["clock"][0] += 1
    c.tick()
    c.tick()
    assert {x["status"] for x in units(c).values()} == {"delivered"}
    assert sum("PROPOSAL-S45" in p["text"] for p in fake.prompts
               if p["session_id"] == primary) == 1
