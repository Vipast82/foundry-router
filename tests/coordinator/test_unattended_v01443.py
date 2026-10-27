"""0.144.3: the automation stops asking for a person where nothing needs one.

- keepalive: a long verify keeps the heartbeat fresh (the client restarted a busy
  coordinator every ~6.5 min mid-verify, 2026-10-27); a hung tick still goes stale
- a first heartbeat at start, so a new coordinator never looks dead
- a closed lead case withdraws every decision blocking on it, not only its escalation
- separate questions of one kind are not contradictions (lead_test_change approvals
  accumulate; a newer implementer_blocked answer supersedes the older one)
- a running Rojo is not restarted by auto-heal (a new session needs a Connect click)
- a re-applied router revision keeps a chat/CLI-set max_agents
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from foundry_coordinator import autoheal, cli, client_api, keepalive
from foundry_coordinator.automation import get as auto_get, set_live
from foundry_coordinator.lead import DECISION_KIND, LeadManager
from foundry_coordinator.obligations import DecisionConflict, Obligations
from foundry_coordinator.store import Store
from tests.coordinator.test_tracks_manager import env as tracks_env


@pytest.fixture(autouse=True)
def _no_open_steps():
    keepalive.reset()
    yield
    keepalive.reset()


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "c.sqlite"))


# ------------------------------------------------------------------ keepalive

def test_busy_step_is_current_until_its_ceiling():
    now = [1000.0]
    clock = lambda: now[0]                                          # noqa: E731
    assert keepalive.current(clock) is None
    with keepalive.busy("verify game/T1", 60, clock=clock):
        now[0] += 30
        assert keepalive.current(clock) == {"label": "verify game/T1", "elapsed_s": 30}
        now[0] += 31                                     # past the ceiling: a hang shows
        assert keepalive.current(clock) is None
    assert keepalive.current(clock) is None


def test_process_files_beat_at_start_and_while_a_step_runs(tmp_path):
    eng = SimpleNamespace(projects={}, after_tick=[])
    life = cli._ProcessFiles(str(tmp_path), eng)
    try:
        hb = json.loads((tmp_path / "coordinator.heartbeat").read_text(encoding="utf-8"))
        assert hb["starting"] is True and hb["pid"] and time.time() - hb["ts"] < 30
        assert life.keepalive_once(keepalive.current()) is False     # nothing declared
        with keepalive.busy("verify game/T1", 600):
            assert life.keepalive_once(keepalive.current()) is True
        hb = json.loads((tmp_path / "coordinator.heartbeat").read_text(encoding="utf-8"))
        assert hb["busy"] == "verify game/T1" and "starting" not in hb and hb["ok"] is True
        life.tick()                                       # a normal beat drops the busy note
        hb = json.loads((tmp_path / "coordinator.heartbeat").read_text(encoding="utf-8"))
        assert "busy" not in hb and hb["ok"] is True
    finally:
        life.close()


# ------------------------------------------------------------------ decisions

def test_closing_a_case_withdraws_every_decision_blocking_on_it(store):
    ob = Obligations(store)
    cid = "rc-test"
    esc = ob.open_decision("game", "T1", DECISION_KIND, "escalate?", blocks=["T1"])
    tests = [ob.open_decision("game", "T1", "lead_test_change", f"[lead case {cid}] approve "
                              f"tests/{n}.lua", blocks=[f"lead_case:{cid}"]) for n in "ab"]
    other = ob.open_decision("game", "T1", "lead_test_change", "[lead case rc-other] approve x",
                             blocks=["lead_case:rc-other"])
    acts = []
    fake = SimpleNamespace(store=store, _act=lambda *a, **k: acts.append(k))
    LeadManager._withdraw_escalation(fake, {"case_id": cid, "decision_id": esc,
                                            "project_id": "game", "task_id": "T1"}, "cancelled")
    st = {d: ob.decision(d)["status"] for d in [esc, *tests, other]}
    assert st == {esc: "withdrawn", tests[0]: "withdrawn", tests[1]: "withdrawn",
                  other: "open"}
    ev = [e for e in store.events("game", 50) if e["kind"] == "decision_withdrawn"]
    assert len(ev) == 3 and len(acts) == 3


def test_test_change_approvals_accumulate(store):
    ob = Obligations(store)
    blocks = ["lead_case:rc-1"]
    a = ob.open_decision("game", "T1", "lead_test_change", "approve tests/A.lua", blocks=blocks)
    ob.answer(a, {"approved": "tests/A.lua"}, "victor", "operator", channel="control")
    b = ob.open_decision("game", "T1", "lead_test_change", "approve tests/B.lua", blocks=blocks)
    ob.answer(b, {"approved": "tests/B.lua"}, "victor", "operator", channel="control")
    assert ob.decision(a)["status"] == "answered" and ob.decision(b)["status"] == "answered"


def test_a_newer_blocked_answer_supersedes_the_older_one(store):
    ob = Obligations(store)
    a = ob.open_decision("game", "T1", "implementer_blocked", "blocked: studio lock held",
                         blocks=["T1"])
    ob.answer(a, "wait for the lock", "victor", "operator", channel="control")
    b = ob.open_decision("game", "T1", "implementer_blocked", "blocked: plugin not connected",
                         blocks=["T1"])
    ob.answer(b, "reconnect the plugin", "victor", "operator", channel="control")
    assert ob.decision(b)["status"] == "answered"
    assert ob.decision(a)["status"] == "superseded" and ob.decision(a)["superseded_by"] == b


def test_one_setting_answered_twice_still_conflicts(store):
    ob = Obligations(store)
    d1 = ob.open_decision("game", "T1", "budget_extension", "extend by 2?", blocks=["attempts"])
    ob.answer(d1, {"n": 2}, "victor", "operator", channel="control")
    d2 = ob.open_decision("game", "T1", "budget_extension", "extend by 4?", blocks=["attempts"])
    with pytest.raises(DecisionConflict):
        ob.answer(d2, {"n": 4}, "victor", "operator", channel="control")


# ------------------------------------------------------------------ auto-heal

def _heal_env(tmp_path, monkeypatch, serving):
    restarts = []
    monkeypatch.setattr(client_api, "component_serving", lambda name, api=None: serving)
    monkeypatch.setattr(client_api, "studio_settings", lambda api=None: {})   # reopen off
    monkeypatch.setattr(client_api, "restart_component",
                        lambda name, **k: restarts.append(name) or "")
    events = []
    c = SimpleNamespace(store=SimpleNamespace(event=lambda *a, **k: events.append((a, k))))
    P = SimpleNamespace(id="game", profile=SimpleNamespace(workspace=str(tmp_path)))
    return c, P, restarts


def test_a_serving_rojo_is_not_restarted(tmp_path, monkeypatch):
    c, P, restarts = _heal_env(tmp_path, monkeypatch, "port 34872, Game session 7e63a208")
    out = autoheal.repair(c, P, {"task_id": "T1"}, "rojo", "restart")
    assert restarts == []
    assert [r["repair"] for r in out] == ["keep_rojo", "reopen_studio"] and out[0]["ok"]
    assert not out[1]["ok"] and "no Foundry Client" in out[1]["detail"]


def test_a_down_rojo_is_restarted(tmp_path, monkeypatch):
    c, P, restarts = _heal_env(tmp_path, monkeypatch, "")
    out = autoheal.repair(c, P, {"task_id": "T1"}, "rojo", "restart")
    assert restarts == ["rojo"] and [r["repair"] for r in out] == ["restart_rojo"]


# ------------------------------------------------------------------ max_agents

def test_a_reapplied_revision_keeps_the_live_max_agents(tmp_path):
    c, P, _ = tracks_env(tmp_path)
    c.tracks.set_max_agents(P, 2, "victor")
    doc = {"settings": {"name": "game"}}
    set_live(P, "max_agents", 1)                  # what replacing the profile tables did
    c.tracks.after_config_apply(P, doc, doc)
    assert auto_get(P, "max_agents") == 2 and c.tracks.max_agents() == 2


def test_a_revision_that_changes_max_agents_wins(tmp_path):
    c, P, _ = tracks_env(tmp_path)
    c.tracks.set_max_agents(P, 2, "victor")
    set_live(P, "max_agents", 3)                  # the revision's value, now applied
    c.tracks.after_config_apply(P, {"settings": {}},
                                {"settings": {"automation": {"max_agents": 3}}})
    assert auto_get(P, "max_agents") == 3
    assert not c.store.meta("max_agents:game")
