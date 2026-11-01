"""Studio slot ssh without pasting keys (v0.162.0): the main PC registers its own key with the
router, slot hosts report their host keys and refresh the keys they allow, and the coordinator
checks each slot strictly against Foundry's own known_hosts."""

import base64
import importlib.util
import json
import os
from pathlib import Path

from foundry_automation import document as D


def _pub(seed: int, kind: str = "ssh-ed25519", comment: str = "test") -> str:
    blob = (len(kind).to_bytes(4, "big") + kind.encode() + (32).to_bytes(4, "big")
            + bytes([seed]) * 32)
    return f"{kind} {base64.b64encode(blob).decode()} {comment}"


def _svc(app):
    return app.state.services


def _deployed(app, project="animaluprising"):
    d = D.empty(project)
    d["settings"]["roles"] = {"implementer": "Foundry-Kilo-Local"}
    _svc(app).automation._append(project, d, "test", "", "test")


def _key(client, name, scopes):
    r = client.post("/admin/api/setup/clients", json={"name": name, "scopes": scopes})
    assert r.status_code == 200, r.text
    return r.json()["key"]


def _slot(app, client):
    """An enrolled slot-2 on 192.168.0.52 -> its secrets key."""
    _deployed(app)
    code = client.post("/admin/api/machines", json={
        "kind": "slot", "project": "animaluprising", "slot": "slot-2", "ip": "192.168.0.52",
        "place_id": 111}).json()["code"]
    return client.post("/v1/foundry/enroll", json={"code": code}).json()["key"]


# ----------------------------------------------------------------------------- router

def test_the_main_pc_registers_its_key_and_slots_allow_it(app, client):
    slot_key = _slot(app, client)
    coord = {"Authorization": "Bearer " + _key(client, "coordinator-test", ["orchestration"])}
    ws = _pub(1, comment="foundry-coordinator@PC1")
    r = client.post("/v1/foundry/control/workstation-ssh", json={"name": "PC1", "public_key": ws},
                    headers=coord)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "new" and out["fingerprint"].startswith("SHA256:")
    assert out["slots"] == [{"machine": "slot-2", "project": "animaluprising", "slot": "slot-2",
                             "host": "192.168.0.52", "host_keys": []}]
    again = client.post("/v1/foundry/control/workstation-ssh",
                        json={"name": "PC1", "public_key": ws}, headers=coord).json()
    assert again["status"] == "seen"
    # the slot host allows it (plus the extra keys typed on Machines)
    extra = _pub(9, comment="victor-manual")
    client.post("/admin/api/machines/settings", json={"slot_ssh_keys": extra})
    s = {"Authorization": "Bearer " + slot_key}
    keys = client.get("/v1/foundry/secrets/authorized-keys", headers=s).json()["keys"]
    assert keys == [ws, extra]
    # a new main PC: both allowed; a replaced key: the old one is gone
    client.post("/v1/foundry/control/workstation-ssh",
                json={"name": "PC2", "public_key": _pub(2)}, headers=coord)
    assert client.post("/v1/foundry/control/workstation-ssh",
                       json={"name": "PC1", "public_key": _pub(3)}, headers=coord
                       ).json()["status"] == "changed"
    keys = client.get("/v1/foundry/secrets/authorized-keys", headers=s).json()["keys"]
    assert _pub(3, comment="test") in keys and ws not in keys and _pub(2) in keys
    listing = client.get("/admin/api/machines").json()
    assert sorted(w["name"] for w in listing["workstations"]) == ["PC1", "PC2"]
    # removed on Machines -> slots drop it at their next refresh
    assert client.delete("/admin/api/machines/workstations/PC2").status_code == 200
    assert _pub(2) not in client.get("/v1/foundry/secrets/authorized-keys", headers=s).json()["keys"]
    assert client.delete("/admin/api/machines/workstations/PC2").status_code == 404


def test_registration_needs_the_coordinator_key_and_a_public_key(app, client):
    _slot(app, client)
    body = {"name": "PC1", "public_key": _pub(1)}
    infer = {"Authorization": "Bearer " + _key(client, "chat-test", ["inference"])}
    assert client.post("/v1/foundry/control/workstation-ssh", json=body, headers=infer
                       ).status_code == 403
    coord = {"Authorization": "Bearer " + _key(client, "coordinator-test", ["orchestration"])}
    bad = {"name": "PC1", "public_key": "-----BEGIN OPENSSH PRIVATE KEY----- abc"}
    assert client.post("/v1/foundry/control/workstation-ssh", json=bad, headers=coord
                       ).status_code == 400
    assert client.post("/v1/foundry/control/workstation-ssh",
                       json={"name": "bad name!", "public_key": _pub(1)}, headers=coord
                       ).status_code == 400


def test_slot_reports_host_keys_and_the_coordinator_gets_them(app, client):
    slot_key = _slot(app, client)
    s = {"Authorization": "Bearer " + slot_key}
    hk = [_pub(5, comment="root@foundry-slot-2"), _pub(6, "ssh-rsa", "root@foundry-slot-2")]
    r = client.post("/v1/foundry/secrets/host-keys", json={"keys": hk}, headers=s)
    assert r.status_code == 200, r.text
    ev = [e["event"] for e in _svc(app).machines.events(10)]
    assert "host_keys" in ev
    m = client.get("/admin/api/machines").json()["machines"][0]
    assert {k["type"] for k in m["host_keys"]} == {"ssh-ed25519", "ssh-rsa"}
    coord = {"Authorization": "Bearer " + _key(client, "coordinator-test", ["orchestration"])}
    slots = client.post("/v1/foundry/control/workstation-ssh",
                        json={"name": "PC1", "public_key": _pub(1)}, headers=coord).json()["slots"]
    # comments dropped: "type blob" only
    assert sorted(slots[0]["host_keys"]) == sorted(" ".join(k.split()[:2]) for k in hk)
    # only an enrolled slot's own key may report or pull
    other = {"Authorization": "Bearer " + _key(client, "other-secrets", ["secrets"])}
    assert client.post("/v1/foundry/secrets/host-keys", json={"keys": hk}, headers=other
                       ).status_code == 403
    assert client.get("/v1/foundry/secrets/authorized-keys", headers=other).status_code == 403
    assert client.post("/v1/foundry/secrets/host-keys", json={"keys": ["nope"]}, headers=s
                       ).status_code == 400


def test_enrollment_hands_out_the_registered_keys(app, client):
    _deployed(app)
    coord = {"Authorization": "Bearer " + _key(client, "coordinator-test", ["orchestration"])}
    client.post("/v1/foundry/control/workstation-ssh",
                json={"name": "PC1", "public_key": _pub(1)}, headers=coord)
    code = client.post("/admin/api/machines", json={
        "kind": "slot", "project": "animaluprising", "slot": "slot-3", "ip": "192.168.0.53"}
    ).json()["code"]
    e = client.post("/v1/foundry/enroll", json={"code": code}).json()
    assert e["ssh_authorized_keys"] == [_pub(1)]


# ----------------------------------------------------------------------------- coordinator

def test_ssh_options_are_strict_once_the_router_knows_the_host_key(tmp_path):
    from foundry_coordinator import slot_ssh, slots
    slot_ssh.configure(str(tmp_path))
    try:
        opts = slot_ssh.ssh_options("studio@192.168.0.52")
        assert "StrictHostKeyChecking=accept-new" in opts and "-i" not in opts
        os.makedirs(slot_ssh.ssh_dir(), exist_ok=True)
        Path(slot_ssh.key_path()).write_text("private", encoding="utf-8")
        n = slot_ssh.write_known_hosts([{"host": "192.168.0.52", "host_keys": [_pub(5)]},
                                        {"host": "bad host", "host_keys": [_pub(6)]}])
        assert n == 1
        opts = slot_ssh.ssh_options("studio@192.168.0.52")
        assert "StrictHostKeyChecking=yes" in opts and "-i" in opts
        assert any(o.startswith("UserKnownHostsFile=") and o.endswith("ssh/known_hosts")
                   for o in opts)
        assert "\\" not in " ".join(opts)
        # another slot the router has no host key for: trust on first use, as before
        assert "StrictHostKeyChecking=accept-new" in slot_ssh.ssh_options("studio@192.168.0.60")
        # slots.ssh_cmd uses them for a slot with the default ssh; a custom ssh stays as it is
        slot = {**slots.DEFAULTS, "host": "studio@192.168.0.52", "container": "studio-slot"}
        cmd = slots.ssh_cmd(slot)
        assert cmd[0] == "ssh" and "StrictHostKeyChecking=yes" in cmd
        assert slots.host_argv(slot, ["docker", "ps"])[-2:] == ["studio@192.168.0.52", "docker ps"]
        assert slots.ssh_cmd({**slot, "ssh": ["ssh", "-p", "2222"]}) == ["ssh", "-p", "2222"]
        env = slots.git_ssh_env(slot)
        assert "StrictHostKeyChecking=yes" in env["GIT_SSH_COMMAND"]
    finally:
        slot_ssh._dir = None


class _FakeRouter:
    def __init__(self, answer=None, fail=None):
        self.answer, self.fail, self.calls = answer, fail, []

    def _req(self, method, path, body=None, timeout=None):
        self.calls.append((method, path, body))
        if self.fail:
            raise self.fail
        return self.answer, {}


class _P:
    def __init__(self, foundry):
        self.foundry = foundry


class _C:
    def __init__(self, state_dir, foundry):
        self.state_dir = str(state_dir)
        self.projects = {"animaluprising": _P(foundry)}


def test_sync_registers_writes_known_hosts_and_state(tmp_path, monkeypatch):
    from foundry_coordinator import slot_ssh
    monkeypatch.setattr(slot_ssh, "ensure_key", lambda name="": _pub(1, comment="fc@PC1"))
    logs = []
    router = _FakeRouter({"status": "new", "fingerprint": "SHA256:abc",
                          "slots": [{"host": "192.168.0.115", "host_keys": [_pub(5)]}]})
    try:
        s = slot_ssh.SlotSsh(_C(tmp_path, router), log=logs.append, clock=lambda: 1000.0,
                             name="PC1")
        st = s.sync()
        assert st["ok"] and st["host_keys"] == 1 and st["fingerprint"] == "SHA256:abc"
        assert router.calls[0][1] == "/v1/foundry/control/workstation-ssh"
        assert router.calls[0][2] == {"name": "PC1", "public_key": _pub(1, comment="fc@PC1")}
        assert "192.168.0.115" in slot_ssh.known_hosts()
        assert json.loads(Path(slot_ssh.state_path()).read_text())["ok"] is True
        assert any("registered as PC1" in m for m in logs)
        # the router down: the known host keys stay, the error is logged once
        from foundry_coordinator.adapters.foundry import FoundryError
        s2 = slot_ssh.SlotSsh(_C(tmp_path, _FakeRouter(fail=FoundryError("down"))),
                              log=logs.append, clock=lambda: 2000.0, name="PC1")
        st = s2.sync()
        st = s2.sync()
        assert st["ok"] is False and "down" in st["error"]
        assert sum("could not register" in m for m in logs) == 1
        assert "192.168.0.115" in slot_ssh.known_hosts()
        assert st["fingerprint"] == "SHA256:abc"           # the last good one is kept
    finally:
        slot_ssh._dir = None


def test_poll_runs_at_start_then_every_interval(tmp_path, monkeypatch):
    from foundry_coordinator import slot_ssh
    now = [5000.0]
    s = slot_ssh.SlotSsh(_C(tmp_path, None), clock=lambda: now[0], name="PC1")
    runs = []
    monkeypatch.setattr(s, "sync", lambda: runs.append(now[0]))
    try:
        s.poll()
        s.poll()
        now[0] += slot_ssh.INTERVAL
        s.poll()
        assert runs == [5000.0, 5000.0 + slot_ssh.INTERVAL]
    finally:
        slot_ssh._dir = None


# ----------------------------------------------------------------------------- slot host

def _slot_keys():
    p = Path(__file__).resolve().parents[1] / "contrib" / "studio-slot" / "proxmox" / "slot-keys.py"
    spec = importlib.util.spec_from_file_location("slot_keys", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_slot_keys_rewrites_only_its_own_block():
    sk = _slot_keys()
    mine = _pub(9, comment="added-by-hand")
    text = mine + "\n"
    out = sk.merge_block(text, [_pub(1), _pub(2)])
    assert out.startswith(mine + "\n\n" + sk.START) and _pub(2) in out
    out2 = sk.merge_block(out, [_pub(3)])
    assert mine in out2 and _pub(3) in out2 and _pub(1) not in out2
    assert out2.count(sk.START) == 1
    assert sk.merge_block(out2, [_pub(3)]) == out2              # nothing to change
    assert sk.merge_block("", []) == sk.START + "\n" + sk.END + "\n"


def test_installer_sets_up_the_key_refresh():
    root = Path(__file__).resolve().parents[1] / "contrib" / "studio-slot" / "proxmox"
    setup = (root / "setup-host.sh").read_text(encoding="utf-8")
    assert "/usr/local/sbin/foundry-slot-keys --router" in setup
    assert "systemctl enable --now foundry-slot-keys.timer" in setup
    assert "OnUnitActiveSec=5min" in setup
    # Foundry is downloaded (step 3) before the ssh step uses its slot-keys.py (step 4)
    assert setup.index('bold "3/6 Foundry') < setup.index('bold "4/6 ssh access')


def test_personal_keys_stay_usable_after_foundrys_own(tmp_path, monkeypatch):
    """-i turns ssh's default keys off, so the personal ones are listed after Foundry's: a slot
    installed before v0.162.0 only allows those until its key refresh runs."""
    from foundry_coordinator import slot_ssh
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("personal", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    slot_ssh.configure(str(tmp_path / "state"))
    try:
        os.makedirs(slot_ssh.ssh_dir(), exist_ok=True)
        Path(slot_ssh.key_path()).write_text("foundry", encoding="utf-8")
        opts = slot_ssh.ssh_options("studio@192.168.0.52")
        ids = [opts[i + 1] for i, o in enumerate(opts) if o == "-i"]
        assert len(ids) == 2 and ids[0].endswith("ssh/id_ed25519") and "state" in ids[0]
        assert ids[1].endswith(".ssh/id_ed25519") and "state" not in ids[1]
    finally:
        slot_ssh._dir = None
