"""Coordinator-dispatched optional helpers, end to end against the fake
`kilo serve`: admission defer (persisted reason, no retry storm), admit,
read-only bound helper session, restart without duplicates, cutoff while a
helper runs (it finishes, nothing new starts), proposals delivered only when
the primary is idle, purpose spoofing in a report, and independent review of
helper-authored code. Simulated clock and admission answers; no real quota."""

import json
import time
import uuid

import pytest

from foundry_coordinator.adapters import build_project
from foundry_coordinator.adapters.kilo import KiloClient
from foundry_coordinator.devtools.fake_kilo import FakeKilo
from foundry_coordinator.engine import Coordinator
from foundry_coordinator.helpers import child_agent
from foundry_coordinator.profile import load
from foundry_coordinator.store import Store
from tests.coordinator.helpers import FakeFoundry, git, make_repo, write_profile

HELPERS = """
[helpers]
enabled = true
order = ["meridian", "cpa"]
selective = ["cpa"]
agent = "au-helper"
worker_client = "kilo"
max_running = 3
poll_seconds = 30

[helpers.personas]
meridian = "Foundry-Kilo-Helper-Claude"
cpa = "Foundry-Kilo-Helper-GPT"
"""
T1 = {"id": "T1", "title": "UI frames", "spec": "Do it.", "scope": ["src/a.txt", "src/tests/*"],
      "helpers": [
          {"responsibility": "S45 timing test design", "kind": "test_design",
           "prompt": "Design the S45 ordering test.", "files": ["src/tests/s45.lua"]},
          {"responsibility": "gallery layout proposal", "kind": "code_proposal", "value": "high"},
          {"responsibility": "S45 timing test design", "kind": "code_proposal"}]}  # duplicate


class AdmissionFoundry(FakeFoundry):
    def __init__(self):
        super().__init__()
        self.asked = []

    def helper_admission(self, persona):
        self.asked.append(persona)
        return super().helper_admission(persona)


def admit(state, reason="fixture", nxt=None):
    return {"admit": state == "admit", "state": state, "reason": reason, "next_eligible": nxt,
            "routes": [{"source": "fixture"}]}


@pytest.fixture()
def env(tmp_path):
    fake = FakeKilo()
    url = fake.start()
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = load(str(write_profile(tmp_path, "game", root, extra=HELPERS)))
    clock = [time.time()]
    ff = AdmissionFoundry()
    state = str(tmp_path / "state")

    def make():
        project = build_project(prof, state, client=KiloClient(url, password=fake.password),
                                foundry=ff)
        return Coordinator(Store(str(tmp_path / "state" / "c.sqlite")), [project], state,
                           clock=lambda: clock[0])
    c = make()
    c.approve("game", "T1", [], "victor")
    c.tick()
    c.tick()
    assert c.store.task("game", "T1")["state"] == "IMPLEMENTING"
    yield {"fake": fake, "c": c, "ff": ff, "clock": clock, "make": make, "root": root}
    fake.stop()


def units(c):
    return {u["responsibility"]: u for u in c.helpers.units("game", "T1")}


def helper_sessions(fake, primary):
    return [s for s in fake.sessions if s != primary]


@pytest.mark.parametrize('filter_name', ['whitelist', 'blacklist'])
def test_helper_model_filters_defer_before_admission_or_session_creation(env, filter_name):
    fake, c, ff = env['fake'], env['c'], env['ff']
    primary = c.store.task('game', 'T1')['session_id']
    provider = fake.config['provider']['foundry']
    provider[filter_name] = (['Foundry-Kilo-Local'] if filter_name == 'whitelist' else
                            ['Foundry-Kilo-Helper-Claude', 'Foundry-Kilo-Helper-GPT'])
    c.tick()
    assert helper_sessions(fake, primary) == []
    assert ff.asked == []
    assert all(u['status'] == 'deferred' for u in units(c).values())
    assert all(filter_name in u['reason'] for u in units(c).values())
    # Fixing the config allows normal dispatch on the next scheduled poll;
    # no attempt is lost to the original 30-minute timeout.
    provider.pop(filter_name)
    env['clock'][0] += 31
    ff.admission['Foundry-Kilo-Helper-Claude'] = admit('admit')
    c.tick()
    assert len(helper_sessions(fake, primary)) == 2
    assert all(u['attempts'] == 1 for u in units(c).values())


def test_kilo_config_is_read_once_per_dispatch_not_per_candidate(env, monkeypatch):
    """A unit with several candidate backends reads /config/effective once."""
    c = env['c']
    reads = []
    real = c.projects['game'].client.effective_config
    monkeypatch.setattr(c.projects['game'].client, 'effective_config',
                        lambda d: reads.append(d) or real(d))
    env['fake'].config['provider']['foundry']['whitelist'] = ['Foundry-Kilo-Local']
    c.tick()
    n = len(units(c))
    assert n and len(reads) == n           # one read per dispatched unit, whatever its chain


def test_full_admission_defer_admit_deliver_cycle(env):
    fake, c, ff, clock = env["fake"], env["c"], env["ff"], env["clock"]
    primary = c.store.task("game", "T1")["session_id"]
    ff.admission = {"Foundry-Kilo-Helper-Claude": admit("unknown", "weekly window observed "
                                                                   "402 min ago"),
                    "Foundry-Kilo-Helper-GPT": admit("defer", "weekly 72% used", nxt=clock[0] + 600)}
    c.tick()
    u = units(c)
    assert set(u) == {"S45 timing test design", "gallery layout proposal"}   # deduplicated
    assert {x["status"] for x in u.values()} == {"deferred"}
    assert "UNKNOWN" in u["S45 timing test design"]["reason"]
    # the normal unit never tries the selective backend; the high-value one does
    assert "cpa" not in u["S45 timing test design"]["reason"]
    assert "cpa: DEFER" in u["gallery layout proposal"]["reason"]
    assert helper_sessions(fake, primary) == []
    asked = len(ff.asked)
    c.tick()
    c.tick()
    assert len(ff.asked) == asked                       # no retry storm before next_check

    clock[0] += 31
    ff.admission["Foundry-Kilo-Helper-Claude"] = admit("admit")
    c.tick()
    u = units(c)
    assert {x["status"] for x in u.values()} == {"running"}
    hs = helper_sessions(fake, primary)
    assert len(hs) == 2
    hb = [b for b in ff.binds if b["session_id"] in hs]
    assert {b["purpose"] for b in hb} == {"test_design", "code_proposal"}
    assert all(b["parent"] == primary and b["role"] == "helper" for b in hb)
    # read-only: the helper prompt + Kilo session rules
    prompts = [p for p in fake.prompts if p["session_id"] in hs]
    assert all("must not edit" in p["text"] for p in prompts)

    # restart: the running units continue; no second session per unit
    c2 = env["make"]()
    c2.tick()
    assert len(helper_sessions(fake, primary)) == 2

    # cutoff reached while they run: they finish, nothing new is admitted
    ff.admission["Foundry-Kilo-Helper-Claude"] = admit("defer", "5-hour 70.0% used")
    t1 = units(c2)["S45 timing test design"]["session_id"]
    fake.finish(t1, "```lua\n-- proposed S45 test\n```")
    c2.tick()
    assert units(c2)["S45 timing test design"]["status"] == "done"
    assert not any("Helper proposals" in p["text"] for p in fake.prompts
                   if p["session_id"] == primary)       # primary still busy: not yet

    # the primary goes idle: proposals go to the same session, task keeps implementing
    fake.finish(primary, "first pass done")
    c2.tick()
    t = c2.store.task("game", "T1")
    assert t["state"] == "IMPLEMENTING" and t["session_id"] == primary
    last = fake.prompts[-1]
    assert last["session_id"] == primary and "Helper proposals (advisory)" in last["text"]
    assert "proposed S45 test" in last["text"]
    assert units(c2)["S45 timing test design"]["status"] == "delivered"
    # each proposal once: the next idle doesn't resend it
    fake.finish(primary, "integrated")
    c2.tick()
    assert sum("proposed S45 test" in p["text"] for p in fake.prompts) == 1


def test_model_requests_are_always_optional_and_scoped(env):
    c = env["c"]
    t = c.store.task("game", "T1")
    P = c.projects["game"]
    c.helpers.collect_report(P, t, P.adapter.describe("T1"), {"helper_requests": [
        {"kind": "review", "responsibility": "approve my own patch", "files": ["src/a.txt", "../x"],
         "value": "high"},
        {"kind": "planning", "responsibility": "plan more"}]})
    u = units(c)
    assert u["approve my own patch"]["kind"] == "code_proposal"      # not "review"
    assert u["approve my own patch"]["files"] == ["src/a.txt"]       # outside scope dropped
    assert u["approve my own patch"]["value"] == "normal"            # no self-awarded high value
    assert u["plan more"]["kind"] == "code_proposal"                  # not required planning


def test_helper_session_permissions_are_read_only(env):
    P = env["c"].projects["game"]
    rules = P.adapter.helper_permissions(P.adapter.describe("T1"))
    denied = {r["permission"] for r in rules if r["action"] == "deny"}
    assert {"edit", "write", "bash", "task", "Roblox_Studio_*", "robloxstudio-mcp_*"} <= denied


def test_child_session_purpose_from_its_agent(env):
    c = env["c"]
    P = c.projects["game"]
    assert child_agent("Plan frames (@au-planner subagent)") == "au-planner"
    assert c.helpers.child_purpose(P, "Plan frames (@au-planner subagent)") == "planning"
    assert c.helpers.child_purpose(P, "Read things (@explore subagent)") == "exploration"
    assert c.helpers.child_purpose(P, "no marker") == "exploration"


def test_helper_author_is_not_the_sole_reviewer(tmp_path):
    """A proposal by the reviewer's own persona was integrated: the review goes
    to the deep reviewer, and the packet records the authorship."""
    extra = HELPERS.replace('meridian = "Foundry-Kilo-Helper-Claude"',
                            'meridian = "Foundry-Kilo-Sonnet"')
    root, _ = make_repo(tmp_path, "game", [{**T1, "helpers": T1["helpers"][:1]}])
    prof = load(str(write_profile(tmp_path, "game", root, extra=extra)))
    fake = FakeKilo()
    url = fake.start()
    try:
        from tests.coordinator.helpers import review_json
        ff = AdmissionFoundry()
        ff.reviewer = lambda packet: review_json(packet)
        ff.admission = {"Foundry-Kilo-Sonnet": admit("admit")}
        clock = [time.time()]
        P = build_project(prof, str(tmp_path / "s"), client=KiloClient(url, password=fake.password),
                          foundry=ff)
        c = Coordinator(Store(str(tmp_path / "s" / "c.sqlite")), [P], str(tmp_path / "s"),
                        clock=lambda: clock[0])
        c.approve("game", "T1", [], "victor")
        c.tick()
        c.tick()
        primary = c.store.task("game", "T1")["session_id"]
        c.tick()                                          # helper admitted and started
        hs = [s for s in fake.sessions if s != primary]
        fake.finish(hs[0], "proposal: write OK")
        c.tick()
        fake.finish(primary, "pass 1")
        c.tick()                                          # proposals delivered
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "a.txt").write_text("OK")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "T1: integrate helper proposal")
        fake.finish(primary, "done")
        for _ in range(4):
            c.tick()
        t = c.store.task("game", "T1")
        assert ff.chats, (t["state"], t["next_action"], t["blocker"],
                          [(h["to_state"], str(h["evidence"])[:200]) for h in c.store.history("game", "T1")])
        assert ff.chats[-1]["persona"] == "Foundry-Kilo-Opus"       # not Sonnet, the author
        assert "Code authorship" in ff.chats[-1]["packet"]
        assert c.store.task("game", "T1")["state"] in ("APPROVED", "CLOSING", "COMPLETE")
    finally:
        fake.stop()
    _ = (json, uuid)


def test_pinned_fallback_admission_binds_the_session_to_that_route(env):
    """Foundry admitted a later route of the chain because the first was UNKNOWN:
    the helper's binding carries meta.pinned_route, so Foundry serves its calls
    only there. An ordinary (first-route) admission binds without a pin."""
    fake, c, ff = env["fake"], env["c"], env["ff"]
    primary = c.store.task("game", "T1")["session_id"]
    pinned = {"admit": True, "state": "admit", "pinned": True,
              "route": {"backend": "cliproxy", "model": "gpt-6.1-sol"},
              "reason": "first route meridian UNKNOWN (usage endpoint HTTP 429); fallback "
                        "cliproxy admitted: 5-hour 12%", "next_eligible": None,
              "routes": [{"backend": "meridian", "model": "claude-sonnet-5-5",
                          "state": "unknown", "source": "fixture"},
                         {"backend": "cliproxy", "model": "gpt-6.1-sol", "state": "admit",
                          "source": "fixture"}]}
    ff.admission = {"Foundry-Kilo-Helper-Claude": pinned}
    c.tick()
    hs = helper_sessions(fake, primary)
    assert len(hs) == 2
    binds = [b for b in ff.binds if b["session_id"] in hs]
    assert binds and all(b["meta"] == {"pinned_route": {"backend": "cliproxy",
                                                        "model": "gpt-6.1-sol"}} for b in binds)
    started = [u for u in units(c).values() if u["status"] == "running"]
    assert started and all("pinned to cliproxy/gpt-6.1-sol" in u["reason"] for u in started)
    ev = [e for e in c.store.events("game", 100) if e["kind"] == "helper_started"]
    details = [e["detail"] if isinstance(e["detail"], dict) else json.loads(e["detail"] or "{}")
               for e in ev]
    assert details and all(d.get("pinned_route") == pinned["route"] for d in details)
    # ordinary text helpers never ask for image/approved-model requirements
    assert all(need == {} for _, need in ff.admission_asks)


def test_first_route_admission_binds_without_a_pin(env):
    fake, c, ff = env["fake"], env["c"], env["ff"]
    primary = c.store.task("game", "T1")["session_id"]
    ff.admission = {"Foundry-Kilo-Helper-Claude": {**admit("admit"), "pinned": False,
                                                   "route": {"backend": "meridian",
                                                             "model": "claude-sonnet-5-5"}}}
    c.tick()
    hs = helper_sessions(fake, primary)
    assert len(hs) == 2
    assert all(b["meta"] == {} for b in ff.binds if b["session_id"] in hs)


def test_pinned_local_fallback_is_refused_by_the_coordinator(env):
    """Foundry never pins a local route, but if an answer ever did, the
    coordinator's own primary-held local check judges the pinned route."""
    fake, c, ff = env["fake"], env["c"], env["ff"]
    primary = c.store.task("game", "T1")["session_id"]
    ff.admission = {"Foundry-Kilo-Helper-Claude": {
        "admit": True, "state": "admit", "pinned": True, "reason": "x",
        "route": {"backend": "strata", "model": "qwen"},
        "routes": [{"backend": "meridian", "model": "claude", "source": "fixture"},
                   {"backend": "strata", "model": "qwen", "source": "local"}]}}
    c.tick()
    assert helper_sessions(fake, primary) == []
    assert all(u["status"] == "deferred" and "local backend" in u["reason"]
               for u in units(c).values())
