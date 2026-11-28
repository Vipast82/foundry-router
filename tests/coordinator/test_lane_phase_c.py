"""Distributed Foundry phase c: a lane on a slot runs its Lune rows and Studio gate there, reads
through its read mirror here, and its agent config uses the slot's own Studio MCP."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import types

import pytest

from foundry_client import lane as L
from foundry_coordinator import slots as SL
from foundry_coordinator.adapters.base import FAIL, PASS, UNKNOWN
from foundry_coordinator.adapters.git import GitError, Workspace
from foundry_coordinator.adapters.remote_git import LaneAgent, RemoteWorkspace

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True, env=ENV).stdout.strip()


# ---------------------------------------------------------------------------- Lune on the slot

def au(monkeypatch, job):
    from foundry_coordinator.projects.animaluprising import adapter as A
    a = A.AnimalUprisingAdapter.__new__(A.AnimalUprisingAdapter)
    a.p = types.SimpleNamespace(id="animaluprising", workspace="C:/AU/.lanes/slot-4/l1")
    a.p.__dict__.update(lane_host="slot-4", track="l1")
    a.cfg, a.state_dir = {}, "s"
    seen = {}

    def fake(agent, project, lane, kind, args, wait_s=0, sleep=None):
        seen.update(agent=agent.name, project=project, lane=lane, kind=kind, args=args)
        return job
    monkeypatch.setattr("foundry_coordinator.lane_fetch.wait_job", fake)
    return a, seen


def test_lune_rows_of_a_remote_lane_run_on_its_slot(monkeypatch, tmp_path):
    a, seen = au(monkeypatch, {"state": "done", "rc": 0,
                               "output": "LANE lune ...\r\nLUNE 1654/1654 PASS (12 s)\r\n"})
    r = a._lune_run("lune", "au.lune", [], r"^LUNE \d+/\d+ PASS[^\r\n]*", 3, str(tmp_path), "sha")
    assert r.result == PASS and r.detail.startswith("LUNE 1654/1654 PASS")
    assert seen == {"agent": "slot-4", "project": "animaluprising", "lane": "l1", "kind": "lune",
                    "args": {"args": [], "workers": 3, "timeout": a._lune_timeout()}}
    assert "LUNE 1654/1654 PASS" in open(r.evidence, encoding="utf-8").read()


@pytest.mark.parametrize("job,want", [
    ({"state": "failed", "rc": 1, "output": "LUNE 1650/1654 FAIL"}, FAIL),
    ({"state": "unreachable", "rc": None, "output": "tunnel down"}, UNKNOWN),
    ({"state": "failed", "rc": 127, "output": "LANE command not found: lune"}, UNKNOWN),
])
def test_a_remote_lune_that_does_not_pass_never_passes(monkeypatch, tmp_path, job, want):
    a, _ = au(monkeypatch, job)
    r = a._lune_run("lune", "au.lune", [], r"^LUNE \d+/\d+ PASS[^\r\n]*", 0, str(tmp_path), "sha")
    assert r.result == want


def test_the_agent_says_when_lune_is_missing(tmp_path, monkeypatch):
    root = tmp_path / "lanes"
    root.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "rokit.toml").write_text("[tools]\n", encoding="utf-8")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "x")
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(root / "au.git")], check=True)
    h = L.LaneHost(str(root), str(tmp_path / "st"))
    monkeypatch.setattr(L.shutil, "which", lambda name, path=None: None)
    from foundry_coordinator.lane_fetch import wait_job

    class Direct:
        name = "x"

        def call(self, route, body, timeout=60):
            if route == "run":
                return h.start_job(body["project"], body["lane"], body["job"], body["args"])
            return h.run_status(body["id"], body.get("offset", 0))
    base = git(src, "rev-parse", "HEAD")
    added = wait_job(Direct(), "au", "l1", "worktree-add", {"base": base}, sleep=lambda s: None)
    # the toolchain install is its own job (review 2026-11-27): worktree-add does not wait
    import re
    import time
    assert added["ok"], added
    rid = re.search(r"LANE toolchain: started as run (\w+)", added["output"]).group(1)
    t0 = time.time()
    while h.run_status(rid)["state"] == "running" and time.time() - t0 < 60:
        time.sleep(0.05)
    assert "toolchain: rokit not found (skipped)" in h.run_status(rid)["output"]
    job = wait_job(Direct(), "au", "l1", "lune", {"args": []}, sleep=lambda s: None)
    assert job["rc"] == 127 and "not found: lune" in job["output"]


# ---------------------------------------------------------------------------- the Studio gate

def test_a_remote_lanes_gate_outputs_also_land_in_its_slot_worktree(monkeypatch):
    seen = {}
    monkeypatch.setattr(SL, "_docker", lambda slot, script, timeout=None, stdout=None:
                        seen.update(script=script) or types.SimpleNamespace(stdout=b"3\n"))
    n = SL.copy_to_lane({"container": "studio-slot"}, "/home/studio/work/AnimalUprising",
                        "/srv/foundry-lanes/animaluprising/l1", "GF-SEC3")
    assert n == 3
    assert "docs/progress/GF-SEC3_* docs/evidence/GF-SEC3 .plan/tmp/gate/GF-SEC3" in seen["script"]
    assert "/srv/foundry-lanes/animaluprising/l1" in seen["script"]
    assert SL.copy_to_lane({"container": "c"}, "/r", "relative/path", "T1") == 0
    assert SL.copy_to_lane({"container": "c"}, "/r", "/srv/x", "../bad") == 0


def test_wrap_pipeline_names_the_lane_worktree():
    argv = SL.wrap_pipeline({"container": "c", "repo": "/r", "foundry": "/opt/foundry"},
                            ["pipeline", "--repo", "x"], "C:/AU/.lanes/slot-4/l1", "T1", "abc",
                            lane_worktree="/srv/foundry-lanes/animaluprising/l1")
    i = argv.index("--lane-worktree")
    assert argv[i + 1] == "/srv/foundry-lanes/animaluprising/l1" and argv.index("--") > i
    plain = SL.wrap_pipeline({"container": "c", "repo": "/r", "foundry": "/opt/foundry"},
                             ["pipeline"], "C:/AU/.tracks/l1", "T1", "abc")
    assert "--lane-worktree" not in plain


# ---------------------------------------------------------------------------- uploads

class Spy(LaneAgent):
    def __init__(self, host):
        from fastapi.testclient import TestClient
        from foundry_client import api

        class Mgr:
            cfg = {"client": {}}

            def status(self):
                return {"components": []}
        self.c = TestClient(api.create_app(Mgr(), "CT", lane=host, lane_token="LT"),
                            base_url="http://127.0.0.1:47811")
        self.writes = []
        super().__init__(lambda: {"url": "x", "token": "LT"}, "slot-t")

    def call(self, route, body, timeout=60):
        if route == "write":
            self.writes.append(body["path"])
        r = self.c.post("/api/lane/" + route, json=body, headers={"x-lane-token": "LT"})
        if r.status_code >= 400:
            raise GitError(f"HTTP {r.status_code} {r.json().get('error')}")
        return r.json()


def test_only_changed_mirror_files_are_sent_and_identical_ones_are_not(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "a.txt").write_text("a\n", encoding="utf-8")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", "1")
    root = tmp_path / "lanes"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(root / "p.git")], check=True)
    host = L.LaneHost(str(root), str(tmp_path / "st"))
    from foundry_coordinator.lane_fetch import wait_job
    spy = Spy(host)
    base = git(src, "rev-parse", "HEAD")
    assert wait_job(spy, "p", "l1", "worktree-add", {"base": base}, sleep=lambda s: None)["ok"]
    slot_wt = host.worktree_of("p", "l1")
    mirror = tmp_path / "mirror"
    subprocess.run(["git", "clone", "-q", str(src), str(mirror)], check=True)
    ws = RemoteWorkspace(spy, "p", "l1", slot_wt, mirror=str(mirror))
    # the gate copied the same output to both sides
    for d in (slot_wt, str(mirror)):
        os.makedirs(os.path.join(d, "docs", "evidence", "T1"), exist_ok=True)
        with open(os.path.join(d, "docs", "evidence", "T1", "f.png"), "wb") as f:
            f.write(b"\x89PNG same")
    # and the coordinator wrote one file only here
    with open(os.path.join(mirror, "docs", "note.md"), "w", encoding="utf-8") as f:
        f.write("here only\n")
    spy.writes.clear()
    sent = ws._upload_mirror_changes(["docs/evidence/T1", "docs/note.md"])
    assert sent == ["docs/note.md"] and spy.writes == ["docs/note.md"]
    assert open(os.path.join(slot_wt, "docs", "note.md"), encoding="utf-8").read() == "here only\n"


# ---------------------------------------------------------------------------- Studio MCP

def test_a_studio_lane_gets_the_slots_own_studio_mcp(tmp_path, monkeypatch):
    from tests.coordinator.test_lane_track import AppAgent
    from tests.coordinator.helpers import by_task, coordinator, make_repo, write_ok, write_profile
    from foundry_coordinator.tracks import manager as M
    from foundry_coordinator import lane_tunnel
    tasks = [{"id": "S1", "title": "studio sprint", "spec": "Write src/s.txt.",
              "scope": ["src/s.txt"], "tags": ["needs:studio"]}]
    kilo = json.dumps({"mcp": {
        "Roblox_Studio": {"type": "local", "command": ["C:/x/StudioMCP.exe"], "enabled": True},
        "robloxstudio-mcp": {"type": "local", "command": ["npx.cmd", "-y", "x"], "enabled": True},
        "media": {"type": "local", "command": ["C:/x/media.exe"], "enabled": True}}}) + "\n"
    root, _ = make_repo(tmp_path, "game", tasks, extra_files={"kilo.jsonc": kilo})
    prof = write_profile(tmp_path, "game", root, extra="[automation]\nremote_lanes = true\n",
                         publish="none", review=False)
    c = coordinator(tmp_path, [prof], {"game": by_task({"S1": write_ok("src/s.txt")})})
    P = c.projects["game"]
    lanes = tmp_path / "lanes"
    lanes.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", str(lanes / "game.git")], check=True)
    P.profile.raw.setdefault("slots", {})["slot-4"] = {
        "container": "studio-slot", "lanes": True, "lanes_root": str(lanes).replace("\\", "/")}
    agent = AppAgent(L.LaneHost(str(lanes), str(tmp_path / "ls")))
    monkeypatch.setattr(M.TrackManager, "_lane_agent", lambda self, sid: agent)
    monkeypatch.setattr(lane_tunnel.MANAGER, "endpoint",
                        lambda sid, kind="agent": {"url": "x", "token": "LT"})
    import foundry_coordinator.adapters.remote_git as RG
    monkeypatch.setattr(RG, "LaneAgent", lambda endpoint, name="": agent)
    row = c.tracks.create(P, "studio1", ["S1"], actor="victor", slot="slot-4")
    assert "studio" in row["needs"] and row["data"]["host"] == "slot-4"
    cfg = json.load(open(os.path.join(row["data"]["remote_worktree"], "kilo.jsonc"),
                         encoding="utf-8"))
    assert cfg["mcp"]["Roblox_Studio"]["command"] == ["/usr/local/bin/studio-mcp-official"]
    assert cfg["mcp"]["robloxstudio-mcp"]["command"] == ["/usr/local/bin/robloxstudio-mcp"]
    assert cfg["mcp"]["Roblox_Studio"]["enabled"] is True
    assert cfg["mcp"]["media"]["enabled"] is False
