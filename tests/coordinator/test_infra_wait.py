"""v0.181.229: an outage the coordinator can see is waited out (autoheal.fault_present),
a session that ended on infrastructure is not the sprint's (no-progress count, session
budget), restarts are bounded whatever the head, and a slot push that times out is a named
fault. The texts are the blockers the live coordinator recorded on 2026-11-17 (a Studio
update closed Studio; ORI-2, FIN-5a1, POL-7 and LOB-1b were restarted into it until three
were parked) and 2026-11-20 (FIN-4a's first push to slot-3 timed out; four hours in a Lead
case). Fakes only (scripted Kilo, fake Foundry, temp git repos)."""

from __future__ import annotations

import json
import subprocess

import pytest

from foundry_coordinator import autoheal, infra, slots
from foundry_coordinator import states as S
from foundry_coordinator.obligations import INFRA_ACTOR, INFRA_REF, LedgerRefused
from tests.coordinator.test_lead_e2e import (DONE, TID, Clock, game_assignment, hist,
                                             lead_reply, lead_toml, make_game, task)
from tests.coordinator.test_revamp_lead import blocked, drive, events

# ORI-2, 2026-11-17 22:51 (class session_blocked) and 21:47 (class studio_calls_error)
STUDIO_GONE = ('Roblox Studio is not running: list_roblox_studios -> {"studios":[]}, '
               'get_connected_instances -> {"instances":[]}, no RobloxStudioBeta process at '
               '2026-11-18T04:50Z (rojo serve up). The Studio half of the scripted gate cannot '
               'run until Studio is reopened on the TEST place')
FOUND_0 = ("scripted Studio gate: McpError: need exactly one Studio matching '[TEST]', found 0 "
           "(open: none)")
# FIN-4a, 2026-11-20 18:25 (class studio_calls_error)
PUSH_TIMEOUT = ("scripted Studio gate: the pipeline exited with code 1 without a result; log "
                "C:\\Users\\shock\\.foundry-coordinator\\evidence\\animaluprising\\FIN-4a\\"
                "7e845dfef533\\studio_calls.log: raise TimeoutExpired(self.args, orig_timeout) "
                "subprocess.TimeoutExpired: Command '['git', '-C', "
                "'C:\\\\Roblox\\\\AnimalUprising\\\\.tracks\\\\fin4a', 'push', '-q', '--force', "
                "'ssh://studio@192.168.0.117/srv/studio-slot/AnimalUprising.git', "
                "'7e845dfef5332a47ee3dfe704319eab37dd3d267:refs/heads/slot/FIN-4a']' timed out "
                "after 300 seconds)")
INFRA = "Kilo did not admit the prompt within the admission window (HTTP 503)"
NO_STUDIO = ("no_studio", "no Studio with the TEST place '[TEST]' is connected (open: none)")


def studio(e, verdict=NO_STUDIO):
    """The workstation's Studio as the adapter reports it; the test moves it."""
    st = {"verdict": verdict, "asked": 0}

    def ready():
        st["asked"] += 1
        return st["verdict"]
    e["P"].adapter.studio_ready = ready
    return st


def ah(e):
    return (task(e)["data"] or {}).get("auto_heal") or {}


def sessions_room(e):
    return e["c"].obligations.effective_limits(e["P"], task(e))["sessions"]


# ------------------------------------------------------------------ the wait

def test_replay_a_closed_studio_is_waited_out_and_costs_nothing(tmp_path):
    """11-17: restarted "free" after 60 s into the same missing Studio, five times in an
    hour. Now the retry is held while Studio is still gone: one session, no Lead case, one
    alert after infra_wait_notice_minutes, and the sprint goes on when Studio is back."""
    clock = Clock()
    log = []

    def primary(ctx):
        log.append(ctx)
        if len(log) == 1:
            return blocked(STUDIO_GONE)
        return {"writes": {"src/a.txt": "OK healed\n"}, "commit": f"{TID}: fix", "final": DONE}
    e = make_game(tmp_path, primary, extra=lead_toml(), clock=clock)
    st = studio(e)
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert not drive(e, lambda: task(e)["state"] == S.COMPLETE, clock, n=60, step=40)   # 40 min
    assert task(e)["state"] == S.BLOCKED and len(e["client"].primary_starts()) == 1
    assert not c.lead.cases("game", TID) and not e["client"].lead_starts()
    w = ah(e)
    assert w["signature"] == "studio" and w["waits"] >= 25 and w["wait_since"]
    assert "waiting for Studio with the TEST place" in task(e)["next_action"]
    assert "(open: none)" in task(e)["next_action"]
    assert len(events(e, "infra_waiting")) == 1 and not events(e, "auto_heal_retry")
    # asked about once a minute, not once a tick
    assert 25 <= st["asked"] <= 45
    [notice] = [n for n in events(e, "operator_notice") if n.get("kind") == "infra_outage"]
    assert notice["alert_class"] == "watch" and notice["waiting"] == [TID]
    assert notice["title"].startswith("Studio with the TEST place has been down for 3")
    assert "restart by themselves" in notice["text"]
    # the session that ran into it is not the sprint's: budget back, no-progress count clean
    [refund] = events(e, "infra_session_refund")
    assert refund["signature"] == "studio"
    room = sessions_room(e)
    assert room["authorized"] == 1 and room["max"] == room["base_max"] + 1
    # Studio is back: the retry goes, free
    st["verdict"] = ("ok", "")
    assert drive(e, lambda: task(e)["state"] == S.COMPLETE, clock), hist(e)
    [back] = events(e, "infra_back")
    assert back["signature"] == "studio" and back["waited_s"] >= 30 * 60
    assert [s["metadata"]["attempt"] for s in e["client"].primary_starts()] == [1, 1]
    assert not c.lead.cases("game", TID)
    assert len([n for n in events(e, "operator_notice") if n.get("kind") == "infra_outage"]) == 1


def test_an_outage_still_there_after_the_retries_waits_instead_of_a_lead_case(tmp_path):
    """Studio flaps: back when the retry is released, gone again when the session needs it.
    With the retries used up and Studio still gone the stop waits; only a stop with Studio
    there (so not the outage) goes to the Lead."""
    clock = Clock()
    e = make_game(tmp_path, lambda ctx: blocked(STUDIO_GONE),
                  lead=lambda ctx: lead_reply(game_assignment()),
                  extra=lead_toml() + "\n[automation]\nauto_heal_retries = 1\n", clock=clock)
    st = studio(e, ("ok", ""))
    c = e["c"]
    c.approve("game", TID, [], "victor")
    # stop 1 -> retry 1/1 released (Studio answers) -> stop 2 with Studio gone
    assert drive(e, lambda: len(e["client"].primary_starts()) == 2, clock, step=20), hist(e)
    st["verdict"] = NO_STUDIO
    clock.advance(60)                                        # past the probe's 45 s cache
    assert drive(e, lambda: bool(ah(e).get("wait_since")), clock, step=20), hist(e)
    assert task(e)["blocker"].startswith("waiting for Studio with the TEST place (studio)")
    assert not c.lead.cases("game", TID) and not events(e, "auto_heal_exhausted")
    assert [x.get("waiting") for x in events(e, "auto_heal")] == [None, True]
    n = len(e["client"].primary_starts())
    for _ in range(20):
        c.tick()
        clock.advance(60)
    assert len(e["client"].primary_starts()) == n and not c.lead.cases("game", TID)
    # back, and the session still stops on it: that is not the outage any more
    st["verdict"] = ("ok", "")
    assert drive(e, lambda: bool(c.lead.cases("game", TID)), clock, step=30), hist(e)
    assert events(e, "auto_heal_exhausted")


def test_restarts_of_one_fault_are_bounded_whatever_the_head(tmp_path):
    """11-17: every auto-heal read "1/2" because each run moved the head and the count is
    per head; the bound never applied. The same class and signature is now also counted
    over six hours: 3 x auto_heal_retries restarts, then the stop goes on to the Lead."""
    clock = Clock()
    n = [0]

    def primary(ctx):
        n[0] += 1
        return {"writes": {"src/a.txt": f"try {n[0]}\n"}, "commit": f"{TID}: try {n[0]}",
                **blocked(INFRA)}
    e = make_game(tmp_path, primary, lead=lambda ctx: lead_reply(game_assignment()),
                  extra=lead_toml() + "\n[automation]\nauto_heal_retries = 2\n"
                                      "infra_retry_max = 50\n", clock=clock)
    c = e["c"]
    c.approve("game", TID, [], "victor")
    assert drive(e, lambda: bool(c.lead.cases("game", TID)), clock, n=400, step=30), hist(e)
    heals = events(e, "auto_heal")
    assert len(heals) == 6 and {x["n"] for x in heals} == {1}       # a new head every time
    assert len({x["key"] for x in heals}) == 6
    [done] = events(e, "auto_heal_exhausted")
    assert done["recent"] == 6 and done["retries"] == 0
    # six hours later the window is empty again
    data = task(e)["data"]
    assert len(data["auto_heal_recent"]["session_blocked|kilo_not_admitted"]) == 6


# ------------------------------------------------------------------ what can be seen

def test_fault_present_reads_studio_and_the_router_once_per_window(tmp_path, monkeypatch):
    clock = Clock()
    e = make_game(tmp_path, lambda ctx: {"final": DONE}, extra=lead_toml(), clock=clock)
    c, P = e["c"], e["P"]
    st = studio(e)
    assert autoheal.fault_present(c, P, "studio_reload") is True
    assert autoheal.fault_present(c, P, "studio") is True and st["asked"] == 1     # cached
    clock.advance(46)
    st["verdict"] = ("play", "a playtest is still running in Studio")
    assert autoheal.fault_present(c, P, "studio") is False                  # Studio is there
    clock.advance(46)
    st["verdict"] = ("unknown", "pre-flight probe timed out")
    assert autoheal.fault_present(c, P, "studio") is None                   # cannot tell
    # signatures with nothing to look at keep the bounded retries
    for sig in ("slot_push", "slot_checkout", "kilo_not_admitted", "rojo", "gate_tooling", ""):
        assert autoheal.fault_present(c, P, sig) is None
    # the router: its /health, when the project talks to one
    assert autoheal.fault_present(c, P, "connection") is None               # no router url
    monkeypatch.setattr(infra, "router_probe", lambda P_: ["router=http://r:1/health"])
    seen = []
    monkeypatch.setattr(infra, "check_endpoint",
                        lambda url, timeout=3.0: seen.append(url) or "ConnectionRefusedError")
    clock.advance(46)
    assert autoheal.fault_present(c, P, "backend_5xx") is True
    assert autoheal.fault_present(c, P, "connection") is True and seen == ["http://r:1/health"]
    monkeypatch.setattr(infra, "check_endpoint", lambda url, timeout=3.0: "")
    clock.advance(46)
    assert autoheal.fault_present(c, P, "router_storage") is False
    # a probe that raises is "cannot tell", never a crash in the tick
    P.adapter.studio_ready = lambda: 1 / 0
    clock.advance(46)
    assert autoheal.fault_present(c, P, "studio") is None


def test_a_project_without_a_studio_probe_keeps_the_bounded_retries(tmp_path):
    clock = Clock()
    e = make_game(tmp_path, lambda ctx: blocked(STUDIO_GONE),
                  lead=lambda ctx: lead_reply(game_assignment()),
                  extra=lead_toml() + "\n[automation]\nauto_heal_retries = 2\n", clock=clock)
    c = e["c"]
    assert not hasattr(e["P"].adapter, "studio_ready")
    c.approve("game", TID, [], "victor")
    assert drive(e, lambda: bool(c.lead.cases("game", TID)), clock), hist(e)
    assert [x["n"] for x in events(e, "auto_heal")] == [1, 2] and not events(e, "infra_waiting")


# ------------------------------------------------------------------ the session is not the sprint's

def _implementing(tmp_path, clock):
    """A sprint whose session ended its turn and waits on a scripted gate (IMPLEMENTING)."""
    e = make_game(tmp_path, lambda ctx: {"final": "working"}, extra=lead_toml(), clock=clock)
    e["c"].approve("game", TID, [], "victor")
    assert drive(e, lambda: task(e)["state"] == S.IMPLEMENTING, clock, step=1), hist(e)
    return e


@pytest.mark.parametrize("text,sig", [(FOUND_0, "studio_reload"), (PUSH_TIMEOUT, "slot_push")])
def test_a_gate_that_died_on_infrastructure_marks_the_session_and_gives_it_back(
        tmp_path, text, sig):
    """The session asked for its gate and ended its turn; the gate found no Studio (ORI-2) or
    the push to the slot timed out (FIN-4a). The session reported nothing wrong, so it was
    counted as a session with no progress and against the session budget."""
    clock = Clock()
    e = _implementing(tmp_path, clock)
    c, P = e["c"], e["P"]
    t = task(e)
    sid = t["session_id"]
    assert sessions_room(e)["authorized"] == 0
    out = c._stop(P, t, S.BLOCKED, {"blocker": text, "blocker_kind": "tooling"},
                  cls="studio_calls_error", blocker=text[:500])
    assert out["state"] == S.BLOCKED and ah(e)["signature"] == sig
    assert ah(e)["action"] == "restart" and not c.lead.cases("game", TID)
    s = c.store.session(sid)
    assert s["data"]["infra_stop"] == sig
    room = sessions_room(e)
    assert room["authorized"] == 1 and room["max"] == room["base_max"] + 1
    [row] =[r for r in c.obligations.ledger("game", TID)
             if str(r["decision_id"] or "").startswith(INFRA_REF)]
    assert row["actor"] == INFRA_ACTOR and row["decision_id"] == f"infra:sessions:{sid}"
    assert sid in c.recovery._infra_sessions("game", TID, [sid])
    # once per session, however often the stop is seen
    autoheal._mark_infra_session(c, P, t, sig, "k")
    assert sessions_room(e)["authorized"] == 1


def test_only_a_marked_session_of_the_task_gets_its_place_back(tmp_path):
    clock = Clock()
    e = _implementing(tmp_path, clock)
    c = e["c"]
    sid = task(e)["session_id"]
    ob = c.obligations

    def give(ref, actor=INFRA_ACTOR, counter="sessions", n=1):
        return ob.authorize("game", TID, counter, n, "x", actor, ref)
    with pytest.raises(LedgerRefused, match="did not end on infrastructure"):
        give(f"infra:sessions:{sid}")                       # not marked by the auto-heal
    c.store.update_session(sid, data={"infra_stop": "studio"})
    with pytest.raises(LedgerRefused, match="only the 'infra' actor"):
        give(f"infra:sessions:{sid}", actor="operator")
    with pytest.raises(LedgerRefused, match="sessions counter"):
        give(f"infra:attempt_ordinary:{sid}", counter="attempt_ordinary")
    with pytest.raises(LedgerRefused, match="one unit"):
        give(f"infra:sessions:{sid}", n=3)
    with pytest.raises(LedgerRefused, match="did not end on infrastructure"):
        give("infra:sessions:ses_of_another_task")
    assert give(f"infra:sessions:{sid}")
    with pytest.raises(LedgerRefused, match="already authorized"):
        give(f"infra:sessions:{sid}")
    # a signature that misfires cannot hand out sessions without end
    c._infra_ref_ok = lambda *a: True
    ob.infra_validator = lambda *a: True
    for i in range(11):
        give(f"infra:sessions:s{i}")
    with pytest.raises(LedgerRefused, match="12/12 sessions"):
        give("infra:sessions:s99")


# ------------------------------------------------------------------ the slot

def test_a_slot_push_that_times_out_is_a_named_fault_not_a_traceback(monkeypatch):
    slot = {"host": "192.168.0.117", "au_push_url": "ssh://studio@h/srv/AnimalUprising.git",
            "foundry_push_url": "ssh://studio@h/srv/foundry.git", "foundry_host_dir": "/srv/f"}
    monkeypatch.setattr(slots, "git_ssh_env", lambda s: None)

    def git_timeout(cwd, *args, timeout=300, env=None):
        raise subprocess.TimeoutExpired(["git", "push"], timeout)
    monkeypatch.setattr(slots, "_git", git_timeout)
    ok, detail = slots.push_sources(slot, "wt", "FIN-4a", "7e845dfef533" + "0" * 28)
    assert ok is False and detail.startswith("AU push: timed out after 3600 s")
    line = f"SLOT push ({slot['host']}): {detail}"
    assert autoheal.plan("studio_calls_error", line) == ("slot_push", "restart")

    # the AU push lands, the Foundry push times out
    def git(cwd, *args, timeout=300, env=None):
        if "refs/heads/deploy" in " ".join(args):
            raise subprocess.TimeoutExpired(["git", "push"], timeout)
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr(slots, "_git", git)
    assert slots.push_sources(slot, "wt", "FIN-4a", "a" * 40) == \
        (False, "Foundry push: timed out after 300 s")
    monkeypatch.setattr(slots, "_git", lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    ok, detail = slots.push_sources(slot, "wt", "FIN-4a", "a" * 40)
    assert ok is False and detail.startswith("AU push: OSError")


def test_the_slot_faults_of_last_week_are_recognised_and_findings_are_not():
    assert autoheal.plan("studio_calls_error", PUSH_TIMEOUT) == ("slot_push", "restart")
    assert autoheal.plan("studio_calls_error", FOUND_0) == ("studio_reload", "restart")
    assert autoheal.plan("session_blocked", "session reported BLOCKED: " + STUDIO_GONE) == \
        ("studio", "restart")
    for line, sig in (("SLOT FAIL: could not push the sources to the slot host", "slot_push"),
                      ("SLOT FAIL: slot-checkout timed out", "slot_checkout"),
                      ("SLOT FAIL: slot-checkout rc=1", "slot_checkout"),
                      # LOB-1b, 2026-11-16 08:40: the PC rebooted under the gate
                      ("scripted Studio gate: the pipeline (pid 38784) is gone without a "
                       "result; log C:\\x\\studio_calls.log: SLOT run: export ...",
                       "gate_runner_gone")):
        assert autoheal.plan("studio_calls_error", line) == (sig, "restart")
    # a pipeline that exited by itself is a crash to look at, not a vanished process
    assert autoheal.plan("studio_calls_error", "scripted Studio gate: the pipeline exited "
                         "with code 1 without a result; log x: KeyError: 'rows'") is None
    # a gate that ran and found something, or ran out of time, is not an outage
    for text in ("scripted Studio gate: timed out after 2400s",
                 "S46 c6: expected 12 rows, got 11",
                 "the push sent 300 files in 36 seconds",
                 "suite S06 FAIL: pass=5 (want 6)"):
        assert autoheal.plan("studio_calls_error", text) is None, text
    # a slot fault has nothing to probe from here: bounded retries, no Studio reopen
    assert autoheal.wait_family("slot_push") == "" and autoheal.wait_family("studio") == "studio"


def test_the_setting_is_typed_in_the_schema():
    from foundry_automation import schema
    f = schema.find_field("automation.infra_wait_notice_minutes")
    assert f is not None and schema._AUTO_DESC["infra_wait_notice_minutes"]
    assert schema.AUTOMATION_DEFAULTS["infra_wait_notice_minutes"] == 30


def test_no_alert_with_the_notice_off_and_the_wait_stays(tmp_path):
    clock = Clock()
    e = make_game(tmp_path, lambda ctx: blocked(STUDIO_GONE),
                  extra=lead_toml() + "\n[automation]\ninfra_wait_notice_minutes = 0\n",
                  clock=clock)
    studio(e)
    e["c"].approve("game", TID, [], "victor")
    assert not drive(e, lambda: task(e)["state"] == S.COMPLETE, clock, n=60, step=60)
    assert ah(e)["waits"] >= 40 and len(e["client"].primary_starts()) == 1
    assert not [n for n in events(e, "operator_notice") if n.get("kind") == "infra_outage"]
    assert json.dumps(ah(e))                                 # the wait record stays plain data
