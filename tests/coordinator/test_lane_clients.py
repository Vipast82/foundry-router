"""Remote sessions (Distributed Foundry phase b B4): a lane slot's kilo / opencode become
ClientSet members `<slot>/kilo` and `<slot>/opencode`; a session with `lane_host` starts there;
local projects are unchanged."""

from __future__ import annotations

import ast
import pathlib
import types

import pytest

from foundry_coordinator import lane_tunnel
from foundry_coordinator import slots as SL
from foundry_coordinator.adapters import build_client, lane_members
from foundry_coordinator.adapters.clientset import ClientSet
from foundry_coordinator.adapters.laneclient import (LaneDown, TunnelledClient, lane_host_meta,
                                                     serves)
from foundry_coordinator.adapters.oc1 import AgentServerError

ROOT = pathlib.Path(__file__).resolve().parents[2]


class FakeManager:
    def __init__(self):
        self.eps = {}
        self.configured = []

    def configure(self, slots, **kw):
        self.configured.append(dict(slots))

    def endpoint(self, sid, kind="agent"):
        return self.eps.get((sid, kind))


@pytest.fixture
def fm(monkeypatch):
    m = FakeManager()
    monkeypatch.setattr(lane_tunnel, "MANAGER", m)
    monkeypatch.setattr(lane_tunnel, "_BY_PROJECT", {})
    return m


def profile(slots=None, client=None, clients=None):
    raw = {"slots": slots or {}}
    if clients:
        raw["clients"] = clients
    p = types.SimpleNamespace(id="pilot", raw=raw, workspace="C:/AU",
                              client=client or {"kind": "kilo", "url": "http://127.0.0.1:4096",
                                                "agent": "code"})
    p.secret = lambda table, name: ""
    return p


LANE = {"slot-4": {"container": "studio-slot", "host": "studio@10.0.0.9", "lanes": True}}


def test_no_lane_slot_keeps_the_single_client(fm):
    c = build_client(profile({"slot-1": {"container": "x"}}))
    assert not isinstance(c, ClientSet) and c.name == "kilo"
    assert fm.configured == []


def test_a_lane_slot_adds_its_two_members(fm):
    c = build_client(profile(LANE))
    assert isinstance(c, ClientSet)
    assert set(c.members) == {"default", "slot-4/kilo", "slot-4/opencode"}
    k, o = c.members["slot-4/kilo"], c.members["slot-4/opencode"]
    import os
    mirror = os.path.join(os.path.realpath("C:/AU"), ".lanes", "slot-4").replace("\\", "/")
    # phase c: the member serves the lanes' read mirrors here; the slot sees its own folder
    assert k.lane_host == o.lane_host == "slot-4" and k.lane_root == mirror
    assert k.table["slot_root"] == "/srv/foundry-lanes/pilot"
    assert k.client.dir_map == (mirror, "/srv/foundry-lanes/pilot")
    assert k.agent == "code" and o.agent == "build"
    assert fm.configured and "slot-4" in fm.configured[-1]


def test_the_opencode_member_takes_the_projects_opencode_table(fm):
    m = lane_members(profile(LANE, clients={"oc": {"kind": "opencode", "agent": "plan",
                                                   "worker_client": "opencode"}}))
    t = m["slot-4/opencode"][1]
    assert t["agent"] == "plan"
    # v0.181.266: the slot's server calls the router with the slot's own lane key
    assert t["worker_client"] == "slot-4-opencode" and m["slot-4/kilo"][1]["worker_client"] == "slot-4-kilo"


def test_tunnelled_client_follows_the_endpoint(fm):
    tc = TunnelledClient("slot-4", "kilo", fm.endpoint)
    assert tc.name == "kilo"
    with pytest.raises(LaneDown, match="tunnel is down"):
        tc.health()
    assert issubclass(LaneDown, AgentServerError)
    fm.eps[("slot-4", "kilo")] = {"url": "http://127.0.0.1:50002", "password": "KP"}
    a = tc._client()
    assert a.url == "http://127.0.0.1:50002" and tc._client() is a
    fm.eps[("slot-4", "kilo")] = {"url": "http://127.0.0.1:50009", "password": "KP"}
    assert tc._client() is not a and tc._client().url == "http://127.0.0.1:50009"
    oc = TunnelledClient("slot-4", "opencode", fm.endpoint)
    fm.eps[("slot-4", "opencode")] = {"url": "http://127.0.0.1:50003", "password": "OP"}
    assert oc._client().name == "opencode" and oc.name == "opencode"
    with pytest.raises(ValueError):
        TunnelledClient("slot-4", "cline", fm.endpoint)


class Rec:
    def __init__(self, name):
        self.name = name
        self.started, self.disposed, self.exists_calls = [], [], []

    def start_session(self, **kw):
        self.started.append(kw)
        return f"{self.name}-sid"

    def dispose(self, d):
        self.disposed.append(d)
        return True

    def preflight(self, d):
        return [("ok", "x", d)]

    def exists(self, sid, directory=""):
        self.exists_calls.append(directory)
        return sid.startswith(self.name)


def clientset():
    local, sk, so = Rec("kilo"), Rec("kilo"), Rec("opencode")
    cs = ClientSet(local, {"slot-4/kilo": (sk, {"lane_host": "slot-4",
                                                "lane_root": "/srv/foundry-lanes"}),
                           "slot-4/opencode": (so, {"lane_host": "slot-4", "agent": "build",
                                                    "lane_root": "/srv/foundry-lanes"})},
                   default_table={"agent": "code"})
    return cs, local, sk, so


def kw(**meta):
    return dict(directory="/srv/foundry-lanes/pilot/l1", title="t", persona="p", agent="code",
                metadata=meta, permissions=[])


def test_a_lane_session_starts_on_its_slot_of_the_same_kind():
    cs, local, sk, so = clientset()
    cs.start_session(**kw(role="implementer", lane_host="slot-4"))
    assert sk.started and not local.started and not so.started
    cs.roles = {"helper": "slot-4/opencode"}
    cs.start_session(**kw(role="helper", lane_host="slot-4"))
    assert so.started and so.started[0]["agent"] == "build"


def test_a_lane_host_without_a_member_never_falls_back_to_a_local_server():
    cs, local, *_ = clientset()
    with pytest.raises(AgentServerError, match="lanes is off"):
        cs.start_session(**kw(role="implementer", lane_host="slot-9"))
    assert not local.started


def test_a_local_session_is_unchanged():
    cs, local, sk, so = clientset()
    cs.start_session(**{**kw(role="implementer"), "directory": "C:/Roblox/AU"})
    assert local.started and not sk.started


def test_folder_calls_reach_only_the_members_that_serve_the_folder():
    cs, local, sk, so = clientset()
    cs.dispose("C:/Roblox/AU")
    assert local.disposed == ["C:/Roblox/AU"] and sk.disposed == [] and so.disposed == []
    cs.dispose("/srv/foundry-lanes/pilot/l1")
    assert sk.disposed == so.disposed == ["/srv/foundry-lanes/pilot/l1"]
    assert all(not r[1].startswith("slot-4") for r in cs.preflight("C:/Roblox/AU"))
    cs.owner("opencode-sid", "C:/Roblox/AU")                   # a local folder: no slot asked
    assert sk.exists_calls == [] and so.exists_calls == []
    assert cs.owner("opencode-x", "/srv/foundry-lanes/pilot/l1").client is so


def test_folders_under_the_mirror_reach_the_slot_as_its_own(fm):
    tc = TunnelledClient("slot-4", "kilo", fm.endpoint,
                         dir_map=("C:/AU/.lanes/slot-4", "/srv/foundry-lanes/pilot"))
    assert tc.slot_dir("C:\\AU\\.lanes\\slot-4\\l1") == "/srv/foundry-lanes/pilot/l1"
    assert tc.slot_dir("c:/au/.lanes/slot-4/l1/x") == "/srv/foundry-lanes/pilot/l1/x"
    assert tc.slot_dir("C:/AU") == "C:/AU" and tc.slot_dir(3) == 3
    seen = {}

    class Inner:
        def start_session(self, **kw):
            seen.update(kw)
            return "sid"

        def dispose(self, directory):
            seen["dispose"] = directory
            return True
    fm.eps[("slot-4", "kilo")] = {"url": "http://127.0.0.1:9", "password": ""}
    tc._inner, tc._key = Inner(), ("http://127.0.0.1:9", "")
    tc.start_session(directory="C:/AU/.lanes/slot-4/l1", title="t", persona="p", agent="code",
                     metadata={}, permissions=[])
    tc.dispose("C:/AU/.lanes/slot-4/l1")
    assert seen["directory"] == "/srv/foundry-lanes/pilot/l1"
    assert seen["dispose"] == "/srv/foundry-lanes/pilot/l1"


def test_serves():
    assert serves("", "anything") and serves(None, "x")
    assert serves("/srv/foundry-lanes", "/srv/foundry-lanes/p/l")
    assert not serves("/srv/foundry-lanes", "/srv/foundry-lanes-other/p")
    assert not serves("/srv/foundry-lanes", "C:\\Roblox\\AU")


def test_lane_host_meta():
    P = types.SimpleNamespace(profile=types.SimpleNamespace(lane_host="slot-4"))
    assert lane_host_meta(P) == {"lane_host": "slot-4"}
    assert lane_host_meta(types.SimpleNamespace(profile=types.SimpleNamespace())) == {}


def test_a_track_row_with_a_host_gets_a_remote_workspace(fm):
    from foundry_coordinator.adapters.git import Workspace
    from foundry_coordinator.adapters.remote_git import RemoteWorkspace
    from foundry_coordinator.tracks.manager import TrackManager
    P = types.SimpleNamespace(id="pilot")
    row = {"name": "l1", "worktree": "C:/AU/.lanes/slot-4/l1",
           "data": {"host": "slot-4", "remote_worktree": "/srv/foundry-lanes/pilot/l1"}}
    tm = TrackManager.__new__(TrackManager)
    ws = tm._workspace(P, row)
    assert isinstance(ws, RemoteWorkspace) and ws.lane == "l1" and ws.project == "pilot"
    assert ws.path == "/srv/foundry-lanes/pilot/l1" and ws.mirror == "C:/AU/.lanes/slot-4/l1"
    fm.eps[("slot-4", "agent")] = {"url": "http://127.0.0.1:1", "token": "T"}
    assert ws.agent.endpoint() == {"url": "http://127.0.0.1:1", "token": "T"}
    local = tm._workspace(P, {"name": "l2", "worktree": "C:/x", "data": {}})
    assert type(local) is Workspace
    ex = TrackManager._track_extras(types.SimpleNamespace(workspace="C:/AU"), row)
    assert ex["lane_host"] == "slot-4" and ex["remote_workspace"] == "/srv/foundry-lanes/pilot/l1"
    assert TrackManager._track_extras(types.SimpleNamespace(workspace="C:/AU"),
                                      {"name": "x", "data": {}})["lane_host"] == ""


def test_slot_settings_and_the_schema_agree():
    from foundry_automation.schema import SLOT_FIELDS
    fields = {k: v for k, _t, v, *_ in SLOT_FIELDS}
    for k in ("lanes", "lanes_root", "lanes_secrets", "agent_port", "kilo_port", "opencode_port"):
        assert fields[k] == SL.DEFAULTS[k], k
    assert SL.DEFAULTS["lanes"] is False


def test_every_session_start_says_where_its_lane_is():
    """Fix every path: each start_session the coordinator makes passes lane_host_meta, so a
    lane on a slot never gets a session on this machine's server."""
    missing = []
    for p in sorted((ROOT / "foundry_coordinator").rglob("*.py")):
        if "adapters" in p.parts:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "start_session":
                meta = next((k.value for k in node.keywords if k.arg == "metadata"), None)
                if meta is None or "lane_host_meta" not in ast.unparse(meta):
                    missing.append(f"{p.relative_to(ROOT).as_posix()}:{node.lineno}")
    assert not missing, "start_session without lane_host_meta: " + ", ".join(missing)
