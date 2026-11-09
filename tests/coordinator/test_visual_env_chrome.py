"""2026-11-09 UI-SQHP1: the visual judge failed HUD frames on Studio's navigation widget and the
Roblox core topbar, and the sprint stopped to ask the owner to change Studio settings."""
import inspect

from foundry_coordinator import visual_approval as VA


def test_the_judge_is_told_capture_chrome_is_not_a_defect():
    t = VA.ENV_CHROME
    for words in ("core topbar", "navigation widget", "thumbstick", "Studio window chrome"):
        assert words in t
    # the game's own UI under the topbar still counts (the topbar is in the live client)
    assert "still a defect" in t


def test_the_review_prompt_carries_it():
    assert "{ENV_CHROME}" in inspect.getsource(VA)
