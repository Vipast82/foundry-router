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
        self.health = 200                   # what the agent's /api/lane/health answers
        self.probes = []
        self.popen_kw = []
        self.ssh_says = b""                 # what ssh writes to its stderr before it exits
        import tempfile
        self.logs = tempfile.mkdtemp(prefix="dff-tun-")

    def run(self, argv, **kw):
        self.runs.append(argv)
        out = json.dumps(self.secrets).encode() if isinstance(self.secrets, dict) else self.secrets
        return subprocess.CompletedProcess(argv, self.rc, out, self.stderr)

    def popen(self, argv, **kw):
        self.popens.append(argv)
        self.popen_kw.append(kw)
        p = Proc()
        self.procs.append(p)
        if self.ssh_says:
            kw["stderr"].write(self.ssh_says)
            kw["stderr"].flush()
            p.rc = 255
            return p
        # the forwarded local ports open once ssh runs
        for i, a in enumerate(argv):
            if a == "-L":
                self.open_ports.add(int(argv[i + 1].split(":")[1]))
        return p

    def probe(self, url, token):
        self.probes.append((url, token))
        h = self.health
        return h(token) if callable(h) else h

    def manager(self):
        return T.TunnelManager(popen=self.popen, run=self.run, clock=lambda: self.now,
                               pick_port=lambda: next(self.ports),
                               is_open=lambda p: p in self.open_ports, sleep=lambda s: None,
                               probe=self.probe, log_dir=lambda: self.logs)


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
    # each forward is bound to this machine's loopback, never every address
    assert all(x.startswith("127.0.0.1:") for x in fwd)
    assert [x.split(":", 2)[2] for x in fwd] == ["127.0.0.1:47811", "127.0.0.1:4096",
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
    assert ["-L", "127.0.0.1:1:127.0.0.1:1111"] == argv[argv.index("-L"):argv.index("-L") + 2]
    assert "127.0.0.1:3:127.0.0.1:3333" in argv


def test_ensure_keeps_other_projects_tunnels(monkeypatch):
    seen = []
    monkeypatch.setattr(T.MANAGER, "configure", lambda slots, **kw: seen.append(dict(slots)))
    monkeypatch.setattr(T, "_BY_PROJECT", {})

    class Prof:
        def __init__(self, pid, slots):
            self.id, self.raw = pid, {"slots": slots, "automation": {"remote_lanes": True}}
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


# ---------------------------------------------------------------------------- review 2026-11-27

def test_no_tunnel_and_no_secrets_read_while_remote_lanes_is_off(monkeypatch):
    called = []
    monkeypatch.setattr(T.MANAGER, "configure", lambda *a, **k: called.append(a))
    monkeypatch.setattr(T, "_BY_PROJECT", {})

    class Prof:
        id = "au"
        raw = {"slots": {"slot-4": {"container": "c", "lanes": True}},
               "automation": {"remote_lanes": False}}
    assert T.lane_slots(Prof()) == {} and T.ensure(Prof()) == {} and called == []

    class NoTable:
        id, raw = "au", {"slots": {"slot-4": {"container": "c", "lanes": True}}}
    assert T.ensure(NoTable()) == {} and called == []          # off by default


def up(f):
    m = f.manager()
    m.configure({"slot-4": SLOT}, start_thread=False)
    m.poll()
    assert m.view()["slot-4"]["state"] == "up"
    return m


def test_the_health_probe_carries_the_token_and_is_throttled():
    f = Fake()
    m = up(f)
    m.poll()
    assert f.probes == []                                     # just started: no probe yet
    f.now += T.HEALTH_EVERY_S
    m.poll()
    assert f.probes == [("http://127.0.0.1:50001", "TOK")]
    f.now += 5
    m.poll()
    assert len(f.probes) == 1


def test_a_401_after_an_agent_restart_rereads_the_secrets_and_keeps_the_tunnel():
    f = Fake()
    m = up(f)
    f.secrets = {"token": "TOK2", "kilo_password": "KP2"}      # the agent restarted
    f.health = lambda token: 200 if token == "TOK2" else 401
    f.now += T.HEALTH_EVERY_S
    m.poll()
    assert m.view()["slot-4"]["state"] == "up" and len(f.popens) == 1
    assert m.endpoint("slot-4")["token"] == "TOK2"
    assert m.endpoint("slot-4", "kilo")["password"] == "KP2"
    assert len(f.runs) == 2                                    # the secrets read once more


def test_a_401_the_new_secrets_do_not_cure_restarts_the_tunnel():
    f = Fake()
    m = up(f)
    f.health = 401
    f.now += T.HEALTH_EVERY_S
    m.poll()
    v = m.view()["slot-4"]
    assert v["state"] == "backoff" and "refuses the token" in v["error"]
    assert m.endpoint("slot-4") is None and f.procs[0].rc == -15


def test_no_health_answer_twice_restarts_the_tunnel():
    f = Fake()
    m = up(f)
    f.health = 0
    f.now += T.HEALTH_EVERY_S
    m.poll()
    assert m.view()["slot-4"]["state"] == "up"                # one miss is a blip
    f.now += T.HEALTH_EVERY_S
    m.poll()
    v = m.view()["slot-4"]
    assert v["state"] == "backoff" and "did not answer its health check" in v["error"]


def test_a_lane_call_refused_with_401_refreshes_once():
    f = Fake()
    m = up(f)
    assert m.refresh("slot-4", "TOK") is False                 # same secrets: nothing new
    f.now += T.REFRESH_GAP_S
    f.secrets = {"token": "TOK2"}
    assert m.refresh("slot-4", "TOK") is True and m.endpoint("slot-4")["token"] == "TOK2"
    # a second caller that also used the old token: already refreshed, no ssh read
    n = len(f.runs)
    assert m.refresh("slot-4", "TOK") is True and len(f.runs) == n
    # a new 401 with the new token inside the gap: no re-read
    assert m.refresh("slot-4", "TOK2") is False and len(f.runs) == n
    assert m.refresh("slot-9", "x") is False


def test_ssh_stderr_goes_to_a_log_file_and_still_names_the_startup_error():
    import os
    f = Fake()
    f.ssh_says = b"studio@10.0.0.9: Permission denied (publickey).\n"
    m = f.manager()
    m.configure({"slot-4": SLOT}, start_thread=False)
    m.poll()
    kw = f.popen_kw[0]
    assert kw["stderr"] is not subprocess.PIPE and hasattr(kw["stderr"], "write")
    v = m.view()["slot-4"]
    assert v["state"] == "backoff" and "Permission denied (publickey)" in v["error"]
    assert os.path.isfile(os.path.join(f.logs, "lane-tunnel-slot-4.log"))


def test_two_projects_share_a_slot_only_with_the_same_tunnel_settings(monkeypatch):
    seen = []
    monkeypatch.setattr(T.MANAGER, "configure", lambda slots, **kw: seen.append(dict(slots)))
    monkeypatch.setattr(T, "_BY_PROJECT", {})
    monkeypatch.setattr(T, "_CONFLICTS", {})

    class Prof:
        def __init__(self, pid, slot):
            self.id = pid
            self.raw = {"slots": {"slot-4": slot}, "automation": {"remote_lanes": True}}
    a = Prof("a", {"container": "c", "lanes": True, "host": "u@10.0.0.9"})
    same = Prof("b", {"container": "c", "lanes": True, "host": "u@10.0.0.9"})
    other = Prof("c", {"container": "c", "lanes": True, "host": "u@10.0.0.77"})
    assert "slot-4" in T.ensure(a) and "slot-4" in T.ensure(same)
    assert T.conflict("b", "slot-4") == ""
    assert T.ensure(other) == {}
    why = T.conflict("c", "slot-4")
    assert "project a" in why and "host" in why
    assert seen[-1]["slot-4"]["host"] == "u@10.0.0.9"           # a's tunnel is kept
