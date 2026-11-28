"""A lane on a slot from end to end through the real TrackManager (Distributed Foundry b-3, B6):
`tracks create --slot` on a slot with lanes on, in a project with remote_lanes on, puts the
worktree on the slot (here: a LaneHost on this machine behind the real routes), writes its agent
config there, fetches its commits back and saves its dirty files at a stop."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from foundry_client import lane as L
from foundry_coordinator.adapters.git import GitError
from foundry_coordinator.adapters.remote_git import LaneAgent, RemoteWorkspace
from foundry_coordinator.tracks import manager as M
from tests.coordinator.helpers import by_task, coordinator, make_repo, write_ok, write_profile

TASKS = [
    {"id": "T1", "title": "a sprint on the slot", "spec": "Write src/a.txt.", "scope": ["src/a.txt"]},
    {"id": "B1", "title": "blender sprint", "spec": "Write art/b.txt.", "scope": ["art/b.txt"],
     "tags": ["needs:blender"]},
]
EXTRA = """
[automation]
max_agents = 2
remote_lanes = true
"""


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
        super().__init__(lambda: {"url": "x", "token": "LT"}, "slot-4")

    def call(self, route, body, timeout=60):
        r = self.c.post("/api/lane/" + route, json=body, headers={"x-lane-token": "LT"})
        if r.status_code >= 400:
            raise GitError(f"HTTP {r.status_code} {r.json().get('error')}")
        return r.json()


@pytest.fixture
def world(tmp_path, monkeypatch):
    kilo = '{"mcp": {"media": {"type": "local", "command": ["C:/x/media.exe"], "enabled": true},' \
           ' "docs": {"type": "remote", "url": "http://docs", "enabled": true}}}\n'
    root, _remote = make_repo(tmp_path, "game", TASKS, extra_files={"kilo.jsonc": kilo})
    prof = write_profile(tmp_path, "game", root, extra=EXTRA, publish="none", review=False)
    c = coordinator(tmp_path, [prof], {"game": by_task({"T1": write_ok("src/a.txt"),
                                                        "B1": write_ok("art/b.txt")})})
    P = c.projects["game"]
    lanes = tmp_path / "lanes"
    lanes.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", str(lanes / "game.git")], check=True)
    P.profile.raw.setdefault("slots", {})["slot-4"] = {
        "container": "studio-slot", "lanes": True, "lanes_root": str(lanes).replace("\\", "/")}
    host = L.LaneHost(str(lanes), str(tmp_path / "lane-state"))
    agent = AppAgent(host)
    monkeypatch.setattr(M.TrackManager, "_lane_agent", lambda self, sid: agent)
    from foundry_coordinator import lane_tunnel
    # the tunnel's endpoint for slot-4 "is up" (the agent itself is the in-process AppAgent)
    monkeypatch.setattr(lane_tunnel.MANAGER, "endpoint",
                        lambda sid, kind="agent": {"url": "x", "token": "LT"})
    import foundry_coordinator.adapters.remote_git as RG
    monkeypatch.setattr(RG, "LaneAgent", lambda endpoint, name="": agent)
    return {"c": c, "P": P, "root": root, "host": host, "agent": agent, "lanes": lanes}


def test_a_remote_lane_lives_on_the_slot(world):
    c, P = world["c"], world["P"]
    row = c.tracks.create(P, "remote1", ["T1"], actor="victor", slot="slot-4")
    assert row["data"]["host"] == "slot-4" and row["data"]["slot"] == "slot-4"
    slot_wt = row["data"]["remote_worktree"]
    assert slot_wt == f"{world['lanes'].as_posix()}/game/remote1"
    assert os.path.isdir(slot_wt)                              # made by the lane agent
    # phase c: the row's worktree is the read mirror here, on the lane's branch
    mirror = os.path.join(os.path.realpath(world["root"]), ".lanes", "slot-4", "remote1")
    assert row["worktree"] == os.path.realpath(mirror) and os.path.isfile(
        os.path.join(mirror, "src", "a.txt") if os.path.exists(os.path.join(mirror, "src")) else
        os.path.join(mirror, "tasks.json"))
    assert subprocess.run(["git", "-C", mirror, "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True).stdout.strip() == "track/remote1"
    assert "/.lanes/" in open(os.path.join(world["root"], ".git", "info", "exclude"),
                              encoding="utf-8").read()
    assert not os.path.isdir(os.path.join(world["root"], ".tracks", "remote1"))
    # the agent config was written on the slot (and the mirror): local MCP servers off
    for where in (slot_wt, mirror):
        cfg = json.load(open(os.path.join(where, "kilo.jsonc"), encoding="utf-8"))
        assert cfg["mcp"]["media"]["enabled"] is False and cfg["mcp"]["docs"]["enabled"] is True
    assert row["data"]["kilo_config"] == slot_wt + "/kilo.jsonc"
    st = c.tracks.status(P)
    assert next(t for t in st["tracks"] if t["name"] == "remote1")["host"] == "slot-4"
    TP = c.tracks.track_project(P, c.store.track(P.id, "remote1"))
    assert isinstance(TP.ws, RemoteWorkspace) and TP.profile.__dict__["lane_host"] == "slot-4"
    assert TP.ws.path == slot_wt and TP.ws.mirror == row["worktree"]
    assert TP.profile.workspace == row["worktree"]             # reads go to the mirror
    assert TP.profile.__dict__["remote_workspace"] == slot_wt


def test_its_commits_come_back_and_a_stop_saves_its_files(world):
    c, P = world["c"], world["P"]
    row = c.tracks.create(P, "remote1", ["T1"], actor="victor", slot="slot-4")
    slot_wt = row["data"]["remote_worktree"]
    # a session's commit on the slot (straight into the slot's worktree, as Kilo does)
    sess = RemoteWorkspace(world["agent"], P.id, "remote1", slot_wt)
    sess.write("src/a.txt", b"made on the slot\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    sha = sess.commit_paths(["src/a.txt"], "T1: a", env=env)
    out = c.tracks.lane_fetch(P, c.store.track(P.id, "remote1"), force=True)
    assert out["ok"] and out["moved"], out
    got = subprocess.run(["git", "-C", str(world["root"]), "rev-parse", "refs/heads/track/remote1"],
                         capture_output=True, text=True).stdout.strip()
    assert got == sha
    # the mirror's files followed the fast-forward
    assert open(os.path.join(row["worktree"], "src", "a.txt"), "rb").read().replace(b"\r\n", b"\n") \
        == b"made on the slot\n"
    # the coordinator writes a file in the MIRROR (as studio_calls / memory do) and commits it
    # through the track's workspace: it reaches the slot's commit and comes back here
    ws = c.tracks.track_project(P, c.store.track(P.id, "remote1")).ws
    with open(os.path.join(row["worktree"], "docs_note.txt"), "w", encoding="utf-8") as f:
        f.write("written here\n")
    sha2 = ws.commit_paths(["docs_note.txt"], "coordinator note", env=env)
    assert sha2 and sess.read("docs_note.txt").replace(b"\r\n", b"\n") == b"written here\n"
    got = subprocess.run(["git", "-C", row["worktree"], "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    assert got == sha2                                          # fetched back at once
    ws = sess
    ws.write("notes.txt", b"not committed\n")
    c.tracks.stop(P, "remote1", actor="victor", reason="test")
    ev = [e for e in c.store.events("game", 200) if e["kind"] == "track_lane_stopped"]
    # notes.txt and kilo.jsonc (the coordinator's own rewrite, as on a local track)
    assert ev and "2 of 2" in ev[0]["detail"]["saved"]
    saved = ev[0]["detail"]["saves"]
    listed = open(os.path.join(saved, "_list.txt"), encoding="utf-8").read()
    assert "notes.txt" in listed and "kilo.jsonc" in listed
    assert open(os.path.join(saved, "notes.txt"), "rb").read() == b"not committed\n"
    assert os.path.isdir(slot_wt) and os.path.isdir(row["worktree"])   # both kept


def test_what_a_remote_lane_cannot_do_yet_is_refused_before_anything_is_made(world):
    c, P = world["c"], world["P"]
    with pytest.raises(M.TrackError, match="no blender yet"):
        c.tracks.create(P, "art", ["B1"], actor="victor", slot="slot-4")
    assert c.store.track(P.id, "art") is None
    assert not os.path.exists(os.path.join(world["lanes"], "game", "art"))


def test_with_remote_lanes_off_the_slot_is_studio_only_as_before(world):
    c, P = world["c"], world["P"]
    P.profile.raw["automation"]["remote_lanes"] = False
    from foundry_coordinator import automation
    automation._cache.clear() if hasattr(automation, "_cache") else None
    row = c.tracks.create(P, "local1", ["T1"], actor="victor", slot="slot-4")
    assert not row["data"].get("host") and "studio" in row["needs"]
    assert os.path.isdir(os.path.join(world["root"], ".tracks", "local1"))
