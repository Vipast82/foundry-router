"""Distributed Foundry b-3 (docs/plans/DISTRIBUTED_FOUNDRY_PHASE_B.md B2, B6, B7, B8): checks run
on the slot, remote sessions reach the training store, a remote lane is never merged before
phase d, and the slot image starts the lane agent only when lanes are on."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import types

import pytest

from foundry_client import lane as L
from foundry_coordinator.adapters.base import FAIL, PASS, UNKNOWN
from foundry_coordinator.adapters.git import GitError
from foundry_coordinator.adapters.remote_git import LaneAgent
from foundry_coordinator.adapters.verify_cmd import run_check_remote

ROOT = pathlib.Path(__file__).resolve().parents[2]
SLOT = ROOT / "contrib" / "studio-slot"


class AppAgent(LaneAgent):
    def __init__(self, host):
        from fastapi.testclient import TestClient
        from foundry_client import api

        class Mgr:
            cfg = {"client": {}}

            def status(self):
                return {"components": []}
        self.c = TestClient(api.create_app(Mgr(), "CT", lane=host, lane_token="LT"),
                            base_url="http://127.0.0.1:47811")
        super().__init__(lambda: {"url": "x", "token": "LT"}, "slot-t")

    def call(self, route, body, timeout=60):
        r = self.c.post("/api/lane/" + route, json=body, headers={"x-lane-token": "LT"})
        if r.status_code >= 400:
            raise GitError(f"HTTP {r.status_code} {r.json().get('error')}")
        return r.json()


@pytest.fixture
def lane(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    for args in (["init", "-q", "-b", "main"],):
        subprocess.run(["git", "-C", str(src), *args], check=True)
    (src / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(src), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "first"], check=True, env=env)
    base = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()
    root = tmp_path / "lanes"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(root / "pilot.git")], check=True)
    host = L.LaneHost(str(root), str(tmp_path / "state"))
    agent = AppAgent(host)
    from foundry_coordinator.lane_fetch import wait_job
    assert wait_job(agent, "pilot", "l1", "worktree-add", {"base": base},
                    sleep=lambda s: None)["ok"]
    return {"host": host, "agent": agent, "wt": host.worktree_of("pilot", "l1"),
            "state": str(tmp_path / "coord"), "base": base}


def check(cmd, **kw):
    return types.SimpleNamespace(name=kw.get("name", "c"), command=cmd, timeout_seconds=60,
                                 pass_regex=kw.get("pass_regex"), fail_regex=None,
                                 evidence=kw.get("evidence"), required=True)


def remote(lane, c):
    return run_check_remote(c, lane["agent"], "pilot", "l1", lane["wt"], "T1", lane["base"], "",
                            lane["state"], sleep=lambda s: None)


def test_a_check_runs_on_the_slot_and_is_judged_here(lane):
    py = sys.executable
    r = remote(lane, check([py, "-c", "import os; print('task', os.environ['FOUNDRY_COORD_TASK'])"],
                           pass_regex=r"^task T1$"))
    assert r.result == PASS, r.detail
    assert os.path.isfile(r.evidence) and "task T1" in open(r.evidence, encoding="utf-8").read()
    r = remote(lane, check([py, "-c", "import sys; sys.exit(3)"]))
    assert r.result == FAIL and "exit code 3" in r.detail


def test_a_missing_command_on_the_slot_is_unknown_never_pass(lane):
    r = remote(lane, check(["no-such-program-xyz"]))
    assert r.result == UNKNOWN and "not found" in r.detail


def test_evidence_is_looked_for_on_the_slot(lane):
    py = sys.executable
    c = check([py, "-c", "open('ev.txt', 'w').write('x')"], evidence="ev.txt")
    assert remote(lane, c).result == PASS
    c = check([py, "-c", "pass"], evidence="nope.txt")
    r = remote(lane, c)
    assert r.result == FAIL and "evidence file nope.txt missing" in r.detail


def test_an_unreachable_slot_is_unknown(lane):
    down = LaneAgent(lambda: None, "slot-t")
    r = run_check_remote(check(["true"]), down, "pilot", "l1", lane["wt"], "T1", lane["base"],
                         "", lane["state"], sleep=lambda s: None)
    assert r.result == UNKNOWN and "did not finish" in r.detail


def test_the_generic_adapter_sends_a_lane_on_a_slot_to_its_agent(monkeypatch):
    from foundry_coordinator.adapters import project as PR
    seen = []
    monkeypatch.setattr("foundry_coordinator.adapters.verify_cmd.run_check_remote",
                        lambda c, agent, pid, lane, ws, tid, sha, base, sd: seen.append(
                            (agent.name, pid, lane, ws)) or "R")
    monkeypatch.setattr(PR, "run_check", lambda *a: pytest.fail("ran locally"))
    a = PR.GenericAdapter.__new__(PR.GenericAdapter)
    prof = types.SimpleNamespace(id="pilot", workspace="/srv/foundry-lanes/pilot/l1",
                                 checks=[types.SimpleNamespace(phase="verify")])
    prof.__dict__.update(lane_host="slot-4", track="l1")
    a.p, a.state_dir = prof, "s"
    out = a.verify(types.SimpleNamespace(task_id="T1"), "verify", "sha", "base")
    assert out == ["R"] and seen == [("slot-4", "pilot", "l1", "/srv/foundry-lanes/pilot/l1")]


# ---------------------------------------------------------------------------- B7 training

def test_remote_sessions_are_exported_with_their_children(tmp_path, monkeypatch):
    from foundry_coordinator import training_hook as TH
    from foundry_training.adapters import kilo as K
    got = {"s1": {"info": {"id": "s1"}, "messages": [1], "children": ["h1"]},
           "h1": {"info": {"id": "h1"}, "messages": [2], "children": []}}
    monkeypatch.setattr(K, "from_api", lambda url, sid, pw, d: got[sid])
    P = types.SimpleNamespace(profile=types.SimpleNamespace(workspace="/srv/x"))
    P.profile.__dict__["lane_host"] = "slot-4"
    ep = lambda host, kind: {"url": "http://127.0.0.1:1", "password": "p"}
    out, gaps = TH.export_remote_sessions(P, [{"session_id": "s1", "client": "kilo"}],
                                          str(tmp_path / "x"), endpoint=ep)
    assert set(out) == {"s1", "h1"} and gaps == []
    d = json.load(open(out["h1"], encoding="utf-8"))
    assert d["_lane_host"] == "slot-4" and os.path.basename(out["h1"]).startswith("kilo__")
    out, gaps = TH.export_remote_sessions(P, [{"session_id": "s9", "client": "opencode"}],
                                          str(tmp_path / "y"), endpoint=lambda h, k: None)
    assert out == {} and "tunnel is down" in gaps[0]
    local = types.SimpleNamespace(profile=types.SimpleNamespace(workspace="C:/x"))
    assert TH.export_remote_sessions(local, [{"session_id": "s1"}], "z") == ({}, [])


def test_the_pipeline_ingests_exports_and_skips_them_on_the_api(tmp_path):
    from types import SimpleNamespace
    from foundry_training.pipeline import Pipeline
    calls = []
    p = Pipeline.__new__(Pipeline)
    p.projects = {"au": SimpleNamespace(workspace="ws")}
    p._allowed = lambda pid, source: True
    p._cfg = lambda pid: p.projects[pid]
    p.ingest_coordinator = lambda db: None
    p.ingest_kilo = lambda api=None, exports=(), agent="kilo": calls.append(
        ("api", tuple(api["sessions"])) if api else ("exports", agent, tuple(exports)))
    p.ingest_opencode = lambda api: calls.append(("oc-api", tuple(api["sessions"])))
    p.ingest_foundry = lambda url, key: None
    p.ingest_git = p.ingest_gate = lambda pid: None
    p.curate = lambda pid: {"episodes": 1}
    p.store = SimpleNamespace(events=lambda pid, where: [], gap=lambda *a: None)
    spool = tmp_path / "spool"
    (spool / "remote").mkdir(parents=True)
    ex = spool / "remote" / "kilo__s1.json"
    ex.write_text("{}")
    (spool / "au__T1__1.json").write_text(json.dumps({
        "project_id": "au", "task_id": "T1", "sessions": ["s1", "s2"], "session_roles": {},
        "session_exports": {"s1": str(ex)}}))
    p.process_queue(str(spool), kilo_api={"url": "k"})
    assert ("exports", "kilo", (str(ex),)) in calls
    assert ("api", ("s2",)) in calls                       # s1 is not asked of this machine


# ---------------------------------------------------------------------------- B6 switches

def test_a_remote_lane_needs_both_switches(monkeypatch):
    from foundry_coordinator.tracks import manager as M
    tm = M.TrackManager.__new__(M.TrackManager)
    prof = types.SimpleNamespace(raw={"slots": {"slot-4": {"container": "c", "lanes": True},
                                                "slot-1": {"container": "c"}}})
    P = types.SimpleNamespace(profile=prof)
    monkeypatch.setattr("foundry_coordinator.automation.get", lambda P, k: True)
    assert tm._remote_lane_slot(P, "slot-4")["id"] == "slot-4"
    assert tm._remote_lane_slot(P, "slot-1") == {}           # lanes off on that slot
    assert tm._remote_lane_slot(P, "") == {}
    monkeypatch.setattr("foundry_coordinator.automation.get", lambda P, k: False)
    assert tm._remote_lane_slot(P, "slot-4") == {}           # [automation] remote_lanes off


@pytest.mark.parametrize("needs,slot,why", [
    # phase c lifted the adapter and Studio refusals; Blender, and Studio on Windows, remain
    (["blender", "code"], {"id": "slot-4"}, "no blender yet"),
    (["studio"], {"id": "slot-w", "os": "windows"}, "no studio yet"),
])
def test_what_a_remote_lane_cannot_do_yet_is_refused(needs, slot, why):
    from foundry_coordinator.tracks import manager as M
    tm = M.TrackManager.__new__(M.TrackManager)
    P = types.SimpleNamespace(profile=types.SimpleNamespace(adapter=""))
    with pytest.raises(M.TrackError, match=why):
        tm._create_remote(P, "l1", "parallel", ["T1"], needs, "op", slot["id"], slot)


def test_a_remote_lane_brought_home_merges_as_a_local_track():
    """Phase d replaced b-3's "never merged": once home, the merge goes on as for any track
    (here it reaches the usual wait for a quiet main sprint)."""
    from foundry_coordinator.tracks.merge import MergeManager
    homed, waits = [], []
    home = {"name": "l1", "state": "merging", "branch": "track/l1",
            "data": {"was_host": "slot-4", "merge_stage": "wait"}}
    mm = MergeManager.__new__(MergeManager)
    mm.tm = types.SimpleNamespace(main_of=lambda P: P,
                                  bring_home=lambda P, name, actor, why: homed.append(why)
                                  or {"ok": True, "detail": "home"},
                                  main_quiescent=lambda P: (False, "the main sprint is busy"))
    mm.store = types.SimpleNamespace(track=lambda pid, name: home)
    mm._wait = lambda P, row, why: waits.append(why)
    row = {"name": "l1", "state": "merging", "branch": "track/l1",
           "data": {"host": "slot-4", "merge_stage": "wait"}}
    mm.step(types.SimpleNamespace(id="pilot"), row)
    assert homed == ["merge"] and waits == ["the main sprint is busy"]


def test_remote_automation_setting_ships_off():
    from foundry_automation.schema import AUTOMATION_DEFAULTS, _AUTO_DESC
    assert AUTOMATION_DEFAULTS["remote_lanes"] is False and "phase d" in _AUTO_DESC["remote_lanes"]


# ---------------------------------------------------------------------------- B2 the slot

def test_the_lane_compose_publishes_on_the_hosts_loopback_only():
    text = (SLOT / "proxmox" / "docker-compose.lanes.yml").read_text(encoding="utf-8")
    ports = [ln.strip() for ln in text.splitlines() if ln.strip().startswith('- "127') or
             ln.strip().startswith("- \"0") or ":47811" in ln or ":4096" in ln or ":4097" in ln]
    assert ports and all(p.startswith('- "127.0.0.1:') for p in ports), ports
    assert '"/srv/foundry-lanes:/srv/foundry-lanes"' in text
    assert "/srv/studio-slot/AnimalUprising.git:/srv/studio-slot/AnimalUprising.git:ro" in text


def test_the_slot_starts_the_agent_only_with_lanes_on():
    ep = (SLOT / "entrypoint.sh").read_text(encoding="utf-8")
    assert 'if [ "${SLOT_LANES:-0}" = "1" ]; then\n    lane-agent' in ep
    sh = (SLOT / "proxmox" / "setup-host.sh").read_text(encoding="utf-8")
    assert 'if [ "$(py get settings.lanes)" = True ]; then' in sh
    assert 'COMPOSE_FILES="$COMPOSE_FILES:docker-compose.lanes.yml"' in sh
    df = (SLOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY lane-agent.sh /usr/local/bin/lane-agent" in df and "/opt/foundry-venv" in df


def test_the_lane_agent_script_never_prints_a_secret():
    text = (SLOT / "lane-agent.sh").read_text(encoding="utf-8")
    for ln in text.splitlines():
        if "echo" in ln:
            assert "vals" not in ln and "password" not in ln.lower() and "KEY" not in ln, ln
    assert "0o600" in text and 'secret_store = "file"' in text
    assert 'listen = "0.0.0.0"' in text and 'hostname = "0.0.0.0"' in text


def _gnu_bash() -> str:
    """A real bash, or ''. On a Windows runner `bash` can be System32's WSL launcher, which
    only prints "no distribution installed" (in UTF-16): that is no bash."""
    import shutil
    exe = shutil.which("bash") or ""
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return exe if r.returncode == 0 and b"GNU bash" in (r.stdout or b"") else ""


@pytest.mark.skipif(not _gnu_bash(), reason="no GNU bash here")
@pytest.mark.parametrize("script", ["lane-agent.sh", "entrypoint.sh", "proxmox/setup-host.sh"])
def test_the_scripts_parse(script):
    r = subprocess.run([_gnu_bash(), "-n", str(SLOT / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_agent_answers_run_status_by_post(lane):
    rid = lane["agent"].call("run", {"project": "pilot", "lane": "l1", "job": "verify",
                                     "args": {"argv": ["git", "--version"]}})["run_id"]
    st = lane["agent"].call("run_status", {"id": rid, "offset": 0})
    assert st["id"] == rid
