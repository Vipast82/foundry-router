"""tracks set-slot (2026-11-01, Victor: "split them"): all 8 Blender tracks were bound to slot-1
while slot-2 sat idle. A running track moves to another slot when none of its sprints is
using a slot; its kilo.jsonc follows and Kilo reloads that folder before its next session."""
import pytest

from foundry_coordinator import slots as SL
from foundry_coordinator.tracks.manager import TrackError
from tests.coordinator.test_tracks_manager import env


def _two_slots(P):
    P.profile.raw.setdefault("slots", {}).update({
        "slot-1": {"container": "studio-slot", "host": "studio@10.0.0.5"},
        "slot-2": {"container": "studio-slot", "host": "studio@10.0.0.6"}})


def test_a_track_moves_to_another_slot_and_flags_a_kilo_reload(tmp_path):
    c, P, root = env(tmp_path)
    _two_slots(P)
    row = c.tracks.create(P, "blender", ["B1", "B2"], actor="victor", slot="slot-1")
    assert row["data"]["slot"] == "slot-1"
    moved = c.tracks.set_slot(P, "blender", "slot-2", actor="operator")
    assert moved["data"]["slot"] == "slot-2" and moved["data"]["kilo_reload"] is True
    assert "studio" in moved["needs"]
    ev = [e for e in c.store.events("game", 100) if e["kind"] == "track_slot_changed"]
    d = ev[0]["detail"]
    assert d["to"] == "slot-2" and d["from"] == "slot-1"
    assert set(d["servers"]) == {"Roblox_Studio", "robloxstudio-mcp"}  # pointed at slot-2
    assert c.tracks.set_slot(P, "blender", "slot-2")["data"]["slot"] == "slot-2"   # no-op


def test_a_track_never_moves_while_a_sprint_holds_its_slot_or_an_unknown_slot(tmp_path):
    c, P, root = env(tmp_path)
    _two_slots(P)
    c.tracks.create(P, "blender", ["B1", "B2"], actor="victor", slot="slot-1")
    assert c.store.acquire([SL.LOCK_PREFIX + "slot-1"], "game", "B1") is None
    with pytest.raises(TrackError, match="holds slot:slot-1"):
        c.tracks.set_slot(P, "blender", "slot-2")
    c.store.release("game", "B1")
    with pytest.raises(TrackError, match="no enabled Studio slot"):
        c.tracks.set_slot(P, "blender", "slot-9")
    assert c.store.track("game", "blender")["data"]["slot"] == "slot-1"


def test_the_next_track_session_reloads_its_kilo_folder_once(tmp_path):
    c, P, root = env(tmp_path)
    _two_slots(P)
    row = c.tracks.create(P, "blender", ["B1"], actor="victor", slot="slot-1")
    c.tracks.set_slot(P, "blender", "slot-2")
    TP = c.tracks.track_project(P, c.store.track("game", "blender"))
    calls = []
    TP.client.dispose = lambda directory: calls.append(directory) or True
    c._kilo_reload_if_flagged(TP, "B1")
    assert calls == [TP.profile.workspace]
    assert c.store.track("game", "blender")["data"]["kilo_reload"] is False
    c._kilo_reload_if_flagged(TP, "B1")
    assert len(calls) == 1                                       # once per change


def test_a_failed_reload_keeps_the_flag_and_never_blocks(tmp_path):
    c, P, root = env(tmp_path)
    _two_slots(P)
    c.tracks.create(P, "blender", ["B1"], actor="victor", slot="slot-1")
    c.tracks.set_needs(P, "blender", ["blender", "studio"])
    TP = c.tracks.track_project(P, c.store.track("game", "blender"))

    def boom(directory):
        raise RuntimeError("HTTP 500")
    TP.client.dispose = boom
    c._kilo_reload_if_flagged(TP, "B1")                          # no exception
    assert c.store.track("game", "blender")["data"]["kilo_reload"] is True


def test_a_track_unbinds_from_its_slot_and_its_studio_work_moves_to_the_merge(tmp_path):
    """2026-11-02 Victor: "The slots are very slow systems as far as gaming goes, why is that the
    test?" AS-V08's frame-timing check failed on slot-1 (~36 fps under Wine). `set-slot none`
    unbinds a track: its sessions get no Studio tools and its Studio checks run on the
    workstation in the merge's Studio batch."""
    from foundry_coordinator.tracks.manager import TrackManager
    c, P, root = env(tmp_path)
    _two_slots(P)
    c.tracks.create(P, "blender", ["B1", "B2"], actor="victor", slot="slot-1")
    TP = c.tracks.track_project(P, c.store.track("game", "blender"))
    assert not c.tracks.studio_less(TP)
    row = c.tracks.set_slot(P, "blender", "none", actor="victor")
    assert "slot" not in row["data"] and "studio" not in row["needs"]
    assert row["data"]["kilo_reload"] is True
    TP2 = c.tracks.track_project(P, c.store.track("game", "blender"))
    assert c.tracks.studio_less(TP2)
    assert c.tracks.studio_less(TP)             # a stale cached profile reads the store too
    ev = [e for e in c.store.events("game", 100) if e["kind"] == "track_slot_changed"]
    assert ev[0]["detail"]["from"] == "slot-1" and ev[0]["detail"]["to"] == ""
    assert c.tracks.set_slot(P, "blender", "none")["data"].get("slot") is None   # no-op
    # unbinding waits like a move: never while a sprint holds the slot
    c.tracks.set_slot(P, "blender", "slot-2")
    assert c.store.acquire([SL.LOCK_PREFIX + "slot-2"], "game", "B1") is None
    with pytest.raises(TrackError, match="holds slot:slot-2"):
        c.tracks.set_slot(P, "blender", "none")


def test_workstation_only_sprints_never_go_on_a_slot(tmp_path):
    """[automation] workstation_only_sprints: sprints whose Studio checks measure frames or
    performance (VFX timing, S56) are refused on a slot-bound track."""
    c, P, root = env(tmp_path)
    _two_slots(P)
    P.profile.raw.setdefault("automation", {})["workstation_only_sprints"] = ["B*"]
    from foundry_coordinator import automation
    automation._CACHE.clear()
    with pytest.raises(TrackError, match="workstation"):
        c.tracks.create(P, "blender", ["B1"], actor="victor", slot="slot-1")
    c.tracks.create(P, "blender", ["B1"], actor="victor")            # no slot: fine
    with pytest.raises(TrackError, match="workstation"):
        c.tracks.set_slot(P, "blender", "slot-1")
    c.tracks.create(P, "audio", ["A1"], actor="victor", slot="slot-1")   # not listed: fine
