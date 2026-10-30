"""0.145.0: Studio auto-reopen from auto-heal (guards, waiting for the client job)
and track sprints that never need Studio (verify / closeout without the Studio rows,
the track brief, a Studio-only BLOCKED report goes to verify)."""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from foundry_coordinator import autoheal, client_api
from foundry_coordinator import states as S
from foundry_coordinator.adapters.base import FAIL, PASS
from foundry_coordinator.engine import TRACK_BRIEF, TRACK_STUDIO_BLOCKER
from tests.coordinator.helpers import by_task, run_until
from tests.coordinator.test_tracks_manager import env as tracks_env


# ------------------------------------------------------------------ signatures

def test_owner_click_reports_are_a_rojo_connect_stop():
    for text in ("Owner click needed in the open Animal Uprising [TEST] Studio: Plugins → "
                 "Rojo → Connect", "the Rojo plugin is not connected", "click Connect in Rojo"):
        assert autoheal.infra_signature(text) == "rojo_connect", text
    assert autoheal.plan("session_needs_human", "Plugins -> Rojo -> Connect") == \
        ("rojo_connect", "restart")


# ------------------------------------------------------------------ reopen guards

class _Store:
    def __init__(self, locks=()):
        self._locks = list(locks)
        self.events = []

    def locks(self):
        return self._locks

    def event(self, *a, **k):
        self.events.append((a, k))


def _env(tmp_path, monkeypatch, *, auto=True, locks=(), busy="", job_state="running"):
    calls = []
    monkeypatch.setattr(client_api, "component_serving", lambda name, api=None: "port 34872")
    monkeypatch.setattr(client_api, "studio_settings", lambda api=None: {"auto_reopen": auto})
    monkeypatch.setattr(client_api, "studio_reopen",
                        lambda task, dry_run=False, api=None: calls.append(task) or
                        {"job": "sr-1", "state": job_state})
    c = SimpleNamespace(store=_Store(locks))
    P = SimpleNamespace(id="game", profile=SimpleNamespace(workspace=str(tmp_path)),
                        adapter=SimpleNamespace(studio_busy=lambda tid: busy))
    return c, P, {"task_id": "T1"}, calls


def test_a_serving_rojo_with_auto_reopen_starts_a_studio_job(tmp_path, monkeypatch):
    c, P, t, calls = _env(tmp_path, monkeypatch)
    out = autoheal.repair(c, P, t, "rojo_connect", "restart")
    assert calls == ["T1"]
    job = [r for r in out if r["repair"] == "reopen_studio"]
    assert job and job[0]["job"] == "sr-1" and job[0]["ok"]


@pytest.mark.parametrize("kw,why", [
    ({"auto": False}, "auto_reopen is off"),
    ({"locks": [{"resource": "studio:ws", "project_id": "game", "task_id": "T2"}]},
     "leased to game/T2"),
    ({"busy": "studio.lock is held: kilo-x ... T2"}, "studio.lock is held"),
])
def test_reopen_guards(tmp_path, monkeypatch, kw, why):
    c, P, t, calls = _env(tmp_path, monkeypatch, **kw)
    out = autoheal.repair(c, P, t, "rojo_connect", "restart")
    assert calls == []
    r = next(r for r in out if r["repair"] == "reopen_studio")
    assert not r["ok"] and why in r["detail"]


def test_a_live_gate_runner_blocks_a_reopen(tmp_path, monkeypatch):
    c, P, t, calls = _env(tmp_path, monkeypatch)
    p = autoheal.runner_lock_path(str(tmp_path))
    os.makedirs(os.path.dirname(p))
    open(p, "w").write('{"pid": %d, "sprint": "T2"}' % os.getpid())
    assert "gate runner is live" in autoheal.studio_guard(c, P, t)


def test_au_studio_lock_names_its_sprint(tmp_path, monkeypatch):
    from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
    monkeypatch.setenv("AU_LOCK_DIR", str(tmp_path))
    a = object.__new__(AnimalUprisingAdapter)
    assert a.studio_busy("T1") == ""
    (tmp_path / "studio.lock").write_text("kilo-t1 2026-10-27T13:58:19Z T1 gate\n")
    assert a.studio_busy("T1") == ""
    assert "held" in a.studio_busy("T2")


def test_the_retry_waits_for_the_studio_job(tmp_path, monkeypatch):
    states = {"s": "running"}
    monkeypatch.setattr(client_api, "studio_job",
                        lambda jid, api=None: {"job": jid, "state": states["s"],
                                               "result": "TEST place reopened", "steps": []})
    c = SimpleNamespace(store=_Store(), clock=lambda: 1000.0)
    P = SimpleNamespace(id="game")
    ah = {"studio_job": "sr-1", "studio_job_ts": 990.0}
    assert autoheal._studio_job_settled(c, P, {"task_id": "T1"}, {}, ah) is False
    states["s"] = "done"
    assert autoheal._studio_job_settled(c, P, {"task_id": "T1"}, {}, ah) is True
    assert c.store.events[-1][0][2] == "studio_reopened"


# ------------------------------------------------------------------ track sprints

def test_au_track_done_check_leaves_the_studio_rows_to_the_merge_gate(monkeypatch, tmp_path):
    from foundry_coordinator.projects.animaluprising import adapter as AD
    out = ("PASS COMMITS  3 commit(s)\nPASS SCOPE  all in the plan list\n"
           "FAIL GATE     no gate verdict\nFAIL BOOT  0 boot lines\nFAIL EVIDENCE missing\n"
           "PASS STATE ledger row\nDONE-CHECK T1 FAIL (3 problems)\n")
    monkeypatch.setattr(AD.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 1, stdout=out, stderr=""))
    a = object.__new__(AD.AnimalUprisingAdapter)
    a.p = SimpleNamespace(id="game", workspace=str(tmp_path))
    a.state_dir = str(tmp_path / "state")
    monkeypatch.setattr(a, "_python", lambda: ["python"], raising=False)
    monkeypatch.setattr(a, "_forgive_memory_scope", lambda spec, res: False)
    spec = SimpleNamespace(task_id="T1", data={})
    full = {r.check: r.result for r in a._done_check(spec, "closeout", "rev")}
    assert full["au.done_check.GATE"] == FAIL and full["au.done_check"] == FAIL
    track = {r.check: r.result for r in a._done_check(spec, "closeout", "rev",
                                                      skip=AD.STUDIO_ROWS)}
    assert not any(k.endswith((".GATE", ".BOOT", ".EVIDENCE")) for k in track)
    assert track["au.done_check"] == PASS


def test_track_studio_blocker_pattern():
    for text in ("studio.lock owned by kilo-gfgate1-a3", "needs the Studio gate",
                 "Rojo is serving the main checkout", "gate run blocked"):
        assert TRACK_STUDIO_BLOCKER.search(text), text
    assert not TRACK_STUDIO_BLOCKER.search("the boss name needs a design decision")


def _blocked_on_studio(ctx):
    if ctx["phase"] != "implement":
        return {"final": '```coordinator-report\n{"status": "DONE", "summary": "ok"}\n```'}
    return {"writes": {"art/b.txt": "OK"}, "commit": f"{ctx['task_id']}: work",
            "final": '```coordinator-report\n{"status": "BLOCKED", "blocker": "studio.lock '
                     'owned by kilo-main; the Studio gate cannot run", "options": ["wait", '
                     '"skip"]}\n```'}


def test_a_track_session_blocked_on_studio_goes_to_verify(tmp_path):
    prompts = []

    def b1(ctx):
        prompts.append(ctx["prompt"])
        return _blocked_on_studio(ctx)
    c, P, _ = tracks_env(tmp_path, extra="\n[automation]\nmax_agents = 2\n",
                         scripts={"game": by_task({"B1": b1})})
    c.tracks.create(P, "blender", ["B1"], actor="victor")
    c.tracks.start(P, "blender", "victor")
    assert run_until(c, lambda: c.store.task("game", "B1")["state"] == S.COMPLETE, limit=60)
    ev = [e for e in c.store.events("game", 200) if e["kind"] == "track_studio_blocker_ignored"]
    assert ev and not [e for e in c.store.events("game", 200)
                       if e["kind"] == "implementer_blocked_decision"]
    assert "Track sprint: no Studio here" in prompts[0] and "`blender`" in prompts[0]


def test_files_missing_in_studio_restart_rojo_then_reopen(tmp_path, monkeypatch):
    """2026-10-29 22:16: a track merge added mounted check files to default.project.json;
    the running Rojo never re-read it and Studio had none of them (live=missing)."""
    c, P, t, calls = _env(tmp_path, monkeypatch)
    restarted = []
    monkeypatch.setattr(client_api, "restart_component",
                        lambda name, api=None: restarted.append(name) or "")
    t["blocker"] = ("scripted Studio gate: tooling: PRE-PLAY verify: Studio's loaded sources are "
                    "not HEAD's (matched=3/11 mismatched=2 unresolved=6; UNRESOLVED "
                    "tests/MV2_4_check.lua live=missing head=0a0ef0d1); Rojo / plugin sync is stale")
    out = autoheal.repair(c, P, t, "rojo_stale", "verify")
    assert restarted == ["rojo"] and calls == ["T1"]
    names = [r["repair"] for r in out]
    assert "restart_rojo" in names and "reopen_studio" in names and "keep_rojo" not in names
    # without missing files a serving Rojo is still kept (reopen only)
    c, P, t, calls = _env(tmp_path, monkeypatch)
    restarted.clear()
    autoheal.repair(c, P, t, "rojo_stale", "verify")
    assert restarted == [] and calls == ["T1"]
