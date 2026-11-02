"""One active workstation per project (LINUX_WORKSTATION_PLAN W4): workstation_gate.py and the
engine skipping a project this coordinator does not hold."""

from types import SimpleNamespace

from foundry_coordinator.workstation_gate import WorkstationGate


class FakeRouter:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def _req(self, method, path, body=None, timeout=None):
        self.calls.append((method, path, body))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer, {}


class FakeStore:
    def __init__(self):
        self.events = []

    def event(self, pid, task, kind, detail=None):
        self.events.append((pid, kind, detail))


def _coord(answer, raw=None):
    P = SimpleNamespace(profile=SimpleNamespace(raw=raw or {}), foundry=FakeRouter(answer))
    return SimpleNamespace(projects={"au": P}, store=FakeStore()), P


def test_another_holder_puts_the_project_on_standby_and_back():
    c, P = _coord({"active": False, "holder": {"client": "ws-linux", "name": "LINUX"}})
    logs = []
    g = WorkstationGate(c, log=logs.append, name="PC")
    assert g.check() == {"au": "standby"}
    assert g.on_standby("au")["name"] == "LINUX"
    assert P.foundry.calls == [("POST", "/v1/foundry/control/workstation-active",
                                {"project": "au", "name": "PC"})]
    assert "STANDBY" in logs[0] and c.store.events[0][1] == "workstation_standby"
    P.foundry.answer = {"active": True, "holder": {"client": "coord", "name": "PC"}}
    assert g.check() == {"au": "active"} and g.on_standby("au") is None
    assert c.store.events[-1][1] == "workstation_active"


def test_it_fails_open():
    for answer in (RuntimeError("404 not found"), {}, {"active": False}, None):
        c, _ = _coord(answer)
        g = WorkstationGate(c, name="PC")
        g.check()
        assert g.on_standby("au") is None, answer
    c, P = _coord({"active": False, "holder": {"client": "x"}})
    P.foundry = None
    g = WorkstationGate(c, name="PC")
    assert g.check() == {"au": "no router"} and g.on_standby("au") is None


def test_the_setting_turns_it_off_and_polling_is_rate_limited():
    c, P = _coord({"active": False, "holder": {"client": "x"}},
                  raw={"workspace": {"single_workstation": False}})
    g = WorkstationGate(c, name="PC")
    assert g.check() == {"au": "off"} and not P.foundry.calls
    t = [1000.0]
    c2, P2 = _coord({"active": True, "holder": {"client": "me"}})
    g2 = WorkstationGate(c2, name="PC", clock=lambda: t[0])
    g2.poll()
    g2.poll()
    assert len(P2.foundry.calls) == 1
    t[0] += 61
    g2.poll()
    assert len(P2.foundry.calls) == 2


def test_the_engine_skips_a_project_on_standby(tmp_path):
    import inspect
    from foundry_coordinator import engine
    src = inspect.getsource(engine.Coordinator.tick)
    assert "workstation_gate" in src and "on_standby(pid)" in src
