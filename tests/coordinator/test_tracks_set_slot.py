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
