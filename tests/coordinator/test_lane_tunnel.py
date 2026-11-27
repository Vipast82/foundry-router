"""Lane tunnels (foundry_coordinator/lane_tunnel.py), Distributed Foundry phase b B3: one ssh
-L per lane slot, the lane secrets read once over ssh and kept in memory, backoff, restart."""

from __future__ import annotations

import json
import subprocess

import pytest

from foundry_coordinator import lane_tunnel as T

SLOT = {"container": "studio-slot", "host": "studio@10.0.0.9", "lanes": True,
        "ssh": ["ssh", "-o", "BatchMode=yes"]}


class Proc:
    def __init__(self):
        self.rc = None
        self.stderr = None
        self.killed = False

    def poll(self):
        return self.rc

    def terminate(self):
        self.rc = -15

    def kill(self):
        self.rc = -9

    def wait(self, timeout=None):
        return self.rc


class Fake:
    def __init__(self, secrets=None, rc=0, stderr=b""):
        self.secrets = secrets if secrets is not None else {"token": "TOK", "kilo_password": "KP",
                                                            "opencode_password": "OP"}
        self.rc, self.stderr = rc, stderr
        self.runs, self.popens, self.procs = [], [], []
        self.open_ports = set()
        self.ports = iter(range(50001, 50100))
        self.now = 1000.0

    def run(self, argv, **kw):
        self.runs.append(argv)
        out = json.dumps(self.secrets).encode() if isinstance(self.secrets, dict) else self.secrets
        return subprocess.CompletedProcess(argv, self.rc, out, self.stderr)

    def popen(self, argv, **kw):
        self.popens.append(argv)
        p = Proc()
        self.procs.append(p)
        # the forwarded local ports open once ssh runs
        for i, a in enumerate(argv):
            if a == "-L":
                self.open_ports.add(int(argv[i + 1].split(":")[0]))
        return p

    def manager(self):
        return T.TunnelManager(popen=self.popen, run=self.run, clock=lambda: self.now,
                               pick_port=lambda: next(self.ports),
                               is_open=lambda p: p in self.open_ports, sleep=lambda s: None)


def test_tunnel_starts_reads_secrets_once_and_answers_endpoints():
    f = Fake()
    m = f.manager()
    m.configure({"slot-4": SLOT}, start_thread=False)
    m.poll()
    assert m.view()["slot-4"]["state"] == "up"
    argv = f.popens[0]
    assert argv[0] == "ssh" and argv[-1] == "studio@10.0.0.9" and "-N" in argv
    assert "ExitOnForwardFailure=yes" in argv
    fwd = [argv[i + 1] for i, a in enumerate(argv) if a == "-L"]
    assert [x.split(":", 1)[1] for x in fwd] == ["127.0.0.1:47811", "127.0.0.1:4096",
                                                 "127.0.0.1:4097"]
    # the secrets come from inside the container, over ssh + docker exec
    assert f.runs[0][-2:] == ["studio@10.0.0.9",
                              "docker exec studio-slot cat /run/secrets/lane/lane.json"]
    ep = m.endpoint("slot-4")
    assert ep == {"url": "http://127.0.0.1:50001", "token": "TOK"}
    assert m.endpoint("slot-4", "kilo") == {"url": "http://127.0.0.1:50002", "password": "KP"}
    assert m.endpoint("slot-4", "opencode")["password"] == "OP"
    m.poll()
    assert len(f.runs) == 1 and len(f.popens) == 1          # nothing redone while up


def test_view_and_errors_never_carry_secrets():
    f = Fake()
    m = f.manager()
    m.configure({"slot-4": SLOT}, start_thread=False)
    m.poll()
    assert "TOK" not in json.dumps(m.view()) and "KP" not in json.dumps(m.view())


def test_a_dead_tunnel_backs_off_then_comes_back_on_new_ports_with_a_new_token():
    f = Fake()
    m = f.manager()
    m.configure({"slot-4": SLOT}, start_thread=False)
    m.poll()
    f.procs[0].rc = 255                                       # ssh exited
    f.open_ports.clear()
    m.poll()
    v = m.view()["slot-4"]
    assert v["state"] == "backoff" and "ssh tunnel exited" in v["error"]
    assert m.endpoint("slot-4") is None
    m.poll()
    assert len(f.popens) == 1                                 # still waiting out the backoff
    f.now += 6
    f.secrets = {"token": "TOK2"}
    m.poll()
    assert m.view()["slot-4"]["state"] == "up"
    assert m.endpoint("slot-4") == {"url": "http://127.0.0.1:50004", "token": "TOK2"}


def test_backoff_grows():
    f = Fake(rc=1, stderr=b"ssh: connect to host 10.0.0.9 port 22: No route to host")
    m = f.manager()
    m.configure({"slot-4": SLOT}, start_thread=False)
    waits = []
    for _ in range(4):
        m.poll()
        t = m._tunnels["slot-4"]
        waits.append(t.next_try - t.since)
        f.now = t.next_try
    assert waits == [5, 10, 20, 40]
    assert "No route to host" in m.view()["slot-4"]["error"]
    assert f.popens == []                                     # secrets first: no tunnel


@pytest.mark.parametrize("secrets,why", [(b"not json", "not JSON"), ({}, "no token")])
def test_bad_secrets_files(secrets, why):
    f = Fake(secrets=secrets)
    m = f.manager()
    m.configure({"slot-4": SLOT}, start_thread=False)
    m.poll()
    assert why in m.view()["slot-4"]["error"]


def test_a_failed_read_reports_stderr_never_stdout():
    f = Fake(secrets=b'{"token": "half-a-secr', rc=1, stderr=b"cat: no such file")
    m = f.manager()
    m.configure({"slot-4": SLOT}, start_thread=False)
    m.poll()
    err = m.view()["slot-4"]["error"]
    assert "no such file" in err and "half-a-secr" not in err


def test_a_local_slot_needs_no_ssh():
    f = Fake()
    m = f.manager()
    f.open_ports.add(47811)
    m.configure({"slot-l": {"container": "c", "lanes": True}}, start_thread=False)
    m.poll()
    assert f.popens == [] and f.runs[0][:3] == ["docker", "exec", "c"]
    assert m.endpoint("slot-l") == {"url": "http://127.0.0.1:47811", "token": "TOK"}


def test_a_slot_without_a_container_reads_on_the_host():
    argv = T.secrets_argv({"host": "u@h", "ssh": ["ssh"], "lanes_secrets": "C:/Foundry/lane.json"})
    assert argv[-2] == "u@h" and argv[-1] == "cat C:/Foundry/lane.json"


def test_configure_closes_removed_and_changed_slots():
    f = Fake()
    m = f.manager()
    m.configure({"slot-4": SLOT}, start_thread=False)
    m.poll()
    m.configure({}, start_thread=False)
    assert f.procs[0].rc == -15 and m.view() == {}


def test_ports_come_from_the_slot_settings():
    s = {**SLOT, "agent_port": 1111, "kilo_port": 2222, "opencode_port": 3333}
    argv = T.tunnel_argv(s, {"agent": 1, "kilo": 2, "opencode": 3})
    assert ["-L", "1:127.0.0.1:1111"] == argv[argv.index("-L"):argv.index("-L") + 2]
    assert "3:127.0.0.1:3333" in argv


def test_ensure_keeps_other_projects_tunnels(monkeypatch):
    seen = []
    monkeypatch.setattr(T.MANAGER, "configure", lambda slots, **kw: seen.append(dict(slots)))
    monkeypatch.setattr(T, "_BY_PROJECT", {})

    class Prof:
        def __init__(self, pid, slots):
            self.id, self.raw = pid, {"slots": slots}
    a = Prof("a", {"slot-4": {"container": "c", "lanes": True}})
    b = Prof("b", {"slot-5": {"container": "c", "lanes": False}})
    T.ensure(a)
    T.ensure(b)                                              # b has none: nothing changes
    assert seen == [{"slot-4": T.SL.configured(a)["slot-4"]}]
    T.ensure(Prof("a", {}))                                  # a drops its lane slot
    assert seen[-1] == {}


def test_no_lane_slot_starts_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(T.MANAGER, "configure", lambda *a, **k: called.append(a))
    monkeypatch.setattr(T, "_BY_PROJECT", {})

    class Prof:
        id, raw = "au", {"slots": {"slot-1": {"container": "studio-slot-poc"}}}
    assert T.ensure(Prof()) == {} and called == []
