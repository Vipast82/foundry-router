"""2026-11-03: a never-started sprint queued on one running track moves to another track
(`tracks start` with it), so idle agents and Blender pool jobs can take it; the old track's
next sprint then follows the one before it. A started or current sprint never moves."""

from __future__ import annotations

import pytest

from foundry_coordinator import states as S
from foundry_coordinator.tracks.manager import TrackError
from tests.coordinator.test_tracks_merge import env
from tests.coordinator.helpers import run_until


def test_a_queued_sprint_moves_to_a_new_track_and_the_old_chain_is_relinked(tmp_path):
    c, P, clock, root = env(tmp_path)
    c.tracks.create(P, "a", ["B1", "C1", "T2"])
    c.tracks.start(P, "a", "victor")
    assert c.store.queue_entry("game", "C1")["deps"] == ["B1"]
    assert c.store.queue_entry("game", "T2")["deps"] == ["C1"]
    c.tracks.create(P, "b", ["C1"])
    c.tracks.start(P, "b", "victor")
    assert c.store.track("game", "a")["sprints"] == ["B1", "T2"]
    assert c.store.task_track("game", "C1") == "b"
    assert "B1" not in c.store.queue_entry("game", "C1")["deps"]
    assert c.store.queue_entry("game", "T2")["deps"] == ["B1"]          # relinked
    kinds = [e["kind"] for e in c.store.events("game", 100)]
    assert "moved_off_track" in kinds and "moved_to_track" in kinds
    assert run_until(c, lambda: all(c.store.task("game", s)["state"] == S.COMPLETE
                                    for s in ("B1", "C1", "T2")), limit=120)


def test_the_current_sprint_of_a_running_track_never_moves(tmp_path):
    c, P, clock, root = env(tmp_path)
    c.tracks.create(P, "a", ["B1", "C1"])
    c.tracks.start(P, "a", "victor")
    with pytest.raises(TrackError, match="already"):
        c.tracks.create(P, "b", ["B1"])
