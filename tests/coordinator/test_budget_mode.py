"""The budget mode switch, coordinator half (budget_mode.py; item 16 of
COORDINATOR_AUTOMATION_NEEDS): one setting over the agent caps. The cutoffs are the router's
half: tests/test_budget_caps.py."""

from __future__ import annotations

import json

import pytest

from foundry_coordinator import automation, board, budget_mode
from foundry_coordinator.tracks.resources import CLAUDE_USAGE, claude_capacity, claude_settings
from tests.coordinator.test_budget_policy import game, task
from tests.coordinator.test_modelwatch import PERSONA


def events(c, kind):
    return [json.loads(r["detail"]) for r in c.store.q(
        "SELECT detail FROM events WHERE kind=? ORDER BY id", (kind,))]


def test_normal_changes_nothing_and_the_modes_cap_without_rewriting_a_setting(tmp_path):
    c, P, clk = game(tmp_path)
    P.profile.roles["implementer"] = PERSONA
    res = P.profile.raw.setdefault("resources", {})
    c.max_writers_override = None          # the test engine pins --max-writers; a service does not
    automation.set_live(P, "max_agents", 5)
    assert budget_mode.mode(P) == "normal"
    assert claude_settings(P.profile)["agents"] == 0 and c.tracks.max_agents() == 5
    assert budget_mode.tick(c, P) is None                     # nothing to say about normal
    res["claude_agents"] = 5
    assert claude_settings(P.profile)["agents"] == 5

    automation.set_live(P, "budget_mode", "save_claude")
    assert claude_settings(P.profile)["agents"] == 1 and claude_capacity(P) == 1
    assert c.tracks.max_agents() == 5                         # only Claude is capped
    said = budget_mode.tick(c, P)
    assert said["before"] == "normal" and said["text"] == (
        "budget mode is save Claude: 1 Claude sprint session(s) at once, Claude's cutoff at "
        "most 75%")
    assert budget_mode.tick(c, P) is None                     # said once

    automation.set_live(P, "budget_mode", "save_all")
    assert c.tracks.max_agents() == 1 and claude_settings(P.profile)["agents"] == 1
    assert budget_mode.tick(c, P)["text"].endswith(
        "; 1 sprint(s) at once, every other paid model's cutoff at most 75%")
    # the presets are settings of their own
    automation.set_live(P, "save_all_agents", 3)
    automation.set_live(P, "save_claude_agents", 2)
    automation.set_live(P, "save_all_cutoff_pct", 0)
    assert c.tracks.max_agents() == 3 and claude_settings(P.profile)["agents"] == 2
    assert budget_mode.words(budget_mode.describe(P)).endswith("cutoff unchanged")
    automation.set_live(P, "save_all_agents", 9)              # never more than max_agents
    assert c.tracks.max_agents() == 5
    # a cap set from the CLI (stored) is capped the same way
    automation.set_live(P, "save_all_agents", 2)
    c.store.set_meta(f"max_agents:{P.id}", "4")
    assert c.tracks.max_agents() == 2

    # back to normal: every value is the project's own again, nothing to restore
    automation.set_live(P, "budget_mode", "normal")
    assert c.tracks.max_agents() == 4 and claude_settings(P.profile)["agents"] == 5
    assert res["claude_agents"] == 5 and automation.get(P, "max_agents") == 5
    assert budget_mode.tick(c, P)["text"].startswith("budget mode is normal")
    assert [e["mode"] for e in events(c, "budget_mode_changed")] == [
        "save_claude", "save_all", "normal"]


def test_a_saving_mode_counts_claude_sessions_without_resources_claude_agents(tmp_path):
    """resources.claude_agents = 0 means "no cap" and the engine then counts no session as a
    Claude one; in a saving mode the cap is the mode's and the sessions are counted."""
    c, P, clk = game(tmp_path)
    P.profile.roles["implementer"] = PERSONA
    t = task(c)
    assert not c._claude_session(P, t)
    automation.set_live(P, "budget_mode", "save_claude")
    assert c._claude_session(P, t) and claude_capacity(P) == 1
    # the existing drop at resources.claude_usage_pct never raises the mode's cap
    CLAUDE_USAGE[P.id] = {"pct": 80.0, "ts": clk.t, "detail": ""}
    try:
        assert claude_capacity(P) == 1
    finally:
        CLAUDE_USAGE.pop(P.id, None)


def test_the_board_shows_a_saving_mode_and_the_settings_are_in_the_schema(tmp_path):
    from foundry_automation import schema
    c, P, clk = game(tmp_path)
    b = board.build(c, P)
    assert b["budget"]["mode"] == "normal" and "Budget:" not in board.text(b)
    automation.set_live(P, "budget_mode", "save_all")
    b = board.build(c, P)
    assert b["budget"]["label"] == "save all"
    assert "\nBudget: budget mode is save all: 1 Claude sprint session(s) at once" in board.text(b)
    f = schema.find_field("automation.budget_mode")
    assert f is not None and list(f.enum) == ["normal", "save_claude", "save_all"]
    for k, d in (("save_claude_agents", 1), ("save_claude_cutoff_pct", 75),
                 ("save_all_agents", 1), ("save_all_cutoff_pct", 75), ("budget_mode", "normal")):
        assert schema.AUTOMATION_DEFAULTS[k] == d and schema._AUTO_DESC[k]
        assert schema.find_field(f"automation.{k}") is not None
    with pytest.raises(schema.SettingsError):
        schema.automation_table("x", {"budget_mode": "frugal"})


def test_a_lower_route_the_mode_asked_for_is_logged_not_alerted(tmp_path):
    """In a saving mode Claude is held to a lower cutoff on purpose, so a Sonnet sprint served
    by the next route is what was asked for: it is written down, and nobody is paged."""
    from foundry_coordinator import modelwatch
    from tests.coordinator.test_modelwatch import ROUTES, Router, admission, working
    router = Router(served=[ROUTES[1]], ans=admission(claude=(76.0, 1500), claude_cut=75.0))
    c, P, clk = working(tmp_path, router)
    automation.set_live(P, "budget_mode", "save_claude")
    [a] = modelwatch.sweep(c, P)
    assert a["kind"] == "lower_model" and "alert_class" not in a and a["digest"] is True
    assert a["text"].endswith('This is what budget mode "save Claude" asks for.')
    # the same fall with Claude under its cutoff is not the mode's doing: an alert
    router2 = Router(served=[ROUTES[1]], ans=admission(claude=(20.0, 1500), claude_cut=75.0))
    c2, P2, clk2 = working(tmp_path / "b", router2)
    automation.set_live(P2, "budget_mode", "save_claude")
    [b] = modelwatch.sweep(c2, P2)
    assert b["alert_class"] == "watch"
