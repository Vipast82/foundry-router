"""Studio auto-reopen (studio.py, 0.145.0): only the TEST window, a polite close
that never loses unsaved work, an idle wait, a rate limit, and a dry run."""

from __future__ import annotations

import json

import pytest

from foundry_client import config, studio as SR

TEST_ST = {"auto_reopen": True, "place_id": 77417134434265, "universe_id": 10772507751,
           "title_match": "[TEST]", "never_place_ids": [138024886323256], "idle_minutes": 2,
           "max_wait_minutes": 10, "max_per_hour": 2, "rojo_auto_sync": True}


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeStudio:
    """Windows as the backend sees them; closing the TEST window ends its process
    unless `asks` names a dialog Studio shows instead."""

    def __init__(self, clock, wins=None, asks="", idle=600.0, reopen_title="Game [TEST] - Roblox Studio"):
        self.clock = clock
        self.wins = list(wins if wins is not None else
                         [{"hwnd": 1, "pid": 10, "title": "Game [TEST] - Roblox Studio"}])
        self.asks = asks
        self.idle = idle
        self.reopen_title = reopen_title
        self.closed, self.launched = [], []

    def windows(self):
        return [dict(w) for w in self.wins]

    def pid_alive(self, pid):
        return any(w["pid"] == pid for w in self.wins)

    def close(self, hwnd):
        self.closed.append(hwnd)
        w = next((x for x in self.wins if x["hwnd"] == hwnd), None)
        if w is None:
            return
        if w.get("dialog"):
            self.wins.remove(w)                     # the question is cancelled
        elif self.asks:
            self.wins.append({"hwnd": 99, "pid": w["pid"], "title": self.asks, "dialog": True})
        else:
            self.wins = [x for x in self.wins if x["pid"] != w["pid"]]

    def idle_seconds(self):
        return self.idle

    def launch(self, uri):
        self.launched.append(uri)
        self.wins.append({"hwnd": 2, "pid": 20, "title": self.reopen_title})


def _job(tmp_path, fake, clock, st=None, dry=False):
    alerts = []
    r = SR.StudioReopen(lambda: dict(st or TEST_ST), str(tmp_path), backend_factory=lambda: fake,
                        alert=lambda k, t: alerts.append(t), clock=clock, sleep=clock.sleep,
                        plugin_settings_dir=str(tmp_path / "Roblox"))
    j = r.start(task="T1", dry_run=dry, wait=True)
    return r, j, alerts


def test_reopen_closes_only_the_test_window_and_opens_the_test_place(tmp_path):
    clock = Clock()
    live = {"hwnd": 5, "pid": 50, "title": "Game [LIVE] - Roblox Studio"}
    fake = FakeStudio(clock, wins=[{"hwnd": 1, "pid": 10, "title": "Game [TEST] - Roblox Studio"},
                                   live])
    _, j, _ = _job(tmp_path, fake, clock)
    assert j["state"] == "done", j
    assert fake.closed == [1] and live in fake.wins
    assert fake.launched == ["roblox-studio:1+launchmode:edit+task:EditPlace+placeId:"
                             "77417134434265+universeId:10772507751"]


def test_a_save_question_is_cancelled_and_studio_stays_open(tmp_path):
    clock = Clock()
    fake = FakeStudio(clock, asks="Save changes to Roblox?")
    _, j, alerts = _job(tmp_path, fake, clock)
    assert j["state"] == "failed" and "Save changes to Roblox?" in j["result"]
    assert fake.launched == [] and any(w["pid"] == 10 for w in fake.wins)
    assert 99 in fake.closed and alerts


def test_waits_for_an_idle_pc_then_goes_ahead_with_an_alert(tmp_path):
    clock = Clock()
    fake = FakeStudio(clock, idle=5.0)                 # someone is typing
    _, j, alerts = _job(tmp_path, fake, clock)
    assert j["state"] == "done"
    assert any("not idle" in s["text"] for s in j["steps"])
    assert any("Reopening the TEST place" in a for a in alerts)


def test_rate_limit_and_off_switch(tmp_path):
    clock = Clock()
    for _ in range(2):
        _, j, _ = _job(tmp_path, FakeStudio(clock), clock)
        assert j["state"] == "done"
    _, j, _ = _job(tmp_path, FakeStudio(clock), clock)
    assert j["state"] == "failed" and "rate limit" in j["result"]
    _, j, _ = _job(tmp_path / "other", FakeStudio(clock), clock, st={**TEST_ST, "auto_reopen": False})
    assert j["state"] == "failed" and "auto_reopen is off" in j["result"]


def test_dry_run_touches_nothing(tmp_path):
    clock = Clock()
    fake = FakeStudio(clock)
    _, j, _ = _job(tmp_path, fake, clock, st={**TEST_ST, "auto_reopen": False}, dry=True)
    assert j["state"] == "done" and "would close" in j["result"]
    assert fake.closed == [] and fake.launched == []


def test_two_matching_windows_refuse(tmp_path):
    clock = Clock()
    fake = FakeStudio(clock, wins=[{"hwnd": 1, "pid": 10, "title": "A [TEST]"},
                                   {"hwnd": 2, "pid": 11, "title": "B [TEST]"}])
    _, j, _ = _job(tmp_path, fake, clock)
    assert j["state"] == "failed" and "more than one" in j["result"] and fake.closed == []


def test_settings_validation():
    assert SR.validate(TEST_ST) == []
    assert any("never_place_ids" in e for e in SR.validate({**TEST_ST,
                                                            "never_place_ids": [77417134434265]}))
    assert any("title_match" in e for e in SR.validate({**TEST_ST, "title_match": ""}))
    assert SR.validate(SR.DEFAULTS) == []                     # off by default is valid
    cfg = config._merge(config.DEFAULTS, {})
    assert cfg["studio"]["auto_reopen"] is False
    bad = config._merge(cfg, {"studio": {"auto_reopen": True}})
    assert any("place_id" in e for e in config.validate(bad))


def _settings_file(tmp_path, uid="95289024", data=None):
    p = tmp_path / "Roblox" / uid / "InstalledPlugins" / "0" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data if data is not None else {
        "Rojo_autoReconnect": False, "Rojo_confirmationBehavior": "Initial", "Rojo_logLevel": "Info",
        "Rojo_priorEndpoints": {"77417134434265": {"projectName": "AnimalUprising"},
                                "138024886323256": {"projectName": "AnimalUprising"}}}),
        encoding="utf-8")
    return p


def test_reopen_sets_rojo_to_reconnect_and_sync_without_asking_while_studio_is_closed(tmp_path):
    """2026-10-30 Victor: Rojo should connect and sync by itself after a Studio reopen."""
    p = _settings_file(tmp_path)
    clock = Clock()
    fake = FakeStudio(clock)
    _, j, _ = _job(tmp_path, fake, clock)
    assert j["state"] == "done", j
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["Rojo_autoReconnect"] is True and d["Rojo_confirmationBehavior"] == "Never"
    assert d["Rojo_logLevel"] == "Info"                                  # other keys kept
    # the LIVE place (never_place_ids) never auto-reconnects and auto-syncs
    assert list(d["Rojo_priorEndpoints"]) == ["77417134434265"]
    steps = [s["text"] for s in j["steps"]]
    i_closed = steps.index("Studio closed")
    i_rojo = next(i for i, s in enumerate(steps) if s.startswith("Rojo auto-reconnect"))
    i_open = next(i for i, s in enumerate(steps) if s.startswith("opened "))
    assert i_closed < i_rojo < i_open                      # written only while Studio is closed
    assert SR.apply_rojo_auto_sync([138024886323256], str(tmp_path / "Roblox")) == []


def test_rojo_auto_sync_off_and_bad_files_are_left_alone(tmp_path):
    p = _settings_file(tmp_path)
    clock = Clock()
    _, j, _ = _job(tmp_path, FakeStudio(clock), clock, st=dict(TEST_ST, rojo_auto_sync=False))
    assert j["state"] == "done"
    assert json.loads(p.read_text(encoding="utf-8"))["Rojo_confirmationBehavior"] == "Initial"
    bad = tmp_path / "Roblox" / "1" / "InstalledPlugins" / "0" / "settings.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("not json", encoding="utf-8")
    assert SR.apply_rojo_auto_sync([], str(tmp_path / "Roblox")) == [str(p)]
    assert bad.read_text(encoding="utf-8") == "not json"
    assert "studio.rojo_auto_sync must be true or false" in SR.validate(dict(TEST_ST, rojo_auto_sync="yes"))
