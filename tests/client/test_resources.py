"""Resource watch (2026-10-29): a stuck training run held 32 GB for 30 h unnoticed. The client
watches memory and the development / testing jobs; it stops only those, warns about the rest."""

from types import SimpleNamespace

from foundry_client import resources as R

GB = 1024 ** 3


class FakeProc:
    def __init__(self, pid, name, cmd, gb=0.1, ppid=1, started=0.0, children=()):
        self.pid, self._name, self._cmd, self._gb = pid, name, cmd, gb
        self._ppid, self._started, self._children = ppid, started, list(children)
        self.terminated = self.killed = False

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmd.split()

    def ppid(self):
        return self._ppid

    def create_time(self):
        return self._started

    def children(self, recursive=False):
        return list(self._children)

    def memory_info(self):
        return SimpleNamespace(private=int(self._gb * GB), rss=int(self._gb * GB))

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class FakePs:
    def __init__(self, procs, free_gb=10.0, total_gb=32.0):
        self.procs, self.free, self.total = {p.pid: p for p in procs}, free_gb, total_gb

    def process_iter(self):
        return [p for p in self.procs.values() if not p.terminated]

    def virtual_memory(self):
        return SimpleNamespace(available=self.free * GB, total=self.total * GB)

    def Process(self, pid):
        return self.procs[pid]

    def wait_procs(self, procs, timeout=0):
        return [p for p in procs if p.terminated], [p for p in procs if not p.terminated]

    def pid_exists(self, pid):
        return pid in self.procs and not self.procs[pid].terminated


def _watch(procs, settings=None, free=10.0, now=10_000.0):
    alerts, events = {}, []
    ps = FakePs(procs, free_gb=free)
    w = R.ResourceWatch(lambda: settings or {},
                        alert=lambda k, t, level="warn": alerts.__setitem__(k, (t, level)),
                        clear_alert=lambda k: alerts.pop(k, None),
                        event=lambda c, t: events.append(t),
                        game=lambda: "C:/Roblox/AnimalUprising", clock=lambda: now, ps=ps)
    return w, ps, alerts, events


def test_classify_names_only_development_and_testing_jobs():
    c = R.classify
    assert c("python.exe", "python -m foundry_training --profiles x auto --spool y") == "training"
    assert c("python.exe", "python -m foundry_coordinator.studio_calls pipeline --repo r") == \
        "studio_check"
    assert c("pwsh.exe", "pwsh -nofile -file tools/gate.ps1 -sprint x") == "gate_ps1"
    assert c("lune.exe", "lune run tests/lune/run.luau") == "lune"
    assert c("blender.exe", "blender -b -p tools/blender/build.py") == "blender_build"
    assert c("rojo.exe", "rojo build default.project.json -o x.rbxl") == "rojo_build"
    assert c("python.exe", "python tools/run_s05.py") == "repo_script"
    # never touched: Studio, Blender's window, rojo serve, the components, apps
    for name, cmd in (("robloxstudiobeta.exe", "robloxstudiobeta"), ("blender.exe", "blender"),
                      ("rojo.exe", "rojo serve"), ("python.exe", "python -m foundry_client run"),
                      ("python.exe", "python -m foundry_client coordinator --state s"),
                      ("kilo.exe", "kilo serve"), ("msedge.exe", "msedge")):
        assert c(name, cmd) is None, (name, cmd)


def test_a_runaway_training_run_is_stopped_with_its_tree_after_two_checks():
    child = FakeProc(11, "python.exe", "python worker", gb=7.0, ppid=10)
    shim = FakeProc(9, "python.exe", "python -m foundry_training auto --spool s", gb=0.0, ppid=1,
                    started=9_900.0)
    real = FakeProc(10, "python.exe", "python -m foundry_training auto --spool s", gb=1.0,
                    ppid=9, started=9_900.0, children=[child])
    shim._children = [real, child]
    studio = FakeProc(20, "RobloxStudioBeta.exe", "RobloxStudioBeta", gb=12.0)
    w, ps, alerts, events = _watch([shim, real, child, studio])
    first = w.tick(w.cfg())
    assert [j["kind"] for j in first["jobs"]] == ["training"]   # the shim counts once
    assert not real.terminated                                   # one check over: not yet
    w.tick(w.cfg())
    assert shim.terminated and real.terminated and child.terminated
    assert not studio.terminated                                 # never touched
    assert any(k.startswith("resources:runaway:") for k in alerts) and events


def test_a_job_over_its_time_limit_is_stopped_and_auto_stop_off_only_warns():
    gate = FakeProc(30, "pwsh.exe", "pwsh -file tools/gate.ps1 -sprint x", gb=0.2,
                    started=10_000.0 - 200 * 60)
    w, ps, alerts, _ = _watch([gate], settings={"auto_stop": False})
    w.tick(w.cfg())
    assert not gate.terminated and "Auto-stop is off" in alerts["resources:runaway:30"][0]
    w, ps, alerts, _ = _watch([gate])
    w.tick(w.cfg())
    assert gate.terminated


def test_low_ram_warns_with_the_biggest_users_and_touches_nothing():
    studio = FakeProc(20, "RobloxStudioBeta.exe", "RobloxStudioBeta", gb=12.0)
    w, ps, alerts, _ = _watch([studio], free=1.0)
    w.tick(w.cfg())
    assert "RAM low: 1.0 GB free" in alerts["resources:low_ram"][0]
    assert "RobloxStudioBeta.exe 12.0 GB" in alerts["resources:low_ram"][0]
    assert not studio.terminated
    ps.free = 5.0
    w.tick(w.cfg())
    assert "resources:low_ram" not in alerts


def test_protected_component_pids_are_never_jobs():
    tr = FakeProc(40, "python.exe", "python -m foundry_training auto", gb=50.0, started=0.0)
    w, ps, alerts, _ = _watch([tr])
    w.protected = lambda: {40}
    w.tick(w.cfg())
    w.tick(w.cfg())
    assert not tr.terminated


def test_settings_validate():
    assert R.validate({**R.DEFAULTS}) == []
    assert R.validate({**R.DEFAULTS, "low_ram_gb": -1, "auto_stop": "yes"})


# ------------------------------------------------------------------ Kilo's memory (item 6 of
# COORDINATOR_AUTOMATION_NEEDS): Kilo (Bun) grows to 7-10 GB within hours and takes the
# workstation below 2 GB free

class Now:
    def __init__(self, t=10_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _kilo_watch(kilo_gb=7.5, child_gb=1.0, free=3.0, settings=None, kilo_pid=77):
    child = FakeProc(78, "bun.exe", "bun worker", gb=child_gb, ppid=77)
    kilo = FakeProc(77, "bun.exe", "bun kilo serve --port 4096", gb=kilo_gb, children=[child])
    ps = FakePs([kilo, child, FakeProc(20, "RobloxStudioBeta.exe", "RobloxStudioBeta", gb=4.0)],
                free_gb=free)
    st = {"alerts": {}, "events": [], "asked": [], "pending": False, "kilo": kilo, "ps": ps,
          "now": Now()}

    def restart(why):
        st["asked"].append(why)
        st["pending"] = True
        return "waiting for a safe point: 2 session(s) working"
    w = R.ResourceWatch(lambda: settings or {},
                        alert=lambda k, t, level="warn": st["alerts"].__setitem__(k, (t, level)),
                        clear_alert=lambda k: st["alerts"].pop(k, None),
                        event=lambda c, t: st["events"].append(t),
                        game=lambda: "C:/Roblox/AnimalUprising", clock=st["now"], ps=ps,
                        protected=lambda: {77}, kilo_pid=lambda: kilo_pid,
                        kilo_pending=lambda: st["pending"], restart_kilo=restart)
    return w, st


def test_replay_kilo_over_its_limit_with_ram_low_gets_a_safe_point_restart():
    w, st = _kilo_watch()
    last = w.tick(w.cfg())
    assert last["kilo_gb"] == 8.5 and last["kilo"]["limit_gb"] == 6          # its whole tree
    assert st["asked"] == []                                 # one check is not enough
    last = w.tick(w.cfg())
    assert st["asked"] == ["Kilo is using 8.5 GB (limit 6 GB) with 3.0 GB of RAM free "
                           "(under 4 GB)"]
    text, level = st["alerts"]["resources:kilo_memory"]
    assert level == "info" and "restarts at the next safe point" in text
    assert "the coordinator holds new session starts until then" in text
    assert "asked for a Kilo restart at the next safe point (waiting for a safe point" in \
        st["events"][-1]
    assert last["kilo"]["pending"] is True
    assert not st["kilo"].terminated and not st["kilo"].killed       # never stopped by the watch
    # while it is pending nothing is asked again, however often it is checked
    for _ in range(10):
        st["now"].t += 60
        w.tick(w.cfg())
    assert len(st["asked"]) == 1
    # it restarted: small again, the alert goes
    st["pending"], st["kilo"]._gb = False, 0.4
    st["kilo"]._children[0]._gb = 0.1
    last = w.tick(w.cfg())
    assert last["kilo_gb"] == 0.5 and "resources:kilo_memory" not in st["alerts"]


def test_kilo_is_left_alone_when_ram_is_fine_it_is_small_or_the_rule_is_off():
    for kw in ({"free": 10.0},                               # big, but RAM is not low
               {"kilo_gb": 3.0, "child_gb": 0.5},           # RAM is low, Kilo is not why
               {"settings": {"kilo_restart_gb": 0}},        # switched off
               {"kilo_pid": None}):                          # no Kilo server here
        w, st = _kilo_watch(**kw)
        for _ in range(4):
            st["now"].t += 60
            w.tick(w.cfg())
        assert st["asked"] == [] and "resources:kilo_memory" not in st["alerts"], kw
    w, st = _kilo_watch(kilo_pid=None)
    assert w.tick(w.cfg())["kilo_gb"] is None
    # free RAM threshold 0: its own size is enough
    w, st = _kilo_watch(free=20.0, settings={"kilo_restart_free_gb": 0})
    w.tick(w.cfg())
    w.tick(w.cfg())
    assert st["asked"] == ["Kilo is using 8.5 GB (limit 6 GB) with 20.0 GB of RAM free"]


def test_one_kilo_restart_request_per_half_hour():
    w, st = _kilo_watch()
    w.tick(w.cfg())
    w.tick(w.cfg())
    assert len(st["asked"]) == 1
    st["pending"] = False                                    # it restarted and grew straight back
    for _ in range(20):
        st["now"].t += 60
        w.tick(w.cfg())
    assert len(st["asked"]) == 1
    st["now"].t += 15 * 60
    w.tick(w.cfg())
    assert len(st["asked"]) == 2


def test_the_kilo_settings_are_validated_defaulted_and_on_the_page():
    from pathlib import Path

    from foundry_client import config
    assert config.DEFAULTS["resources"]["kilo_restart_gb"] == 6
    assert config.DEFAULTS["resources"]["kilo_restart_free_gb"] == 4
    ok = {**R.DEFAULTS}
    assert R.validate(ok) == []
    assert R.validate({k: v for k, v in ok.items() if not k.startswith("kilo_")}) == []   # older file
    assert "resources.kilo_restart_gb must be a number, 0 or more" in R.validate(
        {**ok, "kilo_restart_gb": -1})
    assert "resources.kilo_restart_free_gb must be a number, 0 or more" in R.validate(
        {**ok, "kilo_restart_free_gb": "lots"})
    page = (Path(R.__file__).parent / "static" / "client.html").read_text(encoding="utf-8")
    assert '"resources.kilo_restart_gb"' in page and '"resources.kilo_restart_free_gb"' in page
    assert "rs.kilo.gb" in page and "restart pending" in page


def test_the_manager_asks_for_kilos_own_safe_restart_never_a_forced_one():
    from foundry_client.manager import Manager
    calls, events = [], []
    owned = SimpleNamespace(p=SimpleNamespace(pid=4096, poll=lambda: None), adopted=False,
                            pending_restart="", spec=SimpleNamespace(owner_pid=None))
    me = SimpleNamespace(procs={"kilo": owned},
                         cfg={"components": {"kilo": {"enabled": True}}},
                         restart=lambda name, force=False: calls.append((name, force)) or {
                             "result": "waiting for a safe point: 1 session(s) working"},
                         _event=lambda n, t: events.append((n, t)))
    assert Manager._kilo_pid(me) == 4096 and Manager._kilo_pending(me) is False
    assert Manager._restart_kilo_for_memory(me, "Kilo is using 8.5 GB") == \
        "waiting for a safe point: 1 session(s) working"
    assert calls == [("kilo", False)] and "resource watch" in events[0][1]
    owned.pending_restart = "safe"
    assert Manager._kilo_pending(me) is True
    # adopted (started by something else): the process that owns its port
    adopted = SimpleNamespace(p=None, adopted=True, pending_restart="",
                              spec=SimpleNamespace(owner_pid=lambda: 5150))
    assert Manager._kilo_pid(SimpleNamespace(procs={"kilo": adopted})) == 5150
    assert Manager._kilo_pid(SimpleNamespace(procs={})) is None
    me.cfg["components"]["kilo"]["enabled"] = False
    assert "disabled" in Manager._restart_kilo_for_memory(me, "x") and len(calls) == 1
