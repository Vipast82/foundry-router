"""The OpenCode server's memory watchdog (v0.181.256): the same rule as Kilo's (its whole
process tree over opencode_restart_gb with free RAM under opencode_restart_free_gb on two
checks in a row asks for a safe-point restart, once per half hour), with its own settings,
alert and event text; Kilo's rule is unchanged beside it."""

from pathlib import Path
from types import SimpleNamespace

from foundry_client import resources as R
from tests.client.test_resources import FakeProc, FakePs, Now


def _watch(oc_gb=7.5, free=3.0, settings=None, oc_pid=88, kilo_pid=None):
    child = FakeProc(89, "bun.exe", "bun worker", gb=1.0, ppid=88)
    oc = FakeProc(88, "bun.exe", "bun opencode serve --port 4097", gb=oc_gb, children=[child])
    ps = FakePs([oc, child], free_gb=free)
    st = {"alerts": {}, "events": [], "asked": [], "kilo_asked": [], "pending": False,
          "now": Now()}

    def restart(why):
        st["asked"].append(why)
        st["pending"] = True
        return "waiting for a safe point: 1 session(s) working"
    w = R.ResourceWatch(lambda: settings or {},
                        alert=lambda k, t, level="warn": st["alerts"].__setitem__(k, (t, level)),
                        clear_alert=lambda k: st["alerts"].pop(k, None),
                        event=lambda c, t: st["events"].append(t), clock=st["now"], ps=ps,
                        kilo_pid=lambda: kilo_pid, restart_kilo=st["kilo_asked"].append,
                        opencode_pid=lambda: oc_pid, opencode_pending=lambda: st["pending"],
                        restart_opencode=restart)
    return w, st


def test_opencode_over_its_limit_with_ram_low_gets_a_safe_point_restart():
    w, st = _watch()
    last = w.tick(w.cfg())
    assert last["opencode_gb"] == 8.5 and last["opencode"]["limit_gb"] == 6
    assert st["asked"] == []                                 # one check is not enough
    last = w.tick(w.cfg())
    assert st["asked"] == ["OpenCode is using 8.5 GB (limit 6 GB) with 3.0 GB of RAM free "
                           "(under 4 GB)"]
    text, level = st["alerts"]["resources:opencode_memory"]
    assert level == "info" and "OpenCode restarts at the next safe point" in text
    assert "asked for an OpenCode restart at the next safe point" in st["events"][-1]
    assert last["opencode"]["pending"] is True and st["kilo_asked"] == []
    assert last["kilo_gb"] is None                           # no Kilo here: its rule is idle
    for _ in range(5):
        st["now"].t += 60
        w.tick(w.cfg())
    assert len(st["asked"]) == 1
    st["pending"] = False
    w.ps.procs[88]._gb, w.ps.procs[89]._gb = 0.3, 0.1
    w.tick(w.cfg())
    assert "resources:opencode_memory" not in st["alerts"]


def test_opencode_rule_switched_off_ram_fine_or_no_server():
    for kw in ({"settings": {"opencode_restart_gb": 0}}, {"free": 12.0}, {"oc_pid": None}):
        w, st = _watch(**kw)
        for _ in range(4):
            st["now"].t += 60
            w.tick(w.cfg())
        assert st["asked"] == [] and "resources:opencode_memory" not in st["alerts"], kw


def test_opencode_settings_defaulted_validated_and_on_the_page():
    from foundry_client import config
    assert config.DEFAULTS["resources"]["opencode_restart_gb"] == 6
    assert config.DEFAULTS["resources"]["opencode_restart_free_gb"] == 4
    assert R.validate({**R.DEFAULTS}) == []
    assert "resources.opencode_restart_gb must be a number, 0 or more" in R.validate(
        {**R.DEFAULTS, "opencode_restart_gb": -1})
    page = (Path(R.__file__).parent / "static" / "client.html").read_text(encoding="utf-8")
    assert '"resources.opencode_restart_gb"' in page
    assert '"resources.opencode_restart_free_gb"' in page


def test_the_manager_asks_for_opencodes_own_safe_restart():
    from foundry_client.manager import Manager
    calls, events = [], []
    owned = SimpleNamespace(p=SimpleNamespace(pid=4097, poll=lambda: None), adopted=False,
                            pending_restart="", spec=SimpleNamespace(owner_pid=None))
    me = SimpleNamespace(procs={"opencode": owned},
                         cfg={"components": {"opencode": {"enabled": True}}},
                         restart=lambda name, force=False: calls.append((name, force)) or {
                             "result": "waiting for a safe point"},
                         _event=lambda n, t: events.append((n, t)))
    assert Manager._opencode_pid(me) == 4097 and Manager._opencode_pending(me) is False
    assert Manager._restart_opencode_for_memory(me, "OpenCode is using 8 GB") == \
        "waiting for a safe point"
    assert calls == [("opencode", False)] and events[0][0] == "opencode"
    me.cfg["components"]["opencode"]["enabled"] = False
    assert "disabled" in Manager._restart_opencode_for_memory(me, "x") and len(calls) == 1
