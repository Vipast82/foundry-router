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


# ------------------------------------------------------------------ v0.181.229: one reopen at
# a time, and a Studio update (the 2026-11-17 update, 0.747 -> 0.748)

OLD, NEW = "0.747.0.7471481", "0.748.0.7481526"


class UpdatingStudio(FakeStudio):
    """A Studio whose installer runs until `update_until`, with the versions its logs name."""

    def __init__(self, clock, update_until=0.0, versions=(), opens_after=0.0, **kw):
        super().__init__(clock, **kw)
        self.update_until, self.versions, self.opens_after = update_until, list(versions), opens_after
        self.launch_at = None

    def updating(self):
        return "RobloxStudioInstaller.exe pid 4242" if self.clock() < self.update_until else ""

    def log_versions(self):
        return sorted(self.versions, reverse=True)

    def launch(self, uri):
        self.launched.append(uri)
        self.launch_at = self.clock()

    def windows(self):
        if self.launch_at is not None and self.clock() - self.launch_at >= self.opens_after \
                and not any(w["hwnd"] == 2 for w in self.wins):
            self.wins.append({"hwnd": 2, "pid": 20, "title": self.reopen_title})
            self.versions.append((self.clock(), NEW))
        return super().windows()


def _reopen(tmp_path, fake, clock, st=None):
    alerts = []
    r = SR.StudioReopen(lambda: dict(st or TEST_ST), str(tmp_path), backend_factory=lambda: fake,
                        alert=lambda k, t: alerts.append(t), clock=clock, sleep=clock.sleep,
                        plugin_settings_dir=str(tmp_path / "Roblox"))
    return r, alerts


def test_replay_a_second_request_joins_the_reopen_under_way(tmp_path):
    """11-17: the job for ORI-2 (21:47) and the one for FIN-5a1 (21:52) both sat in the idle
    wait; the first opened the TEST place at 21:58, the second closed it again at 22:02. Two
    reopens in five minutes used up max_per_hour and Studio stayed closed for an hour."""
    clock = Clock()
    fake = FakeStudio(clock, idle=5.0)                 # someone is typing: the job waits
    r, _ = _reopen(tmp_path, fake, clock)
    joined = []
    real_sleep = clock.sleep

    def sleep(s):                                      # FIN-5a1 asks while the first job waits
        real_sleep(s)
        if not joined:
            joined.append(r.start(task="FIN-5a1"))
    r.sleep = sleep
    j1 = r.start(task="ORI-2", wait=True)
    assert j1["state"] == "done" and len(fake.launched) == 1 and fake.closed == [1]
    assert joined[0]["job"] == j1["job"] and joined[0]["state"] == "running"
    assert any("also asked for by FIN-5a1" in s["text"] for s in j1["steps"])
    assert j1["joined"] == ["FIN-5a1"]
    # POL-7 asks two minutes after it opened: the same reopen answers, nothing is closed
    clock.t += 120
    j3 = r.start(task="POL-7", wait=True)
    assert j3["job"] == j1["job"] and j3["state"] == "done"
    assert len(fake.launched) == 1 and fake.closed == [1]
    assert len(json.loads((tmp_path / "studio-reopen.json").read_text())) == 1   # one counted
    # later it is a new reopen
    clock.t += SR.REUSE_S
    j4 = r.start(task="LOB-1b", wait=True)
    assert j4["job"] != j1["job"] and j4["state"] == "done" and len(fake.launched) == 2
    # a dry run never joins a real reopen
    assert r.start(task="x", dry_run=True, wait=True)["job"] not in (j1["job"], j4["job"])


def test_a_failed_reopen_is_not_handed_to_the_next_request(tmp_path):
    clock = Clock()
    fake = FakeStudio(clock, asks="Save changes to Roblox?")
    r, _ = _reopen(tmp_path, fake, clock)
    j1 = r.start(task="ORI-2", wait=True)
    assert j1["state"] == "failed"
    fake.asks = ""
    fake.wins = [w for w in fake.wins if not w.get("dialog")]
    j2 = r.start(task="FIN-5a1", wait=True)
    assert j2["job"] != j1["job"] and j2["state"] == "done"


def test_a_studio_update_in_progress_is_waited_for(tmp_path):
    clock = Clock()
    fake = UpdatingStudio(clock, update_until=clock.t + 240, wins=[])
    r, _ = _reopen(tmp_path, fake, clock)
    j = r.start(task="ORI-2", wait=True)
    assert j["state"] == "done", j
    texts = [s["text"] for s in j["steps"]]
    assert texts[0] == ("Studio is updating (RobloxStudioInstaller.exe pid 4242): waiting for "
                        "it to finish")
    assert texts[1].startswith("the update finished after 24")
    assert fake.launch_at >= 1_000_000.0 + 240          # nothing opened while it installed


def test_an_update_that_does_not_finish_fails_without_touching_or_counting(tmp_path):
    clock = Clock()
    fake = UpdatingStudio(clock, update_until=clock.t + 10 ** 6)
    r, alerts = _reopen(tmp_path, fake, clock)
    j = r.start(task="ORI-2", wait=True)
    assert j["state"] == "failed" and "still updating after 15 min" in j["result"]
    assert fake.closed == [] and fake.launched == [] and alerts
    assert not (tmp_path / "studio-reopen.json").exists()
    # a dry run only reports it
    d = r.start(task="x", dry_run=True, wait=True)
    assert d["state"] == "done" and "a real reopen would wait" in d["steps"][0]["text"]


def test_an_update_starts_the_hourly_count_again(tmp_path):
    """Two reopens were made at 21:58 and 22:02; the new version first ran at 21:57:45, so
    the one that launched the update (recorded before it) does not count and a third request
    inside the hour is allowed. With one version in the logs the limit holds as before."""
    clock = Clock()
    now = clock.t
    (tmp_path / "studio-reopen.json").write_text(json.dumps([now - 1500, now - 900]))
    logs = [(now - 6000, OLD), (now - 4000, OLD), (now - 1200, NEW), (now - 800, NEW)]
    fake = UpdatingStudio(clock, versions=logs)
    r, _ = _reopen(tmp_path, fake, clock)
    j = r.start(task="POL-7", wait=True)
    assert j["state"] == "done", j
    assert j["result"] == f"TEST place reopened (Studio {NEW})"
    step = next(s["text"] for s in j["steps"] if "was updated" in s["text"])
    assert f"{OLD} -> {NEW}" in step and "20 min ago" in step and "do not count" in step
    # the same history, no update in the logs: refused
    clock2 = Clock()
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "studio-reopen.json").write_text(json.dumps([now - 1500, now - 900]))
    r2, _ = _reopen(tmp_path / "b", UpdatingStudio(clock2, versions=[(now - 4000, NEW)]), clock2)
    j2 = r2.start(task="POL-7", wait=True)
    assert j2["state"] == "failed" and "rate limit: 2 reopen(s)" in j2["result"]
    # an update more than an hour old changes nothing either
    clock3 = Clock()
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "studio-reopen.json").write_text(json.dumps([now - 1500, now - 900]))
    old_update = [(now - 9000, OLD), (now - 4000, NEW)]
    r3, _ = _reopen(tmp_path / "c", UpdatingStudio(clock3, versions=old_update), clock3)
    assert "rate limit" in r3.start(task="x", wait=True)["result"]


def test_a_launch_that_starts_an_update_is_given_time_and_names_both_versions(tmp_path):
    clock = Clock()
    fake = UpdatingStudio(clock, wins=[], versions=[(clock.t - 5000, OLD)], opens_after=400)
    fake.update_until = clock.t + 390                  # the launcher installs for 6.5 min
    fake.updating = lambda: ("RobloxStudioInstaller.exe pid 9"
                             if fake.launch_at is not None and clock() < fake.update_until else "")
    r, _ = _reopen(tmp_path, fake, clock)
    j = r.start(task="ORI-2", wait=True)
    assert j["state"] == "done", j                     # 400 s > OPEN_WAIT_S, but it installed
    assert j["result"] == f"TEST place reopened (Studio updated {OLD} -> {NEW})"
    assert any("the launch started a Studio update" in s["text"] for s in j["steps"])
    # a window that never comes still fails
    clock2 = Clock()
    fake2 = UpdatingStudio(clock2, wins=[], opens_after=10 ** 6)
    r2, _ = _reopen(tmp_path / "n", fake2, clock2)
    assert "did not appear within 240 s" in r2.start(task="x", wait=True)["result"]


def test_log_versions_reads_version_and_start_time_from_the_log_names(tmp_path):
    for name in ("0.748.0.7481526_20261118T035745Z_Studio_9151C_last.log",
                 "0.748.0.7481526_20261118T040226Z_Studio_12CC9_last.log",
                 "0.747.0.7471481_20261117T170211Z_Studio_AA001_last.log",
                 "RobloxStudioInstaller_4D8BA.log", "cacert.pem",
                 "0.748.0.7481526_2026111T04Z_Studio_bad_last.log"):
        (tmp_path / name).write_text("x")
    got = SR.log_versions(str(tmp_path))
    assert [v for _, v in got] == [NEW, NEW, OLD]
    assert got[0][0] - got[1][0] == 4 * 60 + 41          # 04:02:26 - 03:57:45, UTC
    assert SR.log_versions(str(tmp_path / "missing")) == []
