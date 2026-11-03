"""2026-11-03: router revision 20 raised [automation] context_ceiling_pct 63 -> 85. config_pull
updated the main profile in place, but track test2's Project held a copy of the profile taken
when the track started: HK-TEST2 kept handing off at the old ceiling (144.5k). After a config
apply each cached track profile takes the main profile's values again, keeping its own
workspace, branch, locks and slot."""

from __future__ import annotations

import dataclasses

from foundry_coordinator.automation import get as auto_get
from tests.coordinator.test_tracks_manager import env as tracks_env


def test_a_config_apply_reaches_running_track_profiles(tmp_path):
    c, P, root = tracks_env(tmp_path, extra="\n[automation]\nmax_agents = 2\n"
                                            "context_ceiling_pct = 63\n")
    row = c.tracks.create(P, "test2", ["B1"], actor="victor")
    TP = c.tracks.track_project(P, row)
    assert auto_get(TP, "context_ceiling_pct") == 63
    # what config_pull does: a new profile object's fields copied into the live one
    raw = dict(P.profile.raw)
    raw["automation"] = {**(raw.get("automation") or {}), "context_ceiling_pct": 85}
    new = dataclasses.replace(P.profile, raw=raw)
    P.profile.__dict__.update(new.__dict__)
    assert auto_get(P, "context_ceiling_pct") == 85
    c.tracks.after_config_apply(P, None, {"settings": {}})
    assert c.tracks.track_project(P, row) is TP
    assert auto_get(TP, "context_ceiling_pct") == 85
    assert TP.profile.workspace == row["worktree"] and TP.profile.__dict__["track"] == "test2"
    assert TP.profile.publish == {"mode": "none"}
    assert P.profile.workspace == str(root) or P.profile.workspace != TP.profile.workspace
