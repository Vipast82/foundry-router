"""Safety for every client test: this workstation runs a live Kilo, Rojo and coordinator.
No test may look up a real port owner or kill a real process; tests that need either
install their own stand-ins."""
import pytest

from foundry_client import components as C
from foundry_client import manager as M


@pytest.fixture(autouse=True)
def _never_touch_real_processes(monkeypatch):
    monkeypatch.setattr(C, "port_owner_pid", lambda port: None)
    monkeypatch.setattr(C, "pid_alive", lambda pid: False)
    monkeypatch.setattr(C, "port_open", lambda port, **kw: False)
    M.REAL_KILL_PID = M.Manager.__dict__["_kill_pid"]        # the staticmethod object
    monkeypatch.setattr(M.Manager, "_kill_pid", staticmethod(lambda pid, p, tree=True: None))


@pytest.fixture(autouse=True)
def _never_probe_the_real_network(monkeypatch):
    """MCP / service probing opens sockets (the audio MCPs live on the LAN) and reads the real
    global Kilo config; tests get neither unless they pass their own."""
    from foundry_automation import service_map as SM
    monkeypatch.setattr(SM, "port_open", lambda host, port, timeout=1.0: False)
    monkeypatch.setattr(SM, "global_kilo_paths", lambda: [])
