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
    monkeypatch.setattr(M.Manager, "_kill_pid", staticmethod(lambda pid, p: None))
