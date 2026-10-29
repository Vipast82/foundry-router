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
